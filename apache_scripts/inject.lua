-- inject.lua — mod_lua WADM HTML honeytoken injection for Apache edge (profile: apache).
-- Registered via: LuaOutputFilter WADM_INJECT /usr/local/apache2/scripts/inject.lua handle_inject
--                 (body injection) and LuaHookFixups ... handle_headers (response headers).
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
local sqli = require("sqli")

-- Config loaded once at module scope; LuaScope thread means this runs once per worker thread.
local _cfg_file = io.open("/usr/local/apache2/conf/config.json", "r")
local _config   = json.decode(_cfg_file:read("*a"))
_cfg_file:close()

local _html_comments = _config.honeytokens
    and _config.honeytokens.html_comments
    or {}

-- Per-token on/off switch (all kinds): active unless explicitly disabled. 1/on/true/yes = on;
-- anything else = off; absent = on (backward compatible).
local function token_enabled(token)
    local v = token.enabled
    if v == nil then return true end
    if v == true or v == 1 then return true end
    if type(v) == "string" then
        local s = v:lower()
        return s == "1" or s == "on" or s == "true" or s == "yes"
    end
    return false
end

-- "/*" matches every path, otherwise exact match against the request URI.
local function path_matches(paths, uri)
    if not paths then return false end
    for _, pattern in ipairs(paths) do
        if pattern == "/*" or pattern == uri then return true end
    end
    return false
end

-- Setup helper (runs outside the timed region): select every ENABLED comment whose path
-- patterns match this request, mirroring the OpenResty / Envoy / WASM path-matching logic.
local function comments_for_path(uri)
    local to_inject = {}
    for _, token in ipairs(_html_comments) do
        if token_enabled(token) and token.paths then
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

-- Additional-kind body payloads (parity with OpenResty): hidden form fields spliced before
-- </form>, decoy links before </body>. Each entry is { kind, markup, anchor }. Building the
-- markup is setup and happens here, outside the per-kind splice timers in handle_inject.
local function extra_body_payloads(uri)
    local ht = _config.honeytokens or {}
    local out = {}
    for _, t in ipairs(ht.decoy_paths or {}) do
        if token_enabled(t) and (t.advertise_via or "link") == "link"
           and path_matches(t.advertise_on_paths, uri) then
            out[#out + 1] = {
                kind = "decoy_paths",
                markup = '<a href="' .. (t.trap_path or "") .. '" style="display:none">'
                         .. (t.link_text or "") .. '</a>',
                anchor = "</body>",
            }
        end
    end
    for _, t in ipairs(ht.form_fields or {}) do
        if token_enabled(t) and path_matches(t.paths, uri)
           and t.field_name and t.field_name ~= "" then
            out[#out + 1] = {
                kind = "form_fields",
                markup = '<input type="hidden" name="' .. t.field_name .. '" value="'
                         .. (t.field_value or "") .. '">',
                anchor = "</form>",
            }
        end
    end
    return out
end

-- Response-header injection (http_headers + cookies). Runs as a Fixups hook — a request-phase
-- hook that fires before mod_proxy generates the response — because Apache must stage response
-- headers before ProxyPass. err_headers_out is used so the values survive the proxied response.
function handle_headers(r)
    -- Step aside for a request the SQLi trap owns, so its page ships without the bait header
    -- and Set-Cookie the other three edges also suppress on a local reply. This hook cannot
    -- read a note left by login.lua: Fixups runs in the request phase, before the handler.
    if sqli.owns(r.method, r.uri) then
        return apache2.DECLINED
    end

    -- Token selection is setup and stays outside; the timed region is the header write itself.
    local ht = _config.honeytokens or {}
    local uri = r.uri or "/"

    local header_tokens = {}
    for _, t in ipairs(ht.http_headers or {}) do
        if token_enabled(t) and path_matches(t.paths, uri)
           and t.header_name and t.header_name ~= "" then
            header_tokens[#header_tokens + 1] = t
        end
    end
    if #header_tokens > 0 then
        local hstart = r:clock()
        for _, t in ipairs(header_tokens) do
            r.err_headers_out[t.header_name] = t.header_value or ""
        end
        r:warn("WADM TOKEN http_headers inject (us): " .. tostring(r:clock() - hstart))
    end

    local cookie_tokens = {}
    for _, t in ipairs(ht.cookies or {}) do
        if token_enabled(t) and path_matches(t.paths, uri)
           and t.cookie_name and t.cookie_name ~= "" then
            cookie_tokens[#cookie_tokens + 1] = t
        end
    end
    if #cookie_tokens > 0 then
        local cstart = r:clock()
        for _, t in ipairs(cookie_tokens) do
            local c = t.cookie_name .. "=" .. (t.cookie_value or "")
            if t.attributes and t.attributes ~= "" then c = c .. "; " .. t.attributes end
            r.err_headers_out["Set-Cookie"] = c
        end
        r:warn("WADM TOKEN cookies inject (us): " .. tostring(r:clock() - cstart))
    end

    return apache2.DECLINED
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
    -- SetOutputFilter applies unconditionally, so the SQLi trap page (which is text/html)
    -- takes the same passthrough route rather than being stamped with honeytokens.
    local is_html = r.content_type and r.content_type:find("text/html", 1, true)
    if not is_html or sqli.owns(r.method, r.uri) then
        while bucket ~= nil do
            coroutine.yield(bucket)
        end
        return
    end

    -- Setup (outside timer): resolve html_comments for this path plus additional-kind payloads.
    local uri = r.uri or "/"
    local injection = table.concat(comments_for_path(uri), "\n")
    local extra = extra_body_payloads(uri)

    -- Buffer the whole body across brigade chunks (untimed), matching OpenResty's
    -- ctx.body_chunks accumulation. Emitting "" holds output back until end-of-stream.
    local chunks = {}
    while bucket ~= nil do
        chunks[#chunks + 1] = bucket
        coroutine.yield("")
    end

    -- Nothing matches → emit the untouched body, untimed (nothing was injected).
    if injection == "" and #extra == 0 then
        coroutine.yield(table.concat(chunks))
        return
    end

    if #extra == 0 then
        -- Unchanged benchmarked path: assemble body → locate first </body> → splice → write back.
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
        coroutine.yield(new_body)
    else
        -- Additional-kind payloads present: splice each under its own per-kind timer, then run
        -- the html_comments splice under its (unchanged) timer on the already-assembled body.
        -- The per-kind timed region is locate-anchor → splice only; the single yield that emits
        -- the finished body happens once, outside.
        local body = table.concat(chunks)
        for _, p in ipairs(extra) do
            local extra_start = r:clock()
            local nb = body:gsub(p.anchor, p.markup .. "\n" .. p.anchor, 1)
            local extra_end = r:clock()
            if nb ~= body then body = nb end
            r:warn("WADM TOKEN " .. p.kind .. " inject (us): " .. tostring(extra_end - extra_start))
        end
        if injection ~= "" then
            local start_time = r:clock()
            local new_body = body:gsub("</body>", injection .. "\n</body>", 1)
            if new_body == body then
                new_body = body .. injection
            end
            local end_time = r:clock()
            r:warn("Apache Injection execution time (us): " .. tostring(end_time - start_time))
            coroutine.yield(new_body)
        else
            coroutine.yield(body)
        end
    end
end
