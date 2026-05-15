"""Tests for ``cloud_archive_service._run_sync`` lock-state tracking.

Regression coverage for Phase 2.9 (epic #97 item 2.9). Before this fix
``_run_sync`` always called ``release_task('cloud_sync')`` in its
``finally`` block. The yield-to-Live-Event-Sync path inside the upload
loop could leave the function without the lock (when the post-yield
``acquire_task`` lost a race to another task), so the unconditional
release would log a spurious::

    Task 'cloud_sync' tried to release but '<other>' holds the lock

The warning is harmless (``task_coordinator`` handles it gracefully)
but appeared as a yellow flag in the logs and confused anyone reading
them. The fix tracks ``lock_held`` across every acquire/release pair
and only releases when actually held.

These tests pin the contract on three code paths:

1. Initial-acquire failure — ``_run_sync`` must NOT call
   ``release_task`` at all (we never held the lock).
2. Normal completion (no events) — release exactly once, no warnings.
3. Mid-loop exception (creds unavailable) — release exactly once,
   no warnings.

The fourth path (yield-to-LES then failed re-acquire) is gone in
Wave 4 PR-F4 (issue #184): the LES subsystem was deleted and the
inter-file LES yield with it. The corresponding test class was
removed alongside this change.
"""
from __future__ import annotations

import logging
import sqlite3
import threading

import pytest

from services import cloud_archive_service as svc
from services import task_coordinator as tc


@pytest.fixture(autouse=True)
def _reset_coordinator():
    """Each test starts with a clean coordinator state.

    Without this fixture, leakage from one test (e.g., a leaked
    cloud_sync hold) would mask the very bug we're testing.
    """
    with tc._lock:
        tc._current_task = None
        tc._task_started = 0.0
        tc._waiter_count = 0
        tc._skipped_log_last.clear()
        tc._task_stats.clear()
    yield
    with tc._lock:
        tc._current_task = None
        tc._task_started = 0.0
        tc._waiter_count = 0
        tc._skipped_log_last.clear()
        tc._task_stats.clear()


@pytest.fixture
def _reset_sync_status():
    """Reset the module-global ``_sync_status`` dict between tests."""
    snapshot = dict(svc._sync_status)
    yield
    svc._sync_status.clear()
    svc._sync_status.update(snapshot)


def _spurious_release_warnings(records):
    """Filter caplog records for the specific warning we're guarding."""
    return [
        r for r in records
        if r.levelno == logging.WARNING
        and "tried to release" in r.getMessage()
    ]


def _make_in_memory_db(_path):
    """Stub for ``_init_cloud_tables`` returning a usable in-memory DB.

    ``_run_sync`` writes a session row plus per-file rows, so the
    connection needs both ``cloud_sync_sessions`` and
    ``cloud_synced_files`` tables.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE cloud_sync_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            ended_at TEXT,
            trigger TEXT,
            window_mode TEXT,
            files_synced INTEGER DEFAULT 0,
            bytes_transferred INTEGER DEFAULT 0,
            status TEXT,
            error_msg TEXT
        );
        CREATE TABLE cloud_synced_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT UNIQUE,
            file_size INTEGER,
            file_mtime REAL,
            status TEXT,
            synced_at TEXT,
            remote_path TEXT,
            retry_count INTEGER DEFAULT 0,
            last_error TEXT
        );
    """)
    return conn


# ---------------------------------------------------------------------------
# 1. Initial acquire failure — must NOT call release
# ---------------------------------------------------------------------------

class TestAcquireFailureNoRelease:
    def test_run_sync_returns_without_release_when_acquire_fails(
        self, monkeypatch, caplog, _reset_sync_status
    ):
        # Pre-arrange: another task holds the lock.
        assert tc.acquire_task('indexer') is True

        release_calls = []
        real_release = tc.release_task

        def _spy_release(name):
            release_calls.append(name)
            real_release(name)

        monkeypatch.setattr(tc, 'release_task', _spy_release)

        cancel = threading.Event()
        with caplog.at_level(logging.WARNING):
            svc._run_sync(
                teslacam_path="/tmp/_phase29_unused",
                db_path="/tmp/_phase29_unused.db",
                trigger="test",
                cancel_event=cancel,
            )

        # The 'cloud_sync' release must NEVER fire — we never held it.
        assert 'cloud_sync' not in release_calls, (
            "release_task('cloud_sync') was called even though the "
            "initial acquire failed. lock_held tracking is broken."
        )
        # And no spurious-release warning was emitted.
        assert _spurious_release_warnings(caplog.records) == []
        # The other task still holds the lock.
        assert tc._current_task == 'indexer'
        # Cleanup so the autouse fixture's reset doesn't double-warn.
        tc.release_task('indexer')


