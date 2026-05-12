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


# ---------------------------------------------------------------------------
# Phase 2.2 (#97) — Manual upload waits for task_coordinator slot
# ---------------------------------------------------------------------------


class TestManualUploadCoordination:
    """Verify _archive_worker uses task_coordinator before any rclone work.

    The contract is documented in cloud_rclone_service._archive_worker
    docstring: the manual-upload worker MUST acquire
    ``'cloud_manual_upload'`` from ``task_coordinator`` (with a 60 s
    blocking wait) before doing any rclone subprocess. If the slot
    cannot be acquired, the worker exits cleanly with a "system busy"
    error in the status dict and never spawns rclone.

    These tests pin that contract so a future refactor can't quietly
    re-introduce the May 12 06:11 race condition (#109).
    """

    @pytest.fixture(autouse=True)
    def _reset_coordinator_and_status(self):
        """Reset task_coordinator + _archive_status between tests."""
        from services import task_coordinator
        with task_coordinator._lock:
            task_coordinator._current_task = None
            task_coordinator._task_started = 0.0
            task_coordinator._waiter_count = 0
        svc._archive_status.update({
            "running": False,
            "event_name": "",
            "folder": "",
            "file_count": 0,
            "files_done": 0,
            "current_file": "",
            "total_size": 0,
            "bytes_done": 0,
            "started_at": None,
            "error": None,
            "completed": False,
        })
        svc._archive_cancel.clear()
        yield
        with task_coordinator._lock:
            task_coordinator._current_task = None
            task_coordinator._task_started = 0.0
            task_coordinator._waiter_count = 0

    def test_worker_blocks_when_other_task_holds_slot(self, monkeypatch):
        """When indexer holds the slot, the manual upload bails after timeout."""
        from services import task_coordinator

        # Pre-acquire the lock as a different task. The manual upload
        # worker should wait for it (we'll use a tiny wait_seconds via
        # monkeypatch so the test runs fast).
        assert task_coordinator.acquire_task('indexer') is True

        # Patch acquire_task wait window to 0.2 s so the test is quick.
        original_acquire = task_coordinator.acquire_task

        def quick_acquire(name, wait_seconds=0.0, **kw):
            if name == 'cloud_manual_upload' and wait_seconds > 0:
                return original_acquire(name, wait_seconds=0.2, **kw)
            return original_acquire(name, wait_seconds=wait_seconds, **kw)

        monkeypatch.setattr(task_coordinator, 'acquire_task', quick_acquire)
        # Re-import path used inside _archive_worker
        monkeypatch.setattr(
            'services.task_coordinator.acquire_task', quick_acquire,
        )

        # Track whether subprocess.run was called — it MUST NOT be.
        subprocess_calls = []

        def fail_subprocess(*args, **kwargs):
            subprocess_calls.append(args)
            raise AssertionError(
                "subprocess.run called even though slot was unavailable!"
            )

        monkeypatch.setattr('subprocess.run', fail_subprocess)

        svc._archive_status['running'] = True  # caller would have set this

        # Run the worker synchronously (no thread).
        svc._archive_worker(
            local_path='/tmp/fake',
            rel_path='SentryClips/2026-05-12_06-00',
            files=['front.mp4'],
            total_size=1000,
            creds={'type': 'onedrive'},
            is_event_dir=True,
        )

        # Worker bailed out: status reflects busy, no rclone fired.
        assert svc._archive_status['running'] is False
        assert 'busy' in (svc._archive_status['error'] or '').lower()
        assert subprocess_calls == []

        # Indexer slot was never released by us — clean up.
        task_coordinator.release_task('indexer')

    def test_worker_acquires_slot_when_idle(self, monkeypatch):
        """When no other task holds the slot, worker acquires & runs."""
        from services import task_coordinator

        # Make the rclone subprocess calls succeed without actually invoking rclone.
        class _FakeResult:
            def __init__(self, returncode=0, stderr='', stdout=''):
                self.returncode = returncode
                self.stderr = stderr
                self.stdout = stdout

        def fake_subprocess(*args, **kwargs):
            return _FakeResult()

        monkeypatch.setattr('subprocess.run', fake_subprocess)
        # Skip actual conf write/remove and token capture
        monkeypatch.setattr(svc, '_write_temp_conf', lambda creds: '/tmp/fake.conf')
        monkeypatch.setattr(svc, '_remove_temp_conf', lambda: None)
        monkeypatch.setattr(svc, '_capture_refreshed_token', lambda creds: None)

        svc._archive_status['running'] = True

        svc._archive_worker(
            local_path='/tmp/fake',
            rel_path='SentryClips/2026-05-12_06-00',
            files=['front.mp4'],
            total_size=1000,
            creds={'type': 'onedrive'},
            is_event_dir=True,
        )

        # Worker completed cleanly.
        assert svc._archive_status['running'] is False
        assert svc._archive_status['error'] is None
        assert svc._archive_status['completed'] is True

        # Coordinator slot was released so other tasks can run.
        assert task_coordinator.is_busy() is False

    def test_worker_releases_slot_on_exception(self, monkeypatch):
        """If rclone raises an unexpected exception, the slot is still released."""
        from services import task_coordinator

        def boom(*args, **kwargs):
            raise RuntimeError("simulated rclone crash")

        monkeypatch.setattr('subprocess.run', boom)
        monkeypatch.setattr(svc, '_write_temp_conf', lambda creds: '/tmp/fake.conf')
        monkeypatch.setattr(svc, '_remove_temp_conf', lambda: None)
        monkeypatch.setattr(svc, '_capture_refreshed_token', lambda creds: None)

        svc._archive_status['running'] = True

        svc._archive_worker(
            local_path='/tmp/fake',
            rel_path='SentryClips/2026-05-12_06-00',
            files=['front.mp4'],
            total_size=1000,
            creds={'type': 'onedrive'},
            is_event_dir=True,
        )

        # Status reflects the failure but slot is released.
        assert svc._archive_status['running'] is False
        # Either the worker captured the error or the finally cleared running.
        assert task_coordinator.is_busy() is False



