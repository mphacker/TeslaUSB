from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from teslausb_web.services.cloud_archive.reconcile import _reconcile_with_remote
from teslausb_web.services.cloud_archive_migrations import open_db

if TYPE_CHECKING:
    from pathlib import Path


def _listing(*entries: tuple[str, bool]) -> SimpleNamespace:
    return SimpleNamespace(
        entries=[SimpleNamespace(name=name, is_dir=is_dir) for name, is_dir in entries]
    )


def test_reconcile_marks_pending_rows_synced(tmp_path: Path) -> None:
    db_path = tmp_path / "cloud.db"
    with open_db(db_path) as connection:
        connection.execute(
            "INSERT INTO cloud_synced_files (file_path, status, retry_count) VALUES "
            "('SentryClips/2026-01-01_10-00-00', 'pending', 0)"
        )
        connection.commit()
        rclone = MagicMock()
        rclone.list_directory.side_effect = [
            _listing(("2026-01-01_10-00-00", True)),
            _listing(),
        ]
        rclone.list_files.return_value = _listing()

        summary = _reconcile_with_remote(connection, rclone)
        row = connection.execute(
            "SELECT status FROM cloud_synced_files WHERE "
            "file_path = 'SentryClips/2026-01-01_10-00-00'"
        ).fetchone()

    assert summary.reconciled == 1
    assert row["status"] == "synced"


def test_reconcile_inserts_missing_remote_archived_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "cloud.db"
    with open_db(db_path) as connection:
        rclone = MagicMock()
        rclone.list_directory.side_effect = [_listing(), _listing()]
        rclone.list_files.return_value = _listing(("clip.mp4", False))

        summary = _reconcile_with_remote(connection, rclone)
        row = connection.execute(
            "SELECT status FROM cloud_synced_files WHERE file_path = 'ArchivedClips/clip.mp4'"
        ).fetchone()

    assert summary.inserted == 1
    assert row["status"] == "synced"
