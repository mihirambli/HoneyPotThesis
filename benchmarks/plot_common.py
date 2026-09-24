#!/usr/bin/env python3
"""Shared loading, palette and axis conventions for the benchmark plotters.

Both figures answer a different question from the same result files:

    plot_edge_comparison.py   which EDGE is cheapest at honeytoken work overall
                              (all kinds pooled into one distribution per edge)
    plot_token_comparison.py  what each KIND costs on each edge (per-kind panels)

Keeping the palette, the symlog convention and the raw/summary fallback rule here
means an edge keeps one colour and one visual language across every figure.

Data sources (per edge, written by run_internal_<edge>_benchmark.py):

    internal_<edge>_raw.json      raw per-request latencies -> true box plots
    internal_<edge>_profile.json  summary stats -> fallback approximation, hatched
"""

import json

import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, LogLocator, SymmetricalLogLocator

from wadm_timings import ALL_KINDS, KINDS, KINDS_FOR, PHASES, SQLI_ARMS

# Display name -> file stem. Order here is the left-to-right box order and the
# categorical slot assignment order below.
EDGES = {
    "OpenResty": "internal_openresty",
    "WASM (Envoy)": "internal_wasm",
    "Apache+Lua": "internal_apache_lua",
    "Envoy+Lua": "internal_envoy_lua",
}

# Categorical slots 1-4 of the validated default data-viz palette, assigned in fixed
# order. Adjacent-pair CVD separation is what the ordering guarantees, so edges keep
# these colours across every figure — colour follows the edge, never its rank.
COLORS = {
    "OpenResty": "#2a78d6",
    "WASM (Envoy)": "#eb6834",
    "Apache+Lua": "#1baf7a",
    "Envoy+Lua": "#eda100",
}

INK = "#0b0b0b"
INK_MUTED = "#52514e"

# html_comments leads: it is the reference measurement the added kinds are compared to.
# The taxonomy is imported rather than redeclared so the scrapers and the plotters can never
# disagree about which kinds exist or which phases each one carries.
KIND_LABELS = {
    "html_comments": "html_comments\n(body comment)",
    "http_headers": "http_headers\n(response header)",
    "cookies": "cookies\n(Set-Cookie bait)",
    "decoy_paths": "decoy_paths\n(hidden link / trap URI)",
    "form_fields": "form_fields\n(hidden input)",
    "sql_injection": "sql_injection\n(signature hit)",
    "sql_injection_encoded": "sql_injection\n(signature hit, %-encoded)",
    "sql_injection_miss": "sql_injection\n(no signature match)",
    "sql_injection_miss_encoded": "sql_injection\n(no match, %-encoded)",
}
PHASE_LABELS = {"detect": "Detection", "inject": "Injection"}

# Short axis labels for the SQLi 2x2. Outcome varies across the pair, encoding within it.
SQLI_ARM_LABELS = {
    "sql_injection": "hit\nplain",
    "sql_injection_encoded": "hit\n%-encoded",
    "sql_injection_miss": "no match\nplain",
    "sql_injection_miss_encoded": "no match\n%-encoded",
}
SQLI_ARM_ENCODED = {
    "sql_injection": False,
    "sql_injection_encoded": True,
    "sql_injection_miss": False,
    "sql_injection_miss_encoded": True,
}

# ── End-to-end (millisecond) plane ───────────────────────────────────────────────────────────
#
# The internal microsecond timers above exist only where WADM runs. A bare edge has no
# detect/inject region to time, so the two tiers meet only on end-to-end request latency, which
# k6 reports for all of them. These constants describe that second plane.

# `origin` is not an edge and never gets a box: it is the floor every edge is measured above,
# drawn as a reference line.
TIERS = ["bare", "wadm"]
TIER_LABELS = {
    "origin": "Origin only (no proxy)",
    "bare": "Bare proxy (no WADM)",
    "wadm": "WADM active",
}
ORIGIN_KEY = "origin"

