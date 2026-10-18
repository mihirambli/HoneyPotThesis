#!/usr/bin/env python3
"""Single registry of the four edges and the origin floor, shared by every runner.

The four `run_internal_<edge>_benchmark.py` scripts each carried their own copy of this table
(profile, service name, target URL, config paths, html_comments regexes). Keeping four copies is
what let the WADM and baseline suites drift apart in the details that decide comparability — the
mounted config, the Compose profile, the log source — so the table lives here once and both
suites read it.

`detect_re` / `inject_re` differ per edge only in their log-line prefix. They are deliberately
anchored on that prefix: OpenResty's lines are unprefixed, so an unanchored pattern would also
match Apache's and WASM's and silently pool three edges into one distribution.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The three configurations of an edge a paired cell can run, in the order they are reported.
# The paired runner adds a fourth tier, ORIGIN_TIER, below.
#
#   bare    the edge's WADM-free config: what the server did before WADM existed.
#   silent  full WADM with the benchmark's timing log lines switched off (`emit_timings: false`):
#           every detection, injection, alert and clock read, none of the ~39 timing writes per
#           iteration. This is the WADM a deployment would run, so silent - bare is the headline.
#   wadm    full WADM with the timing lines on: the only tier the in-edge timers can be read from.
#           wadm - silent is the cost of the instrumentation itself.
TIERS = ["bare", "silent", "wadm"]
WADM_TIERS = ("silent", "wadm")

# The no-proxy floor: k6 straight to the origin, with the edge left idle and out of the request path.
# It runs inside the same replicate as the edge's own tiers, so bare - origin (what the proxy alone
# adds) is paired exactly as silent - bare is; a floor measured in a separate session would carry
# that session's drift, which is larger than the proxy's cost (BENCHMARK_METHODOLOGY.md §1.3).
#
# Kept out of TIERS on purpose: TIERS means "configurations of an edge", and the capacity runner
# builds its tier list from it with no notion of a tier that has no edge.
ORIGIN_TIER = "origin"
PAIRED_TIERS = [ORIGIN_TIER, *TIERS]

ORIGIN_SERVICE = "backend"
ORIGIN_TARGET = "http://backend:80"

SILENT_CONFIG_NAME = "config-silent.json"


@dataclass(frozen=True)
class EdgeSpec:
    key: str
    label: str
    profile: str | None
    service: str
    target: str
    config_env: str | None
    wadm_config: str | None
    bare_config: str | None
    detect_re: re.Pattern[str] | None
    inject_re: re.Pattern[str] | None
    # WASM needs its filter rebuilt from the Rust source before the edge will start.
    needs_build: bool = False
    # Set on a remediation variant: the edge key it is a modified copy of. A variant shares its
    # bare tier with that edge, so its WADM-vs-bare difference is measured on the same floor and
    # the two can be compared directly.
    variant_of: str | None = None

    def compose_prefix(self) -> list[str]:
        command = ["docker", "compose"]
        if self.profile:
            command += ["--profile", self.profile]
        return command

    def target_for(self, tier: str) -> str:
        """The URL k6 sends this tier's requests to: the edge, or the origin directly."""
        return ORIGIN_TARGET if tier == ORIGIN_TIER else self.target

    def tier_env(self, tier: str) -> dict[str, str]:
        """Compose variables that select this tier: the server config, and the WADM config.json.

        Both are set explicitly for every tier, never left to inherit, so a variable exported in the
        operator's shell cannot quietly put one tier's config under another tier's name. The origin
        tier sends no traffic through the edge; it is given the WADM-free config so that any
        Compose command touching the idle edge cannot load WADM into it.
        """
        if tier not in PAIRED_TIERS:
            raise ValueError(f"unknown tier {tier!r}; expected one of {PAIRED_TIERS}")
        env: dict[str, str] = {
            "WADM_CONFIG_JSON": f"./{SILENT_CONFIG_NAME}" if tier == "silent" else "./config.json",
        }
        if self.config_env:
            config = self.wadm_config if tier in WADM_TIERS else self.bare_config
            if config:
                env[self.config_env] = config
        return env


def write_silent_config() -> Path:
    """Generate config-silent.json from config.json, differing only in `emit_timings: false`.

    Generated rather than committed so the two can never drift: every token, signature and trap
    setting comes from config.json at the moment the run starts.
    """
    source = json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8"))
    source["emit_timings"] = False
    target = REPO_ROOT / SILENT_CONFIG_NAME
    target.write_text(json.dumps(source, indent=2) + "\n", encoding="utf-8")
    return target


