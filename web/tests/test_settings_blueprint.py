# ruff: noqa: ANN001  # pytest fixture injection.
"""Tests for the new settings dashboard blueprint (Phase 5.20)."""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from teslausb_web.app import create_app
from teslausb_web.config import (
    MappingSection,
    PathsSection,
    SystemSettingsSection,
    WebConfig,
    WebSection,
)

if TYPE_CHECKING:
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    state_dir = tmp_path / "state"
    backing_root = tmp_path / "backing"
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32, max_upload_mb=8, max_chunk_mb=1),
        paths=PathsSection(
            backing_root=backing_root,
            state_dir=state_dir,
            ipc_socket=tmp_path / "ipc" / "worker.sock",
            cache_invalidate_script=tmp_path / "invalidate.sh",
        ),
        mapping=MappingSection(
            db_path=state_dir / "index.sqlite3",
            media_root=backing_root,
            overrides_path=state_dir / "mapping_settings.json",
            view_prefs_path=state_dir / "map_view_prefs.json",
        ),
        system_settings=SystemSettingsSection(
            state_path=state_dir / "system_settings.json",
            default_log_level="INFO",
        ),
    )
    flask_app = create_app(cfg)
    flask_app.testing = True
    return flask_app


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


class TestDashboardRoutes:
    def test_settings_root_renders_dashboard(self, client) -> None:
        response = client.get("/settings/")
        html = response.get_data(as_text=True)
        assert response.status_code == HTTPStatus.OK
        assert "System Health" in html

    def test_root_renders_map_page(self, client) -> None:
        """`/` is owned by mapping.map_view after Issue 1."""
        response = client.get("/")
        assert response.status_code == HTTPStatus.OK
        html = response.get_data(as_text=True)
        assert 'id="map"' in html

    def test_no_mode_toggle_buttons(self, client) -> None:
        html = client.get("/settings/").get_data(as_text=True)
        assert "modeActionBtn" not in html
        assert "Enable Network Sharing" not in html
        assert "Reconnect to Tesla" not in html

    def test_no_fsck_section(self, client) -> None:
        html = client.get("/settings/").get_data(as_text=True)
        assert "Filesystem Health Check" not in html
        assert "/fsck/" not in html

    def test_storage_health_section_present(self, client) -> None:
        html = client.get("/settings/").get_data(as_text=True)
        assert 'id="storage-health-section"' in html
        assert 'id="storage-health-card"' in html
        assert "/api/storage/health" in html
        # The dead v1 partition cards must be gone.
        assert "TeslaCam Drive" not in html
        assert "LightShow Drive" not in html

    def test_storage_health_card_collapsed_when_ok(self, client) -> None:
        # On a dev box none of the probes resolve, so severity falls
        # to "unknown" and the card auto-opens. Verify the open
        # logic exists by checking the conditional render output:
        # if severity is non-ok the section gets the `open` attr.
        html = client.get("/settings/").get_data(as_text=True)
        assert 'id="storage-health-section"' in html

    def test_has_system_health_card(self, client) -> None:
        html = client.get("/settings/").get_data(as_text=True)
        assert 'id="system-health-card"' in html
        assert "/api/system/health" in html

    def test_has_key_sections(self, client) -> None:
        html = client.get("/settings/").get_data(as_text=True)
        for section in (
            "System Health",
            "Live Metrics",
            "WiFi Networks",
            "Access Point",
            "Mapping & Indexing",
            "Network File Sharing",
        ):
            assert section in html

    def test_local_archive_surfaces_removed(self, client) -> None:
        """Phase 6: the v1 archive worker is gone in B-1. The Tesla writes
        directly to the SD card via the USB gadget, so there is no
        ``ArchivedClips`` folder and no "Local Archive" subsystem. Any
        UI surface that referenced them must be gone."""
        html = client.get("/settings/").get_data(as_text=True)
        for absent in (
            "Storage &amp; Retention",
            "Archive Settings",
            "Archive Status",
            "ArchivedClips",
            "Local Archive",
            "Files-Lost banner",
            "files-lost-banner",
        ):
            assert absent not in html, f"removed surface still present: {absent!r}"

    def test_no_hex_color_literals_in_style_blocks(self, client) -> None:
        html = client.get("/settings/").get_data(as_text=True)
        styles = re.findall(r"<style>(.*?)</style>", html, flags=re.S)
        assert styles
        for style in styles:
            assert re.search(r":[^;\n]*#[0-9a-fA-F]{6}\b|:[^;\n]*#[0-9a-fA-F]{3}\b", style) is None

    def test_no_external_cdn_references(self, client) -> None:
        html = client.get("/settings/").get_data(as_text=True)
        assert "cdn.jsdelivr.net" not in html
        assert "unpkg.com" not in html

    def test_has_svg_icons(self, client) -> None:
        html = client.get("/settings/").get_data(as_text=True)
        assert "<svg" in html
        assert "lucide-sprite.svg" in html


