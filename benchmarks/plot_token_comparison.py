#!/usr/bin/env python3
"""Compare the four honeypot edges per honeytoken kind, for both WADM phases.

Every edge now times each honeytoken kind in its own microsecond region and logs it
as `WADM TOKEN <kind> <phase> (us): N` (html_comments keeps its original
`Detection/Injection execution time (us)` lines). The orchestrators fold both families
into a `tokens` section of their result files; this script renders them.

Two figure families are produced into <results_dir>/plots/:

    token_comparison_vus_<N>.png   one panel per (phase, kind) at a fixed VU level;
                                   four edge boxes per panel, y shared across a row so
                                   kinds are comparable within a phase.
    token_scaling_<phase>.png      median latency vs. VU level per kind, one line per
                                   edge with an IQR band, showing how each kind scales.

Data sources (per edge, written by run_internal_<edge>_benchmark.py):

    internal_<edge>_raw.json      raw per-request latencies -> true box plots
    internal_<edge>_profile.json  summary stats -> fallback approximation, drawn hatched

Usage:
    python3 benchmarks/plot_token_comparison.py [results_dir]
"""

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter, SymmetricalLogLocator

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
KINDS = ["html_comments", "http_headers", "cookies", "decoy_paths", "form_fields"]
KIND_LABELS = {
    "html_comments": "html_comments\n(body comment)",
    "http_headers": "http_headers\n(response header)",
    "cookies": "cookies\n(Set-Cookie bait)",
    "decoy_paths": "decoy_paths\n(hidden link / trap URI)",
    "form_fields": "form_fields\n(hidden input)",
}
PHASES = ["detect", "inject"]
PHASE_LABELS = {"detect": "Detection", "inject": "Injection"}


def load_edge(results_dir, stem):
    """Return (raw, summary) as {vus: {kind: {phase: samples|stats}}} or None each."""
    raw_by_vus = None
    raw_path = results_dir / f"{stem}_raw.json"
    if raw_path.exists():
        doc = json.loads(raw_path.read_text())
        raw_by_vus = {}
        for run in doc["runs"]:
            tokens = run.get("tokens") or {}
            raw_by_vus[run["vus"]] = {
                kind: {
                    phase: tokens.get(kind, {}).get(f"{phase}_us", []) for phase in PHASES
                }
                for kind in KINDS
            }

    summary_by_vus = None
    profile_path = results_dir / f"{stem}_profile.json"
    if profile_path.exists():
        doc = json.loads(profile_path.read_text())
        summary_by_vus = {}
        for run in doc["runs"]:
            tokens = run.get("tokens") or {}
            summary_by_vus[run["vus"]] = {
                kind: {phase: tokens.get(kind, {}).get(phase) for phase in PHASES}
                for kind in KINDS
            }

    return raw_by_vus, summary_by_vus


def collect(results_dir):
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


def quartiles(values):
    """(q1, median, q3) by the nearest-rank convention used across this repo."""
    ordered = sorted(values)
    n = len(ordered)

    def at(pct):
        idx = max(1, int(-(-pct * n // 100))) - 1
        return float(ordered[idx])

    return at(25), at(50), at(75)


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
    ax.grid(True, which="major", axis="y", alpha=0.25, linewidth=0.8)
    ax.tick_params(colors=INK_MUTED, labelsize=9)
    ax.tick_params(axis="y", which="minor", labelsize=7, colors=INK_MUTED)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color("#d5d4cf")


def add_headroom(ax, factor=1.35):
    """Keep the topmost mark and its label clear of the axis frame."""
    top = ax.get_ylim()[1]
    ax.set_ylim(0, top * factor)


def draw_panel(ax, data, edges_present, vus, kind, phase):
    """One (kind, phase) panel: one box per edge. Returns True if a fallback was drawn."""
    used_fallback = False
    approx, approx_pos, approx_colors = [], [], []

    for i, name in enumerate(edges_present, start=1):
        edge = data[name]
        values = samples_for(edge, vus, kind, phase)
        if values:
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

        stats = stats_for(edge, vus, kind, phase)
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
    return used_fallback


def plot_for_vus(vus, data, edges_present, out_dir):
    fig, axes = plt.subplots(
        len(PHASES),
        len(KINDS),
        figsize=(3.4 * len(KINDS), 4.1 * len(PHASES)),
        sharey="row",
    )
    any_fallback = False

    for row, phase in enumerate(PHASES):
        for col, kind in enumerate(KINDS):
            ax = axes[row][col]
            any_fallback |= draw_panel(ax, data, edges_present, vus, kind, phase)

            # Edge names on the x axis of the bottom row make identity readable without
            # colour; the top row shares the same fixed left-to-right order.
            if row == len(PHASES) - 1:
                ax.set_xticklabels(edges_present, rotation=40, ha="right", fontsize=8)
            else:
                ax.set_xticklabels([])
            if row == 0:
                ax.set_title(KIND_LABELS[kind], fontsize=9.5, color=INK, pad=10)
            if col == 0:
                ax.set_ylabel(f"{PHASE_LABELS[phase]}\nlatency (µs, symlog)", fontsize=10, color=INK)

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


def plot_scaling(phase, data, edges_present, vus_list, out_dir):
    """Median latency vs. VU level, one line per edge, IQR shaded — one panel per kind."""
    fig, axes = plt.subplots(1, len(KINDS), figsize=(3.4 * len(KINDS), 4.6), sharey=True)
    x = range(len(vus_list))
    # Labels are placed in a second pass: with sharey, the y limits are not final until
    # every panel has been drawn, and the stagger is computed against those limits.
    endpoints_per_panel = {}

    for col, kind in enumerate(KINDS):
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
