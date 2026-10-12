#!/usr/bin/env python3
"""
One measure per figure, one figure set per offered rate: the paired dataset split into views that
each answer a single question.

The combined figures (paired_internal_timers.png, paired_latency_added.png) put every kind, phase,
edge and rate on one canvas, so no single comparison can be read at a glance. Here each figure
compares the four edges on exactly one measure at exactly one rate:

    results/plots/by_rate/<rate>_per_s/
        0_inject_vs_detect.png    inject vs detect per edge, averaged over the five token kinds (µs)
        1_injection_timers.png    in-edge inject region per token kind (µs)
        2_detection_timers.png    in-edge detect region per token kind (µs)
        3_sqli_scan_timers.png    in-edge SQLi signature scan per arm: hit/miss x plain/encoded (µs)
        4_cpu_per_request.png     edge CPU per request, bare vs WADM as deployed (µs)
        5_latency_added_get.png   end-to-end latency WADM adds to each GET probe (µs)
        6_latency_added_sqli.png  end-to-end latency WADM adds to each SQLi POST probe (µs)

SQLi is kept off the detection figure: it is the one detect-only feature, its scan is 3-20x longer
than any other detect region, and on the latency plane its sign is reversed (the trap answers
without the origin round-trip), so sharing an axis with the GET kinds flattens them.

Every bar is a median over replicates and every whisker the range across replicates, so a long
whisker means the replicates disagreed, not that the requests were spread. Timer bars are the
5%-trimmed mean pooled over valid instrumented replicates (the edges log whole microseconds, so a
median of 1-5 µs regions would be an integer); their whiskers are the range of the same statistic
computed per replicate.

Reads benchmarks/results/paired_<edge>.json through analyze_paired, so the numbers match the
console report and paired_headline.md.

Usage:
    python3 benchmarks/plot_by_rate.py [results_dir]
"""

from __future__ import annotations

import sys
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.colors
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

from analyze_paired import analyse_edge, load_paired, valid_cells
from edge_specs import EDGE_KEYS, EDGES
from plot_common import COLORS, INK, INK_MUTED
from wadm_timings import summarize

US_PER_MS = 1000.0
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BARE_GREY = "#c3c2b7"

GET_KINDS = [
    ("html_comments", "html_comments\nHTML comment in body"),
    ("http_headers", "http_headers\nresponse header"),
    ("cookies", "cookies\nSet-Cookie bait"),
    ("decoy_paths", "decoy_paths\nhidden link / trap URL"),
    ("form_fields", "form_fields\nhidden form input"),
]
SQLI_ARMS = [
    ("sql_injection", "signature hit\nplain body"),
    ("sql_injection_encoded", "signature hit\n%-encoded body"),
    ("sql_injection_miss", "no match\nplain body"),
    ("sql_injection_miss_encoded", "no match\n%-encoded body"),
]
GET_PROBES = [
    ("inject_get_duration", "GET /\ninjection only"),
    ("detect_query_duration", "GET /api/login?password=…\n+ html_comments hit"),
    ("token_tamper_duration", "GET /login.html?…\n+ header, cookie, form hits"),
    ("token_decoy_duration", "GET /api/v1/debug\n+ decoy_paths hit"),
]
SQLI_PROBES = [
    ("sqli_hit_duration", "POST /api/login\nsignature hit, plain"),
    ("sqli_hit_enc_duration", "POST /api/login\nsignature hit, %-encoded"),
    ("sqli_miss_duration", "POST /api/login\nno match, plain"),
    ("sqli_miss_enc_duration", "POST /api/login\nno match, %-encoded"),
]

# One iteration of the reportable `all` mix: one unrecorded priming GET, four GET probes and four
# SQLi POSTs (test.js). Used only to turn the offered iteration rate into a request rate in titles.
REQUESTS_PER_ITERATION = 9


@dataclass
class Bar:
    value: float
    low: float | None = None
    high: float | None = None
    resolved: bool = True


# A bar source returns the bar for (edge, category), or a short reason string when there is none.
BarFn = Callable[[str, str], "Bar | str"]


# ── Data extraction ──────────────────────────────────────────────────────────────────────────

