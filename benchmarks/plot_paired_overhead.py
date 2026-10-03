#!/usr/bin/env python3
"""
What WADM adds, drawn from the PAIRED dataset: latency per probe, the latency shift by quantile, the
internal timers, and a headline table.

Replaces the figures the legacy plotters drew from the unpaired constant-VU files
(`e2e_comparison_*`, `e2e_phase_overhead_*`, `e2e_overhead_scaling`, `edge_comparison_*`,
`token_comparison_*`, `token_scaling_*`, `sqli_*`). Those subtracted two runs taken up to two hours
apart; every number here is a within-replicate difference summarised across replicates.

Figures (results/plots/):

    paired_latency_added.png     forest plot: median latency added per probe, per edge, per rate.
                                 Dot = median of the paired differences, line = their range across
                                 replicates, hollow dot = the range spans zero.
    paired_latency_shift.png     shift function: latency added per GET at p50/p75/p90/p95(/p99).
                                 Shows whether WADM moves the whole distribution or stretches its
                                 tail, which the median alone cannot.
    paired_internal_timers.png   heatmap of the in-edge detect/inject timers, 5%-trimmed mean.

Tables (results/):

    paired_headline.md / .csv    one row per edge and rate: latency added per GET (median and p95,
                                 absolute and relative), CPU added per request (total, user,
                                 system), and the coverage check.

Reads benchmarks/results/paired_<edge>.json and runs the same analysis as analyze_paired.py, so the
figures can never disagree with the console report.

Usage:
    python3 benchmarks/plot_paired_overhead.py [results_dir]
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, LogNorm
from matplotlib.lines import Line2D

from analyze_paired import GET_METRICS, analyse_edge, load_paired
from edge_specs import EDGE_KEYS, EDGES
from plot_common import COLORS, INK, INK_MUTED, _style_frame
from wadm_timings import PHASES

PROBES = [
    ("inject_get_duration", "GET /\ninjection only"),
    ("detect_query_duration", "GET /api/login?password=…\n+ html_comments hit"),
    ("token_tamper_duration", "GET /login.html?…\n+ header, cookie, form hits"),
    ("token_decoy_duration", "GET /api/v1/debug\n+ decoy_paths hit"),
    ("sqli_hit_duration", "POST /api/login\nSQLi hit, plain"),
    ("sqli_hit_enc_duration", "POST /api/login\nSQLi hit, %-encoded"),
    ("sqli_miss_duration", "POST /api/login\nno match, plain"),
    ("sqli_miss_enc_duration", "POST /api/login\nno match, %-encoded"),
]

QUANTILE_LABELS = {"med": "p50", "p75": "p75", "p90": "p90", "p95": "p95", "p99": "p99"}

# Rows of the timer heatmap. sql_injection plants nothing, so its arms have a detect row only.
TIMER_ROWS = [
    (kind, phase)
    for kind in ["html_comments", "http_headers", "cookies", "decoy_paths", "form_fields"]
    for phase in PHASES
] + [
    ("sql_injection", "detect"),
    ("sql_injection_encoded", "detect"),
    ("sql_injection_miss", "detect"),
    ("sql_injection_miss_encoded", "detect"),
]

# Sequential single-hue ramp (blue 100 -> 650 of the validated palette). Magnitude, not identity, so
# one hue light to dark rather than the edges' categorical colours.
SEQUENTIAL_BLUE = LinearSegmentedColormap.from_list(
    "wadm_blue", ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#104281"]
)

US_PER_MS = 1000.0


def load_summaries(results_dir: Path) -> dict[str, dict[str, Any]]:
    out = {}
    for edge in EDGE_KEYS:
        doc = load_paired(results_dir, edge)
        if doc:
            out[edge] = analyse_edge(doc)
        else:
            print(f"  ! no paired_{edge}.json — skipping {EDGES[edge].label}")
    return out


def rates_of(summaries: dict[str, dict[str, Any]]) -> list[int]:
    return sorted({int(r) for s in summaries.values() for r in s["rates"]})


def finish(fig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, facecolor="white")
    plt.close(fig)
    return path


# ── Forest plot ──────────────────────────────────────────────────────────────────────────────

def plot_latency_added(summaries: dict[str, Any], rates: list[int], out_dir: Path) -> Path:
    edges = list(summaries)
    fig, axes = plt.subplots(1, len(rates), figsize=(5.2 * len(rates) + 2.6, 8.4),
                             sharey=True, sharex=True, squeeze=False)
    axes = axes[0]
    spacing = 0.17
    offsets = {e: (i - (len(edges) - 1) / 2) * spacing for i, e in enumerate(edges)}
    # The SQLi group sits below a gap, so the eye reads it as a different kind of request: the
    # trap answers it locally on three edges, which is why its sign flips.
    y_of = {m: -(i + (0.8 if i >= len(GET_METRICS) else 0)) for i, (m, _) in enumerate(PROBES)}

    for ax, rate in zip(axes, rates):
        for edge in edges:
            data = summaries[edge]["rates"].get(str(rate))
            if not data:
                continue
            color = COLORS[EDGES[edge].label]
            for metric, _ in PROBES:
                d = data["latency_delta_ms"].get(metric) or {}
                if d.get("median") is None:
                    continue
                y = y_of[metric] + offsets[edge]
                if d.get("ci_low") is not None:
                    ax.plot([d["ci_low"] * US_PER_MS, d["ci_high"] * US_PER_MS], [y, y],
                            color=color, linewidth=2, solid_capstyle="round", zorder=2)
                resolved = d.get("significant")
                ax.plot(d["median"] * US_PER_MS, y, marker="o", markersize=8, zorder=3,
                        color=color, markerfacecolor=color if resolved else "white",
                        markeredgecolor=color, markeredgewidth=2)
        ax.axvline(0, color=INK_MUTED, linewidth=1, zorder=1)
        ax.axhline(-(len(GET_METRICS) - 0.1), color="#d5d4cf", linewidth=0.8, linestyle=":")
        ax.set_title(f"{rate} iteration{'s' if rate != 1 else ''}/s offered "
                     f"(≈{rate * 9:,} req/s)", fontsize=11, color=INK, loc="left")
        ax.set_xlabel("latency added by WADM (µs, median request)", fontsize=9, color=INK_MUTED)
        _style_frame(ax)
        ax.grid(True, which="major", axis="x", alpha=0.25, linewidth=0.8)
        ax.grid(False, axis="y")

    axes[0].set_yticks([y_of[m] for m, _ in PROBES])
    axes[0].set_yticklabels([label for _, label in PROBES], fontsize=8.5)

    handles = [
        Line2D([], [], color=COLORS[EDGES[e].label], marker="o", markersize=8, linewidth=2,
               label=EDGES[e].label)
        for e in edges
    ] + [
        Line2D([], [], color=INK_MUTED, marker="o", markersize=8, linewidth=0,
               markerfacecolor="white", markeredgewidth=2, label="range spans zero (not resolved)"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=len(handles), frameon=False,
               fontsize=9, bbox_to_anchor=(0.5, 0.075))
    fig.suptitle("Latency WADM adds per request, paired against the same edge without WADM",
                 fontsize=14, fontweight="bold", color=INK, x=0.01, ha="left")
    fig.text(
        0.01, 0.005,
        "Dot = median over replicates of (WADM − bare) median latency, each pair measured minutes "
        "apart. Line = the range of those paired differences, which for five replicates is the\n"
        "distribution-free 93.75% interval for the median. Hollow = the range spans zero. Below "
        "the dotted rule are the SQLi trap's POSTs: OpenResty, Envoy+Lua and Apache answer them\n"
        "without contacting the origin, so WADM REMOVES a round-trip there and the difference is "
        "negative by design; Envoy+WASM cannot answer from the request phase and still pays it.\n"
        "Apache's POST arms are additionally confounded by connection teardown after its 500 "
        "responses (probe order is rotated, so each probe always follows the same predecessor).",
        fontsize=7.5, color=INK_MUTED, style="italic", va="bottom", linespacing=1.5,
    )
    fig.tight_layout(rect=[0, 0.12, 1, 0.95])
    return finish(fig, out_dir / "paired_latency_added.png")


# ── Shift function ───────────────────────────────────────────────────────────────────────────

def plot_latency_shift(summaries: dict[str, Any], rates: list[int], out_dir: Path) -> Path:
    edges = list(summaries)
    fig, axes = plt.subplots(1, len(rates), figsize=(5.0 * len(rates), 5.6),
                             sharey=True, squeeze=False)
    axes = axes[0]
    floor = 10.0
    clipped = []
    top = floor

    for ax, rate in zip(axes, rates):
        stats_here: list[str] = []
        for i, edge in enumerate(edges):
            data = summaries[edge]["rates"].get(str(rate))
            if not data:
                continue
            per_get = data["latency_delta_quantiles_ms"]["per_get"]
            stats = [s for s, d in per_get.items() if d.get("median") is not None]
            stats_here = stats if len(stats) > len(stats_here) else stats_here
            color = COLORS[EDGES[edge].label]
            # A small horizontal dodge keeps four edges' range bars from sitting on one another.
            dodge = (i - (len(edges) - 1) / 2) * 0.07
            xs = [k + dodge for k in range(len(stats))]
            ys = [per_get[s]["median"] * US_PER_MS for s in stats]
            ax.plot(xs, ys, color=color, linewidth=2, marker="o", markersize=8, zorder=3,
                    markeredgecolor="white", markeredgewidth=1.5, label=EDGES[edge].label)
            for x, s in zip(xs, stats):
                d = per_get[s]
                if d.get("ci_low") is None:
                    continue
                low, high = d["ci_low"] * US_PER_MS, d["ci_high"] * US_PER_MS
                top = max(top, high)
                if low < floor:
                    clipped.append((edge, rate, s, low))
                ax.plot([x, x], [max(low, floor), high], color=color, linewidth=1.2,
                        alpha=0.55, zorder=2)

        ax.set_yscale("log")
        ax.set_xticks(range(len(stats_here)))
        ax.set_xticklabels([QUANTILE_LABELS[s] for s in stats_here])
        ax.set_xlabel("quantile of the request-latency distribution", fontsize=9, color=INK_MUTED)
        ax.set_title(f"{rate} iteration{'s' if rate != 1 else ''}/s offered",
                     fontsize=11, color=INK, loc="left")
        _style_frame(ax)
    # Framed explicitly from every panel's data: the axes share y, and a limit set on one panel
    # would otherwise freeze the scale before a later panel's larger tail is drawn.
    axes[0].set_ylim(floor, top * 1.4)
    axes[0].set_ylabel("latency added per GET (µs, log)", fontsize=9, color=INK_MUTED)

    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=len(labels), frameon=False,
               fontsize=9, bbox_to_anchor=(0.5, 0.09))
    fig.suptitle("WADM stretches the latency tail more than it shifts the median",
                 fontsize=14, fontweight="bold", color=INK, x=0.01, ha="left")
    fig.text(
        0.01, 0.005,
        "For each replicate, (WADM − bare) at the given quantile is averaged over the four GET "
        "probes; points are the median of those paired values, bars their range across\n"
        "replicates. A flat line means WADM adds a constant delay; a rising line means slow "
        "requests get slower by more than typical ones. p99 is shown only from 10/s upward,\n"
        f"where each probe contributes enough samples per cell. Range bars below {floor:.0f} µs "
        "(including negative lower bounds) are clipped at the axis floor.",
        fontsize=7.5, color=INK_MUTED, style="italic", va="bottom", linespacing=1.5,
    )
    fig.tight_layout(rect=[0, 0.15, 1, 0.94])
    for edge, rate, stat, low in clipped:
        print(f"  ! shift function: {EDGES[edge].label} {rate}/s {QUANTILE_LABELS[stat]} range "
              f"lower bound {low:+.0f} µs clipped at the {floor:.0f} µs floor")
    return finish(fig, out_dir / "paired_latency_shift.png")


# ── Internal timer heatmap ───────────────────────────────────────────────────────────────────

def plot_internal_timers(summaries: dict[str, Any], rates: list[int], out_dir: Path) -> Path:
    edges = list(summaries)
    grids = {}
    for rate in rates:
        grid = []
        for kind, phase in TIMER_ROWS:
            row = []
            for edge in edges:
                stats = (((summaries[edge]["rates"].get(str(rate)) or {}).get("internal_us") or {})
                         .get(kind) or {}).get(phase) or {}
                row.append((stats.get("trimmed_mean_us"), stats.get("p90_us")))
            grid.append(row)
        grids[rate] = grid

    values = [v for g in grids.values() for row in g for v, _ in row if v]
    norm = LogNorm(vmin=max(min(values), 0.1), vmax=max(values))
    fig, axes = plt.subplots(1, len(rates), figsize=(3.9 * len(rates) + 2.4, 7.6),
                             sharey=True, squeeze=False)
    axes = axes[0]
    for ax, rate in zip(axes, rates):
        grid = grids[rate]
        matrix = [[(v if v else float("nan")) for v, _ in row] for row in grid]
        image = ax.imshow(matrix, cmap=SEQUENTIAL_BLUE, norm=norm, aspect="auto")
        for r, row in enumerate(grid):
            for c, (mean, p90) in enumerate(row):
                if mean is None:
                    continue
                # Text on the dark half of the ramp switches to white so it stays legible.
                dark = norm(max(mean, norm.vmin)) > 0.55
                ax.text(c, r, f"{mean:.1f}\np90 {p90:.0f}", ha="center", va="center",
                        fontsize=7.5, color="white" if dark else INK, linespacing=1.15)
        ax.set_xticks(range(len(edges)))
        ax.set_xticklabels([EDGES[e].label for e in edges], fontsize=8.5, rotation=20, ha="right")
        ax.set_title(f"{rate} iteration{'s' if rate != 1 else ''}/s", fontsize=11, color=INK,
                     loc="left")
        ax.tick_params(colors=INK_MUTED, length=0)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.set_xticks([x - 0.5 for x in range(1, len(edges))], minor=True)
        ax.set_yticks([y - 0.5 for y in range(1, len(TIMER_ROWS))], minor=True)
        ax.grid(True, which="minor", color="white", linewidth=2)
        ax.tick_params(which="minor", length=0)

    axes[0].set_yticks(range(len(TIMER_ROWS)))
    axes[0].set_yticklabels([f"{kind}  {phase}" for kind, phase in TIMER_ROWS], fontsize=8.5)
    fig.subplots_adjust(left=0.17, right=0.88, top=0.9, bottom=0.17, wspace=0.08)
    bar = fig.colorbar(image, cax=fig.add_axes([0.9, 0.25, 0.012, 0.6]))
    bar.set_label("5%-trimmed mean (µs, log colour)", fontsize=9, color=INK_MUTED)
    bar.ax.tick_params(colors=INK_MUTED, labelsize=8)

    fig.suptitle("In-edge timers: what each detect / inject region costs",
                 fontsize=14, fontweight="bold", color=INK, x=0.01, ha="left")
    fig.text(
        0.01, 0.005,
        "Cell = 5%-trimmed mean of every sample pooled across valid WADM replicates, with p90 "
        "beneath. The edges log whole microseconds, so a median of these 1–5 µs regions can only\n"
        "be an integer; the trimmed mean recovers sub-µs resolution and drops the preemption tail. "
        "Envoy+WASM truncates each delta to whole µs while the other edges subtract two\n"
        "truncated timestamps, so WASM reads ≈0.5 µs low here. These regions are 4–11% of the CPU "
        "WADM adds (see cost_attribution): compare them with each other, not with latency.",
        fontsize=7.5, color=INK_MUTED, style="italic", va="bottom", linespacing=1.5,
    )
    return finish(fig, out_dir / "paired_internal_timers.png")


# ── Headline table ───────────────────────────────────────────────────────────────────────────

def headline_rows(summaries: dict[str, Any], rates: list[int]) -> list[dict[str, Any]]:
    def us(d: dict[str, Any]) -> float | None:
        return None if d.get("median") is None else round(d["median"] * US_PER_MS)

    rows = []
    for edge, summary in summaries.items():
        marginal = summary["cpu_marginal"]["wadm_minus_bare"]
        for rate in rates:
            data = summary["rates"].get(str(rate))
            if not data:
                continue
            per_get = data["latency_delta_quantiles_ms"]["per_get"]
            pct = [data["latency_delta_pct"][m].get("median") for m in GET_METRICS]
            pct = [p for p in pct if p is not None]
            cov = [c["worst_ratio"] for phases in data["coverage"].values() for c in phases.values()]
            split = data["cpu_delta_split_us_per_request"]
            rows.append({
                "edge": EDGES[edge].label,
                "rate_iter_per_s": rate,
                "replicates": data["replicates_paired"],
                "get_p50_added_us": us(per_get.get("med", {})),
                "get_p95_added_us": us(per_get.get("p95", {})),
                "get_p50_added_pct": round(sum(pct) / len(pct)) if pct else None,
                "get_p50_resolved": per_get.get("med", {}).get("sign_agreement"),
                "cpu_added_us_per_req": (
                    round(data["cpu_delta_us_per_request"]["median"])
                    if data["cpu_delta_us_per_request"].get("median") is not None else None
                ),
                "cpu_user_us": round(split["user"]["median"]) if split["user"].get("n") else None,
                "cpu_system_us": (
                    round(split["system"]["median"]) if split["system"].get("n") else None
                ),
                "cpu_marginal_added_us": (
                    round(marginal["median"]) if marginal.get("median") is not None else None
                ),
                "coverage_worst": round(min(cov), 4) if cov else None,
            })
    return rows


def write_headline(rows: list[dict[str, Any]], results_dir: Path) -> list[Path]:
    csv_path = results_dir / "paired_headline.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    def cell(v: Any) -> str:
        return "—" if v is None else (f"{v:+,}" if isinstance(v, int) and not isinstance(v, bool)
                                       else str(v))

    lines = [
        "# WADM headline figures (paired dataset)",
        "",
        "Generated by `benchmarks/plot_paired_overhead.py` from `paired_<edge>.json`. Latency is "
        "per GET probe (the four requests every edge proxies and injects into); CPU is per request "
        "over the whole 9-request iteration and is therefore **net** of the work the SQLi trap "
        "saves by not proxying its POSTs, and **includes** the cost of writing WADM's timing log "
        "lines. Coverage is recorded timing samples ÷ expected (1.0 = every request that should "
        "have taken the WADM path left a record).",
        "",
        "| Edge | Rate (iter/s) | Δ p50 per GET (µs) | Δ p50 (% of bare) | Δ p95 per GET (µs) "
        "| Resolved | CPU added (µs/req) | user | system | Marginal CPU added (µs/req) | "
        "Coverage (worst) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['edge']} | {r['rate_iter_per_s']} | {cell(r['get_p50_added_us'])} | "
            f"{cell(r['get_p50_added_pct'])}% | {cell(r['get_p95_added_us'])} | "
            f"{r['get_p50_resolved'] or '—'} | {cell(r['cpu_added_us_per_req'])} | "
            f"{cell(r['cpu_user_us'])} | {cell(r['cpu_system_us'])} | "
            f"{cell(r['cpu_marginal_added_us'])} | {cell(r['coverage_worst'])} |"
        )
    lines += [
        "",
        "`user` and `system` are each the median of their own paired differences, so they need "
        "not sum to the total. Marginal CPU is fitted across all three rates (idle cost separated "
        "from per-request cost), so it is one value per edge, repeated on each row; Apache's fit "
        "is rejected as unphysical (crash recovery makes its CPU per request climb with rate). "
        "`Resolved` counts replicates agreeing in sign. Capacity is deliberately "
        "absent: the current ladder does not record which resource saturated first, and the bare "
        "tiers appear to be origin-bound (see `docs/BENCHMARK_METHODOLOGY.md` §6).",
        "",
    ]
    md_path = results_dir / "paired_headline.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return [csv_path, md_path]


def main() -> int:
    results_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent / "results"
    )
    summaries = load_summaries(results_dir)
    if not summaries:
        print("Nothing to plot. Run run_paired_benchmark.py first.")
        return 1
    rates = rates_of(summaries)
    out_dir = results_dir / "plots"

    written = [
        plot_latency_added(summaries, rates, out_dir),
        plot_latency_shift(summaries, rates, out_dir),
        plot_internal_timers(summaries, rates, out_dir),
    ]
    written += write_headline(headline_rows(summaries, rates), results_dir)
    for path in written:
        print(f"  wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
