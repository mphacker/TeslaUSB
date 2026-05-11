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
        # Mix of priorities: RecentClips and a generic path
        recent_dir = tmp_path / "RecentClips"
        recent_dir.mkdir()
        recent_clip = recent_dir / "r.mp4"
        recent_clip.write_bytes(b"r")
        other = tmp_path / "other.mp4"
        other.write_bytes(b"o")
        enqueue_many_for_archive([str(recent_clip), str(other)], db_path=db)
        # Sorted by priority
        rows = list_queue(db_path=db)
        assert rows[0]['priority'] == PRIORITY_RECENT_CLIPS
        assert rows[0]['source_path'] == str(recent_clip)
        assert rows[1]['priority'] == PRIORITY_OTHER


# ---------------------------------------------------------------------------
# Status counts
# ---------------------------------------------------------------------------

class TestGetQueueStatus:
    def test_empty_returns_zero_for_every_known_status(self, db):
        counts = get_queue_status(db_path=db)
        assert counts == {
            'pending': 0, 'claimed': 0, 'copied': 0,
            'source_gone': 0, 'error': 0, 'dead_letter': 0,
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
        # Three files with controlled priorities
        recent_dir = tmp_path / "RecentClips"; recent_dir.mkdir()
        r1 = recent_dir / "r1.mp4"; r1.write_bytes(b"x")
        time.sleep(0.01)  # mtime ordering
        r2 = recent_dir / "r2.mp4"; r2.write_bytes(b"x")
        other = tmp_path / "other.mp4"; other.write_bytes(b"x")
        enqueue_many_for_archive(
            [str(other), str(r2), str(r1)], db_path=db,
        )
        rows = list_queue(db_path=db)
        # RecentClips (priority 1) come first, oldest mtime first within tier.
        assert rows[0]['source_path'] == str(r1)
        assert rows[1]['source_path'] == str(r2)
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
    mark_copied,
    mark_failed,
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
        # Mix of priorities: P3 enqueued first, then P2, then P1.
        # Worker MUST pick P1 first.
        p3 = tmp_path / "other.mp4"; p3.write_bytes(b'x')
        p2 = tmp_path / "SentryClips" / "evt" / "front.mp4"
        p2.parent.mkdir(parents=True); p2.write_bytes(b'x')
        p1 = tmp_path / "RecentClips" / "front.mp4"
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
        a = tmp_path / "RecentClips" / "a-front.mp4"
        b = tmp_path / "RecentClips" / "b-front.mp4"
        a.parent.mkdir(parents=True); a.write_bytes(b'a'); b.write_bytes(b'b')
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