def load_all(results_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    docs, summaries = {}, {}
    for edge in EDGE_KEYS:
        doc = load_paired(results_dir, edge)
        if doc:
            docs[edge], summaries[edge] = doc, analyse_edge(doc)
        else:
            print(f"  ! no paired_{edge}.json — skipping {EDGES[edge].label}")
    return docs, summaries


def rate_block(summaries: dict[str, Any], edge: str, rate: int) -> dict[str, Any]:
    return summaries[edge]["rates"].get(str(rate)) or {}


def timer_bar(docs, summaries, rate: int, phase: str) -> BarFn:
    def get(edge: str, kind: str) -> Bar | str:
        pooled = ((rate_block(summaries, edge, rate).get("internal_us") or {}).get(kind) or {}).get(phase)
        if not pooled:
            return "no valid instrumented replicate"
        per_rep = []
        for cell in valid_cells(docs[edge], "wadm", rate):
            samples = ((cell.get("tokens_raw") or {}).get(kind) or {}).get(f"{phase}_us") or []
            if samples:
                per_rep.append(summarize(samples).to_json()["trimmed_mean_us"])
        return Bar(pooled["trimmed_mean_us"], min(per_rep, default=None), max(per_rep, default=None))
    return get


def delta_bar(delta: dict[str, Any] | None, scale: float) -> Bar | str:
    if not delta or not delta.get("n"):
        return "no valid pair"
    return Bar(delta["median"] * scale, delta["min"] * scale, delta["max"] * scale,
               bool(delta.get("significant")))


def latency_bar(summaries, rate: int) -> BarFn:
    def get(edge: str, probe: str) -> Bar | str:
        return delta_bar((rate_block(summaries, edge, rate).get("latency_delta_ms") or {}).get(probe),
                         US_PER_MS)
    return get


def cpu_bar(summaries, rate: int) -> BarFn:
    def get(edge: str, tier: str) -> Bar | str:
        stats = (rate_block(summaries, edge, rate).get("cpu_absolute_us_per_request") or {}).get(tier)
        if not stats or not stats.get("n"):
            return "no valid replicate"
        return Bar(stats["median"], stats["min"], stats["max"])
    return get


def replicate_note(docs, summaries, edges: list[str], rate: int, instrumented: bool = False) -> str:
    """Names every edge whose cell lost replicates or crashed, so a short whisker is never read as
    a clean n=5. `instrumented` counts valid timing-tier runs instead of valid bare/WADM pairs."""
    notes = []
    for edge in edges:
        planned = summaries[edge]["metadata"].get("replicates")
        if instrumented:
            kept, what = len(valid_cells(docs[edge], "wadm", rate)), "instrumented runs"
        else:
            kept, what = rate_block(summaries, edge, rate).get("replicates_paired"), "replicate pairs"
        if kept is not None and planned and kept < planned:
            notes.append(f"{EDGES[edge].label}: {kept}/{planned} {what} passed the validity gate")
        wadm_runs = [c for c in docs[edge]["cells"]
                     if c["rate"] == rate and c["tier"] in ("silent", "wadm")]
        crashed = sum(1 for c in wadm_runs if c["validity"].get("crash_count"))
        if crashed:
            notes.append(f"{EDGES[edge].label}: workers segfaulted in {crashed}/{len(wadm_runs)} WADM "
                         "runs, so its figures include crash recovery (see apache_remediation.png)")
    return ". ".join(notes)


# ── Drawing ──────────────────────────────────────────────────────────────────────────────────

def rate_label(rate: int) -> str:
    return (f"{rate} iteration{'s' if rate != 1 else ''}/s offered "
            f"(≈{rate * REQUESTS_PER_ITERATION} requests/s)")


def new_figure(n_rows: int, title: str, subtitle: str):
    fig, ax = plt.subplots(figsize=(10, 1.35 * n_rows + 2.2))
    fig.subplots_adjust(left=0.24, right=0.9, top=1 - 1.45 / fig.get_figheight(),
                        bottom=1.05 / fig.get_figheight())
    fig.text(0.015, 1 - 0.3 / fig.get_figheight(), title, fontsize=14, fontweight="bold",
             color=INK, va="top")
    fig.text(0.015, 1 - 0.62 / fig.get_figheight(), subtitle, fontsize=10, color=INK_MUTED, va="top")
    return fig, ax


def style(ax, xlabel: str) -> None:
    ax.set_xlabel(xlabel, fontsize=9.5, color=INK_MUTED)
    ax.grid(True, axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK_MUTED, labelsize=9, length=0)
    ax.tick_params(axis="y", labelsize=9.5, labelcolor=INK)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)


