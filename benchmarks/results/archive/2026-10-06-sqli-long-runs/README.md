# `archive/2026-10-06-sqli-long-runs/`

Everything the long-form SQLi hit/miss experiment produced (60 s windows, 7 replicates), kept when
it was replaced by a shorter configuration on 2026-10-06.

## What is in it

| Path | Content |
|---|---|
| `sqli-{hit,miss}_<edge>.json` | First pass of all four edges (`sqli_suite_20261005T203935Z`); Apache's files also hold the merged second pass as replicates 8-14. |
| `second-pass/` | The second pass of Apache (Envoy+Lua's was stopped before it finished). |
| `sqli.md`, `sqli.json`, `sqli_hit_miss.png` | Outputs drawn from the files above. |
| `sqli_suite_*`, `sqli_topup_*` | Logs and status files of both passes. |

## Why it was replaced

The host repeatedly froze the VM during these runs (dozens of kernel soft lockups, one 14.7 h
pause), so 60 s cells were often rejected and the experiment took far longer than planned. The SQLi
comparison is secondary to the main paired dataset, so it was re-run for all four edges with 20 s
windows and 5 replicates (`run_sqli_suite.sh`), which keeps every edge in one consistent
configuration. The first pass of `sqli-*_<edge>.json` alone is also in
`../2026-10-06-sqli-first-pass/`.

Nothing reads from `archive/`.
