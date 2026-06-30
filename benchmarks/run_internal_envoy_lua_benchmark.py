#!/usr/bin/env python3
"""
Automate internal Envoy Lua microsecond benchmark runs via k6.

Runs VU levels [1, 10, 100, 500], parses envoy logs for:
  - Envoy Lua Detection execution time (us): N
  - Envoy Lua Injection execution time (us): N

Writes machine-readable results to:
  benchmarks/results/internal_envoy_lua_profile.json
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DETECTION_RE = re.compile(r"Envoy Lua Detection execution time \(us\):\s*(\d+)")
INJECTION_RE = re.compile(r"Envoy Lua Injection execution time \(us\):\s*(\d+)")

DEFAULT_VUS = [1, 10, 100, 500]
DEFAULT_DURATION = "30s"
DEFAULT_START_DELAY = "5s"
DEFAULT_TRIGGER = "internal-admin.example.com"
DEFAULT_TARGET = "http://envoy:8080"


@dataclass
class PhaseStats:
    count: int
    min_us: int | None
    avg_us: float | None
    p90_us: float | None
    max_us: int | None

    def to_json(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "min_us": self.min_us,
            "avg_us": self.avg_us,
            "p90_us": self.p90_us,
            "max_us": self.max_us,
        }


def run_cmd(command: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, env=env, text=True, capture_output=True, check=False)


def percentile_nearest_rank(values: list[int], pct: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = int(math.ceil((pct / 100.0) * len(ordered)))
    idx = max(1, rank) - 1
    return float(ordered[idx])


def summarize(values: list[int]) -> PhaseStats:
    if not values:
        return PhaseStats(count=0, min_us=None, avg_us=None, p90_us=None, max_us=None)
    return PhaseStats(
        count=len(values),
        min_us=min(values),
        avg_us=round(statistics.fmean(values), 2),
        p90_us=round(percentile_nearest_rank(values, 90) or 0.0, 2),
        max_us=max(values),
    )


def parse_timings(envoy_logs: str) -> tuple[list[int], list[int]]:
    detection_us = [int(m) for m in DETECTION_RE.findall(envoy_logs)]
    injection_us = [int(m) for m in INJECTION_RE.findall(envoy_logs)]
    return detection_us, injection_us


def parse_vus(raw: str | None) -> list[int]:
    if not raw:
        return DEFAULT_VUS
    parsed: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            vus = int(part)
            if vus <= 0:
                raise ValueError
            parsed.append(vus)
        except ValueError:
            raise ValueError(f"Invalid VU value '{part}'. Expected positive integers.") from None
    if not parsed:
        raise ValueError("No valid VU values were provided.")
    return parsed


def ensure_compose_cleanup(base_env: dict[str, str]) -> None:
    run_cmd(["docker", "compose", "down", "--remove-orphans"], env=base_env)
    # Prune networks left behind by interrupted or partially-cleaned runs; otherwise
    # the next `docker compose up` fails with "network <id> not found".
    run_cmd(["docker", "network", "prune", "-f"], env=base_env)


def start_envoy_stack(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Start backend + envoy in detached mode. Lua is interpreted so there is no build step."""
    return run_cmd(
        ["docker", "compose", "--profile", "envoy", "up", "-d"],
        env=env,
    )


def cycle_loadtester(env: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], datetime]:
    """Remove any leftover load-tester container, start a fresh one, and wait for k6 to finish.

    The envoy and backend containers are left running so Envoy state is preserved between
    VU levels. Returns the wait result and a timestamp captured just before the container
    started, used with --since to isolate this run's Envoy log lines from previous runs.
    """
    run_cmd(
        ["docker", "compose", "--profile", "loadtest", "rm", "-f", "-s", "load-tester"],
        env=env,
    )
    run_start = datetime.now(timezone.utc)
    run_cmd(
        ["docker", "compose", "--profile", "loadtest", "up", "-d", "load-tester"],
        env=env,
    )
    wait_result = run_cmd(
        ["docker", "compose", "--profile", "loadtest", "wait", "load-tester"],
        env=env,
    )
    return wait_result, run_start


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    results_dir = repo_root / "benchmarks" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    output_file = results_dir / "internal_envoy_lua_profile.json"

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
            "script": "benchmarks/run_internal_envoy_lua_benchmark.py",
            "target": target,
            "trigger_keyword": trigger,
            "duration": duration,
            "start_delay": start_delay,
            "vus_list": vus_list,
        },
        "runs": [],
    }

    print("=== Internal Envoy Lua Benchmark ===")
    print(f"VUs: {vus_list}")
    print(f"TARGET={target} K6_DURATION={duration} K6_START_DELAY={start_delay}")
    print("")

    base_env = os.environ.copy()
    base_env["TRIGGER_KEYWORD"] = trigger
    base_env["TARGET"] = target

    print("Starting Envoy Lua stack...")
    ensure_compose_cleanup(base_env)
    start_result = start_envoy_stack(base_env)
    if start_result.returncode != 0:
        print(f"Failed to start Envoy Lua stack:\n{start_result.stderr}", file=sys.stderr)
        return 1
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
            ["docker", "compose", "logs", "--no-color", "--since", since_str, "envoy"],
            env=env,
        )
        detection_values, injection_values = parse_timings(logs_result.stdout)

        detect_stats = summarize(detection_values)
        inject_stats = summarize(injection_values)

        run_data = {
            "vus": vus,
            "compose_exit_code": up_result.returncode,
            "detection": detect_stats.to_json(),
            "injection": inject_stats.to_json(),
            "errors": {
                "compose_stderr": up_result.stderr.strip(),
                "logs_stderr": logs_result.stderr.strip(),
            },
        }
        all_results["runs"].append(run_data)

        print(f"Detection: count={detect_stats.count} min_us={detect_stats.min_us} avg_us={detect_stats.avg_us} p90_us={detect_stats.p90_us} max_us={detect_stats.max_us}")
        print(f"Injection: count={inject_stats.count} min_us={inject_stats.min_us} avg_us={inject_stats.avg_us} p90_us={inject_stats.p90_us} max_us={inject_stats.max_us}")
        print(f"compose_exit_code={up_result.returncode}")
        print("")

    ensure_compose_cleanup(base_env)

    output_file.write_text(json.dumps(all_results, indent=2), encoding="utf-8")
    print(f"Saved results: {output_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