def footnote(fig, text: str) -> None:
    lines = [w for para in text.split("\n") for w in textwrap.wrap(para, 165)]
    # The x-axis label sits below the plot, so the bottom margin grows with the footnote.
    fig.subplots_adjust(bottom=(0.7 + 0.16 * len(lines)) / fig.get_figheight())
    fig.text(0.015, 0.012, "\n".join(lines), fontsize=7.8, color=INK_MUTED, style="italic",
             va="bottom", linespacing=1.45)


def grouped_bars(ax, categories: list[tuple[str, str]], series: list[tuple[str, str, str]],
                 get: BarFn, unit: str, clip: float | None = None, decimals: int = 1) -> None:
    """Horizontal bars, one group per category, one bar per series in fixed order.

    `series` is (key, label, colour); `get(series_key, category_key)` supplies each bar. A hollow
    bar means the replicate range spans zero. Bars beyond `clip` stop at the axis edge with an
    arrow and their true value, so one outlier edge does not flatten the other three.
    """
    n = len(series)
    height = 0.8 / n
    values = []
    negative_bar = False
    for row, (cat, _) in enumerate(categories):
        for i, (key, _, colour) in enumerate(series):
            y = row + (i - (n - 1) / 2) * height
            bar = get(key, cat)
            if isinstance(bar, str):
                ax.text(0, y, f"  {bar}", va="center", ha="left", fontsize=7.5, color=INK_MUTED,
                        style="italic")
                continue
            shown = bar.value if clip is None else max(min(bar.value, clip), -clip)
            ax.barh(y, shown, height=height * 0.86, color=colour if bar.resolved else "white",
                    edgecolor=colour, linewidth=1.4)
            if bar.low is not None and bar.high is not None:
                lo, hi = bar.low, bar.high
                if clip is not None:
                    lo, hi = max(min(lo, clip), -clip), max(min(hi, clip), -clip)
                ax.plot([lo, hi], [y, y], color=INK, linewidth=0.9, alpha=0.65,
                        solid_capstyle="butt")
            clipped = clip is not None and abs(bar.value) > clip
            if clipped:
                ax.annotate("", xy=(shown, y), xytext=(shown * 0.94, y),
                            arrowprops={"arrowstyle": "-|>", "color": colour, "lw": 1.4})
            edge = max(shown, bar.high if bar.high is not None and not clipped else shown) \
                if bar.value >= 0 else min(shown, bar.low if bar.low is not None and not clipped else shown)
            text = f"{bar.value:,.{decimals}f} {unit}" + (" (clipped)" if clipped else "")
            ax.text(edge, y, f"  {text}  " , va="center", ha="left" if bar.value >= 0 else "right",
                    fontsize=7.8, color=INK)
            values.extend(v for v in (shown, bar.low, bar.high) if v is not None)
            negative_bar |= bar.value < 0

    ax.set_yticks(range(len(categories)))
    ax.set_yticklabels([label for _, label in categories])
    ax.set_ylim(len(categories) - 0.5, -0.5)
    lo, hi = min([0.0, *values]), max([0.0, *values])
    if clip is not None:
        lo, hi = max(lo, -clip), min(hi, clip)
    span = hi - lo or 1.0
    # Head-room for the value labels, which sit outside the bar ends.
    # Labels only sit left of zero for negative bars; a whisker alone dipping below zero needs no room.
    ax.set_xlim(lo - (0.22 * span if negative_bar else 0.03 * span), hi + 0.22 * span)
    ax.axvline(0, color=INK_MUTED, linewidth=0.9)
    for row in range(1, len(categories)):
        ax.axhline(row - 0.5, color=GRID, linewidth=0.8)


def edge_series(edges: list[str]) -> list[tuple[str, str, str]]:
    return [(e, EDGES[e].label, COLORS[EDGES[e].label]) for e in edges]


def edge_legend(fig, edges: list[str], extra: list[Patch] | None = None) -> None:
    handles = [Patch(facecolor=COLORS[EDGES[e].label], edgecolor=COLORS[EDGES[e].label],
                     label=EDGES[e].label) for e in edges] + (extra or [])
    fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.01, 1 - 0.85 / fig.get_figheight()),
               ncol=len(handles), frameon=False, fontsize=9, handlelength=1.2, columnspacing=1.2)


