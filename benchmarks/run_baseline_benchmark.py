#!/usr/bin/env python3
"""
Measure end-to-end request latency with WADM absent — the baseline the WADM runs are compared to.

Three tiers exist across the two suites; this script produces the lower two:

    origin   k6 -> backend                      floor: network + origin only
    bare     k6 -> edge (no WADM) -> backend    proxy cost      (bare - origin)
    wadm     k6 -> edge (WADM)    -> backend    full cost       (wadm - bare), written by
                                                run_internal_<edge>_benchmark.py

A bare edge runs no detection or injection code, so it has no internal microsecond timers to
scrape. End-to-end latency, which k6 reports for every tier, is therefore the only plane on which
baseline and WADM are comparable — hence this script scrapes the *load-tester's* logs rather than
the edge's, and writes only the `e2e_*` result family.

The bare tier reuses the SAME Compose service, port and network path as the WADM tier; only the
mounted edge config differs (via the ${..._CONF} overrides in docker-compose.yml). VU ladder,
duration, warm-up and quiet-host guard are shared with the WADM suite through wadm_timings, so
both land on one x-axis.

Writes machine-readable results to:
  benchmarks/results/e2e_<edge>_bare.json   (end-to-end k6 latency, milliseconds)

Usage:
    python3 benchmarks/run_baseline_benchmark.py --all
    python3 benchmarks/run_baseline_benchmark.py --edge openresty
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from wadm_timings import (
    DEFAULT_DURATION,
    DEFAULT_START_DELAY,
    WARMUP_DURATION,
    WARMUP_VUS,
    append_e2e_run,
    cycle_loadtester,
    ensure_compose_cleanup,
    fetch_k6_summary,
    format_e2e_line,
    k6_iterations,
    new_e2e_document,
    parse_vus,
    run_cmd,
    throughput_check,
    wait_for_quiet_host,
)


@dataclass(frozen=True)
class EdgeSpec:
    label: str
    profile: str | None
    target: str
    config_env: str | None
    baseline_config: str | None


# `profile: None` marks the origin tier — `backend` carries no Compose profile, so it is already
# running and there is no edge to configure. Every other key mounts its WADM-free config into the
# stock service, keeping service name, listener port and DNS path identical to the WADM run.
EDGE_SPECS = {
    "openresty": EdgeSpec(
        "OpenResty", "openresty", "http://openresty:80",
        "OPENRESTY_CONF", "./nginx/nginx-baseline.conf",
    ),
    "envoy_lua": EdgeSpec(
        "Envoy+Lua", "envoy", "http://envoy:8080",
        "ENVOY_CONF", "./envoy/envoy-baseline.yaml",
    ),
    "wasm": EdgeSpec(
        "WASM (Envoy)", "wasm", "http://envoy-wasm:8080",
        "ENVOY_WASM_CONF", "./envoy-wasm/envoy-wasm-baseline.yaml",
    ),
    "apache_lua": EdgeSpec(
        "Apache+Lua", "apache", "http://apache:80",
        "HTTPD_CONF", "./httpd-baseline.conf",
    ),
    "origin": EdgeSpec("Origin only", None, "http://backend:80", None, None),
}

DEFAULT_TRIGGER = "internal-admin.example.com"


def build_env(spec: EdgeSpec, target: str, trigger: str) -> dict[str, str]:
    """Base environment for every Compose call in this edge's run.

    Setting the config override here rather than per-command means `up`, `down` and `logs` all
    see the same mount, so Compose never decides the container is out of date mid-run.
    """
    env = os.environ.copy()
    env["TARGET"] = target
    env["TRIGGER_KEYWORD"] = trigger
    if spec.config_env and spec.baseline_config:
        env[spec.config_env] = spec.baseline_config
    return env


def start_stack(spec: EdgeSpec, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Bring up backend plus (for a proxied tier) the edge, with its baseline config mounted."""
    command = ["docker", "compose"]
    if spec.profile:
        command += ["--profile", spec.profile]
    command += ["up", "-d"]
    return run_cmd(command, env=env)


