# ruff: noqa: ANN001, ANN201  # pytest injects fixtures dynamically in test signatures.
"""Tests for the cloud archive blueprint."""

from __future__ import annotations

import re
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from jinja2 import DictLoader
from teslausb_web.app import create_app
from teslausb_web.blueprints import cloud_archive as cloud_archive_module
from teslausb_web.blueprints.cloud_archive import (
    _archive_status_dto,
    _get_archive_service,
    _get_oauth_service,
    _get_queries,
    _get_rclone_service,
    _invalidate_caches,
    _resolve_event_path,
)
from teslausb_web.config import (
    CloudSection,
    FeaturesSection,
    MappingSection,
    PathsSection,
    WebConfig,
    WebSection,
)
from teslausb_web.services.cloud_archive import CloudArchiveQueries, CloudArchiveService, SyncStats
from teslausb_web.services.cloud_archive.pipeline import ShadowTelemetry
from teslausb_web.services.cloud_archive_migrations import open_db
from teslausb_web.services.cloud_oauth_service import (
    AuthorizationStart,
    CloudOAuthService,
    OAuthCredentials,
)
from teslausb_web.services.cloud_rclone_service import (
    CloudRcloneService,
    RcloneAuthError,
    RcloneEntry,
    RcloneError,
    RcloneListing,
    RcloneRemote,
    RcloneStats,
    RcloneTransferProgress,
    RcloneTransferResult,
)

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient

_XHR = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    backing_root = tmp_path / "backing"
    state_dir = tmp_path / "state"
    teslacam_root = backing_root / "TeslaCam"
    for folder in ("RecentClips", "SavedClips", "SentryClips"):
        (teslacam_root / folder).mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32, max_upload_mb=8, max_chunk_mb=1),
        paths=PathsSection(
            backing_root=backing_root,
            state_dir=state_dir,
            cache_invalidate_script=tmp_path / "invalidate.sh",
        ),
        features=FeaturesSection(cloud_archive_enabled=True),
        cloud=CloudSection(
            credentials_path=state_dir / "cloud-creds.json",
            oauth_state_path=state_dir / "oauth-state.json",
            rclone_config_path=state_dir / "rclone-dir",
            rclone_log_path=state_dir / "rclone-dir" / "rclone.log",
            db_path=state_dir / "cloud.db",
            teslacam_path=teslacam_root,
            bwlimit_kbps=5 * 1024,
            priority_folders=("SavedClips", "SentryClips"),
            sync_folders=("SavedClips", "SentryClips", "RecentClips"),
        ),
        mapping=MappingSection(
            db_path=state_dir / "mapping.db",
            media_root=teslacam_root,
        ),
        source_path=None,
    )
    flask_app = create_app(cfg)
    flask_app.testing = True
    archive_service = flask_app.extensions["cloud_archive_service"]
    assert isinstance(archive_service, CloudArchiveService)
    archive_service.shutdown(timeout=0.1)
    return flask_app


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def archive_service(app: Flask) -> CloudArchiveService:
    service = app.extensions["cloud_archive_service"]
    assert isinstance(service, CloudArchiveService)
    return service


@pytest.fixture
def oauth_service(app: Flask) -> CloudOAuthService:
    service = app.extensions["cloud_oauth_service"]
    assert isinstance(service, CloudOAuthService)
    return service


@pytest.fixture
def rclone_service(app: Flask) -> CloudRcloneService:
    service = app.extensions["cloud_rclone_service"]
    assert isinstance(service, CloudRcloneService)
    return service


@pytest.fixture
def invalidator(app: Flask):
    return app.extensions["cache_invalidator"]


@pytest.fixture
def teslacam_root(app: Flask) -> Path:
    return app.config["teslausb_config"].cloud.teslacam_path


def _install_template(app: Flask, body: str = "{{ provider or 'none' }}") -> None:
    app.jinja_loader = DictLoader({"cloud_archive.html": body})


def _remote(rclone_service: CloudRcloneService) -> RcloneRemote:
    return RcloneRemote(
        name="teslausb",
        provider="dropbox",
        backend="dropbox",
        root="teslausb:",
        config_path=rclone_service.config_file_path,
    )


def _sync_stats() -> SyncStats:
    return SyncStats(
        total_synced=1,
        total_pending=2,
        total_failed=3,
        total_dead_letter=1,
        total_bytes=1024,
        stats_baseline_at="2026-01-01T00:00:00Z",
    )


def _seed_queue_row(app: Flask, file_path: str, status: str, retry_count: int = 0) -> None:
    db_path = app.config["teslausb_config"].cloud.db_path
    with open_db(db_path) as connection:
        connection.execute(
            "INSERT INTO cloud_synced_files (file_path, status, retry_count) VALUES (?, ?, ?)",
            (file_path, status, retry_count),
        )
        connection.commit()


