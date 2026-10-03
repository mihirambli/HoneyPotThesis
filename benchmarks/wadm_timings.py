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


# A timed region that reports far above this is not doing CPU work: every measured region is a
# bounded substring scan or a header write, and all four edges time them with a WALL clock
# (gettimeofday / apr_time_now / proxy-wasm get_current_time). A sample in the millisecond range
# therefore records the worker being descheduled, a page fault, or a THP compaction stall that
# happened to land inside the region — host noise billed to WADM. The count of such samples is
# kept as a per-run contamination measure rather than silently averaged in.
PREEMPTION_THRESHOLD_US = 1000


@dataclass
class PhaseStats:
    """Robust and non-robust summaries of one timed region, kept side by side.

    `avg_us` is retained because the existing plotters and result files index it, but it must not
    be read as the cost of the operation: a sub-1% preemption tail moves it by two orders of
    magnitude while the median does not move at all. On Apache at 500 iterations/s the mean of
    html_comments detection came out at 552 us against a median of 5 us, because 2% of samples
    exceeded 1 ms and the largest was 196 ms. `p50_us` is the headline; `tail_over_threshold`
    says how much of the distribution the mean was reporting on.
    """

    count: int
    min_us: int | None
    avg_us: float | None
    p90_us: float | None
    max_us: int | None
    p50_us: float | None = None
    p99_us: float | None = None
    trimmed_mean_us: float | None = None
    mad_us: float | None = None
    tail_over_threshold: int = 0
    tail_pct: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "min_us": self.min_us,
            "avg_us": self.avg_us,
            "p50_us": self.p50_us,
            "p90_us": self.p90_us,
            "p99_us": self.p99_us,
            "max_us": self.max_us,
            "trimmed_mean_us": self.trimmed_mean_us,
            "mad_us": self.mad_us,
            "tail_over_threshold": self.tail_over_threshold,
            "tail_pct": self.tail_pct,
            "threshold_us": PREEMPTION_THRESHOLD_US,
        }


