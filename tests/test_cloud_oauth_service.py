"""Tests for cloud_oauth_service — tiered OAuth authentication."""

import json
import threading
import time
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError
from io import BytesIO

import pytest

from services import cloud_oauth_service as svc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_auth_session():
    """Reset global auth session between tests."""
    with svc._auth_lock:
        svc._auth_session = None
    yield
    with svc._auth_lock:
        svc._cancel_existing_session()


# ---------------------------------------------------------------------------
# Tier 1 — Device Code Flow
# ---------------------------------------------------------------------------

class TestDeviceCodeStart:
    """Tests for device_code_start()."""

    def test_onedrive_success(self):
        """OneDrive device code start returns expected fields."""
        mock_resp = json.dumps({
            "device_code": "DEVICE123",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://microsoft.com/devicelogin",
            "interval": 5,
            "expires_in": 900,
        }).encode()

        with patch("services.cloud_oauth_service.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = lambda s: MagicMock(
                read=lambda: mock_resp
            )
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

            result = svc.device_code_start("onedrive")

        assert result["method"] == "device_code"
        assert result["user_code"] == "ABCD-EFGH"
        assert result["verification_uri"] == "https://microsoft.com/devicelogin"
        assert result["session_id"]
        assert result["interval"] == 5

    def test_unsupported_provider_raises(self):
        """Providers without device code endpoints raise DeviceCodeUnsupported."""
        with pytest.raises(svc.DeviceCodeUnsupported):
            svc.device_code_start("dropbox")

    def test_http_error_raises_unsupported(self):
        """HTTP error from provider triggers DeviceCodeUnsupported."""
        err_resp = BytesIO(b'{"error": "unauthorized_client"}')
        http_err = HTTPError(
            url="https://example.com", code=400, msg="Bad Request",
            hdrs={}, fp=err_resp,
        )

        with patch("services.cloud_oauth_service.urlopen", side_effect=http_err):
            with pytest.raises(svc.DeviceCodeUnsupported, match="rejected"):
                svc.device_code_start("onedrive")


class TestDeviceCodePoll:
    """Tests for device_code_poll()."""

    def _setup_session(self):
        """Create a mock device code session."""
        sid = "test-session-123"
        svc._auth_session = {
            "session_id": sid,
            "method": "device_code",
            "provider": "onedrive",
            "device_code": "DEVICE123",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://microsoft.com/devicelogin",
            "interval": 5,
            "expires_at": time.time() + 900,
            "token": None,
            "error": None,
            "state": "pending",
        }
        return sid

    def test_poll_pending(self):
        """Pending poll returns authorization_pending."""
        sid = self._setup_session()
        err_resp = BytesIO(b'{"error": "authorization_pending"}')
        http_err = HTTPError(
            url="https://example.com", code=400, msg="Bad Request",
            hdrs={}, fp=err_resp,
        )

        with patch("services.cloud_oauth_service.urlopen", side_effect=http_err):
            result = svc.device_code_poll(sid)
        assert result["state"] == "pending"

    def test_poll_success(self):
        """Successful poll returns completed with token."""
        sid = self._setup_session()
        mock_resp = json.dumps({
            "access_token": "acc123",
            "token_type": "Bearer",
            "refresh_token": "ref456",
            "expires_in": 3600,
        }).encode()

        with patch("services.cloud_oauth_service.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = lambda s: MagicMock(
                read=lambda: mock_resp
            )
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

            result = svc.device_code_poll(sid)

        assert result["state"] == "completed"
        assert result["token"]["access_token"] == "acc123"
        assert result["token"]["refresh_token"] == "ref456"

    def test_poll_expired(self):
        """Expired device code returns expired state."""
        sid = self._setup_session()
        err_resp = BytesIO(b'{"error": "expired_token"}')
        http_err = HTTPError(
            url="https://example.com", code=400, msg="Bad Request",
            hdrs={}, fp=err_resp,
        )

        with patch("services.cloud_oauth_service.urlopen", side_effect=http_err):
            result = svc.device_code_poll(sid)
        assert result["state"] == "expired"

    def test_poll_slow_down(self):
        """slow_down response increases interval."""
        sid = self._setup_session()
        original_interval = svc._auth_session["interval"]
        err_resp = BytesIO(b'{"error": "slow_down"}')
        http_err = HTTPError(
            url="https://example.com", code=400, msg="Bad Request",
            hdrs={}, fp=err_resp,
        )

        with patch("services.cloud_oauth_service.urlopen", side_effect=http_err):
            result = svc.device_code_poll(sid)
        assert result["state"] == "pending"
        assert svc._auth_session["interval"] > original_interval

    def test_poll_wrong_session(self):
        """Polling with wrong session_id returns error."""
        self._setup_session()
        result = svc.device_code_poll("wrong-session")
        assert result["state"] == "error"

    def test_poll_timeout(self):
        """Expired session returns expired."""
        sid = self._setup_session()
        svc._auth_session["expires_at"] = time.time() - 1  # Already expired

        result = svc.device_code_poll(sid)
        assert result["state"] == "expired"


