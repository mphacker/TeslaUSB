"""Tests for issue #184 Wave 3 — Phase G (idle-time WAL checkpoints).

Covers:

* ``_checkpoint_one`` runs ``PRAGMA wal_checkpoint(TRUNCATE)`` and
  resets the WAL file when there are no active readers.
* ``_is_coordinator_idle`` correctly reads the
  ``task_coordinator.is_busy()`` and ``waiter_count()`` signals.
* ``start`` is idempotent and ``stop`` joins cleanly.
* The service is defensive: a missing DB path is a no-op.
"""

from __future__ import annotations

import os
import sqlite3
import time

import pytest

from services import wal_checkpoint_service as wcs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_wal_db(path: str, *, write_rows: int = 50) -> sqlite3.Connection:
    """Create a SQLite DB in WAL mode and write enough rows to grow
    the WAL file to a measurable size. Returns an open reader
    connection that the caller MUST keep alive — closing the only
    open connection triggers an implicit checkpoint and truncates
    the WAL file out from under the test.
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (k INTEGER PRIMARY KEY, v TEXT)")
    for i in range(write_rows):
        conn.execute("INSERT INTO t (v) VALUES (?)", (f"row-{i}-" + "x" * 64,))
    conn.commit()
    # Run a SELECT so the connection holds a read transaction snapshot
    # — without this an implicit checkpoint can still fire on the
    # writer's WAL when no other readers exist.
    conn.execute("SELECT COUNT(*) FROM t").fetchone()
    return conn


# ---------------------------------------------------------------------------
# _checkpoint_one
# ---------------------------------------------------------------------------

class TestCheckpointOne:
    def test_truncates_wal_after_writes(self, tmp_path):
        db = str(tmp_path / "test.db")
        keep_alive = _make_wal_db(db, write_rows=50)
        try:
            wal_before = os.path.getsize(db + '-wal')
            assert wal_before > 0

            wcs._checkpoint_one(db)

            wal_after = (
                os.path.getsize(db + '-wal') if os.path.isfile(db + '-wal') else 0
            )
            # TRUNCATE checkpoint folds frames back into the main DB
            # and truncates the WAL to zero (or near-zero) bytes.
            assert wal_after < wal_before
        finally:
            keep_alive.close()

    def test_missing_db_is_noop(self, tmp_path):
        # Must not raise on a path that doesn't exist.
        wcs._checkpoint_one(str(tmp_path / "nonexistent.db"))
        wcs._checkpoint_one('')
        # No exception → pass

    def test_handles_locked_db_gracefully(self, tmp_path):
        # Open an exclusive transaction in a separate connection so
        # the checkpoint is forced to back off. ``_checkpoint_one``
        # must NOT raise — the optimization tolerates contention.
        db = str(tmp_path / "test.db")
        keep_alive = _make_wal_db(db, write_rows=10)
        try:
            blocker = sqlite3.connect(db, timeout=0.5)
            try:
                blocker.isolation_level = None
                blocker.execute("BEGIN IMMEDIATE")
                try:
                    # Should not raise even when the writer lock is held.
                    wcs._checkpoint_one(db)
                finally:
                    blocker.execute("ROLLBACK")
            finally:
                blocker.close()
        finally:
            keep_alive.close()


# ---------------------------------------------------------------------------
# _is_coordinator_idle — reads ``task_coordinator`` signals.
# ---------------------------------------------------------------------------

class TestCoordinatorIdleProbe:
    def test_returns_false_when_busy(self, monkeypatch):
        from services import task_coordinator
        monkeypatch.setattr(task_coordinator, 'is_busy', lambda: True)
        monkeypatch.setattr(task_coordinator, 'waiter_count', lambda: 0)
        assert wcs._is_coordinator_idle() is False

    def test_returns_false_when_waiters_pending(self, monkeypatch):
        from services import task_coordinator
        monkeypatch.setattr(task_coordinator, 'is_busy', lambda: False)
        monkeypatch.setattr(task_coordinator, 'waiter_count', lambda: 1)
        assert wcs._is_coordinator_idle() is False

    def test_returns_true_when_idle(self, monkeypatch):
        from services import task_coordinator
        monkeypatch.setattr(task_coordinator, 'is_busy', lambda: False)
        monkeypatch.setattr(task_coordinator, 'waiter_count', lambda: 0)
        assert wcs._is_coordinator_idle() is True

    def test_returns_false_when_probe_raises(self, monkeypatch):
        from services import task_coordinator

        def _boom():
            raise RuntimeError("simulated coordinator failure")

        monkeypatch.setattr(task_coordinator, 'is_busy', _boom)
        # Conservative behavior: any error → back off, not check-
        # point. Better to under-checkpoint than to compete for I/O
        # while a heavy task is running.
        assert wcs._is_coordinator_idle() is False


# ---------------------------------------------------------------------------
# start / stop / is_running
# ---------------------------------------------------------------------------

class TestServiceLifecycle:
    def setup_method(self, method):
        # Make sure no leftover state from another test.
        wcs.stop(timeout=2.0)

    def teardown_method(self, method):
        wcs.stop(timeout=2.0)

    def test_start_is_idempotent(self, tmp_path):
        db = str(tmp_path / "test.db")
        keep_alive = _make_wal_db(db, write_rows=5)
        try:
            assert wcs.start([db]) is True
            try:
                assert wcs.is_running()
                # Second call must NOT spawn a second thread.
                assert wcs.start([db]) is False
                assert wcs.is_running()
            finally:
                wcs.stop(timeout=2.0)
        finally:
            keep_alive.close()

    def test_stop_joins_thread(self, tmp_path):
        db = str(tmp_path / "test.db")
        keep_alive = _make_wal_db(db, write_rows=5)
        try:
            wcs.start([db])
            assert wcs.is_running()
            wcs.stop(timeout=3.0)
            # Give the daemon a moment to release.
            for _ in range(20):
                if not wcs.is_running():
                    break
                time.sleep(0.05)
            assert wcs.is_running() is False
        finally:
            keep_alive.close()


# ---------------------------------------------------------------------------
# _trigger_for_test — synchronous test entry point.
# ---------------------------------------------------------------------------

class TestTriggerForTest:
    def test_synchronous_checkpoint(self, tmp_path):
        # The trigger MUST checkpoint inline (no thread, no sleeps).
        db = str(tmp_path / "test.db")
        keep_alive = _make_wal_db(db, write_rows=50)
        try:
            wal_before = os.path.getsize(db + '-wal')
            wcs._trigger_for_test(db)
            wal_after = (
                os.path.getsize(db + '-wal') if os.path.isfile(db + '-wal') else 0
            )
            assert wal_after < wal_before
        finally:
            keep_alive.close()
