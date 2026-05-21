# ruff: noqa: ANN001  # pytest injects fixtures dynamically in test signatures.
"""Tests for the light-shows blueprint."""

from __future__ import annotations

import json
import zipfile
from http import HTTPStatus
from io import BytesIO
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from teslausb_web.app import create_app
from teslausb_web.blueprints.light_shows import (
    _build_show_groups,
    _flash_or_json_error,
    _flash_or_json_success,
    _format_size_bytes,
    _get_service,
    _index_context,
    _invalidate_caches,
    _matching_files_for_base_name,
    _mimetype_for_filename,
    _request_list,
    _safe_base_name,
    _safe_library_filename,
    _safe_plain_name,
    _safe_zip_filename,
    _single_upload_result,
)
from teslausb_web.config import (
    FeaturesSection,
    LightShowsSection,
    PathsSection,
    WebConfig,
    WebSection,
)
from teslausb_web.services.light_show_service import (
    LightShowError,
    LightShowFileError,
    LightShowService,
)
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
    (backing_root / "lightshow" / "LightShow").mkdir(parents=True)
    state_dir.mkdir()
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32, max_upload_mb=8, max_chunk_mb=1),
        paths=PathsSection(
            backing_root=backing_root,
            state_dir=state_dir,
            cache_invalidate_script=tmp_path / "invalidate.sh",
        ),
        features=FeaturesSection(),
        light_shows=LightShowsSection(max_upload_size=1024, max_zip_size=2048),
        source_path=None,
    )
    flask_app = create_app(cfg)
    flask_app.testing = True
    return flask_app


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def service(app) -> LightShowService:
    return app.extensions["light_show_service"]


@pytest.fixture
def invalidator(app: Flask) -> CacheInvalidator:
    return app.extensions["cache_invalidator"]


@pytest.fixture
def light_show_dir(app) -> Path:
    cfg = app.config["teslausb_config"]
    path = cfg.paths.backing_root / "lightshow" / cfg.light_shows.folder
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def active_show_path(app) -> Path:
    cfg = app.config["teslausb_config"]
    return cfg.paths.state_dir / cfg.light_shows.active_show_relpath


def _upload_data(field: str, filename: str, payload: bytes) -> dict[str, object]:
    return {field: (BytesIO(payload), filename)}


def _multi_upload_data(files: list[tuple[str, bytes]]) -> dict[str, object]:
    return {"show_files": [(BytesIO(payload), filename) for filename, payload in files]}