HOLLOW = Patch(facecolor="white", edgecolor=INK_MUTED, label="range spans zero")


def save(fig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, facecolor="white")
    plt.close(fig)
    return path


# ── Figures ──────────────────────────────────────────────────────────────────────────────────

TIMER_NOTE = (
    "Bar = 5%-trimmed mean of every sample pooled over valid instrumented replicates; whisker = range "
    "of that statistic per replicate. Edges log whole µs, so the trimmed mean is used to recover "
    "sub-µs resolution. WASM truncates each delta to whole µs and reads ≈0.5 µs low."
)


def plot_timers(docs, summaries, edges, rate, phase, out: Path) -> Path:
    what = {"inject": ("Injection", "time to plant one token in a response"),
            "detect": ("Detection", "time to check one request for that token's trigger")}[phase]
    fig, ax = new_figure(len(GET_KINDS), f"{what[0]} cost per token kind",
                         f"In-edge timer, {what[1]} — {rate_label(rate)}")
    grouped_bars(ax, GET_KINDS, edge_series(edges), lambda e, k: timer_bar(docs, summaries, rate, phase)(e, k),
                 "µs")
    style(ax, "microseconds inside the timed region (lower is cheaper)")
    edge_legend(fig, edges)
    note = replicate_note(docs, summaries, edges, rate, instrumented=True)
    footnote(fig, TIMER_NOTE + (f"\n{note}." if note else ""))
    return save(fig, out)


def merged_timer(docs, summaries, edge: str, rate: int, phase: str) -> Bar | str:
    """One phase's cost averaged over the five GET token kinds, each kind weighted equally.

    Equal weights rather than pooling the raw samples: test.js injects form_fields on one GET in five
    but the other kinds on every GET, so a pooled mean would mostly describe whichever kinds the
    probe mix happens to repeat. Each kind's 5%-trimmed mean is taken first, for the reasons in
    plot_by_rate's docstring, so the result reads as "one token operation on this edge".
    """
    kinds = [k for k, _ in GET_KINDS]
    pooled = (rate_block(summaries, edge, rate).get("internal_us") or {})
    means = [((pooled.get(k) or {}).get(phase) or {}).get("trimmed_mean_us") for k in kinds]
    if any(m is None for m in means):
        return "no valid instrumented run"
    per_rep = []
    for cell in valid_cells(docs[edge], "wadm", rate):
        raw = cell.get("tokens_raw") or {}
        rep = [summarize(s).to_json()["trimmed_mean_us"]
               for s in ((raw.get(k) or {}).get(f"{phase}_us") for k in kinds) if s]
        if len(rep) == len(kinds):
            per_rep.append(sum(rep) / len(rep))
    return Bar(sum(means) / len(means), min(per_rep, default=None), max(per_rep, default=None))


def timer_axis_max(docs, summaries, edges: list[str], rates: list[int]) -> float:
    """Largest bar or replicate bound over every rate, so the per-rate figures share one scale."""
    bars = [merged_timer(docs, summaries, e, r, ph)
            for r in rates for e in edges for ph in ("inject", "detect")]
    return max((max(b.value, b.high or 0) for b in bars if not isinstance(b, str)), default=1.0)


def tint(hex_colour: str, amount: float) -> tuple[float, float, float]:
    r, g, b = matplotlib.colors.to_rgb(hex_colour)
    return (r + (1 - r) * amount, g + (1 - g) * amount, b + (1 - b) * amount)


