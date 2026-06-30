-- detect.lua — mod_lua WADM detection and cleaning hook for Apache edge (profile: apache).
-- Registered via: LuaHookAccessChecker /usr/local/apache2/scripts/detect.lua handle_detect early
-- Mirrors envoy_scripts/injection.lua envoy_on_request: inspects query string, strips trigger keywords,
-- logs WADM ALERT. Returns apache2.DECLINED so ProxyPass always proceeds (non-blocking).

package.path = "/usr/local/apache2/scripts/?.lua;" .. package.path
require "apache2"
local json = require("json")

-- Config loaded once at module scope; LuaScope thread means this runs once per worker thread.
local _cfg_file = io.open("/usr/local/apache2/conf/config.json", "r")
local _config   = json.decode(_cfg_file:read("*a"))
_cfg_file:close()

-- Collect all non-empty trigger keywords from every html_comments entry (mirrors Envoy behavior).
local trigger_keywords = {}
if _config.honeytokens and _config.honeytokens.html_comments then
    for _, token in ipairs(_config.honeytokens.html_comments) do
        if token.trigger_keyword and token.trigger_keyword ~= "" then
            trigger_keywords[#trigger_keywords + 1] = token.trigger_keyword
        end
    end
end

-- Inspect the query string for any trigger keyword; strip each hit from r.args.
-- r.args is a writable mod_lua field — assigning it rewrites the query string seen by mod_proxy upstream.
function handle_detect(r)
    local start_time = r:clock()

    if #trigger_keywords == 0 or not r.args or r.args == "" then
        return apache2.DECLINED
    end

    local ip = r.useragent_ip or "unknown"

    for _, keyword in ipairs(trigger_keywords) do
        if r.args:find(keyword, 1, true) then
            r:warn("WADM ALERT: honeytoken triggered by " .. ip
                   .. " — keyword '" .. keyword .. "' found in query string")

            -- Remove every key=value segment containing the keyword plus its adjacent & separator.
            r.args = r.args:gsub("[^&]*" .. keyword .. "[^&]*&?", "")
            -- Clean a stray leading & left when the matched segment was the last one.
            r.args = r.args:gsub("^&", "")
        end
    end

    -- DECLINED: not the authoritative access handler; continue to ProxyPass.
    local end_time = r:clock()
    r:warn("Apache Detection execution time (us): " .. tostring(end_time - start_time))
    return apache2.DECLINED
end
