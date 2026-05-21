"""SQLite schema and migrations for cloud archive."""

from __future__ import annotations

import logging
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from teslausb_web.services.cloud_archive.paths import canonical_cloud_path
from teslausb_web.services.cloud_archive.reconcile import (
    _backfill_missing_live_event_mirrors,
)
from teslausb_web.services.cloud_archive.settings import CloudArchiveDBError

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

CLOUD_MODULE: Final[str] = "cloud_archive"
CLOUD_SCHEMA_VERSION: Final[int] = 5
MIGRATE_STATUS_PRIORITY: Final[dict[str, int]] = {
    "synced": 5,
    "dead_letter": 4,
    "failed": 3,
    "uploading": 2,
    "pending": 1,
    "queued": 0,
}
_SCHEMA_VERSION_CANONICALIZE_PATHS: Final[int] = 2
_SCHEMA_VERSION_PREVIOUS_LAST_ERROR: Final[int] = 3
_SCHEMA_VERSION_DROP_LIVE_EVENT_QUEUE: Final[int] = 4

_SCHEMA_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS module_versions (
    module TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS cloud_synced_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL UNIQUE,
    file_size INTEGER,
    file_mtime REAL,
    remote_path TEXT,
    status TEXT DEFAULT 'pending',
    added_at TEXT,
    synced_at TEXT,
    retry_count INTEGER DEFAULT 0,
    last_error TEXT,
    previous_last_error TEXT
);

CREATE TABLE IF NOT EXISTS cloud_sync_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    files_synced INTEGER DEFAULT 0,
    bytes_transferred INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running',
    trigger TEXT,
    window_mode TEXT,
    error_msg TEXT
);

