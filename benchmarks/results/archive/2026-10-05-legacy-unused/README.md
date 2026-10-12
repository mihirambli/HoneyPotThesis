# `archive/2026-10-05-legacy-unused/`

Result files moved out of `benchmarks/results/` on 2026-10-05 because nothing in the current
pipeline reads them.

## What is in it

| Files | Produced by | State |
|---|---|---|
| `e2e_<edge>_<tier>.json`, `e2e_origin_bare.json` | `run_baseline_benchmark.py`, `run_internal_<edge>_benchmark.py` | Unpaired constant-VU runs of 2026-09-21/24 (VU 1/10/100/500). |
| `internal_<edge>_profile.json`, `internal_<edge>_raw.json` | `run_internal_<edge>_benchmark.py` | In-edge timer samples from the same runs. |

## Why

They were the input of the retired plotters (`plot_baseline_comparison.py`,
`plot_edge_comparison.py`, `plot_token_comparison.py`, `plot_sqli_comparison.py`). Overheads derived
from them are differences of two unpaired runs taken up to two hours apart, which
`docs/BENCHMARK_METHODOLOGY.md` §1 shows are dominated by drift. Every current figure and table is
drawn from `paired_*`, `get_*`, `size_*`, `capacity_*` and `functional_matrix.json`, which stay in
`results/`.

## Data flow

    results/e2e_*.json, results/internal_*.json  -> moved here, unchanged
    retired plotters                              -> would need these copied back to run

Nothing reads from `archive/`.
