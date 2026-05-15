"""Tests for generic rclone remote support — issue #165.

Covers:

* :func:`services.cloud_rclone_service.parse_rclone_config_block` —
  paste-form parser including the allow-list and multi-section
  rejection.
* :func:`services.cloud_rclone_service.save_credentials_generic` —
  round-trip through the encrypted store with private metadata
  preserved and ``rclone obscure`` invoked for the listed fields.
* :func:`services.cloud_archive_service._write_rclone_conf` —
  regression: the new ``creds["type"]``-wins rule and the
  ``_*``-key skip rule MUST hold across both the OAuth and generic
  flows.
* :func:`blueprints.cloud_archive.api_connect_provider` — three
  payload shapes are honoured and the legacy OAuth path is unbroken.
"""

import json
import os
import sys
import pytest

# Make sure the web app modules are importable.
_WEB_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'web')
if _WEB_DIR not in sys.path:
    sys.path.insert(0, _WEB_DIR)

from services import cloud_rclone_service as svc  # noqa: E402


# ---------------------------------------------------------------------------
# parse_rclone_config_block
# ---------------------------------------------------------------------------

class TestParseRcloneConfigBlock:
    """Tests for the pasted-rclone.conf-block parser."""

    def test_parses_section_form(self):
        text = """
        [my-nas]
        type = sftp
        host = nas.local
        user = pi
        pass = obscured-blob
        """
        out = svc.parse_rclone_config_block(text)
        assert out["type"] == "sftp"
        assert out["host"] == "nas.local"
        assert out["user"] == "pi"
        assert out["pass"] == "obscured-blob"

    def test_parses_bare_keyvalue_form(self):
        text = "type=webdav\nurl=https://dav.example.com\nuser=alice\n"
        out = svc.parse_rclone_config_block(text)
        assert out == {"type": "webdav", "url": "https://dav.example.com",
                       "user": "alice"}

    def test_drops_section_name(self):
        """Section name is irrelevant — caller pins the remote name."""
        text = "[anything-the-user-typed]\ntype = b2\naccount = abc\nkey = def\n"
        out = svc.parse_rclone_config_block(text)
        assert "anything-the-user-typed" not in out
        assert out["type"] == "b2"

    def test_lowercases_keys(self):
        out = svc.parse_rclone_config_block("Type = sftp\nHost = X\n")
        assert "type" in out and "host" in out
        # Values are NOT case-folded.
        assert out["host"] == "X"

    def test_strips_whitespace_around_value(self):
        out = svc.parse_rclone_config_block("type =   sftp   \nhost = X  \n")
        assert out["type"] == "sftp"
        assert out["host"] == "X"

    def test_ignores_comments_and_blanks(self):
        text = """
        # this is a comment
        ; semicolon comment
        type = sftp
        
        host = nas.local
        """
        out = svc.parse_rclone_config_block(text)
        assert out == {"type": "sftp", "host": "nas.local"}

    def test_rejects_missing_type(self):
        with pytest.raises(ValueError, match="missing required 'type'"):
            svc.parse_rclone_config_block("host = nas.local\nuser = pi\n")

    def test_rejects_unknown_type(self):
        with pytest.raises(ValueError, match="not in the supported"):
            svc.parse_rclone_config_block("type = onedrive\nhost = X\n")

    def test_rejects_crypt_wrap(self):
        with pytest.raises(ValueError, match="not in the supported"):
            svc.parse_rclone_config_block(
                "[wrap]\ntype = crypt\nremote = backend:foo\n"
            )

    def test_rejects_local_backend(self):
        """``local`` would let an attacker write to arbitrary paths."""
        with pytest.raises(ValueError, match="not in the supported"):
            svc.parse_rclone_config_block("type = local\n")

    def test_rejects_multi_section(self):
        with pytest.raises(ValueError, match="at most one"):
            svc.parse_rclone_config_block(
                "[a]\ntype = sftp\nhost = x\n[b]\ntype = sftp\n"
            )

    def test_rejects_invalid_line(self):
        with pytest.raises(ValueError, match="invalid rclone config line"):
            svc.parse_rclone_config_block("type = sftp\ngarbage no equals\n")

    def test_rejects_empty_key(self):
        with pytest.raises(ValueError, match="invalid rclone config line"):
            svc.parse_rclone_config_block("type = sftp\n = bad\n")

    def test_rejects_non_string(self):
        with pytest.raises(ValueError, match="must be a string"):
            svc.parse_rclone_config_block(b"type = sftp\n")  # type: ignore[arg-type]

    def test_accepts_all_allowlisted_types(self):
        for t in svc._GENERIC_RCLONE_TYPES:
            out = svc.parse_rclone_config_block(f"type = {t}\n")
            assert out["type"] == t


