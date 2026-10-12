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
  three-tier  bare (no WADM), silent (WADM without the benchmark's timing log lines) and wadm (the
              instrumented build the in-edge timers are read from). silent - bare is WADM's cost
              as deployed; wadm - silent is the cost of measuring it.
  replicated  Each set is repeated `--replicates` times, and the tier order is balanced across
              replicates (tier_order) so that "whichever ran second" cannot become the result.
              The reported number is the median of the per-replicate differences with a bootstrap
              interval, so a difference that is indistinguishable from zero is reported as such
              instead of being drawn as a speed-up.
  gated       Every run is assessed (achieved rate, dropped iterations, failure rate, edge crash
              indicators, preemption-tail share) and a failing run is recorded with its reasons
              instead of silently entering a figure.
  uniform     Every cell gets its own stack start and its own warm-up at its own rate, so no level
              is measured on a hotter edge than another. Previously one 100-VU warm-up preceded a
              ladder that then ran on an increasingly warm stack.
  CPU-costed  The edge's, origin's and load generator's cgroup counters are sampled from the host
              and integrated over exactly the window k6 measured, giving CPU microseconds per
              request, cores in use and memory. Queueing adds wall-clock latency without adding
              CPU time, so this is the WADM cost that stays measurable at every rate.
  checked     test.js compares every response with the reference the edge returned before the
              load started, records which requests had to reconnect, and the runner counts the
              alert lines the window produced — so functional damage under load is recorded, not
              assumed away.

Writes:
  benchmarks/results/paired_<edge>.json    every cell: all tiers, all replicates, with verdicts
  benchmarks/results/<mix>_<edge>.json     the same, for --mix get|post|size

Run `analyze_paired.py` afterwards to get the paired statistics and the console report.

Usage:
    python3 benchmarks/run_paired_benchmark.py --preset smoke --all
    python3 benchmarks/run_paired_benchmark.py --preset full --all
    python3 benchmarks/run_paired_benchmark.py --edge openresty --replicates 3 --rates 1,10
    python3 benchmarks/run_paired_benchmark.py --all --mix get --rates 100 --replicates 3
    python3 benchmarks/run_paired_benchmark.py --edge wasm --mixes sqli-hit,sqli-miss --no-prime \
        --rates 100 --duration 60s --replicates 7
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from edge_specs import (
    EDGE_KEYS,
    EDGES,
    RUNNABLE_KEYS,
    TIERS,
    EdgeSpec,
    count_crashes,
    scan_for_crashes,
    write_silent_config,
)
from wadm_timings import (
    ALL_KINDS,
    CgroupSampler,
    DEFAULT_RATES,
    DEFAULT_REPLICATES,
    DEFAULT_START_DELAY,
    assess_run,
    build_token_sections,
    cgroup_dir,
    container_id,
    container_provenance,
    count_alerts,
    cpu_cost_per_request,
    cycle_loadtester,
    duration_for_rate,
    duration_seconds,
    ensure_compose_cleanup,
    ensure_test_pages,
    fetch_k6_summary,
    host_stalls,
    k6_env,
    k6_window_start,
    lines_since,
    read_container_cpu,
    run_cmd,
    strip_k6_option_env,
    summarize,
    tier_order,
)

DEFAULT_TRIGGER = "internal-admin.example.com"

# Any per-region timing line, from either family (see wadm_timings.py's module docstring).
TIMING_LINE_RE = re.compile(r"execution time \(us\):|WADM TOKEN \w+ (?:detect|inject) \(us\):")

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
    seconds = max(MIN_WARMUP_S, int(duration_seconds(duration) * WARMUP_FRACTION))
    return f"{seconds}s"


