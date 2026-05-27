# ruff: noqa: ANN001  # pytest fixture injection.
"""Tests for the storage_health blueprint."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from teslausb_web.app import create_app
from teslausb_web.config import PathsSection, SystemSettingsSection, WebConfig, WebSection
from teslausb_web.services.storage_health_service import (
    SEV_CRITICAL,
    SEV_OK,
    StorageHealthService,
    StorageHealthSnapshot,
)

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32, max_upload_mb=8, max_chunk_mb=1),
        paths=PathsSection(
            backing_root=tmp_path / "backing",
            state_dir=tmp_path / "state",
            ipc_socket=tmp_path / "ipc" / "worker.sock",
            cache_invalidate_script=tmp_path / "invalidate.sh",
        ),
        system_settings=SystemSettingsSection(
            state_path=tmp_path / "state" / "system_settings.json",
        ),
    )
    flask_app = create_app(cfg)
    flask_app.testing = True
    return flask_app


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


def test_storage_health_service_registered(app) -> None:
    assert isinstance(app.extensions["storage_health_service"], StorageHealthService)


def test_storage_health_endpoint_returns_snapshot_json(app, client) -> None:
    fake = MagicMock(spec=StorageHealthService)
    fake.read_snapshot.return_value = StorageHealthSnapshot(
        severity=SEV_OK,
        messages=("All checks passed.",),
        fs_type="ext4",
        device="/dev/mmcblk0p2",
        mount_readonly=False,
        fs_errors=0,
        io_errors_24h=0,
        sd_card_name="GF8S5",
        sd_card_manfid="0x00001b",
    )
    app.extensions["storage_health_service"] = fake
    response = client.get("/api/storage/health")
    assert response.status_code == HTTPStatus.OK
    data = response.get_json()
    assert data["severity"] == SEV_OK
    assert data["fs_type"] == "ext4"
    assert data["device"] == "/dev/mmcblk0p2"
    assert data["mount_readonly"] is False
    assert data["sd_card_name"] == "GF8S5"
    fake.read_snapshot.assert_called_once()


def test_storage_health_endpoint_handles_service_crash(app, client) -> None:
    fake = MagicMock(spec=StorageHealthService)
    fake.read_snapshot.side_effect = RuntimeError("boom")
    app.extensions["storage_health_service"] = fake
    response = client.get("/api/storage/health")
    assert response.status_code == HTTPStatus.OK
    data = response.get_json()
    assert data["severity"] == "unknown"
    assert any("crashed" in m for m in data["messages"])
    assert any("boom" in e for e in data["probe_errors"])


def test_storage_health_endpoint_caches_snapshot_per_request(app, client) -> None:
    fake = MagicMock(spec=StorageHealthService)
    fake.read_snapshot.return_value = StorageHealthSnapshot(severity=SEV_OK)
    app.extensions["storage_health_service"] = fake
    # Two separate HTTP requests → two snapshot calls.
    client.get("/api/storage/health")
    client.get("/api/storage/health")
    assert fake.read_snapshot.call_count == 2


def test_schedule_fsck_post_invokes_service_and_returns_fresh_snapshot(
    app, client
) -> None:
    fake = MagicMock(spec=StorageHealthService)
    fake.read_snapshot.return_value = StorageHealthSnapshot(
        severity=SEV_OK, fsck_scheduled=True
    )
    app.extensions["storage_health_service"] = fake

    response = client.post("/api/storage/health/fsck-on-next-boot")

    assert response.status_code == HTTPStatus.OK
    fake.schedule_fsck_at_next_boot.assert_called_once()
    data = response.get_json()
    assert data["fsck_scheduled"] is True


def test_schedule_fsck_post_surfaces_runtime_error_as_500(app, client) -> None:
    fake = MagicMock(spec=StorageHealthService)
    fake.schedule_fsck_at_next_boot.side_effect = RuntimeError("denied")
    fake.read_snapshot.return_value = StorageHealthSnapshot(severity=SEV_OK)
    app.extensions["storage_health_service"] = fake

    response = client.post("/api/storage/health/fsck-on-next-boot")

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    data = response.get_json()
    assert "denied" in data["error"]
    assert "snapshot" in data


def test_cancel_fsck_delete_invokes_service_and_returns_fresh_snapshot(
    app, client
) -> None:
    fake = MagicMock(spec=StorageHealthService)
    fake.read_snapshot.return_value = StorageHealthSnapshot(
        severity=SEV_OK, fsck_scheduled=False
    )
    app.extensions["storage_health_service"] = fake

    response = client.delete("/api/storage/health/fsck-on-next-boot")

    assert response.status_code == HTTPStatus.OK
    fake.cancel_scheduled_fsck.assert_called_once()
    data = response.get_json()
    assert data["fsck_scheduled"] is False


def test_reboot_now_requires_confirm(app, client) -> None:
    fake = MagicMock(spec=StorageHealthService)
    fake.read_snapshot.return_value = StorageHealthSnapshot(severity=SEV_OK)
    app.extensions["storage_health_service"] = fake

    response = client.post("/api/storage/health/reboot-now", json={})

    assert response.status_code == HTTPStatus.BAD_REQUEST
    fake.reboot_now.assert_not_called()
    assert "snapshot" in response.get_json()


def test_reboot_now_invokes_service_and_returns_202(app, client) -> None:
    fake = MagicMock(spec=StorageHealthService)
    fake.read_snapshot.return_value = StorageHealthSnapshot(
        severity=SEV_OK, fsck_scheduled=True
    )
    app.extensions["storage_health_service"] = fake

    response = client.post(
        "/api/storage/health/reboot-now", json={"confirm": True}
    )

    assert response.status_code == HTTPStatus.ACCEPTED
    fake.reboot_now.assert_called_once()
    data = response.get_json()
    assert data["rebooting"] is True
    assert "snapshot" in data


def test_reboot_now_surfaces_runtime_error_as_500(app, client) -> None:
    fake = MagicMock(spec=StorageHealthService)
    fake.reboot_now.side_effect = RuntimeError("nope")
    fake.read_snapshot.return_value = StorageHealthSnapshot(severity=SEV_OK)
    app.extensions["storage_health_service"] = fake

    response = client.post(
        "/api/storage/health/reboot-now", json={"confirm": True}
    )

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    data = response.get_json()
    assert "nope" in data["error"]
    assert "snapshot" in data


def test_online_check_post_starts_run_and_returns_202(app, client) -> None:
    fake = MagicMock(spec=StorageHealthService)
    fake.maybe_start_background_online_check.return_value = True
    fake.read_snapshot.return_value = StorageHealthSnapshot(
        severity=SEV_OK, online_check_running=True
    )
    app.extensions["storage_health_service"] = fake

    response = client.post("/api/storage/health/online-check")

    assert response.status_code == HTTPStatus.ACCEPTED
    fake.maybe_start_background_online_check.assert_called_once_with(force=True)
    data = response.get_json()
    assert data["started"] is True
    assert data["snapshot"]["online_check_running"] is True


def test_online_check_post_reports_not_started_when_already_running(app, client) -> None:
    fake = MagicMock(spec=StorageHealthService)
    fake.maybe_start_background_online_check.return_value = False
    fake.read_snapshot.return_value = StorageHealthSnapshot(
        severity=SEV_OK, online_check_running=True
    )
    app.extensions["storage_health_service"] = fake

    response = client.post("/api/storage/health/online-check")

    assert response.status_code == HTTPStatus.ACCEPTED
    data = response.get_json()
    assert data["started"] is False


def test_online_check_post_surfaces_runtime_error_as_500(app, client) -> None:
    fake = MagicMock(spec=StorageHealthService)
    fake.maybe_start_background_online_check.side_effect = RuntimeError("kaboom")
    fake.read_snapshot.return_value = StorageHealthSnapshot(severity=SEV_OK)
    app.extensions["storage_health_service"] = fake

    response = client.post("/api/storage/health/online-check")

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    data = response.get_json()
    assert "kaboom" in data["error"]
    assert "snapshot" in data
