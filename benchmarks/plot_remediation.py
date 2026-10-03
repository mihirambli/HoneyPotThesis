#!/usr/bin/env python3
"""
Apache's LuaScope remediation, reported on its own: what it fixes and what it costs.

The four-edge comparison keeps `LuaScope thread`, because that configuration runs the same
algorithm as the other three edges and the cross-edge claim depends on that being true. This
figure is the separate report on `LuaScope conn`: a one-directive change that removes the
segfaults, measured against its own paired baseline rather than folded into the main comparison.

Three panels, because a remediation has to be judged on all three at once:

    crashes     what it fixes — the whole reason the variant exists
    latency     what it costs end to end, as WADM overhead per GET
    CPU         what it costs in work, as marginal CPU per request

Both arms share the same bare tier (`httpd-baseline.conf` has no mod_lua at all, so LuaScope is
meaningless there), which means each arm's WADM overhead is measured above the same floor and the
two are directly comparable.

The internal microsecond timings are printed rather than drawn, and the printed check earned its
keep: the expectation was that LuaScope changes only WHEN a Lua state is built, leaving the
detection and injection medians alone. It does not. Under `conn` scope the state is rebuilt for
every connection, so the first request on each connection pays module-level setup INSIDE the timed
regions — `form_fields.inject` moved from 3 µs to 21 µs at rate 1. The remediation therefore
inflates the measured mechanism cost as well as the surrounding work, which is a reason to keep it
out of the cross-edge comparison rather than a detail.

Reads paired_apache_lua.json and paired_apache_lua_conn.json.

Usage:
    python3 benchmarks/plot_remediation.py [results_dir]
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

from edge_specs import EDGES
from plot_common import COLORS, INK, INK_MUTED, _style_frame
from wadm_timings import ALL_KINDS, PHASES, paired_delta

BASELINE_KEY = "apache_lua"
VARIANT_KEY = "apache_lua_conn"

GET_METRICS = [
    "inject_get_duration",
    "detect_query_duration",
    "token_tamper_duration",
    "token_decoy_duration",
]

# The two probes that, under test.js's rotated order, always follow one of the trap's 500 responses.
# httpd closes keep-alive after a 500, so these two reconnect on every iteration; under
# `LuaScope conn` a new connection also builds a fresh Lua state. The latency panel is GET-only and
# cannot show that cost, so it is reported here instead of being left invisible.
POST_500_FOLLOWERS = ["sqli_hit_enc_duration", "sqli_miss_duration"]

# Both arms are Apache, so both wear Apache's hue; the shipped configuration is solid and the
# remediation is hatched. Hue never encodes rank here, and a second categorical colour would imply
# these are different edges rather than two settings of one.
BASE_COLOR = COLORS[EDGES[BASELINE_KEY].label]
ARMS = [
    (BASELINE_KEY, "LuaScope thread\n(shipped, benchmarked)", False),
    (VARIANT_KEY, "LuaScope conn\n(remediation)", True),
]


def load(results_dir: Path, key: str) -> dict[str, Any] | None:
    path = results_dir / f"paired_{key}.json"
    return json.loads(path.read_text()) if path.exists() else None


def valid(doc: dict[str, Any], tier: str, rate: int) -> dict[int, dict[str, Any]]:
    return {
        c["replicate"]: c for c in doc["cells"]
        if c["tier"] == tier and c["rate"] == rate and c["validity"]["valid"]
    }


def crash_stats(doc: dict[str, Any], rate: int) -> dict[str, Any]:
    """Segfaults per 10,000 requests, and how many replicates saw any at all.

    Counted over EVERY WADM cell, valid or not: whether the edge crashed is a property of the run
    that a rejected cell still records, and excluding rejected runs would hide crashes in exactly
    the runs most likely to contain them.
    """
    cells = [c for c in doc["cells"] if c["tier"] == "wadm" and c["rate"] == rate]
    if not cells:
        return {"available": False}
    crashes = [c["validity"].get("crash_count") or 0 for c in cells]
    reqs = [(c.get("k6") or {}).get("http_reqs") or 0 for c in cells]
    total_reqs = sum(reqs)
    return {
        "available": True,
        "replicates": len(cells),
        "replicates_valid": sum(1 for c in cells if c["validity"]["valid"]),
        "replicates_crashed": sum(1 for x in crashes if x),
        "crashes_total": sum(crashes),
        "per_10k": (10000.0 * sum(crashes) / total_reqs) if total_reqs else 0.0,
    }


def latency_overhead(doc: dict[str, Any], rate: int) -> dict[str, Any]:
    """WADM's added latency per iteration across the four GETs, in milliseconds."""
    bare, wadm = valid(doc, "bare", rate), valid(doc, "wadm", rate)
    deltas = []
    for rep in sorted(set(bare) & set(wadm)):
        pairs = []
        for m in GET_METRICS:
            b = ((bare[rep].get("k6") or {}).get("trends") or {}).get(m)
            w = ((wadm[rep].get("k6") or {}).get("trends") or {}).get(m)
            if b and w and b.get("count") and w.get("count"):
                pairs.append(w["med"] - b["med"])
        if len(pairs) == len(GET_METRICS):
            deltas.append(sum(pairs))
    return paired_delta(deltas)


