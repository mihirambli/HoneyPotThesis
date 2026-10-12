#!/usr/bin/env python3
"""
Find each edge's maximum sustained throughput, which resource limits it, and what a request costs
in CPU at that load.

This is the question the old 500-VU level was actually asking, and could not answer. At 500 VUs
the origin tier alone — no proxy, no WADM — showed a 4.9x latency spread across the eight requests
of an iteration and a median of 3.6 ms against 0.6 ms at rate 1. Nearly all of that is queueing
delay. Subtracting two queueing delays does not yield a WADM cost, so latency at saturation was
never interpretable; capacity and cost-per-request are, and they are what saturation is good for.

A staircase of short fixed-rate runs rather than one `ramping-arrival-rate` run: each step gets its
own k6 summary, its own validity verdict and its own CPU window, where a single ramping run would
blur every stage into one distribution and one CPU total.

An edge "sustains" a rate when it places the offered iterations (>= 95%), drops almost none
(<= 1%), errors almost never (<= 0.5%), and keeps the WORST GET probe's p95 under `--slo-ms`. The
SLO reads the GETs, not the pooled latency, because on three edges the SQLi trap answers its POSTs
locally and faster: pooling them would let WADM's own short-circuit relax the SLO it is held to.

What the first version of this runner got wrong, and what replaced it:

  * It did not record which resource saturated. Every bare edge stalled near 7,000-7,200 req/s
    with its own cores half idle, while the WADM tier — whose trap keeps 4 of 9 requests off the
    origin — stalled on the edge. "Throughput retained" therefore compared two different
    bottlenecks. Each step now records the cores the edge, the origin and the load generator used
    over the measured window, as a fraction of each one's cpuset, and the step's `binding`
    resource is the most saturated of the three.
  * A doubling ladder cannot resolve a difference smaller than 2x. After the first failing rate the
    ladder bisects (geometrically) between the last passing and first failing rate.
  * One 20 s run per step. Each step now runs `--replicates` times; a rate is sustained when a
    majority of its replicates sustain it.

Writes:
  benchmarks/results/capacity_<edge>.json

Usage:
    python3 benchmarks/run_capacity_benchmark.py --all
    python3 benchmarks/run_capacity_benchmark.py --edge apache_lua --rates 50,100,200 --tiers bare,wadm
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from edge_specs import EDGE_KEYS, EDGES, RUNNABLE_KEYS, TIERS, EdgeSpec, scan_for_crashes, write_silent_config
from wadm_timings import (
    DEFAULT_START_DELAY,
    MAX_DROPPED_RATE,
    MAX_FAILED_RATE,
    MAX_ORIGIN_CPU_US_PER_REQUEST,
    MIN_ACHIEVED_RATIO,
    CgroupSampler,
    cgroup_dir,
    container_id,
    cycle_loadtester,
    duration_seconds,
    ensure_compose_cleanup,
    ensure_test_pages,
    fetch_k6_summary,
    k6_env,
    k6_window_start,
    lines_since,
    run_cmd,
    strip_k6_option_env,
)

DEFAULT_TRIGGER = "internal-admin.example.com"

# Each iteration issues nine requests (one priming GET plus eight probes), so an offered rate of
# 400/s is 3,600 requests/s. The ladder starts above the latency suite's top rate and doubles, which
# brackets the knee in few steps; bisection then narrows it.
DEFAULT_LADDER = [100, 200, 400, 800, 1600]
DEFAULT_STEP_DURATION = "20s"
DEFAULT_SLO_MS = 100.0
DEFAULT_REPLICATES = 3
DEFAULT_BISECT = 2

# The GET probes the SLO is read from: every edge proxies them, and every one carries injection.
GET_TRENDS = [
    "inject_get_duration",
    "detect_query_duration",
    "token_tamper_duration",
    "token_decoy_duration",
]

# k6 itself needs VUs in hand to place a high rate; too few and the shortfall is the load
# generator's, not the edge's.
MAX_VUS_FACTOR = 4
MIN_MAX_VUS = 200

# Cores each component is pinned to, from the same variables docker-compose.yml reads. A container
# using all of its cpuset is saturated whatever its latency says.
CPUSETS = {
    "edge": os.environ.get("EDGE_CPUS", "1-2"),
    "origin": os.environ.get("BACKEND_CPUS", "3"),
    "loadgen": os.environ.get("LOADGEN_CPUS", "4-7"),
}

# Above this share of its cores a component is reported as saturated in its own right.
SATURATED_FRACTION = 0.85


def cpuset_size(spec: str) -> int:
    total = 0
    for part in spec.split(","):
        lo, _, hi = part.partition("-")
        total += (int(hi) - int(lo) + 1) if hi else 1
    return total


def utilisation(cpu: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Cores used by each component as a fraction of its cpuset, and the busiest one.

    `binding` is always the busiest component: a single-worker origin queues requests long before
    it averages 100% of its core, so a failure with the origin at ~75% is still most plausibly the
    origin's. `saturated` says whether that component was actually near its limit; when it was not,
    the host-wide busy and softirq shares (kernel networking, charged to no container) are the next
    place to look.
    """
    fractions = {
        name: round(cpu[name]["cores_used"] / cpuset_size(CPUSETS[name]), 3)
        for name in CPUSETS
        if (cpu.get(name) or {}).get("cores_used") is not None
    }
    busiest = max(fractions, key=fractions.get) if fractions else None
    return {
        "fraction_of_cpuset": fractions,
        "cpusets": dict(CPUSETS),
        "binding": busiest,
        "saturated": bool(busiest and fractions[busiest] >= SATURATED_FRACTION),
        "host": cpu.get("host") or {"available": False},
    }


