#!/usr/bin/env python3
"""Compare each edge's end-to-end request latency with and without WADM.

The internal-timer figures (plot_edge_comparison.py, plot_token_comparison.py) answer "what does
a honeytoken operation cost inside the edge". They cannot answer "what does deploying WADM cost a
request", because a bare edge runs no detect/inject code and therefore has no internal timer to
compare against. End-to-end latency, which k6 reports for every tier, is the plane where that
question is answerable — and these are the figures that answer it.

Three tiers, all measured with the identical k6 script and VU ladder:

    origin   k6 -> backend                      the floor
    bare     k6 -> edge (no WADM) -> backend    written by run_baseline_benchmark.py
    wadm     k6 -> edge (WADM)    -> backend    written by run_internal_<edge>_benchmark.py

Data sources
------------
    e2e_<edge>_bare.json      end-to-end milliseconds, WADM absent
    e2e_<edge>_wadm.json      end-to-end milliseconds, WADM active
    e2e_origin_bare.json      end-to-end milliseconds, no proxy at all
    internal_<edge>_raw.json  internal microseconds, used only by the breakdown figure

Usage:
    python3 benchmarks/plot_baseline_comparison.py [results_dir]
"""

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from plot_common import (
    COLORS,
    E2E_METRIC_LABELS,
    E2E_METRICS,
    EDGES,
    INK,
    INK_MUTED,
    KINDS,
    PHASES,
    POOLED_METRIC,
    TIER_LABELS,
    TIERS,
    add_headroom,
    collect,
    collect_e2e,
    draw_tier_box,
    e2e_stats,
    place_direct_labels,
    quartiles,
    samples_for,
    style_axis_ms,
)

# Requests per k6 iteration (see test.js). Converts a per-request end-to-end delta into the
# per-iteration figure the internal timers are naturally expressed against.
REQUESTS_PER_ITERATION = 4

TIER_OFFSET = {"bare": -0.19, "wadm": 0.19}

# Below this share of the offered rate an edge is no longer serving every request it was asked
# for, so its latency reflects queueing rather than per-request cost. Matches the threshold
# wadm_timings.throughput_check warns at.
SUSTAINED_RATIO = 0.9


def saturated_levels(data, edges_present, vus_list):
    """VU levels where some edge could not sustain the offered request rate.

    Latency at such a level is a capacity measurement, not a per-request one: the queue, not the
    filter chain, sets the number. Marking them keeps a reader from reading a 300 ms "overhead"
    as the cost of injecting a honeytoken.
    """
    saturated = set()
    for vus in vus_list:
        for name in edges_present:
            for tier in TIERS:
                run = (data[name].get(tier) or {}).get(vus)
                ratio = (run or {}).get("throughput", {}).get("throughput_ratio")
                if ratio is not None and ratio < SUSTAINED_RATIO:
                    saturated.add(vus)
    return saturated


def tier_legend_handles(edges_present, marks="box", with_origin=False):
    """Identity (hue = edge) and tier (fill or dash = with/without WADM) as one legend row.

    Encoding both in one set of swatches would need eight entries for four edges and would imply
    the tier is a category of its own rather than a treatment applied to each edge. `marks`
    selects swatches that match how the figure actually draws the tiers, so the legend never
    shows a filled box for what the panel drew as a dashed line.
    """
    edge_handles = [
        Patch(facecolor=COLORS[name], edgecolor="white", linewidth=2, label=name)
        for name in edges_present
    ]
    if with_origin:
        edge_handles.insert(
            0, Patch(facecolor=INK_MUTED, edgecolor="white", linewidth=2,
                     label=TIER_LABELS["origin"])
        )
    if marks == "box":
        tier_handles = [
            Patch(facecolor="white", edgecolor=INK_MUTED, linewidth=2, label=TIER_LABELS["bare"]),
            Patch(facecolor=INK_MUTED, edgecolor="white", linewidth=2, label=TIER_LABELS["wadm"]),
        ]
    else:
        tier_handles = [
            Line2D([0], [0], color=INK_MUTED, linewidth=2, linestyle=(0, (5, 3)),
                   marker="o", markersize=6, markerfacecolor="white",
                   markeredgecolor=INK_MUTED, markeredgewidth=2, label=TIER_LABELS["bare"]),
            Line2D([0], [0], color=INK_MUTED, linewidth=2, marker="o", markersize=6,
                   label=TIER_LABELS["wadm"]),
        ]
    if not with_origin:
        origin_style = (0, (1, 2)) if marks == "line" else (0, (4, 3))
        tier_handles.append(
            Line2D([0], [0], color=INK_MUTED, linewidth=1.4, linestyle=origin_style,
                   label=TIER_LABELS["origin"] + " (median)")
        )
    return edge_handles + tier_handles