def plot_inject_vs_detect(docs, summaries, edges, rate, out: Path, xmax: float) -> Path:
    phases = [("inject", "injection"), ("detect", "detection")]
    fig, ax = new_figure(len(edges), "Injection vs detection cost per edge",
                         f"In-edge timers, averaged over all five token kinds; same scale at every "
                         f"rate — {rate_label(rate)}")
    height = 0.8 / len(phases)
    for row, edge in enumerate(edges):
        colour = COLORS[EDGES[edge].label]
        for i, (phase, label) in enumerate(phases):
            y = row + (i - (len(phases) - 1) / 2) * height
            bar = merged_timer(docs, summaries, edge, rate, phase)
            if isinstance(bar, str):
                ax.text(0, y, f"  {label}: {bar}", va="center", fontsize=8, color=INK_MUTED,
                        style="italic")
                continue
            # Detection takes a light tint of the edge's own hue, so edge identity survives and the
            # phase is still told apart by the direct label, never by colour alone.
            ax.barh(y, bar.value, height=height * 0.86,
                    color=colour if phase == "inject" else tint(colour, 0.55))
            if bar.low is not None:
                ax.plot([bar.low, bar.high], [y, y], color=INK, linewidth=0.9, alpha=0.65)
            ax.text(max(bar.value, bar.high or bar.value), y, f"  {label}: {bar.value:.2f} µs",
                    va="center", fontsize=8.5, color=INK)
    ax.set_yticks(range(len(edges)))
    ax.set_yticklabels([EDGES[e].label for e in edges])
    ax.set_ylim(len(edges) - 0.5, -0.5)
    for row in range(1, len(edges)):
        ax.axhline(row - 0.5, color=GRID, linewidth=0.8)
    ax.set_xlim(0, xmax * 1.35)
    style(ax, "microseconds per token operation (lower is cheaper)")
    fig.subplots_adjust(left=0.16)
    note = replicate_note(docs, summaries, edges, rate, instrumented=True)
    footnote(fig, "Bar = mean of the five token kinds' 5%-trimmed means (html_comments, http_headers, "
                  "cookies, decoy_paths, form_fields), each kind weighted equally; SQLi is excluded "
                  "(see 3_sqli_scan_timers). Whisker = range of the same figure across replicate runs. "
                  "Trimmed rather than plain mean because readings are whole µs and a few are host "
                  "preemption up to ~2 ms. WASM truncates each reading and sits ≈0.5 µs low."
             + (f"\n{note}." if note else ""))
    return save(fig, out)


def plot_sqli_timers(docs, summaries, edges, rate, out: Path) -> Path:
    fig, ax = new_figure(len(SQLI_ARMS), "SQL-injection trap: signature scan cost",
                         f"In-edge timer around the scan of a POST /api/login body — {rate_label(rate)}")
    grouped_bars(ax, SQLI_ARMS, edge_series(edges),
                 lambda e, k: timer_bar(docs, summaries, rate, "detect")(e, k), "µs")
    style(ax, "microseconds inside the signature scan (lower is cheaper)")
    edge_legend(fig, edges)
    note = replicate_note(docs, summaries, edges, rate, instrumented=True)
    footnote(fig, "A hit stops at the first matching signature; a miss tries every signature, so "
                  "misses are the worst case. %-encoded bodies are decoded before scanning.\n"
             + TIMER_NOTE + (f"\n{note}." if note else ""))
    return save(fig, out)


def plot_cpu(docs, summaries, edges, rate, out: Path) -> Path:
    fig, ax = new_figure(len(edges), "Edge CPU per request: without vs with WADM",
                         f"Container CPU ÷ requests served; WADM as deployed (no timing-log lines) — {rate_label(rate)}")
    get = cpu_bar(summaries, rate)
    categories = [(e, EDGES[e].label) for e in edges]
    tiers = [("bare", "without WADM"), ("silent", "with WADM")]

    def bar_for(tier: str, edge: str) -> Bar | str:
        return get(edge, tier)

    n = len(tiers)
    height = 0.8 / n
    for row, edge in enumerate(edges):
        colour = COLORS[EDGES[edge].label]
        for i, (tier, label) in enumerate(tiers):
            y = row + (i - (n - 1) / 2) * height
            bar = bar_for(tier, edge)
            if isinstance(bar, str):
                ax.text(0, y, f"  {bar}", va="center", fontsize=7.5, color=INK_MUTED, style="italic")
                continue
            ax.barh(y, bar.value, height=height * 0.86, color=BARE_GREY if tier == "bare" else colour)
            ax.plot([bar.low, bar.high], [y, y], color=INK, linewidth=0.9, alpha=0.65)
            ax.text(max(bar.value, bar.high), y, f"  {label}: {bar.value:,.0f} µs", va="center",
                    fontsize=7.8, color=INK)
        delta = rate_block(summaries, edge, rate).get("cpu_delta_us_per_request") or {}
        if delta.get("n"):
            sign = "+" if delta["median"] >= 0 else "−"
            ax.text(1.0, row, f"WADM adds\n{sign}{abs(delta['median']):,.0f} µs/req",
                    transform=ax.get_yaxis_transform(), ha="left", va="center", fontsize=8.5,
                    color=INK, fontweight="bold")
    ax.set_yticks(range(len(edges)))
    ax.set_yticklabels([label for _, label in categories])
    ax.set_ylim(len(edges) - 0.5, -0.5)
    for row in range(1, len(edges)):
        ax.axhline(row - 0.5, color=GRID, linewidth=0.8)
    ax.set_xlim(0, ax.get_xlim()[1] * 1.3)
    style(ax, "CPU microseconds per request (lower is cheaper)")
    fig.subplots_adjust(right=0.86)
    note = replicate_note(docs, summaries, edges, rate)
    footnote(fig, "Bar = median over replicates, whisker = range. Includes the edge's idle CPU spread "
                  "over the requests, which is why low rates read higher; the bold figure is the "
                  "paired per-replicate difference (WADM − bare), in which idle cancels." + (f"\n{note}." if note else ""))
    return save(fig, out)


