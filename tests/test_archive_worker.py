"""Tests for the Phase 2b archive worker (issue #76).

Coverage matches the issue spec:

* TestArchiveWorkerLifecycle  — start, stop, pause, resume, idempotent start
* TestArchiveWorkerCopy       — successful copy → status='copied' + indexer enqueued
* TestArchiveWorkerStableGate — fresh + drift → release; stable → proceed
* TestArchiveWorkerSourceGone — FileNotFoundError → no retry, no dead-letter
* TestArchiveWorkerDeadLetter — synthetic OSError × max_attempts → sidecar
* TestArchiveWorkerPriority   — P1 RecentClips drains before P2/P3
* TestArchiveWorkerStarvation — synthetic indexer load + 10 archive items
* TestArchiveWorkerPauseResume — claim released cleanly on pause; resume picks up

Most tests drive ``process_one_claim`` directly so we don't need to spin
up a thread for every assertion. The lifecycle / starvation tests run the
real loop.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import List
from unittest.mock import patch

import pytest

from services import archive_queue
from services import archive_worker
from services import task_coordinator
from services.archive_queue import (
    claim_next_for_worker,
    enqueue_for_archive,
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
    _init_db(db_path).close()
    return db_path


@pytest.fixture
def archive_root(tmp_path):
    p = tmp_path / "ArchivedClips"
    p.mkdir()
    return str(p)


@pytest.fixture
def teslacam_root(tmp_path):
    p = tmp_path / "TeslaCam"
    p.mkdir()
    (p / "RecentClips").mkdir()
    (p / "SavedClips").mkdir()
    (p / "SentryClips").mkdir()
    return str(p)


@pytest.fixture
def make_clip(teslacam_root):
    """Factory for fake mp4 files. ``rel`` is relative to teslacam_root."""
    def _factory(rel: str, content: bytes = b"X" * 100,
                 mtime: float = None) -> str:
        full = os.path.join(teslacam_root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(content)
        if mtime is not None:
            os.utime(full, (mtime, mtime))
        else:
            # Backdate so the stable-write age gate (5 s) is satisfied.
            old = time.time() - 60
            os.utime(full, (old, old))
        return full
    return _factory


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Stop any running worker between tests so module state stays clean."""
    archive_worker.stop_worker(timeout=5.0)
    # Reset task_coordinator too — leftover ownership from an earlier
    # test would block our acquire.
    with task_coordinator._lock:
        task_coordinator._current_task = None
        task_coordinator._task_started = 0.0
        task_coordinator._waiter_count = 0
    yield
    archive_worker.stop_worker(timeout=5.0)
    with task_coordinator._lock:
        task_coordinator._current_task = None
        task_coordinator._task_started = 0.0
        task_coordinator._waiter_count = 0


@pytest.fixture(autouse=True)
def _block_real_indexer_enqueue(monkeypatch):
    """Stop the worker from calling into the real ``mapping_service``.

    The worker enqueues the destination path into ``indexing_queue``
    after a successful copy. Tests that don't care about that side
    effect would otherwise need a fully-initialized indexing schema +
    config import. We stub it here and the few tests that DO care
    monkeypatch a recording stub on top.
    """
    monkeypatch.setattr(archive_worker, '_enqueue_indexed', lambda *a, **k: None)


# ---------------------------------------------------------------------------
# TestArchiveWorkerLifecycle
# ---------------------------------------------------------------------------