def plot_comparison_for_vus(vus, data, origin, edges_present, saturated, out_dir):
    """The three tiers side by side at one load level: no proxy, bare proxy, proxy with WADM.

    The no-proxy floor gets its own box rather than the reference line it used to be drawn as:
    it is one of the three things being compared, so it should be readable as a distribution
    (spread and tail included), not just as a median. Its median is still extended across the
    panel as a rule, which is what lets every other box be read as a height above the floor.
    """
    fig, ax = plt.subplots(figsize=(12, 6.2))

    origin_stats = e2e_stats(origin, vus, POOLED_METRIC)
    if origin_stats:
        draw_tier_box(ax, origin_stats, 1, INK_MUTED, "origin")
        ax.axhline(origin_stats["med"], color=INK_MUTED, linewidth=1.2,
                   linestyle=(0, (4, 3)), zorder=1)

    for i, name in enumerate(edges_present, start=2):
        for tier in TIERS:
            stats = e2e_stats(data[name].get(tier), vus, POOLED_METRIC)
            if stats:
                draw_tier_box(ax, stats, i + TIER_OFFSET[tier], COLORS[name], tier)

    positions = range(1, len(edges_present) + 2)
    ax.set_xlim(0.4, len(edges_present) + 1.6)
    ax.set_xticks(list(positions))
    ax.set_xticklabels([TIER_LABELS["origin"]] + list(edges_present), fontsize=10)
    # A rule between the floor and the proxied edges: the first slot is a different kind of
    # thing (one measurement, no edge) from the paired slots to its right.
    ax.axvline(1.6, color="#d5d4cf", linewidth=1, zorder=0)
    style_axis_ms(ax)
    add_headroom(ax, factor=1.35)
    ax.set_ylabel("End-to-end request latency (ms, log)", fontsize=10, color=INK)

    fig.legend(
        handles=tier_legend_handles(edges_present, with_origin=True),
        loc="lower center",
        ncol=len(edges_present) + 3,
        frameon=False,
        fontsize=8.5,
        bbox_to_anchor=(0.5, 0.045),
    )
    suffix = " — SATURATED LEVEL" if vus in saturated else ""
    fig.suptitle(
        f"No proxy vs. bare proxy vs. WADM, at {vus} VU(s){suffix}",
        fontsize=14,
        fontweight="bold",
        color=INK,
    )
    caption = (
        "All four requests of an iteration pooled (http_req_duration). Box = Q1–Q3, line = "
        "median, whiskers = p5–p95 (k6 summary quantiles — real percentiles, unlike the hatched "
        "approximations in the internal-timer figures). Hollow = WADM absent, filled = WADM "
        "active; hue identifies the edge in every figure. The dashed rule is the no-proxy median. "
        "Y axis is log."
    )
    if vus in saturated:
        caption += (
            "  At this level at least one edge served under 90% of the offered request rate: "
            "these numbers measure queueing capacity, not per-request cost."
        )
    fig.text(0.5, 0.012, caption, ha="center", fontsize=8, style="italic", color=INK_MUTED,
             wrap=True)
    fig.tight_layout(rect=[0, 0.11, 1, 0.94])

    out = out_dir / f"e2e_comparison_vus_{vus}.png"
    fig.savefig(out, dpi=150, facecolor="white")
    plt.close(fig)
    print(f"  wrote {out}")


