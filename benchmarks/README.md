<!-- benchmarks/README.md: external k6 load generator for WADM edge benchmarking; lives outside any edge profile so it never auto-runs with a normal `up`. -->
# benchmarks (k6 load generator)

External request-latency probe for the WADM edge proxies. A `grafana/k6` container joins the `honeypot` Docker network and hits whichever edge is selected via the `TARGET` env var, recording per-phase latency for the two WADM behaviours that matter:

| Phase | Request | k6 Trend metric |
|-------|---------|-----------------|
| HTML injection (response rewrite) | `GET ${TARGET}/` | `inject_get_duration` |
| Detection + stripping (request scrub) | `GET ${TARGET}/api/login?password=${TRIGGER_KEYWORD}` | `detect_query_duration` |

The two custom `Trend`s are reported separately in k6's end-of-run summary; the built-in `http_req_duration` mixes both calls and is less useful for per-phase analysis.

## Why the keyword is in the query string

All four edges already inspect the request **query string** (OpenResty `get_uri_args`, Envoy+Lua `parse_query_string`, Apache `r.args`, WASM `:path` substring). Putting `TRIGGER_KEYWORD` in `?password=...` therefore exercises the detection path on every edge, so `detect_query_duration` is comparable across all four.

POST-body inspection still exists in OpenResty (`nginx/nginx.conf`) and Envoy+Lua (`envoy_scripts/injection.lua`); it is intentionally left in place for a follow-up iteration that raises Apache and Envoy+WASM to the same level (by implementing body inspection in `apache_scripts/detect.lua` and `wasm-filter/src/lib.rs`) and re-introduces a `detect_body_duration` scenario alongside this one.

## Data flow

```mermaid
flowchart LR
  k6[load-tester k6 container]
  Edge["Edge proxy (TARGET)"]
  Backend[backend nginx]

  k6 -->|"GET / and GET /api/login?password=KW"| Edge
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
| `TRIGGER_KEYWORD` | `internal-admin.example.com` | Substring placed in the query string to fire the detection phase. Should match a `trigger_keyword` in `config.json`. |
| `K6_VUS` | `5` | Concurrent virtual users. |
| `K6_DURATION` | `30s` | Run length. |

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

Compose profile `wasm` rebuilds the filter via `rust-builder` before starting `envoy-wasm`, so each run picks up the latest `wasm-filter` sources.

## Raw sample files

Each internal benchmark writes two artifacts per edge:

| File | Contents |
|------|----------|
| `internal_<edge>_profile.json` | Per-VU **summary** stats (`count, min_us, avg_us, p90_us, max_us`) for each phase. Small, human-readable. |
| `internal_<edge>_raw.json` | Per-VU **raw** per-request latency arrays: `runs[].detection_us[]` and `runs[].injection_us[]`, in microseconds. |

The raw file exists so that box plots can be drawn from the true latency distribution (real quartiles), which the summary stats alone cannot reconstruct. Shape:

```json
{
  "metadata": { "...": "same as the profile file, plus a note field" },
  "runs": [
    { "vus": 1, "detection_us": [97, 41, ...], "injection_us": [26, 8, ...] },
    { "vus": 10, "detection_us": [...], "injection_us": [...] }
  ]
}
```

## Box-plot comparison across edges

`plot_edge_comparison.py` renders one figure per VU level, placing all four edges side by side with a detection subplot and an injection subplot (log-scale y-axis).

```bash
# defaults to benchmarks/results/, writes PNGs to benchmarks/results/plots/
python3 benchmarks/plot_edge_comparison.py

