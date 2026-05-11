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
    # Reset the disk-space self-pause so a previous test that armed it
    # doesn't leak into the next test's process_one_claim path.
    archive_worker._disk_space_pause_until = 0.0
    archive_worker._load_pause_until = 0.0
    with archive_worker._state_lock:
        archive_worker._state['last_load_pause_at'] = None
        archive_worker._state['last_load_pause_loadavg'] = None
    # Reset task_coordinator too — leftover ownership from an earlier
    # test would block our acquire.
    with task_coordinator._lock:
        task_coordinator._current_task = None
        task_coordinator._task_started = 0.0
        task_coordinator._waiter_count = 0
    yield
    archive_worker.stop_worker(timeout=5.0)
    archive_worker._disk_space_pause_until = 0.0
    archive_worker._load_pause_until = 0.0
    with archive_worker._state_lock:
        archive_worker._state['last_load_pause_at'] = None
        archive_worker._state['last_load_pause_loadavg'] = None
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


# ---------------------------------------------------------------------------
# TestArchiveWorkerDiskSpaceGuard (Phase 2c — issue #76, acceptance 7)
# ---------------------------------------------------------------------------


class _FakeUsage:
    """``shutil.disk_usage``-shaped namedtuple-replacement."""
    def __init__(self, total: int, used: int, free: int):
        self.total = total
        self.used = used
        self.free = free