def _seed_history_row(app: Flask, *, status: str = "completed") -> None:
    db_path = app.config["teslausb_config"].cloud.db_path
    with open_db(db_path) as connection:
        connection.execute(
            "INSERT INTO cloud_sync_sessions (started_at, files_synced, bytes_transferred, status) "
            "VALUES ('2026-01-01T00:00:00Z', 2, 20, ?)",
            (status,),
        )
        connection.commit()


def _make_event(
    root: Path, folder: str = "SentryClips", event: str = "2026-01-01_10-00-00"
) -> Path:
    event_dir = root / folder / event
    event_dir.mkdir(parents=True, exist_ok=True)
    (event_dir / f"{event}-front.mp4").write_bytes(b"video")
    return event_dir


def test_app_registers_cloud_archive_blueprint_and_services(
    app: Flask,
    archive_service: CloudArchiveService,
    oauth_service: CloudOAuthService,
    rclone_service: CloudRcloneService,
) -> None:
    assert "cloud_archive" in app.blueprints
    assert isinstance(archive_service, CloudArchiveService)
    assert isinstance(oauth_service, CloudOAuthService)
    assert isinstance(rclone_service, CloudRcloneService)


def test_helper_invalidate_caches_is_noop_without_extension(app: Flask) -> None:
    removed = app.extensions.pop("cache_invalidator")
    _invalidate_caches(app)
    app.extensions["cache_invalidator"] = removed


def test_helper_get_archive_service_rejects_misconfigured_extension(app: Flask) -> None:
    with app.app_context():
        original = app.extensions["cloud_archive_service"]
        app.extensions["cloud_archive_service"] = object()
        with pytest.raises(RuntimeError, match="cloud_archive_service"):
            _get_archive_service()
        app.extensions["cloud_archive_service"] = original


def test_helper_get_oauth_service_rejects_misconfigured_extension(app: Flask) -> None:
    with app.app_context():
        original = app.extensions["cloud_oauth_service"]
        app.extensions["cloud_oauth_service"] = object()
        with pytest.raises(RuntimeError, match="cloud_oauth_service"):
            _get_oauth_service()
        app.extensions["cloud_oauth_service"] = original


def test_helper_get_rclone_service_rejects_misconfigured_extension(app: Flask) -> None:
    with app.app_context():
        original = app.extensions["cloud_rclone_service"]
        app.extensions["cloud_rclone_service"] = object()
        with pytest.raises(RuntimeError, match="cloud_rclone_service"):
            _get_rclone_service()
        app.extensions["cloud_rclone_service"] = original


def test_helper_get_queries_builds_fresh_query_facade(app: Flask) -> None:
    with app.app_context():
        assert isinstance(_get_queries(), CloudArchiveQueries)


def test_helper_resolve_event_path_accepts_existing_event(app: Flask, teslacam_root: Path) -> None:
    event_dir = _make_event(teslacam_root)
    with app.app_context():
        assert _resolve_event_path("SentryClips", event_dir.name) == event_dir.resolve(strict=False)


def test_helper_resolve_event_path_rejects_unknown_folder(app: Flask) -> None:
    with app.app_context(), pytest.raises(ValueError, match="Invalid folder"):
        _resolve_event_path("Unknown", "2026-01-01_10-00-00")


def test_helper_resolve_event_path_rejects_path_traversal(app: Flask) -> None:
    with app.app_context(), pytest.raises(ValueError, match="Invalid event"):
        _resolve_event_path("SentryClips", "..\\escape")


def test_helper_archive_status_dto_round_trips_progress() -> None:
    progress = RcloneTransferProgress(summary="copy", percent=50.0, raw_line="Progress: 50%")
    dto = _archive_status_dto(progress)
    assert dto.running is True
    assert dto.progress == {
        "summary": "copy",
        "transferred": None,
        "total": None,
        "percent": 50.0,
        "speed": None,
        "eta": None,
        "raw_line": "Progress: 50%",
    }


def test_index_route_renders_with_stub_template(client: FlaskClient, app: Flask) -> None:
    _install_template(app)
    response = client.get("/cloud/")
    assert response.status_code == HTTPStatus.OK
    assert response.get_data(as_text=True) == "none"


def test_index_template_html_assertions(client: FlaskClient) -> None:
    response = client.get("/cloud/")
    html = response.get_data(as_text=True)
    template_path = (
        Path(cloud_archive_module.__file__).resolve().parent.parent
        / "templates"
        / "cloud_archive.html"
    )
    template_source = template_path.read_text(encoding="utf-8")

    assert response.status_code == HTTPStatus.OK
    assert "Cloud Sync" in html
    assert "<title>" in html
    for forbidden in ("Edit Mode", "Present Mode", "quick_edit", "current_mode"):
        assert forbidden not in html
        assert forbidden not in template_source
    for forbidden in ("cdn.", "googleapis.com", "cdnjs.cloudflare.com"):
        assert forbidden not in html
        assert forbidden not in template_source
    assert html.count("<svg") >= 10
    assert "<script>" in html
    assert 'id="syncNowBtn"' in html
    assert 'id="oauthStartBtn"' in html
    assert html.count("aria-label=") >= 2
    assert re.search(r"(?<![&(])#[0-9a-fA-F]{3,6}\b", template_source) is None
    assert 1880 <= len(template_source.splitlines()) <= 2310