# ---------------------------------------------------------------------------
# save_credentials_generic — round-trip through encrypted store
# ---------------------------------------------------------------------------

class TestSaveCredentialsGeneric:
    """Verify generic creds round-trip and obscure() is invoked."""

    @pytest.fixture
    def tmp_creds(self, tmp_path, monkeypatch):
        """Redirect the creds path to a tmpfs-style temp file."""
        path = str(tmp_path / "cloud_provider_creds.bin")
        monkeypatch.setattr(svc, "CLOUD_PROVIDER_CREDS_PATH", path)
        yield path

    def test_round_trip_form_no_obscure(self, tmp_creds):
        """B2-style: secret stored verbatim (no obscure)."""
        svc.save_credentials_generic(
            "b2",
            {"account": "abc", "key": "secret-key"},
            obscure_keys=[],
            source="form",
        )
        creds = svc._load_creds()
        assert creds["type"] == "b2"
        assert creds["account"] == "abc"
        assert creds["key"] == "secret-key"
        # Metadata preserved.
        assert creds["_obscure_keys"] == ""
        assert creds["_source"] == "form"

    def test_round_trip_paste_with_obscure(self, tmp_creds, monkeypatch):
        """SFTP-style: pass goes through rclone obscure (mocked)."""
        calls = []

        def fake_obscure(plaintext):
            calls.append(plaintext)
            return f"OBS({plaintext})"

        monkeypatch.setattr(svc, "_rclone_obscure", fake_obscure)
        svc.save_credentials_generic(
            "sftp",
            {"host": "nas.local", "user": "pi", "pass": "hunter2"},
            obscure_keys=["pass"],
            source="paste",
        )
        creds = svc._load_creds()
        assert creds["pass"] == "OBS(hunter2)"
        assert creds["host"] == "nas.local"
        assert calls == ["hunter2"]
        assert creds["_obscure_keys"] == "pass"
        assert creds["_source"] == "paste"

    def test_rejects_unknown_type(self, tmp_creds):
        with pytest.raises(ValueError, match="not in the supported"):
            svc.save_credentials_generic("crypt", {}, obscure_keys=[])

    def test_rejects_reserved_underscore_key(self, tmp_creds):
        with pytest.raises(ValueError, match="reserved"):
            svc.save_credentials_generic(
                "sftp", {"_evil": "x"}, obscure_keys=[],
            )

    def test_rejects_type_in_fields(self, tmp_creds):
        """``type`` only comes from rclone_type; reject silent override."""
        with pytest.raises(ValueError, match="set from rclone_type"):
            svc.save_credentials_generic(
                "sftp", {"type": "smb"}, obscure_keys=[],
            )

    def test_rejects_empty_field_key(self, tmp_creds):
        with pytest.raises(ValueError, match="non-empty"):
            svc.save_credentials_generic(
                "sftp", {"": "x"}, obscure_keys=[],
            )

    def test_rejects_non_string_key(self, tmp_creds):
        with pytest.raises(ValueError, match="must be strings"):
            svc.save_credentials_generic(
                "sftp", {1: "x"}, obscure_keys=[],  # type: ignore[dict-item]
            )

    def test_rejects_non_dict_fields(self, tmp_creds):
        with pytest.raises(ValueError, match="must be a dict"):
            svc.save_credentials_generic(
                "sftp", "not a dict", obscure_keys=[],  # type: ignore[arg-type]
            )

    def test_none_field_value_becomes_empty_string(self, tmp_creds):
        svc.save_credentials_generic(
            "sftp", {"host": "X", "comment": None}, obscure_keys=[],
        )
        assert svc._load_creds()["comment"] == ""

    def test_obscure_keys_dedup_in_metadata(self, tmp_creds, monkeypatch):
        monkeypatch.setattr(svc, "_rclone_obscure", lambda v: f"O({v})")
        svc.save_credentials_generic(
            "sftp", {"host": "X", "pass": "p"},
            obscure_keys=["pass", "pass", "pass"],
        )
        # Recorded as a sorted, deduped CSV so audit logs stay stable.
        assert svc._load_creds()["_obscure_keys"] == "pass"