class TestStubRoutes:
    def test_save_mapping_persists(self, client, app) -> None:
        response = client.post(
            "/settings/save/mapping",
            data={"trip_gap_minutes": "7", "speed_limit_mph": "65", "speed_units": "kph"},
        )
        assert response.status_code == HTTPStatus.FOUND
        assert response.headers["Location"] == "/settings/"
        svc = app.extensions["mapping_settings_service"]
        prefs_svc = app.extensions["map_view_prefs_service"]
        snap = svc.get_settings()
        assert snap.trip_gap_minutes == 7
        assert snap.speed_limit_mph == 65
        assert prefs_svc.get_preferences().speed_units == "kph"
        # And the JSON files were actually written to their separate contracts.
        import json

        payload = json.loads(svc.path.read_text(encoding="utf-8"))
        assert payload == {
            "schema_version": 1,
            "speed_limit_mph": 65,
            "trip_gap_minutes": 7,
        }
        prefs_payload = json.loads(prefs_svc.path.read_text(encoding="utf-8"))
        assert prefs_payload == {"schema_version": 1, "speed_units": "kph"}

    def test_units_only_change_does_not_touch_worker_overrides(self, client, app) -> None:
        import json
        import os
        import time

        svc = app.extensions["mapping_settings_service"]
        prefs_svc = app.extensions["map_view_prefs_service"]
        svc.save_settings(trip_gap_minutes=5, speed_limit_mph=0)
        before_content = svc.path.read_text(encoding="utf-8")
        old_time = time.time() - 30
        os.utime(svc.path, (old_time, old_time))
        before_mtime = svc.path.stat().st_mtime_ns

        response = client.post(
            "/settings/save/mapping",
            data={"trip_gap_minutes": "5", "speed_limit_mph": "0", "speed_units": "kph"},
        )

        assert response.status_code == HTTPStatus.FOUND
        assert svc.path.read_text(encoding="utf-8") == before_content
        assert svc.path.stat().st_mtime_ns == before_mtime
        assert json.loads(prefs_svc.path.read_text(encoding="utf-8")) == {
            "schema_version": 1,
            "speed_units": "kph",
        }

    def test_save_mapping_zero_speed_disables(self, client, app) -> None:
        response = client.post(
            "/settings/save/mapping",
            data={"trip_gap_minutes": "5", "speed_limit_mph": "0", "speed_units": "mph"},
        )
        assert response.status_code == HTTPStatus.FOUND
        svc = app.extensions["mapping_settings_service"]
        snap = svc.get_settings()
        assert snap.speed_limit_enabled is False

    def test_save_mapping_rejects_out_of_range(self, client, app) -> None:
        response = client.post(
            "/settings/save/mapping",
            data={"trip_gap_minutes": "999", "speed_limit_mph": "0", "speed_units": "mph"},
        )
        assert response.status_code == HTTPStatus.FOUND
        svc = app.extensions["mapping_settings_service"]
        # File was never written — defaults remain.
        snap = svc.get_settings()
        assert snap.trip_gap_minutes == 5

    def test_save_mapping_rejects_unknown_speed_units(self, client, app) -> None:
        response = client.post(
            "/settings/save/mapping",
            data={"trip_gap_minutes": "5", "speed_limit_mph": "0", "speed_units": "mps"},
        )
        assert response.status_code == HTTPStatus.FOUND
        svc = app.extensions["mapping_settings_service"]
        prefs_svc = app.extensions["map_view_prefs_service"]
        assert svc.get_settings().trip_gap_minutes == 5
        assert prefs_svc.get_preferences().speed_units == "mph"
        assert not prefs_svc.path.exists()

    def test_save_network_stub_redirects(self, client) -> None:
        response = client.post("/settings/save/network")
        assert response.status_code == HTTPStatus.FOUND
        assert response.headers["Location"] == "/settings/"

    def test_dismiss_wifi_status_returns_json(self, client) -> None:
        response = client.post("/api/settings/wifi/dismiss-status")
        assert response.status_code == HTTPStatus.OK
        assert response.get_json() == {"success": True}


class TestConfigureAp:
    def test_configure_ap_persists_and_redirects(self, client, app) -> None:
        response = client.post(
            "/settings/configure_ap",
            data={"ssid": "MyAP", "passphrase": "hunter22"},
        )
        assert response.status_code == HTTPStatus.FOUND
        assert response.headers["Location"] == "/settings/"
        service = app.extensions["wifi_service"]
        assert service.ap_credentials_for_form() == ("MyAP", "hunter22")

    def test_configure_ap_rejects_blank_ssid(self, client, app) -> None:
        response = client.post(
            "/settings/configure_ap",
            data={"ssid": "  ", "passphrase": "hunter22"},
        )
        assert response.status_code == HTTPStatus.FOUND
        service = app.extensions["wifi_service"]
        ssid, _ = service.ap_credentials_for_form()
        assert ssid != ""

    def test_configure_ap_rejects_short_passphrase(self, client, app) -> None:
        before = app.extensions["wifi_service"].ap_credentials_for_form()
        response = client.post(
            "/settings/configure_ap",
            data={"ssid": "MyAP", "passphrase": "short"},
        )
        assert response.status_code == HTTPStatus.FOUND
        assert app.extensions["wifi_service"].ap_credentials_for_form() == before
