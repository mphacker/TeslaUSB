"""Tests for the archive_queue module (services.archive_queue).

Phase 2a producer-side API for issue #76. These tests cover:

* Priority inference for every documented directory pattern.
* Single enqueue (happy path, idempotent dedup, rejects empty paths).
* Batch enqueue (happy path, dedup within batch, dedup across calls).
* Metadata capture (size + mtime for existing files; NULL for missing).
* Status counts (with rows in multiple statuses including unknown).
* List queue (sorted, with status filter, with limit).
* Concurrent enqueue from multiple threads (no exceptions, correct count).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest

from services import archive_queue
from services.archive_queue import (
    PRIORITY_EVENTS,
    PRIORITY_OTHER,
    PRIORITY_RECENT_CLIPS,
    _infer_priority,
    enqueue_for_archive,
    enqueue_many_for_archive,
    get_queue_status,
    list_queue,
)
from services.mapping_service import _init_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    """Initialize a fresh geodata.db with the v10 schema (incl. archive_queue)."""
    db_path = str(tmp_path / "geodata.db")
    conn = _init_db(db_path)
    conn.close()
    return db_path


@pytest.fixture(autouse=True)
def _isolate_lost_dismissed_tombstone(tmp_path, monkeypatch):
    """Redirect the banner-dismiss tombstone to ``tmp_path`` for every test.

    PR #169 follow-up — :func:`services.archive_queue.delete_source_gone`
    now writes a small JSON file alongside the GADGET_DIR runtime state.
    Without this fixture, every test that exercises the dismiss path
    would write ``archive_lost_dismissed.json`` to the real repo root
    (``GADGET_DIR`` resolves to ``<repo>`` when ``config`` is imported
    in-tree — see ``conftest.py``), polluting the working tree.
    """
    state_path = str(tmp_path / "archive_lost_dismissed.json")
    monkeypatch.setattr(
        archive_queue, '_lost_dismissed_path', lambda: state_path,
    )
    return state_path


@pytest.fixture
def sample_file(tmp_path):
    """Write a small file we can stat for size/mtime."""
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x" * 1234)
    return str(f)


# ---------------------------------------------------------------------------
# Priority inference
# ---------------------------------------------------------------------------

class TestInferPriority:
    @pytest.mark.parametrize("path,expected", [
        ('/mnt/gadget/part1-ro/TeslaCam/RecentClips/clip.mp4',
         PRIORITY_RECENT_CLIPS),
        ('/mnt/gadget/part1-ro/TeslaCam/recentclips/clip.mp4',
         PRIORITY_RECENT_CLIPS),
        # Backslash on Windows
        (r'C:\TeslaCam\RecentClips\clip.mp4', PRIORITY_RECENT_CLIPS),
        ('/mnt/gadget/part1-ro/TeslaCam/SentryClips/2026-01-01_12-00-00/'
         'front.mp4', PRIORITY_EVENTS),
        ('/mnt/gadget/part1-ro/TeslaCam/SavedClips/2026-01-01_12-00-00/'
         'back.mp4', PRIORITY_EVENTS),
        ('/mnt/gadget/part1-ro/TeslaCam/sentryclips/lower/clip.mp4',
         PRIORITY_EVENTS),
        ('/home/pi/ArchivedClips/2026-01-01/clip.mp4', PRIORITY_OTHER),
        ('/somewhere/else/random.mp4', PRIORITY_OTHER),
        ('', PRIORITY_OTHER),
    ])
    def test_infer_priority(self, path, expected):
        assert _infer_priority(path) == expected

    def test_recent_clips_beats_archive_when_both_present(self):
        # If both substrings appear (synthetic edge case), RecentClips wins
        # because it's checked first.
        path = '/var/ArchivedClips/RecentClips/clip.mp4'
        assert _infer_priority(path) == PRIORITY_RECENT_CLIPS


# ---------------------------------------------------------------------------
# Single-row enqueue
# ---------------------------------------------------------------------------

class TestEnqueueForArchive:
    def test_inserts_new_row(self, db, sample_file):
        assert enqueue_for_archive(sample_file, db_path=db) is True
        rows = list_queue(db_path=db)
        assert len(rows) == 1
        row = rows[0]
        assert row['source_path'] == sample_file
        assert row['status'] == 'pending'
        assert row['attempts'] == 0
        assert row['priority'] == PRIORITY_OTHER  # tmp_path isn't under any TeslaCam dir
        assert row['expected_size'] == 1234
        assert row['expected_mtime'] is not None
        assert row['enqueued_at'] is not None

    def test_idempotent_returns_false_on_dupe(self, db, sample_file):
        assert enqueue_for_archive(sample_file, db_path=db) is True
        # Second insert — still pending — returns False
        assert enqueue_for_archive(sample_file, db_path=db) is False
        rows = list_queue(db_path=db)
        assert len(rows) == 1

    def test_explicit_priority_overrides_inference(self, db, sample_file):
        assert enqueue_for_archive(
            sample_file, priority=1, db_path=db,
        ) is True
        rows = list_queue(db_path=db)
        assert rows[0]['priority'] == 1

    def test_rejects_empty_path(self, db):
        assert enqueue_for_archive('', db_path=db) is False
        assert enqueue_for_archive(None, db_path=db) is False  # type: ignore
        assert get_queue_status(db_path=db)['total'] == 0

    def test_missing_file_still_inserts_with_null_metadata(self, db, tmp_path):
        ghost = str(tmp_path / "does-not-exist.mp4")
        assert enqueue_for_archive(ghost, db_path=db) is True
        rows = list_queue(db_path=db)
        assert len(rows) == 1
        assert rows[0]['expected_size'] is None
        assert rows[0]['expected_mtime'] is None

    def test_priority_inferred_from_recent_clips_path(self, db, tmp_path):
        # Synthesize a RecentClips path (file doesn't need to exist for
        # priority inference)
        recent_dir = tmp_path / "RecentClips"
        recent_dir.mkdir()
        clip = recent_dir / "clip.mp4"
        clip.write_bytes(b"data")
        assert enqueue_for_archive(str(clip), db_path=db) is True
        rows = list_queue(db_path=db)
        assert rows[0]['priority'] == PRIORITY_RECENT_CLIPS


# ---------------------------------------------------------------------------
# Batch enqueue
# ---------------------------------------------------------------------------

class TestEnqueueManyForArchive:
    def test_batch_inserts(self, db, tmp_path):
        files = []
        for i in range(5):
            f = tmp_path / f"clip_{i}.mp4"
            f.write_bytes(b"x" * (100 + i))
            files.append(str(f))
        assert enqueue_many_for_archive(files, db_path=db) == 5
        assert get_queue_status(db_path=db)['pending'] == 5

    def test_batch_dedups_within_call(self, db, tmp_path):
        f = tmp_path / "clip.mp4"
        f.write_bytes(b"x")
        # Same path repeated 3 times — UNIQUE constraint dedups,
        # only 1 actually inserted.
        n = enqueue_many_for_archive([str(f), str(f), str(f)], db_path=db)
        assert n == 1
        assert get_queue_status(db_path=db)['pending'] == 1

    def test_batch_dedups_across_calls(self, db, tmp_path):
        f1 = tmp_path / "a.mp4"; f1.write_bytes(b"a")
        f2 = tmp_path / "b.mp4"; f2.write_bytes(b"b")
        assert enqueue_many_for_archive([str(f1), str(f2)], db_path=db) == 2
        # Second batch with one new + one duplicate: only the new one counts.
        f3 = tmp_path / "c.mp4"; f3.write_bytes(b"c")
        assert enqueue_many_for_archive([str(f1), str(f3)], db_path=db) == 1
        assert get_queue_status(db_path=db)['pending'] == 3

    def test_batch_skips_empty_paths(self, db, tmp_path):
        f = tmp_path / "clip.mp4"; f.write_bytes(b"x")
        assert enqueue_many_for_archive(
            ['', None, str(f), ''], db_path=db,  # type: ignore
        ) == 1

    def test_batch_empty_iterable_returns_zero(self, db):
        assert enqueue_many_for_archive([], db_path=db) == 0
        assert enqueue_many_for_archive(iter([]), db_path=db) == 0

    def test_batch_priority_override_applies_to_all(self, db, tmp_path):
        f1 = tmp_path / "a.mp4"; f1.write_bytes(b"a")
        f2 = tmp_path / "RecentClips"; f2.mkdir()
        f2_clip = f2 / "b.mp4"; f2_clip.write_bytes(b"b")
        # Override forces priority=1 regardless of inference
        enqueue_many_for_archive([str(f1), str(f2_clip)],
                                 priority=1, db_path=db)
        rows = list_queue(db_path=db)
        assert len(rows) == 2
        assert all(r['priority'] == 1 for r in rows)

    def test_batch_priority_inferred_when_none(self, db, tmp_path):
        # Mix of priorities: SentryClips event (P1 post-#178) and a
        # generic path (P3). Sorted output puts the event first.
        sentry_dir = tmp_path / "SentryClips" / "evt"
        sentry_dir.mkdir(parents=True)
        sentry_clip = sentry_dir / "front.mp4"
        sentry_clip.write_bytes(b"s")
        other = tmp_path / "other.mp4"
        other.write_bytes(b"o")
        enqueue_many_for_archive([str(sentry_clip), str(other)], db_path=db)
        # Sorted by priority
        rows = list_queue(db_path=db)
        assert rows[0]['priority'] == PRIORITY_EVENTS
        assert rows[0]['source_path'] == str(sentry_clip)
        assert rows[1]['priority'] == PRIORITY_OTHER


# ---------------------------------------------------------------------------
# Phase 2.8 — bulk-enqueue is transactional (issue #97 item 2.8)
# ---------------------------------------------------------------------------
#
# `_open_archive_conn` is opened in autocommit mode (`isolation_level=None`)
# so the helper itself never wraps writes in a transaction. Phase 2.8
# adds an explicit BEGIN IMMEDIATE / COMMIT around `executemany` in
# `enqueue_many_for_archive` so the whole batch lands in one fsync and
# is atomic on failure. These tests pin that contract.

class TestEnqueueManyAtomicity:
    """Bulk enqueue must be all-or-nothing.

    Before Phase 2.8 the connection was in autocommit mode and each
    row of `executemany` committed independently — a SQLite error
    half-way through left a partial batch in the DB. After 2.8 the
    explicit BEGIN/COMMIT (with ROLLBACK on exception) makes the batch
    atomic.
    """

    def test_rollback_on_executemany_error_leaves_db_unchanged(
        self, db, tmp_path, monkeypatch,
    ):
        """If `executemany` raises mid-batch, no rows from the batch
        survive."""
        # Pre-existing row that must not be disturbed.
        pre = tmp_path / "pre.mp4"
        pre.write_bytes(b"pre")
        assert enqueue_for_archive(str(pre), db_path=db) is True
        assert get_queue_status(db_path=db)['pending'] == 1

        # Build a batch and force `executemany` to raise.
        files = []
        for i in range(10):
            f = tmp_path / f"new_{i}.mp4"
            f.write_bytes(b"x")
            files.append(str(f))

        original_open = archive_queue._open_archive_conn

        class _RaisingExecuteMany:
            def __init__(self, real_conn):
                self._c = real_conn
                self.calls = 0

            def __getattr__(self, name):
                return getattr(self._c, name)

            def executemany(self, *a, **kw):
                self.calls += 1
                raise sqlite3.OperationalError(
                    "simulated mid-batch failure"
                )

        def _patched_open(path):
            real = original_open(path)
            return _RaisingExecuteMany(real)

        monkeypatch.setattr(archive_queue,
                            '_open_archive_conn', _patched_open)

        # The function catches sqlite3.Error and returns 0 (logging a warning).
        n = enqueue_many_for_archive(files, db_path=db)
        assert n == 0

        # Undo the monkeypatch so the post-state check uses the real
        # connection helper (the wrapper proxies attribute access via
        # ``__getattr__``, which doesn't expose ``__enter__``/``__exit__``
        # — those are dunder lookups bypassing ``__getattr__`` in Python).
        monkeypatch.undo()

        # Pre-existing row still there; none of the new batch landed.
        status = get_queue_status(db_path=db)
        assert status['pending'] == 1, (
            "Atomicity violated: a partial batch leaked into the DB. "
            f"Expected pending=1 (the pre-existing row), got {status}"
        )

    def test_no_partial_batch_visible_to_concurrent_reader(
        self, db, tmp_path,
    ):
        """A concurrent reader must see either zero or all rows of a
        batch — never a partial state."""
        files = []
        for i in range(50):
            f = tmp_path / f"clip_{i}.mp4"
            f.write_bytes(b"x")
            files.append(str(f))

        ready = threading.Event()
        stop = threading.Event()
        observed_partial = []

        def _writer():
            ready.wait()
            enqueue_many_for_archive(files, db_path=db)
            stop.set()

        def _reader():
            ready.wait()
            # Poll until the writer is done; record any non-zero,
            # non-final count.
            while not stop.is_set():
                n = get_queue_status(db_path=db)['pending']
                if 0 < n < len(files):
                    observed_partial.append(n)
                time.sleep(0.001)

        tw = threading.Thread(target=_writer)
        tr = threading.Thread(target=_reader)
        tw.start(); tr.start()
        ready.set()
        tw.join(timeout=10)
        tr.join(timeout=10)

        # Final state: all 50 rows landed.
        assert get_queue_status(db_path=db)['pending'] == len(files)
        # Reader never saw an in-between count. WAL + atomic commit
        # guarantees this; if it ever fails the bulk enqueue is back
        # in row-by-row mode.
        assert not observed_partial, (
            f"Reader observed partial batch counts {observed_partial} — "
            "bulk enqueue is not atomic"
        )

    def test_batch_uses_single_commit_not_n_commits(
        self, db, tmp_path, monkeypatch,
    ):
        """The contract: one BEGIN, one COMMIT, regardless of batch size.

        We instrument the sqlite Connection to count execute() calls
        with COMMIT in them. Before Phase 2.8 with `isolation_level=None`,
        each executemany row implicitly committed. After Phase 2.8
        there is exactly one explicit COMMIT.
        """
        files = []
        for i in range(20):
            f = tmp_path / f"clip_{i}.mp4"
            f.write_bytes(b"x")
            files.append(str(f))

        original_open = archive_queue._open_archive_conn
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

        monkeypatch.setattr(archive_queue,
                            '_open_archive_conn', _patched_open)

        n = enqueue_many_for_archive(files, db_path=db)
        assert n == 20
        assert len(begin_calls) == 1, (
            f"Expected exactly 1 BEGIN, got {len(begin_calls)}: {begin_calls}"
        )
        assert len(commit_calls) == 1, (
            f"Expected exactly 1 COMMIT, got {len(commit_calls)}: {commit_calls}"
        )
        # And the BEGIN should be IMMEDIATE so we don't upgrade locks.
        assert "IMMEDIATE" in begin_calls[0].upper(), (
            f"BEGIN must be IMMEDIATE to avoid lock upgrade races, "
            f"got {begin_calls[0]!r}"
        )

    def test_connection_closed_on_success(self, db, tmp_path, monkeypatch):
        """The bulk path must close its connection (don't leak FDs).

        With autocommit mode the `with conn:` context manager does NOT
        close the connection (sqlite3 only commits/rollbacks). We
        moved to an explicit try/finally with `conn.close()` — this
        test pins that invariant.
        """
        f = tmp_path / "clip.mp4"; f.write_bytes(b"x")

        original_open = archive_queue._open_archive_conn
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

        monkeypatch.setattr(archive_queue,
                            '_open_archive_conn', _patched_open)

        enqueue_many_for_archive([str(f)], db_path=db)
        assert len(opened) == 1
        assert opened[0].closed, (
            "enqueue_many_for_archive leaked its SQLite connection — "
            "the finally block must call conn.close()"
        )

    def test_connection_closed_on_failure(self, db, tmp_path, monkeypatch):
        """Even when executemany fails, the connection must be closed."""
        f = tmp_path / "clip.mp4"; f.write_bytes(b"x")

        original_open = archive_queue._open_archive_conn
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

        monkeypatch.setattr(archive_queue,
                            '_open_archive_conn', _patched_open)

        n = enqueue_many_for_archive([str(f)], db_path=db)
        assert n == 0
        assert opened[0].closed, (
            "enqueue_many_for_archive leaked its SQLite connection on "
            "the failure path — the finally block must always close"
        )

    def test_keyboard_interrupt_mid_batch_rolls_back(
        self, db, tmp_path, monkeypatch,
    ):
        """A non-sqlite exception (e.g. KeyboardInterrupt) mid-batch
        must still ROLLBACK — never leave a half-committed batch."""
        # Pre-existing row.
        pre = tmp_path / "pre.mp4"; pre.write_bytes(b"pre")
        enqueue_for_archive(str(pre), db_path=db)

        files = []
        for i in range(5):
            f = tmp_path / f"new_{i}.mp4"
            f.write_bytes(b"x")
            files.append(str(f))

        original_open = archive_queue._open_archive_conn

        class _InterruptingConn:
            def __init__(self, real):
                self._c = real

            def __getattr__(self, name):
                return getattr(self._c, name)

            def executemany(self, *a, **kw):
                raise KeyboardInterrupt("user pressed Ctrl-C")

        def _patched_open(path):
            return _InterruptingConn(original_open(path))

        monkeypatch.setattr(archive_queue,
                            '_open_archive_conn', _patched_open)

        # KeyboardInterrupt is a BaseException, not Exception — it
        # propagates out (we only catch sqlite3.Error). ROLLBACK is
        # invoked before the re-raise.
        with pytest.raises(KeyboardInterrupt):
            enqueue_many_for_archive(files, db_path=db)

        # See note in test_rollback_on_executemany_error_leaves_db_unchanged
        # for why we undo here.
        monkeypatch.undo()

        # Pre-existing row survived; new rows did NOT land.
        status = get_queue_status(db_path=db)
        assert status['pending'] == 1, (
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
        rows (conservative — typical is 50–100×) so the assertion
        survives test-runner noise but still catches a regression
        back to per-row commits.
        """
        # Two equivalent sets of paths.
        many_files = []
        for i in range(100):
            f = tmp_path / f"many_{i}.mp4"
            f.write_bytes(b"x")
            many_files.append(str(f))
        single_files = []
        for i in range(100):
            f = tmp_path / f"single_{i}.mp4"
            f.write_bytes(b"x")
            single_files.append(str(f))

        # Time per-row.
        t0 = time.perf_counter()
        for p in single_files:
            enqueue_for_archive(p, db_path=db)
        per_row = time.perf_counter() - t0

        # Time bulk.
        t0 = time.perf_counter()
        n = enqueue_many_for_archive(many_files, db_path=db)
        bulk = time.perf_counter() - t0
        assert n == 100

        # Bulk must be at least 3× faster. (Typical ratio is 50–100×;
        # 3× gives huge margin while still catching a regression to
        # row-by-row commits in the bulk path.)
        # If `bulk` is so close to zero that the ratio is unstable,
        # the test still passes — the per-row time is always > 0.
        assert per_row > bulk * 3, (
            f"Bulk enqueue is not transactional — per_row={per_row*1000:.1f}ms, "
            f"bulk={bulk*1000:.1f}ms (ratio {per_row/max(bulk,1e-9):.1f}×). "
            f"Expected bulk to be ≥3× faster than per-row."
        )


# ---------------------------------------------------------------------------
# Status counts
# ---------------------------------------------------------------------------

class TestGetQueueStatus:
    def test_empty_returns_zero_for_every_known_status(self, db):
        counts = get_queue_status(db_path=db)
        assert counts == {
            'pending': 0, 'claimed': 0, 'copied': 0,
            'source_gone': 0, 'skipped_stationary': 0,
            'error': 0, 'dead_letter': 0,
            'total': 0,
        }

    def test_counts_include_all_statuses(self, db, tmp_path):
        # Insert one row per status by hand
        conn = sqlite3.connect(db)
        for i, status in enumerate(
            ['pending', 'pending', 'claimed', 'copied',
             'source_gone', 'error', 'dead_letter']
        ):
            conn.execute(
                """
                INSERT INTO archive_queue
                    (source_path, status, enqueued_at)
                VALUES (?, ?, ?)
                """,
                (f"/tmp/x_{i}.mp4", status, "2026-05-11T09:00:00+00:00"),
            )
        conn.commit()
        conn.close()
        counts = get_queue_status(db_path=db)
        assert counts['pending'] == 2
        assert counts['claimed'] == 1
        assert counts['copied'] == 1
        assert counts['source_gone'] == 1
        assert counts['error'] == 1
        assert counts['dead_letter'] == 1
        assert counts['total'] == 7

    def test_unknown_status_folded_into_total_only(self, db):
        # Defensive: a stray status value doesn't blow up the API.
        conn = sqlite3.connect(db)
        conn.execute(
            """
            INSERT INTO archive_queue
                (source_path, status, enqueued_at)
            VALUES (?, ?, ?)
            """,
            ("/tmp/x.mp4", "weird-status", "2026-05-11T09:00:00+00:00"),
        )
        conn.commit()
        conn.close()
        counts = get_queue_status(db_path=db)
        assert counts['pending'] == 0
        assert counts['total'] == 1


# ---------------------------------------------------------------------------
# list_queue
# ---------------------------------------------------------------------------

class TestListQueue:
    def test_empty_returns_empty_list(self, db):
        assert list_queue(db_path=db) == []

    def test_zero_or_negative_limit_returns_empty(self, db, tmp_path):
        f = tmp_path / "x.mp4"; f.write_bytes(b"x")
        enqueue_for_archive(str(f), db_path=db)
        assert list_queue(limit=0, db_path=db) == []
        assert list_queue(limit=-1, db_path=db) == []

    def test_status_filter(self, db, tmp_path):
        # 2 pending, 1 copied
        for name in ('a', 'b'):
            f = tmp_path / f"{name}.mp4"; f.write_bytes(b"x")
            enqueue_for_archive(str(f), db_path=db)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE archive_queue SET status='copied' "
                     "WHERE source_path LIKE '%a.mp4'")
        conn.commit()
        conn.close()
        pending = list_queue(status='pending', db_path=db)
        copied = list_queue(status='copied', db_path=db)
        assert len(pending) == 1
        assert pending[0]['source_path'].endswith('b.mp4')
        assert len(copied) == 1
        assert copied[0]['source_path'].endswith('a.mp4')

    def test_sorted_by_priority_then_mtime(self, db, tmp_path):
        # Three files with controlled priorities (issue #178: events
        # are P1 and drain first, RecentClips are P2).
        sentry_dir = tmp_path / "SentryClips" / "evt"
        sentry_dir.mkdir(parents=True)
        s1 = sentry_dir / "s1.mp4"; s1.write_bytes(b"x")
        time.sleep(0.01)  # mtime ordering
        s2 = sentry_dir / "s2.mp4"; s2.write_bytes(b"x")
        other = tmp_path / "other.mp4"; other.write_bytes(b"x")
        enqueue_many_for_archive(
            [str(other), str(s2), str(s1)], db_path=db,
        )
        rows = list_queue(db_path=db)
        # Events (priority 1) come first, oldest mtime first within tier.
        assert rows[0]['source_path'] == str(s1)
        assert rows[1]['source_path'] == str(s2)
        assert rows[2]['source_path'] == str(other)

    def test_limit_caps_results(self, db, tmp_path):
        for i in range(10):
            f = tmp_path / f"c_{i}.mp4"; f.write_bytes(b"x")
            enqueue_for_archive(str(f), db_path=db)
        assert len(list_queue(limit=3, db_path=db)) == 3
        assert len(list_queue(limit=20, db_path=db)) == 10

    def test_null_mtime_sorted_after_real_mtimes(self, db, tmp_path):
        ghost = str(tmp_path / "ghost.mp4")
        real = tmp_path / "real.mp4"; real.write_bytes(b"x")
        # Same priority — ghost has NULL mtime, real has a real mtime
        enqueue_many_for_archive(
            [ghost, str(real)], priority=2, db_path=db,
        )
        rows = list_queue(db_path=db)
        # Real comes first (NULLs sorted last)
        assert rows[0]['source_path'] == str(real)
        assert rows[1]['source_path'] == ghost


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

