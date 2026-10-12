#!/usr/bin/env python3
"""
Generate the origin's test pages into backend/www/test/: the response-size sweep and the
functional-matrix cases.

Generated rather than committed because two of them are megabytes of filler and one is random
bytes; the content is fully determined by this script, so regenerating yields identical files.
The origin serves backend/www read-only, so this must run before the backend container starts —
the benchmark runners call ensure_test_pages() for that reason.

    size-<n>.html          HTML of a fixed total size, for injection cost vs. body size
    size-2m.html           above Envoy's default 1 MiB buffer limit, for the functional matrix
    no-body-close.html     HTML without </body>, so the comment token has no anchor
    gz/page.html           served gzip-compressed (see backend/nginx.conf), to test injection
                           into an encoded body
    data.json              non-HTML text, which no edge should modify
    blob.bin               non-text bytes, which no edge should modify

Usage:
    python3 backend/generate_test_pages.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent / "www" / "test"

SIZES = {"1k": 1024, "16k": 16 * 1024, "256k": 256 * 1024, "768k": 768 * 1024, "2m": 2 * 1024 * 1024}

HEAD = (
    "<!DOCTYPE html>\n<html>\n<head><meta charset=\"utf-8\"><title>{title}</title></head>\n"
    "<body>\n<h1>{title}</h1>\n"
)
TAIL = "</body>\n</html>\n"
LINE = "<p>Quarterly figures for region {i:06d}: revenue steady, churn within tolerance.</p>\n"


def sized_page(title: str, size: int) -> str:
    """An HTML page of exactly `size` bytes, padded with paragraphs and closed with </body>."""
    head, tail = HEAD.format(title=title), TAIL
    body, i = [], 0
    room = size - len(head) - len(tail)
    while room > 0:
        line = LINE.format(i=i)
        if len(line) > room:
            # Close on the exact byte count: a comment of the remaining length, or spaces when
            # too little room is left for even an empty one.
            line = f"<!--{'x' * (room - 7)}-->" if room >= 7 else " " * room
        body.append(line)
        room -= len(line)
        i += 1
    page = head + "".join(body) + tail
    assert len(page.encode()) == size, (title, len(page), size)
    return page


def ensure_test_pages() -> Path:
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    for label, size in SIZES.items():
        (TEST_DIR / f"size-{label}.html").write_text(sized_page(f"size {label}", size))
    (TEST_DIR / "no-body-close.html").write_text(
        HEAD.format(title="no body close") + "<p>This page never closes its body.</p>\n"
    )
    (TEST_DIR / "gz").mkdir(exist_ok=True)
    (TEST_DIR / "gz" / "page.html").write_text(sized_page("gzip page", 4096))
    (TEST_DIR / "data.json").write_text(
        json.dumps({"service": "inventory", "items": [{"id": i, "qty": i * 3} for i in range(50)]})
    )
    (TEST_DIR / "blob.bin").write_bytes(random.Random(20261003).randbytes(32 * 1024))
    return TEST_DIR


if __name__ == "__main__":
    print(f"wrote test pages to {ensure_test_pages()}")
