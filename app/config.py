"""Environment driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

_TRUE = {"1", "true", "yes", "y", "on"}

ALBUM_BACKENDS = ("gpmc", "library_api", "none")
# `gotohp` was the name of the previous uploader; keep old configs working.
ALBUM_BACKEND_ALIASES = {"gotohp": "gpmc"}
METADATA_BACKENDS = ("embed", "library_api", "both", "none")


def env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in _TRUE


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def env_list(name: str, default: List[str]) -> List[str]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return list(default)
    return [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]


@dataclass
class Config:
    # --- Immich ---
    immich_base_url: str = ""
    immich_api_key: str = ""
    immich_timeout: int = 60
    immich_retries: int = 4

    # --- gpmc (Google Photos mobile API uploader) ---
    gpmc_auth_data: str = ""
    gpmc_threads: int = 3
    gpmc_timeout: int = 60
    gpmc_proxy: str = ""
    gpmc_language: str = ""
    gpmc_log_level: str = ""
    gpmc_cache_dir: str = "/config"
    gpmc_use_quota: bool = False
    gpmc_saver: bool = False
    gpmc_force_upload: bool = False
    gpmc_skip_existing_filenames: bool = False
    gpmc_show_progress: bool = False

    # --- Google side behaviour ---
    album_backend: str = "gpmc"
    album_name_template: str = "{album}"
    album_include_shared: bool = False
    backfill_albums: bool = True
    metadata_backend: str = "embed"
    gphotos_client_id: str = ""
    gphotos_client_secret: str = ""
    gphotos_refresh_token: str = ""

    # --- paths ---
    state_dir: str = "/state"
    sidecar_dir: str = "/sidecar"
    work_dir: str = "/work"

    # --- selection / sync ---
    include_archived: bool = True
    asset_types: List[str] = field(default_factory=lambda: ["IMAGE", "VIDEO"])
    page_size: int = 250
    full_scan: bool = False
    watermark_skew_minutes: int = 10
    max_assets_per_run: int = 0
    upload_batch_size: int = 200
    download_retries: int = 3

    # --- sidecar / metadata ---
    write_json_sidecar: bool = True
    write_xmp_sidecar: bool = True
    exiftool_bin: str = "exiftool"
    set_file_mtime: bool = True
    keep_local_copies: bool = False

    # --- scheduling ---
    schedule_cron: str = ""
    interval_minutes: int = 1440
    run_on_start: bool = True

    dry_run: bool = False
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            immich_base_url=env_str("IMMICH_BASE_URL").rstrip("/"),
            immich_api_key=env_str("IMMICH_API_KEY"),
            immich_timeout=env_int("IMMICH_TIMEOUT", 60),
            immich_retries=env_int("IMMICH_RETRIES", 4),
            # GP_AUTH_DATA is gpmc's own variable, GOTOHP_AUTH_STRING the legacy one
            gpmc_auth_data=(
                env_str("GPMC_AUTH_DATA")
                or env_str("GP_AUTH_DATA")
                or env_str("GOTOHP_AUTH_STRING")
            ),
            gpmc_threads=env_int("GPMC_THREADS", env_int("GOTOHP_THREADS", 3)),
            gpmc_timeout=env_int("GPMC_TIMEOUT", 60),
            gpmc_proxy=env_str("GPMC_PROXY"),
            gpmc_language=env_str("GPMC_LANGUAGE"),
            gpmc_log_level=env_str("GPMC_LOG_LEVEL").upper(),
            gpmc_cache_dir=env_str("GPMC_CACHE_DIR", "/config"),
            gpmc_use_quota=env_bool("GPMC_USE_QUOTA", False),
            gpmc_saver=env_bool("GPMC_SAVER", False),
            gpmc_force_upload=env_bool("GPMC_FORCE_UPLOAD", False),
            gpmc_skip_existing_filenames=env_bool("GPMC_SKIP_EXISTING_FILENAMES", False),
            gpmc_show_progress=env_bool("GPMC_SHOW_PROGRESS", False),
            album_backend=normalize_album_backend(env_str("ALBUM_BACKEND", "gpmc")),
            album_name_template=env_str("ALBUM_NAME_TEMPLATE", "{album}"),
            album_include_shared=env_bool("ALBUM_INCLUDE_SHARED", False),
            backfill_albums=env_bool("BACKFILL_ALBUMS", True),
            metadata_backend=env_str("METADATA_BACKEND", "embed").lower(),
            gphotos_client_id=env_str("GPHOTOS_CLIENT_ID"),
            gphotos_client_secret=env_str("GPHOTOS_CLIENT_SECRET"),
            gphotos_refresh_token=env_str("GPHOTOS_REFRESH_TOKEN"),
            state_dir=env_str("STATE_DIR", "/state"),
            sidecar_dir=env_str("SIDECAR_DIR", "/sidecar"),
            work_dir=env_str("WORK_DIR", "/work"),
            include_archived=env_bool("INCLUDE_ARCHIVED", True),
            asset_types=[t.upper() for t in env_list("ASSET_TYPES", ["IMAGE", "VIDEO"])],
            page_size=env_int("PAGE_SIZE", 250),
            full_scan=env_bool("FULL_SCAN", False),
            watermark_skew_minutes=env_int("WATERMARK_SKEW_MINUTES", 10),
            max_assets_per_run=env_int("MAX_ASSETS_PER_RUN", 0),
            upload_batch_size=env_int("UPLOAD_BATCH_SIZE", 200),
            download_retries=env_int("DOWNLOAD_RETRIES", 3),
            write_json_sidecar=env_bool("WRITE_JSON_SIDECAR", True),
            write_xmp_sidecar=env_bool("WRITE_XMP_SIDECAR", True),
            exiftool_bin=env_str("EXIFTOOL_BIN", "exiftool"),
            set_file_mtime=env_bool("SET_FILE_MTIME", True),
            keep_local_copies=env_bool("KEEP_LOCAL_COPIES", False),
            schedule_cron=env_str("SCHEDULE_CRON"),
            interval_minutes=env_int("INTERVAL_MINUTES", 1440),
            run_on_start=env_bool("RUN_ON_START", True),
            dry_run=env_bool("DRY_RUN", False),
            log_level=env_str("LOG_LEVEL", "INFO").upper(),
        )

    # ---- derived paths ----
    @property
    def state_path(self) -> Path:
        return Path(self.state_dir) / "sidecar-state.sqlite3"

    @property
    def sidecar_assets_dir(self) -> Path:
        return Path(self.sidecar_dir) / "assets"

    @property
    def library_dir(self) -> Path:
        return Path(self.sidecar_dir) / "library"

    @property
    def reports_dir(self) -> Path:
        return Path(self.sidecar_dir) / "reports"

    @property
    def cache_dir(self) -> Path:
        return Path(self.work_dir) / "cache"

    @property
    def stage_dir(self) -> Path:
        return Path(self.work_dir) / "stage"

    # ---- derived switches ----
    @property
    def wants_embed(self) -> bool:
        return self.metadata_backend in ("embed", "both")

    @property
    def wants_library_api(self) -> bool:
        return self.metadata_backend in ("library_api", "both") or self.album_backend == "library_api"

    @property
    def gphotos_configured(self) -> bool:
        return bool(self.gphotos_client_id and self.gphotos_client_secret and self.gphotos_refresh_token)

    @property
    def gpmc_configured(self) -> bool:
        return bool(self.gpmc_auth_data)

    @property
    def gpmc_effective_log_level(self) -> str:
        """Log level handed to gpmc (its INFO level is very chatty)."""
        if self.gpmc_log_level:
            return self.gpmc_log_level
        return "DEBUG" if self.log_level == "DEBUG" else "ERROR"

    def ensure_dirs(self) -> None:
        for path in (
            Path(self.state_dir),
            self.sidecar_assets_dir,
            self.library_dir,
            self.reports_dir,
            self.cache_dir,
            self.stage_dir,
            Path(self.gpmc_cache_dir),
        ):
            path.mkdir(parents=True, exist_ok=True)

    def problems(self) -> List[str]:
        issues: List[str] = []
        if not self.immich_base_url:
            issues.append("IMMICH_BASE_URL is required")
        if not self.immich_api_key:
            issues.append("IMMICH_API_KEY is required")
        if not self.gpmc_configured and not self.dry_run:
            issues.append("GPMC_AUTH_DATA is required (or run with --dry-run)")
        if self.album_backend not in ALBUM_BACKENDS:
            issues.append(f"ALBUM_BACKEND must be one of {ALBUM_BACKENDS}")
        if self.metadata_backend not in METADATA_BACKENDS:
            issues.append(f"METADATA_BACKEND must be one of {METADATA_BACKENDS}")
        if "{album}" not in self.album_name_template:
            issues.append("ALBUM_NAME_TEMPLATE must contain the {album} placeholder")
        if self.wants_library_api and not self.gphotos_configured:
            issues.append(
                "GPHOTOS_CLIENT_ID / GPHOTOS_CLIENT_SECRET / GPHOTOS_REFRESH_TOKEN are "
                "required for library_api backends"
            )
        if self.page_size < 1 or self.page_size > 1000:
            issues.append("PAGE_SIZE must be between 1 and 1000")
        if self.gpmc_threads < 1:
            issues.append("GPMC_THREADS must be 1 or more")
        return issues


def normalize_album_backend(value: str) -> str:
    backend = (value or "").strip().lower()
    return ALBUM_BACKEND_ALIASES.get(backend, backend)
