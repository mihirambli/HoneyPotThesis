#!/usr/bin/env python3
"""
What a SQL-injection hit and a clean login (miss) cost on each edge: latency, CPU and scan time.

The trap watches POST /api/login. A body matching one of the signatures is a HIT: the trap answers
with a fake database error (500) and logs `WADM ALERT`. Anything else is a MISS: it answers with a
login-failed page (401) and logs `WADM TRAP`. Without WADM the same POST is proxied to the origin,
which has no such route and answers 404 — that is the baseline every "added" figure is measured
against.

Read from one interleaved run per edge (run_paired_benchmark.py --mixes sqli-hit,sqli-miss
--no-prime), split into one file per outcome: every iteration holds only the two requests of one
outcome (plain and %-encoded body), so the edge's CPU per iteration, net of idle, divided by two is
what one request of that outcome costs. The main mix cannot give this: cgroup CPU counters cannot
tell request types apart. Hit and miss cells share each replicate and tier, minutes apart, so
hit - miss is paired in time like every other difference here.

Figure (results/plots/sqli_hit_miss.png), one row per edge, circle = hit, diamond = miss:
    latency added    (silent WADM - bare) median latency per request
    CPU added        (silent WADM - bare) edge CPU per request, net of idle
    scan time        time inside the signature scan, from the in-edge timer (instrumented tier):
                     the only part of the trap's work that differs by construction between a hit
                     (stops at the first matching signature) and a miss (tries every signature)
The gap between the two markers in a row is the hit-versus-miss difference, printed at the right.

Table (results/sqli.md): absolute latencies, each encoding's scan time, reconnects, and whether
every hit produced an ALERT and every miss a TRAP (and no miss an ALERT).

Usage:
    python3 benchmarks/plot_sqli.py [results_dir]
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.transforms import blended_transform_factory

from analyze_paired import cell_by_replicate, tiers_of, trend_stat, valid_cells
from edge_specs import EDGE_KEYS, EDGES
from plot_common import COLORS, INK, INK_MUTED, _style_frame
from plot_mechanisms import net_cpu_per_iteration
from wadm_timings import paired_delta, summarize

OUTCOMES = {
    "hit": ["sqli_hit", "sqli_hit_enc"],
    "miss": ["sqli_miss", "sqli_miss_enc"],
}
SCAN_KINDS = {
    "hit": {"plain": "sql_injection", "encoded": "sql_injection_encoded"},
    "miss": {"plain": "sql_injection_miss", "encoded": "sql_injection_miss_encoded"},
}
# Log lines one request of each outcome must produce on a WADM tier, and on bare (none).
EXPECTED_LINES = {"hit": {"alert": 1, "trap": 0}, "miss": {"alert": 0, "trap": 1}}
MARKERS = {"hit": "o", "miss": "D"}
OUTCOME_TEXT = {
    "hit": "hit: signature matched → 500 error page + ALERT",
    "miss": "miss: clean login → 401 page + TRAP log",
}
# The main mix's rate whose per-arm latency is shown beside the dedicated runs, as a cross-check.
MAIN_MIX_RATE = 10
US_PER_MS = 1000.0


def load(results_dir: Path, name: str) -> dict[str, Any] | None:
    path = results_dir / name
    return json.loads(path.read_text()) if path.exists() else None


def deployed(doc: dict[str, Any]) -> str:
    return "silent" if "silent" in tiers_of(doc) else "wadm"


def requests_per_iteration(cell: dict[str, Any]) -> float | None:
    k6 = cell.get("k6") or {}
    if not k6.get("iterations") or not k6.get("edge_requests"):
        return None
    return k6["edge_requests"] / k6["iterations"]


def arm_latency_added(low: dict[str, Any], high: dict[str, Any], arms: list[str]) -> float | None:
    """Mean over the outcome's two arms of (high - low) median latency, in microseconds."""
    deltas = []
    for arm in arms:
        a, b = trend_stat(low, f"{arm}_duration"), trend_stat(high, f"{arm}_duration")
        if a is not None and b is not None:
            deltas.append((b - a) * US_PER_MS)
    return statistics.fmean(deltas) if len(deltas) == len(arms) else None


