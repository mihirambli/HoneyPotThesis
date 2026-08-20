<!-- benchmarks/EDGE_LEVELING.md: record of the source changes that made the four WADM
     edges directly comparable for internal detection/injection microbenchmarking. -->
# Bringing the four edges onto the same plane

The four WADM edges — **OpenResty** (`nginx/nginx.conf`), **Envoy+Lua**
(`envoy_scripts/injection.lua`), **Apache+mod_lua** (`apache_scripts/`), and
**Envoy+WASM** (`wasm-filter/src/lib.rs`) — are each timed for two hot-path operations:

- **Detection** — scan the query string for a trigger keyword and strip it.
- **Injection** — rewrite the HTML response to embed the honeytoken comment(s).

For the numbers to mean anything, every edge's timer must wrap the **same logical work on
the same request population**, with per-request bookkeeping and buffering *waits* excluded.
This document records the changes that got them there.

**OpenResty is the reference model.** It already timed the minimal, correct region, so the
other three edges were aligned to what OpenResty does. The result is captured as
"Level-playing-field invariants" #5 and #6 in [README.md](README.md).

---

## 1. Detection — in-memory state, equal timed region (invariant #5)

### The problem
The four detection timers wrapped different amounts of surrounding bookkeeping, so the
detection subplot partly ranked the *honeytoken state implementation*, not the detection
logic. The worst offender was **Envoy+Lua**, whose timer enclosed:

- a read + JSON-decode of `/tmp/detected_ips.json` on **every** request,
- a write + JSON-encode of that file on **every hit**, and
- a rebuild of the trigger-keyword table on **every** request.

This inflated its detection numbers **3–6×** and produced a spurious "gets faster under
load" curve (the file write is skipped once an IP is already recorded).

### The fix
Every edge records the attacker IP into an **in-memory** store on a hit, *inside* the
timer, mirroring OpenResty's `ngx.shared.wadm_state` (`wadm:set`). All per-request setup —
trigger-table build, client-IP read, known-attacker lookup — moved **outside** the timer.
No filesystem I/O or JSON (de)serialisation in the timed region.

| Edge | Before | After |
|------|--------|-------|
| **OpenResty** | in-memory shared dict, setup already outside the timer | unchanged (reference) |
| **Envoy+Lua** | `/tmp/detected_ips.json` read+write + JSON + trigger rebuild, all inside the timer | module-scope Lua table `detected_ips`; trigger build / IP read / known-attacker lookup hoisted out of the timer |
| **Apache** | no state store at all; timer started before the guards | module-scope `detected_ips` table written on a hit inside the timer; timer now starts right before the scan |
| **WASM** | no state store at all | `Rc<RefCell<HashSet<String>>>` on the root context, cloned per request; IP inserted on a hit inside the timer; client IP read before the timer |

### Result
Envoy+Lua detection dropped from **avg ≈ 369 / 411 / 348 / 154 µs** (VU 1/10/100/500) to
**≈ 62 / 56 / 36 / 29 µs**, with the tightest p90 of any edge. OpenResty and WASM were
unchanged (confirming they were already fair).

---

## 2. Injection — the canonical injection contract (invariant #6)

### The problem
Injection was the least fair phase:

- **Envoy+Lua** started its timer *before* the first `:body()` call. Envoy's Lua filter
  buffers the whole body on first `:body()` access, suspending the coroutine until it is
  complete — so the measured window included the **upstream body-arrival wait**, not just
  the transform (**≈5–10× inflation**).
- **Apache** ran a `gsub` on **each brigade chunk** with **no path matching** (it hardcoded
  `html_comments[1]`), and logged **one timing line per chunk** — a different, cheaper unit.
- Even **OpenResty** and **WASM** disagreed on whether path-matching sat inside the timer.

### The contract (identical on all four)
- **Setup outside the timer:** the `text/html` content-type guard, path matching / token
  selection, and the comment join.
- **Whole body buffered before the timer starts**, so no arrival/buffering wait is charged
  to injection.
- **Timed region:** assemble the full body → locate the **first** `</body>` → splice the
  joined comment(s) before it (append if absent) → write the body back.
- **Content-Length** adjusted outside the timer.

