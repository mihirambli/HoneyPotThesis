#!/usr/bin/env python3
"""Compare the four honeypot edges from their internal benchmark results.

For each VU (virtual-user) level the benchmarks were run at, this produces one
figure that places the four edges side by side as box plots, with a detection
subplot and an injection subplot.

Data sources
------------
The benchmark scripts now write two files per edge into benchmarks/results/:

    internal_<edge>_profile.json  - summary stats (min / avg / p90 / max)
    internal_<edge>_raw.json      - raw per-request latencies (microseconds)

This script prefers the raw file and draws a *true* box plot (real quartiles:
Q1 / median / Q3 with 1.5*IQR whiskers). If an edge's raw file is missing, it
falls back to a *summary* box plot built from that edge's profile stats
(box = min->p90, center line = mean, whisker = max) and marks the box with "≈"
so the approximation is obvious. Re-run the benchmarks to replace any fallback
boxes with real ones.

Usage:
    python3 benchmarks/plot_edge_comparison.py [results_dir]
"""

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Display name -> file stem. Order here is the left-to-right box order.
EDGES = {
    "OpenResty": "internal_openresty",
    "WASM (Envoy)": "internal_wasm",
    "Apache+Lua": "internal_apache_lua",
    "Envoy+Lua": "internal_envoy_lua",
}

# Categorical slots 1-4 of the validated data-viz palette, assigned in fixed order and
# shared with plot_token_comparison.py so an edge keeps one colour across every figure.
COLORS = {
    "OpenResty": "#2a78d6",
    "WASM (Envoy)": "#eb6834",
    "Apache+Lua": "#1baf7a",
    "Envoy+Lua": "#eda100",
}

METRICS = ["detection", "injection"]


def load_edge(results_dir, stem):
    """Return (raw_by_vus, summary_by_vus) for one edge.

    raw_by_vus: {vus: {"detection": [..], "injection": [..]}} or None if no raw file.
    summary_by_vus: {vus: {"detection": stats, "injection": stats}} or None.
    """
    raw_by_vus = None
    raw_path = results_dir / f"{stem}_raw.json"
    if raw_path.exists():
        raw = json.loads(raw_path.read_text())
        raw_by_vus = {
            run["vus"]: {
                "detection": run.get("detection_us", []),
                "injection": run.get("injection_us", []),
            }
            for run in raw["runs"]
        }

    summary_by_vus = None
    profile_path = results_dir / f"{stem}_profile.json"
    if profile_path.exists():
        prof = json.loads(profile_path.read_text())
        summary_by_vus = {
            run["vus"]: {m: run[m] for m in METRICS} for run in prof["runs"]
        }

    return raw_by_vus, summary_by_vus


def collect(results_dir):
    """Load every edge and the union of VU levels seen."""
    data = {}
    vus_seen = set()
    for name, stem in EDGES.items():
        raw_by_vus, summary_by_vus = load_edge(results_dir, stem)
        if raw_by_vus is None and summary_by_vus is None:
            print(f"  ! skipping {name}: no result files found for '{stem}'")
            continue
        data[name] = {"raw": raw_by_vus, "summary": summary_by_vus}
        for src in (raw_by_vus, summary_by_vus):
            if src:
                vus_seen.update(src.keys())
    return data, sorted(vus_seen)


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


def plot_for_vus(vus, data, edges_present, out_dir):
    fig, axes = plt.subplots(1, len(METRICS), figsize=(6 * len(METRICS), 6))
    if len(METRICS) == 1:
        axes = [axes]

    any_fallback = False

    for ax, metric in zip(axes, METRICS):
        positions = []
        colors = []
        approx_bxp = []          # summary-based boxes drawn via ax.bxp
        approx_positions = []
        for i, name in enumerate(edges_present, start=1):
            edge = data[name]
            raw = edge["raw"].get(vus) if edge["raw"] else None
            samples = raw[metric] if raw else None

            if samples:
                bp = ax.boxplot(
                    samples,
                    positions=[i],
                    widths=0.6,
                    patch_artist=True,
                    showfliers=False,
                    medianprops={"color": "black", "linewidth": 2},
                )
                for patch in bp["boxes"]:
                    patch.set_facecolor(COLORS[name])
                    patch.set_alpha(0.75)
            else:
                # Fall back to the summary approximation for this edge.
                stats = (edge["summary"] or {}).get(vus)
                if not stats or stats[metric]["count"] == 0:
                    continue
                approx_bxp.append(summary_bxp_stats(stats[metric], name))
                approx_positions.append(i)
                colors.append(COLORS[name])
                any_fallback = True

            positions.append(i)

        if approx_bxp:
            bp = ax.bxp(
                approx_bxp,
                positions=approx_positions,
                showfliers=False,
                patch_artist=True,
                medianprops={"color": "black", "linewidth": 2, "linestyle": "--"},
                widths=0.6,
            )
            for patch, color in zip(bp["boxes"], colors):
                patch.set_facecolor(color)
                patch.set_alpha(0.4)
                patch.set_hatch("//")

        ax.set_title(f"{metric.capitalize()} latency")
        ax.set_ylabel("Latency (µs, log scale)")
        ax.set_yscale("log")
        ax.set_xticks(range(1, len(edges_present) + 1))
        ax.set_xticklabels(edges_present, rotation=15)
        ax.grid(True, which="both", axis="y", alpha=0.3)

    title = f"Edge latency comparison at {vus} VU(s)"
    fig.suptitle(title, fontsize=14, fontweight="bold")

    caption = "True box plots (box = Q1–Q3, line = median, whiskers = 1.5×IQR)."
    if any_fallback:
        caption += (
            "  Hatched/faded boxes are summary approximations "
            "(box = min→p90, line = mean, whisker = max) — re-run that edge's "
            "benchmark to get raw samples."
        )
    fig.text(0.5, 0.01, caption, ha="center", fontsize=8, style="italic", wrap=True)
    fig.tight_layout(rect=[0, 0.05, 1, 0.96])

    out = out_dir / f"edge_comparison_vus_{vus}.png"
    fig.savefig(out, dpi=150)
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

    print(f"Generating box plots for VU levels: {vus_list}")
    for vus in vus_list:
        plot_for_vus(vus, data, edges_present, out_dir)
    print(f"Done. {len(vus_list)} figure(s) in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
