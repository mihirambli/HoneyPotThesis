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

# Lightest to darkest within one edge's hue. The scopes are ordered — each is a wider accounting
# boundary than the last — so this is a sequential encoding, one hue light to dark, rather than
# four categorical colours that would compete with the edge hue carrying identity across figures.
SCOPE_TINTS = [0.55, 0.28, 0.0]
SCOPE_LABELS = [
    "Instrumented\ndetect + inject timers",
    "Edge CPU added\n(marginal, cgroup cpu.stat)",
    "End-to-end added\n(4 GET probes, wall clock)",
]

# Instrumented time and CPU time are both measures of WORK, and they nest: every timed region is
# CPU the container spent, so instrumented / CPU is a true subset ratio and is the headline.
#
# End-to-end latency is NOT in that chain. It is wall clock at the client, and a container with
# several workers on two pinned cores can burn more than one microsecond of CPU per microsecond of
# elapsed time — measured here, Apache added 4,835 us of CPU while adding 1,565 us of latency.
# Drawing it as a third nested scope would assert a containment that parallelism makes false, so
# it is drawn as a separate instrument and labelled as one.
WORK_SCOPES = 2

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
    if not k6.get("iterations") or not k6.get("http_reqs"):
        return None
    return k6["http_reqs"] / k6["iterations"]


def trend_med(cell: dict[str, Any], metric: str) -> float | None:
    stats = ((cell.get("k6") or {}).get("trends") or {}).get(metric)
    return stats.get("med") if stats and stats.get("count") else None


def scopes_for(doc: dict[str, Any], rate: int) -> dict[str, Any] | None:
    """The four scopes at one rate, each as a paired-delta summary in microseconds per iteration."""
    bare, wadm = valid(doc, "bare", rate), valid(doc, "wadm", rate)
    shared = sorted(set(bare) & set(wadm))
    if not shared:
        return None

    instrumented, edge_cpu, origin_cpu, e2e_get, e2e_post, coverage = [], [], [], [], [], []
    for replicate in shared:
        b, w = bare[replicate], wadm[replicate]

        value = instrumented_us_per_iteration(w)
        if value is not None:
            instrumented.append(value)

        per_iter = reqs_per_iteration(w)
        if per_iter:
            b_edge = ((b.get("cpu") or {}).get("edge") or {}).get("cpu_us_per_request")
            w_edge = ((w.get("cpu") or {}).get("edge") or {}).get("cpu_us_per_request")
            if b_edge is not None and w_edge is not None:
                edge_cpu.append((w_edge - b_edge) * per_iter)
                if value is not None and w_edge > b_edge:
                    coverage.append(100.0 * value / ((w_edge - b_edge) * per_iter))
            b_org = ((b.get("cpu") or {}).get("origin") or {}).get("cpu_us_per_request")
            w_org = ((w.get("cpu") or {}).get("origin") or {}).get("cpu_us_per_request")
            if b_org is not None and w_org is not None:
                origin_cpu.append((w_org - b_org) * per_iter)

        for metrics, sink in ((GET_METRICS, e2e_get), (POST_METRICS, e2e_post)):
            deltas = [
                trend_med(w, m) - trend_med(b, m)
                for m in metrics
                if trend_med(w, m) is not None and trend_med(b, m) is not None
            ]
            if len(deltas) == len(metrics):
                sink.append(sum(deltas) * 1000.0)

    return {
        "scopes": [
            paired_delta(instrumented),
            paired_delta(edge_cpu),
            paired_delta(e2e_get),
        ],
        "origin_cpu": paired_delta(origin_cpu),
        "post": paired_delta(e2e_post),
        "coverage_pct": paired_delta(coverage),
        "reqs_per_iteration": statistics.median(
            [r for r in (reqs_per_iteration(wadm[rep]) for rep in shared) if r]
        ),
        "replicates": len(shared),
    }


def with_marginal_cpu(entry: dict[str, Any], marginal: dict[str, Any]) -> dict[str, Any]:
    """Swap the per-rate CPU scope for the marginal fit, scaled to one iteration.

    A per-rate CPU-per-request divides the edge's whole consumption, idle timers included, by the
    requests served, so at low rates it is mostly idle cost and its range spans hundreds of µs. The
    marginal fit separates idle from per-request cost across every rate within each replicate,
    which is the figure that answers "what does one more request cost".
    """
    scale = entry["reqs_per_iteration"]
    scaled = paired_delta([v * scale for v in marginal.get("samples") or []])
    return {**entry, "scopes": [entry["scopes"][0], scaled, entry["scopes"][2]], "cpu_marginal": True}


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
    fig, axes = plt.subplots(rows, cols, figsize=(7.6 * cols, 4.9 * rows), squeeze=False)

    for index, edge in enumerate(present):
        ax = axes[index // cols][index % cols]
        entry = data[edge][str(rate)]
        scopes = entry["scopes"]
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

        for slot, (pos, value, scope, amount) in enumerate(
            zip(positions, values, scopes, SCOPE_TINTS)
        ):
            err = err_from(scope)
            is_work = slot < WORK_SCOPES
            ax.barh(
                pos, value, height=0.58,
                # Hollow for the latency bar: it is a different instrument, not a wider scope, and
                # the fill difference says so without spending a second hue.
                color=tint(base, amount) if is_work else "white",
                edgecolor="white" if is_work else base,
                linewidth=2, hatch=None if is_work else "///", zorder=2,
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
        labels = list(SCOPE_LABELS)
        if not entry.get("cpu_marginal"):
            labels[1] = "Edge CPU added\n(per-rate, cgroup cpu.stat)"
        ax.set_yticklabels(labels, fontsize=8.5)
        ax.set_xlim(0, widest * 1.32)
        ax.set_ylim(-0.6, len(scopes) - 0.4)
        ax.set_xlabel("microseconds per iteration", fontsize=9, color=INK_MUTED)

        instrumented, cpu = values[0], values[1]
        coverage = f"{100 * instrumented / cpu:.0f}%" if cpu > 0 else "n/a"
        ax.set_title(
            f"{EDGES[edge].label}\ntimers see {coverage} of the CPU WADM burns",
            fontsize=11.5, color=INK, loc="left", pad=8,
        )

        # Below the axis, where nothing competes with the bars or their value labels.
        notes = []
        origin = entry.get("origin_cpu")
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
        post = entry.get("post")
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
        "The two SOLID bars are both measures of WORK and they nest: every timed region is CPU the "
        "container spent. The gap between them is WADM work\n"
        "no timer wraps \u2014 Lua VM entry and exit per phase, the response body filter, alert "
        "rendering \u2014 AND the writing of the ~39 timing log lines per\n"
        "iteration that only the WADM tier emits, so part of the gap is the instrumentation itself. "
        "The CPU bar is the marginal CPU per request (idle cost\n"
        "fitted out across all rates) \u00d7 requests per iteration, and it is NET: the trap answers "
        "4 of 9 requests itself on three edges, saving proxy work.\n"
        "The HATCHED bar is a different instrument \u2014 wall clock at the client, not work \u2014 "
        "shown for scale only; several workers on two cores can burn more\n"
        "than one CPU-\u00b5s per elapsed \u00b5s. Error bars = range across paired replicates.",
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
            cov = (data[edge].get(str(rate)) or {}).get("coverage_pct") or {}
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
            marginal[edge] = paired_marginal_cpu(doc)

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
        if marginal[edge].get("n"):
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
