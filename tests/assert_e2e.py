"""Assertions for the offline end-to-end test."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

STATE = Path(sys.argv[1])
SIDECAR = Path(sys.argv[2])
STORE = Path(sys.argv[3])

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}{(' :: ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


conn = sqlite3.connect(str(STATE / "sidecar-state.sqlite3"))
conn.row_factory = sqlite3.Row

runs = [dict(row) for row in conn.execute("SELECT * FROM runs ORDER BY started_at").fetchall()]
check("three runs recorded", len(runs) == 3, f"{len(runs)} runs")
reports = [json.loads(run["report"]) for run in runs]

if len(reports) >= 1:
    first = reports[0]
    check("run1 uploaded 3 assets", first["uploaded"] == 3, json.dumps(first["uploaded"]))
    check("run1 created 3 album links", first["album_links"] == 3, str(first["album_links"]))
    check("run1 wrote 3 sidecars", first["sidecars_written"] == 3, str(first["sidecars_written"]))
    check("run1 downloaded 3 originals", first["downloaded"] == 3, str(first["downloaded"]))
    check("run1 had no failures", first["failed"] == 0, json.dumps(first["errors"])[:300])
    check("run1 used 3 gotohp calls (2 albums + unsorted=0)", first["upload_calls"] == 2, str(first["upload_calls"]))

if len(reports) >= 2:
    second = reports[1]
    check("run2 is a no-op (nothing uploaded)", second["uploaded"] == 0, str(second["uploaded"]))
    check("run2 made no gotohp calls", second["upload_calls"] == 0, str(second["upload_calls"]))
    check("run2 wrote no new sidecars", second["sidecars_written"] == 0, str(second["sidecars_written"]))
    check("run2 planned no work", second["planned"] == 0, str(second["planned"]))
    check("run2 downloaded nothing again", second["downloaded"] == 0, str(second["downloaded"]))
    check(
        "run2 examined nothing (watermark honoured)",
        second["candidates"] == 0,
        str(second["candidates"]),
    )
    check("run2 had no failures", second["failed"] == 0, json.dumps(second["errors"])[:300])

if len(reports) >= 3:
    third = reports[2]
    check("run3 (pty + album backfill) restored 3 links", third["album_links"] == 3, str(third["album_links"]))
    check("run3 had no failures", third["failed"] == 0, json.dumps(third["errors"])[:300])

assets = [dict(row) for row in conn.execute("SELECT * FROM assets ORDER BY asset_id").fetchall()]
check("state knows 3 assets", len(assets) == 3, str(len(assets)))
check(
    "every asset has a media key",
    all(asset["media_key"] for asset in assets),
    json.dumps([(a["original_file_name"], a["media_key"]) for a in assets]),
)
check(
    "original filenames preserved in state",
    {a["original_file_name"] for a in assets} == {"IMG_0001.jpg", "IMG_0002.jpg", "VID_0003.mp4"},
    str(sorted(a["original_file_name"] for a in assets)),
)

links = conn.execute(
    "SELECT album_id, COUNT(*) AS c FROM album_links GROUP BY album_id ORDER BY album_id"
).fetchall()
check("two albums linked", len(links) == 2, str([tuple(row) for row in links]))
check(
    "album membership counts are 2 and 1",
    sorted(row["c"] for row in links) == [1, 2],
    str(sorted(row["c"] for row in links)),
)

albums = [dict(row) for row in conn.execute("SELECT * FROM albums").fetchall()]
check(
    "album names captured (incl. japanese)",
    {album["name"] for album in albums} == {"2024 \u65c5\u884c", "Trains"},
    str(sorted(album["name"] for album in albums)),
)
check(
    "google album keys recorded",
    all((album["gp_album_key"] or "").startswith("AF1Qip") for album in albums),
    str([album["gp_album_key"] for album in albums]),
)
conn.close()

# ---- sidecar files ----
json_files = sorted(SIDECAR.glob("assets/*/*.json"))
xmp_files = sorted(SIDECAR.glob("assets/*/*.xmp"))
check("3 json sidecars on disk", len(json_files) == 3, str([p.name for p in json_files]))
check("3 xmp sidecars on disk", len(xmp_files) == 3, str([p.name for p in xmp_files]))

payloads = {}
for path in json_files:
    data = json.loads(path.read_text(encoding="utf-8"))
    payloads[data["originalFileName"]] = data

first_asset = payloads.get("IMG_0001.jpg", {})
check("sidecar keeps original path", first_asset.get("originalPath", "").endswith("IMG_0001.jpg"))
check("sidecar keeps checksum", bool(first_asset.get("checksum")))
check("sidecar keeps tags", first_asset.get("tags") == ["travel", "spring"], str(first_asset.get("tags")))
check("sidecar keeps people", first_asset.get("people") == ["Kaichi"], str(first_asset.get("people")))
check(
    "sidecar keeps album membership",
    first_asset.get("albums") == ["2024 \u65c5\u884c"],
    str(first_asset.get("albums")),
)
check("sidecar keeps exif gps", first_asset.get("exifInfo", {}).get("latitude") == 35.7156)
xmp_text = (json_files[0].with_suffix(".xmp")).read_text(encoding="utf-8")
check("xmp is well formed", xmp_text.startswith("<?xpacket") and "immich:AssetId" in xmp_text)

library = SIDECAR / "library"
manifest_lines = [
    line for line in (library / "manifest.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()
]
check("manifest has 3 rows", len(manifest_lines) == 3, str(len(manifest_lines)))
albums_snapshot = json.loads((library / "albums.json").read_text(encoding="utf-8"))
check("album snapshot has 2 albums", len(albums_snapshot["albums"]) == 2)
check("tags snapshot exists", (library / "tags.json").exists())
check("people snapshot exists", (library / "people.json").exists())
reports_dir = SIDECAR / "reports"
check("3 run reports written", len(list(reports_dir.glob("*.json"))) == 3)

# ---- fake google photos side ----
store = json.loads(STORE.read_text(encoding="utf-8"))
check("google side has 3 media items", len(store["media"]) == 3, str(len(store["media"])))
check(
    "google albums created with immich names",
    set(store["albums"]) == {"2024 \u65c5\u884c", "Trains"},
    str(sorted(store["albums"])),
)
check(
    "google album membership 2 + 1",
    sorted(len(v) for v in store["albums"].values()) == [1, 2],
    str({k: len(v) for k, v in store["albums"].items()}),
)
upload_calls = [call for call in store["calls"] if call and call[0] == "upload"]
check(
    "uploads always passed -a for album batches",
    all("-a" in call for call in upload_calls),
    str(len(upload_calls)) + " upload calls",
)
check(
    "uploads used the configured thread count",
    all("-t" in call and call[call.index("-t") + 1] == "2" for call in upload_calls),
)
check(
    "uploads passed the config path",
    all("-c" in call for call in upload_calls),
)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED: {failures}")
    sys.exit(1)
print("ALL E2E CHECKS PASSED")
