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

<!-- Step 4: response headers — why pause on HTML — must drop content-length and pause so body callback can rewrite length. -->
4. **Response headers (`on_http_response_headers`)**  
   If `content-type` contains `text/html`, clears `content-length` and returns `Action::Pause` so the filter can observe and rewrite the body; otherwise continues.

<!-- Step 5: response body — why buffer to end_of_stream — proxy-wasm needs full body to inject before </body> in this implementation. -->
5. **Response body (`on_http_response_body`)**  
   - Buffers: returns `Action::Pause` until `end_of_stream` is true, then reads the full body with `get_http_response_body`.  
   - If UTF-8 fails, forwards unchanged.  
   - Collects `comment_value` strings whose `paths` match `/*` or the stored `request_path`.  
   - Injects the joined comments immediately before the first `</body>`, or appends if no `</body>`.  
   - `set_http_response_body` replaces the buffered chunk and returns `Action::Continue`.

<!-- Summary: why one paragraph — quick mental model for readers comparing to Lua/OpenResty. -->
**Summary:** configuration is parsed once at the root; each stream matches paths and optionally scrubs keywords from `:path` on the way in, then for HTML responses buffers the entire body on the way out to inject HTML comment honeytokens.
