#!/usr/bin/env bash
# Second pass of the SQLi hit/miss experiment for edges that host freezes left short of valid
# pairs, merged into the existing results as further replicates. Same 20 s x 5 settings as
# run_sqli_suite.sh, so the two passes are one configuration.
#
# Waits up to 30 minutes for the guest to be tuned (tune_guest.sh needs sudo, which the operator
# runs), then proceeds either way and records the state, so an unattended night is not lost.
#
# Usage:
#   ./benchmarks/run_sqli_topup.sh [edge ...]     # default: apache_lua envoy_lua
set -uo pipefail

cd "$(dirname "$0")/.."
RESULTS=benchmarks/results
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$RESULTS/sqli_topup_${STAMP}.log"
STATUS="$RESULTS/sqli_topup_${STAMP}.status"
PASS="$RESULTS/sqli_topup_${STAMP}"
EDGES=("${@:-apache_lua envoy_lua}")
read -r -a EDGES <<< "${EDGES[*]}"
source benchmarks/suite_common.sh

tuned() {
    ! grep -q '\[always\]' /sys/kernel/mm/transparent_hugepage/enabled && [[ -z "$(swapon --noheadings)" ]]
}
for _ in $(seq 60); do tuned && break; sleep 30; done
if tuned; then echo "$(date -u +%FT%TZ) guest tuned" >> "$STATUS"; else
    echo "$(date -u +%FT%TZ) WARNING guest NOT tuned after 30 min; proceeding" >> "$STATUS"; fi
warn_if_untuned
echo "$(date -u +%FT%TZ) START sqli top-up ${STAMP}: ${EDGES[*]}" >> "$STATUS"

mkdir -p "$PASS"
for edge in "${EDGES[@]}"; do
    step python3 benchmarks/run_paired_benchmark.py --edge "$edge" --mixes sqli-hit,sqli-miss \
        --no-prime --rates 100 --duration 20s --replicates 5 --results "$PASS"
    for mix in sqli-hit sqli-miss; do
        if [[ -f "$PASS/${mix}_${edge}.json" ]]; then
            step python3 benchmarks/merge_replicates.py "$RESULTS/${mix}_${edge}.json" "$PASS/${mix}_${edge}.json"
        fi
    done
done
step python3 benchmarks/plot_sqli.py

echo "$(date -u +%FT%TZ) DONE" >> "$STATUS"
echo "=== $(date -u +%H:%M:%S) done. Log: $LOG  Status: $STATUS" | tee -a "$LOG"