| Edge | Before | After |
|------|--------|-------|
| **OpenResty** | buffered whole body, timed only the last chunk; token join inside the timer | token join moved out of the timer (otherwise already conformant) |
| **Envoy+Lua** | timer opened before `:body()` → included the body-arrival wait; CL replace inside the timer | forces `:body()` buffering **before** the timer; path-match/join as setup; CL replace outside the timer |
| **Apache** | per-chunk `gsub`, no path matching, hardcoded first token, one log line per chunk | **rewritten**: buffers every brigade chunk, then one first-match splice at end-of-stream; real path matching; content-type guard; one log line per response |
| **WASM** | path-matching/join inside the timer; timing logged even on the no-op paths | path-matching/join hoisted out of the timer; timer wraps only read → find → splice → write |

### The splice primitive was not a fair "intended difference"

This section previously argued that each runtime's native string primitive (OpenResty PCRE
`ngx.re.sub`, WASM `str::find`, Envoy+Lua / Apache Lua `gsub`) was an *intended* residual
difference, on the grounds that all three produce the same first-match splice. That was wrong:
`gsub` runs Lua's backtracking **pattern matcher** for what is a fixed-string insert, builds the
output through a `luaL_Buffer` match loop, and re-concatenates the replacement on every request.
It is strictly more work than the job needs, so the injection figure was ranking an implementation
choice on two edges rather than the runtimes.

**Evidence.** Splitting injection by what its timed region does — write a response header vs.
locate an anchor and splice — isolated the effect exactly (VU=100 medians, before the fix):

| Edge | Header-write kinds | Body-splice kinds | Ratio | Primitive |
|---|---|---|---|---|
| WASM | 2 µs | 1 µs | 0.5× | `str::find` |
| OpenResty | 1 µs | 3 µs | 3.0× | `ngx.re.sub` (PCRE, JIT, cached) |
| Envoy+Lua | 2 µs | 5 µs | 2.5× | `string.gsub` |
| Apache | 2 µs | 6 µs | 3.0× | `string.gsub` |

The two `gsub` edges were the two slowest at body splices while being fully competitive at header
writes. The sharpest evidence sat *inside* Envoy's own filter: detection uses
`key:find(keyword, 1, true)` — a **plain** find, pattern matching disabled — and came out at 1 µs,
tied for fastest of all four edges, while injection's `gsub` in the same file on the same request
took 5 µs. Same runtime, same VM, ~5× apart.

**The fix.** Both Lua edges now use a `splice_before` helper that mirrors the WASM filter's:

```lua
local pos = body:find(anchor, 1, true)   -- plain find, no patterns
return body:sub(1, pos - 1) .. insert .. "\n" .. body:sub(pos)
```

Result (VU=100 body-splice medians): **Envoy+Lua 5 → 1 µs**, **Apache 6 → 3 µs**. Envoy+Lua now
ties WASM. Output is byte-identical — verified by fetching `/` and `/login.html` through all four
edges in-network and comparing md5sums (`e79c8b9d…` for `/login.html` on every edge).

The genuinely intended difference that remains is narrower: **OpenResty still uses `ngx.re.sub`**
(PCRE with JIT and a cached compile via the `o` flag) while the other three now do a plain
find-and-splice. That leaves OpenResty with the highest body-to-header ratio (3.0×, 3 µs vs 1 µs)
— worth either aligning it too, or stating plainly in the write-up that its body-splice column
reflects PCRE rather than the runtime.

### Result
A warm request through Envoy+Lua now logs injection at **≈ 4–6 µs**, in line with
OpenResty/WASM, versus the previous **avg ≈ 33–59 µs**. Apache now measures one whole-body
transform per response instead of a per-chunk unit.

---

## 3. Injection correctness — Envoy request-path capture

### The problem
Envoy+Lua read the request path from the **response** headers, where the `:path`
pseudo-header does not exist. It fell back to `/`, so only `/*` tokens ever matched and
exact-path tokens (e.g. one scoped to `/index.html`) were silently missed. A real bug —
but it did **not** affect the benchmark, which hits `GET /` (where `/` is the true path).

