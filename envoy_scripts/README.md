<!-- envoy_scripts/README.md: documents Lua assets for Envoy profile; json.lua is third-party, do not hand-edit without reason. -->
# envoy_scripts (Envoy Lua HTTP filter)

<!-- Scope: why this folder — Envoy mounts it read-only; injection.lua is the WADM implementation for Envoy+Lua. -->
Scripts mounted at `/etc/envoy/scripts` for the **Envoy + Lua** profile. `injection.lua` is the active filter; `json.lua` is a vendored JSON encoder/decoder (see `download_json_lua.sh`). `package.path` is adjusted so `require("json")` resolves.

The detection and injection behaviour is the cross-edge canonical mechanism shared with OpenResty, Apache and Envoy+WASM; `docs/EDGE_LEVELING.md` ("Behavioural equivalence and latency pass") defines it.

## Internal data flow

### Startup (module scope, once per worker thread's Lua state)

<!-- Startup: why precompile here — the Lua filter has no init hook; module load runs once per worker. -->
1. Reads `/etc/envoy/config.json` and decodes it with `json.decode`.
2. Precompiles the same structures as the OpenResty edge:
   - per-path plans: joined comments, decoy headers, prebuilt `Set-Cookie` strings, extra body payloads
   - the trigger list
   - per-kind detector token lists, with `enabled` already applied
   - SQLi method and path sets
3. Preallocates the `gettimeofday` FFI struct used by `now_us`.

### `envoy_on_request(request_handle)`

<!-- Request phase: why the path goes into dynamic metadata — :path does not exist on the response side. -->
1. **Setup (untimed).**
   - Caches `headers()`, splits `:path` into the raw `path` and the query, and stores `path` in the `wadm.honeypot` dynamic-metadata namespace for the response phase.
   - Reads the client IP from `streamInfo():downstreamDirectRemoteAddress()` (port stripped). This is the socket peer, as on OpenResty and Apache; the IP used to be read from `X-Forwarded-For`, which k6 never sends.
   - Parses the query once into ordered segments.
2. **SQLi trap (first, and terminal).** When the path is a trap path, `:method` is read. If the method also matches, the filter buffers the body with `body()`, matches it, logs `WADM ALERT` on a hit or `WADM TRAP` otherwise, marks `local_response`, and answers with `respond()`.
3. **Additional kinds**, one timed region each, in the fixed order. Each timer wraps the scan plus an in-memory `detected_ips` write on a hit. Alerts are rendered and logged after the timer closes.
4. **html_comments.**
   - Timed region: scan the segments → drop every segment that hit → `headers:replace(":path", path .. "?" .. kept)` → record the IP.
   - `Envoy Lua Detection execution time (us): N` is logged only on a hit.
   - The old per-request "known attacker" log line is gone. It fired on every request after the first hit and existed on this edge only; the IP store is write-only on every edge.
5. **POST body** (`post_body_inspection`, off by default): the same segment parser, with `setBytes` rebuilding the body, plus a raw substring scan for other content types. `form_fields` checks the body for tamper, as on OpenResty.

### `envoy_on_response(response_handle)`

<!-- Response phase: why the local_response bail-out — sendLocalReply re-enters the whole encoder chain. -->
1. Returns immediately if the request phase marked `local_response`. Envoy's `sendLocalReply` re-enters the **whole** encoder chain, this filter included, so without this check the SQLi trap page would be stamped with honeytokens.
2. Looks up the plan by the stashed path.
3. Writes the decoy headers with `headers:replace` (set semantics, like the other edges) and the `Set-Cookie` baits with `add`, each under its own timer.
4. For `text/html` responses whose plan has body work:
   - `body()` buffers the whole body before any timer starts.
   - Each extra payload is spliced under its own timer.
   - The `html_comments` splice plus `setBytes` runs under the benchmarked timer. `setBytes` also rewrites `Content-Length`.

**Why `body()` and never `bodyChunks()` in the trap.** `respond()` is rejected once Envoy's
`headers_continued_` flag is set. Buffering with `body()` parks the coroutine in `WaitForBody` and
resumes it in `State::Responded`, leaving the flag clear; `bodyChunks()` uses `WaitForBodyChunk`,
which falls through the branch that sets it. This edge is therefore the *only* one of the four that
can both read the request body and emit a local reply — see `docs/EDGE_LEVELING.md` for how the
WASM edge has to work around the opposite constraint.
