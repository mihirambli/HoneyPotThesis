#!/usr/bin/env python3
"""
Does WADM break what the server did without it? One request per case, WADM tier against bare tier.

The latency suite checks that its own eight probes come back intact under load. This asks the
broader question once, without load, across the response shapes a real site serves and the probe
set never exercises: compressed bodies, HEAD, conditional and range requests, bodiless responses,
non-HTML content, pages without an injection anchor, pages larger than a proxy's buffer, and an
HTTP/1.0 client. Reading the edge code predicted breakage in several of these (no Content-Encoding
guard; baits on every response; full-body buffering bounded at 1 MiB on Envoy); this measures it.

The reference is each edge's BARE tier — the same server, same config, minus WADM — because that is
what "existing functionality" means for the thesis. That holds for the two in-server variants too:
their reference is the same web server serving backend/www itself without the module, so their
columns say what WADM changes in that deployment, not how they differ from a proxy. For every case
and variant the WADM response is classified:

    not exercised    the bare edge itself refuses the request (Envoy answers HTTP/1.0 with 426), so
                     there is no existing behaviour for WADM to preserve or break
    preserved        byte-identical to bare (status, body, framing-relevant headers)
    injected         HTML whose only difference from bare is inserted text — the intended effect
    changed          a difference that is not breakage but should be reported (e.g. a bait header
                     on a JSON response, a validator kept on a body that changed)
    BROKEN           the client receives something the bare server would not have produced and
                     cannot use: a different status, an undecodable body, deleted content

Writes:
  benchmarks/results/functional_matrix.json   every response, both tiers, per edge, with verdicts
  benchmarks/results/functional_matrix.md     the case x edge verdict table

Usage:
    python3 benchmarks/functional_matrix.py
"""

from __future__ import annotations

import argparse
import difflib
import gzip
import hashlib
import http.client
import json
import os
import re
import sys
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from edge_specs import EDGE_KEYS, EDGES, INSERVER_KEYS, write_silent_config
from wadm_timings import ensure_compose_cleanup, ensure_test_pages, lines_since, run_cmd

# Host ports docker-compose.yml publishes for each edge, so all six can be probed at once.
PORTS = {
    "openresty": 8080, "envoy_lua": 8081, "wasm": 8082, "apache_lua": 8083,
    "openresty_inserver": 8084, "apache_inserver": 8085,
}

# The four proxy edges first, so their columns keep the order of earlier matrices.
MATRIX_KEYS = EDGE_KEYS + INSERVER_KEYS

# Strings an injected insertion is expected to carry: the comment token, the decoy link's target,
# and the hidden form field. An insertion containing none of them is not WADM's and is reported.
TOKEN_MARKERS = ["DEV-PORTAL", "/api/v1/debug", 'name="is_admin"', "abc123", "sk_live_"]

# Headers whose difference matters to a client or cache. Content-Length vs. chunked framing is
# recorded but judged separately: removing a length while keeping a correct body is a known,
# documented consequence of body rewriting, not breakage.
COMPARED_HEADERS = [
    "content-type", "content-encoding", "content-length", "transfer-encoding", "etag",
    "last-modified", "content-range", "x-backend-server", "set-cookie", "connection",
]


@dataclass(frozen=True)
class Case:
    name: str
    path: str
    method: str = "GET"
    headers: tuple[tuple[str, str], ...] = ()
    http10: bool = False
    # Set when the request needs a validator from the bare response first (conditional GET).
    conditional: bool = False
    # A benign request must produce no alert on any edge; this is the false-positive check.
    benign: bool = True
    # An error status the bare tier is EXPECTED to return, as opposed to one that means the edge
    # refused the case and nothing was exercised.
    expect_error: bool = False