class TestConcurrentEnqueue:
    def test_many_threads_no_exceptions_correct_count(self, db, tmp_path):
        # 10 threads, each enqueueing 50 distinct paths plus 50 shared paths.
        # Shared paths must dedup; distinct paths must all land.
        shared = []
        for i in range(50):
            f = tmp_path / f"shared_{i}.mp4"; f.write_bytes(b"x")
            shared.append(str(f))

        results = []
        errors = []

        def worker(worker_id: int):
            try:
                # Distinct paths for this worker
                distinct = []
                for i in range(50):
                    f = tmp_path / f"w{worker_id}_{i}.mp4"
                    f.write_bytes(b"x")
                    distinct.append(str(f))
                count = enqueue_many_for_archive(distinct, db_path=db)
                count += enqueue_many_for_archive(shared, db_path=db)
                results.append(count)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == [], f"Workers raised: {errors}"
        # Exactly: 10 workers × 50 distinct = 500 + 50 shared = 550 unique rows.
        assert get_queue_status(db_path=db)['pending'] == 550
        # Total inserted across all workers' return values: distinct (10×50=500)
        # + shared (only the first worker to win each row counts; 50 total).
        assert sum(results) == 550

    def test_concurrent_single_enqueue_dedups_correctly(self, db, tmp_path):
        f = tmp_path / "race.mp4"; f.write_bytes(b"x")
        path = str(f)
        results: list = []
        errors: list = []

        def worker():
            try:
                results.append(enqueue_for_archive(path, db_path=db))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        assert results.count(True) == 1
        assert results.count(False) == 19
        assert get_queue_status(db_path=db)['pending'] == 1