### The fix
Capture the path in `envoy_on_request` (where `:path` exists) and carry it to
`envoy_on_response` via per-stream dynamic metadata
(`streamInfo():dynamicMetadata()`, namespace `wadm.honeypot`) — the same trick the WASM
filter uses with `self.request_path`. Both the stash and the read sit **outside** the timed
regions, so the benchmark numbers are unaffected. After the fix, `GET /index.html` injects
both matching tokens.

---

## Verification

All changes were checked, not just written:

- **WASM** compiles (`docker compose build rust-builder`, exit 0); all Lua files parse
  (`luajit -bl`).
- **Functional end-to-end** on every edge: byte-identical 343-byte output for `GET /`, the
  comment correctly before `</body>`, no body duplication.
- **Path matching**: `GET /index.html` injects both tokens on Apache, WASM, and (after the
  fix) Envoy+Lua.
- **Apache**: exactly one injection-timing line per response (not per chunk).

The result JSON in `results/` and the plots in `results/plots/` should be regenerated
(re-run the four `run_internal_*_benchmark.py` scripts, then `plot_edge_comparison.py`) to
capture the leveled distributions — the injection subplot in particular.

---

## Additional honeytoken kinds get their own timed regions

Beyond `html_comments`, all four edges implement `http_headers`, `cookies`, `decoy_paths`,
and `form_fields` (plus a per-token `enabled` switch), with matching semantics and identical
`WADM ALERT` message formats. These four kinds are now **benchmarked in their own right**:
each edge times each kind separately and logs

```
WADM TOKEN <kind> detect (us): N
WADM TOKEN <kind> inject (us): N
```

The lowercase `detect` / `inject` words are load-bearing. They share no substring with either
scraper pattern, so OpenResty's **unprefixed** `Detection execution time \(us\):` regex cannot
swallow a per-kind line and silently corrupt `detection_us` — the same hazard the SQLi trap
label was designed around (see below).

### What each per-kind timer wraps

Mirroring the html_comments contract, per-request setup is hoisted out and the timer covers only
the kind's own work:

| Phase | Outside the timer (setup) | Inside the timer |
|---|---|---|
| **detect** | client IP, request URI, parsed query args, `Cookie` header — read once and shared by all four kinds; plus alert rendering and the `WADM ALERT` write, which happen *after* the timer closes (see "No log I/O inside a timer" below) | that kind's scan over its enabled tokens and the in-memory attacker-IP record on a hit |
| **inject** (header kinds) | enabled + path-match token selection | building the header/cookie value and writing it onto the response |
| **inject** (body kinds) | enabled + path-match selection and markup construction | locating the anchor (`</form>` / `</body>`) and producing the spliced body |

### No log I/O inside a timer

Detectors record hits as small descriptors; the alert is rendered and written **after** the timer
closes. This applies to the additional kinds and to html_comments, on all four edges.

**Why it had to change.** The first version enclosed the `WADM ALERT` write inside the detect
timer, matching what html_comments had always done. Measured that way, a kind's cost tracked
*whether it was the first to log on that request* rather than how much scanning it did. At 100 VUs:

| Kind | First to log on its request? | OpenResty | WASM | Apache | Envoy+Lua |
|---|---|---|---|---|---|
| html_comments | yes | 20 | 18 | 47 | 8 |
| http_headers | yes | 10 | 12 | 38 | 8 |
| decoy_paths | yes | 9 | 11 | 35 | 8 |
| cookies | no (2nd) | 5 | 2 | 8 | 3 |
| form_fields | no (3rd) | 2 | 3 | 4 | 1 |

A 3–10× split reproduced independently on all four edges, even though `cookies` does *more* string
work than `http_headers`. The injection timers, which never contained a write, showed no such
split. The detection figure was therefore ranking each runtime's logging path rather than its
detection logic.

**The fix.** Each per-kind detector appends `{tpl, a, b, c}` descriptors (Lua) or `Alert` enum
variants (Rust) to a list; a `format_alert` function outside the timer renders them to the exact
same wire text. So neither string formatting nor the write is charged to detection. The
`WADM ALERT` message format is byte-identical to before on every edge — verified by diffing the
distinct alert lines from a smoke run per edge.

