"""Fake `gpmc` package for the offline end-to-end test.

`tests/fake_gpmc` is put first on PYTHONPATH, so `import gpmc` resolves here
instead of the real library and the sidecar can be exercised without talking to
Google. Every call is appended to a JSON store (`FAKE_GPMC_STORE`) so the
assertions can check what the uploader actually did.

The surface mirrors the parts of `gpmc.Client` the sidecar uses:
`upload()`, `add_to_album()`, `add_to_existing_album()`, `update_cache()`,
`get_media_key_by_hash()` and `api.bearer_token`.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

__version__ = "0.0.0-fake"

STORE = Path(os.environ.get("FAKE_GPMC_STORE", "/tmp/fake-gpmc-store.json"))
MEDIA_MIME_PREFIXES = ("image/", "video/")


def _load() -> Dict[str, Any]:
    if STORE.exists():
        try:
            return json.loads(STORE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"media": {}, "albums": {}, "album_names": {}, "calls": []}


def _save(store: Dict[str, Any]) -> None:
    STORE.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


def _record(entry: Dict[str, Any]) -> None:
    store = _load()
    store["calls"].append(entry)
    _save(store)


def _media_key(path: Path) -> str:
    digest = hashlib.sha1(path.read_bytes() + path.name.encode()).hexdigest()  # noqa: S324
    return "AF1Qip" + digest[:26]


def _album_key(album_name: str) -> str:
    return "AF1Qip" + hashlib.sha1(("album:" + album_name).encode()).hexdigest()[:26]  # noqa: S324


class _Api:
    """Stand-in for `gpmc.api.Api` (only what the sidecar touches)."""

    def __init__(self, auth_data: str, timeout: int) -> None:
        self.auth_data = auth_data
        self.timeout = timeout

    @property
    def bearer_token(self) -> str:
        if os.environ.get("FAKE_GPMC_AUTH_FAILS"):
            raise RuntimeError("fake auth failure")
        if not self.auth_data:
            raise RuntimeError("no auth data")
        return "fake-bearer-token"


class Client:
    def __init__(
        self,
        auth_data: str = "",
        proxy: str = "",
        language: str = "",
        timeout: int = 60,
        log_level: str = "INFO",
    ) -> None:
        resolved = auth_data or os.environ.get("GP_AUTH_DATA", "")
        if not resolved:
            raise ValueError(
                "`GP_AUTH_DATA` environment variable not set. "
                "Create it or provide `auth_data` as an argument."
            )
        self.auth_data = resolved
        self.log_level = log_level
        self.api = _Api(resolved, timeout)
        self.cache_dir = Path.home() / ".gpmc" / self._email(resolved)
        self.db_path = self.cache_dir / "storage.db"
        _record(
            {
                "call": "init",
                "logLevel": log_level,
                "timeout": timeout,
                "proxy": proxy,
                "language": language,
            }
        )

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _email(auth_data: str) -> str:
        for part in auth_data.split("&"):
            key, _, value = part.partition("=")
            if key.strip().lower() == "email" and value:
                return value.replace("%40", "@")
        return "tester@example.com"

    @staticmethod
    def _is_media(path: Path) -> bool:
        guess, _ = mimetypes.guess_type(path.name)
        return bool(guess and guess.startswith(MEDIA_MIME_PREFIXES))

    def _media_files(self, target: Path, recursive: bool) -> List[Path]:
        if target.is_file():
            return [target] if self._is_media(target) else []
        if not target.is_dir():
            return []
        candidates = target.rglob("*") if recursive else target.iterdir()
        return sorted(p for p in candidates if p.is_file() and self._is_media(p))

    # ------------------------------------------------------------------ upload
    def upload(
        self,
        target: Any,
        album_name: Optional[str] = None,
        album_id: Optional[str] = None,
        use_quota: bool = False,
        saver: bool = False,
        recursive: bool = False,
        show_progress: bool = False,
        threads: int = 1,
        force_upload: bool = False,
        delete_from_host: bool = False,
        filter_exp: str = "",
        filter_exclude: bool = False,
        filter_regex: bool = False,
        filter_ignore_case: bool = False,
        filter_path: bool = False,
        skip_existing_filenames: bool = False,
        progress_callback: Any = None,
    ) -> Dict[str, str]:
        if album_name and album_id:
            raise ValueError("`album_name` and `album_id` are mutually exclusive.")
        paths = self._media_files(Path(target), recursive)
        if not paths:
            raise ValueError("No valid media files found to upload.")

        store = _load()
        results: Dict[str, str] = {}
        for path in paths:
            key = _media_key(path)
            duplicate = key in store["media"]
            store["media"][key] = {"name": path.name, "duplicate": duplicate}
            results[path.absolute().as_posix()] = key
        store["calls"].append(
            {
                "call": "upload",
                "target": str(target),
                "recursive": recursive,
                "threads": threads,
                "forceUpload": force_upload,
                "useQuota": use_quota,
                "saver": saver,
                "skipExistingFilenames": skip_existing_filenames,
                "albumName": album_name,
                "albumId": album_id,
                "files": [path.name for path in paths],
            }
        )
        _save(store)

        if album_id:
            self.add_to_existing_album(list(results.values()), album_id, show_progress)
        elif album_name:
            self.add_to_album(list(results.values()), album_name, show_progress)
        return results

    # ------------------------------------------------------------------ albums
    def add_to_album(
        self, media_keys: Sequence[str], album_name: str, show_progress: bool = False
    ) -> List[str]:
        key = _album_key(album_name)
        store = _load()
        store["album_names"][key] = album_name
        members = store["albums"].setdefault(key, [])
        added = 0
        for media_key in media_keys:
            if media_key not in members:
                members.append(media_key)
                added += 1
        store["calls"].append(
            {
                "call": "add_to_album",
                "album": album_name,
                "albumKey": key,
                "items": len(media_keys),
                "added": added,
            }
        )
        _save(store)
        return [key]

    def add_to_existing_album(
        self, media_keys: Sequence[str], album_id: str, show_progress: bool = False
    ) -> str:
        store = _load()
        if album_id not in store["albums"]:
            store["calls"].append(
                {
                    "call": "add_to_existing_album",
                    "albumKey": album_id,
                    "items": len(media_keys),
                    "error": "unknown album",
                }
            )
            _save(store)
            raise RuntimeError(f"album {album_id} does not exist")
        members = store["albums"][album_id]
        added = 0
        for media_key in media_keys:
            if media_key not in members:
                members.append(media_key)
                added += 1
        store["calls"].append(
            {
                "call": "add_to_existing_album",
                "albumKey": album_id,
                "items": len(media_keys),
                "added": added,
            }
        )
        _save(store)
        return album_id

    # ------------------------------------------------------------------- misc
    def get_media_key_by_hash(self, sha1_hash: Any) -> Optional[str]:
        _record({"call": "get_media_key_by_hash"})
        return None

    def update_cache(self, show_progress: bool = True, max_sync_cycles: int = 10) -> None:
        _record({"call": "update_cache"})


ProgressCallback = Any
UploadOptions = dict
UploadProgressEvent = dict

ALL = [Client, ProgressCallback, UploadOptions, UploadProgressEvent]