def run_edge(key: str, spec: EdgeSpec, results_dir: Path, args: argparse.Namespace) -> int:
    target = spec.target
    output_file = results_dir / f"e2e_{key}_bare.json"

    doc = new_e2e_document(
        script="benchmarks/run_baseline_benchmark.py",
        edge=key,
        tier="bare",
        target=target,
        edge_config=spec.baseline_config,
        duration=args.duration,
        start_delay=args.start_delay,
        vus_list=args.vus_list,
    )

    print(f"=== Baseline (no WADM) — {spec.label} ===")
    print(f"VUs: {args.vus_list}")
    print(f"TARGET={target} K6_DURATION={args.duration} K6_START_DELAY={args.start_delay}")
    print(f"edge config: {spec.baseline_config or '(none — origin tier)'}")
    print("")

    base_env = build_env(spec, target, args.trigger)

    # Same cool-down the WADM suite uses: the previous 500-VU level saturates this 2-core host and
    # the load takes time to decay, so starting immediately would measure the low-VU levels of
    # whichever tier came later against a busy machine — an ordering bias, not an edge property.
    print("Waiting for a quiet host...")
    wait_for_quiet_host()

    print(f"Starting {spec.label} stack...")
    ensure_compose_cleanup(base_env)
    start_result = start_stack(spec, base_env)
    if start_result.returncode != 0:
        print(f"Failed to start {spec.label} stack:\n{start_result.stderr}", file=sys.stderr)
        return 1
    print("")

    warmup_env = build_env(spec, target, args.trigger)
    warmup_env["K6_VUS"] = str(WARMUP_VUS)
    warmup_env["K6_DURATION"] = WARMUP_DURATION
    warmup_env["K6_START_DELAY"] = args.start_delay
    print(f"--- Warm-up (VUs={WARMUP_VUS}, {WARMUP_DURATION}, discarded) ---")
    cycle_loadtester(warmup_env)
    print("")

    for vus in args.vus_list:
        env = build_env(spec, target, args.trigger)
        env["K6_VUS"] = str(vus)
        env["K6_DURATION"] = args.duration
        env["K6_START_DELAY"] = args.start_delay

        print(f"--- Running VUs={vus} ---")
        up_result, run_start = cycle_loadtester(env)

        since_str = run_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        k6_summary, k6_logs_result = fetch_k6_summary(env, since_str)

        throughput = throughput_check(vus, args.duration, k6_iterations(k6_summary))
        append_e2e_run(
            doc,
            vus=vus,
            throughput=throughput,
            summary=k6_summary,
            errors={
                "compose_stderr": up_result.stderr.strip(),
                "logs_stderr": k6_logs_result.stderr.strip(),
            },
        )

        print(format_e2e_line(k6_summary))
        ratio = throughput["throughput_ratio"]
        if ratio is not None:
            note = ""
            if throughput["expected_reachable"] and ratio < 0.9:
                note = "  <-- WARNING: host was busy, treat this level as invalid"
            print(f"Throughput: {throughput['iterations']}/{throughput['expected_iterations']}"
                  f" iterations ({ratio:.0%} of the sleep(1) ceiling){note}")
        print(f"compose_exit_code={up_result.returncode}")
        print("")

    ensure_compose_cleanup(base_env)

    output_file.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"Saved end-to-end latency: {output_file}")
    print("")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--edge", choices=sorted(EDGE_SPECS), help="Run one tier.")
    group.add_argument("--all", action="store_true", help="Run every tier, sequentially.")
    parser.add_argument("--duration", default=os.getenv("K6_DURATION", DEFAULT_DURATION))
    parser.add_argument("--start-delay", default=os.getenv("K6_START_DELAY", DEFAULT_START_DELAY))
    parser.add_argument("--trigger", default=os.getenv("TRIGGER_KEYWORD", DEFAULT_TRIGGER))
    parser.add_argument(
        "--vus",
        default=os.getenv("K6_VUS_LIST"),
        help="Comma-separated VU levels. Defaults to the ladder the WADM suite uses.",
    )
    args = parser.parse_args()

    try:
        args.vus_list = parse_vus(args.vus)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    results_dir = Path(__file__).resolve().parent / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    # `origin` last: it is the cheapest tier, so leaving it until the end means the expensive edge
    # tiers are not the ones competing with a still-decaying load average.
    keys = sorted(EDGE_SPECS, key=lambda k: (k == "origin", k)) if args.all else [args.edge]

    for key in keys:
        status = run_edge(key, EDGE_SPECS[key], results_dir, args)
        if status != 0:
            return status
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
