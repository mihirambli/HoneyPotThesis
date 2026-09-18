# Thesis Notes

Explanations and reasoning captured during development for later use when writing the thesis. Entries are dated and kept in the order they were recorded.

---

## 2026-07-08 — Why a warm-up phase is needed in the benchmarks

**Cold start**, in this project, means the extra latency the very first requests hit before the runtime's internal caches and optimizations have kicked in. Several independent things are cold at container startup:

**1. JIT compilation (the dominant effect here)**
- OpenResty and Envoy+Lua both run on **LuaJIT**. LuaJIT doesn't compile Lua to machine code immediately — it starts by *interpreting* bytecode, and only after a loop/function executes enough times does its trace compiler kick in and emit native machine code for that hot path. The first N calls to `access_by_lua_block` / `envoy_on_request` run in the slow interpreted path; later calls run compiled and are dramatically faster.
- Envoy's WASM VM (used for the Rust filter) does something analogous — WASM runtimes typically use a fast baseline compiler for the first executions, then re-optimize hot functions with a higher-tier compiler.
- Apache's mod_lua is less JIT-dependent (often plain Lua 5.x, not LuaJIT), so it's less affected by this specific mechanism.

**2. Everything else that "warms up" under sustained traffic**
- TCP connection pool / keep-alive reuse between k6 and the edge — early requests pay full connection setup cost, later ones reuse open connections.
- OS/page cache for the proxy binaries and shared libraries.
- Memory allocator behavior (heap/arena growth stabilizes after initial allocations).
- Envoy's cluster connection pool to the upstream `backend` ramping up.

**Why it distorts the benchmark:** the VU=1 run is always the *first* traffic the edge has ever seen, so it pays 100% cold-start cost. The VU=500 run happens last, after thousands of prior requests, so it is fully warm. That means what looks like "latency improves under higher concurrency" is partly a real scaling effect and partly just "this run happened later." This was observed directly: Envoy+Lua's detection median dropped from 234µs (VU=1) to 47µs (VU=500) — a large chunk of that gap was warm-up, not concurrency.

**What the warm-up phase does:** before any *recorded* VU level runs, the benchmark orchestrators fire one throwaway burst of real traffic (VU=100 for 20s) at the edge and discard the results. That traffic exercises the exact same code paths (LuaJIT tracing, WASM tiering, connection reuse) so the JIT has already compiled the hot paths and the pools are already established by the time recording starts at VU=1. All four recorded levels then start from the same warm baseline, so differences between VU=1 and VU=500 reflect actual concurrency/scaling behavior rather than "which level happened to run first."

---

## 2026-09-17 — OpenResty opens a new upstream connection for every request

**The finding:** in this project's configuration, OpenResty opens a fresh TCP connection to the
`backend` origin for *every* proxied request and closes it afterwards. Envoy and Apache both
reuse a small pool of connections. This is a property of how nginx is configured here, not of
OpenResty as a product.

### How it was measured

With each edge running its **baseline (no WADM)** config, 100 requests were sent through it over a
*single* client keep-alive connection:

```
curl -s -o /dev/null "http://localhost:<port>/?n=[1-100]"
```

Because the client side is one connection, any additional connections must be on the
proxy → backend side. Sockets touching port 80 were then counted inside the proxy's and the
backend's network namespaces by reading `/proc/net/tcp` and `/proc/net/tcp6` directly
(state column `06` = TIME_WAIT, i.e. a connection that has been closed; `01` = ESTABLISHED):

| Bare proxy | New TIME_WAIT sockets after 100 requests | ESTABLISHED to backend afterwards |
|---|---|---|
| **OpenResty** | **100** | 0 |
| Envoy | 0 | 1 |
| Apache | 0 | 2 |

So OpenResty performed a full TCP handshake *and* teardown per request, while Envoy served all
100 requests over one pooled connection and Apache over two.

### Why

nginx does not reuse upstream connections by default. Keeping them alive requires three things
together, none of which are present in either OpenResty config
(`nginx/nginx.conf` or `nginx/nginx-baseline.conf`):

- `keepalive <n>;` inside the `upstream` block — sets the per-worker cache of idle upstream
  connections. Without it there is no pool at all.
- `proxy_http_version 1.1;` — nginx proxies with HTTP/1.0 by default, which has no persistent
  connections.
- `proxy_set_header Connection "";` — otherwise the client's `Connection` header is forwarded
  upstream and can close the connection.

Envoy pools upstream connections per cluster by default, and Apache's `mod_proxy` reuses backend
connections by default, so both get pooling without configuration.

