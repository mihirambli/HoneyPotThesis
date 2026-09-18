-- wadm.lua — shared WADM core for the Apache edge (profile: apache), required by detect.lua,
-- inject.lua and login.lua.
--
-- mod_lua gives every script file its own Lua state per thread (LuaScope thread), and each of
-- those used to parse config.json and redefine the same helpers for itself. Keeping the config,
-- the precompiled plans and every helper here means one parse per state and one definition of
-- the behaviour all three hooks must agree on. Timing and logging stay in the hook files because
-- they need the request object (r:clock, r:warn).
--
-- Runs on mod_lua's PUC Lua 5.1, so it avoids anything LuaJIT-only.

package.path = "/usr/local/apache2/scripts/?.lua;" .. package.path
local json = require("json")

local find, sub, gsub, gmatch, match = string.find, string.sub, string.gsub, string.gmatch, string.match
local lower, char, tonumber = string.lower, string.char, tonumber
local concat = table.concat

local _cfg_file = io.open("/usr/local/apache2/conf/config.json", "r")
local config = json.decode(_cfg_file:read("*a"))
_cfg_file:close()

local ht = config.honeytokens or {}

local M = {}

-- Per-token on/off switch (all kinds): active unless explicitly disabled. 1/on/true/yes = on;
-- anything else = off; absent = on (backward compatible).
local function token_enabled(token)
    local v = token.enabled
    if v == nil then return true end
    if v == true or v == 1 then return true end
    if type(v) == "string" then
        local s = lower(v)
        return s == "1" or s == "on" or s == "true" or s == "yes"
    end
    return false
end