# ---------------------------------------------------------------------------
# Default db_path resolution (sanity — mocked config import)
# ---------------------------------------------------------------------------

class TestDefaultDbPath:
    def test_resolves_via_config_when_not_passed(self, tmp_path, monkeypatch):
        """When ``db_path`` is omitted, the module reads ``MAPPING_DB_PATH``
        from ``config``. We patch the import to point at a tmp DB.
        """
        db_path = str(tmp_path / "default.db")
        _init_db(db_path).close()

        # Patch config.MAPPING_DB_PATH
        import config as _cfg
        monkeypatch.setattr(_cfg, 'MAPPING_DB_PATH', db_path)

        f = tmp_path / "x.mp4"; f.write_bytes(b"x")
        # No db_path arg
        assert enqueue_for_archive(str(f)) is True
        # Status query also resolves the same way
        assert get_queue_status()['pending'] == 1


# ---------------------------------------------------------------------------
# Worker-side helpers (Phase 2b — issue #76)
# ---------------------------------------------------------------------------
#
# These cover the state-transition helpers consumed by ``archive_worker``:
# claim_next_for_worker, mark_copied, mark_source_gone, release_claim,
# mark_failed, recover_stale_claims. The worker's own loop is exercised
# in ``test_archive_worker.py``; here we pin the SQL semantics in
# isolation so a regression in the queue layer surfaces immediately.

from services.archive_queue import (  # noqa: E402
    claim_next_for_worker,
    delete_skipped_stationary,
    mark_copied,
    mark_failed,
    mark_skipped_stationary,
    mark_source_gone,
    recover_stale_claims,
    release_claim,
)


class TestClaimNextForWorker:
    def test_returns_none_on_empty_queue(self, db):
        assert claim_next_for_worker('w1', db_path=db) is None

    def test_claims_single_pending_row(self, db, sample_file):
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        assert row is not None
        assert row['source_path'] == sample_file
        assert row['status'] == 'claimed'
        assert row['claimed_by'] == 'w1'
        assert row['claimed_at'] is not None

    def test_picks_priority_one_first(self, db, tmp_path):
        # Mix of priorities (issue #178: P1=events, P2=RecentClips,
        # P3=other). P3 is enqueued first, then P2, then P1. Worker
        # MUST pick P1 (event) first regardless of insertion order.
        p3 = tmp_path / "other.mp4"; p3.write_bytes(b'x')
        p2 = tmp_path / "RecentClips" / "front.mp4"
        p2.parent.mkdir(parents=True); p2.write_bytes(b'x')
        p1 = tmp_path / "SentryClips" / "evt" / "front.mp4"
        p1.parent.mkdir(parents=True); p1.write_bytes(b'x')
        enqueue_for_archive(str(p3), db_path=db)
        enqueue_for_archive(str(p2), db_path=db)
        enqueue_for_archive(str(p1), db_path=db)

        row = claim_next_for_worker('w1', db_path=db)
        assert row['source_path'] == str(p1)
        row = claim_next_for_worker('w1', db_path=db)
        assert row['source_path'] == str(p2)
        row = claim_next_for_worker('w1', db_path=db)
        assert row['source_path'] == str(p3)

    def test_picks_oldest_mtime_within_priority(self, db, tmp_path):
        a = tmp_path / "SentryClips" / "evt-a" / "front.mp4"
        b = tmp_path / "SentryClips" / "evt-b" / "front.mp4"
        a.parent.mkdir(parents=True); a.write_bytes(b'a')
        b.parent.mkdir(parents=True); b.write_bytes(b'b')
        # Make 'a' older than 'b' on disk.
        os.utime(str(a), (1000.0, 1000.0))
        os.utime(str(b), (2000.0, 2000.0))
        enqueue_for_archive(str(b), db_path=db)
        enqueue_for_archive(str(a), db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        assert row['source_path'] == str(a)

    def test_skips_claimed_row(self, db, sample_file):
        enqueue_for_archive(sample_file, db_path=db)
        first = claim_next_for_worker('w1', db_path=db)
        assert first is not None
        # No more pending rows — second claim must return None.
        assert claim_next_for_worker('w2', db_path=db) is None

    def test_two_workers_race_only_one_wins(self, db, sample_file):
        enqueue_for_archive(sample_file, db_path=db)
        results = []
        barrier = threading.Barrier(2)

        def claimer(name):
            barrier.wait()
            r = claim_next_for_worker(name, db_path=db)
            results.append(r)

        t1 = threading.Thread(target=claimer, args=('w1',))
        t2 = threading.Thread(target=claimer, args=('w2',))
        t1.start(); t2.start(); t1.join(); t2.join()
        winners = [r for r in results if r is not None]
        losers = [r for r in results if r is None]
        assert len(winners) == 1, (
            f"Expected exactly one worker to win; got {results}"
        )
        assert len(losers) == 1


class TestMarkCopied:
    def test_marks_claimed_row_as_copied(self, db, sample_file, tmp_path):
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        dest = str(tmp_path / "dest.mp4")
        assert mark_copied(row['id'], dest, db_path=db) is True
        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'copied'
        assert rows[0]['dest_path'] == dest
        assert rows[0]['copied_at'] is not None

    def test_returns_false_for_unknown_id(self, db):
        assert mark_copied(999999, '/x', db_path=db) is False

    def test_returns_false_for_zero_id(self, db):
        assert mark_copied(0, '/x', db_path=db) is False


class TestMarkSourceGone:
    def test_marks_claimed_row(self, db, sample_file):
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        assert mark_source_gone(row['id'], db_path=db) is True
        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'source_gone'
        # last_error must be cleared — source-gone is not an error.
        assert rows[0]['last_error'] is None

    def test_returns_false_for_unknown_id(self, db):
        assert mark_source_gone(999999, db_path=db) is False

    def test_refuses_to_mark_pending_row(self, db, sample_file):
        """PR #134 review-fix: precondition tightening.

        ``mark_source_gone`` MUST require ``status='claimed'`` so the
        ``count_source_gone_recent`` 24-hour-window query (which filters
        on ``claimed_at``) cannot silently undercount a hypothetical
        out-of-flow caller's row that has ``claimed_at IS NULL``.
        """
        assert enqueue_for_archive(sample_file, db_path=db) is True
        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'pending'
        row_id = rows[0]['id']
        # Row is `pending`, claimed_at IS NULL — must refuse.
        assert mark_source_gone(row_id, db_path=db) is False
        rows = list_queue(db_path=db)
        # Row stays pending; status was NOT mutated.
        assert rows[0]['status'] == 'pending'
        assert rows[0]['claimed_at'] is None

    def test_refuses_to_mark_already_copied_row(self, db, sample_file, tmp_path):
        """Once a row is `copied`, mark_source_gone must not regress it."""
        dest = tmp_path / 'dest.mp4'
        dest.write_bytes(b'x')
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        assert mark_copied(row['id'], str(dest), db_path=db) is True
        # Row is now `copied` — mark_source_gone must refuse.
        assert mark_source_gone(row['id'], db_path=db) is False
        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'copied'

    def test_refuses_to_mark_dead_letter_row(self, db, sample_file):
        """A failed (dead-letter) row must not be re-classifiable."""
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        # mark_failed transitions to dead_letter once attempts hit cap.
        # Force the cap quickly by pre-bumping attempts via direct DB.
        with sqlite3.connect(db) as c:
            c.execute(
                "UPDATE archive_queue SET attempts = 99 WHERE id = ?",
                (row['id'],),
            )
        mark_failed(row['id'], 'simulated', db_path=db)
        rows = list_queue(db_path=db)
        # Confirm the row is now dead_letter (precondition for the test).
        assert rows[0]['status'] == 'dead_letter'
        # Now mark_source_gone must refuse.
        assert mark_source_gone(row['id'], db_path=db) is False
        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'dead_letter'


class TestCountSourceGoneRecent:
    """Phase 4.3 (#101) — files-lost banner data source."""

    def test_zero_when_no_source_gone_rows(self, db, sample_file):
        # Just claim+copy a row, no source_gone events.
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        archive_queue.mark_copied(row['id'], '/tmp/dest.mp4', db_path=db)
        assert archive_queue.count_source_gone_recent(24, db_path=db) == 0

    def test_counts_recent_source_gone(self, db, tmp_path):
        # Three source_gone rows, all recent.
        for i in range(3):
            f = tmp_path / f"clip_{i}.mp4"
            f.write_text("x")
            enqueue_for_archive(str(f), db_path=db)
            row = claim_next_for_worker(f'w{i}', db_path=db)
            mark_source_gone(row['id'], db_path=db)
        assert archive_queue.count_source_gone_recent(24, db_path=db) == 3

    def test_excludes_non_source_gone(self, db, tmp_path):
        f1 = tmp_path / "a.mp4"
        f1.write_text("x")
        enqueue_for_archive(str(f1), db_path=db)
        r1 = claim_next_for_worker('w1', db_path=db)
        mark_source_gone(r1['id'], db_path=db)

        f2 = tmp_path / "b.mp4"
        f2.write_text("x")
        enqueue_for_archive(str(f2), db_path=db)
        r2 = claim_next_for_worker('w2', db_path=db)
        archive_queue.mark_copied(r2['id'], '/tmp/b.mp4', db_path=db)

        # Only the source_gone row counts.
        assert archive_queue.count_source_gone_recent(24, db_path=db) == 1

    def test_excludes_old_source_gone(self, db, sample_file):
        # Insert a source_gone row but backdate claimed_at to 48 h ago.
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        mark_source_gone(row['id'], db_path=db)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE archive_queue SET claimed_at = "
                "datetime('now', '-48 hours') WHERE id = ?",
                (row['id'],),
            )
            conn.commit()
        assert archive_queue.count_source_gone_recent(24, db_path=db) == 0
        # Wider window picks it back up.
        assert archive_queue.count_source_gone_recent(72, db_path=db) == 1

    def test_zero_hours_returns_zero(self, db, sample_file):
        # Defensive: a 0-hour window must short-circuit, not return all.
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        mark_source_gone(row['id'], db_path=db)
        assert archive_queue.count_source_gone_recent(0, db_path=db) == 0

    def test_handles_iso_with_t_separator(self, db, sample_file):
        """``_iso_now`` writes ``YYYY-MM-DDTHH:MM:SS+00:00`` — the
        ``T`` separator and tz suffix must not break the SQLite
        ``strftime('%s', ...)`` filter.
        """
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        mark_source_gone(row['id'], db_path=db)
        # Confirm the stored claimed_at is the ISO-T format.
        with sqlite3.connect(db) as conn:
            ts = conn.execute(
                "SELECT claimed_at FROM archive_queue WHERE id = ?",
                (row['id'],),
            ).fetchone()[0]
        assert 'T' in ts
        assert '+' in ts or 'Z' in ts
        assert archive_queue.count_source_gone_recent(24, db_path=db) == 1

    def test_returns_zero_on_db_failure(self, db, monkeypatch):
        def boom(*a, **kw):
            raise sqlite3.OperationalError("disk I/O error")
        monkeypatch.setattr(archive_queue, '_open_archive_conn', boom)
        # Must never raise — health card poll on a broken DB should
        # show 0 lost rather than 500 the dashboard.
        assert archive_queue.count_source_gone_recent(24, db_path=db) == 0