def reconnect_overhead(doc: dict[str, Any], rate: int) -> dict[str, Any]:
    """Median latency added to the requests that follow a 500, per request, in milliseconds."""
    bare, wadm = valid(doc, "bare", rate), valid(doc, "wadm", rate)
    deltas = []
    for rep in sorted(set(bare) & set(wadm)):
        pairs = []
        for m in POST_500_FOLLOWERS:
            b = ((bare[rep].get("k6") or {}).get("trends") or {}).get(m)
            w = ((wadm[rep].get("k6") or {}).get("trends") or {}).get(m)
            if b and w and b.get("count") and w.get("count"):
                pairs.append(w["med"] - b["med"])
        if len(pairs) == len(POST_500_FOLLOWERS):
            deltas.append(sum(pairs) / len(pairs))
    return paired_delta(deltas)


def cpu_overhead(doc: dict[str, Any], rate: int) -> dict[str, Any]:
    bare, wadm = valid(doc, "bare", rate), valid(doc, "wadm", rate)
    deltas = []
    for rep in sorted(set(bare) & set(wadm)):
        b = ((bare[rep].get("cpu") or {}).get("edge") or {}).get("cpu_us_per_request")
        w = ((wadm[rep].get("cpu") or {}).get("edge") or {}).get("cpu_us_per_request")
        if b is not None and w is not None:
            deltas.append(w - b)
    return paired_delta(deltas)


def internal_medians(doc: dict[str, Any], rate: int) -> dict[str, float]:
    """Per-kind, per-phase median microseconds, pooled across valid WADM replicates."""
    out = {}
    for kind in ALL_KINDS:
        for phase in PHASES:
            samples: list[int] = []
            for c in valid(doc, "wadm", rate).values():
                samples.extend((c.get("tokens_raw") or {}).get(kind, {}).get(f"{phase}_us", []))
            if samples:
                out[f"{kind}.{phase}"] = statistics.median(samples)
    return out


def err_from(delta: dict[str, Any]) -> list[list[float]] | None:
    if not delta.get("ci_reliable") or delta.get("ci_low") is None:
        return None
    med = delta["median"]
    return [[max(0.0, med - delta["ci_low"])], [max(0.0, delta["ci_high"] - med)]]


