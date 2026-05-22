# ruff: noqa: ANN001, ANN201, ANN202, TC003
"""Tests for the captive portal blueprint."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from teslausb_web.app import create_app
from teslausb_web.blueprints.captive_portal import (
    _get_service,
    _mutation_response,
    _request_value,
    _serialize_status,
    _wants_json_response,
)
from teslausb_web.config import FeaturesSection, PathsSection, WebConfig, WebSection
from teslausb_web.services.wifi_service import WifiError, WifiService

_XHR = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def app(tmp_path: Path):
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32),
        paths=PathsSection(
            backing_root=tmp_path / "backing",
            state_dir=tmp_path / "state",
            cache_invalidate_script=tmp_path / "invalidate.sh",
        ),
        features=FeaturesSection(),
    )
    flask_app = create_app(cfg)
    flask_app.testing = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def service(app):
    return app.extensions["wifi_service"]


def _status(
    *,
    connected: bool = False,
    current_ssid: str | None = None,
    ap_active: bool = True,
    restore_deadline=None,
):
    ap_mode = MagicMock(
        requested_enabled=ap_active,
        active=ap_active,
        ssid="TeslaUSB-Setup",
        passphrase_configured=False,
        restore_deadline=restore_deadline,
    )
    saved = [MagicMock(ssid="SavedOne", security="WPA2", has_passphrase=True, active=False)]
    return MagicMock(
        connected=connected,
        current_ssid=current_ssid,
        signal_strength=72,
        ip_address="192.168.1.50" if connected else None,
        ap_mode=ap_mode,
        saved_networks=saved,
    )


def test_app_registers_captive_portal_blueprint_and_service(app) -> None:
    assert "captive_portal" in app.blueprints
    assert isinstance(app.extensions["wifi_service"], WifiService)


def test_get_service_rejects_misconfigured_extension(app) -> None:
    with app.app_context():
        original = app.extensions["wifi_service"]
        app.extensions["wifi_service"] = object()
        with pytest.raises(RuntimeError, match="wifi_service"):
            _get_service()
        app.extensions["wifi_service"] = original


def test_helper_request_value_and_json_preference(app) -> None:
    with app.test_request_context(
        "/settings/wifi/connect", method="POST", json={"ssid": "JsonNet"}
    ):
        assert _request_value("ssid") == "JsonNet"
        assert _wants_json_response() is True
    with app.test_request_context(
        "/settings/wifi/connect", method="POST", data={"ssid": "FormNet"}
    ):
        assert _request_value("ssid") == "FormNet"
        assert _wants_json_response() is False


def test_serialize_status_returns_json_ready_payload(app, service) -> None:
    with (
        app.app_context(),
        patch.object(
            service, "get_status", return_value=_status(connected=True, current_ssid="Home")
        ),
    ):
        payload = _serialize_status()
    assert payload["connected"] is True
    assert payload["current_ssid"] == "Home"
    assert payload["saved_networks"][0]["ssid"] == "SavedOne"


def test_mutation_response_redirects_for_html(app) -> None:
    with app.test_request_context("/settings/wifi/connect", method="POST"):
        response = _mutation_response(success=True, message="ok", status=HTTPStatus.OK)
    assert response.status_code == HTTPStatus.FOUND
    assert response.headers["Location"].endswith("/settings/wifi")


def test_wifi_setup_renders_template(client, service) -> None:
    with (
        patch.object(
            service,
            "list_available_networks",
            return_value=[
                MagicMock(
                    ssid="Cafe",
                    signal_strength=40,
                    secured=True,
                    security="WPA2",
                    active=False,
                    saved=False,
                )
            ],
        ),
        patch.object(service, "get_status", return_value=_status()),
    ):
        response = client.get("/settings/wifi")
    html = response.get_data(as_text=True)
    assert response.status_code == HTTPStatus.OK
    assert "Connect TeslaUSB to Wi-Fi" in html
    assert "Cafe" in html
    assert "current_mode" not in html
    assert "quick_edit" not in html
    assert "cdn.jsdelivr.net" not in html
    assert "unpkg.com" not in html


def test_wifi_setup_handles_scan_error(client, service) -> None:
    with (
        patch.object(service, "list_available_networks", side_effect=RuntimeError("scan failed")),
        patch.object(service, "get_status", return_value=_status()),
    ):
        response = client.get("/settings/wifi")
    assert response.status_code == HTTPStatus.OK
    assert "scan failed" in response.get_data(as_text=True)


def test_captive_portal_shortcuts_render_template(client, service) -> None:
    with (
        patch.object(service, "list_available_networks", return_value=[]),
        patch.object(service, "get_status", return_value=_status()),
    ):
        for url in (
            "/hotspot-detect.html",
            "/library/test/success.html",
            "/generate_204",
            "/gen_204",
            "/connecttest.txt",
            "/ncsi.txt",
            "/redirect",
            "/success.txt",
            "/canonical.html",
        ):
            response = client.get(url)
            assert response.status_code == HTTPStatus.OK
            assert "Wi-Fi setup" in response.get_data(as_text=True)


def test_favicon_returns_204(client) -> None:
    response = client.get("/favicon.ico")
    assert response.status_code == HTTPStatus.NO_CONTENT
    assert response.get_data(as_text=True) == ""


def test_wifi_status_endpoint_returns_json(client, service) -> None:
    with patch.object(
        service, "get_status", return_value=_status(connected=True, current_ssid="Home")
    ):
        response = client.get("/settings/wifi/status")
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["current_ssid"] == "Home"


def test_wifi_networks_endpoint_returns_json(client, service) -> None:
    with patch.object(
        service,
        "list_available_networks",
        return_value=[
            MagicMock(
                ssid="Cafe",
                signal_strength=55,
                secured=False,
                security="open",
                active=False,
                saved=False,
            )
        ],
    ):
        response = client.get("/settings/wifi/networks?rescan=1")
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert payload["success"] is True
    assert payload["networks"][0]["ssid"] == "Cafe"


def test_wifi_networks_endpoint_translates_error(client, service) -> None:
    with patch.object(service, "list_available_networks", side_effect=RuntimeError("boom")):
        response = client.get("/settings/wifi/networks")
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "Internal server error"


def test_api_wifi_saved_marks_in_range_from_scan(client, service) -> None:
    saved = [
        MagicMock(ssid="HomeNet", security="WPA2", has_passphrase=True, active=True),
        MagicMock(ssid="GuestNet", security="WPA2", has_passphrase=True, active=False),
        MagicMock(ssid="GoneNet", security="WPA2", has_passphrase=True, active=False),
    ]
    status = MagicMock(
        connected=True,
        current_ssid="HomeNet",
        signal_strength=78,
        ip_address="192.168.1.50",
        ap_mode=MagicMock(active=False),
        saved_networks=saved,
    )
    scan = [
        MagicMock(ssid="HomeNet", signal_strength=70),
        MagicMock(ssid="HomeNet", signal_strength=82),  # stronger band, should win
        MagicMock(ssid="GuestNet", signal_strength=45),
        MagicMock(ssid="StrangerNet", signal_strength=60),
    ]
    with (
        patch.object(service, "get_status", return_value=status),
        patch.object(service, "list_available_networks", return_value=scan),
        patch.object(service, "saved_wifi_profile_ssids", return_value={}),
    ):
        response = client.get("/api/wifi/saved")
    assert response.status_code == HTTPStatus.OK
    payload = {row["ssid"]: row for row in response.get_json()}
    # Active connection: in_range True, signal from strongest scan entry.
    assert payload["HomeNet"]["in_range"] is True
    assert payload["HomeNet"]["active"] is True
    assert payload["HomeNet"]["signal"] == 82
    # Saved + visible but not connected: in_range True with scan signal.
    assert payload["GuestNet"]["in_range"] is True
    assert payload["GuestNet"]["signal"] == 45
    # Saved but not in scan: in_range False, signal 0.
    assert payload["GoneNet"]["in_range"] is False
    assert payload["GoneNet"]["signal"] == 0


def test_api_wifi_saved_resolves_profile_name_to_air_ssid(client, service) -> None:
    """NM profile id may differ from broadcast SSID (e.g. WiFi-Trez/Trez)."""
    saved = [
        MagicMock(ssid="WiFi-Trez", security="WPA2", has_passphrase=True, active=False),
        MagicMock(ssid="WiFi-Trez_EXT", security="WPA2", has_passphrase=True, active=True),
    ]
    status = MagicMock(
        connected=True,
        current_ssid="WiFi-Trez_EXT",
        signal_strength=78,
        ip_address="192.168.1.50",
        ap_mode=MagicMock(active=False),
        saved_networks=saved,
    )
    # Scan returns the on-air SSIDs, not profile names.
    scan = [
        MagicMock(ssid="Trez", signal_strength=55),
        MagicMock(ssid="Trez_EXT", signal_strength=82),
    ]
    profile_map = {
        "WiFi-Trez": "Trez",
        "WiFi-Trez_EXT": "Trez_EXT",
    }
    with (
        patch.object(service, "get_status", return_value=status),
        patch.object(service, "list_available_networks", return_value=scan),
        patch.object(service, "saved_wifi_profile_ssids", return_value=profile_map),
    ):
        response = client.get("/api/wifi/saved")
    payload = {row["ssid"]: row for row in response.get_json()}
    assert payload["WiFi-Trez"]["in_range"] is True
    assert payload["WiFi-Trez"]["signal"] == 55
    assert payload["WiFi-Trez_EXT"]["in_range"] is True
    assert payload["WiFi-Trez_EXT"]["signal"] == 82


def test_api_wifi_saved_active_in_range_when_scan_unavailable(client, service) -> None:
    saved = [MagicMock(ssid="HomeNet", security="WPA2", has_passphrase=True, active=True)]
    status = MagicMock(
        connected=True,
        current_ssid="HomeNet",
        signal_strength=78,
        ip_address="192.168.1.50",
        ap_mode=MagicMock(active=False),
        saved_networks=saved,
    )
    with (
        patch.object(service, "get_status", return_value=status),
        patch.object(
            service, "list_available_networks", side_effect=WifiError("scan unavailable")
        ),
        patch.object(service, "saved_wifi_profile_ssids", return_value={"HomeNet": "HomeNet"}),
    ):
        response = client.get("/api/wifi/saved")
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    # Even with no scan, the actively connected SSID is in range.
    assert payload[0]["in_range"] is True
    assert payload[0]["signal"] == 78


def test_connect_route_redirects_for_html(client, service) -> None:
    with patch.object(
        service, "connect", return_value=_status(connected=True, current_ssid="Home")
    ):
        response = client.post(
            "/settings/wifi/connect", data={"ssid": "Home", "passphrase": "supersecret"}
        )
    assert response.status_code == HTTPStatus.FOUND
    assert response.headers["Location"].endswith("/settings/wifi")


def test_connect_route_returns_json_for_xhr(client, service) -> None:
    with (
        patch.object(service, "connect", return_value=_status(connected=True, current_ssid="Home")),
        patch.object(
            service, "get_status", return_value=_status(connected=True, current_ssid="Home")
        ),
    ):
        response = client.post("/settings/wifi/connect", json={"ssid": "Home"}, headers=_XHR)
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["success"] is True
    assert response.get_json()["current_ssid"] == "Home"


def test_connect_route_translates_wifi_command_error(client, service) -> None:
    with patch.object(service, "connect", side_effect=RuntimeError("boom")):
        response = client.post("/settings/wifi/connect", json={"ssid": "Home"}, headers=_XHR)
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "Internal server error"


def test_disconnect_route_enables_ap(client, service) -> None:
    with (
        patch.object(service, "disconnect", return_value=_status()),
        patch.object(service, "get_status", return_value=_status()),
    ):
        response = client.post("/settings/wifi/disconnect", json={}, headers=_XHR)
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["message"].startswith("Disconnected")


def test_forget_route_redirects(client, service) -> None:
    with patch.object(service, "forget_network", return_value=_status()):
        response = client.post("/settings/wifi/forget", data={"ssid": "SavedOne"})
    assert response.status_code == HTTPStatus.FOUND


def test_forget_route_returns_json(client, service) -> None:
    with (
        patch.object(service, "forget_network", return_value=_status()),
        patch.object(service, "get_status", return_value=_status()),
    ):
        response = client.post("/settings/wifi/forget", json={"ssid": "SavedOne"}, headers=_XHR)
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["message"] == "Forgot network SavedOne"


def test_toggle_ap_mode_enables(client, service) -> None:
    with (
        patch.object(service, "set_ap_mode", return_value=MagicMock()),
        patch.object(service, "get_status", return_value=_status(ap_active=True)),
    ):
        response = client.post("/settings/wifi/access-point", json={"enabled": True}, headers=_XHR)
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["message"] == "Setup access point enabled"


def test_toggle_ap_mode_disables(client, service) -> None:
    with patch.object(service, "set_ap_mode", return_value=MagicMock()):
        response = client.post("/settings/wifi/access-point", data={"enabled": "false"})
    assert response.status_code == HTTPStatus.FOUND


def test_boundary_error_redirects_non_json_mutations(client, service) -> None:
    with patch.object(service, "forget_network", side_effect=ValueError("bad ssid")):
        response = client.post("/settings/wifi/forget", data={"ssid": ""})
    assert response.status_code == HTTPStatus.FOUND


def test_wifi_setup_boundary_error_returns_json_for_status_path(client, service) -> None:
    with patch.object(service, "get_status", side_effect=ValueError("broken")):
        response = client.get("/settings/wifi/status")
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "broken"
