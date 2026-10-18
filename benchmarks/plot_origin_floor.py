#!/usr/bin/env python3
"""
Latency each edge adds above the no-proxy floor: the bare proxy alone, and the proxy with WADM as
deployed, both against the origin answering k6 directly.

Every difference is paired: the origin tier runs inside the same replicate as the edge's bare and
silent tiers, minutes apart, so the floor drifts with them and cancels. A floor taken from a
separate session would not, and the drift between sessions is larger than the proxy's cost.

Figures (results/plots/origin_floor/):

    <rate>_per_s.png      per edge, latency added per GET above the origin: bare proxy (grey) and
                          proxy with WADM (edge colour). Bar = median of the paired differences
                          over replicates, whisker = their range, hollow = unresolved (the range
                          spans zero, or fewer than 3 replicates).
                          The bold figure on the right is the paired silent - bare difference, the
                          share WADM itself is responsible for.
    tier_comparison.png   the three-way comparison: per rate and edge, the absolute median latency per
                          GET with the origin answering directly, through the bare proxy, and through
                          the proxy with WADM, as grouped bars on one shared axis.
    latency_levels.png    the absolute median latency per GET of each tier, one panel per rate:
                          where the floor is and how far above it each tier sits. Descriptive only;
                          the paired figures are the comparison.

Tables (results/):

    origin_floor.md / .csv   per edge and rate: the origin's own latency per GET, bare - origin,
                             silent - origin and silent - bare, absolute and relative, then the same
                             per GET probe.

Only the four GET probes are drawn. The `all` mix's four SQLi POSTs are sent in the origin tier too,
so every tier offers the same load, but they are left out here: the origin has no trap, so against it
they measure a 404 rather than anything WADM does.

Reads benchmarks/results/paired_<edge>.json through analyze_paired's own statistics, so the numbers
match the console report.

Usage:
    python3 benchmarks/plot_origin_floor.py [results_dir]
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.legend_handler import HandlerTuple
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from analyze_paired import (
    GET_METRICS,
    cell_by_replicate,
    get_level,
    latency_levels,
    load_paired,
    paired_get_mean,
    paired_get_relative,
    paired_latency,
    paired_pairs,
    tiers_of,
)
from edge_specs import EDGE_KEYS, EDGES, ORIGIN_TIER
from plot_by_rate import (
    AXIS,
    BARE_GREY,
    GET_PROBES,
    GRID,
    US_PER_MS,
    Bar,
    footnote,
    new_figure,
    rate_label,
    save,
    style,
)
from plot_common import COLORS, INK, INK_MUTED
from wadm_timings import paired_delta

# (comparison name, lower tier, higher tier). silent is WADM as a deployment runs it, the tier every
# headline figure in this repo reports; the instrumented tier would add the benchmark's own logging.
ABOVE_FLOOR = [
    ("bare_minus_origin", ORIGIN_TIER, "bare"),
    ("silent_minus_origin", ORIGIN_TIER, "silent"),
]
WADM_ONLY = ("silent_minus_bare", "bare", "silent")

BAR_LABELS = {"bare_minus_origin": "bare proxy", "silent_minus_origin": "proxy + WADM"}
TIER_LEVEL_LABELS = {ORIGIN_TIER: "origin directly", "bare": "bare proxy", "silent": "proxy + WADM"}

# Clip only when one bar is more than this many times the next largest: Apache's crashing workers at
# 100/s add milliseconds where the other edges add tenths, and an unclipped axis flattens the rest.
CLIP_RATIO = 4.0


# ── Data ─────────────────────────────────────────────────────────────────────────────────────

def has_floor(doc: dict[str, Any]) -> bool:
    tiers = tiers_of(doc)
    return ORIGIN_TIER in tiers and "bare" in tiers and "silent" in tiers


def load_docs(results_dir: Path) -> dict[str, dict[str, Any]]:
    docs = {}
    for edge in EDGE_KEYS:
        doc = load_paired(results_dir, edge)
        if not doc:
            print(f"  ! no paired_{edge}.json — skipping {EDGES[edge].label}")
        elif not has_floor(doc):
            print(f"  ! paired_{edge}.json has no origin tier (recorded before it existed) — "
                  f"skipping {EDGES[edge].label}; re-run run_paired_benchmark.py --preset full")
        else:
            docs[edge] = doc
    return docs


def wadm_share(doc: dict[str, Any], rate: int) -> dict[str, Any]:
    """Share of the latency the deployed stack adds above the origin that WADM accounts for, in %.

    (silent - bare) / (silent - origin) per GET, within each replicate, then summarised: a ratio of
    two medians would mix replicates. Withheld unless the stack adds latency in every replicate,
    since a denominator at or below zero has no share to divide.
    """
    tiers = [cell_by_replicate(doc, t, rate) for t in (ORIGIN_TIER, "bare", "silent")]
    shares = []
    for rep in sorted(set.intersection(*(set(t) for t in tiers))):
        origin, bare, silent = (get_level(t[rep]) for t in tiers)
        if None in (origin, bare, silent):
            continue
        if silent - origin <= 0:
            return paired_delta([])
        shares.append(100.0 * (silent - bare) / (silent - origin))
    return paired_delta(shares)


def floor_stats(doc: dict[str, Any], rate: int) -> dict[str, Any]:
    """Every statistic the figures and tables draw for one edge at one rate."""
    out: dict[str, Any] = {
        "levels": latency_levels(doc, rate), "wadm_share_pct": wadm_share(doc, rate), "comparisons": {},
    }
    for name, low, high in [*ABOVE_FLOOR, WADM_ONLY]:
        out["comparisons"][name] = {
            "replicates_paired": len(paired_pairs(doc, rate, low, high)),
            "per_get": paired_get_mean(doc, rate, "med", low, high),
            "per_get_pct": paired_get_relative(doc, rate, low, high),
            "per_probe": {m: paired_latency(doc, rate, m, "med", low, high) for m in GET_METRICS},
        }
    return out


def rates_of(docs: dict[str, dict[str, Any]]) -> list[int]:
    return sorted({c["rate"] for d in docs.values() for c in d["cells"] if c["tier"] == ORIGIN_TIER})


def to_bar(delta: dict[str, Any]) -> Bar | str:
    if not delta.get("n"):
        return "no valid pair"
    return Bar(delta["median"] * US_PER_MS, delta["min"] * US_PER_MS, delta["max"] * US_PER_MS,
               bool(delta.get("significant")))


# ── Figures ──────────────────────────────────────────────────────────────────────────────────

def edge_colours_handle(edges: list[str], make) -> tuple:
    """One legend entry showing every edge's colour, for a series whose hue follows the edge."""
    return tuple(make(COLORS[EDGES[e].label]) for e in edges)


