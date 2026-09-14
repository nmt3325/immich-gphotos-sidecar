"""Assertions for the local-library end-to-end phase.

Usage: assert_local_e2e.py <state dir> <library root> <sha256 snapshot> [expected direct uploads] [gpmc store]
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

STATE = Path(sys.argv[1])
LIBRARY = Path(sys.argv[2])
SNAPSHOT = Path(sys.argv[3])
EXPECTED_DIRECT = int(sys.argv[4]) if len(sys.argv) > 4 else 0
STORE = Path(sys.argv[5]) if len(sys.argv) > 5 else None
ORIGINAL_NAMES = ["IMG_0001.jpg", "IMG_0002.jpg", "VID_0003.mp4"]

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}{(' :: ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


conn = sqlite3.connect(str(STATE / "sidecar-state.sqlite3"))
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT report FROM runs ORDER BY started_at").fetchall()
conn.close()
check("a run was recorded", bool(rows), f"{len(rows)} run(s)")
report = json.loads(rows[-1]["report"]) if rows else {}

check("run reports the local asset source", report.get("assetSource") == "local", str(report.get("assetSource")))
check("3 originals came from the library", report.get("local_reads") == 3, str(report.get("local_reads")))
check("nothing was downloaded over http", report.get("downloaded") == 0, str(report.get("downloaded")))
check("no http bytes transferred", report.get("bytes_downloaded") == 0, str(report.get("bytes_downloaded")))
check("bytes were read from disk", (report.get("bytes_local") or 0) > 0, str(report.get("bytes_local")))
check(
    f"{EXPECTED_DIRECT} upload(s) straight from the library",
    report.get("local_direct") == EXPECTED_DIRECT,
    str(report.get("local_direct")),
)
check("3 assets uploaded", report.get("uploaded") == 3, str(report.get("uploaded")))
check("3 album links created", report.get("album_links") == 3, str(report.get("album_links")))
check("no failures", report.get("failed") == 0, json.dumps(report.get("errors") or [])[:300])

# ---- the library itself must be untouched ----
snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
current = {
    str(path.relative_to(LIBRARY)): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted(LIBRARY.rglob("*"))
    if path.is_file()
}
check(
    "library file list unchanged",
    sorted(current) == sorted(snapshot),
    str(sorted(set(current) ^ set(snapshot)))[:200],
)
check(
    "library contents unchanged (nothing rewritten in place)",
    current == snapshot,
    str([name for name, digest in current.items() if snapshot.get(name) != digest])[:200],
)
check(
    "no temporary or exiftool backup files left behind",
    not [name for name in current if name.endswith((".part", ".tmp", "_original"))],
    str([name for name in current if name.endswith((".part", ".tmp", "_original"))])[:200],
)

# ---- google photos must see the original names, not immich's <assetId>.ext ----
if STORE is not None:
    store = json.loads(STORE.read_text(encoding="utf-8"))
    names = sorted(item["name"] for item in store.get("media", {}).values())
    check("uploads keep the original file names", names == ORIGINAL_NAMES, str(names))
    staged = sorted(
        {
            name
            for call in store.get("calls", [])
            if call.get("call") == "upload"
            for name in call.get("files", [])
        }
    )
    check("every staged file was named after the original", staged == ORIGINAL_NAMES, str(staged))

print()
if failures:
    print(f"{len(failures)} LOCAL LIBRARY CHECK(S) FAILED: {failures}")
    sys.exit(1)
print("ALL LOCAL LIBRARY CHECKS PASSED")
