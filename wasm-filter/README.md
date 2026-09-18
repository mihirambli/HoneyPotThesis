<!-- wasm-filter/README.md: explains the Rust WASM filter only; Envoy wiring lives in envoy-wasm/ and CONTEXT.md. -->
# wasm-filter (Envoy HTTP WASM)

<!-- Opening: why this crate exists — Envoy loads the .wasm binary and passes JSON plugin config. -->
This is a Rust `cdylib` implementing an **Envoy HTTP filter** on the **proxy-wasm** ABI.
- Envoy loads `filter.wasm`. It is built as `wasm_filter.wasm` and copied to `/etc/envoy/wasm/filter.wasm` in Compose.
- The plugin configuration arrives as JSON, injected from `config.json` by `envoy-wasm/entrypoint.sh`.

The detection and injection behaviour is the cross-edge canonical mechanism shared with OpenResty, Envoy+Lua and Apache; `docs/EDGE_LEVELING.md` ("Behavioural equivalence and latency pass") defines it.

## Build

`Dockerfile` runs `cargo build --locked --release`:
- `Cargo.lock` pins the crate versions the benchmark results were produced with.
- `[profile.release]` in `Cargo.toml` turns on LTO, a single codegen unit and abort-on-panic. Symbols are kept so a V8 trap still names the failing function.

Compose reuses an existing `rust-builder` image unless it is told to rebuild. So after editing this crate, use `docker compose --profile wasm up -d --build` (the WASM runner and `parity_check.py` both do this).

## Internal data flow

<!-- Step 1: RootContext — why compile here — per-request work becomes lookups only. -->
1. **Bootstrap (`HoneypotRoot::on_configure`)**
   - Deserialises the plugin JSON into `Config`. Every token field defaults when absent, as on the Lua edges.
   - `compile()` turns the config into an `Rc<Compiled>`:
     - **per-path plans** — joined comments as bytes, decoy headers, prebuilt `Set-Cookie` strings, extra body payloads
     - the trigger list
     - the per-kind detector vectors, with the `enabled` switch already applied
     - the SQLi policy
2. **Per-request context (`create_http_context`)**: clones the two `Rc`s: the compiled config and the attacker-IP set.
3. **Request headers (`on_http_request_headers`)**
   - **Setup, untimed.**
     - Reads `:path` **once** and splits it into the raw path and the query.
     - Takes the client IP from the `source.address` property (port stripped). This is the socket peer, as on the other edges; the IP used to be read from `X-Forwarded-For`, which k6 never sends.
     - Parses the query once into ordered segments. A segment is decoded only if it contains `%` or `+`.
     - Reads `:method` only when the path is a trap path, and `cookie` only if a cookie token exists, because every header read is a host call into Envoy.
   - **SQLi trap:** see below.
   - **Additional kinds**, one timed region each, in the fixed order. `WADM ALERT` lines are rendered from `Alert` values after the timer closes.
   - **html_comments.** Timed region: scan the segments → drop every segment that hit → `set_http_request_header(":path", path?kept)` → insert the IP. `WASM Detection execution time (us): N` is logged only on a hit.
4. **Request body (`on_http_request_body`), SQLi trap only.** Treats the body as complete on `end_of_stream` **or** once `body_size` reaches the declared `Content-Length`. It then matches the body and logs either `WADM ALERT` or `WADM TRAP`, and stashes the rendered page for the response phase.
5. **Response headers (`on_http_response_headers`)**
   - For a trap request, rewrites `:status`, `content-type` and framing, and skips injection.
   - Otherwise, if the plan has body work and the response is `text/html`, drops `content-length` and marks the stream for injection.
   - The decoy headers (set) and the `Set-Cookie` baits (added) are each written under their own timer.
6. **Response body (`on_http_response_body`)**
   - Pauses until `end_of_stream`, then reads the body as **bytes**.
   - Each extra payload is spliced under its own timer, then the html_comments splice plus `set_http_response_body` runs under the benchmarked timer.
   - It used to decode the body as UTF-8 first. That cost a validation pass and skipped injection entirely for non-UTF-8 pages, which the Lua edges inject into.

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
(verified by `benchmarks/parity_check.py`); the cost is one round-trip to a same-network static
nginx that would have 404'd anyway.

Two Rust-specific parity details:
- `sqli_normalize` uses `to_ascii_lowercase()` rather than the full-Unicode `to_lowercase()`, and an explicit whitespace set that includes `\x0b`, so the fold matches Lua's byte-wise `string.lower` and `%s` exactly.
- Decoded query values and request bodies go through `from_utf8_lossy`, so invalid UTF-8 is replaced rather than passed through as the Lua edges do. The result is identical for all ASCII input.
