package.path = "/etc/envoy/scripts/?.lua;" .. package.path

local json = require("json")

-- Load honeytoken definitions once when the filter script loads (path is bind-mounted from docker-compose).
local file = io.open("/etc/envoy/config.json", "r")
local content = file:read("*a")
file:close()
local config = json.decode(content)

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
    if token.trigger_keyword and token.trigger_keyword ~= "" then
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
    if token.paths then
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

-- Envoy hook: inspect and optionally rewrite request path/body before routing to the cluster.
function envoy_on_request(request_handle)
  -- Setup runs *before* the timer (matches OpenResty): building the trigger table,
  -- reading the client IP and any known-attacker lookup are excluded from the timed
  -- region so detection measures only query scan + strip + in-memory record.
  local triggers = get_trigger_keywords()
  if #triggers == 0 then
    return
  end

  local ip = request_handle:headers():get("x-forwarded-for")
      or request_handle:headers():get("x-real-ip")
      or "unknown"

  if is_known_attacker(ip) then
    request_handle:logWarn(
      "WADM ALERT: known attacker IP " .. ip .. " detected in new request"
    )
  end

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
          request_handle:logWarn(
            "WADM ALERT: honeytoken triggered by " .. ip
            .. " — keyword '" .. keyword
            .. "' found in query param '" .. key .. "=" .. val .. "'"
          )
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
            request_handle:logWarn(
              "WADM ALERT: honeytoken triggered by " .. ip
              .. " — keyword '" .. keyword
              .. "' found in POST param '" .. key .. "=" .. val .. "'"
            )
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
          request_handle:logWarn(
            "WADM ALERT: honeytoken triggered by " .. ip
            .. " — keyword '" .. keyword
            .. "' found in request body"
          )
          detected = true
        end
      end
    end
  end
  end -- post_body_inspection

  -- On detection, record the attacker IP in the in-memory store (mirrors OpenResty's
  -- wadm:set) and log the timing. Only trigger-bearing requests are timed so all edges
  -- sample the same population (the keyword-matching request).
  if detected or dirty then
    record_attacker_ip(ip)
    request_handle:logWarn(
      "Envoy Lua Detection execution time (us): "
      .. (get_micro_time() - detection_start)
    )
  end
end

-- Envoy hook: mutate HTML responses from upstream to embed honeytoken HTML comments.
function envoy_on_response(response_handle)
  local ct = response_handle:headers():get("content-type") or ""
  if not ct:find("text/html", 1, true) then
    return
  end
  local injection_start = get_micro_time()

  local request_path = response_handle:headers():get(":path") or "/"
  local uri = request_path:match("^([^?]+)") or request_path

  local to_inject = get_comments_for_path(uri)
  if #to_inject == 0 then
    return
  end

  local body = response_handle:body():getBytes(0, response_handle:body():length())
  local body_str = tostring(body)

  local injection = table.concat(to_inject, "\n")
  local new_body = body_str:gsub("</body>", injection .. "\n</body>", 1)

  if new_body == body_str then
    new_body = body_str .. injection
  end

  response_handle:body():setBytes(new_body)
  response_handle:headers():replace("content-length", tostring(#new_body))
  response_handle:logWarn(
    "Envoy Lua Injection execution time (us): "
    .. (get_micro_time() - injection_start)
  )
end
