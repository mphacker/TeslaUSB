# ruff: noqa: ANN001  # pytest injects fixtures dynamically in test signatures.
"""Tests for the wraps blueprint."""

from __future__ import annotations

import struct
import zlib
from http import HTTPStatus
from io import BytesIO
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from teslausb_web.app import create_app
from teslausb_web.blueprints.wraps import (
    _format_size_bytes,
    _get_service,
    _index_context,
    _invalidate_caches,
    _request_list,
    _safe_wrap_filename,
    _serialize_wrap_info,
    _upload_error_status,
    _wrap_dimensions,
)
from teslausb_web.config import FeaturesSection, PathsSection, WebConfig, WebSection, WrapsSection
from teslausb_web.services.wrap_service import WrapError, WrapFileError, WrapInfo, WrapService
from werkzeug.datastructures import FileStorage

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
    (backing_root / "lightshow" / "Wraps").mkdir(parents=True)
    state_dir.mkdir()
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32, max_upload_mb=8, max_chunk_mb=1),
        paths=PathsSection(
            backing_root=backing_root,
            state_dir=state_dir,
            cache_invalidate_script=tmp_path / "invalidate.sh",
        ),
        features=FeaturesSection(),
        wraps=WrapsSection(
            folder="Wraps",
            max_size=1 * 1024 * 1024,
            min_dimension=512,
            max_dimension=1024,
            max_filename_length=30,
            max_upload_count=10,
            allowed_extensions=(".png",),
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
def service(app) -> WrapService:
    return app.extensions["wrap_service"]


@pytest.fixture
def invalidator(app: Flask) -> CacheInvalidator:
    return app.extensions["cache_invalidator"]


@pytest.fixture
def wraps_dir(app) -> Path:
    cfg = app.config["teslausb_config"]
    path = cfg.paths.backing_root / "lightshow" / cfg.wraps.folder
    path.mkdir(parents=True, exist_ok=True)
    return path


def _upload_data(field: str, filename: str, payload: bytes) -> dict[str, object]:
    return {field: (BytesIO(payload), filename)}


def _multi_upload_data(files: list[tuple[str, bytes]]) -> dict[str, object]:
    return {"wrap_files": [(BytesIO(payload), filename) for filename, payload in files]}


def _chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)


def _png_bytes(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + (b"\x00\x00\x00" * width)
    raw = row * height
    idat = zlib.compress(raw)
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _chunk(b"IHDR", ihdr),
            _chunk(b"IDAT", idat),
            _chunk(b"IEND", b""),
        )
    )


def _upload(name: str, payload: bytes) -> FileStorage:
    return FileStorage(stream=BytesIO(payload), filename=name)


def test_app_registers_wrap_blueprint_and_service(app: Flask) -> None:
    assert "wraps" in app.blueprints
    assert isinstance(app.extensions["wrap_service"], WrapService)


def test_index_route_skipped_until_template_port() -> None:
    pytest.skip(reason="template in 5.10c")


def test_helper_invalidate_caches_is_noop_without_extension(app: Flask) -> None:
    invalidator = app.extensions.pop("cache_invalidator")
    _invalidate_caches(app)
    app.extensions["cache_invalidator"] = invalidator


def test_helper_get_service_rejects_misconfigured_extension(app: Flask) -> None:
    with app.app_context():
        original = app.extensions["wrap_service"]
        app.extensions["wrap_service"] = object()
        with pytest.raises(RuntimeError, match="wrap_service"):
            _get_service()
        app.extensions["wrap_service"] = original


def test_helper_request_list_and_status_mapping(app: Flask) -> None:
    with app.test_request_context("/wraps/bulk_delete", method="POST", json={"files": "one.png"}):
        assert _request_list("filenames", "files") == ["one.png"]
    with app.test_request_context(
        "/wraps/bulk_delete", method="POST", data={"filenames": ["one.png", "two.png"]}
    ):
        assert _request_list("filenames") == ["one.png", "two.png"]
    assert (
        _upload_error_status("File size must be 1 MB or less")
        == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    )
    assert _upload_error_status("bad dimensions") == HTTPStatus.BAD_REQUEST


