#!/usr/bin/env python3
"""
Append a later run's cells to an existing paired result file as additional replicates.

Used when host interference left an edge with too few valid pairs and a second pass was run into a
separate directory. Every cell of the second pass has its replicate number shifted past the
first file's highest, so a pair (bare vs. silent, hit vs. miss) is still always formed inside one
replicate — never across the two passes, which ran hours apart.

Usage:
    python3 benchmarks/merge_replicates.py <target.json> <second_pass.json> [...more pairs]
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def merge(target: Path, extra: Path) -> tuple[int, int]:
    doc = json.loads(target.read_text())
    more = json.loads(extra.read_text())
    if doc["metadata"].get("edge") != more["metadata"].get("edge") or \
            doc["metadata"].get("mix") != more["metadata"].get("mix"):
        raise SystemExit(f"{extra.name} is a different edge or mix from {target.name}")
    offset = max(c["replicate"] for c in doc["cells"])
    for cell in more["cells"]:
        cell["replicate"] += offset
        cell["merged_from"] = extra.name
    doc["cells"] += more["cells"]
    doc["metadata"].setdefault("merged_passes", []).append({
        "file": extra.name,
        "generated_at": more["metadata"].get("generated_at"),
        "replicates_added": sorted({c["replicate"] for c in more["cells"]}),
        "merged_at": datetime.now(timezone.utc).isoformat(),
    })
    doc["metadata"]["replicates"] = max(c["replicate"] for c in doc["cells"])
    target.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return offset, len(more["cells"])


def main() -> int:
    args = sys.argv[1:]
    if not args or len(args) % 2:
        print(__doc__)
        return 2
    for target, extra in zip(args[::2], args[1::2]):
        offset, added = merge(Path(target), Path(extra))
        print(f"  {Path(target).name}: +{added} cells as replicates {offset + 1}+")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
