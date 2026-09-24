#!/usr/bin/env python3
"""Compare the SQLi trap's scan cost across outcome and payload encoding, per edge.

`sql_injection` is the only WADM feature with a meaningful non-triggering path that still does
full work, and the only one measured in a single phase: it plants nothing, so there is no
injection region to time. That makes it unplottable by the per-kind figures alone, which pair a
detection panel with an injection panel, and it is why this script exists.

What the four arms measure
--------------------------
Two factors are crossed, because measuring only hit-vs-miss confounds them.

**Outcome.** `sqli_match` is a linear scan that returns on first match, walking
`watch_fields -> body pairs -> signatures` in config order — an ordering that is part of the
cross-edge contract, so all four edges resolve "first match wins" identically. The hit payload
matches `admin'`, #11 of the 22 signatures, in the first watch field, so the scan stops after 11
comparisons; a non-matching body walks all 22 against `username` and then all 22
against `password`, 44 comparisons.

That 33-comparison difference costs nothing measurable on three of the four edges. Most signatures
are *longer* than a real field value — `information_schema`, `union all select`, `waitfor delay`
against `alice` — so they are rejected on length before a character is compared, and a JIT compiles
what remains down to noise.

Apache is the exception, and consistently so: ~2.5 µs, in the direction the scan-depth hypothesis
predicts. Its `mod_lua` links `liblua.so.5` — standard PUC Lua — while OpenResty and Envoy both run
LuaJIT and the WASM filter is compiled Rust. Scan depth is therefore measurable only where the
matching loop is genuinely interpreted.

**Encoding.** Whether the body needs percent-decoding. This is where the time actually goes:
`url_decode`'s substitution path runs over every body pair and again inside `sqli_normalize`, so
an encoded payload pays it twice. The `%27` form of each payload decodes to exactly the plain
form, so the two arms of one outcome scan identical bytes and differ only in decoding work.

Neither path authenticates anything: the origin has no `/api/login` route, and the edge fabricates
both the fake MySQL error and the canned 401. A non-matching request is exactly the cost of
checking the body against the signature list, and nothing else.

The practical reading is that SQLi detection cost is driven by input normalisation, not by how
many signatures are configured — so growing the signature list is close to free, while an attacker
who percent-encodes their payload makes the honeypot work measurably harder.

All four arms share one timed-region boundary (the scan alone, with page rendering and the
attacker-IP record outside it on every edge), so no arm is diluted by work only it performs.

Data sources (per edge, written by run_internal_<edge>_benchmark.py):

    internal_<edge>_raw.json      raw per-request latencies -> true box plots
    internal_<edge>_profile.json  summary stats -> fallback approximation, hatched

Figures produced into <results_dir>/plots/:

    sqli_arms_vus_<N>.png          four edges side by side at a fixed VU level, four boxes each
    sqli_arms_scaling.png          median scan cost vs. VU level, one panel per arm
    sqli_e2e_overhead_vus_<N>.png  what the trap changes end to end, against the no-WADM baseline

Usage:
    python3 benchmarks/plot_sqli_comparison.py [results_dir]
"""

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.patches import Patch

from plot_common import (
    COLORS,
    E2E_METRIC_LABELS,
    INK,
    INK_MUTED,
    SQLI_E2E_METRICS,
    SQLI_ARM_ENCODED,
    SQLI_ARM_LABELS,
    SQLI_ARMS,
    add_headroom,
    collect,
    finalize_flat_boxes,
    place_direct_labels,
    collect_e2e,
    e2e_stats,
    quartiles,
    samples_for,
    stats_for,
    style_axis,
    style_axis_ms,
    summary_bxp_stats,
)

PHASE = "detect"

# Encoding is the factor that moves the number, so it gets the fill — solid = needs decoding —
# matching the tier convention in the baseline figures. Hue stays with the edge across every
# figure in this repo, so it can never also mean the arm. Outcome is read off the x position:
# the four slots are ordered hit-plain, hit-encoded, nomatch-plain, nomatch-encoded, so the two
# encoded slots sit second and fourth and the effect shows as an alternating pattern.
ARM_SOLID = SQLI_ARM_ENCODED
ARM_OFFSET = {arm: -0.27 + 0.18 * i for i, arm in enumerate(SQLI_ARMS)}
ARM_WIDTH = 0.16


