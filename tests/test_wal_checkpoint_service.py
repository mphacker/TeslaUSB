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


# ---------------------------------------------------------------------------
# Connection caching (issue #189) — new behaviour added in PR for #189.
# ---------------------------------------------------------------------------


class TestConnectionCaching:
    """Issue #189: the per-DB sqlite3.Connection is opened once and
    reused across ticks. Each tick re-stats the file and re-opens
    if the inode/device changed (DB recreated by rebuild/recovery)."""

    def setup_method(self, method):
        # Make sure no leftover state from another test.
        wcs.stop(timeout=2.0)
        wcs._close_all_cached_conns()

    def teardown_method(self, method):
        wcs.stop(timeout=2.0)
        wcs._close_all_cached_conns()

    def test_connection_is_cached_across_ticks(self, tmp_path):
        """Two consecutive checkpoints on the same DB must reuse the
        SAME sqlite3.Connection object — that's the whole point of
        the cache."""
        db = str(tmp_path / "test.db")
        keep_alive = _make_wal_db(db, write_rows=20)
        try:
            wcs._trigger_for_test(db)
            cached_first = wcs._conn_cache[db]
            wcs._trigger_for_test(db)
            cached_second = wcs._conn_cache[db]
            # Same NamedTuple → same connection object (id-equal).
            assert cached_first.conn is cached_second.conn, (
                "Cached connection was replaced between ticks even "
                "though the DB inode did not change. The cache is "
                "not delivering the #189 saving."
            )
            assert cached_first.ino == cached_second.ino
            assert cached_first.dev == cached_second.dev
        finally:
            keep_alive.close()

    def test_connection_reopens_on_inode_change(self, tmp_path, caplog):
        """When the DB file is replaced under us (corruption-recovery,
        rebuild_index), the next checkpoint must detect the inode
        change and re-open the connection. Without invalidation, the
        cached FD would silently keep writing to the deleted inode.

        We simulate the inode change by mutating the cached entry's
        recorded ``ino`` to a synthetic value (rather than actually
        unlinking the file, which is blocked on Windows when the
        cached handle holds it open). Cross-platform; the
        invalidation logic — ``cached.ino == cur_ino`` — is the
        thing under test, not the OS-level unlink semantics.
        """
        import logging
        db = str(tmp_path / "test.db")
        keep_alive = _make_wal_db(db, write_rows=20)
        try:
            wcs._trigger_for_test(db)
            cached_first = wcs._conn_cache[db]
            real_ino = cached_first.ino

            # Mutate the cached entry to simulate the file being
            # replaced under us — recorded ino no longer matches the
            # on-disk stat.
            with wcs._conn_cache_lock:
                wcs._conn_cache[db] = cached_first._replace(
                    ino=real_ino + 1,
                )

            with caplog.at_level(
                logging.INFO, logger='services.wal_checkpoint_service'
            ):
                wcs._trigger_for_test(db)

            cached_second = wcs._conn_cache[db]
            assert cached_second.conn is not cached_first.conn, (
                "Cached connection was NOT replaced after the "
                "recorded inode diverged from the on-disk stat. "
                "The next checkpoint would have written to a "
                "deleted/replaced inode."
            )
            assert cached_second.ino == real_ino, (
                "Cache did not record the on-disk inode after re-open."
            )
            invalidation_msgs = [
                r for r in caplog.records
                if 'inode changed' in r.message
            ]
            assert invalidation_msgs, (
                "No inode-change log emitted; operators have no "
                "visibility into cache turnover during a "
                "rebuild_index operation."
            )
        finally:
            keep_alive.close()

    def test_sqlite_error_evicts_cached_conn(self, tmp_path):
        """A transient sqlite error during a checkpoint must evict
        the cached connection so the next tick re-opens fresh.
        Otherwise a single error could leave a wedged connection in
        the cache forever."""
        db = str(tmp_path / "test.db")
        keep_alive = _make_wal_db(db, write_rows=20)
        try:
            wcs._trigger_for_test(db)
            assert db in wcs._conn_cache, "First checkpoint should have cached"
            cached = wcs._conn_cache[db]

            # Force a sqlite error by closing the cached connection
            # out from under the next call. The execute() will raise
            # ProgrammingError; the error path MUST evict.
            cached.conn.close()
            wcs._trigger_for_test(db)
            assert db not in wcs._conn_cache, (
                "Cached connection was NOT evicted after a sqlite "
                "error; the next tick would re-use a wedged handle."
            )

            # Next call re-opens fresh and succeeds.
            wcs._trigger_for_test(db)
            assert db in wcs._conn_cache, (
                "Cache did not re-open the connection after eviction."
            )
        finally:
            keep_alive.close()

    def test_close_all_closes_every_cached_conn(self, tmp_path):
        """``_close_all_cached_conns`` (called from ``stop()``) must
        close every cached handle and clear the cache. Test-driven
        start/stop cycles otherwise leak FDs."""
        db1 = str(tmp_path / "a.db")
        db2 = str(tmp_path / "b.db")
        keep1 = _make_wal_db(db1, write_rows=5)
        keep2 = _make_wal_db(db2, write_rows=5)
        try:
            wcs._trigger_for_test(db1)
            wcs._trigger_for_test(db2)
            assert db1 in wcs._conn_cache
            assert db2 in wcs._conn_cache
            cached1 = wcs._conn_cache[db1]
            cached2 = wcs._conn_cache[db2]

            wcs._close_all_cached_conns()
            assert wcs._conn_cache == {}, (
                "_close_all_cached_conns left entries in the cache"
            )
            # The connections themselves are closed — executing
            # against them now raises ProgrammingError.
            with pytest.raises(sqlite3.ProgrammingError):
                cached1.conn.execute("SELECT 1")
            with pytest.raises(sqlite3.ProgrammingError):
                cached2.conn.execute("SELECT 1")
        finally:
            keep1.close()
            keep2.close()

    def test_stop_closes_cached_conns(self, tmp_path):
        """``stop()`` must invoke ``_close_all_cached_conns`` so a
        test that starts the daemon, lets it run, then stops doesn't
        leak a long-lived FD between tests."""
        db = str(tmp_path / "test.db")
        keep_alive = _make_wal_db(db, write_rows=10)
        try:
            # Prime the cache via the synchronous test entry-point so
            # the test doesn't have to wait for a 30 s tick.
            wcs._trigger_for_test(db)
            assert db in wcs._conn_cache

            wcs.start([db])
            wcs.stop(timeout=3.0)
            assert wcs._conn_cache == {}, (
                "stop() did not close cached connections"
            )
        finally:
            keep_alive.close()

    def test_missing_db_does_not_populate_cache(self, tmp_path):
        """If the DB file doesn't exist at tick time, the cache must
        NOT get a None entry or a half-open connection — the early-
        return must skip cleanly."""
        wcs._trigger_for_test(str(tmp_path / "nonexistent.db"))
        assert str(tmp_path / "nonexistent.db") not in wcs._conn_cache
