package.path = "/etc/envoy/scripts/?.lua;" .. package.path

local json = require("json")

local file = io.open("/etc/envoy/config.json", "r")
local content = file:read("*a")
file:close()
local config = json.decode(content)

local function url_decode(str)
  str = str:gsub("+", " ")
  str = str:gsub("%%(%x%x)", function(h)
    return string.char(tonumber(h, 16))
  end)
  return str
end

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

local function url_encode(str)
  str = str:gsub("([^%w%-_.~])", function(c)
    return string.format("%%%02X", string.byte(c))
  end)
  return str
end

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

function envoy_on_request(request_handle)
  local triggers = get_trigger_keywords()
  if #triggers == 0 then
    return
  end

  local path = request_handle:headers():get(":path") or "/"
  local ip = request_handle:headers():get("x-forwarded-for")
      or request_handle:headers():get("x-real-ip")
      or "unknown"

  local query_start = path:find("?")
  if query_start then
    local base_path = path:sub(1, query_start - 1)
    local query_string = path:sub(query_start + 1)
    local params = parse_query_string(query_string)
    local dirty = false

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

  local body_handle = request_handle:body()
  if body_handle and body_handle:length() > 0 then
    local body_str = tostring(body_handle:getBytes(0, body_handle:length()))
    for _, keyword in ipairs(triggers) do
      if body_str:find(keyword, 1, true) then
        request_handle:logWarn(
          "WADM ALERT: honeytoken triggered by " .. ip
          .. " — keyword '" .. keyword
          .. "' found in request body"
        )
      end
    end
  end
end

function envoy_on_response(response_handle)
  local ct = response_handle:headers():get("content-type") or ""
  if not ct:find("text/html", 1, true) then
    return
  end

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
end
