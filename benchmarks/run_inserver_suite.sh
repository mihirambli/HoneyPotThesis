#!/usr/bin/env bash
# The in-server deployment's paired dataset, and the comparison with the proxy deployment drawn from
# it. ~3.3 hours on the reference VM (~1.6 h per variant), plus ~30 min with --with-size.
#
# Per in-server variant, one `--preset full` run of the `all` mix: 5 replicates x 3 rates (1, 10,
# 100 iterations/s) x 4 tiers (origin, bare, silent, wadm), paired and order-balanced, exactly as
# run_paired_suite.sh measures the proxy edges. Here bare is the web server serving backend/www
# itself without the module, and origin is still k6 straight to the shared nginx:alpine origin with
# the in-server container idle.
#
# Nothing is archived first: the runs write new files (paired_openresty_inserver.json,
# paired_apache_inserver.json). analyze_paired.py does rewrite paired_summary.json, adding the two
# new keys; its proxy entries are reproduced unchanged from the existing paired_<edge>.json files
# (the copy taken before this work is in results/archive/2026-10-18-pre-inserver/). The four-edge
# tables and figures are not regenerated.
#
# --with-size adds a `--mix size` run per variant (size_<key>.json), for the page-size question, with
# the settings run_full_suite.sh used for the proxy edges' size_<edge>.json (rate 10, 3 replicates),
# so the two can be compared.
#
# Usage:
#   sudo ./benchmarks/tune_guest.sh apply      # first; and keep the host from sleeping
#   ./benchmarks/run_inserver_suite.sh [--with-size]   # logs to results/inserver_suite_<timestamp>.log
set -uo pipefail

cd "$(dirname "$0")/.."
RESULTS=benchmarks/results
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$RESULTS/inserver_suite_${STAMP}.log"
STATUS="$RESULTS/inserver_suite_${STAMP}.status"
source benchmarks/suite_common.sh

EDGES=(openresty_inserver apache_inserver)

WITH_SIZE=0
for arg in "$@"; do
    case "$arg" in
        --with-size) WITH_SIZE=1 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

warn_if_untuned
echo "$(date -u +%FT%TZ) START inserver suite ${STAMP} (with_size=${WITH_SIZE})" >> "$STATUS"

for edge in "${EDGES[@]}"; do
    step python3 -u benchmarks/run_paired_benchmark.py --preset full --edge "$edge"
    if [[ "$WITH_SIZE" == 1 ]]; then
        step python3 -u benchmarks/run_paired_benchmark.py --edge "$edge" --mix size --rates 10 --replicates 3
    fi
done

step python3 -u benchmarks/analyze_paired.py
step python3 -u benchmarks/compare_deployments.py

echo "$(date -u +%FT%TZ) DONE" >> "$STATUS"
echo "=== $(date -u +%H:%M:%S) done. Log: $LOG  Status: $STATUS" | tee -a "$LOG"
