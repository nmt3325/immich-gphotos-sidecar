"""Read Immich originals straight from the mounted library.

Immich and this sidecar normally run on the same host, so pulling every
original through `GET /api/assets/{id}/original` pushes gigabytes through the
HTTP stack for nothing. When the Immich upload location is mounted into this
container, `asset.originalPath` is mapped onto that mount and the file is read
directly: no HTTP round trip, and no second copy at all when metadata
embedding is disabled.

Rules this module keeps:
  * the library is only ever read; `materialize()` only writes into the work dir
  * a resolved path must stay inside one of the configured roots
  * files are validated against Immich's size and checksum before being used
"""

from __future__ import annotations

import base64
import hashlib
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .log import get_logger

log = get_logger("library")

ASSET_SOURCES = ("auto", "local", "api")
READ_CHUNK = 1024 * 1024
# Immich stores `originalPath` relative to /usr/src/app (e.g.
# "upload/upload/<userId>/ab/cd/<assetId>.jpg", or
# "upload/library/admin/2024/IMG_0001.jpg" when a storage template is set),
# and older versions store it absolute. Either way the leading components map
# onto whatever UPLOAD_LOCATION is mounted as over here, so they are stripped.
# The file on disk is usually named after the asset id, so the name to upload
# with comes from `originalFileName` and never from the path.
DEFAULT_PATH_PREFIXES: Sequence[str] = ("/usr/src/app/upload", "/usr/src/app")


class LocalLibraryError(RuntimeError):
    """A local original is missing or cannot be trusted."""


def file_checksum(path: Path) -> str:
    """base64(sha1(file)) - the exact shape Immich stores in `checksum`."""
    digest = hashlib.sha1()  # noqa: S324 - matching Immich's own algorithm
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(READ_CHUNK), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def _components(value: str) -> List[str]:
    text = (value or "").strip().replace("\\", "/")
    if not text:
        return []
    return [part for part in PurePosixPath(text).parts if part not in ("", "/")]


class LocalLibrary:
    """Maps Immich `originalPath` values onto mounted directories."""

    def __init__(
        self,
        roots: Sequence[str] | None,
        prefixes: Sequence[str] | None = None,
        verify_checksum: bool = True,
    ) -> None:
        self.roots: List[Path] = []
        for raw in roots or []:
            text = str(raw).strip()
            if text:
                self.roots.append(Path(text).expanduser())
        self.prefixes: List[List[str]] = []
        for raw in (DEFAULT_PATH_PREFIXES if prefixes is None else prefixes):
            parts = _components(str(raw))
            if parts:
                self.prefixes.append(parts)
        self.verify_checksum = bool(verify_checksum)
        self.resolved = 0
        self.missed = 0
        self._hint: Optional[Tuple[int, int]] = None

    @property
    def enabled(self) -> bool:
        return bool(self.roots)

    def missing_roots(self) -> List[Path]:
        return [root for root in self.roots if not root.is_dir()]

    # ------------------------------------------------------------ resolution
    def _drop_counts(self, parts: List[str]) -> List[int]:
        """How many leading components to drop, most likely first."""
        drops: List[int] = []
        for prefix in self.prefixes:
            if len(prefix) < len(parts) and parts[: len(prefix)] == prefix:
                drops.append(len(prefix))
        drops.extend(range(len(parts)))
        return list(dict.fromkeys(drops))

    def _candidates(self, parts: List[str]) -> Iterator[Tuple[int, int, Path]]:
        drops = self._drop_counts(parts)
        order: List[Tuple[int, int]] = []
        # the mapping that worked last time is almost always the right one
        if self._hint and self._hint[0] < len(self.roots) and self._hint[1] in drops:
            order.append(self._hint)
        for index in range(len(self.roots)):
            for drop in drops:
                pair = (index, drop)
                if pair not in order:
                    order.append(pair)
        for index, drop in order:
            yield index, drop, self.roots[index].joinpath(*parts[drop:])

    @staticmethod
    def _inside(root: Path, candidate: Path) -> bool:
        """Guard against `..` and symlinks pointing out of the library."""
        try:
            real_root = root.resolve()
            real = candidate.resolve()
        except OSError:
            return False
        return real_root in real.parents

    def resolve(self, original_path: str) -> Optional[Path]:
        parts = _components(original_path)
        if not parts or not self.roots:
            return None
        for index, drop, candidate in self._candidates(parts):
            try:
                if not candidate.is_file():
                    continue
            except OSError:
                continue
            if not self._inside(self.roots[index], candidate):
                log.warning("ignoring %s: it points outside %s", candidate, self.roots[index])
                continue
            if self._hint != (index, drop):
                log.info("library mapping learned: %s -> %s", original_path, candidate)
                self._hint = (index, drop)
            return candidate
        return None

    # ------------------------------------------------------------ validation
    def verify(self, path: Path, asset: Dict[str, Any]) -> int:
        """Make sure the file on disk really is the asset Immich described."""
        size = path.stat().st_size
        if size <= 0:
            raise LocalLibraryError(f"{path} is empty")
        raw_expected = (asset.get("exifInfo") or {}).get("fileSizeInByte")
        try:
            expected = int(raw_expected) if raw_expected is not None else 0
        except (TypeError, ValueError):
            expected = 0
        if expected > 0 and expected != size:
            raise LocalLibraryError(f"{path} is {size} bytes but immich reported {expected}")
        checksum = asset.get("checksum")
        if self.verify_checksum and checksum:
            actual = file_checksum(path)
            if actual != checksum:
                raise LocalLibraryError(
                    f"checksum mismatch for {path} (immich={checksum} local={actual})"
                )
        return size

    def locate(self, asset: Dict[str, Any]) -> Optional[Path]:
        """Validated on-disk original for an Immich asset payload, or None."""
        if not self.enabled:
            return None
        path = self.resolve(asset.get("originalPath") or "")
        if path is None:
            self.missed += 1
            return None
        self.verify(path, asset)
        self.resolved += 1
        return path

    # ----------------------------------------------------------- copy helper
    def materialize(self, source: Path, target: Path) -> int:
        """Copy an original into the work dir. Never writes to the library."""
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".part")
        temp.unlink(missing_ok=True)
        if not self._reflink(source, temp):
            shutil.copy2(source, temp)
        size = temp.stat().st_size
        temp.replace(target)
        return size

    @staticmethod
    def _reflink(source: Path, target: Path) -> bool:
        """Copy-on-write clone (btrfs/xfs): instant and free. Best effort."""
        try:
            result = subprocess.run(
                [
                    "cp",
                    "--reflink=always",
                    "--preserve=timestamps",
                    str(source),
                    str(target),
                ],
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode == 0 and target.exists():
            return True
        target.unlink(missing_ok=True)
        return False

    def describe(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "roots": [str(root) for root in self.roots],
            "missingRoots": [str(root) for root in self.missing_roots()],
            "verifyChecksum": self.verify_checksum,
            "resolved": self.resolved,
            "missed": self.missed,
        }
