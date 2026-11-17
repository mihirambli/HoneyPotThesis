# Plan: sqlmap payloads for the SQL-injection trap

Status: plan only, not implemented (2026-10-25). Source discussion: `docs/sqlmap_Plan.txt`.

## Goal

The SQL-injection trap is currently measured with four hand-written bodies (`admin'` / `alice`,
plain and `%`-encoded). This plan replaces them, in one small supplementary run, with payloads
recorded from sqlmap, so that the existing RQs get a "with realistic attack payloads" answer next
to the current one. **No RQ is added or reworded.** Method, tiers, pairing, Williams order,
validity gates and analysis stay the same; only the request bodies change.

Out of scope (deliberately): the attack-traffic flood (second k6 stream), new RQs, new figures,
lengthening any existing run, the proxy OpenResty/Apache variants.

## What it adds to each RQ

| RQ | Addition | Thesis location |
|---|---|---|
| RQ2.2 (for which requests) | CPU time per request to the SQL-injection trap with sqlmap's payloads, paired against the hand-written ones in the same session | Results, "GET Requests and the SQL-Injection Trap": one sentence or one extra column |
| RQ4.2 (decoding and scanning) | Duration of the trap's work per probe with longer, varied input; decoding cost on realistic input per runtime | Results, "The SQL-Injection Trap: Decoding and Scanning": extra rows in `tab:results.sqli` |
| RQ3.3 (every expected alert) | Every variant writes one alert line per sqlmap request that contains a signature and one trap line per request that does not, under load | Results, "Integrity under Load and Detection": one sentence |
| RQ3.2 (intact under load) | The trap's pages, which reflect the input, stay identical to their references for varied input | same subsection, same sentence |
| R5 / §5.7 Verifying Equivalence | Parity check extended with the recorded sqlmap requests; sqlmap itself, run against each variant, reaches the same verdict | §5.7, one paragraph + one line in the results of the check |
| Discussion | sqlmap's verdict on the trap; share of sqlmap's requests that contain none of the 22 signatures (coverage gap of the signature list) | Discussion, "Deployment Implications" or a short new paragraph; Limitations sentence "no real attacker traffic" is refined, not removed |

## Design decisions

1. **One sqlmap recording, both encoding arms derived from it.** (Differs from the earlier chat,
   which proposed a second sqlmap run with `--skip-urlencode`.) Each recorded field value is
   decoded once, then sent as a *plain* body (raw bytes) and as an *encoded* body (fully
   percent-encoded). Both decode to the same bytes, which is exactly the property the current 2×2
   relies on to read the decoding cost. Values that cannot be sent raw without changing meaning
   (`&`, `%` or `+` in the value) are dropped from both arms of that pool and counted.
2. **Hit/miss decided offline by a Python port of `sqli_match`** (`injection.lua`: split on `&`,
   split on the first `=`, decode, then `sqli_normalize` decodes *again*, lower-cases and
   collapses whitespace; watch fields `username` then `password`; first of the 22 signatures
   wins). The port is the independent "expected" for RQ3.3. It is cross-checked once by replaying
   every body to the reference variant (hit → 500, miss → 401); any disagreement is a bug to fix
   before measuring.
3. **Measured as a `post-sqlmap` mix interleaved with the existing `post` mix**, in the same
   session, following `run_sqli_suite.sh`: `--mixes post,post-sqlmap --no-prime --rates 100
   --duration 20s --replicates 5`, tiers bare, silent, instrumented (no origin tier). Each
   iteration is the four trap probes only, so CPU per iteration ÷ 4 is the cost of one trap
   request directly, and sqlmap minus hand-written is paired in time. 100/s only, because the
   thesis tables report 100/s. 20 s × 5 is the agreed SQLi setting; do not lengthen it.
4. **Deterministic body choice.** Probe *p* in iteration *i* sends `pool_p[i mod len(pool_p)]`, so
   every tier and replicate sees the same sequence and the pairing holds.
5. **Pools capped at 50 distinct values per cell**, taken in recorded order, so `setup()` can
   record one reference response per body (≤ 200 bodies) for the intact check.
6. **sqlmap intensity: default `--level 1 --risk 1`** (recommended). Only raise to level 2 if a
   pool ends up with fewer than ~10 values, and state it. Version and full command line go into
   the payload file and the thesis, because they define the workload.
7. **Scanning column caveat.** Hit and miss pools now contain *different* payloads, so "miss − hit"
   is no longer the clean "rest of the signature list" measure. For sqlmap, report the per-probe
   durations and the decoding difference (encoded − plain, still clean); keep "scanning further"
   for the hand-written payloads only. Report the first-match signature histogram instead, which
   explains where in the list sqlmap's hits stop.

## Safety rules for running sqlmap

- Target **only** the local testbed containers of this repo (localhost / the compose network).
  Never a third-party host.
- Detection only: `--batch` with default level/risk; no `--tamper`, no `--os-*`, `--file-*`,
  `--sql-shell`, `--dump`, `--passwords` or other takeover/enumeration options. There is no real
  database behind the trap anyway; the run exists only to record the requests sqlmap sends.
- Keep the raw traffic log local (`benchmarks/results/`); the committed payload file contains only
  sqlmap's standard test strings.

## Implementation steps

### 1. Record (`benchmarks/record_sqlmap_payloads.py`, ~15 min runtime)
- Precondition: reference variant (OpenResty in-server, instrumented) is up, no load.
- Run, with output captured to a scratch dir:
  `sqlmap -u http://<host>:<port>/api/login --data "username=alice&password=secret" --batch
  --flush-session --level 1 --risk 1 -t benchmarks/results/sqlmap_traffic_<date>.txt
  --output-dir <scratch>`