def worst_get_p95(summary: dict[str, Any]) -> float | None:
    trends = summary.get("trends") or {}
    values = [(trends.get(t) or {}).get("p95") for t in GET_TRENDS]
    values = [v for v in values if v is not None]
    return max(values) if values else None


def step_verdict(summary: dict[str, Any] | None, offered: int, slo_ms: float,
                 crashes: list[str],
                 origin_cpu_us_per_request: float | None = None) -> dict[str, Any]:
    """Whether the edge sustained this rate, and which limit bound first if not."""
    if summary is None:
        return {"sustained": False, "reasons": ["k6 summary could not be read"], "warnings": []}

    iterations = int(summary.get("iterations") or 0)
    dropped = int(summary.get("dropped_iterations") or 0)
    failed = summary.get("http_req_failed_rate") or 0.0
    pooled = (summary.get("trends") or {}).get("http_req_duration") or {}
    p95 = worst_get_p95(summary)
    achieved = iterations / offered if offered else 0.0

    reasons = []
    warnings = []
    # A crash warns; the damage gates below decide whether the step counted. Treating the mere
    # presence of a crash line as a hard failure stopped Apache's WADM ladder at its FIRST step —
    # a step that placed 100% of its offered load at a 11 ms p95 — so the edge that most needed a
    # capacity number was the one that got none. This mirrors assess_run() in wadm_timings.py;
    # the two gates must agree or an edge is judged differently depending on which script ran it.
    if crashes:
        warnings.append(f"edge crashed during this step ({', '.join(crashes)})")
    if achieved < MIN_ACHIEVED_RATIO:
        reasons.append(f"placed only {achieved:.1%} of offered iterations")
    if offered and dropped / offered > MAX_DROPPED_RATE:
        reasons.append(f"dropped {dropped}/{offered} iterations")
    if failed > MAX_FAILED_RATE:
        reasons.append(f"http_req_failed {failed:.2%}")
    if p95 is not None and p95 > slo_ms:
        reasons.append(f"worst GET p95 {p95:.1f} ms over the {slo_ms:.0f} ms SLO")
    # Same negative control as the paired gate: the backend is identical in every step, so a large
    # excursion means the host was contended and the step measured the machine, not the edge.
    if (
        origin_cpu_us_per_request is not None
        and origin_cpu_us_per_request > MAX_ORIGIN_CPU_US_PER_REQUEST
    ):
        reasons.append(
            f"origin control burned {origin_cpu_us_per_request:.0f} us/req "
            f"(host contention, not the edge)"
        )

    return {
        "sustained": not reasons,
        "reasons": reasons,
        "warnings": warnings,
        "achieved_ratio": round(achieved, 4),
        "iterations": iterations,
        "dropped_iterations": dropped,
        "http_req_failed_rate": failed,
        "get_p95_ms": p95,
        "pooled_p50_ms": pooled.get("med"),
        "pooled_p95_ms": pooled.get("p95"),
        "pooled_p99_ms": pooled.get("p99"),
        "origin_cpu_us_per_request": origin_cpu_us_per_request,
    }