def per_replicate(doc: dict[str, Any], outcome: str) -> dict[int, dict[str, float | None]]:
    """{replicate: latency and CPU added per request, silent WADM - bare} for one outcome's run."""
    rate = max(c["rate"] for c in doc["cells"])
    high = deployed(doc)
    bare, other = cell_by_replicate(doc, "bare", rate), cell_by_replicate(doc, high, rate)
    out: dict[int, dict[str, float | None]] = {}
    for rep in sorted(set(bare) & set(other)):
        b, h = bare[rep], other[rep]
        b_cpu, h_cpu, per_iter = net_cpu_per_iteration(b), net_cpu_per_iteration(h), requests_per_iteration(h)
        out[rep] = {
            "latency_us": arm_latency_added(b, h, OUTCOMES[outcome]),
            "cpu_us": (h_cpu - b_cpu) / per_iter if None not in (b_cpu, h_cpu) and per_iter else None,
        }
    return out


def tier_medians(doc: dict[str, Any], rate: int, tier: str, outcome: str) -> dict[str, Any]:
    """Per tier: absolute latency, share of requests that reconnected, and log lines per request."""
    cells = valid_cells(doc, tier, rate)
    latency, reconnect, alert, trap = [], [], [], []
    for c in cells:
        k6 = c.get("k6") or {}
        arms = [trend_stat(c, f"{a}_duration") for a in OUTCOMES[outcome]]
        if None not in arms:
            latency.append(statistics.fmean(arms) * US_PER_MS)
        rates = [((k6.get("new_conn") or {}).get(a) or {}).get("rate") for a in OUTCOMES[outcome]]
        if None not in rates:
            reconnect.append(statistics.fmean(rates))
        requests = k6.get("edge_requests")
        if c.get("alerts") is not None and requests:
            alert.append(c["alerts"]["alert"] / requests)
            trap.append(c["alerts"]["trap"] / requests)
    median = lambda v: statistics.median(v) if v else None  # noqa: E731
    return {
        "latency_us": median(latency),
        "reconnect": median(reconnect),
        "alert_per_request": median(alert),
        "trap_per_request": median(trap),
    }


def scan_time(doc: dict[str, Any], outcome: str) -> dict[str, Any]:
    """Time inside the signature scan (instrumented tier), pooled and per encoding."""
    rate = max(c["rate"] for c in doc["cells"])
    out: dict[str, Any] = {}
    pooled: list[int] = []
    for encoding, kind in SCAN_KINDS[outcome].items():
        samples = [
            v for c in valid_cells(doc, "wadm", rate)
            for v in ((c.get("tokens_raw") or {}).get(kind) or {}).get("detect_us", [])
        ]
        pooled += samples
        out[encoding] = summarize(samples).to_json() if samples else None
    out["pooled"] = summarize(pooled).to_json() if pooled else None
    return out


def main_mix_added(doc: dict[str, Any] | None, outcome: str) -> dict[str, Any] | None:
    """The same latency difference read from the main paired dataset, as a cross-check."""
    if not doc:
        return None
    high = deployed(doc)
    bare = cell_by_replicate(doc, "bare", MAIN_MIX_RATE)
    other = cell_by_replicate(doc, high, MAIN_MIX_RATE)
    values = [
        arm_latency_added(bare[r], other[r], OUTCOMES[outcome]) for r in sorted(set(bare) & set(other))
    ]
    return paired_delta([v for v in values if v is not None])


