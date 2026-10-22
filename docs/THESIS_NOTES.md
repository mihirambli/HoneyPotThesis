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

---

## 2026-09-19 — How the benchmark harness works, and why each step exists

This entry describes the benchmark procedure from start to finish:
- the environment and the workload;
- the order of operations for one edge;
- the quiet-host wait and the warm-up;
- what is measured and how it is stored;
- how the numbers become figures, and when a run counts as valid.

Each step comes with the reason it exists. This entry extends the 2026-07-08 warm-up entry and
replaces it where the two differ.

The code it describes:
- `benchmarks/wadm_timings.py`, shared by every runner;
- `benchmarks/test.js`, the k6 workload;
- the four `run_internal_<edge>_benchmark.py` runners, for the WADM tier;
- `run_baseline_benchmark.py`, for the bare and origin tiers;
- the plotters.

### 1. The two questions, and the two measurement planes

The benchmarks answer two different questions, and each needs its own measurement:

| Question | Plane | Unit | Source | Exists for |
|---|---|---|---|---|
| What does one honeytoken operation cost inside an edge? | Internal timers | µs | Timing lines the edge writes to its own log | WADM tier only |
| What does deploying WADM add to a request? | End-to-end latency | ms | k6, the load generator | Every tier |

Internal timers exist only where WADM code runs, so they cannot compare WADM against no WADM. The
end-to-end plane can, because k6 measures every tier the same way. The two planes are never mixed:
each stays in its native unit on disk and is converted only when plotted.

Three tiers are measured on the end-to-end plane:

| Tier | Request path | What it isolates |
|---|---|---|
| origin | k6 → backend | the floor: network plus origin |
| bare | k6 → edge without WADM → backend | the cost of the proxy itself (bare − origin) |
| wadm | k6 → edge with WADM → backend | the cost of WADM (wadm − bare) |

The four edges are OpenResty, Envoy+Lua, Envoy+WASM and Apache+mod_lua.

### 2. Test environment

- **Everything runs in Docker Compose on one host.** Three containers share one bridge network,
  `honeypot`:
  - the backend, nginx serving static HTML;
  - the edge under test;
  - the load generator.
- **The load generator runs inside that network.** k6 (`grafana/k6`) reaches the edge by its
  Compose service name, for example `http://envoy:8080`.
  - *Why:* going through a published host port would add Docker's NAT and the host networking
    stack to every request. Inside the network, the measurement covers only the proxy and the
    backend.
- **Only one edge runs at a time.** Each edge has its own Compose profile.
  - *Why:* otherwise the edges would compete for the same CPUs.
- **The bare tier uses the same container as the WADM tier.** Only the mounted config file
  changes, through the `${OPENRESTY_CONF}`, `${ENVOY_CONF}`, `${ENVOY_WASM_CONF}` and
  `${HTTPD_CONF}` overrides.
  - *Why:* service name, image, port and network path are then identical by construction, so
    `wadm − bare` measures WADM and nothing else.
  - Each baseline config is its WADM config with parts deleted and nothing added. The deletions
    are listed in `docs/EDGE_LEVELING.md`.
- **The host is a KVM virtual machine.** The harness and its thresholds were built on a VM with
  two vCPUs. On 2026-09-19 the same VM reports eight vCPUs (Intel i7-13650HX).
  - This changes more than speed. nginx runs `worker_processes auto`, and Envoy starts one worker
    per CPU by default, so the number of edge workers follows the vCPU count.
  - Every reported run must therefore state the hardware it ran on.

### 3. The workload

#### One iteration

Each k6 virtual user (VU) repeats the same iteration for the whole run: four `GET` requests, then
`sleep(1)`.

| # | Request | Detection it triggers | k6 metric |
|---|---|---|---|
| 1 | `GET /` | none (injection only) | `inject_get_duration` |
| 2 | `GET /api/login?password=<trigger keyword>` | html_comments | `detect_query_duration` |
| 3 | `GET /login.html?is_admin=1&probe=<header keyword>` with a tampered `admin_ui` cookie | form_fields, http_headers, cookies | `token_tamper_duration` |
| 4 | `GET /api/v1/debug` | decoy_paths | `token_decoy_duration` |

All four responses also go through injection.

- *Why these four requests:* together they run both the detection path and the injection path of
  all five honeytoken kinds on every iteration. Every kind is therefore sampled equally on every
  edge.
- *Why the triggers are in the query string:* it is the one part of a request all four edges
  inspect, and after levelling they parse and strip it the same way.
  - POST-body inspection exists on only two edges, so it is switched off
    (`post_body_inspection: false`) and the script sends only `GET`s.
- `/api/login` and `/api/v1/debug` have no backend route and return 404. The script treats 404
  as a success on those two requests, so k6's failure rate counts only real errors.

#### Load model and load levels

The k6 executor is `constant-vus`: a fixed number of VUs, and each sends its next request only
after the previous one has returned. This is a closed-loop load model.

`sleep(1)` adds one second of think time, so each VU completes at most one iteration per second.
While responses take only a small fraction of a second, the VU count sets the rate of incoming
requests, not the edge:

| VUs | Iterations per 30 s run, at most | Requests/s offered | Requests per run, at most |
|---|---|---|---|
| 1 | 30 | 4 | 120 |
| 10 | 300 | 40 | 1,200 |
| 100 | 3,000 | 400 | 12,000 |
| 500 | 15,000 | 2,000 | 60,000 |

- *Why this ladder:* 1, 10 and 100 VUs are fixed-rate levels well below capacity. That is where a
  latency comparison is meaningful.
- *Why 500 is different:* the edges saturate at 500 VUs. An edge that cannot keep up makes the
  VUs wait for slow responses, and the offered rate drops. The 500-VU level therefore measures
  capacity rather than per-request cost (see section 7.3).
- *Why the closed-loop model matters for 500-VU numbers:* a slow edge automatically receives less
  traffic. Latency at saturation is therefore lower than it would be for traffic that does not
  wait for responses, such as real attackers. This bias is known as coordinated omission. It does
  not affect 1–100 VUs, where the think time sets the rate.

#### Duration and start delay

- **30 s per level** (`K6_DURATION`).
  - Long enough for thousands of samples at 100 VUs.
  - Short enough that one edge's full ladder finishes in a few minutes.
