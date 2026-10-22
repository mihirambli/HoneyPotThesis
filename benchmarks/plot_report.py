#!/usr/bin/env python3
"""
The figures and tables of the thesis report's results chapter, for its four variants only.

The other plotters draw the repository's full edge set (EDGE_KEYS), which still includes the
OpenResty and Apache proxy variants the report no longer covers. This one draws exactly the report's
four, in the report's order and names, at the size of a printed A4 page, so a figure can be included
at \\textwidth without rescaling its text.

Every value is read from benchmarks/results/ (paired_summary.json, paired_<key>.json, get_<key>.json,
size_<key>.json) and is a median of paired replicates with its range, as Chapter 6 defines them.
Variants come from different runs, so a figure places paired differences side by side; it never
subtracts one variant's latency from another's.

Writes (default benchmarks/results/plots/report/; pass --out to change):
  latency_added.pdf      latency HDM adds per GET, median and p95, per rate
  latency_levels.pdf     absolute latency per GET of origin, reverse proxy, reverse proxy with HDM
                         (proxy variants only)
  cpu_added.pdf          CPU time HDM adds per request, per rate and as a marginal fit
  hdm_timers.pdf         duration of HDM's detection and insertion per honeytoken type
  cost_scopes.pdf        HDM's timed operations against all CPU and latency it adds, per iteration
  page_size.pdf          latency HDM adds against the size of the page
  report_values.md       every plotted number, for the text to quote from

Usage:
    python3 benchmarks/plot_report.py [--out DIR]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from analyze_paired import paired_marginal_cpu
from plot_cost_attribution import scopes_for, with_marginal_cpu
import plot_mechanisms as mech

RESULTS = Path(__file__).resolve().parent / "results"

# Report order: the proxy deployment first, then the in-server one, as Chapter 5 presents them.
VARIANTS = ["envoy_lua", "wasm", "openresty_inserver", "apache_inserver"]
NAMES = {
    "envoy_lua": "Envoy Lua",
    "wasm": "Envoy WebAssembly",
    "openresty_inserver": "OpenResty in-server",
    "apache_inserver": "Apache in-server",
}
# Validated as a set (all pairs) with the dataviz palette checker: CVD separation sits in the 6-8
# floor band for aqua vs red, so identity is always carried twice — marker shape on dots, hatching
# on the in-server bars, and a legend in every figure.
COLOR = {
    "envoy_lua": "#eda100",
    "wasm": "#e34948",
    "openresty_inserver": "#2a78d6",
    "apache_inserver": "#1baf7a",
}
MARKER = {"envoy_lua": "o", "wasm": "s", "openresty_inserver": "^", "apache_inserver": "D"}
# The origin tier is reported for the proxy variants only: there it isolates what a reverse proxy
# without HDM adds, while for the in-server variants it would compare two web servers, which the
# thesis does not set out to measure.
PROXY_VARIANTS = ["envoy_lua", "wasm"]
HATCH = {"envoy_lua": "", "wasm": "", "openresty_inserver": "///", "apache_inserver": "///"}
RATES = [1, 10, 100]
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#dcdad5"

# A4 with the class's margins leaves a text block of about 6.2 in.
WIDTH = 6.2

TYPES = ["html_comments", "http_headers", "cookies", "decoy_paths", "form_fields"]
TYPE_NAMES = {
    "html_comments": "HTML\ncomment",
    "http_headers": "HTTP\nheader",
    "cookies": "Cookie",
    "decoy_paths": "Decoy\npath",
    "form_fields": "Form\nfield",
}


def style() -> None:
    plt.rcParams.update({
        "font.size": 8.5,
        "axes.titlesize": 9,
        "axes.labelsize": 8.5,
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "legend.fontsize": 8,
        "axes.edgecolor": MUTED,
        "axes.labelcolor": INK,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "text.color": INK,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "axes.axisbelow": True,
        "pdf.fonttype": 42,
        "savefig.bbox": "tight",
        "savefig.pad_inches": 0.03,
    })


def load_json(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def us(delta: dict[str, Any] | None, scale: float = 1000.0) -> tuple[float, float, float] | None:
    """(median, low, high) of a paired-delta summary, scaled (ms to µs by default)."""
    if not delta or delta.get("median") is None:
        return None
    return delta["median"] * scale, delta["min"] * scale, delta["max"] * scale


def legend_handles(markers: bool = True) -> list:
    if markers:
        return [Line2D([], [], color=COLOR[v], marker=MARKER[v], linestyle="", markersize=6,
                       label=NAMES[v]) for v in VARIANTS]
    return [Patch(facecolor=COLOR[v], edgecolor="white", hatch=HATCH[v], label=NAMES[v])
            for v in VARIANTS]


def top_legend(fig, markers: bool = True) -> None:
    fig.legend(handles=legend_handles(markers), loc="outside upper center", ncol=4, frameon=False)


def dots_by_rate(ax, values: dict[str, dict[int, tuple[float, float, float] | None]], ylabel: str):
    """One column per rate, one offset marker per variant, the replicate range as a whisker."""
    offsets = {v: (i - 1.5) * 0.17 for i, v in enumerate(VARIANTS)}
    for v in VARIANTS:
        for i, rate in enumerate(RATES):
            point = values[v].get(rate)
            if point is None:
                continue
            med, lo, hi = point
            x = i + offsets[v]
            ax.errorbar(x, med, yerr=[[med - lo], [hi - med]], fmt=MARKER[v], color=COLOR[v],
                        markersize=5.5, elinewidth=1.4, capsize=2.5, markeredgecolor="white",
                        markeredgewidth=0.7, zorder=3)
    ax.set_xticks(range(len(RATES)))
    ax.set_xticklabels([f"{r}" for r in RATES])
    ax.set_xlabel("offered load (iterations per second)")
    ax.set_ylabel(ylabel)
    ax.set_xlim(-0.6, len(RATES) - 0.4)
    ax.axhline(0, color=MUTED, linewidth=0.8, zorder=1)
    ax.grid(axis="x", visible=False)


def fig_latency_added(summary: dict[str, Any], out: Path, notes: list[str]) -> Path:
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, 2.9), layout="constrained")
    for ax, stat, title in ((axes[0], "med", "Median"), (axes[1], "p95", "95th percentile")):
        values = {v: {} for v in VARIANTS}
        for v in VARIANTS:
            for rate in RATES:
                block = summary[v]["rates"][str(rate)]["comparisons"]["silent_minus_bare"]
                values[v][rate] = us(block["latency_delta_quantiles_ms"]["per_get"][stat])
                notes.append(f"latency_added {stat} {v} {rate}/s: {fmt(values[v][rate])}")
        dots_by_rate(ax, values, "latency added per GET (µs)")
        ax.set_title(title, loc="left")
    top_legend(fig)
    path = out / "latency_added.pdf"
    fig.savefig(path)
    fig.savefig(path.with_suffix(".png"), dpi=200)
    plt.close(fig)
    return path


def fig_latency_levels(summary: dict[str, Any], out: Path, notes: list[str]) -> Path:
    fig, axes = plt.subplots(1, 3, figsize=(WIDTH, 2.0), sharey=True, layout="constrained")
    tiers = [("origin", "origin server directly"), ("bare", "reverse proxy without HDM"),
             ("silent", "reverse proxy with HDM")]
    for ax, rate in zip(axes, RATES):
        for row, v in enumerate(reversed(PROXY_VARIANTS)):
            levels = summary[v]["rates"][str(rate)]["latency_levels_ms"]
            pts = {t: levels[t]["median"] * 1000.0 for t, _ in tiers}
            notes.append(f"latency_levels {v} {rate}/s: " + ", ".join(
                f"{t}={pts[t]:.0f}" for t, _ in tiers))
            ax.plot([pts["origin"], pts["bare"], pts["silent"]], [row] * 3, color=COLOR[v],
                    linewidth=1.6, alpha=0.55, zorder=2)
            ax.plot(pts["origin"], row, "o", color="white", markeredgecolor=MUTED, markersize=6,
                    markeredgewidth=1.2, zorder=3)
            ax.plot(pts["bare"], row, MARKER[v], color="white", markeredgecolor=COLOR[v],
                    markersize=6, markeredgewidth=1.6, zorder=3)
            ax.plot(pts["silent"], row, MARKER[v], color=COLOR[v], markeredgecolor="white",
                    markersize=6.5, markeredgewidth=0.7, zorder=4)
        ax.set_title(f"{rate} iteration{'s' if rate > 1 else ''}/s", loc="left")
        ax.grid(axis="y", visible=False)
        ax.set_xlim(left=200)
    axes[0].set_yticks(range(len(PROXY_VARIANTS)))
    axes[0].set_yticklabels([NAMES[v] for v in reversed(PROXY_VARIANTS)])
    axes[0].set_ylim(-0.6, len(PROXY_VARIANTS) - 0.4)
    handles = [
        Line2D([], [], marker="o", color="white", markeredgecolor=MUTED, markersize=6,
               markeredgewidth=1.2, linestyle="", label="origin server directly"),
        Line2D([], [], marker="s", color="white", markeredgecolor=INK, markersize=6,
               markeredgewidth=1.4, linestyle="", label="reverse proxy without HDM (hollow)"),
        Line2D([], [], marker="s", color=INK, markersize=6, linestyle="",
               label="reverse proxy with HDM (filled)"),
    ]
    fig.supxlabel("median latency per GET (µs)", fontsize=8.5)
    fig.legend(handles=handles, loc="outside upper center", ncol=3, frameon=False)
    path = out / "latency_levels.pdf"
    fig.savefig(path)
    fig.savefig(path.with_suffix(".png"), dpi=200)
    plt.close(fig)
    return path


def fig_cpu_added(summary: dict[str, Any], docs: dict[str, Any], out: Path, notes: list[str]) -> Path:
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, 2.9), layout="constrained",
                             gridspec_kw={"width_ratios": [1.5, 1]})
    values = {v: {} for v in VARIANTS}
    for v in VARIANTS:
        for rate in RATES:
            block = summary[v]["rates"][str(rate)]["comparisons"]["silent_minus_bare"]
            values[v][rate] = us(block["cpu_delta_us_per_request"], scale=1.0)
            notes.append(f"cpu_added {v} {rate}/s: {fmt(values[v][rate])}")
    dots_by_rate(axes[0], values, "CPU time added per request (µs)")
    axes[0].set_title("Per load level", loc="left")

    ax = axes[1]
    for i, v in enumerate(VARIANTS):
        fit = paired_marginal_cpu(docs[v], "bare", "silent")
        point = us(fit, scale=1.0)
        notes.append(f"cpu_marginal {v}: {fmt(point)}")
        if point is None:
            continue
        med, lo, hi = point
        row = len(VARIANTS) - 1 - i
        ax.barh(row, med, color=COLOR[v], hatch=HATCH[v], edgecolor="white", linewidth=0.8,
                height=0.7)
        ax.errorbar(med, row, xerr=[[med - lo], [hi - med]], color=INK, elinewidth=1.1, capsize=2.5)
    ax.set_yticks(range(len(VARIANTS)))
    ax.set_yticklabels([NAMES[v] for v in reversed(VARIANTS)])
    ax.set_xlabel("CPU time per request (µs)")
    ax.set_title("Marginal, over all loads", loc="left")
    ax.grid(axis="y", visible=False)
    top_legend(fig)
    path = out / "cpu_added.pdf"
    fig.savefig(path)
    fig.savefig(path.with_suffix(".png"), dpi=200)
    plt.close(fig)
    return path


def fig_hdm_timers(summary: dict[str, Any], out: Path, notes: list[str], rate: int = 10) -> Path:
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, 2.9), sharey=True, layout="constrained")
    width = 0.19
    for ax, phase, title in ((axes[0], "detect", "Detection"), (axes[1], "inject", "Insertion")):
        for j, v in enumerate(VARIANTS):
            internal = summary[v]["rates"][str(rate)]["internal_us"]
            heights = []
            for t in TYPES:
                stats = (internal.get(t) or {}).get(phase) or {}
                heights.append(stats.get("trimmed_mean_us") or 0.0)
            notes.append(f"timer {phase} {v} {rate}/s: " + ", ".join(
                f"{t}={h:.2f}" for t, h in zip(TYPES, heights)))
            xs = [i + (j - 1.5) * width for i in range(len(TYPES))]
            ax.bar(xs, heights, width=width * 0.92, color=COLOR[v], hatch=HATCH[v],
                   edgecolor="white", linewidth=0.6)
        ax.set_xticks(range(len(TYPES)))
        ax.set_xticklabels([TYPE_NAMES[t] for t in TYPES])
        ax.set_title(title, loc="left")
        ax.grid(axis="x", visible=False)
    axes[0].set_ylabel("mean duration per operation (µs)")
    top_legend(fig, markers=False)
    path = out / "hdm_timers.pdf"
    fig.savefig(path)
    fig.savefig(path.with_suffix(".png"), dpi=200)
    plt.close(fig)
    return path


def fig_cost_scopes(docs: dict[str, Any], out: Path, notes: list[str], rate: int = 100) -> Path:
    """Per iteration: time inside HDM's timers, all CPU HDM adds, and latency HDM adds to the GETs."""
    rows = [("timers", "inside HDM's timed\ndetection and insertion"),
            ("cpu_deployed", "CPU time HDM adds\n(marginal)"),
            ("e2e_get", "latency HDM adds\nto the four GET requests")]
    fig, axes = plt.subplots(1, 3, figsize=(WIDTH, 2.5), sharey=True, layout="constrained")
    for ax, (key, title) in zip(axes, rows):
        for i, v in enumerate(VARIANTS):
            entry = scopes_for(docs[v], rate)
            marginal = {"cpu_deployed": paired_marginal_cpu(docs[v], "bare", "silent")}
            if marginal["cpu_deployed"].get("n"):
                entry = with_marginal_cpu(entry, marginal)
            point = us(entry["series"][key], scale=1.0)
            notes.append(f"cost_scope {key} {v} {rate}/s per iteration: {fmt(point)}")
            if point is None:
                continue
            med, lo, hi = point
            row = len(VARIANTS) - 1 - i
            ax.barh(row, med, color=COLOR[v], hatch=HATCH[v], edgecolor="white", height=0.7,
                    linewidth=0.8)
            ax.errorbar(med, row, xerr=[[med - lo], [hi - med]], color=INK, elinewidth=1.1,
                        capsize=2.5)
        ax.set_title(title, loc="left", fontsize=8.5)
        ax.set_xlabel("µs per iteration")
        ax.grid(axis="y", visible=False)
        ax.set_xlim(left=0)
    axes[0].set_yticks(range(len(VARIANTS)))
    axes[0].set_yticklabels([NAMES[v] for v in reversed(VARIANTS)])
    path = out / "cost_scopes.pdf"
    fig.savefig(path)
    fig.savefig(path.with_suffix(".png"), dpi=200)
    plt.close(fig)
    return path