class TestDeleteSourceGone:
    """Issue #163 — Dismiss button for the "Footage may have been lost"
    home-page banner. The companion to ``count_source_gone_recent``;
    deletes the rows the banner counts so the operator can clear the
    24-h banner immediately instead of waiting for the rows to age out.
    """

    def _seed_source_gone(self, db, tmp_path, n):
        """Insert ``n`` ``source_gone`` rows; return list of row ids."""
        ids = []
        for i in range(n):
            f = tmp_path / f"sg_{i}.mp4"
            f.write_text("x")
            enqueue_for_archive(str(f), db_path=db)
            row = claim_next_for_worker(f'w{i}', db_path=db)
            mark_source_gone(row['id'], db_path=db)
            ids.append(row['id'])
        return ids

    def test_returns_zero_when_table_empty(self, db):
        assert archive_queue.delete_source_gone(db_path=db) == 0

    def test_deletes_every_source_gone_row_by_default(self, db, tmp_path):
        self._seed_source_gone(db, tmp_path, 5)
        assert archive_queue.count_source_gone_recent(
            24, db_path=db) == 5
        assert archive_queue.delete_source_gone(db_path=db) == 5
        assert archive_queue.count_source_gone_recent(
            24, db_path=db) == 0
        # And the rows are physically gone, not just status-changed.
        with sqlite3.connect(db) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM archive_queue "
                "WHERE status = 'source_gone'"
            ).fetchone()[0]
        assert n == 0

    def test_preserves_non_source_gone_rows(self, db, tmp_path):
        # 3 source_gone + 1 pending + 1 copied + 1 dead_letter.
        self._seed_source_gone(db, tmp_path, 3)

        f_pending = tmp_path / "pending.mp4"
        f_pending.write_text("x")
        enqueue_for_archive(str(f_pending), db_path=db)

        f_copied = tmp_path / "copied.mp4"
        f_copied.write_text("x")
        enqueue_for_archive(str(f_copied), db_path=db)
        r_copied = claim_next_for_worker('w_copied', db_path=db)
        archive_queue.mark_copied(
            r_copied['id'], '/tmp/copied.mp4', db_path=db)

        f_dl = tmp_path / "dl.mp4"
        f_dl.write_text("x")
        enqueue_for_archive(str(f_dl), db_path=db)
        r_dl = claim_next_for_worker('w_dl', db_path=db)
        with sqlite3.connect(db) as c:
            c.execute(
                "UPDATE archive_queue SET attempts = 99 WHERE id = ?",
                (r_dl['id'],),
            )
        mark_failed(r_dl['id'], 'simulated', db_path=db)

        # Sanity: 3 source_gone, 1 pending, 1 copied, 1 dead_letter.
        counts = archive_queue.get_queue_status(db_path=db)
        assert counts['source_gone'] == 3
        assert counts['pending'] == 1
        assert counts['copied'] == 1
        assert counts['dead_letter'] == 1

        # Delete just the source_gone rows.
        assert archive_queue.delete_source_gone(db_path=db) == 3

        counts_after = archive_queue.get_queue_status(db_path=db)
        assert counts_after['source_gone'] == 0
        assert counts_after['pending'] == 1
        assert counts_after['copied'] == 1
        assert counts_after['dead_letter'] == 1

    def test_older_than_hours_keeps_recent(self, db, tmp_path):
        # 2 source_gone rows: one fresh, one backdated 48 h.
        ids = self._seed_source_gone(db, tmp_path, 2)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE archive_queue SET claimed_at = "
                "datetime('now', '-48 hours') WHERE id = ?",
                (ids[0],),
            )
            conn.commit()

        # older_than_hours=24 should delete only the 48 h-old row.
        assert archive_queue.delete_source_gone(
            older_than_hours=24, db_path=db) == 1

        # The recent row survives and is still counted.
        assert archive_queue.count_source_gone_recent(
            24, db_path=db) == 1
        with sqlite3.connect(db) as conn:
            remaining = conn.execute(
                "SELECT id FROM archive_queue "
                "WHERE status = 'source_gone'"
            ).fetchall()
        assert len(remaining) == 1
        assert remaining[0][0] == ids[1]

    def test_returns_zero_on_db_failure(self, db, monkeypatch):
        def boom(*a, **kw):
            raise sqlite3.OperationalError("disk I/O error")
        monkeypatch.setattr(archive_queue, '_open_archive_conn', boom)
        # Must never raise — Dismiss click on a broken DB just returns
        # 0 rather than 500-ing the request.
        assert archive_queue.delete_source_gone(db_path=db) == 0

    def test_idempotent_second_call_returns_zero(self, db, tmp_path):
        self._seed_source_gone(db, tmp_path, 2)
        assert archive_queue.delete_source_gone(db_path=db) == 2
        # Banner is now 0; clicking Dismiss again must be safe.
        assert archive_queue.delete_source_gone(db_path=db) == 0


class TestMarkSkippedStationary:
    """Issue #167 sub-deliverable 2 — terminal transition for the
    ``archive.skip_stationary_recent_clips`` peek-and-skip path. Mirror
    of ``TestMarkSourceGone``: the same precondition/return-value
    contract so the two terminal "we did not copy" buckets stay
    parallel.
    """

    def test_marks_claimed_row(self, db, sample_file):
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        assert mark_skipped_stationary(row['id'], db_path=db) is True
        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'skipped_stationary'
        # last_error must be cleared — skipping is not an error.
        assert rows[0]['last_error'] is None

    def test_returns_false_for_unknown_id(self, db):
        assert mark_skipped_stationary(999999, db_path=db) is False

    def test_refuses_to_mark_pending_row(self, db, sample_file):
        """Same precondition as ``mark_source_gone`` — a row must be
        ``claimed`` before it can transition to ``skipped_stationary``,
        so ``count_skipped_stationary_recent``'s ``claimed_at`` filter
        is never NULL.
        """
        assert enqueue_for_archive(sample_file, db_path=db) is True
        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'pending'
        row_id = rows[0]['id']
        assert mark_skipped_stationary(row_id, db_path=db) is False
        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'pending'
        assert rows[0]['claimed_at'] is None

    def test_refuses_to_mark_copied_row(self, db, sample_file, tmp_path):
        dest = tmp_path / 'dest.mp4'
        dest.write_bytes(b'x')
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        assert mark_copied(row['id'], str(dest), db_path=db) is True
        # Row is now ``copied`` — a stale skip-mark must not regress it.
        assert mark_skipped_stationary(row['id'], db_path=db) is False
        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'copied'

    def test_refuses_to_mark_dead_letter_row(self, db, sample_file):
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        with sqlite3.connect(db) as c:
            c.execute(
                "UPDATE archive_queue SET attempts = 99 WHERE id = ?",
                (row['id'],),
            )
        mark_failed(row['id'], 'simulated', db_path=db)
        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'dead_letter'
        assert mark_skipped_stationary(row['id'], db_path=db) is False
        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'dead_letter'

    def test_get_queue_status_includes_skipped_stationary_bucket(
            self, db, sample_file):
        # The new key must appear in the always-zero default keys
        # returned by get_queue_status, so the Settings card never
        # silently drops the metric.
        counts = get_queue_status(db_path=db)
        assert 'skipped_stationary' in counts
        assert counts['skipped_stationary'] == 0
        # And after a real skip-mark, the count reflects the new row.
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        mark_skipped_stationary(row['id'], db_path=db)
        counts = get_queue_status(db_path=db)
        assert counts['skipped_stationary'] == 1


class TestCountSkippedStationaryRecent:
    """Mirror of ``TestCountSourceGoneRecent`` for the new bucket."""

    def test_zero_when_no_skipped_rows(self, db, sample_file, tmp_path):
        # Mark a row source_gone — must NOT count toward skipped.
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        mark_source_gone(row['id'], db_path=db)
        assert archive_queue.count_skipped_stationary_recent(
            24, db_path=db) == 0

    def test_counts_recent_skipped_rows(self, db, tmp_path):
        for i in range(3):
            f = tmp_path / f"clip_{i}.mp4"
            f.write_text("x")
            enqueue_for_archive(str(f), db_path=db)
            row = claim_next_for_worker(f'w{i}', db_path=db)
            mark_skipped_stationary(row['id'], db_path=db)
        assert archive_queue.count_skipped_stationary_recent(
            24, db_path=db) == 3

    def test_excludes_source_gone(self, db, tmp_path):
        # The two terminal buckets must NEVER cross-contaminate.
        f1 = tmp_path / "skip.mp4"
        f1.write_text("x")
        enqueue_for_archive(str(f1), db_path=db)
        r1 = claim_next_for_worker('w1', db_path=db)
        mark_skipped_stationary(r1['id'], db_path=db)

        f2 = tmp_path / "gone.mp4"
        f2.write_text("x")
        enqueue_for_archive(str(f2), db_path=db)
        r2 = claim_next_for_worker('w2', db_path=db)
        mark_source_gone(r2['id'], db_path=db)

        # Only the skipped row counts toward skipped_stationary.
        assert archive_queue.count_skipped_stationary_recent(
            24, db_path=db) == 1
        # And the source-gone row counts only toward source_gone.
        assert archive_queue.count_source_gone_recent(
            24, db_path=db) == 1

    def test_excludes_old_skipped_rows(self, db, sample_file):
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        mark_skipped_stationary(row['id'], db_path=db)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE archive_queue SET claimed_at = "
                "datetime('now', '-48 hours') WHERE id = ?",
                (row['id'],),
            )
            conn.commit()
        assert archive_queue.count_skipped_stationary_recent(
            24, db_path=db) == 0
        # Wider window picks it back up.
        assert archive_queue.count_skipped_stationary_recent(
            72, db_path=db) == 1

    def test_zero_hours_returns_zero(self, db, sample_file):
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        mark_skipped_stationary(row['id'], db_path=db)
        assert archive_queue.count_skipped_stationary_recent(
            0, db_path=db) == 0

    def test_returns_zero_on_db_failure(self, db, monkeypatch):
        def boom(*a, **kw):
            raise sqlite3.OperationalError("disk I/O error")
        monkeypatch.setattr(archive_queue, '_open_archive_conn', boom)
        assert archive_queue.count_skipped_stationary_recent(
            24, db_path=db) == 0


