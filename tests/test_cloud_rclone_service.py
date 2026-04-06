"""Tests for cloud_rclone_service — rclone token handling."""

import json
import pytest

from services import cloud_rclone_service as svc


# ---------------------------------------------------------------------------
# Token Parsing
# ---------------------------------------------------------------------------

class TestParseRcloneToken:
    """Tests for parse_rclone_token()."""

    def test_parses_raw_json(self):
        """Parses a raw JSON token object."""
        raw = json.dumps({
            "access_token": "ya29.xxx",
            "token_type": "Bearer",
            "refresh_token": "1//0g",
            "expiry": "2026-04-05T12:00:00Z",
        })
        token = svc.parse_rclone_token(raw)
        assert token["access_token"] == "ya29.xxx"
        assert token["refresh_token"] == "1//0g"

    def test_parses_rclone_paste_markers(self):
        """Extracts token from between rclone ---> and <---End paste markers."""
        raw = '''Some rclone output
Paste the following into your remote machine --->
{"access_token":"ya29.xxx","token_type":"Bearer","refresh_token":"1//0g","expiry":"2026-04-05T12:00:00Z"}
<---End paste
Done.'''
        token = svc.parse_rclone_token(raw)
        assert token["access_token"] == "ya29.xxx"

    def test_rejects_invalid_json(self):
        """Raises ValueError for non-JSON input."""
        with pytest.raises(ValueError, match="Could not parse"):
            svc.parse_rclone_token("not json at all")

    def test_rejects_missing_access_token(self):
        """Raises ValueError when access_token is missing."""
        raw = json.dumps({"token_type": "Bearer", "refresh_token": "ref"})
        with pytest.raises(ValueError, match="missing.*access_token"):
            svc.parse_rclone_token(raw)

    def test_rejects_non_object(self):
        """Raises ValueError when token is not a dict."""
        with pytest.raises(ValueError, match="JSON object"):
            svc.parse_rclone_token('"just a string"')

    def test_handles_whitespace(self):
        """Handles leading/trailing whitespace."""
        raw = '  {"access_token":"abc","token_type":"Bearer"}  '
        token = svc.parse_rclone_token(raw)
        assert token["access_token"] == "abc"

    def test_handles_multiline_paste(self):
        """Handles token pasted with extra newlines."""
        raw = '\n\n{"access_token":"abc","token_type":"Bearer"}\n\n'
        token = svc.parse_rclone_token(raw)
        assert token["access_token"] == "abc"


# ---------------------------------------------------------------------------
# Provider Metadata
# ---------------------------------------------------------------------------

class TestProviders:
    """Tests for provider configuration."""

    def test_all_providers_have_required_fields(self):
        """Each provider has label, rclone_type, and authorize_cmd."""
        for key, meta in svc.PROVIDERS.items():
            assert "label" in meta, f"{key} missing label"
            assert "rclone_type" in meta, f"{key} missing rclone_type"
            assert "authorize_cmd" in meta, f"{key} missing authorize_cmd"
            assert "rclone authorize" in meta["authorize_cmd"]

    def test_onedrive_metadata(self):
        """OneDrive provider metadata is correct."""
        assert svc.PROVIDERS["onedrive"]["rclone_type"] == "onedrive"
        assert 'rclone authorize "onedrive"' == svc.PROVIDERS["onedrive"]["authorize_cmd"]

    def test_google_drive_metadata(self):
        """Google Drive uses 'drive' as rclone type."""
        assert svc.PROVIDERS["google-drive"]["rclone_type"] == "drive"
        assert 'rclone authorize "drive"' == svc.PROVIDERS["google-drive"]["authorize_cmd"]

    def test_dropbox_metadata(self):
        """Dropbox provider metadata is correct."""
        assert svc.PROVIDERS["dropbox"]["rclone_type"] == "dropbox"


# ---------------------------------------------------------------------------
# Connection Status
# ---------------------------------------------------------------------------

class TestGetConnectionStatus:
    """Tests for get_connection_status()."""

    def test_no_provider_configured(self):
        """Returns not connected when no provider is set."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("services.cloud_rclone_service.CLOUD_PROVIDER_CREDS_PATH",
                        "/nonexistent/creds")
            # Patch the config import
            import config
            original = getattr(config, 'CLOUD_ARCHIVE_PROVIDER', '')
            mp.setattr(config, 'CLOUD_ARCHIVE_PROVIDER', '')
            try:
                status = svc.get_connection_status()
                assert not status["connected"]
            finally:
                mp.setattr(config, 'CLOUD_ARCHIVE_PROVIDER', original)
