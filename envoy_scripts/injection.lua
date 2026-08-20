package.path = "/etc/envoy/scripts/?.lua;" .. package.path

local json = require("json")

-- Load honeytoken definitions once when the filter script loads (path is bind-mounted from docker-compose).
local file = io.open("/etc/envoy/config.json", "r")
local content = file:read("*a")
file:close()
local config = json.decode(content)

-- Per-token on/off switch (all kinds): active unless explicitly disabled. 1/on/true/yes = on;
-- anything else = off; absent = on (backward compatible).
local function token_enabled(token)
  local v = token.enabled
  if v == nil then return true end
  if v == true or v == 1 then return true end
  if type(v) == "string" then
    local s = v:lower()
    return s == "1" or s == "on" or s == "true" or s == "yes"
  end
  return false
end

-- "/*" matches every path, otherwise exact match against the request URI.
local function path_matches(paths, uri)
  if not paths then return false end
  for _, pattern in ipairs(paths) do
    if pattern == "/*" or pattern == uri then return true end
  end
  return false
end

-- Wall-clock microsecond timer via LuaJIT FFI gettimeofday, mirroring OpenResty's
-- get_micro_time. Replaces os.clock() (process CPU time, not elapsed wall-clock),
-- so Envoy+Lua measures the same quantity as the other three edges. A uniquely
-- named struct avoids clashing with any pre-existing `struct timeval` cdef.
local ffi = require("ffi")
ffi.cdef[[
  typedef long wadm_time_t;
  struct wadm_timeval { wadm_time_t tv_sec; long tv_usec; };
  int gettimeofday(struct wadm_timeval *tv, void *tz);
]]
local function get_micro_time()
  local tv = ffi.new("struct wadm_timeval")
  if ffi.C.gettimeofday(tv, nil) ~= 0 then
    return math.floor(os.time() * 1e6)  -- coarse fallback if the syscall fails
  end
  return tonumber(tv.tv_sec) * 1000000 + tonumber(tv.tv_usec)
end

-- POST-body inspection is a future feature; disabled unless the config flag is set,
-- so detection stays a query-string scan only (matching Apache/WASM).
local post_body_inspection = config.post_body_inspection

-- Decode query-string components the same way browsers send them (+ and %XX).
local function url_decode(str)
  str = str:gsub("+", " ")
  str = str:gsub("%%(%x%x)", function(h)
    return string.char(tonumber(h, 16))
  end)
  return str
end

-- Split "a=b&c=d" into a Lua table so we can remove individual parameters that contain trigger keywords.
local function parse_query_string(qs)
  local params = {}
  if not qs or qs == "" then
    return params
  end
  for pair in qs:gmatch("[^&]+") do
    local key, val = pair:match("^(.-)=(.*)$")
    if key then
      params[url_decode(key)] = url_decode(val)
    else
      params[url_decode(pair)] = ""
    end
  end
  return params
end

-- Re-encode keys/values when rebuilding :path or x-www-form-urlencoded bodies after stripping secrets.
local function url_encode(str)
  str = str:gsub("([^%w%-_.~])", function(c)
    return string.format("%%%02X", string.byte(c))
  end)
  return str
end