def plot_overhead_scaling(data, origin, edges_present, vus_list, saturated, out_dir):
    """Median and p95 end-to-end latency against load, both tiers, one line each."""
    stat_keys = [("med", "Median"), ("p95", "p95")]
    fig, axes = plt.subplots(1, len(stat_keys), figsize=(7.4 * len(stat_keys), 5.2))
    x = range(len(vus_list))
    saturated_x = [xi for xi, vus in zip(x, vus_list) if vus in saturated]

    for ax, (key, key_label) in zip(axes, stat_keys):
        if saturated_x:
            ax.axvspan(min(saturated_x) - 0.5, len(vus_list) - 1 + 1.1,
                       color=INK_MUTED, alpha=0.06, linewidth=0, zorder=0)
        endpoints = []
        for name in edges_present:
            for tier in TIERS:
                xs, ys = [], []
                for xi, vus in zip(x, vus_list):
                    stats = e2e_stats(data[name].get(tier), vus, POOLED_METRIC)
                    if stats:
                        xs.append(xi)
                        ys.append(stats[key])
                if not xs:
                    continue
                solid = tier == "wadm"
                ax.plot(
                    xs, ys,
                    color=COLORS[name],
                    linewidth=2,
                    linestyle="-" if solid else (0, (5, 3)),
                    marker="o" if solid else "o",
                    markersize=6,
                    markerfacecolor=COLORS[name] if solid else "white",
                    markeredgecolor=COLORS[name],
                    markeredgewidth=2,
                )
                if solid:
                    endpoints.append((name, xs[-1], ys[-1]))

        origin_xs, origin_ys = [], []
        for xi, vus in zip(x, vus_list):
            stats = e2e_stats(origin, vus, POOLED_METRIC)
            if stats:
                origin_xs.append(xi)
                origin_ys.append(stats[key])
        if origin_xs:
            ax.plot(origin_xs, origin_ys, color=INK_MUTED, linewidth=1.4,
                    linestyle=(0, (1, 2)), marker="", zorder=1)

        ax.set_title(f"{key_label} of http_req_duration", fontsize=11, color=INK, pad=10)
        ax.set_xticks(list(x))
        ax.set_xticklabels([str(v) for v in vus_list], fontsize=9)
        ax.set_xlabel("VUs", fontsize=9, color=INK_MUTED)
        ax.set_xlim(-0.35, len(vus_list) - 1 + 1.1)
        style_axis_ms(ax)
        ax.set_ylabel("End-to-end request latency (ms, log)", fontsize=10, color=INK)
        add_headroom(ax, factor=1.6)
        place_direct_labels(ax, endpoints)

    fig.legend(
        handles=tier_legend_handles(edges_present, marks="line"),
        loc="lower center",
        ncol=len(edges_present) + 3,
        frameon=False,
        fontsize=8.5,
        bbox_to_anchor=(0.5, 0.045),
    )
    fig.suptitle(
        "End-to-end latency vs. load, with and without WADM",
        fontsize=14,
        fontweight="bold",
        color=INK,
    )
    caption = (
        "Solid + filled markers = WADM active, dashed + hollow markers = WADM absent; the dotted "
        "grey line is the origin floor. Labels mark each edge's WADM line. The vertical gap "
        "between an edge's two lines is the overhead these figures exist to measure. Y axis is "
        "log — on a linear one the saturated level flattens every other into the baseline."
    )
    if saturated_x:
        caption += (
            "  The shaded band marks levels where an edge served under 90% of the offered "
            "request rate: there the curve measures capacity, not per-request cost."
        )
    fig.text(0.5, 0.012, caption, ha="center", fontsize=8, style="italic", color=INK_MUTED,
             wrap=True)
    fig.tight_layout(rect=[0, 0.10, 1, 0.93])

    out = out_dir / "e2e_overhead_scaling.png"
    fig.savefig(out, dpi=150, facecolor="white")
    plt.close(fig)
    print(f"  wrote {out}")


