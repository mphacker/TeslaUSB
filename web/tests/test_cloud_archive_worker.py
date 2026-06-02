from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from teslausb_web.services.cloud_archive.service import CloudArchiveService
from teslausb_web.services.cloud_archive.settings import CloudArchiveConfig
from teslausb_web.services.cloud_archive.worker import SyncStatus, WorkerState
from teslausb_web.services.cloud_archive_migrations import open_db

if TYPE_CHECKING:
    from pathlib import Path


def _make_config(tmp_path: Path) -> CloudArchiveConfig:
    teslacam = tmp_path / "TeslaCam"
    teslacam.mkdir(exist_ok=True)
    return CloudArchiveConfig(
        enabled=True,
        db_path=tmp_path / "cloud.db",
        teslacam_path=teslacam,
        mapping_db_path=tmp_path / "mapping.db",
        worker_idle_seconds=0.05,
        backoff_initial_seconds=0.01,
        backoff_max_seconds=0.05,
    )


def test_worker_state_snapshot_defaults() -> None:
    snapshot = WorkerState().snapshot()
    assert isinstance(snapshot, SyncStatus)
    assert snapshot.running is False
    assert snapshot.files_total == 0


def test_worker_start_sync_requires_credentials(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    with open_db(config.db_path):
        pass
    oauth = MagicMock()
    oauth.load_credentials.return_value = None
    rclone = MagicMock()
    rclone.has_configured_remote.return_value = False
    service = CloudArchiveService(config=config, rclone_service=rclone, oauth_service=oauth)

    ok, message = service.start_sync()

    assert ok is False
    assert message == "No cloud provider configured"
    rclone.has_configured_remote.assert_called_once_with()


def test_worker_start_wake_and_stop(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    with open_db(config.db_path):
        pass
    oauth = MagicMock()
    oauth.load_credentials.return_value = None
    service = CloudArchiveService(config=config, rclone_service=MagicMock(), oauth_service=oauth)

    assert service.start() is True
    service.wake()
    assert service.state.wake_event.is_set()
    assert service.stop(timeout=2.0) is True
