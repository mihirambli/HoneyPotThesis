-- detect.lua — mod_lua WADM detection and cleaning hook for Apache edge (profile: apache).
-- Registered via: LuaHookAccessChecker /usr/local/apache2/scripts/detect.lua handle_detect early
-- Mirrors envoy_scripts/injection.lua envoy_on_request: inspects query string, strips trigger keywords,
-- logs WADM ALERT. Returns apache2.DECLINED so ProxyPass always proceeds (non-blocking).

package.path = "/usr/local/apache2/scripts/?.lua;" .. package.path
require "apache2"
local json = require("json")
local sqli = require("sqli")

-- Config loaded once at module scope; LuaScope thread means this runs once per worker thread.
local _cfg_file = io.open("/usr/local/apache2/conf/config.json", "r")
local _config   = json.decode(_cfg_file:read("*a"))
_cfg_file:close()

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

-- Read one cookie's value from the raw Cookie header (nil if not present).
local function cookie_value(cookie_header, name)
    if not cookie_header then return nil end
    for pair in cookie_header:gmatch("[^;]+") do
        local k, v = pair:match("^%s*(.-)%s*=%s*(.-)%s*$")
        if k == name then return v end
    end
    return nil
end

-- Substring scan of a request's target surface (path, Host, query args) for a replayed value.
local function keyword_in_request(kw, uri, host, args)
    if not kw or kw == "" then return false end
    if uri:find(kw, 1, true) then return true end
    if host and host:find(kw, 1, true) then return true end
    for k, v in pairs(args) do
        if tostring(k):find(kw, 1, true) or tostring(v):find(kw, 1, true) then return true end
    end
    return false
end

