# `results/archive/2026-10-20-report-suite-degraded/`

Result files written by `benchmarks/run_report_suite.sh` on the night of 2026-10-19/20, during
which the host laptop repeatedly froze the VM (kernel soft lockups and RCU stalls every 10–20 min).
Many cells were rejected by the validity gates; the files are kept as evidence, not used.

| File | Valid cells | Replaced by |
|---|---|---|
| `paired_wasm.json` | 39/60 | the 2026-10-15 run (60/60), restored from `../2026-10-19-pre-report-suite/` |
| `paired_envoy_lua.json` | 40/60 | the 2026-10-15 run (60/60), restored |
| `paired_openresty_inserver.json` | 36/60 | the 2026-10-19 run (60/60), restored |
| `paired_apache_inserver.json` | 40/60 | the 2026-10-19 run (60/60), restored |
| `size_wasm.json` | 1/9 | the 2026-10-05 run (9/9), restored |
| `size_envoy_lua.json` | 3/9 | the 2026-10-05 run (9/9), restored |
| `size_openresty_inserver.json` | 4/9, no valid pair | a re-run (`run_inserver_size.sh`) |
| `report_suite_20261019T213523Z.*` | — | log, status and image identifiers of that night |

Data flow: nothing reads these files. The same night's `get_*.json` (all bare and silent cells
valid), `functional_matrix.*` and `parity_check_2026-10-19.txt` stayed in `results/` and are used.