def tier_env(spec: EdgeSpec, tier: str, trigger: str) -> dict[str, str]:
    env = strip_k6_option_env(os.environ.copy())
    env["TARGET"] = spec.target
    env["TRIGGER_KEYWORD"] = trigger
    env.update(spec.tier_env(tier))
    return env


def run_step(spec: EdgeSpec, tier: str, rate: int, replicate: int,
             args: argparse.Namespace) -> dict[str, Any]:
    env = tier_env(spec, tier, args.trigger)
    run_cmd(spec.compose_prefix() + ["up", "-d"], env=env)

    max_vus = max(MIN_MAX_VUS, rate * MAX_VUS_FACTOR)
    shape = k6_env(rate, args.duration, args.start_delay, seed=replicate, max_vus=max_vus)
    run_env = {**env, **shape}

    sampler = CgroupSampler(
        {
            "edge": cgroup_dir(container_id(spec.service, env, spec.profile)),
            "origin": cgroup_dir(container_id("backend", env)),
        },
        late={"loadgen": lambda: cgroup_dir(container_id("load-tester", run_env, "loadtest"))},
    )
    with sampler:
        wait_result, run_start = cycle_loadtester(run_env)

    since = run_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    summary, k6_logs = fetch_k6_summary(run_env, since)
    stamped = run_cmd(
        spec.compose_prefix()
        + ["logs", "--no-color", "--timestamps", "--since", since, spec.service],
        env=env,
    ).stdout if spec.profile else ""
    crashes = scan_for_crashes(lines_since(stamped, 0.0))

    offered = rate * duration_seconds(args.duration)
    requests = (summary or {}).get("edge_requests") or (summary or {}).get("http_reqs")
    start = k6_window_start(k6_logs.stdout)
    cpu: dict[str, Any] = {}
    if start is not None:
        end = start + duration_seconds(args.duration)
        cpu = {
            "edge": sampler.window("edge", start, end, requests),
            "origin": sampler.window("origin", start, end, requests),
            "loadgen": sampler.window("loadgen", start, end, None),
            "host": sampler.host_window(start, end),
        }
    verdict = step_verdict(
        summary, offered, args.slo_ms, crashes,
        (cpu.get("origin") or {}).get("cpu_us_per_request"),
    )

    # Requests actually served per second, which is the capacity number. The offered rate is
    # iterations; each iteration is nine requests.
    seconds = duration_seconds(args.duration)
    throughput_rps = round(requests / seconds, 1) if requests and seconds else None
    intact = [
        v["rate"] for v in ((summary or {}).get("intact") or {}).values()
        if v and v.get("rate") is not None
    ]

    step = {
        "tier": tier,
        "offered_rate": rate,
        "replicate": replicate,
        "offered_iterations": offered,
        "duration": args.duration,
        "throughput_rps": throughput_rps,
        "cpu": cpu,
        "utilisation": utilisation(cpu),
        "worst_intact_rate": min(intact) if intact else None,
        "new_conn": (summary or {}).get("new_conn") or {},
        "verdict": verdict,
        "compose_exit_code": wait_result.returncode,
    }

    util = step["utilisation"]
    status = "sustained" if verdict["sustained"] else "FAILED: " + "; ".join(verdict["reasons"])
    if verdict["sustained"] and verdict.get("warnings"):
        status += "  (WARN: " + "; ".join(verdict["warnings"]) + ")"
    frac = util["fraction_of_cpuset"]
    host = util["host"]
    print(
        f"    {tier:6s} rate={rate:5d}/s rep{replicate} -> {throughput_rps or 0:8.1f} req/s  "
        f"GET p95={verdict.get('get_p95_ms') or 0:8.1f}ms  "
        f"edge={frac.get('edge', 0):.0%} origin={frac.get('origin', 0):.0%} "
        f"k6={frac.get('loadgen', 0):.0%} host={host.get('busy_fraction', 0):.0%}"
        f"(softirq {host.get('softirq_fraction', 0):.0%})  {status}",
        flush=True,
    )
    return step