class TestDeleteSkippedStationary:
    """Issue #167 sub-deliverable 2 — clear-the-tally helper for the
    Settings UI. Mirror of ``TestDeleteSourceGone``.
    """

    def _seed_skipped(self, db, tmp_path, n):
        ids = []
        for i in range(n):
            f = tmp_path / f"sk_{i}.mp4"
            f.write_text("x")
            enqueue_for_archive(str(f), db_path=db)
            row = claim_next_for_worker(f'w{i}', db_path=db)
            mark_skipped_stationary(row['id'], db_path=db)
            ids.append(row['id'])
        return ids

    def test_returns_zero_when_table_empty(self, db):
        assert delete_skipped_stationary(db_path=db) == 0

    def test_deletes_every_skipped_row_by_default(self, db, tmp_path):
        self._seed_skipped(db, tmp_path, 5)
        assert archive_queue.count_skipped_stationary_recent(
            24, db_path=db) == 5
        assert delete_skipped_stationary(db_path=db) == 5
        assert archive_queue.count_skipped_stationary_recent(
            24, db_path=db) == 0
        with sqlite3.connect(db) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM archive_queue "
                "WHERE status = 'skipped_stationary'"
            ).fetchone()[0]
        assert n == 0

    def test_preserves_other_status_rows(self, db, tmp_path):
        # 3 skipped + 1 pending + 1 source_gone — only skipped goes.
        self._seed_skipped(db, tmp_path, 3)

        f_p = tmp_path / "p.mp4"
        f_p.write_text("x")
        enqueue_for_archive(str(f_p), db_path=db)

        f_sg = tmp_path / "sg.mp4"
        f_sg.write_text("x")
        enqueue_for_archive(str(f_sg), db_path=db)
        r_sg = claim_next_for_worker('w_sg', db_path=db)
        mark_source_gone(r_sg['id'], db_path=db)

        assert delete_skipped_stationary(db_path=db) == 3
        counts = get_queue_status(db_path=db)
        assert counts['skipped_stationary'] == 0
        assert counts['pending'] == 1
        assert counts['source_gone'] == 1

    def test_older_than_hours_keeps_recent(self, db, tmp_path):
        ids = self._seed_skipped(db, tmp_path, 2)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE archive_queue SET claimed_at = "
                "datetime('now', '-48 hours') WHERE id = ?",
                (ids[0],),
            )
            conn.commit()
        assert delete_skipped_stationary(
            older_than_hours=24, db_path=db) == 1
        assert archive_queue.count_skipped_stationary_recent(
            24, db_path=db) == 1

    def test_returns_zero_on_db_failure(self, db, monkeypatch):
        def boom(*a, **kw):
            raise sqlite3.OperationalError("disk I/O error")
        monkeypatch.setattr(archive_queue, '_open_archive_conn', boom)
        assert delete_skipped_stationary(db_path=db) == 0


class TestReleaseClaim:
    def test_returns_to_pending_without_metadata(self, db, sample_file):
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        assert release_claim(row['id'], db_path=db) is True
        # Now claimable again by another worker.
        again = claim_next_for_worker('w2', db_path=db)
        assert again is not None
        assert again['claimed_by'] == 'w2'

    def test_refreshes_metadata_when_provided(self, db, sample_file):
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        assert release_claim(
            row['id'], expected_size=999, expected_mtime=1234.5, db_path=db,
        ) is True
        rows = list_queue(db_path=db)
        assert rows[0]['expected_size'] == 999
        assert rows[0]['expected_mtime'] == 1234.5
        assert rows[0]['claimed_at'] is None
        assert rows[0]['claimed_by'] is None

    def test_does_not_burn_attempt(self, db, sample_file):
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        assert release_claim(row['id'], db_path=db) is True
        rows = list_queue(db_path=db)
        # release_claim is not an error — attempts stays at 0.
        assert rows[0]['attempts'] == 0


class TestMarkFailed:
    def test_first_failure_returns_pending(self, db, sample_file):
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        status = mark_failed(row['id'], 'synthetic', max_attempts=3, db_path=db)
        assert status == 'pending'
        rows = list_queue(db_path=db)
        assert rows[0]['attempts'] == 1
        assert rows[0]['last_error'] == 'synthetic'
        assert rows[0]['status'] == 'pending'
        assert rows[0]['claimed_at'] is None

    def test_dead_letter_at_max_attempts(self, db, sample_file):
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        # Three failures with max=3 → final transition to dead_letter.
        for _ in range(2):
            assert mark_failed(
                row['id'], 'oops', max_attempts=3, db_path=db,
            ) == 'pending'
            row = claim_next_for_worker('w1', db_path=db)
        status = mark_failed(
            row['id'], 'final', max_attempts=3, db_path=db,
        )
        assert status == 'dead_letter'
        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'dead_letter'
        assert rows[0]['attempts'] == 3
        assert rows[0]['last_error'] == 'final'

    def test_truncates_long_error_string(self, db, sample_file):
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        big = 'x' * 10000
        mark_failed(row['id'], big, max_attempts=5, db_path=db)
        rows = list_queue(db_path=db)
        assert len(rows[0]['last_error']) == 4096

    def test_unknown_id_returns_error(self, db):
        assert mark_failed(99999, 'x', db_path=db) == 'error'

    def test_zero_id_returns_error(self, db):
        assert mark_failed(0, 'x', db_path=db) == 'error'


class TestRecoverStaleClaims:
    def test_resets_old_claimed_rows(self, db, sample_file):
        from datetime import datetime, timedelta, timezone
        enqueue_for_archive(sample_file, db_path=db)
        # Hand-roll a stale claim by writing an old timestamp.
        old_ts = (
            datetime.now(timezone.utc) - timedelta(seconds=3600)
        ).isoformat()
        with sqlite3.connect(db) as c:
            c.execute(
                """UPDATE archive_queue
                      SET status='claimed', claimed_at=?, claimed_by='zombie'""",
                (old_ts,),
            )
        recovered = recover_stale_claims(max_age_seconds=600.0, db_path=db)
        assert recovered == 1
        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'pending'
        assert rows[0]['claimed_at'] is None

    def test_leaves_recent_claims_alone(self, db, sample_file):
        enqueue_for_archive(sample_file, db_path=db)
        # Fresh claim — within the 600s window.
        claim_next_for_worker('w1', db_path=db)
        recovered = recover_stale_claims(max_age_seconds=600.0, db_path=db)
        assert recovered == 0
        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'claimed'

    def test_recovers_null_claimed_at(self, db, sample_file):
        # Defensive: a row stuck in claimed with NULL claimed_at also
        # gets recovered (treated as infinitely old).
        enqueue_for_archive(sample_file, db_path=db)
        with sqlite3.connect(db) as c:
            c.execute(
                """UPDATE archive_queue
                      SET status='claimed', claimed_at=NULL, claimed_by='x'"""
            )
        recovered = recover_stale_claims(max_age_seconds=60.0, db_path=db)
        assert recovered == 1


# ---------------------------------------------------------------------------
# Phase 2.10 — _atomic_archive_op transactional context manager
# ---------------------------------------------------------------------------

class TestAtomicArchiveOp:
    """The Phase 2.10 transactional helper.

    Contract:
      * BEGIN IMMEDIATE on enter (acquires write lock up front)
      * COMMIT on clean exit
      * ROLLBACK on any BaseException (including KeyboardInterrupt)
      * Connection always closed in finally
      * Re-raises the original exception
    """

    def test_commit_on_success(self, db, sample_file):
        """Successful body commits all writes."""
        with archive_queue._atomic_archive_op(db) as conn:
            conn.execute(
                """INSERT INTO archive_queue
                       (source_path, priority, status, enqueued_at)
                   VALUES (?, ?, 'pending', ?)""",
                (sample_file, 3, '2026-01-01T00:00:00+00:00'),
            )
        # Visible in a fresh connection.
        rows = list_queue(db_path=db)
        assert len(rows) == 1
        assert rows[0]['source_path'] == sample_file

    def test_rollback_on_sqlite_error(self, db, sample_file, tmp_path):
        """sqlite3.Error inside the body rolls back and re-raises."""
        # Pre-existing row that must survive.
        pre = tmp_path / "pre.mp4"
        pre.write_bytes(b"pre")
        enqueue_for_archive(str(pre), db_path=db)
        assert get_queue_status(db_path=db)['pending'] == 1

        class Boom(sqlite3.OperationalError):
            pass

        with pytest.raises(Boom):
            with archive_queue._atomic_archive_op(db) as conn:
                conn.execute(
                    """INSERT INTO archive_queue
                           (source_path, priority, status, enqueued_at)
                       VALUES (?, ?, 'pending', ?)""",
                    (sample_file, 3, '2026-01-01T00:00:00+00:00'),
                )
                raise Boom("simulated")
        # Pre-existing row still present, new one rolled back.
        rows = list_queue(db_path=db)
        assert len(rows) == 1
        assert rows[0]['source_path'] == str(pre)

    def test_rollback_on_keyboard_interrupt(self, db, sample_file, tmp_path):
        """BaseException (e.g. KeyboardInterrupt) also rolls back and
        re-raises — same contract as Phase 2.8 enqueue_many."""
        pre = tmp_path / "pre.mp4"
        pre.write_bytes(b"pre")
        enqueue_for_archive(str(pre), db_path=db)

        with pytest.raises(KeyboardInterrupt):
            with archive_queue._atomic_archive_op(db) as conn:
                conn.execute(
                    """INSERT INTO archive_queue
                           (source_path, priority, status, enqueued_at)
                       VALUES (?, ?, 'pending', ?)""",
                    (sample_file, 3, '2026-01-01T00:00:00+00:00'),
                )
                raise KeyboardInterrupt()
        # New row rolled back; pre-existing row preserved.
        rows = list_queue(db_path=db)
        assert len(rows) == 1
        assert rows[0]['source_path'] == str(pre)

    def test_connection_closed_on_success(self, db, monkeypatch):
        """Connection is closed after a clean commit."""
        opened = []
        original_open = archive_queue._open_archive_conn

        def _spy_open(path):
            conn = original_open(path)
            opened.append(conn)
            return conn

        monkeypatch.setattr(archive_queue,
                            '_open_archive_conn', _spy_open)
        with archive_queue._atomic_archive_op(db) as conn:
            conn.execute("SELECT 1")
        assert len(opened) == 1
        # Operating on a closed connection raises ProgrammingError.
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    def test_connection_closed_on_exception(self, db, monkeypatch):
        """Connection is closed even if the body raised."""
        opened = []
        original_open = archive_queue._open_archive_conn

        def _spy_open(path):
            conn = original_open(path)
            opened.append(conn)
            return conn

        monkeypatch.setattr(archive_queue,
                            '_open_archive_conn', _spy_open)
        with pytest.raises(RuntimeError):
            with archive_queue._atomic_archive_op(db) as conn:
                conn.execute("SELECT 1")
                raise RuntimeError("body raised")
        assert len(opened) == 1
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    def test_connection_closed_on_keyboard_interrupt(self, db, monkeypatch):
        """Connection is closed even on KeyboardInterrupt — no FD leak."""
        opened = []
        original_open = archive_queue._open_archive_conn

        def _spy_open(path):
            conn = original_open(path)
            opened.append(conn)
            return conn

        monkeypatch.setattr(archive_queue,
                            '_open_archive_conn', _spy_open)
        with pytest.raises(KeyboardInterrupt):
            with archive_queue._atomic_archive_op(db) as conn:
                conn.execute("SELECT 1")
                raise KeyboardInterrupt()
        assert len(opened) == 1
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    def test_begin_immediate_acquires_write_lock_up_front(self, db,
                                                          monkeypatch):
        """The first statement in the body should be BEGIN IMMEDIATE,
        not a deferred BEGIN. This avoids lock-upgrade SQLITE_BUSY
        races under load."""
        executed = []
        original_open = archive_queue._open_archive_conn

        class _Spy:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def execute(self, sql, *a, **kw):
                executed.append(sql.strip().split()[0:2])
                return self._real.execute(sql, *a, **kw)

        def _spy_open(path):
            return _Spy(original_open(path))

        monkeypatch.setattr(archive_queue,
                            '_open_archive_conn', _spy_open)

        with archive_queue._atomic_archive_op(db) as conn:
            conn.execute("SELECT 1")

        # First statement should be 'BEGIN IMMEDIATE', not just 'BEGIN'.
        assert executed[0] == ['BEGIN', 'IMMEDIATE'], (
            f"expected BEGIN IMMEDIATE first, got {executed[0]!r}"
        )

    def test_close_failure_in_finally_does_not_mask_body_exception(
            self, db, monkeypatch):
        """If conn.close() raises in the finally, the original body
        exception still propagates — close-failure is swallowed."""
        original_open = archive_queue._open_archive_conn

        class _BadClose:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def close(self):
                # Real close first to avoid resource leak in the test.
                try:
                    self._real.close()
                except sqlite3.Error:
                    pass
                raise sqlite3.OperationalError("close failed")

        def _spy_open(path):
            return _BadClose(original_open(path))

        monkeypatch.setattr(archive_queue,
                            '_open_archive_conn', _spy_open)

        # The body's exception (RuntimeError) must be what propagates,
        # not the close-failure.
        with pytest.raises(RuntimeError, match="body"):
            with archive_queue._atomic_archive_op(db):
                raise RuntimeError("body failed")


