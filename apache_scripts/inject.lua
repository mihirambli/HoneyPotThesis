-- inject.lua — mod_lua WADM HTML honeytoken injection filter for Apache edge (profile: apache).
-- Registered via: LuaOutputFilter WADM_INJECT /usr/local/apache2/scripts/inject.lua handle_inject
-- Applied via:    SetOutputFilter WADM_INJECT inside <VirtualHost> in httpd.conf.
-- Content-Length is stripped by "Header always unset Content-Length" in httpd.conf so the
-- body size increase from injection does not cause a length mismatch.
--
-- Canonical injection contract (shared with OpenResty / Envoy+Lua / WASM):
--   • content-type guard + path matching + token join are setup → OUTSIDE the timer
--   • the whole response body is buffered across brigade chunks BEFORE the timer starts
--     (was previously a per-chunk gsub — see apache_scripts/README.md history)
--   • timed region = assemble body → locate first </body> → splice → write body back
--   • Content-Length handling is external (httpd.conf `Header always unset Content-Length`)

package.path = "/usr/local/apache2/scripts/?.lua;" .. package.path
require "apache2"
local json = require("json")

-- Config loaded once at module scope; LuaScope thread means this runs once per worker thread.
local _cfg_file = io.open("/usr/local/apache2/conf/config.json", "r")
local _config   = json.decode(_cfg_file:read("*a"))
_cfg_file:close()

local _html_comments = _config.honeytokens
    and _config.honeytokens.html_comments
    or {}

-- Setup helper (runs outside the timed region): select every comment whose path patterns
-- match this request, mirroring the OpenResty / Envoy / WASM path-matching logic exactly.
local function comments_for_path(uri)
    local to_inject = {}
    for _, token in ipairs(_html_comments) do
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

-- Output filter coroutine driven by Apache's bucket brigade:
--   1. First coroutine.yield() signals we are ready; Apache then sets the global `bucket`.
--   2. Non-HTML responses stream through unchanged (never buffered).
--   3. For HTML, every chunk is accumulated (yielding "" emits nothing) until end-of-stream,
--      then a single whole-body transform runs and is emitted at the final yield.
function handle_inject(r)
    coroutine.yield()               -- signal ready; Apache populates `bucket` after this

    -- Content-type guard (setup): only HTML is buffered/injected. Anything else passes
    -- through chunk-by-chunk unchanged, so we never buffer binary/JSON/CSS bodies.
    local is_html = r.content_type and r.content_type:find("text/html", 1, true)
    if not is_html then
        while bucket ~= nil do
            coroutine.yield(bucket)
        end
        return
    end

    -- Setup (outside timer): resolve comments for this request path and join them.
    local injection = table.concat(comments_for_path(r.uri or "/"), "\n")

    -- Buffer the whole body across brigade chunks (untimed), matching OpenResty's
    -- ctx.body_chunks accumulation. Emitting "" holds output back until end-of-stream.
    local chunks = {}
    while bucket ~= nil do
        chunks[#chunks + 1] = bucket
        coroutine.yield("")
    end

    -- No matching honeytoken → emit the untouched body, untimed (nothing was injected).
    if injection == "" then
        coroutine.yield(table.concat(chunks))
        return
    end

    -- Timed region: assemble body → locate first </body> → splice → write body back.
    -- The `1` count limits the substitution to the first </body> so every edge does the
    -- same first-match splice (OpenResty ngx.re.sub / Envoy gsub count=1 / WASM find).
    local start_time = r:clock()
    local body = table.concat(chunks)
    local new_body = body:gsub("</body>", injection .. "\n</body>", 1)
    if new_body == body then
        new_body = body .. injection
    end
    local end_time = r:clock()
    r:warn("Apache Injection execution time (us): " .. tostring(end_time - start_time))

    coroutine.yield(new_body)       -- emit the whole modified body at end-of-stream
end
