<!-- nginx/README.md: OpenResty-specific; stock nginx cannot run these lua_* directives. -->
# nginx (OpenResty edge)

<!-- Clarify image: why stress OpenResty — users often assume this is vanilla nginx.conf. -->
This directory is **not** stock nginx: the Compose service uses **`openresty/openresty`** with `nginx.conf` as the main config. Lua runs in several nginx phases to load config once, inspect each request, and transform HTML responses.

`nginx-baseline.conf` is the same edge with every WADM block deleted, used by the no-WADM baseline suite (see `docs/EDGE_LEVELING.md`).

The detection and injection behaviour is the cross-edge canonical mechanism shared with Envoy+Lua, Apache and Envoy+WASM; `docs/EDGE_LEVELING.md` ("Behavioural equivalence and latency pass") defines it.

## Internal data flow

### `init_by_lua_block` (once, in the master before fork)

<!-- init: why everything is precompiled here — every worker inherits it, so per-request work is lookups only. -->
1. Decodes `/etc/openresty/config.json` (mounted from the repo root) with `cjson`.
2. Precompiles the config:
   - **Per-path plans.** Paths are only `/*` or exact, so each exact path gets one plan and there is a default plan for wildcard tokens. A plan holds the joined `html_comments` string, the decoy header list, the prebuilt `Set-Cookie` strings, and the extra body payloads (`decoy_paths` link before `</body>`, `form_fields` input before `</form>`), all in config order.
   - **Detector inputs.** Enabled trigger keywords, and per-kind token lists with the `enabled` switch already applied. A kind with nothing to detect is dropped, timer included.
   - **SQLi trap sets.** Method and path lookup tables.
3. Publishes everything through a single global table, `wadm`, whose `access`, `header_filter` and `body_filter` functions the phase blocks call. Everything else stays a local of the init chunk.

The timer, `now_us`, reads `gettimeofday` into a preallocated `struct timeval` rather than allocating a new one on every call.

### `access_by_lua_block` → `wadm.access()` (per request, before upstream)

<!-- access: why before proxy_pass — must scrub secrets from the query before it reaches the backend; also records IP in shm. -->
1. **Setup (untimed).**
   - Reads `$remote_addr`, `$request_uri`, `$http_host` and, only if a cookie token exists, `$http_cookie`.
   - Splits the raw request target into `path` (up to `?`) and the query string.
   - Parses the query **once** into ordered segments `{raw, key, value}`, decoding only segments that contain `%` or `+`.
   - Stores `{plan}` in `ngx.ctx.wadm`.
2. **SQLi trap (first, and terminal).**
   - If the method and path match the `sql_injection` policy, this edge answers the request itself. It reads the body, matches the watched fields against the signatures, and then logs either `WADM ALERT` (plus an entry in `wadm_state`) or `WADM TRAP` for a request that matched nothing.
   - It marks `ngx.ctx.wadm.local_response` and replies with `ngx.print` + `ngx.exit(ngx.HTTP_OK)`. `ngx.exit(500)` would discard the body and render nginx's own error page instead.
3. **Additional kinds, one timed region each**, in the fixed order `http_headers → cookies → decoy_paths → form_fields`. Each timed region is that kind's scan plus `wadm_state:set` on a hit. The `WADM ALERT` lines and `WADM TOKEN <kind> detect (us): N` are written after the timer closes.
4. **html_comments (the benchmarked reference).** Timed region: scan the segments against the triggers → drop every segment that hit → `ngx.req.set_uri_args(kept raw segments joined by "&")` → `wadm_state:set`. `Detection execution time (us): N` is logged only on a hit.
5. **POST body**, only when `post_body_inspection` is `true` (default `false`).
   - Form-urlencoded bodies use the same segment parser and are rebuilt with `ngx.req.set_body_data`.
   - Other bodies get a raw substring scan.
   - `form_fields` also checks the body for tamper.

### `header_filter_by_lua_block` → `wadm.header_filter()`

<!-- header_filter: why clear content_length — body_filter will change byte length; nginx must not trust upstream length. -->
- Returns immediately for the SQLi trap page.
- If the plan has body work and `Content-Type` contains `text/html`, clears `Content-Length` and marks the request for injection.
- Writes the plan's decoy headers (set) and `Set-Cookie` baits (appended, so upstream cookies survive), each under its own timer logged as `WADM TOKEN <kind> inject (us): N`.

### `body_filter_by_lua_block` → `wadm.body_filter()`

<!-- body_filter: why accumulate — upstream may stream HTML; the splice needs the whole body. -->
- Does nothing unless the header filter marked the request.
- Otherwise buffers every chunk (`ngx.arg[1] = ""`) and, at end-of-stream, assembles the body untimed.
- Then:
  - each extra payload is spliced under its own timer (locate anchor → splice)
  - `html_comments` is spliced before the first `</body>` (appended if there is none) under the benchmarked timer, which also covers the `ngx.arg[1]` write-back.
- The splice is a plain, case-sensitive `string.find` plus two `sub`s. It is the same primitive on all four edges, replacing the case-insensitive PCRE `ngx.re.sub` this edge used to use.

### Upstream and logging

<!-- upstream: why standard headers — the origin's access log is the per-request record and needs the client IP. -->
- `proxy_pass http://backend` over a keep-alive pool (`keepalive 32`, HTTP/1.1, cleared `Connection`), with `Host`, `X-Real-IP` and `X-Forwarded-For` set.
- `access_log off`, because Envoy writes no access log. The per-request record is the origin's access log, which shows the client IP from `X-Forwarded-For`, together with the edge's `WADM ALERT` / `WADM TRAP` lines.
