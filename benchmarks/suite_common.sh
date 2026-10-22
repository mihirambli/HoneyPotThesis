#!/usr/bin/env bash
# Shared by run_full_suite.sh and run_sqli_suite.sh: the guest-tuning check and the retrying step
# runner. Source it after setting LOG and STATUS.
#
# Built for unattended runs: a step that fails is retried once, and if it fails again the suite
# moves on rather than stopping, because each step writes its own result files and a missing one
# should not cost the rest of the run. Every step's outcome is appended to $STATUS.

# The guest tuning is what keeps the millisecond-range preemption samples out of the data; a run
# without it is still gated, but rejects more cells and widens every interval. Warn, don't refuse:
# the operator may have tuned the guest some other way.
warn_if_untuned() {
    if grep -q '\[always\]' /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null \
        || [[ -n "$(swapon --noheadings 2>/dev/null)" ]]; then
        echo "WARNING: guest not tuned (THP=always or swap on). Run: sudo ./benchmarks/tune_guest.sh apply" \
            | tee -a "$LOG"
    fi
}

step() {
    local attempt
    for attempt in 1 2; do
        echo "=== $(date -u +%H:%M:%S) [attempt $attempt] $*" | tee -a "$LOG"
        if "$@" 2>&1 | tee -a "$LOG"; then
            echo "$(date -u +%FT%TZ) OK     $*" >> "$STATUS"
            return 0
        fi
        echo "$(date -u +%FT%TZ) FAILED attempt $attempt: $*" >> "$STATUS"
        # A failed step can leave containers behind that the next attempt would trip over.
        docker compose --profile openresty --profile envoy --profile wasm --profile apache \
            --profile openresty-inserver --profile apache-inserver \
            --profile loadtest down --remove-orphans >> "$LOG" 2>&1 || true
    done
    echo "$(date -u +%FT%TZ) GAVE UP $*" >> "$STATUS"
    return 0
}
