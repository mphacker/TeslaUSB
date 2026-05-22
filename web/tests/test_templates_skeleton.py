"""Tests for Phase 5.4 — base.html + scaffold blueprints.

These tests verify that:

1. The five scaffold blueprints (``mapping``, ``analytics``,
   ``media``, ``cloud_archive``, ``settings``) register and expose
   the endpoints that ``base.html`` references via ``url_for``.
   Mapping and Cloud Archive now render their real pages; the
   remaining scaffold pages still serve placeholder bodies.
2. ``base.html`` renders to completion through Jinja — no
   ``BuildError`` from missing endpoints, no ``UndefinedError``
   from missing context vars (the context processor supplies
   conservative defaults).
3. The B-1 mode-removal edits are present (no `mode_control`
   references; the Samba dot conditional is in place).
4. An ``extras=[...]`` blueprint with the same name as a
   scaffold replaces it cleanly (so Phase 5.7+ can swap each
   scaffold for the real blueprint without touching this
   module's tests).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from flask import Blueprint, Flask, render_template
from teslausb_web.app import create_app
from teslausb_web.config import (
    FeaturesSection,
    LicensePlateSection,
    PathsSection,
    StorageRetentionSection,
    SystemSettingsSection,
    WebConfig,
    WebSection,
)


def _make_config() -> WebConfig:
    return WebConfig(
        web=WebSection(secret_key="t" * 32),
        paths=PathsSection(),
        features=FeaturesSection(),
    )


@pytest.fixture
def app() -> Flask:
    return create_app(_make_config())


SCAFFOLD_NAMES: tuple[str, ...] = (
    "mapping",
    "cloud_archive",
)


def test_all_scaffold_blueprints_registered(app: Flask) -> None:
    for name in SCAFFOLD_NAMES:
        assert name in app.blueprints, f"scaffold blueprint not registered: {name}"


def test_scaffold_endpoints_match_base_html_url_for_calls(app: Flask) -> None:
    """The endpoint names used by ``base.html`` must resolve."""
    with app.test_request_context():
        from flask import url_for

        # These mirror the exact `url_for` calls in
        # `teslausb_web/templates/base.html`. If any of them
        # raises BuildError, the template won't render.
        assert url_for("mapping.map_view").endswith("/")
        assert url_for("analytics.dashboard").endswith("/analytics/")
        assert url_for("media.media_home").endswith("/media/")
        assert url_for("cloud_archive.index").endswith("/cloud/")
        assert url_for("license_plates.license_plates").endswith("/license_plates/")
        assert url_for("settings.index").endswith("/settings/")


def test_mapping_scaffold_now_renders_real_page(app: Flask) -> None:
    resp = app.test_client().get("/")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'class="map-container"' in html
    assert 'id="videoPanel"' in html


def test_cloud_archive_scaffold_now_renders_real_page(app: Flask) -> None:
    resp = app.test_client().get("/cloud/")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'id="syncNowBtn"' in html
    assert 'id="oauthStartBtn"' in html
    assert "window.syncNow = async function()" in html


def test_license_plates_page_renders_real_template(tmp_path: Path) -> None:
    cfg = WebConfig(
        web=WebSection(secret_key="t" * 32),
        paths=PathsSection(
            backing_root=tmp_path / "backing",
            state_dir=tmp_path / "state",
            cache_invalidate_script=tmp_path / "invalidate.sh",
        ),
        features=FeaturesSection(),
        license_plates=LicensePlateSection(db_path=tmp_path / "state" / "license_plates.db"),
    )
    app = create_app(cfg)
    resp = app.test_client().get("/license_plates/")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert '<h1 class="plates-title">License Plates</h1>' in html
    assert 'type="module" src="/static/js/license_plates.js"' in html


def test_storage_settings_page_renders_real_template(tmp_path: Path) -> None:
    cfg = WebConfig(
        web=WebSection(secret_key="t" * 32),
        paths=PathsSection(
            backing_root=tmp_path / "backing",
            state_dir=tmp_path / "state",
            cache_invalidate_script=tmp_path / "invalidate.sh",
        ),
        features=FeaturesSection(),
    )
    app = create_app(cfg)
    resp = app.test_client().get("/storage")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Storage" in html
    assert "Auto-cleanup" in html
    assert "cdn.jsdelivr.net" not in html
    assert "unpkg.com" not in html


def test_advanced_settings_page_renders_real_template(tmp_path: Path) -> None:
    cfg = WebConfig(
        web=WebSection(secret_key="t" * 32),
        paths=PathsSection(
            backing_root=tmp_path / "backing",
            state_dir=tmp_path / "state",
            cache_invalidate_script=tmp_path / "invalidate.sh",
            ipc_socket=tmp_path / "ipc" / "worker.sock",
        ),
        features=FeaturesSection(),
        system_settings=SystemSettingsSection(
            state_path=tmp_path / "state" / "system_settings.json"
        ),
    )
    app = create_app(cfg)
    resp = app.test_client().get("/settings/")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "System Health" in html
    for forbidden in ("mode_control", "current_mode", "quick_edit", "fsck", "loopback"):
        assert forbidden not in html
    assert "cdn.jsdelivr.net" not in html
    assert "unpkg.com" not in html
    assert "#" not in html.split("<style>", 1)[1].split("</style>", 1)[0]


def test_base_template_renders_without_error(app: Flask) -> None:
    """``base.html`` must render to completion against the factory's defaults."""
    with app.test_request_context("/"):
        html = render_template("base.html")
    # Sanity: the rendered shell contains the brand, the nav, and
    # the static-asset links.
    assert "TeslaUSB" in html
    assert "lucide-sprite.svg" in html
    assert "css/style.css" in html
    # Mode-removal contract: the v1 `mode_token` status-dot markup
    # is gone.
    assert "status-present" not in html
    assert "status-edit" not in html
    assert "mode_control" not in html
    # Samba-off default: dot is hidden, so its CSS class doesn't
    # appear in the output.
    assert "status-samba-on" not in html