class TestArchiveWorkerLifecycle:
    def test_start_returns_true_first_time(self, db, archive_root, teslacam_root):
        ok = archive_worker.start_worker(
            db, archive_root, teslacam_root=teslacam_root,
        )
        assert ok is True
        assert archive_worker.is_running() is True
        assert archive_worker.stop_worker(timeout=5) is True
        assert archive_worker.is_running() is False

    def test_double_start_is_noop(self, db, archive_root, teslacam_root):
        assert archive_worker.start_worker(
            db, archive_root, teslacam_root=teslacam_root,
        ) is True
        # Second start while running must refuse and return False.
        assert archive_worker.start_worker(
            db, archive_root, teslacam_root=teslacam_root,
        ) is False
        archive_worker.stop_worker(timeout=5)

    def test_stop_when_not_running_returns_true(self):
        # Idempotent: stop on a never-started worker is a no-op.
        assert archive_worker.stop_worker(timeout=2) is True

    def test_pause_when_not_running_succeeds(self):
        # Pause-flag-only path; no thread to wait on.
        assert archive_worker.pause_worker(timeout=1) is True
        archive_worker.resume_worker()

    def test_pause_resume_round_trip(self, db, archive_root, teslacam_root):
        archive_worker.start_worker(
            db, archive_root, teslacam_root=teslacam_root,
        )
        # No queue work — worker idles. Pause should return quickly.
        assert archive_worker.pause_worker(timeout=5) is True
        assert archive_worker.is_paused() is True
        archive_worker.resume_worker()
        assert archive_worker.is_paused() is False
        archive_worker.stop_worker(timeout=5)

    def test_get_status_includes_queue_counts(self, db, archive_root,
                                              teslacam_root, make_clip):
        clip = make_clip("RecentClips/2025-01-01_10-00-00-front.mp4")
        enqueue_for_archive(clip, db_path=db)
        archive_worker.start_worker(
            db, archive_root, teslacam_root=teslacam_root,
        )
        try:
            # Wait briefly for the worker to drain the single item.
            for _ in range(50):
                if archive_worker.get_status()['copied_count'] >= 1:
                    break
                time.sleep(0.1)
            status = archive_worker.get_status()
            assert status['worker_running'] is True
            assert status['copied_count'] >= 1
        finally:
            archive_worker.stop_worker(timeout=5)


# ---------------------------------------------------------------------------
# TestArchiveWorkerCopy
# ---------------------------------------------------------------------------


class TestArchiveWorkerCopy:
    def test_copy_writes_dest_with_matching_size(
        self, db, archive_root, teslacam_root, make_clip,
    ):
        content = b"abcdef" * 1000  # 6000 bytes
        clip = make_clip(
            "RecentClips/2025-01-01_10-00-00-front.mp4", content=content,
        )
        enqueue_for_archive(clip, db_path=db)
        row = claim_next_for_worker('test-worker', db_path=db)
        outcome = archive_worker.process_one_claim(
            row, db, archive_root, teslacam_root,
            chunk_size=4096, max_attempts=3,
        )
        assert outcome == 'copied'
        dest = os.path.join(
            archive_root, "RecentClips", "2025-01-01_10-00-00-front.mp4",
        )
        assert os.path.isfile(dest)
        assert os.path.getsize(dest) == len(content)
        with open(dest, "rb") as f:
            assert f.read() == content
        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'copied'
        assert rows[0]['dest_path'] == dest
        assert rows[0]['copied_at'] is not None

    def test_copy_enqueues_dest_into_indexer(
        self, db, archive_root, teslacam_root, make_clip, monkeypatch,
    ):
        clip = make_clip("SentryClips/evt1/2025-01-01_10-00-00-front.mp4")
        enqueue_for_archive(clip, db_path=db)
        row = claim_next_for_worker('w', db_path=db)
        # Replace the autouse stub with a recorder.
        recorded: List[tuple] = []
        monkeypatch.setattr(
            archive_worker, '_enqueue_indexed',
            lambda dest, db_path: recorded.append((dest, db_path)),
        )
        archive_worker.process_one_claim(
            row, db, archive_root, teslacam_root,
            chunk_size=4096, max_attempts=3,
        )
        assert len(recorded) == 1
        dest, indexer_db = recorded[0]
        assert dest.endswith(
            os.path.join(
                "SentryClips", "evt1", "2025-01-01_10-00-00-front.mp4",
            ),
        )
        assert indexer_db == db

    def test_copy_creates_intermediate_dirs(
        self, db, archive_root, teslacam_root, make_clip,
    ):
        clip = make_clip("SavedClips/2025-01-01_evt2/x-front.mp4")
        enqueue_for_archive(clip, db_path=db)
        row = claim_next_for_worker('w', db_path=db)
        archive_worker.process_one_claim(
            row, db, archive_root, teslacam_root,
            chunk_size=4096, max_attempts=3,
        )
        dest = os.path.join(
            archive_root, "SavedClips", "2025-01-01_evt2", "x-front.mp4",
        )
        assert os.path.isfile(dest)

    def test_copy_atomic_no_partial_left_on_success(
        self, db, archive_root, teslacam_root, make_clip,
    ):
        clip = make_clip("RecentClips/x-front.mp4")
        enqueue_for_archive(clip, db_path=db)
        row = claim_next_for_worker('w', db_path=db)
        archive_worker.process_one_claim(
            row, db, archive_root, teslacam_root,
            chunk_size=4096, max_attempts=3,
        )
        # No .partial sidecar left over.
        partial = os.path.join(archive_root, "RecentClips", "x-front.mp4.partial")
        assert not os.path.exists(partial)

    def test_compute_dest_path_falls_back_when_outside_teslacam(
        self, archive_root, teslacam_root,
    ):
        # Source path that isn't under teslacam_root falls back to
        # archive_root/<basename>.
        out = archive_worker.compute_dest_path(
            "/random/scratch/foo.mp4", archive_root, teslacam_root,
        )
        assert out == os.path.join(archive_root, "foo.mp4")

    def test_compute_dest_path_handles_missing_teslacam_root(
        self, archive_root,
    ):
        out = archive_worker.compute_dest_path(
            "/random/scratch/foo.mp4", archive_root, None,
        )
        assert out == os.path.join(archive_root, "foo.mp4")

    def test_compute_dest_path_rejects_empty_source(self, archive_root):
        with pytest.raises(ValueError):
            archive_worker.compute_dest_path("", archive_root, None)


