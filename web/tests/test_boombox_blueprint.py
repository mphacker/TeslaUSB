# ruff: noqa: ANN001  # pytest injects fixtures dynamically in test signatures.
"""Tests for the boombox blueprint."""

from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus
from io import BytesIO
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from teslausb_web.app import create_app
from teslausb_web.blueprints.boombox import (
    _boombox_response,
    _delete_boombox_files,
    _format_size_bytes,
    _get_service,
    _index_payload,
    _invalidate_caches,
    _mime_type_for_path,
    _request_list,
    _resolve_boombox_path,
    _safe_boombox_filename,
    _serialize_boombox_file,
)
from teslausb_web.config import (
    BoomboxSection,
    FeaturesSection,
    PathsSection,
    WebConfig,
    WebSection,
)
from teslausb_web.services.boombox_service import (
    BoomboxError,
    BoomboxFile,
    BoomboxFileError,
    BoomboxListing,
    BoomboxService,
)

if TYPE_CHECKING:
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient
    from teslausb_web.services.cache_invalidation import CacheInvalidator

_XHR = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    backing_root = tmp_path / "backing"
    state_dir = tmp_path / "state"
    (backing_root / "Music" / "Boombox").mkdir(parents=True)
    state_dir.mkdir()
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32, max_upload_mb=8, max_chunk_mb=1),
        paths=PathsSection(
            backing_root=backing_root,
            state_dir=state_dir,
            cache_invalidate_script=tmp_path / "invalidate.sh",
        ),
        features=FeaturesSection(),
        boombox=BoomboxSection(
            base_dir="Boombox",
            max_file_bytes=8,
            max_files=5,
            allowed_extensions=(".mp3", ".wav"),
        ),
        source_path=None,
    )
    flask_app = create_app(cfg)
    flask_app.testing = True
    return flask_app


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def service(app: Flask) -> BoomboxService:
    return app.extensions["boombox_service"]


@pytest.fixture
def invalidator(app: Flask) -> CacheInvalidator:
    return app.extensions["cache_invalidator"]


@pytest.fixture
def boombox_dir(app: Flask) -> Path:
    cfg = app.config["teslausb_config"]
    path = cfg.paths.backing_root / cfg.music.folder / cfg.boombox.base_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def _upload_data(field: str, filename: str, payload: bytes) -> dict[str, object]:
    return {field: (BytesIO(payload), filename)}


def _multi_upload_data(files: list[tuple[str, bytes]]) -> dict[str, object]:
    return {"boombox_files": [(BytesIO(payload), filename) for filename, payload in files]}


def test_app_registers_boombox_blueprint_and_service(app: Flask) -> None:
    assert "boombox" in app.blueprints
    assert isinstance(app.extensions["boombox_service"], BoomboxService)


def test_helper_invalidate_caches_is_noop_without_extension(app: Flask) -> None:
    invalidator = app.extensions.pop("cache_invalidator")
    _invalidate_caches(app)
    app.extensions["cache_invalidator"] = invalidator


def test_helper_get_service_rejects_misconfigured_extension(app: Flask) -> None:
    with app.app_context():
        original = app.extensions["boombox_service"]
        app.extensions["boombox_service"] = object()
        with pytest.raises(RuntimeError, match="boombox_service"):
            _get_service()
        app.extensions["boombox_service"] = original


def test_helper_filename_request_list_and_response_helpers(app: Flask) -> None:
    with app.app_context():
        assert _safe_boombox_filename("valid name.mp3") == "valid name.mp3"
        with pytest.raises(BoomboxError, match="Invalid filename"):
            _safe_boombox_filename("..\\evil.mp3")
        with pytest.raises(BoomboxError, match="Only MP3 and WAV"):
            _safe_boombox_filename("bad.txt")

    with app.test_request_context("/boombox/bulk_delete", method="POST", json={"files": "one.mp3"}):
        assert _request_list("filenames", "files") == ["one.mp3"]
    with app.test_request_context(
        "/boombox/bulk_delete", method="POST", data={"filenames": ["one.mp3", "two.wav"]}
    ):
        assert _request_list("filenames") == ["one.mp3", "two.wav"]
    with app.test_request_context("/boombox/delete/song.mp3?_=9", method="POST"):
        response = _boombox_response(
            success=False,
            message="boom",
            status=HTTPStatus.BAD_REQUEST,
        )
    assert response.status_code == HTTPStatus.FOUND
    assert response.headers["Location"] == "/boombox/?_=9"


