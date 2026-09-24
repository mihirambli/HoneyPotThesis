-- login.lua — mod_lua content handler for the fake SQL-injection trap on the Apache edge.
-- Registered via: LuaMapHandler "^/api/login$" /usr/local/apache2/scripts/login.lua handle_login
--
-- lua_map_handler is hooked at AP_LUA_HOOK_FIRST, ahead of mod_proxy's handler, so returning a
-- value other than DECLINED terminates the request here and the origin is never contacted.
-- Returning DECLINED lets ProxyPass run exactly as before, which is what keeps every
-- non-owned request (notably the benchmarked GET /api/login?password=…) on its old path.

package.path = "/usr/local/apache2/scripts/?.lua;" .. package.path
require "apache2"
local wadm = require("wadm")

-- Bound on how much of the body is read; the trap only ever inspects small login forms.
local MAX_BODY = 65536

-- In-memory attacker store at module scope, mirroring detect.lua's. mod_lua gives each script
-- file its own Lua state, so this cannot be the same table detect.lua writes — but it is the
-- same write-only bookkeeping every edge performs on a hit, which is what parity requires.
local detected_ips = {}

function handle_login(r)
    local path = wadm.request_path(r.unparsed_uri)
    if not wadm.sqli_owns(r, path) then
        return apache2.DECLINED
    end

    local cfg = wadm.sqli
    local ip = r.useragent_ip or "unknown"
    local method = r.method
    local raw = r:requestbody(nil, MAX_BODY) or ""

    -- Closed before the hit/miss branch so every arm measures the same unit: the scan alone,
    -- with page rendering and the IP record outside it. The scan covers decoding each body pair,
    -- normalising the watched values and walking the signature list, and the benchmark drives all
    -- four combinations of outcome and encoding to attribute the cost between them.
    local start_time = r:clock()
    local hit = wadm.sqli_match(raw)
    local delta = r:clock() - start_time

    local body, status
    if hit then
        body   = wadm.sqli_render(cfg.error_template, hit.value)
        status = cfg.status_code or 500
    else
        body   = cfg.deny_template
        status = cfg.deny_status_code or 401
    end

    -- The IP record stays outside the timed region, unlike the honeytoken kinds':
    -- only the hit arms perform it, so including it would surface as a hit-vs-miss
    -- difference that has nothing to do with the scan.
    -- Percent-encoded input is an evasion technique worth recording in its own right, and it
    -- is also where the scan actually spends its time: url_decode's substitution path runs
    -- over every body pair and again during normalisation, while most signatures are rejected
    -- on length before a character is compared. Recording the four arms separately is what
    -- lets detection cost be attributed to normalisation rather than to scan depth. Classified
    -- after the timer closes so it never enters the measurement.
    local kind = hit and "sql_injection" or "sql_injection_miss"
    if raw:find("%", 1, true) then kind = kind .. "_encoded" end

    if hit then
        detected_ips[ip] = true
        r:warn("WADM ALERT: honeytoken triggered by " .. ip
            .. " — sql_injection signature '" .. hit.signature .. "' in field '" .. hit.field
            .. "' on " .. method .. " " .. path
            .. " (payload '" .. wadm.log_safe(hit.value) .. "')")
    else
        -- These requests never reach the origin, so without an edge access log this is the
        -- only record that the trap endpoint was hit.
        r:warn("WADM TRAP: " .. ip .. " " .. method .. " " .. path
            .. " answered locally with " .. status .. " (no signature matched)")
    end
    r:warn("WADM TOKEN " .. kind .. " detect (us): " .. tostring(delta))

    -- r.status must be assigned before the first r:puts, and the handler must return OK rather
    -- than the numeric status — returning a status code makes Apache discard this body and
    -- render its own ErrorDocument instead.
    r.status = status
    r.content_type = "text/html; charset=UTF-8"
    r:puts(body)
    return apache2.OK
end
