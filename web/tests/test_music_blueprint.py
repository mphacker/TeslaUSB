# ruff: noqa: ANN001  # pytest injects fixtures dynamically in test signatures.
"""Tests for the music blueprint."""

from __future__ import annotations

from http import HTTPStatus
from io import BytesIO
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from teslausb_web.app import create_app
from teslausb_web.blueprints.music import (
    _format_size_bytes,
    _get_service,
    _index_context,
    _invalidate_caches,
    _mime_type_for_path,
    _music_response,
    _redirect_to_music,
    _request_value,
)
from teslausb_web.config import FeaturesSection, MusicSection, PathsSection, WebConfig, WebSection
from teslausb_web.services.music_service import MusicError, MusicFileError, MusicService
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
    (backing_root / "Music").mkdir(parents=True)
    state_dir.mkdir()
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32, max_upload_mb=8, max_chunk_mb=1),
        paths=PathsSection(
            backing_root=backing_root,
            state_dir=state_dir,
            cache_invalidate_script=tmp_path / "invalidate.sh",
        ),
        features=FeaturesSection(),
        music=MusicSection(
            folder="Music",
            max_file_size=1024,
            chunk_size=8,
            free_space_reserve=0,
            stale_chunk_age=30,
            allowed_extensions=(".mp3", ".flac", ".wav", ".aac", ".m4a"),
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
def service(app) -> MusicService:
    return app.extensions["music_service"]


@pytest.fixture
def invalidator(app: Flask) -> CacheInvalidator:
    return app.extensions["cache_invalidator"]


@pytest.fixture
def music_dir(app) -> Path:
    cfg = app.config["teslausb_config"]
    path = cfg.paths.backing_root / cfg.music.folder
    path.mkdir(parents=True, exist_ok=True)
    return path


def _upload(name: str, payload: bytes) -> FileStorage:
    return FileStorage(stream=BytesIO(payload), filename=name)


def _upload_data(filename: str, payload: bytes, *, path: str = "") -> dict[str, object]:
    data: dict[str, object] = {"music_files": [(BytesIO(payload), filename)]}
    if path:
        data["path"] = path
    return data


def _upload_multiple_data(files: list[tuple[str, bytes]], *, path: str = "") -> dict[str, object]:
    data: dict[str, object] = {
        "music_files": [(BytesIO(payload), filename) for filename, payload in files]
    }
    if path:
        data["path"] = path
    return data


def test_app_registers_music_blueprint_and_service(app: Flask) -> None:
    assert "music" in app.blueprints
    assert isinstance(app.extensions["music_service"], MusicService)


def test_helper_invalidate_caches_is_noop_without_extension(app: Flask) -> None:
    invalidator = app.extensions.pop("cache_invalidator")
    _invalidate_caches(app)
    app.extensions["cache_invalidator"] = invalidator


def test_helper_get_service_rejects_misconfigured_extension(app: Flask) -> None:
    with app.app_context():
        original = app.extensions["music_service"]
        app.extensions["music_service"] = object()
        with pytest.raises(RuntimeError, match="music_service"):
            _get_service()
        app.extensions["music_service"] = original


def test_helper_index_context_and_formatting(app: Flask, service: MusicService) -> None:
    service.create_directory("", "Albums")
    service.save_file(_upload("song.mp3", b"music"), "Albums")

    with app.app_context():
        listing = service.list_files("Albums")
        context = _index_context(listing)

    assert context["media_tab"] == "music"
    assert context["current_path"] == "Albums"
    assert context["files"][0]["path"] == "Albums/song.mp3"
    assert _format_size_bytes(1) == "1 B"
    assert _format_size_bytes(1536) == "1.5 KB"
    assert _mime_type_for_path("track.m4a") == "audio/mp4"
    assert _mime_type_for_path("track.bin") == "application/octet-stream"


def test_helper_request_and_redirect_variants(app: Flask) -> None:
    with app.test_request_context("/music/upload?path=Albums&_=7", method="POST"):
        assert _request_value("path") == "Albums"
        assert _redirect_to_music().headers["Location"] == "/music/"
        assert _redirect_to_music(path="Albums").headers["Location"] == "/music/?path=Albums"
        assert _redirect_to_music(cache_bust="7").headers["Location"] == "/music/?_=7"
        assert (
            _redirect_to_music(path="Albums", cache_bust="7").headers["Location"]
            == "/music/?path=Albums&_=7"
        )


def test_helper_music_response_redirects_for_non_xhr(app: Flask) -> None:
    with app.test_request_context("/music/delete/song.mp3?_=9", method="POST"):
        response = _music_response(
            success=False,
            message="boom",
            status=HTTPStatus.BAD_REQUEST,
            path="Albums",
        )
    assert response.status_code == HTTPStatus.FOUND
    assert response.headers["Location"] == "/music/?path=Albums&_=9"


def test_music_home_route_renders_template_charter_compliant(client, service: MusicService) -> None:
    service.create_directory("", "Albums")
    service.save_file(_upload("song.mp3", b"music"), "Albums")

    response = client.get("/music/?path=Albums")

    assert response.status_code == HTTPStatus.OK
    assert b"<title>" in response.data
    assert b"Music Library" in response.data
    assert b"Edit Mode" not in response.data
    assert b"Present Mode" not in response.data
    assert b"quick_edit" not in response.data
    assert b"cdn.jsdelivr.net" not in response.data
    assert b"unpkg.com" not in response.data
    assert b"<svg" in response.data


def test_music_home_route_success_with_patched_template(client, service: MusicService) -> None:
    service.create_directory("", "Albums")
    with patch("teslausb_web.blueprints.music.render_template", return_value="ok"):
        response = client.get("/music/?path=Albums")
    assert response.status_code == HTTPStatus.OK
    assert response.get_data(as_text=True) == "ok"


def test_music_home_route_translates_music_file_error(client, service: MusicService) -> None:
    with patch.object(service, "list_files", side_effect=MusicFileError("boom")):
        response = client.get("/music/")
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "boom"


def test_music_home_route_translates_value_error(client, service: MusicService) -> None:
    with patch.object(service, "list_files", side_effect=ValueError("bad")):
        response = client.get("/music/")
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "bad"


def test_music_home_route_translates_unhandled_error(client) -> None:
    with patch("teslausb_web.blueprints.music.render_template", side_effect=RuntimeError("boom")):
        response = client.get("/music/")
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "Internal server error"


def test_upload_route_saves_music_and_schedules_cache(client, music_dir: Path, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/music/upload",
            data=_upload_multiple_data([("song.mp3", b"payload")]),
            headers=_XHR,
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {
        "success": True,
        "messages": ["Uploaded song.mp3"],
        "uploaded": 1,
    }
    assert (music_dir / "song.mp3").read_bytes() == b"payload"
    schedule_mock.assert_called_once_with()


def test_upload_route_accepts_multiple_files_in_nested_path(
    client, music_dir: Path, invalidator
) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/music/upload",
            data=_upload_multiple_data(
                [("one.mp3", b"1"), ("two.wav", b"2")],
                path="Albums",
            ),
            headers=_XHR,
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["uploaded"] == 2
    assert (music_dir / "Albums" / "one.mp3").is_file()
    assert (music_dir / "Albums" / "two.wav").is_file()
    schedule_mock.assert_called_once_with()


def test_upload_route_returns_400_when_empty(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/music/upload", data={}, headers=_XHR)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "No files selected"
    schedule_mock.assert_not_called()


def test_upload_route_non_xhr_empty_redirects(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/music/upload", data={})

    assert response.status_code == HTTPStatus.FOUND
    assert response.headers["Location"] == "/music/"
    schedule_mock.assert_not_called()


def test_upload_route_non_xhr_success_redirects(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/music/upload",
            query_string={"_": "5"},
            data=_upload_multiple_data([("song.mp3", b"payload")], path="Albums"),
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.FOUND
    assert response.headers["Location"] == "/music/?path=Albums&_=5"
    schedule_mock.assert_called_once_with()


def test_upload_route_rejects_path_traversal(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/music/upload",
            data=_upload_multiple_data([("..\\evil.mp3", b"payload")]),
            headers=_XHR,
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json() == {
        "success": False,
        "messages": ["Invalid filename: '..\\\\evil.mp3'"],
        "uploaded": 0,
    }
    schedule_mock.assert_not_called()


def test_upload_route_rejects_invalid_extension(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/music/upload",
            data=_upload_multiple_data([("bad.txt", b"payload")]),
            headers=_XHR,
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json() == {
        "success": False,
        "messages": ["Unsupported file type. Allowed: mp3, flac, wav, aac, m4a"],
        "uploaded": 0,
    }
    schedule_mock.assert_not_called()


def test_upload_route_rejects_oversize_payload(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/music/upload",
            data=_upload_multiple_data([("big.mp3", b"x" * 1025)]),
            headers=_XHR,
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert "Limit is" in response.get_json()["messages"][0]
    schedule_mock.assert_not_called()


def test_upload_route_translates_music_file_error(client, service: MusicService) -> None:
    with patch.object(service, "save_file", side_effect=MusicFileError("disk full")):
        response = client.post(
            "/music/upload",
            data=_upload_multiple_data([("song.mp3", b"payload")]),
            headers=_XHR,
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "disk full"


def test_upload_route_translates_value_error(client, service: MusicService) -> None:
    with patch.object(service, "save_file", side_effect=ValueError("bad")):
        response = client.post(
            "/music/upload",
            data=_upload_multiple_data([("song.mp3", b"payload")]),
            headers=_XHR,
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "bad"


def test_upload_route_translates_music_error(client, service: MusicService) -> None:
    with patch.object(service, "save_file", side_effect=MusicError("bad music")):
        response = client.post(
            "/music/upload",
            data=_upload_multiple_data([("song.mp3", b"payload")]),
            headers=_XHR,
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "bad music"


def test_upload_route_translates_unhandled_error(client, service: MusicService) -> None:
    with patch.object(service, "save_file", side_effect=RuntimeError("boom")):
        response = client.post(
            "/music/upload",
            data=_upload_multiple_data([("song.mp3", b"payload")]),
            headers=_XHR,
            content_type="multipart/form-data",
        )

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "Internal server error"


def test_upload_chunk_stores_intermediate_without_invalidating(
    client, invalidator, music_dir: Path
) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/music/upload_chunk",
            query_string={
                "upload_id": "a" * 32,
                "filename": "chunk.mp3",
                "chunk_index": 0,
                "total_chunks": 2,
                "total_size": 8,
            },
            data=b"1234",
            headers=_XHR,
            content_type="application/octet-stream",
        )

    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {
        "success": True,
        "message": "Chunk stored",
        "finalized": False,
    }
    assert (music_dir / ".uploads" / ("a" * 32 + ".part")).is_file()
    schedule_mock.assert_not_called()


def test_upload_chunk_final_chunk_invalidates_once(client, invalidator, music_dir: Path) -> None:
    first_query = {
        "upload_id": "b" * 32,
        "filename": "chunk.mp3",
        "chunk_index": 0,
        "total_chunks": 2,
        "total_size": 8,
    }
    client.post(
        "/music/upload_chunk",
        query_string=first_query,
        data=b"1234",
        headers=_XHR,
        content_type="application/octet-stream",
    )

    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/music/upload_chunk",
            query_string={**first_query, "chunk_index": 1},
            data=b"5678",
            headers=_XHR,
            content_type="application/octet-stream",
        )

    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {
        "success": True,
        "message": "Uploaded chunk.mp3",
        "finalized": True,
    }
    assert (music_dir / "chunk.mp3").read_bytes() == b"12345678"
    schedule_mock.assert_called_once_with()


def test_upload_chunk_returns_400_for_invalid_metadata(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/music/upload_chunk",
            query_string={
                "filename": "chunk.mp3",
                "chunk_index": "nope",
                "total_chunks": 1,
                "total_size": 4,
            },
            data=b"data",
            headers=_XHR,
            content_type="application/octet-stream",
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "Invalid chunk metadata"
    schedule_mock.assert_not_called()


def test_upload_chunk_returns_400_for_missing_filename(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/music/upload_chunk",
            query_string={"total_chunks": 1, "total_size": 4},
            data=b"data",
            headers=_XHR,
            content_type="application/octet-stream",
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "Missing filename"
    schedule_mock.assert_not_called()


def test_upload_chunk_returns_400_for_missing_file_size(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/music/upload_chunk",
            query_string={"filename": "chunk.mp3", "total_chunks": 1},
            data=b"data",
            headers=_XHR,
            content_type="application/octet-stream",
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "Missing file size"
    schedule_mock.assert_not_called()


def test_upload_chunk_rejects_path_traversal(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/music/upload_chunk",
            query_string={
                "upload_id": "c" * 32,
                "filename": "..\\evil.mp3",
                "chunk_index": 0,
                "total_chunks": 1,
                "total_size": 4,
            },
            data=b"data",
            headers=_XHR,
            content_type="application/octet-stream",
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"].startswith("Invalid")
    schedule_mock.assert_not_called()


def test_upload_chunk_rejects_invalid_extension(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/music/upload_chunk",
            query_string={
                "upload_id": "d" * 32,
                "filename": "bad.txt",
                "chunk_index": 0,
                "total_chunks": 1,
                "total_size": 4,
            },
            data=b"data",
            headers=_XHR,
            content_type="application/octet-stream",
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert (
        response.get_json()["error"] == "Unsupported file type. Allowed: mp3, flac, wav, aac, m4a"
    )
    schedule_mock.assert_not_called()


def test_upload_chunk_rejects_oversize_file(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/music/upload_chunk",
            query_string={
                "upload_id": "e" * 32,
                "filename": "big.mp3",
                "chunk_index": 0,
                "total_chunks": 1,
                "total_size": 1025,
            },
            data=b"data",
            headers=_XHR,
            content_type="application/octet-stream",
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert "Limit is" in response.get_json()["error"]
    schedule_mock.assert_not_called()


def test_upload_chunk_translates_music_file_error(
    client, service: MusicService, invalidator
) -> None:
    with (
        patch.object(service, "handle_chunk", side_effect=MusicFileError("disk full")),
        patch.object(invalidator, "schedule") as schedule_mock,
    ):
        response = client.post(
            "/music/upload_chunk",
            query_string={
                "upload_id": "f" * 32,
                "filename": "song.mp3",
                "chunk_index": 0,
                "total_chunks": 1,
                "total_size": 4,
            },
            data=b"data",
            headers=_XHR,
            content_type="application/octet-stream",
        )

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "disk full"
    schedule_mock.assert_not_called()


def test_upload_chunk_translates_value_error(client, service: MusicService) -> None:
    with patch.object(service, "handle_chunk", side_effect=ValueError("bad chunk")):
        response = client.post(
            "/music/upload_chunk",
            query_string={
                "upload_id": "0" * 32,
                "filename": "song.mp3",
                "chunk_index": 0,
                "total_chunks": 1,
                "total_size": 4,
            },
            data=b"data",
            headers=_XHR,
            content_type="application/octet-stream",
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "bad chunk"


def test_upload_chunk_translates_unhandled_error(client, service: MusicService) -> None:
    with patch.object(service, "handle_chunk", side_effect=RuntimeError("boom")):
        response = client.post(
            "/music/upload_chunk",
            query_string={
                "upload_id": "1" * 32,
                "filename": "song.mp3",
                "chunk_index": 0,
                "total_chunks": 1,
                "total_size": 4,
            },
            data=b"data",
            headers=_XHR,
            content_type="application/octet-stream",
        )

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "Server error"


def test_delete_route_removes_file_and_invalidates(client, music_dir: Path, invalidator) -> None:
    (music_dir / "song.mp3").write_bytes(b"payload")

    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/music/delete/song.mp3", headers=_XHR)

    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {"success": True, "message": "Deleted song.mp3"}
    assert not (music_dir / "song.mp3").exists()
    schedule_mock.assert_called_once_with()


def test_delete_route_returns_400_for_missing_file(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/music/delete/missing.mp3", headers=_XHR)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json() == {"success": False, "message": "File not found"}
    schedule_mock.assert_not_called()


def test_delete_route_rejects_path_traversal(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/music/delete/..%5Cevil.mp3", headers=_XHR)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["message"].startswith("Invalid")
    schedule_mock.assert_not_called()


def test_delete_route_translates_music_file_error(client, service: MusicService) -> None:
    with patch.object(service, "delete_file", side_effect=MusicFileError("cannot delete")):
        response = client.post("/music/delete/song.mp3", headers=_XHR)

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json() == {"success": False, "message": "cannot delete"}


def test_delete_dir_route_removes_directory_and_invalidates(
    client, music_dir: Path, invalidator
) -> None:
    target = music_dir / "Albums"
    target.mkdir()
    (target / "song.mp3").write_bytes(b"payload")

    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/music/delete_dir/Albums", headers=_XHR)

    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {"success": True, "message": "Deleted folder"}
    assert not target.exists()
    schedule_mock.assert_called_once_with()


def test_delete_dir_route_does_not_invalidate_on_failed_result(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/music/delete_dir/", headers=_XHR)

    assert response.status_code == HTTPStatus.NOT_FOUND
    schedule_mock.assert_not_called()


def test_delete_dir_route_returns_400_for_root_folder(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/music/delete_dir/%20", headers=_XHR)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    schedule_mock.assert_not_called()


def test_delete_dir_route_translates_music_file_error(client, service: MusicService) -> None:
    with patch.object(service, "delete_directory", side_effect=MusicFileError("cannot delete")):
        response = client.post("/music/delete_dir/Albums", headers=_XHR)

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json() == {"success": False, "message": "cannot delete"}


def test_move_route_moves_file_and_invalidates(client, music_dir: Path, invalidator) -> None:
    source_dir = music_dir / "source"
    source_dir.mkdir()
    (source_dir / "song.mp3").write_bytes(b"payload")

    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/music/move",
            json={"source": "source/song.mp3", "dest_path": "dest", "new_name": "renamed.mp3"},
            headers=_XHR,
        )

    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {"success": True, "message": "Moved to renamed.mp3"}
    assert not (source_dir / "song.mp3").exists()
    assert (music_dir / "dest" / "renamed.mp3").read_bytes() == b"payload"
    schedule_mock.assert_called_once_with()


def test_move_route_returns_400_for_missing_source(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/music/move",
            json={"source": "missing.mp3", "dest_path": "dest", "new_name": ""},
            headers=_XHR,
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json() == {"success": False, "message": "Source file not found"}
    schedule_mock.assert_not_called()


def test_move_route_rejects_path_traversal(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/music/move",
            json={"source": "..\\evil.mp3", "dest_path": "dest", "new_name": ""},
            headers=_XHR,
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["message"].startswith("Invalid")
    schedule_mock.assert_not_called()


def test_move_route_translates_music_file_error(client, service: MusicService) -> None:
    with patch.object(service, "move_file", side_effect=MusicFileError("cannot move")):
        response = client.post(
            "/music/move",
            json={"source": "song.mp3", "dest_path": "dest", "new_name": ""},
            headers=_XHR,
        )

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json() == {"success": False, "message": "cannot move"}


def test_mkdir_route_creates_folder_and_invalidates(client, music_dir: Path, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/music/mkdir",
            json={"path": "", "name": "Albums"},
            headers=_XHR,
        )

    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {"success": True, "message": "Created folder Albums"}
    assert (music_dir / "Albums").is_dir()
    schedule_mock.assert_called_once_with()


def test_mkdir_route_does_not_invalidate_on_duplicate(client, music_dir: Path, invalidator) -> None:
    (music_dir / "Albums").mkdir()

    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/music/mkdir",
            json={"path": "", "name": "Albums"},
            headers=_XHR,
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json() == {"success": False, "message": "Folder already exists"}
    schedule_mock.assert_not_called()


def test_mkdir_route_rejects_path_traversal(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/music/mkdir",
            json={"path": "", "name": "..\\evil"},
            headers=_XHR,
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["message"].startswith("Invalid")
    schedule_mock.assert_not_called()


def test_mkdir_route_translates_music_file_error(client, service: MusicService) -> None:
    with patch.object(service, "create_directory", side_effect=MusicFileError("cannot mkdir")):
        response = client.post(
            "/music/mkdir",
            json={"path": "", "name": "Albums"},
            headers=_XHR,
        )

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json() == {"success": False, "message": "cannot mkdir"}


def test_play_route_streams_audio_file(client, music_dir: Path) -> None:
    (music_dir / "song.mp3").write_bytes(b"payload")

    response = client.get("/music/play/song.mp3")

    assert response.status_code == HTTPStatus.OK
    assert response.mimetype == "audio/mpeg"


def test_play_route_rejects_path_traversal(client) -> None:
    response = client.get("/music/play/..%5Cevil.mp3")

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_data(as_text=True).startswith("Invalid")


def test_play_route_rejects_invalid_extension(client, music_dir: Path) -> None:
    (music_dir / "song.txt").write_bytes(b"payload")

    response = client.get("/music/play/song.txt")

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert (
        response.get_data(as_text=True)
        == "Unsupported file type. Allowed: mp3, flac, wav, aac, m4a"
    )


def test_play_route_translates_music_file_error(client, service: MusicService) -> None:
    with patch.object(service, "resolve_file_path", side_effect=MusicFileError("disk failed")):
        response = client.get("/music/play/song.mp3")

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_data(as_text=True) == "disk failed"


def test_delete_route_translates_value_error(client, service: MusicService) -> None:
    with patch.object(service, "delete_file", side_effect=ValueError("bad delete")):
        response = client.post("/music/delete/song.mp3", headers=_XHR)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json() == {"success": False, "message": "bad delete"}


def test_delete_route_translates_unhandled_error(client, service: MusicService) -> None:
    with patch.object(service, "delete_file", side_effect=RuntimeError("boom")):
        response = client.post("/music/delete/song.mp3", headers=_XHR)

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json() == {"success": False, "message": "Internal server error"}


def test_delete_dir_route_translates_value_error(client, service: MusicService) -> None:
    with patch.object(service, "delete_directory", side_effect=ValueError("bad dir")):
        response = client.post("/music/delete_dir/Albums", headers=_XHR)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json() == {"success": False, "message": "bad dir"}


def test_delete_dir_route_translates_unhandled_error(client, service: MusicService) -> None:
    with patch.object(service, "delete_directory", side_effect=RuntimeError("boom")):
        response = client.post("/music/delete_dir/Albums", headers=_XHR)

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json() == {"success": False, "message": "Internal server error"}


def test_move_route_translates_value_error(client, service: MusicService) -> None:
    with patch.object(service, "move_file", side_effect=ValueError("bad move")):
        response = client.post(
            "/music/move",
            json={"source": "song.mp3", "dest_path": "dest", "new_name": ""},
            headers=_XHR,
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json() == {"success": False, "message": "bad move"}


def test_move_route_translates_unhandled_error(client, service: MusicService) -> None:
    with patch.object(service, "move_file", side_effect=RuntimeError("boom")):
        response = client.post(
            "/music/move",
            json={"source": "song.mp3", "dest_path": "dest", "new_name": ""},
            headers=_XHR,
        )

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json() == {"success": False, "message": "Internal server error"}


def test_mkdir_route_translates_value_error(client, service: MusicService) -> None:
    with patch.object(service, "create_directory", side_effect=ValueError("bad mkdir")):
        response = client.post(
            "/music/mkdir",
            json={"path": "", "name": "Albums"},
            headers=_XHR,
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json() == {"success": False, "message": "bad mkdir"}


def test_mkdir_route_translates_unhandled_error(client, service: MusicService) -> None:
    with patch.object(service, "create_directory", side_effect=RuntimeError("boom")):
        response = client.post(
            "/music/mkdir",
            json={"path": "", "name": "Albums"},
            headers=_XHR,
        )

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json() == {"success": False, "message": "Internal server error"}


def test_play_route_translates_value_error(client, service: MusicService) -> None:
    with patch.object(service, "resolve_file_path", side_effect=ValueError("bad play")):
        response = client.get("/music/play/song.mp3")

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_data(as_text=True) == "bad play"


def test_play_route_translates_unhandled_error(client, service: MusicService) -> None:
    with patch.object(service, "resolve_file_path", side_effect=RuntimeError("boom")):
        response = client.get("/music/play/song.mp3")

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_data(as_text=True) == "Internal server error"
