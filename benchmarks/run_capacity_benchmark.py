#!/usr/bin/env python3
"""
Find each edge's maximum sustained throughput, and what a request costs in CPU at that load.

This is the question the old 500-VU level was actually asking, and could not answer. At 500 VUs
the origin tier alone — no proxy, no WADM — showed a 4.9x latency spread across the eight requests
of an iteration and a median of 3.6 ms against 0.6 ms at rate 1. Nearly all of that is queueing
delay. Subtracting two queueing delays does not yield a WADM cost, so latency at saturation was
never interpretable; capacity and cost-per-request are, and they are what saturation is good for.

A staircase of short fixed-rate runs rather than one `ramping-arrival-rate` run: each step gets its
own k6 summary, its own validity verdict and its own pair of cgroup CPU readings, where a single
ramping run would blur every stage into one distribution and one CPU total.

An edge "sustains" a rate when it places the offered iterations (>= 95%), drops almost none
(<= 1%), errors almost never (<= 0.5%), and keeps p95 latency under `--slo-ms`. The highest such
rate is the capacity figure; the first failing rate and its reason are recorded too, because
*which* limit binds first differs by edge and is itself a result.

Writes:
  benchmarks/results/capacity_<edge>.json

Usage:
    python3 benchmarks/run_capacity_benchmark.py --all
    python3 benchmarks/run_capacity_benchmark.py --edge apache_lua --rates 100,200,400,800
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from edge_specs import EDGE_KEYS, EDGES, RUNNABLE_KEYS, EdgeSpec, scan_for_crashes
from wadm_timings import (
    DEFAULT_START_DELAY,
    MAX_DROPPED_RATE,
    MAX_FAILED_RATE,
    MAX_ORIGIN_CPU_US_PER_REQUEST,
    MIN_ACHIEVED_RATIO,
    cpu_cost_per_request,
    cycle_loadtester,
    duration_seconds,
    ensure_compose_cleanup,
    fetch_k6_summary,
    k6_env,
    read_container_cpu,
    run_cmd,
    strip_k6_option_env,
)

DEFAULT_TRIGGER = "internal-admin.example.com"

# Each iteration issues eight requests, so an offered rate of 400/s is 3200 requests/s. The ladder
# starts above the latency suite's top rate and doubles, which brackets the knee in few steps
# without spending a long run on a rate that was never going to bind.
DEFAULT_LADDER = [100, 200, 400, 800, 1600]
DEFAULT_STEP_DURATION = "20s"
DEFAULT_SLO_MS = 100.0

# k6 itself needs VUs in hand to place a high rate; too few and the shortfall is the load
# generator's, not the edge's. Eight in-flight requests per iteration and a target of well under a
# second per iteration means a few hundred VUs is ample even at the top of the ladder.
MAX_VUS_FACTOR = 4
MIN_MAX_VUS = 200


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
    p95 = pooled.get("p95")
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
        reasons.append(f"p95 {p95:.1f} ms over the {slo_ms:.0f} ms SLO")
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
        "p50_ms": pooled.get("med"),
        "p95_ms": p95,
        "p99_ms": pooled.get("p99"),
        "origin_cpu_us_per_request": origin_cpu_us_per_request,
    }


def run_step(spec: EdgeSpec, tier: str, rate: int, args: argparse.Namespace) -> dict[str, Any]:
    env = strip_k6_option_env(os.environ.copy())
    env["TARGET"] = spec.target
    env["TRIGGER_KEYWORD"] = args.trigger
    if spec.config_env:
        config = spec.wadm_config if tier == "wadm" else spec.bare_config
        if config:
            env[spec.config_env] = config

    run_cmd(spec.compose_prefix() + ["up", "-d"], env=env)

    max_vus = max(MIN_MAX_VUS, rate * MAX_VUS_FACTOR)
    shape = k6_env(rate, args.duration, args.start_delay, max_vus=max_vus)

    cpu_before = read_container_cpu(spec.service, env, spec.profile)
    origin_cpu_before = read_container_cpu("backend", env)
    wait_result, run_start = cycle_loadtester({**env, **shape})
    cpu_after = read_container_cpu(spec.service, env, spec.profile)
    origin_cpu_after = read_container_cpu("backend", env)

    since = run_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    summary, _ = fetch_k6_summary({**env, **shape}, since)
    crashes = scan_for_crashes(
        run_cmd(
            spec.compose_prefix() + ["logs", "--no-color", "--since", since, spec.service],
            env=env,
        ).stdout
    ) if spec.profile else []

    offered = rate * duration_seconds(args.duration)
    http_reqs = (summary or {}).get("http_reqs")
    cpu = cpu_cost_per_request(cpu_before, cpu_after, http_reqs)
    origin_cpu = cpu_cost_per_request(origin_cpu_before, origin_cpu_after, http_reqs)
    verdict = step_verdict(
        summary, offered, args.slo_ms, crashes,
        origin_cpu.get("cpu_us_per_request"),
    )

    # Requests actually served per second, which is the capacity number. The offered rate is
    # iterations; each iteration is eight requests.
    seconds = duration_seconds(args.duration)
    throughput_rps = round(http_reqs / seconds, 1) if http_reqs and seconds else None

    step = {
        "tier": tier,
        "offered_rate": rate,
        "offered_iterations": offered,
        "duration": args.duration,
        "throughput_rps": throughput_rps,
        "cpu": cpu,
        "origin_cpu": origin_cpu,
        "verdict": verdict,
        "compose_exit_code": wait_result.returncode,
    }

    status = "sustained" if verdict["sustained"] else "FAILED: " + "; ".join(verdict["reasons"])
    if verdict["sustained"] and verdict.get("warnings"):
        status += "  (WARN: " + "; ".join(verdict["warnings"]) + ")"
    print(
        f"    {tier:5s} rate={rate:5d}/s -> {throughput_rps or 0:8.1f} req/s  "
        f"p95={verdict.get('p95_ms') or 0:8.1f}ms  "
        f"cpu/req={cpu.get('cpu_us_per_request') or 0:7.1f}us  {status}",
        flush=True,
    )
    return step


def run_edge(spec: EdgeSpec, results_dir: Path, args: argparse.Namespace) -> int:
    print(f"\n{'=' * 78}\n=== Capacity — {spec.label} ===\n{'=' * 78}")
    print(f"ladder={args.rates} step={args.duration} slo={args.slo_ms}ms")

    boot_env = strip_k6_option_env(os.environ.copy())
    boot_env["TARGET"] = spec.target
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
            "schema": "capacity-v1",
            "edge": spec.key,
            "label": spec.label,
            "ladder": args.rates,
            "step_duration": args.duration,
            "slo_ms": args.slo_ms,
            "note": (
                "offered_rate is ITERATIONS/s; each iteration issues 8 requests, so "
                "throughput_rps is the served request rate. cpu_us_per_request is CPU "
                "microseconds from cgroup cpu.stat, and is the cost-per-request figure."
            ),
        },
        "steps": [],
        "capacity": {},
    }

    for tier in ("bare", "wadm"):
        print(f"\n  --- {tier} ---")
        sustained_max = None
        for rate in args.rates:
            step = run_step(spec, tier, rate, args)
            doc["steps"].append(step)
            if step["verdict"]["sustained"]:
                sustained_max = rate
            else:
                # Stop climbing once a rate fails: everything above it fails harder, and the run
                # would only spend minutes confirming that while heating the host for the next tier.
                break
        served = [
            s["throughput_rps"] for s in doc["steps"]
            if s["tier"] == tier and s["offered_rate"] == sustained_max
        ]
        doc["capacity"][tier] = {
            "max_sustained_offered_rate": sustained_max,
            "max_sustained_rps": served[0] if served else None,
            "first_failure": next(
                (
                    {"offered_rate": s["offered_rate"], "reasons": s["verdict"]["reasons"]}
                    for s in doc["steps"]
                    if s["tier"] == tier and not s["verdict"]["sustained"]
                ),
                None,
            ),
        }

    bare = doc["capacity"]["bare"]["max_sustained_rps"]
    wadm = doc["capacity"]["wadm"]["max_sustained_rps"]
    if bare and wadm:
        doc["capacity"]["wadm_throughput_retained"] = round(wadm / bare, 4)
        print(
            f"\n  Capacity: bare {bare:.0f} req/s, WADM {wadm:.0f} req/s "
            f"({wadm / bare:.1%} retained)"
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
    parser.add_argument("--slo-ms", type=float, default=DEFAULT_SLO_MS,
                        help="p95 ceiling a rate must stay under to count as sustained.")
    parser.add_argument("--start-delay", default=DEFAULT_START_DELAY)
    parser.add_argument("--trigger", default=os.getenv("TRIGGER_KEYWORD", DEFAULT_TRIGGER))
    args = parser.parse_args()

    args.rates = (
        [int(p.strip()) for p in args.rates.split(",") if p.strip()]
        if args.rates else DEFAULT_LADDER
    )

    results_dir = Path(__file__).resolve().parent / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    for key in (EDGE_KEYS if args.all else [args.edge]):
        status = run_edge(EDGES[key], results_dir, args)
        if status != 0:
            return status
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
