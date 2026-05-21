"""Tests for the legacy-compat ``/api/*`` blueprint (Phase 5.28)."""

from __future__ import annotations

import wave
from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from teslausb_web.app import create_app
from teslausb_web.config import ChimesSection, FeaturesSection, PathsSection, WebConfig, WebSection
from teslausb_web.services.lock_chime_service import LockChimeFileError

if TYPE_CHECKING:
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient


def _write_real_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(44_100)
        wav.writeframes(b"\x00\x00" * 100)


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    backing_root = tmp_path / "backing"
    state_dir = tmp_path / "state"
    (backing_root / "lightshow" / "Chimes").mkdir(parents=True)
    state_dir.mkdir()
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32, max_upload_mb=8, max_chunk_mb=1),
        paths=PathsSection(
            backing_root=backing_root,
            state_dir=state_dir,
            cache_invalidate_script=tmp_path / "invalidate.sh",
        ),
        features=FeaturesSection(),
        chimes=ChimesSection(),
        source_path=None,
    )
    flask_app = create_app(cfg)
    flask_app.testing = True
    return flask_app


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def chimes_dir(app: Flask) -> Path:
    cfg = app.config["teslausb_config"]
    path = cfg.paths.backing_root / "lightshow" / cfg.chimes.chimes_folder
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- /api/operation_status -----------------------------------------------------


def test_operation_status_returns_stable_not_in_progress_body(client: FlaskClient) -> None:
    response = client.get("/api/operation_status")
    assert response.status_code == HTTPStatus.OK
    body = response.get_json()
    assert body == {
        "in_progress": False,
        "operation": None,
        "message": "B-1 has no IMG-mount-cycle; no operation can be in progress.",
    }


# --- /api/chime_filenames ------------------------------------------------------


def test_chime_filenames_empty_library_returns_empty_list(client: FlaskClient) -> None:
    body = client.get("/api/chime_filenames").get_json()
    assert body == {"chime_filenames": []}


def test_chime_filenames_lists_only_wav_files_alphabetically(
    client: FlaskClient,
    chimes_dir: Path,
) -> None:
    for name in ("zeta.wav", "alpha.wav", "beta.wav"):
        _write_real_wav(chimes_dir / name)
    # Non-WAV file must be skipped.
    (chimes_dir / "ignore.txt").write_text("nope")

    body = client.get("/api/chime_filenames").get_json()
    assert body == {"chime_filenames": ["alpha.wav", "beta.wav", "zeta.wav"]}


def test_chime_filenames_returns_empty_list_on_service_error(
    client: FlaskClient,
) -> None:
    with patch(
        "teslausb_web.blueprints.api.list_chime_files",
        side_effect=LockChimeFileError("disk gone"),
    ):
        response = client.get("/api/chime_filenames")
    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {"chime_filenames": []}


# --- /api/rename_chime ---------------------------------------------------------


def test_rename_chime_success_moves_file_and_returns_200(
    client: FlaskClient,
    chimes_dir: Path,
) -> None:
    src = chimes_dir / "old.wav"
    _write_real_wav(src)

    response = client.post("/api/rename_chime/old.wav/new.wav")
    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {"success": True, "old": "old.wav", "new": "new.wav"}
    assert not src.exists()
    assert (chimes_dir / "new.wav").exists()


def test_rename_chime_missing_source_returns_404(
    client: FlaskClient,
) -> None:
    response = client.post("/api/rename_chime/ghost.wav/other.wav")
    assert response.status_code == HTTPStatus.NOT_FOUND
    body = response.get_json()
    assert body["error"] == "not_found"
    assert "reason" in body


def test_rename_chime_unsafe_destination_returns_400(
    client: FlaskClient,
    chimes_dir: Path,
) -> None:
    _write_real_wav(chimes_dir / "src.wav")
    # secure_filename strips leading dots → "evil.wav" != "...evil.wav".
    response = client.post("/api/rename_chime/src.wav/...evil.wav")
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "bad_request"


def test_rename_chime_destination_exists_returns_409(
    client: FlaskClient,
    chimes_dir: Path,
) -> None:
    for name in ("a.wav", "b.wav"):
        _write_real_wav(chimes_dir / name)

    response = client.post("/api/rename_chime/a.wav/b.wav")
    assert response.status_code == HTTPStatus.CONFLICT
    assert response.get_json()["error"] == "conflict"


def test_rename_chime_io_error_returns_500(client: FlaskClient) -> None:
    with patch(
        "teslausb_web.blueprints.api.rename_chime_file",
        side_effect=LockChimeFileError("disk full"),
    ):
        response = client.post("/api/rename_chime/a.wav/b.wav")
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "io_error"


def test_rename_chime_non_wav_destination_returns_400(
    client: FlaskClient,
    chimes_dir: Path,
) -> None:
    _write_real_wav(chimes_dir / "src.wav")
    response = client.post("/api/rename_chime/src.wav/dest.mp3")
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "bad_request"


# --- /api/gadget_state ---------------------------------------------------------


def test_gadget_state_returns_503_with_documented_body(client: FlaskClient) -> None:
    response = client.get("/api/gadget_state")
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    body = response.get_json()
    assert body["error"] == "not_implemented"
    assert body["phase"] == "6"
    assert "teslafat" in body["reason"].lower()


# --- /api/recent_archive/* -----------------------------------------------------


def test_recent_archive_trigger_returns_501_with_documented_body(
    client: FlaskClient,
) -> None:
    response = client.post("/api/recent_archive/trigger")
    assert response.status_code == HTTPStatus.NOT_IMPLEMENTED
    body = response.get_json()
    assert body["error"] == "not_implemented"
    assert "cloud_archive" in body["reason"]


def test_recent_archive_status_returns_501_with_documented_body(
    client: FlaskClient,
) -> None:
    response = client.get("/api/recent_archive/status")
    assert response.status_code == HTTPStatus.NOT_IMPLEMENTED
    body = response.get_json()
    assert body["error"] == "not_implemented"


# --- /api/recover_gadget -------------------------------------------------------


def test_recover_gadget_returns_410_deprecated(client: FlaskClient) -> None:
    response = client.post("/api/recover_gadget")
    assert response.status_code == HTTPStatus.GONE
    body = response.get_json()
    assert body["error"] == "deprecated"
    assert "rust worker" in body["reason"].lower()