def test_helper_serialization_payload_formatting_and_mime(app: Flask) -> None:
    with app.app_context():
        payload = _serialize_boombox_file(
            BoomboxFile(
                filename="sound.mp3",
                size_bytes=1536,
                modified_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
        listing = BoomboxListing(
            files=(
                BoomboxFile(
                    filename="sound.mp3",
                    size_bytes=1536,
                    modified_at=datetime(2025, 1, 1, tzinfo=UTC),
                ),
            ),
            max_files=5,
        )
        index_payload = _index_payload(listing)

    assert payload["size_str"] == "1.5 KB"
    assert payload["modified_at"] == "2025-01-01T00:00:00+00:00"
    assert index_payload["file_count"] == 1
    assert index_payload["max_files"] == 5
    assert _format_size_bytes(1) == "1 B"
    assert _format_size_bytes(2 * 1024 * 1024) == "2.00 MB"
    assert _mime_type_for_path("track.mp3") == "audio/mpeg"
    assert _mime_type_for_path("track.wav") == "audio/wav"


def test_helper_request_list_returns_empty(app: Flask) -> None:
    with app.test_request_context("/boombox/bulk_delete", method="POST", data={}):
        assert _request_list("filenames", "files") == []


def test_helper_delete_boombox_files_requires_nonempty_list(app: Flask) -> None:
    with app.app_context(), pytest.raises(BoomboxError, match="No files selected"):
        _delete_boombox_files([])


def test_helper_delete_boombox_files_is_all_or_nothing(app: Flask, boombox_dir: Path) -> None:
    (boombox_dir / "keep.mp3").write_bytes(b"keep")

    with app.app_context(), pytest.raises(BoomboxError, match=r"missing\.wav: File not found"):
        _delete_boombox_files(["keep.mp3", "missing.wav"])

    assert (boombox_dir / "keep.mp3").exists()


def test_helper_resolve_path_returns_file(app: Flask, boombox_dir: Path) -> None:
    target = boombox_dir / "sound.mp3"
    target.write_bytes(b"payload")

    with app.app_context():
        assert _resolve_boombox_path("sound.mp3") == target


def test_helper_resolve_path_rejects_symlink(app: Flask, boombox_dir: Path) -> None:
    target = boombox_dir / "real.mp3"
    target.write_bytes(b"payload")
    try:
        (boombox_dir / "linked.mp3").symlink_to(target)
    except OSError:
        pytest.skip("symlink creation not available on this system")

    with app.app_context(), pytest.raises(FileNotFoundError):
        _resolve_boombox_path("linked.mp3")


def test_index_route_renders_boombox_template(client: FlaskClient, boombox_dir: Path) -> None:
    (boombox_dir / "sound.mp3").write_bytes(b"payload")

    response = client.get("/boombox/")
    html = response.get_data(as_text=True)

    assert response.status_code == HTTPStatus.OK
    assert "<title>" in html
    assert "Boombox" in html
    assert "Edit Mode" not in html
    assert "Present Mode" not in html
    assert "quick_edit" not in html
    assert "cdn.jsdelivr.net" not in html
    assert "unpkg.com" not in html
    assert "<svg" in html
    assert "boombox.js" in html
    assert "sound.mp3" in html


def test_index_route_does_not_schedule_cache(
    client: FlaskClient, invalidator, boombox_dir: Path
) -> None:
    (boombox_dir / "sound.mp3").write_bytes(b"payload")

    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.get("/boombox/")

    assert response.status_code == HTTPStatus.OK
    schedule_mock.assert_not_called()


def test_index_route_translates_boombox_file_error(
    client: FlaskClient, service: BoomboxService
) -> None:
    with patch.object(service, "list_files", side_effect=BoomboxFileError("boom")):
        response = client.get("/boombox/")
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "boom"


def test_index_route_translates_value_error(client: FlaskClient, service: BoomboxService) -> None:
    with patch.object(service, "list_files", side_effect=ValueError("bad")):
        response = client.get("/boombox/")
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "bad"


def test_index_route_translates_unhandled_error(
    client: FlaskClient, service: BoomboxService
) -> None:
    with patch.object(service, "list_files", side_effect=RuntimeError("boom")):
        response = client.get("/boombox/")
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "Internal server error"


def test_upload_route_saves_file_and_schedules_once(
    client: FlaskClient, boombox_dir: Path, invalidator
) -> None:
    del invalidator
    with patch(
        "teslausb_web.services.boombox_service._schedule_cache_invalidation"
    ) as schedule_mock:
        response = client.post(
            "/boombox/upload",
            data=_multi_upload_data([("sound.mp3", b"payload")]),
            headers=_XHR,
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {
        "success": True,
        "uploaded": 1,
        "results": [
            {
                "filename": "sound.mp3",
                "success": True,
                "message": "Uploaded sound.mp3",
            }
        ],
    }
    assert (boombox_dir / "sound.mp3").read_bytes() == b"payload"
    schedule_mock.assert_called_once()


def test_upload_route_accepts_single_field_name(
    client: FlaskClient, boombox_dir: Path, invalidator
) -> None:
    del invalidator
    with patch(
        "teslausb_web.services.boombox_service._schedule_cache_invalidation"
    ) as schedule_mock:
        response = client.post(
            "/boombox/upload",
            data=_upload_data("boombox_file", "single.wav", b"payload"),
            headers=_XHR,
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["uploaded"] == 1
    assert (boombox_dir / "single.wav").read_bytes() == b"payload"
    schedule_mock.assert_called_once()


def test_upload_route_returns_400_when_empty(client: FlaskClient, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/boombox/upload", data={}, headers=_XHR)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json() == {"success": False, "message": "No files selected"}
    schedule_mock.assert_not_called()


def test_upload_route_non_xhr_empty_redirects(client: FlaskClient, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/boombox/upload", data={})

    assert response.status_code == HTTPStatus.FOUND
    assert response.headers["Location"] == "/boombox/?_=0"
    schedule_mock.assert_not_called()


def test_upload_route_non_xhr_success_redirects(client: FlaskClient, invalidator) -> None:
    del invalidator
    with patch(
        "teslausb_web.services.boombox_service._schedule_cache_invalidation"
    ) as schedule_mock:
        response = client.post(
            "/boombox/upload",
            query_string={"_": "5"},
            data=_multi_upload_data([("sound.mp3", b"payload")]),
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.FOUND
    assert response.headers["Location"] == "/boombox/?_=5"
    schedule_mock.assert_called_once()


def test_upload_route_enforces_max_files(
    client: FlaskClient, boombox_dir: Path, invalidator
) -> None:
    for index in range(5):
        (boombox_dir / f"clip{index}.mp3").write_bytes(b"ok")

    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/boombox/upload",
            data=_multi_upload_data([("extra.mp3", b"payload")]),
            headers=_XHR,
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["results"][0]["message"] == "Maximum of 5 Boombox sounds allowed"
    schedule_mock.assert_not_called()


def test_upload_route_reports_partial_success(
    client: FlaskClient, boombox_dir: Path, invalidator
) -> None:
    del invalidator
    for index in range(4):
        (boombox_dir / f"clip{index}.mp3").write_bytes(b"ok")

    with patch(
        "teslausb_web.services.boombox_service._schedule_cache_invalidation"
    ) as schedule_mock:
        response = client.post(
            "/boombox/upload",
            data=_multi_upload_data([("new.mp3", b"1"), ("overflow.wav", b"2")]),
            headers=_XHR,
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["uploaded"] == 1
    assert response.get_json()["results"][1]["message"] == "Maximum of 5 Boombox sounds allowed"
    schedule_mock.assert_called_once()


def test_upload_route_rejects_invalid_extension(client: FlaskClient, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/boombox/upload",
            data=_multi_upload_data([("bad.txt", b"payload")]),
            headers=_XHR,
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["results"][0]["message"] == "Only MP3 and WAV files are allowed"
    schedule_mock.assert_not_called()


def test_upload_route_rejects_path_traversal(client: FlaskClient, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/boombox/upload",
            data=_multi_upload_data([("..\\evil.mp3", b"payload")]),
            headers=_XHR,
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["results"][0]["message"].startswith("Invalid filename")
    schedule_mock.assert_not_called()


def test_upload_route_rejects_oversize_file(client: FlaskClient, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/boombox/upload",
            data=_multi_upload_data([("big.mp3", b"123456789")]),
            headers=_XHR,
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert "Limit is" in response.get_json()["results"][0]["message"]
    schedule_mock.assert_not_called()


def test_upload_route_translates_boombox_file_error(
    client: FlaskClient, service: BoomboxService
) -> None:
    with patch.object(service, "list_files", side_effect=BoomboxFileError("disk full")):
        response = client.post(
            "/boombox/upload",
            data=_multi_upload_data([("sound.mp3", b"payload")]),
            headers=_XHR,
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json() == {"success": False, "message": "disk full"}


def test_upload_route_translates_value_error(client: FlaskClient, service: BoomboxService) -> None:
    with patch.object(service, "list_files", side_effect=ValueError("bad upload")):
        response = client.post(
            "/boombox/upload",
            data=_multi_upload_data([("sound.mp3", b"payload")]),
            headers=_XHR,
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json() == {"success": False, "message": "bad upload"}


def test_upload_route_translates_unhandled_error(
    client: FlaskClient, service: BoomboxService
) -> None:
    with patch.object(service, "list_files", side_effect=RuntimeError("boom")):
        response = client.post(
            "/boombox/upload",
            data=_multi_upload_data([("sound.mp3", b"payload")]),
            headers=_XHR,
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json() == {"success": False, "message": "Internal server error"}


def test_delete_route_removes_file_and_schedules_once(
    client: FlaskClient, boombox_dir: Path, invalidator
) -> None:
    del invalidator
    (boombox_dir / "sound.mp3").write_bytes(b"payload")

    with patch(
        "teslausb_web.services.boombox_service._schedule_cache_invalidation"
    ) as schedule_mock:
        response = client.post("/boombox/delete/sound.mp3", headers=_XHR)

    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {"success": True, "message": "Deleted sound.mp3"}
    assert not (boombox_dir / "sound.mp3").exists()
    schedule_mock.assert_called_once()


def test_delete_route_non_xhr_success_redirects(
    client: FlaskClient, boombox_dir: Path, invalidator
) -> None:
    del invalidator
    (boombox_dir / "sound.mp3").write_bytes(b"payload")

    with patch(
        "teslausb_web.services.boombox_service._schedule_cache_invalidation"
    ) as schedule_mock:
        response = client.post("/boombox/delete/sound.mp3", query_string={"_": "4"})

    assert response.status_code == HTTPStatus.FOUND
    assert response.headers["Location"] == "/boombox/?_=4"
    schedule_mock.assert_called_once()


def test_delete_route_returns_400_for_missing_file(client: FlaskClient, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/boombox/delete/missing.mp3", headers=_XHR)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json() == {"success": False, "message": "File not found"}
    schedule_mock.assert_not_called()


def test_delete_route_rejects_path_traversal(client: FlaskClient, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/boombox/delete/..%5Cevil.mp3", headers=_XHR)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["message"].startswith("Invalid filename")
    schedule_mock.assert_not_called()


def test_delete_route_translates_boombox_file_error(
    client: FlaskClient, service: BoomboxService
) -> None:
    with patch.object(service, "delete_file", side_effect=BoomboxFileError("cannot delete")):
        response = client.post("/boombox/delete/sound.mp3", headers=_XHR)

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json() == {"success": False, "message": "cannot delete"}


def test_delete_route_translates_value_error(client: FlaskClient, service: BoomboxService) -> None:
    with patch.object(service, "delete_file", side_effect=ValueError("bad delete")):
        response = client.post("/boombox/delete/sound.mp3", headers=_XHR)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json() == {"success": False, "message": "bad delete"}


def test_delete_route_translates_unhandled_error(
    client: FlaskClient, service: BoomboxService
) -> None:
    with patch.object(service, "delete_file", side_effect=RuntimeError("boom")):
        response = client.post("/boombox/delete/sound.mp3", headers=_XHR)

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json() == {"success": False, "message": "Internal server error"}


def test_bulk_delete_route_deletes_files_and_schedules_once(
    client: FlaskClient, boombox_dir: Path, invalidator
) -> None:
    (boombox_dir / "one.mp3").write_bytes(b"1")
    (boombox_dir / "two.wav").write_bytes(b"2")

    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/boombox/bulk_delete", json={"filenames": ["one.mp3", "two.wav"]})

    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {
        "success": True,
        "message": "Deleted 2 boombox file(s)",
        "deleted_count": 2,
    }
    assert not (boombox_dir / "one.mp3").exists()
    assert not (boombox_dir / "two.wav").exists()
    schedule_mock.assert_called_once_with()


def test_bulk_delete_route_accepts_form_files_key(
    client: FlaskClient, boombox_dir: Path, invalidator
) -> None:
    (boombox_dir / "one.mp3").write_bytes(b"1")

    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/boombox/bulk_delete", data={"files": ["one.mp3"]}, headers=_XHR)

    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["deleted_count"] == 1
    schedule_mock.assert_called_once_with()


def test_bulk_delete_route_non_xhr_success_redirects(
    client: FlaskClient, boombox_dir: Path, invalidator
) -> None:
    (boombox_dir / "one.mp3").write_bytes(b"1")

    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/boombox/bulk_delete",
            query_string={"_": "6"},
            data={"filenames": ["one.mp3"]},
        )

    assert response.status_code == HTTPStatus.FOUND
    assert response.headers["Location"] == "/boombox/?_=6"
    schedule_mock.assert_called_once_with()


def test_bulk_delete_route_returns_400_when_empty(client: FlaskClient, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/boombox/bulk_delete", json={})

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json() == {"success": False, "message": "No files selected"}
    schedule_mock.assert_not_called()


def test_bulk_delete_route_rejects_path_traversal(client: FlaskClient, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/boombox/bulk_delete", json={"filenames": ["..\\evil.mp3"]})

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["message"].startswith("Invalid filename")
    schedule_mock.assert_not_called()


def test_bulk_delete_route_is_all_or_nothing(
    client: FlaskClient, boombox_dir: Path, invalidator
) -> None:
    (boombox_dir / "keep.mp3").write_bytes(b"keep")

    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/boombox/bulk_delete",
            json={"filenames": ["keep.mp3", "missing.wav"]},
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json() == {"success": False, "message": "missing.wav: File not found"}
    assert (boombox_dir / "keep.mp3").exists()
    schedule_mock.assert_not_called()


def test_bulk_delete_route_translates_boombox_file_error(
    client: FlaskClient, boombox_dir: Path
) -> None:
    (boombox_dir / "sound.mp3").write_bytes(b"payload")

    with patch("teslausb_web.blueprints.boombox.Path.unlink", side_effect=OSError("disk full")):
        response = client.post("/boombox/bulk_delete", json={"filenames": ["sound.mp3"]})

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json() == {
        "success": False,
        "message": "Failed to delete boombox file sound.mp3: disk full",
    }


def test_bulk_delete_route_translates_unhandled_error(client: FlaskClient) -> None:
    with patch(
        "teslausb_web.blueprints.boombox._delete_boombox_files", side_effect=RuntimeError("boom")
    ):
        response = client.post("/boombox/bulk_delete", json={"filenames": ["sound.mp3"]})

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json() == {"success": False, "message": "Internal server error"}


def test_play_route_streams_mp3(client: FlaskClient, boombox_dir: Path) -> None:
    (boombox_dir / "sound.mp3").write_bytes(b"payload")

    response = client.get("/boombox/play/sound.mp3")

    assert response.status_code == HTTPStatus.OK
    assert response.mimetype == "audio/mpeg"
    assert response.data == b"payload"


def test_play_route_streams_wav(client: FlaskClient, boombox_dir: Path) -> None:
    (boombox_dir / "sound.wav").write_bytes(b"payload")

    response = client.get("/boombox/play/sound.wav")

    assert response.status_code == HTTPStatus.OK
    assert response.mimetype == "audio/wav"


def test_play_route_does_not_schedule_cache(
    client: FlaskClient, boombox_dir: Path, invalidator
) -> None:
    (boombox_dir / "sound.mp3").write_bytes(b"payload")

    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.get("/boombox/play/sound.mp3")

    assert response.status_code == HTTPStatus.OK
    schedule_mock.assert_not_called()


def test_play_route_returns_404_when_missing(client: FlaskClient, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.get("/boombox/play/missing.mp3")

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.get_json()["error"] == "File not found"
    schedule_mock.assert_not_called()


def test_play_route_rejects_path_traversal(client: FlaskClient) -> None:
    response = client.get("/boombox/play/..%5Cevil.mp3")

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"].startswith("Invalid filename")


def test_play_route_rejects_invalid_extension(client: FlaskClient, boombox_dir: Path) -> None:
    (boombox_dir / "sound.txt").write_bytes(b"payload")

    response = client.get("/boombox/play/sound.txt")

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "Only MP3 and WAV files are allowed"


def test_play_route_translates_boombox_file_error(client: FlaskClient) -> None:
    with patch(
        "teslausb_web.blueprints.boombox._resolve_boombox_path",
        side_effect=BoomboxFileError("boom"),
    ):
        response = client.get("/boombox/play/sound.mp3")

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "boom"


def test_play_route_translates_value_error(client: FlaskClient) -> None:
    with patch(
        "teslausb_web.blueprints.boombox._resolve_boombox_path",
        side_effect=ValueError("bad play"),
    ):
        response = client.get("/boombox/play/sound.mp3")

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "bad play"


def test_play_route_translates_unhandled_error(client: FlaskClient) -> None:
    with (
        patch(
            "teslausb_web.blueprints.boombox._resolve_boombox_path",
            return_value="C:\\boombox\\sound.mp3",
        ),
        patch("teslausb_web.blueprints.boombox.send_file", side_effect=RuntimeError("boom")),
    ):
        response = client.get("/boombox/play/sound.mp3")

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "Internal server error"