# ---------------------------------------------------------------------------
# TestArchiveWorkerStableGate
# ---------------------------------------------------------------------------


class TestArchiveWorkerStableGate:
    def test_fresh_file_with_drift_releases_claim(
        self, db, archive_root, teslacam_root, make_clip,
    ):
        # Fresh file: enqueue with old size (50 bytes), then write 100 bytes.
        # The worker re-stats, sees drift, and the file is fresh (mtime=now)
        # → release_claim with refreshed metadata.
        clip = make_clip(
            "RecentClips/x-front.mp4", content=b"a" * 50,
            mtime=time.time(),  # fresh
        )
        enqueue_for_archive(clip, db_path=db)
        # Now grow the file (drift in size and mtime).
        with open(clip, "wb") as f:
            f.write(b"a" * 100)
        os.utime(clip, (time.time(), time.time()))
        row = claim_next_for_worker('w', db_path=db)
        outcome = archive_worker.process_one_claim(
            row, db, archive_root, teslacam_root,
            chunk_size=4096, max_attempts=3,
        )
        assert outcome == 'pending'
        # Row is back in pending, with refreshed metadata.
        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'pending'
        assert rows[0]['expected_size'] == 100
        assert rows[0]['attempts'] == 0  # not burned

    def test_stable_old_file_proceeds_to_copy(
        self, db, archive_root, teslacam_root, make_clip,
    ):
        # Make a clip with an mtime well in the past — the gate
        # passes immediately.
        clip = make_clip("RecentClips/y-front.mp4", mtime=1000.0)
        enqueue_for_archive(clip, db_path=db)
        row = claim_next_for_worker('w', db_path=db)
        outcome = archive_worker.process_one_claim(
            row, db, archive_root, teslacam_root,
            chunk_size=4096, max_attempts=3,
        )
        assert outcome == 'copied'

    def test_fresh_file_without_drift_proceeds(
        self, db, archive_root, teslacam_root, make_clip,
    ):
        # A fresh file whose stat() matches the enqueue snapshot
        # should NOT requeue — drift, not freshness, is the trigger.
        clip = make_clip(
            "RecentClips/z-front.mp4", content=b"x" * 50,
            mtime=time.time(),
        )
        enqueue_for_archive(clip, db_path=db)
        # Don't touch the file — claim should see expected==actual.
        row = claim_next_for_worker('w', db_path=db)
        outcome = archive_worker.process_one_claim(
            row, db, archive_root, teslacam_root,
            chunk_size=4096, max_attempts=3,
        )
        assert outcome == 'copied'