def draw_arm_box(ax, position, color, arm, samples, stats):
    """One arm's box at `position`, preferring raw samples over the summary approximation.

    Returns (used_fallback, flat_box | None); flat boxes are finalised by the caller once the
    axis limits are settled, as elsewhere in this repo.
    """
    solid = ARM_SOLID[arm]
    if samples:
        q1, med, q3 = quartiles(samples)
        bp = ax.boxplot(
            [samples],
            positions=[position],
            widths=ARM_WIDTH,
            showfliers=False,
            patch_artist=True,
            medianprops={"color": INK if solid else color, "linewidth": 2},
        )
        used_fallback = False
    elif stats:
        bp = ax.bxp(
            [summary_bxp_stats(stats, "")],
            positions=[position],
            widths=ARM_WIDTH,
            showfliers=False,
            patch_artist=True,
            medianprops={"color": INK, "linewidth": 2, "linestyle": "--"},
        )
        q1 = q3 = med = None
        used_fallback = True
    else:
        return False, None

    for patch in bp["boxes"]:
        patch.set_edgecolor(color)
        patch.set_linewidth(1.6)
        patch.set_facecolor(color if solid else "white")
        if used_fallback:
            patch.set_alpha(0.45)
            patch.set_hatch("//")
    for key in ("whiskers", "caps"):
        for artist in bp[key]:
            artist.set_color(color)

    flat = (position, med, color) if (q1 is not None and q1 == q3) else None
    return used_fallback, flat


def plot_for_vus(vus, data, edges_present, out_dir):
    fig, ax = plt.subplots(figsize=(11.5, 6))
    any_fallback = False
    flat_boxes = []

    for i, name in enumerate(edges_present, start=1):
        for arm in SQLI_ARMS:
            fallback, flat = draw_arm_box(
                ax,
                i + ARM_OFFSET[arm],
                COLORS[name],
                arm,
                samples_for(data[name], vus, arm, PHASE),
                stats_for(data[name], vus, arm, PHASE),
            )
            any_fallback |= fallback
            if flat:
                flat_boxes.append(flat)

    ax.set_xlim(0.4, len(edges_present) + 0.6)
    ax.set_xticks(range(1, len(edges_present) + 1))
    ax.set_xticklabels(edges_present, rotation=15)
    ax.set_ylabel("Signature-scan latency (µs, symlog)", fontsize=10, color=INK)
    style_axis(ax)
    add_headroom(ax)
    finalize_flat_boxes(ax, flat_boxes, width=ARM_WIDTH)

    handles = [
        Patch(facecolor="white", edgecolor=INK_MUTED, label="plain body"),
        Patch(facecolor=INK_MUTED, edgecolor=INK_MUTED, label="percent-encoded body"),
    ]
    fig.legend(
        handles=handles,
        loc="lower center",
        ncol=2,
        frameon=False,
        fontsize=9,
        bbox_to_anchor=(0.5, 0.135),
    )

    fig.suptitle(
        f"SQLi scan cost: outcome x payload encoding at {vus} VU(s)",
        fontsize=14,
        fontweight="bold",
        color=INK,
    )
    # Explicit breaks rather than wrap=True, which measures against the figure rather than the
    # axes and overflows the canvas at this width.
    caption = (
        "Four boxes per edge: hit-plain, hit-encoded, nomatch-plain, nomatch-encoded.\n"
        "Outcome (11 vs. 44 comparisons) moves the median only on Apache, the one edge whose\n"
        "Lua is interpreted rather than JIT-compiled. Decoding moves it on every edge but WASM.\n"
        "Box = Q1–Q3, line = median, whiskers = 1.5×IQR; symlog y (linear below 1 µs)."
    )
    if any_fallback:
        caption += (
            "\nHatched boxes are summary approximations (box = min→p90, line = mean, "
            "whisker = max) — re-run that edge's benchmark for raw samples."
        )
    fig.text(0.5, 0.025, caption, ha="center", fontsize=8, style="italic", color=INK_MUTED)
    fig.tight_layout(rect=[0, 0.2, 1, 0.94])

    out = out_dir / f"sqli_arms_vus_{vus}.png"
    fig.savefig(out, dpi=150, facecolor="white")
    plt.close(fig)
    print(f"  wrote {out}")


