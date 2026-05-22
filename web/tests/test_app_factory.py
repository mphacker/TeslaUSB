"""Tests for ``teslausb_web.app.create_app`` — Flask factory."""

from __future__ import annotations

import errno
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from flask import Blueprint, abort
from teslausb_web import app as app_module
from teslausb_web.app import create_app
from teslausb_web.config import (
    FeaturesSection,
    PathsSection,
    SystemSettingsSection,
    WebConfig,
    WebSection,
)
from teslausb_web.services.system_settings_service import SystemSettingsService

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient


def _make_config(
    *, samba: bool = False, secret: str = "test-key-32-chars-xxxxxxxxxxxxxxx"
) -> WebConfig:
    return WebConfig(
        web=WebSection(secret_key=secret, max_upload_mb=128, max_chunk_mb=16),
        paths=PathsSection(),
        features=FeaturesSection(samba_enabled=samba),
        source_path=None,
    )


@pytest.fixture
def app() -> Flask:
    return create_app(_make_config())


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


def test_factory_returns_flask_app(app: Flask) -> None:
    assert app.name == "teslausb_web"


def test_factory_applies_upload_limits() -> None:
    app = create_app(_make_config())
    assert app.config["MAX_CONTENT_LENGTH"] == 128 * 1024 * 1024
    assert app.config["MAX_FORM_MEMORY_SIZE"] == 16 * 1024 * 1024


def test_factory_disables_x_sendfile_and_template_reload() -> None:
    app = create_app(_make_config())
    assert app.config["USE_X_SENDFILE"] is False
    assert app.config["TEMPLATES_AUTO_RELOAD"] is False


def test_factory_uses_provided_secret_key() -> None:
    app = create_app(_make_config(secret="my-very-real-key-aaaaaaaaaaaaaaaaa"))
    assert app.secret_key == "my-very-real-key-aaaaaaaaaaaaaaaaa"


def test_factory_generates_secret_when_empty() -> None:
    app = create_app(_make_config(secret=""))
    assert isinstance(app.secret_key, str)
    assert len(app.secret_key) >= 32


def test_typed_config_is_stashed_under_key() -> None:
    cfg = _make_config(samba=True)
    app = create_app(cfg)
    stashed = app.config["teslausb_config"]
    assert stashed is cfg
    assert stashed.features.samba_enabled is True


def test_healthz_returns_json_ok(client: FlaskClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.is_json
    assert resp.get_json() == {"status": "ok"}


def test_tile_cache_sw_404_when_file_missing(tmp_path: Path) -> None:
    # Phase 5.3 lands the real ``tile-cache-sw.js`` in the package's
    # static dir, so to exercise the missing-file branch we must
    # point Flask at an empty static folder explicitly.
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    cfg = _make_config()
    app = create_app(cfg)
    app.static_folder = str(static_dir)
    resp = app.test_client().get("/tile-cache-sw.js")
    assert resp.status_code == 404


def test_tile_cache_sw_404_when_static_folder_none(tmp_path: Path) -> None:
    cfg = _make_config()
    app = create_app(cfg)
    # Force-clear static_folder to exercise the abort(404) branch.
    app.static_folder = None
    resp = app.test_client().get("/tile-cache-sw.js")
    assert resp.status_code == 404


def test_tile_cache_sw_served_when_file_present(tmp_path: Path) -> None:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "tile-cache-sw.js").write_text("// stub SW\n", encoding="utf-8")
    cfg = _make_config()
    app = create_app(cfg)
    app.static_folder = str(static_dir)
    resp = app.test_client().get("/tile-cache-sw.js")
    assert resp.status_code == 200
    assert resp.mimetype == "application/javascript"
    assert b"stub SW" in resp.data


def test_os_error_enospc_returns_413_html() -> None:
    cfg = _make_config()
    raised = OSError(errno.ENOSPC, "no space left on device")
    bp = Blueprint("boom", __name__)

    @bp.route("/boom")
    def _boom() -> str:
        raise raised

    app = create_app(cfg, extra_blueprints=[bp])
    resp = app.test_client().get("/boom")
    assert resp.status_code == 413
    assert b"Upload too large" in resp.data


