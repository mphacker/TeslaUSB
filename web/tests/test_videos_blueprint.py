"""Tests for ``teslausb_web.blueprints.videos`` (Phase 5.26)."""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from teslausb_web.app import create_app
from teslausb_web.config import (
    MappingSection,
    PathsSection,
    WebConfig,
    WebSection,
)

if TYPE_CHECKING:
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient


_VALID_MP4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 2048


def _make_cfg(tmp_path: Path) -> WebConfig:
    backing = tmp_path / "backing"
    teslacam = backing / "TeslaCam"
    archive = backing / "ArchivedClips"
    teslacam.mkdir(parents=True)
    archive.mkdir(parents=True)
    return WebConfig(
        web=WebSection(secret_key="v" * 32),
        paths=PathsSection(
            backing_root=backing,
            state_dir=tmp_path / "state",
            cache_invalidate_script=tmp_path / "invalidate.sh",
        ),
        mapping=MappingSection(
            db_path=tmp_path / "state" / "mapping.db",
            backup_dir=tmp_path / "state" / "mapping-backups",
            media_root=teslacam,
            archive_root=archive,
        ),
        source_path=None,
    )


def _seed_sentry_event(cfg: WebConfig, event: str = "2025-01-15_12-30-45") -> Path:
    sentry = cfg.paths.backing_root / "TeslaCam" / "SentryClips" / event
    sentry.mkdir(parents=True, exist_ok=True)
    for cam in ("front", "back", "left_repeater", "right_repeater", "left_pillar", "right_pillar"):
        (sentry / f"{event}-{cam}.mp4").write_bytes(_VALID_MP4)
    (sentry / "event.mp4").write_bytes(_VALID_MP4)
    (sentry / "event.json").write_text(
        json.dumps(
            {
                "timestamp": "2025-01-15T12:30:45",
                "city": "Austin",
                "reason": "sentry_aware_object_detection",
            }
        )
    )
    return sentry


def _seed_recent(cfg: WebConfig, session: str = "2025-03-01_09-15-30") -> None:
    recent = cfg.paths.backing_root / "TeslaCam" / "RecentClips"
    recent.mkdir(parents=True, exist_ok=True)
    for cam in ("front", "back"):
        (recent / f"{session}-{cam}.mp4").write_bytes(_VALID_MP4)


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    cfg = _make_cfg(tmp_path)
    _seed_sentry_event(cfg)
    _seed_recent(cfg)
    application = create_app(cfg)
    application.testing = True
    return application


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


# ---------------------------------------------------------------------------
# GET / — XHR JSON vs browser 200 (renders map page).
# ---------------------------------------------------------------------------


