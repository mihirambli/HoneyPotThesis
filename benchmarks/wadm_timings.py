#!/usr/bin/env python3
"""Shared log-scraping and summary-statistics helpers for the four edge orchestrators.

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

`build_token_sections` merges both families into the per-kind structures written to
`internal_<edge>_profile.json` (summary) and `internal_<edge>_raw.json` (raw samples),
with html_comments carried over from family 1 so every kind is queried the same way.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass
from typing import Any

TOKEN_LINE_RE = re.compile(r"WADM TOKEN (\w+) (detect|inject) \(us\):\s*(\d+)")

# Plot/report order. html_comments leads because it is the reference measurement the
# other kinds are compared against.
KINDS = ["html_comments", "http_headers", "cookies", "decoy_paths", "form_fields"]
PHASES = ["detect", "inject"]


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
    for kind in KINDS:
        per_phase = scraped.get(kind, {})
        summary[kind] = {
            phase: summarize(per_phase.get(phase, [])).to_json() for phase in PHASES
        }
        raw[kind] = {f"{phase}_us": per_phase.get(phase, []) for phase in PHASES}
    return summary, raw


def format_token_line(kind: str, summary: dict[str, Any]) -> str:
    """One-line console digest of a kind's two phases, for the orchestrators' stdout."""
    parts = []
    for phase in PHASES:
        s = summary[kind][phase]
        parts.append(f"{phase}: count={s['count']} avg_us={s['avg_us']} p90_us={s['p90_us']}")
    return f"  {kind:<14} " + " | ".join(parts)