# ---------------------------------------------------------------------------
# Tier 2 — PKCE Authorization Code Flow (all providers)
# ---------------------------------------------------------------------------

class TestPKCEPairGeneration:
    """Tests for PKCE code_verifier / code_challenge generation."""

    def test_generates_valid_pair(self):
        """Verifier and challenge have expected format."""
        verifier, challenge = svc._generate_pkce_pair()
        assert len(verifier) >= 43
        assert len(challenge) > 0
        # Challenge should be base64url-encoded (no padding)
        assert "=" not in challenge
        assert "+" not in challenge
        assert "/" not in challenge

    def test_pairs_are_unique(self):
        """Each call produces a different pair."""
        v1, c1 = svc._generate_pkce_pair()
        v2, c2 = svc._generate_pkce_pair()
        assert v1 != v2
        assert c1 != c2

    def test_challenge_derives_from_verifier(self):
        """Challenge is SHA256 of verifier, base64url-encoded."""
        import hashlib
        import base64
        verifier, challenge = svc._generate_pkce_pair()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        assert challenge == expected


class TestDropboxPKCEStart:
    """Tests for dropbox_pkce_start()."""

    def test_returns_auth_url_and_session(self):
        """Start returns auth_url with PKCE params and session_id."""
        result = svc.dropbox_pkce_start()
        assert result["method"] == "pkce"
        assert result["session_id"]
        assert "dropbox.com/oauth2/authorize" in result["auth_url"]
        assert "code_challenge=" in result["auth_url"]
        assert "code_challenge_method=S256" in result["auth_url"]

    def test_session_stored(self):
        """Session is stored in global state."""
        result = svc.dropbox_pkce_start()
        assert svc._auth_session is not None
        assert svc._auth_session["session_id"] == result["session_id"]
        assert svc._auth_session["code_verifier"]


class TestDropboxPKCEExchange:
    """Tests for dropbox_pkce_exchange()."""

    def test_exchange_success(self):
        """Successful code exchange returns completed with token."""
        start = svc.dropbox_pkce_start()
        sid = start["session_id"]

        mock_resp = json.dumps({
            "access_token": "dbx_acc",
            "token_type": "bearer",
            "refresh_token": "dbx_ref",
            "expires_in": 14400,
        }).encode()

        with patch("services.cloud_oauth_service.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = lambda s: MagicMock(
                read=lambda: mock_resp
            )
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

            result = svc.dropbox_pkce_exchange(sid, "auth_code_123")

        assert result["state"] == "completed"
        assert result["token"]["type"] == "dropbox"
        assert "dbx_acc" in result["token"]["token"]

    def test_exchange_wrong_session(self):
        """Exchange with wrong session returns error."""
        svc.dropbox_pkce_start()
        result = svc.dropbox_pkce_exchange("wrong-id", "code")
        assert result["state"] == "error"


