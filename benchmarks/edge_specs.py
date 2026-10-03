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

import re
from dataclasses import dataclass


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
        key="origin", label="Origin only", profile=None, service="backend",
        target="http://backend:80", config_env=None,
        wadm_config=None, bare_config=None, detect_re=None, inject_re=None,
    ),
}

# The four real edges, in the order figures present them. `origin` is excluded: it is a reference
# line, not an edge, and it has no WADM tier to pair against.
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
