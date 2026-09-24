#!/usr/bin/env python3
"""
Automate internal Apache mod_lua microsecond benchmark runs via k6.

Runs VU levels [1, 10, 100, 500], parses apache logs for:
  - Apache Detection execution time (us): N
  - Apache Injection execution time (us): N

Writes machine-readable results to:
  benchmarks/results/internal_apache_lua_profile.json  (summary stats, microseconds)
  benchmarks/results/internal_apache_lua_raw.json      (raw per-request samples, microseconds)
  benchmarks/results/e2e_apache_lua_wadm.json          (end-to-end k6 latency, milliseconds)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Compose driving, stats helpers, the cross-edge `WADM TOKEN <kind> <phase> (us):` scraper and
# the k6 end-to-end summary scraper are all shared with the baseline suite; only the
# html_comments regexes below differ per edge.
from wadm_timings import (
    ALL_KINDS,
    DEFAULT_DURATION,
    DEFAULT_START_DELAY,
    WARMUP_DURATION,
    WARMUP_VUS,
    append_e2e_run,
    build_token_sections,
    cycle_loadtester,
    ensure_compose_cleanup,
    fetch_k6_summary,
    format_e2e_line,
    format_token_line,
    k6_iterations,
    new_e2e_document,
    parse_vus,
    run_cmd,
    summarize,
    throughput_check,
    wait_for_quiet_host,
)


DETECTION_RE = re.compile(r"Apache Detection execution time \(us\):\s*(\d+)")
INJECTION_RE = re.compile(r"Apache Injection execution time \(us\):\s*(\d+)")

# Identity of this edge in the shared end-to-end result schema (see wadm_timings.new_e2e_document).
# The baseline suite writes the same schema under tier "bare", which is what makes the two
# directly comparable.
E2E_EDGE = "apache_lua"
E2E_EDGE_CONFIG = "httpd.conf"

DEFAULT_TRIGGER = "internal-admin.example.com"
DEFAULT_TARGET = "http://apache:80"


def parse_timings(apache_logs: str) -> tuple[list[int], list[int]]:
    detection_us = [int(m) for m in DETECTION_RE.findall(apache_logs)]
    injection_us = [int(m) for m in INJECTION_RE.findall(apache_logs)]
    return detection_us, injection_us


def start_apache_stack(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Start backend + apache in detached mode."""
    return run_cmd(
        ["docker", "compose", "--profile", "apache", "up", "-d"],
        env=env,
    )


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    results_dir = repo_root / "benchmarks" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    output_file = results_dir / "internal_apache_lua_profile.json"
    raw_output_file = results_dir / "internal_apache_lua_raw.json"
    # End-to-end latency (milliseconds) goes in its own file rather than into the profile: it is
    # the one plane the no-WADM baseline tiers can also measure, so the comparison plotter reads
    # this alongside e2e_<edge>_bare.json.
    e2e_output_file = results_dir / "e2e_apache_lua_wadm.json"

    duration = os.getenv("K6_DURATION", DEFAULT_DURATION)
    start_delay = os.getenv("K6_START_DELAY", DEFAULT_START_DELAY)
    trigger = os.getenv("TRIGGER_KEYWORD", DEFAULT_TRIGGER)
    target = os.getenv("TARGET", DEFAULT_TARGET)
    vus_list_raw = os.getenv("K6_VUS_LIST")

    try:
        vus_list = parse_vus(vus_list_raw)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    all_results: dict[str, Any] = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "script": "benchmarks/run_internal_apache_lua_benchmark.py",
            "target": target,
            "trigger_keyword": trigger,
            "duration": duration,
            "start_delay": start_delay,
            "vus_list": vus_list,
        },
        "runs": [],
    }

    # Raw per-request latencies (microseconds) are stored separately so the
    # summary profile stays small while box plots can use the full distribution.
    raw_results: dict[str, Any] = {
        "metadata": {
            **all_results["metadata"],
            "note": (
                "Raw per-request latencies in microseconds, one list per VU level. "
                "`tokens` repeats the same samples split by honeytoken kind and phase."
            ),
        },
        "runs": [],
    }

    e2e_results = new_e2e_document(
        script=all_results["metadata"]["script"],
        edge=E2E_EDGE,
        tier="wadm",
        target=target,
        edge_config=E2E_EDGE_CONFIG,
        duration=duration,
        start_delay=start_delay,
        vus_list=vus_list,
    )

    print("=== Internal Apache mod_lua Benchmark ===")
    print(f"VUs: {vus_list}")
    print(f"TARGET={target} K6_DURATION={duration} K6_START_DELAY={start_delay}")
    print("")

    base_env = os.environ.copy()
    base_env["TRIGGER_KEYWORD"] = trigger
    base_env["TARGET"] = target

    # Cool-down before touching anything: the previous edge's 500-VU level leaves the host
    # saturated, and starting here would measure this edge's low-VU levels against a busy
    # machine (an ordering bias, not an edge property).
    print("Waiting for a quiet host...")
    wait_for_quiet_host()

    print("Starting Apache mod_lua stack...")
    ensure_compose_cleanup(base_env)
    start_result = start_apache_stack(base_env)
    if start_result.returncode != 0:
        print(f"Failed to start Apache mod_lua stack:\n{start_result.stderr}", file=sys.stderr)
        return 1
    print("")

    # Warm-up: one throwaway high-VU burst so LuaJIT/caches are hot before the recorded
    # levels. Its result is discarded (not parsed, not recorded); each recorded level
    # isolates its own log lines via --since, so this earlier traffic never leaks into a
    # measured window.
    warmup_env = os.environ.copy()
    warmup_env["K6_VUS"] = str(WARMUP_VUS)
    warmup_env["K6_DURATION"] = WARMUP_DURATION
    warmup_env["K6_START_DELAY"] = start_delay
    warmup_env["TRIGGER_KEYWORD"] = trigger
    warmup_env["TARGET"] = target
    print(f"--- Warm-up (VUs={WARMUP_VUS}, {WARMUP_DURATION}, discarded) ---")
    cycle_loadtester(warmup_env)
    print("")

    for vus in vus_list:
        env = os.environ.copy()
        env["K6_VUS"] = str(vus)
        env["K6_DURATION"] = duration
        env["K6_START_DELAY"] = start_delay
        env["TRIGGER_KEYWORD"] = trigger
        env["TARGET"] = target

        print(f"--- Running VUs={vus} ---")

        up_result, run_start = cycle_loadtester(env)

        since_str = run_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        logs_result = run_cmd(
            ["docker", "compose", "logs", "--no-color", "--since", since_str, "apache"],
            env=env,
        )
        detection_values, injection_values = parse_timings(logs_result.stdout)

        detect_stats = summarize(detection_values)
        inject_stats = summarize(injection_values)

        # Per-honeytoken-kind sections. html_comments is carried over from the two stats
        # above, so `detection`/`injection` stay as the pre-existing plots expect them.
        token_summary, token_raw = build_token_sections(
            logs_result.stdout, detection_values, injection_values
        )

        k6_summary, k6_logs_result = fetch_k6_summary(env, since_str)

        # Host-contamination guard: a shortfall at a reachable level means the machine was
        # busy, not that the edge is slow. Recorded so a bad run is visible in the results.
        #
        # Counted from k6's own iterations rather than the scraped detection lines: it is the
        # direct measure, and it is the only one the baseline tiers can produce, so both suites
        # define the guard identically. `detection.count` below remains as the cross-check, and
        # is fallen back to if the summary line could not be read.
        iterations = k6_iterations(k6_summary) or detect_stats.count
        throughput = throughput_check(vus, duration, iterations)

        run_data = {
            "vus": vus,
            "compose_exit_code": up_result.returncode,
            "throughput": throughput,
            "detection": detect_stats.to_json(),
            "injection": inject_stats.to_json(),
            "tokens": token_summary,
            "errors": {
                "compose_stderr": up_result.stderr.strip(),
                "logs_stderr": logs_result.stderr.strip(),
            },
        }
        all_results["runs"].append(run_data)
        raw_results["runs"].append(
            {
                "vus": vus,
                "detection_us": detection_values,
                "injection_us": injection_values,
                "tokens": token_raw,
            }
        )
        append_e2e_run(
            e2e_results,
            vus=vus,
            throughput=throughput,
            summary=k6_summary,
            errors={"logs_stderr": k6_logs_result.stderr.strip()},
        )

        print(f"Detection: count={detect_stats.count} min_us={detect_stats.min_us} avg_us={detect_stats.avg_us} p90_us={detect_stats.p90_us} max_us={detect_stats.max_us}")
        print(f"Injection: count={inject_stats.count} min_us={inject_stats.min_us} avg_us={inject_stats.avg_us} p90_us={inject_stats.p90_us} max_us={inject_stats.max_us}")
        print("Honeytoken kinds:")
        for kind in ALL_KINDS:
            print(format_token_line(kind, token_summary))
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

    output_file.write_text(json.dumps(all_results, indent=2), encoding="utf-8")
    print(f"Saved results: {output_file}")
    raw_output_file.write_text(json.dumps(raw_results, indent=2), encoding="utf-8")
    print(f"Saved raw samples: {raw_output_file}")
    e2e_output_file.write_text(json.dumps(e2e_results, indent=2), encoding="utf-8")
    print(f"Saved end-to-end latency: {e2e_output_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
