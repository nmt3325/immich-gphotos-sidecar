"""Album handling: Immich album index + optional Library API album sync."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .gphotos import GooglePhotosClient, GPhotosError, GPhotosPermissionError
from .immich import ImmichClient
from .log import get_logger

log = get_logger("albums")


def format_album_name(template: str, name: str) -> str:
    formatted = (template or "{album}").replace("{album}", name).strip()
    return formatted or name


@dataclass
class AlbumInfo:
    album_id: str
    name: str
    gp_name: str
    asset_ids: List[str] = field(default_factory=list)
    updated_at: str = ""
    shared: bool = False

    @property
    def asset_count(self) -> int:
        return len(self.asset_ids)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "albumId": self.album_id,
            "name": self.name,
            "googlePhotosAlbum": self.gp_name,
            "shared": self.shared,
            "updatedAt": self.updated_at,
            "assetCount": self.asset_count,
            "assetIds": self.asset_ids,
        }


def load_album_index(
    immich: ImmichClient,
    template: str = "{album}",
    include_shared: bool = False,
) -> Tuple[Dict[str, AlbumInfo], Dict[str, List[str]]]:
    """Return (albums_by_id, asset_id -> [album_id])."""
    albums: Dict[str, AlbumInfo] = {}
    asset_albums: Dict[str, List[str]] = {}
    reported_total = 0
    for summary in immich.list_albums():
        album_id = summary.get("id")
        if not album_id:
            continue
        shared = bool(summary.get("shared"))
        if shared and not include_shared:
            log.debug("skipping shared album %s", summary.get("albumName"))
            continue
        detail = immich.get_album(album_id)
        asset_ids = [asset.get("id") for asset in (detail.get("assets") or []) if asset.get("id")]
        name = detail.get("albumName") or summary.get("albumName") or album_id
        try:
            reported = int(summary.get("assetCount") or detail.get("assetCount") or 0)
        except (TypeError, ValueError):
            reported = 0
        reported_total += reported
        if reported and not asset_ids:
            log.warning(
                "album %r reports %s member(s) but the album detail returned no assets "
                "(response keys: %s)",
                name,
                reported,
                ", ".join(sorted(str(key) for key in detail.keys())[:15]) or "-",
            )
        elif not reported and not asset_ids:
            log.debug("album %r is empty in immich", name)
        info = AlbumInfo(
            album_id=album_id,
            name=name,
            gp_name=format_album_name(template, name),
            asset_ids=asset_ids,
            updated_at=str(detail.get("updatedAt") or summary.get("updatedAt") or ""),
            shared=shared,
        )
        albums[album_id] = info
        for asset_id in asset_ids:
            asset_albums.setdefault(asset_id, []).append(album_id)
    log.info(
        "indexed %s albums covering %s assets (immich reports %s member(s))",
        len(albums),
        len(asset_albums),
        reported_total,
    )
    if reported_total and not asset_albums:
        log.warning(
            "no album membership could be read from immich even though it reports %s member(s); "
            "album backup will be skipped this run",
            reported_total,
        )
    return albums, asset_albums


def album_names_for(asset_id: str, asset_albums: Dict[str, List[str]], albums: Dict[str, AlbumInfo]) -> List[str]:
    return [albums[album_id].name for album_id in asset_albums.get(asset_id, []) if album_id in albums]


class LibraryAlbumSyncer:
    """Optional ALBUM_BACKEND=library_api implementation.

    The official API can only see media items created by the same OAuth client,
    so this maps by filename over `mediaItems:search` and silently disables
    itself when Google denies access.
    """

    def __init__(self, client: GooglePhotosClient) -> None:
        self.client = client
        self.disabled = False
        self._index: Optional[Dict[str, str]] = None

    def _filename_index(self) -> Dict[str, str]:
        if self._index is None:
            try:
                self._index = self.client.index_by_filename()
                log.info("library api sees %s media items", len(self._index))
            except GPhotosPermissionError as exc:
                log.warning("library api album sync disabled: %s", exc)
                self.disabled = True
                self._index = {}
            except GPhotosError as exc:
                log.warning("library api listing failed: %s", exc)
                self._index = {}
        return self._index

    def sync(self, album_name: str, filenames: Iterable[str]) -> int:
        if self.disabled:
            return 0
        index = self._filename_index()
        ids = [index[name] for name in filenames if name in index]
        if not ids:
            return 0
        try:
            album = self.client.ensure_album(album_name)
            return self.client.batch_add_media_items(album["id"], ids)
        except GPhotosPermissionError as exc:
            log.warning("library api album sync disabled: %s", exc)
            self.disabled = True
        except GPhotosError as exc:
            log.warning("library api album sync failed for %s: %s", album_name, exc)
        return 0
