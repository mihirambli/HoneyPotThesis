-- detect.lua — mod_lua request-phase hook for the Apache edge (profile: apache).
-- Registered via: LuaHookAccessChecker /usr/local/apache2/scripts/detect.lua handle_detect early
-- Detects (additional kinds, then html_comments), strips triggers from the query string, and
-- stages the response-header baits. Returns apache2.DECLINED so ProxyPass always proceeds.

package.path = "/usr/local/apache2/scripts/?.lua;" .. package.path
require "apache2"
local wadm = require("wadm")

local kinds = wadm.kinds
local triggers = wadm.triggers
local format_alert = wadm.format_alert

-- In-memory attacker store at module scope (persists across requests within this worker
-- thread's Lua state), mirroring OpenResty's ngx.shared.wadm_state. The detection timers record
-- into it on a hit, so all edges do an equivalent in-memory state write inside the measured region.
local detected_ips = {}

local function detect_kinds(r, ctx)
    local ip = ctx.ip
    for i = 1, #kinds do
        local k = kinds[i]
        -- Timed region = the kind's scan + the in-memory IP record. ctx is built before and
        -- alert rendering/logging happens after, as on every edge.
        local hits = {}
        local kind_start = r:clock()
        k.detect(k.tokens, ctx, hits)
        if #hits > 0 then
            detected_ips[ip] = true
        end
        local kind_end = r:clock()

        if #hits > 0 then
            for j = 1, #hits do
                r:warn(format_alert(ip, hits[j]))
            end
            -- Timed only on a hit, so every edge samples the same population.
            r:warn("WADM TOKEN " .. k.name .. " detect (us): " .. tostring(kind_end - kind_start))
        end
    end
end

-- html_comments detection (the benchmarked reference). Query parsing happens in setup, so the
-- timed region is scan → strip → write back → record, the same on all edges. r.args is a
-- writable mod_lua field: assigning it rewrites the query string mod_proxy sends upstream.
local function detect_comments(r, ctx)
    if #triggers == 0 then return end
    local ip = ctx.ip
    local hits = {}
    local start_time = r:clock()

    local dropped = wadm.scan_segments(ctx.segs, "query param", hits)
    if dropped then
        r.args = wadm.join_kept(ctx.segs, dropped)
    end
    if #hits > 0 then
        detected_ips[ip] = true
    end

    local end_time = r:clock()

    if #hits > 0 then
        for j = 1, #hits do
            r:warn(format_alert(ip, hits[j]))
        end
        -- Only trigger-bearing requests are timed so all edges sample the same population.
        r:warn("Apache Detection execution time (us): " .. tostring(end_time - start_time))
    end
end

-- Response-header baits (http_headers + cookies). Staged here rather than in a separate Fixups
-- hook: err_headers_out set in the access phase survives ProxyPass just the same, and every
-- extra Lua hook costs a VM lookup and request-object setup on every request. Token selection
-- is precompiled; the timed region is the header write itself.
local function stage_headers(r, plan)
    local hdrs, cookies = plan.headers, plan.cookies
    if #hdrs == 0 and #cookies == 0 then return end
    local eho = r.err_headers_out

    if #hdrs > 0 then
        local hstart = r:clock()
        for i = 1, #hdrs do
            eho[hdrs[i].name] = hdrs[i].value
        end
        r:warn("WADM TOKEN http_headers inject (us): " .. tostring(r:clock() - hstart))
    end

    if #cookies > 0 then
        local cstart = r:clock()
        for i = 1, #cookies do
            -- mod_lua only exposes apr_table_set here, so several cookie tokens on one path
            -- would overwrite each other; the shipped config plants one.
            eho["Set-Cookie"] = cookies[i]
        end
        r:warn("WADM TOKEN cookies inject (us): " .. tostring(r:clock() - cstart))
    end
end

function handle_detect(r)
    local path = wadm.request_path(r.unparsed_uri)

    -- Step aside for a request the SQLi trap owns. This hook structurally runs before
    -- login.lua's handler, so without this a crafted POST /api/login?password=<trigger> would
    -- emit an extra alert and timing line on Apache alone, and its page would carry the bait
    -- headers the other three edges suppress on a local reply.
    if wadm.sqli_owns(r, path) then
        return apache2.DECLINED
    end

    local hin = r.headers_in
    local ctx = {
        ip = r.useragent_ip or "unknown",
        path = path,
        host = hin["Host"],
        segs = wadm.parse_query(r.args),
        cookie = wadm.need_cookie and hin["Cookie"] or nil,
    }

    detect_kinds(r, ctx)
    detect_comments(r, ctx)
    stage_headers(r, wadm.plan_for(path))

    -- DECLINED: not the authoritative access handler; continue to ProxyPass.
    return apache2.DECLINED
end