def analyse(results_dir: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for edge in EDGE_KEYS:
        docs = {o: load(results_dir, f"sqli-{o}_{edge}.json") for o in OUTCOMES}
        if not all(docs.values()):
            continue
        main = load(results_dir, f"paired_{edge}.json")
        reps = {o: per_replicate(docs[o], o) for o in OUTCOMES}
        entry: dict[str, Any] = {"rate": max(c["rate"] for c in docs["hit"]["cells"])}
        for o, doc in docs.items():
            rate = max(c["rate"] for c in doc["cells"])
            entry[o] = {
                "latency_added": paired_delta(
                    [v["latency_us"] for v in reps[o].values() if v["latency_us"] is not None]
                ),
                "cpu_added": paired_delta(
                    [v["cpu_us"] for v in reps[o].values() if v["cpu_us"] is not None]
                ),
                "tiers": {t: tier_medians(doc, rate, t, o) for t in tiers_of(doc)},
                "scan": scan_time(doc, o),
                "main_mix_latency_added": main_mix_added(main, o),
            }
        # Replicate i of the hit cells against replicate i of the miss cells. Run with --mixes, both
        # share the replicate and tier, minutes apart; from two separate runs the pairing is by
        # index only, but each side is still a within-replicate difference against bare.
        shared = sorted(set(reps["hit"]) & set(reps["miss"]))
        entry["hit_minus_miss"] = {
            key: paired_delta([
                reps["hit"][r][key] - reps["miss"][r][key] for r in shared
                if reps["hit"][r][key] is not None and reps["miss"][r][key] is not None
            ])
            for key in ("latency_us", "cpu_us")
        }
        out[edge] = entry
    return out


# ── Figure ───────────────────────────────────────────────────────────────────────────────────

def draw_panel(ax, data: dict[str, Any], getter, gap_getter, title: str, xlabel: str,
               zero: bool, value_labels: bool = False) -> None:
    edges = list(data)
    offsets = {"hit": 0.17, "miss": -0.17}
    values: list[float] = []
    for i, edge in enumerate(edges):
        color = COLORS[EDGES[edge].label]
        for outcome, dy in offsets.items():
            d = getter(data[edge], outcome)
            if not d or d.get("median") is None:
                continue
            y = -i + dy
            if d.get("ci_low") is not None and d.get("ci_reliable"):
                ax.plot([d["ci_low"], d["ci_high"]], [y, y], color=color, linewidth=2,
                        solid_capstyle="round", zorder=2)
                values += [d["ci_low"], d["ci_high"]]
            resolved = d.get("significant", True)
            ax.plot(d["median"], y, marker=MARKERS[outcome], markersize=8, zorder=3,
                    color=color, markerfacecolor=color if resolved else "white",
                    markeredgecolor=color, markeredgewidth=2)
            values.append(d["median"])
            if value_labels:
                ax.annotate(f"{d['median']:.1f}", (d["median"], y), xytext=(7, 0),
                            textcoords="offset points", va="center", fontsize=8, color=INK)
    lo, hi = min(values + [0.0]), max(values + [0.0])
    span = (hi - lo) or 1.0
    # The right quarter of each panel is kept clear for the hit-minus-miss labels.
    ax.set_xlim(lo - 0.08 * span, hi + (0.55 if gap_getter else 0.2) * span)
    if gap_getter:
        trans = blended_transform_factory(ax.transAxes, ax.transData)
        for i, edge in enumerate(edges):
            gap = gap_getter(data[edge])
            if gap and gap.get("median") is not None:
                ax.text(0.985, -i, f"hit − miss\n{gap['median']:+.0f} µs", transform=trans,
                        ha="right", va="center", fontsize=8, color=INK_MUTED, linespacing=1.2)
    if zero:
        ax.axvline(0, color=INK_MUTED, linewidth=1, zorder=1)
    ax.set_title(title, fontsize=11, color=INK, loc="left")
    ax.set_xlabel(xlabel, fontsize=9, color=INK_MUTED)
    _style_frame(ax)
    ax.grid(True, which="major", axis="x", alpha=0.25, linewidth=0.8)
    ax.grid(False, axis="y")


def plot(data: dict[str, Any], out_dir: Path) -> Path:
    edges = list(data)
    fig, axes = plt.subplots(1, 3, figsize=(16.5, 1.25 * len(edges) + 3.4), sharey=True)
    draw_panel(
        axes[0], data, lambda e, o: e[o]["latency_added"],
        lambda e: e["hit_minus_miss"]["latency_us"],
        "Latency added per request", "µs vs. the same edge without WADM (median request)",
        zero=True,
    )
    draw_panel(
        axes[1], data, lambda e, o: e[o]["cpu_added"],
        lambda e: e["hit_minus_miss"]["cpu_us"],
        "Edge CPU added per request", "µs of CPU vs. the same edge without WADM (net of idle)",
        zero=True,
    )
    draw_panel(
        axes[2], data,
        lambda e, o: {"median": (e[o]["scan"]["pooled"] or {}).get("trimmed_mean_us")},
        None, "Time inside the signature scan", "µs, in-edge timer (5%-trimmed mean)",
        zero=False, value_labels=True,
    )
    axes[2].set_xlim(left=0)
    axes[0].set_yticks([-i for i in range(len(edges))])
    axes[0].set_yticklabels([EDGES[e].label for e in edges], fontsize=10)
    axes[0].set_ylim(-len(edges) + 0.5, 0.5)
    # Which side of zero means what, said once per panel, inside the axis where no title competes.
    for ax in axes[:2]:
        trans = blended_transform_factory(ax.transData, ax.transAxes)
        ax.text(0, 0.015, "← cheaper / faster   costlier / slower →", transform=trans,
                ha="center", va="bottom", fontsize=7.5, color=INK_MUTED,
                bbox={"facecolor": "white", "edgecolor": "none", "pad": 1.5})

    handles = [
        Line2D([], [], color=INK_MUTED, marker=MARKERS[o], markersize=8, linewidth=0, label=OUTCOME_TEXT[o])
        for o in OUTCOMES
    ] + [
        Line2D([], [], color=INK_MUTED, marker="o", markersize=8, linewidth=0,
               markerfacecolor="white", markeredgewidth=2, label="range spans zero (not resolved)"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=3, frameon=False, fontsize=9,
               bbox_to_anchor=(0.5, 0.115))
    rate = next(iter(data.values()))["rate"]
    fig.suptitle(f"What a SQL-injection hit and a clean login cost — {rate} iterations/s "
                 f"of two POSTs each ({rate * 2} req/s)",
                 fontsize=14, fontweight="bold", color=INK, x=0.01, ha="left")
    fig.text(
        0.01, 0.005,
        "Without WADM the POST is proxied to the origin, which answers 404; with WADM the trap "
        "answers it. Left of zero means the trap answers faster or cheaper than that origin\n"
        "round-trip — true on OpenResty, Envoy+Lua and Apache, which answer from the request "
        "phase; Envoy+WASM still forwards the request and replaces the response. Dot = median over\n"
        "replicates of the paired (silent WADM − bare) difference, line = its range; hit and miss "
        "are separate cells of every replicate (plain and %-encoded body, no priming request), so CPU per\n"
        "request is that outcome's alone. Scan time is read from the instrumented build: a hit "
        "stops at the first matching signature, a miss tries all of them.",
        fontsize=7.5, color=INK_MUTED, style="italic", va="bottom", linespacing=1.5,
    )
    fig.tight_layout(rect=[0, 0.16, 1, 0.94])
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "sqli_hit_miss.png"
    fig.savefig(path, dpi=150, facecolor="white")
    plt.close(fig)
    return path


# ── Table ────────────────────────────────────────────────────────────────────────────────────

def fmt(d: dict[str, Any] | None, digits: int = 0) -> str:
    if not d or d.get("median") is None:
        return "—"
    out = f"{d['median']:+.{digits}f}"
    if d.get("ci_reliable"):
        out += f" ({d['ci_low']:+.{digits}f}..{d['ci_high']:+.{digits}f})"
    return out


def num(v: float | None, spec: str) -> str:
    return "—" if v is None else format(v, spec)


def write_table(data: dict[str, Any], results_dir: Path) -> Path:
    rate = next(iter(data.values()))["rate"]
    lines = [
        "# SQL-injection trap: cost of a hit and of a miss",
        "",
        f"Generated by `benchmarks/plot_sqli.py` from `sqli-hit_<edge>.json` and "
        f"`sqli-miss_<edge>.json` ({rate} iterations/s of two POSTs, no priming request). "
        "*Added* = silent WADM − bare, median (range) over paired replicates. Bare proxies the POST "
        "to the origin (404); WADM's trap answers it. *Main mix* repeats the latency difference "
        f"from the main paired dataset at {MAIN_MIX_RATE}/s as a cross-check.",
        "",
        "| Edge | Outcome | Latency bare (µs) | Latency WADM (µs) | Latency added (µs) | Main mix (µs) "
        "| Edge CPU added (µs/req) | Scan plain / %-enc (µs) | Reconnects WADM / bare "
        "| ALERT / TRAP lines per request |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for edge, e in data.items():
        for o in OUTCOMES:
            x = e[o]
            tiers = x["tiers"]
            dep = "silent" if "silent" in tiers else "wadm"
            scan = x["scan"]
            lines.append(
                f"| {EDGES[edge].label} | {o} | {num(tiers['bare']['latency_us'], '.0f')} | "
                f"{num(tiers[dep]['latency_us'], '.0f')} | {fmt(x['latency_added'])} | "
                f"{fmt(x['main_mix_latency_added'])} | {fmt(x['cpu_added'])} | "
                f"{num((scan['plain'] or {}).get('trimmed_mean_us'), '.1f')} / "
                f"{num((scan['encoded'] or {}).get('trimmed_mean_us'), '.1f')} | "
                f"{num(tiers[dep]['reconnect'], '.0%')} / {num(tiers['bare']['reconnect'], '.0%')} | "
                f"{num(tiers[dep]['alert_per_request'], '.3f')} / "
                f"{num(tiers[dep]['trap_per_request'], '.3f')} |"
            )
    lines += [
        "",
        "## Hit − miss",
        "",
        "| Edge | Latency (µs) | Edge CPU (µs/req) | Scan time (µs) |",
        "|---|---|---|---|",
    ]
    for edge, e in data.items():
        scan = [(e[o]["scan"]["pooled"] or {}).get("trimmed_mean_us") for o in OUTCOMES]
        scan_gap = f"{scan[0] - scan[1]:+.1f}" if None not in scan else "—"
        lines.append(
            f"| {EDGES[edge].label} | {fmt(e['hit_minus_miss']['latency_us'])} | "
            f"{fmt(e['hit_minus_miss']['cpu_us'])} | {scan_gap} |"
        )
    lines += [
        "",
        "ALERT / TRAP lines per request must be 1 / 0 for a hit and 0 / 1 for a miss on a WADM "
        "tier: a hit that logs no ALERT is a missed detection, a miss that logs an ALERT a false "
        "positive. Reconnects: the share of requests that opened a new connection — Apache closes "
        "the connection after every 500, so on Apache every hit costs the client a reconnect.",
        "",
    ]
    path = results_dir / "sqli.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    (results_dir / "sqli.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def main() -> int:
    results_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent / "results"
    )
    data = analyse(results_dir)
    if not data:
        print("Nothing to plot: run run_paired_benchmark.py --mix sqli-hit and --mix sqli-miss "
              "(with --no-prime) first.")
        return 1
    for path in (plot(data, results_dir / "plots"), write_table(data, results_dir)):
        print(f"  wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
