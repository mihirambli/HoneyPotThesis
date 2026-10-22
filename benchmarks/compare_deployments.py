#!/usr/bin/env python3
"""
Proxy deployment against in-server deployment of the same server: what the module costs in each,
and what each deployed stack costs above the shared origin.

Every number here is a PAIRED difference measured inside one session: the in-server variant's tiers
were run minutes apart in their session, the proxy edge's tiers in theirs. Comparing the two
deployments is therefore a comparison of two paired differences, each with its own replicate range,
and never a subtraction of raw latencies taken in different sessions — such cross-session deltas
were found to be dominated by session drift (docs/BENCHMARK_METHODOLOGY.md §1.3). Where two ranges
do not overlap, the deployments differ by more than either session's replicate spread; where they
overlap, this harness cannot order them.

For each pair in INSERVER_COUNTERPART and each rate:

  1. Module cost        silent - bare in each deployment: latency added per GET (p50, p95) and CPU
                        microseconds per request of the host container (the server running the module).
  2. Above the origin   silent - origin in each deployment: latency per GET, and stack CPU per
                        request, where stack CPU = host container + origin container over the measured
                        window (on the origin tier only the origin serves; the idle host container is
                        not counted, as in analyze_paired.py).
  3. Bare - origin      in the proxy deployment this is the proxy hop; in the in-server deployment it is
                        the server swap — OpenResty or Apache serving files against nginx:alpine — and is
                        the confound inside item 2's in-server figure.
  4. Sanity             origin CPU per request on the in-server edge tiers, which must be about zero:
                        nothing was proxied; and the wadm tier's timing coverage, which shows whether
                        the module did the same work per iteration in both deployments. It did not on
                        Apache in-server: Apache renders its own 404 pages through
                        ap_send_error_response, which drops RESOURCE filters such as WADM_INJECT, so
                        the two benchmark GETs that end in 404 get their bait headers but no body
                        injection there.

Writes:
  benchmarks/results/deployment_comparison.md / .csv
  benchmarks/results/plots/deployment_comparison.png

Usage:
    python3 benchmarks/compare_deployments.py [--results DIR]
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

from analyze_paired import analyse_edge, load_paired, paired_pairs, valid_cells
from edge_specs import EDGES, INSERVER_COUNTERPART, ORIGIN_TIER, WADM_TIERS
from plot_common import COLORS, INK, INK_MUTED, _style_frame
from wadm_timings import paired_delta

US_PER_MS = 1000.0

# (key, lower tier, higher tier, short title) for the three differences the comparison is built on.
COMPARISONS = [
    ("silent_minus_bare", "bare", "silent", "module cost (silent − bare)"),
    ("silent_minus_origin", ORIGIN_TIER, "silent", "deployed stack above origin (silent − origin)"),
    ("bare_minus_origin", ORIGIN_TIER, "bare", "bare − origin (proxy hop / server swap)"),
]
DEPLOYMENTS = ("proxy", "inserver")
# The pair compares one server in two placements, so it is named after the server, not the proxy
# edge's label (which carries the runtime, e.g. "Apache+Lua").
SERVER_NAME = {"openresty": "OpenResty", "apache_lua": "Apache httpd"}
DEPLOYMENT_LABEL = {"proxy": "proxy", "inserver": "in-server"}
# Marker shape and line style carry the deployment, so it never rests on colour; colour is the
# server. Hollow keeps its meaning from the other paired figures: the range spans zero.
MARKER = {"proxy": "o", "inserver": "D"}
LINESTYLE = {"proxy": "-", "inserver": (0, (3, 1.5))}


def stack_cpu(cell: dict[str, Any], tier: str) -> float | None:
    cpu = cell.get("cpu") or {}
    origin = (cpu.get("origin") or {}).get("cpu_us_per_request")
    if tier == ORIGIN_TIER:
        return origin
    host = (cpu.get("edge") or {}).get("cpu_us_per_request")
    return None if host is None or origin is None else host + origin


def paired_stack_cpu(doc: dict[str, Any], rate: int, low: str, high: str) -> dict[str, Any]:
    deltas = []
    for lo_cell, hi_cell in paired_pairs(doc, rate, low, high):
        a, b = stack_cpu(lo_cell, low), stack_cpu(hi_cell, high)
        if a is not None and b is not None:
            deltas.append(b - a)
    return paired_delta(deltas)


def origin_cpu_on_edge_tiers(doc: dict[str, Any], rate: int) -> dict[str, Any]:
    out = {}
    for tier in ("bare", *WADM_TIERS):
        values = [
            ((c.get("cpu") or {}).get("origin") or {}).get("cpu_us_per_request")
            for c in valid_cells(doc, tier, rate)
        ]
        values = [v for v in values if v is not None]
        if values:
            out[tier] = {"median": round(statistics.median(values), 2), "max": round(max(values), 2),
                         "n": len(values)}
    return out


def in_us(delta: dict[str, Any]) -> dict[str, Any]:
    """A latency delta from analyze_paired (milliseconds) restated in microseconds."""
    if not delta or delta.get("median") is None:
        return {"n": 0}
    out = dict(delta)
    for key in ("median", "ci_low", "ci_high"):
        if out.get(key) is not None:
            out[key] = out[key] * US_PER_MS
    return out


def measures(doc: dict[str, Any], summary: dict[str, Any], rate: int) -> dict[str, Any]:
    """Every figure the comparison reports for one variant at one rate."""
    blocks = (summary["rates"].get(str(rate)) or {}).get("comparisons") or {}
    out: dict[str, Any] = {}
    for name, low, high, _ in COMPARISONS:
        block = blocks.get(name)
        if not block:
            continue
        per_get = block["latency_delta_quantiles_ms"]["per_get"]
        entry = {
            "p50_us": in_us(per_get.get("med")),
            "p95_us": in_us(per_get.get("p95")),
        }
        if low == ORIGIN_TIER:
            entry["cpu_us"] = paired_stack_cpu(doc, rate, low, high)
            entry["cpu_kind"] = "stack (host + origin)"
        else:
            entry["cpu_us"] = block["cpu_delta_us_per_request"]
            entry["cpu_kind"] = "host container"
        out[name] = entry
    out["origin_cpu_on_edge_tiers"] = origin_cpu_on_edge_tiers(doc, rate)
    cov = (summary["rates"].get(str(rate)) or {}).get("coverage") or {}
    out["coverage_shortfall"] = {
        f"{kind}.{phase}": c["worst_ratio"]
        for kind, phases in cov.items() for phase, c in phases.items() if c["worst_ratio"] < 1.0
    }
    out["coverage_measured"] = bool(cov)
    return out


def overlap(a: dict[str, Any], b: dict[str, Any]) -> str:
    if not a.get("n") or not b.get("n") or a.get("ci_low") is None or b.get("ci_low") is None:
        return "n/a"
    if not (a.get("ci_reliable") and b.get("ci_reliable")):
        return "n/a (too few replicates)"
    disjoint = a["ci_high"] < b["ci_low"] or b["ci_high"] < a["ci_low"]
    return "no — deployments differ" if disjoint else "yes — not ordered"


def fmt(delta: dict[str, Any], digits: int = 0) -> str:
    if not delta or not delta.get("n"):
        return "—"
    median = f"{delta['median']:+.{digits}f}"
    if delta.get("ci_low") is None or not delta.get("ci_reliable"):
        return f"{median} (n={delta['n']})"
    rng = f"{delta['ci_low']:+.{digits}f}..{delta['ci_high']:+.{digits}f}"
    flag = "" if delta.get("significant") else ", spans 0"
    return f"{median} [{rng}{flag}]"


def collect(results_dir: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    cache: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for inserver, proxy in INSERVER_COUNTERPART.items():
        docs = {}
        for key in (proxy, inserver):
            if key not in cache:
                doc = load_paired(results_dir, key)
                if doc:
                    cache[key] = (doc, analyse_edge(doc))
            if key in cache:
                docs[EDGES[key].deployment] = (key, *cache[key])
        if len(docs) < 2:
            missing = [k for k in (proxy, inserver) if k not in cache]
            print(f"  ! no paired_{', paired_'.join(missing)}.json — skipping "
                  f"{EDGES[inserver].label} vs {EDGES[proxy].label}")
            continue
        rates = sorted(
            {c["rate"] for c in docs["proxy"][1]["cells"]}
            & {c["rate"] for c in docs["inserver"][1]["cells"]}
        )
        out[proxy] = {
            "server": SERVER_NAME.get(proxy, EDGES[proxy].label),
            "color": COLORS[EDGES[proxy].label],
            "keys": {d: docs[d][0] for d in DEPLOYMENTS},
            "replicates": {d: docs[d][1]["metadata"].get("replicates") for d in DEPLOYMENTS},
            "generated_at": {d: docs[d][1]["metadata"].get("generated_at") for d in DEPLOYMENTS},
            "rates": {
                rate: {d: measures(docs[d][1], docs[d][2], rate) for d in DEPLOYMENTS}
                for rate in rates
            },
        }
    return out


def csv_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for pair in data.values():
        for rate, per_dep in pair["rates"].items():
            for dep in DEPLOYMENTS:
                m = per_dep[dep]
                for name, _, _, _ in COMPARISONS:
                    entry = m.get(name)
                    if not entry:
                        continue
                    row = {
                        "server": pair["server"], "deployment": DEPLOYMENT_LABEL[dep],
                        "variant": pair["keys"][dep], "rate_iter_per_s": rate, "comparison": name,
                        "cpu_kind": entry["cpu_kind"],
                    }
                    for field in ("p50_us", "p95_us", "cpu_us"):
                        d = entry[field]
                        for key, col in (("median", "median"), ("ci_low", "range_low"),
                                         ("ci_high", "range_high")):
                            v = d.get(key)
                            row[f"{field}_{col}"] = None if v is None else round(v, 2)
                        row[f"{field}_sign_agreement"] = d.get("sign_agreement")
                        row[f"{field}_n"] = d.get("n")
                    rows.append(row)
    return rows


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(data: dict[str, Any], path: Path) -> None:
    lines = [
        "# Proxy deployment against in-server deployment",
        "",
        "Generated by `benchmarks/compare_deployments.py`. Every figure is a **paired difference "
        "measured inside one session** (median of the per-replicate differences, with their range "
        "across replicates in brackets; `spans 0` = the range includes zero). The two deployments "
        "were measured in **separate sessions**, so they are compared as two paired differences, "
        "never by subtracting raw latencies across sessions. *Overlap* says whether the two ranges "
        "overlap: `no` means the deployments differ by more than either session's replicate spread; "
        "`yes` means this harness cannot order them.",
        "",
        "Latency is per GET (mean of the four GET probes within a replicate) in microseconds. CPU is "
        "microseconds per request: for the module cost, of the host container (the server running "
        "the module); above the origin, of the stack (host container + origin container; on the "
        "origin tier only the origin serves).",
        "",
    ]
    for pair in data.values():
        keys = pair["keys"]
        lines += [
            f"## {pair['server']}: `{keys['proxy']}` (proxy) vs `{keys['inserver']}` (in-server)",
            "",
            f"Sessions: proxy {pair['generated_at']['proxy'] or '?'} "
            f"({pair['replicates']['proxy']} replicates); in-server "
            f"{pair['generated_at']['inserver'] or '?'} ({pair['replicates']['inserver']} replicates).",
            "",
        ]
        for name, _, _, title in COMPARISONS:
            lines += [
                f"### {title[0].upper()}{title[1:]}",
                "",
                "| Rate (it/s) | Deployment | Δp50 per GET (µs) | Δp95 per GET (µs) | ΔCPU (µs/req) |",
                "|---|---|---|---|---|",
            ]
            overlaps = []
            for rate, per_dep in pair["rates"].items():
                for dep in DEPLOYMENTS:
                    entry = per_dep[dep].get(name)
                    if not entry:
                        continue
                    lines.append(
                        f"| {rate} | {DEPLOYMENT_LABEL[dep]} | {fmt(entry['p50_us'])} | "
                        f"{fmt(entry['p95_us'])} | {fmt(entry['cpu_us'], 1)} |"
                    )
                a, b = per_dep["proxy"].get(name), per_dep["inserver"].get(name)
                if a and b:
                    overlaps.append(
                        f"rate {rate}: p50 overlap {overlap(a['p50_us'], b['p50_us'])}; "
                        f"CPU overlap {overlap(a['cpu_us'], b['cpu_us'])}"
                    )
            lines += [""] + [f"- {o}" for o in overlaps] + [""]
            if name == "bare_minus_origin":
                lines += [
                    "In the proxy deployment this row is the proxy hop. In the in-server deployment "
                    "there is no hop: it is the host server serving files against the nginx:alpine "
                    "origin, which is part of the in-server `silent − origin` figure above and must "
                    "be read with it.",
                    "",
                ]
        lines += [
            "### Sanity: origin CPU per request on the edge tiers",
            "",
            "On the in-server tiers the origin is idle, so its CPU per request must be about zero. "
            "On the proxy tiers it serves every forwarded request.",
            "",
            "| Rate (it/s) | Deployment | bare | silent | wadm |",
            "|---|---|---|---|---|",
        ]
        for rate, per_dep in pair["rates"].items():
            for dep in DEPLOYMENTS:
                o = per_dep[dep]["origin_cpu_on_edge_tiers"]
                cells = [
                    f"{o[t]['median']:.2f} (max {o[t]['max']:.2f})" if t in o else "—"
                    for t in ("bare", "silent", "wadm")
                ]
                lines.append(f"| {rate} | {DEPLOYMENT_LABEL[dep]} | " + " | ".join(cells) + " |")
        lines += [
            "",
            "### Same work per iteration? (wadm-tier timing coverage)",
            "",
            "Timing samples logged per iteration against what the proxy deployment's probes produce "
            "(analyze_paired.py's EXPECTED_SAMPLES_PER_ITERATION). A ratio below 1 means the module "
            "ran that region on fewer requests, so its cost in that deployment covers less work.",
            "",
            "| Rate (it/s) | Deployment | Regions below 1 (worst ratio) |",
            "|---|---|---|",
        ]
        for rate, per_dep in pair["rates"].items():
            for dep in DEPLOYMENTS:
                m = per_dep[dep]
                short = ", ".join(f"{k} {v:.4f}" for k, v in sorted(m["coverage_shortfall"].items()))
                text = short or ("none" if m["coverage_measured"] else "not measured")
                lines.append(f"| {rate} | {DEPLOYMENT_LABEL[dep]} | {text} |")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def plot(data: dict[str, Any], path: Path) -> None:
    servers = list(data)
    rows = [(s, rate) for s in servers for rate in data[s]["rates"]]
    if not rows:
        return
    y_of = {row: -i - 0.6 * servers.index(row[0]) for i, row in enumerate(rows)}
    offset = {"proxy": 0.16, "inserver": -0.16}
    metrics = [("p50_us", "latency added per GET, p50 (µs)"), ("cpu_us", "CPU added (µs/request)")]
    fig, axes = plt.subplots(len(metrics), len(COMPARISONS), figsize=(15.5, 8.6), sharey=True,
                             squeeze=False)
    for col, (name, _, _, title) in enumerate(COMPARISONS):
        for r, (field, xlabel) in enumerate(metrics):
            ax = axes[r][col]
            for (server, rate), y in y_of.items():
                color = data[server]["color"]
                for dep in DEPLOYMENTS:
                    entry = data[server]["rates"][rate][dep].get(name)
                    d = (entry or {}).get(field) or {}
                    if d.get("median") is None:
                        continue
                    yy = y + offset[dep]
                    if d.get("ci_low") is not None:
                        ax.plot([d["ci_low"], d["ci_high"]], [yy, yy], color=color, linewidth=2,
                                linestyle=LINESTYLE[dep], solid_capstyle="round", zorder=2)
                    ax.plot(d["median"], yy, marker=MARKER[dep], markersize=8, zorder=3,
                            color=color,
                            markerfacecolor=color if d.get("significant") else "white",
                            markeredgecolor=color, markeredgewidth=2)
            ax.axvline(0, color=INK_MUTED, linewidth=1, zorder=1)
            _style_frame(ax)
            ax.grid(True, which="major", axis="x", alpha=0.25, linewidth=0.8)
            ax.grid(False, axis="y")
            ax.set_xlabel(xlabel if field == "p50_us" else
                          ("host-container " if name == "silent_minus_bare" else "stack ") + xlabel,
                          fontsize=9, color=INK_MUTED)
            if r == 0:
                ax.set_title(title, fontsize=11, color=INK, loc="left")
    axes[0][0].set_yticks(list(y_of.values()))
    axes[0][0].set_yticklabels([f"{data[s]['server']}, {rate}/s" for s, rate in y_of], fontsize=9)
    axes[1][0].set_yticks(list(y_of.values()))
    axes[1][0].set_yticklabels([f"{data[s]['server']}, {rate}/s" for s, rate in y_of], fontsize=9)

    handles = [
        Line2D([], [], color=data[s]["color"], linewidth=6, label=data[s]["server"])
        for s in servers
    ] + [
        Line2D([], [], color=INK_MUTED, marker=MARKER[d], markersize=8, linewidth=2,
               linestyle=LINESTYLE[d], label=f"{DEPLOYMENT_LABEL[d]} deployment")
        for d in DEPLOYMENTS
    ] + [
        Line2D([], [], color=INK_MUTED, marker="o", markersize=8, linewidth=0,
               markerfacecolor="white", markeredgewidth=2,
               label="not resolved (range spans zero, or too few replicates)"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=len(handles), frameon=False, fontsize=9,
               bbox_to_anchor=(0.5, 0.06))
    fig.suptitle("The same module in a reverse proxy and inside the web server", fontsize=14,
                 fontweight="bold", color=INK, x=0.01, ha="left")
    fig.text(
        0.01, 0.005,
        "Mark = median over replicates of a paired difference measured inside one session; line = "
        "range of those differences. Proxy and in-server come from separate sessions, so compare "
        "their ranges,\nnot their raw latencies. Right column: in the proxy deployment bare − origin "
        "is the proxy hop; in the in-server deployment it is OpenResty/Apache serving files against "
        "the nginx:alpine origin.\nStack CPU = host container + origin container per request.",
        fontsize=7.5, color=INK_MUTED, style="italic", va="bottom", linespacing=1.5,
    )
    fig.tight_layout(rect=[0, 0.11, 1, 0.95])
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, facecolor="white")
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--results", default=None, help="Results directory.")
    args = parser.parse_args()
    results_dir = Path(args.results) if args.results else Path(__file__).resolve().parent / "results"

    data = collect(results_dir)
    if not data:
        print("Nothing to compare: needs paired_<proxy>.json and paired_<inserver>.json for at "
              "least one pair.", file=sys.stderr)
        return 1
    md, csv_path = results_dir / "deployment_comparison.md", results_dir / "deployment_comparison.csv"
    png = results_dir / "plots" / "deployment_comparison.png"
    write_markdown(data, md)
    write_csv(csv_rows(data), csv_path)
    plot(data, png)
    print(md.read_text(encoding="utf-8"))
    for p in (md, csv_path, png):
        print(f"Saved {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
