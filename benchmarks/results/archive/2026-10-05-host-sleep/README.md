# `archive/2026-10-05-host-sleep/`

`get_apache_lua.json` as the overnight suite (`suite_20261004T203641Z`) first wrote it, kept before
that one step was re-run.

## Why

The host machine slept for about 2.8 hours during this step: VirtualBox's time sync logged a
10,200 s forward clock jump at 04:29:19 UTC on 2026-10-05, and the pause landed inside the
instrumented replicate-2 cell (started 01:39:13 UTC). A cell spanning a pause has a measurement
window that straddles a clock jump and an edge resuming from a frozen state, so its numbers are not
comparable. The step (`run_paired_benchmark.py --edge apache_lua --mix get --rates 100
--replicates 3`) was re-run in full rather than patching single cells, so its tiers stay paired.

## Data flow

    results/get_apache_lua.json (suite run)  -> copied here, unchanged
    results/get_apache_lua.json              -> rewritten by the re-run

Nothing reads from `archive/`.
