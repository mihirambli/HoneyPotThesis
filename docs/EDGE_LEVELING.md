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

For a while one difference remained: **OpenResty still used `ngx.re.sub`** (PCRE with JIT
and a cached compile via the `o` flag) while the other three did a plain find-and-splice. That
left OpenResty with the highest body-to-header ratio (3.0×, 3 µs vs 1 µs). It was not only a
cost difference: the `i` flag made OpenResty's anchor match case-insensitive, so `</BODY>` was
found there and missed everywhere else. **This is now closed** — OpenResty uses the same plain,
case-sensitive `string.find` splice as the Lua edges and WASM's byte search (see
[Behavioural equivalence and latency pass](#behavioural-equivalence-and-latency-pass)).

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

## The SQLi trap is measured, in one phase, as a 2×2

The `sql_injection` trap (see `CONTEXT.md`) terminates `POST /api/login` on all four edges. It is
benchmarked alongside the honeytoken kinds, but it is **not** a honeytoken kind and is not
measured like one.

**It has no injection phase.** The trap plants nothing. The login form is the origin's own
`/login.html`, and the hidden input WADM adds to it belongs to `form_fields`. The trap never
mutates a response the origin produced — it watches, scans, and answers. So it has a detection
region and nothing else, and it is excluded from every injection figure and from the injection
pool rather than being drawn as an empty panel.

**Four arms, crossing outcome with encoding.** A hit/miss pair alone confounds two factors that
pull in opposite directions, so the benchmark drives all four combinations:

```
WADM TOKEN sql_injection detect (us): N               -- hit,         plain body
WADM TOKEN sql_injection_encoded detect (us): N       -- hit,         %-encoded body
WADM TOKEN sql_injection_miss detect (us): N          -- no signature match, plain body
WADM TOKEN sql_injection_miss_encoded detect (us): N  -- no match, %-encoded body
```

`sqli_match` returns on first match, so the hit payload (`admin'`, signature #11 of 22, in the
first watch field) stops after 11 comparisons while a non-matching body walks all 22 against `username`
and then all 22 against `password`, 44 comparisons. **That difference is not measurable.** Most
signatures are longer than a real field value — `information_schema` against `alice` — and are
rejected on length before a character is compared, so the scan never really walks the list.

What does cost is decoding: `url_decode` runs over every body pair and again inside
`sqli_normalize`, so a percent-encoded payload pays the substitution path twice. The `%27` form of
each payload decodes to exactly the plain form, so the two arms of one outcome scan identical
bytes and differ only in decoding work, which is what makes the factor attributable.

The `_encoded` suffix is set by checking the raw body for `%` *after* the timer closes, so the
classification never enters the measurement. It is not benchmark scaffolding: percent-encoded
input is an evasion technique the honeypot has independent reason to record.

Four properties keep the trap from disturbing the levelled measurements:

1. **POST-only, and terminal.** The trap is gated on `methods: ["POST"]` and short-circuits before
   any honeytoken detector runs, so its four requests contribute samples to the `sql_injection*`
   kinds alone and never enter the five honeytoken kinds' population. The four benchmark `GET`s
   stay on exactly the code path they used before; in particular the `GET` to `/api/login` still
   reaches the origin and still returns nginx's `text/html` 404, so it keeps contributing the same
   injection sample it always did. This is why the Apache edge deliberately does **not** add
   `ProxyPass /api/login !` — that would answer the GET from Apache's own 404 page, a
   different-sized body, and shift the injection distribution.
2. **Before both html_comments timers.** The trap runs at the top of each edge's request phase,
   ahead of `get_micro_time()` / `r:clock()` / `get_current_time()`. The response-side filters
   gain only a first-line early return, which for a non-owned request is a single nil/flag check.
3. **One boundary, shared by all four arms.** The timed region is `sqli_match` alone. Page
   rendering, the `WADM ALERT` / `WADM TRAP` write, the arm classification and the attacker-IP
   record all sit outside it on every edge. The IP record being outside is a **deliberate
   departure** from the honeytoken kinds, which time it inside their `detect` region: only the hit
   arms perform it, so including it would surface as an outcome difference that has nothing to do
   with the scan. All four edges do this identically, so cross-edge comparability is unaffected.
4. **A non-colliding log label.** The lowercase `detect` word shares no substring with either
   html_comments scraper pattern. This matters because the OpenResty scraper regex is
   **unprefixed** — `Detection execution time \(us\):` — so any prefixed variant such as
   `SQLi Detection execution time (us):` would be matched by it and silently corrupt
   `detection_us`.

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

`send_http_response` *would* be legal from `on_http_request_headers`, but the filter does not call
it there either: a body-less `POST` still needs the same response-phase swap as every other trap
request, so there is no call site for it anywhere in `wasm-filter/src/lib.rs`. The filter stashes
the rendered page on the request side and swaps the status, `Content-Type` and body in
`on_http_response_headers` / `on_http_response_body` in **all** cases. The attacker sees the same
bytes; the cost is one round-trip to a same-network static nginx that would have 404'd anyway.

This is invisible on the microsecond plane — the timed region closes before the round-trip — but it
makes the four `sqli_*_duration` trends **not comparable across edges** on the end-to-end
plane: Envoy+WASM alone pays that hop. `benchmarks/parity_check.py` also drops `POST /api/login`
from its origin-log comparison, which is what lets the parity check pass despite the divergence.

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

## Baseline-config parity (the no-WADM tier)

The internal microsecond timers exist only where WADM runs. A bare edge has no detection or
injection region, so it has no timer to compare — which means the baseline suite
(`benchmarks/run_baseline_benchmark.py`) measures the *other* plane, end-to-end request latency,
and the levelling problem reappears in a new form: four bare configs that differ from each other
in anything but the WADM layer would make `wadm − bare` non-comparable across edges.

The four baseline configs are therefore derived from the WADM ones by deletion only:

| Baseline config | Deleted from its WADM counterpart |
|---|---|
| `nginx/nginx-baseline.conf` | `lua_shared_dict wadm_state`, `init_by_lua_block`, `access_by_lua_block`, `header_filter_by_lua_block`, `body_filter_by_lua_block` |
| `envoy/envoy-baseline.yaml` | the `envoy.filters.http.lua` entry |
| `envoy-wasm/envoy-wasm-baseline.yaml` | the `envoy.filters.http.wasm` entry and its `{{WASM_CONFIG_JSON}}` placeholder |
| `httpd-baseline.conf` | `mod_lua`, `mod_filter`, `mod_headers`, every `Lua*` directive, `SetOutputFilter`, `Header always unset Content-Length` |

Everything else is kept byte-identical: listener port, upstream/cluster definition, worker and
event tuning, `Host` / `X-Forwarded-For` handling, and the logging destinations. Nothing is added
that the WADM config does not also have.

### Same service, not a parallel one

The bare tier is mounted into the **same** Compose service through the `${OPENRESTY_CONF}`,
`${ENVOY_CONF}`, `${ENVOY_WASM_CONF}` and `${HTTPD_CONF}` overrides, whose defaults are the WADM
configs. Service name, container port, image tag, `depends_on` graph and network path are
therefore identical between the two tiers by construction rather than by review. Adding a second
set of services would have reintroduced exactly the class of drift this document exists to record.

### Two differences that are kept on purpose

Both are genuine costs of running WADM and belong inside the measured overhead, so the baseline
configs must *not* reproduce them:

- **Apache framing.** `Header always unset Content-Length` is required with WADM because
  injection changes the body size, so WADM responses are close-delimited while bare ones carry
  their upstream `Content-Length`.
- **Body size.** An injected page is a few hundred bytes larger on every edge.

### The two Envoy baselines are deliberately redundant

`envoy-baseline.yaml` and `envoy-wasm-baseline.yaml` reduce to the same thing — Envoy with only
the router filter. They are kept separate so each edge owns its baseline series, and their overlap
is used as a check: two independent runs of an identical stack that do not land within noise of
each other mean the host was contaminated, not that the edges differ.

### Verifying a bare edge is bare

Per edge, with its baseline config mounted:

```
docker compose logs <service> | grep -c 'execution time (us)'   # 0
docker compose logs <service> | grep -c 'WADM TOKEN'            # 0
docker compose logs <service> | grep -c 'WADM ALERT'            # 0
curl -s  localhost:<port>/ | grep -c 'DEV-PORTAL'               # 0
curl -s  localhost:<port>/ | grep -c 'api/v1/debug'             # 0
curl -sI localhost:<port>/ | grep -ci 'X-Backend-Server\|admin_ui'  # 0
```

and the four benchmark paths must return the same statuses as the WADM tier: `/` 200,
`/login.html` 200, `/api/login?password=…` 404, `/api/v1/debug` 404.

## Upstream connection reuse — tuning parity, not just code parity

Everything above levels the *code* inside the timed regions. This section records the first case
where an edge had to be levelled at the level of **configuration defaults**, and establishes the
rule for future ones.

### The problem

nginx does not reuse upstream connections unless told to. Envoy pools connections per cluster by
default, and Apache's `mod_proxy` reuses backend connections by default. OpenResty was therefore
opening a new TCP connection to the origin for **every** proxied request, and closing it
afterwards, while the edges it is compared against were not.

Measured directly, by sending 100 requests through each *baseline* proxy over a single client
keep-alive connection and counting sockets on port 80 in the proxy's and backend's network
namespaces (`/proc/net/tcp`, state `06` = TIME_WAIT, `01` = ESTABLISHED):

| Bare proxy | Connections closed per 100 requests | Held open afterwards |
|---|---|---|
| OpenResty (before) | **100** | 0 |
| Envoy | 0 | 1 |
| Apache | 0 | 2 |

### The fix

Three directives, all required together, applied to **both** `nginx/nginx.conf` and
`nginx/nginx-baseline.conf` so the WADM and baseline tiers stay identical outside the filter chain:

- `keepalive 32;` in the `upstream` block — creates the idle-connection cache. Per worker
  process, so the real ceiling is `32 × worker_processes`.
- `proxy_http_version 1.1;` — nginx proxies with HTTP/1.0 by default, which has no persistent
  connections, so `keepalive` alone does nothing.
- `proxy_set_header Connection "";` — otherwise the client's `Connection` header is forwarded
  upstream and closes the pooled connection.

### Result

Re-measured with the same method after the change:

| Bare proxy | Connections closed per 100 requests | Held open afterwards |
|---|---|---|
| OpenResty (after) | **0** | 2 (one per worker process) |

Both tiers pool identically, and injection was confirmed still working on the WADM tier. Both
OpenResty tiers were re-run afterwards, as any levelling change requires.

### The rule this establishes

**Edges are compared only when tuned to equivalent behaviour.** A default that differs between
products silently charges one edge for work the others never do, and the resulting number reads
as a property of the product when it is a property of the configuration. Before attributing any
cross-edge difference to design, check whether it comes from a default; if it does, level it and
re-run both tiers of that edge.

### Note on magnitude

The latency cost of this difference was at first believed to be small — a TCP handshake to a
container on the same Docker bridge sounds cheap, and the +3.20 ms penalty originally attributed
to it was traced to a contaminated run. **Re-running after the fix refuted that.** Comparing runs
made on the same day, enabling connection reuse cut the bare-tier median by roughly 0.5-0.8 ms
per request at 1-100 VUs, and at 500 VUs the WADM tier went from 64.75 ms to 5.11 ms while the
share of offered load it sustained rose from 76% to 95%. Connection churn was what made OpenResty
collapse under overload.

The levelling would have been justified regardless of magnitude — the comparison must be sound
whether or not the difference happens to be large — but in this case the magnitude was decisive.

---

## Behavioural equivalence and latency pass

### Why this pass was needed

Two problems, found together while reviewing all four edges for latency at 500 VUs.

**The edges collapsed very differently at 500 VUs.** Median end-to-end latency, WADM versus bare,
from the runs before this pass:

| Edge | bare → WADM median | WADM p90 | throughput ratio |
|---|---|---|---|
| OpenResty | 1.86 → 5.11 ms | 43 ms | 0.95 |
| Envoy+Lua | 4.96 → 12.29 ms | 61 ms | 0.92 |
| Envoy+WASM | 3.26 → 24.47 ms | 382 ms | 0.63 |
| Apache | 4.47 → 85.95 ms | 258 ms | 0.66 |

On a 2-core host running the edge, the origin, k6 and Docker's log pipeline together, CPU per
request decides which edge saturates first, and queueing near saturation grows faster than
linearly. Most of that CPU sat **outside** the µs timers: token loops, enabled checks and string
building repeated on every request, the query parsed twice, per-chunk recomputation, an extra Lua
hook per request, and log lines. None of it shows in the internal figures, all of it shows in the
end-to-end ones.

**The edges did not actually do the same thing.** Every earlier section levelled *what the timers
wrap*; none of them checked that the four implementations produce the same *behaviour*. They did
not:

| Finding | Edge | Why it mattered |
|---|---|---|
| The strip used `gsub("[^&]*"..keyword.."[^&]*&?")`, treating the keyword as a Lua pattern. `-` and `.` in `internal-admin.example.com` are pattern characters, so the benchmark keyword was **detected but never stripped** | Apache | The secret reached the origin; Apache's detection timer measured a failed pattern match rather than a strip |
| Four different html_comments detectors: decoded args + drop + re-encode in hash order (OpenResty, Envoy+Lua); raw `find` on `r.args` (Apache); raw substring on the **whole path** and removal of only the keyword text (WASM) | all | The "same" detection was four different amounts of work with four different results |
| Client IP read from `X-Forwarded-For` / `X-Real-IP`; k6 sends neither | Envoy+Lua, WASM | Every attacker was recorded and reported as `unknown` |
| After the first hit, a "known attacker" warning was logged on **every** later request | Envoy+Lua | An extra log write per request on one edge only — a pure latency tax |
| An extra "attacker IP recorded" line per hit | OpenResty | Different alert volume per hit |
| Case-insensitive PCRE splice (`ngx.re.sub(..., "io")`) | OpenResty | Different match semantics and cost from the plain find everywhere else |
| Cookie and form-field detectors `return`ed after a tamper hit, skipping the keyword check | OpenResty | Different hits on the same request |
| One form-field hit per mismatching duplicate parameter | WASM | Different alert count |
| Decoy header written with `add` (duplicates an upstream header) instead of set | Envoy+Lua | Different response headers |
| Body decoded as UTF-8 before splicing; non-UTF-8 pages were skipped | WASM | Different injection coverage, plus a validation pass per response |

### The canonical mechanism

All four edges now implement exactly this, and `benchmarks/parity_check.py` verifies it:

- **Per-request setup (untimed, once).** Client IP = the socket peer (`$remote_addr`,
  `r.useragent_ip`, Envoy stream info / `source.address`, port stripped). Path = the raw request
  target up to `?`, used for every path decision (plan lookup, decoy match, keyword surface, SQLi
  ownership) — raw because it is identical by construction on all four and needs no decoding;
  OpenResty's `$uri` and Apache's `r.uri` are decoded and normalised, which the Envoy edges cannot
  reproduce. Query = ordered `&`-separated segments `{raw, key, value}`, `+`/`%XX`-decoded only
  when the segment contains `%` or `+`.
- **Precompiled per-path plans** built once from the config: paths are only `/*` or exact, so each
  request resolves to one plan (joined comments, decoy headers, prebuilt `Set-Cookie` strings,
  extra body payloads, in config order). Kinds with nothing to detect are skipped, timer included.
- **Detection.** Additional kinds in fixed order, one timer each (unchanged contract). A kind's
  tamper and replay checks are both evaluated; `form_fields` raises one hit for the first
  mismatching submission. **html_comments**: a segment whose decoded key or value contains a
  trigger is dropped, and the query is rebuilt from the **raw** text of the kept segments — so
  order and original encoding survive — and written back. Timed region: scan → strip → write
  back → record IP, on every edge; the query parse moved *out* of the timer on OpenResty and
  Envoy+Lua, where it used to sit inside.
- **One alert format:** `WADM ALERT: honeytoken triggered by <ip> — keyword '<kw>' found in query
  param '<k>=<v>'`, with CR/LF in attacker-controlled fields replaced so they cannot forge log lines.
- **Injection.** Header kinds: set the decoy header, append `Set-Cookie`. Body (HTML only, plan has
  body work): buffer and assemble untimed; each extra payload spliced under its own timer; the
  html_comments splice + write-back under the benchmarked timer. One code path on every edge (the
  duplicated "no extra payloads" branch is gone), one primitive (plain case-sensitive find + splice).
- **SQLi trap:** unchanged, plus a `WADM TRAP` line for an owned request that matches no signature
  (see logging below).

### Latency changes per edge

| Edge | Change | Why |
|---|---|---|
| all | Precompiled per-path plans, trigger list and detector token lists | The per-request token loops, `enabled` checks and markup/cookie concatenation were CPU spent on every request and every response |
| all | Query parsed once into segments, decoding only where needed | OpenResty and Envoy+Lua parsed it twice (once inside the html_comments timer); a segment with no `%`/`+` needs no decode |
| OpenResty | All Lua moved into one `wadm` table built in `init_by_lua`; phase blocks are one-line calls | Locals/upvalues instead of a dozen globals; one place for the logic |
| OpenResty | Body filter does one `ngx.ctx` lookup and an append per chunk | Token loops, the `Content-Type` read and `ngx.re.find` used to run on **every** chunk |
| OpenResty, Envoy+Lua | Preallocated `struct timeval` | `ffi.new` allocated a cdata on every timer read |
| Envoy+Lua | Per-request "known attacker" lookup and log line removed | One extra log write per request, on this edge only |
| Envoy+Lua | `content-length` replace after `setBytes` dropped | `setBytes` already rewrites it (verified: header equals body length) |
| Apache | Shared `wadm.lua` core; `sqli.lua` folded in | Each script file has its own Lua state, and each parsed `config.json` (twice, via `sqli.lua`) and redefined the same helpers |
| Apache | Response-header staging moved from a `LuaHookFixups` hook into the access-checker hook | `err_headers_out` survives `ProxyPass` from either phase; every Lua hook costs a VM lookup and request-object setup per request |
| Apache | `LuaCodeCache forever` | The default (`stat`) stats every script on every hook call; the other edges load their code once (tuning parity) |
| Apache | Own segment parser over raw `r.args` instead of `r:parseargs()`; `r.*` fields read once | Identical parsing to the other edges, fewer C-boundary crossings |
| WASM | `Rc<Compiled>` built in `on_configure` | Replaces per-request `is_on`, `format!` and string clones |
| WASM | Fewer host calls: `:path` read once (was twice), IP from one property read (was two missing header reads), `:method` only on trap paths, `cookie` only if a cookie token exists | Every header read crosses the V8 ↔ Envoy boundary |
| WASM | Byte-level splice | No UTF-8 validation pass; non-UTF-8 pages are injected like on the Lua edges |
| WASM | `[profile.release]` LTO, one codegen unit, `panic = "abort"` | Whole-program optimisation of the hot paths; no unwinding machinery |

### Logging and forwarding parity

This applies the tuning-parity rule from
[Upstream connection reuse](#upstream-connection-reuse--tuning-parity-not-just-code-parity) to
logging:

- **Edge access logs removed** (OpenResty's implicit `access_log`, Apache's `CustomLog`), in both
  the WADM and the baseline configs. Envoy never wrote one, so OpenResty and Apache paid a
  per-request write the Envoy edges did not.
- **What they recorded is kept elsewhere.** They held the client IP, request line and status per
  request. For proxied requests the origin's access log now carries the client IP
  (`xff="$http_x_forwarded_for"`), and all four edges forward it: OpenResty sets
  `X-Forwarded-For`, Apache's mod_proxy adds it, and both Envoy configs (WADM and baseline) now set
  `use_remote_address: true` — which also pins Envoy's downstream address to the socket peer, so a
  client-supplied XFF cannot spoof the WADM IP. Requests the edge answers itself (the SQLi trap)
  never reach the origin; a signature hit logs `WADM ALERT` as before, and every other owned
  request now logs `WADM TRAP: <ip> <METHOD> <path> answered locally with <status> (no signature
  matched)` on all four edges. The label shares no substring with any scraper regex.
- **Timing lines unchanged.** The per-region timing lines are benchmark instrumentation that the
  end-to-end WADM tier also pays; they were deliberately left as they are, so every scraper,
  result schema and plot keeps working.

### Integration fixes

- `run_internal_wasm_benchmark.py` now starts the stack with `up -d --build`. Without it Compose
  reuses any existing `rust-builder` image, so an edit to `wasm-filter/` would have been benchmarked
  against the previously compiled `filter.wasm`.
- `wasm-filter/Cargo.lock` (extracted from the image that produced the earlier results) is copied in
  and built with `--locked`, so a rebuild cannot silently pull newer crate versions.

### What this means for the measurements

- The html_comments **detection** region now excludes the query parse on every edge (it was inside
  on OpenResty and Envoy+Lua), and on Apache and WASM it now does the segment scan and strip the
  others do. The html_comments **injection** region is the single consolidated path (the one the
  benchmark already exercised, since the decoy link on `/*` sent every page down the "extra" branch);
  OpenResty's splice changed from PCRE to a plain find. Header-kind inject timers no longer contain
  cookie-string building on any edge.
- Sample counts are unchanged: `detect` = iterations per kind, `inject` = 4 × iterations for the `/*`
  kinds and 1 × for `form_fields`.
- **Results from before this pass are not comparable** with results after it. Every WADM and bare
  tier was re-run together.

### Residual differences, left as they are

| Residual | Why it is left |
|---|---|
| Response framing: Envoy+Lua sends an exact `Content-Length`; the others go chunked | `setBytes` runs before Envoy sends headers; the other three cannot know the final length at header time. Not part of the detection/injection mechanism |
| Apache forwards `path?` when every parameter is stripped | mod_lua can only assign `r.args` a string, and mod_proxy appends `?` whenever args is non-NULL. The origin serves the same resource |
| Apache sets `Set-Cookie` with `apr_table_set`, so several cookie tokens on one path would overwrite each other | mod_lua exposes no `add` for `err_headers_out`; the shipped config plants one cookie |
| The Envoy+WASM SQLi trap contacts the origin | proxy-wasm host constraints — see [The SQLi trap is measured, in one phase, as a 2×2](#the-sqli-trap-is-measured-in-one-phase-as-a-22). Affects the end-to-end POST figures only; the timed region closes first |
| `sql_injection` leaves the attacker-IP record outside its timed region, unlike the honeytoken kinds | Only the hit arms perform that write; including it would surface as an outcome difference unrelated to the scan. Identical on all four edges |
| POST-body inspection exists on OpenResty and Envoy+Lua only | Off by default (`post_body_inspection: false`); aligned between those two edges and kept for a future iteration that adds it to all four |
| Rust decodes query values with `from_utf8_lossy` | Only differs for invalid UTF-8 after decoding; identical for all ASCII input |

### Results

All four WADM tiers, all four bare tiers and the origin floor were re-run in **one session**
(2026-09-18). Every tier sustained ≥ 97% of offered load at VU ≤ 100, and origin ≤ bare ≤ WADM
held for every edge at every level. The "before" figures come from the previous session, whose
origin floor was slower (500 VUs: 2.86 → 1.34 ms), so raw medians are not compared across
sessions. The comparison below is **WADM − bare**, the latency WADM itself adds, in ms (median
`http_req_duration`):

| Edge | VU 1 | VU 10 | VU 100 | VU 500 | 500-VU WADM p90 | 500-VU throughput |
|---|---|---|---|---|---|---|
| OpenResty | 0.42 → 0.22 | 0.52 → 0.07 | 0.72 → 1.30 (re-check **0.56**) | 3.25 → 3.53 (re-check **1.76**) | 43 → 59 (re-check 30) | 0.95 → 0.93 (re-check 0.95) |
| Envoy+Lua | 0.29 → 0.34 | 1.58 → **0.28** | 3.75 → **1.49** | 7.33 → 8.65 | 61 → 58 | 0.92 → 0.92 |
| Envoy+WASM | 0.24 → 0.61 | 1.64 → **0.16** | 1.32 → 1.58 | 21.21 → **8.26** | 382 → **65** | 0.63 → **0.91** |
| Apache | 0.43 → 0.56 | 0.76 → 0.38 | 1.04 → 1.10 | 81.48 → **50.59** | 258 → 244 | 0.66 → 0.66 |

How to read it:

- **Clear effects.** Envoy+WASM's 500-VU collapse is gone: p90 fell from 382 to 65 ms and
  sustained load rose from 63% to 91%, level with Envoy+Lua on the same Envoy. Apache's 500-VU
  overhead fell by ~31 ms. Envoy+Lua's overhead at 10 and 100 VUs fell by 1.3 and 2.3 ms; the
  per-request "known attacker" log line and the in-timer re-encode were the obvious costs removed.
- **OpenResty's first run was host noise.** It ran first, straight after other heavy Docker
  activity on the host, and its 100-VU level reached only 97% throughput. An immediate
  back-to-back re-run of its WADM and bare tiers gave 0.56 ms at 100 VUs and 1.76 ms at 500 VUs,
  both better than before. Its internal timings were equal or lower at every level in both runs.
- **Sub-millisecond differences are within noise.** This host's run-to-run spread at ≤ 100 VUs is
  already known to exceed some between-edge differences (see `docs/THESIS_NOTES.md`, 2026-09-17),
  and every figure here is one run.

Internal medians (µs, VU 100), before → after, for the regions whose content changed:

| Region | OpenResty | Envoy+Lua | Envoy+WASM | Apache |
|---|---|---|---|---|
| html_comments detect | 2 → 2 | 5 → 2 | 3 → 2 | 10 → 4 |
| http_headers detect | 1 → 1 | 1 → 0 | 1 → 1 | 6 → 3 |
| decoy_paths inject (body splice) | 2 → 1 | 1 → 1 | 1 → 1 | 3 → 4 |
| cookies inject (header write) | 1 → 0 | 1 → 1 | 2 → 1 | 2 → 1 |

The html_comments detection figures now measure the same region on all four edges: the query
parse sits outside the timer, and the scan, strip, write-back and IP record sit inside.

### Apache still saturates at 500 VUs — why

Sampled during a 30-second, 500-VU run against the WADM Apache tier:

| | Idle | Under load |
|---|---|---|
| httpd processes | 4 | up to 17 |
| httpd threads | 82 | up to 408 |
| distinct PIDs that logged WADM lines | — | **34** |

34 distinct PIDs against a peak of 17 concurrent processes means the event MPM's default spare-
thread management (`MaxSpareThreads 250`) is **killing and re-spawning** processes mid-run. Every
new thread creates fresh Lua states and re-parses `config.json` in PUC Lua 5.1 on first use. The
other three edges create all their workers once at startup. This is a configuration-default
difference rather than WADM logic, so it is not changed here. The proposed fix is to pre-spawn the
full pool in **both** `httpd.conf` and `httpd-baseline.conf` (for example `StartServers` =
`ServerLimit`, and `MaxSpareThreads` ≥ `MaxRequestWorkers`), then re-run both Apache tiers.

### Verification

- `benchmarks/parity_check.py`: **identical on all four edges**, compared against OpenResty. That
  covers 13 probes, 12 alert/trap lines and 11 origin requests, including the percent- and
  plus-encoded keywords, the order-preserving strip, and both SQLi paths.
- Every alert line shows the real peer IP, never `unknown`. No edge writes an access-log line. The
  origin log shows `xff=<client>` for all four edges. Envoy+Lua's `Content-Length` equals the body
  length after `setBytes`.
- Integration smoke test (VU 1/10, 10 s) through all four WADM runners, all five baseline tiers
  and all three plotters: every runner scraped non-empty samples with the unchanged ratios
  (`detect` = iterations, `inject` = 4× / 1×), and every plot rendered.
- Builds: `cargo build --locked --release` with no warnings; `openresty -t`, `httpd -t` and
  `envoy --mode validate` pass for every WADM and baseline config; every Lua file parses.