local function enabled_tokens(list)
    local out = {}
    for _, t in ipairs(list or {}) do
        if token_enabled(t) then out[#out + 1] = t end
    end
    return out
end

local function nonempty(s)
    if s == nil or s == "" then return nil end
    return s
end

-- ── Precompiled per-path plans (parity with OpenResty nginx/nginx.conf) ─────────────
-- Paths are only ever "/*" or exact, so every request resolves to one of a handful of plans.
-- Building them once replaces the per-request token loops, enabled checks and markup
-- concatenation. Config order is kept within each list because it decides the joined comment
-- order and the splice order.

local comment_tokens = enabled_tokens(ht.html_comments)
local header_tokens = enabled_tokens(ht.http_headers)
local cookie_tokens = enabled_tokens(ht.cookies)
local decoy_tokens = enabled_tokens(ht.decoy_paths)
local form_tokens = enabled_tokens(ht.form_fields)

local function covers(paths, path)
    for _, p in ipairs(paths or {}) do
        if p == "/*" or p == path then return true end
    end
    return false
end

local function build_plan(path)
    local comments = {}
    for _, t in ipairs(comment_tokens) do
        if t.comment_value and covers(t.paths, path) then
            comments[#comments + 1] = t.comment_value
        end
    end
    local headers = {}
    for _, t in ipairs(header_tokens) do
        if nonempty(t.header_name) and covers(t.paths, path) then
            headers[#headers + 1] = { name = t.header_name, value = t.header_value or "" }
        end
    end
    local cookies = {}
    for _, t in ipairs(cookie_tokens) do
        if nonempty(t.cookie_name) and covers(t.paths, path) then
            local c = t.cookie_name .. "=" .. (t.cookie_value or "")
            if nonempty(t.attributes) then c = c .. "; " .. t.attributes end
            cookies[#cookies + 1] = c
        end
    end
    local extras = {}
    for _, t in ipairs(decoy_tokens) do
        if (t.advertise_via or "link") == "link" and covers(t.advertise_on_paths, path) then
            extras[#extras + 1] = {
                kind = "decoy_paths", anchor = "</body>",
                markup = '<a href="' .. (t.trap_path or "") .. '" style="display:none">'
                    .. (t.link_text or "") .. '</a>',
            }
        end
    end
    for _, t in ipairs(form_tokens) do
        if nonempty(t.field_name) and covers(t.paths, path) then
            extras[#extras + 1] = {
                kind = "form_fields", anchor = "</form>",
                markup = '<input type="hidden" name="' .. t.field_name .. '" value="'
                    .. (t.field_value or "") .. '">',
            }
        end
    end
    local joined = #comments > 0 and concat(comments, "\n") or nil
    return {
        comments = joined, headers = headers, cookies = cookies, extras = extras,
        has_body = joined ~= nil or #extras > 0,
    }
end

local plans = {}
local function note_paths(paths)
    for _, p in ipairs(paths or {}) do
        if p ~= "/*" then plans[p] = true end
    end
end
for _, list in ipairs({ comment_tokens, header_tokens, cookie_tokens, form_tokens }) do
    for _, t in ipairs(list) do note_paths(t.paths) end
end
for _, t in ipairs(decoy_tokens) do note_paths(t.advertise_on_paths) end
for p in pairs(plans) do plans[p] = build_plan(p) end
local plan_default = build_plan(nil)

function M.plan_for(path)
    return plans[path] or plan_default
end

local triggers = {}
for _, t in ipairs(comment_tokens) do
    if nonempty(t.trigger_keyword) then triggers[#triggers + 1] = t.trigger_keyword end
end
M.triggers = triggers

-- ── Shared request parsing ───────────────────────────────────────────────────────────

-- The raw request target up to "?", as every edge uses it: r.uri is decoded and normalised by
-- Apache, which the Envoy edges cannot reproduce, so it would make path matching edge-specific.
function M.request_path(unparsed_uri)
    local target = unparsed_uri or "/"
    local qpos = find(target, "?", 1, true)
    return qpos and sub(target, 1, qpos - 1) or target
end

local function url_decode(s)
    s = gsub(s, "%+", " ")
    return (gsub(s, "%%(%x%x)", function(h) return char(tonumber(h, 16)) end))
end

-- Ordered segments that keep their raw text: the strip rebuilds the query from the raw text of
-- the segments it keeps, so order and original encoding survive, identically on all four edges.
-- Decoding is skipped for the common segment that has nothing to decode.
function M.parse_query(q)
    local segs = {}
    if not q or q == "" then return segs end
    local pos, n = 1, #q
    while pos <= n do
        local amp = find(q, "&", pos, true)
        local last = amp and amp - 1 or n
        if last >= pos then
            local raw = sub(q, pos, last)
            local eq = find(raw, "=", 1, true)
            local k = eq and sub(raw, 1, eq - 1) or raw
            local v = eq and sub(raw, eq + 1) or ""
            if find(raw, "%", 1, true) or find(raw, "+", 1, true) then
                k, v = url_decode(k), url_decode(v)
            end
            segs[#segs + 1] = { raw = raw, key = k, value = v }
        end
        if not amp then break end
        pos = amp + 1
    end
    return segs
end

function M.join_kept(segs, dropped)
    local kept = {}
    for i = 1, #segs do
        if not dropped[i] then kept[#kept + 1] = segs[i].raw end
    end
    return concat(kept, "&")
end

-- html_comments scan over parsed segments: returns the set of segment indexes to drop and
-- appends one hit per (segment, keyword) match, in segment order then config order.
function M.scan_segments(segs, where, hits)
    local dropped
    for i = 1, #segs do
        local s = segs[i]
        for j = 1, #triggers do
            local kw = triggers[j]
            if find(s.key, kw, 1, true) or find(s.value, kw, 1, true) then
                hits[#hits + 1] = { tpl = "query_keyword", a = kw, b = s.key, c = s.value, where = where }
                dropped = dropped or {}
                dropped[i] = true
            end
        end
    end
    return dropped
end

local function cookie_value(header, name)
    if not header then return nil end
    for pair in gmatch(header, "[^;]+") do
        local k, v = match(pair, "^%s*(.-)%s*=%s*(.-)%s*$")
        if k == name then return v end
    end
    return nil
end

-- Substring scan of a request's target surface (path, Host, query) for a replayed value.
local function keyword_in_request(kw, ctx)
    if find(ctx.path, kw, 1, true) or (ctx.host and find(ctx.host, kw, 1, true)) then
        return true
    end
    local segs = ctx.segs
    for i = 1, #segs do
        local s = segs[i]
        if find(s.key, kw, 1, true) or find(s.value, kw, 1, true) then return true end
    end
    return false
end

-- Fixed-anchor first-match splice, the same primitive on every edge: a *plain* find (patterns
-- disabled) then two subs. string.gsub was used here before and cost ~2x more for the same
-- result, making the injection figure rank an implementation choice (docs/EDGE_LEVELING.md).
function M.splice_before(body, anchor, insert)
    local pos = find(body, anchor, 1, true)
    if not pos then return nil end
    return sub(body, 1, pos - 1) .. insert .. "\n" .. sub(body, pos)
end

-- CR/LF would let an attacker forge whole log lines that the benchmark scrapers read.
local function log_safe(s)
    return (gsub(s, "[\r\n]", " "))
end
M.log_safe = log_safe

-- ── Detectors (parity with OpenResty nginx/nginx.conf) ───────────────────────────────
-- Detectors record hits as small descriptors and format_alert renders them afterwards, so that
-- alert *formatting and writing* both sit outside the detection timer. Log I/O otherwise
-- dominated the measurement (see docs/EDGE_LEVELING.md). The wire format is identical on all edges.
function M.format_alert(ip, hit)
    local prefix = "WADM ALERT: honeytoken triggered by " .. ip .. " — "
    local t = hit.tpl
    if t == "header_replay" then
        return prefix .. "http_header value '" .. hit.a .. "' replayed in request"
    elseif t == "cookie_tamper" then
        return prefix .. "cookie '" .. hit.a .. "' tampered (got '" .. log_safe(hit.b)
            .. "', expected '" .. hit.c .. "')"
    elseif t == "cookie_replay" then
        return prefix .. "cookie value '" .. hit.a .. "' replayed in request"
    elseif t == "decoy_hit" then
        return prefix .. "decoy path '" .. hit.a .. "' requested (" .. log_safe(hit.b) .. ")"
    elseif t == "form_tamper" then
        return prefix .. "form field '" .. hit.a .. "' tampered (got '" .. log_safe(hit.b)
            .. "', expected '" .. hit.c .. "')"
    elseif t == "query_keyword" then
        return prefix .. "keyword '" .. hit.a .. "' found in " .. hit.where .. " '"
            .. log_safe(hit.b) .. "=" .. log_safe(hit.c) .. "'"
    else
        return prefix .. "form field keyword '" .. hit.a .. "' seen in request"
    end
end

local header_detect = {}
for _, t in ipairs(header_tokens) do
    if nonempty(t.trigger_keyword) then header_detect[#header_detect + 1] = t.trigger_keyword end
end
local cookie_detect = {}
for _, t in ipairs(cookie_tokens) do
    local name, kw = nonempty(t.cookie_name), nonempty(t.trigger_keyword)
    if name or kw then
        cookie_detect[#cookie_detect + 1] = { name = name, value = t.cookie_value or "", kw = kw }
    end
end
local decoy_detect = {}
for _, t in ipairs(decoy_tokens) do
    if nonempty(t.trap_path) then
        decoy_detect[#decoy_detect + 1] = {
            trap = t.trap_path, prefix = t.trap_path .. "/",
            exact = (t.match_type or "prefix") == "exact",
        }
    end
end
local form_detect = {}
for _, t in ipairs(form_tokens) do
    local name, kw = nonempty(t.field_name), nonempty(t.trigger_keyword)
    if name or kw then
        form_detect[#form_detect + 1] = { name = name, value = t.field_value or "", kw = kw }
    end
end

-- The first mismatching submission is the tamper evidence; later duplicates add nothing.
-- Query string only: Apache has no POST-body inspection, which matches every edge while
-- post_body_inspection is off (the default).
local function first_tamper(segs, name, expected)
    for i = 1, #segs do
        local s = segs[i]
        if s.key == name and s.value ~= expected then return s.value end
    end
    return nil
end

local detectors = {
    http_headers = function(tokens, ctx, hits)
        for i = 1, #tokens do
            if keyword_in_request(tokens[i], ctx) then
                hits[#hits + 1] = { tpl = "header_replay", a = tokens[i] }
            end
        end
    end,

    cookies = function(tokens, ctx, hits)
        for i = 1, #tokens do
            local t = tokens[i]
            if t.name then
                local v = cookie_value(ctx.cookie, t.name)
                if v ~= nil and v ~= t.value then
                    hits[#hits + 1] = { tpl = "cookie_tamper", a = t.name, b = v, c = t.value }
                end
            end
            if t.kw and keyword_in_request(t.kw, ctx) then
                hits[#hits + 1] = { tpl = "cookie_replay", a = t.kw }
            end
        end
    end,

    decoy_paths = function(tokens, ctx, hits)
        local path = ctx.path
        for i = 1, #tokens do
            local t = tokens[i]
            local hit = path == t.trap or (not t.exact and sub(path, 1, #t.prefix) == t.prefix)
            if hit then
                hits[#hits + 1] = { tpl = "decoy_hit", a = t.trap, b = path }
            end
        end
    end,

    form_fields = function(tokens, ctx, hits)
        for i = 1, #tokens do
            local t = tokens[i]
            if t.name then
                local got = first_tamper(ctx.segs, t.name, t.value)
                if got then
                    hits[#hits + 1] = { tpl = "form_tamper", a = t.name, b = got, c = t.value }
                end
            end
            if t.kw and keyword_in_request(t.kw, ctx) then
                hits[#hits + 1] = { tpl = "form_keyword", a = t.kw }
            end
        end
    end,
}

-- Fixed order so the per-kind timing regions run in the same sequence on every edge. Kinds with
-- nothing to detect are dropped here rather than timed as empty loops.
local kinds = {}
for _, k in ipairs({
    { "http_headers", header_detect }, { "cookies", cookie_detect },
    { "decoy_paths", decoy_detect }, { "form_fields", form_detect },
}) do
    if #k[2] > 0 then
        kinds[#kinds + 1] = { name = k[1], tokens = k[2], detect = detectors[k[1]] }
    end
end
M.kinds = kinds
M.need_cookie = #cookie_detect > 0

-- ── Fake SQL-injection trap ──────────────────────────────────────────────────────────
-- Apache is the only edge whose request-phase hooks (LuaHookAccessChecker) and output filter
-- run separately from the content handler that answers the trap, so they cannot be
-- short-circuited the way the other three edges' single filter can. Sharing one ownership
-- predicate is what keeps the observable behaviour identical across all four edges.

local sqli = config.sql_injection
if sqli and not token_enabled(sqli) then sqli = nil end
M.sqli = sqli
local sqli_methods, sqli_paths, sqli_any_path = {}, {}, false
if sqli then
    for _, m in ipairs(sqli.methods or {}) do sqli_methods[m] = true end
    for _, p in ipairs(sqli.paths or {}) do
        if p == "/*" then sqli_any_path = true else sqli_paths[p] = true end
    end
end

-- Owned = the request the trap answers itself. Scoped by method AND path so the benchmarked
-- GET population (including GET /api/login?password=…) is untouched. The path is checked first
-- so a non-trap request never reads r.method across the C boundary.
function M.sqli_owns(r, path)
    if not sqli or not (sqli_any_path or sqli_paths[path]) then return false end
    return sqli_methods[r.method] == true
end

-- Signatures are stored pre-lowercased and space-normalised, so the same folding must be
-- applied to the input. ASCII-only lowercasing keeps this byte-identical to the WASM edge.
local function sqli_normalize(v)
    return (gsub(lower(url_decode(v)), "%s+", " "))
end

-- Loop order (watch_fields → body pairs → signatures) is part of the cross-edge contract: it
-- makes "first match wins" resolve identically on all four edges. Every body segment is decoded
-- here (not only those containing % or +) because the signatures must see the normalised text.
function M.sqli_match(raw)
    local pairs_out = {}
    for chunk in gmatch(raw or "", "[^&]+") do
        local k, v = match(chunk, "^(.-)=(.*)$")
        if not k then k, v = chunk, "" end
        pairs_out[#pairs_out + 1] = { key = url_decode(k), value = url_decode(v) }
    end
    for _, field in ipairs(sqli.watch_fields or {}) do
        for _, pair in ipairs(pairs_out) do
            if pair.key == field then
                local norm = sqli_normalize(pair.value)
                for _, sig in ipairs(sqli.signatures or {}) do
                    if find(norm, sig, 1, true) then
                        return { field = field, value = pair.value, signature = sig }
                    end
                end
            end
        end
    end
    return nil
end

-- The trap reflects attacker-controlled input; without escaping the honeypot would itself be a
-- live reflected-XSS vector against anyone who views the page.
local function html_escape(s)
    s = gsub(s, "&", "&amp;")
    s = gsub(s, "<", "&lt;")
    s = gsub(s, ">", "&gt;")
    s = gsub(s, '"', "&quot;")
    s = gsub(s, "'", "&#39;")
    return s
end

function M.sqli_render(template, payload)
    local escaped = html_escape(sub(payload, 1, sqli.reflect_max_len or 200))
    -- Replacement FUNCTION, not string: a payload such as `100%' OR 1=1--` keeps a bare `%`,
    -- which gsub would reject as an invalid replacement escape and turn into a 500.
    return (gsub(template, "{PAYLOAD}", function() return escaped end))
end

return M
