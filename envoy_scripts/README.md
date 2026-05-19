<!-- envoy_scripts/README.md: documents Lua assets for Envoy profile; json.lua is third-party, do not hand-edit without reason. -->
# envoy_scripts (Envoy Lua HTTP filter)

<!-- Scope: why this folder — Envoy mounts it read-only; injection.lua is the WADM implementation for Envoy+Lua. -->
Scripts mounted at `/etc/envoy/scripts` for the **Envoy + Lua** profile. `injection.lua` is the active filter; `json.lua` is a vendored JSON encoder/decoder (see `download_json_lua.sh`). `package.path` is adjusted so `require("json")` resolves.

## Internal data flow

### Startup (module scope)

<!-- Startup: why load config at require time — Lua filter has no separate init hook; module load reads JSON once per worker semantics depend on Envoy; keeps helpers in one place. -->
1. Opens `/etc/envoy/config.json`, reads entire file, `json.decode` into `config`.  
2. Defines helpers: URL decode/encode, query-string parse/rebuild, path-based comment selection (`get_comments_for_path`), trigger keyword collection (`get_trigger_keywords`), and attacker IP persistence in `/tmp/detected_ips.json` (`load_detected_ips`, `record_attacker_ip`, `is_known_attacker`).

### `envoy_on_request(request_handle)` — request path

<!-- Request phase: why richer than WASM — Lua version inspects query + form + raw body and persists IPs to disk for demo alerting. -->
1. If there are no trigger keywords, returns early.  
2. Reads `:path` and client IP from `x-forwarded-for`, `x-real-ip`, or `"unknown"`.  
3. If IP is already in the JSON file, logs a “known attacker” warning.  
4. **Query string:** if `?` present, parses parameters; any key/value containing a trigger keyword logs `WADM ALERT`, removes that param, rebuilds query, replaces `:path` if dirty.  
5. **Body:** if body exists, reads bytes. For `application/x-www-form-urlencoded`, parses like a query string, strips offending params, may rewrite body and `content-length`. For other types, scans raw body for keywords and logs (does not strip).  
6. If any query/body sanitization or keyword hit occurred, `record_attacker_ip(ip)` writes `/tmp/detected_ips.json`.

### `envoy_on_response(response_handle)` — response path

<!-- Response phase: why separate from request — only HTML bodies get honeytoken comments; avoids touching JSON/API responses. -->
1. Ignores non-HTML `content-type`.  
2. Derives URI without query from `:path`, builds `to_inject` comment list from config path rules.  
3. Reads full response body, injects concatenated comments before `</body>` (or appends), updates `content-length`.

<!-- Summary: why — contrasts with wasm-filter and nginx READMEs for stack choice. -->
**Summary:** Lua mirrors the honeypot idea with richer request inspection than the WASM filter (query + POST + generic body + IP file). HTML injection happens on the response body in one shot after the body is available to the script.
