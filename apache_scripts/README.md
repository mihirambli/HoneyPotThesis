<!-- apache_scripts/README.md: documents mod_lua WADM scripts for the Apache edge (Compose profile: apache). -->
# apache_scripts (Apache mod_lua WADM filter)

Scripts mounted read-only at `/usr/local/apache2/scripts/` for **both** Apache services: the proxy edge `apache` (`httpd.conf`) and the in-server variant `apache-inserver` (`httpd-inserver.conf`). They are shared files, not copies, so both deployments run the same module code. They implement the same canonical detection and injection mechanism as the other three edges; `docs/EDGE_LEVELING.md` ("Behavioural equivalence and latency pass") defines it. The scripts are split across Apache's processing phases.

## Files and mounts (bind-mounted by docker-compose.yml)

| Host path | Container path | Purpose |
|-----------|---------------|---------|
| `./config.json` | `/usr/local/apache2/conf/config.json` | Honeytoken definitions and the `sql_injection` policy |
| `./apache_scripts/` | `/usr/local/apache2/scripts/` | This directory |

| File | Role |
|------|------|
| `wadm.lua` | Shared core, `require`d by the three hook files. It parses the config once per Lua state and precompiles the per-path plans, the trigger list, the per-kind detectors and the SQLi sets. It holds every helper the hooks must agree on: `parse_query`, `scan_segments`, `splice_before`, `format_alert`, `sqli_*`. |
| `detect.lua` | `LuaHookAccessChecker … early`: detection, the query strip, **and** staging the response-header baits. |
| `inject.lua` | `LuaOutputFilter WADM_INJECT`: HTML body injection. |
| `login.lua` | `LuaMapHandler "^/api/login$"`: the fake SQL-injection trap. |
| `json.lua` | Vendored rxi JSON library, identical to `envoy_scripts/json.lua`. A separate file bind-mount cannot overlay a read-only directory mount in Docker, so it lives here. |

mod_lua gives each script file its **own** Lua state per worker thread (`LuaScope thread`). Before `wadm.lua` existed, every file (and `sqli.lua` inside each of them) parsed `config.json` separately and redefined the same helpers.

`httpd.conf` sets `LuaCodeCache forever`. The default, `stat`, stats every script on every hook call to check for edits, which the other three edges never do because they load their code once. The cost of this setting is that **after editing a script you must run `docker compose restart apache`**. The benchmark runners recreate the container on every run, so they always pick up edits.

## Data flow

```
Client request
      │
      ▼
┌──────────────────────────────────────────────────────────────┐
│ access_checker (early) — detect.lua → handle_detect(r)        │
│  • raw path from r.unparsed_uri, query segments from r.args   │
│  • additional kinds (one timer each), then html_comments:     │
│    scan → r.args = kept raw segments → record IP              │
│  • stages decoy header + Set-Cookie in r.err_headers_out      │
│  • returns apache2.DECLINED → processing continues            │
└──────────────────────────────┬───────────────────────────────┘
                               │ cleaned query string
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ handler — login.lua (POST /api/login only) or mod_proxy       │
│  ProxyPass / http://backend:80/                               │
└──────────────────────────────┬───────────────────────────────┘
                               │ upstream response
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ output filter — inject.lua → handle_inject(r)                 │
│  • pass-through unless text/html AND the plan has body work   │
│  • buffers all chunks, splices extras + comments at EOS       │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
                             Client
```

## In-server behaviour (`apache-inserver`, `httpd-inserver.conf`)

In the in-server variant Apache serves `backend/www` (mounted at `/srv/www`) itself; there is no `ProxyPass`. The hooks are registered exactly as in `httpd.conf`, and behave as follows:

- **`detect.lua`** runs unchanged. It still assigns `r.args` after a strip; with no proxy, nothing reads it, so the strip is verified only through the alert lines. `err_headers_out` baits are sent on served files and on Apache's own error pages alike.
- **`login.lua`** answers the trap as before. For anything it does not own it returns `DECLINED`, and the core serves the file or answers 404, as it would without WADM.
- **`inject.lua`** runs on every response that passes through the normal output-filter chain: served HTML files and the gzip test page (it is a resource filter, so it runs before `DEFLATE`). It does **not** run on Apache's own error pages. `ap_send_error_response` (modules/http/http_protocol.c) resets `r->output_filters` to the protocol filters, "to get rid of any RESOURCE filters", and mod_lua registers `LuaOutputFilter` as `AP_FTYPE_RESOURCE`. A 404 therefore carries the bait header and cookie but no body tokens. In the proxy deployment the 404 came from the origin as an ordinary proxied response and was injected. Two of the benchmark's four GETs (`detect_query` and `token_decoy`) end in 404, so in this deployment the module injects into three of the five HTML responses per iteration rather than five.
- **`/`** is mapped to `index.html` with `AliasMatch`, as the OpenResty in-server variant must map it with `try_files` (nginx's `index` makes an internal redirect that re-runs detection: measured, two alerts for one trigger). In Apache the mapping is a matter of symmetry, not of correctness: with mod_dir resolving `/` instead, one trigger still raised one alert and `detect.lua` ran once (one header-staging timing line), measured on 2026-10-18.
- **`/test/empty`** is answered by `RedirectMatch 204`, at translate-name time, before the access hook. That 204 therefore carries no bait header and raises no alert. Both deployments still serve 204.

## Hook details

### `detect.lua` — `handle_detect(r)`

- **Return value:** always `apache2.DECLINED`. In the `access_checker` phase that means "not the authoritative handler; continue", which `ProxyPass` needs.
- **Strip mechanism:** `r.args` is a writable mod_lua field, and assigning it rewrites the query that mod_proxy sends upstream. The new value is the raw text of the segments that did not match, joined with `&`. The previous `gsub("[^&]*"..keyword.."[^&]*&?")` treated the keyword as a Lua pattern, so `-` and `.` in `internal-admin.example.com` were pattern characters and the benchmark keyword was **never** stripped.
  - Residual: when every parameter is stripped the origin receives `path?`, not `path`. mod_lua can only assign `r.args` a string, and mod_proxy appends `?` whenever args is non-NULL.
- **Header staging:** the decoy header and `Set-Cookie` are written to `r.err_headers_out`, which survives `ProxyPass`. They used to be written in a separate `LuaHookFixups` hook. Merging them here saves one Lua hook entry per request: a VM lookup plus request-object setup.
  - Limitation: mod_lua exposes only `apr_table_set` here, so several cookie tokens on the same path would overwrite each other. The shipped config plants one.
- **Scope:** query string only. Apache has no POST-body inspection, which matches every edge while `post_body_inspection` is off (the default).

### `login.lua` — `handle_login(r)`

The fake SQL-injection trap. The shared logic lives in `wadm.lua`; `login.lua` is the content handler that answers the request.

- **Why a handler and not another hook:** mod_lua registers `lua_map_handler` at `AP_LUA_HOOK_FIRST`, ahead of mod_proxy's handler, so returning anything other than `DECLINED` terminates the request at the edge and the origin is never contacted.
  - Returning `DECLINED` for a request the trap doesn't own lets `ProxyPass` run exactly as before. That is why `httpd.conf` needs no `ProxyPass /api/login !` exclusion.
  - Adding one would make Apache answer the benchmarked `GET /api/login?password=<keyword>` from its own 404 page instead of nginx's. That is a `text/html` body of a different size, which would shift the injection distribution.
- **Anchored pattern:** `LuaMapHandler` matches with `ap_regexec`, which is an unanchored *search*. A bare `/api/login` would also match `/x/api/loginfoo`.
- **Response shape:** `r.status` is assigned **before** the first `r:puts`, and the handler returns `apache2.OK`, never a numeric status. Returning a status code makes Apache discard the body and render its own `ErrorDocument`.
- **Logging:** a signature hit logs `WADM ALERT`. Any other trap request logs `WADM TRAP`. This is the only record of a clean login attempt, because the edge access log was removed and the origin never sees these requests.
- **Why ownership is checked in three places:** Apache's access-checker hook and output filter run separately from the handler and cannot read a note it leaves. `detect.lua` and `inject.lua` therefore call `wadm.sqli_owns(r, path)` themselves and step aside. Without those guards, a crafted `POST /api/login?password=<trigger>` would produce an extra alert and timing line on Apache alone, and the trap page would carry the bait header and cookie.

### `inject.lua` — `handle_inject(r)`

- **Coroutine stages:** the first `coroutine.yield()` signals readiness. The `while bucket ~= nil` loop accumulates every brigade chunk, yielding `""` so nothing is emitted yet. After end-of-stream the finished body is emitted at the final `coroutine.yield(body)`.
- **Pass-through:** responses that are not `text/html`, and pages whose plan has no body work, stream through unbuffered.
- **Timed regions:** each extra payload's locate-anchor → splice, then the html_comments splice. The final `yield` cannot be timed, because it hands control back to Apache. Content-Length is removed by `Header always unset Content-Length` in `httpd.conf`, outside the timers.

## Parity reference

| Behaviour | OpenResty (`nginx/nginx.conf`) | Envoy Lua (`envoy_scripts/injection.lua`) | Apache mod_lua (this dir) |
|-----------|-------------------------------|------------------------------------------|--------------------------|
| Config load + precompile | `init_by_lua_block` | Module scope | `wadm.lua`, once per Lua state |
| Client IP | `$remote_addr` | `downstreamDirectRemoteAddress()` | `r.useragent_ip` |
| Request path | raw, from `$request_uri` | raw, from `:path` | raw, from `r.unparsed_uri` |
| Query strip | `ngx.req.set_uri_args(kept)` | `headers:replace(":path", …)` | `r.args = kept` |
| POST body clean | `ngx.req.set_body_data` (flag-gated) | `body_handle:setBytes` (flag-gated) | Not implemented |
| IP tracking | `lua_shared_dict` | module-scope Lua table | module-scope Lua table |
| HTML injection | `body_filter_by_lua_block` | `response_handle:body():setBytes(…)` | `LuaOutputFilter` coroutine |
| Splice primitive | plain `string.find` + `sub` | plain `string.find` + `sub` | plain `string.find` + `sub` |
| SQLi trap response | `ngx.print` + `ngx.exit(ngx.HTTP_OK)` | `request_handle:respond(…)` | `r:puts` + `apache2.OK` from `LuaMapHandler` |
| SQLi trap framing | explicit `Content-Length` | explicit `Content-Length` | chunked/close-delimited ‡ |

‡ `httpd.conf`'s `Header always unset Content-Length` is unconditional, so it strips the length from the trap page too. The **body bytes are identical** to the other three edges; only the framing differs. Scoping the `unset` with an `expr` would fix it but risks the injection path, so it is accepted and documented.