class TestArchiveWorkerDiskSpaceGuard:
    """Acceptance criterion 7: disk-full guard refuses copy + releases claim.

    The Phase 2c spec requires:
      * < 100 MB free → log CRITICAL, do NOT copy, release claim back
        to pending (no attempt counted), arm a 5-min worker-side pause.
      * < 500 MB free → log WARNING, proceed (copy still happens).
      * Both thresholds are configurable via ``cloud_archive.disk_space_*_mb``.
    """

    def _set_thresholds(self, monkeypatch, *, warning_mb: int, critical_mb: int):
        monkeypatch.setattr(
            archive_worker, '_resolve_disk_thresholds_mb',
            lambda: (warning_mb, critical_mb),
        )

    def _fake_disk_usage(self, monkeypatch, *, free_mb: int,
                          total_mb: int = 32_000):
        used_mb = max(total_mb - free_mb, 0)
        usage = _FakeUsage(
            total=total_mb * 1024 * 1024,
            used=used_mb * 1024 * 1024,
            free=free_mb * 1024 * 1024,
        )
        monkeypatch.setattr(archive_worker.shutil, 'disk_usage',
                            lambda _path: usage)

    def test_critical_free_refuses_copy_and_releases_claim(
        self, db, archive_root, teslacam_root, make_clip, monkeypatch, caplog,
    ):
        clip = make_clip("RecentClips/x-front.mp4")
        enqueue_for_archive(clip, db_path=db)
        row = claim_next_for_worker('w', db_path=db)

        self._set_thresholds(monkeypatch, warning_mb=500, critical_mb=100)
        self._fake_disk_usage(monkeypatch, free_mb=50)  # < critical

        dest_before = os.path.join(
            archive_root, "RecentClips", "x-front.mp4",
        )
        assert not os.path.exists(dest_before)

        with caplog.at_level('CRITICAL', logger='services.archive_worker'):
            outcome = archive_worker.process_one_claim(
                row, db, archive_root, teslacam_root,
                chunk_size=4096, max_attempts=3,
            )

        assert outcome == 'pending', (
            "Disk-critical must release the claim back to pending"
        )
        # Destination must not have been written.
        assert not os.path.exists(dest_before)

        # Row reverted to pending; attempts NOT incremented.
        rows = list_queue(db_path=db)
        assert len(rows) == 1
        assert rows[0]['status'] == 'pending'
        assert rows[0]['claimed_by'] is None
        assert rows[0]['attempts'] == 0  # no attempt burned
        # CRITICAL log captured.
        assert any('CRITICAL' in rec.message or rec.levelname == 'CRITICAL'
                   for rec in caplog.records), \
            "Disk-critical refusal must log at CRITICAL level"

        # Module-level pause armed.
        pause = archive_worker.get_disk_pause_state()
        assert pause['is_paused_now'] is True
        assert pause['paused_until_epoch'] > time.time()

    def test_warning_free_proceeds_with_copy(
        self, db, archive_root, teslacam_root, make_clip, monkeypatch, caplog,
    ):
        clip = make_clip("RecentClips/y-front.mp4", content=b"X" * 200)
        enqueue_for_archive(clip, db_path=db)
        row = claim_next_for_worker('w', db_path=db)

        self._set_thresholds(monkeypatch, warning_mb=500, critical_mb=100)
        self._fake_disk_usage(monkeypatch, free_mb=300)  # warn but not critical

        with caplog.at_level('WARNING', logger='services.archive_worker'):
            outcome = archive_worker.process_one_claim(
                row, db, archive_root, teslacam_root,
                chunk_size=4096, max_attempts=3,
            )

        assert outcome == 'copied'
        dest = os.path.join(archive_root, "RecentClips", "y-front.mp4")
        assert os.path.isfile(dest)
        # Warning level emitted.
        assert any(rec.levelname == 'WARNING'
                   for rec in caplog.records), \
            "Disk-warning copy must log at WARNING level"

    def test_ample_free_no_log_no_pause(
        self, db, archive_root, teslacam_root, make_clip, monkeypatch,
    ):
        clip = make_clip("RecentClips/z-front.mp4", content=b"X" * 200)
        enqueue_for_archive(clip, db_path=db)
        row = claim_next_for_worker('w', db_path=db)

        self._set_thresholds(monkeypatch, warning_mb=500, critical_mb=100)
        self._fake_disk_usage(monkeypatch, free_mb=10_000)  # plenty

        outcome = archive_worker.process_one_claim(
            row, db, archive_root, teslacam_root,
            chunk_size=4096, max_attempts=3,
        )
        assert outcome == 'copied'
        # No pause armed.
        pause = archive_worker.get_disk_pause_state()
        assert pause['is_paused_now'] is False

    def test_disk_pause_state_present_in_get_status(
        self, db, archive_root, teslacam_root,
    ):
        archive_worker.start_worker(
            db, archive_root, teslacam_root=teslacam_root,
        )
        try:
            status = archive_worker.get_status()
            assert 'disk_pause' in status
            assert 'is_paused_now' in status['disk_pause']
            assert 'paused_until_epoch' in status['disk_pause']
        finally:
            archive_worker.stop_worker(timeout=5)

    def test_check_disk_space_guard_handles_oserror(self, monkeypatch):
        # OSError on stat → 'ok' (don't lock the subsystem out on a
        # transient FS hiccup).
        def _boom(_path):
            raise OSError("simulated FS hiccup")
        monkeypatch.setattr(archive_worker.shutil, 'disk_usage', _boom)
        verdict = archive_worker._check_disk_space_guard("/anywhere")
        assert verdict == 'ok'

    def test_resolve_disk_space_pause_seconds_uses_config(self, monkeypatch):
        # PR #90 reviewer Info #4: hardcoded constant promoted to
        # ``cloud_archive.disk_space_pause_seconds``. The resolver must
        # read the configured value when present.
        import sys
        fake = type(sys)('fake_config')
        fake.CLOUD_ARCHIVE_DISK_SPACE_PAUSE_SECONDS = 600.0
        monkeypatch.setitem(sys.modules, 'config', fake)
        assert archive_worker._resolve_disk_space_pause_seconds() == 600.0

    def test_resolve_disk_space_pause_seconds_falls_back_on_invalid(
        self, monkeypatch,
    ):
        # Negative or zero values are rejected — fall back to the
        # module default so tests can monkeypatch it directly.
        import sys
        fake = type(sys)('fake_config')
        fake.CLOUD_ARCHIVE_DISK_SPACE_PAUSE_SECONDS = 0
        monkeypatch.setitem(sys.modules, 'config', fake)
        monkeypatch.setattr(
            archive_worker, '_DEFAULT_DISK_SPACE_PAUSE_SECONDS', 42.0,
        )
        assert archive_worker._resolve_disk_space_pause_seconds() == 42.0

    def test_resolve_disk_space_pause_seconds_falls_back_on_missing_config(
        self, monkeypatch,
    ):
        # If config import raises, the resolver returns the module default.
        import builtins
        real_import = builtins.__import__

        def _fail_import(name, *a, **kw):
            if name == 'config':
                raise ImportError("simulated")
            return real_import(name, *a, **kw)
        monkeypatch.setattr(builtins, '__import__', _fail_import)
        monkeypatch.setattr(
            archive_worker, '_DEFAULT_DISK_SPACE_PAUSE_SECONDS', 99.0,
        )
        assert archive_worker._resolve_disk_space_pause_seconds() == 99.0