class TestMarkFailedAtomicity:
    """Phase 2.10 regression: mark_failed must be atomic.

    Before Phase 2.10 the SELECT(attempts) and the conditional UPDATE
    ran under autocommit, so two concurrent mark_failed calls could
    both read the same `attempts` and then race to UPDATE — losing
    one increment and potentially leaving a row stuck below
    max_attempts forever.

    After Phase 2.10 the helper wraps both statements in
    BEGIN IMMEDIATE … COMMIT, serializing concurrent writers via the
    SQLite write lock.
    """

    def test_concurrent_mark_failed_does_not_lose_attempts(
            self, db, sample_file, monkeypatch):
        """Two threads call mark_failed on the same row simultaneously.
        After both return, attempts must equal 2 (no lost update).

        Forces the race by injecting a small delay between SELECT and
        UPDATE inside _atomic_archive_op's body. Under autocommit
        without a transaction, both threads' SELECTs would read 0
        and both UPDATEs would write 1 — losing one increment. With
        BEGIN IMMEDIATE wrapping the whole helper, T2 blocks until
        T1 commits, so T2 reads 1 and writes 2.
        """
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)
        row_id = row['id']

        # Wrap conn.execute so SELECT against archive_queue gets a
        # 200ms pause AFTER fetch — enough to provoke any race window.
        original_open = archive_queue._open_archive_conn

        class _SlowSelect:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def __enter__(self):
                return self._real.__enter__()

            def __exit__(self, *a):
                return self._real.__exit__(*a)

            def execute(self, sql, *a, **kw):
                cur = self._real.execute(sql, *a, **kw)
                if 'SELECT attempts' in sql:
                    # Force the SELECT-then-UPDATE window wide open.
                    time.sleep(0.2)
                return cur

        def _spy_open(path):
            return _SlowSelect(original_open(path))

        monkeypatch.setattr(archive_queue,
                            '_open_archive_conn', _spy_open)

        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            r = mark_failed(row_id, 'race', max_attempts=10, db_path=db)
            with results_lock:
                results.append(r)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)
        assert not t1.is_alive() and not t2.is_alive()

        assert results.count('pending') == 2, (
            f"expected both calls to succeed as 'pending', got {results}"
        )
        # The Phase 2.10 contract: SELECT+UPDATE atomicity → no lost
        # update. attempts must be exactly 2.
        rows = list_queue(db_path=db)
        assert rows[0]['attempts'] == 2, (
            f"lost update detected: attempts={rows[0]['attempts']}, "
            f"expected 2"
        )

    def test_mark_failed_select_and_update_are_atomic(
            self, db, sample_file, monkeypatch):
        """Verify mark_failed runs SELECT + UPDATE inside one
        BEGIN IMMEDIATE transaction (the Phase 2.10 contract)."""
        enqueue_for_archive(sample_file, db_path=db)
        row = claim_next_for_worker('w1', db_path=db)

        executed = []
        original_open = archive_queue._open_archive_conn

        class _Spy:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def execute(self, sql, *a, **kw):
                executed.append(sql.strip().split()[0])
                return self._real.execute(sql, *a, **kw)

        def _spy_open(path):
            return _Spy(original_open(path))

        monkeypatch.setattr(archive_queue,
                            '_open_archive_conn', _spy_open)
        mark_failed(row['id'], 'oops', max_attempts=3, db_path=db)

        # Expect: BEGIN, SELECT, UPDATE, COMMIT — in that order.
        # (The exact case may vary; normalize to upper.)
        upper = [s.upper() for s in executed]
        assert upper[0] == 'BEGIN', f"first statement was {upper[0]!r}"
        assert 'SELECT' in upper
        assert 'UPDATE' in upper
        assert upper[-1] == 'COMMIT', f"last statement was {upper[-1]!r}"
        # SELECT must come before UPDATE (ordering preserved).
        assert upper.index('SELECT') < upper.index('UPDATE')


# ---------------------------------------------------------------------------
# Phase 2.10 review fix — connect/PRAGMA failures must be caught
# ---------------------------------------------------------------------------

class TestOpenFailureCaught:
    """Connection-open failures (sqlite3.Error from connect or initial
    PRAGMAs) must be caught by every public helper and turned into a
    safe-default return — not raised to the caller.

    Phase 2.10 first draft hoisted ``conn = _open_archive_conn(db_path)``
    above the ``try:`` block, which made open-time errors escape. The
    review fix moved the call back inside the ``try:`` (with
    ``conn = None`` guard in the ``finally``). These tests pin that
    contract so it can't regress silently.

    On a Pi Zero 2 W, SQLite open / PRAGMA can fail under SDIO
    contention (busy bus, transient I/O error). A producer thread
    raising would crash the file watcher; an archive-worker helper
    raising would crash the worker loop.
    """

    @pytest.fixture
    def patched_open_raises(self, monkeypatch):
        """Make _open_archive_conn raise a sqlite3.OperationalError
        on every call — simulating a connect/PRAGMA failure."""
        def _raising(_path):
            raise sqlite3.OperationalError("simulated open failure")
        monkeypatch.setattr(archive_queue,
                            '_open_archive_conn', _raising)
        return _raising

    def test_enqueue_for_archive_returns_false(self, db, sample_file,
                                               patched_open_raises):
        assert enqueue_for_archive(sample_file, db_path=db) is False

    def test_enqueue_many_for_archive_returns_zero(self, db, sample_file,
                                                   patched_open_raises):
        assert enqueue_many_for_archive([sample_file], db_path=db) == 0

    def test_get_queue_status_returns_zeros(self, db, patched_open_raises):
        result = get_queue_status(db_path=db)
        assert result['total'] == 0
        for s in archive_queue._KNOWN_STATUSES:
            assert result[s] == 0

    def test_list_queue_returns_empty(self, db, patched_open_raises):
        assert list_queue(db_path=db) == []

    def test_claim_next_for_worker_returns_none(self, db,
                                                patched_open_raises):
        assert claim_next_for_worker('w1', db_path=db) is None

    def test_mark_copied_returns_false(self, db, patched_open_raises):
        assert mark_copied(1, '/dest', db_path=db) is False

    def test_mark_source_gone_returns_false(self, db, patched_open_raises):
        assert mark_source_gone(1, db_path=db) is False

    def test_release_claim_returns_false(self, db, patched_open_raises):
        assert release_claim(1, db_path=db) is False

    def test_mark_failed_returns_error(self, db, patched_open_raises):
        assert mark_failed(1, 'oops', db_path=db) == 'error'

    def test_recover_stale_claims_returns_zero(self, db,
                                               patched_open_raises):
        assert recover_stale_claims(db_path=db) == 0

    def test_get_pending_counts_returns_zeros(self, db,
                                              patched_open_raises):
        result = archive_queue.get_pending_counts_by_priority(db_path=db)
        assert result == {1: 0, 2: 0, 3: 0}

    def test_get_last_copied_at_returns_none(self, db,
                                             patched_open_raises):
        assert archive_queue.get_last_copied_at(db_path=db) is None


# ===========================================================================
# Lost-banner dismissal tombstone — PR #169 follow-up
# ===========================================================================
#
# Background: dismissing the home-page "Footage may have been lost" banner
# used to feel broken to the operator on a heavily-backlogged device. The
# POST → DELETE chain worked correctly server-side (verified live on
# cybertruckusb), but with 1200+ pending RecentClips queued and many of
# them already aged out of Tesla's circular buffer, the worker would mark
# fresh source_gone rows within a second of the user's click — and the
# banner would re-appear on the next 30 s health poll. From the operator's
# perspective: "I clicked Dismiss, the banner stays."
#
# Fix: a server-side dismissal tombstone (small JSON in GADGET_DIR) that
# clamps :func:`count_source_gone_recent`'s ``claimed_at`` lower bound to
# ``MAX(now-24h, dismissed_at)``. Old rows the operator acknowledged stay
# hidden forever; brand-new losses incurred *after* the dismissal still
# show up so the operator notices ongoing loss patterns.
# ===========================================================================