def percentile_nearest_rank(values: list[int], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = int(math.ceil((pct / 100.0) * len(ordered)))
    idx = max(1, rank) - 1
    return float(ordered[idx])


def trimmed_mean(values: list[int], proportion: float = 0.05) -> float | None:
    """Mean with `proportion` of the mass dropped from each end.

    Reported alongside the median because it answers the question the mean was meant to answer —
    the average cost, including the shape of the body — without letting a handful of descheduled
    samples set the result.
    """
    if not values:
        return None
    ordered = sorted(values)
    cut = int(len(ordered) * proportion)
    core = ordered[cut: len(ordered) - cut] or ordered
    return round(statistics.fmean(core), 2)


def median_abs_deviation(values: list[int]) -> float | None:
    """Spread measure that does not move when the tail does, unlike the standard deviation."""
    if not values:
        return None
    med = statistics.median(values)
    return round(statistics.median([abs(v - med) for v in values]), 2)


def summarize(values: list[int]) -> PhaseStats:
    if not values:
        return PhaseStats(count=0, min_us=None, avg_us=None, p90_us=None, max_us=None)
    tail = sum(1 for v in values if v > PREEMPTION_THRESHOLD_US)
    return PhaseStats(
        count=len(values),
        min_us=min(values),
        avg_us=round(statistics.fmean(values), 2),
        p90_us=round(percentile_nearest_rank(values, 90) or 0.0, 2),
        max_us=max(values),
        p50_us=round(percentile_nearest_rank(values, 50) or 0.0, 2),
        p99_us=round(percentile_nearest_rank(values, 99) or 0.0, 2),
        trimmed_mean_us=trimmed_mean(values),
        mad_us=median_abs_deviation(values),
        tail_over_threshold=tail,
        tail_pct=round(100.0 * tail / len(values), 3),
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


# ── Load shape (arrival-rate era) ────────────────────────────────────────────────────────────
#
# These names are WADM_-prefixed because k6 claims the whole K6_* namespace for its own options:
# exporting K6_VUS or K6_DURATION makes k6 build its own default scenario and throw away test.js's
# `scenarios` block, executor and startTime with it. The old harness exported both, so every run
# it produced was a plain constant-VUs run with no start delay, whatever test.js asked for.
RATE_ENV = "WADM_RATE"
DURATION_ENV = "WADM_DURATION"
START_DELAY_ENV = "WADM_START_DELAY"
ORDER_ENV = "WADM_ORDER"
SEED_ENV = "WADM_SEED"
MAX_VUS_ENV = "WADM_MAX_VUS"

# Offered iterations per second. 1/10/100 are the latency regime: the edge is far from saturation,
# so a measured difference is service time rather than queueing. 500 is deliberately absent — at
# that level queueing delay reached 4 ms on the origin tier alone (no proxy, no WADM), which is
# 100x the effect being measured. Saturation is a separate question and has its own runner,
# run_capacity_benchmark.py, which reports throughput rather than latency.
DEFAULT_RATES = [1, 10, 100]

# Low rates produce few samples per second, and the sample count is what sets the width of the
# confidence interval. 30 s at rate 1 is 30 iterations; 120 s is 120, and five replicates make
# 600. Runtime is bounded because only the lowest rates get the longer window.
DURATION_FOR_RATE = {1: "120s", 10: "60s"}
DEFAULT_RATE_DURATION = "30s"

DEFAULT_REPLICATES = 5

# Below this, a percentile bootstrap degenerates to the observed range: with n=2 the only resamples
# are the two points themselves. Intervals are still reported for inspection, but `significant` is
# withheld so a 1-2 replicate smoke run cannot produce a confident-looking number.
MIN_REPLICATES_FOR_CI = 3


def duration_for_rate(rate: int, override: str | None = None) -> str:
    if override:
        return override
    return DURATION_FOR_RATE.get(rate, DEFAULT_RATE_DURATION)


def k6_env(
    rate: int,
    duration: str,
    start_delay: str = DEFAULT_START_DELAY,
    order: str = "rotate",
    seed: int = 1,
    max_vus: int | None = None,
) -> dict[str, str]:
    """Load-shape variables for one k6 run.

    Deliberately returns only WADM_* keys: a caller that merges this over os.environ must not
    reintroduce K6_VUS/K6_DURATION, so they are never written here.
    """
    env = {
        RATE_ENV: str(rate),
        DURATION_ENV: duration,
        START_DELAY_ENV: start_delay,
        ORDER_ENV: order,
        SEED_ENV: str(seed),
    }
    if max_vus is not None:
        env[MAX_VUS_ENV] = str(max_vus)
    return env


def strip_k6_option_env(env: dict[str, str]) -> dict[str, str]:
    """Remove any inherited K6_* option that would override test.js's scenarios block.

    A stale `export K6_VUS=...` in the operator's shell is enough to silently turn every run back
    into a constant-VUs run, so the runners scrub it rather than trusting the environment.
    """
    for key in ("K6_VUS", "K6_DURATION", "K6_ITERATIONS", "K6_STAGES", "K6_START_DELAY"):
        env.pop(key, None)
    return env


# ── Run validity ─────────────────────────────────────────────────────────────────────────────
#
# A run can fail in ways that leave its per-operation numbers looking entirely plausible. The
# results directory currently holds one such run: the WASM WADM tier at rate 100 completed 15.6%
# of its offered iterations, with a 75th-percentile latency of 1.78 s, and it still contributed a
# "+110 ms WADM overhead" bar to a figure. Every gate below turns one of those into a recorded,
# machine-readable reason instead.

# Above this share of failed HTTP requests the run is not measuring the intended response path.
MAX_FAILED_RATE = 0.005
# k6 could not place the offered iterations even with maxVUs allocated; latency is queueing delay.
MAX_DROPPED_RATE = 0.01
# Below this share of offered iterations actually completed, something outside the edge was wrong.
MIN_ACHIEVED_RATIO = 0.95
# Share of internal samples in the millisecond range, i.e. dominated by preemption not by work.
MAX_TAIL_PCT = 1.0
# ...but only once enough samples exist for a share to mean anything. At the lowest rate a kind
# collects about 90 samples per run, so ONE preempted sample is 1.1% and trips a 1% threshold on
# its own. That rejected two otherwise pristine replicates in the first full run — cells whose
# medians were 3-4 us with a 0.0% tail on every major kind. A percentage needs a denominator
# before it is evidence, so the gate also requires a minimum absolute count.
MIN_TAIL_SAMPLES = 3
# The backend container is a negative control: the same origin serves the same pages in every
# cell, of every tier, of every edge, so its CPU per request has nothing to do with what is under
# test and should barely move. Across the first 150 paired cells it stayed between 86 and 311 us.
# One cell read 39,948 us — 130x the worst legitimate value — and the edge in the same cell read
# 42,395 us against a normal 350 us. Both containers burning kernel time together is a host event,
# not edge behaviour, and every other gate passed it: at rate 1 the arrival-rate executor simply
# spent more VUs, so nothing dropped, nothing failed, and the offered rate was still met. The
# preemption gate could not see it either, because it reads internal WADM timings and a bare cell
# has none. This ceiling sits ~3x above the worst clean observation, so it catches the host
# stealing the machine without touching any cell that measured the edge.
MAX_ORIGIN_CPU_US_PER_REQUEST = 1000.0


def assess_run(
    rate: int,
    duration: str,
    summary: dict[str, Any] | None,
    crash_patterns: list[str] | None = None,
    worst_tail_pct: float | None = None,
    crash_count: int = 0,
    worst_tail_count: int = 0,
    origin_cpu_us_per_request: float | None = None,
) -> dict[str, Any]:
    """Decide whether one run's numbers may be used, and record why if not.

    Returns a dict carrying both the verdict and every input to it, so a rejected run stays in the
    result file as evidence rather than being dropped and forgotten.
    """
    seconds = duration_seconds(duration)
    offered = rate * seconds
    iterations = k6_iterations(summary)
    dropped = int((summary or {}).get("dropped_iterations") or 0)
    failed_rate = (summary or {}).get("http_req_failed_rate")
    achieved = (iterations / offered) if offered else None

    reasons: list[str] = []
    warnings: list[str] = []
    if summary is None:
        reasons.append("k6 summary could not be read")
    # A crash is a warning, not automatically a rejection. An edge process that dies takes the one
    # request in flight with it, and that shows up in the failure rate and the achieved rate — both
    # already gated below. Rejecting on the mere presence of a crash line discards a whole rate
    # level for damage that may be a hundredth of a percent, and it discards exactly the runs where
    # "this edge crashes under load" is the finding. The crash is recorded, surfaced in the report
    # and carried into the figures; whether the DATA is usable is decided by the damage gates.
    if crash_patterns:
        warnings.append(
            f"edge crashed {crash_count}x during this run "
            f"({', '.join(crash_patterns)}) — surviving samples are gated below"
        )
    if failed_rate is not None and failed_rate > MAX_FAILED_RATE:
        reasons.append(f"http_req_failed {failed_rate:.3%} > {MAX_FAILED_RATE:.1%}")
    if offered and dropped / offered > MAX_DROPPED_RATE:
        reasons.append(f"dropped_iterations {dropped}/{offered} > {MAX_DROPPED_RATE:.0%}")
    if achieved is not None and achieved < MIN_ACHIEVED_RATIO:
        reasons.append(f"achieved only {achieved:.1%} of offered rate")
    if (
        origin_cpu_us_per_request is not None
        and origin_cpu_us_per_request > MAX_ORIGIN_CPU_US_PER_REQUEST
    ):
        reasons.append(
            f"origin control burned {origin_cpu_us_per_request:.0f} us/req > "
            f"{MAX_ORIGIN_CPU_US_PER_REQUEST:.0f} us (host contention, not the edge)"
        )
    if worst_tail_pct is not None and worst_tail_pct > MAX_TAIL_PCT:
        if worst_tail_count >= MIN_TAIL_SAMPLES:
            reasons.append(
                f"{worst_tail_pct:.2f}% of internal samples ({worst_tail_count}) above "
                f"{PREEMPTION_THRESHOLD_US} us (host preemption)"
            )
        else:
            warnings.append(
                f"{worst_tail_count} sample(s) above {PREEMPTION_THRESHOLD_US} us "
                f"({worst_tail_pct:.2f}%) — too few to judge the run contaminated"
            )

    return {
        "valid": not reasons,
        "reasons": reasons,
        "warnings": warnings,
        "crash_count": crash_count,
        "offered_iterations": offered or None,
        "iterations": iterations,
        "achieved_ratio": round(achieved, 4) if achieved is not None else None,
        "dropped_iterations": dropped,
        "http_req_failed_rate": failed_rate,
        "worst_tail_pct": worst_tail_pct,
        "worst_tail_count": worst_tail_count,
        "origin_cpu_us_per_request": origin_cpu_us_per_request,
    }


# ── CPU cost per request ─────────────────────────────────────────────────────────────────────
#
# The most robust metric available on a noisy VM, and the one latency cannot give: cgroup
# cpu.stat's usage_usec is a monotonic counter of CPU time actually consumed by the container's
# processes. Scheduling delay, queueing and hypervisor jitter add wall-clock latency without
# adding CPU time, so (delta usage_usec / requests served) isolates the work WADM does from the
# noise the environment adds. A bare-vs-WADM difference here is a real cost even when the
# end-to-end latency difference is buried under the noise floor.

CPU_STAT_KEYS = ("usage_usec", "user_usec", "system_usec")


def read_container_cpu(service: str, env: dict[str, str], profile: str | None = None) -> dict[str, int] | None:
    """cgroup v2 cpu.stat counters for one Compose service, or None if unreadable.

    Read from inside the container: Docker runs containers in a private cgroup namespace here, so
    /sys/fs/cgroup/cpu.stat inside the container is that container's own accounting and needs no
    knowledge of the host's cgroup layout or the systemd/cgroupfs driver in use.
    """
    command = ["docker", "compose"]
    if profile:
        command += ["--profile", profile]
    command += ["exec", "-T", service, "cat", "/sys/fs/cgroup/cpu.stat"]
    result = run_cmd(command, env=env)
    if result.returncode != 0:
        return None
    out: dict[str, int] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in CPU_STAT_KEYS:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                continue
    return out or None


def cpu_cost_per_request(
    before: dict[str, int] | None,
    after: dict[str, int] | None,
    http_reqs: int | None,
) -> dict[str, Any]:
    """CPU microseconds the edge spent per request served, from two cpu.stat readings."""
    if not before or not after or not http_reqs:
        return {"available": False, "cpu_us_per_request": None}
    delta = {k: after.get(k, 0) - before.get(k, 0) for k in CPU_STAT_KEYS if k in after}
    total = delta.get("usage_usec")
    if total is None or total < 0:
        return {"available": False, "cpu_us_per_request": None}
    return {
        "available": True,
        "http_reqs": http_reqs,
        "cpu_usec_total": total,
        "cpu_us_per_request": round(total / http_reqs, 3),
        "user_us_per_request": (
            round(delta["user_usec"] / http_reqs, 3) if "user_usec" in delta else None
        ),
        "system_us_per_request": (
            round(delta["system_usec"] / http_reqs, 3) if "system_usec" in delta else None
        ),
    }


# ── Paired statistics ────────────────────────────────────────────────────────────────────────
#
# The old suite measured the bare tier and the WADM tier in separate script invocations, 15-30
# minutes apart, and subtracted the two medians. Everything that drifts on that timescale — CPU
# turbo and thermal state, host background work, page cache, which physical core a vCPU landed on
# — went straight into the difference. With a true effect of 5-30 us and drift of 100-500 us the
# sign of the result was set by the drift, which is why overhead bars came out negative.
#
# Pairing replaces that with N replicates in which bare and WADM run within a minute of each
# other, so drift is common to both members of a pair and cancels in the difference. The reported
# statistic is the median of the per-replicate differences, with a bootstrap interval around it.


def bootstrap_ci(
    values: list[float],
    statistic=statistics.median,
    confidence: float = 0.95,
    iterations: int = 10000,
    seed: int = 12345,
) -> tuple[float | None, float | None]:
    """Percentile bootstrap interval for `statistic` over `values`.

    Non-parametric on purpose: with five replicates there is no basis for assuming normality, and
    a t-interval on five points of a skewed quantity would be a stronger claim than the data
    supports.

    For the median of five values this degenerates to exactly [min, max]: a resampled median can
    only be one of the observed values, and min/max are the 2.5th/97.5th percentiles of that
    resampling distribution. [min, max] is still a valid interval — the distribution-free
    order-statistic interval for a median, with 1 - 2/2^n = 93.75% coverage at n=5 — but it should
    be described as "the range of n paired replicates", not as a 95% bootstrap interval.
    """
    if len(values) < 2:
        return (None, None)
    import random

    rng = random.Random(seed)
    n = len(values)
    estimates = []
    for _ in range(iterations):
        estimates.append(statistic([values[rng.randrange(n)] for _ in range(n)]))
    estimates.sort()
    lo_idx = int((1 - confidence) / 2 * iterations)
    hi_idx = min(iterations - 1, int((1 + confidence) / 2 * iterations))
    return (round(estimates[lo_idx], 4), round(estimates[hi_idx], 4))


def paired_delta(values: list[float], confidence: float = 0.95) -> dict[str, Any]:
    """Summarise per-replicate differences, and say plainly when they are indistinguishable from 0.

    `significant` False means the interval spans zero: the measurement cannot tell the direction
    of the effect, let alone its size. Reporting that is the point — a negative point estimate
    with an interval spanning zero is a noise floor, not a speed-up, and the figures must not
    draw it as one.
    """
    if not values:
        return {"n": 0, "median": None, "ci_low": None, "ci_high": None, "significant": False}
    lo, hi = bootstrap_ci(values, confidence=confidence)
    median = round(statistics.median(values), 4)
    # With two points a percentile bootstrap can only ever return the two points, so the interval
    # is the observed range and carries no information about the sampling distribution. The
    # significance claim is withheld rather than made on that basis — a smoke run must not be able
    # to produce a confident-looking result.
    adequate = len(values) >= MIN_REPLICATES_FOR_CI
    significant = (
        adequate and lo is not None and hi is not None and (lo > 0 or hi < 0)
    )
    # At n=5 `significant` is equivalent to every replicate agreeing in sign (a sign test at
    # p = 2/32 = 0.0625), so the agreement count is the plainest honest statement of the evidence.
    same_sign = max(sum(1 for v in values if v > 0), sum(1 for v in values if v < 0))
    return {
        "n": len(values),
        "sign_agreement": f"{same_sign}/{len(values)}",
        "ci_reliable": adequate,
        "median": median,
        "mean": round(statistics.fmean(values), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "ci_low": lo,
        "ci_high": hi,
        "confidence": confidence,
        "significant": significant,
        "samples": [round(v, 4) for v in values],
    }


# ── Run provenance ───────────────────────────────────────────────────────────────────────────
#
# Recorded into every result file so a figure can be traced back to the machine configuration that
# produced it. This matters more than usual here: the edge containers are pinned to a subset of
# CPUs while the edges' own worker counts are auto-detected from the full CPU count, so an edge may
# be running more workers than it has cores. That cancels in a paired bare-vs-WADM difference, since
# both tiers share the config, but it does affect cross-edge comparison and the capacity numbers —
# so the write-up has to be able to state what was actually in force.


def container_provenance(service: str, env: dict[str, str], profile: str | None = None) -> dict[str, Any]:
    """CPU affinity, visible CPU count and worker-process count for one Compose service."""
    prefix = ["docker", "compose"]
    if profile:
        prefix += ["--profile", profile]

    container_id = run_cmd(prefix + ["ps", "-q", service], env=env).stdout.strip().splitlines()
    cpuset = None
    if container_id:
        cpuset = run_cmd(
            ["docker", "inspect", container_id[0], "--format", "{{.HostConfig.CpusetCpus}}"],
            env=env,
        ).stdout.strip() or None

    # Two different CPU counts, because the servers disagree about which one to use. `nproc` calls
    # sched_getaffinity and so respects the cpuset; nginx's `worker_processes auto` calls
    # sysconf(_SC_NPROCESSORS_ONLN), which does NOT. Verified on this host: nproc reports 2 inside
    # a container pinned to two CPUs while nginx spawns 8 workers. Recording only one of these
    # would hide the oversubscription rather than document it.
    affinity_cpus = run_cmd(prefix + ["exec", "-T", service, "nproc"], env=env).stdout.strip()
    online_cpus = run_cmd(
        prefix + ["exec", "-T", service, "getconf", "_NPROCESSORS_ONLN"], env=env
    ).stdout.strip()

    # Worker count from /proc rather than `ps`, which several of these images do not ship.
    cmdlines = run_cmd(
        prefix + ["exec", "-T", service, "sh", "-c",
                  'for p in /proc/[0-9]*; do tr "\\0" " " < $p/cmdline 2>/dev/null; echo; done'],
        env=env,
    ).stdout
    workers = sum(1 for line in cmdlines.splitlines() if "worker process" in line)
    processes = sum(1 for line in cmdlines.splitlines() if line.strip())

    affinity = int(affinity_cpus) if affinity_cpus.isdigit() else None
    online = int(online_cpus) if online_cpus.isdigit() else None

    return {
        "cpuset": cpuset,
        "affinity_cpus": affinity,
        "online_cpus": online,
        "worker_processes": workers or None,
        "process_count": processes or None,
        "oversubscribed": (
            bool(workers and affinity and workers > affinity) if workers and affinity else None
        ),
        "note": (
            "affinity_cpus is sched_getaffinity (respects cpuset); online_cpus is "
            "_SC_NPROCESSORS_ONLN, which nginx's `worker_processes auto` uses and which ignores "
            "the cpuset. oversubscribed=true means more workers than pinned cores."
        ),
    }
