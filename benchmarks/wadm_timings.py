#!/usr/bin/env python3
"""Shared Compose driving, log-scraping and summary-statistics helpers for the benchmark runners.

Used by the four WADM orchestrators (`run_internal_<edge>_benchmark.py`) and by the baseline
suite (`run_baseline_benchmark.py`). Two measurement planes are scraped here:

  * **Internal microseconds**, from the *edge's* logs — what a detect/inject region costs.
    Only the WADM tier produces these; a bare edge has no such regions.
  * **End-to-end milliseconds**, from the *load-tester's* logs — what a request costs. Produced
    by every tier, and therefore the only plane on which baseline and WADM are comparable.

Every edge emits two families of microsecond timing lines:

  1. The html_comments reference pair, with an edge-specific prefix:
         [<Edge> ]Detection execution time (us): N
         [<Edge> ]Injection execution time (us): N
     Each orchestrator owns those two regexes because the prefix differs per edge.

  2. One line per additional honeytoken kind per timed region, identical on all edges:
         WADM TOKEN <kind> detect (us): N
         WADM TOKEN <kind> inject (us): N
     The lowercase `detect`/`inject` words are deliberate: they share no substring with
     the family-1 patterns, so OpenResty's *unprefixed* `Detection execution time \\(us\\):`
     scraper cannot swallow them and silently corrupt the html_comments distribution.

     The `sql_injection` trap uses this family too, but emits `detect` only, under four
     names crossing outcome with encoding: `sql_injection[_encoded]` on a signature hit and
     `sql_injection_miss[_encoded]` when nothing matched, with the `_encoded` suffix set when the
     request body required percent-decoding. It plants nothing, so there is no injection
     region to time. The four names exist because the two factors pull in opposite
     directions — decoding costs several microseconds while scan depth costs nothing
     measurable — and a single hit/miss pair confounds them.

`build_token_sections` merges both families into the per-kind structures written to
`internal_<edge>_profile.json` (summary) and `internal_<edge>_raw.json` (raw samples),
with html_comments carried over from family 1 so every kind is queried the same way.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOKEN_LINE_RE = re.compile(r"WADM TOKEN (\w+) (detect|inject) \(us\):\s*(\d+)")

# k6's handleSummary prints one sentinel line per run (see test.js). Anchoring on the sentinel and
# taking the rest of the line is what makes `docker compose logs`' `load-tester-1  | ` prefix
# harmless, so the JSON never has to be un-prefixed.
K6_SUMMARY_RE = re.compile(r"WADM K6 SUMMARY (\{.*\})\s*$", re.M)

# Plot/report order. html_comments leads because it is the reference measurement the
# other kinds are compared against.
KINDS = ["html_comments", "http_headers", "cookies", "decoy_paths", "form_fields"]
PHASES = ["detect", "inject"]

# sql_injection is measured but is NOT a honeytoken kind: it plants nothing, so it has no
# injection phase at all. Keeping it out of KINDS is what stops it appearing in injection plots
# and in the injection pool, where a page-generation cost would be compared against body mutation.
#
# Only the plain-hit arm is pooled, as the trap's one representative sample per iteration. Pooling
# all four would give sql_injection four times the weight of any honeytoken kind.
DETECT_ONLY_KINDS = ["sql_injection"]

# The other three arms of the same trap, crossing outcome (signature hit / no match) with whether the
# body needed percent-decoding. They are controls for the pooled arm, not separate features, so
# they are persisted and plotted on their own but never pooled with the honeytoken kinds.
CONTROL_KINDS = [
    "sql_injection_encoded",
    "sql_injection_miss",
    "sql_injection_miss_encoded",
]

# The 2x2, in (outcome, encoding) order — what plot_sqli_comparison.py draws.
SQLI_ARMS = [
    "sql_injection",
    "sql_injection_encoded",
    "sql_injection_miss",
    "sql_injection_miss_encoded",
]

# Which kinds carry data in each phase. Pooling and the per-kind grids read this rather than KINDS
# so a detection-only kind cannot leak into an injection figure.
KINDS_FOR = {"detect": KINDS + DETECT_ONLY_KINDS, "inject": KINDS}

# Everything the scrapers persist. Phases a kind does not have land as count 0 / empty list, which
# the plotters already treat as "no data" rather than "measured zero".
ALL_KINDS = KINDS + DETECT_ONLY_KINDS + CONTROL_KINDS

# Shared by every runner so the WADM and baseline tiers land on the same x-axis. VU 1/10/100 are
# fixed-arrival-rate levels (test.js's sleep(1) caps a VU at one iteration/sec), which is the
# regime a latency comparison needs; 500 is where this repo's 2-core host saturates.
DEFAULT_VUS = [1, 10, 100, 500]
DEFAULT_DURATION = "30s"
DEFAULT_START_DELAY = "5s"
# One throwaway warm-up burst is run (and discarded) before the recorded VU levels so JIT/caches
# are hot; the edge stack persists across levels, so warming once is enough.
WARMUP_VUS = 100
WARMUP_DURATION = "20s"


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


def parse_token_timings(logs: str) -> dict[str, dict[str, list[int]]]:
    """Scrape every `WADM TOKEN <kind> <phase> (us): N` line into {kind: {phase: [us]}}."""
    out: dict[str, dict[str, list[int]]] = {}
    for kind, phase, value in TOKEN_LINE_RE.findall(logs):
        out.setdefault(kind, {}).setdefault(phase, []).append(int(value))
    return out


def build_token_sections(
    logs: str, detection_us: list[int], injection_us: list[int]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (summary, raw) per-kind sections for one VU level.

    html_comments is filled from the already-parsed family-1 samples so the reference
    measurement stays byte-identical to the one the pre-existing plots consume.
    """
    scraped = parse_token_timings(logs)
    scraped["html_comments"] = {"detect": detection_us, "inject": injection_us}

    summary: dict[str, Any] = {}
    raw: dict[str, Any] = {}
    for kind in ALL_KINDS:
        per_phase = scraped.get(kind, {})
        summary[kind] = {
            phase: summarize(per_phase.get(phase, [])).to_json() for phase in PHASES
        }
        raw[kind] = {f"{phase}_us": per_phase.get(phase, []) for phase in PHASES}
    return summary, raw


