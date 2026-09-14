"""Tiny mock Immich server used by the offline end-to-end test."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# 1x1 JPEG so exiftool has something real to write into
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPDIzM//bAEMBCQkJDAsMGA0NGDIhHCEyMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy/8AAEQgAAQABAwEiAAIR"
    "AQMRAf/EABUAAQEAAAAAAAAAAAAAAAAAAAAH/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/EABQBAQEA"
    "AAAAAAAAAAAAAAAAAAAH/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAwDAQACEQMRAD8AlgAH/9k="
)

ASSETS = {
    "aaaa1111-0000-4000-8000-000000000001": {
        "id": "aaaa1111-0000-4000-8000-000000000001",
        "type": "IMAGE",
        "originalFileName": "IMG_0001.jpg",
        "originalPath": "upload/library/admin/2024/IMG_0001.jpg",
        "originalMimeType": "image/jpeg",
        "deviceAssetId": "IMG_0001.jpg-12345",
        "deviceId": "pixel",
        "ownerId": "owner-1",
        "checksum": "c2hhMS1jaGVja3N1bS0x",
        "fileCreatedAt": "2024-05-01T10:00:00.000Z",
        "fileModifiedAt": "2024-05-01T10:00:00.000Z",
        "localDateTime": "2024-05-01T19:00:00.000Z",
        "updatedAt": "2026-09-01T00:00:00.000Z",
        "isFavorite": True,
        "isArchived": False,
        "description": "Sakura at Ueno \u4e0a\u91ce\u306e\u685c",
        "tags": [{"id": "t1", "value": "travel"}, {"id": "t2", "value": "spring"}],
        "people": [{"id": "p1", "name": "Kaichi"}],
        "exifInfo": {
            "make": "Google",
            "model": "Pixel 8",
            "dateTimeOriginal": "2024-05-01T10:00:00.000Z",
            "latitude": 35.7156,
            "longitude": 139.7745,
            "city": "Tokyo",
            "state": "Tokyo",
            "country": "Japan",
            "description": "Sakura at Ueno",
            "fileSizeInByte": 1234,
        },
    },
    "aaaa1111-0000-4000-8000-000000000002": {
        "id": "aaaa1111-0000-4000-8000-000000000002",
        "type": "IMAGE",
        "originalFileName": "IMG_0002.jpg",
        "originalPath": "upload/library/admin/2024/IMG_0002.jpg",
        "originalMimeType": "image/jpeg",
        "checksum": "c2hhMS1jaGVja3N1bS0y",
        "fileCreatedAt": "2024-06-02T02:30:00.000Z",
        "fileModifiedAt": "2024-06-02T02:30:00.000Z",
        "localDateTime": "2024-06-02T11:30:00.000Z",
        "updatedAt": "2026-09-02T00:00:00.000Z",
        "isFavorite": False,
        "isArchived": False,
        "description": "",
        "tags": [],
        "people": [],
        "exifInfo": {"dateTimeOriginal": "2024-06-02T02:30:00.000Z"},
    },
    "aaaa1111-0000-4000-8000-000000000003": {
        "id": "aaaa1111-0000-4000-8000-000000000003",
        "type": "VIDEO",
        "originalFileName": "VID_0003.mp4",
        "originalPath": "upload/library/admin/2024/VID_0003.mp4",
        "originalMimeType": "video/mp4",
        "checksum": "c2hhMS1jaGVja3N1bS0z",
        "fileCreatedAt": "2024-07-03T12:00:00.000Z",
        "fileModifiedAt": "2024-07-03T12:00:00.000Z",
        "localDateTime": "2024-07-03T21:00:00.000Z",
        "updatedAt": "2026-09-03T00:00:00.000Z",
        "duration": "00:00:12.500",
        "isFavorite": False,
        "isArchived": True,
        "description": "Shinkansen window",
        "tags": [{"id": "t3", "value": "train"}],
        "people": [],
        "exifInfo": {"dateTimeOriginal": "2024-07-03T12:00:00.000Z"},
    },
}

# the sidecar validates local originals against these, so advertise the real
# checksum and size of the bytes this mock serves
CHECKSUM = base64.b64encode(hashlib.sha1(JPEG).digest()).decode("ascii")
for _asset in ASSETS.values():
    _asset["checksum"] = CHECKSUM
    _asset.setdefault("exifInfo", {})["fileSizeInByte"] = len(JPEG)

ALBUMS = {
    "bbbb2222-0000-4000-8000-00000000000a": {
        "id": "bbbb2222-0000-4000-8000-00000000000a",
        "albumName": "2024 \u65c5\u884c",
        "shared": False,
        "updatedAt": "2026-09-05T00:00:00.000Z",
        "assetIds": [
            "aaaa1111-0000-4000-8000-000000000001",
            "aaaa1111-0000-4000-8000-000000000002",
        ],
    },
    "bbbb2222-0000-4000-8000-00000000000b": {
        "id": "bbbb2222-0000-4000-8000-00000000000b",
        "albumName": "Trains",
        "shared": False,
        "updatedAt": "2026-09-06T00:00:00.000Z",
        "assetIds": ["aaaa1111-0000-4000-8000-000000000003"],
    },
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # keep test output readable
        print("mock-immich %s" % (fmt % args), flush=True)

    # -- helpers --
    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, blob: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _auth_ok(self) -> bool:
        if self.headers.get("x-api-key"):
            return True
        self._send_json({"message": "missing api key"}, 401)
        return False

    @staticmethod
    def _album_payload(album, with_assets: bool = True):
        payload = {
            "id": album["id"],
            "albumName": album["albumName"],
            "shared": album["shared"],
            "updatedAt": album["updatedAt"],
            "assetCount": len(album["assetIds"]),
        }
        if with_assets:
            payload["assets"] = [ASSETS[aid] for aid in album["assetIds"] if aid in ASSETS]
        return payload

    # -- routes --
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._auth_ok():
            return
        if path == "/api/server/ping":
            return self._send_json({"res": "pong"})
        if path == "/api/server/about":
            return self._send_json({"version": "v1.140.0-mock"})
        if path == "/api/albums":
            return self._send_json(
                [self._album_payload(album, False) for album in ALBUMS.values()]
            )
        match = re.fullmatch(r"/api/albums/([\w-]+)", path)
        if match:
            album = ALBUMS.get(match.group(1))
            if not album:
                return self._send_json({"message": "not found"}, 404)
            return self._send_json(self._album_payload(album))
        match = re.fullmatch(r"/api/assets/([\w-]+)/original", path)
        if match:
            asset = ASSETS.get(match.group(1))
            if not asset:
                return self._send_json({"message": "not found"}, 404)
            return self._send_bytes(JPEG, asset["originalMimeType"])
        match = re.fullmatch(r"/api/assets/([\w-]+)", path)
        if match:
            asset = ASSETS.get(match.group(1))
            if not asset:
                return self._send_json({"message": "not found"}, 404)
            return self._send_json(asset)
        if path == "/api/tags":
            return self._send_json([{"id": "t1", "value": "travel"}, {"id": "t3", "value": "train"}])
        if path == "/api/people":
            return self._send_json({"people": [{"id": "p1", "name": "Kaichi"}], "total": 1})
        return self._send_json({"message": f"no route for {path}"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._auth_ok():
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}
        if path == "/api/search/metadata":
            page = int(body.get("page") or 1)
            size = max(1, int(body.get("size") or 250))
            updated_after = body.get("updatedAfter")
            wanted_type = body.get("type")
            items = list(ASSETS.values())
            if updated_after:
                items = [a for a in items if a["updatedAt"] > updated_after]
            if wanted_type:
                items = [a for a in items if a["type"] == wanted_type]
            if body.get("isArchived") is False:
                items = [a for a in items if not a.get("isArchived")]
            start = (page - 1) * size
            chunk = items[start : start + size]
            next_page = str(page + 1) if start + size < len(items) else None
            return self._send_json({"assets": {"items": chunk, "nextPage": next_page, "total": len(items)}})
        return self._send_json({"message": f"no route for {path}"}, 404)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"mock immich listening on http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
