#!/usr/bin/env python3
"""
Where WADM's cost actually shows up: instrumented regions vs. CPU burned vs. latency added.

The detect/inject timers in each edge measure only the regions someone chose to wrap. Everything
else WADM does — Lua VM entry and exit per phase, the body filter walking the response, the extra
header table writes, rendering and writing alert lines, the larger body the origin must send — is
real work that no timer sees. Comparing the three planes gives the size of that blind spot, which
is not visible from any one of them alone.

Two figures:

    cost_attribution.png            per edge, the three scopes side by side, microseconds per
                                    iteration, at the highest rate every edge sustained. The CPU
                                    bar is the MARGINAL CPU fitted across all rates, which removes
                                    the idle cost a per-rate figure divides into few requests.
    instrumented_coverage.png       instrumented share of the CPU WADM adds, across the rate
                                    ladder, with the range across replicates

Two caveats belong in any sentence quoting these numbers. The CPU bar is NET: cgroup counters cannot
tell request types apart, and the trap answers 4 of the 9 requests in an iteration itself on three
edges, saving the proxy work the bare tier does for them. And it INCLUDES the cost of writing the
timing log lines (about 39 per iteration), which only the WADM tier emits — part of the "blind spot"
may be the instrumentation itself rather than WADM.

These are NESTED SCOPES, not addends, and the figure says so. Instrumented time is wall-clock
inside the timed regions; CPU time is what the cgroup counter attributes to the container;
end-to-end is what the client waited. Each contains the previous in the sense of being a wider
accounting boundary, but they are measured on different instruments and must not be stacked or
subtracted into a "remainder" bar — doing so would imply a precision none of them have.

The four SQLi POSTs are excluded from the end-to-end bar and reported beside it instead. On three
of four edges the trap answers them without contacting the origin, so WADM *removes* a round-trip
there: folding that saving into a bar labelled "latency added" would cancel out the GET overhead
the figure exists to show.

Reads benchmarks/results/paired_<edge>.json (written by run_paired_benchmark.py).

Usage:
    python3 benchmarks/plot_cost_attribution.py [results_dir]
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

from edge_specs import EDGE_KEYS, EDGES
from analyze_paired import paired_marginal_cpu
from plot_common import COLORS, INK, INK_MUTED, _style_frame
from wadm_timings import ALL_KINDS, PHASES, paired_delta

# The four GETs, where WADM adds work without removing any. The four POSTs are the SQLi trap and
# are handled separately for the reason in the module docstring.
GET_METRICS = [
    "inject_get_duration",
    "detect_query_duration",
    "token_tamper_duration",
    "token_decoy_duration",
]
POST_METRICS = [
    "sqli_hit_duration",
    "sqli_hit_enc_duration",
    "sqli_miss_duration",
    "sqli_miss_enc_duration",
]

# Bars are tinted lightest to darkest within one edge's hue (see bars_for). The scopes are ordered —
# each is a wider accounting boundary than the last — so this is a sequential encoding, one hue
# light to dark, rather than categorical colours that would compete with the edge hue carrying
# identity across figures.
#
# Instrumented time and CPU time are both measures of WORK, and they nest: every timed region is
# CPU the container spent, so instrumented / CPU is a true subset ratio and is the headline.
#
# End-to-end latency is NOT in that chain. It is wall clock at the client, and a container with
# several workers on two pinned cores can burn more than one microsecond of CPU per microsecond of
# elapsed time — measured here, Apache added 4,835 us of CPU while adding 1,565 us of latency.
# Drawing it as a third nested scope would assert a containment that parallelism makes false, so
# it is drawn as a separate instrument and labelled as one.

# Edges whose SQLi trap cannot answer from the request phase and so still sends the trap's requests
# to the origin (proxy-wasm rejects a local reply there; see docs/EDGE_LEVELING.md).
TRAP_PROXIES_TO_ORIGIN = {"wasm"}

# Origin CPU is measured but is NOT one of the scopes, because it does not nest: WADM's trap
# answers four of the nine requests in an iteration itself, so the origin receives fewer requests
# and its CPU delta is strongly NEGATIVE. Adding it to the edge's would produce a bar smaller than
# the one above it and break the ordering the figure depends on. It is a separate result — the
# work WADM removes from the origin — and is annotated as one.


def tint(hex_color: str, amount: float) -> tuple[float, float, float]:
    """Blend toward white. Monotonic in lightness, which is what a sequential ramp requires."""
    hex_color = hex_color.lstrip("#")
    rgb = [int(hex_color[i: i + 2], 16) / 255 for i in (0, 2, 4)]
    return tuple(c + (1.0 - c) * amount for c in rgb)


def load(results_dir: Path, edge: str) -> dict[str, Any] | None:
    path = results_dir / f"paired_{edge}.json"
    return json.loads(path.read_text()) if path.exists() else None


def valid(doc: dict[str, Any], tier: str, rate: int) -> dict[int, dict[str, Any]]:
    return {
        c["replicate"]: c for c in doc["cells"]
        if c["tier"] == tier and c["rate"] == rate and c["validity"]["valid"]
    }


def instrumented_us_per_iteration(cell: dict[str, Any]) -> float | None:
    """Total time inside every timed region, per iteration.

    Weighted by how often each region actually fired (`count`), not by a mean of per-kind means:
    a kind that injects on four requests per iteration should contribute four times what one
    firing on a single request does. Uses p50, because the mean of a wall-clock-timed region is
    set by its preemption tail.
    """
    tokens = cell.get("tokens")
    iterations = (cell.get("k6") or {}).get("iterations")
    if not tokens or not iterations:
        return None
    total = 0.0
    for kind in ALL_KINDS:
        for phase in PHASES:
            stats = (tokens.get(kind) or {}).get(phase) or {}
            if stats.get("count") and stats.get("p50_us") is not None:
                total += stats["count"] * stats["p50_us"]
    return total / iterations


def reqs_per_iteration(cell: dict[str, Any]) -> float | None:
    k6 = cell.get("k6") or {}
    # edge_requests excludes test.js setup()'s reference requests, half of which bypass the edge.
    requests = k6.get("edge_requests") or k6.get("http_reqs")
    if not k6.get("iterations") or not requests:
        return None
    return requests / k6["iterations"]


def trend_med(cell: dict[str, Any], metric: str) -> float | None:
    stats = ((cell.get("k6") or {}).get("trends") or {}).get(metric)
    return stats.get("med") if stats and stats.get("count") else None


def cpu_delta_per_iteration(low: dict[str, Any], high: dict[str, Any]) -> float | None:
    per_iter = reqs_per_iteration(high)
    a = ((low.get("cpu") or {}).get("edge") or {}).get("cpu_us_per_request")
    b = ((high.get("cpu") or {}).get("edge") or {}).get("cpu_us_per_request")
    if per_iter is None or a is None or b is None:
        return None
    return (b - a) * per_iter


def e2e_delta(low: dict[str, Any], high: dict[str, Any], metrics: list[str]) -> float | None:
    """Sum over `metrics` of (high - low) median latency, in microseconds per iteration."""
    deltas = [
        trend_med(high, m) - trend_med(low, m)
        for m in metrics
        if trend_med(high, m) is not None and trend_med(low, m) is not None
    ]
    return sum(deltas) * 1000.0 if len(deltas) == len(metrics) else None


def scopes_for(doc: dict[str, Any], rate: int) -> dict[str, Any] | None:
    """Every scope at one rate, each as a paired-delta summary in microseconds per iteration.

    With a silent tier, "what WADM costs" is silent - bare (the deployable build) and the timers'
    coverage is measured against that; wadm - silent is the instrumentation's own cost. Without
    one, the instrumented build is the only WADM tier and stands in for both.
    """
    tiers = {c["tier"] for c in doc["cells"]}
    deployed = "silent" if "silent" in tiers else "wadm"
    cells = {t: valid(doc, t, rate) for t in ("bare", "silent", "wadm")}
    shared = sorted(set(cells["bare"]) & set(cells["wadm"]) & set(cells[deployed]))
    if not shared:
        return None

    series: dict[str, list[float]] = {
        k: [] for k in ("timers", "cpu_deployed", "cpu_instrumented", "cpu_probe",
                        "e2e_get", "e2e_post", "origin", "coverage")
    }

    def add(key: str, value: float | None) -> None:
        if value is not None:
            series[key].append(value)

    for rep in shared:
        b, d, w = cells["bare"][rep], cells[deployed][rep], cells["wadm"][rep]
        timers = instrumented_us_per_iteration(w)
        add("timers", timers)
        deployed_cpu = cpu_delta_per_iteration(b, d)
        add("cpu_deployed", deployed_cpu)
        add("cpu_instrumented", cpu_delta_per_iteration(b, w))
        if deployed == "silent":
            add("cpu_probe", cpu_delta_per_iteration(d, w))
        if timers is not None and deployed_cpu and deployed_cpu > 0:
            add("coverage", 100.0 * timers / deployed_cpu)
        add("e2e_get", e2e_delta(b, d, GET_METRICS))
        add("e2e_post", e2e_delta(b, d, POST_METRICS))
        per_iter = reqs_per_iteration(d)
        b_org = ((b.get("cpu") or {}).get("origin") or {}).get("cpu_us_per_request")
        d_org = ((d.get("cpu") or {}).get("origin") or {}).get("cpu_us_per_request")
        if per_iter and b_org is not None and d_org is not None:
            add("origin", (d_org - b_org) * per_iter)

    return {
        "deployed_tier": deployed,
        "series": {k: paired_delta(v) for k, v in series.items()},
        "reqs_per_iteration": statistics.median(
            [r for r in (reqs_per_iteration(cells["wadm"][rep]) for rep in shared) if r]
        ),
        "replicates": len(shared),
        "cpu_marginal": False,
    }


def with_marginal_cpu(entry: dict[str, Any], marginal: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Swap the per-rate CPU scopes for marginal fits, scaled to one iteration.

    A per-rate CPU-per-request divides the edge's whole consumption, idle timers included, by the
    requests served, so at low rates it is mostly idle cost and its range spans hundreds of µs. The
    marginal fit separates idle from per-request cost across every rate within each replicate,
    which is the figure that answers "what does one more request cost". `marginal` maps a series
    name to the paired marginal fit for that comparison.
    """
    scale = entry["reqs_per_iteration"]
    series = dict(entry["series"])
    for key, fit in marginal.items():
        if fit.get("samples"):
            series[key] = paired_delta([v * scale for v in fit["samples"]])
    return {**entry, "series": series, "cpu_marginal": True}