def draw(docs: dict[str, Any], rates: list[int], out_dir: Path) -> Path:
    fig, axes = plt.subplots(1, 3, figsize=(15.5, 5.8))
    width = 0.36
    xs = range(len(rates))

    panels = [
        ("Segfaults per 10,000 requests", "what it fixes",
         lambda d, r: crash_stats(d, r).get("per_10k"), None, "{:.1f}"),
        ("WADM latency overhead (ms / iteration)", "what it costs, end to end",
         lambda d, r: latency_overhead(d, r).get("median"),
         lambda d, r: err_from(latency_overhead(d, r)), "{:.2f}"),
        ("WADM CPU cost (µs / request)", "what it costs, in work",
         lambda d, r: cpu_overhead(d, r).get("median"),
         lambda d, r: err_from(cpu_overhead(d, r)), "{:.0f}"),
    ]

    def unsustained(doc: dict[str, Any], rate: int) -> bool:
        """True when no replicate at this rate produced usable paired data.

        For the variant this is itself the result: `LuaScope conn` could not hold 900 req/s, so
        there is nothing to compare rather than nothing to pay.
        """
        return not (valid(doc, "bare", rate) and valid(doc, "wadm", rate))

    for ax, (ylabel, subtitle, value_fn, err_fn, fmt) in zip(axes, panels):
        # The axis is framed from the bars and their UPPER uncertainty, never from autoscale. A
        # single contaminated replicate can drag a bootstrap lower bound far negative, and letting
        # autoscale answer to that whisker once compressed every real bar in the latency panel to
        # invisibility against a 0-80 ms axis for bars around 1 ms. The gate that rejects such a
        # cell is the real fix; this makes the figure unable to hide the next one.
        axis_top = 0.0
        drawn: list[tuple[str, int, float]] = []
        for i, (key, label, hatched) in enumerate(ARMS):
            doc = docs.get(key)
            if not doc:
                continue
            offset = (i - 0.5) * width
            values, errs, missing = [], [[], []], []
            for r in rates:
                gap = unsustained(doc, r) and err_fn is not None
                v = None if gap else value_fn(doc, r)
                missing.append(gap)
                values.append(v if v is not None else 0.0)
                e = err_fn(doc, r) if (err_fn and not gap) else None
                errs[0].append(e[0][0] if e else 0.0)
                errs[1].append(e[1][0] if e else 0.0)
                axis_top = max(axis_top, (v or 0.0) + (e[1][0] if e else 0.0))
                if e and (v or 0.0) - e[0][0] < 0:
                    drawn.append((key, r, (v or 0.0) - e[0][0]))
            bars = ax.bar(
                [x + offset for x in xs], values, width,
                color="white" if hatched else BASE_COLOR,
                edgecolor=BASE_COLOR if hatched else "white",
                linewidth=2, hatch="///" if hatched else None,
                label=label, zorder=2,
                yerr=errs if any(errs[0] + errs[1]) else None,
                error_kw={"ecolor": INK_MUTED, "elinewidth": 1.2, "capsize": 3},
            )
            for bar, v, gap in zip(bars, values, missing):
                # A gap is "this configuration could not sustain this rate", not "it cost nothing".
                # Writing 0.00 there would invert the finding.
                label_text = "did not\nsustain" if gap else fmt.format(v)
                ax.text(
                    bar.get_x() + bar.get_width() / 2,
                    bar.get_height() + max(axis_top, 1.0) * 0.03,
                    label_text, ha="center", va="bottom", fontsize=8 if gap else 9,
                    color=INK_MUTED if gap else INK,
                    style="italic" if gap else "normal",
                )

        ax.set_xticks(list(xs))
        ax.set_xticklabels([f"{r}/s" for r in rates])
        ax.set_xlabel("offered iterations per second", fontsize=9, color=INK_MUTED)
        ax.set_ylabel(ylabel, fontsize=9, color=INK_MUTED)
        ax.set_title(subtitle, fontsize=11, color=INK, loc="left")
        _style_frame(ax)
        ax.set_ylim(0, (axis_top or 1.0) * 1.25)
        for key, r, low in drawn:
            # Clipped at the y=0 floor, so say so rather than letting the figure imply the
            # interval stopped at zero.
            print(f"  ! {ylabel}: {key} at rate {r}/s has a CI lower bound of {low:.3g}, "
                  f"below the axis floor and therefore clipped in the figure")

    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=2, frameon=False, fontsize=9,
               bbox_to_anchor=(0.5, 0.17))

    fig.suptitle(
        "Apache remediation: one directive removes the segfaults, and what that costs",
        fontsize=14, fontweight="bold", color=INK, x=0.01, ha="left",
    )
    fig.text(
        0.01, 0.01,
        "Both arms are the SAME Apache edge differing only in LuaScope (thread vs conn), sharing "
        "one bare tier, so each overhead is measured above the same floor.\n"
        "`LuaScope conn` rebuilds the Lua state per connection instead of per worker thread. That "
        "removes the accumulation behind the crash — and also resets detect.lua's in-memory\n"
        "attacker store per connection, so recorded attacker IPs no longer survive the connection. "
        "The shipped configuration is kept for the four-edge comparison because it runs the same\n"
        "algorithm as the other three edges; this variant is reported as a validated fix with its "
        "cost stated. Error bars = range across paired replicates. Not drawn: the latency panel is "
        "GET-only, but under `conn` each request that follows a trap 500 (httpd closes the\n"
        "connection after a 500) pays a reconnect plus a fresh Lua state: "
        f"{reconnect_note(docs, rates, VARIANT_KEY)} per such request under `conn`, against "
        f"{reconnect_note(docs, rates, BASELINE_KEY)} under `thread` (net of the round-trip the trap saves).",
        fontsize=7.5, color=INK_MUTED, style="italic", va="bottom", linespacing=1.5,
    )
    fig.tight_layout(rect=[0, 0.235, 1, 0.945])

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "apache_remediation.png"
    fig.savefig(path, dpi=150, facecolor="white")
    plt.close(fig)
    return path


