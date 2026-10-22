# `results/plots/report/`

Figures and values for the results chapter of the thesis report, drawn by
`benchmarks/plot_report.py` for the report's four variants only (Envoy Lua, Envoy WebAssembly,
OpenResty in-server, Apache in-server).

| File | Shows |
|---|---|
| `latency_added.pdf` | latency HDM adds per GET (median, 95th percentile) per load level |
| `latency_levels.pdf` | absolute median latency per GET for the two Envoy variants: origin server, Envoy without HDM, Envoy with HDM (the origin tier is reported for the proxy variants only) |
| `cpu_added.pdf` | CPU time HDM adds per request, per load level and as a marginal fit |
| `hdm_timers.pdf` | duration of HDM's detection and insertion per honeytoken type (10 it/s) |
| `cost_scopes.pdf` | time inside HDM's timers vs CPU time and latency HDM adds, per iteration (100 it/s) |
| `page_size.pdf` | latency HDM adds against page size |
| `report_values.md` | every plotted value, median [min..max] of paired replicates |

Data flow: `paired_summary.json`, `paired_<key>.json`, `get_<key>.json`, `size_<key>.json` →
`plot_report.py` → these files; the PDFs are copied into the thesis folder
(`master_thesis_template_disco/Final_Report_Workspace/figures/`). PNG copies are for quick viewing.