### Consequence for the measurements — smaller than it first appeared

Intuitively, a connection per request should make OpenResty's **proxy tax** (the latency a bare
proxy adds over talking to the origin directly) larger than the other edges'. The first
measurement appeared to show exactly that: +3.20 ms at 10 VUs, against +0.70 to +1.03 for Envoy
and Apache.

**That was a contaminated run, not a connection-churn effect.** Three repeats of the bare
OpenResty tier (2026-09-17) gave median `http_req_duration` in ms, with the throughput ratio in
brackets:

| Run | VU=1 | VU=10 | VU=100 |
|---|---|---|---|
| Original | 1.13 (1.00) | **4.39 (0.97)** | 3.56 (0.98) |
| Repeat 1 | 1.20 (1.00) | 1.77 (1.00) | 2.36 (0.99) |
| Repeat 2 | 1.04 (1.00) | 1.79 (1.00) | 2.41 (0.98) |
| Repeat 3 | 1.20 (1.00) | 1.98 (1.00) | 2.36 (0.99) |

The original run's 10-VU median is 2.4× the repeats', and its 100-VU median ~50% higher. The
throughput ratio of 0.97 at 10 VUs (repeats: 1.00) was the tell that the host was interfering.
Recomputed against the origin floor, OpenResty's proxy tax is **+0.53 / +0.65 / +0.34 ms** at
1 / 10 / 100 VUs — in line with the other edges, and at 100 VUs the lowest of the four. (Caveat:
the origin floor is still from the original session, so this is a cross-session comparison.)

So the connection-per-request behaviour is **real and directly measured, but its latency cost at
these request rates is small**. A TCP handshake to a container on the same Docker bridge costs
tens of microseconds, which does not dominate at ≤100 VUs. It would matter far more against a
remote origin with real network round-trip times, and may still matter at saturation where
connection setup competes for the same CPU.

Two conclusions survive:

1. **The WADM overhead figures remain fair for OpenResty.** The WADM and baseline OpenResty
   configs carry identical `upstream` and `proxy_*` directives (verified by inspection — the
   socket counting above was performed on the baseline tier only), so the per-request connection
   cost is paid in both tiers and largely cancels in the `WADM − bare` subtraction.
2. **The run-to-run noise floor exceeds the between-edge differences** that the proxy-tax
   comparison is trying to resolve. One run per tier cannot support a cross-edge claim about
   proxy cost. Corroborating evidence: the bare Envoy stacks from the Lua and WASM runs are
   *identical* configurations, yet differed by 16% at 10 VUs and 24% at 100 VUs.

### Status

**The 10-VU inversion is resolved: it was noise.** It did not reproduce in any of three repeats,
all monotonic (≈1.2 → 1.8 → 2.4 ms) and tight (2–11% spread across repeats). The earlier
attribution of it to connection churn was wrong.

Upstream keep-alive is **not** enabled, and there is now no measured latency problem motivating
the change. If it is enabled later for levelling reasons, it requires three directives together —
`keepalive <n>` in the `upstream` block, `proxy_http_version 1.1`, and
`proxy_set_header Connection ""` — applied to both OpenResty configs, with both tiers re-run.

Open item: the baseline tiers for the other three edges still have only one run each and carry the
same unquantified risk. Re-running each tier at least three times and reporting medians-of-runs,
or the spread, would put the proxy-tax comparison on solid ground.

### Correction (same day, after levelling and re-running)

The conclusion above that the connection-per-request cost was "small at these request rates" is
**wrong**, and the reasoning that produced it — that a handshake across a local Docker bridge
costs only tens of microseconds — was never measured, only assumed.

Upstream keep-alive was enabled on both OpenResty configs and both tiers re-run. Comparing
against the three clean repeat runs from the *same day* (so this is not a cross-session
comparison), the bare tier improved as follows, median `http_req_duration` in ms:

| VUs | Before keep-alive (mean of 3 repeats) | After keep-alive | Saved |
|---|---|---|---|
| 1 | 1.15 | 0.66 | ~0.49 |
| 10 | 1.85 | 1.34 | ~0.51 |
| 100 | 2.38 | 1.56 | ~0.82 |

So a new connection per request cost roughly **half a millisecond per request** even at low load —
an order of magnitude more than the "tens of microseconds" estimated above.

At 500 VUs the effect was far larger, though this comparison is cross-session and therefore weaker
evidence: the WADM tier fell from 64.75 ms to 5.11 ms, and the share of offered load OpenResty
sustained rose from 76% to 95% (bare: 86% to 97%). OpenResty no longer saturates at 500 VUs.
Connection churn, not WADM, was what made it collapse under overload.