def test_base_template_renders_with_samba_on(app: Flask) -> None:
    with app.test_request_context("/"):
        html = render_template("base.html", samba_on=True)
    assert "status-samba-on" in html
    assert "Network sharing active" in html


def test_media_hub_nav_partial_exists() -> None:
    # The partial must be present so Phase 5.8+ can `include` it.
    import teslausb_web

    partial = Path(teslausb_web.__file__).parent / "templates" / "media_hub_nav.html"
    assert partial.is_file()
    assert partial.read_text(encoding="utf-8").strip(), "partial is empty"


def test_extras_blueprint_replaces_scaffold_with_same_name() -> None:
    """A test/real blueprint with the same name as a scaffold must win."""
    bp = Blueprint("settings", __name__, url_prefix="/settings")

    def view() -> tuple[str, int]:
        return "<p>real settings page</p>", 200

    bp.add_url_rule("/", endpoint="index", view_func=view)
    app = create_app(_make_config(), extra_blueprints=[bp])

    # The real blueprint registered first; the scaffold step must
    # have skipped its own settings registration.
    resp = app.test_client().get("/settings/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "real settings page" in body
    assert "scaffolding only" not in body


def test_context_processor_supplies_base_html_defaults(app: Flask) -> None:
    """All flags referenced by base.html must be defined in the context."""
    with app.test_request_context("/"):
        # The context processor runs lazily during template render;
        # check by rendering base.html and confirming it didn't
        # raise on any missing variable.
        html = render_template("base.html")
    # Defaults: operation_in_progress=False so the operation
    # banner block is NOT rendered.
    assert "operation-banner" not in html


def test_captive_portal_template_renders(app: Flask) -> None:
    response = app.test_client().get("/settings/wifi")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Connect TeslaUSB to Wi-Fi" in html
    assert "current_mode" not in html
    assert "quick_edit" not in html
    assert "cdn.jsdelivr.net" not in html
    assert "unpkg.com" not in html
    assert "#" not in html.split("<style>", 1)[1].split("</style>", 1)[0]


def test_healthz_still_works_with_scaffolds_registered(app: Flask) -> None:
    """Sanity: 5.2's /healthz route is unaffected by 5.4's blueprints."""
    resp = app.test_client().get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}