CREATE TABLE IF NOT EXISTS cloud_archive_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_cloud_synced_status ON cloud_synced_files(status);
CREATE INDEX IF NOT EXISTS idx_cloud_synced_mtime ON cloud_synced_files(file_mtime);
CREATE INDEX IF NOT EXISTS idx_cloud_synced_synced_at ON cloud_synced_files(synced_at);
CREATE INDEX IF NOT EXISTS idx_cloud_sessions_started ON cloud_sync_sessions(started_at);
"""


@dataclass(frozen=True, slots=True)
class CloudArchiveDBConfig:
    db_path: Path
    mapping_db_path: Path | None = None


def _check_db_integrity(db_path: Path) -> bool:
    if not db_path.exists():
        return True
    try:
        connection = sqlite3.connect(str(db_path), timeout=5.0)
    except sqlite3.Error as exc:
        logger.warning("Integrity check could not open %s: %s", db_path, exc)
        return False
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        logger.warning("Integrity check failed for %s: %s", db_path, exc)
        return False
    finally:
        connection.close()
    return row is not None and row[0] == "ok"


def _handle_corrupt_db(db_path: Path) -> None:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    quarantine_path = db_path.with_name(f"{db_path.name}.corrupt.{timestamp}")
    try:
        db_path.rename(quarantine_path)
    except OSError:
        db_path.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        db_path.with_name(f"{db_path.name}{suffix}").unlink(missing_ok=True)


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _column_exists(connection: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(str(column[1]) == column_name for column in columns)


def _migrate_canonicalize_paths_v2(
    connection: sqlite3.Connection, db_path: Path
) -> tuple[int, int]:
    if db_path.exists():
        backup_path = db_path.with_name(f"{db_path.name}.bak.v2-canonical-paths")
        if not backup_path.exists():
            shutil.copy2(db_path, backup_path)
    prior_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT id, file_path, status FROM cloud_synced_files").fetchall()
    finally:
        connection.row_factory = prior_factory
    rewrites = 0
    merges = 0
    for row in rows:
        new_path = canonical_cloud_path(str(row["file_path"]))
        if not new_path or new_path == row["file_path"]:
            continue
        try:
            connection.execute(
                "UPDATE cloud_synced_files SET file_path = ? WHERE id = ?",
                (new_path, row["id"]),
            )
            rewrites += 1
        except sqlite3.IntegrityError:
            existing = connection.execute(
                "SELECT id, status FROM cloud_synced_files WHERE file_path = ?",
                (new_path,),
            ).fetchone()
            if existing is None:
                continue
            existing_priority = MIGRATE_STATUS_PRIORITY.get(str(existing[1]), 0)
            our_priority = MIGRATE_STATUS_PRIORITY.get(str(row["status"]), 0)
            if our_priority > existing_priority:
                connection.execute(
                    "DELETE FROM cloud_synced_files WHERE id = ?",
                    (existing[0],),
                )
                connection.execute(
                    "UPDATE cloud_synced_files SET file_path = ? WHERE id = ?",
                    (new_path, row["id"]),
                )
            else:
                connection.execute(
                    "DELETE FROM cloud_synced_files WHERE id = ?",
                    (row["id"],),
                )
            merges += 1
    return rewrites, merges


def _migrate_add_previous_last_error_v3(connection: sqlite3.Connection) -> None:
    if _column_exists(connection, "cloud_synced_files", "previous_last_error"):
        return
    connection.execute("ALTER TABLE cloud_synced_files ADD COLUMN previous_last_error TEXT")


def _migrate_drop_live_event_queue_v4(
    connection: sqlite3.Connection,
    mapping_db_path: Path | None,
) -> None:
    if not _table_exists(connection, "live_event_queue"):
        return
    if mapping_db_path is not None:
        _backfill_missing_live_event_mirrors(connection, mapping_db_path)
    connection.execute("DROP INDEX IF EXISTS idx_les_status")
    connection.execute("DROP INDEX IF EXISTS idx_les_next_retry")
    connection.execute("DROP TABLE IF EXISTS live_event_queue")


def _apply_pending_migrations(
    connection: sqlite3.Connection,
    config: CloudArchiveDBConfig,
) -> None:
    connection.executescript(_SCHEMA_SQL)
    row = connection.execute(
        "SELECT version FROM module_versions WHERE module = ?",
        (CLOUD_MODULE,),
    ).fetchone()
    current_version = int(row[0]) if row is not None else 0
    if current_version < _SCHEMA_VERSION_CANONICALIZE_PATHS:
        _migrate_canonicalize_paths_v2(connection, config.db_path)
    if current_version < _SCHEMA_VERSION_PREVIOUS_LAST_ERROR:
        _migrate_add_previous_last_error_v3(connection)
    if current_version < _SCHEMA_VERSION_DROP_LIVE_EVENT_QUEUE:
        _migrate_drop_live_event_queue_v4(connection, config.mapping_db_path)
    connection.execute(
        "INSERT OR REPLACE INTO module_versions (module, version, updated_at) VALUES (?, ?, ?)",
        (CLOUD_MODULE, CLOUD_SCHEMA_VERSION, datetime.now(UTC).isoformat()),
    )
    connection.commit()


def recover_startup_state(connection: sqlite3.Connection) -> tuple[int, int, tuple[str, ...]]:
    session_count = connection.execute(
        (
            "UPDATE cloud_sync_sessions SET status = 'interrupted', ended_at = ?, "
            "error_msg = 'Process restarted' WHERE status = 'running'"
        ),
        (datetime.now(UTC).isoformat(),),
    ).rowcount
    paths = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT file_path FROM cloud_synced_files WHERE status = 'uploading'"
        ).fetchall()
    )
    upload_count = connection.execute(
        "UPDATE cloud_synced_files SET status = 'pending' WHERE status = 'uploading'"
    ).rowcount
    if session_count or upload_count:
        connection.commit()
    return session_count, upload_count, paths


def _coerce_db_config(config_or_path: CloudArchiveDBConfig | Path | str) -> CloudArchiveDBConfig:
    if isinstance(config_or_path, CloudArchiveDBConfig):
        return config_or_path
    if isinstance(config_or_path, Path):
        return CloudArchiveDBConfig(db_path=config_or_path)
    return CloudArchiveDBConfig(db_path=Path(config_or_path))


@contextmanager
def open_db(config_or_path: CloudArchiveDBConfig | Path | str) -> Iterator[sqlite3.Connection]:
    config = _coerce_db_config(config_or_path)
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    if not _check_db_integrity(config.db_path):
        _handle_corrupt_db(config.db_path)
    try:
        connection = sqlite3.connect(str(config.db_path), timeout=10.0)
    except sqlite3.Error as exc:
        raise CloudArchiveDBError(f"Failed to open cloud archive database: {exc}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        _apply_pending_migrations(connection, config)
        yield connection
    except sqlite3.Error as exc:
        connection.rollback()
        raise CloudArchiveDBError(f"Cloud archive database error: {exc}") from exc
    finally:
        connection.close()