CASES = [
    Case("HTML page", "/about.html"),
    Case("HTML, gzip-encoded", "/test/gz/page.html", headers=(("Accept-Encoding", "gzip"),)),
    Case("HEAD of an HTML page", "/about.html", method="HEAD"),
    Case("conditional GET (If-None-Match)", "/about.html", conditional=True),
    Case("range request (bytes=0-99)", "/about.html", headers=(("Range", "bytes=0-99"),)),
    Case("204 No Content", "/test/empty"),
    Case("404 page", "/missing-page.html", expect_error=True),
    Case("JSON", "/test/data.json"),
    Case("binary", "/test/blob.bin"),
    Case("HTML without </body>", "/test/no-body-close.html"),
    Case("HTML, 768 KiB", "/test/size-768k.html"),
    Case("HTML, 2 MiB (above Envoy's 1 MiB buffer)", "/test/size-2m.html"),
    Case("HTTP/1.0 client", "/about.html", http10=True),
]


class HTTP10Connection(http.client.HTTPConnection):
    _http_vsn = 10
    _http_vsn_str = "HTTP/1.0"


@dataclass
class Response:
    status: int
    headers: dict[str, list[str]]
    body: bytes
    error: str | None = None
    decoded: bytes | None = field(default=None)

    def header(self, name: str) -> str | None:
        values = self.headers.get(name.lower())
        return ", ".join(values) if values else None

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "error": self.error,
            "headers": {h: self.header(h) for h in COMPARED_HEADERS if self.header(h) is not None},
            "body_bytes": len(self.body),
            "body_sha256": hashlib.sha256(self.body).hexdigest()[:16],
            "decoded_bytes": None if self.decoded is None else len(self.decoded),
        }


def fetch(port: int, case: Case, extra: dict[str, str] | None = None) -> Response:
    conn_cls = HTTP10Connection if case.http10 else http.client.HTTPConnection
    conn = conn_cls("localhost", port, timeout=15)
    try:
        conn.request(case.method, case.path, headers={**dict(case.headers), **(extra or {})})
        resp = conn.getresponse()
        headers: dict[str, list[str]] = {}
        for k, v in resp.getheaders():
            headers.setdefault(k.lower(), []).append(v)
        try:
            body = resp.read()
            error = None
        except (http.client.IncompleteRead, ConnectionError) as exc:
            body = getattr(exc, "partial", b"") or b""
            error = f"body read failed: {type(exc).__name__}"
        out = Response(resp.status, headers, body, error)
    except (OSError, http.client.HTTPException) as exc:
        return Response(0, {}, b"", f"request failed: {type(exc).__name__}: {exc}")
    finally:
        conn.close()
    encoding = (out.header("content-encoding") or "").lower()
    if "gzip" in encoding and out.body:
        try:
            out.decoded = gzip.decompress(out.body)
        except (OSError, EOFError, zlib.error) as exc:
            out.error = f"gzip body does not decode: {type(exc).__name__}"
    return out


def text(resp: Response) -> str:
    raw = resp.decoded if resp.decoded is not None else resp.body
    return raw.decode("utf-8", errors="replace")


def _char_diff(bare: str, wadm: str) -> tuple[list[str], list[str]]:
    matcher = difflib.SequenceMatcher(None, bare, wadm, autojunk=False)
    inserted, removed = [], []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op in ("insert", "replace"):
            inserted.append(wadm[j1:j2])
        if op in ("delete", "replace"):
            removed.append(bare[i1:i2])
    return inserted, removed


def insertion_only(bare: str, wadm: str) -> tuple[bool, list[str], list[str]]:
    """Whether `wadm` is `bare` plus inserted text only; returns (ok, inserted, removed).

    Diffed by line first and by character only inside the blocks that differ: a character-level
    diff is quadratic, and on the 2 MiB page it ran for minutes.
    """
    a, b = bare.splitlines(keepends=True), wadm.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    inserted, removed = [], []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "insert":
            inserted.append("".join(b[j1:j2]))
        elif op in ("delete", "replace"):
            ins, rem = _char_diff("".join(a[i1:i2]), "".join(b[j1:j2]))
            inserted += ins
            removed += rem
    return not removed, inserted, removed


