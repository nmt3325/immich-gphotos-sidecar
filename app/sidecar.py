"""Sidecar metadata: JSON + XMP export and (optional) EXIF/XMP embedding.

Google Photos only stores pictures and videos, so Immich metadata is preserved
in two independent ways:

1. Sidecar files written to SIDECAR_DIR (`<assetId>.json` + `<assetId>.xmp`).
   These are the authoritative backup of filenames, paths, checksums, tags,
   people, album membership and EXIF as Immich saw them.
2. Embedded EXIF/XMP inside the copy that gets uploaded, so Google Photos itself
   shows the right capture date, location, description and keywords.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape

from .log import get_logger

log = get_logger("sidecar")

SCHEMA_VERSION = 1
_UNSAFE = re.compile(r"[^\w.\-+@() \u3000-\u9fff\uac00-\ud7af]", re.UNICODE)


def safe_component(name: str, fallback: str = "unnamed", limit: int = 120) -> str:
    """Filesystem-safe single path component (keeps CJK, drops separators)."""
    cleaned = _UNSAFE.sub("_", (name or "").strip())
    cleaned = cleaned.strip(". ") or fallback
    return cleaned[:limit]


def _exif(asset: Dict[str, Any]) -> Dict[str, Any]:
    return asset.get("exifInfo") or {}


def asset_description(asset: Dict[str, Any]) -> str:
    return (asset.get("description") or _exif(asset).get("description") or "").strip()


def asset_tags(asset: Dict[str, Any]) -> List[str]:
    tags = []
    for tag in asset.get("tags") or []:
        value = tag.get("value") or tag.get("name")
        if value:
            tags.append(value)
    return tags


def asset_people(asset: Dict[str, Any]) -> List[str]:
    people = []
    for person in asset.get("people") or []:
        name = (person.get("name") or "").strip()
        if name:
            people.append(name)
    return people


def capture_datetime(asset: Dict[str, Any]) -> Optional[datetime]:
    """Best-effort original capture timestamp."""
    candidates = [
        _exif(asset).get("dateTimeOriginal"),
        asset.get("localDateTime"),
        asset.get("fileCreatedAt"),
        asset.get("fileModifiedAt"),
    ]
    for raw in candidates:
        if not raw:
            continue
        text = str(raw).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def build_sidecar(asset: Dict[str, Any], album_names: List[str]) -> Dict[str, Any]:
    exif = _exif(asset)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "assetId": asset.get("id"),
        "deviceAssetId": asset.get("deviceAssetId"),
        "deviceId": asset.get("deviceId"),
        "ownerId": asset.get("ownerId"),
        "libraryId": asset.get("libraryId"),
        "type": asset.get("type"),
        "originalFileName": asset.get("originalFileName"),
        "originalPath": asset.get("originalPath"),
        "originalMimeType": asset.get("originalMimeType"),
        "checksum": asset.get("checksum"),
        "fileCreatedAt": asset.get("fileCreatedAt"),
        "fileModifiedAt": asset.get("fileModifiedAt"),
        "localDateTime": asset.get("localDateTime"),
        "updatedAt": asset.get("updatedAt"),
        "isFavorite": asset.get("isFavorite"),
        "isArchived": asset.get("isArchived"),
        "isOffline": asset.get("isOffline"),
        "visibility": asset.get("visibility"),
        "duration": asset.get("duration"),
        "livePhotoVideoId": asset.get("livePhotoVideoId"),
        "description": asset_description(asset),
        "rating": exif.get("rating"),
        "tags": asset_tags(asset),
        "people": asset_people(asset),
        "albums": album_names,
        "stack": asset.get("stack"),
        "exifInfo": exif,
    }


def sidecar_hash(payload: Dict[str, Any]) -> str:
    volatile = {"exportedAt"}
    stable = {key: value for key, value in payload.items() if key not in volatile}
    blob = json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _xmp_value(value: Any) -> str:
    return escape("" if value is None else str(value))


def build_xmp(payload: Dict[str, Any]) -> str:
    keywords = list(dict.fromkeys([*payload.get("tags", []), *payload.get("people", [])]))
    keyword_items = "".join(f"<rdf:li>{_xmp_value(word)}</rdf:li>" for word in keywords)
    album_items = "".join(f"<rdf:li>{_xmp_value(album)}</rdf:li>" for album in payload.get("albums", []))
    exif = payload.get("exifInfo") or {}
    latitude = exif.get("latitude")
    longitude = exif.get("longitude")
    gps = ""
    if latitude is not None and longitude is not None:
        gps = (
            f'\n      exif:GPSLatitude="{_xmp_value(latitude)}"'
            f'\n      exif:GPSLongitude="{_xmp_value(longitude)}"'
        )
    return f"""<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="immich-gphotos-sidecar">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:exif="http://ns.adobe.com/exif/1.0/"
    xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"
    xmlns:immich="https://immich.app/ns/1.0/"
      xmp:CreateDate="{_xmp_value(payload.get('localDateTime') or payload.get('fileCreatedAt'))}"
      xmp:Rating="{5 if payload.get('isFavorite') else (payload.get('rating') or 0)}"
      photoshop:DateCreated="{_xmp_value(payload.get('localDateTime') or payload.get('fileCreatedAt'))}"
      immich:AssetId="{_xmp_value(payload.get('assetId'))}"
      immich:OriginalFileName="{_xmp_value(payload.get('originalFileName'))}"
      immich:OriginalPath="{_xmp_value(payload.get('originalPath'))}"
      immich:Checksum="{_xmp_value(payload.get('checksum'))}"
      immich:IsArchived="{_xmp_value(payload.get('isArchived'))}"
      immich:IsFavorite="{_xmp_value(payload.get('isFavorite'))}"{gps}>
   <dc:title><rdf:Alt><rdf:li xml:lang="x-default">{_xmp_value(payload.get('originalFileName'))}</rdf:li></rdf:Alt></dc:title>
   <dc:description><rdf:Alt><rdf:li xml:lang="x-default">{_xmp_value(payload.get('description'))}</rdf:li></rdf:Alt></dc:description>
   <dc:subject><rdf:Bag>{keyword_items}</rdf:Bag></dc:subject>
   <immich:Albums><rdf:Bag>{album_items}</rdf:Bag></immich:Albums>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""