**Consequence: the origin floor is now stale.** With OpenResty levelled and re-measured, its bare
tier reads *faster* than the no-proxy origin at 100 VUs (1.56 vs 2.04) and 500 VUs (1.86 vs 2.86).
A proxy in front of an origin cannot be faster than the origin alone, so this is not a real
result — it means the origin numbers, still carried over from the earlier session, no longer
describe the machine the OpenResty numbers were taken on. Until every tier is re-measured in one
session, the figures mix sessions and the proxy tax cannot be computed.

---

## 2026-09-18 — Levelling the four edges' behaviour and reducing their latency at 500 VUs

### The problem

At 500 VUs the four edges added very different amounts of latency. Before this change (median
`http_req_duration`, bare → WADM, plus WADM p90 and the share of offered load sustained):

| Edge | bare → WADM median | WADM p90 | throughput |
|---|---|---|---|
| OpenResty | 1.86 → 5.11 ms | 43 ms | 0.95 |
| Envoy+Lua | 4.96 → 12.29 ms | 61 ms | 0.92 |
| Envoy+WASM | 3.26 → 24.47 ms | 382 ms | 0.63 |
| Apache | 4.47 → 85.95 ms | 258 ms | 0.66 |

**Why small per-request savings matter at 500 VUs.** The benchmark host has two cores shared by
the edge, the origin, the k6 load generator and Docker's log pipeline. At low load each request
is served as it arrives, so a few microseconds of extra work is invisible. Near saturation,
requests queue behind one another. Queueing delay grows *faster than linearly* as utilisation
approaches 100%, so shaving CPU off every request lowers latency by far more than the CPU saved.
The edge that needs the most CPU per request saturates first and collapses.

**Why the internal timers did not show it.** Most of that CPU was spent **outside** the
microsecond timers, by design, because the timers measure only the detection and injection work
itself. The extra work included:
- per-request loops over every token, checking `enabled` and building strings
- the query string parsed twice
- setup repeated on every response chunk
- an extra Lua hook per request on Apache
- extra log lines

The end-to-end numbers carry all of this even when the µs figures look small.

### Behaviour differences found, and why each mattered

Reviewing the four implementations side by side showed they did **not** do the same thing. All
earlier levelling had equalised *what the timers wrap*, not *what the code does*.

1. **Apache never stripped the benchmark keyword.**
   - The strip used `gsub("[^&]*" .. keyword .. "[^&]*&?", "")`, which treats the keyword as a
     Lua *pattern*. In `internal-admin.example.com`, `-` is a lazy quantifier and `.` matches any
     character, so the pattern could never match the literal text.
   - The keyword was detected by a plain `find`, logged, and then forwarded to the origin anyway.
   - Apache's "detection" timer was measuring a failed pattern match, not a strip.
2. **Four different detection mechanisms.**
   - OpenResty and Envoy+Lua matched decoded query arguments, dropped the whole argument, and
     re-encoded the query in hash-table order.
   - Apache did a raw `find` on the undecoded query string.
   - WASM searched the whole raw path, including the path part, and removed only the keyword text,
     leaving `password=` behind.
   - Same config, four different results and four different amounts of work.
3. **Both Envoy edges recorded every attacker as `unknown`.** They read the client IP from
   `X-Forwarded-For` or `X-Real-IP`, and k6 sends neither.
4. **Envoy+Lua logged a line on every request.** After the first detection it wrote "known
   attacker IP … detected in new request" on *every* later request, because all k6 traffic shares
   one IP. That is a pure latency tax paid by one edge only.
5. **OpenResty's splice was case-insensitive PCRE** (`ngx.re.sub(..., "io")`); the other three used
   a plain, case-sensitive find. So `</BODY>` matched on one edge only, at a different cost.
6. **Smaller differences.**
   - OpenResty's cookie and form-field detectors returned early after a tamper hit, skipping the
     keyword check.
   - WASM raised one form-field alert per duplicate parameter.
   - Envoy+Lua *added* the decoy header where the others *set* it.
   - WASM decoded response bodies as UTF-8 first and skipped injection for any body that was not
     valid UTF-8.

The alert text itself differed on three edges, so identical attacks produced different logs.

### The canonical mechanism now shared by all four edges

