"""Tests for ``teslausb_web.services.cloud_oauth_service``."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.error import HTTPError

import pytest
from teslausb_web.app import create_app
from teslausb_web.config import CloudSection, PathsSection, WebConfig, WebSection, load_config
from teslausb_web.services.cloud_oauth_service import (
    CloudOAuthService,
    OAuthConfig,
    OAuthConfigError,
    OAuthError,
    TokenRefreshError,
    make_oauth_service,
)


class _StubResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self) -> bytes:
        return self._payload

    def close(self) -> None:
        self.closed = True


def _patch_open_url(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: dict[str, object] | None = None,
    error: Exception | None = None,
) -> None:
    def _fake_open_url(*_args: object, **_kwargs: object) -> _StubResponse:
        if error is not None:
            raise error
        return _StubResponse({} if payload is None else payload)

    monkeypatch.setattr("teslausb_web.services.cloud_oauth_service._open_url", _fake_open_url)


def _service(tmp_path: Path, *, refresh_window_seconds: int = 300) -> CloudOAuthService:
    return CloudOAuthService(
        OAuthConfig(
            credentials_path=tmp_path / "credentials.json",
            oauth_state_path=tmp_path / "oauth-state.json",
            rclone_config_path=tmp_path / "rclone",
            refresh_window_seconds=refresh_window_seconds,
        )
    )


def _write_credentials(
    path: Path,
    *,
    provider: str = "google-drive",
    expires_at: str = "2099-01-01T00:00:00Z",
    refresh_token: str = "refresh-123",
) -> None:
    path.write_text(
        json.dumps(
            {
                "provider": provider,
                "access_token": "access-123",
                "refresh_token": refresh_token,
                "token_type": "Bearer",
                "expires_at": expires_at,
            }
        ),
        encoding="utf-8",
    )


def test_supported_providers_are_stable(tmp_path: Path) -> None:
    service = _service(tmp_path)
    assert service.supported_providers() == ("dropbox", "google-drive", "onedrive")


def test_start_authorization_persists_dropbox_state(tmp_path: Path) -> None:
    service = _service(tmp_path)
    started = service.start_authorization("dropbox")
    state = json.loads((tmp_path / "oauth-state.json").read_text(encoding="utf-8"))
    assert started.provider == "dropbox"
    assert started.session_id == state["session_id"]
    assert "dropbox.com/oauth2/authorize" in started.authorization_url
    assert "code_challenge=" in started.authorization_url
    assert "token_access_type=offline" in started.authorization_url
    assert len(state["state_token"]) >= 40


def test_start_authorization_includes_redirect_for_google(tmp_path: Path) -> None:
    service = _service(tmp_path)
    started = service.start_authorization("google-drive")
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A53682%2F" in started.authorization_url
    assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive" in started.authorization_url


def test_start_authorization_rejects_unknown_provider(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(OAuthError, match="Unsupported OAuth provider"):
        service.start_authorization("box")


def test_import_rclone_token_persists_raw_json(tmp_path: Path) -> None:
    service = _service(tmp_path)
    token = json.dumps(
        {
            "access_token": "abc123",
            "token_type": "Bearer",
            "refresh_token": "refresh-xyz",
            "expiry": "2030-01-01T00:00:00Z",
        }
    )
    creds = service.import_rclone_token("onedrive", token)
    assert creds.provider == "onedrive"
    assert creds.access_token == "abc123"
    assert creds.refresh_token == "refresh-xyz"
    on_disk = json.loads((tmp_path / "credentials.json").read_text(encoding="utf-8"))
    assert on_disk["provider"] == "onedrive"
    assert on_disk["access_token"] == "abc123"


def test_import_rclone_token_strips_paste_markers(tmp_path: Path) -> None:
    service = _service(tmp_path)
    pasted = (
        '---> {"access_token":"tok","token_type":"Bearer",'
        '"refresh_token":"r","expiry":"2030-06-01T12:00:00Z"} <---End paste'
    )
    creds = service.import_rclone_token("google-drive", pasted)
    assert creds.access_token == "tok"
    assert creds.provider == "google-drive"


def test_import_rclone_token_without_expiry_uses_sentinel(tmp_path: Path) -> None:
    service = _service(tmp_path)
    token = json.dumps({"access_token": "t", "token_type": "Bearer"})
    creds = service.import_rclone_token("dropbox", token)
    assert creds.access_token == "t"
    # Sentinel should be a far-future date so refresh logic treats it as valid.
    assert creds.expires_at.startswith("20")


def test_import_rclone_token_rejects_empty_input(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(OAuthError, match="Empty rclone token"):
        service.import_rclone_token("onedrive", "")


def test_import_rclone_token_rejects_missing_access_token(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(OAuthError, match="missing access_token"):
        service.import_rclone_token("onedrive", json.dumps({"token_type": "Bearer"}))


def test_import_rclone_token_rejects_malformed_json(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(OAuthError, match="Could not parse"):
        service.import_rclone_token("onedrive", "not-json")


def test_import_rclone_token_rejects_unknown_provider(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(OAuthError):
        service.import_rclone_token("box", json.dumps({"access_token": "a"}))


def test_get_pending_authorization_round_trips_from_disk(tmp_path: Path) -> None:
    service = _service(tmp_path)
    started = service.start_authorization("onedrive")
    reloaded = _service(tmp_path).get_pending_authorization(started.session_id)
    assert reloaded == started


def test_get_pending_authorization_expires_and_cleans_state(tmp_path: Path) -> None:
    state_path = tmp_path / "oauth-state.json"
    state_path.write_text(
        json.dumps(
            {
                "session_id": "sid",
                "provider": "dropbox",
                "authorization_url": "https://example.invalid/auth",
                "code_verifier": "verifier",
                "state_token": "state",
                "redirect_uri": "",
                "expires_at": "2000-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    service = _service(tmp_path)
    assert service.get_pending_authorization() is None
    assert not state_path.exists()


def test_cancel_authorization_clears_matching_session(tmp_path: Path) -> None:
    service = _service(tmp_path)
    started = service.start_authorization("dropbox")
    assert service.cancel_authorization(started.session_id) is True
    assert service.get_pending_authorization() is None


def test_cancel_authorization_rejects_wrong_session(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.start_authorization("dropbox")
    assert service.cancel_authorization("wrong") is False


def test_exchange_code_accepts_redirect_url_and_stores_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    started = service.start_authorization("dropbox")
    _patch_open_url(
        monkeypatch,
        payload={
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "token_type": "bearer",
            "expires_in": 3600,
        },
    )
    credentials = service.exchange_code(
        started.session_id,
        "https://localhost/callback?code=abc123&state="
        + json.loads((tmp_path / "oauth-state.json").read_text(encoding="utf-8"))["state_token"],
    )
    assert credentials.provider == "dropbox"
    assert credentials.access_token == "new-access"
    assert service.load_credentials() == credentials
    assert not (tmp_path / "oauth-state.json").exists()


def test_exchange_code_rejects_state_mismatch(tmp_path: Path) -> None:
    service = _service(tmp_path)
    started = service.start_authorization("onedrive")
    with pytest.raises(OAuthError, match="state mismatch"):
        service.exchange_code(started.session_id, "https://localhost/callback?code=abc&state=bad")


def test_exchange_code_rejects_wrong_session(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.start_authorization("onedrive")
    with pytest.raises(OAuthError, match="session not found"):
        service.exchange_code("wrong", "code")


def test_exchange_code_raises_on_http_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    started = service.start_authorization("google-drive")
    error = HTTPError(
        url="https://example.invalid/token",
        code=400,
        msg="bad",
        hdrs=None,
        fp=None,
    )
    _patch_open_url(monkeypatch, error=error)
    with pytest.raises(OAuthError, match="HTTP 400"):
        service.exchange_code(started.session_id, "code")


def test_load_credentials_returns_none_when_absent(tmp_path: Path) -> None:
    assert _service(tmp_path).load_credentials() is None


def test_load_credentials_validates_provider(tmp_path: Path) -> None:
    (tmp_path / "credentials.json").write_text(
        json.dumps(
            {
                "provider": "box",
                "access_token": "access",
                "refresh_token": "refresh",
                "token_type": "Bearer",
                "expires_at": "2099-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(OAuthError, match="Unsupported OAuth provider"):
        _service(tmp_path).load_credentials()


def test_load_credentials_rejects_invalid_json(tmp_path: Path) -> None:
    (tmp_path / "credentials.json").write_text("{not-json}", encoding="utf-8")
    with pytest.raises(OAuthError, match="Failed to parse"):
        _service(tmp_path).load_credentials()


def test_refresh_if_needed_returns_message_when_no_credentials(tmp_path: Path) -> None:
    result = _service(tmp_path).refresh_if_needed()
    assert result.refreshed is False
    assert result.credentials is None
    assert "No stored" in result.message


def test_refresh_if_needed_skips_when_token_is_not_near_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_credentials(tmp_path / "credentials.json")

    def _unexpected_open_url(*_args: object, **_kwargs: object) -> _StubResponse:
        pytest.fail("refresh should not be called")

    monkeypatch.setattr(
        "teslausb_web.services.cloud_oauth_service._open_url",
        _unexpected_open_url,
    )
    result = _service(tmp_path).refresh_if_needed()
    assert result.refreshed is False
    assert result.credentials is not None
    assert result.credentials.access_token == "access-123"


def test_refresh_if_needed_refreshes_when_inside_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_credentials(tmp_path / "credentials.json", expires_at="2000-01-01T00:00:00Z")
    _patch_open_url(
        monkeypatch,
        payload={
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
            "token_type": "Bearer",
            "expires_in": 1800,
        },
    )
    result = _service(tmp_path).refresh_if_needed()
    assert result.refreshed is True
    assert result.credentials is not None
    assert result.credentials.access_token == "fresh-access"


def test_refresh_if_needed_force_refreshes_even_when_not_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_credentials(tmp_path / "credentials.json")
    _patch_open_url(
        monkeypatch,
        payload={
            "access_token": "forced-access",
            "refresh_token": "forced-refresh",
            "token_type": "Bearer",
            "expires_in": 900,
        },
    )
    result = _service(tmp_path).refresh_if_needed(force=True)
    assert result.refreshed is True
    assert result.credentials is not None
    assert result.credentials.access_token == "forced-access"


def test_refresh_if_needed_preserves_previous_refresh_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_credentials(tmp_path / "credentials.json", expires_at="2000-01-01T00:00:00Z")
    _patch_open_url(
        monkeypatch,
        payload={
            "access_token": "fresh-access",
            "token_type": "Bearer",
            "expires_in": 1800,
        },
    )
    result = _service(tmp_path).refresh_if_needed()
    assert result.credentials is not None
    assert result.credentials.refresh_token == "refresh-123"


def test_refresh_if_needed_rejects_provider_mismatch(tmp_path: Path) -> None:
    _write_credentials(tmp_path / "credentials.json", provider="dropbox")
    with pytest.raises(TokenRefreshError, match="dropbox"):
        _service(tmp_path).refresh_if_needed(provider="google-drive")


def test_refresh_if_needed_rejects_missing_refresh_token(tmp_path: Path) -> None:
    _write_credentials(
        tmp_path / "credentials.json",
        expires_at="2000-01-01T00:00:00Z",
        refresh_token="",
    )
    with pytest.raises(TokenRefreshError, match="refresh_token"):
        _service(tmp_path).refresh_if_needed()


def test_refresh_if_needed_wraps_http_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_credentials(tmp_path / "credentials.json", expires_at="2000-01-01T00:00:00Z")
    error = HTTPError(
        url="https://example.invalid/token",
        code=500,
        msg="bad",
        hdrs=None,
        fp=None,
    )
    _patch_open_url(monkeypatch, error=error)
    with pytest.raises(TokenRefreshError, match="HTTP 500"):
        _service(tmp_path).refresh_if_needed()


def test_disconnect_without_credentials_is_idempotent(tmp_path: Path) -> None:
    result = _service(tmp_path).disconnect()
    assert result.disconnected is True
    assert result.revoked is False


def test_disconnect_revokes_google_and_removes_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_credentials(tmp_path / "credentials.json")
    (tmp_path / "oauth-state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "rclone").write_text("[teslausb]\ntype = drive\n", encoding="utf-8")
    _patch_open_url(monkeypatch, payload={})
    result = _service(tmp_path).disconnect(provider="google-drive")
    assert result.disconnected is True
    assert result.revoked is True
    assert not (tmp_path / "credentials.json").exists()
    assert not (tmp_path / "oauth-state.json").exists()
    assert not (tmp_path / "rclone").exists()


def test_disconnect_without_revoke_endpoint_removes_local_files(tmp_path: Path) -> None:
    _write_credentials(tmp_path / "credentials.json", provider="onedrive")
    result = _service(tmp_path).disconnect()
    assert result.disconnected is True
    assert result.revoked is False
    assert not (tmp_path / "credentials.json").exists()


def test_disconnect_keeps_local_cleanup_on_revoke_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_credentials(tmp_path / "credentials.json")
    error = HTTPError(
        url="https://example.invalid/revoke",
        code=400,
        msg="bad",
        hdrs=None,
        fp=None,
    )
    _patch_open_url(monkeypatch, error=error)
    result = _service(tmp_path).disconnect()
    assert result.disconnected is True
    assert result.revoked is False
    assert not (tmp_path / "credentials.json").exists()


def test_posix_credentials_are_written_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX permission bits are not meaningful on Windows")
    service = _service(tmp_path)
    started = service.start_authorization("dropbox")
    _patch_open_url(
        monkeypatch,
        payload={
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "token_type": "bearer",
            "expires_in": 3600,
        },
    )
    service.exchange_code(started.session_id, "code")
    mode = (tmp_path / "credentials.json").stat().st_mode & 0o777
    assert mode == 0o600


def test_oauth_config_rejects_relative_paths(tmp_path: Path) -> None:
    with pytest.raises(OAuthConfigError, match="credentials_path"):
        OAuthConfig(
            credentials_path=Path("credentials.json"),
            oauth_state_path=tmp_path / "oauth-state.json",
            rclone_config_path=tmp_path / "rclone",
        )


def test_make_oauth_service_uses_cloud_section(tmp_path: Path) -> None:
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32),
        paths=PathsSection(state_dir=tmp_path),
        cloud=CloudSection(
            credentials_path=tmp_path / "creds.json",
            oauth_state_path=tmp_path / "state.json",
            rclone_config_path=tmp_path / "rclone-config",
            refresh_window_seconds=123,
        ),
    )
    service = make_oauth_service(cfg)
    assert service.rclone_config_path == tmp_path / "rclone-config"


def test_load_config_parses_cloud_section(tmp_path: Path) -> None:
    cfg_file = tmp_path / "web.toml"
    cfg_file.write_text(
        """
[cloud]
credentials_path = "/var/lib/teslausb/custom-creds.json"
oauth_state_path = "/var/lib/teslausb/custom-state.json"
rclone_config_path = "/var/lib/teslausb/custom-rclone"
refresh_window_seconds = 42
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.cloud.credentials_path == Path("/var/lib/teslausb/custom-creds.json")
    assert cfg.cloud.oauth_state_path == Path("/var/lib/teslausb/custom-state.json")
    assert cfg.cloud.rclone_config_path == Path("/var/lib/teslausb/custom-rclone")
    assert cfg.cloud.refresh_window_seconds == 42


def test_create_app_registers_cloud_oauth_service(tmp_path: Path) -> None:
    app = create_app(
        WebConfig(
            web=WebSection(secret_key="x" * 32),
            paths=PathsSection(state_dir=tmp_path),
            cloud=CloudSection(
                credentials_path=tmp_path / "creds.json",
                oauth_state_path=tmp_path / "state.json",
                rclone_config_path=tmp_path / "rclone-config",
            ),
        )
    )
    from teslausb_web.services.cloud_oauth_service import CloudOAuthService as ServiceType

    assert isinstance(app.extensions["cloud_oauth_service"], ServiceType)
