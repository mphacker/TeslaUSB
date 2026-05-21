# ruff: noqa: ANN001  # pytest fixture injection.
"""Tests for the new settings dashboard blueprint (Phase 5.20)."""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from teslausb_web.app import create_app
from teslausb_web.config import PathsSection, SystemSettingsSection, WebConfig, WebSection

if TYPE_CHECKING:
    from pathlib import Path

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

    def test_root_renders_dashboard(self, client) -> None:
        response = client.get("/")
        assert response.status_code == HTTPStatus.OK
        assert "System Health" in response.get_data(as_text=True)

    def test_no_mode_toggle_buttons(self, client) -> None:
        html = client.get("/settings/").get_data(as_text=True)
        assert "modeActionBtn" not in html
        assert "Enable Network Sharing" not in html
        assert "Reconnect to Tesla" not in html

    def test_no_fsck_section(self, client) -> None:
        html = client.get("/settings/").get_data(as_text=True)
        assert "Filesystem Health Check" not in html
        assert "/fsck/" not in html

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
            "Storage &amp; Retention",
            "Archive Settings",
            "Mapping & Indexing",
            "Network File Sharing",
        ):
            assert section in html

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
    def test_save_archive_stub_redirects(self, client) -> None:
        response = client.post("/settings/save/archive")
        assert response.status_code == HTTPStatus.FOUND
        assert response.headers["Location"] == "/settings/"

    def test_save_mapping_stub_redirects(self, client) -> None:
        response = client.post("/settings/save/mapping")
        assert response.status_code == HTTPStatus.FOUND
        assert response.headers["Location"] == "/settings/"

    def test_save_network_stub_redirects(self, client) -> None:
        response = client.post("/settings/save/network")
        assert response.status_code == HTTPStatus.FOUND
        assert response.headers["Location"] == "/settings/"

    def test_dismiss_wifi_status_returns_json(self, client) -> None:
        response = client.post("/api/settings/wifi/dismiss-status")
        assert response.status_code == HTTPStatus.OK
        assert response.get_json() == {"success": True}