def reconnect_note(docs: dict[str, Any], rates: list[int], key: str) -> str:
    """Range over rates of the post-500 latency delta for one arm, as caption text."""
    values = [reconnect_overhead(docs[key], r).get("median") for r in rates if docs.get(key)]
    values = [v for v in values if v is not None]
    if not values:
        return "an unmeasured amount"
    return f"{min(values):+.2f} to {max(values):+.2f} ms"


def report(docs: dict[str, Any], rates: list[int]) -> None:
    print(f"\n{'=' * 78}\nApache remediation: LuaScope thread vs LuaScope conn\n{'=' * 78}")
    for rate in rates:
        print(f"\n  rate {rate}/s")
        for key, label, _ in ARMS:
            doc = docs.get(key)
            if not doc:
                continue
            c = crash_stats(doc, rate)
            lat, cpu = latency_overhead(doc, rate), cpu_overhead(doc, rate)
            name = label.split("\n")[0]
            head = (
                f"    {name:18s} crashes {c.get('crashes_total', 0):3d} "
                f"({c.get('replicates_crashed', 0)}/{c.get('replicates', 0)} reps, "
                f"{c.get('per_10k', 0):.2f}/10k req) | "
            )
            if lat.get("median") is None or cpu.get("median") is None:
                print(head + "NO PAIRED DATA — this rate was not sustained "
                      f"({c.get('replicates_valid', 0)}/{c.get('replicates', 0)} cells valid)")
            else:
                post500 = reconnect_overhead(doc, rate)
                print(head + f"latency {lat['median']:+.3f} ms/iter | "
                             f"cpu {cpu['median']:+.1f} us/req | "
                             f"after a 500: {post500.get('median') or 0:+.3f} ms/req")

        # LuaScope changes state lifetime, not the detection or injection code, so these should
        # match. Printing the comparison is a check; drawing it would only assert it.
        both = [internal_medians(docs[k], rate) for k, _, _ in ARMS if docs.get(k)]
        if len(both) == 2:
            shared = sorted(set(both[0]) & set(both[1]))
            diffs = [(k, both[0][k], both[1][k]) for k in shared if both[0][k] != both[1][k]]
            if not shared:
                print("    internal timings: no overlapping data at this rate")
            else:
                print(f"    internal timings: {len(shared) - len(diffs)}/{len(shared)} medians identical", end="")
                if diffs:
                    worst = max(diffs, key=lambda d: abs(d[2] - d[1]))
                    print(f"; largest shift {worst[0]} {worst[1]:.0f} -> {worst[2]:.0f} us")
                else:
                    print(" (mechanism unchanged)")


def main() -> int:
    results_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent / "results"
    )
    docs = {k: load(results_dir, k) for k, _, _ in ARMS}
    missing = [k for k, v in docs.items() if not v]
    if VARIANT_KEY in missing:
        print(f"  ! no paired_{VARIANT_KEY}.json — run it first:")
        print(f"      python3 benchmarks/run_paired_benchmark.py --edge {VARIANT_KEY} --preset full")
        return 1
    if BASELINE_KEY in missing:
        print(f"  ! no paired_{BASELINE_KEY}.json to compare against")
        return 1

    rates = sorted(set.intersection(*({c["rate"] for c in d["cells"]} for d in docs.values())))
    report(docs, rates)
    path = draw(docs, rates, results_dir / "plots")
    print(f"\n  wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