-- Collect all non-empty trigger keywords from every ENABLED html_comments entry.
local trigger_keywords = {}
if _config.honeytokens and _config.honeytokens.html_comments then
    for _, token in ipairs(_config.honeytokens.html_comments) do
        if token_enabled(token) and token.trigger_keyword and token.trigger_keyword ~= "" then
            trigger_keywords[#trigger_keywords + 1] = token.trigger_keyword
        end
    end
end

-- In-memory attacker store at module scope (persists across requests within this
-- worker thread's Lua state), mirroring OpenResty's ngx.shared.wadm_state. The
-- detection timer records into this table on a hit, so all edges do an equivalent
-- in-memory state write inside the measured region.
local detected_ips = {}

-- Per-kind detectors (parity with OpenResty nginx/nginx.conf). NOT gated on r.args — decoy
-- paths and cookies carry no query string. Each logs its own WADM ALERT lines and returns true
-- on any hit; keeping them separate lets detect_additional time one kind at a time. Note:
-- form-field tamper is a query-string check only here (Apache has no POST-body inspection),
-- which matches every edge's default behaviour while post_body_inspection is off.
local detect_kind = {
    http_headers = function(r, ip, ctx)
        local detected = false
        for _, t in ipairs(ctx.ht.http_headers or {}) do
            if token_enabled(t) and keyword_in_request(t.trigger_keyword, ctx.uri, ctx.host, ctx.args) then
                r:warn("WADM ALERT: honeytoken triggered by " .. ip
                    .. " — http_header value '" .. tostring(t.trigger_keyword) .. "' replayed in request")
                detected = true
            end
        end
        return detected
    end,

    cookies = function(r, ip, ctx)
        local detected = false
        for _, t in ipairs(ctx.ht.cookies or {}) do
            if token_enabled(t) then
                if t.cookie_name and t.cookie_name ~= "" then
                    local v = cookie_value(ctx.cookie, t.cookie_name)
                    if v ~= nil and v ~= (t.cookie_value or "") then
                        r:warn("WADM ALERT: honeytoken triggered by " .. ip
                            .. " — cookie '" .. t.cookie_name .. "' tampered (got '" .. v
                            .. "', expected '" .. (t.cookie_value or "") .. "')")
                        detected = true
                    end
                end
                if keyword_in_request(t.trigger_keyword, ctx.uri, ctx.host, ctx.args) then
                    r:warn("WADM ALERT: honeytoken triggered by " .. ip
                        .. " — cookie value '" .. tostring(t.trigger_keyword) .. "' replayed in request")
                    detected = true
                end
            end
        end
        return detected
    end,

    decoy_paths = function(r, ip, ctx)
        local detected = false
        for _, t in ipairs(ctx.ht.decoy_paths or {}) do
            if token_enabled(t) and t.trap_path and t.trap_path ~= "" then
                local trap = t.trap_path
                local hit
                if (t.match_type or "prefix") == "exact" then
                    hit = (ctx.uri == trap)
                else
                    hit = (ctx.uri == trap) or (ctx.uri:sub(1, #trap + 1) == trap .. "/")
                end
                if hit then
                    r:warn("WADM ALERT: honeytoken triggered by " .. ip
                        .. " — decoy path '" .. trap .. "' requested (" .. ctx.uri .. ")")
                    detected = true
                end
            end
        end
        return detected
    end,

    form_fields = function(r, ip, ctx)
        local detected = false
        for _, t in ipairs(ctx.ht.form_fields or {}) do
            if token_enabled(t) and t.field_name and t.field_name ~= "" then
                local expected = t.field_value or ""
                local qv = ctx.args[t.field_name]
                if qv ~= nil and tostring(qv) ~= expected then
                    r:warn("WADM ALERT: honeytoken triggered by " .. ip
                        .. " — form field '" .. t.field_name .. "' tampered (got '" .. tostring(qv)
                        .. "', expected '" .. expected .. "')")
                    detected = true
                end
                if keyword_in_request(t.trigger_keyword, ctx.uri, ctx.host, ctx.args) then
                    r:warn("WADM ALERT: honeytoken triggered by " .. ip
                        .. " — form field keyword '" .. tostring(t.trigger_keyword) .. "' seen in request")
                    detected = true
                end
            end
        end
        return detected
    end,
}

-- Fixed order so the per-kind timing regions run in the same sequence on every edge.
local KIND_ORDER = { "http_headers", "cookies", "decoy_paths", "form_fields" }

-- Detection for the additional honeytoken kinds, one timed region per kind. The timed region
-- covers the kind's own scan plus the in-memory IP record — the same unit the html_comments
-- detection timer measures — while ctx construction (URI, Host, query parse, Cookie) stays
-- outside as setup.
local function detect_additional(r, ip)
    local ctx = {
        ht     = _config.honeytokens or {},
        uri    = r.uri or "/",
        host   = r.headers_in and r.headers_in["Host"],
        args   = r:parseargs() or {},
        cookie = r.headers_in and r.headers_in["Cookie"],
    }

    for _, kind in ipairs(KIND_ORDER) do
        local kind_start = r:clock()
        local hit = detect_kind[kind](r, ip, ctx)
        if hit then
            detected_ips[ip] = os.time()
        end
        local kind_end = r:clock()
        -- Timed only on a hit, so every edge samples the same population.
        if hit then
            r:warn("WADM TOKEN " .. kind .. " detect (us): " .. tostring(kind_end - kind_start))
        end
    end
end

-- Inspect the query string for any trigger keyword; strip each hit from r.args.
-- r.args is a writable mod_lua field — assigning it rewrites the query string seen by mod_proxy upstream.
function handle_detect(r)
    -- Step aside for a request the SQLi trap owns. The access-checker hook structurally runs
    -- before login.lua's handler, so without this a crafted POST /api/login?password=<trigger>
    -- would emit an extra alert and an extra detection timing line on Apache alone — the other
    -- three edges short-circuit inside their single filter and never reach this code.
    if sqli.owns(r.method, r.uri) then
        return apache2.DECLINED
    end

    local ip = r.useragent_ip or "unknown"

    -- Additional honeytoken kinds run first so they are unaffected by the html_comments
    -- early-return below; same sink (WADM ALERT + in-memory IP record). Each kind is timed
    -- separately under `WADM TOKEN <kind> detect (us):`, a label sharing no substring with the
    -- html_comments scraper pattern, so the benchmarked timer below is untouched.
    detect_additional(r, ip)

    -- html_comments detection (benchmarked): guard and client-IP read stay outside the timed
    -- region (matches OpenResty), so the timer wraps only the query scan + strip + record.
    if #trigger_keywords == 0 or not r.args or r.args == "" then
        return apache2.DECLINED
    end

    local matched = false
    local start_time = r:clock()

    for _, keyword in ipairs(trigger_keywords) do
        if r.args:find(keyword, 1, true) then
            matched = true
            r:warn("WADM ALERT: honeytoken triggered by " .. ip
                   .. " — keyword '" .. keyword .. "' found in query string")

            -- Remove every key=value segment containing the keyword plus its adjacent & separator.
            r.args = r.args:gsub("[^&]*" .. keyword .. "[^&]*&?", "")
            -- Clean a stray leading & left when the matched segment was the last one.
            r.args = r.args:gsub("^&", "")
        end
    end

    -- On detection, record the attacker IP in the in-memory store (mirrors OpenResty's wadm:set).
    if matched then
        detected_ips[ip] = os.time()
    end

    -- DECLINED: not the authoritative access handler; continue to ProxyPass.
    -- Only record detection timing for trigger-bearing requests so all edges
    -- sample the same population (the keyword-matching request).
    local end_time = r:clock()
    if matched then
        r:warn("Apache Detection execution time (us): " .. tostring(end_time - start_time))
    end
    return apache2.DECLINED
end