# Display name -> the key the result files use (`e2e_<key>_<tier>.json`). Derived from EDGES so
# the two result families can never drift apart.
EDGE_KEYS = {name: stem.removeprefix("internal_") for name, stem in EDGES.items()}

# http_req_duration pools all eight requests of an iteration. It is what "the latency of a request
# through this edge" means and is what the tier-comparison figures plot, but it is NOT what the
# overhead decomposition scales up — see measured_overhead_ms_per_iteration.
POOLED_METRIC = "http_req_duration"

# The eight requests of an iteration, and the WADM work each one triggers (see test.js).
#
# Injection fires on the four GETs: every response is text/html and the `/*` tokens are planted on
# every page. Detection differs — only the last three GETs carry a trigger, so only they take the
# hit path (record the attacker IP, render an alert). The *scan* still runs on the first request
# and finds nothing, which is why it is labelled "no detection hit" rather than "no detection".
#
# That makes the first request the closest available injection-only measurement, and the
# difference between it and the other three the marginal cost of a detection hit. The comparison
# is only meaningful against the bare tier, which cancels out the fact that these are different
# backend paths returning different-sized bodies.
#
# The four POSTs are the SQLi trap's 2x2 and behave in the opposite direction: on OpenResty, Envoy+Lua
# and Apache the trap answers from the request phase, so WADM *saves* the origin round-trip the
# bare tier pays and these requests come out faster with WADM than without. Envoy+WASM cannot
# answer from the request phase (proxy-wasm rejects it) and rewrites the upstream response
# instead, so it alone still pays that hop — its two POST figures carry one extra round-trip the
# other three do not, and are not comparable across edges on this plane.
# The four GETs. These are the ones the phase-overhead figure compares, because they share a
# shape: every one is proxied to the origin and every one carries injection, so the difference
# between them is a detection hit and nothing else.
E2E_PHASE_METRICS = [
    "inject_get_duration",
    "detect_query_duration",
    "token_tamper_duration",
    "token_decoy_duration",
]

# The four POSTs. Kept off the phase-overhead axis: they carry no injection, and on the three
# edges that answer from the request phase WADM *removes* the origin round-trip, so their deltas
# are negative. Drawing them beside the GETs puts "WADM added 1.5 ms" and "WADM saved 0.5 ms" on
# one "latency added" axis and flattens the bars the figure exists to show. They get their own
# figure in plot_sqli_comparison.py, where the sign is the result rather than a distraction.
SQLI_E2E_METRICS = [
    "sqli_hit_duration",
    "sqli_hit_enc_duration",
    "sqli_miss_duration",
    "sqli_miss_enc_duration",
]

# Every request in the iteration. This is the summation set for the overhead decomposition, which
# must account for the whole iteration — not the plotting set.
E2E_METRICS = E2E_PHASE_METRICS + SQLI_E2E_METRICS
E2E_METRIC_LABELS = {
    "inject_get_duration": "GET /\ninjection only\n(no detection hit)",
    "detect_query_duration": "GET /api/login?password=…\ninjection\n+ html_comments detection",
    "token_tamper_duration": "GET /login.html?is_admin=1&probe=…\ninjection\n+ headers, cookies, form_fields",
    "token_decoy_duration": "GET /api/v1/debug\ninjection\n+ decoy_paths detection",
    "sqli_hit_duration": "POST /api/login\nsql_injection\nhit, plain",
    "sqli_hit_enc_duration": "POST /api/login\nsql_injection\nhit, %-encoded",
    "sqli_miss_duration": "POST /api/login\nsql_injection\nno signature match, plain",
    "sqli_miss_enc_duration": "POST /api/login\nsql_injection\nno match, %-encoded",
}