def write_sidecar_files(
    payload: Dict[str, Any],
    directory: Path,
    write_json: bool = True,
    write_xmp: bool = True,
) -> Dict[str, str]:
    """Write `<assetId>.json` / `<assetId>.xmp` into a sharded directory."""
    asset_id = str(payload.get("assetId") or "unknown")
    shard = directory / asset_id[:2]
    shard.mkdir(parents=True, exist_ok=True)
    written: Dict[str, str] = {}
    if write_json:
        target = shard / f"{asset_id}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
        written["json"] = str(target)
    if write_xmp:
        target = shard / f"{asset_id}.xmp"
        tmp = target.with_suffix(".xmp.tmp")
        tmp.write_text(build_xmp(payload), encoding="utf-8")
        tmp.replace(target)
        written["xmp"] = str(target)
    return written


def _exif_timestamp(value: datetime) -> str:
    return value.strftime("%Y:%m:%d %H:%M:%S")


def embed_metadata(
    file_path: Path,
    payload: Dict[str, Any],
    exiftool_bin: str = "exiftool",
    timeout: int = 180,
) -> bool:
    """Embed Immich metadata into the upload copy. Non-fatal on failure."""
    exif = payload.get("exifInfo") or {}
    is_video = (payload.get("type") or "").upper() == "VIDEO"
    args: List[str] = [
        exiftool_bin,
        "-m",
        "-q",
        "-q",
        "-overwrite_original",
        "-charset",
        "filename=utf8",
        "-api",
        "QuickTimeUTC=1",
    ]

    captured = capture_datetime(payload)
    if captured:
        stamp = _exif_timestamp(captured)
        offset = captured.strftime("%z")
        if is_video:
            args += [
                f"-QuickTime:CreateDate={stamp}",
                f"-QuickTime:ModifyDate={stamp}",
                f"-Keys:CreationDate={stamp}{offset}",
                f"-XMP:CreateDate={stamp}",
            ]
        else:
            args += [
                f"-EXIF:DateTimeOriginal={stamp}",
                f"-EXIF:CreateDate={stamp}",
                f"-XMP:CreateDate={stamp}",
                f"-XMP:DateTimeOriginal={stamp}",
            ]
            if offset:
                pretty = f"{offset[:3]}:{offset[3:]}"
                args += [f"-EXIF:OffsetTimeOriginal={pretty}", f"-EXIF:OffsetTime={pretty}"]

    description = payload.get("description") or ""
    if description:
        args += [f"-XMP-dc:Description={description}"]
        if not is_video:
            args += [f"-EXIF:ImageDescription={description}", f"-IPTC:Caption-Abstract={description}"]

    original_name = payload.get("originalFileName")
    if original_name:
        args += [f"-XMP-dc:Title={original_name}", f"-XMP-xmpMM:PreservedFileName={original_name}"]

    asset_id = payload.get("assetId")
    if asset_id:
        args += [f"-XMP-dc:Identifier=immich:{asset_id}"]

    for keyword in dict.fromkeys([*payload.get("tags", []), *payload.get("people", [])]):
        args += [f"-XMP-dc:Subject+={keyword}"]
        if not is_video:
            args += [f"-IPTC:Keywords+={keyword}"]

    for album in payload.get("albums", []):
        args += [f"-XMP-dc:Subject+=album:{album}"]

    rating = 5 if payload.get("isFavorite") else exif.get("rating")
    if rating:
        args += [f"-XMP:Rating={int(rating)}"]

    latitude, longitude = exif.get("latitude"), exif.get("longitude")
    if latitude is not None and longitude is not None:
        args += [
            f"-GPSLatitude={abs(float(latitude))}",
            f"-GPSLatitudeRef={'N' if float(latitude) >= 0 else 'S'}",
            f"-GPSLongitude={abs(float(longitude))}",
            f"-GPSLongitudeRef={'E' if float(longitude) >= 0 else 'W'}",
        ]
        if not is_video:
            args += ["-GPSMapDatum=WGS-84"]
    for field, key in (("-XMP-photoshop:City", "city"), ("-XMP-photoshop:State", "state"), ("-XMP-photoshop:Country", "country")):
        if exif.get(key):
            args += [f"{field}={exif[key]}"]

    args.append(str(file_path))
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        log.warning("exiftool not found (%s); skipping metadata embedding", exiftool_bin)
        return False
    except subprocess.TimeoutExpired:
        log.warning("exiftool timed out for %s", file_path.name)
        return False
    if result.returncode != 0:
        log.warning(
            "exiftool failed for %s (rc=%s): %s",
            file_path.name,
            result.returncode,
            (result.stderr or result.stdout or "").strip()[:300],
        )
        return False
    return True


def apply_file_times(file_path: Path, payload: Dict[str, Any]) -> None:
    """gotohp falls back to the file mtime for the media date, so align it."""
    captured = capture_datetime(payload)
    if not captured:
        return
    stamp = captured.timestamp()
    try:
        os.utime(file_path, (stamp, stamp))
    except OSError as exc:
        log.debug("could not set mtime on %s: %s", file_path, exc)