def latency_clip(summaries, edges, rate, probes) -> float | None:
    """Clip only when one edge is more than 4x the next largest, so its bar does not flatten the rest."""
    peaks = sorted(
        (max((abs(((rate_block(summaries, e, rate).get("latency_delta_ms") or {}).get(p) or {})
                  .get("median") or 0) for p, _ in probes), default=0) * US_PER_MS for e in edges),
        reverse=True,
    )
    if len(peaks) > 1 and peaks[1] > 0 and peaks[0] > 4 * peaks[1]:
        return peaks[1] * 2.2
    return None


LATENCY_NOTE = (
    "Bar = median over replicates of (WADM as deployed − bare) median latency, each pair measured "
    "minutes apart; whisker = range of those paired differences. Hollow = range spans zero. "
    "A bar marked (clipped) runs off the axis; its true value is printed."
)


def plot_latency(docs, summaries, edges, rate, probes, title, subtitle, note, out: Path) -> Path:
    fig, ax = new_figure(len(probes), title, f"{subtitle} — {rate_label(rate)}")
    clip = latency_clip(summaries, edges, rate, probes)
    grouped_bars(ax, probes, edge_series(edges), latency_bar(summaries, rate), "µs", clip=clip,
                 decimals=0)
    style(ax, "microseconds added to the median request (negative = WADM made it faster)")
    edge_legend(fig, edges, [HOLLOW])
    replicates = replicate_note(docs, summaries, edges, rate)
    footnote(fig, LATENCY_NOTE + (f" {note}" if note else "")
             + (f"\n{replicates}." if replicates else ""))
    return save(fig, out)


def plot_rate(docs, summaries, edges, rate, out_dir: Path, timer_max: float) -> list[Path]:
    d = out_dir / f"{rate}_per_s"
    return [
        plot_inject_vs_detect(docs, summaries, edges, rate, d / "0_inject_vs_detect.png", timer_max),
        plot_timers(docs, summaries, edges, rate, "inject", d / "1_injection_timers.png"),
        plot_timers(docs, summaries, edges, rate, "detect", d / "2_detection_timers.png"),
        plot_sqli_timers(docs, summaries, edges, rate, d / "3_sqli_scan_timers.png"),
        plot_cpu(docs, summaries, edges, rate, d / "4_cpu_per_request.png"),
        plot_latency(
            docs, summaries, edges, rate, GET_PROBES, "Latency WADM adds to each GET",
            "End-to-end, paired against the same edge without WADM", "", d / "5_latency_added_get.png",
        ),
        plot_latency(
            docs, summaries, edges, rate, SQLI_PROBES, "Latency WADM adds to each SQLi POST",
            "End-to-end, paired against the same edge without WADM",
            "\nWithout WADM the origin answers the POST; with WADM the trap does. OpenResty, Envoy+Lua "
            "and Apache answer from the request phase and skip the origin round-trip (negative); "
            "Envoy+WASM cannot, so it still pays that hop.",
            d / "6_latency_added_sqli.png",
        ),
    ]


def main() -> int:
    results_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "results"
    docs, summaries = load_all(results_dir)
    if not summaries:
        print("No paired_<edge>.json files found.")
        return 1
    edges = list(summaries)
    rates = sorted({int(r) for s in summaries.values() for r in s["rates"]})
    out_dir = results_dir / "plots" / "by_rate"
    timer_max = timer_axis_max(docs, summaries, edges, rates)
    for rate in rates:
        for path in plot_rate(docs, summaries, edges, rate, out_dir, timer_max):
            print(f"  wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
