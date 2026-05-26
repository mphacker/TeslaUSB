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