def test_os_error_enospc_returns_413_json_for_xhr() -> None:
    cfg = _make_config()
    bp = Blueprint("boom", __name__)

    @bp.route("/boom")
    def _boom() -> str:
        raise OSError(errno.ENOSPC, "out of space")

    app = create_app(cfg, extra_blueprints=[bp])
    resp = app.test_client().get("/boom", headers={"X-Requested-With": "XMLHttpRequest"})
    assert resp.status_code == 413
    assert resp.is_json
    payload = resp.get_json()
    assert payload is not None
    assert payload["success"] is False
    assert "Upload too large" in payload["error"]


def test_os_error_other_errno_propagates() -> None:
    cfg = _make_config()
    bp = Blueprint("boom", __name__)

    @bp.route("/boom")
    def _boom() -> str:
        raise OSError(errno.EACCES, "permission denied")

    app = create_app(cfg, extra_blueprints=[bp])
    # Flask in test mode propagates non-handled OSErrors as 500.
    resp = app.test_client().get("/boom")
    assert resp.status_code == 500


def test_extra_blueprints_are_registered() -> None:
    cfg = _make_config()
    bp = Blueprint("hello", __name__)

    @bp.route("/hello")
    def _hello() -> str:
        return "hi"

    app = create_app(cfg, extra_blueprints=[bp])
    resp = app.test_client().get("/hello")
    assert resp.status_code == 200
    assert resp.data == b"hi"


def test_no_blueprints_still_serves_standard_routes() -> None:
    app = create_app(_make_config())
    resp = app.test_client().get("/healthz")
    assert resp.status_code == 200


def test_unknown_route_returns_404(client: FlaskClient) -> None:
    resp = client.get("/this-does-not-exist")
    assert resp.status_code == 404


def test_factory_loads_from_config_path_when_no_config_object(tmp_path: Path) -> None:
    cfg_file = tmp_path / "loader.toml"
    cfg_file.write_text(
        '[web]\nport = 8081\nsecret_key = "loaded-from-file-aaaaaaaaaaaaaaaaa"\n',
        encoding="utf-8",
    )
    app = create_app(config_path=cfg_file)
    assert app.secret_key == "loaded-from-file-aaaaaaaaaaaaaaaaa"
    assert app.config["teslausb_config"].web.port == 8081


def test_factory_allow_defaults_starts_without_file() -> None:
    # Skips on a dev box that has a real /etc/teslausb/teslausb-web.toml.
    from teslausb_web.config import DEFAULT_CONFIG_PATH

    if DEFAULT_CONFIG_PATH.exists():
        pytest.skip("real config file present")
    app = create_app(allow_defaults=True)
    assert app.config["teslausb_config"].source_path is None


def test_factory_does_not_call_logging_basicconfig(monkeypatch: pytest.MonkeyPatch) -> None:
    """Charter §3: no global logger mutation in library code."""
    import logging

    called = False

    def _spy(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(logging, "basicConfig", _spy)
    create_app(_make_config())
    assert not called, "create_app must not call logging.basicConfig"


def test_blueprint_abort_works_alongside_error_handler() -> None:
    cfg = _make_config()
    bp = Blueprint("abrt", __name__)

    @bp.route("/abrt")
    def _abrt() -> str:
        abort(403)

    app = create_app(cfg, extra_blueprints=[bp])
    resp = app.test_client().get("/abrt")
    assert resp.status_code == 403


def test_cleanup_service_no_longer_registered(app: Flask) -> None:
    # AC.7: legacy Python cleanup service was removed. The Rust
    # worker is now the sole executor for TeslaCam cleanup, and the
    # /storage page replaces the old /cleanup blueprint.
    assert "cleanup_service" not in app.extensions
    assert "storage_retention_service" not in app.extensions


def test_cache_invalidator_registered_on_app(app: Flask) -> None:
    from teslausb_web.services.cache_invalidation import CacheInvalidator

    invalidator = app.extensions["cache_invalidator"]
    assert isinstance(invalidator, CacheInvalidator)


def test_boombox_service_registered_on_app(app: Flask) -> None:
    from teslausb_web.services.boombox_service import BoomboxService

    boombox_service = app.extensions["boombox_service"]
    assert isinstance(boombox_service, BoomboxService)


def test_cache_invalidator_uses_configured_script_path() -> None:
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32, max_upload_mb=8, max_chunk_mb=1),
        paths=PathsSection(cache_invalidate_script=Path("/opt/custom/invalidate.sh")),
        features=FeaturesSection(),
        source_path=None,
    )
    app = create_app(cfg)
    invalidator = app.extensions["cache_invalidator"]
    cmd = list(invalidator.command)
    assert cmd[0] == "sudo"
    assert Path(cmd[1]) == Path("/opt/custom/invalidate.sh")


