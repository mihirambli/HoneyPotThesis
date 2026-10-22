#!/usr/bin/env python3
"""Check that the WADM variants behave identically on a fixed set of requests.

The latency comparison is only meaningful if every edge does the same detection and injection
work, so this is the functional counterpart to the benchmark runners. Each edge receives the same
requests; for each one it records what the attacker sees (status, bait headers, body hash), what
the edge alerts on (`WADM ALERT` / `WADM TRAP` lines), and what reached the origin (its access
log, which shows whether a trigger keyword was stripped).

The reference is OpenResty in-server, the reference variant of the thesis report. Because it
serves the pages itself, it has no origin traffic to compare against, so the rules are:
  * every edge: status, bait header and Set-Cookie on every probe; body hash and length only where
    the reference answered 200 (elsewhere the body is an error page, rendered by whichever server
    produced it); the alert and trap lines must be identical;
  * in-server edges: the origin must receive nothing from them;
  * proxy edges: what they forward must be identical across the proxy edges (compared with the
    first proxy edge of the run), and no forwarded query may still carry an html_comments trigger
    keyword, which is the strip check the in-server reference cannot provide.
Before any probing, the OpenResty in-server config's Lua block is checked to be byte-identical to
nginx/nginx.conf's, because that is the only module code the two OpenResty configs do not share as
a file.

By default the four variants of the thesis report run (OpenResty in-server, Envoy+Lua, Envoy+WASM,
Apache in-server). `--all` adds the OpenResty and Apache proxy variants, which the report no longer
includes but whose code the in-server variants share.

Residual differences that are documented rather than levelled are excluded:
  * response framing (chunked vs Content-Length);
  * the Envoy+WASM SQLi trap contacting the origin (docs/EDGE_LEVELING.md explains why); POSTs to
    the trap are left out of the forwarded-request comparison;
  * Apache proxy (`--all` only) forwarding `path?` rather than `path` when the strip removes every
    parameter: mod_lua can only assign r.args a string, and mod_proxy appends "?" whenever args is
    non-NULL;
  * error-page bodies: in-server variants render their own, proxy variants forward the
    nginx:alpine origin's, so only their status and baits are compared;
  * in-server: the keyword strip still runs but has no downstream observer, so it is verified only
    through the alert lines (and, for the shared code, through the proxy edges' forwarded queries);
  * Apache in-server: Apache renders its own error pages through ap_send_error_response, which
    resets the output filters to the protocol filters and so drops the WADM_INJECT resource
    filter. Its 404 pages carry the bait header and cookie (err_headers_out) but no body tokens.
    Not visible here, since 404 bodies are not compared; benchmarks/functional_matrix.py records it.
  * Apache in-server: /test/empty's 204 comes from `RedirectMatch 204`, which answers at
    translate-name time, before the access hook, so it carries no bait header and raises no alert.
    No probe here requests it; benchmarks/functional_matrix.py records it.

Usage:
    python3 benchmarks/parity_check.py          # the four report variants: up, check, tear down
    python3 benchmarks/parity_check.py --all    # also the OpenResty and Apache proxy variants
    python3 benchmarks/parity_check.py --keep   # leave the stack running afterwards
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from wadm_timings import ensure_compose_cleanup, run_cmd


@dataclass(frozen=True)
class Edge:
    label: str
    profile: str
    service: str
    port: int
    deployment: str = "proxy"


REFERENCE = Edge("OpenResty in-server", "openresty-inserver", "openresty-inserver", 8084, "inserver")
REPORT_EDGES = [
    REFERENCE,
    Edge("Envoy+Lua", "envoy", "envoy", 8081),
    Edge("Envoy+WASM", "wasm", "envoy-wasm", 8082),
    Edge("Apache in-server", "apache-inserver", "apache-inserver", 8085, "inserver"),
]
# Excluded from the thesis report, kept checkable because the in-server variants run their code.
ARCHIVED_EDGES = [
    Edge("OpenResty (proxy)", "openresty", "openresty", 8080),
    Edge("Apache (proxy)", "apache", "apache", 8083),
]

REPO_ROOT = Path(__file__).resolve().parent.parent
PROXY_NGINX_CONF = REPO_ROOT / "nginx" / "nginx.conf"
INSERVER_NGINX_CONF = REPO_ROOT / "nginx" / "nginx-inserver.conf"
CONFIG_JSON = REPO_ROOT / "config.json"
# Only meaningful on a 200: elsewhere the body is an error page, and each server renders its own.
BODY_KEYS = ("body_sha256", "body_len")


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


def wadm_lua_block(path: Path) -> list[str]:
    """From `lua_shared_dict wadm_state` through the closing brace of `init_by_lua_block`.

    The block's own Lua is indented deeper than four spaces, so the first four-space `}` after the
    opening line is the block's end.
    """
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if "lua_shared_dict wadm_state" in line)
    opening = next(i for i in range(start, len(lines)) if "init_by_lua_block {" in lines[i])
    end = next(i for i in range(opening, len(lines)) if re.fullmatch(r"    }\s*", lines[i]))
    return lines[start:end + 1]


def assert_inserver_lua_in_sync() -> bool:
    proxy, inserver = wadm_lua_block(PROXY_NGINX_CONF), wadm_lua_block(INSERVER_NGINX_CONF)
    if proxy == inserver:
        print(f"Lua sync: {INSERVER_NGINX_CONF.name} carries nginx.conf's WADM block verbatim "
              f"({len(proxy)} lines)")
        return True
    print("Lua sync FAILED: the in-server WADM block differs from nginx.conf's:", file=sys.stderr)
    sys.stderr.writelines(difflib.unified_diff(
        proxy, inserver, fromfile=str(PROXY_NGINX_CONF), tofile=str(INSERVER_NGINX_CONF),
    ))
    return False


def html_comment_keywords() -> list[str]:
    tokens = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))["honeytokens"]["html_comments"]
    return [t["trigger_keyword"] for t in tokens
            if t.get("trigger_keyword") and str(t.get("enabled", 1)).lower() in ("1", "true", "on", "yes")]


def unstripped_keywords(origin: list[str], keywords: list[str]) -> list[str]:
    """Forwarded requests whose decoded query still carries a trigger keyword (R2 violated)."""
    leaks = []
    for line in origin:
        target = line.split(" ")[1] if " " in line else ""
        query = target.split("?", 1)[1] if "?" in target else ""
        for key, value in parse_qsl(query, keep_blank_values=True):
            if any(kw in key or kw in value for kw in keywords):
                leaks.append(line)
                break
    return leaks


def diff_lines(section: str, ref_lines: list[str], got_lines: list[str]) -> list[str]:
    if ref_lines == got_lines:
        return []
    problems = [f"  {section} differ:"]
    problems += [f"    - {line}" for line in ref_lines if line not in got_lines]
    problems += [f"    + {line}" for line in got_lines if line not in ref_lines]
    if set(ref_lines) == set(got_lines):
        problems.append("    (same lines, different order or count)")
    return problems


def diff(reference: dict[str, object], other: dict[str, object]) -> list[str]:
    """Responses and alerts against the in-server reference; body only where it answered 200."""
    problems = []
    for name, ref_resp in reference["responses"].items():
        got = other["responses"][name]
        for key, ref_val in ref_resp.items():
            if key in BODY_KEYS and ref_resp["status"] != 200:
                continue
            if got[key] != ref_val:
                problems.append(f"  [{name}] {key}: expected {ref_val!r}, got {got[key]!r}")
    return problems + diff_lines("alerts", reference["alerts"], other["alerts"])


def check_origin(edge: Edge, result: dict[str, object], proxy_ref: dict[str, object] | None,
                 keywords: list[str]) -> list[str]:
    if edge.deployment == "inserver":
        if not result["origin"]:
            return []
        return ["  origin contacted by an in-server variant:"] + [f"    + {l}" for l in result["origin"]]
    problems = [f"  keyword reached the origin: {l}" for l in unstripped_keywords(result["origin"], keywords)]
    if proxy_ref is not None:
        problems += diff_lines("forwarded requests", proxy_ref["origin"], result["origin"])
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--keep", action="store_true", help="Leave the stack running afterwards.")
    parser.add_argument("--all", action="store_true",
                        help="Also check the OpenResty and Apache proxy variants.")
    args = parser.parse_args()
    edges = REPORT_EDGES + (ARCHIVED_EDGES if args.all else [])

    if not assert_inserver_lua_in_sync():
        return 2

    env = os.environ.copy()
    ensure_compose_cleanup(env)
    up = ["docker", "compose"]
    for edge in edges:
        up += ["--profile", edge.profile]
    up += ["up", "-d", "--build"]
    result = run_cmd(up, env=env)
    if result.returncode != 0:
        print(f"Failed to start the edges:\n{result.stderr}", file=sys.stderr)
        return 2

    try:
        for edge in edges:
            if not wait_until_up(edge):
                print(f"{edge.label} did not come up on port {edge.port}", file=sys.stderr)
                return 2

        results = {}
        for edge in edges:
            print(f"Probing {edge.label}...")
            results[edge.label] = check_edge(edge, env)

        ref = results[REFERENCE.label]
        keywords = html_comment_keywords()
        print(f"\nReference ({REFERENCE.label}): {len(ref['alerts'])} alert/trap lines")
        for line in ref["alerts"]:
            print(f"  {line}")

        failed = False
        proxy_ref_edge = next(e for e in edges if e.deployment == "proxy")
        proxy_ref = results[proxy_ref_edge.label]
        print(f"\nForwarded by {proxy_ref_edge.label} (proxy-edge reference): "
              f"{len(proxy_ref['origin'])} origin requests")
        for line in proxy_ref["origin"]:
            print(f"  origin: {line}")

        for edge in edges:
            result = results[edge.label]
            problems = [] if edge is REFERENCE else diff(ref, result)
            problems += check_origin(edge, result, None if edge is proxy_ref_edge else proxy_ref,
                                     keywords)
            if problems:
                failed = True
                print(f"\n{edge.label}: MISMATCH")
                print("\n".join(problems))
            elif edge is REFERENCE:
                print(f"\n{edge.label}: reference; no origin requests")
            elif edge.deployment == "inserver":
                print(f"\n{edge.label}: identical to {REFERENCE.label}; no origin requests")
            else:
                print(f"\n{edge.label}: identical to {REFERENCE.label}; forwarded "
                      f"{len(result['origin'])} requests, identical across proxy edges, "
                      f"no trigger keyword among them")
        return 1 if failed else 0
    finally:
        if not args.keep:
            ensure_compose_cleanup(env)


if __name__ == "__main__":
    raise SystemExit(main())