def judge(
    case: Case, bare: Response, wadm: Response, alerts: int, get_length: int | None = None
) -> tuple[str, list[str]]:
    """Verdict and the reasons behind it for one case on one edge.

    `get_length` is the body length the same tier returns to a GET of the same path, which a HEAD
    response's Content-Length must equal.
    """
    notes: list[str] = []
    broken: list[str] = []
    if bare.status >= 400 and not case.expect_error and wadm.status == bare.status:
        return "not exercised", [f"the bare edge itself answers {bare.status}"]
    if wadm.error:
        broken.append(wadm.error)
    if bare.error:
        notes.append(f"bare tier itself: {bare.error}")
    if wadm.status != bare.status:
        broken.append(f"status {bare.status} -> {wadm.status}")
    if (wadm.header("content-encoding") or "") != (bare.header("content-encoding") or ""):
        broken.append(
            f"content-encoding {bare.header('content-encoding')!r} -> {wadm.header('content-encoding')!r}"
        )
    if case.method == "HEAD" and wadm.body:
        broken.append(f"HEAD response carries a {len(wadm.body)}-byte body")
    advertised = wadm.header("content-length")
    if case.method == "HEAD" and advertised and get_length is not None and int(advertised) != get_length:
        broken.append(f"HEAD advertises Content-Length {advertised} but a GET returns "
                      f"{get_length} bytes")
    if case.benign and alerts:
        broken.append(f"{alerts} alert(s) on a benign request (false positive)")

    is_html = "text/html" in (bare.header("content-type") or "")
    same_body = (wadm.decoded or wadm.body) == (bare.decoded or bare.body)
    injected = False
    undecodable = bool(wadm.error and "decode" in wadm.error)
    if not same_body and not undecodable:
        if is_html and bare.status in (200, 404) and case.method != "HEAD":
            ok, inserted, removed = insertion_only(text(bare), text(wadm))
            if not ok:
                broken.append(f"{len(removed)} span(s) of the original body removed or replaced")
            foreign = [s for s in inserted if not any(m in s for m in TOKEN_MARKERS)]
            if foreign:
                notes.append(f"{len(foreign)} inserted span(s) carry no known token marker")
            injected = True
        elif bare.status == 206:
            broken.append("partial-content body modified: the client reassembles a corrupt resource")
        else:
            broken.append(f"non-HTML body modified ({len(bare.body)} -> {len(wadm.body)} bytes)")

    if injected and wadm.header("etag") and wadm.header("etag") == bare.header("etag"):
        notes.append("body changed but the origin's ETag kept: a cache or client can treat the "
                     "modified page as the original")
    if not is_html and wadm.header("x-backend-server") and not bare.header("x-backend-server"):
        notes.append("bait header added to a non-HTML response")
    if (wadm.header("content-length") is None) != (bare.header("content-length") is None):
        notes.append(f"framing: content-length {bare.header('content-length')!r} -> "
                     f"{wadm.header('content-length')!r}, transfer-encoding "
                     f"{wadm.header('transfer-encoding')!r}")
    if case.http10 and wadm.header("transfer-encoding"):
        broken.append("chunked transfer-encoding sent to an HTTP/1.0 client")

    if broken:
        return "BROKEN", broken + notes
    if injected:
        return "injected", notes
    if notes:
        return "changed", notes
    return "preserved", []


def bring_up(tier: str, env: dict[str, str]) -> bool:
    tier_env = dict(env)
    for key in MATRIX_KEYS:
        tier_env.update(EDGES[key].tier_env(tier))
    command = ["docker", "compose"]
    for key in MATRIX_KEYS:
        command += ["--profile", EDGES[key].profile]
    result = run_cmd(command + ["up", "-d", "--build"], env=tier_env)
    if result.returncode != 0:
        print(f"Failed to start the {tier} tier:\n{result.stderr}", file=sys.stderr)
        return False
    for key in MATRIX_KEYS:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if fetch(PORTS[key], Case("up", "/")).status == 200:
                break
            time.sleep(1)
        else:
            print(f"{EDGES[key].label} did not come up", file=sys.stderr)
            return False
    return True


