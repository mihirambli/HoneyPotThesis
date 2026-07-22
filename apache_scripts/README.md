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
│ Output filter chain                                      │
│  inject.lua → handle_inject(r)  [LuaOutputFilter]       │
│  • content-type guard + path matching (setup)           │
│  • buffers every brigade chunk, then one gsub at EOS     │
│  • whole modified body yielded once at end-of-stream     │
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

Implements the **canonical injection contract** (see `benchmarks/README.md` → "Level-playing-field invariants" #6) so its injection timing is directly comparable to OpenResty / Envoy+Lua / WASM.

- **Coroutine stages:** first `coroutine.yield()` signals readiness; the `while bucket ~= nil` loop **accumulates** every brigade chunk (yielding `""` so nothing is emitted yet); after end-of-stream a single whole-body transform runs and the modified body is emitted at the final `coroutine.yield(new_body)`.
- **Buffered whole body (not per-chunk):** the previous version ran `string.gsub` on each bucket individually, which missed a `</body>` split across chunks and logged one timing line *per chunk*. It now buffers the full body first (mirroring OpenResty's `ctx.body_chunks`) and does one first-match splice, logging exactly one timing line per response.
- **Path matching:** `comments_for_path(r.uri)` selects every honeytoken whose `paths` match this request (`/*` or exact), identical to the other edges — the previous version hardcoded `html_comments[1]` and ignored `paths`.
- **Content-Type guard:** `handle_inject` checks `r.content_type` internally; non-HTML responses stream through unchanged and are never buffered. (`httpd.conf` uses `SetOutputFilter WADM_INJECT` — applied to every response — so the guard lives in the script.)
- **Timed region:** only `assemble body → find first </body> → splice → produce new body`. The content-type guard, path matching, and comment join are setup and run *outside* `r:clock()`; Content-Length is handled by `Header always unset Content-Length` in `httpd.conf`, outside the timer.

## Parity reference

| Behaviour | OpenResty (`nginx/nginx.conf`) | Envoy Lua (`envoy_scripts/injection.lua`) | Apache mod_lua (this dir) |
|-----------|-------------------------------|------------------------------------------|--------------------------|
| Config load | `init_by_lua_block` | Module scope | Module scope (`LuaScope thread`) |
| Query string clean | `ngx.req.set_uri_args` | `request_handle:headers():replace(":path", …)` | `r.args = gsub(…)` |
| POST body clean | `ngx.req.set_body_data` | `body_handle:setBytes(…)` | Not implemented |
| IP tracking | in-memory `lua_shared_dict` | in-memory module-scope Lua table | in-memory module-scope Lua table |
| HTML injection | `body_filter_by_lua_block` | `response_handle:body():setBytes(…)` | `LuaOutputFilter` coroutine |
| Injection scope | Full buffered body | Full buffered body | Full buffered body |
| Path matching | `paths` (`/*` / exact) | `paths` (`/*` / exact) † | `paths` (`/*` / exact) |
| Request path source (response phase) | `ngx.var.uri` | dynamic metadata stashed on request † | `r.uri` |

† The `:path` pseudo-header exists only on the **request**, so `envoy_on_response` cannot
read it. Envoy+Lua therefore stashes the path-only portion in per-stream dynamic metadata
(`streamInfo():dynamicMetadata():set("wadm.honeypot", "request_path", …)`) during
`envoy_on_request` and reads it back during `envoy_on_response` — the same trick the WASM
filter uses with `self.request_path`. Before this, `uri` fell back to `/`, so only `/*`
tokens ever matched and exact-path tokens (e.g. one scoped to `/index.html`) were silently
missed. The stash/read both sit **outside** the timed regions, so they do not affect the
benchmark numbers.
