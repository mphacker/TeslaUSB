# ruff: noqa: ANN001  # pytest fixture injection.
"""Tests for the advanced settings blueprint."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from teslausb_web.app import create_app
from teslausb_web.blueprints.settings_advanced import (
    _get_service,
    _get_teslafat_client,
    _invalidate_caches,
    _json_error_payload,
    _redirect_to_settings,
    _request_bool,
    _request_settings_update,
    _request_text,
    _truncate,
)
from teslausb_web.config import PathsSection, SystemSettingsSection, WebConfig, WebSection
from teslausb_web.services.system_settings_service import SystemSettingsService
from teslausb_web.services.teslafat_client import IpcDaemonError, TeslaFatClient
from teslausb_web.services.teslafat_messages import ErrorBody

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
            default_log_level="INFO",
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
def service(app) -> SystemSettingsService:
    resolved = app.extensions["system_settings_service"]
    assert isinstance(resolved, SystemSettingsService)
    return resolved


@pytest.fixture
def teslafat_client(app) -> TeslaFatClient:
    resolved = app.extensions["teslafat_client"]
    assert isinstance(resolved, TeslaFatClient)
    return resolved


class _FakeStatus:
    def __init__(
        self,
        *,
        state: str = "SERVING",
        lun_id: int = 0,
        volume_label: str = "TESLACAM",
    ) -> None:
        self.state = state
        self.lun_id = lun_id
        self.volume_label = volume_label
        self.uptime_seconds = 42


class TestHelpers:
    def test_app_registers_blueprint_and_services(self, app) -> None:
        assert "settings_advanced" in app.blueprints
        assert isinstance(app.extensions["system_settings_service"], SystemSettingsService)
        assert isinstance(app.extensions["teslafat_client"], TeslaFatClient)

    def test_get_service_rejects_misconfigured_extension(self, app) -> None:
        with app.app_context():
            original = app.extensions["system_settings_service"]
            app.extensions["system_settings_service"] = object()
            with pytest.raises(RuntimeError, match="system_settings_service"):
                _get_service()
            app.extensions["system_settings_service"] = original

    def test_get_teslafat_client_rejects_misconfigured_extension(self, app) -> None:
        with app.app_context():
            original = app.extensions["teslafat_client"]
            app.extensions["teslafat_client"] = object()
            with pytest.raises(RuntimeError, match="teslafat_client"):
                _get_teslafat_client()
            app.extensions["teslafat_client"] = original

    def test_invalidate_caches_is_noop_without_extension(self, app) -> None:
        invalidator = app.extensions.pop("cache_invalidator")
        _invalidate_caches(app)
        app.extensions["cache_invalidator"] = invalidator

    def test_request_helpers_accept_json_and_form(self, app) -> None:
        with app.test_request_context(
            "/settings/advanced/save",
            method="POST",
            json={"samba_enabled": True, "log_level": "ERROR"},
        ):
            assert _request_bool("samba_enabled") is True
            assert _request_text("log_level") == "ERROR"
        with app.test_request_context(
            "/settings/advanced/save",
            method="POST",
            data={"samba_enabled": "on", "log_level": "WARNING", "partition": "part1"},
        ):
            payload = _request_settings_update()
            assert payload == {"samba_enabled": True, "log_level": "WARNING"}

    def test_request_bool_rejects_invalid_type(self, app) -> None:
        with (
            app.test_request_context(
                "/settings/advanced/save",
                method="POST",
                json={"samba_enabled": ["bad"]},
            ),
            pytest.raises(Exception, match="boolean"),
        ):
            _request_bool("samba_enabled")

    def test_request_helpers_coerce_scalar_json_values(self, app) -> None:
        with app.test_request_context(
            "/settings/advanced/save",
            method="POST",
            json={"samba_enabled": 1, "log_level": None},
        ):
            assert _request_bool("samba_enabled") is True
            assert _request_text("log_level") == ""
        with app.test_request_context(
            "/settings/advanced/save",
            method="POST",
            json={"samba_enabled": "yes", "log_level": 7},
        ):
            assert _request_bool("samba_enabled") is True
            assert _request_text("log_level") == "7"

    def test_redirect_helper_defaults_to_settings_index(self, app) -> None:
        with app.test_request_context("/settings/advanced/"):
            response = _redirect_to_settings()
        assert response.status_code == HTTPStatus.FOUND
        assert response.location == "/settings/advanced/"

    def test_json_error_payload_and_truncate_helpers(self, app) -> None:
        with app.app_context():
            response = _json_error_payload("boom")
        assert response.get_json() == {"success": False, "error": "boom"}
        assert _truncate("x" * 200).endswith("…")


class TestIndexRoutes:
    def test_index_route_renders_template(self, client) -> None:
        response = client.get("/settings/advanced/")
        html = response.get_data(as_text=True)
        assert response.status_code == HTTPStatus.OK
        assert "Advanced Settings" in html
        assert "Network sharing stub" in html
        removed_mode_terms = (
            "mode_control",
            "current_mode",
            "quick_edit",
            "Edit Mode",
            "Present Mode",
        )
        for forbidden in removed_mode_terms:
            assert forbidden not in html
        for forbidden in ("fsck", "loopback", "format partition"):
            assert forbidden not in html.lower()
        assert "cdn.jsdelivr.net" not in html
        assert "unpkg.com" not in html
        assert "<svg" in html

    def test_advanced_alias_renders_template(self, client) -> None:
        response = client.get("/settings/advanced/")
        assert response.status_code == HTTPStatus.OK
        assert "Advanced Settings" in response.get_data(as_text=True)

    def test_index_route_returns_json_on_unhandled_exception(self, client, service) -> None:
        with patch.object(service, "get_settings", side_effect=RuntimeError("boom")):
            response = client.get("/settings/advanced/")
        assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
        assert response.get_json() == {"success": False, "error": "Internal server error"}

    def test_index_sweep_has_zero_removed_feature_references(self, client) -> None:
        html = client.get("/settings/advanced/").get_data(as_text=True).lower()
        assert html.count("mode_control") == 0
        assert html.count("current_mode") == 0
        assert html.count("quick_edit") == 0
        assert html.count("fsck") == 0
        assert html.count("loopback") == 0

    def test_index_sweep_has_zero_removed_storage_terms(self, client) -> None:
        html = client.get("/settings/advanced/").get_data(as_text=True).lower()
        assert html.count("img") == 0
        assert html.count("format partition") == 0
        assert html.count("loopback") == 0

    def test_removed_feature_routes_are_not_exposed(self, client) -> None:
        for path in (
            "/settings/advanced/fsck",
            "/settings/advanced/loopback",
            "/settings/advanced/img",
        ):
            assert client.get(path).status_code == HTTPStatus.NOT_FOUND


class TestSettingsApi:
    def test_current_settings_json_endpoint(self, client) -> None:
        response = client.get("/api/settings/advanced")
        payload = response.get_json()
        assert response.status_code == HTTPStatus.OK
        assert payload["success"] is True
        assert payload["settings"] == {
            "ipc_socket_path": payload["settings"]["ipc_socket_path"],
            "log_level": "INFO",
            "samba_enabled": False,
            "state_path": payload["settings"]["state_path"],
        }

    def test_form_post_saves_settings_and_invalidates_cache(self, client, service, app) -> None:
        _ = service
        invalidator = app.extensions["cache_invalidator"]
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/settings/advanced/save?_=9",
                data={"samba_enabled": "on", "log_level": "ERROR", "<partition>": "part1"},
            )
        assert response.status_code == HTTPStatus.FOUND
        assert response.headers["Location"] == "/settings/advanced/?_=9"
        assert service.get_settings().samba_enabled is True
        assert service.get_settings().log_level == "ERROR"
        schedule_mock.assert_called_once()

    def test_api_post_saves_settings_and_invalidates_cache(self, client, service, app) -> None:
        _ = service
        invalidator = app.extensions["cache_invalidator"]
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/api/settings/advanced",
                json={"samba_enabled": True, "log_level": "WARNING"},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        payload = response.get_json()
        assert response.status_code == HTTPStatus.OK
        assert payload["success"] is True
        assert payload["settings"]["samba_enabled"] is True
        assert payload["settings"]["log_level"] == "WARNING"
        schedule_mock.assert_called_once()

    def test_api_post_rejects_invalid_log_level(self, client, app) -> None:
        invalidator = app.extensions["cache_invalidator"]
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/api/settings/advanced",
                json={"samba_enabled": True, "log_level": "TRACE"},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        assert response.status_code == HTTPStatus.BAD_REQUEST
        assert response.get_json()["success"] is False
        schedule_mock.assert_not_called()

    def test_current_settings_returns_boundary_error_for_bad_state(self, client, service) -> None:
        state_path = service.config.state_path
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("not json", encoding="utf-8")
        response = client.get("/api/settings/advanced")
        assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
        assert response.get_json()["success"] is False


class TestSambaToggle:
    def test_get_samba_toggle_state(self, client) -> None:
        response = client.get("/api/settings/advanced/samba")
        payload = response.get_json()
        assert response.status_code == HTTPStatus.OK
        assert payload["success"] is True
        assert payload["samba_enabled"] is False

    def test_post_samba_toggle_writes_stub_flag(self, client, service, app) -> None:
        _ = service
        invalidator = app.extensions["cache_invalidator"]
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/api/settings/advanced/samba",
                json={"samba_enabled": True},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        payload = response.get_json()
        assert response.status_code == HTTPStatus.OK
        assert payload["settings"]["samba_enabled"] is True
        assert service.get_settings().samba_enabled is True
        schedule_mock.assert_called_once()

    def test_post_samba_toggle_rejects_invalid_payload(self, client, app) -> None:
        invalidator = app.extensions["cache_invalidator"]
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/api/settings/advanced/samba",
                json={"samba_enabled": []},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        assert response.status_code == HTTPStatus.BAD_REQUEST
        assert response.get_json()["success"] is False
        schedule_mock.assert_not_called()


class TestIpcActions:
    def test_ipc_status_route_uses_teslafat_client(self, client, teslafat_client, app) -> None:
        invalidator = app.extensions["cache_invalidator"]
        with (
            patch.object(teslafat_client, "status", return_value=_FakeStatus()) as status_mock,
            patch.object(invalidator, "schedule") as schedule_mock,
        ):
            response = client.post(
                "/api/settings/advanced/ipc/status",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        payload = response.get_json()
        assert response.status_code == HTTPStatus.OK
        assert payload["ipc"]["state"] == "SERVING"
        status_mock.assert_called_once()
        schedule_mock.assert_called_once()

    def test_ipc_status_route_handles_daemon_error(self, client, teslafat_client, app) -> None:
        invalidator = app.extensions["cache_invalidator"]
        with (
            patch.object(
                teslafat_client,
                "status",
                side_effect=IpcDaemonError(ErrorBody(code="BUSY", message="busy")),
            ),
            patch.object(invalidator, "schedule") as schedule_mock,
        ):
            response = client.post(
                "/api/settings/advanced/ipc/status",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        assert response.status_code == HTTPStatus.BAD_GATEWAY
        assert response.get_json()["success"] is False
        schedule_mock.assert_not_called()

    def test_ipc_status_route_handles_missing_socket(self, client, teslafat_client) -> None:
        with patch.object(teslafat_client, "status", side_effect=FileNotFoundError()):
            response = client.post(
                "/api/settings/advanced/ipc/status",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert response.get_json()["success"] is False

    def test_ipc_status_route_handles_connection_error(self, client, teslafat_client) -> None:
        with patch.object(teslafat_client, "status", side_effect=ConnectionError("x" * 200)):
            response = client.post(
                "/api/settings/advanced/ipc/status",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert len(response.get_json()["message"]) <= 120

    def test_ipc_status_route_handles_protocol_error(self, client, teslafat_client) -> None:
        from teslausb_web.services.teslafat_client import IpcProtocolError

        with patch.object(teslafat_client, "status", side_effect=IpcProtocolError("bad" * 80)):
            response = client.post(
                "/api/settings/advanced/ipc/status",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        assert response.status_code == HTTPStatus.BAD_GATEWAY
        assert response.get_json()["success"] is False

    def test_ipc_cache_invalidate_route_uses_teslafat_client(
        self,
        client,
        teslafat_client,
        app,
    ) -> None:
        invalidator = app.extensions["cache_invalidator"]
        with (
            patch.object(teslafat_client, "invalidate_cache") as invalidate_mock,
            patch.object(invalidator, "schedule") as schedule_mock,
        ):
            response = client.post(
                "/api/settings/advanced/ipc/cache-invalidate",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        payload = response.get_json()
        assert response.status_code == HTTPStatus.OK
        assert payload["success"] is True
        invalidate_mock.assert_called_once()
        schedule_mock.assert_called_once()

    def test_ipc_cache_invalidate_route_handles_transport_error(
        self,
        client,
        teslafat_client,
    ) -> None:
        with patch.object(
            teslafat_client,
            "invalidate_cache",
            side_effect=ConnectionError("x" * 200),
        ):
            response = client.post(
                "/api/settings/advanced/ipc/cache-invalidate",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert len(response.get_json()["message"]) <= 120

    def test_ipc_cache_invalidate_route_handles_protocol_error(
        self,
        client,
        teslafat_client,
    ) -> None:
        from teslausb_web.services.teslafat_client import IpcProtocolError

        with patch.object(
            teslafat_client,
            "invalidate_cache",
            side_effect=IpcProtocolError("bad" * 80),
        ):
            response = client.post(
                "/api/settings/advanced/ipc/cache-invalidate",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        assert response.status_code == HTTPStatus.BAD_GATEWAY
        assert response.get_json()["success"] is False

    def test_ipc_status_form_route_redirects(self, client, teslafat_client, app) -> None:
        invalidator = app.extensions["cache_invalidator"]
        with (
            patch.object(teslafat_client, "status", return_value=_FakeStatus(state="INITIALIZING")),
            patch.object(invalidator, "schedule") as schedule_mock,
        ):
            response = client.post("/settings/advanced/ipc/status?_=5")
        assert response.status_code == HTTPStatus.FOUND
        assert response.headers["Location"] == "/settings/advanced/?_=5"
        schedule_mock.assert_called_once()


class TestTemplateFile:
    def test_template_file_exists(self) -> None:
        import teslausb_web

        template = Path(teslausb_web.__file__).parent / "templates" / "settings_advanced.html"
        assert template.is_file()
        assert template.read_text(encoding="utf-8").strip()
