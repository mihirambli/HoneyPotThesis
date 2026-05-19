<!-- apache_scripts/README.md: documents mod_lua WADM scripts for the Apache edge (Compose profile: apache). -->
# apache_scripts (Apache mod_lua WADM filter)

Scripts mounted read-only at `/usr/local/apache2/scripts/` for the **Apache + mod_lua** edge.
Two Lua hooks implement the same detect-then-inject pipeline as `envoy_scripts/injection.lua`, split into separate files that map to distinct Apache processing phases.

## Shared assets (bind-mounted by docker-compose.yml)

| Host path | Container path | Purpose |
|-----------|---------------|---------|
| `./config.json` | `/usr/local/apache2/conf/config.json` | Honeytoken definitions (`trigger_keyword`, `comment_value`, `paths`) |
| `./apache_scripts/` | `/usr/local/apache2/scripts/` | This directory (detect.lua, inject.lua, json.lua) |

`json.lua` lives directly in this directory (copied from `envoy_scripts/json.lua`). A separate file bind-mount cannot overlay a read-only directory mount in Docker, so the library is kept here rather than mounted independently.

Both scripts load `config.json` at **module scope** (`LuaScope thread` in `httpd.conf`), so the file-open runs once per worker thread rather than once per request.

## Two-phase data flow

```
Client request
      │
      ▼
┌─────────────────────────────────────────────────────────┐
│ Apache access_checker phase (early)                      │
│  detect.lua → handle_detect(r)                           │
│  • reads r.args for trigger_keyword                      │
│  • logs WADM ALERT + strips param via r.args = gsub(…)  │
│  • returns apache2.DECLINED → processing continues       │
└──────────────────────────┬──────────────────────────────┘
                           │ cleaned query string
                           ▼
┌─────────────────────────────────────────────────────────┐
│ mod_proxy                                                │
│  ProxyPass / http://backend:80/                          │
│  backend never sees the trigger keyword                  │
└──────────────────────────┬──────────────────────────────┘
                           │ upstream response
                           ▼
┌─────────────────────────────────────────────────────────┐
│ Output filter chain (text/html only)                     │
│  inject.lua → handle_inject(r)  [LuaOutputFilter]       │
│  • coroutine: yield → while bucket loop → final yield   │
│  • gsub replaces </body> with comment_value + </body>    │
│  • modified bucket yielded downstream per chunk          │
└──────────────────────────┬──────────────────────────────┘
                           │ HTML with injected honeytoken
                           ▼
                        Client
```

## Hook details

### `detect.lua` — `handle_detect(r)` (access_checker early)

- **Return value:** always `apache2.DECLINED`. In the `access_checker` phase, `DECLINED` means "not the authoritative handler; continue." This is required so `ProxyPass` fires on every request. Returning `OK` would satisfy the phase and could prevent later access checkers from running.
- **Cleaning mechanism:** `r.args` is a writable mod_lua request field. Assigning it rewrites the query string that all downstream phases — including the proxy upstream URL — see. The backend is never reached with the trigger keyword present.
- **Scope:** query string only. POST body inspection is out of scope for this implementation; see `envoy_scripts/injection.lua` for full body-parsing parity.

### `inject.lua` — `handle_inject(r)` (LuaOutputFilter WADM_INJECT)

- **Coroutine stages:** first `coroutine.yield()` signals readiness; the `while bucket ~= nil` loop processes each brigade chunk; the final `coroutine.yield()` is the required clean-close.
- **Per-chunk limitation:** `string.gsub` runs on each bucket individually. If `</body>` is split across two consecutive chunks the comment is not injected. This is acceptable for a demo backend serving small, complete pages. To handle it robustly, accumulate all chunks into a buffer and inject once after the loop exits (full-buffer approach used by `envoy_scripts/injection.lua`).
- **Content-Type guard:** `AddOutputFilterByType WADM_INJECT text/html` in `httpd.conf` restricts the filter to HTML responses; no check is needed inside the script.

## Parity reference

| Behaviour | OpenResty (`nginx/nginx.conf`) | Envoy Lua (`envoy_scripts/injection.lua`) | Apache mod_lua (this dir) |
|-----------|-------------------------------|------------------------------------------|--------------------------|
| Config load | `init_by_lua_block` | Module scope | Module scope (`LuaScope thread`) |
| Query string clean | `ngx.req.set_uri_args` | `request_handle:headers():replace(":path", …)` | `r.args = gsub(…)` |
| POST body clean | `ngx.req.set_body_data` | `body_handle:setBytes(…)` | Not implemented |
| IP tracking | `lua_shared_dict` | `/tmp/detected_ips.json` | Not implemented |
| HTML injection | `body_filter_by_lua_block` | `response_handle:body():setBytes(…)` | `LuaOutputFilter` coroutine |
| Injection scope | Full buffered body | Full body | Per bucket (see caveat above) |
