#!/usr/bin/env bash
# Guest-side noise reduction for a benchmark session, and the matching revert.
#
# Each setting below was chosen against a specific artefact observed in this repo's results, not
# as generic "performance tuning". Run `apply` before a measurement run and `revert` after; both
# are idempotent, and `show` prints the current state without changing anything.
#
# What this CANNOT fix, because it lives outside the guest: VirtualBox gives the guest no control
# over which physical core a vCPU runs on, and this host is an i7-13650HX with 6 performance cores
# and 8 efficiency cores. A vCPU migrating from a P-core to an E-core mid-measurement produces a
# step change of roughly 40% in per-operation cost that no guest setting can suppress. See
# docs/BENCHMARK_METHODOLOGY.md for the host-side commands that pin the VM to P-cores.
#
# Usage: sudo ./benchmarks/tune_guest.sh apply | revert | show

set -euo pipefail

STATE_DIR=/var/lib/wadm-bench
STATE_FILE="$STATE_DIR/saved-state"

need_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "This must run as root (it writes /sys and /proc tunables): sudo $0 $*" >&2
        exit 1
    fi
}

read_first_word() { awk '{print $1; exit}' "$1" 2>/dev/null || echo "?"; }

show() {
    echo "── Current guest state ─────────────────────────────────────────────"
    printf '%-34s %s\n' "transparent_hugepage/enabled" \
        "$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || echo n/a)"
    printf '%-34s %s\n' "transparent_hugepage/defrag" \
        "$(cat /sys/kernel/mm/transparent_hugepage/defrag 2>/dev/null || echo n/a)"
    printf '%-34s %s\n' "vm.swappiness" "$(sysctl -n vm.swappiness 2>/dev/null || echo n/a)"
    printf '%-34s %s\n' "swap in use" "$(awk 'NR==2{print $3"/"$2" KB"}' /proc/swaps 2>/dev/null || echo none)"
    printf '%-34s %s\n' "kernel.randomize_va_space" "$(sysctl -n kernel.randomize_va_space 2>/dev/null || echo n/a)"
    printf '%-34s %s\n' "net.core.somaxconn" "$(sysctl -n net.core.somaxconn 2>/dev/null || echo n/a)"
    printf '%-34s %s\n' "nf_conntrack_max" \
        "$(sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null || echo 'n/a (module not loaded)')"
    printf '%-34s %s\n' "clocksource" \
        "$(cat /sys/devices/system/clocksource/clocksource0/current_clocksource 2>/dev/null || echo n/a)"
    printf '%-34s %s\n' "loadavg (1m)" "$(read_first_word /proc/loadavg)"
    printf '%-34s %s\n' "CPU steal (since boot)" \
        "$(awk '/^cpu /{print $9" jiffies"}' /proc/stat)"
    echo
    echo "Services that commonly wake up mid-run on a desktop Kali image:"
    for unit in packagekit.service apt-daily.timer apt-daily-upgrade.timer \
                man-db.timer updatedb.timer tracker-miner-fs-3.service; do
        # is-active exits non-zero for both "inactive" and "no such unit", so the exit status
        # cannot distinguish them; the empty stdout of a missing unit can.
        state=$(systemctl is-active "$unit" 2>/dev/null || true)
        printf '  %-38s %s\n' "$unit" "${state:-absent}"
    done
}

apply() {
    need_root
    mkdir -p "$STATE_DIR"
    if [[ ! -f "$STATE_FILE" ]]; then
        {
            echo "thp_enabled=$(sed 's/.*\[\(.*\)\].*/\1/' /sys/kernel/mm/transparent_hugepage/enabled)"
            echo "thp_defrag=$(sed 's/.*\[\(.*\)\].*/\1/' /sys/kernel/mm/transparent_hugepage/defrag)"
            echo "swappiness=$(sysctl -n vm.swappiness)"
            echo "somaxconn=$(sysctl -n net.core.somaxconn)"
        } > "$STATE_FILE"
        echo "Saved prior state to $STATE_FILE"
    fi

    # THP was `[always]` here. Khugepaged's collapse and the synchronous compaction a fault can
    # trigger both stall the faulting thread for milliseconds. All four edges time their regions
    # with a wall clock, so a stall inside a region is recorded as WADM cost: this is a direct
    # contributor to the millisecond-range samples that pushed Apache's mean detection cost to
    # 552 us against a 5 us median.
    echo never > /sys/kernel/mm/transparent_hugepage/enabled
    echo never > /sys/kernel/mm/transparent_hugepage/defrag
    echo "THP: disabled (was in saved state)"

    # Swapping a container's pages out mid-run adds tens of milliseconds to whichever request
    # faults them back in. 7.8 GB is far more than this workload needs, so swap should never be
    # touched — turning it off makes that a guarantee rather than a hope.
    sysctl -qw vm.swappiness=1
    if swapon --show --noheadings 2>/dev/null | grep -q .; then
        swapoff -a && echo "Swap: disabled for this session"
    else
        echo "Swap: already off"
    fi

    # The accept queue must not be the bottleneck at the top of the capacity ladder, or the
    # measurement records the kernel refusing connections rather than the edge saturating.
    sysctl -qw net.core.somaxconn=4096
    sysctl -qw net.ipv4.tcp_max_syn_backlog=4096
    # TIME_WAIT reuse: the capacity runs open connections far faster than the default ephemeral
    # port recycle allows, and exhaustion shows up as client-side errors attributed to the edge.
    sysctl -qw net.ipv4.ip_local_port_range="10240 65535"
    echo "Network: somaxconn=4096, syn_backlog=4096, wide ephemeral port range"

    if [[ -w /proc/sys/net/netfilter/nf_conntrack_max ]]; then
        sysctl -qw net.netfilter.nf_conntrack_max=262144
        echo "conntrack: max raised to 262144 (Docker bridge NATs every connection)"
    fi

    # Periodic desktop maintenance is the classic cause of one VU level being measured against a
    # busy machine while its pair was not. Stopping the timers removes the possibility.
    for unit in apt-daily.timer apt-daily-upgrade.timer man-db.timer updatedb.timer \
                packagekit.service tracker-miner-fs-3.service; do
        if systemctl is-active --quiet "$unit" 2>/dev/null; then
            systemctl stop "$unit" 2>/dev/null && echo "Stopped $unit"
        fi
    done

    echo
    echo "Applied. Remember the host-side half (P-core pinning) — see docs/BENCHMARK_METHODOLOGY.md"
}

revert() {
    need_root
    if [[ ! -f "$STATE_FILE" ]]; then
        echo "No saved state at $STATE_FILE; nothing to revert." >&2
        exit 1
    fi
    # shellcheck disable=SC1090
    source "$STATE_FILE"
    echo "${thp_enabled:-always}" > /sys/kernel/mm/transparent_hugepage/enabled
    echo "${thp_defrag:-madvise}" > /sys/kernel/mm/transparent_hugepage/defrag
    sysctl -qw vm.swappiness="${swappiness:-60}"
    sysctl -qw net.core.somaxconn="${somaxconn:-4096}"
    swapon -a 2>/dev/null || true
    for unit in apt-daily.timer apt-daily-upgrade.timer man-db.timer updatedb.timer; do
        systemctl start "$unit" 2>/dev/null || true
    done
    rm -f "$STATE_FILE"
    echo "Reverted to pre-benchmark state."
}

case "${1:-show}" in
    apply) apply ;;
    revert) revert ;;
    show) show ;;
    *) echo "Usage: $0 apply|revert|show" >&2; exit 2 ;;
esac
