"""
Generate comparison bar charts for WADM edge benchmark results.
Run with: python3 benchmarks/plot_results.py
Output:   benchmarks/results/benchmark_comparison.png
"""

import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

# ── Raw results ───────────────────────────────────────────────────────────────
# All values in milliseconds.
# Envoy+Lua min values are 0 due to startup failures in that run; marked with
# a note in the chart rather than silently showing 0.

edges = ["OpenResty", "Envoy+Lua*", "Envoy+WASM", "Apache"]

detection = {
    "min":  [0.668, 0.0,   0.830, 0.269],
    "avg":  [2.62,  2.91,  2.99,  2.60 ],
    "p(90)":[4.40,  5.03,  5.82,  4.42 ],
    "max":  [11.18, 13.61, 8.14,  12.97],
}

injection = {
    "min":  [0.503, 0.0,   1.010, 0.210],
    "avg":  [2.58,  3.60,  4.25,  2.83 ],
    "p(90)":[4.33,  6.57,  7.53,  3.69 ],
    "max":  [10.02, 11.34, 20.69, 25.79],
}

metrics   = ["min", "avg", "p(90)", "max"]
colors    = ["#4CAF50", "#2196F3", "#FF9800", "#F44336"]   # green, blue, orange, red
bar_width = 0.18
x         = np.arange(len(edges))

os.makedirs(os.path.join(os.path.dirname(__file__), "results"), exist_ok=True)
out_path = os.path.join(os.path.dirname(__file__), "results", "benchmark_comparison.png")

# ── Figure layout ─────────────────────────────────────────────────────────────
fig, axes = plt.subplots(1, 2, figsize=(16, 7), sharey=False)
fig.suptitle(
    "WADM Edge Proxy Benchmark — Request Latency (ms)\n5 VUs · 30 s · Docker bridge network",
    fontsize=14, fontweight="bold", y=1.01,
)

def draw_panel(ax, data, title):
    for i, (metric, color) in enumerate(zip(metrics, colors)):
        offset  = (i - (len(metrics) - 1) / 2) * bar_width
        bars    = ax.bar(x + offset, data[metric], bar_width,
                         label=metric, color=color, alpha=0.85, edgecolor="white")
        # Value labels on top of each bar
        for bar in bars:
            h = bar.get_height()
            if h == 0:
                ax.text(bar.get_x() + bar.get_width() / 2, 0.3, "—",
                        ha="center", va="bottom", fontsize=7.5, color="#888")
            else:
                ax.text(bar.get_x() + bar.get_width() / 2, h + 0.15,
                        f"{h:.2f}", ha="center", va="bottom", fontsize=7.5)

    ax.set_title(title, fontsize=12, fontweight="bold", pad=10)
    ax.set_ylabel("Latency (ms)", fontsize=10)
    ax.set_xticks(x)
    ax.set_xticklabels(edges, fontsize=10)
    ax.legend(title="Metric", fontsize=9, title_fontsize=9)
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    ax.set_ylim(0, max(max(data[m]) for m in metrics) * 1.18)
    ax.spines[["top", "right"]].set_visible(False)

draw_panel(axes[0], detection, "Detection Phase (detect_query_duration)\nGET /api/login?password=<keyword>")
draw_panel(axes[1], injection, "Injection Phase (inject_get_duration)\nGET /")

# Footnote for tainted Envoy+Lua run
fig.text(
    0.5, -0.02,
    "* Envoy+Lua run had 10 startup failures (min=0 ms excluded from analysis). "
    "Re-run recommended for final comparison.",
    ha="center", fontsize=8, color="#666", style="italic",
)

plt.tight_layout()
plt.savefig(out_path, dpi=150, bbox_inches="tight")
print(f"Saved: {out_path}")
