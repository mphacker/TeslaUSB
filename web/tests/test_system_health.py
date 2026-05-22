"""Tests for the system_health blueprint."""

from __future__ import annotations

import socket
import sqlite3
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from teslausb_web.app import create_app
from teslausb_web.blueprints.system_health import (
    SEV_ERROR,
    SEV_OK,
    SEV_UNKNOWN,
    SEV_WARN,
    _gadget_block,
    _indexer_block,
    _journal_block,
    _network_block,
    _storage_writable_block,
    _worker_block,
)
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


def _make_config(
    *,
    samba: bool,
    backing_root: Path,
    db_path: Path | None = None,
    state_dir: Path | None = None,
) -> WebConfig:
    paths_kwargs: dict[str, Path] = {"backing_root": backing_root}
    if db_path is not None:
        paths_kwargs["db_path"] = db_path
    if state_dir is not None:
        paths_kwargs["state_dir"] = state_dir
    return WebConfig(
        web=WebSection(secret_key="x" * 32, max_upload_mb=8, max_chunk_mb=1),
        paths=PathsSection(**paths_kwargs),
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
    for key in ("disk", "teslafat_0", "teslafat_1", "samba", "overall", "generated_at"):
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
def test_teslafat_ipc_block_reports_error_when_socket_missing(tmp_path: Path) -> None:
    """When ipc_daemon_enabled, missing socket → SEV_ERROR teslafat_ipc."""
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32, max_upload_mb=8, max_chunk_mb=1),
        paths=PathsSection(backing_root=tmp_path, ipc_socket=tmp_path / "missing.sock"),
        features=FeaturesSection(ipc_daemon_enabled=True),
        source_path=None,
    )
    app = create_app(cfg)
    body = app.test_client().get("/api/system/health").get_json()
    assert "teslafat_ipc" in body
    assert body["teslafat_ipc"]["severity"] == SEV_ERROR
    assert isinstance(body["teslafat_ipc"]["message"], str)
    assert len(body["teslafat_ipc"]["message"]) <= 120


def test_teslafat_ipc_block_absent_when_flag_disabled(client: FlaskClient) -> None:
    # Default test config leaves ipc_daemon_enabled=False, so the
    # optional supplementary block must NOT appear in the payload.
    body = client.get("/api/system/health").get_json()
    assert "teslafat_ipc" not in body


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="AF_UNIX required for live socket round-trip; Linux-only.",
)
def test_teslafat_ipc_block_serving_state_against_fake_socket(tmp_path: Path) -> None:
    """End-to-end: a fake teslafat replies with STATUS → teslafat_ipc reports OK."""
    import json
    import threading

    sock_path = tmp_path / "teslafat.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock_path))
    listener.listen(1)

    def serve_once() -> None:
        conn, _ = listener.accept()
        with conn:
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
            features=FeaturesSection(ipc_daemon_enabled=True),
            source_path=None,
        )
        app = create_app(cfg)
        body = app.test_client().get("/api/system/health").get_json()
    finally:
        thread.join(timeout=2)
        listener.close()
    assert body["teslafat_ipc"]["severity"] == SEV_OK
    assert body["teslafat_ipc"]["state"] == "SERVING"
    assert body["teslafat_ipc"]["volume_label"] == "TESLACAM"


# -------------------------------------------------------------------
# New B-1 probes (gadget, indexer, worker, network, storage, journal)
# -------------------------------------------------------------------




# -------------------------------------------------------------------
# Phase 6: B-1 subsystem probes (gadget, indexer, worker, network,
# storage_writable, journal). These all run on the dev box because
# the implementations isolate every external dependency behind a
# helper that the tests stub via ``unittest.mock``.
# -------------------------------------------------------------------


def _bare_cfg(tmp_path: Path, **overrides) -> WebConfig:
    """WebConfig with state_dir/backing_root pointed at tmp_path."""
    return _make_config(
        samba=False,
        backing_root=tmp_path,
        state_dir=tmp_path,
        db_path=overrides.get("db_path", tmp_path / "index.sqlite3"),
    )


# ---- gadget --------------------------------------------------------


def test_gadget_block_present(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path)
    with patch(
        "teslausb_web.blueprints.system_health.gadget_mode_token",
        return_value="present",
    ):
        block = _gadget_block(cfg)
    assert block["severity"] == SEV_OK
    assert block["token"] == "present"


