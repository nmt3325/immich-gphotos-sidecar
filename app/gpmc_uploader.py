"""Google Photos uploads through the gpmc library (https://github.com/xob0t/gpmc).

gpmc talks to the same reverse engineered Google Photos mobile API that the
gotohp CLI used, but it is a Python library, so uploads now happen in-process:
no child process, no pty, no ANSI/TUI scraping and no JSON summary parsing.

Two things are handled here instead of inside ``gpmc.Client.upload()``:

* **Albums.** ``Client.upload(album_name=...)`` always *creates* a new album, so
  repeated runs would pile up albums with the same name. This wrapper uploads
  without album arguments and attaches the media keys afterwards: to the album
  media key remembered in the state DB when there is one (``album_id``),
  otherwise by creating the album once and handing the key back so the caller
  can store it for the next run.
* **Per-file errors.** gpmc logs failures and simply omits them from its result
  mapping, so the staged files are enumerated up front and everything that is
  missing from the result is reported back as an error.

Uploads stay hash-deduplicated server side: already present files return their
existing media key, which is what makes album backfill work for assets that were
uploaded by an earlier run.
"""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import unquote

from .log import get_logger

log = get_logger("gpmc")

MEDIA_MIME_PREFIXES = ("image/", "video/")
NO_MEDIA_KEY = "gpmc returned no media key (see the gpmc log / failed_skipped_files.log)"


class UploadError(RuntimeError):
    """Raised when a whole upload batch could not be handed to Google Photos."""


def parse_account(auth_data: str) -> str:
    """Best effort account e-mail out of a gpmc auth data string."""
    for part in (auth_data or "").split("&"):
        key, _, value = part.partition("=")
        if key.strip().lower() == "email" and value:
            return unquote(value.strip())
    return ""


@dataclass
class UploadOutcome:
    """Result of one :meth:`GpmcUploader.upload_path` call."""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    album_name: Optional[str] = None
    album_keys: List[str] = field(default_factory=list)
    items_added: int = 0
    album_error: Optional[str] = None
    media_keys: Dict[str, str] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)