# ---------------------------------------------------------------------------
# TestArchiveWorkerSourceGone
# ---------------------------------------------------------------------------


class TestArchiveWorkerSourceGone:
    def test_missing_at_stat_marks_source_gone(
        self, db, archive_root, teslacam_root, make_clip,
    ):
        clip = make_clip("RecentClips/gone-front.mp4")
        enqueue_for_archive(clip, db_path=db)
        row = claim_next_for_worker('w', db_path=db)
        os.unlink(clip)
        outcome = archive_worker.process_one_claim(
            row, db, archive_root, teslacam_root,
            chunk_size=4096, max_attempts=3,
        )
        assert outcome == 'source_gone'
        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'source_gone'
        # No attempts burned, no dead-letter sidecar.
        assert rows[0]['attempts'] == 0
        sidecar_dir = os.path.join(archive_root, '.dead_letter')
        assert not os.path.isdir(sidecar_dir) or not os.listdir(sidecar_dir)

    def test_missing_at_open_marks_source_gone(
        self, db, archive_root, teslacam_root, make_clip, monkeypatch,
    ):
        # Race: stat() succeeded, but the file vanished before open().
        # The atomic copy raises FileNotFoundError; we expect source_gone.
        clip = make_clip("RecentClips/race-front.mp4")
        enqueue_for_archive(clip, db_path=db)
        row = claim_next_for_worker('w', db_path=db)

        original_atomic = archive_worker._atomic_copy

        def _fail_with_fnf(src, dst, chunk):
            raise FileNotFoundError(src)

        monkeypatch.setattr(archive_worker, '_atomic_copy', _fail_with_fnf)
        outcome = archive_worker.process_one_claim(
            row, db, archive_root, teslacam_root,
            chunk_size=4096, max_attempts=3,
        )
        # Restore (autouse fixture handles teardown but be tidy).
        monkeypatch.setattr(archive_worker, '_atomic_copy', original_atomic)
        assert outcome == 'source_gone'
        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'source_gone'


# ---------------------------------------------------------------------------
# TestArchiveWorkerDeadLetter
# ---------------------------------------------------------------------------


