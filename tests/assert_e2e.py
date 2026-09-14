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
    check("run1 used 2 upload calls (2 albums + unsorted=0)", first["upload_calls"] == 2, str(first["upload_calls"]))
    check("run1 reports the gpmc album backend", first["albumBackend"] == "gpmc", str(first["albumBackend"]))

if len(reports) >= 2:
    second = reports[1]
    check("run2 is a no-op (nothing uploaded)", second["uploaded"] == 0, str(second["uploaded"]))
    check("run2 made no upload calls", second["upload_calls"] == 0, str(second["upload_calls"]))
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
    check("run3 (album backfill) restored 3 links", third["album_links"] == 3, str(third["album_links"]))
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
check(
    "sidecar keeps original path",
    first_asset.get("originalPath", "").endswith("aaaa1111-0000-4000-8000-000000000001.jpg"),
    str(first_asset.get("originalPath")),
)
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
calls = store["calls"]
upload_calls = [call for call in calls if call.get("call") == "upload"]
create_album_calls = [call for call in calls if call.get("call") == "add_to_album"]
existing_album_calls = [call for call in calls if call.get("call") == "add_to_existing_album"]

check("google side has 3 media items", len(store["media"]) == 3, str(len(store["media"])))
ORIGINAL_NAMES = ["IMG_0001.jpg", "IMG_0002.jpg", "VID_0003.mp4"]
check(
    "google photos got the original file names (not immich's <assetId>.ext)",
    sorted(item["name"] for item in store["media"].values()) == ORIGINAL_NAMES,
    str(sorted(item["name"] for item in store["media"].values())),
)
staged_names = sorted({name for call in upload_calls for name in call["files"]})
check(
    "every staged file was named after the original",
    staged_names == ORIGINAL_NAMES,
    str(staged_names),
)
check(
    "google albums created with immich names",
    set(store["album_names"].values()) == {"2024 \u65c5\u884c", "Trains"},
    str(sorted(store["album_names"].values())),
)
check(
    "exactly 2 google albums exist (no duplicates on re-runs)",
    len(store["albums"]) == 2,
    str(list(store["album_names"].values())),
)
check(
    "google album membership 2 + 1",
    sorted(len(v) for v in store["albums"].values()) == [1, 2],
    str({store["album_names"].get(k, k): len(v) for k, v in store["albums"].items()}),
)
check(
    "albums created once, then appended to by media key",
    len(create_album_calls) == 2 and len(existing_album_calls) >= 2,
    f"create={len(create_album_calls)} append={len(existing_album_calls)}",
)
check(
    "no append call hit an unknown album",
    all("error" not in call for call in existing_album_calls),
    str([call for call in existing_album_calls if "error" in call])[:200],
)
check(
    "uploads never mixed album handling into gpmc.upload()",
    all(call["albumName"] is None and call["albumId"] is None for call in upload_calls),
    str(len(upload_calls)) + " upload calls",
)
check(
    "uploads used the configured thread count",
    all(call["threads"] == 2 for call in upload_calls),
    str([call["threads"] for call in upload_calls]),
)
check(
    "uploads recursed into the staging directory",
    all(call["recursive"] for call in upload_calls),
)
check(
    "uploads relied on server side dedup (no force upload)",
    all(not call["forceUpload"] for call in upload_calls),
)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED: {failures}")
    sys.exit(1)
print("ALL E2E CHECKS PASSED")
