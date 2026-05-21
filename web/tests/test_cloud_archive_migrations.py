from __future__ import annotations

from typing import TYPE_CHECKING

from teslausb_web.services.cloud_archive_migrations import (
    CLOUD_SCHEMA_VERSION,
    open_db,
    recover_startup_state,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_open_db_creates_expected_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "cloud.db"
    with open_db(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"cloud_synced_files", "cloud_sync_sessions", "cloud_archive_meta"} <= tables


def test_open_db_sets_schema_version(tmp_path: Path) -> None:
    db_path = tmp_path / "cloud.db"
    with open_db(db_path) as connection:
        row = connection.execute(
            "SELECT version FROM module_versions WHERE module = 'cloud_archive'"
        ).fetchone()
    assert row is not None
    assert row[0] == CLOUD_SCHEMA_VERSION


def test_open_db_uses_row_factory(tmp_path: Path) -> None:
    db_path = tmp_path / "cloud.db"
    with open_db(db_path) as connection:
        connection.execute(
            "INSERT INTO cloud_synced_files (file_path, status, retry_count) "
            "VALUES ('SentryClips/x', 'pending', 0)"
        )
        connection.commit()
        row = connection.execute("SELECT file_path FROM cloud_synced_files").fetchone()
    assert row["file_path"] == "SentryClips/x"


def test_recover_startup_state_marks_running_and_uploading_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "cloud.db"
    with open_db(db_path) as connection:
        connection.execute(
            "INSERT INTO cloud_sync_sessions (started_at, status) "
            "VALUES ('2026-01-01T00:00:00Z', 'running')"
        )
        connection.execute(
            "INSERT INTO cloud_synced_files (file_path, status, retry_count) "
            "VALUES ('SentryClips/x', 'uploading', 0)"
        )
        connection.commit()
        session_count, upload_count, paths = recover_startup_state(connection)
        session_row = connection.execute("SELECT status FROM cloud_sync_sessions").fetchone()
        file_row = connection.execute("SELECT status FROM cloud_synced_files").fetchone()
    assert session_count == 1
    assert upload_count == 1
    assert paths == ("SentryClips/x",)
    assert session_row["status"] == "interrupted"
    assert file_row["status"] == "pending"