def test_cache_invalidator_shutdown_registered_at_atexit(monkeypatch: pytest.MonkeyPatch) -> None:
    registered: list[object] = []

    def fake_register(fn: object, /) -> object:
        registered.append(fn)
        return fn

    monkeypatch.setattr("teslausb_web.app.atexit.register", fake_register)
    app = create_app(_make_config())
    # Exactly one new atexit entry, and it is the invalidator's shutdown bound method.
    assert any(
        getattr(fn, "__self__", None) is app.extensions["cache_invalidator"] for fn in registered
    )


class _FakeSambaService:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        self.starts += 1

    def stop(self, timeout: float = 5.0) -> None:
        _ = timeout
        self.stops += 1


class _FakeSambaWatcher:
    def __init__(self) -> None:
        self.starts = 0
        self.shutdowns = 0

    def start(self) -> bool:
        self.starts += 1
        return True

    def shutdown(self, timeout: float = 5.0) -> bool:
        _ = timeout
        self.shutdowns += 1
        return True


def _make_runtime_config(tmp_path: Path, *, samba_enabled: bool) -> WebConfig:
    return WebConfig(
        web=WebSection(secret_key="x" * 32),
        paths=PathsSection(
            backing_root=tmp_path / "backing",
            state_dir=tmp_path / "state",
            ipc_socket=tmp_path / "ipc" / "worker.sock",
            cache_invalidate_script=tmp_path / "invalidate.sh",
        ),
        features=FeaturesSection(samba_enabled=samba_enabled),
        system_settings=SystemSettingsSection(state_path=tmp_path / "state" / "settings.json"),
    )


def test_factory_starts_samba_pair_when_settings_enable_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_service = _FakeSambaService()
    fake_watcher = _FakeSambaWatcher()
    monkeypatch.setattr(app_module, "make_samba_service", lambda _cfg: fake_service)
    monkeypatch.setattr(app_module, "make_samba_watcher", lambda _cfg, _invalidator: fake_watcher)
    create_app(_make_runtime_config(tmp_path, samba_enabled=True))
    assert fake_service.starts == 1
    assert fake_watcher.starts == 1


def test_system_settings_toggle_drives_samba_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_service = _FakeSambaService()
    fake_watcher = _FakeSambaWatcher()
    monkeypatch.setattr(app_module, "make_samba_service", lambda _cfg: fake_service)
    monkeypatch.setattr(app_module, "make_samba_watcher", lambda _cfg, _invalidator: fake_watcher)
    app = create_app(_make_runtime_config(tmp_path, samba_enabled=False))
    settings_service = app.extensions["system_settings_service"]
    assert isinstance(settings_service, SystemSettingsService)
    settings_service.update_settings({"samba_enabled": True})
    settings_service.update_settings({"samba_enabled": False})
    assert fake_service.starts == 1
    assert fake_watcher.starts == 1
    assert fake_watcher.shutdowns >= 1
    assert fake_service.stops >= 1
