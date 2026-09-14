"""Google Photos Library API client (optional backend).

IMPORTANT SCOPE LIMITATION
--------------------------
Since 2025-03-31 the official Library API can only read or modify media items
and albums that were created by *the same OAuth client*. Assets uploaded through
gpmc use Google's internal mobile API, so they are invisible to this API and
their MediaKey is not a Library API `mediaItem.id`.

Therefore:
* ALBUM_BACKEND=gpmc / METADATA_BACKEND=embed (the defaults) do all album and
  metadata work through gpmc + embedded EXIF/XMP, and always work.
* The library_api backends here are useful when the container itself also
  uploads through this API, or for managing albums that this client created.
  They cannot retro-fit metadata onto gpmc uploads.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional

import requests

from .log import get_logger

log = get_logger("gphotos")

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://photoslibrary.googleapis.com/v1"
RETRY_STATUS = {429, 500, 502, 503, 504}
BATCH_LIMIT = 50


class GPhotosError(RuntimeError):
    pass


class GPhotosPermissionError(GPhotosError):
    """Raised when the API refuses access to media it did not create."""


class GooglePhotosClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        timeout: int = 60,
        retries: int = 4,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.timeout = timeout
        self.retries = max(1, retries)
        self.session = requests.Session()
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._album_cache: Dict[str, Dict[str, Any]] = {}

    # ---- auth ----
    def access_token(self, force: bool = False) -> str:
        if not force and self._token and time.time() < self._token_expiry - 60:
            return self._token
        response = self.session.post(
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=self.timeout,
        )
        if not response.ok:
            raise GPhotosError(
                f"token refresh failed (HTTP {response.status_code}): {response.text[:300]}"
            )
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expiry = time.time() + float(payload.get("expires_in", 3600))
        return self._token

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            headers = {
                "Authorization": f"Bearer {self.access_token()}",
                "Content-Type": "application/json",
            }
            headers.update(kwargs.pop("headers", {}))
            try:
                response = self.session.request(
                    method, url, headers=headers, timeout=self.timeout, **kwargs
                )
            except requests.RequestException as exc:
                last_error = exc
            else:
                if response.status_code == 401 and attempt < self.retries:
                    self.access_token(force=True)
                    last_error = GPhotosError("401 unauthorized; refreshed token")
                elif response.status_code == 403:
                    raise GPhotosPermissionError(
                        "Google Photos API returned 403. Since 2025-03-31 the Library API "
                        "can only touch media/albums created by this OAuth client, so items "
                        f"uploaded by gpmc cannot be edited here. Detail: {response.text[:200]}"
                    )
                elif response.status_code in RETRY_STATUS:
                    last_error = GPhotosError(f"{method} {path} -> HTTP {response.status_code}")
                elif not response.ok:
                    raise GPhotosError(
                        f"{method} {path} -> HTTP {response.status_code}: {response.text[:300]}"
                    )
                else:
                    return response.json() if response.content else {}
            if attempt < self.retries:
                delay = min(30.0, 2.0 ** (attempt - 1))
                log.warning("google photos %s %s failed (%s); retry in %.0fs", method, path, last_error, delay)
                time.sleep(delay)
        raise GPhotosError(f"{method} {path} failed after {self.retries} attempts: {last_error}")

    # ---- albums ----
    def list_albums(self) -> List[Dict[str, Any]]:
        albums: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"pageSize": 50, "excludeNonAppCreatedData": False}
            if page_token:
                params["pageToken"] = page_token
            payload = self._request("GET", "/albums", params=params)
            albums.extend(payload.get("albums") or [])
            page_token = payload.get("nextPageToken")
            if not page_token:
                return albums

    def find_album(self, title: str) -> Optional[Dict[str, Any]]:
        if not self._album_cache:
            for album in self.list_albums():
                name = album.get("title")
                if name:
                    self._album_cache[name] = album
        return self._album_cache.get(title)

    def create_album(self, title: str) -> Dict[str, Any]:
        album = self._request("POST", "/albums", json={"album": {"title": title}})
        if album.get("title"):
            self._album_cache[album["title"]] = album
        return album

    def ensure_album(self, title: str) -> Dict[str, Any]:
        return self.find_album(title) or self.create_album(title)

    def batch_add_media_items(self, album_id: str, media_item_ids: Iterable[str]) -> int:
        ids = [item for item in media_item_ids if item]
        added = 0
        for start in range(0, len(ids), BATCH_LIMIT):
            chunk = ids[start : start + BATCH_LIMIT]
            self._request(
                "POST",
                f"/albums/{album_id}:batchAddMediaItems",
                json={"mediaItemIds": chunk},
            )
            added += len(chunk)
        return added

    def patch_album_title(self, album_id: str, title: str) -> Dict[str, Any]:
        return self._request(
            "PATCH",
            f"/albums/{album_id}",
            params={"updateMask": "title"},
            json={"title": title},
        )

    # ---- media items ----
    def get_media_item(self, media_item_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/mediaItems/{media_item_id}")

    def patch_description(self, media_item_id: str, description: str) -> Dict[str, Any]:
        return self._request(
            "PATCH",
            f"/mediaItems/{media_item_id}",
            params={"updateMask": "description"},
            json={"description": description[:1000]},
        )

    def iter_app_created_items(self, page_size: int = 100) -> Iterable[Dict[str, Any]]:
        page_token: Optional[str] = None
        while True:
            body: Dict[str, Any] = {
                "pageSize": page_size,
                "filters": {"featureFilter": {"includedFeatures": ["NONE"]}},
            }
            if page_token:
                body["pageToken"] = page_token
            payload = self._request("POST", "/mediaItems:search", json=body)
            for item in payload.get("mediaItems") or []:
                yield item
            page_token = payload.get("nextPageToken")
            if not page_token:
                return

    def index_by_filename(self) -> Dict[str, str]:
        """filename -> mediaItem.id for items this OAuth client can see."""
        index: Dict[str, str] = {}
        for item in self.iter_app_created_items():
            name = item.get("filename")
            if name and name not in index:
                index[name] = item["id"]
        return index

    def check(self) -> Dict[str, Any]:
        self.access_token(force=True)
        albums = self.list_albums()
        return {"ok": True, "visible_albums": len(albums)}