def run_rate(spec: EdgeSpec, tier: str, rate: int, args: argparse.Namespace,
             doc: dict[str, Any]) -> bool:
    """Run every replicate of one rate; sustained when a majority of them sustain it."""
    passes = 0
    for replicate in range(1, args.replicates + 1):
        step = run_step(spec, tier, rate, replicate, args)
        doc["steps"].append(step)
        passes += step["verdict"]["sustained"]
        # Stop early once the majority is decided either way: the remaining replicates cannot
        # change the verdict and would only heat the host for the next step.
        remaining = args.replicates - replicate
        if passes > args.replicates // 2 or passes + remaining <= args.replicates // 2:
            break
    return passes > args.replicates // 2


def climb(spec: EdgeSpec, tier: str, args: argparse.Namespace, doc: dict[str, Any]) -> dict[str, Any]:
    """Doubling ladder to the first failing rate, then geometric bisection of the gap."""
    last_pass, first_fail = None, None
    for rate in args.rates:
        if run_rate(spec, tier, rate, args, doc):
            last_pass = rate
        else:
            first_fail = rate
            break

    for _ in range(args.bisect):
        if last_pass is None or first_fail is None:
            break
        mid = int(round(math.sqrt(last_pass * first_fail)))
        if mid in (last_pass, first_fail):
            break
        if run_rate(spec, tier, mid, args, doc):
            last_pass = mid
        else:
            first_fail = mid

    def steps_at(rate: int | None) -> list[dict[str, Any]]:
        return [s for s in doc["steps"] if s["tier"] == tier and s["offered_rate"] == rate]

    sustained = [s for s in steps_at(last_pass) if s["verdict"]["sustained"]]
    failing = steps_at(first_fail)
    bindings = [s["utilisation"]["binding"] for s in failing if s["utilisation"]["binding"]]
    return {
        "max_sustained_offered_rate": last_pass,
        "max_sustained_rps": (
            statistics.median(s["throughput_rps"] for s in sustained) if sustained else None
        ),
        "first_failure": {
            "offered_rate": first_fail,
            "reasons": sorted({r for s in failing for r in s["verdict"]["reasons"]}),
            "binding_resource": max(set(bindings), key=bindings.count) if bindings else None,
            "binding_saturated": (
                sum(s["utilisation"].get("saturated", False) for s in failing) > len(failing) / 2
                if failing else None
            ),
            "fraction_of_cpuset": {
                name: statistics.median(
                    s["utilisation"]["fraction_of_cpuset"][name] for s in failing
                    if name in s["utilisation"]["fraction_of_cpuset"]
                )
                for name in CPUSETS
                if any(name in s["utilisation"]["fraction_of_cpuset"] for s in failing)
            },
        } if first_fail is not None else None,
        # CPU per request at the highest sustained rate, and the throughput the edge's own cores
        # would allow at that cost. The second is continuous and independent of which component
        # happened to saturate first, so it can be compared across tiers even when the measured
        # capacities hit different bottlenecks.
        "edge_cpu_us_per_request": (
            statistics.median(
                s["cpu"]["edge"]["cpu_us_per_request"] for s in sustained
                if (s["cpu"].get("edge") or {}).get("cpu_us_per_request")
            ) if sustained else None
        ),
    }


def edge_bound_rps(cpu_us_per_request: float | None) -> float | None:
    if not cpu_us_per_request:
        return None
    return round(cpuset_size(CPUSETS["edge"]) * 1e6 / cpu_us_per_request, 1)