- Parse the traffic log: every `POST /api/login` body → decoded `(field, value)` pairs; keep the
  body as a whole (the field sqlmap did not inject keeps its original value); dedupe.
- Save sqlmap's final verdict (injectable or not, technique, DBMS named) for the Discussion.

### 2. Classify and build pools (same script)
- Python port of `sqli_match` (decision 2). Unit-test it against the hand-written bodies in
  `test.js` (`admin'` → hit on signature #11, `alice` → miss).
- Write `benchmarks/sqlmap_payloads.json` (inside the already-mounted `benchmarks/`, so k6 reads
  it at `/scripts/`; no new directory, no compose change):
  - `meta`: sqlmap version, exact command, date, reference variant;
  - `stats`: requests recorded, distinct bodies, hits, misses, dropped for `&`/`%`/`+`, share with
    no signature, first-match signature histogram;
  - `pools`: `hit_plain`, `hit_enc`, `miss_plain`, `miss_enc` (index-aligned plain/enc).
- Replay every pooled body once to the reference variant; assert 500/401 matches the classifier.

### 3. Harness changes
- `test.js`: load the JSON with `open()` in init context only when `WADM_MIX=post-sqlmap`; mix
  `post-sqlmap` reuses the four probe *names* (`sqli_hit`, `sqli_hit_enc`, `sqli_miss`,
  `sqli_miss_enc`) so per-probe metrics and trap timers need no analysis changes; the body comes
  from the pool by iteration index (decision 4); `setup()` stores one reference digest per body
  index and the intact check compares against that index.
- `run_paired_benchmark.py`: add `post-sqlmap` to `--mix` choices and `valid_mixes`; refuse the
  origin tier for it as for the SQLi mixes; output file `post-sqlmap_<variant>.json`.
- `analyze_paired.py`: make `EXPECTED_ALERTS_PER_ITERATION` per mix (`all`: 7/2; `post` and
  `post-sqlmap`: 2 alerts / 2 trap lines).
- `benchmarks/run_sqlmap_suite.sh`: copy of `run_sqli_suite.sh` for the four report variants
  (`envoy_lua wasm openresty_inserver apache_inserver`) with the command in decision 3.
- `merge_replicates.py` / top-up: check it accepts the new mix name (same 20 s × 5 settings).

### 4. Parity (§5.7)
- `parity_check.py --sqlmap`: send each pooled body once to every variant, apply rules 1 and 3
  (status, decoy header, cookies; identical alert lines). Error-page body rule unchanged.
- Live check: run the step-1 sqlmap command against each of the four variants and compare verdict,
  number of requests and the sequence of response statuses. sqlmap randomises numbers and strings
  inside payloads, so requests are not compared byte for byte.

### 5. Analysis output
- One small script (or a section in `plot_report.py`'s `report_values.md`) producing per variant:
  CPU per trap request (hand-written, sqlmap, paired difference with range), trap duration per
  probe for both payload sets, decoding cost for both, intact share, alert/trap lines written vs
  expected. No new figure needed.

### 6. Measure (~1.5–2 h)
- `sudo ./benchmarks/tune_guest.sh apply`, host laptop kept awake, then `run_sqlmap_suite.sh`.
- After the run: check for clock jumps and, for Apache in-server, segfaults in the logs before
  trusting its numbers. Top up short variants with the same settings.

### 7. Thesis edits (after results exist)
- §6 Supplementary Experiments: one paragraph "Recorded attack payloads" (tool, version, options,
  pool sizes, dropped values, classification, run settings).
- §5.7: one paragraph and the parity result (decision: same verdict in all four or not).
- Results RQ2, RQ3, RQ4.2: as in the table above; answers to RQ2/RQ3/RQ4 get one clause each only
  if the sqlmap result changes them.
- Discussion: sqlmap's verdict and the uncovered share; Limitations: "no real attacker traffic"
  stays, now with "the trap was exercised with an automated tool's payloads".
- Wording rules: present tense, no history of how the run came about, "SQL-injection trap",
  "variant", spell out abbreviations; nothing about sqlmap's results in Background.

### 8. Documentation
- `docs/BENCHMARK_METHODOLOGY.md`: new §11 for the sqlmap run; add a row to the runbook table
  next to `run_sqli_suite.sh`.
- `benchmarks/README.md`: the new script, mix and payload file.
- No schema change → `docs/DATA_FLOW.md` untouched. No new directory → no new README.

## Downstream dependents

| File | Change? |
|---|---|
| `test.js` | yes (mix, pool loading, per-body reference) |
| `run_paired_benchmark.py` | yes (mix name, origin refusal) |
| `analyze_paired.py` | yes (expected alerts per mix) |
| `wadm_timings.py` (`k6_env`) | check only: passes the mix string through |
| `merge_replicates.py` | check only |
| `parity_check.py` | yes (`--sqlmap` request set) |
| `plot_report.py` / `report_values.md` | small addition |
| `docker-compose.yml` | no (`benchmarks/` already mounted at `/scripts`) |
| edge code, `config.json` | no |
| existing results / figures | no (new files only) |

## Effort

Code about a day (recording + classifier script, test.js mix, three small harness edits, parity
flag, table output). Runtime: ~15 min recording, ~40 min live parity, ~1.5–2 h suite.

## Open risks

- Pool sizes are unknown until recording; a small miss pool is reused more often — state its size.
- The classifier must mirror the double decode exactly, or RQ3.3's "expected" is wrong; the
  replay check in step 2 guards this.
- Envoy WebAssembly still forwards trap requests to nginx (existing residual difference); its CPU
  per trap request includes that, as in the current table.
