from __future__ import annotations

from typing import TYPE_CHECKING

from teslausb_web.services.cloud_archive_migrations import open_db
from teslausb_web.services.cloud_archive_queries import (
    CloudArchiveQueries,
    CloudArchiveQueriesConfig,
    SyncStats,
)

if TYPE_CHECKING:
    from pathlib import Path


def _insert(
    db_path: Path,
    file_path: str,
    status: str,
    *,
    file_size: int | None = None,
    synced_at: str | None = None,
) -> None:
    with open_db(db_path) as connection:
        connection.execute(
            (
                "INSERT INTO cloud_synced_files ("
                "file_path, status, retry_count, file_size, synced_at"
                ") VALUES (?, ?, 0, ?, ?)"
            ),
            (file_path, status, file_size, synced_at),
        )
        connection.commit()


def test_queries_report_sync_stats(tmp_path: Path) -> None:
    db_path = tmp_path / "cloud.db"
    with open_db(db_path):
        pass
    queries = CloudArchiveQueries(CloudArchiveQueriesConfig(db_path=db_path))
    _insert(db_path, "SentryClips/a", "synced", file_size=10)
    _insert(db_path, "SentryClips/b", "pending")
    _insert(db_path, "SentryClips/c", "failed")
    _insert(db_path, "SentryClips/d", "dead_letter")

    stats = queries.get_sync_stats()

    assert isinstance(stats, SyncStats)
    assert stats.total_synced == 1
    assert stats.total_pending == 1
    assert stats.total_failed == 2
    assert stats.total_dead_letter == 1
    assert stats.total_bytes == 10


def test_queries_manage_stats_baseline(tmp_path: Path) -> None:
    db_path = tmp_path / "cloud.db"
    with open_db(db_path):
        pass
    queries = CloudArchiveQueries(CloudArchiveQueriesConfig(db_path=db_path))

    ok, baseline = queries.reset_stats_baseline()

    assert ok is True
    assert queries.get_stats_baseline() == baseline


def test_queries_return_history_dead_letters_and_event_status(tmp_path: Path) -> None:
    db_path = tmp_path / "cloud.db"
    with open_db(db_path) as connection:
        connection.execute(
            "INSERT INTO cloud_sync_sessions ("
            "started_at, files_synced, bytes_transferred, status"
            ") VALUES ('2026-01-01T00:00:00Z', 2, 20, 'completed')"
        )
        connection.execute(
            "INSERT INTO cloud_synced_files (file_path, status, retry_count) "
            "VALUES ('SentryClips/dl', 'dead_letter', 5)"
        )
        connection.execute(
            "INSERT INTO cloud_synced_files (file_path, status, retry_count) "
            "VALUES ('SentryClips/2026-01-01_10-00-00', 'synced', 0)"
        )
        connection.commit()
    queries = CloudArchiveQueries(CloudArchiveQueriesConfig(db_path=db_path))

    history = queries.get_sync_history()
    dead_letters = queries.list_dead_letters()
    statuses = queries.get_sync_status_for_events(["2026-01-01_10-00-00"])

    assert len(history) == 1
    assert dead_letters[0].file_path == "SentryClips/dl"
    assert queries.count_dead_letters() == 1
    assert statuses["2026-01-01_10-00-00"] == "synced"