def alert_count(service: str, profile: str, since: float, env: dict[str, str]) -> int:
    stamp = datetime.fromtimestamp(since - 1, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    logs = run_cmd(
        ["docker", "compose", "--profile", profile, "logs", "--no-color", "--timestamps",
         "--since", stamp, service], env=env,
    ).stdout
    return len(re.findall(r"WADM (?:ALERT|TRAP):", lines_since(logs, since)))


def probe_tier(tier: str, env: dict[str, str], etags: dict[str, str]) -> dict[str, Any]:
    """Every case against every edge, with the alerts each one produced."""
    out: dict[str, Any] = {}
    for key in MATRIX_KEYS:
        spec = EDGES[key]
        out[key] = {}
        for case in CASES:
            extra = {}
            if case.conditional:
                if tier == "bare":
                    etags[key] = fetch(PORTS[key], Case("etag", case.path)).header("etag") or ""
                extra = {"If-None-Match": etags.get(key, "")}
            started = time.time()
            resp = fetch(PORTS[key], case, extra)
            # Alert lines arrive through the log pipeline; give them a moment before counting.
            time.sleep(0.3)
            alerts = alert_count(spec.service, spec.profile, started, env)
            out[key][case.name] = (resp, alerts)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--results", default=None, help="Results directory.")
    parser.add_argument("--keep", action="store_true", help="Leave the WADM tier running.")
    args = parser.parse_args()

    results_dir = Path(args.results) if args.results else Path(__file__).resolve().parent / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    ensure_test_pages()
    write_silent_config()

    etags: dict[str, str] = {}
    tiers: dict[str, Any] = {}
    try:
        for tier in ("bare", "wadm"):
            ensure_compose_cleanup(env)
            print(f"Bringing up all six variants, {tier} tier...")
            if not bring_up(tier, env):
                return 2
            tiers[tier] = probe_tier(tier, env, etags)
    finally:
        if not args.keep:
            ensure_compose_cleanup(env)

    doc: dict[str, Any] = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "script": "benchmarks/functional_matrix.py",
            "reference": "each edge's bare tier",
            "cases": [c.name for c in CASES],
        },
        "edges": {},
    }
    rows = []
    for key in MATRIX_KEYS:
        doc["edges"][key] = {}
        for case in CASES:
            bare, _ = tiers["bare"][key][case.name]
            wadm, alerts = tiers["wadm"][key][case.name]
            get_twin = next(
                (c for c in CASES if c.method == "GET" and c.path == case.path and not c.headers
                 and not c.http10 and not c.conditional), None,
            )
            get_length = (
                len(tiers["wadm"][key][get_twin.name][0].body) if get_twin else None
            )
            verdict, reasons = judge(case, bare, wadm, alerts, get_length)
            doc["edges"][key][case.name] = {
                "verdict": verdict,
                "reasons": reasons,
                "alerts": alerts,
                "bare": bare.summary(),
                "wadm": wadm.summary(),
            }
            rows.append((case.name, key, verdict, reasons))

    (results_dir / "functional_matrix.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")

    lines = [
        "# Functional matrix: WADM tier against each edge's bare tier",
        "",
        f"Generated by `benchmarks/functional_matrix.py` on {doc['metadata']['generated_at'][:10]}. "
        "`not exercised` = the bare edge itself refuses the request; "
        "`preserved` = identical to bare; `injected` = HTML with only inserted text (the intended "
        "effect); `changed` = a difference worth reporting that is not breakage; `BROKEN` = the "
        "client gets something bare would not have produced and cannot use. Reasons are below "
        "the table.",
        "",
        "| Case | " + " | ".join(EDGES[k].label for k in MATRIX_KEYS) + " |",
        "|---|" + "---|" * len(MATRIX_KEYS),
    ]
    for case in CASES:
        cells = [doc["edges"][k][case.name]["verdict"] for k in MATRIX_KEYS]
        lines.append(f"| {case.name} | " + " | ".join(
            f"**{c}**" if c == "BROKEN" else c for c in cells) + " |")
    lines += ["", "## Reasons", ""]
    for case_name, key, verdict, reasons in rows:
        if reasons:
            lines.append(f"- **{case_name} — {EDGES[key].label}** ({verdict}): " + "; ".join(reasons))
    (results_dir / "functional_matrix.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n".join(lines))
    broken = sum(1 for _, _, v, _ in rows if v == "BROKEN")
    print(f"\n{broken} broken case(s) across {len(MATRIX_KEYS)} variants. "
          f"Saved {results_dir / 'functional_matrix.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
