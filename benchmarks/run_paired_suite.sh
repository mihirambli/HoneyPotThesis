#!/usr/bin/env bash
# The reportable paired dataset, re-measured with the origin tier, and everything drawn from it.
# ~6.5 hours on the reference VM.
#
# Per edge, one `--preset full` run: 5 replicates x 3 rates (1, 10, 100 iterations/s) x 4 tiers
# (origin, bare, silent, wadm), paired and order-balanced. The origin tier sends the same probes to
# the origin directly with the edge idle, inside the same replicate, so bare - origin and
# silent - origin are paired exactly as silent - bare is. It cannot be appended to an older dataset:
# a floor measured in another session carries that session's drift, which is larger than the proxy's
# cost (BENCHMARK_METHODOLOGY.md §1.3), so the edge tiers are re-measured alongside it.
#
# Before anything runs, the files this overwrites are copied to results/archive/<date>-pre-origin-tier/
# (once: a restart keeps the first copy). The SQLi, GET-only, size-sweep, capacity and Apache-variant
# datasets are separate paired runs and are left alone; BENCHMARK_METHODOLOGY.md §9.3 says why none of
# them needs re-measuring.
#
# Usage:
#   sudo ./benchmarks/tune_guest.sh apply   # first; and keep the host from sleeping
#   ./benchmarks/run_paired_suite.sh        # logs to results/paired_suite_<timestamp>.log
set -uo pipefail

cd "$(dirname "$0")/.."
RESULTS=benchmarks/results
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$RESULTS/paired_suite_${STAMP}.log"
STATUS="$RESULTS/paired_suite_${STAMP}.status"
source benchmarks/suite_common.sh

EDGES=(openresty wasm apache_lua envoy_lua)

archive_previous() {
    local existing
    existing="$(compgen -G "$RESULTS/archive/*-pre-origin-tier" | head -1)"
    if [[ -n "$existing" ]]; then
        echo "=== keeping the existing archive $existing" | tee -a "$LOG"
        return
    fi
    local dest="$RESULTS/archive/$(date -u +%F)-pre-origin-tier"
    mkdir -p "$dest"
    local f
    for f in "${EDGES[@]/#/paired_}"; do
        [[ -f "$RESULTS/$f.json" ]] && cp -p "$RESULTS/$f.json" "$dest/"
    done
    for f in paired_summary.json paired_headline.md paired_headline.csv paired_functional.md \
             paired_functional.csv mechanisms.md \
             plots/paired_latency_added.png plots/paired_latency_shift.png \
             plots/paired_internal_timers.png plots/cost_attribution.png \
             plots/instrumented_coverage.png plots/mechanism_cpu.png plots/apache_remediation.png; do
        [[ -f "$RESULTS/$f" ]] && cp -p "$RESULTS/$f" "$dest/"
    done
    [[ -d "$RESULTS/plots/by_rate" ]] && cp -rp "$RESULTS/plots/by_rate" "$dest/"
    cat > "$dest/README.md" <<'EOF'
# `results/archive/<date>-pre-origin-tier/`

The reportable paired dataset as it stood before the origin tier was added, and every table and
figure drawn from it. Copied by `benchmarks/run_paired_suite.sh` before it re-measured the four edges.

## What is in it

| Files | Produced by | State |
|---|---|---|
| `paired_<edge>.json` | `run_paired_benchmark.py --preset full` | three tiers (bare, silent, wadm), 5 replicates, rates 1/10/100; `metadata.generated_at` says when |
| `paired_summary.json`, `paired_headline.*`, `paired_functional.*`, `mechanisms.md` | `analyze_paired.py`, `plot_paired_overhead.py`, `plot_mechanisms.py` | derived from the files above |
| `*.png`, `by_rate/` | the paired plotters | the figures as they were before the re-run |

## Why

The origin tier must run in the same replicate as bare and silent for bare - origin to be a paired
difference, so it could not be added to these files after the fact. They remain valid measurements of
everything they report (silent - bare, the in-edge timers, CPU per request); they are superseded by the
four-tier run, not wrong. Thesis numbers quoted before the re-run trace back to this folder.

## Data flow

    results/paired_<edge>.json and the tables and figures drawn from them -> copied here, unchanged

Nothing reads from `archive/`.
EOF
    echo "=== archived the three-tier dataset to $dest" | tee -a "$LOG"
}

warn_if_untuned
echo "$(date -u +%FT%TZ) START paired suite ${STAMP}" >> "$STATUS"
archive_previous

for edge in "${EDGES[@]}"; do
    step python3 benchmarks/run_paired_benchmark.py --preset full --edge "$edge"
done

step python3 benchmarks/analyze_paired.py
step python3 benchmarks/plot_paired_overhead.py
step python3 benchmarks/plot_by_rate.py
step python3 benchmarks/plot_cost_attribution.py
step python3 benchmarks/plot_mechanisms.py
step python3 benchmarks/plot_remediation.py
step python3 benchmarks/plot_origin_floor.py

echo "$(date -u +%FT%TZ) DONE" >> "$STATUS"
echo "=== $(date -u +%H:%M:%S) done. Log: $LOG  Status: $STATUS" | tee -a "$LOG"
