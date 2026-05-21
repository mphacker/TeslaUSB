from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from teslausb_web.services.cloud_archive.service import (
    CloudArchiveService,
    make_cloud_archive_service,
)
from teslausb_web.services.cloud_archive.settings import CloudArchiveConfig
from teslausb_web.services.cloud_archive_migrations import open_db
from teslausb_web.services.cloud_archive_queries import CloudArchiveQueries

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


def test_service_exposes_queries_and_queue_facade(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    with open_db(config.db_path):
        pass
    service = CloudArchiveService(
        config=config,
        rclone_service=MagicMock(),
        oauth_service=MagicMock(),
    )

    assert isinstance(service.queries, CloudArchiveQueries)
    assert service.get_sync_queue() == ()
    assert service.get_cloud_shadow_telemetry().pipeline_enqueue_count == 0


def test_service_start_and_shutdown_worker(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    with open_db(config.db_path):
        pass
    oauth = MagicMock()
    oauth.load_credentials.return_value = None
    rclone = MagicMock()
    service = CloudArchiveService(config=config, rclone_service=rclone, oauth_service=oauth)

    assert service.start() is True
    assert service.state.thread is not None
    assert service.stop(timeout=2.0) is True


def test_make_cloud_archive_service_accepts_cloud_config(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    with open_db(config.db_path):
        pass

    service = make_cloud_archive_service(config, MagicMock(), MagicMock())

    assert isinstance(service, CloudArchiveService)
