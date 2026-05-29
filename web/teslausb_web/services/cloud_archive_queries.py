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

    def get_sync_queue(
        self,
        limit: int | None = None,
        folder_order: tuple[str, ...] = (),
    ) -> tuple[QueueItem, ...]:
        """Return active queue rows in the order the uploader will drain them.

        Ordering mirrors discovery's drain order: per-row ``priority`` first
        (live events / hard-brake clips float to the top regardless of
        folder), then the operator-configured folder priority
        (``folder_order``, e.g. ``("SentryClips", "SavedClips",
        "RecentClips")``), then insertion order. Passing ``folder_order``
        lets the Sync Queue panel reflect a changed "Upload priority"
        setting immediately, instead of always grouping by insertion id.
        """
        params: list[object] = []
        order_terms = ["priority DESC"]
        if folder_order:
            # Map the top-level folder of each file_path to its configured
            # rank. ``file_path || '/'`` guarantees instr finds a separator
            # even for a bare root, so the substr yields the folder name.
            folder_expr = "substr(file_path, 1, instr(file_path || '/', '/') - 1)"
            when_clauses = []
            for rank, folder in enumerate(folder_order):
                when_clauses.append("WHEN ? THEN ?")
                params.extend((folder, rank))
            params.append(len(folder_order))
            order_terms.append(
                f"CASE {folder_expr} {' '.join(when_clauses)} ELSE ? END ASC"
            )
        order_terms.append("id ASC")
        # order_terms is built only from constants and bound '?' placeholders
        # (folder ranks); no caller-supplied text is interpolated.
        sql = (
            "SELECT file_path, file_size, status, retry_count, last_error, priority "  # noqa: S608
            "FROM cloud_synced_files WHERE status IN ('queued', 'pending', 'uploading') "
            f"ORDER BY {', '.join(order_terms)}"
        )
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        with open_db(self._config.db_path) as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
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

    def count_sync_queue(self) -> tuple[int, int]:
        """Return (total_active, priority_active) — cheap aggregate for the
        queue badge so the UI can show full counts even when ``get_sync_queue``
        is called with a render-cap ``limit``."""
        with open_db(self._config.db_path) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN priority > 0 THEN 1 ELSE 0 END) AS priority_count "
                "FROM cloud_synced_files "
                "WHERE status IN ('queued', 'pending', 'uploading')"
            ).fetchone()
        total = int(row["total"] or 0) if row is not None else 0
        priority_count = int(row["priority_count"] or 0) if row is not None else 0
        return total, priority_count


def make_cloud_archive_queries(
    cfg: WebConfig | CloudArchiveQueriesConfig,
) -> CloudArchiveQueries:
    if isinstance(cfg, CloudArchiveQueriesConfig):
        return CloudArchiveQueries(cfg)
    return CloudArchiveQueries(CloudArchiveQueriesConfig(db_path=cfg.cloud.db_path))