class TestPKCEAuthStart:
    """Tests for pkce_auth_start() with different providers."""

    def test_onedrive_pkce_returns_auth_url(self):
        """OneDrive PKCE start returns Microsoft auth URL."""
        result = svc.pkce_auth_start("onedrive")
        assert result["method"] == "pkce"
        assert result["session_id"]
        assert result["provider"] == "onedrive"
        assert "login.microsoftonline.com" in result["auth_url"]
        assert "code_challenge=" in result["auth_url"]
        assert "code_challenge_method=S256" in result["auth_url"]

    def test_google_pkce_returns_auth_url(self):
        """Google Drive PKCE start returns Google auth URL."""
        result = svc.pkce_auth_start("google-drive")
        assert result["method"] == "pkce"
        assert result["provider"] == "google-drive"
        assert "accounts.google.com" in result["auth_url"]
        assert "code_challenge=" in result["auth_url"]

    def test_pkce_session_stored_with_redirect_uri(self):
        """Session stores redirect_uri for token exchange."""
        svc.pkce_auth_start("onedrive")
        assert svc._auth_session is not None
        assert svc._auth_session["redirect_uri"]
        assert "nativeclient" in svc._auth_session["redirect_uri"]

    def test_unsupported_provider_raises(self):
        """Unknown provider raises ValueError."""
        with pytest.raises(ValueError, match="No PKCE endpoints"):
            svc.pkce_auth_start("unknown-provider")


class TestPKCEExchange:
    """Tests for pkce_exchange() — generic code exchange."""

    def test_onedrive_exchange_success(self):
        """Successful OneDrive code exchange returns completed with token."""
        start = svc.pkce_auth_start("onedrive")
        sid = start["session_id"]

        mock_resp = json.dumps({
            "access_token": "od_acc",
            "token_type": "Bearer",
            "refresh_token": "od_ref",
            "expires_in": 3600,
        }).encode()

        with patch("services.cloud_oauth_service.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = lambda s: MagicMock(
                read=lambda: mock_resp
            )
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

            result = svc.pkce_exchange(sid, "auth_code_onedrive")

        assert result["state"] == "completed"
        assert result["token"]["type"] == "onedrive"
        assert "od_acc" in result["token"]["token"]

    def test_exchange_wrong_session(self):
        """Exchange with wrong session returns error."""
        svc.pkce_auth_start("onedrive")
        result = svc.pkce_exchange("wrong-id", "code")
        assert result["state"] == "error"


# ---------------------------------------------------------------------------
# Token Formatting
# ---------------------------------------------------------------------------

class TestFormatRcloneCredentials:
    """Tests for format_rclone_credentials()."""

    def test_google_drive_format(self):
        """Google Drive credentials include client_id, client_secret, scope."""
        token = {"access_token": "acc", "token_type": "Bearer",
                 "refresh_token": "ref", "expiry": "2026-01-01T00:00:00Z"}
        creds = svc._RCLONE_CREDENTIALS["google-drive"]
        result = svc.format_rclone_credentials("google-drive", token, creds)
        assert result["type"] == "drive"
        assert result["scope"] == "drive"
        assert result["client_id"] == creds["client_id"]
        token_parsed = json.loads(result["token"])
        assert token_parsed["access_token"] == "acc"
        assert token_parsed["refresh_token"] == "ref"

    def test_onedrive_format(self):
        """OneDrive credentials include drive_type."""
        token = {"access_token": "acc", "token_type": "Bearer",
                 "refresh_token": "ref", "expiry": "2026-01-01T00:00:00Z"}
        creds = svc._RCLONE_CREDENTIALS["onedrive"]
        result = svc.format_rclone_credentials("onedrive", token, creds)
        assert result["type"] == "onedrive"
        assert result["drive_type"] == "personal"

    def test_dropbox_format(self):
        """Dropbox credentials use app_key as client_id."""
        token = {"access_token": "acc", "token_type": "bearer",
                 "refresh_token": "ref", "expiry": "2026-01-01T00:00:00Z"}
        creds = svc._RCLONE_CREDENTIALS["dropbox"]
        result = svc.format_rclone_credentials("dropbox", token, creds)
        assert result["type"] == "dropbox"
        assert result["client_id"] == creds["app_key"]


# ---------------------------------------------------------------------------
# Token Extraction from rclone stdout
# ---------------------------------------------------------------------------

