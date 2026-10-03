# `archive/2026-10-03-pre-critique/`

A byte-for-byte snapshot of `benchmarks/results/` taken on 2026-10-03, **before** the
measurement-and-representation review (Phase A) changed any analysis or figure.

## What is in it

| Path | Produced by | State at snapshot time |
|---|---|---|
| `paired_<edge>.json` | `run_paired_benchmark.py` | Current paired dataset (2026-09-30). Unchanged by Phase A — copied for completeness. |
| `paired_summary.json` | `analyze_paired.py` | Summary before quantile deltas, CPU split and coverage were added. |
| `capacity_<edge>.json` | `run_capacity_benchmark.py` | Ladder run of 2026-09-30, before per-container utilisation was recorded. |
| `e2e_<edge>_<tier>.json`, `internal_<edge>_*.json` | `run_baseline_benchmark.py`, `run_internal_<edge>_benchmark.py` | **Legacy, unpaired constant-VU data (2026-09-21/24).** See `docs/BENCHMARK_METHODOLOGY.md` §1 for why overheads derived from it are dominated by drift. |
| `plots/*.png` | all `plot_*.py` | Figures as committed. `cost_attribution_*`, `instrumented_coverage` and `apache_remediation` were drawn from paired data; every other figure was drawn from the legacy files above. |
| `plots/html_comments/` | early `plot_edge_comparison.py` | Oldest html_comments-only figures. |

## Why it exists

Phase A retires the legacy figures and redraws the rest. The user asked that every file a
regeneration replaces be kept, so nothing from this point is lost.

## Data flow

    results/*.json, results/plots/*.png        (as of 2026-10-03)
      -> copied here, unchanged
    results/                                    regenerated in place by Phase A

## Rules

- Nothing reads from `archive/`. It is a record, not an input.
- Files here are never edited. A later snapshot goes in a new dated folder.
