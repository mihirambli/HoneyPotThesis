package.path = "/etc/envoy/scripts/?.lua;" .. package.path

local json = require("json")
local ffi = require("ffi")

local find, sub, gsub, gmatch, match = string.find, string.sub, string.gsub, string.gmatch, string.match
local lower, char, tonumber = string.lower, string.char, tonumber
local concat = table.concat

-- Load honeytoken definitions once when the filter script loads (path is bind-mounted from docker-compose).
local file = io.open("/etc/envoy/config.json", "r")
local config = json.decode(file:read("*a"))
file:close()

local ht = config.honeytokens or {}
local post_body_inspection = config.post_body_inspection == true

-- Wall-clock microsecond timer via LuaJIT FFI gettimeofday, mirroring OpenResty's, so Envoy+Lua
-- measures the same quantity as the other three edges. A uniquely named struct avoids clashing
-- with any pre-existing `struct timeval` cdef. Each Envoy worker thread has its own Lua state, so
-- one preallocated timeval per state replaces an ffi.new allocation on every read.
ffi.cdef[[
  typedef long wadm_time_t;
  struct wadm_timeval { wadm_time_t tv_sec; long tv_usec; };
  int gettimeofday(struct wadm_timeval *tv, void *tz);
]]
local tv = ffi.new("struct wadm_timeval")
local function now_us()
  if ffi.C.gettimeofday(tv, nil) ~= 0 then
    return math.floor(os.time() * 1e6)  -- coarse fallback if the syscall fails
  end
  return tonumber(tv.tv_sec) * 1000000 + tonumber(tv.tv_usec)
end

-- Per-token on/off switch (all kinds): active unless explicitly disabled. 1/on/true/yes = on;
-- anything else = off; absent = on (backward compatible).
local function token_enabled(token)
  local v = token.enabled
  if v == nil then return true end
  if v == true or v == 1 then return true end
  if type(v) == "string" then
    local s = lower(v)
    return s == "1" or s == "on" or s == "true" or s == "yes"
  end
  return false
end

