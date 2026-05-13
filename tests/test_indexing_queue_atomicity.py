"""Tests for indexing_queue_service.enqueue_many_for_indexing atomicity.

Issue #120 — the same autocommit-with-context-manager bug PR #119
fixed for ``archive_queue.enqueue_many_for_archive`` was still present
in ``indexing_queue_service.enqueue_many_for_indexing``. The autocommit
``isolation_level=None`` connection means ``with conn:`` is a no-op for
transaction control — every row of ``executemany`` paid its own fsync,
and any mid-batch SQLite error left a partial batch in the queue.

This file ports the 7-test ``TestEnqueueManyAtomicity`` class from
``tests/test_archive_queue.py`` (added by PR #119) so the indexing
queue has the same atomicity guarantees:

1. ROLLBACK on ``executemany`` error leaves DB unchanged
2. Concurrent reader sees zero or all (never partial)
3. Single ``BEGIN IMMEDIATE`` + single ``COMMIT`` regardless of batch size
4. Connection closed on success
5. Connection closed on failure
6. ``KeyboardInterrupt`` mid-batch still ROLLBACKs
7. Batch is substantially faster than per-row (>=3x for 100 rows)
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from services import indexing_queue_service
from services.indexing_queue_service import (
    enqueue_for_indexing,
    enqueue_many_for_indexing,
    get_queue_status,
)
from services.mapping_service import _init_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    """Initialize a fresh geodata.db with the indexing_queue schema."""
    db_path = str(tmp_path / "geodata.db")
    conn = _init_db(db_path)
    conn.close()
    return db_path


def _make_front_clip(tmp_path, name: str) -> str:
    """Create a front-camera clip path on disk so canonical_key
    accepts it (the indexer canonicalizes by basename, but the file
    must exist on disk for some downstream callers — for the queue,
    only canonical_key matters and it's a pure string transform)."""
    f = tmp_path / f"{name}-front.mp4"
    f.write_bytes(b"x")
    return str(f)


# ---------------------------------------------------------------------------
# Atomicity tests (port of TestEnqueueManyAtomicity from test_archive_queue.py)
# ---------------------------------------------------------------------------


class TestEnqueueManyForIndexingAtomicity:
    """Bulk enqueue must be all-or-nothing.

    Before the issue #120 fix the connection was in autocommit mode
    and each row of ``executemany`` committed independently — a SQLite
    error half-way through left a partial batch in the DB. After the
    fix the explicit ``BEGIN IMMEDIATE`` / ``COMMIT`` (with ``ROLLBACK``
    on exception) makes the batch atomic.
    """

    def test_rollback_on_executemany_error_leaves_db_unchanged(
        self, db, tmp_path, monkeypatch,
    ):
        """If ``executemany`` raises mid-batch, no rows from the batch
        survive. A pre-existing row is preserved.

        Strengthened (per PR #154 review F3): the mock executes a real
        ``INSERT`` for the first 5 of the 10 rows BEFORE raising, so
        the test actually exercises the rollback path. A weaker mock
        that raises before any insert would pass even with the bug
        present (since 'no rows visible' is trivially true if no rows
        were inserted in the first place).
        """
        pre = _make_front_clip(tmp_path, "pre")
        assert enqueue_for_indexing(db, pre) is True
        assert get_queue_status(db)['queue_depth'] == 1

        files = [(_make_front_clip(tmp_path, f"new_{i}"), None)
                 for i in range(10)]

        original_open = indexing_queue_service._open_queue_conn

        class _PartialThenRaiseExecuteMany:
            """Mock that performs the first 5 inserts as individual
            ``execute`` calls before raising. This guarantees that
            those 5 rows would be visible WITHOUT a rollback — so a
            passing assertion proves the ROLLBACK actually fired."""

            def __init__(self, real_conn):
                self._c = real_conn
                self._em_calls = 0

            def __getattr__(self, name):
                return getattr(self._c, name)

            def executemany(self, sql, seq_of_params):
                self._em_calls += 1
                # Materialize the first 5 params as real INSERTs so
                # they're written into the (open) transaction. Then
                # raise — a correct ROLLBACK must undo all 5.
                params_list = list(seq_of_params)
                for params in params_list[:5]:
                    self._c.execute(sql, params)
                raise sqlite3.OperationalError(
                    "simulated mid-batch failure (5 rows already inserted)"
                )

        def _patched_open(path):
            return _PartialThenRaiseExecuteMany(original_open(path))

        monkeypatch.setattr(indexing_queue_service,
                            '_open_queue_conn', _patched_open)

        n = enqueue_many_for_indexing(db, files)
        assert n == 0

        monkeypatch.undo()

        status = get_queue_status(db)
        assert status['queue_depth'] == 1, (
            "Atomicity violated: ROLLBACK did not undo the 5 partial "
            "inserts the mock made before raising. "
            f"Expected queue_depth=1 (the pre-existing row), got {status}"
        )

    def test_no_partial_batch_visible_to_concurrent_reader(
        self, db, tmp_path,
    ):
        """A concurrent reader must see either zero or all rows of a
        batch — never a partial state."""
        files = [(_make_front_clip(tmp_path, f"clip_{i}"), None)
                 for i in range(50)]

        ready = threading.Event()
        stop = threading.Event()
        observed_partial = []

        def _writer():
            ready.wait()
            enqueue_many_for_indexing(db, files)
            stop.set()

        def _reader():
            ready.wait()
            while not stop.is_set():
                n = get_queue_status(db)['queue_depth']
                if 0 < n < len(files):
                    observed_partial.append(n)
                time.sleep(0.001)

        tw = threading.Thread(target=_writer)
        tr = threading.Thread(target=_reader)
        tw.start(); tr.start()
        ready.set()
        tw.join(timeout=10)
        tr.join(timeout=10)

        assert get_queue_status(db)['queue_depth'] == len(files)
        assert not observed_partial, (
            f"Reader observed partial batch counts {observed_partial} — "
            "bulk enqueue is not atomic"
        )

    def test_batch_uses_single_commit_not_n_commits(
        self, db, tmp_path, monkeypatch,
    ):
        """The contract: one BEGIN IMMEDIATE, one COMMIT, regardless
        of batch size. Before the fix the autocommit ``executemany``
        committed each row implicitly. After the fix there is exactly
        one explicit ``BEGIN IMMEDIATE`` + one explicit ``COMMIT``."""
        files = [(_make_front_clip(tmp_path, f"clip_{i}"), None)
                 for i in range(20)]

        original_open = indexing_queue_service._open_queue_conn
        commit_calls = []
        begin_calls = []

        class _Tracking:
            def __init__(self, real_conn):
                self._c = real_conn

            def __getattr__(self, name):
                return getattr(self._c, name)

            def execute(self, sql, *a, **kw):
                stripped = sql.strip().upper()
                if stripped.startswith("COMMIT"):
                    commit_calls.append(sql)
                elif stripped.startswith("BEGIN"):
                    begin_calls.append(sql)
                return self._c.execute(sql, *a, **kw)

        def _patched_open(path):
            return _Tracking(original_open(path))

        monkeypatch.setattr(indexing_queue_service,
                            '_open_queue_conn', _patched_open)

        n = enqueue_many_for_indexing(db, files)
        assert n == 20
        assert len(begin_calls) == 1, (
            f"Expected exactly 1 BEGIN, got {len(begin_calls)}: "
            f"{begin_calls}"
        )
        assert len(commit_calls) == 1, (
            f"Expected exactly 1 COMMIT, got {len(commit_calls)}: "
            f"{commit_calls}"
        )
        assert "IMMEDIATE" in begin_calls[0].upper(), (
            f"BEGIN must be IMMEDIATE to avoid lock upgrade races, "
            f"got {begin_calls[0]!r}"
        )

    def test_connection_closed_on_success(
        self, db, tmp_path, monkeypatch,
    ):
        """The bulk path must close its connection (don't leak FDs).

        With autocommit mode the ``with conn:`` context manager does
        NOT close the connection (sqlite3 only commits/rollbacks). The
        fix moved to the ``_atomic_indexing_op`` context manager which
        always closes in ``finally`` — this test pins that invariant.
        """
        f = _make_front_clip(tmp_path, "clip")

        original_open = indexing_queue_service._open_queue_conn
        opened = []

        class _Tracker:
            def __init__(self, real):
                self._c = real
                self.closed = False

            def __getattr__(self, name):
                return getattr(self._c, name)

            def close(self):
                self.closed = True
                return self._c.close()

        def _patched_open(path):
            t = _Tracker(original_open(path))
            opened.append(t)
            return t

        monkeypatch.setattr(indexing_queue_service,
                            '_open_queue_conn', _patched_open)

        enqueue_many_for_indexing(db, [(f, None)])
        assert len(opened) == 1
        assert opened[0].closed, (
            "enqueue_many_for_indexing leaked its SQLite connection — "
            "the finally block must call conn.close()"
        )

    def test_connection_closed_on_failure(
        self, db, tmp_path, monkeypatch,
    ):
        """Even when executemany fails, the connection must be closed."""
        f = _make_front_clip(tmp_path, "clip")

        original_open = indexing_queue_service._open_queue_conn
        opened = []

        class _RaisingTracker:
            def __init__(self, real):
                self._c = real
                self.closed = False

            def __getattr__(self, name):
                return getattr(self._c, name)

            def executemany(self, *a, **kw):
                raise sqlite3.OperationalError("boom")

            def close(self):
                self.closed = True
                return self._c.close()

        def _patched_open(path):
            t = _RaisingTracker(original_open(path))
            opened.append(t)
            return t

        monkeypatch.setattr(indexing_queue_service,
                            '_open_queue_conn', _patched_open)

        n = enqueue_many_for_indexing(db, [(f, None)])
        assert n == 0
        assert opened[0].closed, (
            "enqueue_many_for_indexing leaked its SQLite connection on "
            "the failure path — the finally block must always close"
        )

    def test_keyboard_interrupt_mid_batch_rolls_back(
        self, db, tmp_path, monkeypatch,
    ):
        """A non-sqlite exception (e.g. KeyboardInterrupt) mid-batch
        must still ROLLBACK — never leave a half-committed batch."""
        pre = _make_front_clip(tmp_path, "pre")
        enqueue_for_indexing(db, pre)

        files = [(_make_front_clip(tmp_path, f"new_{i}"), None)
                 for i in range(5)]

        original_open = indexing_queue_service._open_queue_conn

        class _InterruptingConn:
            def __init__(self, real):
                self._c = real

            def __getattr__(self, name):
                return getattr(self._c, name)

            def executemany(self, *a, **kw):
                raise KeyboardInterrupt("user pressed Ctrl-C")

        def _patched_open(path):
            return _InterruptingConn(original_open(path))

        monkeypatch.setattr(indexing_queue_service,
                            '_open_queue_conn', _patched_open)

        with pytest.raises(KeyboardInterrupt):
            enqueue_many_for_indexing(db, files)

        monkeypatch.undo()

        status = get_queue_status(db)
        assert status['queue_depth'] == 1, (
            f"BaseException mid-batch broke atomicity: {status}"
        )

    def test_batch_speed_is_substantially_faster_than_per_row(
        self, db, tmp_path,
    ):
        """Sanity check that batching produces a measurable speedup
        over enqueueing each path individually.

        On a Pi Zero 2 W, individual enqueues with fsync per row run
        at ~10–30 inserts/sec; a transactional batch is 100+ inserts
        per single fsync. We require at least a 3× speedup for 100
        rows so the assertion survives test-runner noise but still
        catches a regression back to per-row commits.
        """
        many_files = [(_make_front_clip(tmp_path, f"many_{i}"), None)
                      for i in range(100)]
        single_files = [_make_front_clip(tmp_path, f"single_{i}")
                        for i in range(100)]

        # Time per-row (the slow path — N fsyncs).
        t0 = time.perf_counter()
        for p in single_files:
            enqueue_for_indexing(db, p)
        per_row = time.perf_counter() - t0

        # Time bulk (the fast path — 1 fsync).
        t0 = time.perf_counter()
        n = enqueue_many_for_indexing(db, many_files)
        bulk = time.perf_counter() - t0

        assert n == 100
        # Bulk must be substantially faster. On modern SSDs the ratio
        # is often 50-100x; on the Pi's SD card 10-50x. We require
        # only 3x to survive CI noise but still catch a regression.
        assert bulk * 3 < per_row, (
            f"Bulk enqueue not substantially faster than per-row: "
            f"per_row={per_row:.3f}s, bulk={bulk:.3f}s "
            f"(ratio {per_row/bulk:.1f}x, need >=3x)"
        )


class TestEnqueueForIndexingConnectionClose:
    """The single-row enqueue path also benefited from the #120 fix —
    autocommit mode means ``with conn:`` does not close the connection.
    The fix converted to explicit ``try/finally`` with ``conn.close()``.
    """

    def test_single_enqueue_closes_connection_on_success(
        self, db, tmp_path, monkeypatch,
    ):
        f = _make_front_clip(tmp_path, "clip")
        original_open = indexing_queue_service._open_queue_conn
        opened = []

        class _Tracker:
            def __init__(self, real):
                self._c = real
                self.closed = False

            def __getattr__(self, name):
                return getattr(self._c, name)

            def close(self):
                self.closed = True
                return self._c.close()

        def _patched_open(path):
            t = _Tracker(original_open(path))
            opened.append(t)
            return t

        monkeypatch.setattr(indexing_queue_service,
                            '_open_queue_conn', _patched_open)

        assert enqueue_for_indexing(db, f) is True
        assert len(opened) == 1
        assert opened[0].closed, (
            "enqueue_for_indexing leaked its SQLite connection — "
            "with autocommit, with conn: doesn't close. Use try/finally."
        )

    def test_single_enqueue_closes_connection_on_failure(
        self, db, tmp_path, monkeypatch,
    ):
        f = _make_front_clip(tmp_path, "clip")
        original_open = indexing_queue_service._open_queue_conn
        opened = []

        class _RaisingTracker:
            def __init__(self, real):
                self._c = real
                self.closed = False

            def __getattr__(self, name):
                return getattr(self._c, name)

            def execute(self, *a, **kw):
                raise sqlite3.OperationalError("boom")

            def close(self):
                self.closed = True
                return self._c.close()

        def _patched_open(path):
            t = _RaisingTracker(original_open(path))
            opened.append(t)
            return t

        monkeypatch.setattr(indexing_queue_service,
                            '_open_queue_conn', _patched_open)

        # The function catches sqlite3.Error and returns False.
        assert enqueue_for_indexing(db, f) is False
        assert opened[0].closed, (
            "enqueue_for_indexing leaked its SQLite connection on "
            "the failure path — finally must always close"
        )
