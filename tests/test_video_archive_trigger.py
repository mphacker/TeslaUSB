"""Tests for the archive-trigger duplicate-prevention logic in
``services.video_archive_service``.

These guard the ``_archive_pending`` flag introduced after the May
2026 phantom-trips fix: switching ``acquire_task('archive')`` from
non-blocking to ``wait_seconds=60.0`` opened a window where a second
``trigger_archive_now()`` could spawn a duplicate archive thread while
the first was still waiting for the coordinator lock. ``_status['running']``
is set only AFTER lock acquisition, so it cannot guard that window —
``_archive_pending`` must.
"""

import time
import threading

import pytest

from services import video_archive_service as vas
from services import task_coordinator as tc


@pytest.fixture(autouse=True)
def _reset_state():
    """Snapshot and restore module-level state around each test so
    leakage between tests can't mask real regressions."""
    saved_status = dict(vas._status)
    saved_pending = vas._archive_pending
    saved_enabled = vas.ARCHIVE_ENABLED
    # Coordinator state too.
    with tc._lock:
        tc._current_task = None
        tc._task_started = 0.0
        tc._waiter_count = 0
    vas.ARCHIVE_ENABLED = True
    vas._archive_pending = False
    vas._status['running'] = False
    yield
    vas._status.clear()
    vas._status.update(saved_status)
    vas._archive_pending = saved_pending
    vas.ARCHIVE_ENABLED = saved_enabled
    with tc._lock:
        tc._current_task = None
        tc._task_started = 0.0
        tc._waiter_count = 0


class TestTriggerArchiveDuplicateGuard:
    def test_disabled_returns_false(self):
        vas.ARCHIVE_ENABLED = False
        assert vas.trigger_archive_now() is False
        assert vas._archive_pending is False

    def test_first_trigger_starts_thread(self, monkeypatch):
        # Replace _run_archive with a sentinel so we don't actually copy
        # files. We just want to verify trigger_archive_now's guard logic.
        called = threading.Event()

        def fake_run():
            called.set()
            # Hold "pending" for a moment so we can test the guard.
            time.sleep(0.2)
            with vas._archive_lock:
                vas._archive_pending = False

        monkeypatch.setattr(vas, '_run_archive', fake_run)
        assert vas.trigger_archive_now() is True
        assert called.wait(timeout=1.0)

    def test_second_trigger_refused_while_first_is_pending(self, monkeypatch):
        """Critical race: while the first archive is waiting for the
        coordinator lock, ``_status['running']`` is still False. The
        guard must use ``_archive_pending`` to block the second call."""
        gate = threading.Event()

        def fake_run():
            # Simulate the first archive sitting in acquire_task waiting
            # for the lock. Hold _archive_pending until the test releases us.
            gate.wait(timeout=2.0)
            with vas._archive_lock:
                vas._archive_pending = False

        monkeypatch.setattr(vas, '_run_archive', fake_run)
        assert vas.trigger_archive_now() is True
        # Second trigger immediately after must be refused even though
        # _status['running'] is still False.
        assert vas._status.get('running') is not True
        assert vas._archive_pending is True
        assert vas.trigger_archive_now() is False
        # Let the first archive finish so the fixture can clean up.
        gate.set()
        # Give the worker a moment to clear _archive_pending.
        time.sleep(0.1)

    def test_third_trigger_succeeds_after_first_completes(self, monkeypatch):
        gate = threading.Event()
        runs = []

        def fake_run():
            runs.append(time.monotonic())
            gate.wait(timeout=2.0)
            with vas._archive_lock:
                vas._archive_pending = False

        monkeypatch.setattr(vas, '_run_archive', fake_run)
        assert vas.trigger_archive_now() is True
        assert vas.trigger_archive_now() is False
        gate.set()
        # Wait for the first run to clear pending.
        for _ in range(50):
            if not vas._archive_pending:
                break
            time.sleep(0.01)
        # Reset the gate so the next "run" can also exit.
        gate.clear()
        gate.set()
        assert vas.trigger_archive_now() is True
        # Two distinct runs occurred.
        assert len(runs) == 2

    def test_pending_cleared_when_run_returns_normally(self, monkeypatch):
        def fake_run():
            with vas._archive_lock:
                vas._archive_pending = False

        monkeypatch.setattr(vas, '_run_archive', fake_run)
        assert vas.trigger_archive_now() is True
        # Worker thread is short-lived; give it a moment.
        for _ in range(50):
            if not vas._archive_pending:
                break
            time.sleep(0.01)
        assert vas._archive_pending is False, (
            "_archive_pending must be cleared by _run_archive's finally"
        )