def load_edge(results_dir, stem):
    """Return (raw, summary) as {vus: {kind: {phase: samples|stats}}} or None each.

    Result files written before per-kind timing existed carry only the top-level
    html_comments arrays; those are mapped onto the `html_comments` kind so an old
    file still renders (as that one kind) instead of vanishing from the figure.
    """
    raw_by_vus = None
    raw_path = results_dir / f"{stem}_raw.json"
    if raw_path.exists():
        doc = json.loads(raw_path.read_text())
        raw_by_vus = {}
        for run in doc["runs"]:
            tokens = run.get("tokens") or {
                "html_comments": {
                    "detect_us": run.get("detection_us", []),
                    "inject_us": run.get("injection_us", []),
                }
            }
            raw_by_vus[run["vus"]] = {
                kind: {
                    phase: tokens.get(kind, {}).get(f"{phase}_us", []) for phase in PHASES
                }
                for kind in ALL_KINDS
            }

    summary_by_vus = None
    profile_path = results_dir / f"{stem}_profile.json"
    if profile_path.exists():
        doc = json.loads(profile_path.read_text())
        summary_by_vus = {}
        for run in doc["runs"]:
            tokens = run.get("tokens") or {
                "html_comments": {
                    "detect": run.get("detection"),
                    "inject": run.get("injection"),
                }
            }
            summary_by_vus[run["vus"]] = {
                kind: {phase: tokens.get(kind, {}).get(phase) for phase in PHASES}
                for kind in ALL_KINDS
            }

    return raw_by_vus, summary_by_vus


def collect(results_dir):
    """Load every edge and the sorted union of VU levels seen."""
    data = {}
    vus_seen = set()
    for name, stem in EDGES.items():
        raw_by_vus, summary_by_vus = load_edge(results_dir, stem)
        if not raw_by_vus and not summary_by_vus:
            print(f"  ! skipping {name}: no result files found for '{stem}'")
            continue
        data[name] = {"raw": raw_by_vus, "summary": summary_by_vus}
        for src in (raw_by_vus, summary_by_vus):
            if src:
                vus_seen.update(src.keys())
    return data, sorted(vus_seen)


def samples_for(edge, vus, kind, phase):
    raw = (edge["raw"] or {}).get(vus)
    return (raw or {}).get(kind, {}).get(phase) or []


def stats_for(edge, vus, kind, phase):
    summary = (edge["summary"] or {}).get(vus)
    stats = (summary or {}).get(kind, {}).get(phase)
    if not stats or not stats.get("count"):
        return None
    return stats


def pooled_samples(edge, vus, phase):
    """Every kind's samples for one phase, concatenated into one distribution.

    Kinds contribute in proportion to how often they actually fire (the `/*` kinds
    inject on all four benchmark GETs, form_fields on one), so the pooled box is
    "what a honeytoken operation costs on this edge", not a mean of per-kind means.

    KINDS_FOR, not KINDS: sql_injection has a detection region but nothing to inject, and the
    clean-login control never belongs in a honeytoken pool at all.
    """
    out = []
    for kind in KINDS_FOR[phase]:
        out.extend(samples_for(edge, vus, kind, phase))
    return out


def pooled_stats(edge, vus, phase):
    """Count-weighted pooled summary, for edges with no raw file. Approximate."""
    parts = [s for s in (stats_for(edge, vus, k, phase) for k in KINDS_FOR[phase]) if s]
    if not parts:
        return None
    total = sum(p["count"] for p in parts)
    weighted = lambda key: round(sum(p[key] * p["count"] for p in parts) / total, 2)
    return {
        "count": total,
        "min_us": min(p["min_us"] for p in parts),
        "avg_us": weighted("avg_us"),
        # A pooled percentile cannot be recovered from per-kind p90s; the weighted mean
        # is a stand-in, which is why these boxes are drawn hatched.
        "p90_us": weighted("p90_us"),
        "max_us": max(p["max_us"] for p in parts),
    }