def tier_legend(fig, handles: list, labels: list[str]) -> None:
    fig.legend(handles=handles, labels=labels, handler_map={tuple: HandlerTuple(ndivide=None, pad=0)},
               loc="upper left", bbox_to_anchor=(0.01, 1 - 0.85 / fig.get_figheight()), ncol=len(handles),
               frameon=False, fontsize=9, handlelength=2.4, columnspacing=1.4)


def clip_for(bars: list[Bar]) -> float | None:
    peaks = sorted((max(abs(b.value), abs(b.high or 0)) for b in bars), reverse=True)
    if len(peaks) > 1 and peaks[1] > 0 and peaks[0] > CLIP_RATIO * peaks[1]:
        return peaks[1] * 2.2
    return None


def replicate_note(docs, stats, rate: int) -> str:
    notes = []
    for edge, doc in docs.items():
        planned = doc["metadata"].get("replicates")
        kept = min(c["replicates_paired"] for c in stats[edge]["comparisons"].values())
        if planned and kept < planned:
            notes.append(f"{EDGES[edge].label}: {kept}/{planned} replicates paired on every tier")
        crashed = sum(1 for c in doc["cells"]
                      if c["rate"] == rate and c["tier"] == "silent" and c["validity"].get("crash_count"))
        if crashed:
            notes.append(f"{EDGES[edge].label}: workers segfaulted in {crashed} WADM run(s) at this "
                         "rate, so its WADM bar includes crash recovery")
    return ". ".join(notes)


