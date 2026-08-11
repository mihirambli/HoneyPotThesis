<!-- nginx/README.md: OpenResty-specific; stock nginx cannot run these lua_* directives. -->
# nginx (OpenResty edge)

<!-- Clarify image: why stress OpenResty — users often assume this is vanilla nginx.conf. -->
This directory is **not** stock nginx: the Compose service uses **`openresty/openresty`** with `nginx.conf` as the main config. Lua runs in several nginx phases to load config once, inspect each request, and stream-transform HTML responses.

## Internal data flow

### `init_by_lua_block` (worker init)

<!-- init: why once per worker — shared global wadm_config avoids re-reading JSON on every request. -->
1. Opens `/etc/openresty/config.json` (mounted from repo root).  
2. Decodes JSON with `cjson` into global `wadm_config`.  
3. Logs success or parse errors.  
4. Defines the `wadm_handlers` registry (global) — one handler per additional honeytoken kind (`http_headers`, `cookies`, `decoy_paths`, `form_fields`), each exposing only the hooks it needs (`detect`, `header_inject`, `body_payload`) — plus the shared `wadm_path_matches` predicate and `wadm_token_enabled` switch. `html_comments` keeps its own inline, benchmarked code paths; the registry drives everything else and runs **outside** the injection/detection timers.

Every token (all kinds) carries an optional `enabled` field checked by `wadm_token_enabled` before it is injected or watched: `1`/`on`/`true` = active, anything else = dormant, absent = on. The check is setup that stays outside both timers.

### `access_by_lua_block` (per request, before upstream)

<!-- access: why before proxy_pass — must scrub secrets from args/body before they reach backend; also records IP in shm. -->
Runs only inside `location /` before `proxy_pass`:

1. Bails if config or `html_comments` missing.  
2. Builds `triggers` from non-empty `trigger_keyword` fields.  
3. **URI args:** `ngx.req.get_uri_args()` — for each key/value, substring search for triggers; on hit, logs `WADM ALERT`, sets `detected`, removes arg, may `ngx.req.set_uri_args`.  
4. **POST:** `ngx.req.read_body()` then `ngx.req.get_post_args()` for `application/x-www-form-urlencoded`-style parsing; same trigger logic; may rebuild body with `ngx.req.set_body_data`. If post args fail, falls back to scanning raw `ngx.req.get_body_data()`.  
5. If `detected`, stores `ip -> true` in `lua_shared_dict wadm_state` (24h TTL) and logs.

**SQLi trap (first, and terminal).** Ahead of everything else, `wadm_sqli_owns(method, uri)` checks the top-level `sql_injection` policy. On a match this edge stops being a proxy: it reads the body, matches the watched fields against the signature list, logs `WADM ALERT` + `wadm_state` on a hit, sets `ngx.ctx.wadm_local_response`, and answers with `ngx.print` + `ngx.exit(ngx.HTTP_OK)`. `ngx.exit(ngx.HTTP_OK)` rather than `ngx.exit(500)` — the latter discards the body and renders nginx's own error page. The suppression flag is required because `ngx.print` still traverses the output-filter chain; both filters below check it on their first line.

**Additional-kind detection (timed per kind).** Before the `html_comments` scan, a registry pass walks `wadm_kind_order` and runs each handler's `detect`: `cookies`/`form_fields` flag **tampering** (returned value ≠ planted value), `decoy_paths` flags a **request whose URI matches `trap_path`**, and `http_headers`/`cookies` flag **replay** of a planted value (path/Host/query). Hits reuse the same sink (`WADM ALERT` + `wadm_state`). It runs first so the `html_comments` early-returns can't skip it, and outside the `html_comments` detection timer so that measurement is unchanged. Each kind gets **its own** timer — the shared `dctx` (IP, URI, parsed args) is built once outside; the timed region is that kind's scan + alert + `wadm:set`, logged as `WADM TOKEN <kind> detect (us): N` only on a hit. The order is a fixed list rather than `pairs()`, whose unspecified order would let the timed regions run in a different sequence on every request.

### `header_filter_by_lua_block` (response headers from upstream)

<!-- header_filter: why clear content_length — body_filter will change byte length; nginx must not trust upstream length. -->
Returns immediately when `ngx.ctx.wadm_local_response` is set, so the SQLi trap page keeps the explicit `Content-Length` the access phase gave it. Otherwise: if `Content-Type` looks like HTML, clears `content_length` so nginx can change the body length during filtering. Then a registry pass runs each handler's `header_inject` on matching `paths`: `http_headers` sets the decoy response header, `cookies` **appends** a `Set-Cookie` bait (without clobbering upstream cookies). Header-only, so it is content-type agnostic and needs no length handling. Token selection (`enabled` + path match) is setup outside the timer; the timed region is the header write itself, logged as `WADM TOKEN <kind> inject (us): N`.

### `body_filter_by_lua_block` (streaming response body)

<!-- body_filter: why chunk table — upstream may stream HTML; accumulate until eof flag then inject once. -->
0. Returns immediately when `ngx.ctx.wadm_local_response` is set — the SQLi trap page must ship exactly as built, with no honeytokens spliced into it.  
1. Ignores non-HTML responses.  
2. Computes `to_inject` from `ngx.var.uri` and path patterns (same rules as other stacks).  
3. **Chunk accumulation:** pushes each upstream chunk into `ngx.ctx.body_chunks`, zeroes the current chunk (`ngx.arg[1] = ""`) until `ngx.arg[2]` signals EOF.  
4. On last chunk, concatenates all pieces, runs regex replace to insert honeytokens before `</body>` (or appends), outputs final `ngx.arg[1]`.

**Additional-kind body payloads (timed per kind).** The registry also collects `body_payload`s — `form_fields` hidden `<input>`s (spliced before the first `</form>`) and `decoy_paths` hidden links (before `</body>`). When any exist, each is spliced under its own timer (`WADM TOKEN <kind> inject (us): N`, region = locate anchor → splice; markup construction and the single `ngx.arg[1]` write-back sit outside), and the `html_comments` splice still runs under its own unchanged timer afterwards. When none exist, the last-chunk path is byte-for-byte the original benchmarked code, so the `Injection execution time` measurement is unchanged either way.

### Upstream

<!-- upstream: why standard headers — backend logs and apps may rely on X-Forwarded-For for client IP. -->
`proxy_pass http://backend` with standard `Host`, `X-Real-IP`, and `X-Forwarded-For` headers.

<!-- Summary: why — ties phases together for operators comparing to Envoy. -->
**Summary:** configuration loads once; each request is scrubbed in `access_by_lua`; HTML responses are buffered in Lua across body chunks then rewritten with injected comments—OpenResty’s answer to the same WADM pipeline implemented in Envoy Lua and Rust WASM.
