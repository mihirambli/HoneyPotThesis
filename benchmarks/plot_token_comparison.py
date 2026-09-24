#!/usr/bin/env python3
"""Break the four honeypot edges down per honeytoken kind, for both WADM phases.

Every edge times each honeytoken kind in its own microsecond region and logs it as
`WADM TOKEN <kind> <phase> (us): N` (html_comments keeps its original
`Detection/Injection execution time (us)` lines). The orchestrators fold both families
into a `tokens` section of their result files; this script renders them per kind.

This is the companion to plot_edge_comparison.py, which pools the same samples into
one distribution per edge. Use that one to rank edges, this one to see which kind
drives the cost.

`sql_injection` appears in the detection row only. It plants nothing — the login form is the
origin's own page — so it has no injection region to time, and its injection panel is omitted
rather than drawn empty, which would read as "measured, and zero".

Two figure families are produced into <results_dir>/plots/:

    token_comparison_vus_<N>.png   one panel per (phase, kind) at a fixed VU level;
                                   four edge boxes per panel, y shared across a row so
                                   kinds are comparable within a phase.
    token_scaling_<phase>.png      median latency vs. VU level per kind, one line per
                                   edge with an IQR band, showing how each kind scales.

Usage:
    python3 benchmarks/plot_token_comparison.py [results_dir]
"""

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

from plot_common import (
    COLORS,
    EDGES,
    INK,
    INK_MUTED,
    KIND_LABELS,
    KINDS_FOR,
    PHASE_LABELS,
    PHASES,
    add_headroom,
    collect,
    draw_edge_boxes,
    finalize_flat_boxes,
    place_direct_labels,
    quartiles,
    samples_for,
    stats_for,
    style_axis,
)


def plot_for_vus(vus, data, edges_present, out_dir):
    # Phases carry different numbers of kinds (sql_injection is detection-only), so the grid is
    # sized for the widest row and the surplus cells are removed below.
    ncols = max(len(KINDS_FOR[p]) for p in PHASES)
    fig, axes = plt.subplots(
        len(PHASES),
        ncols,
        figsize=(3.4 * ncols, 4.1 * len(PHASES)),
        sharey="row",
    )
    any_fallback = False
    # With sharey the row's limits are not final until every panel is drawn, and the
    # flat-box floor is computed in display space — so collect and finalize afterwards.
    flat_per_panel = {}

    for row, phase in enumerate(PHASES):
        kinds = KINDS_FOR[phase]
        for col in range(len(kinds), ncols):
            axes[row][col].set_visible(False)
        for col, kind in enumerate(kinds):
            ax = axes[row][col]
            fallback, flat = draw_edge_boxes(
                ax,
                edges_present,
                lambda name, k=kind, p=phase: samples_for(data[name], vus, k, p),
                lambda name, k=kind, p=phase: stats_for(data[name], vus, k, p),
            )
            any_fallback |= fallback
            flat_per_panel[(row, col)] = flat

            # Edge names on the x axis of the bottom row make identity readable without
            # colour; the top row shares the same fixed left-to-right order.
            if row == len(PHASES) - 1 or col >= len(KINDS_FOR[PHASES[row + 1]]):
                ax.set_xticklabels(edges_present, rotation=40, ha="right", fontsize=8)
            else:
                ax.set_xticklabels([])
            if row == 0:
                ax.set_title(KIND_LABELS[kind], fontsize=9.5, color=INK, pad=10)
            if col == 0:
                ax.set_ylabel(
                    f"{PHASE_LABELS[phase]}\nlatency (µs, symlog)", fontsize=10, color=INK
                )

    for (row, col), flat in flat_per_panel.items():
        finalize_flat_boxes(axes[row][col], flat)

    handles = [Patch(facecolor=COLORS[n], edgecolor="white", label=n) for n in edges_present]
    fig.legend(
        handles=handles,
        loc="lower center",
        ncol=len(edges_present),
        frameon=False,
        fontsize=9,
        bbox_to_anchor=(0.5, 0.052),
    )

    fig.suptitle(
        f"Per-honeytoken latency by edge — {vus} VU(s)",
        fontsize=14,
        fontweight="bold",
        color=INK,
    )
    caption = (
        "Box = Q1–Q3, line = median, whiskers = 1.5×IQR. Y axis shared per row; symlog "
        "(linear below 1 µs, log above) — the edges time in whole µs and a real share of "
        "samples lands on 0."
    )
    if any_fallback:
        caption += (
            "  Hatched boxes are summary approximations (box = min→p90, line = mean, "
            "whisker = max) — re-run that edge's benchmark for raw samples."
        )
    fig.text(0.5, 0.012, caption, ha="center", fontsize=8, style="italic", color=INK_MUTED, wrap=True)
    fig.tight_layout(rect=[0, 0.085, 1, 0.95])

    out = out_dir / f"token_comparison_vus_{vus}.png"
    fig.savefig(out, dpi=150, facecolor="white")
    plt.close(fig)
    print(f"  wrote {out}")