def base_env(spec: EdgeSpec, trigger: str, tier: str) -> dict[str, str]:
    """Environment shared by every Compose call for one cell.

    The config overrides belong here rather than on individual commands: `up`, `logs` and `exec`
    must all agree on the mounts, or Compose decides the container is out of date mid-run and
    recreates it underneath the measurement.
    """
    env = strip_k6_option_env(os.environ.copy())
    env["TARGET"] = spec.target
    env["TRIGGER_KEYWORD"] = trigger
    env.update(spec.tier_env(tier))
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
    """The edge's log lines since `since`, each carrying Docker's receive timestamp."""
    if not spec.profile:
        return ""
    result = run_cmd(
        spec.compose_prefix()
        + ["logs", "--no-color", "--timestamps", "--since", since, spec.service],
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


def make_sampler(spec: EdgeSpec, env: dict[str, str]) -> CgroupSampler:
    """Sampler over the edge, the origin, and the load generator once it is created."""
    loadgen_env = {**env}

    def loadgen_dir():
        return cgroup_dir(container_id("load-tester", loadgen_env, "loadtest"))

    return CgroupSampler(
        {
            "edge": cgroup_dir(container_id(spec.service, env, spec.profile)),
            "origin": cgroup_dir(container_id("backend", env)),
        },
        late={"loadgen": loadgen_dir},
    )


def run_cell(
    spec: EdgeSpec,
    tier: str,
    rate: int,
    duration: str,
    replicate: int,
    trigger: str,
    start_delay: str,
    mix: str = "all",
    prime: bool = True,
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
    warm_env = {**env, **k6_env(rate, warm, start_delay, mix=mix, checks=False, prime=prime)}
    print(f"    {label}: warm-up {warm}...", flush=True)
    cycle_loadtester(warm_env)

    measure_env = {
        **env, **k6_env(rate, duration, start_delay, seed=replicate, mix=mix, prime=prime)
    }
    print(f"    {label}: measuring {duration}...", flush=True)
    sampler = make_sampler(spec, env)
    # The exec-based reads are kept only as a fallback for a host whose cgroup tree the sampler
    # cannot find; they bracket the whole load-tester lifecycle rather than the measured window.
    cpu_before = read_container_cpu(spec.service, env, spec.profile)
    origin_cpu_before = read_container_cpu("backend", env)
    with sampler:
        wait_result, run_start = cycle_loadtester(measure_env)
    cpu_after = read_container_cpu(spec.service, env, spec.profile)
    origin_cpu_after = read_container_cpu("backend", env)

    since = run_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    k6_summary, k6_logs = fetch_k6_summary(measure_env, since)
    stamped = edge_logs(spec, measure_env, since)
    logs = lines_since(stamped, 0.0)

    # Everything the edge logged before k6's first measured iteration — setup()'s reference
    # requests and the tail of the warm-up — is excluded from samples and alert counts, so both can
    # be compared exactly against the iterations k6 reports. Crash scanning keeps the whole window.
    window_start = k6_window_start(k6_logs.stdout)
    window_end = window_start + duration_seconds(duration) if window_start else None
    window_logs = lines_since(stamped, window_start) if window_start else logs

    token_summary: dict[str, Any] | None = None
    token_raw: dict[str, Any] | None = None
    if tier == "wadm" and spec.detect_re and spec.inject_re:
        detection = [int(m) for m in spec.detect_re.findall(window_logs)]
        injection = [int(m) for m in spec.inject_re.findall(window_logs)]
        token_summary, token_raw = build_token_sections(window_logs, detection, injection)

    crashes = scan_for_crashes(logs)
    tail_pct, tail_count = worst_tail(token_summary) if token_summary else (None, 0)

    requests = (k6_summary or {}).get("edge_requests") or (k6_summary or {}).get("http_reqs")
    if window_start and sampler.samples.get("edge"):
        edge_cpu = sampler.window("edge", window_start, window_end, requests)
        origin_cpu = sampler.window("origin", window_start, window_end, requests)
        loadgen_cpu = sampler.window("loadgen", window_start, window_end, None)
        edge_cpu["idle_cpu_us_per_s"] = sampler.idle_rate("edge", window_start)
    else:
        edge_cpu = cpu_cost_per_request(cpu_before, cpu_after, requests)
        origin_cpu = cpu_cost_per_request(origin_cpu_before, origin_cpu_after, requests)
        loadgen_cpu = {"available": False}
    lifecycle_cpu = cpu_cost_per_request(cpu_before, cpu_after, requests)

    # The warm-up's last seconds are included: an edge resuming from a freeze is not yet in the state
    # the measurement assumes.
    stalls = host_stalls(window_start - 30, window_end) if window_start else []
    validity = assess_run(
        rate, duration, k6_summary,
        crash_patterns=crashes,
        worst_tail_pct=tail_pct,
        crash_count=count_crashes(logs),
        worst_tail_count=tail_count,
        origin_cpu_us_per_request=origin_cpu.get("cpu_us_per_request"),
        host_stalls=stalls,
    )
    summary = k6_summary or {}
    cell: dict[str, Any] = {
        "tier": tier,
        "rate": rate,
        "replicate": replicate,
        "duration": duration,
        "mix": mix,
        "started_at": run_start.isoformat(),
        "window": {"start": window_start, "end": window_end},
        "host_stalls": stalls,
        "validity": validity,
        "k6": {
            "iterations": summary.get("iterations"),
            "http_reqs": summary.get("http_reqs"),
            "edge_requests": summary.get("edge_requests"),
            "data_received": summary.get("data_received"),
            "http_req_failed_rate": summary.get("http_req_failed_rate"),
            "dropped_iterations": summary.get("dropped_iterations"),
            "vus_max_used": summary.get("vus_max_used"),
            "order": summary.get("order"),
            "prime": summary.get("prime"),
            "probes": summary.get("probes"),
            "trends": summary.get("trends") or {},
            "new_conn": summary.get("new_conn") or {},
            "intact": summary.get("intact") or {},
            "reference": summary.get("reference") or {},
        },
        "cpu": {
            "edge": edge_cpu,
            "origin": origin_cpu,
            "loadgen": loadgen_cpu,
            "edge_lifecycle": lifecycle_cpu,
        },
        "alerts": count_alerts(window_logs) if window_start else None,
        # Self-check on the tier switch: bare and silent must log none of these.
        "timing_lines": len(TIMING_LINE_RE.findall(window_logs)),
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
    intact = [
        v["rate"] for v in (summary.get("intact") or {}).values()
        if v and v.get("rate") is not None
    ]
    intact_s = f"{min(intact):.2%}" if intact else "n/a"
    print(
        f"    {label}: {validity['iterations']} iters, "
        f"{validity['achieved_ratio'] or 0:.0%} of offered, "
        f"cpu/req={cpu_pr if cpu_pr is not None else 'n/a'}us, "
        f"worst intact={intact_s} -> {verdict}",
        flush=True,
    )
    return cell


def output_name(edge: str, mix: str) -> str:
    """paired_<edge>.json for the reportable mix; <mix>_<edge>.json for the auxiliary runs.

    Different prefixes so analyze_paired.py's discovery of paired_<edge>.json never picks up a
    GET-only or size-sweep run as if it were the main dataset.
    """
    return f"paired_{edge}.json" if mix == "all" else f"{mix}_{edge}.json"


def run_edge(
    spec: EdgeSpec, results_dir: Path, args: argparse.Namespace
) -> int:
    doc: dict[str, Any] = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "script": "benchmarks/run_paired_benchmark.py",
            "schema": "paired-v2",
            "edge": spec.key,
            "label": spec.label,
            "target": spec.target,
            "wadm_config": spec.wadm_config,
            "bare_config": spec.bare_config,
            "tiers": args.tiers,
            "mix": args.mixes[0],
            "prime": not args.no_prime,
            "rates": args.rates,
            "durations": {str(r): duration_for_rate(r, args.durations.get(r)) for r in args.rates},
            "replicates": args.replicates,
            "start_delay": args.start_delay,
            "order": "williams",
            "note": (
                "Paired cells, one per tier per replicate. Latency trends are MILLISECONDS (k6); "
                "internal token timings are MICROSECONDS (edge logs, wadm tier only); "
                "cpu.*.cpu_us_per_request is CPU microseconds over the measured window from the "
                "host cgroup sampler, divided by k6.edge_requests. Only cells with validity.valid "
                "may be reported."
            ),
        },
        "cells": [],
    }

    print(f"\n{'=' * 78}\n=== Paired benchmark — {spec.label} ({', '.join(args.mixes)}) ===\n{'=' * 78}")
    print(f"tiers={args.tiers} rates={args.rates} replicates={args.replicates}")

    ensure_test_pages()
    if "silent" in args.tiers:
        write_silent_config()
    boot_env = base_env(spec, args.trigger, "wadm")
    ensure_compose_cleanup(boot_env)

    # Build the WASM filter once per edge rather than per cell: the Rust build is minutes long and
    # its artefact is identical for every tier.
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
            # Balanced across replicates: whatever remains systematic about running first or last,
            # or right after a particular tier, lands on every tier equally.
            for tier in tier_order(args.tiers, replicate):
                # With several mixes, each runs inside the same replicate and tier, minutes apart,
                # so a difference BETWEEN mixes (a SQLi hit against a miss) is paired in time just
                # as a difference between tiers is. Their order alternates by replicate.
                mixes = args.mixes if replicate % 2 else args.mixes[::-1]
                for mix in mixes:
                    doc["cells"].append(
                        run_cell(spec, tier, rate, duration, replicate, args.trigger,
                                 args.start_delay, mix, not args.no_prime)
                    )
                if not captured_provenance:
                    env = base_env(spec, args.trigger, tier)
                    doc["metadata"]["provenance"] = {
                        "edge": container_provenance(spec.service, env, spec.profile),
                        "origin": container_provenance("backend", env),
                    }
                    captured_provenance = True

    ensure_compose_cleanup(boot_env)

    # One file per mix, so every reader keeps seeing one mix per file; an interleaved run records
    # which mixes shared its replicates.
    for mix in args.mixes:
        part = {
            "metadata": {**doc["metadata"], "mix": mix, "interleaved_with": args.mixes},
            "cells": [c for c in doc["cells"] if c.get("mix") == mix],
        }
        out = results_dir / output_name(spec.key, mix)
        out.write_text(json.dumps(part, indent=2), encoding="utf-8")
        valid = sum(1 for c in part["cells"] if c["validity"]["valid"])
        print(f"\nSaved {out}  ({valid}/{len(part['cells'])} cells valid)")
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
    parser.add_argument(
        "--tiers", default=",".join(TIERS),
        help="Comma-separated subset of bare,silent,wadm (default: all three).",
    )
    parser.add_argument(
        "--results", default=None,
        help="Directory to write results to (default benchmarks/results). Point smoke runs "
             "elsewhere so they cannot overwrite the reportable dataset.",
    )
    parser.add_argument(
        "--mix", choices=["all", "get", "post", "size", "sqli-hit", "sqli-miss"], default="all",
        help="Probe set (test.js WADM_MIX). Anything but `all` writes <mix>_<edge>.json.",
    )
    parser.add_argument(
        "--mixes", default=None,
        help="Comma-separated mixes to interleave inside every replicate and tier, e.g. "
             "sqli-hit,sqli-miss; overrides --mix and writes one file per mix.",
    )
    parser.add_argument(
        "--no-prime", action="store_true",
        help="Drop the unrecorded priming GET from each iteration (WADM_PRIME=0), so CPU per "
             "iteration holds only the probes of the mix — used for the sqli-hit/sqli-miss runs.",
    )
    args = parser.parse_args()

    args.tiers = [t for t in TIERS if t in {x.strip() for x in args.tiers.split(",")}]
    valid_mixes = {"all", "get", "post", "size", "sqli-hit", "sqli-miss"}
    args.mixes = [m.strip() for m in args.mixes.split(",")] if args.mixes else [args.mix]
    if not set(args.mixes) <= valid_mixes or len(set(args.mixes)) != len(args.mixes):
        print(f"Error: --mixes must be distinct values from {sorted(valid_mixes)}.", file=sys.stderr)
        return 2
    if "bare" not in args.tiers or len(args.tiers) < 2:
        print("Error: --tiers must include bare and at least one WADM tier.", file=sys.stderr)
        return 2

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

    results_dir = Path(args.results) if args.results else Path(__file__).resolve().parent / "results"
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
