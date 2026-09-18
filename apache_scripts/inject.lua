-- inject.lua — mod_lua WADM HTML body injection for the Apache edge (profile: apache).
-- Registered via: LuaOutputFilter WADM_INJECT /usr/local/apache2/scripts/inject.lua handle_inject
-- Applied via:    SetOutputFilter WADM_INJECT inside <VirtualHost> in httpd.conf.
-- Content-Length is stripped by "Header always unset Content-Length" in httpd.conf so the
-- body size increase from injection does not cause a length mismatch. Response-header baits
-- are staged earlier, by detect.lua.
--
-- Canonical injection contract (shared with OpenResty / Envoy+Lua / WASM):
--   • content-type guard + plan lookup are setup → OUTSIDE the timers
--   • the whole response body is buffered across brigade chunks BEFORE any timer starts
--   • per-kind timers wrap locate-anchor → splice; the html_comments timer wraps the splice
--     (the final yield cannot be timed: it hands control back to Apache)

package.path = "/usr/local/apache2/scripts/?.lua;" .. package.path
require "apache2"
local wadm = require("wadm")

local find, concat = string.find, table.concat
local splice_before = wadm.splice_before

-- Output filter coroutine driven by Apache's bucket brigade:
--   1. First coroutine.yield() signals we are ready; Apache then sets the global `bucket`.
--   2. Responses with nothing to inject stream through unchanged (never buffered).
--   3. Otherwise every chunk is accumulated (yielding "" emits nothing) until end-of-stream,
--      then a single whole-body transform runs and is emitted at the final yield.
function handle_inject(r)
    coroutine.yield()

    -- SetOutputFilter applies unconditionally, so the SQLi trap page (which is text/html) takes
    -- the passthrough route rather than being stamped with honeytokens.
    local plan
    local ct = r.content_type
    if ct and find(ct, "text/html", 1, true) then
        local path = wadm.request_path(r.unparsed_uri)
        if not wadm.sqli_owns(r, path) then
            plan = wadm.plan_for(path)
            if not plan.has_body then plan = nil end
        end
    end

    if not plan then
        while bucket ~= nil do
            coroutine.yield(bucket)
        end
        return
    end

    local chunks = {}
    while bucket ~= nil do
        chunks[#chunks + 1] = bucket
        coroutine.yield("")
    end
    local body = concat(chunks)

    local extras = plan.extras
    for i = 1, #extras do
        local p = extras[i]
        local extra_start = r:clock()
        local nb = splice_before(body, p.anchor, p.markup)
        local extra_end = r:clock()
        if nb then body = nb end
        r:warn("WADM TOKEN " .. p.kind .. " inject (us): " .. tostring(extra_end - extra_start))
    end

    local injection = plan.comments
    if injection then
        local start_time = r:clock()
        body = splice_before(body, "</body>", injection) or (body .. injection)
        local end_time = r:clock()
        r:warn("Apache Injection execution time (us): " .. tostring(end_time - start_time))
    end
    coroutine.yield(body)
end