class TestExtractRcloneToken:
    """Tests for _extract_rclone_token()."""

    def test_extracts_token(self):
        """Extracts JSON from between ---> and <---End paste markers."""
        text = '''Some rclone output
Paste the following into your remote machine --->
{"access_token":"ya29.xxx","token_type":"Bearer","refresh_token":"1//0g","expiry":"2026-04-05T12:00:00Z"}
<---End paste
Done.'''
        result = svc._extract_rclone_token(text)
        assert result is not None
        assert result["access_token"] == "ya29.xxx"
        assert result["refresh_token"] == "1//0g"

    def test_no_markers_returns_none(self):
        """Returns None when markers not found."""
        assert svc._extract_rclone_token("no markers here") is None

    def test_invalid_json_returns_none(self):
        """Returns None when content between markers is not valid JSON."""
        text = "---> not-json <---End paste"
        assert svc._extract_rclone_token(text) is None


# ---------------------------------------------------------------------------
# Session Management
# ---------------------------------------------------------------------------

class TestSessionManagement:
    """Tests for cancel, session info, and concurrent prevention."""

    def test_cancel_clears_session(self):
        """cancel_auth() clears the session."""
        result = svc.dropbox_pkce_start()
        assert svc._auth_session is not None
        ok = svc.cancel_auth(result["session_id"])
        assert ok
        assert svc._auth_session is None

    def test_cancel_wrong_session(self):
        """cancel_auth() with wrong ID returns False."""
        svc.dropbox_pkce_start()
        ok = svc.cancel_auth("wrong-id")
        assert not ok

    def test_new_flow_cancels_previous(self):
        """Starting a new flow cancels any existing one."""
        first = svc.dropbox_pkce_start()
        second = svc.dropbox_pkce_start()
        assert first["session_id"] != second["session_id"]
        assert svc._auth_session["session_id"] == second["session_id"]

    def test_get_session_info(self):
        """get_session_info() returns provider and method."""
        svc.dropbox_pkce_start()
        info = svc.get_session_info()
        assert info["provider"] == "dropbox"
        assert info["method"] == "pkce"

    def test_get_session_info_none(self):
        """get_session_info() returns None when no session."""
        assert svc.get_session_info() is None


# ---------------------------------------------------------------------------
# Auto-Tier Selection
# ---------------------------------------------------------------------------

class TestStartOAuth:
    """Tests for start_oauth() auto-tier selection."""

    def test_onedrive_tries_device_code_first(self):
        """OneDrive uses device code as primary."""
        mock_resp = json.dumps({
            "device_code": "DC", "user_code": "UC",
            "verification_uri": "https://microsoft.com/devicelogin",
            "interval": 5, "expires_in": 900,
        }).encode()

        with patch("services.cloud_oauth_service.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = lambda s: MagicMock(
                read=lambda: mock_resp
            )
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            result = svc.start_oauth("onedrive")

        assert result["method"] == "device_code"

    def test_dropbox_uses_pkce(self):
        """Dropbox falls back to PKCE."""
        result = svc.start_oauth("dropbox")
        assert result["method"] == "pkce"

    def test_google_device_code_fallback_to_pkce(self):
        """Google Drive falls back to PKCE when device code rejected."""
        err_resp = BytesIO(b'{"error": "unauthorized_client"}')
        http_err = HTTPError(
            url="https://example.com", code=400, msg="Bad Request",
            hdrs={}, fp=err_resp,
        )

        with patch("services.cloud_oauth_service.urlopen", side_effect=http_err):
            result = svc.start_oauth("google-drive")

        assert result["method"] == "pkce"
        assert "accounts.google.com" in result["auth_url"]

    def test_invalid_provider_raises(self):
        """Unknown provider raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported"):
            svc.start_oauth("unknown-provider")

    def test_onedrive_device_code_fallback_to_pkce(self):
        """OneDrive falls back to PKCE when device code rejected."""
        err_resp = BytesIO(b'{"error": "invalid_client"}')
        http_err = HTTPError(
            url="https://example.com", code=401, msg="Unauthorized",
            hdrs={}, fp=err_resp,
        )

        with patch("services.cloud_oauth_service.urlopen", side_effect=http_err):
            result = svc.start_oauth("onedrive")

        assert result["method"] == "pkce"
        assert "login.microsoftonline.com" in result["auth_url"]