def plot_e2e_overhead_for_vus(vus, e2e, edges_present, out_dir):
    """What the trap costs end to end, against the same edge with no WADM.

    Kept out of the phase-overhead figure because the sign is the opposite of every other
    request in the iteration. The trap terminates `POST /api/login` in the request phase, so
    OpenResty, Envoy+Lua and Apache never contact the origin — WADM *removes* a round-trip the
    bare tier pays, and the bars go down. That is the result here, rather than a distraction
    from the injection costs the GET figure is about.

    Envoy+WASM is the exception and is not comparable to the other three on this plane: proxy-wasm
    rejects generating a response from the body callback, so it forwards to the origin and
    rewrites the reply, paying the hop the others skip.

    Plots `wadm − bare` rather than the WADM latency, for the same reason the phase-overhead figure
    does and one more: the four arms occupy fixed, different positions in the k6 iteration, and the
    bare tier alone shows latency falling ~30% from the iteration's first request to its last. The
    subtraction cancels that gradient because both tiers share the ordering.
    """
    present = [n for n in edges_present if n in e2e]
    if not present:
        return
    fig, ax = plt.subplots(figsize=(3.4 * len(SQLI_E2E_METRICS), 6.4))
    width = 0.19

    for slot, name in enumerate(present):
        offset = (slot - (len(present) - 1) / 2) * width
        xs, heights = [], []
        for i, metric in enumerate(SQLI_E2E_METRICS, start=1):
            wadm = e2e_stats(e2e[name].get("wadm"), vus, metric)
            bare = e2e_stats(e2e[name].get("bare"), vus, metric)
            if not wadm or not bare:
                continue
            xs.append(i + offset)
            heights.append(wadm["med"] - bare["med"])
        if xs:
            ax.bar(xs, heights, width=width, color=COLORS[name], alpha=0.9,
                   edgecolor="white", linewidth=2, zorder=2)

    ax.axhline(0, color="#d5d4cf", linewidth=1, zorder=1)
    ax.set_xlim(0.45, len(SQLI_E2E_METRICS) + 0.55)
    ax.set_xticks(range(1, len(SQLI_E2E_METRICS) + 1))
    ax.set_xticklabels([E2E_METRIC_LABELS[m] for m in SQLI_E2E_METRICS], fontsize=8.5)
    style_axis_ms(ax, scale="linear", from_zero=False)
    low, high = ax.get_ylim()
    ax.set_ylim(low * 1.15 if low < 0 else 0, high * 1.15 if high > 0 else 0)
    ax.set_ylabel("Latency change from WADM (ms, median WADM − median bare)",
                  fontsize=10, color=INK)

    fig.legend(
        handles=[Patch(facecolor=COLORS[n], edgecolor="white", linewidth=2, label=n)
                 for n in present],
        loc="lower center",
        ncol=len(present),
        frameon=False,
        fontsize=9,
        bbox_to_anchor=(0.5, 0.125),
    )
    fig.suptitle(
        f"What the SQLi trap changes end to end at {vus} VU(s)",
        fontsize=13.5,
        fontweight="bold",
        color=INK,
    )
    caption = (
        "Below zero means WADM made the request faster. The trap answers POST /api/login itself,\n"
        "so OpenResty, Envoy+Lua and Apache skip the origin round-trip the no-WADM baseline pays.\n"
        "Envoy+WASM cannot answer from the request phase and still contacts the origin, so it is\n"
        "not comparable to the other three here — see docs/EDGE_LEVELING.md.\n"
        "These are wadm−bare deltas: the four arms sit at different positions in the iteration, and\n"
        "latency falls ~30% from its first request to its last even with WADM absent. The\n"
        "subtraction cancels that gradient; absolute per-request figures would not."
    )
    fig.text(0.5, 0.025, caption, ha="center", fontsize=8, style="italic", color=INK_MUTED)
    fig.tight_layout(rect=[0, 0.2, 1, 0.93])

    out = out_dir / f"sqli_e2e_overhead_vus_{vus}.png"
    fig.savefig(out, dpi=150, facecolor="white")
    plt.close(fig)
    print(f"  wrote {out}")


