#!/usr/bin/env bash
# The page-size runs of the two in-server variants, the only datasets of the thesis report still
# missing after the 2026-10-19/20 suite lost them to host freezes. ~30 minutes.
#
# Same settings as the Envoy variants' size_<edge>.json (rate 10, 3 replicates, bare/silent/wadm), so
# the four can be read side by side. Then analyze_paired.py and plot_mechanisms.py, which pick up the
# new files together with the restored main runs and the 2026-10-20 GET-only runs.
#
# Runs detached from any terminal or Claude session: launch it with `--detach` and it re-executes
# itself under setsid/nohup, so a closed shell, a lost network connection or an ended session does not
# stop it. Everything it needs (images, test pages, configs) is local; nothing is pulled.
#
# Refuses an untuned guest (--allow-untuned overrides): nobody is watching an unattended run.
#
# Usage:
#   sudo ./benchmarks/tune_guest.sh apply        # first; and keep the host from sleeping
#   ./benchmarks/run_inserver_size.sh --detach   # logs to results/inserver_size_<timestamp>.log
set -uo pipefail

cd "$(dirname "$0")/.."

ALLOW_UNTUNED=0
DETACH=0
for arg in "$@"; do
    case "$arg" in
        --allow-untuned) ALLOW_UNTUNED=1 ;;
        --detach) DETACH=1 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

guest_tuned() {
    ! grep -q '\[always\]' /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null \
        && [[ -z "$(swapon --noheadings 2>/dev/null)" ]]
}

if ! guest_tuned && [[ "$ALLOW_UNTUNED" != 1 ]]; then
    echo "Guest not tuned (THP=always or swap on). Run: sudo ./benchmarks/tune_guest.sh apply" >&2
    echo "or pass --allow-untuned." >&2
    exit 2
fi

if [[ "$DETACH" == 1 ]]; then
    args=()
    [[ "$ALLOW_UNTUNED" == 1 ]] && args+=(--allow-untuned)
    setsid nohup "$0" "${args[@]}" > benchmarks/results/inserver_size_nohup.out 2>&1 < /dev/null &
    echo "started detached (pid $!); follow benchmarks/results/inserver_size_*.status"
    exit 0
fi

RESULTS=benchmarks/results
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$RESULTS/inserver_size_${STAMP}.log"
STATUS="$RESULTS/inserver_size_${STAMP}.status"
source benchmarks/suite_common.sh

echo "$(date -u +%FT%TZ) START inserver size ${STAMP}" >> "$STATUS"
for edge in openresty_inserver apache_inserver; do
    step python3 -u benchmarks/run_paired_benchmark.py --edge "$edge" --mix size --rates 10 --replicates 3
done
step python3 -u benchmarks/analyze_paired.py
step python3 -u benchmarks/plot_mechanisms.py
echo "$(date -u +%FT%TZ) DONE" >> "$STATUS"
echo "=== $(date -u +%H:%M:%S) done. Log: $LOG  Status: $STATUS" | tee -a "$LOG"
