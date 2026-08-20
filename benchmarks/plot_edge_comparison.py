#!/usr/bin/env python3
"""Compare the four honeypot edges across ALL honeytoken kinds, per VU level.

This is the edge-level view: for each VU level it places the four edges side by side
as box plots, with a detection subplot and an injection subplot, pooling every
honeytoken kind — html_comments, http_headers, cookies, decoy_paths, form_fields —
into one distribution per edge. It answers "which edge is cheapest at honeytoken work
overall". For the per-kind breakdown behind these numbers, see
plot_token_comparison.py.

The `sql_injection` trap is deliberately excluded: it plants no token, is a response
policy rather than a honeytoken kind, and logs under its own separate label.

Pooling is by concatenation of raw samples, so each kind contributes in proportion to
how often it actually fires within an iteration (the `/*` kinds inject on all four
benchmark requests, form_fields on one). The box therefore reads as "what a honeytoken
operation costs on this edge", not as a mean of per-kind means.

Data sources
------------
The benchmark scripts write two files per edge into benchmarks/results/:

    internal_<edge>_profile.json  - summary stats (min / avg / p90 / max), per kind
    internal_<edge>_raw.json      - raw per-request latencies (microseconds), per kind

This script prefers the raw file and draws a *true* box plot (real quartiles:
Q1 / median / Q3 with 1.5*IQR whiskers). If an edge's raw file is missing, it falls
back to a count-weighted pooled approximation from that edge's profile stats
(box = min->p90, center line = mean, whisker = max) and hatches the box so the
approximation is obvious. Re-run the benchmarks to replace any fallback boxes.

Usage:
    python3 benchmarks/plot_edge_comparison.py [results_dir]
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
    KINDS,
    PHASES,
    PHASE_LABELS,
    add_headroom,
    collect,
    draw_edge_boxes,
    finalize_flat_boxes,
    pooled_samples,
    pooled_stats,
)


def plot_for_vus(vus, data, edges_present, out_dir):
    fig, axes = plt.subplots(1, len(PHASES), figsize=(7 * len(PHASES), 6))
    any_fallback = False

    for ax, phase in zip(axes, PHASES):
        fallback, flat = draw_edge_boxes(
            ax,
            edges_present,
            lambda name, p=phase: pooled_samples(data[name], vus, p),
            lambda name, p=phase: pooled_stats(data[name], vus, p),
        )
        any_fallback |= fallback
        add_headroom(ax)
        finalize_flat_boxes(ax, flat)
        ax.set_title(f"{PHASE_LABELS[phase]} latency", fontsize=12, color=INK, pad=10)
        ax.set_ylabel("Latency (µs, symlog)", fontsize=10, color=INK)
        ax.set_xticklabels(edges_present, rotation=15)

    handles = [Patch(facecolor=COLORS[n], edgecolor="white", label=n) for n in edges_present]
    fig.legend(
        handles=handles,
        loc="lower center",
        ncol=len(edges_present),
        frameon=False,
        fontsize=9,
        bbox_to_anchor=(0.5, 0.055),
    )

    fig.suptitle(
        f"Edge latency comparison at {vus} VU(s) — all honeytoken kinds pooled",
        fontsize=14,
        fontweight="bold",
        color=INK,
    )

    caption = (
        f"Pooled over {', '.join(KINDS)}.\n"
        "Box = Q1–Q3, line = median, whiskers = 1.5×IQR. Y axis is symlog (linear below "
        "1 µs, log above) — the edges time in whole µs and a real share of samples lands on 0."
    )
    if any_fallback:
        caption += (
            "  Hatched boxes are count-weighted summary approximations "
            "(box = min→p90, line = mean, whisker = max) — re-run that edge's "
            "benchmark to get raw samples."
        )
    fig.text(0.5, 0.012, caption, ha="center", fontsize=8, style="italic", color=INK_MUTED, wrap=True)
    fig.tight_layout(rect=[0, 0.09, 1, 0.95])

    out = out_dir / f"edge_comparison_vus_{vus}.png"
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

    print(f"Generating pooled box plots for VU levels: {vus_list}")
    for vus in vus_list:
        plot_for_vus(vus, data, edges_present, out_dir)
    print(f"Done. {len(vus_list)} figure(s) in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
