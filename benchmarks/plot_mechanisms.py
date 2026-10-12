#!/usr/bin/env python3
"""
What each WADM mechanism costs on its own, and how injection scales with the size of the page.

The main paired dataset measures one mixed iteration: a priming GET, four GET probes and four SQLi
POSTs. cgroup CPU counters cannot tell request types apart, and on three edges the trap answers
its POSTs itself — saving the proxy work the bare tier does for them — so a single mixed CPU
figure nets WADM's cost against that saving. Two auxiliary runs separate them:

    get_<edge>.json    the same paired harness with WADM_MIX=get: only the priming GET and the four
                       GET probes. Its CPU per iteration is the GET path's cost (injection +
                       detection). The full mix minus this one is the trap's: what four POSTs cost
                       with the trap answering them, against the bare tier proxying them.
    size_<edge>.json   WADM_MIX=size: one GET each for pages of 1 KiB, 16 KiB, 256 KiB and 768 KiB.
                       Body injection buffers the whole response and scans it, so its cost should
                       grow with the body; the probe set only ever served ~1 KiB pages.

CPU per iteration here is net of idle: the edge's idle rate, measured in the k6 startTime delay
just before each window, times the window length, is subtracted before dividing by iterations.

Figures (results/plots/) and table (results/):
    mechanism_cpu.png   CPU added per request, GET path vs. trap path, per edge
    size_sweep.png      latency added per request vs. page size, per edge
    mechanisms.md       both, as numbers

Usage:
    python3 benchmarks/plot_mechanisms.py [results_dir]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

from analyze_paired import cell_by_replicate, paired_latency, tiers_of
from edge_specs import EDGE_KEYS, EDGES
from plot_common import COLORS, INK, INK_MUTED, _style_frame
from wadm_timings import paired_delta

# The probes in each mix (test.js), so per-request figures can divide by the right count.
GET_PATH_REQUESTS = 5  # the priming GET plus four GET probes
TRAP_REQUESTS = 4
SIZE_PROBES = [("size_1k", 1), ("size_16k", 16), ("size_256k", 256), ("size_768k", 768)]
US_PER_MS = 1000.0


def load(results_dir: Path, name: str) -> dict[str, Any] | None:
    path = results_dir / name
    return json.loads(path.read_text()) if path.exists() else None


def deployed_tier(doc: dict[str, Any]) -> str:
    return "silent" if "silent" in tiers_of(doc) else "wadm"


def net_cpu_per_iteration(cell: dict[str, Any]) -> float | None:
    """Edge CPU per iteration over the measured window, with the idle rate subtracted."""
    cpu = (cell.get("cpu") or {}).get("edge") or {}
    iterations = (cell.get("k6") or {}).get("iterations")
    total, window, idle = cpu.get("cpu_usec_total"), cpu.get("window_s"), cpu.get("idle_cpu_us_per_s")
    if total is None or not window or idle is None or not iterations:
        return None
    return (total - idle * window) / iterations


def paired_iteration_cpu(doc: dict[str, Any], rate: int, high: str) -> dict[int, float]:
    """{replicate: (high - bare) net CPU per iteration} at one rate."""
    bare, other = cell_by_replicate(doc, "bare", rate), cell_by_replicate(doc, high, rate)
    out = {}
    for rep in sorted(set(bare) & set(other)):
        b, h = net_cpu_per_iteration(bare[rep]), net_cpu_per_iteration(other[rep])
        if b is not None and h is not None:
            out[rep] = h - b
    return out


def mechanism_costs(full: dict[str, Any], get_only: dict[str, Any]) -> dict[str, Any] | None:
    """CPU added per request on the GET path and on the trap path, at the GET-only run's rate."""
    rate = max({c["rate"] for c in get_only["cells"]})
    if rate not in {c["rate"] for c in full["cells"]}:
        return None
    tier = deployed_tier(get_only)
    get_path = paired_iteration_cpu(get_only, rate, tier)
    all_path = paired_iteration_cpu(full, rate, tier)
    # Replicate i of one run against replicate i of the other: each is already a within-replicate
    # paired difference, so drift has cancelled inside each before they are combined.
    shared = sorted(set(get_path) & set(all_path))
    return {
        "rate": rate,
        "tier": tier,
        "get_path_per_request": paired_delta([get_path[r] / GET_PATH_REQUESTS for r in get_path]),
        "trap_path_per_request": paired_delta(
            [(all_path[r] - get_path[r]) / TRAP_REQUESTS for r in shared]
        ),
    }