class TestArchiveWorkerDeadLetter:
    def test_three_oserrors_writes_sidecar_and_dead_letters(
        self, db, archive_root, teslacam_root, make_clip, monkeypatch,
    ):
        clip = make_clip("RecentClips/bad-front.mp4")
        enqueue_for_archive(clip, db_path=db)

        def always_fail(src, dst, chunk):
            raise OSError("synthetic disk error")

        monkeypatch.setattr(archive_worker, '_atomic_copy', always_fail)

        # Three failed attempts (max=3 → final transitions to dead_letter).
        outcomes = []
        for _ in range(3):
            row = claim_next_for_worker('w', db_path=db)
            assert row is not None, "expected pending row before dead_letter"
            outcomes.append(archive_worker.process_one_claim(
                row, db, archive_root, teslacam_root,
                chunk_size=4096, max_attempts=3,
            ))
        # First two: pending; third: dead_letter.
        assert outcomes[0] == 'pending'
        assert outcomes[1] == 'pending'
        assert outcomes[2] == 'dead_letter'

        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'dead_letter'
        assert rows[0]['attempts'] == 3
        assert rows[0]['last_error'].startswith('copy:')

        # Sidecar exists with all required fields.
        sidecar_dir = os.path.join(archive_root, '.dead_letter')
        assert os.path.isdir(sidecar_dir)
        sidecars = os.listdir(sidecar_dir)
        assert len(sidecars) == 1
        with open(os.path.join(sidecar_dir, sidecars[0]), encoding='utf-8') as f:
            txt = f.read()
        assert 'source_path:' in txt
        assert clip in txt
        assert 'dest_path:' in txt
        assert 'attempts: 3' in txt
        assert 'enqueued_at:' in txt
        assert 'last_error:' in txt
        assert 'synthetic disk error' in txt

    def test_size_mismatch_treated_as_oserror(
        self, db, archive_root, teslacam_root, make_clip, monkeypatch,
    ):
        # Stub _atomic_copy with a function that raises an OSError mimicking
        # the size-mismatch check. After max_attempts → dead_letter.
        clip = make_clip("RecentClips/mismatch-front.mp4")
        enqueue_for_archive(clip, db_path=db)

        def mismatch(src, dst, chunk):
            raise OSError("size mismatch: wrote 50, expected 100")

        monkeypatch.setattr(archive_worker, '_atomic_copy', mismatch)
        for _ in range(3):
            row = claim_next_for_worker('w', db_path=db)
            archive_worker.process_one_claim(
                row, db, archive_root, teslacam_root,
                chunk_size=4096, max_attempts=3,
            )
        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'dead_letter'
        assert 'mismatch' in rows[0]['last_error']

    def test_partial_file_cleaned_on_failure(
        self, db, archive_root, teslacam_root, make_clip,
    ):
        # Use the real _atomic_copy but make the source a directory so
        # open() fails. Verify no .partial leftover.
        sub_dir = os.path.join(teslacam_root, "RecentClips", "broken")
        os.makedirs(sub_dir)  # this is a DIR, not a file
        # Write a fake row directly bypassing enqueue_for_archive (which
        # would skip non-file targets).
        with sqlite3.connect(db) as c:
            c.execute(
                """INSERT INTO archive_queue (source_path, priority, status,
                       enqueued_at)
                   VALUES (?, 1, 'pending', '2025-01-01T00:00:00+00:00')""",
                (sub_dir,),
            )
        row = claim_next_for_worker('w', db_path=db)
        archive_worker.process_one_claim(
            row, db, archive_root, teslacam_root,
            chunk_size=4096, max_attempts=3,
        )
        # Even with the failure, the .partial in the destination should
        # not exist.
        partial = os.path.join(archive_root, "RecentClips", "broken.partial")
        assert not os.path.exists(partial)


# ---------------------------------------------------------------------------
# TestArchiveWorkerPriority
# ---------------------------------------------------------------------------


class TestArchiveWorkerPriority:
    def test_p1_drains_before_p2_before_p3(self, db, archive_root,
                                            teslacam_root, make_clip):
        """Phase 2b acceptance criterion: priority ordering across the
        wire — RecentClips first, then Sentry/Saved, then everything
        else. The partial index ``archive_queue_ready`` covers this
        exact ORDER BY."""
        # Enqueue in REVERSE priority order to make sure we're testing
        # ORDER BY, not insertion order.
        p3 = make_clip(
            "Other/other-front.mp4", mtime=1000.0,
        )
        p2 = make_clip(
            "SentryClips/evt/sentry-front.mp4", mtime=1000.0,
        )
        p1 = make_clip(
            "RecentClips/recent-front.mp4", mtime=1000.0,
        )
        enqueue_for_archive(p3, db_path=db)
        enqueue_for_archive(p2, db_path=db)
        enqueue_for_archive(p1, db_path=db)

        # Drive all three through process_one_claim and capture order.
        copied_order: List[str] = []
        for _ in range(3):
            row = claim_next_for_worker('w', db_path=db)
            assert row is not None
            outcome = archive_worker.process_one_claim(
                row, db, archive_root, teslacam_root,
                chunk_size=4096, max_attempts=3,
            )
            assert outcome == 'copied'
            copied_order.append(row['source_path'])

        assert copied_order == [p1, p2, p3], (
            f"Priority order violated: {copied_order}"
        )

    def test_oldest_mtime_within_band_drains_first(
        self, db, archive_root, teslacam_root, make_clip,
    ):
        # Two RecentClips files (same priority); the older one should
        # be claimed first.
        old = make_clip("RecentClips/old-front.mp4", mtime=1000.0)
        new = make_clip("RecentClips/new-front.mp4", mtime=2000.0)
        enqueue_for_archive(new, db_path=db)
        enqueue_for_archive(old, db_path=db)

        first = claim_next_for_worker('w', db_path=db)
        assert first['source_path'] == old