def parse_k6_summary(logs: str) -> dict[str, Any] | None:
    """Lift the end-to-end summary k6 printed for the most recent run, or None if absent.

    The last match wins: a `--since` window is only second-accurate, so it can occasionally catch
    the tail of the previous cycle alongside the one being measured.
    """
    matches = K6_SUMMARY_RE.findall(logs)
    if not matches:
        return None
    try:
        return json.loads(matches[-1])
    except json.JSONDecodeError:
        return None


def fetch_k6_summary(
    env: dict[str, str], since_str: str
) -> tuple[dict[str, Any] | None, subprocess.CompletedProcess[str]]:
    """Read the load-tester's logs for this run window and parse k6's summary line out of them.

    `--profile loadtest` is required: Compose refuses to address a service whose profile is not
    enabled, the same reason the rm/up/wait calls carry it.
    """
    result = run_cmd(
        ["docker", "compose", "--profile", "loadtest", "logs", "--no-color",
         "--since", since_str, "load-tester"],
        env=env,
    )
    return parse_k6_summary(result.stdout), result


def k6_iterations(summary: dict[str, Any] | None) -> int:
    """Iteration count k6 itself recorded; 0 when the summary could not be read.

    Preferred over counting scraped log lines for the throughput guard, and the only source
    available at all in the baseline tiers, which emit no timing lines.
    """
    if not summary:
        return 0
    return int(summary.get("iterations") or 0)


def wait_for_quiet_host(max_load: float = 2.0, timeout_s: int = 420, poll_s: int = 10) -> None:
    """Block until the 1-minute load average falls below `max_load`.

    The 500-VU level saturates the host, and the load takes a while to decay after the
    containers stop. Running the edges back-to-back therefore measured whichever edge came
    later against a busy machine: in one run Envoy started at load 5.5 and its VU=1
    detection avg came out at 1636 µs against 2 µs once the host was idle — an ordering
    bias, not an edge property. Waiting here gives every edge the same starting state.

    Linux-only (`/proc/loadavg`); silently skipped where that file is unreadable, and
    capped by `timeout_s` so a permanently busy host cannot hang the run.
    """
    loadavg = Path("/proc/loadavg")
    if not loadavg.exists():
        return
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            current = float(loadavg.read_text().split()[0])
        except (OSError, ValueError):
            return
        if current < max_load:
            return
        print(f"  host busy (load {current:.2f} >= {max_load}); waiting {poll_s}s...")
        time.sleep(poll_s)
    print(f"  warning: host still busy after {timeout_s}s; proceeding anyway")


def duration_seconds(duration: str) -> int:
    """Parse a k6 duration like '30s' / '2m' into seconds (0 if unparseable)."""
    match = re.fullmatch(r"(\d+)([sm])", duration.strip())
    if not match:
        return 0
    value, unit = int(match.group(1)), match.group(2)
    return value * (60 if unit == "m" else 1)


