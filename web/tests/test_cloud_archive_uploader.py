from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from teslausb_web.services.cloud_archive import uploader as uploader_module
from teslausb_web.services.cloud_archive.discovery import EventCandidate
from teslausb_web.services.cloud_archive.uploader import (
    DEGRADED_BWLIMIT_KBPS,
    DEGRADED_INTER_FILE_COOLDOWN_SECONDS,
    INTER_FILE_COOLDOWN_SECONDS,
    UploadFailedError,
    UploadResult,
    _after_upload,
    _DrainAccount,
    _DrainSignal,
    _higher_priority_pending,
    _mark_upload_failure,
    _reserve_allows,
    _sync_degraded_throttle,
    _wifi_degraded,
    upload_path_via_rclone,
)
from teslausb_web.services.cloud_archive_migrations import open_db

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator


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


def test_higher_priority_pending_detects_priority_row(tmp_path: Path) -> None:
    db_path = tmp_path / "cloud.db"
    with open_db(db_path) as connection:
        connection.executemany(
            "INSERT INTO cloud_synced_files (file_path, status, priority) VALUES (?, ?, ?)",
            [
                ("RecentClips/bulk1", "pending", 0),
                ("RecentClips/priority1", "pending", 10),
                ("RecentClips/already_synced", "synced", 10),
            ],
        )
        connection.commit()

    service = MagicMock()
    service.open_db.return_value.__enter__.return_value = open_db(db_path).__enter__()
    # Simpler: stub open_db to actually open the file.
    from contextlib import contextmanager

    @contextmanager
    def _opener() -> Iterator[sqlite3.Connection]:
        with open_db(db_path) as conn:
            yield conn

    service.open_db = _opener

    assert _higher_priority_pending(service, current_priority=0) is True
    assert _higher_priority_pending(service, current_priority=10) is False
    assert _higher_priority_pending(service, current_priority=20) is False


def test_wifi_degraded_reflects_flag_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flag = tmp_path / "wifi_degraded"
    monkeypatch.setattr(uploader_module, "_DEGRADED_FLAG", str(flag))
    assert _wifi_degraded() is False
    flag.write_text("", encoding="utf-8")
    assert _wifi_degraded() is True
    flag.unlink()
    assert _wifi_degraded() is False


def test_degraded_throttle_is_gentler_than_healthy_path() -> None:
    # The degraded path must back off harder than the healthy path:
    # a longer inter-file gap and a positive (capped) bandwidth limit.
    assert DEGRADED_INTER_FILE_COOLDOWN_SECONDS > INTER_FILE_COOLDOWN_SECONDS
    assert DEGRADED_BWLIMIT_KBPS > 0


def test_sync_degraded_throttle_applies_and_clears_on_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MagicMock()
    monkeypatch.setattr(uploader_module, "_wifi_degraded", lambda: True)
    # Healthy -> degraded: caps bandwidth and reports the new state.
    assert _sync_degraded_throttle(service, active=False) is True
    service.rclone_service.set_degraded_bwlimit_kbps.assert_called_once_with(DEGRADED_BWLIMIT_KBPS)

    service.rclone_service.set_degraded_bwlimit_kbps.reset_mock()
    monkeypatch.setattr(uploader_module, "_wifi_degraded", lambda: False)
    # Degraded -> healthy: clears the override.
    assert _sync_degraded_throttle(service, active=True) is False
    service.rclone_service.set_degraded_bwlimit_kbps.assert_called_once_with(None)


def test_sync_degraded_throttle_is_noop_without_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MagicMock()
    monkeypatch.setattr(uploader_module, "_wifi_degraded", lambda: True)
    assert _sync_degraded_throttle(service, active=True) is True
    service.rclone_service.set_degraded_bwlimit_kbps.assert_not_called()


def _candidate(size_bytes: int) -> EventCandidate:
    return EventCandidate(
        local_path=Path("/srv/teslausb/teslacam/clip.mp4"),
        relative_path="SentryClips/clip.mp4",
        size_bytes=size_bytes,
        score=0,
    )


def test_reserve_allows_passes_when_no_reserve_configured() -> None:
    service = MagicMock()
    account = _DrainAccount(running_free=None, reserve_bytes=0)
    assert _reserve_allows(service, _candidate(10), account) is True


def test_reserve_allows_blocks_when_upload_would_breach_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MagicMock()
    account = _DrainAccount(running_free=100, reserve_bytes=80)
    monkeypatch.setattr(
        uploader_module,
        "ensure_remote_headroom",
        lambda _service, _bytes: SimpleNamespace(
            ok=False, free_bytes=100, reserve_bytes=80, reason="reserve"
        ),
    )
    # Uploading 50 bytes would leave 50 < reserve(80): gate must stop the drain.
    assert _reserve_allows(service, _candidate(50), account) is False


def test_after_upload_maps_results(monkeypatch: pytest.MonkeyPatch) -> None:
    service = MagicMock()
    account = _DrainAccount(running_free=None, reserve_bytes=0)
    # _wait_with_events returning False means "not interrupted".
    monkeypatch.setattr(uploader_module, "_wait_with_events", lambda *_: False)
    monkeypatch.setattr(uploader_module, "_backoff_seconds", lambda *_: 0.0)

    assert (
        _after_upload(service, account, UploadResult(success=False, cancelled=True))
        is _DrainSignal.BREAK
    )
    assert (
        _after_upload(service, account, UploadResult(success=True, bytes_transferred=5))
        is _DrainSignal.CONTINUE
    )
    assert account.files_synced == 1
    assert account.bytes_transferred == 5
    assert (
        _after_upload(service, account, UploadResult(success=False, status="failed", retry_count=1))
        is _DrainSignal.CONTINUE
    )


def test_after_upload_breaks_when_failure_backoff_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MagicMock()
    account = _DrainAccount(running_free=None, reserve_bytes=0)
    # _wait_with_events returning True means stop/cancel fired during backoff.
    monkeypatch.setattr(uploader_module, "_wait_with_events", lambda *_: True)
    monkeypatch.setattr(uploader_module, "_backoff_seconds", lambda *_: 0.0)
    result = UploadResult(success=False, status="failed", retry_count=1)
    assert _after_upload(service, account, result) is _DrainSignal.BREAK
