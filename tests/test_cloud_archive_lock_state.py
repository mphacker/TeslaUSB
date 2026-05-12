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

These tests pin the contract on four code paths:

1. Initial-acquire failure — ``_run_sync`` must NOT call
   ``release_task`` at all (we never held the lock).
2. Normal completion (no events) — release exactly once, no warnings.
3. Mid-loop exception (creds unavailable) — release exactly once,
   no warnings.
4. Yield-to-LES then failed re-acquire — release happens once during
   the yield; the ``finally`` must NOT call release again (that was
   the original bug).
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


# ---------------------------------------------------------------------------
# 4. Yield-to-LES then failed re-acquire — the actual bug case
# ---------------------------------------------------------------------------

class TestYieldThenFailedReacquireNoSpuriousWarning:
    """The original Phase 2.9 bug: the yield-to-LES path releases the
    lock so LES can run. If a different task (e.g., archiver) grabs the
    lock during the yield window, the post-yield ``acquire_task`` returns
    False and ``_run_sync`` ``break`` s out of the upload loop. Before
    the fix, ``finally`` then called ``release_task('cloud_sync')`` while
    the archiver held the lock — producing the spurious warning.
    """
    def test_failed_reacquire_skips_finally_release(
        self, monkeypatch, caplog, _reset_sync_status, tmp_path
    ):
        # We drive the upload loop through ONE iteration that completes
        # successfully, then triggers the yield-to-LES path. During the
        # yield, an archiver steals the lock so the post-yield
        # ``acquire_task('cloud_sync')`` returns False. The function
        # must ``break`` and NOT release the lock again in ``finally``.
        monkeypatch.setattr(svc, '_init_cloud_tables', _make_in_memory_db)
        monkeypatch.setattr(
            svc, '_discover_events',
            lambda *a, **kw: [
                ("/fake/event_dir_1", "SentryClips/2025-01-01_12-00", 1024),
            ],
        )
        monkeypatch.setattr(
            svc, '_load_provider_creds',
            lambda: {'type': 'fake', 'token': 'x'},
        )
        monkeypatch.setattr(
            svc, '_write_rclone_conf',
            lambda *a, **kw: str(tmp_path / "fake_rclone.conf"),
        )
        monkeypatch.setattr(
            svc, '_remove_rclone_conf', lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            svc, '_reconcile_with_remote', lambda *a, **kw: None,
        )

        # ``rclone about`` and the token-refresh subprocess.run calls
        # must not fail — return a dummy CompletedProcess with an
        # empty JSON about response so cloud_free_bytes stays None
        # (treated as unlimited).
        import subprocess as _sp

        class _FakeCompleted:
            def __init__(self):
                self.returncode = 0
                self.stdout = "{}"
                self.stderr = ""

        monkeypatch.setattr(_sp, 'run', lambda *a, **kw: _FakeCompleted())

        # The shared rclone helper used inside the upload loop returns
        # success for the one event we feed it.
        monkeypatch.setattr(
            svc, 'upload_path_via_rclone',
            lambda *a, **kw: (0, ""),
        )

        # Force the yield: has_ready_live_event_work returns True the
        # first time it's called (in the upload loop) and False after
        # so the wait-loop breaks immediately. Patch on the LES module
        # because _run_sync imports it inline.
        from services import live_event_sync_service as les
        call_count = {'n': 0}

        def _fake_has_ready_live_event_work(_db_path):
            call_count['n'] += 1
            # First call (inside upload loop) → True (force yield)
            # Subsequent calls (inside wait-loop) → False (drained)
            return call_count['n'] == 1

        monkeypatch.setattr(
            les, 'has_ready_live_event_work',
            _fake_has_ready_live_event_work,
        )

        # Speed up the test: skip real sleeps in the yield wait loop
        # and the inter-upload pause.
        import time as _time
        monkeypatch.setattr(_time, 'sleep', lambda *_: None)

        # Spy on release_task and acquire_task. Critically, after the
        # yield releases the lock, we steal it as 'archiver' so the
        # post-yield ``acquire_task('cloud_sync')`` returns False —
        # which is exactly the failure mode that produced the original
        # spurious warning.
        release_calls = []
        acquire_calls = []
        real_release = tc.release_task
        real_acquire = tc.acquire_task

        def _spy_release(name):
            release_calls.append(name)
            real_release(name)
            # After cloud_sync releases during the yield, steal the
            # lock so the next acquire_task('cloud_sync') fails.
            if name == 'cloud_sync' and len(release_calls) == 1:
                # Acquire as archiver to simulate the race.
                assert real_acquire('archiver') is True

        def _spy_acquire(name, *args, **kwargs):
            acquire_calls.append(name)
            return real_acquire(name, *args, **kwargs)

        monkeypatch.setattr(tc, 'release_task', _spy_release)
        monkeypatch.setattr(tc, 'acquire_task', _spy_acquire)

        cancel = threading.Event()
        with caplog.at_level(logging.WARNING):
            svc._run_sync(
                teslacam_path=str(tmp_path),
                db_path=str(tmp_path / "geodata.db"),
                trigger="test",
                cancel_event=cancel,
            )

        # The release MUST have fired exactly once — during the yield.
        # Without the fix, ``finally`` would have fired a second one
        # while 'archiver' held the lock, producing the warning.
        cloud_releases = [n for n in release_calls if n == 'cloud_sync']
        assert len(cloud_releases) == 1, (
            f"Expected exactly 1 cloud_sync release across the whole "
            f"yield-then-fail-reacquire path; got {len(cloud_releases)} "
            f"(all releases: {release_calls!r}). The lock_held flag is "
            f"not preventing the finally from double-releasing."
        )
        # And — most importantly — no spurious warning was emitted.
        spurious = _spurious_release_warnings(caplog.records)
        assert spurious == [], (
            f"Spurious 'tried to release' warning emitted: "
            f"{[r.getMessage() for r in spurious]!r}"
        )
        # The archiver still holds the lock (we never released it).
        assert tc._current_task == 'archiver'
        # Cleanup for the autouse reset.
        real_release('archiver')