One residual asymmetry: the Lua edges' descriptors hold references to existing strings, while the
Rust edge clones small `String`s into its `Alert` variants. At ~20 ns for a short-string clone
against a 1 µs timer resolution, this is far below one tick.

**Result.** The same table after the change, at the same 100 VUs:

| Kind | Logged first? | OpenResty | WASM | Apache | Envoy+Lua |
|---|---|---|---|---|---|
| html_comments | yes | 3 | 3 | 11 | 7 |
| http_headers | yes | 2 | 1 | 6 | 1 |
| decoy_paths | yes | 1 | 1 | 3 | 0 |
| cookies | no | 3 | 1 | 6 | 2 |
| form_fields | no | 1 | 2 | 2 | 0 |

The split is gone — logging position no longer predicts the number — and detection drops roughly
10×, confirming the write was the dominant term rather than a constant offset. A second artefact
disappeared with it: detection medians used to *fall* as load rose (OpenResty 11→5 µs from VU 1 to
500), which had been provisionally attributed to cold caches at low load. Post-change the curves
are essentially flat (OpenResty 2/2/2/1, Apache 5/6/6/6), so that slope was mostly the log-flush
term amortising under concurrency, not cache behaviour.

### Other consequences

- **Detection is timed only on a hit**, exactly as for html_comments, so every edge samples the
  same population per kind.
- **The body-kind timers exclude the write-back.** The extra payloads are chained on an in-memory
  string and written once at the end, so no edge charges its body-API write to a per-kind number.
  (This is *more* uniform than the html_comments contract, where Apache's timer ends before its
  `coroutine.yield` while the other three include their write — a pre-existing asymmetry the
  per-kind measurements do not inherit.)

### The html_comments measurement is untouched

The per-kind timers were added **around already-existing code**, never inside the html_comments
timed regions:

- The additional-kind detection pass still runs *before* the html_comments timer opens.
- The extra body payloads are still spliced *before* the html_comments splice, and that splice's
  timed region is byte-for-byte what it was. When a request matches no additional-kind body
  payload, the original single-branch code path runs unchanged.

Kind iteration order is pinned to `http_headers → cookies → decoy_paths → form_fields` on all four
edges. OpenResty previously walked its handler registry with `pairs()`, whose order is unspecified;
with per-kind timers that would have let the regions run in a different sequence on every request
and on every edge.

One intentional asymmetry remains: form-field **POST-body** tamper detection is implemented on
OpenResty only. Every edge detects the query-string case, which is the only one active while
`post_body_inspection` is `false` (the default), so the default-config behaviour is uniform.

### Request population

`benchmarks/test.js` issues four `GET`s per iteration so every kind has both phases exercised:

| Request | Detection it fires | Injection it fires |
|---|---|---|
| `GET /` | — | html_comments, http_headers, cookies, decoy_paths |
| `GET /api/login?password=<trigger>` | html_comments | html_comments, http_headers, cookies, decoy_paths |
| `GET /login.html?is_admin=1&probe=<header-kw>` + tampered `Cookie` | form_fields, http_headers, cookies | all five kinds (`/login.html` is the page with a `</form>`) |
| `GET /api/v1/debug` | decoy_paths | html_comments, http_headers, cookies, decoy_paths |

Per 30 s level and per edge this yields equal sample counts across all four edges — verified as
`detect` = iterations for each kind, `inject` = 4× iterations for the `/*` kinds and 1× for
`form_fields`. Because the iteration now carries four requests instead of two, the absolute
html_comments numbers are **not** comparable with runs recorded before this change; all four edges
were re-measured together, so the cross-edge comparison they exist for is intact.

---

## The SQLi trap is outside the timed regions too

The `sql_injection` trap (see `CONTEXT.md`) terminates `POST /api/login` on all four edges. Three
properties keep it from disturbing the leveled measurements:

1. **POST-only.** `benchmarks/test.js` issues nothing but `http.get` (the four requests
   tabulated above). Gating the trap on `methods: ["POST"]` leaves every benchmark request on
   exactly the code path it used before. In particular the `GET` to
   `/api/login` still reaches the origin and still returns nginx's `text/html` 404, so it keeps
   contributing the same injection sample it always did. This is why the Apache edge deliberately
   does **not** add `ProxyPass /api/login !` — that would answer the GET from Apache's own 404
   page, a different-sized body, and shift the injection distribution.
