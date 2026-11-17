# `results/plots/report/`

Figures and values for the results chapter of the thesis report, drawn by
`benchmarks/plot_report.py` for the report's four variants only (Envoy Lua, Envoy WebAssembly,
OpenResty in-server, Apache in-server).

| File | Shows |
|---|---|
| `latency_added.pdf` | latency HDM adds per GET (median, 95th percentile) per load level |
| `latency_levels.pdf` | absolute median latency per GET for the two Envoy variants: origin server, Envoy without HDM, Envoy with HDM (the origin tier is reported for the proxy variants only) |
| `cpu_added.pdf` | CPU time HDM adds per request, per load level and as a marginal fit |
| `hdm_timers.pdf` | duration of HDM's detection and injection per honeytoken type (10 it/s) |
| `cost_scopes.pdf` | time inside HDM's timers vs CPU time and latency HDM adds, per iteration (100 it/s) |
| `page_size.pdf` | latency HDM adds against page size |
| `functional_matrix.pdf` | the functional matrix as a grid: verdict of each variant for each of the 13 kinds of response (from `functional_matrix.json`) |
| `sqli_trap.pdf` | SQL-injection trap: duration of a plain signature hit, and the paired cost of decoding and of scanning further (100 it/s, instrumented tier) |
| `same_runtime.pdf` | Envoy Lua against OpenResty (RQ4.3): detection and injection per honeytoken type, latency and CPU time added per load level |
| `util_added.pdf` | CPU utilisation HDM adds to the server running it, in percentage points (10 and 100 it/s) |
| `report_values.md` | every plotted value, median [min..max] of paired replicates |

Data flow: `paired_summary.json`, `paired_<key>.json`, `get_<key>.json`, `size_<key>.json`, `functional_matrix.json` →
`plot_report.py` → these files; the PDFs are copied into the thesis folder
(`master_thesis_template_disco/Final_Report_Workspace/figures/`). PNG copies are for quick viewing.