class GpmcUploader:
    """Thin, dependency-injectable wrapper around :class:`gpmc.Client`."""

    def __init__(
        self,
        auth_data: str = "",
        threads: int = 3,
        timeout: int = 60,
        proxy: str = "",
        language: str = "",
        log_level: str = "ERROR",
        use_quota: bool = False,
        saver: bool = False,
        force_upload: bool = False,
        skip_existing_filenames: bool = False,
        show_progress: bool = False,
        cache_dir: str = "",
    ) -> None:
        self.auth_data = (auth_data or "").strip()
        self.threads = max(1, int(threads or 1))
        self.timeout = max(1, int(timeout or 60))
        self.proxy = proxy or ""
        self.language = language or ""
        self.log_level = (log_level or "ERROR").upper()
        self.use_quota = bool(use_quota)
        self.saver = bool(saver)
        self.force_upload = bool(force_upload)
        self.skip_existing_filenames = bool(skip_existing_filenames)
        self.show_progress = bool(show_progress)
        self.cache_dir = cache_dir or ""
        self._client: Any = None

    # ------------------------------------------------------------------ client
    def client(self) -> Any:
        """Build the gpmc client once; raise :class:`UploadError` when unusable.

        Constructing the client does not talk to Google yet; it only parses the
        auth data. Building it also registers gpmc's extra RAW mime types, which
        is why it happens before files are enumerated.
        """
        if self._client is not None:
            return self._client
        try:
            from gpmc import Client  # imported lazily so `doctor` can report it
        except ImportError as exc:  # pragma: no cover - packaging problem
            raise UploadError(f"the gpmc package is not installed: {exc}") from exc

        kwargs: Dict[str, Any] = {"timeout": self.timeout, "log_level": self.log_level}
        if self.auth_data:
            kwargs["auth_data"] = self.auth_data
        if self.proxy:
            kwargs["proxy"] = self.proxy
        if self.language:
            kwargs["language"] = self.language
        try:
            client = Client(**kwargs)
        except ValueError as exc:
            raise UploadError(
                f"google photos credentials are missing or malformed: {exc} "
                "(set GPMC_AUTH_DATA)"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - surface as a clean error
            raise UploadError(f"could not initialise gpmc: {exc}") from exc
        self._relocate_cache(client)
        self._client = client
        return client

    def _relocate_cache(self, client: Any) -> None:
        """Keep gpmc's sqlite cache on a persistent volume instead of ``$HOME``."""
        if not self.cache_dir:
            return
        current = getattr(client, "cache_dir", None)
        if current is None:
            return
        try:
            target = Path(self.cache_dir) / ".gpmc" / Path(str(current)).name
            target.mkdir(parents=True, exist_ok=True)
            client.cache_dir = target
            client.db_path = target / "storage.db"
        except OSError as exc:
            log.warning("could not use %s for the gpmc cache: %s", self.cache_dir, exc)

    # ------------------------------------------------------------------- infos
    @staticmethod
    def version() -> str:
        """Version of the installed gpmc package."""
        try:
            import gpmc
        except ImportError as exc:
            raise UploadError(f"the gpmc package is not installed: {exc}") from exc
        declared = getattr(gpmc, "__version__", "")
        if declared:
            return str(declared)
        try:
            from importlib.metadata import version as package_version

            return package_version("gpmc")
        except Exception:  # noqa: BLE001 - version is informational only
            return "unknown"

    def account(self) -> str:
        """Google account the auth data belongs to (empty when unknown)."""
        client = self._client
        if client is not None:
            name = Path(str(getattr(client, "cache_dir", "") or "")).name
            if "@" in name:
                return name
        return parse_account(self.auth_data)

    def ensure_credentials(self) -> List[str]:
        """Verify the auth data by fetching a bearer token. Returns accounts."""
        client = self.client()
        try:
            token = client.api.bearer_token
        except Exception as exc:  # noqa: BLE001 - network/credential problems
            raise UploadError(f"google photos authentication failed: {exc}") from exc
        if not token:
            raise UploadError("google photos authentication returned an empty token")
        account = self.account()
        return [account] if account else []

    # ------------------------------------------------------------------ upload
    def upload_path(
        self,
        path: Any,
        album: Optional[str] = None,
        album_id: Optional[str] = None,
        recursive: bool = True,
        timeout: Optional[int] = None,
    ) -> UploadOutcome:
        """Upload a file or directory and optionally attach it to one album.

        Args:
            path: file or directory to upload.
            album: album name to create (used when no album key is known yet).
            album_id: media key of an existing album to append to. Mutually
                exclusive with ``album``.
            recursive: descend into sub-directories.
            timeout: per-request timeout override, seconds.
        """
        if album and album_id:
            raise UploadError("album and album_id are mutually exclusive")
        target = Path(path)
        client = self.client()
        if timeout is not None:
            try:
                client.api.timeout = max(1, int(timeout))
            except Exception:  # noqa: BLE001 - best effort only
                log.debug("could not apply the %s second timeout override", timeout)

        files = self.media_files(target, recursive)
        outcome = UploadOutcome(album_name=album, total=len(files))
        if not files:
            log.warning("no image/video file found under %s; nothing to upload", target)
            return outcome

        try:
            results = client.upload(
                target=target,
                recursive=recursive,
                show_progress=self.show_progress,
                threads=self.threads,
                force_upload=self.force_upload,
                use_quota=self.use_quota,
                saver=self.saver,
                skip_existing_filenames=self.skip_existing_filenames,
            )
        except ValueError as exc:
            raise UploadError(f"gpmc found nothing to upload in {target}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - one batch must not kill the run
            raise UploadError(f"gpmc upload failed for {target}: {exc}") from exc

        for raw_path, media_key in (results or {}).items():
            if media_key:
                outcome.media_keys[os.path.abspath(str(raw_path))] = str(media_key)
        outcome.succeeded = len(outcome.media_keys)
        for file_path in files:
            absolute = os.path.abspath(str(file_path))
            if absolute not in outcome.media_keys:
                outcome.errors[absolute] = NO_MEDIA_KEY
        outcome.failed = len(outcome.errors)
        outcome.raw = {
            "target": str(target),
            "threads": self.threads,
            "forceUpload": self.force_upload,
            "useQuota": self.use_quota,
            "saver": self.saver,
            "mediaKeys": dict(outcome.media_keys),
        }
        self._attach_album(client, outcome, album, album_id)
        return outcome

    def _attach_album(
        self,
        client: Any,
        outcome: UploadOutcome,
        album: Optional[str],
        album_id: Optional[str],
    ) -> None:
        if not outcome.media_keys or not (album or album_id):
            return
        media_keys = list(outcome.media_keys.values())
        label = album or album_id
        try:
            if album_id:
                client.add_to_existing_album(
                    media_keys, album_id, show_progress=self.show_progress
                )
                outcome.album_keys = [str(album_id)]
            else:
                created = (
                    client.add_to_album(
                        media_keys, album, show_progress=self.show_progress
                    )
                    or []
                )
                outcome.album_keys = [str(key) for key in created if key]
            outcome.items_added = len(media_keys)
        except Exception as exc:  # noqa: BLE001 - uploads already succeeded
            outcome.album_error = str(exc)
            log.error(
                "adding %s item(s) to album %r failed: %s", len(media_keys), label, exc
            )

    # ------------------------------------------------------------------- files
    def media_files(self, target: Path, recursive: bool = True) -> List[Path]:
        """Files gpmc would consider uploadable, in a stable order."""
        if target.is_file():
            return [target] if self.is_media(target) else []
        if not target.is_dir():
            return []
        candidates: Iterable[Path] = target.rglob("*") if recursive else target.iterdir()
        return sorted(
            path for path in candidates if path.is_file() and self.is_media(path)
        )

    @staticmethod
    def is_media(path: Path) -> bool:
        guess, _ = mimetypes.guess_type(path.name)
        return bool(guess and guess.startswith(MEDIA_MIME_PREFIXES))
