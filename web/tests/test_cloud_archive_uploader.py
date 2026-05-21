from __future__ import annotations

import threading
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from teslausb_web.services.cloud_archive.uploader import (
    UploadFailedError,
    UploadResult,
    _mark_upload_failure,
    upload_path_via_rclone,
)
from teslausb_web.services.cloud_archive_migrations import open_db

if TYPE_CHECKING:
    from pathlib import Path


def test_upload_path_via_rclone_success(tmp_path: Path) -> None:
    file_path = tmp_path / "clip.mp4"
    file_path.write_bytes(b"video")
    rclone = MagicMock()
    rclone.transfer.return_value = MagicMock(cancelled=False)

    result = upload_path_via_rclone(rclone, file_path, "SentryClips/clip.mp4")

    assert result == UploadResult(success=True, bytes_transferred=len(b"video"), status="synced")


def test_upload_path_via_rclone_honours_cancel_event(tmp_path: Path) -> None:
    file_path = tmp_path / "clip.mp4"
    file_path.write_bytes(b"video")
    cancel_event = threading.Event()
    cancel_event.set()
    rclone = MagicMock()

    result = upload_path_via_rclone(rclone, file_path, "SentryClips/clip.mp4", cancel_event)

    assert result.cancelled is True
    rclone.transfer.assert_not_called()


def test_upload_path_via_rclone_wraps_transfer_errors(tmp_path: Path) -> None:
    file_path = tmp_path / "clip.mp4"
    file_path.write_bytes(b"video")
    rclone = MagicMock()
    rclone.transfer.side_effect = RuntimeError("boom")

    with pytest.raises(UploadFailedError):
        upload_path_via_rclone(rclone, file_path, "SentryClips/clip.mp4")


def test_mark_upload_failure_dead_letters_after_retry_limit(tmp_path: Path) -> None:
    db_path = tmp_path / "cloud.db"
    with open_db(db_path) as connection:
        connection.execute(
            "INSERT INTO cloud_synced_files (file_path, status, retry_count) "
            "VALUES ('SentryClips/fail', 'pending', 2)"
        )
        connection.commit()
        result = _mark_upload_failure(connection, "SentryClips/fail", "boom", 3)

    assert result.dead_lettered is True
    assert result.status == "dead_letter"
