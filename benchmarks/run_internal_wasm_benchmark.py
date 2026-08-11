#!/usr/bin/env python3
"""
Automate internal Envoy WASM microsecond benchmark runs via k6.

Runs VU levels [1, 10, 100, 500], parses envoy-wasm logs for:
  - WASM Detection execution time (us): N
  - WASM Injection execution time (us): N

Writes machine-readable results to:
  benchmarks/results/internal_wasm_profile.json
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

# Stats helpers and the cross-edge `WADM TOKEN <kind> <phase> (us):` scraper are shared by
# all four orchestrators; only the html_comments regexes below differ per edge.
from wadm_timings import KINDS, build_token_sections, format_token_line, summarize


DETECTION_RE = re.compile(r"WASM Detection execution time \(us\):\s*(\d+)")
INJECTION_RE = re.compile(r"WASM Injection execution time \(us\):\s*(\d+)")

DEFAULT_VUS = [1, 10, 100, 500]
DEFAULT_DURATION = "30s"
DEFAULT_START_DELAY = "5s"
# One throwaway warm-up burst is run (and discarded) before the recorded VU levels
# so JIT/caches are hot; the edge stack persists across levels, so warming once is enough.
WARMUP_VUS = 100
WARMUP_DURATION = "20s"
DEFAULT_TRIGGER = "internal-admin.example.com"
DEFAULT_TARGET = "http://envoy-wasm:8080"


def run_cmd(command: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, env=env, text=True, capture_output=True, check=False)


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


# Every Compose profile that can leave a container behind. `docker compose down`
# only removes containers for *enabled* profiles, so cleaning up with no profile
# leaves stopped edge containers from previous runs in place. When the shared
# network is later recreated with a new ID, those stale containers still point at
# the old (deleted) network, and the next `docker compose up` reuses them and
# fails with "network <id> not found" (exit 128). Enabling all profiles here
# forces every edge container to be removed, so `up` always creates fresh ones.
CLEANUP_PROFILES = ["openresty", "envoy", "wasm", "apache", "loadtest"]


def ensure_compose_cleanup(base_env: dict[str, str]) -> None:
    down_cmd = ["docker", "compose"]
    for profile in CLEANUP_PROFILES:
        down_cmd += ["--profile", profile]
    down_cmd += ["down", "--remove-orphans"]
    run_cmd(down_cmd, env=base_env)
    # Prune networks left behind by interrupted or partially-cleaned runs; otherwise
    # the next `docker compose up` fails with "network <id> not found".
    run_cmd(["docker", "network", "prune", "-f"], env=base_env)


def start_wasm_stack(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Build the WASM filter and start backend + envoy-wasm in detached mode (one-time)."""
    return run_cmd(
        ["docker", "compose", "--profile", "wasm", "up", "-d"],
        env=env,
    )


def cycle_loadtester(env: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], datetime]:
    """Remove any leftover load-tester container, start a fresh one, and wait for k6 to finish.

    envoy-wasm and backend are left running so rust-builder does not re-run between VU levels.
    Returns the wait result and the timestamp captured just before the container started (used
    with --since to isolate this run's Envoy log lines from previous runs).
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
    output_file = results_dir / "internal_wasm_profile.json"
    raw_output_file = results_dir / "internal_wasm_raw.json"

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
            "script": "benchmarks/run_internal_wasm_benchmark.py",
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

    print("=== Internal WASM Benchmark ===")
    print(f"VUs: {vus_list}")
    print(f"TARGET={target} K6_DURATION={duration} K6_START_DELAY={start_delay}")
    print("")

    base_env = os.environ.copy()
    base_env["TRIGGER_KEYWORD"] = trigger
    base_env["TARGET"] = target

    print("Building WASM filter and starting stack (one-time build)...")
    ensure_compose_cleanup(base_env)
    start_result = start_wasm_stack(base_env)
    if start_result.returncode != 0:
        print(f"Failed to start WASM stack:\n{start_result.stderr}", file=sys.stderr)
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
            ["docker", "compose", "logs", "--no-color", "--since", since_str, "envoy-wasm"],
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

        run_data = {
            "vus": vus,
            "compose_exit_code": up_result.returncode,
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

        print(f"Detection: count={detect_stats.count} min_us={detect_stats.min_us} avg_us={detect_stats.avg_us} p90_us={detect_stats.p90_us} max_us={detect_stats.max_us}")
        print(f"Injection: count={inject_stats.count} min_us={inject_stats.min_us} avg_us={inject_stats.avg_us} p90_us={inject_stats.p90_us} max_us={inject_stats.max_us}")
        print("Honeytoken kinds:")
        for kind in KINDS:
            print(format_token_line(kind, token_summary))
        print(f"compose_exit_code={up_result.returncode}")
        print("")

    ensure_compose_cleanup(base_env)

    output_file.write_text(json.dumps(all_results, indent=2), encoding="utf-8")
    print(f"Saved results: {output_file}")
    raw_output_file.write_text(json.dumps(raw_results, indent=2), encoding="utf-8")
    print(f"Saved raw samples: {raw_output_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
