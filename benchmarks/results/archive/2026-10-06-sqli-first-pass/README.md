# `archive/2026-10-06-sqli-first-pass/`

The SQLi hit/miss files as `run_sqli_suite.sh` (`sqli_suite_20261005T203935Z`) first wrote them,
before Apache and Envoy+Lua were topped up with a second pass.

## Why

The host repeatedly stopped running the VM during that suite: 27+ kernel soft lockups of 10-335 s
and one 14.7-hour pause. Cells overlapping a freeze were rejected (the `host_stalls` gate, applied
retroactively to the OpenResty and WASM files), leaving Apache with 1 usable bare/silent pair per
outcome and Envoy+Lua with 3 for misses. A second pass of those two edges was run into a separate
directory and its cells appended to these files as replicates 8-14, so valid first-pass cells are
kept and every pair still comes from one replicate.

## Data flow

    results/sqli-*_<edge>.json (first pass)        -> copied here, unchanged
    results/sqli-*_<edge>.json                     -> first pass + second-pass replicates 8-14

Nothing reads from `archive/`.