def summary_bxp_stats(stats, label):
    """Approximate box-plot stats from summary numbers (min/avg/p90/max)."""
    return {
        "label": label,
        "whislo": stats["min_us"],
        "q1": stats["min_us"],   # Q1 unavailable -> min
        "med": stats["avg_us"],  # mean stands in for the median
        "q3": stats["p90_us"],   # Q3 unavailable -> p90
        "whishi": stats["max_us"],
        "fliers": [],
    }


def load_e2e(results_dir, edge_key, tier):
    """Return {vus: run} from e2e_<edge_key>_<tier>.json, or None if that tier was never run."""
    path = results_dir / f"e2e_{edge_key}_{tier}.json"
    if not path.exists():
        return None
    doc = json.loads(path.read_text())
    return {run["vus"]: run for run in doc["runs"]}


def collect_e2e(results_dir):
    """Load every edge's end-to-end tiers plus the origin floor.

    Returns (data, origin, vus_list) where data is {display name: {tier: {vus: run}}}. An edge
    appears only if at least one of its tiers exists, so a partially-run results directory still
    plots what it has.
    """
    data = {}
    vus_seen = set()
    for name, key in EDGE_KEYS.items():
        tiers = {tier: load_e2e(results_dir, key, tier) for tier in TIERS}
        tiers = {tier: runs for tier, runs in tiers.items() if runs}
        if not tiers:
            print(f"  ! skipping {name}: no end-to-end result files for '{key}'")
            continue
        data[name] = tiers
        for runs in tiers.values():
            vus_seen.update(runs.keys())

    origin = load_e2e(results_dir, ORIGIN_KEY, "bare")
    if origin:
        vus_seen.update(origin.keys())
    else:
        print("  ! no origin floor found (e2e_origin_bare.json)")
    return data, origin, sorted(vus_seen)


def e2e_stats(runs_by_vus, vus, metric):
    """One metric's k6 summary stats at one VU level, or None if it was not measured."""
    run = (runs_by_vus or {}).get(vus)
    if not run:
        return None
    stats = ((run.get("k6") or {}).get("trends") or {}).get(metric)
    if not stats or not stats.get("count"):
        return None
    return stats


def k6_bxp_stats(stats, label):
    """Box-plot stats from k6's summary quantiles.

    Unlike `summary_bxp_stats`, these are *real* percentiles — test.js asks k6 for p(5)/p(25)/
    p(75)/p(95) explicitly — so the box is drawn solid rather than hatched. The whisker runs
    p5→p95 rather than 1.5×IQR, which is why the end-to-end figures carry a different caption
    from the internal-timer ones. `min` is deliberately not the lower whisker: one unusually fast
    sample out of tens of thousands would stretch it to the floor and swamp the difference these
    figures exist to show.
    """
    return {
        "label": label,
        "whislo": stats.get("p5", stats["min"]),
        "q1": stats["p25"],
        "med": stats["med"],
        "q3": stats["p75"],
        "whishi": stats["p95"],
        "fliers": [],
    }


def draw_tier_box(ax, stats, position, color, tier, width=0.34):
    """One end-to-end box. Tier is encoded by fill, never by hue.

    Hue stays with the edge across every figure in this repo, so bare and WADM are separated by
    a hollow vs. solid face. Hatching is deliberately not used: in the internal-timer figures it
    already means "this box is a summary approximation".

    The `origin` tier is drawn solid like `wadm` but is passed a neutral grey rather than an edge
    hue, because it is the no-proxy floor and belongs to no edge.
    """
    solid = tier in ("wadm", "origin")
    bp = ax.bxp(
        [k6_bxp_stats(stats, "")],
        positions=[position],
        widths=width,
        showfliers=False,
        patch_artist=True,
        medianprops={"color": INK if solid else color, "linewidth": 2},
        whiskerprops={"color": INK_MUTED, "linewidth": 1},
        capprops={"color": INK_MUTED, "linewidth": 1},
        boxprops={"edgecolor": "white" if solid else color, "linewidth": 2},
    )
    for patch in bp["boxes"]:
        patch.set_facecolor(color if solid else "white")
        patch.set_alpha(0.9 if solid else 1.0)


