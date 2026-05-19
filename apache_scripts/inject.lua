-- inject.lua — mod_lua WADM HTML honeytoken injection filter for Apache edge (profile: apache).
-- Registered via: LuaOutputFilter WADM_INJECT /usr/local/apache2/scripts/inject.lua handle_inject
-- Applied via:    SetOutputFilter WADM_INJECT inside <VirtualHost> in httpd.conf.
-- Content-Length is stripped by "Header always unset Content-Length" in httpd.conf so the
-- body size increase from injection does not cause a length mismatch.

package.path = "/usr/local/apache2/scripts/?.lua;" .. package.path
require "apache2"
local json = require("json")

-- Config loaded once at module scope; LuaScope thread means this runs once per worker thread.
local _cfg_file = io.open("/usr/local/apache2/conf/config.json", "r")
local _config   = json.decode(_cfg_file:read("*a"))
_cfg_file:close()

local comment_value = _config.honeytokens
    and _config.honeytokens.html_comments
    and _config.honeytokens.html_comments[1]
    and _config.honeytokens.html_comments[1].comment_value

-- Output filter coroutine driven by Apache's bucket brigade:
--   1. First coroutine.yield() signals we are ready; Apache sets the global `bucket`.
--   2. While loop processes each chunk: gsub injects comment before </body> if present.
--   3. Final coroutine.yield() is the required clean-close signal.
--
-- SetOutputFilter applies this filter to every response. The </body> gsub is a no-op on
-- non-HTML content (binary, JSON, CSS) so no guard is needed; those pass through unchanged.
-- Content-Length is already removed by httpd.conf so growing the body is safe.
function handle_inject(r)
    coroutine.yield()               -- signal ready; Apache populates `bucket` after this

    while bucket ~= nil do
        local output = bucket:gsub("</body>", comment_value .. "\n</body>")
        coroutine.yield(output)     -- pass chunk (modified or unchanged) into filter chain
    end

    coroutine.yield()               -- clean close; no footer appended
end