| Choice | Why |
|---|---|
| Client IP = the socket peer | Can't be spoofed with a header, and matches what OpenResty (`$remote_addr`) and Apache (`useragent_ip`) already used |
| Path = the raw request target up to `?` | Identical on every edge by construction, and needs no decoding. OpenResty's and Apache's decoded, normalised URIs cannot be reproduced on Envoy |
| Query = ordered `&`-separated segments, each keeping its raw text; `+`/`%XX` decoded only when present | One parse, shared by every detector. Keeping the raw text lets the strip work without re-encoding anything |
| Strip = rebuild the query from the raw text of the segments that did not match | Keeps the original order and encoding, and replaces four different strip implementations with one |
| Plans precompiled per path (`/*` or exact) at config load | Per-request token selection becomes one table lookup |
| One alert format, with CR/LF in attacker-supplied values neutralised | Identical logs for identical attacks, and no log-line forgery |
| One splice primitive: plain, case-sensitive find, then splice | Same behaviour and same kind of work on every edge |

`benchmarks/parity_check.py` now proves the equivalence rather than asserting it. It sends
thirteen requests to each edge (the benchmark requests plus encoding, ordering, duplicate and
SQLi cases) and diffs three things against OpenResty: responses, alert lines, and the requests
that reached the origin. All four are identical. The only exclusions are documented residuals:
response framing, the WASM trap contacting the origin, and Apache forwarding `path?` when every
parameter is stripped.

### Latency changes per edge, and why

| Edge | Change | Why |
|---|---|---|
| All | Precompiled plans, trigger lists and detector lists | Removes per-request loops, `enabled` checks and string building |
| All | Query parsed once, decoded only where needed | OpenResty and Envoy+Lua parsed it twice, once inside a timer |
| OpenResty | Body filter reduced to one lookup and an append per chunk | Token loops, the header read and a regex used to run on **every** chunk |
| OpenResty, Envoy+Lua | Preallocated timer struct | `ffi.new` allocated on every timer read |
| Envoy+Lua | "Known attacker" line and lookup removed | One extra log write per request |
| Apache | Shared `wadm.lua` core | Each script file has its own Lua state, and each parsed `config.json` separately |
| Apache | Header staging merged into the access-checker hook | Every Lua hook costs a VM lookup and request-object setup |
| Apache | `LuaCodeCache forever` | The default stats every script on every hook call; the other edges load code once |
| WASM | Compiled config in `on_configure`; fewer host calls (`:path` once, IP from one property, `:method` and `cookie` only when needed) | Each header read crosses the V8 ↔ Envoy boundary |
| WASM | Byte-level splice; LTO release profile | No UTF-8 validation pass; whole-program optimisation |

### Logging levelling

