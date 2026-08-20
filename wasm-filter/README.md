<!-- wasm-filter/README.md: explains the Rust WASM filter only; Envoy wiring lives in envoy-wasm/ and CONTEXT.md. -->
# wasm-filter (Envoy HTTP WASM)

<!-- Opening: why this crate exists — Envoy loads the .wasm binary and passes JSON plugin config. -->
Rust `cdylib` implementing an **Envoy HTTP filter** with the **proxy-wasm** ABI. Envoy loads `filter.wasm` (built as `wasm_filter.wasm`, copied to `/etc/envoy/wasm/filter.wasm` in Compose) and passes plugin configuration as JSON (injected from `config.json` via `envoy-wasm/entrypoint.sh`).

## Internal data flow

<!-- Step 1: RootContext — why parse config once here — avoids per-request JSON parse and shares Rc across streams. -->
1. **Bootstrap (`HoneypotRoot`)**  
   `on_configure` reads `get_plugin_configuration()` bytes, deserializes into `Config` (same shape as root `config.json`: `honeytokens.html_comments`), stores `Rc<Config>`, and returns success only if parsing succeeds.

<!-- Step 2: factory — why create_http_context — Envoy needs a new HttpContext per HTTP stream with cloned config. -->
2. **Per-request context (`HoneypotHttp`)**  
   `create_http_context` clones the shared `Rc<Config>` and initializes `request_path` empty.

<!-- Step 3: request path — why on_http_request_headers — strip trigger substrings from :path before upstream sees them; stash path for response injection matching. -->
3. **Request headers (`on_http_request_headers`)**  
   - Reads `:path`, strips query for `request_path` (used later for injection path matching).  
   - For each honeytoken with a non-empty `trigger_keyword`, if the **full** `:path` (including query) contains that keyword, logs `WADM ALERT` and removes the keyword substring from `:path` via `set_http_request_header`.  
   - Returns `Action::Continue` so the request proceeds to the router/upstream.

<!-- Step 3b: request body — why this exists at all — the SQLi trap needs the POST body, and this is the only callback that receives it. -->
3b. **Request body (`on_http_request_body`) — SQLi trap only**  
   Skipped unless request headers matched the `sql_injection` policy. Treats the body as complete on `end_of_stream` **or** once `body_size` reaches the declared `Content-Length`, then matches, logs, and stashes the rendered page in `sqli_page` for the response phase. Always returns `Action::Continue`.

<!-- Step 4: response headers — why continue not pause — content-length is dropped here; body rewriting happens in the body callback. -->
4. **Response headers (`on_http_response_headers`)**  
   For a trap request (`sqli_page` set), overwrites `:status`, sets `content-type`, drops `content-length`, and skips honeytoken injection. Otherwise: if `content-type` contains `text/html`, clears `content-length` and flags the stream for body rewriting. Returns `Action::Continue` either way.

<!-- Step 5: response body — why buffer to end_of_stream — proxy-wasm needs full body to inject before </body> in this implementation. -->
5. **Response body (`on_http_response_body`)**  
   - For a trap request, discards whatever the origin returned and writes `sqli_page` instead.  
   - Buffers: returns `Action::Pause` until `end_of_stream` is true, then reads the full body with `get_http_response_body`.  
   - If UTF-8 fails, forwards unchanged.  
   - Collects `comment_value` strings whose `paths` match `/*` or the stored `request_path`.  
   - Injects the joined comments immediately before the first `</body>`, or appends if no `</body>`.  
   - `set_http_response_body` replaces the buffered chunk and returns `Action::Continue`.

<!-- Summary: why one paragraph — quick mental model for readers comparing to Lua/OpenResty. -->
**Summary:** configuration is parsed once at the root; each stream matches paths and optionally scrubs keywords from `:path` on the way in, then for HTML responses buffers the entire body on the way out to inject HTML comment honeytokens.

## SQLi trap: why this edge is the odd one out

The other three edges answer `POST /api/login` with a local reply and never contact the origin.
This one cannot, and both alternatives were tried and observed to fail against
`envoyproxy/envoy:v1.30-latest`:

- `Action::Pause` from `on_http_request_headers` stops the stream outright — `on_http_request_body`
  is never delivered, and the request hangs until it times out.
- Calling `send_http_response` from `on_http_request_body` returns `BadArgument` (status 2), which
  the Rust SDK `unwrap()`s into a VM panic: `Function: proxy_on_request_body failed: Uncaught
  RuntimeError: unreachable`. This happens whether or not the data path was paused first.

`send_http_response` **is** legal from `on_http_request_headers`, which is how a body-less `POST` is
answered inline. Everything else detects on the request body and swaps the upstream reply for the
trap page on the response side. The attacker-visible bytes are identical to the other three edges
(verified by `md5sum`); the cost is one round-trip to a same-network static nginx that would have
404'd anyway. See `docs/EDGE_LEVELING.md` for the full parity table.

Two Rust-specific parity details: `sqli_normalize` uses `to_ascii_lowercase()` (not the
full-Unicode `to_lowercase()`) and an explicit whitespace set that includes `\x0b`, so the fold
matches Lua's byte-wise `string.lower` and `%s` exactly. Request bodies go through
`from_utf8_lossy`, so invalid UTF-8 is replaced rather than passed through as the Lua edges do —
identical for all ASCII payloads, i.e. every canonical SQLi string.

## Additional honeytoken kinds

`lib.rs` also implements `http_headers`, `cookies`, `decoy_paths`, and `form_fields` (serde structs
`HeaderToken` / `CookieToken` / `DecoyPathToken` / `FormFieldToken`), plus the per-token `enabled`
switch (the `Enabled` untagged enum + `is_on`, accepting JSON number / bool / string). All of it runs
**outside** the timed html_comments regions, so that measurement is unaffected — and each kind
carries **its own** `get_current_time()` region logged as
`WADM TOKEN <kind> detect|inject (us): N` (see `benchmarks/README.md`).

- **Detection** (`detect_additional`, in `on_http_request_headers`): header/cookie value
  **replay** (`:path` / `:authority` substring), cookie & form-field **tamper** (returned value ≠
  planted), and decoy-path **URI match** (`exact` / `prefix`). Form-field tamper is query-only (WASM
  has no POST-body inspection). Hits log `WADM ALERT` and insert the IP. The `DetectCtx` struct (IP,
  `:path`, authority, path-only URI, `cookie`) is built once outside every timer; each kind's timer
  wraps its scan + `detected_ips` insert only — detectors push `Alert` descriptors and the
  alerts are rendered and written after the timer closes, so no log I/O is charged to detection. Kinds run in the fixed
  `KIND_ORDER` so the timed regions sequence identically on every edge.
- **Header injection** (`inject_response_headers`, in `on_http_response_headers`): decoy headers via
  `set_http_response_header`, `Set-Cookie` via `add_http_response_header` (any content type). Token
  selection is setup outside the timer; the timed region is the header write.
- **Body injection** (`on_http_response_body`): hidden form inputs before `</form>`, decoy links
  before `</body>`. Each splice is timed on its own (region = `splice_before_if_present`; markup
  construction and the single `set_http_response_body` write-back sit outside); the html_comments
  splice keeps its own unchanged timer.

All new-kind `WADM ALERT` messages use the shared `honeytoken triggered by <ip> — …` format for
cross-edge comparability (the pre-existing html_comments message is left unchanged). Because
`config.json` is shared across every edge, the serde structs use `#[serde(default)]` and unknown
fields are ignored, so a config with or without these keys parses on all edges.
