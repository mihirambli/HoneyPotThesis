-- sqli.lua — shared fake SQL-injection trap logic for the Apache edge (profile: apache).
-- Required by login.lua (the handler that answers the trap), and by detect.lua / inject.lua,
-- which use owns() to step aside for a request the trap has taken over.
--
-- Apache is the only edge whose request-phase hooks (LuaHookAccessChecker, LuaHookFixups) run
-- BEFORE the content handler, so they cannot be short-circuited by the handler the way the
-- other three edges' single filter can. Sharing one ownership predicate is what keeps the
-- observable behaviour identical across all four edges.

package.path = "/usr/local/apache2/scripts/?.lua;" .. package.path
local json = require("json")

-- Config loaded once at module scope; LuaScope thread means this runs once per worker thread.
local _cfg_file = io.open("/usr/local/apache2/conf/config.json", "r")
local _config   = json.decode(_cfg_file:read("*a"))
_cfg_file:close()

local M = {}

M.config = _config.sql_injection

-- Per-token on/off switch, same semantics as every other WADM kind.
local function enabled(cfg)
    local v = cfg.enabled
    if v == nil then return true end
    if v == true or v == 1 then return true end
    if type(v) == "string" then
        local s = v:lower()
        return s == "1" or s == "on" or s == "true" or s == "yes"
    end
    return false
end

-- Owned = the request the trap answers itself. Scoped by method AND path so the benchmarked
-- GET population (including GET /api/login?password=…) is untouched.
function M.owns(method, uri)
    local cfg = M.config
    if not cfg or not enabled(cfg) then return false end
    local ok_method = false
    for _, m in ipairs(cfg.methods or {}) do
        if m == method then ok_method = true end
    end
    if not ok_method then return false end
    for _, pattern in ipairs(cfg.paths or {}) do
        if pattern == "/*" or pattern == uri then return true end
    end
    return false
end

local function url_decode(s)
    s = s:gsub("+", " ")
    return (s:gsub("%%(%x%x)", function(h) return string.char(tonumber(h, 16)) end))
end

-- Signatures are stored pre-lowercased and space-normalised, so the same folding must be
-- applied to the input. ASCII-only lowercasing keeps this byte-identical to the WASM edge.
function M.normalize(v)
    return (url_decode(v):lower():gsub("%s+", " "))
end

-- Ordered pair list keeping duplicates: a map would be unordered and would drop repeated
-- keys, making the reflected payload differ between edges on a multi-field hit.
function M.parse_pairs(raw)
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
function M.match(body_pairs)
    local cfg = M.config
    for _, field in ipairs(cfg.watch_fields or {}) do
        for _, pair in ipairs(body_pairs) do
            if pair.key == field then
                local norm = M.normalize(pair.value)
                for _, sig in ipairs(cfg.signatures or {}) do
                    if norm:find(sig, 1, true) then
                        return { field = field, value = pair.value, signature = sig }
                    end
                end
            end
        end
    end
    return nil
end

-- The trap reflects attacker-controlled input; without escaping the honeypot would itself be
-- a live reflected-XSS vector against anyone who views the page.
function M.html_escape(s)
    s = s:gsub("&", "&amp;"):gsub("<", "&lt;"):gsub(">", "&gt;")
    s = s:gsub('"', "&quot;"):gsub("'", "&#39;")
    return s
end

function M.render(template, payload)
    local escaped = M.html_escape(payload:sub(1, M.config.reflect_max_len or 200))
    -- Replacement FUNCTION, not string: a payload such as `100%' OR 1=1--` keeps a bare `%`,
    -- which gsub would reject as an invalid replacement escape and turn into a 500.
    return (template:gsub("{PAYLOAD}", function() return escaped end))
end

-- CR/LF would let an attacker forge whole log lines that the benchmark scrapers read.
function M.log_safe(s)
    return (s:gsub("[\r\n]", " "))
end

return M