class TestLostDismissedAtRoundtrip:
    """Read/write semantics of the tombstone file."""

    def test_get_returns_none_when_file_missing(self, tmp_path):
        sp = str(tmp_path / "missing.json")
        assert archive_queue.get_lost_dismissed_at(state_path=sp) is None

    def test_set_then_get_returns_value(self, tmp_path):
        sp = str(tmp_path / "tomb.json")
        archive_queue.set_lost_dismissed_at(
            '2026-05-13T17:00:00+00:00', state_path=sp,
        )
        assert (
            archive_queue.get_lost_dismissed_at(state_path=sp)
            == '2026-05-13T17:00:00+00:00'
        )

    def test_set_default_uses_iso_now(self, tmp_path):
        sp = str(tmp_path / "tomb.json")
        before = archive_queue._iso_now()
        archive_queue.set_lost_dismissed_at(state_path=sp)
        after = archive_queue._iso_now()
        got = archive_queue.get_lost_dismissed_at(state_path=sp)
        assert got is not None
        assert before <= got <= after  # ISO-8601 ordering matches lexicographic

    def test_set_overwrites_existing(self, tmp_path):
        sp = str(tmp_path / "tomb.json")
        archive_queue.set_lost_dismissed_at(
            '2026-01-01T00:00:00+00:00', state_path=sp,
        )
        archive_queue.set_lost_dismissed_at(
            '2026-06-06T12:00:00+00:00', state_path=sp,
        )
        assert (
            archive_queue.get_lost_dismissed_at(state_path=sp)
            == '2026-06-06T12:00:00+00:00'
        )

    def test_get_returns_none_on_corrupt_json(self, tmp_path):
        sp = str(tmp_path / "tomb.json")
        # Write garbage; a half-written file should never crash the poll.
        with open(sp, 'w', encoding='utf-8') as f:
            f.write('{not valid json')
        assert archive_queue.get_lost_dismissed_at(state_path=sp) is None

    def test_get_returns_none_when_key_missing(self, tmp_path):
        sp = str(tmp_path / "tomb.json")
        with open(sp, 'w', encoding='utf-8') as f:
            f.write('{"some_other_key": "value"}')
        assert archive_queue.get_lost_dismissed_at(state_path=sp) is None

    def test_get_returns_none_when_value_blank(self, tmp_path):
        sp = str(tmp_path / "tomb.json")
        with open(sp, 'w', encoding='utf-8') as f:
            f.write('{"dismissed_at": "   "}')
        assert archive_queue.get_lost_dismissed_at(state_path=sp) is None

    def test_set_atomic_write_via_replace(self, tmp_path):
        # The temp file from the atomic-write path must not linger after
        # a successful replace. Catches a regression where set_ would
        # forget to .replace() and instead leave both files behind.
        sp = str(tmp_path / "tomb.json")
        archive_queue.set_lost_dismissed_at(
            '2026-05-13T17:00:00+00:00', state_path=sp,
        )
        assert os.path.isfile(sp)
        assert not os.path.isfile(sp + '.tmp')


class TestEpochFromIso:
    """Internal helper: ISO timestamp → integer epoch seconds."""

    def test_offset_aware_iso(self):
        # _iso_now writes ``2026-...+00:00`` style strings.
        assert (
            archive_queue._epoch_from_iso('2026-01-01T00:00:00+00:00')
            == 1767225600
        )

    def test_naive_iso_assumed_utc(self):
        # SQLite ``datetime('now')`` writes naive strings — our parser
        # must treat them as UTC, not as local time.
        assert (
            archive_queue._epoch_from_iso('2026-01-01 00:00:00')
            == 1767225600
        )

    def test_garbage_returns_none(self):
        assert archive_queue._epoch_from_iso('not-a-date') is None
        assert archive_queue._epoch_from_iso('') is None
        assert archive_queue._epoch_from_iso(None) is None


class TestCountSourceGoneRecentRespectsTombstone:
    """The whole point of PR #169 follow-up: rows older than the
    tombstone must not count toward the banner."""

    def _seed_source_gone_with_claimed_at(self, db, tmp_path, claimed_iso):
        """Insert a single source_gone row with a custom ``claimed_at``."""
        f = tmp_path / f"sg_{claimed_iso.replace(':','-')}.mp4"
        f.write_text("x")
        enqueue_for_archive(str(f), db_path=db)
        row = claim_next_for_worker('w', db_path=db)
        mark_source_gone(row['id'], db_path=db)
        # Backdate the row's ``claimed_at`` so the count's recency
        # filter and the tombstone floor have something to bite on.
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE archive_queue SET claimed_at = ? WHERE id = ?",
                (claimed_iso, row['id']),
            )
            conn.commit()
        return row['id']

    def test_no_tombstone_counts_all_recent(self, db, tmp_path):
        # 3 rows in the last 24 h, no dismissal → count is 3.
        for i in range(3):
            self._seed_source_gone_with_claimed_at(
                db, tmp_path,
                f'2099-12-31T0{i}:00:00+00:00',  # well in the future = "recent" forever
            )
        # Pin a known floor via the kwarg path so the result is
        # reproducible regardless of the autouse fixture's tmp file.
        assert archive_queue.count_source_gone_recent(
            24 * 365 * 100, db_path=db,
            ignore_dismissed=True,
        ) == 3

    def test_tombstone_excludes_older_rows(self, db, tmp_path):
        # 2 rows BEFORE the tombstone, 1 row AFTER. With the tombstone
        # applied, only the AFTER row counts.
        self._seed_source_gone_with_claimed_at(
            db, tmp_path, '2026-01-01T00:00:00+00:00')
        self._seed_source_gone_with_claimed_at(
            db, tmp_path, '2026-02-01T00:00:00+00:00')
        self._seed_source_gone_with_claimed_at(
            db, tmp_path, '2026-04-01T00:00:00+00:00')

        assert archive_queue.count_source_gone_recent(
            hours=24 * 365 * 100, db_path=db,
            dismissed_at='2026-03-01T00:00:00+00:00',
        ) == 1

    def test_tombstone_in_future_excludes_everything(self, db, tmp_path):
        # Edge case: clock skew or test setting the tombstone to a
        # future time — every row is older than the tombstone.
        for i in range(5):
            self._seed_source_gone_with_claimed_at(
                db, tmp_path,
                f'2026-01-0{i+1}T00:00:00+00:00',
            )
        assert archive_queue.count_source_gone_recent(
            hours=24 * 365 * 100, db_path=db,
            dismissed_at='2099-12-31T23:59:59+00:00',
        ) == 0

    def test_24h_window_still_applies_when_tombstone_older(
        self, db, tmp_path,
    ):
        # 1 row from 2 days ago + 1 row from 30 min ago. Tombstone
        # from 1 year ago. 24-h window should still hide the 2-day-old
        # row even though it's after the tombstone.
        self._seed_source_gone_with_claimed_at(
            db, tmp_path, '2026-01-01T00:00:00+00:00')  # 2 days ago, hypothetically

        # Grab a row from 30 min ago via _iso_now() then UPDATE.
        f = tmp_path / "recent.mp4"
        f.write_text("x")
        enqueue_for_archive(str(f), db_path=db)
        row = claim_next_for_worker('w-recent', db_path=db)
        mark_source_gone(row['id'], db_path=db)
        # claimed_at defaults to ~now() in mark_source_gone — leave as is.

        # 24-h recency window + 1-year-old tombstone → only the recent row counts.
        n = archive_queue.count_source_gone_recent(
            hours=24, db_path=db,
            dismissed_at='2025-01-01T00:00:00+00:00',  # well over a year ago
        )
        assert n == 1  # only the recent.mp4 row

    def test_ignore_dismissed_bypasses_tombstone(self, db, tmp_path):
        # The Failed Jobs page wants the raw count regardless of the
        # banner dismissal — wired via ``ignore_dismissed=True``.
        for _ in range(4):
            f = tmp_path / f"sg_{_}.mp4"
            f.write_text("x")
            enqueue_for_archive(str(f), db_path=db)
            row = claim_next_for_worker(f'w{_}', db_path=db)
            mark_source_gone(row['id'], db_path=db)
        n = archive_queue.count_source_gone_recent(
            hours=24, db_path=db,
            dismissed_at='2099-12-31T23:59:59+00:00',
            ignore_dismissed=True,
        )
        assert n == 4

    def test_garbage_tombstone_falls_through_to_window_only(
        self, db, tmp_path,
    ):
        # A corrupt tombstone string must not crash the count — it
        # degrades to "no floor, 24-h window only".
        for _ in range(3):
            f = tmp_path / f"sg_{_}.mp4"
            f.write_text("x")
            enqueue_for_archive(str(f), db_path=db)
            row = claim_next_for_worker(f'w{_}', db_path=db)
            mark_source_gone(row['id'], db_path=db)
        assert archive_queue.count_source_gone_recent(
            hours=24, db_path=db,
            dismissed_at='garbage-date-string',
        ) == 3


class TestDeleteSourceGoneWritesTombstone:
    """:func:`delete_source_gone` is the main producer of the tombstone."""

    def _seed(self, db, tmp_path, n=3):
        for i in range(n):
            f = tmp_path / f"sg_{i}.mp4"
            f.write_text("x")
            enqueue_for_archive(str(f), db_path=db)
            row = claim_next_for_worker(f'w{i}', db_path=db)
            mark_source_gone(row['id'], db_path=db)

    def test_dismiss_writes_tombstone(self, db, tmp_path):
        sp = str(tmp_path / "tomb.json")
        self._seed(db, tmp_path, 3)
        before = archive_queue._iso_now()
        deleted = archive_queue.delete_source_gone(db_path=db, state_path=sp)
        after = archive_queue._iso_now()

        assert deleted == 3
        ts = archive_queue.get_lost_dismissed_at(state_path=sp)
        assert ts is not None
        assert before <= ts <= after

    def test_dismiss_with_set_tombstone_false_skips_write(
        self, db, tmp_path,
    ):
        sp = str(tmp_path / "tomb.json")
        self._seed(db, tmp_path, 2)
        archive_queue.delete_source_gone(
            db_path=db, state_path=sp,
            set_dismissal_tombstone=False,
        )
        assert archive_queue.get_lost_dismissed_at(state_path=sp) is None

    def test_older_than_hours_purge_skips_tombstone(self, db, tmp_path):
        # Forensic / time-window purges aren't user acknowledgments so
        # they MUST NOT write the tombstone (otherwise an admin housekeeping
        # run would silently suppress the banner for all subsequent users).
        sp = str(tmp_path / "tomb.json")
        self._seed(db, tmp_path, 2)
        archive_queue.delete_source_gone(
            db_path=db, state_path=sp,
            older_than_hours=1,
        )
        assert archive_queue.get_lost_dismissed_at(state_path=sp) is None

    def test_tombstone_write_failure_does_not_abort_delete(
        self, db, tmp_path, monkeypatch,
    ):
        # If the tombstone write raises (disk full, permission denied,
        # state dir missing), the DELETE must still proceed — the
        # operator's primary intent is "clear the count NOW".
        self._seed(db, tmp_path, 2)
        monkeypatch.setattr(
            archive_queue, 'set_lost_dismissed_at',
            lambda *a, **kw: (_ for _ in ()).throw(
                OSError('synthetic'),
            ),
        )
        deleted = archive_queue.delete_source_gone(db_path=db)
        assert deleted == 2

    def test_dismiss_then_immediate_new_loss_is_visible(
        self, db, tmp_path,
    ):
        """Acceptance test: after a dismiss, brand-new losses still
        show up so the operator sees ongoing loss patterns. Only the
        previously-acknowledged ones stay hidden."""
        import time
        sp = str(tmp_path / "tomb.json")
        # Old losses (2 rows) — about to be dismissed.
        for i in range(2):
            f = tmp_path / f"old_{i}.mp4"
            f.write_text("x")
            enqueue_for_archive(str(f), db_path=db)
            row = claim_next_for_worker(f'w-old-{i}', db_path=db)
            mark_source_gone(row['id'], db_path=db)
        n_before = archive_queue.count_source_gone_recent(
            24, db_path=db, ignore_dismissed=True,
        )
        assert n_before == 2

        # Operator dismisses.
        archive_queue.delete_source_gone(db_path=db, state_path=sp)
        # Sleep 1 s so the new row's claimed_at is strictly after the tombstone.
        time.sleep(1.1)

        # A brand-new loss arrives.
        f = tmp_path / "fresh.mp4"
        f.write_text("x")
        enqueue_for_archive(str(f), db_path=db)
        row = claim_next_for_worker('w-fresh', db_path=db)
        mark_source_gone(row['id'], db_path=db)

        # The fresh loss must count even though we dismissed.
        ts = archive_queue.get_lost_dismissed_at(state_path=sp)
        assert ts is not None
        assert archive_queue.count_source_gone_recent(
            24, db_path=db, dismissed_at=ts,
        ) == 1


