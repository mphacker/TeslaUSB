"""Tests for the indexing worker (services.indexing_worker).

The module is structured so the dispatch logic (``process_claimed_item``)
is a pure function — no thread, no SQLite needed for outcome-mapping
tests. Lifecycle tests use a real thread but always tear it down.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest

from services import indexing_worker
from services import mapping_service
from services import indexing_queue_service as queue_svc
from services.mapping_service import (
    IndexOutcome,
    IndexResult,
    _init_db,
    canonical_key,
)
from services.indexing_queue_service import (
    enqueue_for_indexing,
    claim_next_queue_item,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_worker():
    """Ensure no leftover worker from a previous test."""
    indexing_worker.stop_worker(timeout=5.0)
    yield
    indexing_worker.stop_worker(timeout=5.0)
    # Also clear pause flag in case a test left it set.
    indexing_worker.resume_worker()


@pytest.fixture
def db(tmp_path):
    db_path = str(tmp_path / "geodata.db")
    _init_db(db_path)
    return db_path


# ---------------------------------------------------------------------------
# Pure-dispatch tests: process_claimed_item
# ---------------------------------------------------------------------------

def _make_row(file_path='/mnt/x/clip.mp4', attempts=0,
              source='watcher'):
    return {
        'canonical_key': canonical_key(file_path),
        'file_path': file_path,
        'priority': 30,
        'enqueued_at': 100.0,
        'next_attempt_at': 0.0,
        'attempts': attempts,
        'last_error': None,
        'source': source,
        'claimed_by': 'worker-test',
        'claimed_at': 200.0,
    }


class TestProcessClaimedItem:
    def test_indexed_yields_complete(self):
        row = _make_row()
        action = indexing_worker.process_claimed_item(
            row, db_path='_unused', teslacam_root='_unused',
            indexer=lambda *a, **k: IndexResult(IndexOutcome.INDEXED, 5, 1),
        )
        assert action.action == 'complete'
        assert action.purge_path is None
        assert action.outcome == IndexOutcome.INDEXED

    def test_already_indexed_yields_complete(self):
        row = _make_row()
        action = indexing_worker.process_claimed_item(
            row, '_x', '_y',
            indexer=lambda *a, **k: IndexResult(IndexOutcome.ALREADY_INDEXED),
        )
        assert action.action == 'complete'

    def test_no_gps_yields_complete(self):
        row = _make_row()
        action = indexing_worker.process_claimed_item(
            row, '_x', '_y',
            indexer=lambda *a, **k: IndexResult(IndexOutcome.NO_GPS_RECORDED),
        )
        assert action.action == 'complete'

    def test_not_front_yields_complete(self):
        row = _make_row(file_path='/x/clip-back.mp4')
        action = indexing_worker.process_claimed_item(
            row, '_x', '_y',
            indexer=lambda *a, **k: IndexResult(IndexOutcome.NOT_FRONT_CAMERA),
        )
        assert action.action == 'complete'

    def test_file_missing_yields_complete_with_purge(self):
        row = _make_row()
        action = indexing_worker.process_claimed_item(
            row, '_x', '_y',
            indexer=lambda *a, **k: IndexResult(IndexOutcome.FILE_MISSING),
        )
        assert action.action == 'complete'
        assert action.purge_path == row['file_path']

    def test_too_new_yields_defer_at_mtime_plus_125(self, tmp_path):
        clip = tmp_path / "clip-front.mp4"
        clip.write_bytes(b'')
        # Force a known mtime so the assertion is deterministic.
        os.utime(str(clip), (1_700_000_000, 1_700_000_000))
        row = _make_row(file_path=str(clip))
        action = indexing_worker.process_claimed_item(
            row, '_x', '_y',
            indexer=lambda *a, **k: IndexResult(IndexOutcome.TOO_NEW),
        )
        assert action.action == 'defer'
        assert action.bump_attempts is False
        assert abs(action.next_attempt_at - (1_700_000_000 + 125)) < 0.5

    def test_too_new_with_missing_file_promotes_to_complete(self):
        row = _make_row(file_path='/nope/missing-front.mp4')
        action = indexing_worker.process_claimed_item(
            row, '_x', '_y',
            indexer=lambda *a, **k: IndexResult(IndexOutcome.TOO_NEW),
        )
        # File vanished between the indexer's mtime check and the
        # dispatcher's — purge it.
        assert action.action == 'complete'
        assert action.purge_path == row['file_path']
        assert action.outcome == IndexOutcome.FILE_MISSING

    def test_parse_error_yields_defer_with_backoff(self):
        row = _make_row(attempts=0)
        action = indexing_worker.process_claimed_item(
            row, '_x', '_y',
            indexer=lambda *a, **k: IndexResult(
                IndexOutcome.PARSE_ERROR, error='boom',
            ),
            now_fn=lambda: 1000.0,
        )
        assert action.action == 'defer'
        assert action.bump_attempts is True
        # First attempt: BASE * 2^0 = 60s.
        assert action.next_attempt_at == 1000.0 + 60.0
        assert action.last_error == 'boom'

    def test_parse_error_backoff_grows(self):
        row = _make_row(attempts=2)
        action = indexing_worker.process_claimed_item(
            row, '_x', '_y',
            indexer=lambda *a, **k: IndexResult(
                IndexOutcome.PARSE_ERROR, error='oof',
            ),
            now_fn=lambda: 1000.0,
        )
        # attempts=2 → 60 * 2^2 = 240.
        assert action.next_attempt_at == 1000.0 + 240.0

    def test_db_busy_yields_release(self):
        row = _make_row()
        action = indexing_worker.process_claimed_item(
            row, '_x', '_y',
            indexer=lambda *a, **k: IndexResult(
                IndexOutcome.DB_BUSY, error='locked',
            ),
        )
        assert action.action == 'release'
        assert action.last_error == 'locked'

    def test_indexer_exception_converts_to_defer(self):
        row = _make_row(attempts=1)

        def boom(*a, **k):
            raise RuntimeError("kaboom")

        action = indexing_worker.process_claimed_item(
            row, '_x', '_y', indexer=boom, now_fn=lambda: 500.0,
        )
        assert action.action == 'defer'
        assert action.bump_attempts is True
        assert action.outcome == IndexOutcome.PARSE_ERROR
        assert 'kaboom' in (action.last_error or '')
        # attempts=1 → 60 * 2^1 = 120s.
        assert action.next_attempt_at == 500.0 + 120.0

    def test_unknown_outcome_yields_release(self):
        row = _make_row()
        # Synthesize an outcome the dispatcher doesn't know.
        from enum import Enum

        class FakeOutcome(Enum):
            NEW_THING = 'new'
        # Wrap in a stand-in IndexResult — process_claimed_item only
        # reads .outcome, so the type check is duck-typed.
        action = indexing_worker.process_claimed_item(
            row, '_x', '_y',
            indexer=lambda *a, **k: IndexResult(FakeOutcome.NEW_THING),  # type: ignore[arg-type]
        )
        assert action.action == 'release'


# ---------------------------------------------------------------------------
# _apply_action: queue mutation glue
# ---------------------------------------------------------------------------

class TestApplyAction:
    def test_complete_deletes_row(self, db):
        enqueue_for_indexing(db, '/x/clip-front.mp4')
        row = claim_next_queue_item(db, 'w-1')
        action = indexing_worker.WorkerAction(
            action='complete', outcome=IndexOutcome.INDEXED,
        )
        indexing_worker._apply_action(action, row, db)
        with sqlite3.connect(db) as c:
            count = c.execute(
                "SELECT COUNT(*) FROM indexing_queue"
            ).fetchone()[0]
        assert count == 0

    def test_defer_releases_claim_and_sets_attempt_time(self, db):
        enqueue_for_indexing(db, '/x/clip-front.mp4')
        row = claim_next_queue_item(db, 'w-1')
        action = indexing_worker.WorkerAction(
            action='defer', next_attempt_at=9999.0,
            bump_attempts=True, last_error='oops',
        )
        indexing_worker._apply_action(action, row, db)
        with sqlite3.connect(db) as c:
            r = c.execute(
                """SELECT claimed_by, attempts, next_attempt_at, last_error
                   FROM indexing_queue"""
            ).fetchone()
        assert r[0] is None
        assert r[1] == 1
        assert abs(r[2] - 9999.0) < 1e-3
        assert r[3] == 'oops'

    def test_release_clears_claim_keeps_row(self, db):
        enqueue_for_indexing(db, '/x/clip-front.mp4')
        row = claim_next_queue_item(db, 'w-1')
        action = indexing_worker.WorkerAction(action='release')
        indexing_worker._apply_action(action, row, db)
        with sqlite3.connect(db) as c:
            r = c.execute(
                "SELECT claimed_by, attempts FROM indexing_queue"
            ).fetchone()
        assert r[0] is None
        assert r[1] == 0

    def test_owner_guard_prevents_stale_complete(self, db):
        # Worker-1 claims, but takes too long. Stale-recovery releases
        # the row, worker-2 claims it. Worker-1 then tries to complete
        # using its OLD claim_at — must be a no-op.
        enqueue_for_indexing(db, '/x/clip-front.mp4')
        row1 = claim_next_queue_item(db, 'w-1')
        # Force the row to be released and re-claimed.
        with sqlite3.connect(db) as c:
            c.execute(
                """UPDATE indexing_queue
                   SET claimed_by=NULL, claimed_at=NULL"""
            )
        row2 = claim_next_queue_item(db, 'w-2')
        assert row2 is not None
        # Stale worker-1 completes with old claim_at.
        action = indexing_worker.WorkerAction(
            action='complete', outcome=IndexOutcome.INDEXED,
        )
        indexing_worker._apply_action(action, row1, db)
        # Row must still exist — owner-guard rejected the delete.
        with sqlite3.connect(db) as c:
            count = c.execute(
                "SELECT COUNT(*) FROM indexing_queue"
            ).fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# Lifecycle: start / stop / pause / resume
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_start_stop(self, db, tmp_path):
        assert indexing_worker.start_worker(db, str(tmp_path)) is True
        assert indexing_worker.is_running() is True
        assert indexing_worker.stop_worker(timeout=5.0) is True
        assert indexing_worker.is_running() is False

    def test_start_is_idempotent(self, db, tmp_path):
        assert indexing_worker.start_worker(db, str(tmp_path)) is True
        # Second start returns False — already running.
        assert indexing_worker.start_worker(db, str(tmp_path)) is False
        indexing_worker.stop_worker(timeout=5.0)

    def test_stop_when_not_running(self):
        # Stopping an already-stopped worker is a no-op success.
        assert indexing_worker.stop_worker(timeout=1.0) is True

    def test_pause_when_not_running_returns_idle(self):
        # No worker → trivially "idle". Caller can proceed.
        assert indexing_worker.pause_worker(timeout=0.1) is True

    def test_stop_timeout_does_not_leak_thread(self, db, tmp_path,
                                                  monkeypatch):
        # Reproduces the race that previously allowed two worker
        # threads to coexist: stop_worker(timeout=tiny) returns False
        # but MUST NOT clear _worker_thread, otherwise the next
        # start_worker() spins up a second thread that races the
        # still-alive first thread over claim rows.
        gate = threading.Event()
        finish = threading.Event()

        def slow_indexer(file_path, db_path, teslacam_root):
            gate.set()
            finish.wait(timeout=5.0)
            return IndexResult(IndexOutcome.INDEXED, 1, 0)

        monkeypatch.setattr(
            mapping_service, 'index_single_file', slow_indexer,
        )

        enqueue_for_indexing(db, '/x/slow-front.mp4')
        assert indexing_worker.start_worker(db, str(tmp_path)) is True
        assert gate.wait(timeout=5.0)
        first_id = indexing_worker.get_worker_status()['worker_id']

        # Force a stop timeout while the indexer is mid-file.
        assert indexing_worker.stop_worker(timeout=0.05) is False
        # Worker must still be alive — we did NOT clear the thread ref.
        assert indexing_worker.is_running() is True

        # Second start must REFUSE; we don't want two threads.
        assert indexing_worker.start_worker(db, str(tmp_path)) is False
        # The same worker_id must still be active (no replacement).
        assert indexing_worker.get_worker_status()['worker_id'] == first_id

        # Release the slow indexer so cleanup can complete.
        finish.set()
        assert indexing_worker.stop_worker(timeout=5.0) is True
        assert indexing_worker.is_running() is False


class TestPauseResume:
    def test_pause_blocks_until_idle(self, db, tmp_path,
                                       monkeypatch):
        # Make the indexer slow so we can observe pause waiting on it.
        gate = threading.Event()
        finish = threading.Event()
        invocations = []

        def slow_indexer(file_path, db_path, teslacam_root):
            invocations.append(file_path)
            gate.set()
            # Hold the worker mid-file until the test releases it.
            finish.wait(timeout=3.0)
            return IndexResult(IndexOutcome.INDEXED, 1, 0)

        monkeypatch.setattr(
            mapping_service, 'index_single_file', slow_indexer,
        )

        # Enqueue one file and start the worker.
        enqueue_for_indexing(db, '/x/slow-front.mp4')
        indexing_worker.start_worker(db, str(tmp_path))

        # Wait for the worker to actually pick up the file.
        assert gate.wait(timeout=5.0)

        # Pause with a short timeout — the indexer is mid-file, so
        # the wait must time out.
        became_idle = indexing_worker.pause_worker(timeout=0.5)
        assert became_idle is False

        # Now release the indexer; the worker finishes the file and
        # becomes idle.
        finish.set()
        assert indexing_worker.pause_worker(timeout=5.0) is True

        # Resume + stop.
        indexing_worker.resume_worker()
        indexing_worker.stop_worker(timeout=5.0)

    def test_paused_worker_does_not_claim_new_files(self, db, tmp_path,
                                                     monkeypatch):
        called = []

        def fake_indexer(*a, **k):
            called.append(a[0])
            return IndexResult(IndexOutcome.INDEXED, 1, 0)

        monkeypatch.setattr(
            mapping_service, 'index_single_file', fake_indexer,
        )

        # Pause BEFORE enqueueing; no file should be claimed.
        indexing_worker.start_worker(db, str(tmp_path))
        indexing_worker.pause_worker(timeout=2.0)

        enqueue_for_indexing(db, '/x/paused-front.mp4')
        # Give the worker a generous window to (incorrectly) pick it up.
        time.sleep(1.0)
        assert called == []
        # Confirm the row is still unclaimed.
        with sqlite3.connect(db) as c:
            row = c.execute(
                "SELECT claimed_by FROM indexing_queue"
            ).fetchone()
        assert row[0] is None

        # Resume — file should now be processed.
        indexing_worker.resume_worker()
        deadline = time.time() + 5.0
        while time.time() < deadline and not called:
            time.sleep(0.1)
        assert called, "worker did not resume"
        indexing_worker.stop_worker(timeout=5.0)


class TestStatus:
    def test_status_when_not_running(self, db, tmp_path):
        # No db_path until first start_worker — status returns mostly Nones.
        st = indexing_worker.get_worker_status()
        assert st['worker_running'] is False
        assert st['active_file'] is None

    def test_status_after_start_includes_queue_info(self, db, tmp_path):
        enqueue_for_indexing(db, '/x/foo-front.mp4')
        # Block the indexer so the status snapshot is deterministic.
        gate = threading.Event()

        def hold_open(*a, **k):
            gate.wait(timeout=3.0)
            return IndexResult(IndexOutcome.INDEXED, 1, 0)

        from unittest.mock import patch
        with patch.object(mapping_service, 'index_single_file', hold_open):
            indexing_worker.start_worker(db, str(tmp_path))
            try:
                # Wait until worker has actually claimed.
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    if indexing_worker.get_worker_status()['active_file']:
                        break
                    time.sleep(0.05)
                st = indexing_worker.get_worker_status()
                assert st['worker_running'] is True
                assert st['active_file'] == '/x/foo-front.mp4'
                assert st['source'] == 'manual'  # default for enqueue_for_indexing
                assert 'queue_depth' in st
            finally:
                gate.set()
                indexing_worker.stop_worker(timeout=5.0)


class TestApplyLowPriority:
    """Issue #72: ``_apply_low_priority`` was lowering the WHOLE
    ``gadget_web`` process priority via ``os.nice(19)``, making the
    Flask request handlers and every other thread low-priority too.
    The fix uses thread-local SCHED_IDLE + ``ionice`` against the
    calling thread's TID instead.
    """

    def test_does_not_call_os_nice(self, monkeypatch):
        """``os.nice`` is process-wide on Linux — must NOT be used
        from a worker thread because it lowers Flask priority too.
        On Windows ``os.nice`` doesn't even exist; on Linux we must
        guarantee the function never invokes it.
        """
        import sys
        if not sys.platform.startswith('linux'):
            # Non-Linux: function early-returns and ``os.nice`` is
            # missing from stdlib, so the regression cannot occur.
            indexing_worker._apply_low_priority()
            return

        called = []

        def fake_nice(_):  # pragma: no cover - just records
            called.append(True)
            raise AssertionError(
                "indexing_worker._apply_low_priority must NOT call "
                "os.nice() — it's process-wide and would lower "
                "Flask request handlers' priority too (issue #72)"
            )

        monkeypatch.setattr(os, 'nice', fake_nice)
        # Should not raise.
        indexing_worker._apply_low_priority()
        assert called == []

    def test_uses_native_tid_for_ionice(self, monkeypatch):
        """``ionice -p`` must receive the calling thread's TID
        (``threading.get_native_id()``), not the process PID. On
        Linux, ioprio_set is per-task — passing the PID only adjusts
        the main thread's I/O class.
        """
        import sys
        if not sys.platform.startswith('linux'):
            pytest.skip("ionice is Linux-only")

        captured_args = []

        def fake_run(cmd, *a, **kw):
            captured_args.append(cmd)

            class R:
                returncode = 0
                stdout = b''
                stderr = b''
            return R()

        import subprocess as sp
        monkeypatch.setattr(sp, 'run', fake_run)

        expected_tid = threading.get_native_id()
        indexing_worker._apply_low_priority()

        ionice_calls = [c for c in captured_args if c and c[0] == 'ionice']
        assert ionice_calls, (
            f"Expected an ionice call from _apply_low_priority, got: "
            f"{captured_args}"
        )
        # Find the -p arg.
        cmd = ionice_calls[0]
        p_idx = cmd.index('-p')
        tid_arg = int(cmd[p_idx + 1])
        # The TID we capture inside the test thread IS the same TID
        # _apply_low_priority sees because we call it synchronously.
        assert tid_arg == expected_tid, (
            f"ionice -p got {tid_arg} but the calling thread's "
            f"native TID is {expected_tid} — must match for I/O "
            f"priority to apply to the indexer thread, not the main "
            f"thread (issue #72)"
        )

    def test_uses_sched_idle_not_sched_batch(self, monkeypatch):
        """SCHED_IDLE (5) lowers CPU priority more than SCHED_BATCH (3),
        and is the right choice for "only run when nothing else wants
        the CPU" semantics. Earlier code used SCHED_BATCH which still
        competed normally for the CPU."""
        import sys
        if not sys.platform.startswith('linux'):
            pytest.skip("sched_setscheduler is Linux-only")
        if not hasattr(os, 'sched_setscheduler'):
            pytest.skip("os.sched_setscheduler missing on this build")

        captured = []

        def fake_setscheduler(pid, policy, param):
            captured.append((pid, policy))

        monkeypatch.setattr(os, 'sched_setscheduler', fake_setscheduler)
        indexing_worker._apply_low_priority()

        assert captured, "Expected sched_setscheduler call"
        pid_arg, policy_arg = captured[0]
        assert pid_arg == 0, (
            "First arg must be 0 (= 'this thread'), not a PID"
        )
        assert policy_arg == 5, (
            f"Policy must be SCHED_IDLE (5), got {policy_arg}"
        )