def plot_above_floor(docs, stats, rate: int, out: Path) -> Path:
    edges = list(docs)
    fig, ax = new_figure(
        len(edges), "Latency added above the origin, per GET",
        f"End-to-end, each tier paired against k6 reaching the origin directly — {rate_label(rate)}",
    )
    bars = {(e, n): to_bar(stats[e]["comparisons"][n]["per_get"]) for e in edges for n, _, _ in ABOVE_FLOOR}
    clip = clip_for([b for b in bars.values() if isinstance(b, Bar)])
    n = len(ABOVE_FLOOR)
    height = 0.8 / n
    extent = [0.0]
    for row, edge in enumerate(edges):
        colour = COLORS[EDGES[edge].label]
        for i, (name, _, _) in enumerate(ABOVE_FLOOR):
            y = row + (i - (n - 1) / 2) * height
            fill = BARE_GREY if name == "bare_minus_origin" else colour
            bar = bars[(edge, name)]
            if isinstance(bar, str):
                ax.text(0, y, f"  {BAR_LABELS[name]}: {bar}", va="center", fontsize=7.5,
                        color=INK_MUTED, style="italic")
                continue
            shown = bar.value if clip is None else max(min(bar.value, clip), -clip)
            # Hollow means the replicates disagree in sign, or are too few to say: a level the harness
            # cannot resolve, not a small cost.
            ax.barh(y, shown, height=height * 0.86, color=fill if bar.resolved else "white",
                    edgecolor=fill, linewidth=1.4)
            lo, hi = bar.low, bar.high
            if clip is not None:
                lo, hi = max(min(lo, clip), -clip), max(min(hi, clip), -clip)
            ax.plot([lo, hi], [y, y], color=INK, linewidth=0.9, alpha=0.65, solid_capstyle="butt")
            clipped = clip is not None and abs(bar.value) > clip
            pct = stats[edge]["comparisons"][name]["per_get_pct"]
            pct_s = f" ({pct['median']:+.0f}%)" if pct.get("n") else ""
            text = f"{BAR_LABELS[name]}: {bar.value:+,.0f} µs{pct_s}" + (" (clipped)" if clipped else "")
            anchor = shown if clipped else max(shown, hi)
            ax.text(anchor, y, f"  {text}", va="center", ha="left", fontsize=7.8, color=INK)
            extent.extend([shown, lo, hi])
        wadm = stats[edge]["comparisons"][WADM_ONLY[0]]["per_get"]
        if wadm.get("n"):
            ax.text(1.0, row, f"of which WADM\n{wadm['median'] * US_PER_MS:+,.0f} µs",
                    transform=ax.get_yaxis_transform(), ha="left", va="center", fontsize=8.5,
                    color=INK, fontweight="bold")

    ax.set_yticks(range(len(edges)))
    ax.set_yticklabels([EDGES[e].label for e in edges])
    ax.set_ylim(len(edges) - 0.5, -0.5)
    for row in range(1, len(edges)):
        ax.axhline(row - 0.5, color=GRID, linewidth=0.8)
    lo, hi = min(extent), max(extent)
    span = (hi - lo) or 1.0
    ax.set_xlim(lo - (0.05 * span if lo < 0 else 0), hi + 0.45 * span)
    ax.axvline(0, color=INK_MUTED, linewidth=0.9)
    style(ax, "microseconds added to the median GET, above the origin answering directly")
    fig.subplots_adjust(left=0.16, right=0.85)
    tier_legend(
        fig,
        [Patch(facecolor=BARE_GREY, edgecolor=BARE_GREY),
         edge_colours_handle(edges, lambda c: Patch(facecolor=c, edgecolor=c)),
         Patch(facecolor="white", edgecolor=INK_MUTED)],
        ["bare proxy − origin", "proxy + WADM − origin", "unresolved: range spans zero, or < 3 replicates"],
    )
    note = replicate_note(docs, stats, rate)
    footnote(
        fig,
        "Per GET = the mean of the four GET probes' median latencies, differenced within each "
        "replicate; bar = median of those paired differences, whisker = their range, % = relative to "
        "the origin. 'Of which WADM' is the paired proxy + WADM − bare proxy difference; it is its "
        "own median, so it need not equal the gap between the two bars. WADM is the deployed build "
        "(no benchmark timing lines). The SQLi POSTs are excluded: the origin has no trap."
        + (f"\n{note}." if note else ""),
    )
    return save(fig, out)


def level_axis_max(medians: list[float]) -> tuple[float, float | None]:
    """(axis maximum, median beyond it) for one panel of the levels figure.

    A tier whose median is more than CLIP_RATIO times the next largest would push every other dot
    against zero, so the axis is fitted to the rest and that one is drawn at the edge.
    """
    peaks = sorted(medians, reverse=True)
    if len(peaks) > 1 and peaks[1] > 0 and peaks[0] > CLIP_RATIO * peaks[1]:
        return peaks[1] * 1.3, peaks[0]
    return (peaks[0] if peaks else 1.0) * 1.15, None