# ===========================================================================
# Issue #178 — priority swap: events drain before RecentClips
# ===========================================================================
#
# Pre-#178 the archive queue had ``PRIORITY_RECENT_CLIPS=1`` and
# ``PRIORITY_EVENTS=2``, so RecentClips drained ahead of Sentry/Saved
# events. Live evidence on cybertruckusb.local showed 71 SentryClips
# events untouched for 130+ minutes while the worker burned its SDIO
# budget on parked-Sentry RecentClips skip-decisions. PR for #178
# swaps the constants (events=1, RecentClips=2) and adds a v12->v13
# schema migration that flips existing non-terminal queue rows so the
# in-flight backlog also benefits.

class TestPriorityConstantsPostIssue178:
    """Issue #178: events MUST drain before RecentClips."""

    def test_events_priority_is_lowest_number(self):
        # Lower number = picked first. Events must be priority 1.
        assert PRIORITY_EVENTS == 1, (
            "Issue #178: SentryClips/SavedClips events are the "
            "highest-value footage and MUST drain first. "
            "PRIORITY_EVENTS must be 1."
        )
        assert PRIORITY_RECENT_CLIPS == 2, (
            "Issue #178: RecentClips driving footage is second-tier "
            "(SEI-peek skip-stationary handles parked-no-event case). "
            "PRIORITY_RECENT_CLIPS must be 2."
        )
        assert PRIORITY_OTHER == 3, (
            "Other (e.g. ArchivedClips back-fill) is the lowest "
            "priority. PRIORITY_OTHER must be 3."
        )
        # Strict ordering — events strictly drain before RecentClips
        # which strictly drain before other.
        assert PRIORITY_EVENTS < PRIORITY_RECENT_CLIPS < PRIORITY_OTHER

    def test_inference_maps_sentryclips_to_events(self):
        for path in (
            '/mnt/gadget/part1-ro/TeslaCam/SentryClips/2026-05-12_'
            '08-00-00/front.mp4',
            '/mnt/gadget/part1-ro/TeslaCam/SavedClips/2026-05-12_'
            '08-00-00/back.mp4',
            '/mnt/gadget/part1-ro/TeslaCam/sentryclips/lower/x.mp4',
        ):
            assert _infer_priority(path) == PRIORITY_EVENTS == 1, (
                f"Path {path!r} must map to PRIORITY_EVENTS (=1) "
                f"post-#178"
            )

    def test_inference_maps_recentclips_to_recent(self):
        for path in (
            '/mnt/gadget/part1-ro/TeslaCam/RecentClips/2026-05-12_'
            '08-00-00-front.mp4',
            r'C:\TeslaCam\RecentClips\clip.mp4',
        ):
            assert _infer_priority(path) == PRIORITY_RECENT_CLIPS == 2, (
                f"Path {path!r} must map to PRIORITY_RECENT_CLIPS (=2) "
                f"post-#178"
            )

    def test_pick_order_event_before_recent_clip(self, db, tmp_path):
        """Acceptance test: with one event and one RecentClip both
        pending, the worker MUST claim the event first regardless of
        insertion order or mtime."""
        recent = tmp_path / "RecentClips" / "front.mp4"
        recent.parent.mkdir(parents=True)
        recent.write_bytes(b"r")
        event = tmp_path / "SentryClips" / "evt" / "front.mp4"
        event.parent.mkdir(parents=True)
        event.write_bytes(b"e")
        # Make the RecentClip OLDER (closer to TTL deadline) so the
        # only thing that picks the event first is the priority band —
        # if the priority swap is wrong, the older RecentClip would win.
        os.utime(str(recent), (1000.0, 1000.0))
        os.utime(str(event), (2000.0, 2000.0))
        enqueue_for_archive(str(recent), db_path=db)
        enqueue_for_archive(str(event), db_path=db)

        first = claim_next_for_worker('w', db_path=db)
        assert first['source_path'] == str(event), (
            "Issue #178 acceptance test: with both an event AND a "
            "RecentClip pending (RecentClip even being OLDER), the "
            "event MUST be claimed first."
        )
        assert int(first['priority']) == PRIORITY_EVENTS == 1
        # And the RecentClip drains second.
        second = claim_next_for_worker('w', db_path=db)
        assert second['source_path'] == str(recent)
        assert int(second['priority']) == PRIORITY_RECENT_CLIPS == 2


class TestPriorityMigrationV12ToV13:
    """Issue #178: the v12 -> v13 migration MUST flip existing
    non-terminal queue rows so the in-flight backlog drains in the
    new (correct) order — otherwise users would have to wait for the
    old rows to drain the slow way before seeing the benefit.

    Terminal-status rows (copied, source_gone, etc.) MUST be left
    alone — their priority is historical and mutating it would
    mislead future debugging.
    """

    def _build_v12_db_with_old_priorities(self, tmp_path, rows):
        """Build a fresh DB and seed archive_queue rows BEFORE running
        the migration. Returns (db_path, list of (source_path, status,
        starting_priority)) tuples for assertion lookups.

        We can't easily roll the schema back to v12 from v13, so we
        build the DB at the current schema and then INSERT rows
        carrying the pre-#178 priority values, then call the migration
        helper directly to confirm it's idempotent and flips correctly.
        """
        db_path = str(tmp_path / "geodata.db")
        conn = _init_db(db_path)
        try:
            # Force schema_version back to 12 so we can re-run the
            # v12 -> v13 block as if upgrading from v12. Idempotent
            # by design: the SQL has ``priority IN (1, 2)`` and only
            # touches non-terminal statuses.
            conn.execute("DELETE FROM schema_version")
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (12)"
            )
            for source_path, status, starting_priority in rows:
                conn.execute(
                    "INSERT INTO archive_queue "
                    "(source_path, priority, status, enqueued_at) "
                    "VALUES (?, ?, ?, datetime('now'))",
                    (source_path, starting_priority, status),
                )
            conn.commit()
        finally:
            conn.close()
        return db_path

    def _read_priority(self, db_path, source_path):
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT priority FROM archive_queue WHERE source_path = ?",
                (source_path,),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def test_migration_flips_pending_priorities(self, tmp_path):
        # Seed rows at the OLD priority mapping (RecentClips=1,
        # Events=2) and pretend we're at v12. Re-run _init_db to
        # trigger the v12 -> v13 migration.
        rows = [
            # source_path, status, starting_priority
            ('/tc/RecentClips/r.mp4', 'pending', 1),     # was P1, must flip to 2
            ('/tc/SentryClips/e/f.mp4', 'pending', 2),    # was P2, must flip to 1
            ('/tc/Other/o.mp4', 'pending', 3),             # P3 untouched
        ]
        db_path = self._build_v12_db_with_old_priorities(tmp_path, rows)
        # Trigger the migration by re-initializing.
        conn = _init_db(db_path)
        conn.close()

        assert self._read_priority(db_path, '/tc/RecentClips/r.mp4') == 2
        assert self._read_priority(
            db_path, '/tc/SentryClips/e/f.mp4',
        ) == 1
        assert self._read_priority(db_path, '/tc/Other/o.mp4') == 3

    def test_migration_flips_claimed_and_error_rows_too(self, tmp_path):
        # Non-terminal statuses ALL get flipped — when release_claim
        # / mark_failed restore them to pending, they must already
        # carry the new priority.
        rows = [
            ('/tc/RecentClips/r-claimed.mp4', 'claimed', 1),
            ('/tc/SentryClips/e-claimed/f.mp4', 'claimed', 2),
            ('/tc/RecentClips/r-error.mp4', 'error', 1),
            ('/tc/SentryClips/e-error/f.mp4', 'error', 2),
        ]
        db_path = self._build_v12_db_with_old_priorities(tmp_path, rows)
        conn = _init_db(db_path)
        conn.close()

        assert self._read_priority(
            db_path, '/tc/RecentClips/r-claimed.mp4',
        ) == 2
        assert self._read_priority(
            db_path, '/tc/SentryClips/e-claimed/f.mp4',
        ) == 1
        assert self._read_priority(
            db_path, '/tc/RecentClips/r-error.mp4',
        ) == 2
        assert self._read_priority(
            db_path, '/tc/SentryClips/e-error/f.mp4',
        ) == 1

    def test_migration_leaves_terminal_rows_untouched(self, tmp_path):
        # Terminal statuses (copied, source_gone, skipped_stationary,
        # dead_letter) keep their historical priority — mutating it
        # would mislead future debugging of "what got picked when".
        rows = [
            ('/tc/RecentClips/r-copied.mp4', 'copied', 1),
            ('/tc/SentryClips/e-copied/f.mp4', 'copied', 2),
            ('/tc/RecentClips/r-gone.mp4', 'source_gone', 1),
            ('/tc/SentryClips/e-gone/f.mp4', 'source_gone', 2),
            (
                '/tc/RecentClips/r-skipped.mp4',
                'skipped_stationary',
                1,
            ),
            ('/tc/RecentClips/r-dl.mp4', 'dead_letter', 1),
            ('/tc/SentryClips/e-dl/f.mp4', 'dead_letter', 2),
        ]
        db_path = self._build_v12_db_with_old_priorities(tmp_path, rows)
        conn = _init_db(db_path)
        conn.close()

        # Each row keeps its ORIGINAL priority value.
        for source_path, _status, starting_priority in rows:
            actual = self._read_priority(db_path, source_path)
            assert actual == starting_priority, (
                f"Terminal-status row {source_path!r} had its priority "
                f"changed from {starting_priority} to {actual} — the "
                f"v12->v13 migration MUST leave terminal rows alone."
            )

    def test_migration_is_idempotent(self, tmp_path):
        # Running _init_db a second time on a v13 database is a no-op
        # for the priority-swap migration (already at v13, so the
        # ``current < 13`` gate is False). Verify by seeding rows at
        # the NEW (post-migration) priority mapping and confirming
        # they are NOT flipped back.
        db_path = str(tmp_path / "geodata.db")
        conn = _init_db(db_path)
        try:
            # Insert a row at the post-migration mapping.
            conn.execute(
                "INSERT INTO archive_queue "
                "(source_path, priority, status, enqueued_at) "
                "VALUES (?, 1, 'pending', datetime('now'))",
                ('/tc/SentryClips/e/f.mp4',),
            )
            conn.execute(
                "INSERT INTO archive_queue "
                "(source_path, priority, status, enqueued_at) "
                "VALUES (?, 2, 'pending', datetime('now'))",
                ('/tc/RecentClips/r.mp4',),
            )
            conn.commit()
        finally:
            conn.close()

        # Re-initialize — already at v13, should be a no-op for
        # priorities.
        conn = _init_db(db_path)
        conn.close()

        assert self._read_priority(
            db_path, '/tc/SentryClips/e/f.mp4',
        ) == 1
        assert self._read_priority(db_path, '/tc/RecentClips/r.mp4') == 2

    def test_migration_does_nothing_when_no_old_rows(self, tmp_path):
        # Fresh install: no rows at all. Migration must complete
        # cleanly without errors.
        db_path = str(tmp_path / "geodata.db")
        conn = _init_db(db_path)
        # Force back to v12 to exercise the migration block.
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (12)")
        conn.commit()
        conn.close()

        # Should not raise.
        conn = _init_db(db_path)
        try:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM schema_version"
            ).fetchone()
            assert row['v'] == 13
        finally:
            conn.close()