def fig_page_size(out: Path, notes: list[str]) -> Path:
    sizes = [(1, "size_1k"), (16, "size_16k"), (256, "size_256k"), (768, "size_768k")]
    fig, ax = plt.subplots(figsize=(WIDTH * 0.62, 2.6), layout="constrained")
    for v in VARIANTS:
        doc = mech.load(RESULTS, f"size_{v}.json")
        if not doc:
            continue
        costs = mech.size_costs(doc)["latency_added_us"]
        xs, meds, los, his = [], [], [], []
        for kib, name in sizes:
            point = us(costs.get(name))
            notes.append(f"page_size {v} {kib} KiB: {fmt(point)} (n={costs.get(name, {}).get('n')})")
            if point is None:
                continue
            xs.append(kib)
            meds.append(point[0])
            los.append(point[0] - point[1])
            his.append(point[2] - point[0])
        ax.errorbar(xs, meds, yerr=[los, his], color=COLOR[v], marker=MARKER[v], markersize=5.5,
                    linewidth=1.8, elinewidth=1.1, capsize=2.5, markeredgecolor="white",
                    markeredgewidth=0.7, label=NAMES[v])
    ax.set_xscale("log")
    ax.set_xticks([1, 16, 256, 768])
    ax.set_xticklabels(["1", "16", "256", "768"])
    ax.minorticks_off()
    ax.set_xlabel("page size (KiB, logarithmic)")
    ax.set_ylabel("latency added per request (µs)")
    ax.axhline(0, color=MUTED, linewidth=0.8)
    ax.legend(frameon=False, loc="upper left")
    path = out / "page_size.pdf"
    fig.savefig(path)
    fig.savefig(path.with_suffix(".png"), dpi=200)
    plt.close(fig)
    return path


