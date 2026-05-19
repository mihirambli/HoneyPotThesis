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

### `access_by_lua_block` (per request, before upstream)

<!-- access: why before proxy_pass — must scrub secrets from args/body before they reach backend; also records IP in shm. -->
Runs only inside `location /` before `proxy_pass`:

1. Bails if config or `html_comments` missing.  
2. Builds `triggers` from non-empty `trigger_keyword` fields.  
3. **URI args:** `ngx.req.get_uri_args()` — for each key/value, substring search for triggers; on hit, logs `WADM ALERT`, sets `detected`, removes arg, may `ngx.req.set_uri_args`.  
4. **POST:** `ngx.req.read_body()` then `ngx.req.get_post_args()` for `application/x-www-form-urlencoded`-style parsing; same trigger logic; may rebuild body with `ngx.req.set_body_data`. If post args fail, falls back to scanning raw `ngx.req.get_body_data()`.  
5. If `detected`, stores `ip -> true` in `lua_shared_dict wadm_state` (24h TTL) and logs.

### `header_filter_by_lua_block` (response headers from upstream)

<!-- header_filter: why clear content_length — body_filter will change byte length; nginx must not trust upstream length. -->
If `Content-Type` looks like HTML, clears `content_length` so nginx can change the body length during filtering.

### `body_filter_by_lua_block` (streaming response body)

<!-- body_filter: why chunk table — upstream may stream HTML; accumulate until eof flag then inject once. -->
1. Ignores non-HTML responses.  
2. Computes `to_inject` from `ngx.var.uri` and path patterns (same rules as other stacks).  
3. **Chunk accumulation:** pushes each upstream chunk into `ngx.ctx.body_chunks`, zeroes the current chunk (`ngx.arg[1] = ""`) until `ngx.arg[2]` signals EOF.  
4. On last chunk, concatenates all pieces, runs regex replace to insert honeytokens before `</body>` (or appends), outputs final `ngx.arg[1]`.

### Upstream

<!-- upstream: why standard headers — backend logs and apps may rely on X-Forwarded-For for client IP. -->
`proxy_pass http://backend` with standard `Host`, `X-Real-IP`, and `X-Forwarded-For` headers.

<!-- Summary: why — ties phases together for operators comparing to Envoy. -->
**Summary:** configuration loads once; each request is scrubbed in `access_by_lua`; HTML responses are buffered in Lua across body chunks then rewritten with injected comments—OpenResty’s answer to the same WADM pipeline implemented in Envoy Lua and Rust WASM.