def size_costs(doc: dict[str, Any]) -> dict[str, Any]:
    rate = max({c["rate"] for c in doc["cells"]})
    tier = deployed_tier(doc)
    return {
        "rate": rate,
        "tier": tier,
        "latency_added_us": {
            name: paired_latency(doc, rate, f"{name}_duration", "med", "bare", tier)
            for name, _ in SIZE_PROBES
        },
    }


def err(delta: dict[str, Any]) -> list[list[float]] | None:
    if not delta.get("ci_reliable") or delta.get("ci_low") is None:
        return None
    return [[delta["median"] - delta["ci_low"]], [delta["ci_high"] - delta["median"]]]


def plot_mechanism_cpu(costs: dict[str, Any], out_dir: Path) -> Path:
    edges = list(costs)
    fig, ax = plt.subplots(figsize=(9.6, 5.4))
    width = 0.36
    for i, edge in enumerate(edges):
        color = COLORS[EDGES[edge].label]
        for j, (key, hatch) in enumerate((("get_path_per_request", None),
                                          ("trap_path_per_request", "///"))):
            d = costs[edge][key]
            if d.get("median") is None:
                continue
            x = i + (j - 0.5) * width
            e = err(d)
            ax.bar(x, d["median"], width, color="white" if hatch else color,
                   edgecolor=color, linewidth=2, hatch=hatch, zorder=2,
                   yerr=e, error_kw={"ecolor": INK_MUTED, "elinewidth": 1.2, "capsize": 3})
            ax.text(x, max(d["median"], (d.get("ci_high") or d["median"])) + 3,
                    f"{d['median']:+.0f}", ha="center", va="bottom", fontsize=9, color=INK)
    ax.axhline(0, color=INK_MUTED, linewidth=1)
    ax.set_xticks(range(len(edges)))
    ax.set_xticklabels([EDGES[e].label for e in edges])
    ax.set_ylabel("edge CPU added per request (µs, net of idle)", fontsize=9, color=INK_MUTED)
    _style_frame(ax)
    sample = next(iter(costs.values()))
    ax.set_title(
        f"What each mechanism costs in CPU — {sample['rate']} iterations/s, "
        f"{'WADM as deployed (silent)' if sample['tier'] == 'silent' else 'instrumented WADM'} − bare",
        fontsize=12, color=INK, loc="left",
    )
    handles = [
        Line2D([], [], color=INK_MUTED, linewidth=8, label="GET path: injection + detection, per GET"),
        plt.Rectangle((0, 0), 1, 1, facecolor="white", edgecolor=INK_MUTED, hatch="///",
                      label="SQLi trap path, per POST (trap answers vs. bare proxies)"),
    ]
    ax.legend(handles=handles, frameon=False, fontsize=9, loc="upper left")
    fig.text(
        0.01, 0.01,
        "GET path = (GET-only run, WADM − bare) ÷ 5 requests. Trap path = (full mix − GET-only) ÷ 4 "
        "POSTs; negative means the trap answering a POST\nitself costs the edge less than "
        "proxying it to the origin. Bars = median over replicates, whiskers = range.",
        fontsize=7.5, color=INK_MUTED, style="italic", va="bottom", linespacing=1.5,
    )
    fig.tight_layout(rect=[0, 0.08, 1, 1])
    out = out_dir / "mechanism_cpu.png"
    fig.savefig(out, dpi=150, facecolor="white")
    plt.close(fig)
    return out


