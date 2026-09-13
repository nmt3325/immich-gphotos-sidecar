"""Minimal Immich REST client (API-key auth).

Only the endpoints a backup needs are implemented:
  POST /api/search/metadata       -> enumerate assets (paged, supports updatedAfter)
  GET  /api/assets/{id}           -> full asset metadata (exif, tags, people, stack)
  GET  /api/assets/{id}/original  -> original bytes
  GET  /api/albums                -> albums
  GET  /api/albums/{id}           -> album with asset membership
  GET  /api/tags, /api/people     -> library-level metadata for the snapshot
"""

from __future__ import annotations

import base64
import hashlib
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import requests

from .log import get_logger

log = get_logger("immich")

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class ImmichError(RuntimeError):
    pass


class ImmichClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 60, retries: int = 4) -> None:
        base = base_url.rstrip("/")
        self.api = base if base.endswith("/api") else f"{base}/api"
        self.timeout = timeout
        self.retries = max(1, retries)
        self.session = requests.Session()
        self.session.headers.update(
            {"x-api-key": api_key, "Accept": "application/json", "User-Agent": "immich-gphotos-sidecar/1.0"}
        )

    # ---- plumbing ----
    def _request(self, method: str, path: str, *, stream: bool = False, **kwargs: Any) -> requests.Response:
        url = f"{self.api}{path}"
        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.request(method, url, timeout=self.timeout, stream=stream, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
            else:
                if response.status_code in RETRY_STATUS:
                    last_error = ImmichError(f"{method} {path} -> HTTP {response.status_code}")
                    response.close()
                elif not response.ok:
                    detail = response.text[:300].replace("\n", " ")
                    response.close()
                    raise ImmichError(f"{method} {path} -> HTTP {response.status_code}: {detail}")
                else:
                    return response
            if attempt < self.retries:
                delay = min(30.0, 2.0 ** (attempt - 1))
                log.warning("immich %s %s failed (%s); retrying in %.0fs", method, path, last_error, delay)
                time.sleep(delay)
        raise ImmichError(f"{method} {path} failed after {self.retries} attempts: {last_error}")

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._request(method, path, **kwargs)
        try:
            return response.json()
        finally:
            response.close()

    # ---- server ----
    def ping(self) -> bool:
        return bool(self._json("GET", "/server/ping"))

    def about(self) -> Dict[str, Any]:
        try:
            return self._json("GET", "/server/about")
        except ImmichError:
            return {}

    # ---- assets ----
    def iter_assets(
        self,
        updated_after: Optional[str] = None,
        page_size: int = 250,
        asset_types: Optional[List[str]] = None,
        include_archived: bool = True,
        with_exif: bool = True,
        with_people: bool = True,
    ) -> Iterator[Dict[str, Any]]:
        page = 1
        seen = 0
        while True:
            body: Dict[str, Any] = {
                "page": page,
                "size": page_size,
                "withExif": with_exif,
                "withPeople": with_people,
                "withDeleted": False,
            }
            if updated_after:
                body["updatedAfter"] = updated_after
            if asset_types and len(asset_types) == 1:
                body["type"] = asset_types[0]
            if not include_archived:
                body["isArchived"] = False
            payload = self._json("POST", "/search/metadata", json=body)
            bucket = payload.get("assets") or {}
            items = bucket.get("items") or []
            for item in items:
                seen += 1
                yield item
            next_page = bucket.get("nextPage")
            if not items or not next_page:
                log.debug("asset enumeration finished after %s items", seen)
                return
            try:
                page = int(next_page)
            except (TypeError, ValueError):
                page += 1

    def get_asset(self, asset_id: str) -> Dict[str, Any]:
        return self._json("GET", f"/assets/{asset_id}")

    def download_original(self, asset_id: str, destination: Path, retries: int = 3) -> int:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_suffix(destination.suffix + ".part")
        last_error: Optional[Exception] = None
        for attempt in range(1, max(1, retries) + 1):
            try:
                response = self._request("GET", f"/assets/{asset_id}/original", stream=True)
                written = 0
                with open(temp, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 512):
                        if chunk:
                            handle.write(chunk)
                            written += len(chunk)
                response.close()
                if written == 0:
                    raise ImmichError("downloaded 0 bytes")
                temp.replace(destination)
                return written
            except Exception as exc:  # noqa: BLE001 - retry on anything transport related
                last_error = exc
                temp.unlink(missing_ok=True)
                if attempt < retries:
                    time.sleep(min(20.0, 2.0 ** (attempt - 1)))
        raise ImmichError(f"download of {asset_id} failed: {last_error}")

    # ---- albums / library ----
    def list_albums(self, shared: Optional[bool] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if shared is not None:
            params["shared"] = str(shared).lower()
        return self._json("GET", "/albums", params=params) or []

    def get_album(self, album_id: str, without_assets: bool = False) -> Dict[str, Any]:
        params = {"withoutAssets": str(without_assets).lower()}
        return self._json("GET", f"/albums/{album_id}", params=params)

    def list_tags(self) -> List[Dict[str, Any]]:
        try:
            return self._json("GET", "/tags") or []
        except ImmichError:
            return []

    def list_people(self) -> Dict[str, Any]:
        try:
            return self._json("GET", "/people", params={"withHidden": "false"}) or {}
        except ImmichError:
            return {}


def immich_checksum(path: Path) -> str:
    """Immich exposes the asset checksum as base64(sha1(file))."""
    digest = hashlib.sha1()  # noqa: S324 - matching Immich's own algorithm
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")