# ---------------------------------------------------------------------------
# TestArchiveWorkerStarvation (synthetic indexer load)
# ---------------------------------------------------------------------------


class TestArchiveWorkerStarvation:
    """The fairness contract from issue #76: even when the indexer is
    cyclically holding the task_coordinator slot, the archive worker
    must still drain its queue. Phase 2b uses
    ``acquire_task('archive', wait_seconds=60)`` which BLOCKS for a
    slot — so 10 archive items must finish within a bounded timeout
    even with synthetic indexer pressure."""

    def test_ten_items_drain_under_synthetic_indexer_load(
        self, db, archive_root, teslacam_root, make_clip,
    ):
        # Create + enqueue 10 items.
        clips = []
        for i in range(10):
            clip = make_clip(
                f"RecentClips/clip-{i:02d}-front.mp4", mtime=1000.0 + i,
            )
            enqueue_for_archive(clip, db_path=db)
            clips.append(clip)

        # Synthetic indexer thread: cyclically acquire/release the
        # 'indexer' slot with yield_to_waiters=True so the archive
        # worker (using wait_seconds=60) can grab the lock at every
        # iteration boundary.
        indexer_stop = threading.Event()
        indexer_iterations = [0]

        def synthetic_indexer():
            while not indexer_stop.is_set():
                if task_coordinator.acquire_task(
                        'indexer', yield_to_waiters=True):
                    try:
                        # Tiny "work" interval so the archive worker
                        # frequently gets a chance.
                        time.sleep(0.01)
                        indexer_iterations[0] += 1
                    finally:
                        task_coordinator.release_task('indexer')
                # Brief inter-cycle pause.
                if indexer_stop.wait(timeout=0.005):
                    break

        idxer = threading.Thread(target=synthetic_indexer, daemon=True)
        idxer.start()
        try:
            archive_worker.start_worker(
                db, archive_root, teslacam_root=teslacam_root,
            )
            # All 10 must drain within 30 s. Generous timeout because
            # CI runners are slow.
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if archive_worker.get_status()['copied_count'] >= 10:
                    break
                time.sleep(0.1)
            status = archive_worker.get_status()
            assert status['copied_count'] >= 10, (
                f"Only {status['copied_count']}/10 archived under load; "
                f"queue_depth={status['queue_depth']}, "
                f"indexer_iterations={indexer_iterations[0]}"
            )
            assert status['queue_depth'] == 0
        finally:
            indexer_stop.set()
            idxer.join(timeout=5)
            archive_worker.stop_worker(timeout=5)


# ---------------------------------------------------------------------------
# TestArchiveWorkerPauseResume
# ---------------------------------------------------------------------------