# ---------------------------------------------------------------------------
# TestArchiveWorkerConfigContract — lock the config-tunables tuple shape so
# archive_worker.py and config.py don't drift out of sync.
# ---------------------------------------------------------------------------


class TestArchiveWorkerConfigContract:
    def test_read_config_returns_six_tunables(self):
        # The worker reads six tunables (chunk, max_attempts, idle,
        # inter_file, load_threshold, load_pause). Old callers expecting
        # three would silently break — lock the contract.
        result = archive_worker._read_config_or_defaults()
        assert len(result) == 6, (
            "_read_config_or_defaults must return 6 tunables; "
            "archive_worker.py and config.py have drifted "
            "(got %d)" % len(result)
        )
        chunk, max_attempts, idle, inter_file, load_thresh, load_pause = result
        assert chunk > 0
        assert max_attempts > 0
        assert idle > 0
        assert inter_file >= 0       # 0 disables inter-file pause
        assert load_thresh >= 0      # 0 disables load-pause guard
        assert load_pause >= 0

    def test_inter_file_sleep_default_is_at_least_one_second(self):
        # Regression guard: the SDIO contention failure mode that
        # caused hardware watchdog reboots was triggered with a 0.25s
        # inter-file sleep. The Pi Zero 2 W needs a minimum of ~1s
        # between copies to let the kernel flush + the WiFi chip get
        # SDIO bus time. Don't lower the default below 1s without
        # re-validating on hardware (see copilot-instructions.md).
        _, _, _, inter_file, _, _ = archive_worker._read_config_or_defaults()
        assert inter_file >= 1.0, (
            "Default inter_file_sleep_seconds must stay >= 1.0 to "
            "prevent SDIO bus saturation. See copilot-instructions.md."
        )

    def test_load_pause_threshold_default_is_set(self):
        # The load-pause guard prevents the archive worker from
        # piling onto an already-loaded system. Default threshold of
        # 3.5 was calibrated against the Pi Zero 2 W's 4 cores.
        _, _, _, _, load_thresh, load_pause = (
            archive_worker._read_config_or_defaults()
        )
        assert load_thresh > 0, (
            "Load-pause guard must be enabled by default."
        )
        assert load_pause >= 10, (
            "Load-pause sleep must be long enough (>=10s) to actually "
            "let load drop, not just throttle every iteration."
        )


# ---------------------------------------------------------------------------
# TestArchiveWorkerLoadPauseUX — verify the load-pause guard's user-visible
# behavior: status visibility, no log spam, wake() is ignored, state
# resets cleanly across worker starts.
# ---------------------------------------------------------------------------