# ---------------------------------------------------------------------------
# _rclone_obscure helper
# ---------------------------------------------------------------------------

class TestRcloneObscure:
    def test_empty_skips_subprocess(self, monkeypatch):
        """Empty plaintext returns empty without invoking the binary."""
        called = []
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: called.append(1) or pytest.fail("should not run"),
        )
        assert svc._rclone_obscure("") == ""
        assert called == []

    def test_success_returns_stdout_stripped(self, monkeypatch):
        class R:
            returncode = 0
            stdout = "obscured-value\n"
            stderr = ""

        monkeypatch.setattr("subprocess.run", lambda *a, **k: R())
        assert svc._rclone_obscure("hunter2") == "obscured-value"

    def test_nonzero_returncode_raises(self, monkeypatch):
        class R:
            returncode = 1
            stdout = ""
            stderr = "boom"

        monkeypatch.setattr("subprocess.run", lambda *a, **k: R())
        with pytest.raises(RuntimeError, match="rclone obscure failed"):
            svc._rclone_obscure("hunter2")

    def test_empty_stdout_raises(self, monkeypatch):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr("subprocess.run", lambda *a, **k: R())
        with pytest.raises(RuntimeError, match="empty output"):
            svc._rclone_obscure("hunter2")

    def test_missing_binary_raises(self, monkeypatch):
        def fake(*a, **k):
            raise FileNotFoundError("rclone not found")

        monkeypatch.setattr("subprocess.run", fake)
        with pytest.raises(RuntimeError, match="not found"):
            svc._rclone_obscure("hunter2")

    def test_timeout_raises(self, monkeypatch):
        import subprocess as _sp

        def fake(*a, **k):
            raise _sp.TimeoutExpired("rclone", 10)

        monkeypatch.setattr("subprocess.run", fake)
        with pytest.raises(RuntimeError, match="timed out"):
            svc._rclone_obscure("hunter2")

    def test_non_string_raises(self):
        with pytest.raises(RuntimeError, match="must be a string"):
            svc._rclone_obscure(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _write_rclone_conf regression — issue #165 changes
# ---------------------------------------------------------------------------

class TestWriteRcloneConfRegression:
    """Verify the cloud_archive_service conf writer:

    * uses ``creds["type"]`` over the ``provider`` argument when both
      are present (generic-flow fix);
    * skips ``_*`` private metadata keys (so rclone never sees them);
    * preserves the legacy OAuth flow (``provider="onedrive"`` with
      no ``type`` in creds still emits ``type = onedrive``).
    """

    @pytest.fixture
    def tmpfs(self, tmp_path, monkeypatch):
        from services import cloud_archive_service as cas
        d = str(tmp_path / "rclone-tmp")
        monkeypatch.setattr(cas, "_RCLONE_TMPFS_DIR", d)
        monkeypatch.setattr(
            cas, "_RCLONE_CONF_PATH", os.path.join(d, "rclone.conf"),
        )
        yield d

    def _read(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def test_generic_creds_type_overrides_provider(self, tmpfs):
        """Generic creds with type=sftp + provider="generic"."""
        from services import cloud_archive_service as cas

        path = cas._write_rclone_conf(
            "generic",
            {"type": "sftp", "host": "nas.local", "user": "pi",
             "pass": "obscured", "_obscure_keys": "pass", "_source": "form"},
        )
        contents = self._read(path)
        lines = contents.splitlines()
        assert "type = sftp" in lines
        assert "type = generic" not in lines  # never falls back to provider
        assert "host = nas.local" in lines
        # Private keys MUST be filtered out.
        assert "_obscure_keys" not in contents
        assert "_source" not in contents
        # ``type`` line MUST appear exactly once (header only); count
        # full lines, not substrings (``drive_type = ...`` contains
        # the substring ``type = `` and would inflate a naive count).
        assert sum(1 for ln in lines if ln.startswith("type = ")) == 1

    def test_legacy_oauth_provider_unchanged(self, tmpfs):
        """OAuth creds (have type=onedrive too) still produce the same conf."""
        from services import cloud_archive_service as cas

        path = cas._write_rclone_conf(
            "onedrive",
            {"type": "onedrive", "token": "{...}", "drive_type": "personal"},
        )
        contents = self._read(path)
        lines = contents.splitlines()
        assert "type = onedrive" in lines
        assert "token = {...}" in lines
        assert "drive_type = personal" in lines
        assert sum(1 for ln in lines if ln.startswith("type = ")) == 1

    def test_underscore_keys_always_skipped(self, tmpfs):
        from services import cloud_archive_service as cas

        path = cas._write_rclone_conf(
            "generic",
            {"type": "b2", "account": "a", "key": "k",
             "_internal_metadata": "leaked?"},
        )
        contents = self._read(path)
        assert "_internal_metadata" not in contents
        assert "leaked?" not in contents
        assert "account = a" in contents
        assert "key = k" in contents


class TestWriteTempConfRegression:
    """The cloud_rclone_service test-conn writer also skips ``_*`` keys."""

    @pytest.fixture
    def tmpfs(self, tmp_path, monkeypatch):
        d = str(tmp_path / "rclone-tmp")
        monkeypatch.setattr(svc, "_RCLONE_TMPFS_DIR", d)
        monkeypatch.setattr(
            svc, "_RCLONE_CONF_PATH", os.path.join(d, "rclone.conf"),
        )
        yield d

    def test_skips_underscore_keys(self, tmpfs):
        path = svc._write_temp_conf(
            {"type": "sftp", "host": "X", "_obscure_keys": "pass",
             "_source": "form"},
        )
        with open(path, "r", encoding="utf-8") as f:
            contents = f.read()
        assert "_obscure_keys" not in contents
        assert "_source" not in contents
        assert "type = sftp" in contents
        assert "host = X" in contents


# ---------------------------------------------------------------------------
# api_connect_provider — three payload shapes
# ---------------------------------------------------------------------------

class TestApiConnectProvider:
    """End-to-end via Flask test client; mocks at the credential layer."""

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        # Redirect the creds file to a tmp path.
        monkeypatch.setattr(
            svc, "CLOUD_PROVIDER_CREDS_PATH",
            str(tmp_path / "creds.bin"),
        )
        # Stop the route's _update_config_yaml from touching the real file.
        from blueprints import cloud_archive as bp
        monkeypatch.setattr(bp, "_update_config_yaml", lambda kv: None)

        from web_control import app
        app.config['TESTING'] = True
        return app.test_client()

    def test_oauth_shape_still_works(self, client, monkeypatch):
        monkeypatch.setattr(svc, "save_credentials",
                            lambda provider, token: None)
        token = {
            "access_token": "ya29.x", "token_type": "Bearer",
            "refresh_token": "r", "expiry": "2026-04-05T12:00:00Z",
        }
        r = client.post(
            "/cloud/api/connect",
            json={"provider": "onedrive", "token": json.dumps(token)},
        )
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.get_json()["success"] is True

    def test_oauth_shape_missing_token(self, client):
        r = client.post(
            "/cloud/api/connect",
            json={"provider": "onedrive"},
        )
        assert r.status_code == 400
        assert "Missing token" in r.get_json()["message"]

    def test_unknown_provider_rejected(self, client):
        r = client.post(
            "/cloud/api/connect",
            json={"provider": "msbox", "token": "x"},
        )
        assert r.status_code == 400
        assert "Unknown provider" in r.get_json()["message"]

    def test_generic_paste_shape(self, client, monkeypatch):
        monkeypatch.setattr(svc, "_rclone_obscure", lambda v: f"O({v})")
        r = client.post(
            "/cloud/api/connect",
            json={
                "provider": "generic",
                "config_block": (
                    "[my-nas]\ntype = sftp\nhost = nas.local\n"
                    "user = pi\npass = hunter2\n"
                ),
            },
        )
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.get_json()["success"] is True
        creds = svc._load_creds()
        assert creds["type"] == "sftp"
        assert creds["host"] == "nas.local"
        # Default obscure list for sftp = ["pass"].
        assert creds["pass"] == "O(hunter2)"
        assert creds["_source"] == "paste"

    def test_generic_form_shape(self, client, monkeypatch):
        monkeypatch.setattr(svc, "_rclone_obscure", lambda v: f"O({v})")
        r = client.post(
            "/cloud/api/connect",
            json={
                "provider": "generic",
                "rclone_type": "webdav",
                "fields": {
                    "url": "https://dav.example.com",
                    "user": "alice", "pass": "p",
                },
            },
        )
        assert r.status_code == 200, r.get_data(as_text=True)
        creds = svc._load_creds()
        assert creds["type"] == "webdav"
        assert creds["url"] == "https://dav.example.com"
        assert creds["pass"] == "O(p)"
        assert creds["_source"] == "form"

    def test_generic_form_explicit_obscure_override(self, client, monkeypatch):
        """Caller can override the per-backend default obscure_keys."""
        monkeypatch.setattr(svc, "_rclone_obscure", lambda v: f"O({v})")
        r = client.post(
            "/cloud/api/connect",
            json={
                "provider": "generic", "rclone_type": "sftp",
                "fields": {"host": "X", "key_file": "/k", "pass": "p"},
                "obscure_keys": [],  # disable obscuring entirely
            },
        )
        assert r.status_code == 200
        creds = svc._load_creds()
        assert creds["pass"] == "p"  # NOT obscured

    def test_generic_s3_default_no_obscure(self, client, monkeypatch):
        """S3 backend: default obscure list is empty (rclone convention)."""
        called = []
        monkeypatch.setattr(
            svc, "_rclone_obscure",
            lambda v: called.append(v) or f"O({v})",
        )
        r = client.post(
            "/cloud/api/connect",
            json={
                "provider": "generic", "rclone_type": "s3",
                "fields": {
                    "provider": "Other",
                    "access_key_id": "AK",
                    "secret_access_key": "SK",
                    "endpoint": "https://s3.example.com",
                },
            },
        )
        assert r.status_code == 200
        creds = svc._load_creds()
        assert creds["secret_access_key"] == "SK"  # cleartext (rclone norm)
        assert called == []  # obscure was never called

    def test_generic_missing_both_shapes(self, client):
        r = client.post(
            "/cloud/api/connect",
            json={"provider": "generic"},
        )
        assert r.status_code == 400
        assert "config_block" in r.get_json()["message"]

    def test_generic_bad_type_returns_400(self, client):
        r = client.post(
            "/cloud/api/connect",
            json={
                "provider": "generic",
                "config_block": "type = onedrive\nhost = X\n",
            },
        )
        assert r.status_code == 400
        assert "not in the supported" in r.get_json()["message"]

    def test_generic_form_obscure_failure_returns_500(self, client, monkeypatch):
        def boom(_v):
            raise RuntimeError("rclone binary not found")

        monkeypatch.setattr(svc, "_rclone_obscure", boom)
        r = client.post(
            "/cloud/api/connect",
            json={
                "provider": "generic", "rclone_type": "sftp",
                "fields": {"host": "X", "pass": "p"},
            },
        )
        assert r.status_code == 500
        assert "not found" in r.get_json()["message"]