def bars_for(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The bars one panel draws, top to bottom: timers, then CPU scopes, then latency."""
    s = entry["series"]
    cpu_kind = "marginal" if entry["cpu_marginal"] else "per-rate"
    if entry["deployed_tier"] == "silent":
        return [
            {"label": "Instrumented\ndetect + inject timers", "delta": s["timers"], "tint": 0.6},
            {"label": f"CPU added by WADM as deployed\n(silent − bare, {cpu_kind})",
             "delta": s["cpu_deployed"], "tint": 0.3},
            {"label": f"CPU added by the timing lines\n(instrumented − silent, {cpu_kind})",
             "delta": s["cpu_probe"], "tint": 0.3, "hatch": ".."},
            {"label": f"CPU added, instrumented build\n(instrumented − bare, {cpu_kind})",
             "delta": s["cpu_instrumented"], "tint": 0.0},
            {"label": "End-to-end added, as deployed\n(4 GET probes, wall clock)",
             "delta": s["e2e_get"], "latency": True},
        ]
    return [
        {"label": "Instrumented\ndetect + inject timers", "delta": s["timers"], "tint": 0.55},
        {"label": f"Edge CPU added\n({cpu_kind}, cgroup cpu.stat)",
         "delta": s["cpu_instrumented"], "tint": 0.28},
        {"label": "End-to-end added\n(4 GET probes, wall clock)", "delta": s["e2e_get"],
         "latency": True},
    ]


def err_from(delta: dict[str, Any]) -> tuple[float, float] | None:
    """Asymmetric error bar from the bootstrap interval, omitted when it is not meaningful."""
    if not delta.get("ci_reliable") or delta.get("ci_low") is None:
        return None
    median = delta["median"]
    return (max(0.0, median - delta["ci_low"]), max(0.0, delta["ci_high"] - median))


def plot_attribution(rate: int, data: dict[str, Any], out_dir: Path) -> Path | None:
    present = [e for e in EDGE_KEYS if data.get(e) and data[e].get(str(rate))]
    if not present:
        return None

    cols = min(2, len(present))
    rows = (len(present) + cols - 1) // cols
    n_bars = max(len(bars_for(data[e][str(rate)])) for e in present)
    fig, axes = plt.subplots(
        rows, cols, figsize=(7.8 * cols, (1.0 + 0.8 * n_bars) * rows + 0.6), squeeze=False
    )

    for index, edge in enumerate(present):
        ax = axes[index // cols][index % cols]
        entry = data[edge][str(rate)]
        bars = bars_for(entry)
        scopes = [b["delta"] for b in bars]
        base = COLORS[EDGES[edge].label]

        positions = list(range(len(scopes)))[::-1]
        values = [s["median"] if s.get("median") is not None else 0.0 for s in scopes]
        # Framed from each bar's UPPER bound, so no interval is clipped at the right edge and every
        # value label can sit past its whisker rather than on top of it.
        uppers = [
            max(v, s.get("ci_high") or v) if s.get("ci_reliable") else v
            for v, s in zip(values, scopes)
        ]
        widest = max(uppers) if max(uppers) > 0 else 1.0

        for slot, (pos, value, scope, bar) in enumerate(zip(positions, values, scopes, bars)):
            err = err_from(scope)
            is_work = not bar.get("latency")
            ax.barh(
                pos, value, height=0.58,
                # Hollow for the latency bar: it is a different instrument, not a wider scope, and
                # the fill difference says so without spending a second hue. The timing-line bar is
                # dotted: it is the difference between two of the solid scopes, not a scope itself.
                color=tint(base, bar.get("tint", 0.0)) if is_work else "white",
                edgecolor=(INK_MUTED if bar.get("hatch") else "white") if is_work else base,
                linewidth=2 if not bar.get("hatch") else 0.6,
                hatch=bar.get("hatch") if is_work else "///", zorder=2,
                xerr=[[err[0]], [err[1]]] if err else None,
                error_kw={"ecolor": INK_MUTED, "elinewidth": 1.2, "capsize": 3},
            )
            # Direct labels on every bar: the palette validator flags these hues as below 3:1
            # against the surface, and a visible label is the required relief.
            ax.text(
                uppers[slot] + widest * 0.025, pos, f"{value:,.0f} \u00b5s",
                va="center", ha="left", fontsize=9.5, color=INK,
            )

        ax.set_yticks(positions)
        ax.set_yticklabels([b["label"] for b in bars], fontsize=8.5)
        ax.set_xlim(0, widest * 1.32)
        ax.set_ylim(-0.6, len(scopes) - 0.4)
        ax.set_xlabel("microseconds per iteration", fontsize=9, color=INK_MUTED)

        # Coverage is timers / the CPU of WADM as deployed: the timed regions run in the deployable
        # build too, so that is the cost they are a share of.
        instrumented, cpu = values[0], values[1]
        coverage = f"{100 * instrumented / cpu:.0f}%" if cpu > 0 else "n/a"
        ax.set_title(
            f"{EDGES[edge].label}\ntimers see {coverage} of the CPU WADM adds"
            + (" as deployed" if entry["deployed_tier"] == "silent" else ""),
            fontsize=11.5, color=INK, loc="left", pad=8,
        )

        # Below the axis, where nothing competes with the bars or their value labels.
        notes = []
        origin = entry["series"].get("origin")
        if origin and origin.get("median") is not None:
            # Explained by the edge's trap mechanism, not by the sign. Envoy+WASM cannot answer from
            # the request phase and still proxies the trap's requests; the other three answer them
            # locally, so a POSITIVE origin delta there (Apache at 100/s) is not that mechanism and
            # must not be described as if it were.
            if edge in TRAP_PROXIES_TO_ORIGIN:
                direction = "this edge still proxies the trap's requests to the origin"
            elif origin["median"] < 0:
                direction = "WADM removes work from the origin (the trap answers 4 of 9 requests itself)"
            else:
                direction = "ROSE despite the trap answering 4 of 9 locally \u2014 not the trap's doing"
            notes.append(f"Origin CPU: {origin['median']:+,.0f} \u00b5s \u2014 {direction}")
        post = entry["series"].get("e2e_post")
        if post and post.get("median") is not None:
            reason = (
                "this edge's trap still pays the origin round-trip"
                if edge in TRAP_PROXIES_TO_ORIGIN else "the trap skips the origin round-trip"
            )
            notes.append(
                f"SQLi POSTs, end-to-end: {post['median']:+,.0f} \u00b5s \u2014 excluded above; {reason}"
            )
        if notes:
            ax.text(
                0.0, -0.30, "\n".join(notes), transform=ax.transAxes,
                fontsize=7.5, color=INK_MUTED, style="italic", va="top", ha="left",
            )

        ax.grid(True, which="major", axis="x", alpha=0.25, linewidth=0.8)
        ax.tick_params(colors=INK_MUTED, labelsize=9)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        for spine in ("left", "bottom"):
            ax.spines[spine].set_color("#d5d4cf")

    for index in range(len(present), rows * cols):
        axes[index // cols][index % cols].axis("off")

    fig.suptitle(
        f"Where WADM's cost is visible \u2014 {rate} iterations/s offered (\u2248{rate * 9:,} req/s)",
        fontsize=14, fontweight="bold", color=INK, x=0.01, ha="left",
    )
    fig.text(
        0.01, 0.005,
        "SOLID bars are measures of WORK and they nest: every timed region is CPU the container "
        "spent, and WADM as deployed (no benchmark timing lines) is contained in the\n"
        "instrumented build. The gap between the timers and the deployed cost is WADM work no timer "
        "wraps \u2014 Lua VM entry and exit, the body filter, alert rendering. The DOTTED bar\n"
        "is the difference between the two CPU scopes: what writing ~39 timing lines per iteration "
        "costs, i.e. the measurement's own overhead. CPU bars are marginal CPU per request\n"
        "(idle cost fitted out across all rates) \u00d7 requests per iteration, and NET of the proxy "
        "work the trap saves by answering 4 of 9 requests itself on three edges. The HATCHED\n"
        "bar is wall clock at the client, not work, shown for scale only. Error bars = range across "
        "paired replicates.",
        fontsize=7.5, color=INK_MUTED, style="italic", va="bottom", linespacing=1.5,
    )
    fig.tight_layout(rect=[0, 0.145, 1, 0.945])

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "cost_attribution.png"
    fig.savefig(path, dpi=150, facecolor="white")
    plt.close(fig)
    return path


def plot_coverage(data: dict[str, Any], rates: list[int], out_dir: Path) -> Path | None:
    """Instrumented share of the CPU WADM adds, per rate, with the range across replicates.

    Only the work-vs-work ratio is drawn. Dividing instrumented work by wall-clock latency (the
    former right-hand panel) mixes two instruments and parallelism across workers, and has no
    interpretation as a share of anything.
    """
    if len(rates) < 2:
        return None

    fig, ax = plt.subplots(figsize=(8.4, 5.4))
    edges = [e for e in EDGE_KEYS if data.get(e)]
    for i, edge in enumerate(edges):
        xs, ys, lows, highs = [], [], [], []
        for rate in rates:
            cov = ((data[edge].get(str(rate)) or {}).get("series") or {}).get("coverage") or {}
            if cov.get("median") is None:
                continue
            xs.append(rate * (1.06 ** (i - (len(edges) - 1) / 2)))
            ys.append(cov["median"])
            lows.append(cov.get("ci_low"))
            highs.append(cov.get("ci_high"))
        if not xs:
            continue
        color = COLORS[EDGES[edge].label]
        ax.plot(xs, ys, marker="o", markersize=8, linewidth=2, color=color,
                markeredgecolor="white", markeredgewidth=1.5, label=EDGES[edge].label, zorder=3)
        for x, lo, hi in zip(xs, lows, highs):
            if lo is not None:
                ax.plot([x, x], [lo, hi], color=color, linewidth=1.2, alpha=0.55, zorder=2)

    ax.set_xscale("log")
    ax.set_xticks(rates)
    ax.set_xticklabels([str(r) for r in rates])
    ax.minorticks_off()
    ax.set_xlim(rates[0] / 1.5, rates[-1] * 1.5)
    ax.set_xlabel("offered iterations per second", fontsize=9, color=INK_MUTED)
    ax.set_ylabel("instrumented share of edge CPU added (%)", fontsize=9, color=INK_MUTED)
    ax.set_ylim(bottom=0)
    _style_frame(ax)
    ax.legend(frameon=False, fontsize=9, loc="upper left")

    fig.suptitle("How much of WADM's CPU cost the detect/inject timers capture",
                 fontsize=13, fontweight="bold", color=INK, x=0.01, ha="left")
    fig.text(
        0.01, 0.01,
        "Per replicate: time inside every timed region per iteration \u00f7 edge CPU added per "
        "iteration (per-rate, paired). Point = median, bar = range across\n"
        "replicates. 100% would mean all of WADM's CPU cost happens inside a timed region. The "
        "denominator is net of the trap's saved proxy work and includes\n"
        "the timing-log writes, so this share is indicative; Apache at 100/s also carries crash "
        "recovery in its denominator.",
        fontsize=7.5, color=INK_MUTED, style="italic", va="bottom", linespacing=1.5,
    )
    fig.tight_layout(rect=[0, 0.11, 1, 0.93])

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "instrumented_coverage.png"
    fig.savefig(path, dpi=150, facecolor="white")
    plt.close(fig)
    return path


def main() -> int:
    results_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent / "results"
    )
    out_dir = results_dir / "plots"

    data: dict[str, Any] = {}
    marginal: dict[str, Any] = {}
    rates: set[int] = set()
    for edge in EDGE_KEYS:
        doc = load(results_dir, edge)
        if not doc:
            print(f"  ! no paired_{edge}.json — skipping {EDGES[edge].label}")
            continue
        per_rate = {}
        for rate in sorted({c["rate"] for c in doc["cells"]}):
            entry = scopes_for(doc, rate)
            if entry:
                per_rate[str(rate)] = entry
                rates.add(rate)
        if per_rate:
            data[edge] = per_rate
            tiers = {c["tier"] for c in doc["cells"]}
            deployed = "silent" if "silent" in tiers else "wadm"
            marginal[edge] = {
                "cpu_deployed": paired_marginal_cpu(doc, "bare", deployed),
                "cpu_instrumented": paired_marginal_cpu(doc, "bare", "wadm"),
                **({"cpu_probe": paired_marginal_cpu(doc, "silent", "wadm")}
                   if deployed == "silent" else {}),
            }

    if not data:
        print("Nothing to plot. Run run_paired_benchmark.py first.")
        return 1

    # The highest rate every edge has paired data at: CPU estimates are tightest there, and one
    # figure at one rate replaces three near-identical per-rate copies.
    common = [r for r in sorted(rates) if all(str(r) in data[e] for e in data)]
    if not common:
        print("No rate is shared by every edge; nothing to compare.")
        return 1
    rate = common[-1]
    attribution = {}
    for edge in data:
        if marginal[edge]["cpu_deployed"].get("n"):
            attribution[edge] = {str(rate): with_marginal_cpu(data[edge][str(rate)], marginal[edge])}
        else:
            # Apache's marginal fit is rejected as unphysical (crash recovery makes CPU per request
            # climb with rate), so it keeps the per-rate figure and says so.
            print(f"  ! {EDGES[edge].label}: no marginal CPU fit — per-rate CPU used at {rate}/s")
            attribution[edge] = {str(rate): data[edge][str(rate)]}

    written = [plot_attribution(rate, attribution, out_dir), plot_coverage(data, sorted(rates), out_dir)]
    for path in written:
        if path:
            print(f"  wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