def quartiles(values):
    """(q1, median, q3) by the nearest-rank convention used across this repo."""
    ordered = sorted(values)
    n = len(ordered)

    def at(pct):
        idx = max(1, int(-(-pct * n // 100))) - 1
        return float(ordered[idx])

    return at(25), at(50), at(75)


def _style_frame(ax):
    """Recessive chrome shared by every figure: hairline grid, muted ticks, no top/right spine."""
    ax.grid(True, which="major", axis="y", alpha=0.25, linewidth=0.8)
    ax.tick_params(colors=INK_MUTED, labelsize=9)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color("#d5d4cf")


def style_axis(ax):
    # symlog, not log: the edges time in whole microseconds and 8-13% of samples on the
    # fast edges land on exactly 0 µs (work finished inside one tick). A pure log axis
    # cannot render 0 and would silently drop that mass; symlog is linear below 1 µs and
    # logarithmic above, so the sub-microsecond samples stay visible and honest.
    ax.set_yscale("symlog", linthresh=1, linscale=0.4)
    ax.set_ylim(bottom=0)
    # Injection panels span barely one decade, where decade-only ticks leave the axis
    # nearly unlabelled; label the 2/3/5 sub-steps too.
    ax.yaxis.set_minor_locator(SymmetricalLogLocator(base=10, linthresh=1, subs=[2, 3, 5]))
    ax.yaxis.set_minor_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    _style_frame(ax)
    ax.tick_params(axis="y", which="minor", labelsize=7, colors=INK_MUTED)


def style_axis_ms(ax, scale="log", from_zero=True):
    """Millisecond axis for the end-to-end figures.

    Log, not the symlog used for the microsecond timers: symlog exists there only because those
    timers bottom out at exactly 0 µs, which end-to-end latency never does. Plain log is
    therefore available here, and it is needed — a saturated 500-VU level runs three decades
    above an idle one, and on a linear axis it flattens every other level into an unreadable
    band along the x-axis. Sub-decade steps are labelled because the per-level panels span
    barely one decade, where decade-only ticks leave the axis almost bare.

    `scale="linear"` is for the overhead figure, whose values are differences rather than
    latencies: they can legitimately come out negative under host noise, which log cannot show.
    `from_zero=False` keeps those negative bars visible instead of clipping them away.
    """
    if scale == "log":
        ax.set_yscale("log")
        ax.yaxis.set_minor_locator(LogLocator(base=10, subs=(2, 3, 5)))
        ax.yaxis.set_minor_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        ax.tick_params(axis="y", which="minor", labelsize=7, colors=INK_MUTED)
    elif from_zero:
        ax.set_ylim(bottom=0)
    _style_frame(ax)


def add_headroom(ax, factor=1.35):
    """Keep the topmost mark and its label clear of the axis frame.

    A log axis keeps whatever bottom it autoscaled to — pinning it at 0 the way the symlog and
    linear axes do is undefined there, and would silently drop the axis entirely.
    """
    bottom, top = ax.get_ylim()
    ax.set_ylim(bottom if ax.get_yscale() == "log" else 0, top * factor)


def draw_edge_boxes(ax, edges_present, get_samples, get_stats):
    """One box per edge, raw samples preferred.

    Returns (used_fallback, flat_boxes). `get_samples(name)` returns raw microsecond
    samples (or an empty list) and `get_stats(name)` the summary fallback (or None), so
    the same drawing code serves the pooled edge figure and the per-kind panels.

    `flat_boxes` lists boxes whose Q1 == Q3 — the operation finished inside one or two
    ticks of the 1 µs timer, so the interquartile range collapses and matplotlib renders
    nothing but the median line, which reads as "no data". Pass the list to
    finalize_flat_boxes once the axis limits are final to give them a visible floor.
    """
    used_fallback = False
    approx, approx_pos, approx_colors = [], [], []
    flat_boxes = []

    for i, name in enumerate(edges_present, start=1):
        values = get_samples(name)
        if values:
            q1, _med, q3 = quartiles(values)
            if q1 == q3:
                flat_boxes.append((i, q1, COLORS[name]))
            bp = ax.boxplot(
                values,
                positions=[i],
                widths=0.62,
                patch_artist=True,
                showfliers=False,
                medianprops={"color": INK, "linewidth": 2},
                whiskerprops={"color": INK_MUTED, "linewidth": 1},
                capprops={"color": INK_MUTED, "linewidth": 1},
                boxprops={"edgecolor": "white", "linewidth": 2},
            )
            for patch in bp["boxes"]:
                patch.set_facecolor(COLORS[name])
                patch.set_alpha(0.9)
            continue

        stats = get_stats(name)
        if not stats:
            continue
        approx.append(summary_bxp_stats(stats, name))
        approx_pos.append(i)
        approx_colors.append(COLORS[name])
        used_fallback = True

    if approx:
        bp = ax.bxp(
            approx,
            positions=approx_pos,
            widths=0.62,
            showfliers=False,
            patch_artist=True,
            medianprops={"color": INK, "linewidth": 2, "linestyle": "--"},
        )
        for patch, color in zip(bp["boxes"], approx_colors):
            patch.set_facecolor(color)
            patch.set_alpha(0.45)
            patch.set_hatch("//")

    ax.set_xlim(0.4, len(edges_present) + 0.6)
    ax.set_xticks(range(1, len(edges_present) + 1))
    style_axis(ax)
    return used_fallback, flat_boxes


def finalize_flat_boxes(ax, flat_boxes, min_px=3.5, width=0.62):
    """Give zero-height boxes a visible floor so they don't read as missing data.

    A box collapses when Q1 == Q3, i.e. the middle 50% of samples all landed on the same
    whole microsecond — common for the cheap kinds on the fast edges, where the work
    completes inside the timer's resolution. Drawing a pixel-height rectangle keeps the
    box visible without overstating the spread. Must run after the axis limits are final
    (with sharey, that means after every panel in the row has been drawn), because the
    height is computed in display space and converted back to data coordinates.
    """
    if not flat_boxes:
        return
    to_data = ax.transData.inverted()
    for position, value, color in flat_boxes:
        _, py = ax.transData.transform((position, value))
        _, low = to_data.transform((0, py - min_px))
        _, high = to_data.transform((0, py + min_px))
        ax.add_patch(
            plt.Rectangle(
                (position - width / 2, low),
                width,
                high - low,
                facecolor=color,
                alpha=0.9,
                edgecolor="white",
                linewidth=2,
                zorder=2.5,
            )
        )


def place_direct_labels(ax, endpoints):
    """Write one label per line end, staggered so near-equal medians stay legible.

    Positions are nudged in axes-fraction space (invariant under the later
    tight_layout resize) rather than data space, which a symlog axis would distort.
    """
    if not endpoints:
        return
    inverse = ax.transAxes.inverted()
    items = []
    for name, x_data, y_data in endpoints:
        _, frac_y = inverse.transform(ax.transData.transform((x_data, y_data)))
        items.append([name, x_data, frac_y])

    items.sort(key=lambda item: item[2], reverse=True)
    min_gap = 0.075
    for i in range(1, len(items)):
        if items[i - 1][2] - items[i][2] < min_gap:
            items[i][2] = items[i - 1][2] - min_gap

    for name, x_data, frac_y in items:
        ax.annotate(
            name,
            xy=(x_data + 0.12, min(max(frac_y, 0.02), 0.98)),
            xycoords=("data", "axes fraction"),
            fontsize=7.5,
            color=INK_MUTED,
            va="center",
        )