def plot_size_sweep(sizes: dict[str, Any], out_dir: Path) -> Path:
    fig, ax = plt.subplots(figsize=(8.6, 5.4))
    for edge, data in sizes.items():
        color = COLORS[EDGES[edge].label]
        xs, ys, lo, hi = [], [], [], []
        for name, kib in SIZE_PROBES:
            d = data["latency_added_us"][name]
            if d.get("median") is None:
                continue
            xs.append(kib)
            ys.append(d["median"] * US_PER_MS)
            lo.append((d.get("ci_low") or d["median"]) * US_PER_MS)
            hi.append((d.get("ci_high") or d["median"]) * US_PER_MS)
        if not xs:
            continue
        ax.plot(xs, ys, color=color, marker="o", markersize=8, linewidth=2,
                markeredgecolor="white", markeredgewidth=1.5, label=EDGES[edge].label, zorder=3)
        for x, a, b in zip(xs, lo, hi):
            ax.plot([x, x], [a, b], color=color, linewidth=1.2, alpha=0.55, zorder=2)
    ax.axhline(0, color=INK_MUTED, linewidth=1)
    ax.set_xscale("log")
    ax.set_xticks([k for _, k in SIZE_PROBES])
    ax.set_xticklabels([f"{k} KiB" for _, k in SIZE_PROBES])
    ax.minorticks_off()
    ax.set_xlabel("page size", fontsize=9, color=INK_MUTED)
    ax.set_ylabel("latency added per request (µs, median)", fontsize=9, color=INK_MUTED)
    _style_frame(ax)
    ax.legend(frameon=False, fontsize=9, loc="upper left")
    sample = next(iter(sizes.values()))
    ax.set_title(f"Injection cost grows with the page — {sample['rate']} iterations/s",
                 fontsize=12, color=INK, loc="left")
    fig.text(
        0.01, 0.01,
        "Each point is the paired (WADM − bare) median latency for one page size, median over "
        "replicates; bars are the range. Every edge buffers the whole\nbody before injecting, so "
        "the client waits for the last byte before the first. Pages above Envoy's 1 MiB buffer "
        "are a functional failure (functional_matrix.md).",
        fontsize=7.5, color=INK_MUTED, style="italic", va="bottom", linespacing=1.5,
    )
    fig.tight_layout(rect=[0, 0.08, 1, 1])
    out = out_dir / "size_sweep.png"
    fig.savefig(out, dpi=150, facecolor="white")
    plt.close(fig)
    return out


def write_table(costs: dict[str, Any], sizes: dict[str, Any], results_dir: Path) -> Path:
    def fmt(d: dict[str, Any] | None, scale: float = 1.0) -> str:
        if not d or d.get("median") is None:
            return "—"
        out = f"{d['median'] * scale:+.0f}"
        if d.get("ci_reliable"):
            out += f" ({d['ci_low'] * scale:+.0f}..{d['ci_high'] * scale:+.0f})"
        return out

    lines = ["# WADM cost per mechanism and per page size", "",
             "Generated by `benchmarks/plot_mechanisms.py`. Values are median (range) over paired "
             "replicates, WADM tier − bare tier.", ""]
    if costs:
        lines += ["## CPU per request, by mechanism (µs, net of idle)", "",
                  "| Edge | GET path, per GET | SQLi trap path, per POST |", "|---|---|---|"]
        for edge, c in costs.items():
            lines.append(f"| {EDGES[edge].label} | {fmt(c['get_path_per_request'])} | "
                         f"{fmt(c['trap_path_per_request'])} |")
        lines.append("")
    if sizes:
        lines += ["## Latency added per request, by page size (µs)", "",
                  "| Edge | " + " | ".join(f"{k} KiB" for _, k in SIZE_PROBES) + " |",
                  "|---|" + "---|" * len(SIZE_PROBES)]
        for edge, s in sizes.items():
            lines.append(f"| {EDGES[edge].label} | " + " | ".join(
                fmt(s["latency_added_us"][n], US_PER_MS) for n, _ in SIZE_PROBES) + " |")
        lines.append("")
    path = results_dir / "mechanisms.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    results_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent / "results"
    )
    out_dir = results_dir / "plots"
    out_dir.mkdir(parents=True, exist_ok=True)

    costs, sizes = {}, {}
    for edge in EDGE_KEYS:
        full, get_only = load(results_dir, f"paired_{edge}.json"), load(results_dir, f"get_{edge}.json")
        if full and get_only:
            result = mechanism_costs(full, get_only)
            if result:
                costs[edge] = result
        size_doc = load(results_dir, f"size_{edge}.json")
        if size_doc:
            sizes[edge] = size_costs(size_doc)

    written = []
    if costs:
        written.append(plot_mechanism_cpu(costs, out_dir))
    if sizes:
        written.append(plot_size_sweep(sizes, out_dir))
    if not written:
        print("Nothing to plot: run run_paired_benchmark.py with --mix get and --mix size first.")
        return 1
    written.append(write_table(costs, sizes, results_dir))
    for path in written:
        print(f"  wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
