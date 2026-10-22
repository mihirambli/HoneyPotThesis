#!/usr/bin/env python3
"""
Turn paired_<edge>.json into paired statistics, a console report, and legacy-shaped result files.

Three things happen here that the previous analysis could not do:

  1. **Differences are taken within a replicate, then summarised across replicates.** The old
     figures subtracted one tier's pooled median from the other's, where the two came from runs
     half an hour apart. Here each replicate yields one difference measured minutes apart, and the
     reported statistic is the median of those differences with a percentile bootstrap interval.

  2. **A difference whose interval spans zero is reported as "below noise floor".** That is the
     honest description of most of the low-rate end-to-end deltas: the effect is 5-30 us and the
     measurement resolves hundreds of microseconds, so the sign is not knowable. Drawing such a
     value as a negative overhead bar claims a speed-up the data does not contain.

  3. **CPU microseconds per request is reported alongside latency.** It is the same underlying
     cost, measured on a counter that queueing does not touch, so it stays significant at rates
     where the latency difference does not. It is NOT immune to hypervisor preemption on this
     VirtualBox guest: steal time is not accounted (/proc/stat steal stays 0), so a vCPU the host
     takes away mid-request is billed to whatever task was running. Pairing handles that
     statistically; the counter alone does not.

  4. **The difference is reported at several quantiles, not only the median.** WADM stretches the
     latency tail more than it shifts the middle (p90 deltas are 1.3-2.6x the median delta), so a
     median-only report understates what a client experiences.

  5. **The no-proxy floor is paired too.** Where the run has an origin tier (k6 straight to the
     origin, edge idle, same replicate), bare - origin is what putting the proxy in the path costs
     and silent - origin what the deployed stack costs, with the same statistics as silent - bare.
     Edge CPU is not compared against it: the origin tier has no edge to measure.
     For an in-server variant there is no proxy: bare - origin is the web server serving its own
     pages against the nginx:alpine origin, and is labelled "bare server - origin".

The slot table is a harness self-check, not a result: with `WADM_ORDER=rotate` every probe spends
an equal number of iterations in every position, so the eight slot medians must agree. A spread
there means the arrival pattern is still shaping the per-probe figures.

Intervals are printed as the RANGE of the paired replicates with a sign-agreement count: with five
replicates a percentile bootstrap of the median returns exactly [min, max] (see bootstrap_ci).

Writes:
  benchmarks/results/paired_summary.json          per-edge, per-rate deltas with intervals
  benchmarks/results/internal_<edge>_profile.json  pooled, legacy schema (existing plotters)
  benchmarks/results/internal_<edge>_raw.json      pooled, legacy schema (existing plotters)
  benchmarks/results/e2e_<edge>_<tier>.json        pooled, legacy schema (existing plotters)

Usage:
    python3 benchmarks/analyze_paired.py
    python3 benchmarks/analyze_paired.py --write-legacy    # also regenerate legacy-schema files
"""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import date
from pathlib import Path
from typing import Any

from edge_specs import (
    EDGE_KEYS,
    EDGES,
    INSERVER_KEYS,
    ORIGIN_TIER,
    PAIRED_TIERS,
    VARIANT_KEYS,
    WADM_TIERS,
)
from wadm_timings import (
    ALL_KINDS,
    assess_run,
    E2E_TRENDS,
    PHASES,
    duration_seconds,
    paired_delta,
    summarize,
)

E2E_METRICS = [t for t in E2E_TRENDS if t != "http_req_duration"]
SLOT_METRICS = [f"slot_{i}_duration" for i in range(1, 9)]

# Internal timings are reported at the 5%-trimmed mean, never the plain mean and no longer the
# median. The plain mean of a wall-clock-timed region on a contended VM is set by its preemption
# tail: Apache's html_comments detection had a median of 5 us and a mean of 552 us at the top rate,
# because 2% of samples landed above 1 ms. The median avoids that but has a different defect here:
# the edges log whole microseconds and most regions cost 1-5 us, so a median can only ever be an
# integer and cannot resolve the sub-microsecond differences between edges. Quantisation phase
# varies from sample to sample, so a mean over many samples recovers sub-tick resolution, and
# trimming 5% from each end removes the tail the validity gate already bounds at 1%.
INTERNAL_STAT = "trimmed_mean_us"

# The four GETs every edge proxies and injects into. Their deltas are comparable across edges; the
# SQLi POSTs are not, because the trap removes the origin round-trip on three edges and not on WASM.
GET_METRICS = [
    "inject_get_duration",
    "detect_query_duration",
    "token_tamper_duration",
    "token_decoy_duration",
]

# Quantiles at which the paired tier difference is reported. The median says how much a typical
# request slows; the upper quantiles say whether WADM shifts the whole distribution or stretches its
# tail, and in this dataset they differ by 1.3-2.6x at p90 and by ~70x on Apache at 100/s.
DELTA_QUANTILES = ["med", "p75", "p90", "p95"]

# p99 of a single probe rests on too few samples to be stable at low rates (90 per probe per cell at
# rate 1, so p99 is effectively the maximum). It is reported only pooled over the four GETs, and only
# from this rate upward, where each probe contributes >= 450 samples per cell.
POOLED_P99_MIN_RATE = 10

# Timing samples each WADM kind should emit per iteration, given test.js's probe set. Injection fires
# on every proxied HTML response — the unrecorded priming GET plus the four measured GETs, hence 5 —
# except form_fields, whose </form> anchor exists only on /login.html. Every kind's detect region
# fires once per iteration on the probe that carries its trigger, and each SQLi arm once. A ratio
# below 1 means requests that took the WADM path left no record: a crashed worker, a dropped log
# line, or a skipped injection. Update this table whenever test.js's probes or config.json's token
# placement change.
EXPECTED_SAMPLES_PER_ITERATION = {
    "html_comments": {"detect": 1, "inject": 5},
    "http_headers": {"detect": 1, "inject": 5},
    "cookies": {"detect": 1, "inject": 5},
    "decoy_paths": {"detect": 1, "inject": 5},
    "form_fields": {"detect": 1, "inject": 1},
    "sql_injection": {"detect": 1},
    "sql_injection_encoded": {"detect": 1},
    "sql_injection_miss": {"detect": 1},
    "sql_injection_miss_encoded": {"detect": 1},
}

CPU_FIELDS = ["cpu_us_per_request", "user_us_per_request", "system_us_per_request"]