- **5 s start delay** (the scenario's `startTime`, set by `K6_START_DELAY`). k6 waits 5 s before
  the first request.
  - *Why:* Envoy and the WASM edge take longer to start. Without the delay, the first requests
    were refused, which distorted the minimum and the mean.
  - The 30 s used for the ceiling above counts from the end of the delay.

### 4. The procedure for one edge

Every runner, WADM or baseline, follows the same sequence using the helpers in `wadm_timings.py`:

```
wait_for_quiet_host        wait until the 1-minute load average is below 2.0
ensure_compose_cleanup     remove every container from every profile, prune networks
start the stack            backend + this edge (WASM: rebuilt with --build)
warm-up                    100 VUs for 20 s, results thrown away
for VUs in 1, 10, 100, 500:
    remove the old load-tester container
    record timestamp T
    start a fresh load-tester with K6_VUS set, and wait for it to exit
    scrape the edge's log lines since T        -> internal µs samples  (WADM tier only)
    scrape the load-tester's log lines since T -> end-to-end ms summary
    compute statistics and the throughput check
ensure_compose_cleanup
write the result files
```

Why each step is there:

- **The edge keeps running across all levels; only the load generator restarts.**
  - The edge's warm state carries over from one level to the next: compiled JIT traces, upstream
    connection pools and loaded code. One warm-up therefore covers all four levels.
  - A fresh k6 container per level gives each level a clean run and its own summary.
- **Each level's data is cut out of the logs by time.** `docker compose logs --since T` returns
  only what was written after the level started, for the edge and for k6. Warm-up traffic and
  earlier levels never enter a level's sample.
  - `--since` has one-second resolution. The k6 scraper therefore takes the *last* summary line in
    the window, in case the window also catches the end of the previous run.
  - Any leakage would show up as surplus samples, and there is none. At 1 and 10 VUs, every
    edge's per-kind detection count equals k6's iteration count exactly (for example, 300 and
    300).
- **Cleanup enables every profile.**
  - `docker compose down` only removes containers whose profiles are enabled. Stopped containers
    from earlier runs could survive, still attached to a network that had since been deleted.
  - The next `up` then failed with "network not found".
  - Cleaning up with every profile enabled, then pruning networks, makes every run start from
    fresh containers.
- **The WASM edge is started with `--build`.**
  - Without it, Compose reuses the previously built `rust-builder` image. A change to the Rust
    filter would then be benchmarked with the *old* `filter.wasm`.
  - `Cargo.lock` is committed and the build uses `--locked`, so a rebuild cannot pull different
    crate versions.
- **The load levels run in ascending order.**
  - 500 VUs is the only level that saturates the host, and it is always an edge's last level.
    Its after-effects therefore fall on the *next* edge, which the quiet-host wait handles
    (section 5).
  - `K6_VUS_LIST` is run in the order given, so a custom list must also be ascending.
- **Order across edges.**
  - `run_baseline_benchmark.py --all` runs the bare tiers in the order Apache, Envoy+Lua,
    OpenResty, WASM, and then the origin.
  - *Why the origin goes last:* it puts the least load on the host. The edge tiers are therefore
    not the ones that start while the load average is still falling.
  - The WADM runners are started one at a time.
  - In the 2026-09-19 session the bare tiers started between 09:38 and 09:54 UTC. The WADM tiers
    started at 09:58 (OpenResty), 10:03 (Envoy+Lua), 10:11 (WASM) and 10:33 (Apache).

### 5. Waiting for a quiet host

#### What it does

`wait_for_quiet_host()` reads the 1-minute load average from `/proc/loadavg` every 10 s. It
returns once the value is below 2.0.
- If the host is still busy after 420 s, it prints a warning and continues anyway.
- It runs once at the start of every edge, before any container is touched, in both the WADM and
  the baseline runners.

#### Why it is needed

The 500-VU level saturates the host, and the machine stays busy for a while after the containers
stop. When edges ran back-to-back, the next edge's low-load levels were measured on a busy
machine.
- In one run, Envoy started while the load average was 5.5. Its VU=1 detection average came out
  at **1,636 µs**, against **2 µs** once the host was idle.
- That is an ordering effect. Whichever edge ran second was penalised, so the result described the
  run order, not the edge.

Why a busy host distorts even the microsecond timers:
- All four edges time with a **wall-clock** clock (section 7.1), not CPU time.
- The operating system can pause the edge's thread in the middle of a timed region to run another
  process. The time spent paused is then counted as the edge's work.
- The busier the host, the more often this happens. Every sample is then inflated by scheduling
  delay that has nothing to do with WADM.

The end-to-end numbers are affected the same way, and by queueing as well.

#### Why the 1-minute load average, and why these values

- **The metric.** The Linux load average counts tasks that are running, waiting for a CPU, or in
  uninterruptible sleep (typically disk I/O).
  - It therefore also registers disk activity, such as Docker writing out the hundreds of
    thousands of log lines a 500-VU level produces.
  - The 1-minute figure reacts fastest of the three averages Linux reports.
  - Exactly what keeps the host busy after a saturating level has not been profiled.
- **How fast it falls.** The kernel updates the 1-minute average every 5 s. It is an
  exponentially weighted moving average with a 60 s time constant.
  - Once the machine is idle, falling from a load L to 2.0 takes about `60 × ln(L / 2)` seconds.
    From 5.5, that is about 61 s.
- **Why the wait errs on the side of caution.** The average lags behind reality, so the wait
  continues for about a minute after the real work has stopped. It can wait too long, but never
  too little.
- **Poll interval (10 s) and cap (420 s).**
  - Polling every 10 s adds at most 10 s to the wait.
  - Any realistic post-run load decays well within 420 s; only a load above about 2,000 would take
    longer.
  - Hitting the cap therefore means something *else* is keeping the host busy. The cap stops such
    a host from blocking the run forever, and the warning flags it.
- **The threshold (2.0).**
  - On the two-vCPU VM the harness was built on, a load of 2.0 means one runnable task per CPU on
    average.
  - On eight vCPUs, the same 2.0 is a stricter test: a quarter of capacity. The guard is still
    valid, just more cautious.
  - Because 2.0 is an absolute number, it should be revisited if the hardware changes again.
- **Why once per edge and not before every level.** Within one edge the levels run upward, so no
  level follows a saturated one (section 4). A saturated level is followed by more measurement
  only at the boundary between two edges.

#### What it does not protect against

The throughput check (section 7.3) is a second, independent guard. It catches the cases below:
- **Load outside the VM.** The load average is measured inside the guest, so it cannot see other
  activity on the physical machine.
- **Load that starts after the wait.** Two examples:
  - the host becoming busy during a run;
  - the WASM `--build`, which runs after the wait. It is a cache hit when the filter has not
    changed. When it has changed, the compile finishes before `up` returns and is followed by the
    20 s warm-up before the first recorded level.

A limitation of recording, not of protection: whether the wait timed out is only printed to the
console. It is not saved in the result files.

### 6. Warming up

#### What it does

After the stack starts, and before the first recorded level, each runner runs k6 once at **100 VUs
for 20 s** (plus the 5 s start delay), using the unchanged `test.js`.
- Nothing from this run is scraped or saved.
- The `--since` windows of the recorded levels begin after it.
- At 100 VUs, the warm-up is about 2,000 iterations, or about 8,000 requests.

#### Why it is needed

The first requests a freshly started edge handles are slower than later ones. The reasons have
nothing to do with WADM's logic:

| Runtime | What is cold at startup |
|---|---|
| OpenResty, Envoy+Lua (LuaJIT) | LuaJIT interprets bytecode at first. It compiles a loop or function to machine code only once it becomes hot (the default `hotloop` threshold is 56 iterations) |
| Envoy+WASM (V8, set in `envoy-wasm.yaml`) | V8 compiles WebAssembly in tiers: a fast baseline compile first, then an optimising recompile of the functions that turn out to be hot |
| Apache+mod_lua (PUC Lua, `LuaScope thread`) | Each worker thread builds its own Lua state, and loads the scripts and `config.json`, the first time it runs WADM code |
| All edges | Several things fill up during the first traffic: connection pools to the backend, the OS page cache for binaries and libraries, and the memory allocator's arenas |

Without a warm-up, all of this would land on the VU=1 level, which is always the first traffic an
edge sees. VU=500, run last, would be fully warm.
- Latency would then seem to *improve* with load. That trend would come from the run order, not
  from concurrency.
- With a warm-up, all four levels start from the same warm state.

#### Why these settings

- **Same script.** The warm-up sends the same four requests. Every detection and injection code
  path that is timed later has therefore already run about 2,000 times, far beyond LuaJIT's hot
  threshold.
- **100 VUs.**
  - That is enough traffic to warm every code path and to fill the connection pools with
    concurrent connections.
  - Every edge handles 100 VUs without saturating (at least 96% of the ceiling in the 2026-09-19
    session). The warm-up therefore does not leave an overloaded host behind.
- **Once per edge.** The edge container persists across levels (section 4), and its warm state
  persists with it.
- **Also for the bare and origin tiers.**
  - These tiers have no WADM code to compile, but their connection pools and caches still need
    to warm up.
  - If only the WADM tier were warmed, the bare tier would carry cold-start cost and the WADM
    overhead would look smaller than it is.
  - Every tier therefore follows the same procedure.

#### Caveats

- **Apache cannot be kept fully warm.**
  - Its event MPM shrinks the worker pool whenever load drops, and spawns new processes when load
    rises. Each new thread builds fresh Lua states.
  - Threads created during the warm-up may be gone by the 500-VU level, which then pays the
    cold-start cost again part-way through the run.
  - Pre-spawning the whole pool in both Apache configs was proposed on 2026-09-18. It has not been
    done yet.
- **Client connections are new at every level.** Each level starts a fresh k6 container, so the
  client opens new connections. k6's `http_req_duration` excludes connection setup (section 7.2),
  so this does not affect the end-to-end numbers.
- **The size of the warm-up effect has never been measured on its own.** No run was made with and
  without a warm-up under otherwise identical conditions.
  - The 2026-07-08 entry blamed cold start for much of a detection median that fell as load rose
    (Envoy+Lua: 234 µs at VU=1, 47 µs at VU=500).
  - Later evidence changes that reading. Once the log write was moved out of the detection timers
    (see `docs/EDGE_LEVELING.md`, "No log I/O inside a timer"), the downward slope largely
    disappeared. For example, OpenResty's 11→5 µs became 2/2/2/1 µs.
  - That slope was therefore mostly the log write's cost shrinking under concurrency, not cold
    start.
  - The warm-up is still justified by the mechanisms above, but how much it changes the numbers
    is unknown. One run with it and one without would answer that.

### 7. What data is collected

#### 7.1 Internal timers (µs, WADM tier only)

**How a sample is produced.** Each edge reads a clock before and after each honeytoken operation.
It writes the difference, in whole microseconds, as one log line.

| Edge | Clock | Log call |
|---|---|---|
| OpenResty | `gettimeofday` via LuaJIT FFI | `ngx.log(ngx.WARN, …)` → error log → the container's stderr |
| Envoy+Lua | `gettimeofday` via LuaJIT FFI | `logWarn` → Envoy's log |
| Apache | `r:clock()` | `r:warn` → `ErrorLog /proc/self/fd/2` |
| Envoy+WASM | `get_current_time()` (the host's wall clock) | `warn!` → Envoy's log |

- All four clocks are wall-clock microsecond sources, so all four edges measure the same quantity.
  - Envoy+Lua once used `os.clock()`, which measures process CPU time, a different quantity. It
    was replaced for that reason.
- Every one of these log lines ends up in `docker compose logs`, which is where the runners read
  them.

**Line formats.**
- html_comments: `[<Edge> ]Detection execution time (us): N` and
  `[<Edge> ]Injection execution time (us): N`.
- The other four kinds: `WADM TOKEN <kind> detect (us): N` and `WADM TOKEN <kind> inject (us): N`.
- Each runner has a regular expression for its own edge's html_comments lines. One shared
  expression reads every `WADM TOKEN` line.
- The lowercase `detect` and `inject` are deliberate. OpenResty's html_comments pattern has no
  prefix, so any line containing "Detection execution time" would match it. If a per-kind line
  used that wording, it would be counted as an html_comments sample.

**What the timers include and exclude.** `docs/EDGE_LEVELING.md` sets this out in full. In
summary:
- **Before any timer starts:** per-request setup, done once: reading the client IP, the path and
  the cookie, parsing the query, and choosing which tokens apply.
- **Detection timer:** the scan, the strip, and recording the attacker's IP in memory. It is
  logged only when something matched, so every edge samples the same population of hits.
- **Injection timer:** the header write, or finding the anchor and splicing into the fully
  buffered body.
- **Never inside a timer:** formatting the alert and writing the log. When the write was inside,
  detection times reflected each runtime's logging speed rather than its detection.

The timers therefore measure the honeytoken logic itself, and deliberately not WADM's total cost.
Section 8 shows how large the untimed part is.

**Expected sample counts per level.** These also serve as a completeness check:

| Kind | Detect samples | Inject samples |
|---|---|---|
| html_comments, http_headers, cookies, decoy_paths | = iterations | = 4 × iterations |
| form_fields | = iterations | = iterations (only `/login.html` has a `</form>`) |

In the 2026-09-19 session:
- at 1 and 10 VUs, the counts match k6's iteration count exactly on every edge;
- at 100 VUs, they match to within one sample;
- at 500 VUs, they are exact on three edges and within 0.2% on Apache. There, the edge timed
  requests from a few iterations that k6 did not count as complete. Apache is also the only edge
  with any failed requests at that level (0.15%).

**Statistics per level, phase and kind:**
- count, minimum, mean, 90th percentile (nearest-rank method) and maximum, saved in
  `internal_<edge>_profile.json`;
- every raw sample, saved in `internal_<edge>_raw.json`.

*Why both:* the summary is small and readable, but quartiles cannot be recovered from it. The raw
samples let every box plot show the real distribution.

#### 7.2 End-to-end latency (ms, every tier)

**What k6 measures.** For each request, `timings.duration` covers sending the request, waiting for
the first byte, and receiving the response. It excludes DNS lookup and TCP connection setup,
which k6 reports separately.

Each value is recorded twice:
- in `http_req_duration`, which pools all four requests;
- in the trend for its request: `inject_get_duration`, `detect_query_duration`,
  `token_tamper_duration` or `token_decoy_duration`.

*Why both:* the pooled figure is the basis of the overhead calculation (section 8). The
per-request figures show which kind of request WADM slows down.

**Statistics.** For each trend: count, min, p5, p25, median, p75, p90, p95, max and mean.
- *Why p5, p25, p75 and p95 are requested explicitly:* k6's default summary has no quartiles. With
  them, a real box (Q1, median, Q3) can be drawn from the summary alone.
- *Why the raw samples are not exported:* k6's per-sample output (`--out json`) would be hundreds
  of megabytes at 500 VUs.
- k6 computes percentiles by linear interpolation between neighbouring samples. The internal
  timers use the nearest-rank method instead. At these sample sizes (at least 120 per level) the
  difference is negligible, but it should be stated.

**Also recorded:** completed iterations, total requests and the failed-request rate.

**How the numbers leave the container.** `handleSummary` in `test.js` prints one line,
`WADM K6 SUMMARY {…json…}`. The runner reads it from `docker compose logs load-tester`.
- *Why this route:* the edges' timings already travel this way, so the k6 container needs no
  writable folder shared with the repository.
- *Why it must be one line:* `docker compose logs` puts a `load-tester-1  | ` prefix before every
  line. Matching from the fixed `WADM K6 SUMMARY` marker to the end of the line makes that prefix
  harmless.
- The payload is about 1 KB, far below the 16 KB at which the log driver splits lines.

#### 7.3 Throughput check

For every level the runner records:

```
expected_iterations = VUs × duration in seconds       (the ceiling that sleep(1) imposes)
throughput_ratio    = iterations k6 completed / expected_iterations
expected_reachable  = VUs ≤ 100
```

**Why the check exists.** Latency alone cannot reveal a contaminated run. In one run, an edge
completed only **47%** of the achievable iterations, yet its per-operation latencies still looked
plausible. The throughput ratio exposes that kind of run.

**Why the threshold is 0.90 and not 1.0.**
- Each iteration takes one second *plus* four round-trips and k6's own overhead. Even on a
  healthy host, the ratio falls slightly short of 1. In the 2026-09-19 session it was 0.96–1.00
  at 1–100 VUs.
- A ratio below 0.90 at a reachable level means the host was busy. The runner then prints
  `WARNING: host was busy, treat this level as invalid`.

**At 500 VUs,** `expected_reachable` is false.
- The edge itself is the bottleneck, so the ratio measures capacity. It is compared between edges,
  not against 1.0.
- In the 2026-09-19 session it ranged from 0.30 (Apache with WADM) to 0.91 (the origin alone).

The runners count iterations from k6, not from the timing lines they scrape. k6's count is the
direct measure, and the only one the bare and origin tiers can produce.

#### 7.4 Metadata and errors

Each result file records:
- when the run was made and by which script;
- the target, trigger keyword, duration, start delay and VU list;
- the tier and the edge config file.

Each level also records the Compose exit code and any error output from starting k6 or reading
the logs. A failed level therefore shows as a failure, not as an empty entry that looks fine.

**Not recorded automatically:**
- the load average at the start of the run;
- whether the quiet-host wait timed out;
- the vCPU count;
- the image versions. `openresty:latest`, `grafana/k6:latest` and `envoy:v1.30-latest` are
  moving tags. On 2026-09-19 the local `grafana/k6:latest` image is k6 v2.0.0.

These must be written down by hand for any run reported in the thesis.

#### 7.5 Result files

| File | Plane | Contents |
|---|---|---|
| `internal_<edge>_profile.json` | µs | Summary statistics per level, phase and kind, plus the throughput check |
| `internal_<edge>_raw.json` | µs | Every sample, per level, phase and kind |
| `e2e_<edge>_wadm.json` | ms | k6 summary per level, WADM tier (written by the internal runner) |
| `e2e_<edge>_bare.json` | ms | k6 summary per level, bare tier |
| `e2e_origin_bare.json` | ms | k6 summary per level, origin only |

`<edge>` is one of `openresty`, `envoy_lua`, `wasm`, `apache_lua`.

### 8. From data to figures

- **Internal-timer box plots** (`plot_edge_comparison.py`, `plot_token_comparison.py`):
  - They are drawn from the raw samples. The box spans Q1–Q3 (nearest-rank), the line is the
    median, and the whiskers reach 1.5 × IQR. Outliers are not drawn.
  - If an edge's raw file is missing, the plot falls back to an approximation built from the
    summary statistics. That box is drawn **hatched**, so it cannot be mistaken for real
    quartiles.
  - The y-axis is symlog: linear below 1 µs, logarithmic above. *Why:* 8–13% of samples on three
    edges are exactly 0 µs, and a log axis cannot show zero.
- **End-to-end box plots** (`plot_baseline_comparison.py`):
  - The box spans p25–p75 and the whiskers p5–p95, all from k6's percentiles.
  - *Why the whisker is not the minimum:* one unusually fast request among tens of thousands would
    stretch the whisker to the floor and hide the differences the figure exists to show.
- **Overhead breakdown.** How much of WADM's end-to-end cost the internal timers account for:

  ```
  measured_ms  = 4 × (median http_req_duration, WADM − median, bare)          per iteration
  accounted_ms = Σ over kinds and phases (median µs × number of operations) ÷ iterations ÷ 1000
  ```

  - Both sides use medians, so they are comparable.
  - The gap between them is real cost that the timers leave out by design: setup, buffering the
    response body, the content-type check and writing the alert logs.
  - Four times a difference of medians only approximates the cost per iteration. It is not an
    exact sum of the per-request differences.
  - A negative measured value means the bare tier ran slower than the WADM tier. That is host
    noise, and the level must be re-run.
- **Saturated levels are marked** in the end-to-end figures wherever any edge or tier falls below
  0.90 of its ceiling. The figures therefore never present queueing delay as the cost of WADM.

### 9. When a run counts as valid

A set of results is reportable only if **both** of these hold:

1. **Throughput:** `throughput_ratio ≥ 0.90` at 1, 10 and 100 VUs, in every tier.
2. **Ordering:** `median(origin) ≤ median(bare) ≤ median(wadm)` for every edge at every level.
   - *Why:* a proxy cannot be faster than the origin it forwards to, and adding WADM cannot make
     it faster.
   - A violation means the numbers were taken under different host conditions. This happened on
     2026-09-17: a re-measured OpenResty appeared faster than an origin measured in an earlier
     session.

The 2026-09-19 session meets both. Its lowest throughput ratio at 1–100 VUs is 0.961, and the
ordering holds at all four levels for all four edges.

Two further checks support the numbers:

- **The duplicate Envoy baselines.** The bare tiers of Envoy+Lua and Envoy+WASM are the same
  stack: Envoy with only its router filter. The difference between them is therefore a direct
  measure of run-to-run noise.
  - In the 2026-09-19 session their medians differ by 3–19% at 1–100 VUs, and by a factor of 1.8
    at 500 VUs.
  - Differences between edges smaller than that should not be claimed from a single run.
- **`parity_check.py` runs before benchmarking** after any change to an edge.
  - It sends the same 13 requests to all four edges. It then compares three things against
    OpenResty: the responses, the alert lines, and the requests that reached the backend.
  - This confirms that the edges do *the same work* before their speed is compared.
  - It uses the same Compose project as the runners, so it must never run during a benchmark.

### 10. Known limitations

- **The load generator runs on the same machine as the system under test.** At high VU counts,
  k6 competes with the edge and the backend for CPU. This affects all tiers equally, but it
  lowers the load at which the edges saturate.
- **Virtualisation.** The guest cannot see what else is running on the physical machine, so the
  quiet-host guard cannot either.
- **One run per tier.** Run-to-run noise (section 9, and the 2026-09-17 entry) is larger than some
  of the differences between edges. Repeating each tier at least three times would make
  cross-edge claims under about 1 ms defensible, reporting either the median of the runs or their
  spread.
- **The closed-loop model at saturation.** Latency at 500 VUs is lower than it would be for
  traffic that does not wait for responses (section 3). Throughput is the more reliable metric
  there.
- **The timers use wall-clock time at 1 µs resolution.**
  - They include any time the scheduler pauses the thread.
  - Many operations finish within one tick.
  - `gettimeofday` is not monotonic, so a clock adjustment during a timed region would corrupt
    that sample. The WASM filter turns a negative result into 0. Over microsecond intervals this
    is very unlikely.
- **The warm-up's effect has not been measured on its own** (section 6).
- **The hardware has changed since the thresholds were set:** two vCPUs then, eight now. The vCPU
  count, the image versions and the load at the start of a run are not recorded automatically
  (section 7.4).

---

## 2026-09-21 — How the SQL-injection trap is benchmarked, and why it differs from the honeytoken tests

This entry describes the benchmark procedure for the `sql_injection` trap: what it measures, why
it could not be measured the same way as the five honeytoken kinds, the 2×2 design that replaced
the first attempt, and what the measurement showed. It extends the 2026-09-19 harness entry, which
describes the machinery this reuses.

The code it describes:
- the trap itself, in `nginx/nginx.conf`, `envoy_scripts/injection.lua`, `apache_scripts/login.lua`
  and `wasm-filter/src/lib.rs`;
- `benchmarks/test.js`, which drives the four probes;
- `benchmarks/wadm_timings.py`, which defines the kind taxonomy and scrapes the timings;
- `benchmarks/plot_sqli_comparison.py`, which renders the result.

### 1. What the trap is, and what it is not

There is **no authentication anywhere in this system**. The origin is a static nginx container
serving five HTML files; it has no user store, no password check, and no `/api/login` route at all
— a request to that path 404s. The trap runs entirely in the edge proxy and fabricates both of its
responses:

| Request body | Response the attacker sees |
|---|---|
| contains a configured signature | 500 + a fake MySQL syntax error with the payload reflected back |
| contains none | 401 + a canned "Sign in failed" page |

So the two outcomes are not "a failed login" and "a successful attack". They are "the signature
scan found something" and "the signature scan found nothing". A non-matching request costs exactly
what it takes to check the body against the list, and nothing else. This matters because the
obvious reading — that a clean login does more work, or touches a slower path — is wrong: there is
no such path.

### 2. Why it cannot be measured like a honeytoken kind

The five honeytoken kinds share one shape: WADM **plants** something in a response the origin
produced, and later **detects** an attacker interacting with what was planted. Both halves are
timed, giving every kind a `detect` and an `inject` number.

The trap does not fit that shape in four separate ways.

| | Five honeytoken kinds | `sql_injection` |
|---|---|---|
| Plants a bait | yes — comment, header, cookie, link, hidden input | **no** |
| Injection phase | yes, timed | **none — nothing to time** |
| Triggered by | a `GET` carrying a trigger keyword or a tampered value | a `POST` to one configured path |
| Reaches the origin | yes, the response is mutated on the way back | **no** — the edge answers from the request phase |
| Produces the response | origin | **the edge fabricates it** |

The consequence for the benchmark: `sql_injection` is a **detection-only** kind. It appears in the
detection figures and the detection pool, and is absent from the injection figures entirely —
omitted rather than drawn as an empty panel, which would read as "measured, and it was zero".

One further asymmetry is deliberate and documented as a residual. The honeytoken kinds time the
in-memory attacker-IP record **inside** their `detect` region. The trap leaves it outside, because
only the matching outcomes perform that write; including it would surface as a difference between
outcomes that has nothing to do with the scan. All four edges do this identically, so cross-edge
comparability is unaffected.

### 3. The first design was confounded

The original design used two probes: a signature hit and a non-matching body. The reasoning was
that `sqli_match` is a linear scan returning on first match, so a hit should stop early while a
miss walks the whole list:

- hit — `username=admin%27`, matching `admin'`, signature #11 of 22, in the first watch field:
  the scan stops after **11** comparisons;
- miss — `username=alice&password=secret`: all 22 signatures against `username`, then all 22
  against `password`, **44** comparisons.

The expectation was that the miss would be measurably more expensive, and that this would
demonstrate the cost of linear-scan matching.

The measurement did not show that. It showed the *hit* costing more. The reason was a confound I
had built into the probes: the hit payload was percent-encoded (`%27`) and the miss payload was
not. `url_decode` runs over every body pair inside `sqli_match`, and again inside `sqli_normalize`
on the matched value, so the encoded payload paid a substitution path the plain one never entered.
The comparison measured encoding and attributed it to scan depth.

### 4. The 2×2 that replaced it

Two factors are crossed so that each can be attributed separately. The `%27` form of each payload
decodes to exactly the plain form, so the two arms of one outcome scan identical bytes and differ
**only** in decoding work.

| Kind name | Outcome | Body | Comparisons | Decode |
|---|---|---|---|---|
| `sql_injection` | hit | `username=admin'&password=x` | 11 | no |
| `sql_injection_encoded` | hit | `username=admin%27&password=x` | 11 | yes |
| `sql_injection_miss` | no match | `username=alice&password=secret` | 44 | no |
| `sql_injection_miss_encoded` | no match | `username=alice%27&password=secret` | 44 | yes |

Each k6 iteration sends all four as `POST`s, so an iteration now carries eight requests: the four
original `GET`s plus these.

The edge labels each sample itself. It knows the outcome from the match result, and sets the
`_encoded` suffix by checking the raw body for `%` **after the timer has closed**, so the
classification never enters the measurement. That check is not benchmark scaffolding —
percent-encoded input is an evasion technique the honeypot has independent reason to record.

Only `sql_injection` is pooled into the detection figures, as the trap's one representative sample
per iteration; pooling all four would give the trap four times the weight of any honeytoken kind.
The other three are controls, plotted only in the trap's own figure.

The timed region is `sqli_match` alone — body parse, decode, normalise, scan. Page rendering, the
alert write, the arm classification and the IP record all sit outside it, identically on all four
edges.

### 5. What the measurement showed

Full four-edge run, medians of the raw samples in microseconds. "Encoding" is the mean of the two
plain→encoded deltas; "outcome" is the mean of the two no-match→hit deltas. Levels marked invalid
failed the achieved-iteration check and are shown only for completeness.

| Edge | VUs | n | hit | hit+enc | no match | no match+enc | **encoding** | **outcome** |
|---|---|---|---|---|---|---|---|---|
| OpenResty | 10 | 300 | 10 | 10 | 8 | 9 | **+0.5** | +1.5 |
| OpenResty | 100 | 2878 | 8 | 8 | 7 | 8 | **+0.5** | +0.5 |
| Envoy+Lua | 10 | 300 | 8 | 8 | 7 | 8 | **+0.5** | +0.5 |
| Envoy+Lua | 100 | 2806 | 7 | 8 | 7 | 8 | **+1.0** | 0.0 |
| Envoy+WASM | 10 | 300 | 3 | 3 | 3 | 3 | **0.0** | 0.0 |
| Envoy+WASM | 100 | 2699 | 3 | 3 | 3 | 3 | **0.0** | 0.0 |
| Apache | 1 | 30 | 17.0 | 17.5 | 18.5 | 21.5 | **+1.8** | **−2.8** |
| Apache | 10 | 294 | 15 | 18 | 18 | 20 | **+2.5** | **−2.5** |

Three results:

**Decoding costs 0.5–2.5 µs on every interpreted edge, and nothing on WASM.** The direction is
positive in every row. `url_decode`'s substitution path runs over each body pair and again during
normalisation, so an encoded payload pays it twice; in Rust the same work is a `String` allocation
cheap enough to sit under the 1 µs timer resolution.

**Scan depth is measurable only where the matching loop is genuinely interpreted.** On OpenResty,
Envoy+Lua and WASM, 33 extra comparisons move the median by 0.0–0.5 µs — nothing. On Apache it
costs ~2.5 µs, consistently, at both valid load levels, and in the direction the original
hypothesis predicted. The cause is the runtime, not the algorithm: Apache's `mod_lua` links
`liblua.so.5`, standard PUC Lua, while OpenResty and Envoy both run LuaJIT and the WASM filter is
compiled Rust. Verified with `ldd` on `mod_lua.so` inside the container.

The reason the effect is small even on Apache is that most configured signatures are *longer* than
a realistic form value — `information_schema` (18 characters), `union all select` (16),
`waitfor delay` (13) against `alice` (5) — so a substring search rejects them on length before
comparing a single character. The scan never really walks the list; it mostly checks lengths.

**Envoy+WASM is flat on both factors.** All four arms have a median of 3 µs, with 221 of 300
samples landing on exactly that value. The whole scan completes below the timer's resolution, so
the 2×2 has nothing to resolve. That is itself the result: on a compiled filter, neither what the
attacker sends nor how they encode it changes the detection cost.

The practical readings for the thesis:

1. **Growing the signature list is close to free on a JIT-compiled or native edge, and cheap but
   non-zero on an interpreted one.** Detection cost is dominated by input normalisation rather
   than by how many patterns are configured. A researcher can add signatures without a latency
   budget on three of the four edges.
2. **The attacker controls the cost more than the defender does.** Percent-encoding a payload — a
   standard WAF-evasion technique — makes the honeypot work measurably harder, and on the
   LuaJIT edges it costs more than the entire difference between being attacked and not.
3. **A single-edge measurement would have been wrong.** Measured on OpenResty alone, the honest
   conclusion is "scan depth is free". Measured on Apache alone, it is "scan depth costs 2.5 µs".
   Both are true of their runtime and neither generalises, which is the argument for running the
   2×2 across all four edges rather than characterising the algorithm once.

### 5a. Validity of these levels

Apache failed the achieved-iteration check at 100 VUs (56% of the offered rate) and collapsed at
500 (13%). Before this change it managed 96% at 100 VUs. The cause is the iteration growing from
four requests to eight: the trap is POST-gated, so all four new requests are terminal work Apache
performs itself, and `mod_lua` under prefork is the least able of the four edges to absorb it.
Apache's 100- and 500-VU rows are therefore excluded above; its scan-depth result rests on the
1- and 10-VU levels, which passed at 100% and 98%.

The other three edges passed at 1, 10 and 100 VUs and saturate at 500, as they did before.

### 5b. A within-iteration position gradient on the end-to-end plane

Measured while checking why Apache's first SQLi bar sat at zero. In the **bare** tier — no WADM
code running at all — median latency falls monotonically across the eight requests of an
iteration, on every edge (10 VUs, medians in ms):

| Edge | req 1 | req 2 | req 3 | req 4 | req 5 | req 6 | req 7 | req 8 | drop |
|---|---|---|---|---|---|---|---|---|---|
| OpenResty | 1.300 | 1.184 | 1.305 | 1.150 | 1.172 | 1.094 | 1.050 | 0.957 | −26% |
| Envoy+Lua | 1.629 | 1.353 | 1.241 | 1.249 | 1.209 | 1.163 | 1.093 | 1.098 | −33% |
| Apache | 1.646 | 1.503 | 1.434 | 1.405 | 1.471 | 1.370 | 1.267 | 1.133 | −31% |
| Envoy+WASM | 1.744 | 1.394 | 1.452 | 1.409 | 1.408 | 1.318 | 1.223 | 1.144 | −34% |

The first request after `sleep(1)` pays a wake-up cost — connection revalidation, socket and CPU
cache state — that later requests in the same iteration do not. This generalises the 2026-09-18
observation about Apache shedding keep-alives at 500 VUs: it is neither Apache-specific nor
confined to the saturated level.

Three consequences:

1. **Absolute per-request end-to-end figures are not comparable across positions.** The eight k6
   Trends are not eight measurements of the same thing; they are eight measurements taken at
   eight different points on this gradient.
2. **The `wadm − bare` subtraction cancels it**, because both tiers run the same script in the
   same order. This is why every end-to-end figure in the repo plots a delta rather than a
   latency, and it is a stronger reason than the one originally recorded (different backend paths
   returning different-sized bodies).
3. **The microsecond plane is immune.** Those timers wrap a region inside the edge and never
   include the network, so the per-kind and SQLi 2×2 results are unaffected.

One visible artefact: Apache's `sqli_hit_duration` delta is −0.002 ms where its other three arms
are −0.27 to −0.38 ms. That arm is the first request after the four GETs, and Apache's bare tier
pays the wake-up cost there too (1.471 ms against 1.370/1.267/1.133 for the later POSTs), so the
saving and the gradient cancel. It is a positional artefact, not a property of the trap.

### 6. One caveat that does not affect the above

On the **end-to-end** (millisecond) plane the four `POST`s are *not* comparable across edges.
OpenResty, Envoy+Lua and Apache answer the trap from the request phase, so WADM saves them the
origin round-trip the no-WADM baseline pays — these requests are *faster* with WADM than without.
Envoy+WASM cannot do this (proxy-wasm rejects `send_http_response` from the body callback, and
pausing the stream hangs the request), so it forwards to the origin and rewrites the response,
paying a hop the other three avoid.

This is why the overhead decomposition sums per-request-type deltas rather than scaling the pooled
median by the request count: scaling would let the POSTs' saving cancel the overhead the `GET`s
add, understating — and potentially inverting — the measured bar.

The microsecond plane above is unaffected, because every timed region closes long before the
round-trip.

---

## 2026-10-07 — Why Apache crashes at 100 iterations/s, and how a run is judged valid

### 1. The crash is driven by request volume, not by rate

Apache+mod_lua is the only edge that crashes under WADM. Its workers segfault (`AH00052: child pid N
exit signal Segmentation fault (11)`), Apache respawns them, and the request in flight fails.
The investigation is in `docs/BENCHMARK_METHODOLOGY.md` §4b; the findings are:

- **Component.** The response body filter, `inject.lua` (`LuaOutputFilter`). Under identical load
  (900 req/s, 20 s), full WADM produced 10 segfaults; WADM with only the body filter removed — detection
  and the SQLi trap still active — produced 0.
- **Mechanism.** With `LuaScope thread` and `LuaCodeCache forever`, one Lua state is reused for every
  request a worker thread serves. The filter runs as a coroutine over Apache's bucket brigade and
  buffers the whole body, and something in that reused state accumulates per request until the
  worker segfaults. The exact allocation was not identified (that would need a core dump); the
  component, the driver and the rate are established.
- **Driver.** Cumulative requests served, not arrival rate or concurrency. Nothing crashes below
  roughly 4,000 requests. At a fixed 90 req/s, 909 requests were clean and 8,100 crashed four times.
  A threshold followed by a steady rate per request is the signature of per-request accumulation,
  not of a race between concurrent requests.

"Crashes at 100/s" is therefore a consequence of run length. One iteration is 9 requests (one
unrecorded priming `GET /`, four GET probes, four SQLi POSTs), so each instrumented run serves:

| Offered rate | Requests per run | Segfaults per run |
|---|---|---|
| 1 iteration/s (≈9 req/s, 90 s) | ~835 | 0 |
| 10 iterations/s (≈90 req/s, 45 s) | ~4,070 | 0–2 |
| 100 iterations/s (≈900 req/s, 30 s) | ~27,000 | 13–16 |

Only the 100/s runs serve far past the ~4,000-request onset.

**Remediation.** `LuaScope conn` (a fresh Lua state per connection) eliminates the crash, but at
roughly 7–9× the CPU per request; it cannot sustain 100/s, and it makes Apache forget recorded
attacker IPs when a connection closes, breaking parity with OpenResty's shared dictionary.
`LuaScope request` also fixes it but placed only two thirds of the offered load. The shipped
`LuaScope thread` configuration is kept for the main results, with the crash characterised rather
than configured away; the remediation is reported separately (`apache_remediation.png`,
methodology §4c).

### 2. How a run is judged valid

Every run (one tier × one rate × one replicate) passes through `assess_run()` in
`benchmarks/wadm_timings.py`. A run is **rejected** if any of these hold:

| Gate | Threshold | What it catches |
|---|---|---|
| HTTP failures | > 0.5% of requests | broken responses |
| Dropped iterations | > 1% of offered | k6 could not start iterations on time |
| Achieved rate | < 95% of offered | the edge could not keep up |
| Origin CPU per request | > 1,000 µs | host contention. The origin container is identical in every run, so a spike there cannot be the edge — a negative control |
| Host freeze | any | the VM was paused inside the measurement window |
| Timer tail | > 1% of internal timer samples above 1,000 µs, **and** ≥ 3 such samples | a run whose in-edge timings are contaminated by stalls |

A crash on its own is a **warning**, not a rejection. A crash takes the in-flight request with it,
which the failure-rate and achieved-rate gates already measure; the original "any crash rejects"
rule threw away whole rate levels for ~0.03% damage, and discarded exactly the runs where "this
edge crashes under load" is the finding (methodology §1.6b). The "≥ 3 samples" condition exists
because at 1/s a kind collects ~90 samples, so a single slow sample would otherwise be 1.1% and
reject an otherwise clean run. Rejected runs stay in the result JSON with their reasons and are
excluded from every statistic; `analyze_paired.py --reassess` re-applies changed rules to stored
data without re-running.

### 3. Why Apache has no timer data at 100/s

The suite runs three tiers: `bare` (no WADM), `silent` (WADM as deployed, no timing-log lines) and
`wadm` (instrumented). CPU and latency figures use silent − bare; the in-edge timers come only from
`wadm`.

At 100/s, 4 of Apache's 5 silent runs passed, so its CPU and latency figures exist. **All 5 of its
instrumented runs failed the timer-tail gate**: 1.1–1.7% of samples above 1 ms (33–51 samples per
run). Two of them also failed the HTTP-failure gate (0.75% and 0.51%).

The gate's message attributes this to "host preemption", but the evidence says otherwise. Samples
above 1 ms per instrumented run at 100/s, same host and protocol:

| Edge | Samples > 1 ms per run |
|---|---|
| OpenResty | 0–1 |
| WASM (Envoy) | 1–4 |
| Envoy+Lua | 1–3 |
| **Apache+Lua** | **33–51** |

At 1/s and 10/s Apache's count is 0–1 per run, like the others. Preemption by the host would
affect every edge alike; a tail ten to fifty times larger on one edge, appearing only at the rate
where that edge crashes 13–16 times per run, is internal to Apache. The likely cause is
crash-and-respawn churn: worker processes dying and being replaced on two pinned cores while the
surviving workers' timed regions wait.

**How to state it in the write-up:** the rejection is correct, but the accurate wording is that
*Apache's in-edge timings at 100/s are contaminated by its own crash recovery*, not by host noise.
Its CPU figure at that rate should likewise be read as "WADM plus crash recovery", not the cost of
the deception logic alone (methodology §4b, "How to read Apache's numbers"). Its timings at 1/s and
10/s are unaffected: a crashed child contributes no samples rather than wrong ones.

### 4. Presentation options (open)

1. Keep Apache at 100/s as "no valid instrumented run" in the figures and explain the cause in
   the text (current state).
2. Make the timer-tail gate's message say "edge-internal stalls" instead of "host preemption"
   when only one edge shows the tail. Label only; the same runs are rejected.
3. Plot the rejected Apache runs hollow and marked as rejected. The 5%-trimmed mean already drops
   the slow tail, so the value would still be meaningful, but it would be the only plotted figure
   drawn from rejected runs.

Recommended: 1 with 2.

## 2026-10-13 — The origin tier: what the host proxy itself adds, and why the paired dataset is re-measured

RPDL is the thesis's name for what the code and the earlier entries call WADM, and a *host proxy* is
what they call an *edge*. This entry uses the thesis's terms so passages can be lifted directly.

### 1. The question the existing tiers could not answer

Every paired latency figure so far compares two configurations of the same host proxy: `bare` (no
RPDL), `silent` (RPDL as deployed) and `wadm` (RPDL plus the benchmark's timing log lines). All three
have the proxy in the request path, so they answer "what does RPDL cost on top of a reverse proxy?"
and say nothing about what the reverse proxy costs in the first place. The answer matters for how the
RPDL result is read. For example, if the proxy alone added 0.4 ms and RPDL another 0.15 ms
(illustrative values, not measurements), RPDL would be about a quarter of what deploying it costs
in front of an origin that previously had no proxy, and the proxy would be the rest.

A no-proxy tier existed once (`run_baseline_benchmark.py --edge origin`), but it was measured in its
own session, unpaired, and was retired with the old harness (methodology §1.3; files in
`benchmarks/results/archive/2026-10-05-legacy-unused/`). Read naively, those archived files put
bare − origin at roughly 0.3–0.7 ms at 1 and 10 VUs. That is a difference of two runs taken in
different sessions, and is not reportable.

### 2. How it is measured

The reportable `all` mix now runs a fourth tier, **`origin`**, in every replicate of every host
proxy's run:

| | `origin` | `bare` / `silent` / `wadm` |
|---|---|---|
| k6 sends to | the origin (`http://backend:80`) directly | the host proxy, which forwards to the origin |
| Iteration | identical: priming GET, four GET probes, four SQLi POSTs, Williams order | same |
| Rate, window, warm-up, start delay | identical | same |
| Load generator, cores, network | identical | same |
| Host proxy container | left running from the previous tier, idle, receives nothing | serves every request |

The tier order within a replicate is balanced across replicates (`tier_order`). With four tiers
every tier holds every position twice in eight replicates; the five replicates used cover five rows
of that cycle. So **bare − origin** and **silent − origin** are paired exactly as silent − bare is:
both members are measured minutes apart in the same replicate, and the reported value is the median
of the per-replicate differences with their range and sign agreement. Session drift is common to
both members and cancels.

There are four floors, one per host proxy's run. They should coincide. How far apart they are shows
how much the floor moved between sessions, which is the drift that pairing removes and an unpaired
comparison would have kept.

### 3. What the three differences mean, and what they do not

- **bare − origin** is what placing the host proxy, without RPDL, in the path adds on this testbed.
  It contains the proxy's request handling, forwarding and upstream connection pool, **and** one
  extra traversal of the Docker bridge in each direction (client → proxy → origin instead of
  client → origin). It is a property of this topology, not of the proxy software alone. A proxy on
  its own machine would replace the bridge traversal with a real network hop.
- **silent − origin** is the deployed stack: host proxy plus RPDL.
- **silent − bare** is the unchanged headline RPDL cost, reported beside the others as "of which
  RPDL". Within one replicate `silent − origin = (bare − origin) + (silent − bare)` exactly. Across
  replicates each figure is its own median, and medians need not add up, so the thesis should never
  derive one of the three by subtracting the other two.
- **Percentages are relative to a very fast origin.** The origin is static nginx serving HTML files
  of 864–1,396 B, answering in a fraction of a millisecond. A fixed cost of a few hundred
  microseconds is therefore a large percentage of it, and would be a few percent in front of an
  application that takes tens of milliseconds. The absolute µs is the result; a percentage must be
  stated as "relative to this origin".
- **Client connections end somewhere different.** In the origin tier, k6's keep-alive connections
  terminate at the origin's nginx under its default policy (75 s idle timeout, 1,000 requests per
  connection; `backend/nginx.conf` sets neither). In the proxied tiers they terminate at the host
  proxy, under its own settings. Any difference in reconnects is part of bare − origin.
  `new_conn_<probe>` is recorded for every tier, so it can be checked.
- **Only the four GETs are reported.** The SQLi POSTs stay in the origin tier's iteration so that
  every tier offers the same load, but the origin has no `/api/login` and answers 404, so a
  difference against it is not anything RPDL does. The SQLi hit/miss experiment does not get an
  origin tier at all, and the runner refuses one for the SQLi mixes.

### 4. Why the main dataset is re-measured, and nothing else is

**"The current data is stale" is not quite the reason.** The three-tier dataset of 2026-10-04 is not
wrong: its silent − bare is a valid paired measurement. The reason is pairing. A difference is
trustworthy here only because both members come from the same replicate, minutes apart. Origin
cells measured now could only be compared with bare cells from the 2026-10-04 suite. That is the
unpaired comparison of methodology §1.3, where session drift of 100–500 µs would be an unknown,
large fraction of a proxy cost estimated at a few hundred µs. `merge_replicates.py` cannot help
either: it adds whole replicates and never forms a pair across sessions.

So the main paired run is repeated with four tiers: `run_paired_suite.sh`, about 6.5 hours, guest
tuned, host kept awake. It first copies the three-tier files and figures to
`benchmarks/results/archive/<date>-pre-origin-tier/`, then regenerates every analysis and figure drawn
from them. Every number the thesis takes from `paired_<edge>.json` is then re-derived from the new
run and should be quoted from it alone. The new values of the same quantities (silent − bare, CPU
added, timers) will differ from the old ones by about the spread of their replicates.

| Dataset | Re-measured? | Reason |
|---|---|---|
| Main paired run, `paired_<edge>.json` | **Yes** | the origin tier must share replicates with bare and silent |
| Functional matrix | No | a functional check with no timing and no origin tier |
| SQLi hit/miss | No | excluded by design; the origin has no trap |
| GET-only and page-size mixes | No | self-contained paired runs. The per-mechanism split combines the GET-only run with the main one, but each side is already a within-replicate difference, and the two were hours apart before |
| Apache `LuaScope conn` variant | No (optional) | its own paired run, set beside Apache's main delta as before |
| Capacity | No | separate runner with no origin tier |

### 5. What changes in the thesis after the re-run

- The tier description in the contributions list ("Each host proxy is measured in three tiers in the
  same container …", `template_final.tex` ~line 165) becomes four tiers. The origin tier is **not in
  the host proxy's container** (it bypasses the proxy), so "in the same container" must not cover it.
  A precise form: *each host proxy is measured in four tiers within the same replicate: the origin
  reached directly with no host proxy in the path, the host proxy without RPDL (bare), with RPDL as
  deployed (silent), and with RPDL plus timing instrumentation.*
- Every number quoted from the paired dataset must be re-read from the new run:
  - the contributions list (~lines 175–180): 114–218 µs, 13–23 %, 109–136 µs, Apache's 2,174 µs
    (+334 %), its 13–16 segfaults per run, and marginal CPU of 33/41/123/200 µs;
  - RQ1 (~line 507);
  - RQ2's CPU, cost-attribution and probe-effect figures (~lines 520–527);
  - the GET-path side of the per-mechanism split.
- RQ1 gains a result it could not state before: per host proxy, the latency the proxy adds on its
  own, and RPDL's share of the latency the deployed stack adds above the origin. That share is
  computed per replicate as (silent − bare) / (silent − origin) and then summarised, never as a
  ratio of two medians.
  Sources: `benchmarks/results/origin_floor.md`, figures `plots/origin_floor/<rate>_per_s.png`, and
  `plots/origin_floor/latency_levels.png` for context. Design and caveats:
  `docs/BENCHMARK_METHODOLOGY.md` §9.

### 6. Harness check before the re-run

A one-replicate smoke run of all four host proxies with the four tiers (2026-10-13, methodology
§9.5) completed with 47 of 48 runs valid. All 12 origin runs were valid. The single rejection was
Apache's instrumented tier at 100 iterations/s, the known mod_lua crash. The four independently
measured origin floors agreed to within about 50 µs at each rate (for example 341–350 µs at 100/s).
That is the evidence that the floor is stable enough for per-replicate pairing to remove what drift
remains. Smoke numbers have no interval and are not results; the thesis quotes only the five-replicate
re-run.

### 7. Results of the four-tier run (2026-10-15)

The re-run (`paired_suite_20261014T203839Z`, guest tuned, no host freezes) produced 236 of 240 valid
runs. All four rejections were Apache's instrumented tier at 100 iterations/s. Every origin, bare and
silent run is valid, so each figure below rests on five paired replicates that all agree in sign.

**What the host proxy itself costs, and what RPDL adds** (median latency added per GET request, at
1 / 10 / 100 iterations/s; `benchmarks/results/origin_floor.md`):

| Host proxy | Proxy alone (bare − origin) | Proxy + RPDL (silent − origin) | of which RPDL (silent − bare) | RPDL's share |
|---|---|---|---|---|
| OpenResty | 260 / 255 / 184 µs | 409 / 388 / 294 µs | 145 / 114 / 111 µs | 35 / 31 / 37 % |
| Envoy + WASM | 372 / 369 / 319 | 500 / 484 / 442 | 150 / 110 / 121 | 29 / 23 / 28 % |
| Apache + mod_lua | 324 / 372 / 279 | 545 / 553 / 580 | 233 / 189 / 295 | 42 / 33 / 51 % |
| Envoy + Lua | 396 / 389 / 309 | 571 / 557 / 455 | 203 / 170 / 147 | 34 / 30 / 32 % |

The origin answered a GET in about 0.52–0.55 ms at the two lower rates and 0.35 ms at 100/s, and the
four separately measured floors agree within 36 µs. Read plainly: **putting a reverse proxy in front
of this origin costs 0.18–0.40 ms per request, and RPDL adds a further 0.11–0.30 ms. RPDL is roughly a
quarter to a half of the total, and the proxy is the larger part on every host proxy except Apache at
100/s.** The caveats of §3 apply: the proxy figure includes an extra Docker-bridge traversal, and the
percentages are relative to a very fast static origin.

**Two earlier numbers did not reproduce, and the thesis currently quotes both:**

1. *Apache's median at 100/s.* The three-tier run gave +2,174 µs (+334 %); the re-run gives +295 µs
   (+48 %), with the same number of worker crashes (12–16 per run). The per-replicate medians spanned
   +605 to +26,332 µs before and +235 to +544 µs now. Crash recovery produces stalls of tens of
   milliseconds, and whether enough of them land in the middle of the distribution to move the median
   is chance. What did reproduce is the tail: the 90th percentile rose by tens of milliseconds in both
   runs. The defensible statement is that under RPDL at 100 iterations/s Apache's workers crash
   repeatedly and its latency tail grows by tens of milliseconds, while the median increase varied
   between about 0.2 ms and 26 ms across runs. A single median such as "2,174 µs" is not defensible.
2. *Apache's marginal CPU of 200 µs.* The linear fit that separates per-request from idle CPU is now
   rejected as unphysical for Apache's RPDL tiers in every replicate. Respawning crashed workers makes
   CPU per request rise at 100/s (722 µs against 430 µs at 10/s), which no straight line through the
   three rates describes. Quote Apache's CPU added per rate instead (+295 / +129 / +495 µs per request
   at 1 / 10 / 100 iterations/s). The other three marginal figures reproduced: OpenResty 31 µs (was 33),
   Envoy + WASM 108 (123), Envoy + Lua 45 (41).

Every other number the thesis draws from the paired dataset should now be re-read from
`paired_headline.md`, `origin_floor.md` and the regenerated figures of this run, not from the
archived three-tier files.

---

## 2026-10-18 — The in-server deployment: HDM inside the web server that serves the pages

This entry records why two in-server variants were added, how they were built and checked, what
their tiers mean, how the two deployments may be compared, and what was found before the full
benchmark run. It uses the thesis's terms (§1 below) so passages can be lifted directly. The full
paired run (`run_inserver_suite.sh`) and therefore every RQ5 number are **still pending**; nothing
below is a reportable cost.

### 1. Terms (changed in the thesis on this date)

| Term | Meaning |
|---|---|
| **HDM**, HTTP Deception Module | a deception module that runs in the extension mechanism of an HTTP server (was "Reverse Proxy Deception Module, RPDM"; the code, log prefixes and file names keep "WADM") |
| **host server** | the server whose extension mechanism runs HDM (was "host proxy") |
| **proxy deployment** | HDM in a reverse proxy in front of an unmodified origin server |
| **in-server deployment** | HDM in the web server that serves the pages itself; no proxy |
| **six variants** | four proxy variants on three reverse proxies (OpenResty, Envoy Lua, Envoy WebAssembly, Apache httpd) and two in-server variants (OpenResty in-server, Apache in-server) |

RQ1–RQ4 are answered for the four proxy variants. The new RQ5 compares the two deployments:

* **RQ5.** How do the cost and the correctness of HDM change when it runs inside the web server
  that serves the pages instead of in a reverse proxy in front of it?
  * RQ5.1 Does the same HDM code add the same latency and CPU time per request in the in-server
    deployment as in the proxy deployment of the same server?
  * RQ5.2 How much latency and CPU time does each deployment add over direct access to the origin
    server, and how much of the difference between the deployments is the proxy hop?
  * RQ5.3 Do compressed pages, partial content and responses to `HEAD` requests behave differently
    in the in-server deployment?

### 2. Why an in-server deployment is possible, and why Envoy has none

nginx and Apache httpd are web servers that act as reverse proxies only through their proxy
modules. Their extension hooks are not tied to proxying:

* **nginx** processes a request in fixed phases: an access phase, a pre-content phase in which
  `try_files` acts, and a content phase in which, for example, the static-file module produces the
  response. Then "both header and body are passed through a chain of filters and eventually get
  written to the client socket" (nginx Development Guide). lua-nginx-module's `access_by_lua_block`
  "acts as an access phase handler … for every request", and `header_filter_by_lua_block` /
  `body_filter_by_lua_block` "define an output header filter" / "an output body filter" (README).
  So the same Lua code sees a file served from disk exactly as it sees a proxied response.
* **Apache httpd**: "All requests pass through `ap_process_request_internal()` … including
  subrequests and internal redirects", with `translate_name` (where mod_alias acts, and where the
  core prepends `DocumentRoot`) before `access_checker` (Request Processing in Apache 2.4). The
  filter chain "enables applications to … post-process outgoing data … regardless of where the data
  comes from" (Filters, Apache 2.4 docs). One exception, error pages, is in §5.
* **Envoy** is a dedicated proxy. In its route API "precisely one of `route`, `redirect`,
  `direct_response` must be set": forward to an upstream cluster, return a redirect, or "return an
  arbitrary HTTP response directly, without proxying", whose status and body are fixed in the route
  configuration. None of them serves the files of a document root, so Envoy hosts HDM only as a
  proxy. (The claim is deliberately no stronger than that.)

Related work: Kahlhofer and Rass treat reverse proxies as one deployment method (their §5.3) and
divide the application layer into finer levels, including a *plugin-system level* ("applications
that provide means for third parties to easily add custom code"). Classifying the in-server
deployment at the plugin-system level of the web server is this thesis's mapping, not theirs. They
do not discuss web-server modules for deception.

### 3. How the in-server variants were built

| | OpenResty in-server (`nginx/nginx-inserver.conf`, port 8084) | Apache in-server (`httpd-inserver.conf`, port 8085) |
|---|---|---|
| Removed | `upstream backend`, `proxy_pass`, every `proxy_*` | `mod_proxy`, `mod_proxy_http`, `ProxyPass*` |
| Content | `root /srv/www` (`backend/www`, read-only) | `DocumentRoot "/srv/www"`, `TypesConfig conf/mime.types` |
| Module code | `lua_shared_dict` + `init_by_lua_block` copied **verbatim** (754 lines); `parity_check.py` diffs it against `nginx.conf` before every run and stops on any difference (tested with a tampered copy) | the same `apache_scripts/` directory, mounted into both services |
| Hooks | the three `*_by_lua_block` calls at server level, inherited by every location | identical to `httpd.conf` |
| `/` | `location = / { try_files /index.html =404; }` | `AliasMatch "^/$" "/srv/www/index.html"` |
| gzip test page | `location /test/gz/ { gzip on; … }` | `<Location "/test/gz/"> AddOutputFilterByType DEFLATE text/html` |
| 204 test page | `try_files /__no_such_file__ =204` | `RedirectMatch 204 "^/test/empty$"` |
| Bare tier | the same file without the module's lines | the same file without mod_lua, mod_headers and the `Lua*` / filter directives |
| Image | the same local `openresty/openresty:latest` (`sha256:12c49073…`) | the same local `httpd:2.4-alpine` (`sha256:c9e51ef5…`, Apache 2.4.67) |

**Why `/` is mapped directly, measured rather than assumed.** nginx's `index` directive "causes an
internal redirect" (nginx docs), and internal redirects "will destroy the original request
`ngx.ctx` data" (lua-nginx-module README). A copy of `nginx-inserver.conf` without the `location = /`
block raised **two** `WADM ALERT` lines for one trigger to `/` (one for `/about.html`). `try_files`
"uses the first found file for request processing; the processing is performed in the current
context", so the shipped mapping raises one.

The plan assumed the same for Apache, because mod_dir looks the index up with a sub-request
(`ap_sub_req_lookup_uri`, then `ap_internal_fast_redirect`, in `modules/mappers/mod_dir.c`). **That
assumption was wrong.** A copy of `httpd-inserver.conf` with `DirectoryIndex index.html` in place of
the `AliasMatch` raised **one** alert for one trigger to `/`, and `detect.lua` ran once: one
`WADM TOKEN http_headers inject` line, a line it writes on every request. Apache keeps the
`AliasMatch` for symmetry with OpenResty, and the config comments, READMEs, `docs/EDGE_LEVELING.md`
and the thesis say so. (Not traced in source: why the sub-request does not re-run the access hook.
Do not state a mechanism for it.)

**Why the 204s differ.** `try_files … =204` runs in nginx's pre-content phase, after the access
phase, so detection and baits apply. Apache's `RedirectMatch` with a non-3xx status and no URL is
documented in mod_alias ("If the status is not between 300 and 399, the URL argument must be
omitted"). mod_alias acts at `translate_name`, before `access_checker`, so Apache's 204 carries no
bait header and raises no alert. No benchmark probe requests it.

### 4. What the tiers mean in-server, and how the deployments may be compared

* `silent − bare` keeps its meaning in both deployments: the module's cost on the same server.
* `bare − origin` changes meaning. In the proxy deployment it is the proxy hop. In-server it is
  **OpenResty or Apache serving files, against nginx:alpine serving the same files** (the "server
  swap"). It is part of the in-server `silent − origin`, which is therefore confounded by the choice
  of file server, most for Apache. `analyze_paired.py` labels it "bare server - origin".
* The deployments are measured in **separate sessions**. Each difference is paired inside its own
  session, so the deployments are compared as **two paired differences with their replicate
  ranges**, never as a subtraction of raw latencies across sessions. `compare_deployments.py`
  reports whether the ranges overlap and prints no difference of the medians.
* **Core allocation.** Proxy deployment: the work is spread over the proxy's two cores plus the
  origin's core. In-server: all of it is on the host server's two cores, and the origin's core is
  idle. Latency and CPU per request are comparable at the measured rates; **capacity is not
  compared**, and no in-server capacity run was made.
* **Stack CPU** (for the comparisons with origin) = host container + origin container CPU per
  request, because the proxy deployment splits the work between two containers. On the in-server
  edge tiers the origin's CPU per request should be about zero: that is the check that nothing was
  proxied.

### 5. Verification results (2026-10-18)

* All four in-server configs parse (`openresty -t`, `httpd -t`).
* The four proxy variants are untouched: `git diff` on their configs, scripts, `config.json`,
  `test.js` and `backend/` is empty. The modified `analyze_paired.py` reproduces the archived
  `paired_summary.json` **byte for byte** (the bootstrap is seeded).
* **Parity check** (`results/parity_check_2026-10-18.txt`): the Lua sync check passes. Envoy+Lua,
  Envoy+WASM and Apache are identical to OpenResty as before. **Both in-server variants are identical
  under the in-server rules**: the same 11 `WADM ALERT` lines and one `WADM TRAP` line, matching
  status/baits on every probe and bodies on every 200, and **no request at the origin**.
* **Functional matrix** (`results/functional_matrix.md`): the four proxy columns are identical,
  verdicts and reasons, to the 2026-10-04 matrix. In-server, against each variant's own bare tier:

| Case | OpenResty proxy | OpenResty in-server | Apache proxy | Apache in-server |
|---|---|---|---|---|
| HTML, gzip-encoded | BROKEN | **injected** (decodes) | BROKEN | **injected** (decodes) |
| HEAD | changed | changed | BROKEN | **preserved** |
| Range `bytes=0-99` | BROKEN | BROKEN by the matrix's rule: 200 with the whole injected page | BROKEN | **preserved**: 206 of the injected page, `Content-Range: bytes 0-99/975` |
| 204 | changed (bait header) | changed | changed | **preserved** (answered before the access hook) |
| 404 page | injected | injected | injected | **preserved**: bait header and cookie, no body tokens |
| everything else | as proxy | as proxy | as proxy | as proxy |

  How to read it:
  * **gzip works in-server.** The decoded page carries the tokens and decodes cleanly, so in both
    servers the module's body filter ran before the server compressed the page (observed; the
    filter order was not traced in source). In every proxy variant the page arrives already
    compressed and the tokens are spliced into compressed bytes.
  * **OpenResty's range answer is valid HTTP.** "A server MAY ignore the Range header field"
    (RFC 9110 §14.2). The matrix calls any status change BROKEN, but the client receives the full
    injected page with 200.
  * **Apache does not inject into its own error pages.** `ap_send_error_response`
    (`modules/http/http_protocol.c`) resets `r->output_filters = r->proto_output_filters`, with the
    comment "get rid of any RESOURCE filters that might be lurking around". mod_lua registers
    `LuaOutputFilter` filters with `AP_FTYPE_RESOURCE` (`modules/lua/mod_lua.c`). Headers staged in
    `err_headers_out` survive. In the proxy deployment the 404 came from the origin as an ordinary
    proxied response and was injected.

**Consequence for RQ5.1 (important).** Two of the four benchmark GETs (`detect_query` =
`/api/login?password=…`, `token_decoy` = `/api/v1/debug`) are 404s. On Apache in-server the module
therefore inserts body tokens into **three of the five** HTML responses per iteration rather than
five. Its wadm-tier html_comments and decoy_paths inject coverage is 3/5 by construction, and its
`BAIT MISSING` lines for those two probes are expected. Its `silent − bare` therefore covers less
body-injection work than the proxy variant's. `compare_deployments.py` prints the coverage beside
the costs so this cannot be overlooked. OpenResty in-server injects into nginx's own error pages
(they pass through the Lua body filter), so it does the same work as its proxy counterpart.

**A correction to earlier text (proxy deployment).** The thesis said that Envoy Lua is the only
variant that sends `Content-Length` with an injected page. The functional matrices of 2026-10-04 and
2026-10-18 show **Apache (proxy) sending a `Content-Length` that matches the injected body** too
(975 bytes for the 864-byte `about.html`), despite `Header always unset Content-Length`; the
mechanism was not traced. Only OpenResty and Envoy+WASM send injected pages chunked. Corrected in
§5.3 (Envoy Lua), §5.5 (Apache), §5.7 (levelling), `tab:impl.residuals` and `docs/EDGE_LEVELING.md`. Apache in-server sends a matching
`Content-Length` as well, and drops the file's `ETag` on its WADM tier.

**A mechanism verified for later use (C8).** lua-nginx-module's header filter sets
`r->filter_need_in_memory = 1` whenever a body filter is configured (`ngx_http_lua_header_filter`,
`src/ngx_http_lua_headerfilterby.c`). With the hook at server level, every in-server response is
therefore read into memory instead of being handed to `sendfile`; the bare tier keeps `sendfile`.
That is part of the module's in-server cost, and it does not arise in the proxy deployment, where a
proxied body arrives in memory anyway.

### 6. Harness smoke run (2026-10-18; one replicate, 20 s windows; NOT reportable)

`run_paired_benchmark.py --preset smoke --results <scratch>` for `openresty_inserver`,
`apache_inserver` and, as a check that the proxy path still works, `openresty`.

**The guest was untuned and busy**: THP `[always]`, swap on, load average 7.5–10 on 8 vCPUs (VS Code,
two Claude sessions, Xorg, and an unidentified `rphcp` process at ~38 % CPU for 5 h). The origin
control, the same nginx:alpine doing the same work, burned 280–1,520 µs of CPU per request, against
153–287 µs in the tuned 2026-10-15 run. **All smoke CPU figures are inflated 2–6× by host
contention** and say nothing about the variants.

What the smoke run does show:

* **Harness path.** Every tier ran and was gated. OpenResty in-server 8/12 cells valid, Apache
  in-server 7/12. All rejections are at 100/s or are host-contention gates (the origin tier itself
  dropped 5–7 % of iterations at 100/s; one origin cell failed the origin-CPU control).
* **Tier self-checks (both variants, 1 and 10/s; OpenResty also 100/s):** exactly **7 `WADM ALERT`
  + 2 `WADM TRAP` per iteration** on silent and wadm, 0 on bare; timing lines only on wadm (0 on bare
  and silent); **origin CPU per request 0.0 on every in-server edge tier** (nothing proxied); no
  crash indicator in any cell.
* **Coverage.** OpenResty in-server: complete on every kind. Apache in-server: html_comments and
  decoy_paths inject at **0.600**, exactly 3/5, the error-page finding of §5; every other region is
  complete. `analyze_paired.py` also prints `BAIT MISSING: detect_query, token_decoy` there, as
  expected, and no spurious `DIFFERS FROM ORIGIN` on the bare tier (the 404-origin rule works).
* **Apache reconnects.** ~25 % of Apache in-server's silent/wadm requests open a new connection,
  the same pattern as Apache proxy's silent tier in the real dataset (0.249 per probe, about half
  that on the two SQLi-hit probes). It is not specific to the in-server deployment.
* **Apache in-server at 100/s collapsed on the WADM tiers**: 19–20 % of the offered rate achieved
  (393 of 2,000 iterations), 500 VUs in use, 1,250–1,424 dropped iterations. Its bare tier sustained
  100 %, with 0.54 % failed requests. Started-but-unfinished iterations still log their alerts, which
  is why alerts per *completed* iteration read 9.7–11.2 there. Part of this is host contention, but
  bare survived the same host, so on two cores **Apache in-server with HDM is close to or past its
  limit at 100 iterations/s** (≈ 900 requests/s), where the proxy Apache (two cores plus the
  origin's core) sustained the rate in the real dataset. This is a capacity-flavoured observation
  from one contended replicate, not a result. Watch this cell in the full run.
* **The proxy path still works.** `--edge openresty` after the changes: 9/12 cells valid, labels
  `bare proxy - origin`, alert recall 1.0, coverage complete. Its 100/s cells were rejected for
  dropped iterations, as the in-server ones were; all 60 OpenResty cells were valid in the tuned
  2026-10-15 run, which again points at the host.
* **`compare_deployments.py`** runs end to end on these files (OpenResty pair) and on the real
  `paired_apache_lua.json` with the Apache smoke file (Apache pair). It writes the `.md`, `.csv` and
  `.png`, shows "n/a (too few replicates)" for one-replicate overlaps, and its coverage table shows
  Apache in-server's 0.6000.

**Before the full run.** Tune the guest (`sudo ./benchmarks/tune_guest.sh apply`), disable host
sleep, and stop what is not needed (the `rphcp` process, extra editors and sessions). Then
`./benchmarks/run_inserver_suite.sh` in the background (~3.3 h). Afterwards check for clock jumps
(`journalctl -k | grep "Radical host time change"`), 5/5 valid replicates per cell, and Apache crash
counts; and check that the four proxy entries in the regenerated `paired_summary.json` are byte for
byte the archived ones.

### 7. Sources verified for this work

All archived copies were downloaded and read (not only looked up). Accessed 18.10.2026.

| Key | What it supports | Archived URL |
|---|---|---|
| `nginx_core` | `try_files` serves "in the current context"; `=code`; `root` | web.archive.org/web/20261015150358/https://nginx.org/en/docs/http/ngx_http_core_module.html |
| `nginx_index` | `index` "causes an internal redirect" | web.archive.org/web/20260913194145/https://nginx.org/en/docs/http/ngx_http_index_module.html |
| `nginx_devguide` | phases; header and body pass through filter chains | web.archive.org/web/20261011192353/http://nginx.org/en/docs/dev/development_guide.html |
| `apache_alias` | `AliasMatch`; numeric non-3xx `Redirect` status omits the URL | web.archive.org/web/20261010105038/https://httpd.apache.org/docs/2.4/mod/mod_alias.html |
| `apache_request` | hook order; all requests incl. subrequests pass through one path; DocumentRoot fallback | web.archive.org/web/20260915203602/https://httpd.apache.org/docs/2.4/developer/request.html |
| `apache_filters` | filters process outgoing data "regardless of where the data comes from" | web.archive.org/web/20260915201917/https://httpd.apache.org/docs/2.4/filter.html |
| `apache_httpprotocol_src` | error responses drop RESOURCE filters | web.archive.org/web/20250125180822/https://github.com/apache/httpd/blob/2.4.x/modules/http/http_protocol.c |
| `envoy_route` | route = upstream, redirect or direct response | web.archive.org/web/20260516172838/https://www.envoyproxy.io/docs/envoy/v1.30.0/api-v3/config/route/v3/route_components.proto |
| `rfc9110` (existing) | §14.2 "A server MAY ignore the Range header field" | web.archive.org/web/20261013083737/https://www.rfc-editor.org/rfc/rfc9110.html |
| `luanginx` (existing) | the three directive descriptions quoted above (checked on the live README; the archived GitHub page holds only its table of contents) | existing entry |

Read in source but **without an archived copy**, so not yet citable under the department's rule:
`modules/lua/mod_lua.c` (`AP_FTYPE_RESOURCE`; marked `\todo` in thesis §5.6),
lua-nginx-module `src/ngx_http_lua_headerfilterby.c` (`filter_need_in_memory`; needed only if C8
explains the in-server cost through `sendfile`), and `modules/mappers/mod_dir.c` (sub-request;
used in these notes only). The lua-nginx-module README sentence on `ngx.ctx` and internal
redirects was read on the live file (line 4468), which the existing `luanginx` entry covers.

---

## 2026-10-19 — Results of the in-server run: what RQ5 measured

This entry records the full in-server run and what it answers. It uses the thesis's terms (HDM,
host server, proxy deployment, in-server deployment; see the 2026-10-18 entry). The thesis section
"RQ5: Deployment inside the Web Server" (Chapter 7) is drafted from exactly these numbers.

### 1. The run

`run_inserver_suite.sh` (log `results/inserver_suite_20261018T214050Z.log`) ran on the tuned guest
from 21:40 to 00:47 UTC: `--preset full`, 5 replicates × 3 rates (1, 10, 100 iterations/s) × 4 tiers
(origin, bare, silent, wadm) for each in-server variant.

* **120 of 120 cells valid**, 5/5 replicates everywhere. No clock jump, no soft lockup, no host
  stall.
* Apache in-server's workers segfaulted 9–12 times per HDM run at 100/s and 2–3 times in three HDM
  runs at 10/s, never without HDM. This is the same mod_lua crash as proxy Apache (12–16 per run at
  100/s).
* `paired_summary.json` was regenerated, and its five earlier entries are byte-identical to the
  archived copy.
* On every in-server tier with a host server, the origin's CPU was 0.00–0.61 µs per request. This
  is the direct evidence that nothing was proxied.

**Correction to the 2026-10-18 entry.** The smoke run had Apache in-server's HDM tiers collapsing at
100/s. On the tuned guest every one of those cells reached 3,000 iterations, 100 % of the offered
rate. The collapse was host contention, not the variant. The smoke CPU figures (2–6× inflated) are
superseded too.

### 2. The numbers (from `results/deployment_comparison.md`)

Median per GET of the paired differences; proxy figures are the 2026-10-15 run. "Overlap" compares
the two replicate ranges. The medians are **not** subtracted from each other: the deployments were
measured in different sessions.

| | 1 / 10 / 100 per s | proxy | in-server | overlap |
|---|---|---|---|---|
| OpenResty | HDM's latency (silent − bare) | +145 / +114 / +111 µs | +69 / +81 / +72 µs | no / no / no |
| | HDM's CPU (silent − bare) | +61 / +31 / +31 µs/req | +26 / +19 / +32 µs/req | yes / yes / yes |
| | stack above origin (silent − origin) | +409 / +388 / +294 µs | +12 (spans 0) / +29 / +82 µs | no / no / no |
| | bare − origin | +260 / +255 / +184 (proxy hop) | −58 / −49 / +10 (server swap) | no |
| | stack CPU above origin | +203 / +113 / +114 µs/req | −0.6 (spans 0) / −28 / +21 µs/req | no |
| Apache | HDM's latency (silent − bare) | +233 / +189 / +295 µs | +244 / +198 / +183 µs | yes / yes / no |
| | HDM's p95 at 100/s | +83,946 µs | +1,491 µs | no |
| | HDM's CPU (silent − bare) | +295 / +129 / +495 µs/req | +279 / +183 / +260 µs/req | yes / yes / yes |
| | stack above origin (silent − origin) | +545 / +553 / +580 µs | +286 / +271 / +224 µs | no / no / no |
| | bare − origin | +324 / +372 / +279 (proxy hop) | +46 / +71 / +35 (server swap) | no |
| | stack CPU above origin | +484 / +294 / +812 µs/req | +360 / +276 / +290 µs/req | yes / yes / no |

### 3. What it means, sub-question by sub-question

**RQ5.1, the same code in two places.**

* *OpenResty.* HDM adds less latency in-server, 69–81 µs against 111–145 µs per GET, with ranges
  that do not overlap at any rate. The CPU it adds cannot be told apart between the deployments.
  The measurements do not say why. A plausible candidate is that in the proxy deployment the body
  filter waits for a body arriving from the upstream in pieces, but this was not measured, so it
  must not appear in the thesis as an explanation.
* *Apache.* At 1 and 10/s the two deployments cannot be ordered, in latency or in CPU. At 100/s the
  proxy variant's median is not a stable quantity (+2,174 and +295 µs in two runs), so compare the
  tail: p95 +1,491 µs in-server against +83,946 µs proxy, with similar crash counts.
* *Apache caveat.* Apache in-server inserts into only 3 of 5 HTML responses per iteration (error
  pages bypass HDM; coverage 0.60), so its cost covers less body work. Any sentence comparing
  Apache's module cost across deployments must say so.

**RQ5.2, cost above the origin, and the proxy hop.** In the in-server deployment the stack adds
12–82 µs per GET above direct access to the origin for OpenResty and 224–286 µs for Apache. In the
proxy deployment it adds 294–580 µs, and the proxy hop alone (bare − origin, paired within its own
session) accounts for 184–372 µs of that. In-server there is no hop. What bare − origin measures
there is the server swap: OpenResty serves these files slightly faster than the nginx:alpine origin
at 1 and 10/s (−58, −49 µs) and 10 µs slower at 100/s (range +4..+16); Apache serves them 35–71 µs
slower. Apache's in-server stack figure therefore includes that file-server difference, which is
not HDM's cost. Stack CPU follows the same pattern: OpenResty in-server's stack uses between 28 µs
less and 21 µs more CPU per request than the origin alone (−0.6 spans zero at 1/s; −28 and +21 are
resolved at 10 and 100/s), against +113 to +203 µs/req in the proxy deployment.

**RQ5.3, responses.** From the functional matrix (2026-10-18, see the previous entry):

* in-server, gzip pages receive their tokens and still decode, where every proxy variant breaks
  them;
* Apache in-server answers HEAD and range requests correctly (proxy Apache: BROKEN for both);
* OpenResty in-server answers a range request with 200 and the whole injected page, which is legal
  (RFC 9110 §14.2) but counted BROKEN by the matrix's rule;
* Apache in-server's own 404 pages carry baits but no body tokens, and its 204 carries no bait.

Under load, OpenResty in-server is 100 % intact with recall 1.0 at every rate. Apache in-server's
worst intact share is 99.78 % (10/s) and 99.63 % (100/s), with alert recall ≥ 0.9978 and trap
recall ≥ 0.998, close to proxy Apache (99.36–99.78 %, 0.9979–0.9997).

### 4. Draft answer (as written into the thesis)

> In OpenResty, the same HDM code added less latency in-server than in the proxy deployment, 69–81
> against 111–145 µs per GET, at a CPU cost that the measurements cannot distinguish (RQ5.1). In
> Apache httpd, the module's cost could not be ordered at 1 and 10 iterations/s; at 100, its latency
> tail stayed below 5 ms in-server, where the proxy variant's reached tens of milliseconds, and
> Apache in-server does less insertion work because its own error pages bypass HDM. Above the
> origin server, the in-server deployments add far less: 12–82 µs per GET for OpenResty and
> 224–286 µs for Apache, which includes 35–71 µs of Apache serving files, against 294–580 µs in the
> proxy deployments, in which the proxy hop alone accounts for 184–372 µs (RQ5.2). In-server,
> compressed pages receive their honeytokens without being broken, Apache answers HEAD and range
> requests correctly, OpenResty answers a range request with the whole page, and Apache inserts no
> honeytokens into its own error pages (RQ5.3).

### 5. Limits to state alongside it

* Two sessions, compared through paired differences only. The origin floors of the sessions agree
  (0.551 ms vs 0.543/0.545 ms at 10/s), which supports but does not replace the pairing.
* Two cores for the in-server host server against two plus one in the proxy deployment. No capacity
  comparison was made, and none may be claimed.
* The server swap confound (largest for Apache) and Apache's reduced insertion work, both above.
* "In-server costs less" is a statement about this static site, where the origin answers in
  ~0.35–0.55 ms. In front of a slow application server, the proxy deployment's extra hop would be a
  smaller share.