def test_index_template_restores_v1_ui_sections(client: FlaskClient) -> None:
    html = client.get("/cloud/").get_data(as_text=True)

    assert "providerCardGrid" in html
    assert "Provider authorization" in html
    assert "Bandwidth limit:" in html
    assert "Retry attempts before giving up" in html
    assert "Dead Letters" in html
    # Schedule editor + Reconcile placeholder sections were removed for
    # v1 parity (operator H5 hardware test); the corresponding backend
    # routes were never wired in Phase 5, so the placeholder UI was
    # creating an Issue 2 visual regression versus v1.
    assert "Upload Schedule" not in html
    assert "Reconcile now" not in html


def test_index_route_degrades_when_services_raise(
    client: FlaskClient, app: Flask, oauth_service: CloudOAuthService
) -> None:
    _install_template(app)
    with patch.object(oauth_service, "load_credentials", side_effect=RuntimeError("boom")):
        response = client.get("/cloud/")
    assert response.status_code == HTTPStatus.OK


def test_mode_awareness_strings_are_absent_from_blueprint() -> None:
    source = Path(cloud_archive_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("current_mode", "quick_edit", "is_edit_mode", "Present Mode", "Edit Mode"):
        assert forbidden not in source


def test_save_settings_persists_and_redirects(
    client: FlaskClient, archive_service: CloudArchiveService
) -> None:
    response = client.post(
        "/cloud/settings",
        data={
            "sync_folders": ["SentryClips", "SavedClips"],
            "priority_order": "SavedClips,SentryClips",
            "sync_recent_with_telemetry": "1",
            "cloud_retry_max_attempts": "7",
        },
    )
    assert response.status_code in (HTTPStatus.FOUND, HTTPStatus.SEE_OTHER)
    with archive_service.open_db() as connection:
        from teslausb_web.services.cloud_archive.settings import (
            _read_priority_order_setting,
            _read_retry_max_attempts_setting,
            _read_sync_folders_setting,
            _read_sync_non_event_setting,
            _read_sync_recent_with_telemetry_setting,
        )

        assert "RecentClips" in _read_sync_folders_setting(
            archive_service.config, connection
        )
        assert _read_priority_order_setting(
            archive_service.config, connection
        ) == ("SavedClips", "SentryClips", "RecentClips")
        assert _read_sync_non_event_setting(archive_service.config, connection) is False
        assert (
            _read_sync_recent_with_telemetry_setting(archive_service.config, connection)
            is True
        )
        assert (
            _read_retry_max_attempts_setting(archive_service.config, connection) == 7
        )


def test_api_sync_now_success_invalidates_once(
    client: FlaskClient, archive_service: CloudArchiveService, invalidator
) -> None:
    invalidator.schedule = MagicMock()
    with patch.object(archive_service, "start_sync", return_value=(True, "queued")):
        response = client.post("/cloud/api/sync_now", headers=_XHR)
    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {"success": True, "message": "queued"}
    invalidator.schedule.assert_called_once_with()


def test_api_sync_now_error_returns_500(
    client: FlaskClient, archive_service: CloudArchiveService, invalidator
) -> None:
    invalidator.schedule = MagicMock()
    with patch.object(archive_service, "start_sync", side_effect=RuntimeError("boom")):
        response = client.post("/cloud/api/sync_now", headers=_XHR)
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    invalidator.schedule.assert_not_called()


def test_api_wake_returns_worker_status_and_invalidates(
    client: FlaskClient, archive_service: CloudArchiveService, invalidator
) -> None:
    invalidator.schedule = MagicMock()
    with (
        patch.object(archive_service, "wake") as wake_mock,
        patch.object(
            archive_service,
            "get_sync_status",
            return_value=archive_service.get_sync_status(),
        ),
    ):
        response = client.post("/cloud/api/wake", headers=_XHR)
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["success"] is True
    wake_mock.assert_called_once_with()
    invalidator.schedule.assert_called_once_with()


def test_api_sync_stop_returns_payload(
    client: FlaskClient, archive_service: CloudArchiveService, invalidator
) -> None:
    invalidator.schedule = MagicMock()
    with patch.object(archive_service, "stop_sync", return_value=(True, "stopping")):
        response = client.post("/cloud/api/sync_stop", json={"graceful": False}, headers=_XHR)
    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {"success": True, "message": "stopping"}
    invalidator.schedule.assert_called_once_with()


def test_api_status_uses_stats_cache(
    client: FlaskClient, archive_service: CloudArchiveService
) -> None:
    cloud_archive_module._stats_cache["stats"] = None
    cloud_archive_module._stats_cache["timestamp"] = 0.0
    with (
        patch.object(
            archive_service, "get_sync_status", return_value=archive_service.get_sync_status()
        ),
        patch.object(
            archive_service,
            "get_sync_stats",
            side_effect=[_sync_stats()],
        ) as stats_mock,
        patch.object(
            archive_service,
            "get_cloud_shadow_telemetry",
            return_value=ShadowTelemetry(
                agreement_count=1, disagreement_count=0, pipeline_enqueue_count=2
            ),
        ),
    ):
        first = client.get("/cloud/api/status")
        second = client.get("/cloud/api/status")
    assert first.status_code == HTTPStatus.OK
    assert second.status_code == HTTPStatus.OK
    assert stats_mock.call_count == 1


def test_api_history_returns_rows(client: FlaskClient, app: Flask) -> None:
    _seed_history_row(app)
    response = client.get("/cloud/api/history")
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert payload["history"][0]["status"] == "completed"


def test_api_history_rejects_invalid_limit(client: FlaskClient) -> None:
    response = client.get("/cloud/api/history?limit=abc")
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "limit must be an integer"


def test_api_reset_stats_success_invalidates_and_clears_cache(
    client: FlaskClient, archive_service: CloudArchiveService, invalidator
) -> None:
    invalidator.schedule = MagicMock()
    cloud_archive_module._stats_cache["stats"] = _sync_stats()
    cloud_archive_module._stats_cache["timestamp"] = 1.0
    with patch.object(
        archive_service, "reset_stats_baseline", return_value=(True, "2026-01-02T00:00:00Z")
    ):
        response = client.post("/cloud/api/reset_stats", headers=_XHR)
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["stats_baseline_at"] == "2026-01-02T00:00:00Z"
    assert cloud_archive_module._stats_cache["stats"] is None
    invalidator.schedule.assert_called_once_with()


def test_api_save_provider_stores_session_and_invalidates(client: FlaskClient, invalidator) -> None:
    invalidator.schedule = MagicMock()
    response = client.post("/cloud/api/provider", json={"provider": "dropbox"}, headers=_XHR)
    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {"success": True}
    with client.session_transaction() as session_state:
        assert session_state["cloud_archive_provider"] == "dropbox"
    invalidator.schedule.assert_called_once_with()


def test_api_save_provider_requires_provider(client: FlaskClient) -> None:
    response = client.post("/cloud/api/provider", json={}, headers=_XHR)
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "Missing provider."


def test_api_save_provider_rejects_unknown_provider(client: FlaskClient) -> None:
    response = client.post("/cloud/api/provider", json={"provider": "box"}, headers=_XHR)
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "Unknown provider: box"


def test_api_connect_starts_authorization_json(
    client: FlaskClient, oauth_service: CloudOAuthService, invalidator
) -> None:
    invalidator.schedule = MagicMock()
    start = AuthorizationStart(
        session_id="sid-1",
        provider="dropbox",
        authorization_url="https://example.invalid/auth",
        expires_at="2099-01-01T00:00:00Z",
    )
    with patch.object(oauth_service, "start_authorization", return_value=start):
        response = client.post("/cloud/api/connect", json={"provider": "dropbox"}, headers=_XHR)
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["session_id"] == "sid-1"
    invalidator.schedule.assert_called_once_with()


def test_api_connect_starts_authorization_redirect_for_browser(
    client: FlaskClient, oauth_service: CloudOAuthService, invalidator
) -> None:
    invalidator.schedule = MagicMock()
    start = AuthorizationStart(
        session_id="sid-2",
        provider="dropbox",
        authorization_url="https://example.invalid/auth",
        expires_at="2099-01-01T00:00:00Z",
    )
    with patch.object(oauth_service, "start_authorization", return_value=start):
        response = client.post("/cloud/api/connect", data={"provider": "dropbox"})
    assert response.status_code == HTTPStatus.FOUND
    assert response.headers["Location"] == "https://example.invalid/auth"
    invalidator.schedule.assert_called_once_with()


def test_api_connect_start_error_returns_500(
    client: FlaskClient, oauth_service: CloudOAuthService
) -> None:
    with patch.object(oauth_service, "start_authorization", side_effect=RuntimeError("boom")):
        response = client.post("/cloud/api/connect", json={"provider": "dropbox"}, headers=_XHR)
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR


def test_api_connect_callback_exchanges_code(
    client: FlaskClient, oauth_service: CloudOAuthService, invalidator
) -> None:
    invalidator.schedule = MagicMock()
    credentials = OAuthCredentials(
        provider="dropbox",
        access_token="access",
        refresh_token="refresh",
        token_type="Bearer",
        expires_at="2099-01-01T00:00:00Z",
    )
    with patch.object(oauth_service, "exchange_code", return_value=credentials):
        response = client.post(
            "/cloud/api/connect",
            json={"session_id": "sid-1", "redirect_url": "https://localhost/?code=abc"},
            headers=_XHR,
        )
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["provider"] == "dropbox"
    invalidator.schedule.assert_called_once_with()


def test_api_connect_callback_error_returns_500(
    client: FlaskClient, oauth_service: CloudOAuthService
) -> None:
    with patch.object(oauth_service, "exchange_code", side_effect=RuntimeError("boom")):
        response = client.post(
            "/cloud/api/connect",
            json={"session_id": "sid-1", "code": "abc"},
            headers=_XHR,
        )
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR


def test_api_disconnect_returns_result(
    client: FlaskClient, oauth_service: CloudOAuthService, invalidator
) -> None:
    invalidator.schedule = MagicMock()
    with patch.object(
        oauth_service,
        "disconnect",
        return_value=MagicMock(
            disconnected=True, revoked=False, message="Removed local credentials"
        ),
    ):
        response = client.post("/cloud/api/disconnect", headers=_XHR)
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["success"] is True
    invalidator.schedule.assert_called_once_with()


def test_api_connect_generic_form_persists_remote(
    client: FlaskClient, app: Flask, oauth_service: CloudOAuthService, invalidator
) -> None:
    invalidator.schedule = MagicMock()
    generic = app.extensions["cloud_generic_remote_service"]
    response = client.post(
        "/cloud/api/connect",
        json={
            "provider": "generic",
            "rclone_type": "s3",
            "fields": {
                "provider": "Other",
                "access_key_id": "AK",
                "secret_access_key": "SK",
            },
            "obscure_keys": [],
        },
        headers=_XHR,
    )
    assert response.status_code == HTTPStatus.OK, response.get_data(as_text=True)
    body = response.get_json()
    assert body["success"] is True
    assert body["provider"] == "generic:s3"
    record = generic.load()
    assert record is not None
    assert record["type"] == "s3"
    assert record["secret_access_key"] == "SK"
    invalidator.schedule.assert_called_once_with()


def test_api_connect_generic_config_block_persists_remote(
    client: FlaskClient, app: Flask
) -> None:
    generic = app.extensions["cloud_generic_remote_service"]
    block = "[my-nas]\ntype = sftp\nhost = nas.local\nuser = pi\npass = obscured\n"
    response = client.post(
        "/cloud/api/connect",
        json={"provider": "generic", "config_block": block},
        headers=_XHR,
    )
    assert response.status_code == HTTPStatus.OK, response.get_data(as_text=True)
    record = generic.load()
    assert record is not None
    assert record["type"] == "sftp"
    assert record["host"] == "nas.local"


def test_api_connect_generic_rejects_bad_type(client: FlaskClient) -> None:
    response = client.post(
        "/cloud/api/connect",
        json={"provider": "generic", "rclone_type": "bogus", "fields": {}},
        headers=_XHR,
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST


def test_api_disconnect_also_clears_generic_remote(
    client: FlaskClient, app: Flask, oauth_service: CloudOAuthService
) -> None:
    generic = app.extensions["cloud_generic_remote_service"]
    generic.import_form("s3", {"access_key_id": "AK", "secret_access_key": "SK"})
    assert generic.load() is not None
    with patch.object(
        oauth_service,
        "disconnect",
        return_value=MagicMock(disconnected=True, revoked=False, message="ok"),
    ):
        response = client.post("/cloud/api/disconnect", headers=_XHR)
    assert response.status_code == HTTPStatus.OK
    assert generic.load() is None


def test_api_test_connection_success(
    client: FlaskClient, rclone_service: CloudRcloneService
) -> None:
    with (
        patch.object(rclone_service, "has_configured_remote", return_value=True),
        patch.object(rclone_service, "check_connection", return_value=_remote(rclone_service)),
    ):
        response = client.post("/cloud/api/test_connection", headers=_XHR)
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["success"] is True


def test_api_test_connection_returns_400_when_no_remote(
    client: FlaskClient, rclone_service: CloudRcloneService
) -> None:
    with patch.object(rclone_service, "has_configured_remote", return_value=False):
        response = client.post("/cloud/api/test_connection", headers=_XHR)
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["message"] == "No configured remote."


def test_api_test_connection_reports_auth_failure(
    client: FlaskClient, rclone_service: CloudRcloneService
) -> None:
    with (
        patch.object(rclone_service, "has_configured_remote", return_value=True),
        patch.object(
            rclone_service,
            "check_connection",
            side_effect=RcloneAuthError("401 unauthorized"),
        ),
    ):
        response = client.post("/cloud/api/test_connection", headers=_XHR)
    assert response.status_code == HTTPStatus.OK
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["auth_error"] is True


def test_api_test_connection_reports_unreachable_provider(
    client: FlaskClient, rclone_service: CloudRcloneService
) -> None:
    with (
        patch.object(rclone_service, "has_configured_remote", return_value=True),
        patch.object(
            rclone_service,
            "check_connection",
            side_effect=RcloneError("rclone lsd failed: connection refused"),
        ),
    ):
        response = client.post("/cloud/api/test_connection", headers=_XHR)
    assert response.status_code == HTTPStatus.OK
    payload = response.get_json()
    assert payload["success"] is False
    assert "auth_error" not in payload
    assert "Could not reach" in payload["message"]


def test_api_connection_status_reports_connected_provider(
    client: FlaskClient, oauth_service: CloudOAuthService, rclone_service: CloudRcloneService
) -> None:
    credentials = OAuthCredentials(
        provider="dropbox",
        access_token="access",
        refresh_token="refresh",
        token_type="Bearer",
        expires_at="2099-01-01T00:00:00Z",
    )
    with (
        patch.object(oauth_service, "load_credentials", return_value=credentials),
        patch.object(oauth_service, "get_pending_authorization", return_value=None),
        patch.object(rclone_service, "render_config", return_value=_remote(rclone_service)),
    ):
        response = client.get("/cloud/api/connection_status")
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["connected"] is True
    assert response.get_json()["provider"] == "dropbox"


def test_api_connection_status_reports_pending_authorization(
    client: FlaskClient, oauth_service: CloudOAuthService
) -> None:
    started = AuthorizationStart(
        session_id="sid-3",
        provider="dropbox",
        authorization_url="https://example.invalid/auth",
        expires_at="2099-01-01T00:00:00Z",
    )
    with (
        patch.object(oauth_service, "load_credentials", return_value=None),
        patch.object(oauth_service, "get_pending_authorization", return_value=started),
    ):
        response = client.get("/cloud/api/connection_status")
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["pending_authorization"]["session_id"] == "sid-3"


def test_api_storage_usage_returns_rclone_stats(
    client: FlaskClient, rclone_service: CloudRcloneService
) -> None:
    stats = RcloneStats(
        remote=_remote(rclone_service),
        path="",
        total_bytes=10,
        used_bytes=4,
        free_bytes=6,
        trashed_bytes=0,
        object_count=1,
        size_bytes=4,
    )
    with patch.object(rclone_service, "get_stats", return_value=stats):
        response = client.get("/cloud/api/storage_usage")
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["used_bytes"] == 4


def test_api_storage_usage_returns_empty_payload_on_runtime_error(
    client: FlaskClient, rclone_service: CloudRcloneService
) -> None:
    with patch.object(rclone_service, "get_stats", side_effect=RuntimeError("boom")):
        response = client.get("/cloud/api/storage_usage")
    assert response.status_code == HTTPStatus.OK
    payload = response.get_json()
    assert payload == {"available": False, "reason": "boom"}


def test_api_browse_returns_directory_names(
    client: FlaskClient, rclone_service: CloudRcloneService
) -> None:
    listing = RcloneListing(
        remote=_remote(rclone_service),
        path="archive",
        entries=(
            RcloneEntry(name="folder-a", path="archive/folder-a", is_dir=True),
            RcloneEntry(name="clip.mp4", path="archive/clip.mp4", is_dir=False),
            RcloneEntry(name="folder-b", path="archive/folder-b", is_dir=True),
        ),
    )
    with patch.object(rclone_service, "list_directory", return_value=listing):
        response = client.get("/cloud/api/browse?path=archive")
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["folders"] == ["folder-a", "folder-b"]


def test_api_browse_rejects_invalid_path(
    client: FlaskClient, rclone_service: CloudRcloneService
) -> None:
    with patch.object(rclone_service, "list_directory", side_effect=ValueError("bad path")):
        response = client.get("/cloud/api/browse?path=../escape")
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "bad path"


def test_api_mkdir_rejects_missing_path(client: FlaskClient) -> None:
    response = client.post("/cloud/api/mkdir", headers=_XHR, json={})
    assert response.status_code == HTTPStatus.BAD_REQUEST


def test_api_mkdir_invokes_rclone(
    client: FlaskClient, rclone_service: CloudRcloneService
) -> None:
    with patch.object(rclone_service, "mkdir", return_value=None) as mock_mkdir:
        response = client.post(
            "/cloud/api/mkdir", headers=_XHR, json={"path": "MyCar/Sentry"}
        )
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["path"] == "MyCar/Sentry"
    mock_mkdir.assert_called_once_with("MyCar/Sentry")


def test_api_set_remote_path_persists_value(
    client: FlaskClient, rclone_service: CloudRcloneService
) -> None:
    response = client.post(
        "/cloud/api/set_remote_path", headers=_XHR, json={"path": " MyCar/Sentry/ "}
    )
    assert response.status_code == HTTPStatus.OK
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["path"] == "MyCar/Sentry"


def test_api_toggle_sync_endpoint_removed(client: FlaskClient) -> None:
    """The /api/toggle_sync route was removed when auto-sync went always-on.

    Auto-sync now runs continuously whenever WiFi is up and a provider
    is configured (operator directive, 2026-05-28). Verify the legacy
    endpoint is gone so we don't ship a half-wired UI.
    """
    response = client.post("/cloud/api/toggle_sync", headers=_XHR, json={"enabled": False})
    assert response.status_code == HTTPStatus.NOT_FOUND


def test_auto_sync_is_always_enabled(app: Flask) -> None:
    """``is_auto_sync_enabled`` returns ``True`` regardless of KV state."""
    service = app.extensions["cloud_archive_service"]
    service.update_settings(enabled=False)
    assert service.is_auto_sync_enabled() is True
    service.update_settings(enabled=True)
    assert service.is_auto_sync_enabled() is True


def test_api_sync_status_batch_returns_statuses(client: FlaskClient, app: Flask) -> None:
    _seed_queue_row(app, "SentryClips/2026-01-01_10-00-00", "synced")
    response = client.post(
        "/cloud/api/sync_status_batch",
        json={"events": ["2026-01-01_10-00-00"]},
        headers=_XHR,
    )
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["statuses"]["2026-01-01_10-00-00"] == "synced"


def test_api_sync_status_batch_ignores_non_list_payload(client: FlaskClient) -> None:
    response = client.post("/cloud/api/sync_status_batch", json={"events": "bad"}, headers=_XHR)
    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {"statuses": {}}


def test_api_queue_event_adds_real_event(
    client: FlaskClient, teslacam_root: Path, invalidator
) -> None:
    invalidator.schedule = MagicMock()
    _make_event(teslacam_root)
    response = client.post(
        "/cloud/api/queue_event",
        json={"folder": "SentryClips", "event": "2026-01-01_10-00-00", "priority": True},
        headers=_XHR,
    )
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["success"] is True
    invalidator.schedule.assert_called_once_with()


def test_api_queue_event_requires_folder_and_event(client: FlaskClient) -> None:
    response = client.post("/cloud/api/queue_event", json={}, headers=_XHR)
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["message"] == "Missing folder or event"


def test_api_queue_event_surfaces_runtime_error(
    client: FlaskClient, archive_service: CloudArchiveService
) -> None:
    with patch.object(archive_service, "queue_event_for_sync", side_effect=RuntimeError("boom")):
        response = client.post(
            "/cloud/api/queue_event",
            json={"folder": "SentryClips", "event": "2026-01-01_10-00-00"},
            headers=_XHR,
        )
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR


def test_api_queue_returns_queue_envelope(client: FlaskClient, app: Flask) -> None:
    _seed_queue_row(app, "SentryClips/item", "queued")
    response = client.get("/cloud/api/queue")
    assert response.status_code == HTTPStatus.OK
    payload = response.get_json()
    assert payload["queue"][0]["file_path"] == "SentryClips/item"


def test_api_queue_returns_error_wrapper_on_failure(client: FlaskClient, app: Flask) -> None:
    with patch.object(cloud_archive_module, "_get_queries", side_effect=RuntimeError("boom")):
        response = client.get("/cloud/api/queue")
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["queue"] == []


def test_api_queue_remove_invalidates_on_success(
    client: FlaskClient, app: Flask, invalidator
) -> None:
    invalidator.schedule = MagicMock()
    _seed_queue_row(app, "SentryClips/remove-me", "queued")
    response = client.post(
        "/cloud/api/queue/remove",
        json={"file_path": "SentryClips/remove-me"},
        headers=_XHR,
    )
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["message"] == "Removed from queue"
    invalidator.schedule.assert_called_once_with()


def test_api_queue_remove_without_path_returns_v1_shape(client: FlaskClient) -> None:
    response = client.post("/cloud/api/queue/remove", json={}, headers=_XHR)
    assert response.status_code == HTTPStatus.OK
    assert response.get_json() == {"success": False, "message": "No file path"}


def test_api_queue_clear_invalidates(client: FlaskClient, app: Flask, invalidator) -> None:
    invalidator.schedule = MagicMock()
    _seed_queue_row(app, "SentryClips/clear-me", "queued")
    response = client.post("/cloud/api/queue/clear", headers=_XHR)
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["success"] is True
    invalidator.schedule.assert_called_once_with()


def test_api_dead_letters_lists_rows(client: FlaskClient, app: Flask) -> None:
    _seed_queue_row(app, "SentryClips/dl", "dead_letter", retry_count=4)
    response = client.get("/cloud/api/dead_letters")
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert payload["count"] == 1
    assert payload["dead_letters"][0]["file_path"] == "SentryClips/dl"


def test_api_dead_letters_rejects_invalid_limit(client: FlaskClient) -> None:
    response = client.get("/cloud/api/dead_letters?limit=bad")
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "limit must be an integer"


def test_api_dead_letters_retry_invalidates(client: FlaskClient, app: Flask, invalidator) -> None:
    invalidator.schedule = MagicMock()
    _seed_queue_row(app, "SentryClips/dl", "dead_letter", retry_count=4)
    response = client.post(
        "/cloud/api/dead_letters/retry",
        json={"file_path": "SentryClips/dl"},
        headers=_XHR,
    )
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["count"] == 1
    invalidator.schedule.assert_called_once_with()


def test_api_dead_letters_delete_all_invalidates(
    client: FlaskClient, app: Flask, invalidator
) -> None:
    invalidator.schedule = MagicMock()
    _seed_queue_row(app, "SentryClips/dl-1", "dead_letter", retry_count=4)
    _seed_queue_row(app, "SentryClips/dl-2", "dead_letter", retry_count=4)
    response = client.post("/cloud/api/dead_letters/delete", headers=_XHR)
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["count"] == 2
    invalidator.schedule.assert_called_once_with()


def test_api_dead_letters_delete_supports_delete_method(
    client: FlaskClient, app: Flask, invalidator
) -> None:
    invalidator.schedule = MagicMock()
    _seed_queue_row(app, "SentryClips/dl-delete", "dead_letter", retry_count=4)
    response = client.delete(
        "/cloud/api/dead_letters/delete",
        json={"file_path": "SentryClips/dl-delete"},
        headers=_XHR,
    )
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["count"] == 1
    invalidator.schedule.assert_called_once_with()


def test_api_archive_cleanup_runs_cleanup(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from teslausb_web.services.cloud_archive import cloud_cleanup as cc

    def _fake(_service: object) -> cc.CloudCleanupResult:
        return cc.CloudCleanupResult(
            triggered=True, deleted_count=3, bytes_freed=4096, reason="ok"
        )

    monkeypatch.setattr(cc, "run_cloud_cleanup", _fake)
    response = client.post("/cloud/api/archive_cleanup", headers=_XHR)
    assert response.status_code == HTTPStatus.OK
    body = response.get_json()
    assert body == {
        "success": True,
        "triggered": True,
        "deleted_count": 3,
        "bytes_freed": 4096,
        "reason": "ok",
    }


def test_api_archive_file_transfers_event_and_invalidates(
    client: FlaskClient,
    teslacam_root: Path,
    rclone_service: CloudRcloneService,
    invalidator,
) -> None:
    invalidator.schedule = MagicMock()
    event_dir = _make_event(teslacam_root)
    result = RcloneTransferResult(
        remote=_remote(rclone_service),
        operation="copy",
        source_path=event_dir,
        destination=f"SentryClips/{event_dir.name}",
        returncode=0,
        stdout="",
        stderr="",
        cancelled=False,
        progress=None,
        log_path=rclone_service.log_path,
    )
    with patch.object(rclone_service, "transfer", return_value=result) as transfer_mock:
        response = client.post(
            "/cloud/api/archive_file",
            json={"folder": "SentryClips", "event": event_dir.name},
            headers=_XHR,
        )
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["success"] is True
    assert transfer_mock.call_args.args[0] == event_dir.resolve(strict=False)
    invalidator.schedule.assert_called_once_with()


def test_api_archive_file_requires_folder_and_event(client: FlaskClient) -> None:
    response = client.post("/cloud/api/archive_file", json={}, headers=_XHR)
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["message"] == "Missing folder or event."


def test_api_archive_file_returns_not_found_for_missing_event(client: FlaskClient) -> None:
    response = client.post(
        "/cloud/api/archive_file",
        json={"folder": "SentryClips", "event": "missing"},
        headers=_XHR,
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.get_json()["error"] == "TeslaCam event not found"


def test_api_archive_status_reports_progress(
    client: FlaskClient, rclone_service: CloudRcloneService
) -> None:
    progress = RcloneTransferProgress(summary="copy", transferred="1 MiB", percent=25.0)
    with patch.object(rclone_service, "current_progress", return_value=progress):
        response = client.get("/cloud/api/archive_status")
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["running"] is True
    assert response.get_json()["progress"]["percent"] == 25.0


def test_api_archive_cancel_invalidates_when_transfer_cancelled(
    client: FlaskClient, rclone_service: CloudRcloneService, invalidator
) -> None:
    invalidator.schedule = MagicMock()
    with patch.object(rclone_service, "cancel_active_transfer", return_value=True):
        response = client.post("/cloud/api/archive_cancel", headers=_XHR)
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["success"] is True
    invalidator.schedule.assert_called_once_with()