def fmt(point: tuple[float, float, float] | None) -> str:
    if point is None:
        return "n/a"
    med, lo, hi = point
    spans = " (spans zero)" if lo <= 0 <= hi else ""
    return f"{med:+.0f} [{lo:+.0f}..{hi:+.0f}]{spans}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--out", default=str(RESULTS / "plots" / "report"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    style()

    summary = load_json(RESULTS / "paired_summary.json")
    docs = {v: load_json(RESULTS / f"paired_{v}.json") for v in VARIANTS}
    missing = [v for v in VARIANTS if not (summary and v in summary and docs[v])]
    if missing:
        print(f"Missing paired data for {missing}; run analyze_paired.py first.")
        return 1

    notes: list[str] = []
    written = [
        fig_latency_added(summary, out, notes),
        fig_latency_levels(summary, out, notes),
        fig_cpu_added(summary, docs, out, notes),
        fig_hdm_timers(summary, out, notes),
        fig_cost_scopes(docs, out, notes),
        fig_page_size(out, notes),
    ]
    (out / "report_values.md").write_text(
        "# Values plotted by plot_report.py\n\nMedian [min..max] of paired replicates, µs.\n\n"
        + "\n".join(f"- {n}" for n in notes) + "\n", encoding="utf-8")
    for path in written:
        print(f"  wrote {path}")
    print(f"  wrote {out / 'report_values.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