def load_paired(results_dir: Path, edge: str) -> dict[str, Any] | None:
    path = results_dir / f"paired_{edge}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def reassess(doc: dict[str, Any]) -> int:
    """Re-run the validity gate over stored cells, without re-running the benchmark.

    The gate encodes judgement, and judgement changes — the crash rule was tightened to a warning
    once it was clear that Apache's segfaults damage about 0.03% of a run while leaving the median
    untouched. Everything the gate reads is already persisted per cell, so a gate change can be
    applied to existing data instead of costing hours of re-measurement.
    """
    changed = 0
    for cell in doc["cells"]:
        k6 = cell.get("k6") or {}
        if not k6.get("iterations"):
            continue
        summary = {
            "iterations": k6.get("iterations"),
            "dropped_iterations": k6.get("dropped_iterations"),
            "http_req_failed_rate": k6.get("http_req_failed_rate"),
        }
        crashes = (cell.get("errors") or {}).get("crash_indicators") or []
        fresh = assess_run(
            cell["rate"], cell["duration"], summary,
            crash_patterns=crashes,
            worst_tail_pct=cell["validity"].get("worst_tail_pct"),
            # Not recorded by earlier runs; it only shapes the warning text, never the verdict.
            crash_count=cell["validity"].get("crash_count") or len(crashes),
            worst_tail_count=cell["validity"].get("worst_tail_count") or 0,
            # The origin control was recorded from the first run onward, so the gate that reads it
            # can be applied retroactively to data collected before the gate existed.
            origin_cpu_us_per_request=(
                ((cell.get("cpu") or {}).get("origin") or {}).get("cpu_us_per_request")
            ),
            host_stalls=cell.get("host_stalls"),
        )
        if fresh["valid"] != cell["validity"]["valid"]:
            changed += 1
        cell["validity"] = fresh
    return changed


def valid_cells(doc: dict[str, Any], tier: str, rate: int) -> list[dict[str, Any]]:
    return [
        c for c in doc["cells"]
        if c["tier"] == tier and c["rate"] == rate and c["validity"]["valid"]
    ]


def cell_by_replicate(doc: dict[str, Any], tier: str, rate: int) -> dict[int, dict[str, Any]]:
    return {c["replicate"]: c for c in valid_cells(doc, tier, rate)}


def trend_stat(cell: dict[str, Any], metric: str, stat: str = "med") -> float | None:
    stats = (cell.get("k6") or {}).get("trends", {}).get(metric)
    if not stats or not stats.get("count"):
        return None
    return stats.get(stat)


