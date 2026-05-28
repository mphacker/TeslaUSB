"""Queue mutation helpers for cloud archive."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from teslausb_web.services.cloud_archive.paths import canonical_cloud_path
from teslausb_web.services.cloud_archive_migrations import open_db

if TYPE_CHECKING:
    from teslausb_web.services.cloud_archive.settings import CloudArchiveConfig
    from teslausb_web.services.cloud_archive_queries import (
        CloudArchiveQueries,
        DeadLetterEntry,
        QueueItem,
    )


def queue_event_for_sync(
    config: CloudArchiveConfig,
    folder: str,
    event_name: str,
    *,
    priority: bool = False,
) -> tuple[bool, str]:
    relative_path = canonical_cloud_path(f"{folder}/{event_name}")
    local_path = config.teslacam_path / Path(relative_path)
    if not local_path.exists():
        return False, "TeslaCam event not found"
    if local_path.is_dir():
        size_bytes = sum(child.stat().st_size for child in local_path.rglob("*") if child.is_file())
    else:
        size_bytes = local_path.stat().st_size
    queued_at = datetime.now(UTC).isoformat()
    with open_db(config.db_path) as connection:
        existing = connection.execute(
            "SELECT status FROM cloud_synced_files WHERE file_path = ?",
            (relative_path,),
        ).fetchone()
        if existing is not None and str(existing[0]) == "synced":
            return True, "All files already synced or queued"
        connection.execute(
            (
                "INSERT INTO cloud_synced_files ("
                "file_path, file_size, file_mtime, status, retry_count, added_at"
                ") VALUES (?, ?, ?, 'queued', 0, ?) "
                "ON CONFLICT(file_path) DO UPDATE SET "
                "file_size = excluded.file_size, "
                "file_mtime = excluded.file_mtime, "
                "status = 'queued', "
                "retry_count = CASE WHEN ? THEN 0 ELSE retry_count END, "
                "added_at = excluded.added_at"
            ),
            (
                relative_path,
                size_bytes,
                local_path.stat().st_mtime,
                queued_at,
                priority,
            ),
        )
        connection.commit()
    return True, "Added 1 file to sync queue"


def get_sync_queue(queries: CloudArchiveQueries) -> tuple[QueueItem, ...]:
    return queries.get_sync_queue()


def remove_from_queue(config: CloudArchiveConfig, file_path: str) -> tuple[bool, str]:
    try:
        canonical = canonical_cloud_path(file_path)
    except ValueError:
        return False, "Invalid path"
    with open_db(config.db_path) as connection:
        rowcount = connection.execute(
            "DELETE FROM cloud_synced_files WHERE file_path = ? AND status != 'synced'",
            (canonical,),
        ).rowcount
        connection.commit()
    return True, "Removed from queue" if rowcount else "Not in queue"


def clear_queue(config: CloudArchiveConfig) -> tuple[bool, str]:
    with open_db(config.db_path) as connection:
        rowcount = connection.execute(
            "DELETE FROM cloud_synced_files WHERE status != 'synced'"
        ).rowcount
        connection.commit()
    return True, f"Cleared {rowcount} items from queue"


def list_dead_letters(
    queries: CloudArchiveQueries,
    limit: int = 100,
) -> tuple[DeadLetterEntry, ...]:
    return queries.list_dead_letters(limit)


def retry_dead_letter(config: CloudArchiveConfig, file_path: str | None = None) -> int:
    with open_db(config.db_path) as connection:
        if file_path is None:
            rowcount = connection.execute(
                "UPDATE cloud_synced_files SET status = 'pending', retry_count = 0 "
                "WHERE status = 'dead_letter'"
            ).rowcount
        else:
            try:
                canonical = canonical_cloud_path(file_path)
            except ValueError:
                return 0
            rowcount = connection.execute(
                (
                    "UPDATE cloud_synced_files SET status = 'pending', retry_count = 0 "
                    "WHERE status = 'dead_letter' AND file_path = ?"
                ),
                (canonical,),
            ).rowcount
        connection.commit()
    return rowcount


def delete_dead_letter(config: CloudArchiveConfig, file_path: str | None = None) -> int:
    with open_db(config.db_path) as connection:
        if file_path is None:
            rowcount = connection.execute(
                "DELETE FROM cloud_synced_files WHERE status = 'dead_letter'"
            ).rowcount
        else:
            try:
                canonical = canonical_cloud_path(file_path)
            except ValueError:
                return 0
            rowcount = connection.execute(
                "DELETE FROM cloud_synced_files WHERE status = 'dead_letter' AND file_path = ?",
                (canonical,),
            ).rowcount
        connection.commit()
    return rowcount


def recover_interrupted_uploads(config: CloudArchiveConfig) -> int:
    with open_db(config.db_path) as connection:
        rowcount = connection.execute(
            "UPDATE cloud_synced_files SET status = 'pending' WHERE status = 'uploading'"
        ).rowcount
        connection.execute(
            "UPDATE cloud_sync_sessions SET status = 'interrupted', "
            "ended_at = ?, error_msg = COALESCE(error_msg, "
            "'Worker stopped before session finished') "
            "WHERE status = 'running' AND ended_at IS NULL",
            (datetime.now(UTC).isoformat(),),
        )
        connection.commit()
    return rowcount