**Why the access logs were removed.** OpenResty (through nginx's implicit default) and Apache
(through `CustomLog`) wrote an access-log line for every request. Envoy writes none. That charged
two edges a per-request write the other two never paid. This is the same kind of product-default
difference as upstream keep-alive (2026-09-17), and the same rule applies: level it, and do so in
both tiers.

**How what they recorded is kept.** The access logs held three things per request: the client
IP, the request line and the status. Nothing in the code read them, but that information must
not be lost.
- **Proxied requests.** These are now recorded by the origin's access log, which gained
  `xff="$http_x_forwarded_for"`. All four edges forward the client IP:
  - OpenResty and Apache already did.
  - Both Envoy configs now set `use_remote_address: true`. That also makes Envoy's view of the
    client the socket peer, so a client-supplied header cannot spoof it.
- **Requests the edge answers itself** (the SQLi trap) never reach the origin.
  - A signature hit already logged `WADM ALERT`.
  - Every other trap request now logs `WADM TRAP: <ip> <METHOD> <path> answered locally with
    <status>`, on all four edges.

**The timing log lines were deliberately left alone.** They are benchmark instrumentation, and
the end-to-end WADM tier pays for them. Changing their format would have broken every scraper and
result file. The same instrumentation cost therefore remains in every WADM end-to-end figure.

### Integration fixes that came out of the review

- **The WASM runner would have benchmarked a stale filter.** It started the stack with
  `docker compose up -d`, which reuses an existing `rust-builder` image. After any change to the
  Rust source it would have measured the *previous* `filter.wasm`. It now uses `--build`.
- **`Cargo.lock` is now committed.** It is taken from the image that produced the earlier
  results, and the build uses `--locked`. Previously a rebuild could silently pull newer crate
  versions.

### What this means for the measurements

- The html_comments detection timer now wraps the same region on every edge: scan → strip →
  write back → record the IP.
  - The query parse moved *out* of the timer on OpenResty and Envoy+Lua.
  - Apache and WASM now do the same scan-and-strip the others do.
- There is now one injection code path on every edge. OpenResty's splice changed from PCRE to a
  plain find.
- Sample counts are unchanged, so every runner, plot and result schema works as before.
- **Numbers from before this change are not comparable** with numbers after it. All tiers were
  re-run together in one session. That also resolves the stale-origin problem noted on
  2026-09-17.

### Results

The added latency, WADM − bare, median ms, before → after. Before is the earlier session; after
is 2026-09-18.

| Edge | VU 1 | VU 10 | VU 100 | VU 500 | 500-VU p90 | 500-VU throughput |
|---|---|---|---|---|---|---|
| OpenResty | 0.42 → 0.22 | 0.52 → 0.07 | 0.72 → 1.30 (re-check 0.56) | 3.25 → 3.53 (re-check 1.76) | 43 → 59 (re-check 30) | 0.95 → 0.93 (re-check 0.95) |
| Envoy+Lua | 0.29 → 0.34 | 1.58 → 0.28 | 3.75 → 1.49 | 7.33 → 8.65 | 61 → 58 | 0.92 → 0.92 |
| Envoy+WASM | 0.24 → 0.61 | 1.64 → 0.16 | 1.32 → 1.58 | 21.21 → 8.26 | 382 → 65 | 0.63 → 0.91 |
| Apache | 0.43 → 0.56 | 0.76 → 0.38 | 1.04 → 1.10 | 81.48 → 50.59 | 258 → 244 | 0.66 → 0.66 |

- **Envoy+WASM no longer collapses.** Its 500-VU tail fell sixfold (p90 382 → 65 ms) and the
  load it sustains rose from 63% to 91%. That is level with Envoy+Lua on the same proxy, which
  suggests the collapse was the filter's own per-request cost rather than anything inherent to
  WASM on Envoy.
- **Apache's overload latency fell by about 31 ms, but it still saturates.** The cause is below.
- **Envoy+Lua improved clearly at moderate load** (10 and 100 VUs) once the per-request log line
  and the in-timer re-encode were gone.
- **OpenResty's first run was noise.** It ran first, right after other heavy Docker activity,
  and reached only 97% throughput at 100 VUs. A back-to-back re-run of its two tiers showed it
  improved: 0.56 ms at 100 VUs and 1.76 ms at 500 VUs.
- **Differences under 1 ms at low load should not be read as real.** The 2026-09-17 entry showed
  that this host's run-to-run noise exceeds them, and every figure here is a single run.

The internal medians confirm the timed regions are now the same work everywhere. At 100 VUs
html_comments detection fell from 5 to 2 µs on Envoy+Lua, 10 to 4 on Apache, and 3 to 2 on
WASM; OpenResty stayed at 2.

### Why Apache still saturates

During a 30-second, 500-VU run, Apache grew from 4 processes and 82 threads to 17 processes and
408 threads, and **34 different process IDs** wrote WADM log lines. More IDs than the peak process
count means processes were being killed and re-spawned mid-run.
- The event MPM's default spare-thread limits shrink the pool whenever load dips, then grow it
  again.
- Every new thread builds fresh Lua states and re-parses `config.json` in PUC Lua 5.1 before it
  serves a request.
- OpenResty, Envoy and the WASM filter create their workers once, at startup.

This is a configuration default rather than WADM logic, so it was not changed here. The proposed
next step, applying the same tuning-parity rule:
1. Pre-spawn Apache's full worker pool in both `httpd.conf` and `httpd-baseline.conf`
   (`StartServers` = `ServerLimit`, and `MaxSpareThreads` ≥ `MaxRequestWorkers`).
2. Re-run both Apache tiers.

### Residual differences left as they are

| Residual | Why |
|---|---|
| Response framing: Envoy+Lua sends a `Content-Length`; the others send chunked | Only Envoy+Lua holds the headers until the body is rewritten. Framing is not part of the detection/injection mechanism |
| Apache forwards `path?` when every parameter is stripped | mod_lua can only set `r.args` to a string, and mod_proxy appends `?` whenever it is set. The origin serves the same resource |
| Apache can set only one `Set-Cookie` per path | mod_lua exposes no "add" for these headers. The config plants one cookie |
| The WASM trap contacts the origin | A proxy-wasm host constraint, documented in `docs/EDGE_LEVELING.md` |
| POST-body inspection exists on OpenResty and Envoy+Lua only | Off by default. Kept and aligned between those two edges for a future four-edge implementation |
