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


class TestArchiveIoniceUsesThreadId:
    """Issue #72: ``_archive_timer_loop`` was calling
    ``ionice -c 3 -p <os.getpid()>`` to drop the archive thread's I/O
    priority — but on Linux ``ioprio_set`` is per-task. Passing the
    process PID only adjusts the main thread's I/O class, leaving
    the archive worker thread at default best-effort priority and
    fully able to starve the gadget endpoint and Flask. The fix
    passes ``threading.get_native_id()`` instead.
    """

    def test_archive_ionice_uses_native_tid_not_pid(self, monkeypatch):
        import sys
        if not sys.platform.startswith('linux'):
            pytest.skip("ionice is Linux-only")

        captured = []
        # Mock subprocess.run to capture the ionice invocation. Then
        # raise after capturing so the archive timer loop exits early
        # without waiting 2 minutes for the initial-delay.
        gate_event = threading.Event()

        def fake_run(cmd, *a, **kw):
            captured.append(cmd)

            class R:
                returncode = 0
                stdout = b''
                stderr = b''
            # Trip the cancel after the first ionice call to exit the
            # loop quickly.
            gate_event.set()
            return R()

        import subprocess as sp
        monkeypatch.setattr(sp, 'run', fake_run)

        # Stop the loop right after the ionice call by setting the
        # cancel event — that way the for-loop initial delay exits on
        # its first iteration.
        cancel = threading.Event()
        monkeypatch.setattr(vas, '_archive_cancel', cancel)

        def stop_after_first_run():
            gate_event.wait(timeout=2.0)
            cancel.set()

        stopper = threading.Thread(target=stop_after_first_run, daemon=True)
        stopper.start()

        # Run the timer loop directly. It should ionice-then-exit.
        loop_thread = threading.Thread(
            target=vas._archive_timer_loop, daemon=True,
        )
        loop_thread.start()
        # Capture the loop thread's TID — that's what should be passed
        # to ionice. Native TID is set as soon as the thread starts.
        # Wait briefly for the thread to register its TID and run
        # ionice.
        for _ in range(100):
            if captured:
                break
            time.sleep(0.01)
        loop_thread.join(timeout=5.0)

        ionice_calls = [c for c in captured if c and c[0] == 'ionice']
        assert ionice_calls, (
            f"Expected an ionice call from _archive_timer_loop, got: "
            f"{captured}"
        )
        cmd = ionice_calls[0]
        p_idx = cmd.index('-p')
        tid_arg = int(cmd[p_idx + 1])

        import os
        # The TID must NOT be the process PID — that's the bug being
        # fixed.
        assert tid_arg != os.getpid(), (
            f"ionice -p {tid_arg} matches process PID {os.getpid()} "
            f"— this is the issue #72 bug; archive thread's I/O "
            f"priority must be set on its OWN native TID, not the "
            f"process PID"
        )
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
