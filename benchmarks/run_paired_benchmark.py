#!/usr/bin/env python3
"""
Measure WADM's cost as a PAIRED bare-vs-WADM difference, replicated, with validity gating.

Replaces the split `run_baseline_benchmark.py` + `run_internal_<edge>_benchmark.py` pair for any
result that is going to be reported as an overhead. Those two scripts measured the two tiers in
separate invocations, typically 15-30 minutes apart, and the figures subtracted one median from
the other. The effect being measured is 5-30 us per request; CPU turbo state, host background
work and vCPU-to-physical-core placement drift by 100-500 us over that interval. The drift set the
sign, which is why "WADM overhead" came out negative for most of the SQLi arms and for several
whole edges.

What this script does differently:

  paired      For one edge and one offered rate, the bare and WADM runs happen within a minute of
              each other, inside one process, with the origin container untouched between them.
              Drift on any timescale longer than that is common to both members of the pair and
              subtracts out.
  replicated  Each pair is repeated `--replicates` times, and the tier order is swapped on
              alternate replicates so that "whichever ran second" cannot become the result.
              The reported number is the median of the per-replicate differences with a bootstrap
              interval, so a difference that is indistinguishable from zero is reported as such
              instead of being drawn as a speed-up.
  gated       Every run is assessed (achieved rate, dropped iterations, failure rate, edge crash
              indicators, preemption-tail share) and a failing run is recorded with its reasons
              instead of silently entering a figure.
  uniform     Every cell gets its own stack start and its own warm-up at its own rate, so no level
              is measured on a hotter edge than another. Previously one 100-VU warm-up preceded a
              ladder that then ran on an increasingly warm stack.
  CPU-costed  cgroup cpu.stat is read either side of each measured window, giving CPU microseconds
              per request. Queueing and hypervisor jitter add wall-clock latency without adding
              CPU time, so this is the one WADM cost that stays measurable at every rate.

Writes:
  benchmarks/results/paired_<edge>.json    every cell: both tiers, all replicates, with verdicts

Run `analyze_paired.py` afterwards to get the paired statistics, the console report, and the
legacy-shaped result files the existing plotters read.

Usage:
    python3 benchmarks/run_paired_benchmark.py --preset smoke --all
    python3 benchmarks/run_paired_benchmark.py --preset full --all
    python3 benchmarks/run_paired_benchmark.py --edge openresty --replicates 3 --rates 1,10
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from edge_specs import EDGE_KEYS, EDGES, RUNNABLE_KEYS, EdgeSpec, count_crashes, scan_for_crashes
from wadm_timings import (
    ALL_KINDS,
    DEFAULT_RATES,
    DEFAULT_REPLICATES,
    DEFAULT_START_DELAY,
    assess_run,
    build_token_sections,
    container_provenance,
    cpu_cost_per_request,
    cycle_loadtester,
    duration_for_rate,
    ensure_compose_cleanup,
    fetch_k6_summary,
    k6_env,
    read_container_cpu,
    run_cmd,
    strip_k6_option_env,
    summarize,
)

DEFAULT_TRIGGER = "internal-admin.example.com"

# Warm-up is run at the SAME rate as the measurement it precedes, so the code paths that get hot
# are the ones about to be measured. A fixed high-rate warm-up (the previous design) leaves a
# rate-1 measurement running against caches and JIT traces warmed by a different load shape.
WARMUP_FRACTION = 0.4
MIN_WARMUP_S = 8

# Presets. `smoke` exists to prove the harness end to end before committing hours to it: same code
# path, same gating, same outputs, short windows and a single replicate. Its numbers are not
# reportable — one replicate cannot produce an interval.
PRESETS = {
    "smoke": {
        "replicates": 1,
        "rates": [1, 10, 100],
        "durations": {1: "20s", 10: "20s", 100: "20s"},
    },
    "full": {
        "replicates": DEFAULT_REPLICATES,
        "rates": DEFAULT_RATES,
        "durations": {1: "90s", 10: "45s", 100: "30s"},
    },
}


def warmup_duration(duration: str) -> str:
    from wadm_timings import duration_seconds

    seconds = max(MIN_WARMUP_S, int(duration_seconds(duration) * WARMUP_FRACTION))
    return f"{seconds}s"


def base_env(spec: EdgeSpec, trigger: str, tier: str) -> dict[str, str]:
    """Environment shared by every Compose call for one cell.

    The config override belongs here rather than on individual commands: `up`, `logs` and `exec`
    must all agree on the mount, or Compose decides the container is out of date mid-run and
    recreates it underneath the measurement.
    """
    env = strip_k6_option_env(os.environ.copy())
    env["TARGET"] = spec.target
    env["TRIGGER_KEYWORD"] = trigger
    if spec.config_env:
        config = spec.wadm_config if tier == "wadm" else spec.bare_config
        if config:
            env[spec.config_env] = config
    return env


def start_edge(spec: EdgeSpec, env: dict[str, str], build: bool = False) -> int:
    """Bring up (or recreate) the edge for this tier, leaving the origin container alone.

    A targeted `up -d` rather than a full down/up: recreating only the edge keeps the origin's
    worker state constant across a pair, which is one less thing that can differ between the two
    halves of a difference.
    """
    command = spec.compose_prefix() + ["up", "-d"]
    if build and spec.needs_build:
        command.append("--build")
    return run_cmd(command, env=env).returncode


def edge_logs(spec: EdgeSpec, env: dict[str, str], since: str) -> str:
    if not spec.profile:
        return ""
    result = run_cmd(
        spec.compose_prefix() + ["logs", "--no-color", "--since", since, spec.service],
        env=env,
    )
    return result.stdout


def worst_tail(token_summary: dict[str, Any]) -> tuple[float | None, int]:
    """Highest preemption-tail share across every kind and phase, with its absolute count.

    Both are needed: the share says how contaminated the distribution is, the count says whether
    the share rests on enough samples to mean anything.
    """
    tails = [
        (phase["tail_pct"], phase.get("tail_over_threshold") or 0)
        for kind in token_summary.values()
        for phase in kind.values()
        if phase.get("count") and phase.get("tail_pct") is not None
    ]
    return max(tails) if tails else (None, 0)


def subsample(values: list[int], cap: int = 20000) -> list[int]:
    """Systematic every-k-th subsample, so pooled raw files stay a sane size in git.

    Systematic rather than random: it preserves the shape of the distribution the box plots draw
    without needing a seed recorded to make the file reproducible.
    """
    if len(values) <= cap:
        return values
    step = len(values) / cap
    return [values[int(i * step)] for i in range(cap)]


def run_cell(
    spec: EdgeSpec,
    tier: str,
    rate: int,
    duration: str,
    replicate: int,
    trigger: str,
    start_delay: str,
) -> dict[str, Any]:
    """One (edge, tier, rate, replicate) measurement, start to finish."""
    env = base_env(spec, trigger, tier)
    label = f"{spec.key}/{tier} rate={rate} rep={replicate}"

    if start_edge(spec, env) != 0:
        return {
            "tier": tier, "rate": rate, "replicate": replicate,
            "validity": {"valid": False, "reasons": ["edge failed to start"]},
        }

    warm = warmup_duration(duration)
    warm_env = {**env, **k6_env(rate, warm, start_delay)}
    print(f"    {label}: warm-up {warm}...", flush=True)
    cycle_loadtester(warm_env)

    cpu_before = read_container_cpu(spec.service, env, spec.profile)
    origin_cpu_before = read_container_cpu("backend", env)

    measure_env = {**env, **k6_env(rate, duration, start_delay, seed=replicate)}
    print(f"    {label}: measuring {duration}...", flush=True)
    wait_result, run_start = cycle_loadtester(measure_env)

    cpu_after = read_container_cpu(spec.service, env, spec.profile)
    origin_cpu_after = read_container_cpu("backend", env)

    since = run_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    k6_summary, k6_logs = fetch_k6_summary(measure_env, since)
    logs = edge_logs(spec, measure_env, since)

    # The bare tier runs no WADM code, so it has no internal regions to scrape. Its cell carries
    # end-to-end latency and CPU cost only, which is exactly the plane the two tiers share.
    token_summary: dict[str, Any] | None = None
    token_raw: dict[str, Any] | None = None
    if tier == "wadm" and spec.detect_re and spec.inject_re:
        detection = [int(m) for m in spec.detect_re.findall(logs)]
        injection = [int(m) for m in spec.inject_re.findall(logs)]
        token_summary, token_raw = build_token_sections(logs, detection, injection)

    crashes = scan_for_crashes(logs)
    tail_pct, tail_count = worst_tail(token_summary) if token_summary else (None, 0)

    http_reqs = (k6_summary or {}).get("http_reqs")
    edge_cpu = cpu_cost_per_request(cpu_before, cpu_after, http_reqs)
    origin_cpu = cpu_cost_per_request(origin_cpu_before, origin_cpu_after, http_reqs)

    validity = assess_run(
        rate, duration, k6_summary,
        crash_patterns=crashes,
        worst_tail_pct=tail_pct,
        crash_count=count_crashes(logs),
        worst_tail_count=tail_count,
        origin_cpu_us_per_request=origin_cpu.get("cpu_us_per_request"),
    )
    cell: dict[str, Any] = {
        "tier": tier,
        "rate": rate,
        "replicate": replicate,
        "duration": duration,
        "started_at": run_start.isoformat(),
        "validity": validity,
        "k6": {
            "iterations": (k6_summary or {}).get("iterations"),
            "http_reqs": http_reqs,
            "http_req_failed_rate": (k6_summary or {}).get("http_req_failed_rate"),
            "dropped_iterations": (k6_summary or {}).get("dropped_iterations"),
            "vus_max_used": (k6_summary or {}).get("vus_max_used"),
            "order": (k6_summary or {}).get("order"),
            "trends": (k6_summary or {}).get("trends") or {},
        },
        "cpu": {"edge": edge_cpu, "origin": origin_cpu},
        "errors": {
            "compose_exit_code": wait_result.returncode,
            "logs_stderr": k6_logs.stderr.strip(),
            "crash_indicators": crashes,
        },
    }
    if token_summary is not None:
        cell["tokens"] = token_summary
        cell["tokens_raw"] = {
            kind: {
                f"{phase}_us": subsample((token_raw or {}).get(kind, {}).get(f"{phase}_us", []))
                for phase in ("detect", "inject")
            }
            for kind in ALL_KINDS
        }

    verdict = "ok" if validity["valid"] else "REJECTED: " + "; ".join(validity["reasons"])
    if validity["valid"] and validity["warnings"]:
        verdict = "ok (WARN: " + "; ".join(validity["warnings"]) + ")"
    cpu_pr = edge_cpu.get("cpu_us_per_request")
    print(
        f"    {label}: {validity['iterations']} iters, "
        f"{validity['achieved_ratio'] or 0:.0%} of offered, "
        f"cpu/req={cpu_pr if cpu_pr is not None else 'n/a'}us -> {verdict}",
        flush=True,
    )
    return cell


def run_edge(
    spec: EdgeSpec, results_dir: Path, args: argparse.Namespace
) -> int:
    doc: dict[str, Any] = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "script": "benchmarks/run_paired_benchmark.py",
            "schema": "paired-v1",
            "edge": spec.key,
            "label": spec.label,
            "target": spec.target,
            "wadm_config": spec.wadm_config,
            "bare_config": spec.bare_config,
            "rates": args.rates,
            "durations": {str(r): duration_for_rate(r, args.durations.get(r)) for r in args.rates},
            "replicates": args.replicates,
            "start_delay": args.start_delay,
            "order": "rotate",
            "note": (
                "Paired bare-vs-WADM cells. Latency trends are MILLISECONDS (k6); internal token "
                "timings are MICROSECONDS (edge logs); cpu_us_per_request is CPU microseconds "
                "from cgroup cpu.stat. Only cells with validity.valid may be reported."
            ),
        },
        "cells": [],
    }

    print(f"\n{'=' * 78}\n=== Paired benchmark — {spec.label} ===\n{'=' * 78}")
    print(f"rates={args.rates} replicates={args.replicates}")

    boot_env = base_env(spec, args.trigger, "wadm")
    ensure_compose_cleanup(boot_env)

    # Build the WASM filter once per edge rather than per cell: the Rust build is minutes long and
    # its artefact is identical for both tiers.
    if spec.needs_build:
        print("Building WASM filter (once)...")
        if start_edge(spec, boot_env, build=True) != 0:
            print(f"Failed to build/start {spec.label}", file=sys.stderr)
            return 1

    captured_provenance = False
    for rate in args.rates:
        duration = duration_for_rate(rate, args.durations.get(rate))
        print(f"\n  --- rate={rate}/s duration={duration} ---")
        for replicate in range(1, args.replicates + 1):
            # Alternate which tier goes first. If anything systematic remains about running
            # second, it now lands on both tiers equally instead of on one of them.
            tiers = ["bare", "wadm"] if replicate % 2 == 1 else ["wadm", "bare"]
            for tier in tiers:
                doc["cells"].append(
                    run_cell(spec, tier, rate, duration, replicate, args.trigger, args.start_delay)
                )
                if not captured_provenance:
                    env = base_env(spec, args.trigger, tier)
                    doc["metadata"]["provenance"] = {
                        "edge": container_provenance(spec.service, env, spec.profile),
                        "origin": container_provenance("backend", env),
                    }
                    captured_provenance = True

    ensure_compose_cleanup(boot_env)

    out = results_dir / f"paired_{spec.key}.json"
    out.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    valid = sum(1 for c in doc["cells"] if c["validity"]["valid"])
    print(f"\nSaved {out}  ({valid}/{len(doc['cells'])} cells valid)")
    return 0


def parse_rates(raw: str | None, default: list[int]) -> list[int]:
    if not raw:
        return default
    rates = [int(p.strip()) for p in raw.split(",") if p.strip()]
    if any(r <= 0 for r in rates):
        raise ValueError("Rates must be positive integers (offered iterations per second).")
    return rates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--edge", choices=RUNNABLE_KEYS,
        help="Run one edge, or a remediation variant (variants are excluded from --all).",
    )
    group.add_argument("--all", action="store_true", help="Run all four comparable edges, sequentially.")
    parser.add_argument("--preset", choices=sorted(PRESETS), default="full")
    parser.add_argument("--replicates", type=int, help="Override the preset's replicate count.")
    parser.add_argument("--rates", help="Comma-separated offered iterations/sec.")
    parser.add_argument("--duration", help="Force one duration for every rate.")
    parser.add_argument("--start-delay", default=DEFAULT_START_DELAY)
    parser.add_argument("--trigger", default=os.getenv("TRIGGER_KEYWORD", DEFAULT_TRIGGER))
    args = parser.parse_args()

    preset = PRESETS[args.preset]
    try:
        args.rates = parse_rates(args.rates, preset["rates"])
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if args.replicates is None:
        args.replicates = preset["replicates"]
    args.durations = (
        {r: args.duration for r in args.rates} if args.duration else dict(preset["durations"])
    )

    if args.replicates < 2 and args.preset != "smoke":
        print(
            "Warning: fewer than 2 replicates cannot produce a confidence interval; "
            "the result will have no error bars.",
            file=sys.stderr,
        )

    results_dir = Path(__file__).resolve().parent / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    keys = EDGE_KEYS if args.all else [args.edge]
    for key in keys:
        status = run_edge(EDGES[key], results_dir, args)
        if status != 0:
            return status

    print("\nNext: python3 benchmarks/analyze_paired.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