def plot_phase_overhead_for_vus(vus, data, edges_present, saturated, out_dir):
    """WADM's added latency per request type — injection alone vs. injection plus a detection hit.

    Plots `wadm − bare` rather than the WADM latency itself. That subtraction is what makes the
    four request types comparable: they hit different backend paths returning different-sized
    bodies, and the bare tier pays exactly those same costs without doing any WADM work, so the
    difference is WADM's contribution and nothing else.
    """
    fig, ax = plt.subplots(figsize=(13, 7.4))
    width = 0.19

    for slot, name in enumerate(edges_present):
        offset = (slot - (len(edges_present) - 1) / 2) * width
        xs, heights = [], []
        for i, metric in enumerate(E2E_METRICS, start=1):
            wadm = e2e_stats(data[name].get("wadm"), vus, metric)
            bare = e2e_stats(data[name].get("bare"), vus, metric)
            if not wadm or not bare:
                continue
            xs.append(i + offset)
            heights.append(wadm["med"] - bare["med"])
        if xs:
            ax.bar(xs, heights, width=width, color=COLORS[name], alpha=0.9,
                   edgecolor="white", linewidth=2, zorder=2)

    ax.axhline(0, color="#d5d4cf", linewidth=1, zorder=1)
    ax.set_xlim(0.45, len(E2E_METRICS) + 0.55)
    ax.set_xticks(range(1, len(E2E_METRICS) + 1))
    ax.set_xticklabels([E2E_METRIC_LABELS[m] for m in E2E_METRICS], fontsize=8.5)
    style_axis_ms(ax, scale="linear", from_zero=False)
    low, high = ax.get_ylim()
    ax.set_ylim(min(low, 0) * 1.15 if low < 0 else 0, high * 1.15)
    ax.set_ylabel("Latency added by WADM (ms, median WADM − median bare)", fontsize=10, color=INK)

    fig.legend(
        handles=[Patch(facecolor=COLORS[n], edgecolor="white", linewidth=2, label=n)
                 for n in edges_present],
        loc="lower center",
        ncol=len(edges_present),
        frameon=False,
        fontsize=9,
        bbox_to_anchor=(0.5, 0.105),
    )
    suffix = " — SATURATED LEVEL" if vus in saturated else ""
    fig.suptitle(
        f"What WADM adds per request: injection alone vs. injection + detection, at {vus} VU(s){suffix}",
        fontsize=13.5,
        fontweight="bold",
        color=INK,
    )
    caption = (
        "Injection fires on all four requests — every response is text/html and the `/*` tokens "
        "are planted on every page — so the leftmost group is the injection-only cost and the "
        "other three add a detection hit on top of that same injection. The detection *scan* runs "
        "on all four; only the hit path (record the IP, render the alert) is extra. End-to-end "
        "latency cannot separate the two phases within one request — for that, see the detect and "
        "inject panels of the internal-timer figures."
    )
    if vus in saturated:
        caption += (
            "  At this level at least one edge served under 90% of the offered request rate, so "
            "these differences are dominated by queueing."
        )
    fig.text(0.5, 0.012, caption, ha="center", fontsize=8, style="italic", color=INK_MUTED,
             wrap=True)
    fig.tight_layout(rect=[0, 0.19, 1, 0.94])

    out = out_dir / f"e2e_phase_overhead_vus_{vus}.png"
    fig.savefig(out, dpi=150, facecolor="white")
    plt.close(fig)
    print(f"  wrote {out}")


def attributed_us_per_iteration(internal_edge, vus, iterations):
    """Internal WADM time the edge's own timers account for, per k6 iteration, in µs.

    Summed as (median cost of one operation) × (how many of that operation fired), over every
    kind and both phases. Medians rather than means, to stay consistent with the end-to-end
    delta this is compared against, which is also a median difference.
    """
    if not iterations:
        return None
    total = 0.0
    for kind in KINDS:
        for phase in PHASES:
            values = samples_for(internal_edge, vus, kind, phase)
            if not values:
                continue
            _q1, med, _q3 = quartiles(values)
            total += med * len(values)
    return total / iterations


def measured_overhead_ms_per_iteration(data, name, vus):
    """End-to-end cost WADM adds to one iteration, in ms."""
    wadm = e2e_stats(data[name].get("wadm"), vus, POOLED_METRIC)
    bare = e2e_stats(data[name].get("bare"), vus, POOLED_METRIC)
    if not wadm or not bare:
        return None
    return (wadm["med"] - bare["med"]) * REQUESTS_PER_ITERATION


def iterations_for(data, name, vus):
    run = (data[name].get("wadm") or {}).get(vus)
    if not run:
        return None
    return (run.get("k6") or {}).get("iterations")