2. **Before both timers.** The trap runs at the top of each edge's request phase, ahead of
   `get_micro_time()` / `r:clock()` / `get_current_time()`. The response-side filters gain only a
   first-line early return, which for a non-owned request is a single nil/flag check.
3. **A non-colliding log label.** The trap logs `WADM SQLI trap build (us):` (edge-prefixed on
   Envoy/WASM/Apache). It shares no substring with either scraper pattern. This matters because
   the OpenResty scraper regex is **unprefixed** — `Detection execution time \(us\):` — so any
   prefixed variant such as `SQLi Detection execution time (us):` would have been matched by it
   and silently corrupted `detection_us`.

**One owned request runs one detector.** An owned request short-circuits *all* other WADM
detection. Enforcing this needs an explicit guard only on Apache, whose `LuaHookAccessChecker` and
`LuaHookFixups` hooks structurally run before the content handler and so cannot be short-circuited
the way the other three edges' single filter can — hence the `sqli.owns(...)` early return at the
top of `detect.lua`'s `handle_detect` and `inject.lua`'s `handle_headers`. Without it a crafted
`POST /api/login?password=<trigger>` would emit an extra alert *and* an extra detection timing line
on Apache alone.

### Response generation: what could not be levelled

The trap is the first WADM feature where an edge generates its own response, and the four runtimes
do not all allow it in the same place. The **body bytes are byte-identical on all four** (verified
by `md5sum`); the differences are in framing and in whether the origin is touched.

| Edge | How the page is emitted | Origin contacted? |
|---|---|---|
| OpenResty | `ngx.print` + `ngx.exit(ngx.HTTP_OK)` in `access_by_lua_block` | No |
| Envoy+Lua | `request_handle:respond()` in `envoy_on_request` | No |
| Apache | `r:puts` + `apache2.OK` from a `LuaMapHandler` | No |
| Envoy+WASM | Detects on the request body, then **rewrites the upstream response** | **Yes** — one round-trip |

The WASM divergence is forced by Envoy's proxy-wasm host, and both alternatives were tried and
measured to fail:

- Returning `Action::Pause` from `on_http_request_headers` stops the stream outright — Envoy never
  delivers `on_http_request_body`, so the request hangs until it times out.
- Returning `Action::Continue` and then calling `send_http_response` from `on_http_request_body`
  fails with `BadArgument` (status 2), which the Rust SDK `unwrap()`s into a VM panic
  (`Function: proxy_on_request_body failed: Uncaught RuntimeError: unreachable`). This happens
  whether or not the data path was paused first.

`send_http_response` *is* legal from `on_http_request_headers`, which is how the trap answers a
body-less `POST`. For the normal case the filter instead stashes the rendered page on the request
side and swaps the status, `Content-Type` and body in `on_http_response_headers` /
`on_http_response_body`. The attacker sees the same bytes; the cost is one round-trip to a
same-network static nginx that would have 404'd anyway.

Envoy+Lua is the mirror-image case and *is* levelled: `request_handle:respond()` is rejected once
`headers_continued_` is set, and buffering with `body()` leaves that flag clear — but
`bodyChunks()` does not. The trap therefore uses `body()` deliberately.

**Framing** also differs on Apache: `httpd.conf`'s `Header always unset Content-Length` is
unconditional, so the trap page has no `Content-Length` and is close-delimited, while the other
three send an explicit length. Scoping that `unset` with an `expr` would fix it but risks the
existing injection path, so it is accepted rather than fixed.

**Response-filter suppression.** Every edge re-enters its own response phase for a locally
generated reply, so each needed an explicit opt-out or the trap page would arrive stamped with the
`DEV-PORTAL` comment, the hidden decoy link, `X-Backend-Server` and `Set-Cookie: admin_ui=0`:
`ngx.ctx.wadm_local_response` (OpenResty), a `local_response` key in the existing
`wadm.honeypot` dynamic-metadata namespace (Envoy+Lua), `self.sqli_page.is_some()` (WASM), and the
two `sqli.owns(...)` guards in `inject.lua` (Apache).
