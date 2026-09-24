<!-- benchmarks/README.md: external k6 load generator for WADM edge benchmarking; lives outside any edge profile so it never auto-runs with a normal `up`. -->
# benchmarks (k6 load generator)

External request-latency probe for the WADM edge proxies. A `grafana/k6` container joins the `honeypot` Docker network and hits whichever edge is selected via the `TARGET` env var. One iteration issues four `GET`s and four `POST`s, chosen so that **every deception feature in `config.json` has every path it owns exercised**:

| Request | k6 Trend metric | Detection fired | Injection fired |
|---------|-----------------|-----------------|-----------------|
| `GET ${TARGET}/` | `inject_get_duration` | — | html_comments, http_headers, cookies, decoy_paths |
| `GET ${TARGET}/api/login?password=${TRIGGER_KEYWORD}` | `detect_query_duration` | html_comments | html_comments, http_headers, cookies, decoy_paths |
| `GET ${TARGET}${FORM_PAGE}?${FORM_FIELD}=1&probe=${HEADER_KEYWORD}` with a tampered `Cookie` | `token_tamper_duration` | form_fields, http_headers, cookies | all five kinds |
| `GET ${TARGET}${DECOY_PATH}` | `token_decoy_duration` | decoy_paths | html_comments, http_headers, cookies, decoy_paths |
| `POST ${TARGET}/api/login` with `${SQLI_HIT_BODY}` | `sqli_hit_duration` | sql_injection (hit, plain) | — |
| `POST ${TARGET}/api/login` with `${SQLI_HIT_ENC_BODY}` | `sqli_hit_enc_duration` | sql_injection (hit, `%`-encoded) | — |
| `POST ${TARGET}/api/login` with `${SQLI_MISS_BODY}` | `sqli_miss_duration` | sql_injection (no match, plain) | — |
| `POST ${TARGET}/api/login` with `${SQLI_MISS_ENC_BODY}` | `sqli_miss_enc_duration` | sql_injection (no match, `%`-encoded) | — |

The custom `Trend`s keep the eight calls distinguishable; the built-in `http_req_duration` pools them into one distribution.

`sql_injection` fires no injection because it plants nothing — the login form is the origin's own page, and the hidden input WADM adds to it belongs to `form_fields`. It is the one feature measured in a single phase; see [The SQLi trap](#the-sqli-trap-a-detection-only-feature-with-two-arms).

These two things are measured on **different planes** and must not be confused:

- The `Trend`s measure **end-to-end request latency** (milliseconds). They are published by `handleSummary` and recorded in the `e2e_*.json` files — this is the plane on which a WADM run can be compared against a no-WADM [baseline](#baseline-no-wadm-benchmarking).
- The per-phase, per-kind numbers behind the honeytoken figures are **microseconds** measured by the edges' own internal timers and scraped from their logs (see below). They exist only where WADM runs.

The third request deliberately bundles three kinds: each edge times each kind in its own region, so bundling costs nothing in attribution while keeping the iteration short.

The four `POST`s are the only non-`GET` requests, because the trap is gated on `POST /api/login`. On OpenResty, Envoy+Lua and Apache the trap answers them from the request phase, so WADM *saves* these requests the origin round-trip the bare tier pays and their end-to-end deltas are negative — which is why the overhead decomposition sums per-request-type deltas instead of scaling the pooled median.

## Why the keyword is in the query string

All four edges inspect the request **query string**, and they parse and strip it the same way (see invariant 9 under [Cross-edge comparability](#cross-edge-comparability)). Putting `TRIGGER_KEYWORD` in `?password=...` therefore exercises the same detection path on every edge, so `detect_query_duration` is comparable across all four. The same reasoning drives the `form_fields` and `http_headers` surfaces in request 3 — both are query-string checks on every edge.

POST-body inspection still exists in OpenResty (`nginx/nginx.conf`) and Envoy+Lua (`envoy_scripts/injection.lua`); it is intentionally left in place for a follow-up iteration that raises Apache and Envoy+WASM to the same level (by implementing body inspection in `apache_scripts/detect.lua` and `wasm-filter/src/lib.rs`) and re-introduces a `detect_body_duration` scenario alongside this one.

## Data flow

```mermaid
flowchart LR
  k6[load-tester k6 container]
  Edge["Edge proxy (TARGET)"]
  Backend[backend nginx]

  k6 -->|"4 GETs per iteration (see table above)"| Edge
  Edge --> Backend
  Backend --> Edge
  Edge --> k6
```

The load-tester runs in-network, so it resolves edges by Compose service name. No host-port hop, no Docker NAT — measurements reflect proxy + backend cost, not the host networking stack.

## Running

The service is gated behind the `loadtest` profile and never starts with a plain edge `up`. Combine `loadtest` with whichever edge profile you want to benchmark, and override `TARGET` to match that edge's in-network address (each listens on a different container port):

```bash
# OpenResty edge (in-network port 80)
docker compose --profile openresty --profile loadtest up --abort-on-container-exit

# Envoy + Lua edge (in-network port 8080)
TARGET=http://envoy:8080 \
  docker compose --profile envoy --profile loadtest up --abort-on-container-exit

# Envoy + WASM edge (in-network port 8080, service name envoy-wasm)
TARGET=http://envoy-wasm:8080 \
  docker compose --profile wasm --profile loadtest up --abort-on-container-exit

# Apache + mod_lua edge (in-network port 80)
TARGET=http://apache:80 \
  docker compose --profile apache --profile loadtest up --abort-on-container-exit
```

`--abort-on-container-exit` stops the edge once k6 finishes its run so you don't have to `down` manually.

### Tunables

All passed through environment variables on the host (read by Compose, then forwarded into the container):

| Var | Default | Effect |
|-----|---------|--------|
| `TARGET` | `http://openresty:80` | Base URL the script hits. Must be an in-network address. |
| `TRIGGER_KEYWORD` | `internal-admin.example.com` | Substring placed in the query string to fire the html_comments detection phase. Should match a `trigger_keyword` in `config.json`. |
| `HEADER_KEYWORD` | `app-07.internal.example.com` | Planted `http_headers` value, replayed in the query to fire that kind's detection. |
| `COOKIE_NAME` / `COOKIE_TAMPER_VALUE` | `admin_ui` / `1` | Bait cookie sent back with a value that differs from the planted `cookie_value`, firing the tamper check. |
| `DECOY_PATH` | `/api/v1/debug` | Trap URI requested to fire `decoy_paths` detection. |
| `FORM_PAGE` / `FORM_FIELD` / `FORM_TAMPER_VALUE` | `/login.html` / `is_admin` / `1` | Page carrying the hidden input (it must contain a `</form>`) and the tampered value submitted in the query string. |
| `K6_VUS` | `5` | Concurrent virtual users. |
| `K6_DURATION` | `30s` | Run length. |

All the honeytoken defaults mirror `config.json`; override them if the token definitions change.

## Internal OpenResty profiling automation

Use the orchestrator to benchmark **internal** Lua execution timings logged by OpenResty:
- `Detection execution time (us): ...`
- `Injection execution time (us): ...`

The script runs four VU levels (`1,10,100,500`) sequentially and prints per-VU stats for each phase (`count, min_us, avg_us, p90_us, max_us`).

```bash
python3 benchmarks/run_internal_openresty_benchmark.py
```

Optional overrides:

```bash
K6_VUS_LIST=1,10,100,500 \
K6_DURATION=30s \
K6_START_DELAY=5s \
TARGET=http://openresty:80 \
TRIGGER_KEYWORD=internal-admin.example.com \
python3 benchmarks/run_internal_openresty_benchmark.py
```

Result artifacts:
- `benchmarks/results/internal_openresty_profile.json` — per-VU summary stats.
- `benchmarks/results/internal_openresty_raw.json` — raw per-request latencies (see [Raw sample files](#raw-sample-files)).

The `openresty` container stays up between VU levels — only the `load-tester` is cycled, and each VU level's logs are isolated with `docker compose logs --since` — so OpenResty/LuaJIT is not cold-started for each level and every VU level produces distinct measurements.

If Docker is unavailable, the script exits after reporting the failed compose command output; no benchmark stats are produced for that VU.

## Internal Envoy Lua profiling automation

Use the orchestrator to benchmark **internal** Lua execution timings logged by the Envoy Lua filter (`envoy_scripts/injection.lua`):
- `Envoy Lua Detection execution time (us): ...`
- `Envoy Lua Injection execution time (us): ...`

The script runs four VU levels (`1,10,100,500`) sequentially and prints per-VU stats for each phase (`count, min_us, avg_us, p90_us, max_us`). The `envoy` container stays up between VU levels — only the `load-tester` is cycled — so Envoy is not cold-started for each level.

```bash
python3 benchmarks/run_internal_envoy_lua_benchmark.py
```

Optional overrides:

```bash
K6_VUS_LIST=1,10,100,500 \
K6_DURATION=30s \
K6_START_DELAY=5s \
TARGET=http://envoy:8080 \
TRIGGER_KEYWORD=internal-admin.example.com \
python3 benchmarks/run_internal_envoy_lua_benchmark.py
```

Result artifacts:
- `benchmarks/results/internal_envoy_lua_profile.json` — per-VU summary stats.
- `benchmarks/results/internal_envoy_lua_raw.json` — raw per-request latencies (see [Raw sample files](#raw-sample-files)).

## Internal Apache mod_lua profiling automation

Use the orchestrator to benchmark **internal** Lua execution timings logged by the Apache `mod_lua` handler (`apache_scripts/detect.lua`):
- `Apache Detection execution time (us): ...`
- `Apache Injection execution time (us): ...`

The script runs four VU levels (`1,10,100,500`) sequentially and prints per-VU stats for each phase (`count, min_us, avg_us, p90_us, max_us`). The `apache` container stays up between VU levels — only the `load-tester` is cycled — so Apache is not cold-started for each level.

```bash
python3 benchmarks/run_internal_apache_lua_benchmark.py
```

Optional overrides:

```bash
K6_VUS_LIST=1,10,100,500 \
K6_DURATION=30s \
K6_START_DELAY=5s \
TARGET=http://apache:80 \
TRIGGER_KEYWORD=internal-admin.example.com \
python3 benchmarks/run_internal_apache_lua_benchmark.py
```

Result artifacts:
- `benchmarks/results/internal_apache_lua_profile.json` — per-VU summary stats.
- `benchmarks/results/internal_apache_lua_raw.json` — raw per-request latencies (see [Raw sample files](#raw-sample-files)).

## Internal WASM profiling automation

Use the orchestrator to benchmark **internal** Rust WASM execution timings logged by `envoy-wasm`:
- `WASM Detection execution time (us): ...`
- `WASM Injection execution time (us): ...`

The script runs four VU levels (`1,10,100,500`) sequentially and prints per-VU stats for each phase (`count, min_us, avg_us, p90_us, max_us`).

```bash
python3 benchmarks/run_internal_wasm_benchmark.py
```

Optional overrides:

```bash
K6_VUS_LIST=1,10,100,500 \
K6_DURATION=30s \
K6_START_DELAY=5s \
TARGET=http://envoy-wasm:8080 \
TRIGGER_KEYWORD=internal-admin.example.com \
python3 benchmarks/run_internal_wasm_benchmark.py
```

Result artifacts:
- `benchmarks/results/internal_wasm_profile.json` — per-VU summary stats.
- `benchmarks/results/internal_wasm_raw.json` — raw per-request latencies (see [Raw sample files](#raw-sample-files)).

The runner starts the stack with `docker compose --profile wasm up -d --build`, so each run recompiles the filter from the current `wasm-filter` sources. Without `--build`, Compose silently reuses the previously built `rust-builder` image and its old `filter.wasm`.

## Raw sample files

Each internal benchmark writes two artifacts per edge:

| File | Contents |
|------|----------|
| `internal_<edge>_profile.json` | Per-VU **summary** stats (`count, min_us, avg_us, p90_us, max_us`) for each phase, plus a `tokens` section with the same stats per honeytoken kind and a `throughput` section recording achieved vs. achievable iterations. Small, human-readable. |
| `internal_<edge>_raw.json` | Per-VU **raw** per-request latency arrays: `runs[].detection_us[]`, `runs[].injection_us[]`, and the same samples split per kind under `runs[].tokens`. All in microseconds. |

The raw file exists so that box plots can be drawn from the true latency distribution (real quartiles), which the summary stats alone cannot reconstruct. Shape:

```json
{
  "metadata": { "...": "same as the profile file, plus a note field" },
  "runs": [
    {
      "vus": 1,
      "detection_us": [97, 41, ...],
      "injection_us": [26, 8, ...],
      "tokens": {
        "html_comments": { "detect_us": [97, 41, ...], "inject_us": [26, 8, ...] },
        "http_headers":  { "detect_us": [...], "inject_us": [...] },
        "cookies":       { "detect_us": [...], "inject_us": [...] },
        "decoy_paths":   { "detect_us": [...], "inject_us": [...] },
        "form_fields":   { "detect_us": [...], "inject_us": [...] },
        "sql_injection":              { "detect_us": [...], "inject_us": [] },
        "sql_injection_encoded":      { "detect_us": [...], "inject_us": [] },
        "sql_injection_miss":         { "detect_us": [...], "inject_us": [] },
        "sql_injection_miss_encoded": { "detect_us": [...], "inject_us": [] }
      }
    }
  ]
}
```

`tokens.html_comments` repeats `detection_us` / `injection_us` verbatim, so every kind can be queried through one uniform path while the top-level keys stay where the older plots expect them.

## Per-honeytoken-kind timing

Every edge times each honeytoken kind in its own microsecond region and logs:

```
WADM TOKEN <kind> detect (us): N
WADM TOKEN <kind> inject (us): N
```

for `kind` in `http_headers | cookies | decoy_paths | form_fields`. `html_comments` keeps its original `Detection/Injection execution time (us)` lines and is folded into the same `tokens` structure by the orchestrators.

The SQLi trap uses the same family but emits `detect` only, under four kind names crossing outcome with encoding — `sql_injection[_encoded]` on a signature hit, `sql_injection_miss[_encoded]` when nothing matched, with the `_encoded` suffix set when the request body required percent-decoding. The scraper regex accepts them unchanged (`\w+` already covers the underscore).

The lowercase `detect` / `inject` words are deliberate: they share no substring with either html_comments scraper pattern, so OpenResty's **unprefixed** `Detection execution time \(us\):` regex cannot match a per-kind line and silently corrupt `detection_us`.

What each timer wraps, and why the html_comments numbers are unaffected, is documented in [EDGE_LEVELING.md](../docs/EDGE_LEVELING.md#additional-honeytoken-kinds-get-their-own-timed-regions). In short: per-request setup (client IP, URI, parsed query, `Cookie`) is read once outside every timer; a `detect` timer wraps that kind's scan + in-memory IP record and fires only on a hit, with alert rendering and the log write happening *after* it closes; an `inject` timer wraps the header write or the anchor-locate-and-splice, with token selection and markup construction hoisted out.

Because an iteration now carries eight requests rather than four, absolute html_comments numbers are **not** comparable with runs recorded before this change. All four edges are re-measured together, so the cross-edge comparison remains valid.

## Baseline (no-WADM) benchmarking

Everything above measures the **internal microsecond cost** of a detect/inject region. That says
what a honeytoken operation costs in isolation; it cannot say what deploying WADM costs a
*request*, because there is nothing to compare it against. The baseline suite supplies that
comparison.

### The three tiers

| Tier | Stack | Isolates | Written by |
|------|-------|----------|------------|
| `origin` | k6 → `backend` | the floor: network + origin only | `run_baseline_benchmark.py --edge origin` |
| `bare` | k6 → edge (no WADM) → `backend` | proxy cost (`bare − origin`) | `run_baseline_benchmark.py --edge <edge>` |
| `wadm` | k6 → edge (WADM) → `backend` | full cost (`wadm − bare`) | `run_internal_<edge>_benchmark.py` |

A bare edge runs no detection or injection code, so it has **no internal timers**. End-to-end
request latency — which k6 reports for every tier — is therefore the only plane on which baseline
and WADM are comparable, and it is the plane the comparison figures use. `test.js` computed those
numbers all along; they are now captured instead of discarded.

**Units.** End-to-end results are in **milliseconds** (k6's native unit); internal timers are in
**microseconds**. Both stay in their native unit on disk and are converted only at plot time.

### How the tiers stay comparable

- **Same k6 script, byte for byte.** All three tiers run the four-GET iteration unchanged. In the
  bare and origin tiers the keyword requests simply match nothing, and `/api/login` and
  `/api/v1/debug` still 404 (already covered by `ALLOW_404`). Same paths, same headers, same
  arrival rate — only the WADM layer differs.
- **Same VU ladder** (`1, 10, 100, 500`), duration, start delay, warm-up burst and
  `wait_for_quiet_host` cool-down, all shared through `wadm_timings.py`. `sleep(1)` makes VU
  1/10/100 fixed-arrival-rate levels, which is the regime a latency comparison needs; VU=500 is
  where this repo's 2-core host saturates.
- **Same Compose service.** The bare tier swaps only the mounted edge config, via the
  `${OPENRESTY_CONF}` / `${ENVOY_CONF}` / `${ENVOY_WASM_CONF}` / `${HTTPD_CONF}` overrides whose
  defaults are the WADM configs. Service name, port, image and network path are unchanged.
  What each baseline config may and may not contain is recorded in
  [EDGE_LEVELING.md](../docs/EDGE_LEVELING.md#baseline-config-parity-the-no-wadm-tier).

### How k6's numbers get out

`handleSummary` in `test.js` prints **one line** per run:

```
WADM K6 SUMMARY {"iterations":…,"http_reqs":…,"trends":{…}}
```

which the runners scrape from `docker compose logs load-tester --since <ts>` — the same transport
the edges already use for their internal timings, so no writable mount is needed. It must stay on
one line because `docker compose logs` prefixes every line with `load-tester-1  | `; anchoring on
the sentinel and taking the rest of the line makes that prefix harmless.

`options.summaryTrendStats` asks k6 for `p(5)`, `p(25)`, `p(75)` and `p(95)` on top of its
defaults. Those quartiles are what let the figures draw a **true** box without dumping raw
samples — a `--out json` at 500 VUs would be hundreds of megabytes.

### Running

```bash
# every bare tier plus the origin floor (~25 min on a 2-core host)
python3 benchmarks/run_baseline_benchmark.py --all

# or one at a time
python3 benchmarks/run_baseline_benchmark.py --edge openresty
python3 benchmarks/run_baseline_benchmark.py --edge origin
```

`--edge` accepts `openresty | envoy_lua | wasm | apache_lua | origin`. Optional overrides:
`--vus 1,10,100,500`, `--duration 30s`, `--start-delay 5s`, `--trigger <keyword>` (each also
readable from the corresponding `K6_*` / `TRIGGER_KEYWORD` env var).

The four WADM orchestrators must be re-run to produce their `e2e_*_wadm.json` counterparts —
runs recorded before end-to-end capture existed have no `e2e` file and are skipped by the
comparison plotter.

### Result artifacts

One file per (edge, tier), all in **milliseconds**:

```
benchmarks/results/e2e_<edge>_bare.json    edge ∈ openresty | envoy_lua | apache_lua | wasm
benchmarks/results/e2e_<edge>_wadm.json
benchmarks/results/e2e_origin_bare.json
```

```jsonc
{
  "metadata": { "generated_at", "note", "script", "edge", "tier", "target",
                "edge_config", "duration", "start_delay", "vus_list" },
  "runs": [
    {
      "vus": 1,
      "throughput": { "iterations", "expected_iterations", "throughput_ratio", "expected_reachable" },
      "k6": {
        "iterations": 30, "http_reqs": 120, "http_req_failed_rate": 0.0,
        "trends": {
          "http_req_duration":     { "count", "min", "p5", "p25", "med", "p75", "p90", "p95", "max", "avg" },
          "inject_get_duration":   { "…": "…" },
          "detect_query_duration": { "…": "…" },
          "token_tamper_duration": { "…": "…" },
          "token_decoy_duration":  { "…": "…" }
        }
      },
      "errors": { "compose_stderr", "logs_stderr" }
    }
  ]
}
```

The `throughput` section keeps the shape it has in the profile files, but its iteration count now
comes from k6 rather than from counting scraped detection lines — the direct measure, and the only
one a bare tier can produce. In the WADM files `detection.count` remains as the cross-check.

## Baseline-vs-WADM plots

```bash
# defaults to benchmarks/results/, writes PNGs to benchmarks/results/plots/
python3 benchmarks/plot_baseline_comparison.py
```

| File | Layout |
|------|--------|
| `e2e_comparison_vus_<N>.png` (one per VU level) | The three tiers side by side: a no-proxy box, then per edge a bare box and a WADM box, all pooled over the iteration's four requests (`http_req_duration`). The no-proxy median is extended across the panel as a rule so every box reads as a height above the floor. |
| `e2e_phase_overhead_vus_<N>.png` (one per VU level) | Latency WADM adds (`median WADM − median bare`) per request type, grouped by edge. The leftmost group is injection-only; the other three add a detection hit on top of the same injection. |
| `e2e_overhead_scaling.png` | Median and p95 `http_req_duration` vs. VU level. Solid line = WADM, dashed = bare, dotted grey = origin. The gap between an edge's two lines is the overhead. |
| `wadm_overhead_breakdown.png` | Per edge, per VU: measured end-to-end overhead per iteration, with the portion the internal timers account for overlaid and written out as a percentage. |

Boxes here are **real** percentiles (box = Q1–Q3, whiskers = p5–p95), so they are drawn solid —
the hatched style stays reserved for the summary approximations in the internal-timer figures.
Tier is encoded by **fill** (hollow = WADM absent, filled = WADM active), never by hue, because
hue follows the edge across every figure in this repo.

### Reading the breakdown figure

```
measured_ms  = 4 × (median http_req_duration with WADM − without)     # 4 requests per iteration
accounted_ms = Σ over kinds and phases of (median op cost × ops fired) ÷ iterations ÷ 1000
```

The gap between them is **not** measurement error. It is real WADM cost the internal timers
exclude by design (invariants 5–8 above): config parse, per-request setup, response-body
buffering, the content-type guard, and the `WADM ALERT` log writes that were deliberately moved
outside the timers. Expect it to dominate, especially on the buffering edges. Splitting it further
would need a third tier — WADM loaded with every honeytoken `enabled: 0` — which is not currently
measured.

A **negative** bar means the bare tier measured slower than the WADM tier. That is host noise, not
a speed-up; re-run that level.

### Validity

A run is only reportable if, in **every** tier, `throughput.throughput_ratio ≥ 0.90` at VU 1/10/100
and `median(origin) ≤ median(bare) ≤ median(wadm)` holds for every edge at every level. A violation
means the host was contaminated.

## Box-plot comparison across edges

`plot_edge_comparison.py` renders one figure per VU level, placing all four edges side by side with a detection subplot and an injection subplot. Every honeytoken kind — `html_comments`, `http_headers`, `cookies`, `decoy_paths`, `form_fields` — is **pooled into one distribution per edge**, so the figure answers "which edge is cheapest at honeytoken work overall". For the per-kind breakdown behind those numbers, use `plot_token_comparison.py` below.

Pooling is by concatenation of raw samples, so each kind contributes in proportion to how often it actually fires within an iteration (the `/*` kinds inject on all four benchmark `GET`s, `form_fields` on one). A box therefore reads as "what a honeytoken operation costs on this edge", not as a mean of per-kind means.

The pool is **per phase**. `sql_injection` joins the detection pool but has no injection region at all, so the injection pool stays the five honeytoken kinds. Its clean-login arm is a control rather than a feature and is pooled nowhere — it is plotted against the hit arm by `plot_sqli_comparison.py`.

```bash
# defaults to benchmarks/results/, writes PNGs to benchmarks/results/plots/
python3 benchmarks/plot_edge_comparison.py

# or point it at a different results directory
python3 benchmarks/plot_edge_comparison.py path/to/results
```

For each edge it prefers `internal_<edge>_raw.json` and draws a **true box plot** (box = Q1–Q3, line = median, whiskers = 1.5×IQR). If an edge's raw file is missing, it falls back to a **count-weighted summary approximation** from the profile file (box = min→p90, line = mean, whisker = max) and marks that box with a hatched/faded style so it is not mistaken for real quartile data. Re-run that edge's benchmark to replace the fallback with a real box.

Result files written before per-kind timing existed carry only the top-level html_comments arrays; those are mapped onto the `html_comments` kind, so an old file still renders (as that one kind) rather than vanishing from the figure.

Output artifacts:
- `benchmarks/results/plots/edge_comparison_vus_<N>.png` (one per VU level).

## Per-honeytoken-kind plots

`plot_token_comparison.py` breaks the same samples down per kind: every honeytoken kind, both phases, all four edges. Use `plot_edge_comparison.py` to rank edges, this one to see which kind drives the cost.

```bash
# defaults to benchmarks/results/, writes PNGs to benchmarks/results/plots/
python3 benchmarks/plot_token_comparison.py

# or point it at a different results directory
python3 benchmarks/plot_token_comparison.py path/to/results
```

Output artifacts:

| File | Layout |
|------|--------|
| `token_comparison_vus_<N>.png` (one per VU level) | 2 rows (detection, injection) × one column per kind in that phase — 6 for detection, 5 for injection, with the surplus injection cell removed rather than drawn empty. Four edge box plots per panel, log-scale y shared across each row so kinds are comparable within a phase. |
| `token_scaling_detect.png`, `token_scaling_inject.png` | Median latency vs. VU level, one panel per kind, one direct-labelled line per edge with a Q1–Q3 band — shows how each kind scales with load. |

Both scripts share [plot_common.py](plot_common.py) — the palette, the raw-preferred / summary-fallback rule (hatched, faded boxes mark an approximation), the symlog convention and the result-file loader all live there, so an edge keeps one colour and one visual language across every figure.

## The SQLi trap: a detection-only feature, measured as a 2×2

`sql_injection` is the only feature in `config.json` that plants nothing. It never mutates a response the origin produced — it watches `POST /api/login`, scans the body, and answers. So it has a detection region and no injection region, and it is the only feature whose non-triggering path still does full work. That makes it worth plotting on its own, which `plot_sqli_comparison.py` does.

Two factors are crossed, because measuring only hit-vs-miss confounds them.

### Outcome: how deep the scan goes

Nothing authenticates anything here. The origin is a static nginx with no `/api/login` route, no user store and no password check; the edge fabricates both the fake MySQL error and the canned 401. A non-matching request costs exactly what it takes to check the body against the signature list, and nothing more.

`sqli_match` is a linear scan that returns on first match, walking `watch_fields → body pairs → signatures` in config order. That order is part of the [cross-edge contract](../docs/EDGE_LEVELING.md), so "first match wins" resolves identically on all four edges.

| Outcome | Benchmark payload | Comparisons |
|---------|-------------------|-------------|
| hit | `username=admin'&password=x` | 11 — matches `admin'`, signature #11 of 22, in the first watch field |
| no signature match | `username=alice&password=secret` | 44 — all 22 against `username`, then all 22 against `password` |

**That 33-comparison difference costs nothing measurable on three of the four edges.** Most signatures are *longer* than a real field value — `information_schema` (18 chars), `union all select` (16), `waitfor delay` (13) against `alice` (5) — so they are rejected on length before a single character is compared, and a JIT compiles what is left down to noise.

**Apache is the exception**, consistently by ~2.5 µs and in the direction the scan-depth hypothesis predicts. Its `mod_lua` links `liblua.so.5` — standard PUC Lua — while OpenResty and Envoy both run LuaJIT and the WASM filter is compiled Rust. Scan depth is measurable only where the matching loop is genuinely interpreted.

The practical consequence is that **growing the signature list is close to free on a JIT-compiled or native edge**, and carries a small but real cost on an interpreted one. A single-edge measurement would have reported one of those two answers and missed the other.

### Encoding: whether the body needs decoding

This is where the time actually goes. `url_decode` runs over every body pair in `sqli_match`, and then again inside `sqli_normalize` on the matched value, so a percent-encoded payload pays the substitution path twice.

The `%27` form of each payload decodes to exactly the plain form, so the two arms of one outcome scan identical bytes and differ **only** in decoding work. That isolation is what makes the factor attributable.

### The four arms

| Kind name | Outcome | Body |
|-----------|---------|------|
| `sql_injection` | hit | `username=admin'&password=x` |
| `sql_injection_encoded` | hit | `username=admin%27&password=x` |
| `sql_injection_miss` | no match | `username=alice&password=secret` |
| `sql_injection_miss_encoded` | no match | `username=alice%27&password=secret` |

The edge sets the `_encoded` suffix by checking the raw body for `%` *after* the timer closes, so the classification never enters the measurement. That check is not benchmark scaffolding — percent-encoded input is an evasion technique a honeypot has independent reason to record.

Only `sql_injection` is pooled into the detection figures, as the trap's one representative sample per iteration; pooling all four would give it four times the weight of any honeytoken kind. The other three are controls, plotted only here.

**If the `signatures` list in `config.json` is reordered, re-check the hit payloads** — `admin'` sitting at #11 is what makes the outcome axis mean what it says.

```bash
# defaults to benchmarks/results/, writes PNGs to benchmarks/results/plots/
python3 benchmarks/plot_sqli_comparison.py

# or point it at a different results directory
python3 benchmarks/plot_sqli_comparison.py path/to/results
```

Output artifacts:

| File | Layout |
|------|--------|
| `sqli_arms_vus_<N>.png` (one per VU level) | Four edges side by side, four boxes each in the order hit-plain, hit-encoded, clean-plain, clean-encoded. Filled = the body needed decoding; hue identifies the edge, as in every other figure. |
| `sqli_arms_scaling.png` | Median scan cost vs. VU level, one panel per arm, one direct-labelled line per edge with a Q1–Q3 band. |

### The end-to-end side

The four `POST`s are deliberately **excluded** from `e2e_phase_overhead_vus_*.png` and given their own figure, `sqli_e2e_overhead_vus_*.png`. Two reasons: they carry no injection, so they do not belong on an axis comparing injection-only against injection-plus-detection; and on the three edges that answer from the request phase WADM *removes* the origin round-trip the bare tier pays, so their deltas are **negative**. Sharing an axis would put "WADM added 1.5 ms" beside "WADM saved 0.5 ms" under one "latency added" label and flatten the bars that figure exists to show.

Note that `E2E_METRICS` still contains all eight requests — it is the summation set for the overhead decomposition, which must account for the whole iteration. `E2E_PHASE_METRICS` is the plotting subset.

Two caveats on that plane, neither of which touches the microsecond figures:

- **Envoy+WASM is not comparable to the other three.** It cannot answer from the request phase and rewrites the upstream response instead, so it alone pays an origin round-trip. Its bars sit near zero for that reason, not because WADM costs it more.
- **Latency falls ~30% across an iteration even with WADM absent.** The first request after `sleep(1)` pays a wake-up cost later ones do not, so the eight Trends are eight measurements taken at eight points on a gradient. The `wadm − bare` subtraction cancels it (both tiers share the ordering), which is why every end-to-end figure plots a delta; absolute per-request figures would not be comparable across positions. See the 2026-09-21 entry in [THESIS_NOTES.md](../docs/THESIS_NOTES.md).

The microsecond plane the 2×2 figures use is unaffected by both — those timers wrap a region inside the edge and never include the network.

### Why symlog rather than log

The edges time in whole microseconds, and 8–13% of samples on OpenResty, WASM and Envoy+Lua land on exactly **0 µs** — the work finished inside one timer tick. A pure log axis cannot render 0 and would silently drop that mass. Every figure therefore uses a symlog y-axis: linear below 1 µs, logarithmic above.

## Cross-edge comparability

`detect_query_duration` measures the cost of `GET /api/login?password=<KW>` — a surface every edge already inspects, so the numbers are directly comparable:

| Edge | Inspects query string? | What `detect_query_duration` measures |
|------|------------------------|---------------------------------------|
| OpenResty (`nginx/nginx.conf`) | Yes (`ngx.req.get_uri_args`) | Detection + stripping cost. |
| Envoy + Lua (`envoy_scripts/injection.lua`) | Yes (`parse_query_string`) | Detection + stripping cost. |
| Apache + mod_lua (`apache_scripts/detect.lua`) | Yes (`r.args`) | Detection + stripping cost. |
| Envoy + WASM (`wasm-filter/src/lib.rs`) | Yes (`:path` substring search) | Detection + stripping cost. |

### Level-playing-field invariants

To keep the four edges directly comparable, the internal benchmarks enforce these
guarantees (all four edges obey them):

1. **Wall-clock microseconds.** Every edge times with an elapsed wall-clock µs
   source — OpenResty `gettimeofday` (FFI), Apache `r:clock()`, Envoy+Lua
   `gettimeofday` (FFI, matching OpenResty), WASM `get_current_time()`. Envoy+Lua
   previously used `os.clock()` (process CPU time) and was **not** comparable.
2. **Detection is timed only for trigger-bearing requests.** Each edge logs
   "Detection execution time (us)" only when a trigger keyword actually matched,
   so all four sample the same population (the `?password=<KW>` request), not the
   cheap no-keyword `GET /`. Expect `detection_us` count ≈ iterations. The same
   rule holds per kind: a `WADM TOKEN <kind> detect (us)` line is emitted only when
   that kind fired, so each kind's distribution is a hit-only population on every
   edge.
3. **Warm start, quiet host, validated throughput.** Each orchestrator runs one
   throwaway warm-up burst (`WARMUP_VUS=100`, `WARMUP_DURATION=20s`) before the
   recorded levels and discards it, so JIT/caches are hot and VU=1 is not a
   cold-start outlier. The edge stack persists across levels, so warming once suffices.

   Two guards protect against host noise, both learned the hard way on this repo's
   2-core machine:

   - **Cool-down between edges** (`wait_for_quiet_host`). The 500-VU level saturates
     the host and the load takes time to decay, so running edges back-to-back measured
     whichever edge came later against a busy machine — in one run Envoy started at load
     5.5 and its VU=1 detection averaged 1636 µs against 2 µs once idle. Each orchestrator
     now waits for the 1-minute load average to fall below 2.0 before it starts.
   - **Throughput validation** (`throughput_check`). `sleep(1)` in `test.js` caps a VU at
     one iteration per second, so at VU ≤ 100 a healthy edge lands within a few percent of
     `vus × seconds`. Each level records `throughput.throughput_ratio` and prints a
     `WARNING: host was busy, treat this level as invalid` when a reachable level falls
     below 90%. Without it, a contaminated run produced 47% of the achievable iterations
     while its per-operation latencies still looked plausible. At VU=500 the edge itself
     saturates, so `expected_reachable` is false there and the ratio is a capability
     measure — compare edges to each other, not to 1.0.

   A run is only valid if every edge reports ≥90% at VU 1/10/100. The committed results
   are 94–100% across the board.
4. **Equal work: query-string scan only.** POST request-body inspection is gated
   behind the `post_body_inspection` flag in `config.json` (default `false`), so
   OpenResty and Envoy+Lua do the same detection work as Apache and WASM. The
   body-scan code is kept in place — rebuilt on the same segment parser on both edges,
   and aligned so both also check `form_fields` tamper in the body — for a future
   iteration that enables it on all four edges at once (set the flag `true` and
   implement it in Apache + WASM).
   The `sql_injection` trap does read POST bodies, but only on `POST /api/login`,
   and it terminates the request before any honeytoken detector runs — so the two
   `POST`s contribute samples to `sql_injection` / `sql_injection_miss` alone and
   never touch the five honeytoken kinds' population. The trap logs under
   `WADM TOKEN sql_injection[_miss] detect (us):`, in the per-kind family. Note the
   OpenResty pattern is *unprefixed* (`Detection execution time \(us\):`), so any
   prefixed variant of those words — e.g. `SQLi Detection execution time (us):` —
   would be matched by it and silently corrupt `detection_us`, which is why the
   per-kind family uses the lowercase `detect` / `inject` words instead.
   See `docs/EDGE_LEVELING.md`.
5. **Equal timed region: in-memory state, no disk I/O.** Every edge records the
   attacker IP into an *in-memory* store on a hit, inside the timed region, mirroring
   OpenResty's `ngx.shared.wadm_state` (`wadm:set`): Envoy+Lua and Apache use a
   module-scope Lua table, WASM uses an `Rc<RefCell<HashSet<String>>>` shared from the
   root context. No edge does filesystem I/O or JSON (de)serialisation inside the
   detection timer, and per-request setup (trigger-table build, client-IP read,
   query parse) is hoisted *out* of the timer on all four. The store is write-only
   everywhere: Envoy+Lua's per-request "known attacker" lookup and log line, which no
   other edge had, was removed. Envoy+Lua
   previously read and rewrote `/tmp/detected_ips.json` on the hot path (plus a
   per-request trigger-table rebuild) inside its timer, which inflated its detection
   numbers by 3–6× and was **not** comparable.
6. **Canonical injection contract: buffered whole body, single first-match splice.**
   All four edges perform the *same* HTML injection and time the *same* region:
   - **Setup outside the timer:** the `text/html` content-type guard, path matching /
     token selection, and the comment join all run *before* the injection timer starts.
   - **Whole body buffered before the timer:** OpenResty times only its final
     `body_filter` chunk (earlier chunks are accumulated untimed); WASM `Pause`s per
     chunk until `end_of_stream`; Envoy+Lua forces `:body()` buffering *before* the
     timer so the upstream body-arrival wait is excluded; Apache buffers every brigade
     chunk before transforming.
   - **Timed region (identical on all four):** locate the first `</body>` → splice the
     joined comment(s) before it (append if absent) → write the body back (Apache's
     final `coroutine.yield` cannot be timed). Assembling the buffered chunks into one
     body is untimed buffering work on every edge. Content-Length is adjusted *outside*
     the timer.

   Two edges were previously non-comparable on injection: Envoy+Lua started its timer
   *before* `:body()`, charging the body-arrival/buffering wait to injection (≈5–10×
   inflation — it drops to single-digit µs once the wait is excluded); Apache ran a
   *per-chunk* `gsub` with **no path matching** (a different, cheaper unit that also
   logged one timing line per chunk instead of one per response). The one residual,
   *intended* difference is each runtime's native string primitive (OpenResty PCRE
   `ngx.re.sub`, WASM `str::find`, Envoy+Lua/Apache Lua `gsub` with count 1) — all
   produce the same first-match splice, so the leftover time is the runtime's body-API
   cost, which is exactly what the injection subplot is meant to measure.

   **Correction:** the splice primitive was *not* a fair intended difference. Envoy+Lua and
   Apache used `string.gsub`, which runs Lua's backtracking pattern matcher for what is a
   fixed-string insert — strictly more work than the job needs, and 2–3× slower than the
   other two edges on body splices while being competitive on header writes. Both now use a
   `splice_before` helper (plain `find` + two `sub`s) mirroring the WASM filter, which cut
   Envoy+Lua's body splice from 5 µs to 1 µs and Apache's from 6 µs to 3 µs, with
   byte-identical output. OpenResty's `ngx.re.sub` (case-insensitive PCRE) has since been
   replaced by the same plain find, so all four edges now share one splice primitive. See
   [EDGE_LEVELING.md](../docs/EDGE_LEVELING.md#the-splice-primitive-was-not-a-fair-intended-difference).
7. **Per-kind contract: same setup hoisting, same fixed order.** The four additional
   honeytoken kinds follow the same rules as html_comments. `sql_injection` is the one
   documented exception: its timed region is the signature scan alone, with the
   attacker-IP record left *outside*. The miss arm performs no such write, so including
   it would make the hit-vs-miss gap partly an artefact of one hash insert rather than
   of scan depth. All four edges do this identically, so cross-edge comparability is
   unaffected. Per-request setup (client
   IP, URI, parsed query args, `Cookie` header) is read **once** outside every timer
   and shared by all kinds; a `detect` timer wraps only that kind's scan, its alert
   log and the in-memory IP record; an `inject` timer wraps only the header write or
   the anchor-locate-and-splice, with token selection and markup construction hoisted
   out. Kinds are always walked in the fixed order
   `http_headers → cookies → decoy_paths → form_fields` (OpenResty previously used
   `pairs()`, whose order is unspecified). The body-kind timers exclude the write-back
   because the extra payloads chain on an in-memory string that is written once at the
   end — so no edge charges its body-API write to a per-kind number.

8. **No log I/O inside any timer.** Detectors record hits as small descriptors; both
   the alert *rendering* and the `WADM ALERT` *write* happen after the timer closes.
   Every edge does this, for the additional kinds and for html_comments alike.

   This was not the original design, and the reason for the change is worth recording.
   With the write inside the timer, a kind's measured cost tracked **whether it was the
   first to log on that request**, not how much scanning it did: at 100 VUs the three
   kinds that log first on their request (html_comments, http_headers, decoy_paths) came
   out at 8–47 µs while the two that log behind another kind (cookies, form_fields) came
   out at 1–8 µs — a 3–10× split, reproduced independently on all four edges, even though
   `cookies` does *more* string work than `http_headers`. The injection timers, which
   never contained a write, showed no such split. The detection figure was therefore
   ranking each runtime's logging path rather than its detection logic. Moving the I/O
   out makes detection measure detection; the alert wire format is unchanged.

9. **One detection mechanism, not four.** Every edge parses the query string the same
   way (ordered `&`-separated segments, `+`/`%XX` decoding only where present), matches
   decoded keys and values, strips by rebuilding the query from the *raw* text of the
   segments it keeps, uses the raw request path for every path check, records the
   socket peer as the client IP, and emits byte-identical `WADM ALERT` lines. Before
   this, OpenResty and Envoy+Lua re-encoded the query in hash order, Apache's strip
   silently failed for the benchmark keyword (its `-` and `.` were Lua pattern
   characters), WASM stripped only the keyword substring from the whole path, and both
   Envoy edges recorded every attacker as `unknown`. `parity_check.py` (below) verifies
   it. See [EDGE_LEVELING.md](../docs/EDGE_LEVELING.md#behavioural-equivalence-and-latency-pass).
10. **Equal logging configuration.** No edge writes a per-request access log (Envoy never
    did; OpenResty's implicit default and Apache's `CustomLog` were removed, in both
    tiers). The per-request record is the origin's access log, which carries the client
    IP in `xff=` for every edge.

## Behavioural parity check

The latency comparison assumes every edge does the same work. `parity_check.py` verifies it
functionally: it brings all four edges up at once (their host ports differ), sends each the same
fixed set of requests (the four benchmark GETs, exact-path pages, order-preserving strip,
`%XX`- and `+`-encoded keywords, a duplicated form field, a decoy sub-path, and a SQLi hit and miss),
and diffs three things against OpenResty:

- what the attacker sees — status, `X-Backend-Server`, `Set-Cookie`, body hash;
- what the edge alerts on — every `WADM ALERT` / `WADM TRAP` line, IPs normalised;
- what reached the origin — the request lines in the origin's access log, attributed to an edge by
  its container IP, which shows whether a trigger keyword was stripped.

```bash
python3 benchmarks/parity_check.py          # exit 0 = identical, 1 = mismatch (printed)
python3 benchmarks/parity_check.py --keep   # leave the stack up for inspection
```

It excludes only the documented residuals: response framing (chunked vs `Content-Length`), the
Envoy+WASM SQLi trap contacting the origin, and Apache forwarding `path?` when every parameter is
stripped. Run it after any change to an edge, before benchmarking. It shares the Compose project
with the runners, so never run it (or anything else that calls `docker compose down`) while a
benchmark is in progress.

## Files

| File | Role |
|------|------|
| [test.js](test.js) | The k6 default-function script: four `GET`s and four `POST`s per iteration covering every deception feature's injection and detection paths, the `Trend` definitions, and the `handleSummary` sentinel line that publishes end-to-end latency to the runners. |
| [run_baseline_benchmark.py](run_baseline_benchmark.py) | Orchestrates the no-WADM tiers (four bare edges + the origin floor) from one parameterised table and writes `e2e_<edge>_bare.json`. |
| [run_internal_openresty_benchmark.py](run_internal_openresty_benchmark.py) | Orchestrates internal OpenResty microsecond profiling runs and writes summary + raw JSON results. |
| [run_internal_envoy_lua_benchmark.py](run_internal_envoy_lua_benchmark.py) | Orchestrates internal Envoy Lua microsecond profiling runs and writes summary + raw JSON results. |
| [run_internal_apache_lua_benchmark.py](run_internal_apache_lua_benchmark.py) | Orchestrates internal Apache mod_lua microsecond profiling runs and writes summary + raw JSON results. |
| [run_internal_wasm_benchmark.py](run_internal_wasm_benchmark.py) | Orchestrates internal Envoy WASM microsecond profiling runs and writes summary + raw JSON results. |
| [parity_check.py](parity_check.py) | Functional check that all four edges produce identical responses, alert lines and origin requests for a fixed probe set; run before benchmarking after any edge change. |
| [wadm_timings.py](wadm_timings.py) | Shared by every runner: Compose driving (cleanup, stack start, load-tester cycling), summary statistics, the cross-edge `WADM TOKEN <kind> <phase> (us):` scraper, the k6 end-to-end summary scraper, and the end-to-end result-document builders. |
| [plot_common.py](plot_common.py) | Shared by every plotter: edge palette, result-file loader, raw/summary fallback, pooled-sample helpers, symlog axis styling. |
| [plot_edge_comparison.py](plot_edge_comparison.py) | Per-VU box-plot comparison of all four edges with **all honeytoken kinds pooled** — the edge-level ranking. |
| [plot_token_comparison.py](plot_token_comparison.py) | Per-honeytoken-kind breakdown: box plots per (phase, kind) at each VU level, plus median-vs-load scaling panels. |
| [plot_baseline_comparison.py](plot_baseline_comparison.py) | End-to-end latency with vs. without WADM: per-request-type boxes, latency-vs-load scaling, and the overhead breakdown against the internal timers. |
| [plot_sqli_comparison.py](plot_sqli_comparison.py) | The SQLi trap's 2×2 per edge: outcome (signature hit / no match) crossed with payload encoding, plus median-vs-load scaling panels. |
| [EDGE_LEVELING.md](EDGE_LEVELING.md) | Record of the source changes that made the four edges comparable (detection state store, canonical injection contract, Envoy path capture). |
| [README.md](README.md) | This document. |
