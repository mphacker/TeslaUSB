"""Tests for ``teslausb_web.blueprints.media`` (Phase 5.25)."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

from teslausb_web.app import create_app
from teslausb_web.config import (
    FeaturesSection,
    PathsSection,
    WebConfig,
    WebSection,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from flask import Flask
    from flask.testing import FlaskClient


def _make_config(
    tmp_path: Path,
    *,
    music_enabled: bool = False,
    boombox_enabled: bool = False,
) -> WebConfig:
    return WebConfig(
        web=WebSection(secret_key="m" * 32),
        paths=PathsSection(
            backing_root=tmp_path / "backing",
            state_dir=tmp_path / "state",
            cache_invalidate_script=tmp_path / "invalidate.sh",
        ),
        features=FeaturesSection(
            music_enabled=music_enabled,
            boombox_enabled=boombox_enabled,
        ),
        source_path=None,
    )


def _ensure_lightshow(cfg: WebConfig) -> Path:
    target = cfg.paths.media_root
    target.mkdir(parents=True, exist_ok=True)
    return target


def _ensure_music(cfg: WebConfig) -> Path:
    target = cfg.paths.backing_root / cfg.music.folder
    target.mkdir(parents=True, exist_ok=True)
    return target


def _client(cfg: WebConfig) -> FlaskClient:
    app: Flask = create_app(cfg)
    app.testing = True
    return app.test_client()


# ---------------------------------------------------------------------------
# Cascade exhaustion — one test per branch of _pick_target.
# ---------------------------------------------------------------------------


class TestCascade:
    def test_lightshow_drive_present_redirects_to_lock_chimes(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path, music_enabled=True, boombox_enabled=True)
        _ensure_lightshow(cfg)
        _ensure_music(cfg)  # music drive also present — must lose to lightshow

        response = _client(cfg).get("/media/")

        assert response.status_code == HTTPStatus.FOUND
        location = response.headers.get("Location")
        assert location is not None
        assert location.endswith("/lock_chimes/")

    def test_music_drive_present_and_music_enabled_redirects_to_chimes(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path, music_enabled=True, boombox_enabled=False)
        _ensure_music(cfg)

        response = _client(cfg).get("/media/")

        assert response.status_code == HTTPStatus.FOUND
        location = response.headers.get("Location")
        assert location is not None
        assert location.endswith("/lock_chimes/")

    def test_music_drive_present_boombox_only_redirects_to_chimes(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path, music_enabled=False, boombox_enabled=True)
        _ensure_music(cfg)

        response = _client(cfg).get("/media/")

        assert response.status_code == HTTPStatus.FOUND
        location = response.headers.get("Location")
        assert location is not None
        assert location.endswith("/lock_chimes/")

    def test_music_present_but_both_features_disabled_falls_back(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path, music_enabled=False, boombox_enabled=False)
        _ensure_music(cfg)

        response = _client(cfg).get("/media/")

        assert response.status_code == HTTPStatus.FOUND
        location = response.headers.get("Location")
        assert location is not None
        assert location.endswith("/lock_chimes/")

    def test_music_present_features_disabled_skips_music(self, tmp_path: Path) -> None:
        """Music drive present but ``music_enabled`` false must NOT
        redirect to ``/music/`` — that would surface a disabled feature.
        """
        cfg = _make_config(tmp_path, music_enabled=False, boombox_enabled=False)
        _ensure_music(cfg)

        response = _client(cfg).get("/media/")

        location = response.headers.get("Location") or ""
        assert not location.endswith("/music/")

    def test_no_drives_present_falls_back_to_lock_chimes(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path, music_enabled=True, boombox_enabled=True)
        # Neither lightshow nor music dir created.

        response = _client(cfg).get("/media/")

        assert response.status_code == HTTPStatus.FOUND
        location = response.headers.get("Location")
        assert location is not None
        assert location.endswith("/lock_chimes/")


# ---------------------------------------------------------------------------
# Pure cascade helper — covers OSError-from-stat branch too.
# ---------------------------------------------------------------------------


class TestPickTarget:
    def test_pick_target_prefers_lightshow(self) -> None:
        from teslausb_web.blueprints.media import _MediaAvailability, _pick_target

        availability = _MediaAvailability(
            lightshow_present=True,
            music_drive_present=True,
            music_enabled=True,
            boombox_enabled=True,
        )
        assert _pick_target(availability) == "lock_chimes.lock_chimes"

    def test_pick_target_music_enabled_still_picks_chimes(self) -> None:
        from teslausb_web.blueprints.media import _MediaAvailability, _pick_target

        availability = _MediaAvailability(
            lightshow_present=False,
            music_drive_present=True,
            music_enabled=True,
            boombox_enabled=True,
        )
        assert _pick_target(availability) == "lock_chimes.lock_chimes"

    def test_pick_target_falls_back_when_nothing_available(self) -> None:
        from teslausb_web.blueprints.media import _MediaAvailability, _pick_target

        availability = _MediaAvailability(
            lightshow_present=False,
            music_drive_present=False,
            music_enabled=True,
            boombox_enabled=True,
        )
        assert _pick_target(availability) == "lock_chimes.lock_chimes"


class TestDirExists:
    def test_returns_false_when_stat_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from teslausb_web.services import media_availability

        target = tmp_path / "boom"

        def _explode(_self: object) -> bool:
            raise OSError("permission denied")

        monkeypatch.setattr("pathlib.Path.is_dir", _explode)
        assert media_availability._dir_exists(target) is False


# ---------------------------------------------------------------------------
# URL-map / endpoint contract.
# ---------------------------------------------------------------------------


class TestUrlMap:
    def test_media_home_endpoint_is_registered(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        app = create_app(cfg)
        endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
        assert "media.media_home" in endpoints

    def test_media_blueprint_replaces_scaffold(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        app = create_app(cfg)
        # The scaffold stub served a 200 placeholder; the real
        # blueprint must serve a 302 redirect now.
        response = app.test_client().get("/media/")
        assert response.status_code == HTTPStatus.FOUND
