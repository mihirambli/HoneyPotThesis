#!/usr/bin/env python3
"""Check that the four WADM edges behave identically on a fixed set of requests.

The latency comparison is only meaningful if every edge does the same detection and injection
work, so this is the functional counterpart to the benchmark runners. Each edge receives the same
requests; for each one it records what the attacker sees (status, bait headers, body hash), what
the edge alerts on (`WADM ALERT` / `WADM TRAP` lines), and what reached the origin (its access
log, which shows whether a trigger keyword was stripped). Everything is diffed against OpenResty,
the reference edge.

Residual differences that are documented rather than levelled are excluded:
  * response framing (chunked vs Content-Length);
  * the Envoy+WASM SQLi trap contacting the origin (docs/EDGE_LEVELING.md explains why);
  * Apache forwarding `path?` rather than `path` when the strip removes every parameter: mod_lua
    can only assign r.args a string, and mod_proxy appends "?" whenever args is non-NULL.

Usage:
    python3 benchmarks/parity_check.py          # build, bring all four edges up, check, tear down
    python3 benchmarks/parity_check.py --keep   # leave the stack running afterwards
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from wadm_timings import ensure_compose_cleanup, run_cmd


@dataclass(frozen=True)
class Edge:
    label: str
    profile: str
    service: str
    port: int


EDGES = [
    Edge("OpenResty", "openresty", "openresty", 8080),
    Edge("Envoy+Lua", "envoy", "envoy", 8081),
    Edge("Envoy+WASM", "wasm", "envoy-wasm", 8082),
    Edge("Apache", "apache", "apache", 8083),
]
REFERENCE = EDGES[0]


@dataclass(frozen=True)
class Probe:
    name: str
    method: str
    path: str
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes | None = None


FORM = (("Content-Type", "application/x-www-form-urlencoded"),)

PROBES = [
    Probe("benchmark: injection only", "GET", "/"),
    Probe("benchmark: html_comments detection", "GET",
          "/api/login?password=internal-admin.example.com"),
    Probe("benchmark: form/header/cookie tamper", "GET",
          "/login.html?is_admin=1&probe=app-07.internal.example.com",
          (("Cookie", "admin_ui=1"),)),
    Probe("benchmark: decoy path", "GET", "/api/v1/debug"),
    Probe("exact-path token (/index.html)", "GET", "/index.html"),
    Probe("exact-path token (/dashboard.html)", "GET", "/dashboard.html"),
    Probe("strip keeps order of other params", "GET",
          "/about.html?a=1&password=internal-admin.example.com&b=2"),
    Probe("percent-encoded keyword", "GET", "/about.html?q=internal%2Dadmin.example.com"),
    Probe("plus-encoded keyword", "GET", "/about.html?note=key+abc123+here"),
    Probe("duplicated form field", "GET", "/login.html?is_admin=0&is_admin=2"),
    Probe("decoy sub-path", "GET", "/api/v1/debug/users"),
    Probe("SQLi trap: signature hit", "POST", "/api/login", FORM,
          b"username=admin%27+OR+1%3D1--&password=x"),
    Probe("SQLi trap: clean login", "POST", "/api/login", FORM,
          b"username=alice&password=secret"),
]

IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
WADM_LINE_RE = re.compile(r"(WADM (?:ALERT|TRAP): .*)$")
# nginx appends request context (", client: …, server: …") to every error-log line.
NGINX_SUFFIX_RE = re.compile(r", client: .*$")
EMPTY_QUERY_RE = re.compile(r"\? (?=HTTP/)")
ORIGIN_LINE_RE = re.compile(r'^(?:\S+\s+\|\s+)?(\S+) - \[[^\]]*\] "([^"]*)" (\d{3}) .*xff="([^"]*)"')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - urllib hook signature
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def normalise_ip(text: str) -> str:
    return IPV4_RE.sub("<ip>", text)


def send(edge: Edge, probe: Probe) -> dict[str, object]:
    req = urllib.request.Request(
        f"http://localhost:{edge.port}{probe.path}",
        data=probe.body,
        method=probe.method,
        headers=dict(probe.headers),
    )
    try:
        resp = OPENER.open(req, timeout=10)
    except urllib.error.HTTPError as err:
        resp = err
    body = resp.read()
    return {
        "status": resp.status,
        "x-backend-server": resp.headers.get("X-Backend-Server"),
        "set-cookie": sorted(resp.headers.get_all("Set-Cookie") or []),
        "body_sha256": hashlib.sha256(body).hexdigest()[:16],
        "body_len": len(body),
    }


def wait_until_up(edge: Edge, timeout_s: int = 120) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with OPENER.open(f"http://localhost:{edge.port}/", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(1)
    return False


def container_ip(service: str, env: dict[str, str]) -> str | None:
    cid = run_cmd(["docker", "compose", "ps", "-q", service], env=env).stdout.strip()
    if not cid:
        return None
    ip = run_cmd(
        ["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", cid],
        env=env,
    ).stdout.strip()
    return ip or None


def service_logs(service: str, since: str, env: dict[str, str]) -> list[str]:
    result = run_cmd(["docker", "compose", "logs", "--no-color", "--since", since, service], env=env)
    return (result.stdout + result.stderr).splitlines()


def wadm_lines(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        # Apache escapes non-ASCII bytes in its error log; fold the em dash back for comparison.
        line = line.replace("\\xe2\\x80\\x94", "—")
        m = WADM_LINE_RE.search(line)
        if m:
            out.append(normalise_ip(NGINX_SUFFIX_RE.sub("", m.group(1))).rstrip())
    return out


def origin_lines(lines: list[str], edge_ip: str | None) -> list[str]:
    out = []
    for line in lines:
        m = ORIGIN_LINE_RE.search(line)
        if not m or m.group(1) != edge_ip:
            continue
        request, status, xff = m.group(2), m.group(3), m.group(4)
        if request.startswith("POST /api/login"):
            continue
        request = EMPTY_QUERY_RE.sub(" ", request)
        out.append(f'{request} -> {status} xff="{normalise_ip(xff)}"')
    return out


def check_edge(edge: Edge, env: dict[str, str]) -> dict[str, object]:
    since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # --since is second-granular; starting on a fresh second keeps the previous edge's lines out.
    time.sleep(1.1)
    responses = {probe.name: send(edge, probe) for probe in PROBES}
    time.sleep(1.5)
    return {
        "responses": responses,
        "alerts": wadm_lines(service_logs(edge.service, since, env)),
        "origin": origin_lines(service_logs("backend", since, env), container_ip(edge.service, env)),
    }


def diff(reference: dict[str, object], other: dict[str, object]) -> list[str]:
    problems = []
    for name, ref_resp in reference["responses"].items():
        got = other["responses"][name]
        for key, ref_val in ref_resp.items():
            if got[key] != ref_val:
                problems.append(f"  [{name}] {key}: expected {ref_val!r}, got {got[key]!r}")
    for section in ("alerts", "origin"):
        ref_lines, got_lines = reference[section], other[section]
        if ref_lines != got_lines:
            problems.append(f"  {section} differ:")
            problems += [f"    - {line}" for line in ref_lines if line not in got_lines]
            problems += [f"    + {line}" for line in got_lines if line not in ref_lines]
            if set(ref_lines) == set(got_lines):
                problems.append("    (same lines, different order or count)")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--keep", action="store_true", help="Leave the stack running afterwards.")
    args = parser.parse_args()

    env = os.environ.copy()
    ensure_compose_cleanup(env)
    up = ["docker", "compose"]
    for edge in EDGES:
        up += ["--profile", edge.profile]
    up += ["up", "-d", "--build"]
    result = run_cmd(up, env=env)
    if result.returncode != 0:
        print(f"Failed to start the edges:\n{result.stderr}", file=sys.stderr)
        return 2

    try:
        for edge in EDGES:
            if not wait_until_up(edge):
                print(f"{edge.label} did not come up on port {edge.port}", file=sys.stderr)
                return 2

        results = {}
        for edge in EDGES:
            print(f"Probing {edge.label}...")
            results[edge.label] = check_edge(edge, env)

        ref = results[REFERENCE.label]
        print(f"\nReference ({REFERENCE.label}): {len(ref['alerts'])} alert/trap lines, "
              f"{len(ref['origin'])} origin requests")
        for line in ref["alerts"]:
            print(f"  {line}")
        for line in ref["origin"]:
            print(f"  origin: {line}")

        failed = False
        for edge in EDGES[1:]:
            problems = diff(ref, results[edge.label])
            if problems:
                failed = True
                print(f"\n{edge.label}: MISMATCH")
                print("\n".join(problems))
            else:
                print(f"\n{edge.label}: identical to {REFERENCE.label}")
        return 1 if failed else 0
    finally:
        if not args.keep:
            ensure_compose_cleanup(env)


if __name__ == "__main__":
    raise SystemExit(main())