def plot_overhead_breakdown(data, internal, edges_present, vus_list, saturated, out_dir):
    """How much of the measured end-to-end overhead the internal timers actually account for."""
    fig, axes = plt.subplots(1, len(vus_list), figsize=(3.8 * len(vus_list), 5.2))
    axes = [axes] if len(vus_list) == 1 else list(axes)
    any_negative = False

    for ax, vus in zip(axes, vus_list):
        panel_negative = False
        for i, name in enumerate(edges_present, start=1):
            measured = measured_overhead_ms_per_iteration(data, name, vus)
            if measured is None:
                continue
            panel_negative |= measured < 0
            any_negative |= measured < 0
            ax.bar(i, measured, width=0.62, color=COLORS[name], alpha=0.32,
                   edgecolor="white", linewidth=2, zorder=2)

            edge_internal = internal.get(name)
            attributed_us = (
                attributed_us_per_iteration(edge_internal, vus, iterations_for(data, name, vus))
                if edge_internal
                else None
            )
            label = f"{measured:.2f} ms"
            if attributed_us is not None:
                ax.bar(i, attributed_us / 1000.0, width=0.3, color=COLORS[name], alpha=0.95,
                       edgecolor="white", linewidth=2, zorder=3)
                # The internal timers measure tens of microseconds against an end-to-end
                # overhead measured in hundreds; at that ratio the inner bar can be a sliver
                # too thin to read, so the share it represents is written out instead of being
                # left to be judged by eye.
                if measured > 0:
                    share = 100.0 * (attributed_us / 1000.0) / measured
                    label += f"\n{share:.0f}% accounted"

            offset = 0.02 * max(abs(measured), 1e-9)
            ax.annotate(
                label,
                xy=(i, measured + offset if measured >= 0 else measured - offset),
                ha="center",
                va="bottom" if measured >= 0 else "top",
                fontsize=8,
                linespacing=1.4,
                color=INK_MUTED,
            )

        ax.axhline(0, color="#d5d4cf", linewidth=1)
        ax.set_xlim(0.4, len(edges_present) + 0.6)
        ax.set_xticks(range(1, len(edges_present) + 1))
        ax.set_xticklabels(edges_present, rotation=20, fontsize=8.5)
        style_axis_ms(ax, scale="linear", from_zero=not panel_negative)
        # Headroom is set here rather than via add_headroom because a bar may be negative and
        # each annotation is two lines tall — both of which that helper's fixed 0-based
        # rescale would get wrong.
        low, high = ax.get_ylim()
        ax.set_ylim(min(low, 0) * 1.18 if low < 0 else 0, high * 1.28)
        title = f"{vus} VU(s)" + ("\n(saturated — capacity, not per-request cost)"
                                  if vus in saturated else "")
        ax.set_title(title, fontsize=11, color=INK, pad=14)

    axes[0].set_ylabel("WADM overhead per iteration (ms)", fontsize=10, color=INK)

    handles = [
        Patch(facecolor=INK_MUTED, alpha=0.32, edgecolor="white", linewidth=2,
              label="Measured end-to-end overhead (WADM − bare)"),
        Patch(facecolor=INK_MUTED, alpha=0.95, edgecolor="white", linewidth=2,
              label="Accounted for by the edge's internal timers"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=2, frameon=False, fontsize=9,
               bbox_to_anchor=(0.5, 0.05))

    fig.suptitle(
        "What the internal timers account for, against what a request actually pays",
        fontsize=14,
        fontweight="bold",
        color=INK,
    )
    caption = (
        "Per k6 iteration (4 requests); each panel is scaled to its own load level. "
        "Measured = 4 × (median http_req_duration with WADM − without). Accounted = Σ over kinds "
        "and phases of (median operation cost × operations fired) ÷ iterations. The gap is real "
        "WADM cost the timers exclude by design — config parse, per-request setup, "
        "response-body buffering, and the alert log writes."
    )
    if any_negative:
        caption += (
            "  A negative bar means the bare tier measured slower than the WADM tier: host "
            "noise, not a speed-up. Re-run that level."
        )
    fig.text(0.5, 0.012, caption, ha="center", fontsize=8, style="italic", color=INK_MUTED,
             wrap=True)
    fig.tight_layout(rect=[0, 0.11, 1, 0.93])

    out = out_dir / "wadm_overhead_breakdown.png"
    fig.savefig(out, dpi=150, facecolor="white")
    plt.close(fig)
    print(f"  wrote {out}")


def main():
    results_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent / "results"
    )
    out_dir = results_dir / "plots"
    out_dir.mkdir(parents=True, exist_ok=True)

    data, origin, vus_list = collect_e2e(results_dir)
    if not data:
        print(
            f"No end-to-end result files found in {results_dir}.\n"
            "Run `python3 benchmarks/run_baseline_benchmark.py --all` for the bare tiers and the "
            "four run_internal_<edge>_benchmark.py scripts for the WADM tiers."
        )
        return 1

    edges_present = [name for name in EDGES if name in data]
    internal, _ = collect(results_dir)
    saturated = saturated_levels(data, edges_present, vus_list)
    if saturated:
        print(f"  saturated VU levels (offered rate not sustained): {sorted(saturated)}")

    print(f"Generating end-to-end comparison plots for VU levels: {vus_list}")
    for vus in vus_list:
        plot_comparison_for_vus(vus, data, origin, edges_present, saturated, out_dir)
        plot_phase_overhead_for_vus(vus, data, edges_present, saturated, out_dir)
    plot_overhead_scaling(data, origin, edges_present, vus_list, saturated, out_dir)
    plot_overhead_breakdown(data, internal, edges_present, vus_list, saturated, out_dir)
    print(f"Done. {2 * len(vus_list) + 2} figure(s) in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
