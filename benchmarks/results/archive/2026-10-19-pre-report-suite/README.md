# `results/archive/<date>-pre-report-suite/`

The result files of the thesis report's four variants (Envoy+Lua, Envoy+WASM, OpenResty in-server,
Apache in-server) as they stood before `benchmarks/run_report_suite.sh` re-measured all of them in
one session. Copied by that script before its first step; nothing here is read by any runner.

| Files | Produced by | Measured |
|---|---|---|
| `paired_wasm.json`, `paired_envoy_lua.json` | `run_paired_suite.sh` | 2026-10-14/15 |
| `paired_openresty_inserver.json`, `paired_apache_inserver.json` | `run_inserver_suite.sh` | 2026-10-18/19 |
| `get_*.json`, `size_*.json` | `run_paired_benchmark.py --mix get / size` | 2026-10-05 (Envoy only) |
| `functional_matrix.*` | `functional_matrix.py` | 2026-10-18 |
| `paired_summary.json`, `mechanisms.md`, plots | `analyze_paired.py`, `plot_mechanisms.py` | derived from the above |

Data flow: these are inputs that were replaced, kept so that numbers quoted from them stay
traceable. The proxy variants of OpenResty and Apache are not affected by the suite and were not
copied.