def _build_zip(entries: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _read_active(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def test_index_route_renders_template_charter_compliant(client, service: LightShowService) -> None:
    service.upload_files([_file_upload("show.fseq", b"fseq"), _file_upload("show.wav", b"wav")])
    service.set_active_show("show.fseq")

    response = client.get("/light_shows/")

    assert response.status_code == HTTPStatus.OK
    assert b"<title>" in response.data
    assert b"Light Shows" in response.data
    assert b"Edit Mode" not in response.data
    assert b"Present Mode" not in response.data
    assert b"quick_edit" not in response.data
    assert b"cdn.jsdelivr.net" not in response.data
    assert b"unpkg.com" not in response.data
    assert b"<svg" in response.data


def test_helper_invalidate_caches_is_noop_without_extension(app: Flask) -> None:
    invalidator = app.extensions.pop("cache_invalidator")
    _invalidate_caches(app)
    app.extensions["cache_invalidator"] = invalidator


def test_helper_get_service_rejects_misconfigured_extension(app: Flask) -> None:
    with app.app_context():
        original = app.extensions["light_show_service"]
        app.extensions["light_show_service"] = object()
        with pytest.raises(RuntimeError, match="light_show_service"):
            _get_service()
        app.extensions["light_show_service"] = original


def test_helper_flash_or_json_variants_cover_redirect_paths(
    app: Flask, invalidator: CacheInvalidator
) -> None:
    with app.test_request_context("/light_shows/upload", method="POST"):
        error_response = _flash_or_json_error("boom", HTTPStatus.BAD_REQUEST)
        assert error_response.status_code == 302
    with app.test_request_context("/light_shows/upload", method="POST"):
        with patch.object(invalidator, "schedule") as schedule_mock:
            success_response = _flash_or_json_success("done")
        assert success_response.status_code == 302
        schedule_mock.assert_called_once_with()


def test_helper_name_validation_and_size_formatting(app: Flask) -> None:
    with app.app_context(), pytest.raises(LightShowError, match="Filename is required"):
        _safe_plain_name(" ")
    with app.app_context(), pytest.raises(LightShowError, match="Invalid light show name"):
        _safe_base_name("show.wav")
    with app.app_context(), pytest.raises(LightShowError, match="Only fseq, mp3, and wav"):
        _safe_library_filename("bad.txt")
    with app.app_context(), pytest.raises(LightShowError, match=r"Filename must end with \.zip"):
        _safe_zip_filename("bad.wav")
    assert _format_size_bytes(1536) == "1.5 KB"
    assert _format_size_bytes(2 * 1024 * 1024) == "2.00 MB"


def test_helper_index_context_and_grouping(app: Flask, service: LightShowService) -> None:
    service.upload_files([_file_upload("show.fseq", b"fseq"), _file_upload("show.wav", b"wav")])
    service.set_active_show("show.fseq")
    with app.app_context():
        context = _index_context()
    groups = _build_show_groups(service.list_files(), active_show="show.fseq")
    assert context["media_tab"] == "shows"
    assert context["active_show"] == "show.fseq"
    assert groups[0]["is_active"] is True


def test_helper_request_list_handles_json_scalar_and_form(app: Flask) -> None:
    with app.test_request_context(
        "/light_shows/bulk_delete", method="POST", json={"files": "one.wav"}
    ):
        assert _request_list("filenames", "files") == ["one.wav"]
    with app.test_request_context(
        "/light_shows/bulk_delete",
        method="POST",
        data={"base_names": ["show", "other"]},
    ):
        assert _request_list("base_names") == ["show", "other"]


def test_helper_matching_files_and_upload_dispatch(app: Flask, service: LightShowService) -> None:
    service.upload_files([_file_upload("show.wav", b"wav")])
    with app.app_context():
        assert _matching_files_for_base_name("show")[0].filename == "show.wav"
        assert _mimetype_for_filename("show.wav") == "audio/wav"
        result = _single_upload_result(FileStorage(stream=BytesIO(b""), filename=""))
        assert result.message == "No file selected"
        with pytest.raises(LightShowError, match="File not found"):
            _matching_files_for_base_name("missing")


def test_list_route_returns_grouped_files_and_active_show(
    client, service: LightShowService
) -> None:
    service.upload_files(
        [
            _file_upload("show.fseq", b"fseq"),
            _file_upload("show.mp3", b"mp3"),
            _file_upload("other.wav", b"wav"),
        ]
    )
    service.set_active_show("show.fseq")

    response = client.get("/light_shows/list")
    body = response.get_json()

    assert response.status_code == 200
    assert body["success"] is True
    assert body["active_show"] == "show.fseq"
    assert len(body["show_groups"]) == 2
    assert body["show_groups"][0]["partition_key"] == "library"
    assert body["show_groups"][0]["partition"] == "LightShow"


def test_list_route_returns_empty_payload_when_library_empty(client) -> None:
    response = client.get("/light_shows/list")
    body = response.get_json()
    assert response.status_code == 200
    assert body["files"] == []
    assert body["show_groups"] == []
    assert body["active_show"] is None


def test_list_route_does_not_schedule_cache(client, invalidator, service: LightShowService) -> None:
    service.upload_files([_file_upload("show.fseq", b"fseq")])
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.get("/light_shows/list")
    assert response.status_code == 200
    schedule_mock.assert_not_called()


def test_list_route_translates_light_show_file_error(client, service: LightShowService) -> None:
    with patch.object(service, "list_files", side_effect=LightShowFileError("boom")):
        response = client.get("/light_shows/list")
    assert response.status_code == 500
    assert response.get_json()["error"] == "boom"


def test_active_route_returns_active_show_name(client, service: LightShowService) -> None:
    service.upload_files([_file_upload("show.fseq", b"fseq")])
    service.set_active_show("show.fseq")
    response = client.get("/light_shows/active")
    assert response.status_code == 200
    assert response.get_json() == {"success": True, "filename": "show.fseq"}


def test_active_route_returns_none_when_unset(client) -> None:
    response = client.get("/light_shows/active")
    assert response.status_code == 200
    assert response.get_json()["filename"] is None


def test_active_route_translates_light_show_file_error(client, service: LightShowService) -> None:
    with patch.object(service, "get_active_show", side_effect=LightShowFileError("state failed")):
        response = client.get("/light_shows/active")
    assert response.status_code == 500


def test_play_route_streams_audio_file(client, light_show_dir: Path) -> None:
    (light_show_dir / "show.mp3").write_bytes(b"mp3")
    response = client.get("/light_shows/play/library/show.mp3")
    assert response.status_code == 200
    assert response.mimetype == "audio/mpeg"


def test_play_route_rejects_path_traversal(client) -> None:
    response = client.get("/light_shows/play/library/..%5Cevil.mp3")
    assert response.status_code == 400


def test_play_route_rejects_non_audio_extension(client, light_show_dir: Path) -> None:
    (light_show_dir / "show.fseq").write_bytes(b"fseq")
    response = client.get("/light_shows/play/library/show.fseq")
    assert response.status_code == 400


def test_play_route_returns_404_for_missing_file(client) -> None:
    response = client.get("/light_shows/play/library/missing.wav")
    assert response.status_code == 404


def test_download_route_returns_zip_attachment(client, light_show_dir: Path) -> None:
    (light_show_dir / "show.fseq").write_bytes(b"fseq")
    (light_show_dir / "show.wav").write_bytes(b"wav")

    response = client.get("/light_shows/download/library/show")

    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    assert "attachment;" in response.headers["Content-Disposition"]
    assert "show.zip" in response.headers["Content-Disposition"]


def test_download_route_rejects_path_traversal(client) -> None:
    response = client.get("/light_shows/download/library/..%5Cevil")
    assert response.status_code == 302


def test_download_route_returns_redirect_when_missing_show(client) -> None:
    response = client.get("/light_shows/download/library/missing")
    assert response.status_code == 302


def test_upload_route_saves_single_file_and_schedules_cache(
    client, light_show_dir: Path, invalidator
) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/light_shows/upload",
            data=_upload_data("show_file", "new.wav", b"wav"),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert (light_show_dir / "new.wav").read_bytes() == b"wav"
    schedule_mock.assert_called_once_with()


def test_upload_route_accepts_multiple_non_zip_files(
    client, light_show_dir: Path, invalidator
) -> None:
    data = {
        "show_files": [(BytesIO(b"one"), "one.wav"), (BytesIO(b"two"), "two.fseq")],
    }
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/light_shows/upload",
            data=data,
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == 200
    assert (light_show_dir / "one.wav").is_file()
    assert (light_show_dir / "two.fseq").is_file()
    schedule_mock.assert_called_once_with()


def test_upload_route_accepts_zip_file(client, light_show_dir: Path, invalidator) -> None:
    payload = _build_zip({"nested/show.fseq": b"fseq", "nested/show.mp3": b"mp3"})
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/light_shows/upload",
            data=_upload_data("show_file", "shows.zip", payload),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == 200
    assert (light_show_dir / "show.fseq").is_file()
    assert (light_show_dir / "show.mp3").is_file()
    schedule_mock.assert_called_once_with()


def test_upload_route_returns_400_when_file_missing(client) -> None:
    response = client.post("/light_shows/upload", data={}, headers=_XHR)
    assert response.status_code == 400
    assert response.get_json()["error"] == "No file selected"


def test_upload_route_returns_400_when_filename_empty(client) -> None:
    response = client.post(
        "/light_shows/upload",
        data=_upload_data("show_file", "", b""),
        headers=_XHR,
        content_type="multipart/form-data",
    )
    assert response.status_code == 400


def test_upload_route_returns_413_for_oversize_payload(client) -> None:
    response = client.post(
        "/light_shows/upload",
        data=_upload_data("show_file", "big.wav", b"x" * 1025),
        headers=_XHR,
        content_type="multipart/form-data",
    )
    assert response.status_code == 413


def test_upload_route_translates_light_show_file_error(client, service: LightShowService) -> None:
    with patch.object(service, "upload_files", side_effect=LightShowFileError("disk full")):
        response = client.post(
            "/light_shows/upload",
            data=_upload_data("show_file", "new.wav", b"wav"),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == 500
    assert response.get_json()["error"] == "disk full"


def test_upload_route_does_not_schedule_cache_on_failure(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/light_shows/upload",
            data=_upload_data("show_file", "bad.txt", b"bad"),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == 400
    schedule_mock.assert_not_called()


def test_upload_multiple_route_returns_v1_aggregate_shape(
    client, light_show_dir: Path, invalidator
) -> None:
    payload = _build_zip({"show.fseq": b"fseq"})
    files = [("show.wav", b"wav"), ("archive.zip", payload), ("bad.txt", b"bad")]
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/light_shows/upload_multiple",
            data=_multi_upload_data(files),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    body = response.get_json()
    assert response.status_code == 200
    assert body["success"] is True
    assert body["total_uploaded"] == 2
    assert len(body["results"]) == 3
    assert body["summary"] == "Successfully uploaded 2 file(s) from 2/3 submission(s)"
    assert (light_show_dir / "show.wav").is_file()
    assert (light_show_dir / "show.fseq").is_file()
    schedule_mock.assert_called_once_with()


def test_upload_multiple_route_returns_400_when_empty(client) -> None:
    response = client.post("/light_shows/upload_multiple", data={}, headers=_XHR)
    assert response.status_code == 400
    assert response.get_json()["error"] == "No files selected"


def test_upload_multiple_route_does_not_schedule_cache_when_all_rejected(
    client, invalidator
) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/light_shows/upload_multiple",
            data=_multi_upload_data([("bad.txt", b"bad")]),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == 200
    assert response.get_json()["success"] is False
    schedule_mock.assert_not_called()


def test_upload_multiple_route_translates_light_show_file_error(
    client, service: LightShowService
) -> None:
    with patch.object(service, "upload_files", side_effect=LightShowFileError("boom")):
        response = client.post(
            "/light_shows/upload_multiple",
            data=_multi_upload_data([("show.wav", b"wav")]),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == 500


def test_upload_zip_route_succeeds_and_schedules_cache(
    client, light_show_dir: Path, invalidator
) -> None:
    payload = _build_zip({"show.fseq": b"fseq", "show.wav": b"wav"})
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/light_shows/upload_zip",
            data=_upload_data("show_file", "shows.zip", payload),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == 200
    assert (light_show_dir / "show.fseq").is_file()
    assert (light_show_dir / "show.wav").is_file()
    schedule_mock.assert_called_once_with()


def test_upload_zip_route_rejects_missing_file(client) -> None:
    response = client.post("/light_shows/upload_zip", data={}, headers=_XHR)
    assert response.status_code == 400


def test_upload_zip_route_rejects_bad_extension(client) -> None:
    response = client.post(
        "/light_shows/upload_zip",
        data=_upload_data("show_file", "shows.wav", b"wav"),
        headers=_XHR,
        content_type="multipart/form-data",
    )
    assert response.status_code == 400


def test_upload_zip_route_returns_400_for_invalid_zip(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/light_shows/upload_zip",
            data=_upload_data("show_file", "shows.zip", b"not-a-zip"),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == 400
    schedule_mock.assert_not_called()


def test_upload_zip_route_translates_light_show_file_error(
    client, service: LightShowService
) -> None:
    with patch.object(service, "upload_zip", side_effect=LightShowFileError("disk full")):
        response = client.post(
            "/light_shows/upload_zip",
            data=_upload_data("show_file", "shows.zip", _build_zip({"a.wav": b"wav"})),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == 500


def test_delete_group_route_removes_matching_files_and_schedules_cache(
    client,
    light_show_dir: Path,
    invalidator,
    service: LightShowService,
) -> None:
    service.upload_files([_file_upload("show.fseq", b"fseq"), _file_upload("show.wav", b"wav")])
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/light_shows/delete/library/show", headers=_XHR)
    assert response.status_code == 200
    assert not (light_show_dir / "show.fseq").exists()
    assert not (light_show_dir / "show.wav").exists()
    schedule_mock.assert_called_once_with()


def test_delete_group_route_returns_400_when_missing(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/light_shows/delete/library/missing", headers=_XHR)
    assert response.status_code == 400
    schedule_mock.assert_not_called()


def test_delete_group_route_rejects_traversal(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/light_shows/delete/library/..%5Cevil", headers=_XHR)
    assert response.status_code == 400
    schedule_mock.assert_not_called()


def test_delete_filename_route_removes_single_file_and_schedules_cache(
    client,
    light_show_dir: Path,
    invalidator,
    service: LightShowService,
) -> None:
    service.upload_files([_file_upload("show.wav", b"wav")])
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/light_shows/delete/show.wav", headers=_XHR)
    assert response.status_code == 200
    assert not (light_show_dir / "show.wav").exists()
    schedule_mock.assert_called_once_with()


def test_delete_filename_route_translates_light_show_file_error(
    client, service: LightShowService
) -> None:
    with patch.object(service, "delete_file", side_effect=LightShowFileError("cannot delete")):
        response = client.post("/light_shows/delete/show.wav", headers=_XHR)
    assert response.status_code == 500


def test_bulk_delete_route_deletes_by_filename_and_schedules_cache(
    client,
    light_show_dir: Path,
    invalidator,
    service: LightShowService,
) -> None:
    service.upload_files([_file_upload("one.wav", b"1"), _file_upload("two.fseq", b"2")])
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/light_shows/bulk_delete", json={"filenames": ["one.wav", "two.fseq"]}
        )
    assert response.status_code == 200
    assert not (light_show_dir / "one.wav").exists()
    assert not (light_show_dir / "two.fseq").exists()
    schedule_mock.assert_called_once_with()


def test_bulk_delete_route_deletes_by_base_name(
    client, light_show_dir: Path, invalidator, service
) -> None:
    service.upload_files([_file_upload("show.fseq", b"fseq"), _file_upload("show.mp3", b"mp3")])
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/light_shows/bulk_delete", json={"base_names": ["show"]})
    assert response.status_code == 200
    assert not (light_show_dir / "show.fseq").exists()
    assert not (light_show_dir / "show.mp3").exists()
    schedule_mock.assert_called_once_with()


def test_bulk_delete_route_returns_400_when_empty(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/light_shows/bulk_delete", json={})
    assert response.status_code == 400
    schedule_mock.assert_not_called()


def test_bulk_delete_route_translates_light_show_file_error(
    client, service: LightShowService
) -> None:
    with patch.object(service, "bulk_delete", side_effect=LightShowFileError("disk full")):
        response = client.post("/light_shows/bulk_delete", json={"filenames": ["show.wav"]})
    assert response.status_code == 500


def test_set_active_route_persists_selection_and_schedules_cache(
    client,
    active_show_path: Path,
    invalidator,
    service: LightShowService,
) -> None:
    service.upload_files([_file_upload("show.fseq", b"fseq")])
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/light_shows/set_active/show.fseq", headers=_XHR)
    assert response.status_code == 200
    assert _read_active(active_show_path) == {"filename": "show.fseq"}
    schedule_mock.assert_called_once_with()


def test_set_active_route_rejects_path_traversal(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/light_shows/set_active/..%5Cevil.fseq", headers=_XHR)
    assert response.status_code == 400
    schedule_mock.assert_not_called()


def test_set_active_route_returns_400_for_missing_file(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/light_shows/set_active/missing.fseq", headers=_XHR)
    assert response.status_code == 400
    schedule_mock.assert_not_called()


def test_set_active_route_translates_light_show_file_error(
    client, service: LightShowService
) -> None:
    with patch.object(service, "set_active_show", side_effect=LightShowFileError("state failed")):
        response = client.post("/light_shows/set_active/show.fseq", headers=_XHR)
    assert response.status_code == 500


def _file_upload(name: str, payload: bytes) -> FileStorage:
    return FileStorage(stream=BytesIO(payload), filename=name)