class TestFileBrowser:
    def test_browser_get_renders_map_page(self, client: FlaskClient) -> None:
        """Non-XHR GET renders the map page (200) instead of redirecting."""
        resp = client.get("/videos/")
        assert resp.status_code == HTTPStatus.OK
        html = resp.get_data(as_text=True)
        assert 'class="map-container"' in html
        assert 'id="videoPanel"' in html

    def test_xhr_returns_json(self, client: FlaskClient) -> None:
        resp = client.get(
            "/videos/?folder=SentryClips",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == HTTPStatus.OK
        payload = resp.get_json()
        assert payload is not None
        assert payload["folder_structure"] == "events"
        assert len(payload["events"]) == 1
        assert payload["events"][0]["name"] == "2025-01-15_12-30-45"

    def test_xhr_flat_folder(self, client: FlaskClient) -> None:
        resp = client.get(
            "/videos/?folder=RecentClips",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        payload = resp.get_json()
        assert payload["folder_structure"] == "flat"
        assert len(payload["events"]) == 1

    def test_xhr_unknown_folder_returns_empty(self, client: FlaskClient) -> None:
        resp = client.get(
            "/videos/?folder=DoesNotExist",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        payload = resp.get_json()
        assert payload["events"] == []

    def test_xhr_bad_page_defaults_to_one(self, client: FlaskClient) -> None:
        resp = client.get(
            "/videos/?folder=SentryClips&page=abc",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == HTTPStatus.OK


# ---------------------------------------------------------------------------
# Stream + range.
# ---------------------------------------------------------------------------


class TestStream:
    def test_stream_full_file_200(self, client: FlaskClient) -> None:
        resp = client.get("/videos/stream/SentryClips/2025-01-15_12-30-45/event.mp4")
        assert resp.status_code == HTTPStatus.OK
        assert resp.headers.get("Accept-Ranges") == "bytes"

    def test_stream_range_206(self, client: FlaskClient) -> None:
        resp = client.get(
            "/videos/stream/SentryClips/2025-01-15_12-30-45/event.mp4",
            headers={"Range": "bytes=0-9"},
        )
        assert resp.status_code == HTTPStatus.PARTIAL_CONTENT
        assert resp.headers["Content-Range"].startswith("bytes 0-9/")
        assert resp.headers["Content-Length"] == "10"

    def test_stream_bad_range_416(self, client: FlaskClient) -> None:
        resp = client.get(
            "/videos/stream/SentryClips/2025-01-15_12-30-45/event.mp4",
            headers={"Range": "bytes=999999-9999999"},
        )
        assert resp.status_code == HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE

    def test_stream_traversal_404(self, client: FlaskClient) -> None:
        resp = client.get("/videos/stream/SentryClips/../../etc/passwd")
        assert resp.status_code == HTTPStatus.NOT_FOUND

    def test_stream_missing_404(self, client: FlaskClient) -> None:
        resp = client.get("/videos/stream/SentryClips/nope.mp4")
        assert resp.status_code == HTTPStatus.NOT_FOUND


class TestSeiFetch:
    def test_sei_returns_full_body(self, client: FlaskClient) -> None:
        resp = client.get("/videos/sei/SentryClips/2025-01-15_12-30-45/event.mp4")
        assert resp.status_code == HTTPStatus.OK
        assert resp.headers.get("Cache-Control") == "public, max-age=3600"


class TestDownloadVideo:
    def test_download_single_clip(self, client: FlaskClient) -> None:
        resp = client.get("/videos/download/SentryClips/2025-01-15_12-30-45/event.mp4")
        assert resp.status_code == HTTPStatus.OK
        disp = resp.headers.get("Content-Disposition", "")
        assert "attachment" in disp
        assert "event.mp4" in disp

    def test_download_traversal_404(self, client: FlaskClient) -> None:
        resp = client.get("/videos/download/x/../../../etc/passwd")
        assert resp.status_code == HTTPStatus.NOT_FOUND


# ---------------------------------------------------------------------------
# ZIP download.
# ---------------------------------------------------------------------------


class TestDownloadEvent:
    def test_download_event_zip(self, client: FlaskClient) -> None:
        resp = client.get("/videos/download_event/SentryClips/2025-01-15_12-30-45")
        assert resp.status_code == HTTPStatus.OK
        assert resp.headers.get("Content-Type") == "application/zip"
        assert "2025-01-15_12-30-45.zip" in (resp.headers.get("Content-Disposition") or "")

    def test_download_event_unknown_folder_404(self, client: FlaskClient) -> None:
        resp = client.get("/videos/download_event/Nope/2025-01-15_12-30-45")
        assert resp.status_code == HTTPStatus.NOT_FOUND


# ---------------------------------------------------------------------------
# Delete — happy path + traversal-blocked.
# ---------------------------------------------------------------------------


class TestDeleteEvent:
    def test_delete_events_folder(self, app: Flask, client: FlaskClient) -> None:
        resp = client.post("/videos/delete_event/SentryClips/2025-01-15_12-30-45")
        assert resp.status_code == HTTPStatus.OK
        body = resp.get_json()
        assert body["success"] is True
        cfg: WebConfig = app.config["teslausb_config"]
        assert not (
            cfg.paths.backing_root / "TeslaCam" / "SentryClips" / "2025-01-15_12-30-45"
        ).exists()

    def test_delete_unknown_folder_404(self, client: FlaskClient) -> None:
        resp = client.post("/videos/delete_event/Nope/anything")
        assert resp.status_code == HTTPStatus.NOT_FOUND

    def test_delete_flat_session(self, client: FlaskClient) -> None:
        resp = client.post("/videos/delete_event/RecentClips/2025-03-01_09-15-30")
        assert resp.status_code == HTTPStatus.OK
        body = resp.get_json()
        assert body["deleted_count"] >= 1


# ---------------------------------------------------------------------------
# Event-player template render.
# ---------------------------------------------------------------------------


class TestEventPlayer:
    def test_event_player_renders(self, client: FlaskClient) -> None:
        resp = client.get("/videos/event/SentryClips/2025-01-15_12-30-45")
        assert resp.status_code == HTTPStatus.OK
        html = resp.get_data(as_text=True)
        # Lucide sprite is referenced, no stray Bootstrap-Icons classes
        assert "lucide-sprite.svg" in html
        assert "bi bi-" not in html
        # Delete button is unconditionally rendered (Phase 5.26).
        assert 'id="deleteButton"' in html
        # No leftover v1 mode-token jinja conditional.
        assert "mode_token" not in html

    def test_event_player_missing_event_404(self, client: FlaskClient) -> None:
        resp = client.get("/videos/event/SentryClips/2099-01-01_00-00-00")
        assert resp.status_code == HTTPStatus.NOT_FOUND


# ---------------------------------------------------------------------------
# Edge cases for blueprint coverage.
# ---------------------------------------------------------------------------


class TestExtraCoverage:
    def test_file_browser_no_folder_param_empty_archive(self, tmp_path: Path) -> None:
        # Build a config where TeslaCam exists but has no subdirs and the
        # archive root doesn't exist — list_folders returns nothing so the
        # XHR response hits the "no current_folder" branch.
        backing = tmp_path / "backing"
        (backing / "TeslaCam").mkdir(parents=True)
        cfg = WebConfig(
            web=WebSection(secret_key="v" * 32),
            paths=PathsSection(
                backing_root=backing,
                state_dir=tmp_path / "state",
                cache_invalidate_script=tmp_path / "x.sh",
            ),
            mapping=MappingSection(
                db_path=tmp_path / "state" / "m.db",
                backup_dir=tmp_path / "state" / "mb",
                media_root=backing / "TeslaCam",
                archive_root=backing / "absent",
            ),
            source_path=None,
        )
        application = create_app(cfg)
        application.testing = True
        c = application.test_client()
        resp = c.get("/videos/", headers={"X-Requested-With": "XMLHttpRequest"})
        payload = resp.get_json()
        assert payload["events"] == []

    def test_stream_head_request_no_body(self, client: FlaskClient) -> None:
        resp = client.head(
            "/videos/stream/SentryClips/2025-01-15_12-30-45/event.mp4",
            headers={"Range": "bytes=0-9"},
        )
        # HEAD with Range still returns 206 with empty body.
        assert resp.status_code in (
            HTTPStatus.PARTIAL_CONTENT,
            HTTPStatus.METHOD_NOT_ALLOWED,
        )

    def test_delete_event_no_files_listed_path_returns_404_for_missing(
        self, client: FlaskClient
    ) -> None:
        # An events-structure folder with a missing event 404s through
        # FileNotFoundError raised in safe_delete_clip.
        resp = client.post("/videos/delete_event/SentryClips/no-such-event")
        assert resp.status_code == HTTPStatus.NOT_FOUND