def test_helper_safe_wrap_filename_and_serialization(app: Flask) -> None:
    with app.app_context():
        assert _safe_wrap_filename("valid name.png") == "valid name.png"
        with pytest.raises(WrapError, match="Invalid filename"):
            _safe_wrap_filename("..\\evil.png")
        from datetime import UTC, datetime

        payload = _serialize_wrap_info(
            WrapInfo(
                filename="cover.png",
                size_bytes=1536,
                width=512,
                height=512,
                modified_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
    assert payload["size_str"] == "1.5 KB"
    assert payload["dimensions"] == "512x512"
    assert payload["partition_key"] == "library"


def test_helper_safe_wrap_filename_rejects_additional_invalid_cases(app: Flask) -> None:
    with app.app_context():
        with pytest.raises(WrapError, match="Filename is required"):
            _safe_wrap_filename(" ")
        with pytest.raises(WrapError, match="Invalid filename"):
            _safe_wrap_filename(".")
        with pytest.raises(WrapError, match="30 characters or less"):
            _safe_wrap_filename(f"{'a' * 31}.png")
        with pytest.raises(WrapError, match="letters, numbers"):
            _safe_wrap_filename("bad!.png")


def test_helper_format_context_and_wrap_dimensions(
    app: Flask, wraps_dir: Path, service: WrapService
) -> None:
    wraps_dir.joinpath("bad.png").write_bytes(b"not-a-png")
    service.upload_files([_upload("cover.png", _png_bytes(512, 512))])

    with app.app_context():
        context = _index_context()
        assert _format_size_bytes(1) == "1 B"
        assert _format_size_bytes(2 * 1024 * 1024) == "2.00 MB"
        assert _wrap_dimensions("cover.png") == "512x512"
        assert _wrap_dimensions("bad.png") is None
        assert _wrap_dimensions("missing.png") is None

    assert context["wrap_count"] == 2
    assert context["media_tab"] == "wraps"


def test_helper_request_list_returns_empty(app: Flask) -> None:
    with app.test_request_context("/wraps/bulk_delete", method="POST", data={}):
        assert _request_list("filenames", "files") == []


def test_index_route_translates_wrap_file_error(client, service: WrapService) -> None:
    with patch.object(service, "list_wraps", side_effect=WrapFileError("boom")):
        response = client.get("/wraps/")
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "boom"


def test_index_route_translates_value_error(client, service: WrapService) -> None:
    with patch.object(service, "list_wraps", side_effect=ValueError("boom")):
        response = client.get("/wraps/")
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "boom"


def test_index_route_translates_unhandled_error(client, service: WrapService) -> None:
    service.upload_files([_upload("cover.png", _png_bytes(512, 512))])
    with patch("teslausb_web.blueprints.wraps.render_template", side_effect=RuntimeError("boom")):
        response = client.get("/wraps/")
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "Internal server error"


def test_download_route_returns_png_attachment(client, wraps_dir: Path) -> None:
    (wraps_dir / "cover.png").write_bytes(_png_bytes(512, 512))

    response = client.get("/wraps/download/library/cover.png")

    assert response.status_code == HTTPStatus.OK
    assert response.mimetype == "image/png"
    assert "attachment;" in response.headers["Content-Disposition"]
    assert "cover.png" in response.headers["Content-Disposition"]


def test_download_route_does_not_schedule_cache(client, wraps_dir: Path, invalidator) -> None:
    (wraps_dir / "cover.png").write_bytes(_png_bytes(512, 512))

    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.get("/wraps/download/library/cover.png")

    assert response.status_code == HTTPStatus.OK
    schedule_mock.assert_not_called()


def test_download_route_returns_redirect_when_missing(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.get("/wraps/download/library/missing.png")
    assert response.status_code == HTTPStatus.FOUND
    schedule_mock.assert_not_called()


def test_download_route_rejects_traversal_for_xhr(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.get("/wraps/download/library/..%5Cevil.png", headers=_XHR)
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"].startswith("Invalid filename")
    schedule_mock.assert_not_called()


def test_download_route_translates_wrap_file_error(client, service: WrapService) -> None:
    with patch.object(service, "list_wraps", side_effect=WrapFileError("boom")):
        response = client.get("/wraps/download/library/cover.png", headers=_XHR)
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "boom"


def test_download_route_translates_unhandled_error(client, service: WrapService) -> None:
    with patch.object(service, "list_wraps", side_effect=RuntimeError("boom")):
        response = client.get("/wraps/download/library/cover.png", headers=_XHR)
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "Internal server error"


def test_upload_route_saves_wrap_and_schedules_cache(client, wraps_dir: Path, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/wraps/upload",
            data=_upload_data("wrap_file", "cover.png", _png_bytes(512, 512)),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {
        "success": True,
        "message": "Successfully uploaded 1 wrap(s)",
        "file_count": 1,
    }
    assert (wraps_dir / "cover.png").is_file()
    schedule_mock.assert_called_once_with()


def test_upload_route_non_xhr_redirects_and_schedules_cache(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/wraps/upload",
            data=_upload_data("wrap_file", "cover.png", _png_bytes(512, 512)),
            content_type="multipart/form-data",
        )
    assert response.status_code == HTTPStatus.FOUND
    assert "/wraps/?_=" in response.headers["Location"]
    schedule_mock.assert_called_once_with()


def test_upload_route_returns_400_when_missing_file(client) -> None:
    response = client.post("/wraps/upload", data={}, headers=_XHR)
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "No file selected"


def test_upload_route_returns_400_when_filename_empty(client) -> None:
    response = client.post(
        "/wraps/upload",
        data=_upload_data("wrap_file", "", b""),
        headers=_XHR,
        content_type="multipart/form-data",
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "No file selected"


def test_upload_route_rejects_path_traversal(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/wraps/upload",
            data=_upload_data("wrap_file", "..\\evil.png", _png_bytes(512, 512)),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"].startswith("Invalid filename")
    schedule_mock.assert_not_called()


def test_upload_route_rejects_wrong_dimensions(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/wraps/upload",
            data=_upload_data("wrap_file", "rect.png", _png_bytes(512, 768)),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert "square" in response.get_json()["error"]
    schedule_mock.assert_not_called()


def test_upload_route_returns_413_for_oversize_png(client, invalidator) -> None:
    payload = _png_bytes(512, 512) + (b"x" * (1 * 1024 * 1024))
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/wraps/upload",
            data=_upload_data("wrap_file", "big.png", payload),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    schedule_mock.assert_not_called()


def test_upload_route_rejects_when_library_is_full(
    client, invalidator, service: WrapService
) -> None:
    service.upload_files([_upload(f"wrap{index}.png", _png_bytes(512, 512)) for index in range(10)])

    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/wraps/upload",
            data=_upload_data("wrap_file", "extra.png", _png_bytes(512, 512)),
            headers=_XHR,
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "Maximum of 10 wraps allowed. Delete some wraps first."
    schedule_mock.assert_not_called()


def test_upload_route_translates_wrap_file_error(client, service: WrapService) -> None:
    with patch.object(service, "upload_files", side_effect=WrapFileError("disk full")):
        response = client.post(
            "/wraps/upload",
            data=_upload_data("wrap_file", "cover.png", _png_bytes(512, 512)),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "disk full"


def test_upload_multiple_route_returns_v1_aggregate_shape(
    client, wraps_dir: Path, invalidator
) -> None:
    files = [
        ("one.png", _png_bytes(512, 512)),
        ("bad.txt", b"bad"),
        ("two.png", _png_bytes(1024, 1024)),
    ]
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/wraps/upload_multiple",
            data=_multi_upload_data(files),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    body = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert body["success"] is True
    assert body["total_uploaded"] == 2
    assert body["summary"] == "Successfully uploaded 2 wrap(s) from 2/3 file(s)"
    assert len(body["results"]) == 3
    assert body["results"][0]["dimensions"] == "512x512"
    assert body["results"][1]["success"] is False
    assert body["results"][1]["dimensions"] is None
    assert (wraps_dir / "one.png").is_file()
    assert (wraps_dir / "two.png").is_file()
    schedule_mock.assert_called_once_with()


def test_upload_multiple_route_hits_no_results_branch(client) -> None:
    response = client.post(
        "/wraps/upload_multiple",
        data=_multi_upload_data([("", b"")]),
        headers=_XHR,
        content_type="multipart/form-data",
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "No files selected"


def test_upload_multiple_route_returns_400_when_empty(client) -> None:
    response = client.post("/wraps/upload_multiple", data={}, headers=_XHR)
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "No files selected"


def test_upload_multiple_route_does_not_schedule_cache_when_all_rejected(
    client, invalidator
) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/wraps/upload_multiple",
            data=_multi_upload_data([("rect.png", _png_bytes(512, 768))]),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["success"] is False
    assert response.get_json()["total_uploaded"] == 0
    schedule_mock.assert_not_called()


def test_upload_multiple_route_enforces_library_max_count(
    client, invalidator, service: WrapService
) -> None:
    service.upload_files([_upload(f"wrap{index}.png", _png_bytes(512, 512)) for index in range(9)])

    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/wraps/upload_multiple",
            data=_multi_upload_data(
                [
                    ("ten.png", _png_bytes(512, 512)),
                    ("eleven.png", _png_bytes(512, 512)),
                ]
            ),
            headers=_XHR,
            content_type="multipart/form-data",
        )

    body = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert body["total_uploaded"] == 1
    assert body["results"][1]["message"] == "Maximum of 10 wraps allowed"
    schedule_mock.assert_called_once_with()


def test_upload_multiple_route_non_xhr_failure_redirects(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/wraps/upload_multiple",
            data=_multi_upload_data([("rect.png", _png_bytes(512, 768))]),
            content_type="multipart/form-data",
        )
    assert response.status_code == HTTPStatus.FOUND
    assert "/wraps/?_=" in response.headers["Location"]
    schedule_mock.assert_not_called()


def test_upload_multiple_route_translates_wrap_error(client, service: WrapService) -> None:
    with patch.object(service, "get_wrap_count", side_effect=WrapError("bad")):
        response = client.post(
            "/wraps/upload_multiple",
            data=_multi_upload_data([("cover.png", _png_bytes(512, 512))]),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "bad"


def test_upload_multiple_route_translates_value_error(client, service: WrapService) -> None:
    with patch.object(service, "get_wrap_count", side_effect=ValueError("bad")):
        response = client.post(
            "/wraps/upload_multiple",
            data=_multi_upload_data([("cover.png", _png_bytes(512, 512))]),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "bad"


def test_upload_multiple_route_translates_wrap_file_error(client, service: WrapService) -> None:
    with patch.object(service, "upload_files", side_effect=WrapFileError("disk full")):
        response = client.post(
            "/wraps/upload_multiple",
            data=_multi_upload_data([("cover.png", _png_bytes(512, 512))]),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "disk full"


def test_upload_multiple_route_translates_unhandled_error(client, service: WrapService) -> None:
    with patch.object(service, "get_wrap_count", side_effect=RuntimeError("boom")):
        response = client.post(
            "/wraps/upload_multiple",
            data=_multi_upload_data([("cover.png", _png_bytes(512, 512))]),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "Internal server error"


def test_delete_route_removes_wrap_and_schedules_cache(
    client, wraps_dir: Path, invalidator, service: WrapService
) -> None:
    service.upload_files([_upload("cover.png", _png_bytes(512, 512))])

    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/wraps/delete/library/cover.png", headers=_XHR)

    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {
        "success": True,
        "message": "Deleted cover.png",
        "deleted_count": 1,
    }
    assert not (wraps_dir / "cover.png").exists()
    schedule_mock.assert_called_once_with()


def test_delete_route_returns_400_when_missing(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/wraps/delete/library/missing.png", headers=_XHR)
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "File not found"
    schedule_mock.assert_not_called()


def test_delete_route_rejects_traversal(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/wraps/delete/library/..%5Cevil.png", headers=_XHR)
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"].startswith("Invalid filename")
    schedule_mock.assert_not_called()


def test_delete_route_translates_wrap_file_error(client, service: WrapService) -> None:
    with patch.object(service, "delete_wrap", side_effect=WrapFileError("cannot delete")):
        response = client.post("/wraps/delete/library/cover.png", headers=_XHR)
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "cannot delete"


def test_delete_route_translates_unhandled_error(client, service: WrapService) -> None:
    with patch.object(service, "delete_wrap", side_effect=RuntimeError("boom")):
        response = client.post("/wraps/delete/library/cover.png", headers=_XHR)
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "Internal server error"


def test_bulk_delete_route_deletes_files_and_schedules_cache(
    client, wraps_dir: Path, invalidator, service: WrapService
) -> None:
    service.upload_files(
        [
            _upload("one.png", _png_bytes(512, 512)),
            _upload("two.png", _png_bytes(512, 512)),
        ]
    )

    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/wraps/bulk_delete",
            json={"filenames": ["one.png", "two.png"]},
        )

    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {
        "success": True,
        "message": "Deleted 2 wrap(s)",
        "deleted_count": 2,
    }
    assert not (wraps_dir / "one.png").exists()
    assert not (wraps_dir / "two.png").exists()
    schedule_mock.assert_called_once_with()


def test_bulk_delete_route_accepts_form_files_key(
    client, invalidator, service: WrapService
) -> None:
    service.upload_files([_upload("one.png", _png_bytes(512, 512))])

    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/wraps/bulk_delete",
            data={"files": ["one.png"]},
            headers=_XHR,
        )

    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["deleted_count"] == 1
    schedule_mock.assert_called_once_with()


def test_bulk_delete_route_returns_400_when_empty(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/wraps/bulk_delete", json={})
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "No files selected"
    schedule_mock.assert_not_called()


def test_bulk_delete_route_rejects_traversal(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/wraps/bulk_delete",
            json={"filenames": ["..\\evil.png"]},
        )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"].startswith("Invalid filename")
    schedule_mock.assert_not_called()


def test_bulk_delete_route_translates_wrap_file_error(client, service: WrapService) -> None:
    with patch.object(service, "bulk_delete", side_effect=WrapFileError("disk full")):
        response = client.post(
            "/wraps/bulk_delete",
            json={"filenames": ["one.png"]},
        )
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "disk full"


def test_bulk_delete_route_translates_unhandled_error(client, service: WrapService) -> None:
    with patch.object(service, "bulk_delete", side_effect=RuntimeError("boom")):
        response = client.post(
            "/wraps/bulk_delete",
            json={"filenames": ["one.png"]},
        )
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "Internal server error"


def test_bulk_delete_route_does_not_schedule_cache_on_failed_result(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/wraps/bulk_delete",
            json={"filenames": ["missing.png"]},
        )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "missing.png: File not found"
    schedule_mock.assert_not_called()
