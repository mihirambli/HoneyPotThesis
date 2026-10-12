# `archive/2026-10-03-pre-rerun/`

A byte-for-byte snapshot of `benchmarks/results/` taken on 2026-10-03, after the Phase A
re-analysis and **before** the Phase B harness changes and benchmark re-runs.

## What is in it

| Path | Produced by | State at snapshot time |
|---|---|---|
| `paired_<edge>.json` | `run_paired_benchmark.py` | The 2026-09-30 paired dataset: two tiers (bare, WADM), `rotate` probe order, no functional checks. |
| `paired_summary.json` | `analyze_paired.py` | Phase A analysis of the above (quantile deltas, CPU split, coverage). |
| `paired_headline.md` / `.csv` | `plot_paired_overhead.py` | Headline table from the above. |
| `capacity_<edge>.json` | `run_capacity_benchmark.py` | 2026-09-30 doubling ladder, single run per step, no per-container utilisation. |
| `e2e_*`, `internal_*` | legacy runners | Unpaired constant-VU data (2026-09-21/24); see `../2026-10-03-pre-critique/README.md`. |
| `plots/*.png` | Phase A plotters | Figures drawn from the 2026-09-30 paired dataset. |

## Why it exists

Phase B re-runs the paired and capacity benchmarks with a third tier (WADM without timing logs), a
carry-over-balanced probe order and functional checks. Those runs overwrite the files above. The
user asked that every regenerated file be kept.

## Data flow

    results/ (2026-10-03, after Phase A)  -> copied here, unchanged
    results/                               -> regenerated in place by Phase B

## Rules

- Nothing reads from `archive/`. It is a record, not an input.
- Files here are never edited. A later snapshot goes in a new dated folder.