def paired_pairs(
    doc: dict[str, Any], rate: int, low: str = "bare", high: str = "wadm"
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(low, high) cells of every replicate where both tiers are valid, in replicate order."""
    a = cell_by_replicate(doc, low, rate)
    b = cell_by_replicate(doc, high, rate)
    return [(a[r], b[r]) for r in sorted(set(a) & set(b))]


def paired_latency(
    doc: dict[str, Any], rate: int, metric: str, stat: str = "med",
    low: str = "bare", high: str = "wadm",
) -> dict[str, Any]:
    """Per-replicate (high − low) latency differences at one quantile, in milliseconds."""
    deltas = []
    for b_cell, w_cell in paired_pairs(doc, rate, low, high):
        b, w = trend_stat(b_cell, metric, stat), trend_stat(w_cell, metric, stat)
        if b is not None and w is not None:
            deltas.append(w - b)
    return paired_delta(deltas)


def paired_relative(
    doc: dict[str, Any], rate: int, metric: str, low: str = "bare", high: str = "wadm"
) -> dict[str, Any]:
    """Per-replicate median latency added, as a percentage of that replicate's `low` median."""
    deltas = []
    for b_cell, w_cell in paired_pairs(doc, rate, low, high):
        b, w = trend_stat(b_cell, metric), trend_stat(w_cell, metric)
        if b and w is not None:
            deltas.append(100.0 * (w - b) / b)
    return paired_delta(deltas)


def paired_get_mean(
    doc: dict[str, Any], rate: int, stat: str, low: str = "bare", high: str = "wadm"
) -> dict[str, Any]:
    """Latency added per GET at one quantile, averaged over the four GET probes within a replicate.

    Averaging before differencing across replicates pools four probes' worth of samples into each
    replicate's figure, which is what makes an upper quantile like p99 stable enough to report.
    """
    deltas = []
    for b_cell, w_cell in paired_pairs(doc, rate, low, high):
        per_probe = [
            (trend_stat(w_cell, m, stat), trend_stat(b_cell, m, stat)) for m in GET_METRICS
        ]
        if all(w is not None and b is not None for w, b in per_probe):
            deltas.append(statistics.fmean(w - b for w, b in per_probe))
    return paired_delta(deltas)


def paired_get_relative(
    doc: dict[str, Any], rate: int, low: str = "bare", high: str = "wadm"
) -> dict[str, Any]:
    """Per-replicate median latency added per GET, as a percentage of that replicate's `low` level.

    Both sides are the mean of the four GET probes' medians, so the ratio is "how much slower is a
    GET" rather than an average of four ratios dominated by whichever probe is fastest.
    """
    deltas = []
    for b_cell, w_cell in paired_pairs(doc, rate, low, high):
        b, w = get_level(b_cell), get_level(w_cell)
        if b and w is not None:
            deltas.append(100.0 * (w - b) / b)
    return paired_delta(deltas)


def get_level(cell: dict[str, Any], stat: str = "med") -> float | None:
    """One cell's latency per GET in ms: the mean over the four GET probes of each one's `stat`."""
    values = [trend_stat(cell, m, stat) for m in GET_METRICS]
    return statistics.fmean(values) if all(v is not None for v in values) else None


def latency_levels(doc: dict[str, Any], rate: int) -> dict[str, Any]:
    """Absolute median latency per GET for every tier, spread across valid replicates.

    Descriptive, not a comparison: each tier's level is summarised on its own, so two tiers' levels
    differ by session drift as well as by what the tiers do. The paired differences answer the
    question; this says where the floor and each tier sit, and the per-edge origin levels show how
    much the floor itself moved between the four edges' sessions.
    """
    out = {}
    for tier in tiers_of(doc):
        values = [v for v in (get_level(c) for c in valid_cells(doc, tier, rate)) if v is not None]
        if values:
            out[tier] = paired_delta(values)
    return out


def latency_quantile_deltas(
    doc: dict[str, Any], rate: int, low: str = "bare", high: str = "wadm"
) -> dict[str, Any]:
    """Shift function: the paired difference at several quantiles, per probe and per GET."""
    per_probe = {
        metric: {
            stat: paired_latency(doc, rate, metric, stat, low, high) for stat in DELTA_QUANTILES
        }
        for metric in E2E_METRICS
    }
    get_stats = DELTA_QUANTILES + (["p99"] if rate >= POOLED_P99_MIN_RATE else [])
    return {
        "per_probe": per_probe,
        "per_get": {stat: paired_get_mean(doc, rate, stat, low, high) for stat in get_stats},
    }


def paired_cpu(
    doc: dict[str, Any], rate: int, field: str = "cpu_us_per_request",
    low: str = "bare", high: str = "wadm",
) -> dict[str, Any]:
    """Per-replicate (high − low) CPU microseconds per request for the edge container.

    `field` selects total, user or system time. The user/system split separates work done in the
    edge's own code (Lua, WASM, C) from work done in the kernel on its behalf — syscalls such as the
    timing-log writes, which exist only in the WADM tier.
    """
    deltas = []
    for b_cell, w_cell in paired_pairs(doc, rate, low, high):
        b = ((b_cell.get("cpu") or {}).get("edge") or {}).get(field)
        w = ((w_cell.get("cpu") or {}).get("edge") or {}).get(field)
        if b is not None and w is not None:
            deltas.append(w - b)
    return paired_delta(deltas)


def coverage(doc: dict[str, Any], rate: int) -> dict[str, Any]:
    """Timing samples recorded per iteration, relative to what test.js's probes should produce.

    A proxy for "did every request that should have taken the WADM path actually take it". Cells
    from the paired-v2 harness count only lines logged after k6's first measured iteration, so the
    ratio is exact; older cells can exceed 1 by a sample or two because `--since` is second-granular
    and could catch the tail of the warm-up run. A shortfall cannot come from either and is the
    signal. Only the instrumented tier emits timing lines, so only it can be checked this way.
    """
    out: dict[str, Any] = {}
    cells = valid_cells(doc, "wadm", rate)
    for kind, phases in EXPECTED_SAMPLES_PER_ITERATION.items():
        for phase, expected in phases.items():
            ratios = []
            for cell in cells:
                iterations = (cell.get("k6") or {}).get("iterations")
                count = (((cell.get("tokens") or {}).get(kind) or {}).get(phase) or {}).get("count")
                if iterations and count is not None:
                    ratios.append(count / (expected * iterations))
            if ratios:
                out.setdefault(kind, {})[phase] = {
                    "expected_per_iteration": expected,
                    "median_ratio": round(statistics.median(ratios), 4),
                    "worst_ratio": round(min(ratios), 4),
                    "replicates": len(ratios),
                }
    return out


def absolute_cpu(doc: dict[str, Any], tier: str, rate: int) -> dict[str, Any]:
    values = [
        ((c.get("cpu") or {}).get("edge") or {}).get("cpu_us_per_request")
        for c in valid_cells(doc, tier, rate)
    ]
    values = [v for v in values if v is not None]
    return paired_delta(values)


# Eight requests issued back-to-back down one connection are not identical in cost even against a
# static origin: the later ones benefit from state the earlier ones established. Measured against
# the WADM-free origin tier that residual is about 1.3x, and it is irreducible without abandoning
# the eight-probes-per-iteration shape. What matters is that it is COMMON-MODE — under `williams`
# (and the older `rotate`) every probe occupies every position equally often, so the residual lands
# on all of them alike and cancels in a bare-vs-WADM difference. A spread beyond SLOT_SPREAD_ALARM means something else is
# happening, typically a synchronised arrival burst, and the per-probe figures are then unsafe.
SLOT_SPREAD_ALARM = 2.0


def _fit_cpu_model(cells: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Least-squares fit of cpu_usec_total against (window seconds, requests served).

    An absolute cpu_us_per_request divides the container's WHOLE CPU consumption by the requests it
    served, so it also carries the edge's idle cost — worker processes waking on timers, health
    polling, the master process. At a low offered rate that fixed component is amortised over few
    requests and dominates: OpenResty measured 232 us/request at 10 iterations/s, which is mostly
    not per-request work at all.

    Fitting cpu_total ~ idle_per_second * seconds + marginal * requests separates the two, and
    `marginal` is the figure that answers "what does one more request cost". Requires cells at two
    or more rates; a single rate leaves the system underdetermined and returns None.
    """
    rows = []
    rates_seen = set()
    for cell in cells:
        cpu = ((cell.get("cpu") or {}).get("edge") or {})
        k6 = cell.get("k6", {})
        # paired-v2 cells integrate CPU over the measured window only and count only the requests
        # the measured iterations sent; older cells bracket the whole load-tester lifecycle, for
        # which the nominal duration is the closest available regressor.
        reqs = k6.get("edge_requests") or k6.get("http_reqs")
        total = cpu.get("cpu_usec_total")
        seconds = cpu.get("window_s") or duration_seconds(cell.get("duration", ""))
        if total is None or not reqs or not seconds:
            continue
        rows.append((float(seconds), float(reqs), float(total)))
        rates_seen.add(cell["rate"])
    # Two distinct RATES, not merely two cells. Cells at one rate differ only by the handful of
    # iterations k6 happened to place, which is not enough leverage to separate the idle term from
    # the per-request term: the fit converges on an arbitrary split of the two, and has been seen
    # to return a negative idle cost.
    if len(rows) < 2 or len(rates_seen) < 2:
        return None

    s_dd = sum(d * d for d, _, _ in rows)
    s_dr = sum(d * r for d, r, _ in rows)
    s_rr = sum(r * r for _, r, _ in rows)
    s_dy = sum(d * y for d, _, y in rows)
    s_ry = sum(r * y for _, r, y in rows)

    det = s_dd * s_rr - s_dr * s_dr
    # Degenerate when every cell shares one (seconds, requests) ratio, i.e. only one rate was run.
    if abs(det) < 1e-9:
        return None
    idle_per_s = (s_dy * s_rr - s_ry * s_dr) / det
    marginal = (s_ry * s_dd - s_dy * s_dr) / det
    # Both coefficients are CPU time and cannot be negative. A negative one means the fit is not
    # describing the data — too few rates, or a contaminated cell that gating did not catch — and a
    # number derived from it would be worse than none.
    if marginal < 0 or idle_per_s < 0:
        return {
            "marginal_cpu_us_per_request": None,
            "idle_cpu_us_per_second": None,
            "cells_fitted": len(rows),
            "rates_fitted": sorted(rates_seen),
            "unphysical_fit": True,
        }
    return {
        "marginal_cpu_us_per_request": round(marginal, 3),
        "idle_cpu_us_per_second": round(idle_per_s, 1),
        "cells_fitted": len(rows),
        "rates_fitted": sorted(rates_seen),
    }


def marginal_cpu(doc: dict[str, Any], tier: str) -> dict[str, Any] | None:
    """Marginal CPU per request for one tier, fitted across every valid cell at every rate."""
    cells = [c for c in doc["cells"] if c["tier"] == tier and c["validity"]["valid"]]
    return _fit_cpu_model(cells)


def paired_marginal_cpu(
    doc: dict[str, Any], low: str = "bare", high: str = "wadm"
) -> dict[str, Any]:
    """Per-replicate (high - low) marginal CPU per request.

    Fitted within a replicate, so the difference is between two fits taken minutes apart rather
    than between two pooled numbers whose cells were spread across the whole session.
    """
    replicates = sorted({c["replicate"] for c in doc["cells"]})
    deltas = []
    for replicate in replicates:
        fits = {}
        for tier in (low, high):
            cells = [
                c for c in doc["cells"]
                if c["tier"] == tier and c["replicate"] == replicate and c["validity"]["valid"]
            ]
            fits[tier] = _fit_cpu_model(cells)
        bare_m = (fits[low] or {}).get("marginal_cpu_us_per_request")
        wadm_m = (fits[high] or {}).get("marginal_cpu_us_per_request")
        if bare_m is not None and wadm_m is not None:
            deltas.append(wadm_m - bare_m)
    return paired_delta(deltas)


def slot_balance(doc: dict[str, Any], rate: int) -> dict[str, Any]:
    """Spread across the eight iteration positions, and whether rotation neutralised it."""
    meds: dict[str, list[float]] = {m: [] for m in SLOT_METRICS}
    orders: set[str] = set()
    for tier in tiers_of(doc):
        for cell in valid_cells(doc, tier, rate):
            order = (cell.get("k6") or {}).get("order")
            if order:
                orders.add(order)
            for metric in SLOT_METRICS:
                value = trend_stat(cell, metric)
                if value is not None:
                    meds[metric].append(value)
    pooled = {m: statistics.median(v) for m, v in meds.items() if v}
    if len(pooled) < 2:
        return {"available": False}
    lo, hi = min(pooled.values()), max(pooled.values())
    ratio = round(hi / lo, 3) if lo else None
    rotated = len(orders) == 1 and orders <= {"williams", "rotate", "shuffle"}
    return {
        "available": True,
        "orders_seen": sorted(orders),
        "rotation_active": rotated,
        "slot_medians_ms": {m: round(v, 4) for m, v in pooled.items()},
        "spread_ratio": ratio,
        "spread_ms": round(hi - lo, 4),
        # Balanced means the positional residual is shared equally by all probes, not that it is
        # absent. Unbalanced means a probe is pinned to a position and its figure is confounded.
        "balanced": bool(rotated and ratio is not None and ratio < SLOT_SPREAD_ALARM),
    }


def internal_stats(doc: dict[str, Any], rate: int) -> dict[str, Any]:
    """Per-kind, per-phase internal microsecond timings, pooled across valid WADM replicates."""
    out: dict[str, Any] = {}
    for kind in ALL_KINDS:
        for phase in PHASES:
            samples: list[int] = []
            for cell in valid_cells(doc, "wadm", rate):
                samples.extend((cell.get("tokens_raw") or {}).get(kind, {}).get(f"{phase}_us", []))
            if samples:
                out.setdefault(kind, {})[phase] = summarize(samples).to_json()
    return out


# Every difference the analysis reports, as (name, lower tier, higher tier). `silent` is WADM as a
# deployment would run it (no benchmark timing lines), so silent - bare is the headline cost of
# WADM; wadm - silent is the cost of the instrumentation; wadm - bare is what the two-tier harness
# reported, kept for continuity with earlier results. The two origin comparisons place both against
# the no-proxy floor: bare - origin is the proxy alone, silent - origin the deployed stack. They come
# last so headline_pair never picks one while an edge-to-edge comparison exists.
COMPARISONS = [
    ("silent_minus_bare", "bare", "silent"),
    ("wadm_minus_silent", "silent", "wadm"),
    ("wadm_minus_bare", "bare", "wadm"),
    ("bare_minus_origin", ORIGIN_TIER, "bare"),
    ("silent_minus_origin", ORIGIN_TIER, "silent"),
]

# Alert and trap lines one iteration of the `all` mix should produce on a WADM tier: the
# html_comments trigger (1), the tamper probe's form_fields, http_headers and cookies hits (3), the
# decoy path (1) and the two SQLi signature hits (2) are ALERTs; the two clean logins are TRAPs.
# Update with test.js's probes or config.json's tokens.
EXPECTED_ALERTS_PER_ITERATION = {"alert": 7, "trap": 2}


COMPARISON_LABELS = {
    "silent_minus_bare": "WADM (silent) - bare",
    "wadm_minus_silent": "timing lines (wadm - silent)",
    "wadm_minus_bare": "WADM (instrumented) - bare",
    "bare_minus_origin": "bare proxy - origin",
    "silent_minus_origin": "WADM (silent) - origin",
}

# In the in-server deployment the bare tier is not a proxy: it is the web server serving the pages
# itself, so bare - origin compares two file servers rather than measuring a proxy hop.
INSERVER_LABELS = {"bare_minus_origin": "bare server - origin"}


def is_inserver(doc_or_summary: dict[str, Any]) -> bool:
    spec = EDGES.get((doc_or_summary.get("metadata") or {}).get("edge", ""))
    return bool(spec and spec.deployment == "inserver")


def comparison_label(name: str, inserver: bool) -> str:
    return (INSERVER_LABELS.get(name) if inserver else None) or COMPARISON_LABELS[name]


def tiers_of(doc: dict[str, Any]) -> list[str]:
    present = {c["tier"] for c in doc["cells"]}
    return [t for t in PAIRED_TIERS if t in present]


def edge_tiers_of(doc: dict[str, Any]) -> list[str]:
    """The tiers that have an edge in the request path, and so an edge CPU counter to compare."""
    return [t for t in tiers_of(doc) if t != ORIGIN_TIER]


def headline_pair(doc: dict[str, Any]) -> tuple[str, str, str]:
    """The comparison reported as WADM's cost: silent - bare when the silent tier exists."""
    tiers = tiers_of(doc)
    for name, low, high in COMPARISONS:
        if low in tiers and high in tiers and name != "wadm_minus_silent":
            return name, low, high
    return "wadm_minus_bare", "bare", "wadm"


def comparison_block(doc: dict[str, Any], rate: int, low: str, high: str) -> dict[str, Any]:
    block = {
        "replicates_paired": len(paired_pairs(doc, rate, low, high)),
        "latency_delta_ms": {
            m: paired_latency(doc, rate, m, "med", low, high) for m in E2E_METRICS
        },
        "latency_delta_pct": {m: paired_relative(doc, rate, m, low, high) for m in E2E_METRICS},
        "latency_delta_pct_per_get": paired_get_relative(doc, rate, low, high),
        "latency_delta_quantiles_ms": latency_quantile_deltas(doc, rate, low, high),
    }
    if low != ORIGIN_TIER:
        block["cpu_delta_us_per_request"] = paired_cpu(doc, rate, "cpu_us_per_request", low, high)
        block["cpu_delta_split_us_per_request"] = {
            field.removesuffix("_us_per_request"): paired_cpu(doc, rate, field, low, high)
            for field in CPU_FIELDS[1:]
        }
    return block


def _median(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 4) if values else None


def functional_stats(doc: dict[str, Any], rate: int) -> dict[str, Any]:
    """Does each tier still do what the server did without WADM, under this load?

    Per tier and probe:
      intact            share of responses identical to the reference the same edge returned
                        before the load started (status and body hash). Below 1 means responses
                        were corrupted, truncated or left un-injected under load.
      new_conn          share of requests that opened a new connection instead of reusing one.
      matches_origin    whether the edge's reference response equals the origin's. Must hold on
                        bare; must NOT hold for proxied probes on a WADM tier (they carry tokens).
      bait              whether the injected header, cookie and comment were present.
    And per tier, the alert lines logged in the measured window against what the probes should
    produce (detection recall) — zero expected on bare.
    """
    out: dict[str, Any] = {}
    inserver = is_inserver(doc)
    for tier in tiers_of(doc):
        cells = valid_cells(doc, tier, rate)
        if not cells:
            continue
        probes: dict[str, Any] = {}
        names = sorted({n for c in cells for n in ((c.get("k6") or {}).get("intact") or {})})
        for name in names:
            intact = [((c["k6"].get("intact") or {}).get(name) or {}).get("rate") for c in cells]
            conn = [((c["k6"].get("new_conn") or {}).get(name) or {}).get("rate") for c in cells]
            refs = [(c["k6"].get("reference") or {}).get(name) or {} for c in cells]
            probes[name] = {
                "intact_median": _median(intact),
                "intact_worst": min((v for v in intact if v is not None), default=None),
                "new_conn_median": _median(conn),
                "matches_origin_all": all(r.get("matches_origin") for r in refs) if refs else None,
                "baited": any(r.get("baited") for r in refs),
                "bait_all_present": (
                    all(all((r.get("bait") or {}).values()) for r in refs) if refs else None
                ),
            }
            if inserver:
                # An in-server variant generates its own error pages, so a non-200 probe's body
                # differs from nginx:alpine's by construction; the report skips those.
                statuses = {(r.get("origin") or {}).get("status") for r in refs}
                probes[name]["origin_status"] = statuses.pop() if len(statuses) == 1 else None
        recall: dict[str, Any] = {}
        for kind, per_iteration in EXPECTED_ALERTS_PER_ITERATION.items():
            ratios = []
            for c in cells:
                counted = (c.get("alerts") or {}).get(kind)
                iterations = (c.get("k6") or {}).get("iterations")
                if counted is None or not iterations:
                    continue
                expected = per_iteration * iterations if tier in WADM_TIERS else 0
                ratios.append(counted / expected if expected else float(counted))
            if ratios:
                recall[kind] = {
                    "median": _median(ratios),
                    "worst": round(min(ratios) if tier in WADM_TIERS else max(ratios), 4),
                    "meaning": "lines logged / lines expected" if tier in WADM_TIERS
                               else f"lines logged ({tier}: count, expected 0)",
                }
        timing = [c.get("timing_lines") for c in cells if c.get("timing_lines") is not None]
        out[tier] = {
            "probes": probes,
            "alerts": recall,
            # Must be 0 on bare and silent: the tier switch's own self-check.
            "timing_lines_max": max(timing) if timing else None,
        }
    return out


def resources(doc: dict[str, Any], rate: int) -> dict[str, Any]:
    """Per tier: the edge's cores in use, idle CPU, memory, and the load generator's cores."""
    out: dict[str, Any] = {}
    for tier in tiers_of(doc):
        cells = valid_cells(doc, tier, rate)
        edge = [((c.get("cpu") or {}).get("edge") or {}) for c in cells]
        loadgen = [((c.get("cpu") or {}).get("loadgen") or {}) for c in cells]
        out[tier] = {
            "edge_cores_used": _median([e.get("cores_used") for e in edge]),
            "edge_idle_cpu_us_per_s": _median([e.get("idle_cpu_us_per_s") for e in edge]),
            "edge_memory_mb_max": _median(
                [e["memory_bytes_max"] / 2**20 for e in edge if e.get("memory_bytes_max")]
            ),
            "loadgen_cores_used": _median([g.get("cores_used") for g in loadgen]),
        }
    return out


def analyse_edge(doc: dict[str, Any]) -> dict[str, Any]:
    rates = sorted({c["rate"] for c in doc["cells"]})
    tiers = tiers_of(doc)
    edge_tiers = edge_tiers_of(doc)
    head_name, head_low, head_high = headline_pair(doc)
    comparisons = [(n, lo, hi) for n, lo, hi in COMPARISONS if lo in tiers and hi in tiers]
    edge_comparisons = [(n, lo, hi) for n, lo, hi in comparisons if lo != ORIGIN_TIER]
    per_rate: dict[str, Any] = {}
    for rate in rates:
        cells = [c for c in doc["cells"] if c["rate"] == rate]
        rejected = [
            {
                "tier": c["tier"], "replicate": c["replicate"],
                "reasons": c["validity"]["reasons"],
            }
            for c in cells if not c["validity"]["valid"]
        ]
        warned = [
            {
                "tier": c["tier"], "replicate": c["replicate"],
                "warnings": c["validity"].get("warnings") or [],
            }
            for c in cells
            if c["validity"]["valid"] and c["validity"].get("warnings")
        ]
        blocks = {name: comparison_block(doc, rate, lo, hi) for name, lo, hi in comparisons}
        headline = blocks[head_name]
        per_rate[str(rate)] = {
            # The headline comparison's figures stay at the top level, under the keys the plotters
            # read, so a two-tier file and a three-tier file are drawn the same way.
            "headline_comparison": head_name,
            "replicates_paired": headline["replicates_paired"],
            "rejected_cells": rejected,
            "warned_cells": warned,
            "latency_delta_ms": headline["latency_delta_ms"],
            "latency_delta_pct": headline["latency_delta_pct"],
            "latency_delta_quantiles_ms": headline["latency_delta_quantiles_ms"],
            "cpu_delta_us_per_request": headline["cpu_delta_us_per_request"],
            "cpu_delta_split_us_per_request": headline["cpu_delta_split_us_per_request"],
            "comparisons": blocks,
            "coverage": coverage(doc, rate),
            "functional": functional_stats(doc, rate),
            "resources": resources(doc, rate),
            "cpu_absolute_us_per_request": {
                tier: absolute_cpu(doc, tier, rate) for tier in edge_tiers
            },
            "latency_levels_ms": latency_levels(doc, rate),
            "slot_balance": slot_balance(doc, rate),
            "internal_us": internal_stats(doc, rate),
        }
    return {
        "metadata": doc["metadata"],
        "tiers": tiers,
        "headline_comparison": head_name,
        "cpu_marginal": {
            "per_tier": {tier: marginal_cpu(doc, tier) for tier in edge_tiers},
            **{name: paired_marginal_cpu(doc, lo, hi) for name, lo, hi in edge_comparisons},
            "headline": paired_marginal_cpu(doc, head_low, head_high),
        },
        "rates": per_rate,
    }


# ── Console report ───────────────────────────────────────────────────────────────────────────

def verdict(delta: dict[str, Any], unit: str) -> str:
    """One line per difference, distinguishing "measured as zero" from "not measured enough".

    The two need separate wording. An interval that spans zero is a result: the effect is smaller
    than this harness resolves. Too few replicates to form an interval is not a result at all, and
    reporting it in the same words would let a smoke run masquerade as a finding.

    The interval is printed as the RANGE of the paired replicates, because that is what a percentile
    bootstrap of a five-point median returns (see `bootstrap_ci`), together with how many replicates
    agree in sign — the plainest statement of the evidence behind "resolved".
    """
    if not delta.get("n"):
        return "no paired data"
    if not delta.get("ci_reliable"):
        return (
            f"{delta['median']:+.3f}{unit} point estimate only "
            f"(n={delta['n']}, too few replicates for an interval)"
        )
    if not delta["significant"]:
        return (
            f"below noise floor (median {delta['median']:+.3f}{unit}, "
            f"range {delta['ci_low']:+.3f}..{delta['ci_high']:+.3f}, "
            f"{delta.get('sign_agreement', '?')} agree in sign)"
        )
    return (
        f"{delta['median']:+.3f}{unit} "
        f"(range {delta['ci_low']:+.3f}..{delta['ci_high']:+.3f}, "
        f"{delta.get('sign_agreement', '?')} agree in sign)"
    )


def report(summary: dict[str, Any]) -> None:
    label = summary["metadata"]["label"]
    inserver = is_inserver(summary)
    print(f"\n{'=' * 78}\n{label}\n{'=' * 78}")

    marginal = summary["cpu_marginal"]
    for tier, fit in marginal["per_tier"].items():
        if fit and fit.get("marginal_cpu_us_per_request") is not None:
            print(
                f"  Marginal CPU, {tier:5s}: {fit['marginal_cpu_us_per_request']:.2f} us/request"
                f"   (idle {fit['idle_cpu_us_per_second']:.0f} us/s,"
                f" rates {fit['rates_fitted']})"
            )
        elif fit and fit.get("unphysical_fit"):
            print(f"  Marginal CPU, {tier:5s}: fit rejected (negative coefficient)")
    head = summary.get("headline_comparison", "wadm_minus_bare")
    for name, _, _ in COMPARISONS:
        delta = marginal.get(name)
        if delta and delta.get("n"):
            tag = "  <- headline" if name == head else ""
            print(f"  Marginal CPU, {comparison_label(name, inserver)}: "
                  f"{verdict(delta, ' us/req')}{tag}")
    if not any((marginal.get(n) or {}).get("n") for n, _, _ in COMPARISONS):
        print("  Marginal CPU: needs two or more RATES to separate idle from per-request cost")

    for rate, data in summary["rates"].items():
        head_label = comparison_label(data.get("headline_comparison", "wadm_minus_bare"), inserver)
        print(f"\n  rate {rate}/s — {data['replicates_paired']} paired replicates "
              f"(headline: {head_label})")
        for item in data["rejected_cells"]:
            print(f"    ! rejected {item['tier']} rep{item['replicate']}: {'; '.join(item['reasons'])}")
        for item in data.get("warned_cells", []):
            print(f"    ~ WARN {item['tier']} rep{item['replicate']}: {'; '.join(item['warnings'])}")

        for name, block in (data.get("comparisons") or {}).items():
            if "cpu_delta_us_per_request" not in block:
                continue
            cpu = block["cpu_delta_us_per_request"]
            split = block["cpu_delta_split_us_per_request"]
            parts = ", ".join(
                f"{part} {d['median']:+.1f}" for part, d in split.items() if d.get("n")
            )
            print(f"    CPU, {comparison_label(name, inserver):28s} {verdict(cpu, ' us/req')}"
                  + (f"  [{parts}]" if parts else ""))
        for tier, abs_cpu in data["cpu_absolute_us_per_request"].items():
            if abs_cpu.get("n"):
                res = (data.get("resources") or {}).get(tier) or {}
                mem = res.get("edge_memory_mb_max")
                print(f"      {tier:6s} absolute: {abs_cpu['median']:.1f} us/req"
                      + (f", edge memory {mem:.1f} MB" if mem else ""))

        slots = data["slot_balance"]
        if slots.get("available"):
            if not slots["rotation_active"]:
                flag = "   <-- NOT rotated: per-probe figures are confounded by position"
            elif not slots["balanced"]:
                flag = "   <-- spread too large even rotated: suspect arrival bursts"
            else:
                flag = "   (common-mode under rotation, cancels in the delta)"
            print(f"    Slot spread:       {slots['spread_ratio']}x across 8 positions{flag}")

        head_low = next(lo for n, lo, _ in COMPARISONS if n == data.get("headline_comparison"))
        print(f"    End-to-end latency delta ({head_label}), median request:")
        for metric, delta in data["latency_delta_ms"].items():
            pct = data["latency_delta_pct"][metric]
            pct_s = f"  [{pct['median']:+.0f}% of {head_low}]" if pct.get("n") else ""
            print(f"      {metric:24s} {verdict(delta, ' ms')}{pct_s}")

        per_get = data["latency_delta_quantiles_ms"]["per_get"]
        if any(d.get("n") for d in per_get.values()):
            cols = "  ".join(
                f"{stat}={d['median'] * 1000:+.0f}us" for stat, d in per_get.items() if d.get("n")
            )
            print(f"    Latency added per GET, by quantile (shift function): {cols}")

        floor = [(n, b) for n, b in (data.get("comparisons") or {}).items()
                 if n.endswith("_minus_origin")]
        if floor:
            levels = data.get("latency_levels_ms") or {}
            origin = levels.get(ORIGIN_TIER) or {}
            level_s = (f", origin itself {origin['median'] * 1000:.0f}us" if origin.get("n") else "")
            print(f"    Above the origin floor, latency added per GET at the median{level_s}:")
            for name, block in floor:
                delta = block["latency_delta_quantiles_ms"]["per_get"]["med"]
                pct = block["latency_delta_pct_per_get"]
                pct_s = f"  [{pct['median']:+.0f}% of origin]" if pct.get("n") else ""
                print(f"      {comparison_label(name, inserver):24s} {verdict(delta, ' ms')}{pct_s}")

        probe_effect = (data.get("comparisons") or {}).get("wadm_minus_silent")
        if probe_effect:
            cols = "  ".join(
                f"{m.removesuffix('_duration')}={d['median'] * 1000:+.0f}us"
                for m, d in probe_effect["latency_delta_ms"].items()
                if d.get("n") and m in GET_METRICS
            )
            print(f"    Probe effect on latency (timing lines on - off): {cols}")

        functional = data.get("functional") or {}
        for tier, f in functional.items():
            probes = f.get("probes") or {}
            worst = [(n, v["intact_worst"]) for n, v in probes.items() if v["intact_worst"] is not None]
            conn = [(n, v["new_conn_median"]) for n, v in probes.items()
                    if v["new_conn_median"]]
            bad_ref = [n for n, v in probes.items()
                       if tier not in WADM_TIERS and v["matches_origin_all"] is False
                       and (not inserver or v.get("origin_status") == 200)]
            no_bait = [n for n, v in probes.items()
                       if tier in WADM_TIERS and v["baited"] and v["bait_all_present"] is False]
            alerts = f.get("alerts") or {}
            line = f"    Functional {tier:6s}: "
            line += (f"worst intact {min(w for _, w in worst):.2%}" if worst else "intact n/a")
            if conn:
                line += "; reconnects " + ", ".join(f"{n}={r:.0%}" for n, r in conn)
            if alerts:
                line += "; alerts " + ", ".join(
                    f"{k}={v['median']}" for k, v in alerts.items())
            if bad_ref:
                line += f"; DIFFERS FROM ORIGIN: {', '.join(bad_ref)}"
            if no_bait:
                line += f"; BAIT MISSING: {', '.join(no_bait)}"
            print(line)

        cov = data["coverage"]
        short = [
            f"{kind}.{phase}={c['worst_ratio']:.3f}"
            for kind, phases in cov.items() for phase, c in phases.items()
            if c["worst_ratio"] < 1.0
        ]
        if cov:
            print(
                "    Coverage (timing samples / expected): "
                + ("complete on every kind" if not short else "SHORTFALL " + ", ".join(short))
            )

        internal = data["internal_us"]
        if internal:
            print("    Internal timings (5%-trimmed mean us, preemption tail %):")
            for kind, phases in internal.items():
                parts = [
                    f"{phase}={phases[phase][INTERNAL_STAT]:.2f}us"
                    f"/tail={phases[phase]['tail_pct']:.2f}%"
                    for phase in PHASES if phase in phases
                ]
                print(f"      {kind:28s} " + "  ".join(parts))


# ── Legacy-shaped outputs ────────────────────────────────────────────────────────────────────
#
# The existing plotters index `runs[].vus`, `runs[].tokens`, `runs[].k6.trends` and the
# `internal_*` / `e2e_*` filenames. Rewriting them all is a separate job, so the pooled paired data
# is also emitted in that shape, with `vus` carrying the offered rate. Percentile fields are the
# median across replicates of each percentile, which preserves the box the plotters draw.

def pooled_trend(doc: dict[str, Any], tier: str, rate: int, metric: str) -> dict[str, Any] | None:
    per_stat: dict[str, list[float]] = {}
    for cell in valid_cells(doc, tier, rate):
        stats = (cell.get("k6") or {}).get("trends", {}).get(metric)
        if not stats or not stats.get("count"):
            continue
        for key, value in stats.items():
            if value is not None:
                per_stat.setdefault(key, []).append(value)
    if not per_stat:
        return None
    out = {k: round(statistics.median(v), 4) for k, v in per_stat.items()}
    out["count"] = int(sum(per_stat.get("count", [0])))
    return out


# Every result file a regeneration replaces is kept under results/archive/, in one dated folder per
# kind of event, so the archive is the single place to look for any earlier dataset. This used to be
# a separate results/superseded/ directory; one archive replaces it so there are not two.
ARCHIVE_DIRNAME = "archive"
ARCHIVE_LABEL = "write-legacy"

ARCHIVE_README = """# `results/archive/{folder}/`

Result files that `analyze_paired.py --write-legacy` replaced on {date}, kept as they were before
the first overwrite of that day.

## Why this exists

`--write-legacy` regenerates `internal_<edge>_*.json` and `e2e_<edge>_<tier>.json` from paired
data, because those are the filenames and the schema the legacy plotters read. Doing so destroys
whatever dataset produced the figures drawn from them. A copy lands here first.

## Data flow

    paired_<edge>.json            written by run_paired_benchmark.py (the record of truth)
      -> analyze_paired.py --write-legacy
           -> results/archive/{folder}/<name>.json   the file being replaced, copied once
           -> results/<name>.json                    regenerated, pooled across replicates

## Rules

- Copies are made **once per filename per folder**, so re-running the analysis the same day keeps
  the ORIGINAL rather than the second-most-recent version.
- Nothing reads from `archive/`. It is a record, not an input.
- Files from before 2026-09-24 predate the paired methodology, so any overhead derived from them is
  unpaired: see `docs/BENCHMARK_METHODOLOGY.md` for why such a difference is dominated by drift.
"""


def preserve(path: Path) -> None:
    """Copy a result file into today's archive folder before it is replaced, once per day.

    Once, not every time: the point is to keep the ORIGINAL dataset, and re-running the analysis
    three times must not end with three copies of the third one and none of the first.
    """
    if not path.exists():
        return
    today = date.today().isoformat()
    folder = f"{today}-{ARCHIVE_LABEL}"
    archive = path.parent / ARCHIVE_DIRNAME / folder
    archive.mkdir(parents=True, exist_ok=True)
    readme = archive / "README.md"
    if not readme.exists():
        readme.write_text(ARCHIVE_README.format(folder=folder, date=today), encoding="utf-8")
    target = archive / path.name
    if target.exists():
        return
    target.write_bytes(path.read_bytes())
    print(f"  preserved {path.name} -> {ARCHIVE_DIRNAME}/{folder}/")


def write_legacy(doc: dict[str, Any], results_dir: Path) -> list[Path]:
    edge = doc["metadata"]["edge"]
    rates = sorted({c["rate"] for c in doc["cells"]})
    written = []

    meta = {
        "generated_at": doc["metadata"]["generated_at"],
        "script": "benchmarks/analyze_paired.py (pooled from run_paired_benchmark.py)",
        "source": f"paired_{edge}.json",
        "edge": edge,
        "replicates": doc["metadata"]["replicates"],
        "vus_list": rates,
        "note": (
            "`vus` is the OFFERED ITERATIONS PER SECOND of a constant-arrival-rate scenario, "
            "pooled across valid replicates. Internal timings are microseconds; read p50_us, not "
            "avg_us, which a sub-1% preemption tail dominates."
        ),
    }

    profile = {"metadata": meta, "runs": []}
    raw = {"metadata": meta, "runs": []}
    for rate in rates:
        tokens_summary: dict[str, Any] = {}
        tokens_raw: dict[str, Any] = {}
        for kind in ALL_KINDS:
            tokens_summary[kind] = {}
            tokens_raw[kind] = {}
            for phase in PHASES:
                samples: list[int] = []
                for cell in valid_cells(doc, "wadm", rate):
                    samples.extend(
                        (cell.get("tokens_raw") or {}).get(kind, {}).get(f"{phase}_us", [])
                    )
                tokens_summary[kind][phase] = summarize(samples).to_json()
                tokens_raw[kind][f"{phase}_us"] = samples
        profile["runs"].append({
            "vus": rate,
            "tokens": tokens_summary,
            "detection": tokens_summary["html_comments"]["detect"],
            "injection": tokens_summary["html_comments"]["inject"],
        })
        raw["runs"].append({
            "vus": rate,
            "detection_us": tokens_raw["html_comments"]["detect_us"],
            "injection_us": tokens_raw["html_comments"]["inject_us"],
            "tokens": tokens_raw,
        })

    for name, payload in (
        (f"internal_{edge}_profile.json", profile),
        (f"internal_{edge}_raw.json", raw),
    ):
        path = results_dir / name
        preserve(path)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        written.append(path)

    for tier in ("bare", "wadm"):
        e2e = {"metadata": {**meta, "tier": tier}, "runs": []}
        for rate in rates:
            cells = valid_cells(doc, tier, rate)
            if not cells:
                continue
            e2e["runs"].append({
                "vus": rate,
                "throughput": {
                    "iterations": int(statistics.median(
                        [c["validity"]["iterations"] for c in cells]
                    )),
                    "expected_iterations": cells[0]["validity"]["offered_iterations"],
                    "throughput_ratio": round(statistics.median(
                        [c["validity"]["achieved_ratio"] or 0 for c in cells]
                    ), 3),
                    "expected_reachable": True,
                },
                "k6": {
                    "iterations": int(statistics.median(
                        [c["validity"]["iterations"] for c in cells]
                    )),
                    "http_reqs": int(statistics.median([c["k6"]["http_reqs"] or 0 for c in cells])),
                    "http_req_failed_rate": statistics.median(
                        [c["k6"]["http_req_failed_rate"] or 0 for c in cells]
                    ),
                    "trends": {
                        m: pooled_trend(doc, tier, rate, m)
                        for m in ["http_req_duration"] + E2E_METRICS
                    },
                },
                "replicates_pooled": len(cells),
            })
        path = results_dir / f"e2e_{edge}_{tier}.json"
        preserve(path)
        path.write_text(json.dumps(e2e, indent=2), encoding="utf-8")
        written.append(path)

    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--write-legacy", action="store_true",
        help="Also rewrite the internal_*/e2e_* files the existing plotters read. Off by default: "
             "it replaces the dataset the committed figures were drawn from. Any file it "
             "replaces is first copied to results/archive/<date>-write-legacy/.",
    )
    parser.add_argument("--results", default=None, help="Results directory.")
    parser.add_argument(
        "--reassess", action="store_true",
        help="Re-run the validity gate over the stored cells and save the result. Use after the "
             "gate rules change, to avoid re-running the benchmark.",
    )
    args = parser.parse_args()

    results_dir = Path(args.results) if args.results else Path(__file__).resolve().parent / "results"
    summaries: dict[str, Any] = {}
    written: list[Path] = []

    # Discovered from the results directory, not from a fixed list: a remediation variant produces
    # the same schema under its own key, and should be reported without the analyser needing to
    # know about it in advance. Main edges lead so the four-way comparison reads first.
    present = [k for k in EDGE_KEYS if (results_dir / f"paired_{k}.json").exists()]
    present += [
        k for k in VARIANT_KEYS + INSERVER_KEYS if (results_dir / f"paired_{k}.json").exists()
    ]
    for k in EDGE_KEYS:
        if k not in present:
            print(f"  ! no paired_{k}.json — skipping {EDGES[k].label}")

    for edge in present:
        doc = load_paired(results_dir, edge)
        if not doc:
            continue
        if args.reassess:
            changed = reassess(doc)
            (results_dir / f"paired_{edge}.json").write_text(
                json.dumps(doc, indent=2), encoding="utf-8"
            )
            print(f"  re-assessed paired_{edge}.json ({changed} verdict(s) changed)")
        summary = analyse_edge(doc)
        summaries[edge] = summary
        report(summary)
        if args.write_legacy:
            written.extend(write_legacy(doc, results_dir))

    if not summaries:
        print("\nNothing to analyse. Run run_paired_benchmark.py first.")
        return 1

    out = results_dir / "paired_summary.json"
    out.write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    print(f"\nSaved {out}")
    for path in written:
        print(f"Rewrote {path.name} (legacy schema, for the existing plotters)")
    if not args.write_legacy:
        print(
            "\nThe plotters' input files were NOT touched. Re-run with --write-legacy to "
            "regenerate them from this paired data."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
