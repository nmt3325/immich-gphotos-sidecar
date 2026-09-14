"""Backup orchestration: Immich -> sidecar files -> gpmc -> Google Photos.

One pass does three phases:

Phase A (prepare)  enumerate changed assets, write sidecar JSON/XMP, download
                   the originals that still need uploading and embed metadata.
Phase B (upload)   stage hardlinks per Immich album and hand every album batch
                   to the gpmc uploader, which attaches the returned media keys
                   to the album. Duplicates are deduplicated server side but
                   still return their media key, which is how album backfill
                   works for already-uploaded assets.
Phase C (finish)   optional Library API metadata pass, library snapshot,
                   manifest, watermark advance and run report.

Phases A and B are interleaved in cycles of UPLOAD_BATCH_SIZE assets so a large
first run uploads as it goes instead of downloading the whole library first.
That keeps the work cache bounded, prints progress and stays resumable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set

from .albums import AlbumInfo, LibraryAlbumSyncer, load_album_index
from .config import Config
from .gpmc_uploader import GpmcUploader, UploadError
from .gphotos import GooglePhotosClient, GPhotosError, GPhotosPermissionError
from .immich import ImmichClient, ImmichError
from .log import get_logger
from .sidecar import (
    apply_file_times,
    build_sidecar,
    embed_metadata,
    safe_component,
    sidecar_hash,
    write_sidecar_files,
)
from .state import State

log = get_logger("backup")

MAX_ATTEMPTS = 5
RETRY_BUDGET = 500
PROGRESS_EVERY = 25
UNSORTED = "_unsorted"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def chunked(items: Sequence[Any], size: int) -> Iterator[List[Any]]:
    step = max(1, size)
    for start in range(0, len(items), step):
        yield list(items[start : start + step])


@dataclass
class Prepared:
    asset_id: str
    payload: Dict[str, Any]
    album_ids: List[str] = field(default_factory=list)
    media_key: Optional[str] = None
    local_path: Optional[Path] = None
    needs_upload: bool = False


class BackupRunner:
    def __init__(self, cfg: Config) -> None:
        cfg.ensure_dirs()
        self.cfg = cfg
        self.state = State(cfg.state_path)
        self.immich = ImmichClient(
            cfg.immich_base_url, cfg.immich_api_key, cfg.immich_timeout, cfg.immich_retries
        )
        self.uploader = GpmcUploader(
            auth_data=cfg.gpmc_auth_data,
            threads=cfg.gpmc_threads,
            timeout=cfg.gpmc_timeout,
            proxy=cfg.gpmc_proxy,
            language=cfg.gpmc_language,
            log_level=cfg.gpmc_effective_log_level,
            use_quota=cfg.gpmc_use_quota,
            saver=cfg.gpmc_saver,
            force_upload=cfg.gpmc_force_upload,
            skip_existing_filenames=cfg.gpmc_skip_existing_filenames,
            show_progress=cfg.gpmc_show_progress,
            cache_dir=cfg.gpmc_cache_dir,
        )
        self.gphotos: Optional[GooglePhotosClient] = None
        self.album_syncer: Optional[LibraryAlbumSyncer] = None
        if cfg.wants_library_api and cfg.gphotos_configured:
            self.gphotos = GooglePhotosClient(
                cfg.gphotos_client_id, cfg.gphotos_client_secret, cfg.gphotos_refresh_token
            )
            if cfg.album_backend == "library_api":
                self.album_syncer = LibraryAlbumSyncer(self.gphotos)
        self._uploaded_ids: Set[str] = set()
        self._truncated = False

    def close(self) -> None:
        self.state.close()

    # ------------------------------------------------------------------ run
    def run(self, should_stop: Callable[[], bool] = lambda: False) -> Dict[str, Any]:
        cfg = self.cfg
        started = utcnow()
        run_id = f"{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
        counters: Dict[str, int] = {
            "scanned": 0,
            "candidates": 0,
            "skipped": 0,
            "planned": 0,
            "sidecars_written": 0,
            "downloaded": 0,
            "bytes_downloaded": 0,
            "uploaded": 0,
            "upload_calls": 0,
            "album_links": 0,
            "failed": 0,
            "library_api_album_adds": 0,
            "library_api_descriptions": 0,
        }
        errors: List[Dict[str, str]] = []
        self._uploaded_ids.clear()
        self._truncated = False

        log.info("run %s starting (dry_run=%s full_scan=%s)", run_id, cfg.dry_run, cfg.full_scan)
        self.immich.ping()
        if not cfg.dry_run:
            accounts = self.uploader.ensure_credentials()
            log.info(
                "google photos account: %s (gpmc %s)",
                ", ".join(accounts) or "-",
                self.uploader.version(),
            )

        albums: Dict[str, AlbumInfo] = {}
        asset_albums: Dict[str, List[str]] = {}
        if cfg.album_backend != "none":
            albums, asset_albums = load_album_index(
                self.immich, cfg.album_name_template, cfg.album_include_shared
            )
            for info in albums.values():
                self.state.upsert_album(
                    info.album_id,
                    name=info.name,
                    gp_album_name=info.gp_name,
                    asset_count=info.asset_count,
                    updated_at=info.updated_at,
                )

        watermark = None if cfg.full_scan else self.state.get_meta("watermark")
        log.info("watermark: %s", watermark or "(full scan)")
        candidates = self._collect_candidates(watermark, albums, asset_albums, counters)
        counters["candidates"] = len(candidates)
        log.info("scanned %s assets, %s need work", counters["scanned"], len(candidates))

        total = len(candidates)
        cycle_size = max(1, cfg.upload_batch_size)
        if total > cycle_size:
            log.info("processing %s assets in cycles of %s (prepare -> upload -> cleanup)", total, cycle_size)
        processed = 0
        for cycle_index, cycle in enumerate(chunked(candidates, cycle_size), start=1):
            if should_stop():
                log.warning("stop requested; %s of %s assets processed", processed, total)
                break
            prepared: List[Prepared] = []
            for asset_id in cycle:
                if should_stop():
                    log.warning("stop requested during prepare phase")
                    break
                processed += 1
                try:
                    item = self._prepare_asset(asset_id, albums, asset_albums, counters)
                except Exception as exc:  # noqa: BLE001 - one bad asset must not kill the run
                    counters["failed"] += 1
                    errors.append({"assetId": asset_id, "stage": "prepare", "error": str(exc)[:300]})
                    log.warning("prepare failed for %s: %s", asset_id, exc)
                    self.state.bump_attempt(asset_id, f"prepare: {exc}")
                    continue
                if item is not None:
                    prepared.append(item)
                if processed % PROGRESS_EVERY == 0 or processed == total:
                    log.info(
                        "progress %s/%s assets (uploaded=%s skipped=%s downloaded=%s failed=%s)",
                        processed,
                        total,
                        len(self._uploaded_ids),
                        counters["skipped"],
                        counters["downloaded"],
                        counters["failed"],
                    )

            if prepared and not cfg.dry_run:
                self._upload_phase(prepared, albums, counters, errors, should_stop)
                self._library_api_phase(prepared, albums, counters)
                if not cfg.keep_local_copies:
                    self._cleanup_cache(prepared)
                log.info(
                    "cycle %s finished at %s/%s assets (uploaded=%s album_links=%s failed=%s)",
                    cycle_index,
                    processed,
                    total,
                    len(self._uploaded_ids),
                    counters["album_links"],
                    counters["failed"],
                )

        counters["uploaded"] = len(self._uploaded_ids)
        self._write_library_snapshot(albums, run_id)

        stopped = should_stop()
        if (
            not cfg.dry_run
            and not stopped
            and not self._truncated
            and counters["failed"] == 0
        ):
            new_watermark = iso(started - timedelta(minutes=cfg.watermark_skew_minutes))
            self.state.set_meta("watermark", new_watermark)
            log.info("watermark advanced to %s", new_watermark)
        elif self._truncated:
            log.warning(
                "keeping previous watermark because this run was capped at %s assets; "
                "rerun until the cap is no longer hit",
                cfg.max_assets_per_run,
            )
        elif stopped:
            log.warning("keeping previous watermark because the run was interrupted")
        elif counters["failed"]:
            log.warning("keeping previous watermark because %s assets failed", counters["failed"])

        ended = utcnow()
        report: Dict[str, Any] = {
            "runId": run_id,
            "startedAt": iso(started),
            "endedAt": iso(ended),
            "durationSeconds": round((ended - started).total_seconds(), 1),
            "dryRun": cfg.dry_run,
            "stopped": stopped,
            "truncated": self._truncated,
            "albumBackend": cfg.album_backend,
            "metadataBackend": cfg.metadata_backend,
            "albums": len(albums),
            **counters,
            "errors": errors[:25],
            "state": self.state.stats(),
        }
        self.state.record_run(run_id, iso(started), iso(ended), report)
        report_path = self.cfg.reports_dir / f"{run_id}.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info(
            "run %s done: uploaded=%s album_links=%s sidecars=%s failed=%s",
            run_id,
            counters["uploaded"],
            counters["album_links"],
            counters["sidecars_written"],
            counters["failed"],
        )
        return report

    # -------------------------------------------------------------- phase A
    def _collect_candidates(
        self,
        watermark: Optional[str],
        albums: Dict[str, AlbumInfo],
        asset_albums: Dict[str, List[str]],
        counters: Dict[str, int],
    ) -> List[str]:
        cfg = self.cfg
        ordered: List[str] = []
        seen: Set[str] = set()

        for asset in self.immich.iter_assets(
            updated_after=watermark,
            page_size=cfg.page_size,
            asset_types=cfg.asset_types,
            include_archived=cfg.include_archived,
        ):
            counters["scanned"] += 1
            asset_id = asset.get("id")
            if not asset_id or asset_id in seen:
                continue
            if (asset.get("type") or "").upper() not in cfg.asset_types:
                continue
            if not cfg.include_archived and asset.get("isArchived"):
                continue
            seen.add(asset_id)
            ordered.append(asset_id)

        for asset_id in self.state.pending_asset_ids(MAX_ATTEMPTS, RETRY_BUDGET):
            if asset_id not in seen:
                seen.add(asset_id)
                ordered.append(asset_id)

        if cfg.backfill_albums and cfg.album_backend != "none":
            for album_id, info in albums.items():
                for asset_id in info.asset_ids:
                    if asset_id in seen:
                        continue
                    if not self.state.has_album_link(album_id, asset_id):
                        seen.add(asset_id)
                        ordered.append(asset_id)

        if cfg.max_assets_per_run > 0 and len(ordered) > cfg.max_assets_per_run:
            self._truncated = True
            log.warning(
                "capping this run at %s of %s candidate assets (MAX_ASSETS_PER_RUN); "
                "the watermark will not advance",
                cfg.max_assets_per_run,
                len(ordered),
            )
            ordered = ordered[: cfg.max_assets_per_run]
        return ordered

    def _prepare_asset(
        self,
        asset_id: str,
        albums: Dict[str, AlbumInfo],
        asset_albums: Dict[str, List[str]],
        counters: Dict[str, int],
    ) -> Optional[Prepared]:
        cfg = self.cfg
        existing = self.state.get_asset(asset_id) or {}
        media_key = (existing.get("media_key") or "") or None
        album_ids = [aid for aid in asset_albums.get(asset_id, []) if aid in albums]

        asset = self.immich.get_asset(asset_id)
        if (asset.get("type") or "").upper() not in cfg.asset_types:
            return None
        if not cfg.include_archived and asset.get("isArchived"):
            return None

        payload = build_sidecar(asset, [albums[aid].name for aid in album_ids])
        digest = sidecar_hash(payload)
        if digest != (existing.get("sidecar_hash") or "") and (
            cfg.write_json_sidecar or cfg.write_xmp_sidecar
        ):
            write_sidecar_files(
                payload, cfg.sidecar_assets_dir, cfg.write_json_sidecar, cfg.write_xmp_sidecar
            )
            counters["sidecars_written"] += 1

        self.state.upsert_asset(
            asset_id,
            checksum=asset.get("checksum"),
            original_file_name=asset.get("originalFileName"),
            asset_type=asset.get("type"),
            immich_updated_at=str(asset.get("updatedAt") or ""),
            sidecar_hash=digest,
            sidecar_written_at=iso(utcnow()),
            status=existing.get("status") or "pending",
        )

        missing_links = [
            aid for aid in album_ids if not self.state.has_album_link(aid, asset_id)
        ]
        needs_upload = media_key is None
        needs_album = bool(missing_links) and cfg.album_backend == "gpmc" and (
            cfg.backfill_albums or needs_upload
        )
        if not needs_upload and not needs_album and not (
            missing_links and cfg.album_backend == "library_api"
        ):
            counters["skipped"] += 1
            return None

        if cfg.dry_run:
            counters["planned"] += 1
            log.info(
                "[dry-run] would upload %s (%s) albums=%s",
                asset.get("originalFileName"),
                asset_id,
                [albums[aid].gp_name for aid in album_ids],
            )
            return None

        file_name = safe_component(asset.get("originalFileName") or f"{asset_id}.bin")
        target = cfg.cache_dir / asset_id[:2] / asset_id / file_name
        if not target.exists() or target.stat().st_size == 0:
            size = self.immich.download_original(asset_id, target, cfg.download_retries)
            counters["downloaded"] += 1
            counters["bytes_downloaded"] += size
            if cfg.wants_embed:
                embed_metadata(target, payload, cfg.exiftool_bin)
        if cfg.set_file_mtime:
            apply_file_times(target, payload)
        return Prepared(
            asset_id=asset_id,
            payload=payload,
            album_ids=album_ids,
            media_key=media_key,
            local_path=target,
            needs_upload=needs_upload,
        )

    # -------------------------------------------------------------- phase B
    def _upload_phase(
        self,
        prepared: List[Prepared],
        albums: Dict[str, AlbumInfo],
        counters: Dict[str, int],
        errors: List[Dict[str, str]],
        should_stop: Callable[[], bool],
    ) -> None:
        cfg = self.cfg
        by_album: Dict[str, List[Prepared]] = {}
        unsorted: List[Prepared] = []
        for item in prepared:
            targets: List[str] = []
            if cfg.album_backend == "gpmc":
                targets = [
                    aid
                    for aid in item.album_ids
                    if not self.state.has_album_link(aid, item.asset_id)
                ]
            if targets:
                for album_id in targets:
                    by_album.setdefault(album_id, []).append(item)
            elif item.needs_upload:
                unsorted.append(item)

        for album_id, items in sorted(by_album.items(), key=lambda kv: albums[kv[0]].name):
            info = albums[album_id]
            log.info("uploading %s asset(s) into album %r", len(items), info.gp_name)
            for batch in chunked(items, cfg.upload_batch_size):
                if should_stop():
                    return
                self._upload_batch(batch, album_id, info.gp_name, counters, errors)

        if unsorted:
            log.info("uploading %s asset(s) without album", len(unsorted))
        for batch in chunked(unsorted, cfg.upload_batch_size):
            if should_stop():
                return
            self._upload_batch(batch, None, None, counters, errors)

    def _stage_file(self, item: Prepared, stage_dir: Path) -> Optional[Path]:
        if not item.local_path or not item.local_path.exists():
            return None
        stage_dir.mkdir(parents=True, exist_ok=True)
        target = stage_dir / item.local_path.name
        if target.exists():
            target = stage_dir / (
                f"{item.local_path.stem}_{item.asset_id[:8]}{item.local_path.suffix}"
            )
        try:
            os.link(item.local_path, target)
        except OSError:
            shutil.copy2(item.local_path, target)
        return target

    def _upload_batch(
        self,
        items: List[Prepared],
        album_id: Optional[str],
        album_name: Optional[str],
        counters: Dict[str, int],
        errors: List[Dict[str, str]],
    ) -> None:
        cfg = self.cfg
        label = safe_component(album_name or UNSORTED, UNSORTED, 60)
        stage = cfg.stage_dir / f"{label}-{(album_id or 'none')[:8]}-{uuid.uuid4().hex[:6]}"
        mapping: Dict[str, str] = {}
        try:
            for item in items:
                staged = self._stage_file(item, stage)
                if staged is None:
                    log.warning("missing local copy for %s; skipping", item.asset_id)
                    continue
                mapping[os.path.abspath(str(staged))] = item.asset_id
            if not mapping:
                return
            counters["upload_calls"] += 1
            # Append to the Google album an earlier run created instead of letting
            # gpmc create a second album with the same name.
            album_key = None
            if album_id:
                album_key = (self.state.album(album_id) or {}).get("gp_album_key") or None
            try:
                outcome = self.uploader.upload_path(
                    stage,
                    album=None if album_key else album_name,
                    album_id=album_key,
                )
            except UploadError as exc:
                counters["failed"] += len(mapping)
                errors.append({"stage": "upload", "album": album_name or "", "error": str(exc)[:400]})
                log.error("gpmc upload failed: %s", exc)
                for asset_id in mapping.values():
                    self.state.bump_attempt(asset_id, f"upload: {exc}")
                return

            album_ok = album_id is None or not outcome.album_error
            now = iso(utcnow())
            for path, media_key in outcome.media_keys.items():
                asset_id = self._resolve_asset(path, mapping)
                if not asset_id:
                    log.debug("unmatched upload result path %s", path)
                    continue
                self.state.upsert_asset(
                    asset_id,
                    media_key=media_key,
                    uploaded_at=now,
                    status="uploaded",
                    last_error=None,
                )
                self._uploaded_ids.add(asset_id)
                if album_id and album_ok:
                    self.state.add_album_link(album_id, asset_id, media_key, now)
                    counters["album_links"] += 1
            for path, error in outcome.errors.items():
                asset_id = self._resolve_asset(path, mapping)
                counters["failed"] += 1
                errors.append(
                    {"assetId": asset_id or "", "stage": "upload", "error": str(error)[:300]}
                )
                if asset_id:
                    self.state.bump_attempt(asset_id, str(error))
            if outcome.album_error:
                counters["failed"] += 1
                errors.append(
                    {
                        "stage": "album",
                        "album": album_name or "",
                        "error": str(outcome.album_error)[:400],
                    }
                )
            if album_id:
                album_fields: Dict[str, Any] = {"synced_at": now}
                if outcome.album_keys:
                    album_fields["gp_album_key"] = outcome.album_keys[0]
                self.state.upsert_album(album_id, **album_fields)
            log.info(
                "gpmc: total=%s ok=%s failed=%s album=%r added=%s existing_album=%s",
                outcome.total,
                outcome.succeeded,
                outcome.failed,
                album_name,
                outcome.items_added,
                bool(album_key),
            )
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    @staticmethod
    def _resolve_asset(path: str, mapping: Dict[str, str]) -> Optional[str]:
        absolute = os.path.abspath(path)
        if absolute in mapping:
            return mapping[absolute]
        base = os.path.basename(path)
        for staged_path, asset_id in mapping.items():
            if os.path.basename(staged_path) == base:
                return asset_id
        return None

    # -------------------------------------------------------------- phase C
    def _library_api_phase(
        self,
        prepared: List[Prepared],
        albums: Dict[str, AlbumInfo],
        counters: Dict[str, int],
    ) -> None:
        cfg = self.cfg
        if not self.gphotos:
            return
        if self.album_syncer and cfg.album_backend == "library_api":
            grouped: Dict[str, List[str]] = {}
            for item in prepared:
                name = item.payload.get("originalFileName")
                if not name:
                    continue
                for album_id in item.album_ids:
                    grouped.setdefault(albums[album_id].gp_name, []).append(name)
            for gp_name, filenames in grouped.items():
                counters["library_api_album_adds"] += self.album_syncer.sync(gp_name, filenames)

        if cfg.metadata_backend not in ("library_api", "both"):
            return
        try:
            index = self.gphotos.index_by_filename()
        except GPhotosPermissionError as exc:
            log.warning("library api metadata pass skipped: %s", exc)
            return
        except GPhotosError as exc:
            log.warning("library api listing failed: %s", exc)
            return
        for item in prepared:
            description = item.payload.get("description")
            name = item.payload.get("originalFileName")
            if not description or not name:
                continue
            media_item_id = index.get(name)
            if not media_item_id:
                continue
            try:
                self.gphotos.patch_description(media_item_id, description)
                counters["library_api_descriptions"] += 1
            except GPhotosPermissionError as exc:
                log.warning("library api metadata pass aborted: %s", exc)
                return
            except GPhotosError as exc:
                log.warning("description patch failed for %s: %s", name, exc)

    def _cleanup_cache(self, prepared: List[Prepared]) -> None:
        for item in prepared:
            if not item.local_path:
                continue
            if self.state.media_key(item.asset_id):
                shutil.rmtree(item.local_path.parent, ignore_errors=True)

    def _write_library_snapshot(self, albums: Dict[str, AlbumInfo], run_id: str) -> None:
        cfg = self.cfg
        try:
            (cfg.library_dir / "albums.json").write_text(
                json.dumps(
                    {
                        "runId": run_id,
                        "exportedAt": iso(utcnow()),
                        "albums": [info.as_dict() for info in albums.values()],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (cfg.library_dir / "tags.json").write_text(
                json.dumps(self.immich.list_tags(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (cfg.library_dir / "people.json").write_text(
                json.dumps(self.immich.list_people(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            manifest = cfg.library_dir / "manifest.jsonl"
            with open(manifest, "w", encoding="utf-8") as handle:
                for row in self.state.iter_manifest():
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 - snapshot is best effort
            log.warning("library snapshot failed: %s", exc)


def run_doctor(cfg: Config) -> Dict[str, Any]:
    """Verify configuration, connectivity and tooling."""
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: Any = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": str(detail)[:400]})

    problems = cfg.problems()
    add("config", not problems, "; ".join(problems) or "ok")

    try:
        cfg.ensure_dirs()
        add("directories", True, f"state={cfg.state_dir} sidecar={cfg.sidecar_dir} work={cfg.work_dir}")
    except OSError as exc:
        add("directories", False, exc)

    if cfg.immich_base_url and cfg.immich_api_key:
        client = ImmichClient(cfg.immich_base_url, cfg.immich_api_key, cfg.immich_timeout, 2)
        try:
            client.ping()
            about = client.about()
            add("immich", True, f"version={about.get('version', 'unknown')}")
        except ImmichError as exc:
            add("immich", False, exc)
        try:
            albums = client.list_albums()
            total = sum(int(album.get("assetCount") or 0) for album in albums)
            add("immich_albums", True, f"{len(albums)} album(s) visible, {total} member(s) reported")
        except ImmichError as exc:
            add("immich_albums", False, exc)
    else:
        add("immich", False, "IMMICH_BASE_URL / IMMICH_API_KEY missing")

    uploader = GpmcUploader(
        auth_data=cfg.gpmc_auth_data,
        threads=cfg.gpmc_threads,
        timeout=min(cfg.gpmc_timeout, 120),
        proxy=cfg.gpmc_proxy,
        language=cfg.gpmc_language,
        log_level=cfg.gpmc_effective_log_level,
        cache_dir=cfg.gpmc_cache_dir,
    )
    try:
        add("gpmc_library", True, f"gpmc {uploader.version()}")
    except UploadError as exc:
        add("gpmc_library", False, exc)
    try:
        accounts = uploader.ensure_credentials()
        add("gpmc_credentials", True, ", ".join(accounts) or "authenticated")
    except UploadError as exc:
        add("gpmc_credentials", False, exc)

    if cfg.wants_embed:
        try:
            result = subprocess.run(
                [cfg.exiftool_bin, "-ver"], capture_output=True, text=True, timeout=30, check=False
            )
            add("exiftool", result.returncode == 0, (result.stdout or result.stderr).strip())
        except (OSError, subprocess.SubprocessError) as exc:
            add("exiftool", False, exc)

    if cfg.wants_library_api:
        if not cfg.gphotos_configured:
            add("google_photos", False, "GPHOTOS_CLIENT_ID/SECRET/REFRESH_TOKEN missing")
        else:
            client = GooglePhotosClient(
                cfg.gphotos_client_id, cfg.gphotos_client_secret, cfg.gphotos_refresh_token
            )
            try:
                add("google_photos", True, client.check())
            except GPhotosError as exc:
                add("google_photos", False, exc)

    try:
        state = State(cfg.state_path)
        try:
            add("state", True, state.stats())
        finally:
            state.close()
    except Exception as exc:  # noqa: BLE001
        add("state", False, exc)

    return {
        "ok": all(item["ok"] for item in checks),
        "albumBackend": cfg.album_backend,
        "metadataBackend": cfg.metadata_backend,
        "checks": checks,
    }