# ---------------------------------------------------------------------------
# 2. Normal completion (no events) — release exactly once
# ---------------------------------------------------------------------------

class TestNormalCompletionReleasesOnce:
    def test_no_events_releases_lock_once_and_no_warnings(
        self, monkeypatch, caplog, _reset_sync_status
    ):
        # Stub the DB init and event discovery so _run_sync hits the
        # "No events to sync" early-return path.
        # Force the legacy disk-walk discovery path; the pipeline
        # reader path (Wave 4 PR-F3) is the new default but this
        # specific lock-release regression lives on the legacy branch.
        monkeypatch.setattr(svc, '_use_pipeline_reader_enabled', lambda: False)
        monkeypatch.setattr(svc, '_init_cloud_tables', _make_in_memory_db)
        monkeypatch.setattr(
            svc, '_discover_events', lambda *a, **kw: []
        )

        release_calls = []
        real_release = tc.release_task

        def _spy_release(name):
            release_calls.append(name)
            real_release(name)

        monkeypatch.setattr(tc, 'release_task', _spy_release)

        cancel = threading.Event()
        with caplog.at_level(logging.WARNING):
            svc._run_sync(
                teslacam_path="/tmp/_phase29_unused",
                db_path="/tmp/_phase29_unused.db",
                trigger="test",
                cancel_event=cancel,
            )

        # release_task('cloud_sync') called exactly once.
        cloud_releases = [n for n in release_calls if n == 'cloud_sync']
        assert len(cloud_releases) == 1, (
            f"Expected exactly 1 cloud_sync release, got {len(cloud_releases)}: "
            f"{release_calls!r}"
        )
        # No spurious-release warning.
        assert _spurious_release_warnings(caplog.records) == [], (
            "Spurious 'tried to release' warning emitted on the normal "
            "completion path."
        )
        # Lock is fully released.
        assert tc._current_task is None


# ---------------------------------------------------------------------------
# 3. Exception path — release exactly once, no warnings
# ---------------------------------------------------------------------------

class TestExceptionPathReleasesOnce:
    def test_creds_unavailable_raises_then_releases_once(
        self, monkeypatch, caplog, _reset_sync_status
    ):
        # Simulate: discovery returns work but credentials are missing,
        # which throws RuntimeError out of _run_sync. The except block
        # records the failure and the finally block releases.
        # Force the legacy disk-walk discovery path; the pipeline
        # reader path (Wave 4 PR-F3) is the new default but this
        # exception-handling regression lives on the legacy branch.
        monkeypatch.setattr(svc, '_use_pipeline_reader_enabled', lambda: False)
        monkeypatch.setattr(svc, '_init_cloud_tables', _make_in_memory_db)
        monkeypatch.setattr(
            svc, '_discover_events',
            lambda *a, **kw: [("/fake/event/dir", "/fake/event.json", 1024)],
        )
        monkeypatch.setattr(svc, '_load_provider_creds', lambda: {})

        release_calls = []
        real_release = tc.release_task

        def _spy_release(name):
            release_calls.append(name)
            real_release(name)

        monkeypatch.setattr(tc, 'release_task', _spy_release)

        cancel = threading.Event()
        with caplog.at_level(logging.WARNING):
            svc._run_sync(
                teslacam_path="/tmp/_phase29_unused",
                db_path="/tmp/_phase29_unused.db",
                trigger="test",
                cancel_event=cancel,
            )

        cloud_releases = [n for n in release_calls if n == 'cloud_sync']
        assert len(cloud_releases) == 1, (
            f"Expected exactly 1 cloud_sync release on exception path, "
            f"got {len(cloud_releases)}: {release_calls!r}"
        )
        assert _spurious_release_warnings(caplog.records) == [], (
            "Spurious 'tried to release' warning on exception path."
        )
        assert tc._current_task is None
        # The error was captured in _sync_status.
        assert svc._sync_status.get('error') is not None