def plot_levels(docs, stats, rates: list[int], out: Path) -> Path:
    edges = list(docs)
    fig, axes = plt.subplots(1, len(rates), figsize=(4.0 * len(rates) + 1.6, 1.0 * len(edges) + 2.4),
                             squeeze=False)
    axes = axes[0]
    fig.subplots_adjust(left=0.13, right=0.98, top=1 - 1.5 / fig.get_figheight(),
                        bottom=1.25 / fig.get_figheight(), wspace=0.12)
    fig.text(0.015, 1 - 0.3 / fig.get_figheight(), "Median latency per GET, by tier",
             fontsize=14, fontweight="bold", color=INK, va="top")
    fig.text(0.015, 1 - 0.62 / fig.get_figheight(),
             "Where the origin floor sits and how far above it each tier lands; each edge's floor is "
             "the one measured in that edge's own replicates",
             fontsize=10, color=INK_MUTED, va="top")
    markers = {ORIGIN_TIER: ("white", INK), "bare": (BARE_GREY, BARE_GREY)}
    for ax, rate in zip(axes, rates):
        panel = {
            edge: [(t, lvl) for t in (ORIGIN_TIER, "bare", "silent")
                   if (lvl := stats[rate][edge]["levels"].get(t) or {}).get("n")]
            for edge in edges
        }
        xmax, beyond = level_axis_max(
            [lvl["median"] * US_PER_MS for points in panel.values() for _, lvl in points]
        )
        for row, edge in enumerate(edges):
            colour = COLORS[EDGES[edge].label]
            xs = [min(lvl["median"] * US_PER_MS, xmax) for _, lvl in panel[edge]]
            if len(xs) > 1:
                ax.plot([min(xs), max(xs)], [row, row], color=AXIS, linewidth=2, zorder=1)
            for tier, lvl in panel[edge]:
                face, edgecol = markers.get(tier, (colour, colour))
                med, hi = lvl["median"] * US_PER_MS, lvl["max"] * US_PER_MS
                ax.plot([lvl["min"] * US_PER_MS, min(hi, xmax)], [row, row], color=INK,
                        linewidth=0.9, alpha=0.5, zorder=2)
                if hi > xmax and med <= xmax:
                    ax.text(xmax, row - 0.18, f"range to {hi / US_PER_MS:,.1f} ms →", ha="right",
                            va="bottom", fontsize=7.2, color=INK_MUTED)
                ax.plot(min(med, xmax), row, marker=">" if med > xmax else "o", markersize=8,
                        markerfacecolor=face, markeredgecolor=edgecol, markeredgewidth=1.6,
                        linestyle="none", zorder=3, clip_on=False)
                if beyond is not None and med > xmax:
                    ax.text(xmax, row - 0.18, f"{med / US_PER_MS:,.1f} ms →", ha="right",
                            va="bottom", fontsize=7.6, color=INK)
        ax.set_yticks(range(len(edges)))
        ax.set_yticklabels([EDGES[e].label for e in edges] if ax is axes[0] else [])
        ax.set_ylim(len(edges) - 0.5, -0.5)
        ax.set_title(f"{rate} iteration{'s' if rate != 1 else ''}/s", fontsize=10.5, color=INK, loc="left")
        ax.set_xlim(0, xmax)
        ax.grid(True, axis="x", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.tick_params(colors=INK_MUTED, labelsize=9, length=0)
        ax.tick_params(axis="y", labelsize=9.5, labelcolor=INK)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(AXIS)
        ax.set_xlabel("µs per GET (median)", fontsize=9, color=INK_MUTED)
    def dot(face: str, edge: str | None = None) -> Line2D:
        return Line2D([], [], marker="o", linestyle="none", markersize=7, markerfacecolor=face,
                      markeredgecolor=edge or face, markeredgewidth=1.6)

    tier_legend(
        fig,
        [dot("white", INK), dot(BARE_GREY), edge_colours_handle(edges, dot)],
        [TIER_LEVEL_LABELS[t] for t in (ORIGIN_TIER, "bare", "silent")],
    )
    fig.text(
        0.015, 0.02,
        "Dot = median over valid replicates of each run's per-GET latency (mean of the four GET probes' "
        "medians); thin line = range across replicates. Levels are not paired, so two dots also\n"
        "differ by drift between runs: read the size of each tier's cost from the paired figures "
        "(<rate>_per_s.png), and the spread of the four origin dots as how much the floor moved.",
        fontsize=7.8, color=INK_MUTED, style="italic", va="bottom", linespacing=1.45,
    )
    return save(fig, out)


TIER_BARS = [
    (ORIGIN_TIER, "origin directly"),
    ("bare", "bare proxy (WADM off)"),
    ("silent", "proxy + WADM"),
]


def plot_tier_comparison(docs, stats, rates: list[int], out: Path) -> Path:
    """Origin vs bare proxy vs proxy + WADM, absolute, every edge and rate on one shared axis."""
    edges = list(docs)
    fig, axes = plt.subplots(1, len(rates), figsize=(4.6 * len(rates) + 0.8, 6.2), sharey=True,
                             squeeze=False)
    axes = axes[0]
    fig.subplots_adjust(left=0.07, right=0.99, top=1 - 1.45 / fig.get_figheight(),
                        bottom=1.55 / fig.get_figheight(), wspace=0.06)
    fig.text(0.012, 1 - 0.3 / fig.get_figheight(), "Origin vs bare proxy vs proxy + WADM",
             fontsize=14, fontweight="bold", color=INK, va="top")
    fig.text(0.012, 1 - 0.62 / fig.get_figheight(),
             "Median latency of a GET request at each offered rate; three tiers measured in the same "
             "replicates for every edge",
             fontsize=10, color=INK_MUTED, va="top")
    width = 0.27
    top = 0.0
    for ax, rate in zip(axes, rates):
        for group, edge in enumerate(edges):
            colour = COLORS[EDGES[edge].label]
            levels = stats[rate][edge]["levels"]
            for i, (tier, _) in enumerate(TIER_BARS):
                lvl = levels.get(tier) or {}
                if not lvl.get("n"):
                    continue
                x = group + (i - 1) * width
                med, lo, hi = (lvl[k] * US_PER_MS for k in ("median", "min", "max"))
                face, line = {ORIGIN_TIER: ("white", INK), "bare": (BARE_GREY, BARE_GREY)}.get(
                    tier, (colour, colour))
                # A 2-px surface gap between neighbours rather than borders between them.
                ax.bar(x, med, width=width * 0.9, color=face, edgecolor=line, linewidth=1.3, zorder=2)
                ax.plot([x, x], [lo, hi], color=INK, linewidth=0.9, alpha=0.6, zorder=3)
                ax.text(x, hi + 12, f"{med:,.0f}", ha="center", va="bottom", fontsize=6.8,
                        color=INK, rotation=90)
                top = max(top, hi)
        ax.set_xticks(range(len(edges)))
        ax.set_xticklabels([EDGES[e].label.replace(" (Envoy)", "\n(Envoy)") for e in edges],
                           fontsize=8.6, color=INK)
        ax.set_title(f"{rate} iteration{'s' if rate != 1 else ''}/s (≈{rate * 9} req/s)",
                     fontsize=10.5, color=INK, loc="left")
        ax.grid(True, axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.tick_params(colors=INK_MUTED, labelsize=8.5, length=0)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(AXIS)
    axes[0].set_ylabel("µs per GET (median)", fontsize=9.5, color=INK_MUTED)
    axes[0].set_ylim(0, top * 1.16)
    tier_legend(
        fig,
        [Patch(facecolor="white", edgecolor=INK), Patch(facecolor=BARE_GREY, edgecolor=BARE_GREY),
         edge_colours_handle(edges, lambda c: Patch(facecolor=c, edgecolor=c))],
        [label for _, label in TIER_BARS],
    )
    fig.text(
        0.012, 0.02,
        "Bar = median over 5 replicates of each run's latency per GET (mean of the four GET probes' "
        "medians); thin line = range across replicates; label = the bar's value in µs. 'WADM off' is the\n"
        "edge's WADM-free configuration (WADM not loaded); 'WADM' is the deployed build without the "
        "benchmark's timing lines. The gap between two bars is not itself a paired statistic: the paired\n"
        "differences, with their replicate ranges, are in <rate>_per_s.png and origin_floor.md. "
        "SQLi POSTs excluded (the origin has no trap).",
        fontsize=7.6, color=INK_MUTED, style="italic", va="bottom", linespacing=1.45,
    )
    return save(fig, out)


def level_cell(level: dict[str, Any]) -> str:
    if not level.get("n"):
        return "—"
    return (f"{level['median'] * US_PER_MS:,.0f} "
            f"[{level['min'] * US_PER_MS:,.0f}..{level['max'] * US_PER_MS:,.0f}]")


# ── Tables ───────────────────────────────────────────────────────────────────────────────────

def us(delta: dict[str, Any]) -> float | None:
    return None if not delta.get("n") else round(delta["median"] * US_PER_MS, 1)


def table_rows(docs, stats, rates: list[int]) -> list[dict[str, Any]]:
    rows = []
    for rate in rates:
        for edge in docs:
            s = stats[rate][edge]
            origin = s["levels"].get(ORIGIN_TIER) or {}
            for scope, metrics in [("per_get", [None]), ("per_probe", GET_METRICS)]:
                for metric in metrics:
                    for name, low, high in [*ABOVE_FLOOR, WADM_ONLY]:
                        c = s["comparisons"][name]
                        d = c["per_get"] if metric is None else c["per_probe"][metric]
                        rows.append({
                            "edge": EDGES[edge].label,
                            "rate_iter_per_s": rate,
                            "scope": scope,
                            "probe": metric.removesuffix("_duration") if metric else "mean of 4 GETs",
                            "comparison": f"{high} - {low}",
                            "replicates": d.get("n"),
                            "median_us": us(d),
                            "min_us": None if not d.get("n") else round(d["min"] * US_PER_MS, 1),
                            "max_us": None if not d.get("n") else round(d["max"] * US_PER_MS, 1),
                            "sign_agreement": d.get("sign_agreement"),
                            "resolved": bool(d.get("significant")),
                            "pct_of_low": (
                                round(c["per_get_pct"]["median"], 1)
                                if metric is None and c["per_get_pct"].get("n") else None
                            ),
                            "origin_level_us": (
                                round(origin["median"] * US_PER_MS, 1) if origin.get("n") else None
                            ),
                            "wadm_share_pct": (
                                round(s["wadm_share_pct"]["median"], 1)
                                if metric is None and s["wadm_share_pct"].get("n") else None
                            ),
                        })
    return rows


def fmt(delta: dict[str, Any], pct: dict[str, Any] | None = None) -> str:
    if not delta.get("n"):
        return "—"
    text = f"{delta['median'] * US_PER_MS:+,.0f}"
    if pct and pct.get("n"):
        text += f" ({pct['median']:+.0f}%)"
    text += f" [{delta['min'] * US_PER_MS:+,.0f}..{delta['max'] * US_PER_MS:+,.0f}]"
    if not delta.get("ci_reliable"):
        text += " †"
    elif not delta.get("significant"):
        text += " ‡"
    return text


def us_signed(delta: dict[str, Any]) -> str:
    return "—" if not delta.get("n") else f"{delta['median'] * US_PER_MS:+,.0f}"


def share_cell(share: dict[str, Any]) -> str:
    if not share.get("n"):
        return "—"
    return f"{share['median']:.0f} [{share['min']:.0f}..{share['max']:.0f}]"


def write_tables(docs, stats, rates: list[int], results_dir: Path) -> list[Path]:
    rows = table_rows(docs, stats, rates)
    csv_path = results_dir / "origin_floor.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    lines = [
        "# Latency above the origin floor (paired dataset)",
        "",
        "Generated by `benchmarks/plot_origin_floor.py` from `paired_<edge>.json`. The origin tier "
        "sends the same probes as every other tier straight to the origin, with the edge idle, inside "
        "the same replicate, so each difference below is paired: measured minutes apart and summarised "
        "as the median over replicates, with the replicate range in brackets. *WADM* is the deployed "
        "build (no benchmark timing lines). µs per GET is the mean of the four GET probes' median "
        "latencies; the SQLi POSTs are excluded because the origin has no trap. ‡ = the replicates "
        "disagree in sign, so the difference is below what this harness resolves; † = too few "
        "replicates (under 3) to judge, as in a smoke run.",
        "",
        "## Origin vs bare proxy vs proxy + WADM",
        "",
        "Absolute median latency per GET for each tier, in µs, as the median over replicates with the "
        "range in brackets (`plots/origin_floor/tier_comparison.png`). *Bare proxy* is the edge's "
        "WADM-free configuration. The columns on the right are the paired differences from the next "
        "section, so the comparison can be read without subtracting the levels, which are not paired.",
        "",
        "| Edge | Rate (iter/s) | Origin directly | Bare proxy (WADM off) | Proxy + WADM "
        "| Bare − origin | WADM − origin |",
        "|---|---|---|---|---|---|---|",
    ]
    for rate in rates:
        for edge in docs:
            s = stats[rate][edge]
            lv, c = s["levels"], s["comparisons"]
            lines.append(
                f"| {EDGES[edge].label} | {rate} | {level_cell(lv.get(ORIGIN_TIER) or {})}"
                f" | {level_cell(lv.get('bare') or {})} | {level_cell(lv.get('silent') or {})}"
                f" | {us_signed(c['bare_minus_origin']['per_get'])}"
                f" | {us_signed(c['silent_minus_origin']['per_get'])} |"
            )
    lines += [
        "",
        "## Per GET",
        "",
        "| Edge | Rate (iter/s) | Replicates | Origin itself (µs) | Bare proxy − origin, µs (% of origin) "
        "| Proxy + WADM − origin, µs (% of origin) | of which WADM (proxy + WADM − bare), µs (% of bare) "
        "| WADM share of the added latency, % |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for rate in rates:
        for edge in docs:
            s = stats[rate][edge]
            c = s["comparisons"]
            origin = s["levels"].get(ORIGIN_TIER) or {}
            reps = min(v["replicates_paired"] for v in c.values())
            origin_s = f"{origin['median'] * US_PER_MS:,.0f}" if origin.get("n") else "—"
            lines.append(
                f"| {EDGES[edge].label} | {rate} | {reps} | {origin_s}"
                f" | {fmt(c['bare_minus_origin']['per_get'], c['bare_minus_origin']['per_get_pct'])}"
                f" | {fmt(c['silent_minus_origin']['per_get'], c['silent_minus_origin']['per_get_pct'])}"
                f" | {fmt(c['silent_minus_bare']['per_get'], c['silent_minus_bare']['per_get_pct'])}"
                f" | {share_cell(s['wadm_share_pct'])} |"
            )
    lines += [
        "",
        "The three differences are each their own median of paired values, so the WADM column need not "
        "equal the gap between the other two. *WADM share* is (proxy + WADM − bare) ÷ (proxy + WADM − "
        "origin) taken within each replicate, then its median and range: how much of the latency the "
        "deployed stack adds above the origin is WADM rather than the proxy. *Origin itself* is "
        "descriptive (the median of each replicate's level, not a paired value).",
        "",
        "## Per GET probe (µs added at the median)",
        "",
        "| Edge | Rate | Probe | Bare proxy − origin | Proxy + WADM − origin | of which WADM |",
        "|---|---|---|---|---|---|",
    ]
    for rate in rates:
        for edge in docs:
            c = stats[rate][edge]["comparisons"]
            for metric, label in GET_PROBES:
                lines.append(
                    f"| {EDGES[edge].label} | {rate} | {label.replace(chr(10), ' ')} | "
                    f"{fmt(c['bare_minus_origin']['per_probe'][metric])} | "
                    f"{fmt(c['silent_minus_origin']['per_probe'][metric])} | "
                    f"{fmt(c['silent_minus_bare']['per_probe'][metric])} |"
                )
    md_path = results_dir / "origin_floor.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [csv_path, md_path]


def main() -> int:
    results_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "results"
    docs = load_docs(results_dir)
    if not docs:
        print("Nothing to plot: no paired_<edge>.json with origin, bare and silent tiers.")
        return 1
    rates = rates_of(docs)
    stats = {rate: {edge: floor_stats(doc, rate) for edge, doc in docs.items()} for rate in rates}
    out_dir = results_dir / "plots" / "origin_floor"
    written = [
        plot_above_floor(docs, stats[rate], rate, out_dir / f"{rate}_per_s.png") for rate in rates
    ]
    written.append(plot_tier_comparison(docs, stats, rates, out_dir / "tier_comparison.png"))
    written.append(plot_levels(docs, stats, rates, out_dir / "latency_levels.png"))
    written += write_tables(docs, stats, rates, results_dir)
    for path in written:
        print(f"  wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