def _res(prefix: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    return (
        re.compile(rf"{prefix}Detection execution time \(us\):\s*(\d+)"),
        re.compile(rf"{prefix}Injection execution time \(us\):\s*(\d+)"),
    )


_OPENRESTY_RES = _res("")
_ENVOY_RES = _res("Envoy Lua ")
_WASM_RES = _res("WASM ")
_APACHE_RES = _res("Apache ")

# `origin` is the no-proxy floor: `backend` carries no Compose profile, so it is already running
# and there is no edge config to swap. It has no timing regexes because it runs no WADM code —
# which is exactly what makes it the control that exposes harness artefacts.
EDGES: dict[str, EdgeSpec] = {
    "openresty": EdgeSpec(
        key="openresty", label="OpenResty", profile="openresty", service="openresty",
        target="http://openresty:80", config_env="OPENRESTY_CONF",
        wadm_config="./nginx/nginx.conf", bare_config="./nginx/nginx-baseline.conf",
        detect_re=_OPENRESTY_RES[0], inject_re=_OPENRESTY_RES[1],
    ),
    "envoy_lua": EdgeSpec(
        key="envoy_lua", label="Envoy+Lua", profile="envoy", service="envoy",
        target="http://envoy:8080", config_env="ENVOY_CONF",
        wadm_config="./envoy/envoy.yaml", bare_config="./envoy/envoy-baseline.yaml",
        detect_re=_ENVOY_RES[0], inject_re=_ENVOY_RES[1],
    ),
    "wasm": EdgeSpec(
        key="wasm", label="WASM (Envoy)", profile="wasm", service="envoy-wasm",
        target="http://envoy-wasm:8080", config_env="ENVOY_WASM_CONF",
        wadm_config="./envoy-wasm/envoy-wasm.yaml",
        bare_config="./envoy-wasm/envoy-wasm-baseline.yaml",
        detect_re=_WASM_RES[0], inject_re=_WASM_RES[1], needs_build=True,
    ),
    "apache_lua": EdgeSpec(
        key="apache_lua", label="Apache+Lua", profile="apache", service="apache",
        target="http://apache:80", config_env="HTTPD_CONF",
        wadm_config="./httpd.conf", bare_config="./httpd-baseline.conf",
        detect_re=_APACHE_RES[0], inject_re=_APACHE_RES[1],
    ),
    # Remediation variant of apache_lua: identical in every respect except LuaScope, which is
    # `conn` rather than `thread`. Deliberately NOT in EDGE_KEYS — the four-edge comparison must
    # keep the configuration that runs the same algorithm as the other three, and this is reported
    # separately as a fix with a measured cost. See httpd-conn.conf for the evidence.
    "apache_lua_conn": EdgeSpec(
        key="apache_lua_conn", label="Apache+Lua (LuaScope conn)", profile="apache",
        service="apache", target="http://apache:80", config_env="HTTPD_CONF",
        wadm_config="./httpd-conn.conf", bare_config="./httpd-baseline.conf",
        detect_re=_APACHE_RES[0], inject_re=_APACHE_RES[1], variant_of="apache_lua",
    ),
    "origin": EdgeSpec(
        key="origin", label="Origin only", profile=None, service=ORIGIN_SERVICE,
        target=ORIGIN_TARGET, config_env=None,
        wadm_config=None, bare_config=None, detect_re=None, inject_re=None,
    ),
}

# The four real edges, in the order figures present them. `origin` is excluded: it is not an edge and
# has no WADM tier. The paired runner measures the no-proxy floor as ORIGIN_TIER inside each edge's
# own run instead, so it is paired with that edge's tiers.
#
# Remediation variants are NOT here. The cross-edge comparison rests on every edge running the same
# mechanism, so a variant that changes one edge's runtime semantics would make the four-way figure
# compare different things. Variants are run explicitly by key and reported on their own.
EDGE_KEYS = ["openresty", "wasm", "apache_lua", "envoy_lua"]

VARIANT_KEYS = ["apache_lua_conn"]

# Everything a runner may be pointed at with --edge.
RUNNABLE_KEYS = EDGE_KEYS + VARIANT_KEYS

# Patterns that mean the edge process itself failed during a run, not that it was merely slow.
# Apache children have been observed to segfault and corrupt their Lua state at high rates, after
# which that child's timings are meaningless while still looking plausible in isolation.
CRASH_PATTERNS = [
    re.compile(r"segmentation fault", re.I),
    re.compile(r"caught SIGSEGV", re.I),
    re.compile(r"child pid \d+ exit signal", re.I),
    re.compile(r"\[emerg\]", re.I),
    re.compile(r"panicked at", re.I),
    re.compile(r"Proxy-Wasm plugin .*(failed|crash)", re.I),
]


def scan_for_crashes(logs: str) -> list[str]:
    """Distinct crash indicators present in an edge's logs for one run window."""
    return sorted({p.pattern for p in CRASH_PATTERNS if p.search(logs)})


def count_crashes(logs: str) -> int:
    """How many crash lines, not merely whether there were any.

    The distinction decides whether a run is unusable or merely damaged: Apache's mod_lua
    segfaults roughly nine times per twenty seconds at 800 req/s, and each crash kills the one
    request in flight on that child. That is about 0.03% of the population, and the surviving
    median is identical to the crash-free levels. A count lets the gate scale with the damage
    instead of discarding a whole rate level over it.
    """
    # Distinct LINES, not pattern matches. Apache writes one line per dead child —
    # "AH00052: child pid 10 exit signal Segmentation fault (11)" — which matches both the
    # "child pid ... exit signal" and the "segmentation fault" patterns, so summing per-pattern
    # match counts reports exactly twice the number of crashes that happened.
    return sum(
        1 for line in logs.splitlines()
        if any(p.search(line) for p in CRASH_PATTERNS)
    )