class TestArchiveWorkerLoadPauseUX:
    def test_get_load_pause_state_initial(self):
        # Before any pause has fired, all fields are zero/None.
        state = archive_worker.get_load_pause_state()
        assert state['paused_until_epoch'] == 0.0
        assert state['is_paused_now'] is False
        assert state['last_pause_at'] is None
        assert state['last_loadavg'] is None

    def test_status_includes_load_pause_block(self, db, archive_root):
        # ``get_status()`` must surface a ``load_pause`` block parallel
        # to ``disk_pause`` so the UI can show *why* the worker isn't
        # draining. Regression guard against the block being dropped.
        archive_worker.start_worker(db, archive_root, teslacam_root=None)
        try:
            status = archive_worker.get_status()
            assert 'load_pause' in status, (
                "get_status() must include a 'load_pause' block "
                "(parity with 'disk_pause')."
            )
            assert 'disk_pause' in status, "Existing disk_pause block lost."
            lp = status['load_pause']
            assert set(lp.keys()) >= {
                'paused_until_epoch', 'is_paused_now',
                'last_pause_at', 'last_loadavg',
            }
        finally:
            archive_worker.stop_worker(timeout=5)

    def test_start_worker_resets_load_pause_state(self, db, archive_root):
        # Simulate a previous run that left state populated.
        archive_worker._load_pause_until = time.time() + 100
        with archive_worker._state_lock:
            archive_worker._state['last_load_pause_at'] = time.time()
            archive_worker._state['last_load_pause_loadavg'] = 5.5

        # A fresh start_worker MUST clear it (parity with disk_pause).
        # We don't actually need the worker to drain anything for this
        # test — start + stop is enough.
        archive_worker.start_worker(db, archive_root, teslacam_root=None)
        try:
            assert archive_worker._load_pause_until == 0.0, (
                "start_worker must reset _load_pause_until."
            )
            state = archive_worker.get_load_pause_state()
            assert state['last_pause_at'] is None
            assert state['last_loadavg'] is None
        finally:
            archive_worker.stop_worker(timeout=5)

    def test_load_pause_logs_only_on_transition(
        self, db, archive_root, teslacam_root, make_clip, monkeypatch, caplog,
    ):
        # Force os.getloadavg() to report sustained high load. The
        # worker must log the "pausing" INFO line ONCE (entering the
        # window), NOT on every iteration. Even if the load stays high
        # for many iterations, each subsequent iteration should be
        # silent because we're still inside the same pause window.
        # NB: ``getloadavg`` doesn't exist on Windows, so we install
        # it (raising=False) for the duration of the test.
        monkeypatch.setattr(
            archive_worker.os, 'getloadavg',
            lambda: (99.0, 99.0, 99.0), raising=False,
        )
        # Tighten the pause to keep the test snappy.
        def fake_config(*a, **kw):
            return (4096, 3, 0.05, 0.05, 0.5, 0.5)
        monkeypatch.setattr(archive_worker, '_read_config_or_defaults', fake_config)

        # Enqueue something so the worker has work to do (it'll hit
        # the load-pause guard before claiming).
        clip = make_clip("RecentClips/loadpause-front.mp4")
        enqueue_for_archive(clip, db_path=db)

        with caplog.at_level('INFO', logger='services.archive_worker'):
            archive_worker.start_worker(
                db, archive_root, teslacam_root=teslacam_root,
            )
            # Let the worker iterate several times under sustained load.
            time.sleep(2.0)
            archive_worker.stop_worker(timeout=5)

        # Count "pausing" INFO lines. With pause window = 0.5s and
        # 2.0s observation, we expect ~4 pause windows → 4 entry
        # logs at most. Without the transition guard this would log
        # on every iteration (~20+ times).
        pause_logs = [r for r in caplog.records
                      if 'pausing' in r.getMessage()
                      and 'relieve SDIO' in r.getMessage()]
        # Bound: at most 1 log per (load_pause_seconds + slack). With
        # 0.5s window over 2s + cleanup, 6 is a generous upper bound.
        assert len(pause_logs) <= 6, (
            "Load-pause must log on transition into the window only, "
            "not on every iteration. Got %d 'pausing' lines under "
            "sustained high load — that's the spam regression PR #93's "
            "review flagged." % len(pause_logs)
        )
        # And we must have logged AT LEAST one — otherwise the test
        # didn't actually exercise the guard.
        assert len(pause_logs) >= 1, (
            "Load-pause guard didn't fire under simulated load=99.0."
        )

    def test_load_pause_ignores_wake_event(
        self, db, archive_root, teslacam_root, make_clip, monkeypatch,
    ):
        # Producers calling ``wake()`` MUST NOT shorten the load-pause
        # back-off. The whole point of the pause is to give the SDIO
        # bus a clear runway; producer wakes would defeat that.
        monkeypatch.setattr(
            archive_worker.os, 'getloadavg',
            lambda: (99.0, 99.0, 99.0), raising=False,
        )
        # 1.0s pause window so the test can observe that wake() does
        # NOT cut it short.
        def fake_config(*a, **kw):
            return (4096, 3, 0.05, 0.05, 0.5, 1.0)
        monkeypatch.setattr(archive_worker, '_read_config_or_defaults', fake_config)

        clip = make_clip("RecentClips/wake-front.mp4")
        enqueue_for_archive(clip, db_path=db)

        archive_worker.start_worker(
            db, archive_root, teslacam_root=teslacam_root,
        )
        try:
            # Wait a hair so the worker enters the load-pause branch.
            time.sleep(0.15)
            t0 = time.time()
            # Hammer wake() — under the OLD (buggy) code each wake
            # would cut the 1s pause short within 1s of polling.
            for _ in range(20):
                archive_worker.wake()
                time.sleep(0.02)
            # Total elapsed under the test loop is ~0.4s. The pause
            # window is 1.0s; the worker MUST still be paused.
            elapsed = time.time() - t0
            assert elapsed < 0.6, "test loop overran"
            state = archive_worker.get_load_pause_state()
            assert state['is_paused_now'] is True, (
                "Load-pause was cut short by wake() — that's the bug "
                "PR #93's review flagged. The pause MUST honor stop "
                "events only, not wake events."
            )
        finally:
            archive_worker.stop_worker(timeout=5)

    def test_last_pause_at_pinned_within_window(
        self, db, archive_root, teslacam_root, make_clip, monkeypatch,
    ):
        # Within a single sustained pause window, ``last_pause_at`` must
        # NOT tick forward on every loop iteration — it represents
        # "when did THIS pause start", not "last time we checked".
        # This is parity with disk-pause (``last_disk_pause_at`` is
        # set inside process_one_claim only on first hit) and is the
        # natural reading of the field name. Regression guard for the
        # re-review INFO finding on PR #93.
        monkeypatch.setattr(
            archive_worker.os, 'getloadavg',
            lambda: (99.0, 99.0, 99.0), raising=False,
        )
        # Use a long pause window (5s) so the test stays well inside
        # one window across multiple iterations.
        def fake_config(*a, **kw):
            return (4096, 3, 0.05, 0.05, 0.5, 5.0)
        monkeypatch.setattr(archive_worker, '_read_config_or_defaults', fake_config)

        clip = make_clip("RecentClips/pin-front.mp4")
        enqueue_for_archive(clip, db_path=db)

        archive_worker.start_worker(
            db, archive_root, teslacam_root=teslacam_root,
        )
        try:
            # Give the worker a beat to enter the pause branch and
            # arm last_pause_at.
            time.sleep(0.2)
            first = archive_worker.get_load_pause_state()['last_pause_at']
            assert first is not None, (
                "Worker didn't enter load-pause within 200ms."
            )
            # Now sample several more times within the same 5s window.
            # If the bug existed, last_pause_at would tick forward as
            # the worker re-evaluated load on each wakeup. With the
            # fix, it stays pinned.
            time.sleep(0.4)
            second = archive_worker.get_load_pause_state()['last_pause_at']
            time.sleep(0.4)
            third = archive_worker.get_load_pause_state()['last_pause_at']
            assert second == first, (
                "last_pause_at advanced from %r to %r within the same "
                "pause window — it must pin to the moment the pause "
                "started, not the last time the worker re-checked load."
                % (first, second)
            )
            assert third == first, (
                "last_pause_at advanced from %r to %r within the same "
                "pause window — must remain pinned." % (first, third)
            )
        finally:
            archive_worker.stop_worker(timeout=5)