def throughput_check(vus: int, duration: str, iterations: int) -> dict[str, Any]:
    """Compare achieved iterations against what the k6 script's sleep(1) allows.

    One iteration per VU per second is the ceiling the script itself imposes, so at low VU
    levels a healthy edge lands within a few percent of `vus * seconds`. Falling far short
    means the host was busy, not that the edge is slow — this repo's 2-core host has
    silently produced runs where one edge managed 47% of the achievable iterations while
    its per-operation numbers still looked plausible. Recording the ratio makes that
    visible instead of leaving it to be inferred from odd-looking latencies.

    At the highest VU level the edge itself saturates, so a ratio below 1 is expected there
    and `expected_reachable` is False — compare edges against each other, not against 1.0.
    """
    seconds = duration_seconds(duration)
    expected = vus * seconds
    ratio = (iterations / expected) if expected else None
    return {
        "iterations": iterations,
        "expected_iterations": expected or None,
        "throughput_ratio": round(ratio, 3) if ratio is not None else None,
        # Levels where sleep(1) governs rather than the edge; a shortfall here is host noise.
        "expected_reachable": vus <= 100,
    }


def format_token_line(kind: str, summary: dict[str, Any]) -> str:
    """One-line console digest of a kind's two phases, for the orchestrators' stdout."""
    parts = []
    for phase in PHASES:
        s = summary[kind][phase]
        parts.append(f"{phase}: count={s['count']} avg_us={s['avg_us']} p90_us={s['p90_us']}")
    return f"  {kind:<14} " + " | ".join(parts)


def format_e2e_line(summary: dict[str, Any] | None) -> str:
    """One-line console digest of the end-to-end trends, for the runners' stdout."""
    if not summary:
        return "  end-to-end:    (k6 summary unavailable)"
    trends = summary.get("trends") or {}
    parts = []
    for name in ("http_req_duration", "detect_query_duration", "inject_get_duration"):
        stats = trends.get(name) or {}
        med, p90 = stats.get("med"), stats.get("p90")
        med_s = f"{med:.2f}" if med is not None else "n/a"
        p90_s = f"{p90:.2f}" if p90 is not None else "n/a"
        parts.append(f"{name.replace('_duration', '')}: med={med_s}ms p90={p90_s}ms")
    return "  end-to-end:    " + " | ".join(parts)


# ── End-to-end result documents ──────────────────────────────────────────────────────────────

# Order matters only for readability of the result files. http_req_duration leads because it is
# the pooled headline the overhead decomposition is built on.
E2E_TRENDS = [
    "http_req_duration",
    "inject_get_duration",
    "detect_query_duration",
    "token_tamper_duration",
    "token_decoy_duration",
    "sqli_hit_duration",
    "sqli_hit_enc_duration",
    "sqli_miss_duration",
    "sqli_miss_enc_duration",
]


def new_e2e_document(**metadata: Any) -> dict[str, Any]:
    """Container for the end-to-end (millisecond) result file every tier writes.

    Baseline and WADM runs must land on one schema because a single plotter reads them side by
    side; constructing the document in one place is what keeps that true as either suite changes.
    """
    return {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "note": (
                "End-to-end request latency measured by k6, in MILLISECONDS. The edges' internal "
                "detect/inject timings are microseconds and live in internal_<edge>_*.json."
            ),
            **metadata,
        },
        "runs": [],
    }


def append_e2e_run(
    doc: dict[str, Any],
    vus: int,
    throughput: dict[str, Any],
    summary: dict[str, Any] | None,
    errors: dict[str, str],
) -> None:
    trends = (summary or {}).get("trends") or {}
    doc["runs"].append(
        {
            "vus": vus,
            "throughput": throughput,
            "k6": {
                "iterations": (summary or {}).get("iterations"),
                "http_reqs": (summary or {}).get("http_reqs"),
                "http_req_failed_rate": (summary or {}).get("http_req_failed_rate"),
                "trends": {name: trends.get(name) for name in E2E_TRENDS},
            },
            "errors": errors,
        }
    )


# ── Compose driving ──────────────────────────────────────────────────────────────────────────

def run_cmd(command: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, env=env, text=True, capture_output=True, check=False)


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


def cycle_loadtester(env: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], datetime]:
    """Remove any leftover load-tester container, start a fresh one, and wait for k6 to finish.

    The edge and backend containers are left running so the edge's runtime state (LuaJIT traces,
    connection pools, V8 compilation) is preserved between VU levels. Returns the wait result and
    a timestamp captured just before the container started, used with --since to isolate this
    run's log lines — on both the edge and the load-tester — from previous runs.
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
