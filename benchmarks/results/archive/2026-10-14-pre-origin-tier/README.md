# `results/archive/<date>-pre-origin-tier/`

The reportable paired dataset as it stood before the origin tier was added, and every table and
figure drawn from it. Copied by `benchmarks/run_paired_suite.sh` before it re-measured the four edges.

## What is in it

| Files | Produced by | State |
|---|---|---|
| `paired_<edge>.json` | `run_paired_benchmark.py --preset full` | three tiers (bare, silent, wadm), 5 replicates, rates 1/10/100; `metadata.generated_at` says when |
| `paired_summary.json`, `paired_headline.*`, `paired_functional.*`, `mechanisms.md` | `analyze_paired.py`, `plot_paired_overhead.py`, `plot_mechanisms.py` | derived from the files above |
| `*.png`, `by_rate/` | the paired plotters | the figures as they were before the re-run |

## Why

The origin tier must run in the same replicate as bare and silent for bare - origin to be a paired
difference, so it could not be added to these files after the fact. They remain valid measurements of
everything they report (silent - bare, the in-edge timers, CPU per request); they are superseded by the
four-tier run, not wrong. Thesis numbers quoted before the re-run trace back to this folder.

## Data flow

    results/paired_<edge>.json and the tables and figures drawn from them -> copied here, unchanged

Nothing reads from `archive/`.
