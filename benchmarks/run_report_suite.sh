#!/usr/bin/env bash
# Every dataset the thesis report rests on, re-measured in one session for its four variants:
# Envoy+Lua and Envoy+WASM (proxy deployment), OpenResty and Apache in-server. ~8 hours on the
# reference VM.
#
#   1. parity_check.py             the four variants behave identically (~15 min)
#   2. functional_matrix.py        response shapes the probes never exercise, all edges (~10 min)
#   3. main paired runs            --preset full: 4 tiers x 3 rates x 5 replicates (~1.6 h each)
#   4. GET-only and page-size runs --mix get at 100/s and --mix size at 10/s, 3 replicates (~15 min each)
#   5. analyze_paired.py, plot_mechanisms.py
#
# One session does not pair the variants with each other (each still runs on its own, hours from
# the others); what it buys is one code state, one tuned guest, one set of images, and image
# identifiers recorded for all four, instead of datasets from four different days.
#
# Refuses to start on an untuned guest: an overnight run on an untuned guest rejects cells and widens
# every interval, and nobody is there to notice. --allow-untuned overrides.
#
# Before anything runs, the files this overwrites are copied to results/archive/<date>-pre-report-suite/
# (once: a restart keeps the first copy).
#
# Usage:
#   sudo ./benchmarks/tune_guest.sh apply      # first; and keep the host from sleeping
#   ./benchmarks/run_report_suite.sh           # logs to results/report_suite_<timestamp>.log
set -uo pipefail

cd "$(dirname "$0")/.."
RESULTS=benchmarks/results
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$RESULTS/report_suite_${STAMP}.log"
STATUS="$RESULTS/report_suite_${STAMP}.status"
IMAGES="$RESULTS/report_suite_${STAMP}_images.txt"
source benchmarks/suite_common.sh

EDGES=(wasm envoy_lua openresty_inserver apache_inserver)

ALLOW_UNTUNED=0
for arg in "$@"; do
    case "$arg" in
        --allow-untuned) ALLOW_UNTUNED=1 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

guest_tuned() {
    ! grep -q '\[always\]' /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null \
        && [[ -z "$(swapon --noheadings 2>/dev/null)" ]]
}

if ! guest_tuned; then
    if [[ "$ALLOW_UNTUNED" == 1 ]]; then
        warn_if_untuned
    else
        echo "Guest not tuned (THP=always or swap on). Run: sudo ./benchmarks/tune_guest.sh apply" >&2
        echo "or pass --allow-untuned." >&2
        exit 2
    fi
fi

archive_previous() {
    local existing
    existing="$(compgen -G "$RESULTS/archive/*-pre-report-suite" | head -1)"
    if [[ -n "$existing" ]]; then
        echo "=== keeping the existing archive $existing" | tee -a "$LOG"
        return
    fi
    local dest="$RESULTS/archive/$(date -u +%F)-pre-report-suite"
    mkdir -p "$dest"
    local edge f
    for edge in "${EDGES[@]}"; do
        for f in "paired_${edge}.json" "get_${edge}.json" "size_${edge}.json"; do
            [[ -f "$RESULTS/$f" ]] && cp -p "$RESULTS/$f" "$dest/"
        done
    done
    for f in paired_summary.json functional_matrix.json functional_matrix.md mechanisms.md \
             plots/mechanism_cpu.png plots/size_sweep.png; do
        [[ -f "$RESULTS/$f" ]] && cp -p "$RESULTS/$f" "$dest/"
    done
    cat > "$dest/README.md" <<'EOF'
# `results/archive/<date>-pre-report-suite/`

The result files of the thesis report's four variants (Envoy+Lua, Envoy+WASM, OpenResty in-server,
Apache in-server) as they stood before `benchmarks/run_report_suite.sh` re-measured all of them in
one session. Copied by that script before its first step; nothing here is read by any runner.

| Files | Produced by | Measured |
|---|---|---|
| `paired_wasm.json`, `paired_envoy_lua.json` | `run_paired_suite.sh` | 2026-10-14/15 |
| `paired_openresty_inserver.json`, `paired_apache_inserver.json` | `run_inserver_suite.sh` | 2026-10-18/19 |
| `get_*.json`, `size_*.json` | `run_paired_benchmark.py --mix get / size` | 2026-10-05 (Envoy only) |
| `functional_matrix.*` | `functional_matrix.py` | 2026-10-18 |
| `paired_summary.json`, `mechanisms.md`, plots | `analyze_paired.py`, `plot_mechanisms.py` | derived from the above |

Data flow: these are inputs that were replaced, kept so that numbers quoted from them stay
traceable. The proxy variants of OpenResty and Apache are not affected by the suite and were not
copied.
EOF
    echo "=== archived previous results to $dest" | tee -a "$LOG"
}

record_images() {
    local image
    for image in openresty/openresty:latest envoyproxy/envoy:v1.30-latest httpd:2.4-alpine \
                 nginx:alpine grafana/k6:latest; do
        docker image inspect "$image" --format "$image {{.Id}} created={{.Created}}" >> "$IMAGES" 2>&1
    done
    echo "=== image identifiers recorded in $IMAGES" | tee -a "$LOG"
}

echo "$(date -u +%FT%TZ) START report suite ${STAMP}" >> "$STATUS"
archive_previous
record_images

step bash -c "set -o pipefail; python3 -u benchmarks/parity_check.py | tee $RESULTS/parity_check_$(date -u +%F).txt"
step python3 -u benchmarks/functional_matrix.py

for edge in "${EDGES[@]}"; do
    step python3 -u benchmarks/run_paired_benchmark.py --preset full --edge "$edge"
done
for edge in "${EDGES[@]}"; do
    step python3 -u benchmarks/run_paired_benchmark.py --edge "$edge" --mix get --rates 100 --replicates 3
    step python3 -u benchmarks/run_paired_benchmark.py --edge "$edge" --mix size --rates 10 --replicates 3
done

step python3 -u benchmarks/analyze_paired.py
step python3 -u benchmarks/plot_mechanisms.py

echo "$(date -u +%FT%TZ) DONE" >> "$STATUS"
echo "=== $(date -u +%H:%M:%S) done. Log: $LOG  Status: $STATUS" | tee -a "$LOG"
