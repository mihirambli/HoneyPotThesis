#!/usr/bin/env bash
# Run every reportable measurement in order, then the analysis, figures and tables.
#
# Nothing here runs concurrently: every step drives the same Docker stack on the same pinned cores,
# and two runs at once would measure each other. Expect ~14 hours on the reference VM.
#
# Built to run unattended (see suite_common.sh): a failing step is retried once, then skipped, and
# every step's outcome is appended to the status file, which is what to read in the morning.
#
# Usage:
#   sudo ./benchmarks/tune_guest.sh apply      # first, once per boot
#   ./benchmarks/run_full_suite.sh             # logs to results/suite_<timestamp>.log
#   ./benchmarks/run_full_suite.sh --skip-capacity
set -uo pipefail

cd "$(dirname "$0")/.."
RESULTS=benchmarks/results
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$RESULTS/suite_${STAMP}.log"
STATUS="$RESULTS/suite_${STAMP}.status"
SKIP_CAPACITY=0
[[ "${1:-}" == "--skip-capacity" ]] && SKIP_CAPACITY=1

source benchmarks/suite_common.sh
warn_if_untuned

echo "$(date -u +%FT%TZ) START suite ${STAMP}" >> "$STATUS"

step python3 benchmarks/functional_matrix.py
# One step per edge, so a retry repeats only the edge that failed rather than all four.
EDGES=(openresty wasm apache_lua envoy_lua)
for edge in "${EDGES[@]}"; do
    step python3 benchmarks/run_paired_benchmark.py --preset full --edge "$edge"
done
for edge in "${EDGES[@]}"; do
    step python3 benchmarks/run_paired_benchmark.py --edge "$edge" --mix get --rates 100 --replicates 3
done
for edge in "${EDGES[@]}"; do
    step python3 benchmarks/run_paired_benchmark.py --edge "$edge" --mix size --rates 10 --replicates 3
done
# The SQLi trap's cost per hit and per miss (its own script, with its own log and per-edge retries).
step ./benchmarks/run_sqli_suite.sh
step python3 benchmarks/run_paired_benchmark.py --edge apache_lua_conn --tiers bare,wadm
if [[ $SKIP_CAPACITY -eq 0 ]]; then
    for edge in "${EDGES[@]}"; do
        step python3 benchmarks/run_capacity_benchmark.py --edge "$edge"
    done
fi

step python3 benchmarks/analyze_paired.py
step python3 benchmarks/plot_paired_overhead.py
step python3 benchmarks/plot_cost_attribution.py
step python3 benchmarks/plot_mechanisms.py
step python3 benchmarks/plot_remediation.py
[[ $SKIP_CAPACITY -eq 0 ]] && step python3 benchmarks/report_capacity.py

echo "$(date -u +%FT%TZ) DONE" >> "$STATUS"
echo "=== $(date -u +%H:%M:%S) done. Log: $LOG  Status: $STATUS" | tee -a "$LOG"