def run_edge(spec: EdgeSpec, results_dir: Path, args: argparse.Namespace) -> int:
    print(f"\n{'=' * 78}\n=== Capacity — {spec.label} ===\n{'=' * 78}")
    print(f"tiers={args.tiers} ladder={args.rates} step={args.duration} "
          f"replicates={args.replicates} bisect={args.bisect} slo={args.slo_ms}ms (worst GET p95)")

    ensure_test_pages()
    if "silent" in args.tiers:
        write_silent_config()
    boot_env = tier_env(spec, "wadm", args.trigger)
    ensure_compose_cleanup(boot_env)
    if spec.needs_build:
        print("Building WASM filter (once)...")
        if run_cmd(spec.compose_prefix() + ["up", "-d", "--build"], env=boot_env).returncode != 0:
            print(f"Failed to build {spec.label}", file=sys.stderr)
            return 1

    doc: dict[str, Any] = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "script": "benchmarks/run_capacity_benchmark.py",
            "schema": "capacity-v2",
            "edge": spec.key,
            "label": spec.label,
            "tiers": args.tiers,
            "ladder": args.rates,
            "bisect": args.bisect,
            "replicates": args.replicates,
            "step_duration": args.duration,
            "slo_ms": args.slo_ms,
            "slo_metric": "worst p95 over the four GET probes",
            "cpusets": dict(CPUSETS),
            "note": (
                "offered_rate is ITERATIONS/s; each iteration issues 9 requests, so "
                "throughput_rps is the served request rate. cpu.*.cpu_us_per_request is CPU "
                "microseconds over the measured window. utilisation.binding is the component "
                "using the largest share of its cpuset; compare capacities across tiers only "
                "when they share a binding resource."
            ),
        },
        "steps": [],
        "capacity": {},
    }

    for tier in args.tiers:
        print(f"\n  --- {tier} ---")
        result = climb(spec, tier, args, doc)
        result["edge_bound_rps_at_measured_cost"] = edge_bound_rps(result["edge_cpu_us_per_request"])
        doc["capacity"][tier] = result

    bare = doc["capacity"].get("bare", {})
    for tier in [t for t in args.tiers if t != "bare"]:
        other = doc["capacity"][tier]
        b_bind = (bare.get("first_failure") or {}).get("binding_resource")
        o_bind = (other.get("first_failure") or {}).get("binding_resource")
        comparable = b_bind is not None and b_bind == o_bind
        retained = (
            round(other["max_sustained_rps"] / bare["max_sustained_rps"], 4)
            if bare.get("max_sustained_rps") and other.get("max_sustained_rps") else None
        )
        cost_ratio = (
            round(bare["edge_cpu_us_per_request"] / other["edge_cpu_us_per_request"], 4)
            if bare.get("edge_cpu_us_per_request") and other.get("edge_cpu_us_per_request") else None
        )
        doc["capacity"][f"{tier}_vs_bare"] = {
            "throughput_retained": retained,
            "same_binding_resource": comparable,
            "binding": {"bare": b_bind, tier: o_bind},
            # The edge-CPU-bound ratio: what fraction of its own capacity the edge keeps once WADM's
            # per-request CPU is added, independent of any other component's limit.
            "edge_capacity_retained_at_measured_cost": cost_ratio,
        }
        print(
            f"\n  {tier} vs bare: throughput retained {retained if retained is not None else 'n/a'}"
            f" ({'same' if comparable else 'DIFFERENT'} binding resource: {b_bind} vs {o_bind}); "
            f"edge-CPU-bound capacity retained {cost_ratio if cost_ratio is not None else 'n/a'}"
        )

    ensure_compose_cleanup(boot_env)
    out = results_dir / f"capacity_{spec.key}.json"
    out.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"Saved {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--edge", choices=RUNNABLE_KEYS)
    group.add_argument("--all", action="store_true")
    parser.add_argument("--rates", default=None, help="Comma-separated offered iterations/sec ladder.")
    parser.add_argument("--duration", default=DEFAULT_STEP_DURATION)
    parser.add_argument("--replicates", type=int, default=DEFAULT_REPLICATES)
    parser.add_argument("--bisect", type=int, default=DEFAULT_BISECT,
                        help="Bisection steps between the last passing and first failing rate.")
    parser.add_argument("--slo-ms", type=float, default=DEFAULT_SLO_MS,
                        help="Worst GET p95 a rate must stay under to count as sustained.")
    parser.add_argument("--tiers", default=",".join(TIERS),
                        help="Comma-separated subset of bare,silent,wadm (default: all three).")
    parser.add_argument("--start-delay", default=DEFAULT_START_DELAY)
    parser.add_argument("--trigger", default=os.getenv("TRIGGER_KEYWORD", DEFAULT_TRIGGER))
    parser.add_argument("--results", default=None,
                        help="Directory to write results to (default benchmarks/results).")
    args = parser.parse_args()

    args.rates = (
        [int(p.strip()) for p in args.rates.split(",") if p.strip()]
        if args.rates else DEFAULT_LADDER
    )
    args.tiers = [t for t in TIERS if t in {x.strip() for x in args.tiers.split(",")}]
    if args.replicates < 1:
        print("Error: --replicates must be at least 1.", file=sys.stderr)
        return 2

    results_dir = Path(args.results) if args.results else Path(__file__).resolve().parent / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    for key in (EDGE_KEYS if args.all else [args.edge]):
        status = run_edge(EDGES[key], results_dir, args)
        if status != 0:
            return status
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
