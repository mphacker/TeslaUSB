"""Read-only query helpers for cloud archive."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from teslausb_web.services.cloud_archive.kv import KV_KEY_STATS_BASELINE_AT, kv_get, kv_set
from teslausb_web.services.cloud_archive_migrations import open_db

if TYPE_CHECKING:
    from pathlib import Path

    from teslausb_web.config import WebConfig
    from teslausb_web.services.cloud_archive.worker import SyncStatus

_DEFAULT_HISTORY_LIMIT: Final[int] = 20
_DEFAULT_DEAD_LETTER_LIMIT: Final[int] = 100


@dataclass(frozen=True, slots=True)
class CloudArchiveQueriesConfig:
    db_path: Path


@dataclass(frozen=True, slots=True)
class SyncHistoryEntry:
    id: int
    started_at: str
    ended_at: str | None
    files_synced: int
    bytes_transferred: int
    status: str
    trigger: str | None
    error_msg: str | None


@dataclass(frozen=True, slots=True)
class SyncStats:
    total_synced: int
    total_pending: int
    total_failed: int
    total_dead_letter: int
    total_bytes: int
    stats_baseline_at: str | None


@dataclass(frozen=True, slots=True)
class QueueItem:
    file_path: str
    file_size: int | None
    status: str
    retry_count: int
    last_error: str | None
    priority: int = 0


@dataclass(frozen=True, slots=True)
class DeadLetterEntry:
    id: int
    file_path: str
    file_size: int | None
    retry_count: int
    last_error: str | None
    previous_last_error: str | None


class CloudArchiveQueries:
    """Read-only query facade over the cloud archive SQLite database."""

    def __init__(self, config: CloudArchiveQueriesConfig) -> None:
        self._config = config

    def get_sync_history(
        self,
        limit: int = _DEFAULT_HISTORY_LIMIT,
    ) -> tuple[SyncHistoryEntry, ...]:
        with open_db(self._config.db_path) as connection:
            rows = connection.execute(
                (
                    "SELECT id, started_at, ended_at, files_synced, bytes_transferred, "
                    "status, trigger, error_msg FROM cloud_sync_sessions "
                    "ORDER BY started_at DESC LIMIT ?"
                ),
                (max(1, limit),),
            ).fetchall()
        return tuple(
            SyncHistoryEntry(
                id=int(row["id"]),
                started_at=str(row["started_at"]),
                ended_at=row["ended_at"],
                files_synced=int(row["files_synced"] or 0),
                bytes_transferred=int(row["bytes_transferred"] or 0),
                status=str(row["status"]),
                trigger=row["trigger"],
                error_msg=row["error_msg"],
            )
            for row in rows
        )

    def get_stats_baseline(self) -> str | None:
        with open_db(self._config.db_path) as connection:
            return kv_get(connection, KV_KEY_STATS_BASELINE_AT)

    def reset_stats_baseline(self) -> tuple[bool, str]:
        baseline = datetime.now(UTC).isoformat()
        with open_db(self._config.db_path) as connection:
            kv_set(connection, KV_KEY_STATS_BASELINE_AT, baseline)
            connection.commit()
        return True, baseline

    def get_sync_stats(self, sync_status: SyncStatus | None = None) -> SyncStats:
        with open_db(self._config.db_path) as connection:
            baseline = kv_get(connection, KV_KEY_STATS_BASELINE_AT)
            counts = {
                status: int(
                    connection.execute(
                        "SELECT COUNT(*) FROM cloud_synced_files WHERE status = ?",
                        (status,),
                    ).fetchone()[0]
                )
                for status in ("pending", "queued", "uploading", "failed", "dead_letter")
            }
            if baseline is None:
                synced_row = connection.execute(
                    "SELECT COUNT(*) AS count, COALESCE(SUM(file_size), 0) AS total "
                    "FROM cloud_synced_files WHERE status = 'synced'"
                ).fetchone()
            else:
                synced_row = connection.execute(
                    (
                        "SELECT COUNT(*) AS count, COALESCE(SUM(file_size), 0) AS total "
                        "FROM cloud_synced_files WHERE status = 'synced' "
                        "AND (synced_at IS NULL OR synced_at > ?)"
                    ),
                    (baseline,),
                ).fetchone()
        db_pending = counts["pending"] + counts["queued"] + counts["uploading"]
        mem_pending = 0
        if sync_status is not None and sync_status.running:
            mem_pending = max(0, sync_status.files_total - sync_status.files_done)
        return SyncStats(
            total_synced=int(synced_row["count"]),
            total_pending=max(db_pending, mem_pending),
            total_failed=counts["failed"] + counts["dead_letter"],
            total_dead_letter=counts["dead_letter"],
            total_bytes=int(synced_row["total"]),
            stats_baseline_at=baseline,
        )

    def get_sync_status_for_events(self, event_names: list[str]) -> dict[str, str | None]:
        if not event_names:
            return {}
        statuses: dict[str, str | None] = dict.fromkeys(event_names)
        with open_db(self._config.db_path) as connection:
            for event_name in event_names:
                row = connection.execute(
                    (
                        "SELECT status FROM cloud_synced_files WHERE file_path LIKE ? "
                        "ORDER BY synced_at DESC LIMIT 1"
                    ),
                    (f"%{event_name}%",),
                ).fetchone()
                if row is not None:
                    statuses[event_name] = str(row["status"])
        return statuses

    def list_dead_letters(
        self, limit: int = _DEFAULT_DEAD_LETTER_LIMIT
    ) -> tuple[DeadLetterEntry, ...]:
        with open_db(self._config.db_path) as connection:
            rows = connection.execute(
                (
                    "SELECT id, file_path, file_size, retry_count, last_error, previous_last_error "
                    "FROM cloud_synced_files WHERE status = 'dead_letter' ORDER BY id ASC LIMIT ?"
                ),
                (max(0, limit),),
            ).fetchall()
        return tuple(
            DeadLetterEntry(
                id=int(row["id"]),
                file_path=str(row["file_path"]),
                file_size=row["file_size"],
                retry_count=int(row["retry_count"] or 0),
                last_error=row["last_error"],
                previous_last_error=row["previous_last_error"],
            )
            for row in rows
        )

    def count_dead_letters(self) -> int:
        with open_db(self._config.db_path) as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM cloud_synced_files WHERE status = 'dead_letter'"
                ).fetchone()[0]
            )

    def get_sync_queue(self) -> tuple[QueueItem, ...]:
        with open_db(self._config.db_path) as connection:
            rows = connection.execute(
                "SELECT file_path, file_size, status, retry_count, last_error, priority "
                "FROM cloud_synced_files WHERE status IN ('queued', 'pending', 'uploading') "
                "ORDER BY priority DESC, id ASC"
            ).fetchall()
        return tuple(
            QueueItem(
                file_path=str(row["file_path"]),
                file_size=row["file_size"],
                status=str(row["status"]),
                retry_count=int(row["retry_count"] or 0),
                last_error=row["last_error"],
                priority=int(row["priority"] or 0),
            )
            for row in rows
        )


def make_cloud_archive_queries(
    cfg: WebConfig | CloudArchiveQueriesConfig,
) -> CloudArchiveQueries:
    if isinstance(cfg, CloudArchiveQueriesConfig):
        return CloudArchiveQueries(cfg)
    return CloudArchiveQueries(CloudArchiveQueriesConfig(db_path=cfg.cloud.db_path))