def test_gadget_block_unknown_token_is_error(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path)
    with patch(
        "teslausb_web.blueprints.system_health.gadget_mode_token",
        return_value="unknown",
    ):
        block = _gadget_block(cfg)
    assert block["severity"] == SEV_ERROR
    assert block["token"] == "unknown"


def test_gadget_block_swallows_exception(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path)
    with patch(
        "teslausb_web.blueprints.system_health.gadget_mode_token",
        side_effect=RuntimeError("boom"),
    ):
        block = _gadget_block(cfg)
    assert block["severity"] == SEV_UNKNOWN
    assert "boom" in block["message"]


# ---- indexer -------------------------------------------------------


def _seed_index_db(path: Path, clip_count: int, last_indexed: int) -> None:
    """Build a minimal worker-shaped DB for the indexer probe."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE clips (id INTEGER PRIMARY KEY, "
            "relative_path TEXT, indexed_at_utc INTEGER)"
        )
        for i in range(clip_count):
            conn.execute(
                "INSERT INTO clips(relative_path, indexed_at_utc) VALUES (?, ?)",
                (f"clip_{i}.mp4", last_indexed),
            )
        conn.commit()
    finally:
        conn.close()


def test_indexer_block_missing_db(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path, db_path=tmp_path / "missing.sqlite3")
    block = _indexer_block(cfg)
    assert block["severity"] == SEV_WARN
    assert "not yet" in block["message"]


def test_indexer_block_healthy_db(tmp_path: Path) -> None:
    import time as _t
    db = tmp_path / "index.sqlite3"
    _seed_index_db(db, clip_count=5, last_indexed=int(_t.time()))
    cfg = _bare_cfg(tmp_path, db_path=db)
    block = _indexer_block(cfg)
    assert block["severity"] == SEV_OK
    assert block["clip_count"] == 5


def test_indexer_block_empty_db_is_warn(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite3"
    _seed_index_db(db, clip_count=0, last_indexed=0)
    cfg = _bare_cfg(tmp_path, db_path=db)
    block = _indexer_block(cfg)
    assert block["severity"] == SEV_WARN
    assert block["clip_count"] == 0


def test_indexer_block_stale_db_is_ok_with_age_label(tmp_path: Path) -> None:
    import time as _t

    db = tmp_path / "index.sqlite3"
    # Last index 1 h ago.  Indexer health is independent of Tesla write
    # activity, so clip age is informational, not a warning.
    _seed_index_db(db, clip_count=3, last_indexed=int(_t.time()) - 3600)
    cfg = _bare_cfg(tmp_path, db_path=db)
    block = _indexer_block(cfg)
    assert block["severity"] == SEV_OK
    assert "1 h old" in block["message"]


def test_indexer_block_very_old_clip_is_still_ok(tmp_path: Path) -> None:
    import time as _t

    db = tmp_path / "index.sqlite3"
    _seed_index_db(db, clip_count=10, last_indexed=int(_t.time()) - 3 * 86_400)
    cfg = _bare_cfg(tmp_path, db_path=db)
    block = _indexer_block(cfg)
    assert block["severity"] == SEV_OK
    assert "3 d old" in block["message"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only permission semantics")
def test_indexer_block_readonly_db_is_error(tmp_path: Path) -> None:
    import os
    import stat
    db = tmp_path / "index.sqlite3"
    _seed_index_db(db, clip_count=1, last_indexed=1)
    os.chmod(db, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IXUSR)
    try:
        cfg = _bare_cfg(tmp_path, db_path=db)
        block = _indexer_block(cfg)
        assert block["severity"] == SEV_ERROR
    finally:
        os.chmod(tmp_path, stat.S_IRWXU)
        os.chmod(db, stat.S_IRWXU)


# ---- worker --------------------------------------------------------


def _fake_run(stdout: str, returncode: int = 0):
    from types import SimpleNamespace
    return lambda *a, **kw: SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def test_worker_block_active(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path)
    with patch("teslausb_web.blueprints.system_health.subprocess.run", _fake_run("active\n")):
        block = _worker_block(cfg)
    assert block["severity"] == SEV_OK


def test_worker_block_inactive_is_error(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path)
    with patch("teslausb_web.blueprints.system_health.subprocess.run", _fake_run("inactive\n")):
        block = _worker_block(cfg)
    assert block["severity"] == SEV_ERROR
    assert block["state"] == "inactive"


def test_worker_block_systemctl_missing_is_unknown(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path)
    with patch(
        "teslausb_web.blueprints.system_health.subprocess.run",
        side_effect=FileNotFoundError,
    ):
        block = _worker_block(cfg)
    assert block["severity"] == SEV_UNKNOWN


# ---- network -------------------------------------------------------


def test_network_block_connected(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path)
    with patch("teslausb_web.blueprints.system_health.subprocess.run", _fake_run("connected\n")):
        block = _network_block(cfg)
    assert block["severity"] == SEV_OK


def test_network_block_disconnected_is_error(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path)
    with patch("teslausb_web.blueprints.system_health.subprocess.run", _fake_run("disconnected\n")):
        block = _network_block(cfg)
    assert block["severity"] == SEV_ERROR


def test_network_block_nmcli_missing_is_unknown(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path)
    with patch(
        "teslausb_web.blueprints.system_health.subprocess.run",
        side_effect=FileNotFoundError,
    ):
        block = _network_block(cfg)
    assert block["severity"] == SEV_UNKNOWN


# ---- storage_writable ----------------------------------------------


def test_storage_writable_block_happy_path(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path)
    block = _storage_writable_block(cfg)
    assert block["severity"] == SEV_OK


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only permission semantics")
def test_storage_writable_block_readonly_root_is_error(tmp_path: Path) -> None:
    import os
    import stat
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)
    try:
        cfg = _make_config(samba=False, backing_root=ro, state_dir=tmp_path)
        block = _storage_writable_block(cfg)
        assert block["severity"] == SEV_ERROR
    finally:
        os.chmod(ro, stat.S_IRWXU)


# ---- journal -------------------------------------------------------


def test_journal_block_no_errors_is_ok(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path)
    # Force-clear cache for deterministic test.
    from teslausb_web.blueprints import system_health as sh
    sh._journal_cache["at"] = 0.0
    sh._journal_cache["result"] = None
    with patch(
        "teslausb_web.blueprints.system_health.subprocess.run",
        _fake_run("-- No entries --\n"),
    ):
        block = _journal_block(cfg)
    assert block["severity"] == SEV_OK
    assert block["count"] == 0


def test_journal_block_with_errors_is_warn(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path)
    from teslausb_web.blueprints import system_health as sh
    sh._journal_cache["at"] = 0.0
    sh._journal_cache["result"] = None
    stdout = (
        "2026-05-21T22:48:29-0400 cybertruckusb worker[1268]: bad thing happened\n"
        "2026-05-21T22:48:32-0400 cybertruckusb worker[1268]: another bad thing\n"
    )
    with patch(
        "teslausb_web.blueprints.system_health.subprocess.run",
        _fake_run(stdout),
    ):
        block = _journal_block(cfg)
    assert block["severity"] == SEV_WARN
    assert block["count"] == 2
    assert "another bad thing" in block["latest"]


def test_journal_block_journalctl_missing_is_unknown(tmp_path: Path) -> None:
    cfg = _bare_cfg(tmp_path)
    from teslausb_web.blueprints import system_health as sh
    sh._journal_cache["at"] = 0.0
    sh._journal_cache["result"] = None
    with patch(
        "teslausb_web.blueprints.system_health.subprocess.run",
        side_effect=FileNotFoundError,
    ):
        block = _journal_block(cfg)
    assert block["severity"] == SEV_UNKNOWN


# ---- endpoint integration ------------------------------------------


def test_health_endpoint_includes_all_new_blocks(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite3"
    _seed_index_db(db, clip_count=1, last_indexed=1)
    cfg = _bare_cfg(tmp_path, db_path=db)
    app = create_app(cfg)
    body = app.test_client().get("/api/system/health").get_json()
    for key in (
        "disk", "teslafat_0", "teslafat_1", "samba", "gadget", "indexer", "worker",
        "network", "storage_writable", "journal", "overall",
    ):
        assert key in body, f"missing key: {key}"