class TestArchiveWorkerPauseResume:
    def test_pause_releases_in_flight_claim(
        self, db, archive_root, teslacam_root, make_clip,
    ):
        # Enqueue a single item. Start the worker, immediately pause.
        # The worker should drop its claim back to pending without
        # burning an attempt — even if it had picked the row up.
        clip = make_clip("RecentClips/p-front.mp4", mtime=1000.0)
        enqueue_for_archive(clip, db_path=db)
        archive_worker.start_worker(
            db, archive_root, teslacam_root=teslacam_root,
        )
        try:
            # Wait for the worker to either copy or pause.
            assert archive_worker.pause_worker(timeout=10) is True
            rows = list_queue(db_path=db)
            # Either the worker already copied it (fast path) or the
            # row is back to pending. Either way, attempts MUST be 0.
            assert rows[0]['status'] in ('copied', 'pending')
            assert rows[0]['attempts'] == 0
        finally:
            archive_worker.resume_worker()
            archive_worker.stop_worker(timeout=5)

    def test_resume_processes_pending_claim(
        self, db, archive_root, teslacam_root, make_clip,
    ):
        clip = make_clip("RecentClips/r-front.mp4", mtime=1000.0)
        enqueue_for_archive(clip, db_path=db)
        archive_worker.start_worker(
            db, archive_root, teslacam_root=teslacam_root,
        )
        try:
            # Pause first so we don't race the auto-drain.
            assert archive_worker.pause_worker(timeout=10) is True
            # Force release of any in-flight claim by waiting; then
            # resume and verify the file gets archived.
            archive_worker.resume_worker()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                rows = list_queue(db_path=db)
                if rows and rows[0]['status'] == 'copied':
                    break
                time.sleep(0.1)
            rows = list_queue(db_path=db)
            assert rows[0]['status'] == 'copied'
        finally:
            archive_worker.stop_worker(timeout=5)

    def test_pause_with_empty_queue_succeeds_quickly(
        self, db, archive_root, teslacam_root,
    ):
        archive_worker.start_worker(
            db, archive_root, teslacam_root=teslacam_root,
        )
        try:
            t0 = time.monotonic()
            assert archive_worker.pause_worker(timeout=5) is True
            # With no work, pause should land within ~idle_sleep (default 5s).
            assert time.monotonic() - t0 < 6.0
        finally:
            archive_worker.resume_worker()
            archive_worker.stop_worker(timeout=5)


# ---------------------------------------------------------------------------
# Hard-constraint sanity: no USB-touching imports leak in here.
# ---------------------------------------------------------------------------


class TestModuleSafety:
    """Issue #76 hard constraint: the archive subsystem must NEVER
    invoke USB-gadget operations. A grep is part of the PR checklist;
    this test gives that contract a tripwire in CI."""

    def test_module_source_does_not_reference_gadget_ops(self):
        # Strip docstrings and comments before searching — the module
        # docstring intentionally enumerates the forbidden tokens as
        # part of its hard-constraint contract; only ACTUAL code
        # references would be a violation.
        import ast
        import inspect
        src = inspect.getsource(archive_worker)
        tree = ast.parse(src)
        # Collect every ast.Str / Constant value used as a docstring
        # (module-level + class-level + function-level) and excise it
        # from the source by deleting any line whose contents falls
        # inside a docstring node. Easiest robust path: walk the tree
        # and unparse only the executable statements (imports + defs).
        # AST.dump leaks the constant strings too, so just scan the
        # source line-by-line, skipping triple-quoted blocks.
        executable_lines: list = []
        in_triple = False
        triple_marker = None
        for line in src.splitlines():
            stripped = line.lstrip()
            if not in_triple:
                # Detect a triple-quote OPEN.
                for marker in ('"""', "'''"):
                    if stripped.startswith(marker):
                        in_triple = True
                        triple_marker = marker
                        # Same-line closer? "..."""...
                        rest = stripped[len(marker):]
                        if marker in rest:
                            in_triple = False
                            triple_marker = None
                        break
                else:
                    # Strip inline comments.
                    code = line.split('#', 1)[0]
                    executable_lines.append(code)
            else:
                # In a triple-quoted block — look for the closer.
                if triple_marker and triple_marker in line:
                    in_triple = False
                    triple_marker = None
        body = '\n'.join(executable_lines)

        forbidden = [
            'partition_mount_service', 'quick_edit_part2',
            'rebind_usb_gadget', 'losetup', 'nsenter',
        ]
        for tok in forbidden:
            assert tok not in body, (
                f"archive_worker.py executable code references forbidden "
                f"token {tok!r} — Phase 2b hard constraint: no USB "
                f"gadget interaction."
            )
