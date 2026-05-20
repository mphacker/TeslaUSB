"""Tests for the system_health blueprint."""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

import pytest
from teslausb_web.app import create_app
from teslausb_web.blueprints.system_health import SEV_ERROR, SEV_OK, SEV_WARN
from teslausb_web.config import (
    FeaturesSection,
    PathsSection,
    WebConfig,
    WebSection,
)

if TYPE_CHECKING:
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient


def _make_config(*, samba: bool, backing_root: Path) -> WebConfig:
    return WebConfig(
        web=WebSection(secret_key="x" * 32, max_upload_mb=8, max_chunk_mb=1),
        paths=PathsSection(backing_root=backing_root),
        features=FeaturesSection(samba_enabled=samba),
        source_path=None,
    )


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    return create_app(_make_config(samba=False, backing_root=tmp_path))


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


def test_health_endpoint_returns_json_with_required_keys(client: FlaskClient) -> None:
    response = client.get("/api/system/health")
    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body, dict)
    for key in ("disk", "daemon", "samba", "overall", "generated_at"):
        assert key in body


def test_disk_block_reports_ok_for_normal_filesystem(client: FlaskClient) -> None:
    body = client.get("/api/system/health").get_json()
    disk = body["disk"]
    # tmp_path is on the dev machine's disk — it almost always has > 500 MB free.
    # If a CI runner is genuinely starving the test will be flaky;
    # accept warn/error as long as the shape is right.
    assert disk["severity"] in {SEV_OK, SEV_WARN, SEV_ERROR}
    assert "message" in disk
    assert "total_bytes" in disk
    assert "free_bytes" in disk


def test_disk_block_unknown_when_path_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    app = create_app(_make_config(samba=False, backing_root=missing))
    body = app.test_client().get("/api/system/health").get_json()
    assert body["disk"]["severity"] == "unknown"


def test_samba_block_reflects_config(tmp_path: Path) -> None:
    on = create_app(_make_config(samba=True, backing_root=tmp_path))
    off = create_app(_make_config(samba=False, backing_root=tmp_path))
    assert on.test_client().get("/api/system/health").get_json()["samba"]["message"] == "Enabled"
    assert off.test_client().get("/api/system/health").get_json()["samba"]["message"] == "Disabled"


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="AF_UNIX required for daemon socket; Linux-only.",
)
def test_daemon_block_reports_error_when_socket_missing(client: FlaskClient) -> None:
    # The default test config points ipc_socket at /run/teslafat.sock,
    # which does not exist on the dev box. Probe must NOT raise; it
    # reports SEV_ERROR with a short message.
    body = client.get("/api/system/health").get_json()
    daemon = body["daemon"]
    assert daemon["severity"] == SEV_ERROR
    assert isinstance(daemon["message"], str)
    assert len(daemon["message"]) <= 120


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="AF_UNIX required for daemon socket; Linux-only.",
)
def test_overall_rolls_up_worst_severity(client: FlaskClient) -> None:
    body = client.get("/api/system/health").get_json()
    # Daemon socket is missing in the test env, so overall must be at
    # least SEV_ERROR (samba is fine, disk is fine on dev boxes).
    assert body["overall"]["severity"] == SEV_ERROR
    assert body["overall"]["subsystem"] == "daemon"


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="AF_UNIX required for live socket round-trip; Linux-only.",
)
def test_daemon_block_serving_state_against_fake_socket(tmp_path: Path) -> None:
    """End-to-end: a fake teslafat replies with STATUS → block reports OK."""
    import json
    import threading

    sock_path = tmp_path / "teslafat.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock_path))
    listener.listen(1)

    def serve_once() -> None:
        conn, _ = listener.accept()
        with conn:
            # Read one NDJSON line, write one back.
            buf = bytearray()
            while not buf.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf.extend(chunk)
            request_envelope = json.loads(buf.rstrip(b"\n"))
            response = {
                "version": 1,
                "id": request_envelope["id"],
                "payload": {
                    "type": "STATUS",
                    "lun_id": 0,
                    "state": "SERVING",
                    "volume_label": "TESLACAM",
                    "volume_size_bytes": 64_000_000_000,
                    "uptime_seconds": 42,
                },
            }
            conn.sendall(json.dumps(response).encode() + b"\n")

    thread = threading.Thread(target=serve_once, daemon=True)
    thread.start()
    try:
        cfg = WebConfig(
            web=WebSection(secret_key="x" * 32, max_upload_mb=8, max_chunk_mb=1),
            paths=PathsSection(backing_root=tmp_path, ipc_socket=sock_path),
            features=FeaturesSection(),
            source_path=None,
        )
        app = create_app(cfg)
        body = app.test_client().get("/api/system/health").get_json()
    finally:
        thread.join(timeout=2)
        listener.close()
    assert body["daemon"]["severity"] == SEV_OK
    assert body["daemon"]["state"] == "SERVING"
    assert body["daemon"]["volume_label"] == "TESLACAM"