-- Turn the params table back into a single string for setBytes / replace :path.
local function rebuild_query_string(params)
  local parts = {}
  for k, v in pairs(params) do
    if v == "" then
      parts[#parts + 1] = url_encode(k)
    else
      parts[#parts + 1] = url_encode(k) .. "=" .. url_encode(v)
    end
  end
  return table.concat(parts, "&")
end

-- Collect all non-empty trigger_keyword values from config for request scanning.
local function get_trigger_keywords()
  local triggers = {}
  if not config.honeytokens or not config.honeytokens.html_comments then
    return triggers
  end
  for _, token in ipairs(config.honeytokens.html_comments) do
    if token_enabled(token) and token.trigger_keyword and token.trigger_keyword ~= "" then
      triggers[#triggers + 1] = token.trigger_keyword
    end
  end
  return triggers
end

-- Decide which HTML comment strings to inject for a given request URI (matches `/*` or exact path).
local function get_comments_for_path(uri)
  local to_inject = {}
  if not config.honeytokens or not config.honeytokens.html_comments then
    return to_inject
  end
  for _, token in ipairs(config.honeytokens.html_comments) do
    if token_enabled(token) and token.paths then
      for _, pattern in ipairs(token.paths) do
        if pattern == "/*" or pattern == uri then
          to_inject[#to_inject + 1] = token.comment_value
          break
        end
      end
    end
  end
  return to_inject
end

-- Fixed-anchor first-match splice, mirroring the WASM filter's `splice_before`: a *plain*
-- find (the `true` disables pattern matching, same call detection already uses) then two
-- subs. Returns nil when the anchor is absent so callers pick their own fallback.
--
-- This replaces `string.gsub`, which was costing 2-3x more for the same result: gsub runs
-- Lua's backtracking pattern matcher rather than an optimised substring search, builds the
-- output through a luaL_Buffer match loop, and re-concatenates the replacement on every
-- request. Using it for a fixed-string insert made the injection figure rank an
-- implementation choice rather than the runtime — see docs/EDGE_LEVELING.md.
local function splice_before(body, anchor, insert)
  local pos = body:find(anchor, 1, true)
  if not pos then return nil end
  return body:sub(1, pos - 1) .. insert .. "\n" .. body:sub(pos)
end

-- Per-stream dynamic-metadata namespace used to carry the request path from
-- envoy_on_request to envoy_on_response (see set/get below).
local WADM_META_FILTER = "wadm.honeypot"

-- In-memory attacker store (module scope → persists across requests on this worker's
-- Lua VM), mirroring OpenResty's ngx.shared.wadm_state. Replaces the previous
-- /tmp/detected_ips.json file store so the detection timer measures the same work as
-- the other edges — no per-request filesystem read/write or JSON (de)serialisation.
local detected_ips = {}

-- Record an attacker IP on detection (in-memory write, matching OpenResty's wadm:set).
local function record_attacker_ip(ip)
  if detected_ips[ip] then
    return
  end
  detected_ips[ip] = os.time()
end

-- Check whether we have seen this IP before (in-memory read; called outside the timed region).
local function is_known_attacker(ip)
  return detected_ips[ip] ~= nil
end

-- ── Fake SQL-injection trap (parity with OpenResty nginx/nginx.conf) ─────────────
-- The login endpoint has no origin route, so the edge terminates it and plays a
-- vulnerable MySQL app. Runs before every other detector and outside both timers.

local sqli = config.sql_injection

-- Owned = the request the trap answers itself. Scoped by method AND path so the
-- benchmarked GET population (including GET /api/login?password=…) is untouched.
local function sqli_owns(method, uri)
  if not sqli or not token_enabled(sqli) then return false end
  local ok_method = false
  for _, m in ipairs(sqli.methods or {}) do
    if m == method then ok_method = true end
  end
  if not ok_method then return false end
  return path_matches(sqli.paths, uri)
end

-- Signatures are stored pre-lowercased and space-normalised, so the same folding must be
-- applied to the input. ASCII-only lowercasing keeps this byte-identical to the WASM edge.
local function sqli_normalize(v)
  return (url_decode(v):lower():gsub("%s+", " "))
end

-- Ordered pair list rather than parse_query_string's map: that map is unordered and a
-- duplicate key overwrites, which would make the reflected payload differ between edges
-- on a multi-field hit.
local function sqli_parse_pairs(raw)
  local out = {}
  for chunk in (raw or ""):gmatch("[^&]+") do
    local k, v = chunk:match("^(.-)=(.*)$")
    if not k then k, v = chunk, "" end
    out[#out + 1] = { key = url_decode(k), value = url_decode(v) }
  end
  return out
end

-- Loop order (watch_fields → body pairs → signatures) is part of the cross-edge contract:
-- it makes "first match wins" resolve identically on all four edges.
local function sqli_match(body_pairs)
  for _, field in ipairs(sqli.watch_fields or {}) do
    for _, pair in ipairs(body_pairs) do
      if pair.key == field then
        local norm = sqli_normalize(pair.value)
        for _, sig in ipairs(sqli.signatures or {}) do
          if norm:find(sig, 1, true) then
            return { field = field, value = pair.value, signature = sig }
          end
        end
      end
    end
  end
  return nil
end

-- The trap reflects attacker-controlled input; without escaping the honeypot would itself
-- be a live reflected-XSS vector against anyone who views the page.
local function sqli_html_escape(s)
  s = s:gsub("&", "&amp;"):gsub("<", "&lt;"):gsub(">", "&gt;")
  s = s:gsub('"', "&quot;"):gsub("'", "&#39;")
  return s
end

local function sqli_render(template, payload)
  local escaped = sqli_html_escape(payload:sub(1, sqli.reflect_max_len or 200))
  -- Replacement FUNCTION, not string: a payload such as `100%' OR 1=1--` keeps a bare `%`,
  -- which gsub would reject as an invalid replacement escape and turn into a 500.
  return (template:gsub("{PAYLOAD}", function() return escaped end))
end

-- CR/LF would let an attacker forge whole log lines that the benchmark scrapers read.
local function sqli_log_safe(s)
  return (s:gsub("[\r\n]", " "))
end

-- ── Additional honeytoken kinds (parity with OpenResty nginx/nginx.conf) ──────────
-- Everything below runs OUTSIDE the html_comments detection/injection timers so the
-- benchmarked measurements stay comparable across edges. token_enabled / path_matches
-- are defined near the top (shared with the html_comments path).

local function tokens_of(kind)
  return (config.honeytokens and config.honeytokens[kind]) or {}
end

-- Substring scan of a request's target surface (path, Host, query) for a replayed value.
local function keyword_in_request(kw, ctx)
  if not kw or kw == "" then return false end
  if ctx.uri:find(kw, 1, true) or (ctx.host and ctx.host:find(kw, 1, true)) then
    return true
  end
  for k, v in pairs(ctx.params) do
    if k:find(kw, 1, true) or v:find(kw, 1, true) then return true end
  end
  return false
end

local function cookie_value(cookie_header, name)
  if not cookie_header then return nil end
  for pair in cookie_header:gmatch("[^;]+") do
    local k, v = pair:match("^%s*(.-)%s*=%s*(.-)%s*$")
    if k == name then return v end
  end
  return nil
end

-- Detectors record hits as small descriptors and this renders them afterwards, so that
-- alert *formatting and writing* both sit outside the detection timer. Log I/O otherwise
-- dominated the measurement: it made a kind's cost depend on whether it was the first to
-- log on that request rather than on its scan (see docs/EDGE_LEVELING.md). The wire format
-- is unchanged and identical on all edges.
local function format_alert(ip, hit)
  local prefix = "WADM ALERT: honeytoken triggered by " .. ip .. " — "
  local t = hit.tpl
  if t == "header_replay" then
    return prefix .. "http_header value '" .. hit.a .. "' replayed in request"
  elseif t == "cookie_tamper" then
    return prefix .. "cookie '" .. hit.a .. "' tampered (got '" .. hit.b
      .. "', expected '" .. hit.c .. "')"
  elseif t == "cookie_replay" then
    return prefix .. "cookie value '" .. hit.a .. "' replayed in request"
  elseif t == "decoy_hit" then
    return prefix .. "decoy path '" .. hit.a .. "' requested (" .. hit.b .. ")"
  elseif t == "form_tamper" then
    return prefix .. "form field '" .. hit.a .. "' tampered (got '" .. hit.b
      .. "', expected '" .. hit.c .. "')"
  else
    return prefix .. "form field keyword '" .. hit.a .. "' seen in request"
  end
end

-- Per-kind detectors. Each only appends hit descriptors to `hits`; keeping them separate
-- lets detect_additional time one kind at a time.
local detect_kind = {
  http_headers = function(ctx, hits)
    for _, t in ipairs(tokens_of("http_headers")) do
      if token_enabled(t) and keyword_in_request(t.trigger_keyword, ctx) then
        hits[#hits + 1] = { tpl = "header_replay", a = tostring(t.trigger_keyword) }
      end
    end
  end,

  cookies = function(ctx, hits)
    for _, t in ipairs(tokens_of("cookies")) do
      if token_enabled(t) then
        if t.cookie_name and t.cookie_name ~= "" then
          local v = cookie_value(ctx.cookie, t.cookie_name)
          if v ~= nil and v ~= (t.cookie_value or "") then
            hits[#hits + 1] = { tpl = "cookie_tamper", a = t.cookie_name, b = v,
              c = t.cookie_value or "" }
          end
        end
        if keyword_in_request(t.trigger_keyword, ctx) then
          hits[#hits + 1] = { tpl = "cookie_replay", a = tostring(t.trigger_keyword) }
        end
      end
    end
  end,

  decoy_paths = function(ctx, hits)
    for _, t in ipairs(tokens_of("decoy_paths")) do
      if token_enabled(t) and t.trap_path and t.trap_path ~= "" then
        local trap = t.trap_path
        local hit
        if (t.match_type or "prefix") == "exact" then
          hit = (ctx.uri == trap)
        else
          hit = (ctx.uri == trap) or (ctx.uri:sub(1, #trap + 1) == trap .. "/")
        end
        if hit then
          hits[#hits + 1] = { tpl = "decoy_hit", a = trap, b = ctx.uri }
        end
      end
    end
  end,

  form_fields = function(ctx, hits)
    for _, t in ipairs(tokens_of("form_fields")) do
      if token_enabled(t) and t.field_name and t.field_name ~= "" then
        local expected = t.field_value or ""
        local qv = ctx.params[t.field_name]
        if qv ~= nil and qv ~= expected then
          hits[#hits + 1] = { tpl = "form_tamper", a = t.field_name, b = qv, c = expected }
        end
        if keyword_in_request(t.trigger_keyword, ctx) then
          hits[#hits + 1] = { tpl = "form_keyword", a = tostring(t.trigger_keyword) }
        end
      end
    end
  end,
}

-- Fixed order so the per-kind timing regions run in the same sequence on every edge.
local KIND_ORDER = { "http_headers", "cookies", "decoy_paths", "form_fields" }

-- Detection for the additional kinds, one timed region per kind. The timed region covers
-- the kind's own scan plus the in-memory IP record — the same unit the html_comments
-- detection timer measures — while ctx construction stays outside as setup and alert
-- rendering/logging happens after the timer closes.
local function detect_additional(request_handle, ctx)
  for _, kind in ipairs(KIND_ORDER) do
    local hits = {}
    local kind_start = get_micro_time()
    detect_kind[kind](ctx, hits)
    if #hits > 0 then
      record_attacker_ip(ctx.ip)
    end
    local kind_delta = get_micro_time() - kind_start

    for _, hit in ipairs(hits) do
      request_handle:logWarn(format_alert(ctx.ip, hit))
    end
    -- Timed only on a hit, so every edge samples the same population.
    if #hits > 0 then
      request_handle:logWarn("WADM TOKEN " .. kind .. " detect (us): " .. kind_delta)
    end
  end
end

-- Envoy hook: inspect and optionally rewrite request path/body before routing to the cluster.
function envoy_on_request(request_handle)
  -- Carry the request path to the response phase. The ":path" pseudo-header exists only
  -- on the request, so envoy_on_response cannot read it — it would fall back to "/" and
  -- silently miss exact-path honeytokens (e.g. a token scoped to /index.html). Stash the
  -- path-only portion in per-stream dynamic metadata here; envoy_on_response reads it
  -- back. This mirrors how the WASM filter saves self.request_path on the request side.
  -- Deliberately placed before the early return below (injection must know the path even
  -- when no trigger keywords are configured) and before the detection timer, since this
  -- is bookkeeping rather than detection work.
  local raw_path = request_handle:headers():get(":path") or "/"
  local path_only = raw_path:match("^([^?]+)") or raw_path
  request_handle:streamInfo():dynamicMetadata():set(
    WADM_META_FILTER, "request_path", path_only
  )

  local ip = request_handle:headers():get("x-forwarded-for")
      or request_handle:headers():get("x-real-ip")
      or "unknown"

  -- Fake SQL-injection trap. Placed first and returning unconditionally: an owned request
  -- never reaches any other detector, keeping all four edges observably identical. POST-only,
  -- so the k6 GET population (including GET /api/login?password=…) is unaffected.
  if sqli_owns(request_handle:headers():get(":method"), path_only) then
    -- body(), never bodyChunks(): respond() is rejected once headers_continued_ is set, and
    -- only the chunked path sets it. Buffering via body() leaves the flag clear.
    local body_handle = request_handle:body()
    local raw = ""
    if body_handle and body_handle:length() > 0 then
      raw = tostring(body_handle:getBytes(0, body_handle:length()))
    end

    local sqli_start = get_micro_time()
    local hit = sqli_match(sqli_parse_pairs(raw))
    local page, status
    if hit then
      page = sqli_render(sqli.error_template, hit.value)
      status = sqli.status_code or 500
    else
      page = sqli.deny_template
      status = sqli.deny_status_code or 401
    end
    local sqli_delta = get_micro_time() - sqli_start

    if hit then
      request_handle:logWarn("WADM ALERT: honeytoken triggered by " .. ip
        .. " — sql_injection signature '" .. hit.signature .. "' in field '" .. hit.field
        .. "' on " .. (request_handle:headers():get(":method") or "?") .. " " .. path_only
        .. " (payload '" .. sqli_log_safe(hit.value) .. "')")
      record_attacker_ip(ip)
      -- Label deliberately shares no substring with the benchmark scrapers'
      -- "Detection/Injection execution time (us):" patterns.
      request_handle:logWarn("Envoy Lua WADM SQLI trap build (us): " .. sqli_delta)
    end

    -- sendLocalReply re-enters the whole encoder chain, this filter included, so
    -- envoy_on_response would otherwise stamp the trap page with honeytokens.
    request_handle:streamInfo():dynamicMetadata():set(
      WADM_META_FILTER, "local_response", "1"
    )
    request_handle:respond(
      { [":status"] = tostring(status), ["content-type"] = "text/html; charset=UTF-8" },
      page
    )
    return
  end

  -- Additional honeytoken kinds: cookie/form tamper, decoy-path hits, header/cookie value
  -- replays. Runs before the html_comments block so its early-return can't skip it; same sink
  -- (WARN log + in-memory IP). Each kind is timed separately and logged under
  -- `WADM TOKEN <kind> detect (us):`, a label that shares no substring with the html_comments
  -- scraper pattern, so the benchmarked detection timer below is untouched.
  do
    local q = raw_path:find("?", 1, true)
    local actx = {
      ip = ip,
      uri = path_only,
      host = request_handle:headers():get(":authority")
             or request_handle:headers():get("host"),
      params = q and parse_query_string(raw_path:sub(q + 1)) or {},
      cookie = request_handle:headers():get("cookie"),
    }
    detect_additional(request_handle, actx)
  end

  -- Setup runs *before* the timer (matches OpenResty): building the trigger table and the
  -- known-attacker lookup are excluded from the timed region so detection measures only
  -- query scan + strip + in-memory record.
  local triggers = get_trigger_keywords()
  if #triggers == 0 then
    return
  end

  if is_known_attacker(ip) then
    request_handle:logWarn(
      "WADM ALERT: known attacker IP " .. ip .. " detected in new request"
    )
  end

  -- Hits are collected here and rendered after the timer closes, keeping alert formatting
  -- and log I/O out of the measured region (see docs/EDGE_LEVELING.md).
  local alerts = {}
  local detection_start = get_micro_time()

  local path = request_handle:headers():get(":path") or "/"
  local dirty = false

  local query_start = path:find("?") --path check
  if query_start then
    local base_path = path:sub(1, query_start - 1)
    local query_string = path:sub(query_start + 1)
    local params = parse_query_string(query_string)

    for key, val in pairs(params) do
      for _, keyword in ipairs(triggers) do
        if key:find(keyword, 1, true) or val:find(keyword, 1, true) then
          alerts[#alerts + 1] = { kw = keyword, key = key, val = val, where = "query param" }
          params[key] = nil
          dirty = true
        end
      end
    end

    if dirty then
      local cleaned = rebuild_query_string(params)
      local cleaned_path = cleaned ~= "" and (base_path .. "?" .. cleaned) or base_path
      request_handle:headers():replace(":path", cleaned_path)
    end
  end

  local detected = false

  -- POST-body inspection (disabled unless post_body_inspection flag is set).
  if post_body_inspection then
  local body_handle = request_handle:body() --body check
  if body_handle and body_handle:length() > 0 then
    local body_str = tostring(body_handle:getBytes(0, body_handle:length()))
    local ct = request_handle:headers():get("content-type") or ""

    if ct:find("application/x%-www%-form%-urlencoded", 1) then --form urlencoded check
      local post_params = parse_query_string(body_str)
      local dirty_body = false

      for key, val in pairs(post_params) do
        for _, keyword in ipairs(triggers) do
          if key:find(keyword, 1, true) or val:find(keyword, 1, true) then
            alerts[#alerts + 1] = { kw = keyword, key = key, val = val, where = "POST param" }
            post_params[key] = nil
            dirty_body = true
            detected = true
          end
        end
      end

      if dirty_body then
        local cleaned_body = rebuild_query_string(post_params)
        body_handle:setBytes(cleaned_body)
        request_handle:headers():replace("content-length", tostring(#cleaned_body))
      end
    else --body check for other content types
      for _, keyword in ipairs(triggers) do
        if body_str:find(keyword, 1, true) then
          alerts[#alerts + 1] = { kw = keyword, where = "body" }
          detected = true
        end
      end
    end
  end
  end -- post_body_inspection

  -- On detection, record the attacker IP in the in-memory store (mirrors OpenResty's
  -- wadm:set) inside the timer.
  if detected or dirty then
    record_attacker_ip(ip)
  end
  local detection_delta = get_micro_time() - detection_start

  -- Alert rendering and log I/O sit outside the timer: writing them inside made the
  -- measurement track log-flush cost rather than scan cost.
  for _, a in ipairs(alerts) do
    if a.where == "body" then
      request_handle:logWarn("WADM ALERT: honeytoken triggered by " .. ip
        .. " — keyword '" .. a.kw .. "' found in request body")
    else
      request_handle:logWarn("WADM ALERT: honeytoken triggered by " .. ip
        .. " — keyword '" .. a.kw .. "' found in " .. a.where
        .. " '" .. a.key .. "=" .. a.val .. "'")
    end
  end
  -- Only trigger-bearing requests are timed so all edges sample the same population
  -- (the keyword-matching request).
  if detected or dirty then
    request_handle:logWarn("Envoy Lua Detection execution time (us): " .. detection_delta)
  end
end

-- Envoy hook: mutate HTML responses from upstream to embed honeytoken HTML comments.
-- Canonical injection contract (shared with OpenResty / Apache / WASM):
--   • content-type guard + path matching + token join are setup → OUTSIDE the timer
--   • the whole response body is buffered BEFORE the timer starts, so the measured
--     window excludes the upstream body-arrival wait (Envoy's Lua filter buffers the
--     full body on first :body() access, suspending the coroutine until it is complete)
--   • timed region = read body → locate first </body> → splice → write body back
--   • Content-Length adjustment is external to the timed region
function envoy_on_response(response_handle)
  -- Path stashed by envoy_on_request (":path" is request-only, absent on the response).
  local uri = "/"
  local meta = response_handle:streamInfo():dynamicMetadata():get(WADM_META_FILTER)
  if meta and meta["request_path"] then
    uri = meta["request_path"]
  end

  -- The SQLi trap page is emitted by respond(), whose local reply still traverses this
  -- filter's encoder path. Bail out before any headers()/body() call so the page ships
  -- exactly as built — matching the edges that bypass their own filters on a local reply.
  if meta and meta["local_response"] then
    return
  end

  -- Header-phase injection (any content type): decoy response headers + Set-Cookie baits.
  -- Header-only, so it runs before the content-type guard and needs no length fix. Token
  -- selection is setup and stays outside; the timed region is the header write itself.
  local header_tokens = {}
  for _, t in ipairs(tokens_of("http_headers")) do
    if token_enabled(t) and path_matches(t.paths, uri)
       and t.header_name and t.header_name ~= "" then
      header_tokens[#header_tokens + 1] = t
    end
  end
  if #header_tokens > 0 then
    local hstart = get_micro_time()
    for _, t in ipairs(header_tokens) do
      response_handle:headers():add(t.header_name, t.header_value or "")
    end
    response_handle:logWarn("WADM TOKEN http_headers inject (us): " .. (get_micro_time() - hstart))
  end

  local cookie_tokens = {}
  for _, t in ipairs(tokens_of("cookies")) do
    if token_enabled(t) and path_matches(t.paths, uri)
       and t.cookie_name and t.cookie_name ~= "" then
      cookie_tokens[#cookie_tokens + 1] = t
    end
  end
  if #cookie_tokens > 0 then
    local cstart = get_micro_time()
    for _, t in ipairs(cookie_tokens) do
      local c = t.cookie_name .. "=" .. (t.cookie_value or "")
      if t.attributes and t.attributes ~= "" then c = c .. "; " .. t.attributes end
      response_handle:headers():add("set-cookie", c)
    end
    response_handle:logWarn("WADM TOKEN cookies inject (us): " .. (get_micro_time() - cstart))
  end

  -- Body injection is HTML-only.
  local ct = response_handle:headers():get("content-type") or ""
  if not ct:find("text/html", 1, true) then
    return
  end

  -- Setup (outside timer): html_comments payloads + additional-kind body payloads (hidden
  -- form fields before </form>, decoy links before </body>).
  local to_inject = get_comments_for_path(uri)
  local extra = {}
  for _, t in ipairs(tokens_of("decoy_paths")) do
    if token_enabled(t) and (t.advertise_via or "link") == "link"
       and path_matches(t.advertise_on_paths, uri) then
      extra[#extra + 1] = {
        kind = "decoy_paths",
        markup = '<a href="' .. (t.trap_path or "") .. '" style="display:none">'
                 .. (t.link_text or "") .. '</a>',
        anchor = "</body>",
      }
    end
  end
  for _, t in ipairs(tokens_of("form_fields")) do
    if token_enabled(t) and path_matches(t.paths, uri)
       and t.field_name and t.field_name ~= "" then
      extra[#extra + 1] = {
        kind = "form_fields",
        markup = '<input type="hidden" name="' .. t.field_name .. '" value="'
                 .. (t.field_value or "") .. '">',
        anchor = "</form>",
      }
    end
  end

  if #to_inject == 0 and #extra == 0 then
    return
  end

  -- Force full-body buffering here, before the timer (matches OpenResty / WASM).
  local body_handle = response_handle:body()
  local body_len = body_handle:length()

  if #extra == 0 then
    -- Unchanged benchmarked path: read body → splice → write back, inside the timer.
    local injection = table.concat(to_inject, "\n")
    local injection_start = get_micro_time()
    local body_str = tostring(body_handle:getBytes(0, body_len))
    local new_body = splice_before(body_str, "</body>", injection)
    if not new_body then
      new_body = body_str .. injection
    end
    body_handle:setBytes(new_body)
    local injection_end = get_micro_time()
    response_handle:headers():replace("content-length", tostring(#new_body))
    response_handle:logWarn(
      "Envoy Lua Injection execution time (us): " .. (injection_end - injection_start)
    )
  else
    -- Additional-kind payloads present: splice each under its own per-kind timer, then run
    -- the html_comments splice under its (unchanged) timer on the already-assembled body.
    -- The per-kind timed region is locate-anchor → splice only; the single setBytes write-back
    -- happens once, outside.
    local body_str = tostring(body_handle:getBytes(0, body_len))
    for _, p in ipairs(extra) do
      local extra_start = get_micro_time()
      local replaced = splice_before(body_str, p.anchor, p.markup)
      local extra_delta = get_micro_time() - extra_start
      if replaced then body_str = replaced end
      response_handle:logWarn("WADM TOKEN " .. p.kind .. " inject (us): " .. extra_delta)
    end
    if #to_inject > 0 then
      local injection = table.concat(to_inject, "\n")
      local injection_start = get_micro_time()
      local new_body = splice_before(body_str, "</body>", injection)
      if not new_body then
        new_body = body_str .. injection
      end
      body_handle:setBytes(new_body)
      local injection_end = get_micro_time()
      response_handle:headers():replace("content-length", tostring(#new_body))
      response_handle:logWarn(
        "Envoy Lua Injection execution time (us): " .. (injection_end - injection_start)
      )
    else
      body_handle:setBytes(body_str)
      response_handle:headers():replace("content-length", tostring(#body_str))
    end
  end
end
