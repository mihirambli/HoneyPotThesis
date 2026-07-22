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

The single **intended** remaining difference is each runtime's native string primitive
(OpenResty PCRE `ngx.re.sub`, WASM `str::find`, Envoy+Lua / Apache Lua `gsub` with count 1).
All produce the same first-match splice, so the leftover time is the runtime's body-API
cost — which is exactly what the injection subplot is meant to measure.

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
