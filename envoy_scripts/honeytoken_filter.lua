package.path = "/etc/envoy/scripts/?.lua;" .. package.path

local json = require("json")

local config = nil

local function load_config()
  if config then
    return config
  end
  local file = io.open("/etc/envoy/config.json", "r")
  if not file then
    return nil
  end
  local content = file:read("*a")
  file:close()
  local ok, parsed = pcall(json.decode, content)
  if not ok then
    return nil
  end
  config = parsed
  return config
end

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

local function get_trigger_keywords(cfg)
  local triggers = {}
  if not cfg or not cfg.honeytokens or not cfg.honeytokens.html_comments then
    return triggers
  end
  for _, token in ipairs(cfg.honeytokens.html_comments) do
    if token.trigger_keyword and token.trigger_keyword ~= "" then
      triggers[#triggers + 1] = token.trigger_keyword
    end
  end
  return triggers
end

local function get_comments_for_path(cfg, uri)
  local to_inject = {}
  if not cfg or not cfg.honeytokens or not cfg.honeytokens.html_comments then
    return to_inject
  end
  for _, token in ipairs(cfg.honeytokens.html_comments) do
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
  local cfg = load_config()
  if not cfg then
    return
  end

  local triggers = get_trigger_keywords(cfg)
  if #triggers == 0 then
    return
  end

  local path = request_handle:headers():get(":path") or "/"
  local query_start = path:find("?")
  if not query_start then
    return
  end

  local query_string = path:sub(query_start + 1)
  local params = parse_query_string(query_string)
  local ip = request_handle:headers():get("x-forwarded-for")
      or request_handle:headers():get("x-real-ip")
      or "unknown"

  for key, val in pairs(params) do
    for _, keyword in ipairs(triggers) do
      if key:find(keyword, 1, true) or val:find(keyword, 1, true) then
        request_handle:logWarn(
          "WADM ALERT: honeytoken triggered by " .. ip
          .. " — keyword '" .. keyword
          .. "' found in query param '" .. key .. "=" .. val .. "'"
        )
      end
    end
  end
end

function envoy_on_response(response_handle)
  local cfg = load_config()
  if not cfg then
    return
  end

  local ct = response_handle:headers():get("content-type") or ""
  if not ct:find("text/html", 1, true) then
    return
  end

  local request_path = response_handle:headers():get(":path") or "/"
  local uri = request_path:match("^([^?]+)") or request_path

  local to_inject = get_comments_for_path(cfg, uri)
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
