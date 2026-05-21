"""Tests for ``teslausb_web.helpers.refresh_cloud_token``."""

from __future__ import annotations

from pathlib import Path

from teslausb_web.config import ConfigError
from teslausb_web.helpers import refresh_cloud_token
from teslausb_web.services.cloud_oauth_service import (
    OAuthCredentials,
    RefreshResult,
    TokenRefreshError,
)


class _StubService:
    def __init__(
        self,
        *,
        result: RefreshResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[bool, str | None]] = []

    def refresh_if_needed(
        self,
        *,
        force: bool = False,
        provider: str | None = None,
    ) -> RefreshResult:
        self.calls.append((force, provider))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _stub_config_loader(*_args: object, **_kwargs: object) -> object:
    return object()


def test_main_returns_zero_when_no_credentials(monkeypatch: object) -> None:
    service = _StubService(
        result=RefreshResult(
            refreshed=False,
            credentials=None,
            message="No stored OAuth credentials",
        )
    )
    monkeypatch.setattr(refresh_cloud_token, "load_config", _stub_config_loader)
    monkeypatch.setattr(refresh_cloud_token, "make_oauth_service", lambda _cfg: service)
    assert refresh_cloud_token.main([]) == 0
    assert service.calls == [(False, None)]


def test_main_returns_zero_when_refresh_happens(monkeypatch: object) -> None:
    service = _StubService(
        result=RefreshResult(
            refreshed=True,
            credentials=OAuthCredentials(
                provider="dropbox",
                access_token="access",
                refresh_token="refresh",
                token_type="Bearer",
                expires_at="2099-01-01T00:00:00Z",
            ),
            message="OAuth token refreshed",
        )
    )
    monkeypatch.setattr(refresh_cloud_token, "load_config", _stub_config_loader)
    monkeypatch.setattr(refresh_cloud_token, "make_oauth_service", lambda _cfg: service)
    assert refresh_cloud_token.main([]) == 0


def test_main_forwards_force_and_provider(monkeypatch: object) -> None:
    service = _StubService(
        result=RefreshResult(
            refreshed=False,
            credentials=None,
            message="No stored OAuth credentials",
        )
    )
    monkeypatch.setattr(refresh_cloud_token, "load_config", _stub_config_loader)
    monkeypatch.setattr(refresh_cloud_token, "make_oauth_service", lambda _cfg: service)
    assert refresh_cloud_token.main(["--force", "--provider", "google-drive"]) == 0
    assert service.calls == [(True, "google-drive")]


def test_main_uses_explicit_config_path(monkeypatch: object, tmp_path: Path) -> None:
    service = _StubService(
        result=RefreshResult(
            refreshed=False,
            credentials=None,
            message="No stored OAuth credentials",
        )
    )
    observed: dict[str, Path | None] = {"path": None}

    def _load(*args: object, **_kwargs: object) -> object:
        observed["path"] = args[0] if isinstance(args[0], Path) else None
        return object()

    monkeypatch.setattr(refresh_cloud_token, "load_config", _load)
    monkeypatch.setattr(refresh_cloud_token, "make_oauth_service", lambda _cfg: service)
    cfg_path = tmp_path / "web.toml"
    assert refresh_cloud_token.main(["--config", str(cfg_path)]) == 0
    assert observed["path"] == cfg_path


def test_main_returns_one_on_config_error(monkeypatch: object) -> None:
    def _broken_loader(*_args: object, **_kwargs: object) -> object:
        raise ConfigError(None, "boom")

    monkeypatch.setattr(refresh_cloud_token, "load_config", _broken_loader)
    assert refresh_cloud_token.main([]) == 1


def test_main_returns_one_on_refresh_failure(monkeypatch: object) -> None:
    service = _StubService(error=TokenRefreshError("bad refresh"))
    monkeypatch.setattr(refresh_cloud_token, "load_config", _stub_config_loader)
    monkeypatch.setattr(refresh_cloud_token, "make_oauth_service", lambda _cfg: service)
    assert refresh_cloud_token.main([]) == 1
