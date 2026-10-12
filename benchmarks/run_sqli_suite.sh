#!/usr/bin/env bash
# The SQL-injection trap experiment: what one hit and one clean login (miss) cost on each edge, in
# latency and CPU, against the same edge without WADM. ~1.5 hours.
#
# Per edge, one interleaved run: every replicate and tier runs a hit-only cell and a miss-only cell
# minutes apart (order alternating), each iteration being exactly two POSTs of that outcome (plain
# and %-encoded body, no priming request), so the edge's CPU per iteration halved is one request's
# cost and hit - miss is paired in time. 100 iterations/s (200 req/s), 20 s windows (~4,000
# requests per cell, ample for a median and a CPU-per-request figure) and 5 replicates. Deliberately
# shorter than the main paired runs: this comparison is secondary, and on this host a shorter cell
# is less likely to overlap one of the VM freezes that reject it (see §8.8 of the methodology).
#
# Writes results/sqli-hit_<edge>.json and results/sqli-miss_<edge>.json, then
# results/plots/sqli_hit_miss.png and results/sqli.md.
#
# Usage:
#   sudo ./benchmarks/tune_guest.sh apply   # first; and keep the host from sleeping
#   ./benchmarks/run_sqli_suite.sh          # logs to results/sqli_suite_<timestamp>.log
set -uo pipefail

cd "$(dirname "$0")/.."
RESULTS=benchmarks/results
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$RESULTS/sqli_suite_${STAMP}.log"
STATUS="$RESULTS/sqli_suite_${STAMP}.status"
source benchmarks/suite_common.sh

warn_if_untuned
echo "$(date -u +%FT%TZ) START sqli suite ${STAMP}" >> "$STATUS"

for edge in openresty wasm apache_lua envoy_lua; do
    step python3 benchmarks/run_paired_benchmark.py --edge "$edge" \
        --mixes sqli-hit,sqli-miss --no-prime --rates 100 --duration 20s --replicates 5
done
step python3 benchmarks/plot_sqli.py

echo "$(date -u +%FT%TZ) DONE" >> "$STATUS"
echo "=== $(date -u +%H:%M:%S) done. Log: $LOG  Status: $STATUS" | tee -a "$LOG"