local function enabled_tokens(list)
  local out = {}
  for _, t in ipairs(list or {}) do
    if token_enabled(t) then out[#out + 1] = t end
  end
  return out
end

local function nonempty(s)
  if s == nil or s == "" then return nil end
  return s
end

-- ── Precompiled per-path plans (parity with OpenResty nginx/nginx.conf) ─────────────
-- Paths are only ever "/*" or exact, so every request resolves to one of a handful of plans.
-- Building them once replaces the per-response token loops, enabled checks and markup
-- concatenation. Config order is kept within each list because it decides the joined comment
-- order and the splice order.

local comment_tokens = enabled_tokens(ht.html_comments)
local header_tokens = enabled_tokens(ht.http_headers)
local cookie_tokens = enabled_tokens(ht.cookies)
local decoy_tokens = enabled_tokens(ht.decoy_paths)
local form_tokens = enabled_tokens(ht.form_fields)

local function covers(paths, path)
  for _, p in ipairs(paths or {}) do
    if p == "/*" or p == path then return true end
  end
  return false
end

local function build_plan(path)
  local comments = {}
  for _, t in ipairs(comment_tokens) do
    if t.comment_value and covers(t.paths, path) then
      comments[#comments + 1] = t.comment_value
    end
  end
  local headers = {}
  for _, t in ipairs(header_tokens) do
    if nonempty(t.header_name) and covers(t.paths, path) then
      headers[#headers + 1] = { name = t.header_name, value = t.header_value or "" }
    end
  end
  local cookies = {}
  for _, t in ipairs(cookie_tokens) do
    if nonempty(t.cookie_name) and covers(t.paths, path) then
      local c = t.cookie_name .. "=" .. (t.cookie_value or "")
      if nonempty(t.attributes) then c = c .. "; " .. t.attributes end
      cookies[#cookies + 1] = c
    end
  end
  local extras = {}
  for _, t in ipairs(decoy_tokens) do
    if (t.advertise_via or "link") == "link" and covers(t.advertise_on_paths, path) then
      extras[#extras + 1] = {
        kind = "decoy_paths", anchor = "</body>",
        markup = '<a href="' .. (t.trap_path or "") .. '" style="display:none">'
          .. (t.link_text or "") .. '</a>',
      }
    end
  end
  for _, t in ipairs(form_tokens) do
    if nonempty(t.field_name) and covers(t.paths, path) then
      extras[#extras + 1] = {
        kind = "form_fields", anchor = "</form>",
        markup = '<input type="hidden" name="' .. t.field_name .. '" value="'
          .. (t.field_value or "") .. '">',
      }
    end
  end
  local joined = #comments > 0 and concat(comments, "\n") or nil
  return {
    comments = joined, headers = headers, cookies = cookies, extras = extras,
    has_body = joined ~= nil or #extras > 0,
  }
end

local plans = {}
local function note_paths(paths)
  for _, p in ipairs(paths or {}) do
    if p ~= "/*" then plans[p] = true end
  end
end
for _, list in ipairs({ comment_tokens, header_tokens, cookie_tokens, form_tokens }) do
  for _, t in ipairs(list) do note_paths(t.paths) end
end
for _, t in ipairs(decoy_tokens) do note_paths(t.advertise_on_paths) end
for p in pairs(plans) do plans[p] = build_plan(p) end
local plan_default = build_plan(nil)

local triggers = {}
for _, t in ipairs(comment_tokens) do
  if nonempty(t.trigger_keyword) then triggers[#triggers + 1] = t.trigger_keyword end
end

-- ── Shared request parsing ───────────────────────────────────────────────────────────

local function url_decode(s)
  s = gsub(s, "%+", " ")
  return (gsub(s, "%%(%x%x)", function(h) return char(tonumber(h, 16)) end))
end

-- Ordered segments that keep their raw text: the strip rebuilds the query from the raw text of
-- the segments it keeps, so order and original encoding survive, identically on all four edges.
-- Decoding is skipped for the common segment that has nothing to decode.
local function parse_query(q)
  local segs = {}
  if not q or q == "" then return segs end
  local pos, n = 1, #q
  while pos <= n do
    local amp = find(q, "&", pos, true)
    local last = amp and amp - 1 or n
    if last >= pos then
      local raw = sub(q, pos, last)
      local eq = find(raw, "=", 1, true)
      local k = eq and sub(raw, 1, eq - 1) or raw
      local v = eq and sub(raw, eq + 1) or ""
      if find(raw, "%", 1, true) or find(raw, "+", 1, true) then
        k, v = url_decode(k), url_decode(v)
      end
      segs[#segs + 1] = { raw = raw, key = k, value = v }
    end
    if not amp then break end
    pos = amp + 1
  end
  return segs
end

local function join_kept(segs, dropped)
  local kept = {}
  for i = 1, #segs do
    if not dropped[i] then kept[#kept + 1] = segs[i].raw end
  end
  return concat(kept, "&")
end

-- Stream info yields "ip:port" (or "[v6]:port"); the other edges record the bare address.
local function strip_port(addr)
  if not addr then return "unknown" end
  if sub(addr, 1, 1) == "[" then
    local close = find(addr, "]", 1, true)
    return close and sub(addr, 2, close - 1) or addr
  end
  local colon = find(addr, ":", 1, true)
  if colon and not find(addr, ":", colon + 1, true) then
    return sub(addr, 1, colon - 1)
  end
  return addr
end

local function cookie_value(header, name)
  if not header then return nil end
  for pair in gmatch(header, "[^;]+") do
    local k, v = match(pair, "^%s*(.-)%s*=%s*(.-)%s*$")
    if k == name then return v end
  end
  return nil
end

-- Substring scan of a request's target surface (path, Host, query) for a replayed value.
local function keyword_in_request(kw, ctx)
  if find(ctx.path, kw, 1, true) or (ctx.host and find(ctx.host, kw, 1, true)) then
    return true
  end
  local segs = ctx.segs
  for i = 1, #segs do
    local s = segs[i]
    if find(s.key, kw, 1, true) or find(s.value, kw, 1, true) then return true end
  end
  return false
end

-- Fixed-anchor first-match splice, the same primitive on every edge: a *plain* find (patterns
-- disabled) then two subs. string.gsub was used here before and cost 2-3x more for the same
-- result, making the injection figure rank an implementation choice (docs/EDGE_LEVELING.md).
local function splice_before(body, anchor, insert)
  local pos = find(body, anchor, 1, true)
  if not pos then return nil end
  return sub(body, 1, pos - 1) .. insert .. "\n" .. sub(body, pos)
end

-- CR/LF would let an attacker forge whole log lines that the benchmark scrapers read.
local function log_safe(s)
  return (gsub(s, "[\r\n]", " "))
end

-- ── Detectors (parity with OpenResty nginx/nginx.conf) ───────────────────────────────
-- Detectors record hits as small descriptors and format_alert renders them afterwards, so that
-- alert *formatting and writing* both sit outside the detection timer. Log I/O otherwise
-- dominated the measurement (see docs/EDGE_LEVELING.md). The wire format is identical on all edges.
local function format_alert(ip, hit)
  local prefix = "WADM ALERT: honeytoken triggered by " .. ip .. " — "
  local t = hit.tpl
  if t == "header_replay" then
    return prefix .. "http_header value '" .. hit.a .. "' replayed in request"
  elseif t == "cookie_tamper" then
    return prefix .. "cookie '" .. hit.a .. "' tampered (got '" .. log_safe(hit.b)
      .. "', expected '" .. hit.c .. "')"
  elseif t == "cookie_replay" then
    return prefix .. "cookie value '" .. hit.a .. "' replayed in request"
  elseif t == "decoy_hit" then
    return prefix .. "decoy path '" .. hit.a .. "' requested (" .. log_safe(hit.b) .. ")"
  elseif t == "form_tamper" then
    return prefix .. "form field '" .. hit.a .. "' tampered (got '" .. log_safe(hit.b)
      .. "', expected '" .. hit.c .. "')"
  elseif t == "form_tamper_post" then
    return prefix .. "form field '" .. hit.a .. "' tampered in POST (got '" .. log_safe(hit.b)
      .. "', expected '" .. hit.c .. "')"
  elseif t == "query_keyword" then
    return prefix .. "keyword '" .. hit.a .. "' found in " .. hit.where .. " '"
      .. log_safe(hit.b) .. "=" .. log_safe(hit.c) .. "'"
  elseif t == "body_keyword" then
    return prefix .. "keyword '" .. hit.a .. "' found in request body"
  else
    return prefix .. "form field keyword '" .. hit.a .. "' seen in request"
  end
end

local header_detect = {}
for _, t in ipairs(header_tokens) do
  if nonempty(t.trigger_keyword) then header_detect[#header_detect + 1] = t.trigger_keyword end
end
local cookie_detect = {}
for _, t in ipairs(cookie_tokens) do
  local name, kw = nonempty(t.cookie_name), nonempty(t.trigger_keyword)
  if name or kw then
    cookie_detect[#cookie_detect + 1] = { name = name, value = t.cookie_value or "", kw = kw }
  end
end
local decoy_detect = {}
for _, t in ipairs(decoy_tokens) do
  if nonempty(t.trap_path) then
    decoy_detect[#decoy_detect + 1] = {
      trap = t.trap_path, prefix = t.trap_path .. "/",
      exact = (t.match_type or "prefix") == "exact",
    }
  end
end
local form_detect = {}
for _, t in ipairs(form_tokens) do
  local name, kw = nonempty(t.field_name), nonempty(t.trigger_keyword)
  if name or kw then
    form_detect[#form_detect + 1] = { name = name, value = t.field_value or "", kw = kw }
  end
end

-- The first mismatching submission is the tamper evidence; later duplicates add nothing.
local function first_tamper(segs, name, expected)
  if not segs then return nil end
  for i = 1, #segs do
    local s = segs[i]
    if s.key == name and s.value ~= expected then return s.value end
  end
  return nil
end

local detectors = {
  http_headers = function(tokens, ctx, hits)
    for i = 1, #tokens do
      if keyword_in_request(tokens[i], ctx) then
        hits[#hits + 1] = { tpl = "header_replay", a = tokens[i] }
      end
    end
  end,

  cookies = function(tokens, ctx, hits)
    for i = 1, #tokens do
      local t = tokens[i]
      if t.name then
        local v = cookie_value(ctx.cookie, t.name)
        if v ~= nil and v ~= t.value then
          hits[#hits + 1] = { tpl = "cookie_tamper", a = t.name, b = v, c = t.value }
        end
      end
      if t.kw and keyword_in_request(t.kw, ctx) then
        hits[#hits + 1] = { tpl = "cookie_replay", a = t.kw }
      end
    end
  end,

  decoy_paths = function(tokens, ctx, hits)
    local path = ctx.path
    for i = 1, #tokens do
      local t = tokens[i]
      local hit = path == t.trap or (not t.exact and sub(path, 1, #t.prefix) == t.prefix)
      if hit then
        hits[#hits + 1] = { tpl = "decoy_hit", a = t.trap, b = path }
      end
    end
  end,

  form_fields = function(tokens, ctx, hits)
    for i = 1, #tokens do
      local t = tokens[i]
      if t.name then
        local got = first_tamper(ctx.segs, t.name, t.value)
        if got then
          hits[#hits + 1] = { tpl = "form_tamper", a = t.name, b = got, c = t.value }
        else
          got = first_tamper(ctx.body_segs, t.name, t.value)
          if got then
            hits[#hits + 1] = { tpl = "form_tamper_post", a = t.name, b = got, c = t.value }
          end
        end
      end
      if t.kw and keyword_in_request(t.kw, ctx) then
        hits[#hits + 1] = { tpl = "form_keyword", a = t.kw }
      end
    end
  end,
}

-- Fixed order so the per-kind timing regions run in the same sequence on every edge. Kinds with
-- nothing to detect are dropped here rather than timed as empty loops.
local kinds = {}
for _, k in ipairs({
  { "http_headers", header_detect }, { "cookies", cookie_detect },
  { "decoy_paths", decoy_detect }, { "form_fields", form_detect },
}) do
  if #k[2] > 0 then
    kinds[#kinds + 1] = { name = k[1], tokens = k[2], detect = detectors[k[1]] }
  end
end
local need_cookie = #cookie_detect > 0

-- In-memory attacker store (module scope → persists across requests on this worker's Lua VM),
-- mirroring OpenResty's ngx.shared.wadm_state. Write-only on every edge: nothing reads it back
-- per request, so no edge pays a per-request lookup or log line for it.
local detected_ips = {}

-- ── Fake SQL-injection trap (parity with OpenResty nginx/nginx.conf) ─────────────────
-- The login endpoint has no origin route, so the edge terminates it and plays a vulnerable
-- MySQL app. Runs before every other detector and outside both timers.

local sqli = config.sql_injection
if sqli and not token_enabled(sqli) then sqli = nil end
local sqli_methods, sqli_paths, sqli_any_path = {}, {}, false
if sqli then
  for _, m in ipairs(sqli.methods or {}) do sqli_methods[m] = true end
  for _, p in ipairs(sqli.paths or {}) do
    if p == "/*" then sqli_any_path = true else sqli_paths[p] = true end
  end
end

-- Signatures are stored pre-lowercased and space-normalised, so the same folding must be
-- applied to the input. ASCII-only lowercasing keeps this byte-identical to the WASM edge.
local function sqli_normalize(v)
  return (gsub(lower(url_decode(v)), "%s+", " "))
end

-- Loop order (watch_fields → body pairs → signatures) is part of the cross-edge contract: it
-- makes "first match wins" resolve identically on all four edges. Every body segment is decoded
-- here (not only those containing % or +) because the signatures must see the normalised text.
local function sqli_match(raw)
  local pairs_out = {}
  for chunk in gmatch(raw or "", "[^&]+") do
    local k, v = match(chunk, "^(.-)=(.*)$")
    if not k then k, v = chunk, "" end
    pairs_out[#pairs_out + 1] = { key = url_decode(k), value = url_decode(v) }
  end
  for _, field in ipairs(sqli.watch_fields or {}) do
    for _, pair in ipairs(pairs_out) do
      if pair.key == field then
        local norm = sqli_normalize(pair.value)
        for _, sig in ipairs(sqli.signatures or {}) do
          if find(norm, sig, 1, true) then
            return { field = field, value = pair.value, signature = sig }
          end
        end
      end
    end
  end
  return nil
end

-- The trap reflects attacker-controlled input; without escaping the honeypot would itself be a
-- live reflected-XSS vector against anyone who views the page.
local function html_escape(s)
  s = gsub(s, "&", "&amp;")
  s = gsub(s, "<", "&lt;")
  s = gsub(s, ">", "&gt;")
  s = gsub(s, '"', "&quot;")
  s = gsub(s, "'", "&#39;")
  return s
end

local function sqli_render(template, payload)
  local escaped = html_escape(sub(payload, 1, sqli.reflect_max_len or 200))
  -- Replacement FUNCTION, not string: a payload such as `100%' OR 1=1--` keeps a bare `%`,
  -- which gsub would reject as an invalid replacement escape and turn into a 500.
  return (gsub(template, "{PAYLOAD}", function() return escaped end))
end

-- Per-stream dynamic-metadata namespace carrying request-side facts to envoy_on_response.
local WADM_META_FILTER = "wadm.honeypot"

local function sqli_respond(request_handle, ip, method, path)
  -- body(), never bodyChunks(): respond() is rejected once headers_continued_ is set, and only
  -- the chunked path sets it. Buffering via body() leaves the flag clear.
  local body_handle = request_handle:body()
  local raw = ""
  if body_handle and body_handle:length() > 0 then
    raw = body_handle:getBytes(0, body_handle:length())
  end

  -- Closed before the hit/miss branch so every arm measures the same unit: the scan alone, with
  -- page rendering and the IP record outside it. The scan covers decoding each body pair,
  -- normalising the watched values and walking the signature list, and the benchmark drives all
  -- four combinations of outcome and encoding to attribute the cost between them.
  local sqli_start = now_us()
  local hit = sqli_match(raw)
  local sqli_delta = now_us() - sqli_start

  local page, status
  if hit then
    page = sqli_render(sqli.error_template, hit.value)
    status = sqli.status_code or 500
  else
    page = sqli.deny_template
    status = sqli.deny_status_code or 401
  end

  -- The IP record stays outside the timed region, unlike the honeytoken kinds':
  -- only the hit arms perform it, so including it would surface as a hit-vs-miss
  -- difference that has nothing to do with the scan.
  -- Percent-encoding in a form body is an evasion technique worth recording on its own, and it is
  -- also the dominant cost in the scan: url_decode's substitution path runs over every body pair and
  -- again during normalisation, while the signature walk is mostly rejected on length. Splitting the
  -- two is what lets detection cost be attributed to normalisation rather than to scan depth.
  -- Classified outside the timed region so it never enters the measurement.
  local kind = hit and "sql_injection" or "sql_injection_miss"
  if find(raw, "%", 1, true) then kind = kind .. "_encoded" end

  if hit then
    request_handle:logWarn("WADM ALERT: honeytoken triggered by " .. ip
      .. " — sql_injection signature '" .. hit.signature .. "' in field '" .. hit.field
      .. "' on " .. method .. " " .. path .. " (payload '" .. log_safe(hit.value) .. "')")
    detected_ips[ip] = true
  else
    -- These requests never reach the origin, so this is the only record that the trap
    -- endpoint was hit.
    request_handle:logWarn("WADM TRAP: " .. ip .. " " .. method .. " " .. path
      .. " answered locally with " .. status .. " (no signature matched)")
  end
  request_handle:logWarn("WADM TOKEN " .. kind .. " detect (us): " .. sqli_delta)

  -- sendLocalReply re-enters the whole encoder chain, this filter included, so
  -- envoy_on_response would otherwise stamp the trap page with honeytokens.
  request_handle:streamInfo():dynamicMetadata():set(WADM_META_FILTER, "local_response", "1")
  request_handle:respond(
    { [":status"] = tostring(status), ["content-type"] = "text/html; charset=UTF-8" },
    page
  )
end

local function detect_kinds(request_handle, ctx)
  local ip = ctx.ip
  for i = 1, #kinds do
    local k = kinds[i]
    -- Timed region = the kind's scan + the in-memory IP record. ctx is built before and alert
    -- rendering/logging happens after, as on every edge.
    local hits = {}
    local kind_start = now_us()
    k.detect(k.tokens, ctx, hits)
    if #hits > 0 then
      detected_ips[ip] = true
    end
    local kind_delta = now_us() - kind_start

    if #hits > 0 then
      for j = 1, #hits do
        request_handle:logWarn(format_alert(ip, hits[j]))
      end
      -- Timed only on a hit, so every edge samples the same population.
      request_handle:logWarn("WADM TOKEN " .. k.name .. " detect (us): " .. kind_delta)
    end
  end
end

local function scan_segments(segs, where, hits)
  local dropped
  for i = 1, #segs do
    local s = segs[i]
    for j = 1, #triggers do
      local kw = triggers[j]
      if find(s.key, kw, 1, true) or find(s.value, kw, 1, true) then
        hits[#hits + 1] = { tpl = "query_keyword", a = kw, b = s.key, c = s.value, where = where }
        dropped = dropped or {}
        dropped[i] = true
      end
    end
  end
  return dropped
end

-- html_comments detection (the benchmarked reference). Query parsing happens in setup, so the
-- timed region is scan → strip → write back → record, the same on all edges.
local function detect_comments(request_handle, headers, ctx)
  if #triggers == 0 then return end
  local ip = ctx.ip
  local hits = {}
  local detection_start = now_us()

  local dropped = scan_segments(ctx.segs, "query param", hits)
  if dropped then
    local kept = join_kept(ctx.segs, dropped)
    headers:replace(":path", kept ~= "" and (ctx.path .. "?" .. kept) or ctx.path)
  end

  -- POST-body inspection (behind post_body_inspection; off by default so detection is a
  -- query-string scan only on every edge). OpenResty and Envoy+Lua implement it.
  if ctx.body_segs then
    local body_dropped = scan_segments(ctx.body_segs, "POST param", hits)
    if body_dropped then
      local cleaned = join_kept(ctx.body_segs, body_dropped)
      ctx.body_handle:setBytes(cleaned)
      headers:replace("content-length", tostring(#cleaned))
    end
  elseif ctx.raw_body then
    for j = 1, #triggers do
      if find(ctx.raw_body, triggers[j], 1, true) then
        hits[#hits + 1] = { tpl = "body_keyword", a = triggers[j] }
      end
    end
  end

  if #hits > 0 then
    detected_ips[ip] = true
  end
  local detection_delta = now_us() - detection_start

  if #hits > 0 then
    for j = 1, #hits do
      request_handle:logWarn(format_alert(ip, hits[j]))
    end
    -- Only trigger-bearing requests are timed so all edges sample the same population.
    request_handle:logWarn("Envoy Lua Detection execution time (us): " .. detection_delta)
  end
end

-- Envoy hook: detection (SQLi trap, additional kinds, html_comments) before routing.
function envoy_on_request(request_handle)
  local headers = request_handle:headers()
  local stream_info = request_handle:streamInfo()
  local target = headers:get(":path") or "/"
  local qpos = find(target, "?", 1, true)
  local path = qpos and sub(target, 1, qpos - 1) or target

  -- ":path" exists only on the request, so envoy_on_response cannot read it; carry it across
  -- in per-stream dynamic metadata (the WASM filter keeps self.request_path for the same reason).
  stream_info:dynamicMetadata():set(WADM_META_FILTER, "request_path", path)

  -- The socket peer, as OpenResty ($remote_addr) and Apache (useragent_ip) record it.
  -- X-Forwarded-For was read here before; k6 never sends it, so every attacker became "unknown".
  local ip = strip_port(stream_info:downstreamDirectRemoteAddress())

  if sqli and (sqli_any_path or sqli_paths[path]) then
    local method = headers:get(":method")
    if sqli_methods[method] then
      return sqli_respond(request_handle, ip, method, path)
    end
  end

  local ctx = {
    ip = ip,
    path = path,
    host = headers:get(":authority") or headers:get("host"),
    segs = parse_query(qpos and sub(target, qpos + 1) or ""),
    cookie = need_cookie and headers:get("cookie") or nil,
  }

  if post_body_inspection then
    local body_handle = request_handle:body()
    if body_handle and body_handle:length() > 0 then
      local raw = body_handle:getBytes(0, body_handle:length())
      local ct = headers:get("content-type")
      if ct and find(ct, "application/x-www-form-urlencoded", 1, true) then
        ctx.body_segs = parse_query(raw)
        ctx.body_handle = body_handle
      else
        ctx.raw_body = raw
      end
    end
  end

  detect_kinds(request_handle, ctx)
  detect_comments(request_handle, headers, ctx)
end

-- Envoy hook: response-header baits for every content type, then HTML body injection.
-- Canonical injection contract (shared with OpenResty / Apache / WASM):
--   • content-type guard + plan lookup are setup → OUTSIDE the timers
--   • the whole response body is buffered BEFORE any timer starts (Envoy's Lua filter buffers
--     the full body on first :body() access, suspending the coroutine until it is complete)
--   • per-kind timers wrap locate-anchor → splice; the html_comments timer wraps splice → write
function envoy_on_response(response_handle)
  local meta = response_handle:streamInfo():dynamicMetadata():get(WADM_META_FILTER)
  -- The SQLi trap page is emitted by respond(), whose local reply still traverses this filter's
  -- encoder path. Bail out before any headers()/body() call so the page ships exactly as built.
  if not meta or meta["local_response"] then
    return
  end
  local plan = plans[meta["request_path"]] or plan_default
  local headers = response_handle:headers()

  local hdrs = plan.headers
  if #hdrs > 0 then
    local hstart = now_us()
    for i = 1, #hdrs do
      -- replace, not add: every other edge sets the decoy header, so an upstream header of the
      -- same name must not survive alongside it.
      headers:replace(hdrs[i].name, hdrs[i].value)
    end
    response_handle:logWarn("WADM TOKEN http_headers inject (us): " .. (now_us() - hstart))
  end

  local cookies = plan.cookies
  if #cookies > 0 then
    local cstart = now_us()
    for i = 1, #cookies do
      headers:add("set-cookie", cookies[i])
    end
    response_handle:logWarn("WADM TOKEN cookies inject (us): " .. (now_us() - cstart))
  end

  if not plan.has_body then
    return
  end
  local ct = headers:get("content-type")
  if not ct or not find(ct, "text/html", 1, true) then
    return
  end

  local body_handle = response_handle:body()
  if not body_handle then
    return
  end
  local body = body_handle:getBytes(0, body_handle:length())

  local extras = plan.extras
  for i = 1, #extras do
    local p = extras[i]
    local extra_start = now_us()
    local nb = splice_before(body, p.anchor, p.markup)
    local extra_delta = now_us() - extra_start
    if nb then body = nb end
    response_handle:logWarn("WADM TOKEN " .. p.kind .. " inject (us): " .. extra_delta)
  end

  -- setBytes also rewrites Content-Length, so no separate header update is needed.
  local injection = plan.comments
  if injection then
    local injection_start = now_us()
    body_handle:setBytes(splice_before(body, "</body>", injection) or (body .. injection))
    local injection_end = now_us()
    response_handle:logWarn(
      "Envoy Lua Injection execution time (us): " .. (injection_end - injection_start)
    )
  else
    body_handle:setBytes(body)
  end
end
