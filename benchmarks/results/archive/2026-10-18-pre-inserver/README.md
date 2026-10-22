# `results/archive/2026-10-18-pre-inserver/`

The derived result files as they stood before the two in-server variants (`openresty_inserver`,
`apache_inserver`) were added. Copied by hand on 2026-10-18 before any in-server code ran.

## What is in it

| Files | Produced by | State |
|---|---|---|
| `paired_summary.json` | `analyze_paired.py` | the four proxy edges plus the `apache_lua_conn` variant, from the 2026-10-15 four-tier run |
| `functional_matrix.json`, `functional_matrix.md` | `functional_matrix.py` (2026-10-04) | four proxy columns |
| `parity_check_2026-10-17.txt` | `parity_check.py` | four proxy edges, OpenResty as reference |

## Why

All three files are rewritten by scripts that now also cover the in-server variants:
`analyze_paired.py` discovers `paired_<key>.json` for the in-server keys and rewrites
`paired_summary.json`; `functional_matrix.py` gains two columns; `parity_check.py` gains two edges
and a new output file. The `paired_<edge>.json` inputs are **not** copied here because nothing
rewrites them: the in-server runs write new files (`paired_openresty_inserver.json`,
`paired_apache_inserver.json`).

These copies are the reference the regenerated files are checked against. The four proxy columns
of the new functional matrix, and the four proxy edges in the new `paired_summary.json`, must equal
what is here exactly: `bootstrap_ci` in `wadm_timings.py` uses a fixed seed, so re-analysing the
same `paired_<edge>.json` reproduces the same intervals. Thesis numbers
quoted before the in-server work trace back to this folder or to the `paired_<edge>.json` files.

## Data flow

    results/paired_summary.json, functional_matrix.*, parity_check_2026-10-17.txt -> copied here, unchanged

Nothing reads from `archive/`.
