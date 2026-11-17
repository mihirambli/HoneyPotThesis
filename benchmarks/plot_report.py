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
  latency_added.pdf      latency HDM adds, mean of the four GET probes, median and p95, per rate
  latency_levels.pdf     absolute latency (mean of GET probes) of origin, reverse proxy, reverse proxy with HDM
                         (proxy variants only)
  cpu_added.pdf          CPU time HDM adds per request, per rate and as a marginal fit
  hdm_timers.pdf         duration of HDM's detection and injection per honeytoken type
  cost_scopes.pdf        HDM's timed operations against all CPU and latency it adds, per iteration
  page_size.pdf          latency HDM adds against the size of the page
  report_values.md       every plotted number, for the text to quote from, and the CPU utilisation
                         of each server, which the text reports as a table

Usage:
    python3 benchmarks/plot_report.py [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from analyze_paired import paired_marginal_cpu, valid_cells
from plot_cost_attribution import scopes_for, with_marginal_cpu
import plot_mechanisms as mech

RESULTS = Path(__file__).resolve().parent / "results"

# Report order: the proxy deployment first, then the in-server one, as Chapter 5 presents them.
VARIANTS = ["envoy_lua", "wasm", "openresty_inserver", "apache_inserver"]
NAMES = {
    "envoy_lua": "Envoy Lua",
    "wasm": "Envoy WebAssembly",
    "openresty_inserver": "OpenResty",
    "apache_inserver": "Apache",
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


def dots_by_rate(ax, values: dict[str, dict[int, tuple[float, float, float] | None]], ylabel: str,
                 variants: list[str] = VARIANTS):
    """One column per rate, one offset marker per variant, the replicate range as a whisker."""
    offsets = {v: (i - (len(variants) - 1) / 2) * 0.17 for i, v in enumerate(variants)}
    for v in variants:
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
        dots_by_rate(ax, values, "latency added, mean of GET probes (µs)")
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
    fig.supxlabel("median latency, mean of GET probes (µs)", fontsize=8.5)
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
    for ax, phase, title in ((axes[0], "detect", "Detection"), (axes[1], "inject", "Injection")):
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
    rows = [("timers", "inside HDM's timed\ndetection and injection"),
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
    # The right panel zooms into the two small pages: on the full panel's scale, set by the
    # 768 KiB values, their differences of tens of microseconds collapse into one line.
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, 2.7), layout="constrained",
                             gridspec_kw={"width_ratios": [1.4, 1]})
    full, zoom = axes
    small = [kib for kib, _ in sizes[:2]]
    # Each variant is shifted a little along x so that error bars at the same size do not overlap.
    offset = {v: (i - (len(VARIANTS) - 1) / 2) for i, v in enumerate(VARIANTS)}
    for v in VARIANTS:
        doc = mech.load(RESULTS, f"size_{v}.json")
        if not doc:
            continue
        costs = mech.size_costs(doc)["latency_added_us"]
        points = {}
        for kib, name in sizes:
            point = us(costs.get(name))
            notes.append(f"page_size {v} {kib} KB: {fmt(point)} (n={costs.get(name, {}).get('n')})")
            if point is not None:
                points[kib] = point
        style_kw = dict(color=COLOR[v], marker=MARKER[v], markersize=5.5, elinewidth=1.1,
                        capsize=2.5, markeredgecolor="white", markeredgewidth=0.7)
        xs = sorted(points)
        full.errorbar([x * 1.06 ** offset[v] for x in xs], [points[x][0] for x in xs],
                      yerr=[[points[x][0] - points[x][1] for x in xs],
                            [points[x][2] - points[x][0] for x in xs]],
                      linewidth=1.8, label=NAMES[v], **style_kw)
        zs = [x for x in small if x in points]
        zoom.errorbar([small.index(x) + 0.12 * offset[v] for x in zs], [points[x][0] for x in zs],
                      yerr=[[points[x][0] - points[x][1] for x in zs],
                            [points[x][2] - points[x][0] for x in zs]],
                      linestyle="", **style_kw)
    full.set_xscale("log")
    full.set_xticks([1, 16, 256, 768])
    full.set_xticklabels(["1", "16", "256", "768"])
    full.minorticks_off()
    full.set_xlabel("page size (KB, logarithmic)")
    full.set_ylabel("latency added per request (µs)")
    full.set_title("All page sizes", loc="left")
    zoom.set_xticks(range(len(small)))
    zoom.set_xticklabels([f"{kib} KB" for kib in small])
    zoom.set_xlim(-0.6, len(small) - 0.4)
    zoom.set_xlabel("page size")
    zoom.set_title("1 and 16 KB, enlarged", loc="left")
    zoom.grid(axis="x", visible=False)
    for ax in axes:
        ax.axhline(0, color=MUTED, linewidth=0.8)
    top_legend(fig)
    path = out / "page_size.pdf"
    fig.savefig(path)
    fig.savefig(path.with_suffix(".png"), dpi=200)
    plt.close(fig)
    return path


# vCPUs pinned to each container (docker-compose.yml): utilisation is the share of these that the
# container kept busy, so a host server and nginx are each measured against their own allocation.
VCPUS = {"edge": 2, "origin": 1}


def utilisation(docs: dict[str, Any], notes: list[str]) -> None:
    """Share of its pinned vCPUs each server kept busy in the measured window, in per cent.

    cores_used is the cgroup CPU time over the window divided by the window's length. Per tier the
    median over valid replicates is reported; HDM's addition is silent minus bare within each
    replicate, as for every other cost.
    """
    for v in VARIANTS:
        for rate in RATES:
            cells = {(c["replicate"], c["tier"]): c for c in docs[v]["cells"]
                     if c["rate"] == rate and c["validity"].get("valid")}
            reps = sorted({rep for rep, _ in cells})
            for key in ("edge", "origin") if v in PROXY_VARIANTS else ("edge",):
                def pct(rep: int, tier: str) -> float | None:
                    cell = cells.get((rep, tier))
                    return None if cell is None else cell["cpu"][key]["cores_used"] / VCPUS[key] * 100
                pairs = [(pct(r, "bare"), pct(r, "silent")) for r in reps]
                pairs = [(b, s) for b, s in pairs if b is not None and s is not None]
                if not pairs:
                    continue
                bare = sorted(b for b, _ in pairs)
                silent = sorted(s for _, s in pairs)
                added = sorted(s - b for b, s in pairs)
                mid = len(pairs) // 2
                who = "server" if key == "edge" else "nginx"
                notes.append(f"utilisation {who} {v} {rate}/s: bare {bare[mid]:.2f} %, silent "
                             f"{silent[mid]:.2f} %, added {added[mid]:+.2f} "
                             f"[{added[0]:+.2f}..{added[-1]:+.2f}] points")


MATRIX_CASES = [
    ("HTML page", "HTML page"),
    ("HTML, gzip-encoded", "HTML page, compressed with gzip"),
    ("HEAD of an HTML page", "HEAD request"),
    ("conditional GET (If-None-Match)", "Conditional request"),
    ("range request (bytes=0-99)", "Range request (first 100 bytes)"),
    ("204 No Content", "Empty response (204)"),
    ("404 page", "Page not found (404)"),
    ("JSON", "JSON document"),
    ("binary", "Binary file"),
    ("HTML without </body>", "HTML without </body>"),
    ("HTML, 768 KiB", "HTML page of 768 KB"),
    ("HTML, 2 MiB (above Envoy's 1 MiB buffer)", "HTML page of 2 MB"),
    ("HTTP/1.0 client", "HTTP/1.0 request"),
]
# Ordered from harmless to harmful; every cell also prints its word, so colour is never the only cue.
VERDICT_STYLE = {
    "preserved": ("#e8e6e1", INK),
    "injected": ("#cfe9d4", INK),
    "changed": ("#f6dfa8", INK),
    "broken": ("#c8302f", "#ffffff"),
    "not exercised": ("#ffffff", MUTED),
}


def fig_matrix(out: Path, notes: list[str]) -> Path:
    doc = load_json(RESULTS / "functional_matrix.json")
    fig, ax = plt.subplots(figsize=(WIDTH, 3.9), layout="constrained")
    for row, (case, label) in enumerate(MATRIX_CASES):
        for col, v in enumerate(VARIANTS):
            verdict = doc["edges"][v][case]["verdict"].lower()
            notes.append(f"matrix {v} {label}: {verdict}")
            face, ink = VERDICT_STYLE[verdict]
            ax.add_patch(plt.Rectangle((col, row), 1, 1, facecolor=face, edgecolor="white",
                                       linewidth=2))
            ax.text(col + 0.5, row + 0.5, verdict, ha="center", va="center", fontsize=7.5, color=ink,
                    fontweight="bold" if verdict == "broken" else "normal")
    ax.set_xlim(0, len(VARIANTS))
    ax.set_ylim(len(MATRIX_CASES), 0)
    ax.set_xticks([c + 0.5 for c in range(len(VARIANTS))])
    ax.set_xticklabels([NAMES[v] for v in VARIANTS])
    ax.xaxis.tick_top()
    ax.set_yticks([r + 0.5 for r in range(len(MATRIX_CASES))])
    ax.set_yticklabels([label for _, label in MATRIX_CASES])
    ax.tick_params(length=0)
    ax.grid(False)
    for spine in ax.spines.values():
        spine.set_visible(False)
    path = out / "functional_matrix.pdf"
    fig.savefig(path)
    fig.savefig(path.with_suffix(".png"), dpi=200)
    plt.close(fig)
    return path


def sqli_costs(doc: dict[str, Any], rate: int = 100) -> dict[str, tuple[float, float, float]]:
    """(median, min, max) over replicates of the SQL-injection trap's 2x2, from the instrumented tier.

    Within each replicate, decoding is the encoded minus the plain probe and scanning further the
    clean login minus the signature hit, each averaged over the other factor, so both are paired.
    """
    base, decode, scan = [], [], []
    for c in valid_cells(doc, "wadm", rate):
        arm = lambda kind: c["tokens"][kind]["detect"]["trimmed_mean_us"]
        hit, hit_enc = arm("sql_injection"), arm("sql_injection_encoded")
        miss, miss_enc = arm("sql_injection_miss"), arm("sql_injection_miss_encoded")
        base.append(hit)
        decode.append(((hit_enc - hit) + (miss_enc - miss)) / 2)
        scan.append(((miss - hit) + (miss_enc - hit_enc)) / 2)
    spread = lambda xs: (statistics.median(xs), min(xs), max(xs))
    return {"base": spread(base), "decode": spread(decode), "scan": spread(scan)}


def fig_sqli(docs: dict[str, Any], out: Path, notes: list[str]) -> Path:
    # Own scale per panel: the plain hit takes up to 16 µs, while decoding and scanning further
    # differ between the variants by fractions of a microsecond.
    panels = [("base", "Signature hit, plain"), ("decode", "Decoding"), ("scan", "Scanning further")]
    fig, axes = plt.subplots(1, 3, figsize=(WIDTH, 2.1), sharey=True, layout="constrained")
    costs = {v: sqli_costs(docs[v]) for v in VARIANTS}
    for ax, (key, title) in zip(axes, panels):
        for i, v in enumerate(VARIANTS):
            med, lo, hi = costs[v][key]
            notes.append(f"sqli {key} {v} 100/s: {med:.2f} [{lo:.2f}..{hi:.2f}] µs")
            row = len(VARIANTS) - 1 - i
            ax.barh(row, med, color=COLOR[v], hatch=HATCH[v], edgecolor="white", linewidth=0.8,
                    height=0.7)
            ax.errorbar(med, row, xerr=[[med - lo], [hi - med]], color=INK, elinewidth=1.1,
                        capsize=2.5)
            ax.text(hi, row, f"  {med:.2f}" if med < 10 else f"  {med:.1f}", va="center",
                    fontsize=7.5, color=MUTED)
        ax.set_title(title, loc="left")
        ax.set_xlim(0, ax.get_xlim()[1] * 1.25)
        ax.grid(axis="y", visible=False)
    axes[0].set_yticks(range(len(VARIANTS)))
    axes[0].set_yticklabels([NAMES[v] for v in reversed(VARIANTS)])
    fig.supxlabel("duration (µs)", fontsize=8.5)
    path = out / "sqli_trap.pdf"
    fig.savefig(path)
    fig.savefig(path.with_suffix(".png"), dpi=200)
    plt.close(fig)
    return path


LUAJIT_PAIR = ["envoy_lua", "openresty_inserver"]


def fig_same_runtime(summary: dict[str, Any], out: Path, notes: list[str]) -> Path:
    """Envoy Lua against OpenResty (RQ4.3): the same runtime, similar operations, different totals."""
    fig, axes = plt.subplots(2, 2, figsize=(WIDTH, 4.6), layout="constrained")
    width = 0.36
    for ax, phase, title in ((axes[0][0], "detect", "Detection, 10 iterations/s"),
                             (axes[0][1], "inject", "Injection, 10 iterations/s")):
        for j, v in enumerate(LUAJIT_PAIR):
            internal = summary[v]["rates"]["10"]["internal_us"]
            heights = [((internal.get(t) or {}).get(phase) or {}).get("trimmed_mean_us") or 0.0
                       for t in TYPES]
            ax.bar([i + (j - 0.5) * width for i in range(len(TYPES))], heights, width=width * 0.95,
                   color=COLOR[v], hatch=HATCH[v], edgecolor="white", linewidth=0.6)
        ax.set_xticks(range(len(TYPES)))
        ax.set_xticklabels([TYPE_NAMES[t] for t in TYPES], fontsize=7)
        ax.set_ylabel("mean duration (µs)")
        ax.set_title(title, loc="left")
        ax.grid(axis="x", visible=False)
    # Both bar panels on one scale, so detection and injection can be read against each other.
    top = max(axes[0][0].get_ylim()[1], axes[0][1].get_ylim()[1])
    for ax in axes[0]:
        ax.set_ylim(0, top)
    for ax, key, title, scale in (
        (axes[1][0], "latency", "Latency added", 1000.0),
        (axes[1][1], "cpu", "CPU time added", 1.0),
    ):
        values = {v: {} for v in LUAJIT_PAIR}
        for v in LUAJIT_PAIR:
            for rate in RATES:
                block = summary[v]["rates"][str(rate)]["comparisons"]["silent_minus_bare"]
                delta = (block["latency_delta_quantiles_ms"]["per_get"]["med"] if key == "latency"
                         else block["cpu_delta_us_per_request"])
                values[v][rate] = us(delta, scale=scale)
                notes.append(f"same_runtime {key} {v} {rate}/s: {fmt(values[v][rate])}")
        dots_by_rate(ax, values, "µs per request" if key == "cpu" else "µs, GET probes",
                     variants=LUAJIT_PAIR)
        ax.set_title(title, loc="left")
        ax.set_xlabel("iterations per second")
    fig.legend(handles=[h for h in legend_handles() if h.get_label() in {NAMES[v] for v in LUAJIT_PAIR}],
               loc="outside upper center", ncol=2, frameon=False)
    path = out / "same_runtime.pdf"
    fig.savefig(path)
    fig.savefig(path.with_suffix(".png"), dpi=200)
    plt.close(fig)
    return path


def added_utilisation(doc: dict[str, Any], rate: int) -> tuple[float, float, float] | None:
    """(median, min, max) over replicates of silent minus bare utilisation of the server, in points."""
    cells = {(c["replicate"], c["tier"]): c for c in doc["cells"]
             if c["rate"] == rate and c["validity"].get("valid")}
    added = []
    for rep in sorted({rep for rep, _ in cells}):
        bare, silent = cells.get((rep, "bare")), cells.get((rep, "silent"))
        if bare and silent:
            added.append((silent["cpu"]["edge"]["cores_used"] - bare["cpu"]["edge"]["cores_used"])
                         / VCPUS["edge"] * 100)
    if not added:
        return None
    added.sort()
    return added[len(added) // 2], added[0], added[-1]


def fig_util_added(docs: dict[str, Any], out: Path, notes: list[str]) -> Path:
    # One panel per load level: the additions at 10/s are an order of magnitude below those at
    # 100/s, so a shared axis would flatten them to nothing.
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, 2.0), sharey=True, layout="constrained")
    for ax, rate in zip(axes, (10, 100)):
        for i, v in enumerate(VARIANTS):
            point = added_utilisation(docs[v], rate)
            notes.append(f"util_added {v} {rate}/s: {point}")
            if point is None:
                continue
            med, lo, hi = point
            row = len(VARIANTS) - 1 - i
            ax.barh(row, med, color=COLOR[v], hatch=HATCH[v], edgecolor="white", linewidth=0.8,
                    height=0.7)
            ax.errorbar(med, row, xerr=[[med - lo], [hi - med]], color=INK, elinewidth=1.1,
                        capsize=2.5)
            # The table no longer carries these values, so the median is printed beside its bar.
            ax.text(hi, row, f"  {med:.2f}" if med < 1 else f"  {med:.1f}", va="center",
                    fontsize=7.5, color=MUTED)
        ax.set_title(f"{rate} iterations per second", loc="left")
        ax.set_xlim(0, ax.get_xlim()[1] * 1.15)
        ax.grid(axis="y", visible=False)
    axes[0].set_yticks(range(len(VARIANTS)))
    axes[0].set_yticklabels([NAMES[v] for v in reversed(VARIANTS)])
    fig.supxlabel("CPU utilisation added by HDM (percentage points)", fontsize=8.5)
    path = out / "util_added.pdf"
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
        fig_util_added(docs, out, notes),
        fig_matrix(out, notes),
        fig_sqli(docs, out, notes),
        fig_same_runtime(summary, out, notes),
    ]
    utilisation(docs, notes)
    (out / "report_values.md").write_text(
        "# Values plotted by plot_report.py\n\nMedian [min..max] of paired replicates, µs.\n\n"
        + "\n".join(f"- {n}" for n in notes) + "\n", encoding="utf-8")
    for path in written:
        print(f"  wrote {path}")
    print(f"  wrote {out / 'report_values.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