# or point it at a different results directory
python3 benchmarks/plot_edge_comparison.py path/to/results
```

For each edge it prefers `internal_<edge>_raw.json` and draws a **true box plot** (box = Q1–Q3, line = median, whiskers = 1.5×IQR). If an edge's raw file is missing, it falls back to a **summary approximation** from the profile file (box = min→p90, line = mean, whisker = max) and marks that box with a hatched/faded style so it is not mistaken for real quartile data. Re-run that edge's benchmark to replace the fallback with a real box.

Output artifacts:
- `benchmarks/results/plots/edge_comparison_vus_<N>.png` (one per VU level).

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
   cheap no-keyword `GET /`. Expect `detection_us` count ≈ iterations.
3. **Warm start.** Each orchestrator runs one throwaway warm-up burst
   (`WARMUP_VUS=100`, `WARMUP_DURATION=20s`) before the recorded levels and
   discards it, so JIT/caches are hot and VU=1 is not a cold-start outlier. The
   edge stack persists across levels, so warming once suffices.
4. **Equal work: query-string scan only.** POST request-body inspection is gated
   behind the `post_body_inspection` flag in `config.json` (default `false`), so
   OpenResty and Envoy+Lua do the same detection work as Apache and WASM. The
   body-scan code is kept in place for a future iteration that enables it on all
   four edges at once (set the flag `true` and implement it in Apache + WASM).
5. **Equal timed region: in-memory state, no disk I/O.** Every edge records the
   attacker IP into an *in-memory* store on a hit, inside the timed region, mirroring
   OpenResty's `ngx.shared.wadm_state` (`wadm:set`): Envoy+Lua and Apache use a
   module-scope Lua table, WASM uses an `Rc<RefCell<HashSet<String>>>` shared from the
   root context. No edge does filesystem I/O or JSON (de)serialisation inside the
   detection timer, and per-request setup (trigger-table build, client-IP read,
   known-attacker lookup) is hoisted *out* of the timer on all four. Envoy+Lua
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
   - **Timed region (identical on all four):** assemble the full body → locate the
     first `</body>` → splice the joined comment(s) before it (append if absent) →
     write the body back. Content-Length is adjusted *outside* the timer.

   Two edges were previously non-comparable on injection: Envoy+Lua started its timer
   *before* `:body()`, charging the body-arrival/buffering wait to injection (≈5–10×
   inflation — it drops to single-digit µs once the wait is excluded); Apache ran a
   *per-chunk* `gsub` with **no path matching** (a different, cheaper unit that also
   logged one timing line per chunk instead of one per response). The one residual,
   *intended* difference is each runtime's native string primitive (OpenResty PCRE
   `ngx.re.sub`, WASM `str::find`, Envoy+Lua/Apache Lua `gsub` with count 1) — all
   produce the same first-match splice, so the leftover time is the runtime's body-API
   cost, which is exactly what the injection subplot is meant to measure.

## Files

| File | Role |
|------|------|
| [test.js](test.js) | The k6 default-function script with the two phase requests and `Trend` definitions. |
| [run_internal_openresty_benchmark.py](run_internal_openresty_benchmark.py) | Orchestrates internal OpenResty microsecond profiling runs and writes summary + raw JSON results. |
| [run_internal_envoy_lua_benchmark.py](run_internal_envoy_lua_benchmark.py) | Orchestrates internal Envoy Lua microsecond profiling runs and writes summary + raw JSON results. |
| [run_internal_apache_lua_benchmark.py](run_internal_apache_lua_benchmark.py) | Orchestrates internal Apache mod_lua microsecond profiling runs and writes summary + raw JSON results. |
| [run_internal_wasm_benchmark.py](run_internal_wasm_benchmark.py) | Orchestrates internal Envoy WASM microsecond profiling runs and writes summary + raw JSON results. |
| [plot_edge_comparison.py](plot_edge_comparison.py) | Renders per-VU box-plot comparisons of all four edges from the raw sample files (falls back to summary stats). |
| [EDGE_LEVELING.md](EDGE_LEVELING.md) | Record of the source changes that made the four edges comparable (detection state store, canonical injection contract, Envoy path capture). |
| [README.md](README.md) | This document. |
