-- login.lua — mod_lua content handler for the fake SQL-injection trap on the Apache edge.
-- Registered via: LuaMapHandler "^/api/login$" /usr/local/apache2/scripts/login.lua handle_login
--
-- lua_map_handler is hooked at AP_LUA_HOOK_FIRST, ahead of mod_proxy's handler, so returning a
-- value other than DECLINED terminates the request here and the origin is never contacted.
-- Returning DECLINED lets ProxyPass run exactly as before, which is what keeps every
-- non-owned request (notably the benchmarked GET /api/login?password=…) on its old path.

package.path = "/usr/local/apache2/scripts/?.lua;" .. package.path
require "apache2"
local sqli = require("sqli")

-- Bound on how much of the body is read; the trap only ever inspects small login forms.
local MAX_BODY = 65536

-- In-memory attacker store at module scope, mirroring detect.lua's. mod_lua gives each script
-- file its own Lua state, so this cannot be the same table detect.lua writes — but it is the
-- same write-only bookkeeping every edge performs on a hit, which is what parity requires.
local detected_ips = {}

function handle_login(r)
    if not sqli.owns(r.method, r.uri) then
        return apache2.DECLINED
    end

    local cfg = sqli.config
    local raw = r:requestbody(nil, MAX_BODY) or ""

    local start_time = r:clock()
    local hit = sqli.match(sqli.parse_pairs(raw))
    local body, status
    if hit then
        body   = sqli.render(cfg.error_template, hit.value)
        status = cfg.status_code or 500
    else
        body   = cfg.deny_template
        status = cfg.deny_status_code or 401
    end
    local delta = r:clock() - start_time

    if hit then
        detected_ips[r.useragent_ip or "unknown"] = os.time()
        r:warn("WADM ALERT: honeytoken triggered by " .. (r.useragent_ip or "unknown")
            .. " — sql_injection signature '" .. hit.signature .. "' in field '" .. hit.field
            .. "' on " .. r.method .. " " .. r.uri
            .. " (payload '" .. sqli.log_safe(hit.value) .. "')")
        -- Label deliberately shares no substring with the benchmark scrapers'
        -- "Detection/Injection execution time (us):" patterns.
        r:warn("Apache WADM SQLI trap build (us): " .. tostring(delta))
    end

    -- r.status must be assigned before the first r:puts, and the handler must return OK rather
    -- than the numeric status — returning a status code makes Apache discard this body and
    -- render its own ErrorDocument instead.
    r.status = status
    r.content_type = "text/html; charset=UTF-8"
    r:puts(body)
    return apache2.OK
end
