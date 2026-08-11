<!-- envoy_scripts/README.md: documents Lua assets for Envoy profile; json.lua is third-party, do not hand-edit without reason. -->
# envoy_scripts (Envoy Lua HTTP filter)

<!-- Scope: why this folder — Envoy mounts it read-only; injection.lua is the WADM implementation for Envoy+Lua. -->
Scripts mounted at `/etc/envoy/scripts` for the **Envoy + Lua** profile. `injection.lua` is the active filter; `json.lua` is a vendored JSON encoder/decoder (see `download_json_lua.sh`). `package.path` is adjusted so `require("json")` resolves.

## Internal data flow

### Startup (module scope)

<!-- Startup: why load config at require time — Lua filter has no separate init hook; module load reads JSON once per worker semantics depend on Envoy; keeps helpers in one place. -->
1. Opens `/etc/envoy/config.json`, reads entire file, `json.decode` into `config`.  
2. Defines helpers: URL decode/encode, query-string parse/rebuild, path-based comment selection (`get_comments_for_path`), trigger keyword collection (`get_trigger_keywords`), and in-memory attacker IP tracking in a module-scope Lua table (`record_attacker_ip`, `is_known_attacker`) — mirroring OpenResty's `lua_shared_dict wadm_state` (no on-disk store).

### `envoy_on_request(request_handle)` — request path

<!-- Request phase: why richer than WASM — Lua version inspects query + form + raw body and persists IPs to disk for demo alerting. -->
0. **SQLi trap (first, and terminal).** If `sqli_owns(:method, path)` matches the top-level `sql_injection` policy, this filter answers the request itself and never routes it: buffer the body, match the watched fields against the signature list, log `WADM ALERT` + record the IP on a hit, mark `local_response` in the `wadm.honeypot` dynamic-metadata namespace, then `request_handle:respond(...)`.
1. If there are no trigger keywords, returns early.  
2. Reads `:path` and client IP from `x-forwarded-for`, `x-real-ip`, or `"unknown"`.  
3. If IP is already in the JSON file, logs a “known attacker” warning.  
4. **Query string:** if `?` present, parses parameters; any key/value containing a trigger keyword logs `WADM ALERT`, removes that param, rebuilds query, replaces `:path` if dirty.  
5. **Body:** if body exists, reads bytes. For `application/x-www-form-urlencoded`, parses like a query string, strips offending params, may rewrite body and `content-length`. For other types, scans raw body for keywords and logs (does not strip).  
6. If any query/body sanitization or keyword hit occurred, `record_attacker_ip(ip)` records the IP in the in-memory table.

### `envoy_on_response(response_handle)` — response path

<!-- Response phase: why separate from request — only HTML bodies get honeytoken comments; avoids touching JSON/API responses. -->
0. Returns immediately if the request phase marked `local_response`. Envoy's `sendLocalReply` re-enters the **whole** encoder chain, this filter included, so without the bail-out the SQLi trap page would be stamped with honeytokens. The check must come before any `headers()`/`body()` call.  
1. Ignores non-HTML `content-type`.  
2. Derives URI without query from `:path`, builds `to_inject` comment list from config path rules.  
3. Reads full response body, injects concatenated comments before `</body>` (or appends), updates `content-length`.

**Why `body()` and never `bodyChunks()` in the trap.** `respond()` is rejected once Envoy's
`headers_continued_` flag is set. Buffering with `body()` parks the coroutine in `WaitForBody` and
resumes it in `State::Responded`, leaving the flag clear; `bodyChunks()` uses `WaitForBodyChunk`,
which falls through the branch that sets it. This edge is therefore the *only* one of the four that
can both read the request body and emit a local reply — see `docs/EDGE_LEVELING.md` for how the
WASM edge has to work around the opposite constraint.

<!-- Summary: why — contrasts with wasm-filter and nginx READMEs for stack choice. -->
**Summary:** Lua mirrors the honeypot idea with richer request inspection than the WASM filter (query + POST + generic body + in-memory IP tracking). HTML injection happens on the response body in one shot after the body is available to the script.

## Additional honeytoken kinds

Beyond `html_comments`, `injection.lua` also implements `http_headers`, `cookies`, `decoy_paths`,
and `form_fields` with the same semantics and `WADM ALERT` message formats as the OpenResty
reference (`nginx/nginx.conf`), plus the per-token `enabled` switch (`token_enabled`). All of it
runs **outside** the html_comments detection/injection timers, so that measurement is unaffected —
and each kind carries **its own** `get_micro_time()` region logged as
`WADM TOKEN <kind> detect|inject (us): N` (see `benchmarks/README.md`).

- **Detection** (`detect_additional`, in `envoy_on_request`): header/cookie value **replay**
  (path / `:authority` / query substring), cookie & form-field **tamper** (returned value ≠ planted),
  and decoy-path **URI match** (`exact` / `prefix`). Hits log `WADM ALERT` and record the IP. The
  `actx` table (IP, path, authority, parsed query, `cookie`) is built once outside every timer; each
  kind's timer wraps its scan + alert + `record_attacker_ip` and only logs on a hit. Kinds run in
  the fixed `KIND_ORDER` so the timed regions sequence identically on every edge.
- **Injection** (`envoy_on_response`): decoy response headers + `Set-Cookie` baits via
  `headers():add(...)` (any content type, before the content-type guard); hidden form inputs (before
  `</form>`) and decoy links (before `</body>`) spliced into HTML bodies. Token selection and markup
  construction are setup outside the timers; the header timers wrap the `add(...)` calls and the
  body timers wrap locate-anchor → splice, with the single `setBytes` write-back outside.

Parity note: form-field **POST-body** tamper is OpenResty-only for now; the query-string case — the
only one active while `post_body_inspection` is off (default) — is detected here.