def plot_scaling(phase, data, edges_present, vus_list, out_dir):
    """Median latency vs. VU level, one line per edge, IQR shaded — one panel per kind."""
    kinds = KINDS_FOR[phase]
    fig, axes = plt.subplots(1, len(kinds), figsize=(3.4 * len(kinds), 4.6), sharey=True)
    x = range(len(vus_list))
    # Labels are placed in a second pass: with sharey, the y limits are not final until
    # every panel has been drawn, and the stagger is computed against those limits.
    endpoints_per_panel = {}

    for col, kind in enumerate(kinds):
        ax = axes[col]
        endpoints = []
        for name in edges_present:
            edge = data[name]
            medians, lows, highs, xs = [], [], [], []
            for xi, vus in zip(x, vus_list):
                values = samples_for(edge, vus, kind, phase)
                if values:
                    q1, med, q3 = quartiles(values)
                else:
                    stats = stats_for(edge, vus, kind, phase)
                    if not stats:
                        continue
                    q1, med, q3 = stats["min_us"], stats["avg_us"], stats["p90_us"]
                xs.append(xi)
                medians.append(med)
                lows.append(q1)
                highs.append(q3)
            if not xs:
                continue
            ax.fill_between(xs, lows, highs, color=COLORS[name], alpha=0.14, linewidth=0)
            ax.plot(xs, medians, color=COLORS[name], linewidth=2, marker="o", markersize=5)
            # Direct label at the line end (dark ink; the coloured line carries identity).
            endpoints.append((name, xs[-1], medians[-1]))

        endpoints_per_panel[col] = endpoints
        ax.set_title(KIND_LABELS[kind], fontsize=9.5, color=INK, pad=10)
        ax.set_xticks(list(x))
        ax.set_xticklabels([str(v) for v in vus_list], fontsize=9)
        ax.set_xlabel("VUs", fontsize=9, color=INK_MUTED)
        ax.set_xlim(-0.35, len(vus_list) - 1 + 1.5)
        style_axis(ax)
        if col == 0:
            ax.set_ylabel(f"{PHASE_LABELS[phase]} latency (µs, symlog)", fontsize=10, color=INK)

    add_headroom(axes[0])  # sharey propagates to the rest
    for col, endpoints in endpoints_per_panel.items():
        place_direct_labels(axes[col], endpoints)

    fig.suptitle(
        f"{PHASE_LABELS[phase]} latency vs. load, per honeytoken kind",
        fontsize=14,
        fontweight="bold",
        color=INK,
    )
    fig.text(
        0.5,
        0.015,
        "Line = median, band = Q1–Q3. Each edge is direct-labelled at its right-hand end. "
        "Y axis is symlog (linear below 1 µs, logarithmic above).",
        ha="center",
        fontsize=8,
        style="italic",
        color=INK_MUTED,
    )
    fig.tight_layout(rect=[0, 0.06, 1, 0.93])

    out = out_dir / f"token_scaling_{phase}.png"
    fig.savefig(out, dpi=150, facecolor="white")
    plt.close(fig)
    print(f"  wrote {out}")


def main():
    results_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent / "results"
    )
    out_dir = results_dir / "plots"
    out_dir.mkdir(parents=True, exist_ok=True)

    data, vus_list = collect(results_dir)
    if not data:
        print(f"No result files found in {results_dir}")
        return 1
    edges_present = [name for name in EDGES if name in data]

    print(f"Generating honeytoken plots for VU levels: {vus_list}")
    for vus in vus_list:
        plot_for_vus(vus, data, edges_present, out_dir)
    for phase in PHASES:
        plot_scaling(phase, data, edges_present, vus_list, out_dir)
    print(f"Done. {len(vus_list) + len(PHASES)} figure(s) in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