def plot_scaling(data, edges_present, vus_list, out_dir):
    """Median scan cost vs. VU level, one panel per arm, IQR shaded."""
    fig, axes = plt.subplots(1, len(SQLI_ARMS), figsize=(3.9 * len(SQLI_ARMS), 4.8), sharey=True)
    x = range(len(vus_list))
    endpoints_per_panel = {}

    for col, arm in enumerate(SQLI_ARMS):
        ax = axes[col]
        endpoints = []
        for name in edges_present:
            edge = data[name]
            medians, lows, highs, xs = [], [], [], []
            for xi, vus in zip(x, vus_list):
                values = samples_for(edge, vus, arm, PHASE)
                if values:
                    q1, med, q3 = quartiles(values)
                else:
                    stats = stats_for(edge, vus, arm, PHASE)
                    if not stats:
                        continue
                    q1, med, q3 = stats["min_us"], stats["avg_us"], stats["p90_us"]
                xs.append(xi)
                medians.append(med)
                lows.append(q1)
                highs.append(q3)
            if not xs:
                continue
            ax.fill_between(xs, lows, highs, color=COLORS[name], alpha=0.14, linewidth=0)
            ax.plot(xs, medians, color=COLORS[name], linewidth=2, marker="o", markersize=5)
            endpoints.append((name, xs[-1], medians[-1]))

        endpoints_per_panel[col] = endpoints
        ax.set_title(SQLI_ARM_LABELS[arm].replace("\n", ", "), fontsize=10.5, color=INK, pad=10)
        ax.set_xticks(list(x))
        ax.set_xticklabels([str(v) for v in vus_list], fontsize=9)
        ax.set_xlabel("VUs", fontsize=9, color=INK_MUTED)
        ax.set_xlim(-0.35, len(vus_list) - 1 + 1.5)
        style_axis(ax)
        if col == 0:
            ax.set_ylabel("Signature-scan latency (µs, symlog)", fontsize=10, color=INK)

    add_headroom(axes[0])  # sharey propagates to the rest
    for col, endpoints in endpoints_per_panel.items():
        place_direct_labels(axes[col], endpoints)

    fig.suptitle(
        "SQLi signature-scan cost vs. load, by outcome and payload encoding",
        fontsize=14,
        fontweight="bold",
        color=INK,
    )
    fig.text(
        0.5,
        0.015,
        "Line = median, band = Q1–Q3. Each edge is direct-labelled at its right-hand end. "
        "Y axis is shared between panels and symlog (linear below 1 µs, logarithmic above).",
        ha="center",
        fontsize=8,
        style="italic",
        color=INK_MUTED,
    )
    fig.tight_layout(rect=[0, 0.06, 1, 0.92])

    out = out_dir / "sqli_arms_scaling.png"
    fig.savefig(out, dpi=150, facecolor="white")
    plt.close(fig)
    print(f"  wrote {out}")


def main():
    results_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent / "results"
    )
    if not results_dir.is_dir():
        print(f"Error: results directory not found: {results_dir}", file=sys.stderr)
        return 2

    data, vus_list = collect(results_dir)
    if not data:
        print("Error: no result files found.", file=sys.stderr)
        return 1

    # An edge whose files predate the SQLi instrumentation has no samples for either arm; drawing
    # it as an empty slot would read as "measured, and zero".
    edges_present = [
        name
        for name in data
        if any(
            samples_for(data[name], v, arm, PHASE) or stats_for(data[name], v, arm, PHASE)
            for v in vus_list
            for arm in SQLI_ARMS
        )
    ]
    if not edges_present:
        print(
            "Error: no sql_injection samples in any result file — re-run the internal "
            "benchmarks with the SQLi probes enabled.",
            file=sys.stderr,
        )
        return 1

    skipped = [name for name in data if name not in edges_present]
    for name in skipped:
        print(f"  ! skipping {name}: no sql_injection samples")

    out_dir = results_dir / "plots"
    out_dir.mkdir(parents=True, exist_ok=True)

    # The end-to-end plane needs the bare tier, which lives in its own result files and may not
    # have been re-run; its absence skips that figure rather than failing the microsecond ones.
    e2e, _origin, _e2e_vus = collect_e2e(results_dir)

    for vus in vus_list:
        plot_for_vus(vus, data, edges_present, out_dir)
        plot_e2e_overhead_for_vus(vus, e2e, edges_present, out_dir)
    plot_scaling(data, edges_present, vus_list, out_dir)

    print(f"Done. Figures in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
