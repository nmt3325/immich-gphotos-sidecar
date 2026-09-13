"""SQLite backed sync state.

The state file is the only thing that makes the sidecar incremental; it maps
Immich asset ids to Google Photos media keys and remembers which album links
have already been created.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS assets (
    asset_id           TEXT PRIMARY KEY,
    checksum           TEXT,
    original_file_name TEXT,
    asset_type         TEXT,
    immich_updated_at  TEXT,
    media_key          TEXT,
    uploaded_at        TEXT,
    sidecar_hash       TEXT,
    sidecar_written_at TEXT,
    status             TEXT,
    last_error         TEXT,
    attempts           INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS assets_status_idx ON assets(status);
CREATE TABLE IF NOT EXISTS albums (
    album_id      TEXT PRIMARY KEY,
    name          TEXT,
    gp_album_name TEXT,
    gp_album_key  TEXT,
    asset_count   INTEGER,
    updated_at    TEXT,
    synced_at     TEXT
);
CREATE TABLE IF NOT EXISTS album_links (
    album_id  TEXT NOT NULL,
    asset_id  TEXT NOT NULL,
    media_key TEXT,
    added_at  TEXT,
    PRIMARY KEY (album_id, asset_id)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    started_at TEXT,
    ended_at   TEXT,
    report     TEXT
);
"""


class State:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- meta ----
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    # ---- assets ----
    def get_asset(self, asset_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM assets WHERE asset_id = ?", (asset_id,)
            ).fetchone()
        return dict(row) if row else None

    def upsert_asset(self, asset_id: str, **fields: Any) -> None:
        if not fields:
            return
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        updates = ", ".join(f"{name} = excluded.{name}" for name in fields)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO assets(asset_id, {columns}) VALUES(?, {placeholders}) "
                f"ON CONFLICT(asset_id) DO UPDATE SET {updates}",
                (asset_id, *fields.values()),
            )
            self._conn.commit()

    def bump_attempt(self, asset_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE assets SET attempts = COALESCE(attempts, 0) + 1, "
                "last_error = ?, status = 'error' WHERE asset_id = ?",
                (error[:2000], asset_id),
            )
            self._conn.commit()

    def media_key(self, asset_id: str) -> Optional[str]:
        row = self.get_asset(asset_id)
        return row.get("media_key") if row else None

    def uploaded_asset_ids(self) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT asset_id FROM assets WHERE media_key IS NOT NULL AND media_key != ''"
            ).fetchall()
        return [row["asset_id"] for row in rows]

    def pending_asset_ids(self, max_attempts: int = 5, limit: int = 500) -> List[str]:
        """Assets that are known but not uploaded yet (retry candidates)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT asset_id FROM assets WHERE (media_key IS NULL OR media_key = '') "
                "AND COALESCE(attempts, 0) < ? ORDER BY COALESCE(attempts, 0), asset_id LIMIT ?",
                (max_attempts, limit),
            ).fetchall()
        return [row["asset_id"] for row in rows]

    # ---- albums ----
    def upsert_album(self, album_id: str, **fields: Any) -> None:
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        updates = ", ".join(f"{name} = excluded.{name}" for name in fields)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO albums(album_id, {columns}) VALUES(?, {placeholders}) "
                f"ON CONFLICT(album_id) DO UPDATE SET {updates}",
                (album_id, *fields.values()),
            )
            self._conn.commit()

    def album(self, album_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM albums WHERE album_id = ?", (album_id,)
            ).fetchone()
        return dict(row) if row else None

    def has_album_link(self, album_id: str, asset_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM album_links WHERE album_id = ? AND asset_id = ?",
                (album_id, asset_id),
            ).fetchone()
        return row is not None

    def linked_asset_ids(self, album_id: str) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT asset_id FROM album_links WHERE album_id = ?", (album_id,)
            ).fetchall()
        return [row["asset_id"] for row in rows]

    def add_album_link(self, album_id: str, asset_id: str, media_key: Optional[str], added_at: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO album_links(album_id, asset_id, media_key, added_at) "
                "VALUES(?, ?, ?, ?) ON CONFLICT(album_id, asset_id) DO UPDATE SET "
                "media_key = excluded.media_key, added_at = excluded.added_at",
                (album_id, asset_id, media_key, added_at),
            )
            self._conn.commit()

    # ---- runs / reporting ----
    def record_run(self, run_id: str, started_at: str, ended_at: str, report: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs(run_id, started_at, ended_at, report) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET ended_at = excluded.ended_at, report = excluded.report",
                (run_id, started_at, ended_at, json.dumps(report, ensure_ascii=False)),
            )
            self._conn.commit()

    def last_runs(self, limit: int = 5) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) AS c FROM assets").fetchone()["c"]
            uploaded = self._conn.execute(
                "SELECT COUNT(*) AS c FROM assets WHERE media_key IS NOT NULL AND media_key != ''"
            ).fetchone()["c"]
            errors = self._conn.execute(
                "SELECT COUNT(*) AS c FROM assets WHERE status = 'error'"
            ).fetchone()["c"]
            albums = self._conn.execute("SELECT COUNT(*) AS c FROM albums").fetchone()["c"]
            links = self._conn.execute("SELECT COUNT(*) AS c FROM album_links").fetchone()["c"]
        return {
            "assets_known": total,
            "assets_uploaded": uploaded,
            "assets_error": errors,
            "albums": albums,
            "album_links": links,
            "watermark": self.get_meta("watermark"),
        }

    def iter_manifest(self) -> Iterable[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT asset_id, checksum, original_file_name, asset_type, media_key, "
                "uploaded_at, immich_updated_at FROM assets ORDER BY uploaded_at"
            ).fetchall()
        for row in rows:
            yield dict(row)
