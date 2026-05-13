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


def _build_minimal_mp4(payload: bytes = b"\x00" * 32) -> bytes:
    """Build a minimal-but-valid MP4 byte sequence (ftyp + moov + mdat).

    Used by ``make_clip`` so that ``.mp4`` test fixtures pass the Phase
    2.4 moov verification (``_verify_destination_complete``). Tests that
    explicitly want an INVALID MP4 (no moov, truncated, etc.) can pass
    raw ``content=b"..."`` to override.

    The structure is:

    * ``ftyp`` — file type box (16 bytes header+body)
    * ``moov`` — movie box, minimal empty body (8 bytes)
    * ``mdat`` — media data box wrapping the caller's payload

    Order doesn't matter for our verifier (we walk all top-level boxes).
    Tesla puts moov at the END; we put it BEFORE mdat in test fixtures
    to make the test bytes shorter and easier to inspect, but it
    exercises the same code path.
    """
    def box(typ: bytes, body: bytes) -> bytes:
        size = len(body) + 8
        return size.to_bytes(4, 'big') + typ + body

    ftyp_body = b'isom' + b'\x00\x00\x02\x00' + b'isomiso2avc1mp41'
    return box(b'ftyp', ftyp_body) + box(b'moov', b'') + box(b'mdat', payload)


@pytest.fixture
def make_clip(teslacam_root):
    """Factory for fake mp4 files. ``rel`` is relative to teslacam_root.

    For ``.mp4`` paths the default content is a minimal-but-valid MP4
    so the file passes the Phase 2.4 moov verification. Tests that want
    a deliberately-invalid MP4 (missing moov, truncated, etc.) must
    pass ``content=`` explicitly.
    """
    def _factory(rel: str, content: bytes = None,
                 mtime: float = None) -> str:
        if content is None:
            if rel.lower().endswith('.mp4'):
                content = _build_minimal_mp4()
            else:
                content = b"X" * 100
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
        # Build a valid MP4 wrapping ~6000 bytes of payload so the Phase
        # 2.4 moov verification accepts the copy. The byte-equality
        # assertion below pins that the copy is byte-for-byte identical
        # to the source.
        content = _build_minimal_mp4(payload=b"abcdef" * 1000)
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
        # NOTE: ``make_clip`` builds a valid minimal MP4 by default so
        # the Phase 2.4 moov-verify pass succeeds.
        clip = make_clip(
            "RecentClips/z-front.mp4",
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

    # ------------------------------------------------------------------
    # Phase 2.5 — NULL expected_size / expected_mtime semantics.
    #
    # Pre-2.5 behavior: ``metadata_drifted`` was False when both were
    # None, so the gate skipped and the worker would copy a possibly-
    # half-written file. With 2.5, NULL metadata is treated as "needs
    # settling check": defer if young, proceed if settled, refresh
    # baseline either way so the next claim has something to compare.
    # ------------------------------------------------------------------

    @staticmethod
    def _insert_null_metadata_row(
        db_path: str, source_path: str, priority: int = 1,
    ) -> int:
        """Insert a queue row with NULL expected_size / expected_mtime.

        Mimics the production race condition where the enqueue
        producer's ``stat()`` raced against Tesla's mid-write (or a
        legacy schema row predates the columns being populated).
        """
        with sqlite3.connect(db_path) as c:
            cur = c.execute(
                """INSERT INTO archive_queue
                       (source_path, priority, status, enqueued_at,
                        expected_size, expected_mtime)
                   VALUES (?, ?, 'pending', '2025-01-01T00:00:00+00:00',
                           NULL, NULL)""",
                (source_path, int(priority)),
            )
            return int(cur.lastrowid)

    def test_null_metadata_with_fresh_file_defers(
        self, db, archive_root, teslacam_root, make_clip,
    ):
        # Fresh file (mtime=now) + NULL expected_size/mtime → MUST be
        # deferred. Pre-2.5, this case fell through and copied a
        # potentially half-written file.
        clip = make_clip(
            "RecentClips/null-fresh-front.mp4", mtime=time.time(),
        )
        self._insert_null_metadata_row(db, clip)
        row = claim_next_for_worker('w', db_path=db)
        outcome = archive_worker.process_one_claim(
            row, db, archive_root, teslacam_root,
            chunk_size=4096, max_attempts=3,
        )
        assert outcome == 'pending', (
            "NULL metadata + fresh file MUST defer to next iteration; "
            "pre-2.5 this fell through and could copy a half-written file"
        )
        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'pending'
        assert rows[0]['attempts'] == 0  # not burned
        # Baseline metadata is now populated for the next claim.
        assert rows[0]['expected_size'] is not None
        assert rows[0]['expected_mtime'] is not None

    def test_null_metadata_with_settled_file_proceeds(
        self, db, archive_root, teslacam_root, make_clip,
    ):
        # NULL metadata + file that has been quiet for > 5 s →
        # treat live stat as authoritative and copy. The moov-verify
        # added in 2.4 catches any structural incompleteness.
        clip = make_clip(
            "RecentClips/null-settled-front.mp4", mtime=1000.0,
        )
        self._insert_null_metadata_row(db, clip)
        row = claim_next_for_worker('w', db_path=db)
        outcome = archive_worker.process_one_claim(
            row, db, archive_root, teslacam_root,
            chunk_size=4096, max_attempts=3,
        )
        assert outcome == 'copied', (
            "NULL metadata + settled file should proceed: live stat "
            "is the authoritative baseline"
        )

    def test_null_metadata_only_size_null_defers_when_fresh(
        self, db, archive_root, teslacam_root, make_clip,
    ):
        # Defensive: only ONE column NULL (legacy partial-migration
        # row) should still trigger the settling check, not be
        # accidentally trusted because the other column matches.
        clip = make_clip(
            "RecentClips/null-size-front.mp4", mtime=time.time(),
        )
        with sqlite3.connect(db) as c:
            c.execute(
                """INSERT INTO archive_queue
                       (source_path, priority, status, enqueued_at,
                        expected_size, expected_mtime)
                   VALUES (?, 1, 'pending', '2025-01-01T00:00:00+00:00',
                           NULL, ?)""",
                (clip, os.path.getmtime(clip)),
            )
        row = claim_next_for_worker('w', db_path=db)
        outcome = archive_worker.process_one_claim(
            row, db, archive_root, teslacam_root,
            chunk_size=4096, max_attempts=3,
        )
        assert outcome == 'pending'
        rows = list_queue(db_path=db)
        assert rows[0]['expected_size'] is not None  # refreshed

    def test_null_metadata_eventually_drains_after_settling(
        self, db, archive_root, teslacam_root, make_clip, monkeypatch,
    ):
        # NULL metadata + fresh file → defer. Then simulate the file
        # settling (mtime moved into the past) and re-claim. The
        # previous defer populated expected_size/mtime, so the next
        # claim has a baseline + the file is now stable → copy.
        clip = make_clip(
            "RecentClips/null-then-settled-front.mp4",
            mtime=time.time(),
        )
        self._insert_null_metadata_row(db, clip)

        # First claim: NULL + fresh → defer with refreshed metadata.
        row = claim_next_for_worker('w', db_path=db)
        assert archive_worker.process_one_claim(
            row, db, archive_root, teslacam_root,
            chunk_size=4096, max_attempts=3,
        ) == 'pending'

        # Simulate Tesla finishing the write: backdate mtime so the
        # file is now older than the 5-s gate. The size matches what
        # was just written into expected_size on the defer above.
        os.utime(clip, (1000.0, 1000.0))
        # release_claim updates expected_mtime to the live stat at
        # that moment, so we must also refresh the row's
        # expected_mtime to the now-backdated value to mimic a normal
        # later "no drift" pickup.
        with sqlite3.connect(db) as c:
            c.execute(
                "UPDATE archive_queue SET expected_mtime=1000.0 "
                "WHERE source_path=?",
                (clip,),
            )

        row = claim_next_for_worker('w', db_path=db)
        assert archive_worker.process_one_claim(
            row, db, archive_root, teslacam_root,
            chunk_size=4096, max_attempts=3,
        ) == 'copied'


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

        def _fail_with_fnf(src, dst, chunk, **kwargs):
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

        def always_fail(src, dst, chunk, **kwargs):
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

        def mismatch(src, dst, chunk, **kwargs):
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
        clip = make_clip("RecentClips/y-front.mp4")
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
        clip = make_clip("RecentClips/z-front.mp4")
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
    def test_read_config_returns_eight_tunables(self):
        # The worker reads eight tunables (chunk, max_attempts, idle,
        # inter_file, load_threshold, load_pause, chunk_pause,
        # time_budget). Old callers expecting fewer would silently break
        # — lock the contract. The last two were added by issue #104
        # (mid-copy SDIO safeguards).
        result = archive_worker._read_config_or_defaults()
        assert len(result) == 8, (
            "_read_config_or_defaults must return 8 tunables; "
            "archive_worker.py and config.py have drifted "
            "(got %d)" % len(result)
        )
        (chunk, max_attempts, idle, inter_file, load_thresh,
         load_pause, chunk_pause, time_budget) = result
        assert chunk > 0
        assert max_attempts > 0
        assert idle > 0
        assert inter_file >= 0       # 0 disables inter-file pause
        assert load_thresh >= 0      # 0 disables load-pause guard
        assert load_pause >= 0
        assert chunk_pause >= 0      # 0 disables chunk-pause guard
        assert time_budget >= 0      # 0 disables per-file time budget

    def test_inter_file_sleep_default_is_at_least_one_second(self):
        # Regression guard: the SDIO contention failure mode that
        # caused hardware watchdog reboots was triggered with a 0.25s
        # inter-file sleep. The Pi Zero 2 W needs a minimum of ~1s
        # between copies to let the kernel flush + the WiFi chip get
        # SDIO bus time. Don't lower the default below 1s without
        # re-validating on hardware (see copilot-instructions.md).
        result = archive_worker._read_config_or_defaults()
        inter_file = result[3]
        assert inter_file >= 1.0, (
            "Default inter_file_sleep_seconds must stay >= 1.0 to "
            "prevent SDIO bus saturation. See copilot-instructions.md."
        )

    def test_load_pause_threshold_default_is_set(self):
        # The load-pause guard prevents the archive worker from
        # piling onto an already-loaded system. Default threshold of
        # 3.5 was calibrated against the Pi Zero 2 W's 4 cores.
        result = archive_worker._read_config_or_defaults()
        load_thresh = result[4]
        load_pause = result[5]
        assert load_thresh > 0, (
            "Load-pause guard must be enabled by default."
        )
        assert load_pause >= 10, (
            "Load-pause sleep must be long enough (>=10s) to actually "
            "let load drop, not just throttle every iteration."
        )

    def test_per_file_time_budget_default_is_set(self):
        # Issue #104: the per-file time budget aborts a copy that has
        # been running for too long (sustained SDIO contention) so the
        # claim is released back to ``pending`` instead of starving the
        # userspace watchdog daemon. Default 60s sits at half the
        # 90 s hardware watchdog timeout — don't raise above 60.
        result = archive_worker._read_config_or_defaults()
        time_budget = result[7]
        assert 0 < time_budget <= 60.0, (
            "Default per_file_time_budget_seconds must be in (0, 60] "
            "to keep below the BCM2835 90 s watchdog timeout."
        )

    def test_chunk_pause_default_is_set(self):
        # Issue #104: the per-chunk pause yields the SDIO bus to other
        # readers while loadavg is above threshold. Default 0.25 s is
        # short enough not to cripple normal copies but long enough to
        # let the watchdog daemon get scheduled.
        result = archive_worker._read_config_or_defaults()
        chunk_pause = result[6]
        assert chunk_pause > 0, (
            "Mid-copy chunk-pause guard must be enabled by default."
        )
        assert chunk_pause <= 1.0, (
            "Default chunk_pause_seconds must stay small (<= 1.0); "
            "larger values needlessly slow normal copies."
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
            return (4096, 3, 0.05, 0.05, 0.5, 0.5, 0.0, 0.0)
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
            return (4096, 3, 0.05, 0.05, 0.5, 1.0, 0.0, 0.0)
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
            return (4096, 3, 0.05, 0.05, 0.5, 5.0, 0.0, 0.0)
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


# ---------------------------------------------------------------------------
# TestPartialOrphanSweep (Phase 1, item 1.7)
# ---------------------------------------------------------------------------


class TestPartialOrphanSweep:
    """Verify ``_sweep_partial_orphans`` cleans up half-copied files
    left behind by a prior crash. See ``_sweep_partial_orphans``
    docstring + #95 for the full motivation.
    """

    def test_sweep_removes_partial_files(self, tmp_path):
        archive = tmp_path / "ArchivedClips"
        archive.mkdir()
        sub = archive / "2026-05-11_14-44-00"
        sub.mkdir()
        # Two .partial orphans + one good .mp4 that must NOT be touched.
        (sub / "front.mp4.partial").write_bytes(b"x" * 1024)
        (sub / "back.mp4.partial").write_bytes(b"y" * 2048)
        (sub / "front.mp4").write_bytes(b"good" * 256)
        removed = archive_worker._sweep_partial_orphans(str(archive))
        assert removed == 2
        # Real .mp4 survives.
        assert (sub / "front.mp4").exists()
        # Both partials are gone.
        assert not (sub / "front.mp4.partial").exists()
        assert not (sub / "back.mp4.partial").exists()

    def test_sweep_skips_dead_letter_dir(self, tmp_path):
        archive = tmp_path / "ArchivedClips"
        archive.mkdir()
        dead = archive / ".dead_letter"
        dead.mkdir()
        # A .partial inside .dead_letter is preserved (forensic).
        (dead / "preserve.mp4.partial").write_bytes(b"z" * 16)
        # A .partial outside is removed.
        (archive / "kill.mp4.partial").write_bytes(b"q" * 32)
        removed = archive_worker._sweep_partial_orphans(str(archive))
        assert removed == 1
        assert (dead / "preserve.mp4.partial").exists()
        assert not (archive / "kill.mp4.partial").exists()

    def test_sweep_handles_missing_archive_root(self, tmp_path):
        # A missing / unconfigured archive_root must not raise.
        assert archive_worker._sweep_partial_orphans(
            str(tmp_path / "does-not-exist"),
        ) == 0
        assert archive_worker._sweep_partial_orphans('') == 0
        assert archive_worker._sweep_partial_orphans(None) == 0

    def test_sweep_continues_on_per_file_failure(
        self, tmp_path, monkeypatch,
    ):
        archive = tmp_path / "ArchivedClips"
        archive.mkdir()
        (archive / "a.mp4.partial").write_bytes(b"a")
        (archive / "b.mp4.partial").write_bytes(b"b")

        real_remove = os.remove
        calls: List[str] = []

        def flaky_remove(path):
            calls.append(path)
            if path.endswith("a.mp4.partial"):
                raise OSError("simulated failure")
            return real_remove(path)

        monkeypatch.setattr(archive_worker.os, 'remove', flaky_remove)
        removed = archive_worker._sweep_partial_orphans(str(archive))
        # b.mp4.partial removed; a.mp4.partial was tried and skipped.
        assert removed == 1
        assert len(calls) == 2
        assert (archive / "a.mp4.partial").exists()
        assert not (archive / "b.mp4.partial").exists()



# ---------------------------------------------------------------------------
# TestDiskCriticalCleanupTrigger (Phase 1, item 1.5)
# ---------------------------------------------------------------------------


class TestDiskCriticalCleanupTrigger:
    """Verify that disk-critical pause kicks ``archive_watchdog.force_prune_now()``
    immediately (debounced), instead of waiting up to 24 h for the
    daily retention timer.
    """

    @pytest.fixture(autouse=True)
    def _reset_debounce(self):
        # Ensure each test starts with debounce cleared.
        archive_worker._last_disk_critical_cleanup_at = 0.0
        # When the full test suite runs, ``services.archive_watchdog``
        # may already be cached as an attribute of the ``services``
        # package (loaded by tests/test_archive_watchdog.py). The
        # production helper uses ``from services import archive_watchdog``
        # which short-circuits to that cached attribute â€” bypassing any
        # ``sys.modules`` monkeypatch a test installs. Drop the cached
        # attribute (and the sys.modules entry) so each test starts
        # with a clean import slot. Saved + restored in finally so
        # later tests see the real module again.
        import services as _services_pkg
        import sys as _sys
        saved_attr = getattr(_services_pkg, 'archive_watchdog', None)
        saved_mod = _sys.modules.get('services.archive_watchdog')
        if hasattr(_services_pkg, 'archive_watchdog'):
            delattr(_services_pkg, 'archive_watchdog')
        _sys.modules.pop('services.archive_watchdog', None)
        try:
            yield
        finally:
            archive_worker._last_disk_critical_cleanup_at = 0.0
            if saved_mod is not None:
                _sys.modules['services.archive_watchdog'] = saved_mod
            if saved_attr is not None:
                _services_pkg.archive_watchdog = saved_attr

    def test_critical_triggers_cleanup_thread(self, monkeypatch):
        called = threading.Event()

        def fake_force_prune_now():
            called.set()
            return {'deleted_count': 5, 'freed_bytes': 100,
                    'scanned': 10, 'duration_seconds': 0.1}

        # Inject a fake archive_watchdog before the lazy import.
        import sys
        fake_module = type(sys)('services.archive_watchdog')
        fake_module.force_prune_now = fake_force_prune_now
        monkeypatch.setitem(
            sys.modules, 'services.archive_watchdog', fake_module,
        )

        triggered = archive_worker._maybe_trigger_critical_cleanup('/tmp')
        assert triggered is True
        # Wait for the daemon thread to fire the fake.
        assert called.wait(timeout=2.0), (
            "force_prune_now was not called by the cleanup thread."
        )

    def test_debounce_prevents_re_trigger(self, monkeypatch):
        call_count = [0]

        def fake_force_prune_now():
            call_count[0] += 1
            return {}

        import sys
        fake_module = type(sys)('services.archive_watchdog')
        fake_module.force_prune_now = fake_force_prune_now
        monkeypatch.setitem(
            sys.modules, 'services.archive_watchdog', fake_module,
        )

        # First call fires.
        assert archive_worker._maybe_trigger_critical_cleanup('/tmp') is True
        # Second call within debounce window MUST NOT fire.
        assert archive_worker._maybe_trigger_critical_cleanup('/tmp') is False
        assert archive_worker._maybe_trigger_critical_cleanup('/tmp') is False
        # Wait for the first thread to finish so call_count stabilizes.
        time.sleep(0.2)
        assert call_count[0] == 1, (
            f"Debounce failed: {call_count[0]} calls instead of 1"
        )

    def test_debounce_window_release(self, monkeypatch):
        # After the debounce window elapses, a new call fires.
        monkeypatch.setattr(
            archive_worker, '_DISK_CRITICAL_CLEANUP_DEBOUNCE_SECONDS', 0.0,
        )
        call_count = [0]

        def fake_force_prune_now():
            call_count[0] += 1
            return {}

        import sys
        fake_module = type(sys)('services.archive_watchdog')
        fake_module.force_prune_now = fake_force_prune_now
        monkeypatch.setitem(
            sys.modules, 'services.archive_watchdog', fake_module,
        )

        assert archive_worker._maybe_trigger_critical_cleanup('/tmp') is True
        time.sleep(0.05)  # let first thread complete
        assert archive_worker._maybe_trigger_critical_cleanup('/tmp') is True
        time.sleep(0.2)
        assert call_count[0] == 2

    def test_import_failure_logs_warning(self, monkeypatch, caplog):
        # If archive_watchdog can't be imported, log a warning but
        # don't crash the worker.
        import sys
        # Remove archive_watchdog from sys.modules so the lazy import
        # has a chance to fail. Inject a sentinel that raises.
        original = sys.modules.pop('services.archive_watchdog', None)
        try:
            class _Boom:
                def __getattr__(self, _name):
                    raise ImportError("simulated")

            monkeypatch.setitem(
                sys.modules, 'services.archive_watchdog', _Boom(),
            )
            with caplog.at_level('WARNING', logger='services.archive_worker'):
                archive_worker._maybe_trigger_critical_cleanup('/tmp')
                # Wait for daemon thread.
                time.sleep(0.2)
            warns = [
                r for r in caplog.records
                if r.levelname == 'WARNING'
                and 'disk-critical cleanup' in r.getMessage()
            ]
            assert len(warns) >= 1
        finally:
            if original is not None:
                sys.modules['services.archive_watchdog'] = original

    def test_critical_disk_calls_cleanup_via_process_one_claim(
        self, db, archive_root, teslacam_root, make_clip, monkeypatch,
    ):
        """End-to-end: a disk-critical verdict in process_one_claim
        triggers the cleanup helper. Mocks shutil.disk_usage to force
        critical and asserts _maybe_trigger_critical_cleanup was called.
        """
        clip = make_clip("RecentClips/critical-front.mp4")
        enqueue_for_archive(clip, db_path=db)
        # Force disk_usage to report critical (1 MB free).
        monkeypatch.setattr(
            archive_worker.shutil, 'disk_usage',
            lambda p: _FakeUsage(total=10**12, used=10**12 - 10**6, free=10**6),
        )
        # Capture the trigger call.
        triggered_with: List[str] = []
        original = archive_worker._maybe_trigger_critical_cleanup

        def spy(archive_root_arg):
            triggered_with.append(archive_root_arg)
            return False  # short-circuit so we don't spawn a thread in test

        monkeypatch.setattr(
            archive_worker, '_maybe_trigger_critical_cleanup', spy,
        )

        from services.archive_queue import claim_next_for_worker
        row = claim_next_for_worker('t', db_path=db)
        assert row is not None
        result = archive_worker.process_one_claim(
            row, db, archive_root, teslacam_root,
            chunk_size=4096, max_attempts=3,
        )
        assert result == 'pending'
        assert triggered_with == [archive_root], (
            "process_one_claim must call _maybe_trigger_critical_cleanup "
            "with the archive_root when disk verdict is 'critical'"
        )


# ---------------------------------------------------------------------------
# TestAtomicCopySdioSafeguards (issue #104 mitigations A + B)
# ---------------------------------------------------------------------------


class TestAtomicCopySdioSafeguards:
    """Mid-copy SDIO-contention safeguards in ``_atomic_copy``.

    These guard the per-chunk load-aware backoff (mitigation A) and
    the per-file time budget (mitigation B). Both are part of the
    fix for issue #104 (hardware-watchdog reboots from sustained
    archive backlog drains saturating the shared SDIO controller on
    the Pi Zero 2 W).
    """

    def test_load_pause_disabled_does_not_invoke_sleep(
        self, tmp_path, monkeypatch,
    ):
        # Default load_pause_threshold=0.0 → no syscall, no sleep.
        # Even with a getloadavg that screams "overloaded", the copy
        # finishes without invoking sleep_fn.
        monkeypatch.setattr(
            archive_worker.os, 'getloadavg',
            lambda: (99.0, 99.0, 99.0), raising=False,
        )
        sleep_calls: List[float] = []
        src = tmp_path / "src.bin"
        src.write_bytes(b"X" * 4096)
        dst = tmp_path / "dst.bin"
        archive_worker._atomic_copy(
            str(src), str(dst), 1024,
            sleep_fn=lambda s: sleep_calls.append(s),
        )
        assert dst.read_bytes() == b"X" * 4096
        assert sleep_calls == []

    def test_chunk_pause_fires_when_load_above_threshold(
        self, tmp_path, monkeypatch,
    ):
        # Three chunks of 1024 bytes from a 4096-byte source plus the
        # final empty read. The load-aware backoff fires after each
        # non-empty chunk write, so we expect 4 sleep calls (one per
        # written chunk; the final empty-chunk break exits before the
        # check).
        monkeypatch.setattr(
            archive_worker.os, 'getloadavg',
            lambda: (5.0, 5.0, 5.0), raising=False,
        )
        sleep_calls: List[float] = []
        src = tmp_path / "src.bin"
        src.write_bytes(b"X" * 4096)
        dst = tmp_path / "dst.bin"
        archive_worker._atomic_copy(
            str(src), str(dst), 1024,
            load_pause_threshold=3.5,
            chunk_pause_seconds=0.05,
            sleep_fn=lambda s: sleep_calls.append(s),
        )
        assert dst.read_bytes() == b"X" * 4096
        assert sleep_calls == [0.05, 0.05, 0.05, 0.05]

    def test_chunk_pause_skipped_when_load_below_threshold(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(
            archive_worker.os, 'getloadavg',
            lambda: (1.0, 1.0, 1.0), raising=False,
        )
        sleep_calls: List[float] = []
        src = tmp_path / "src.bin"
        src.write_bytes(b"X" * 4096)
        dst = tmp_path / "dst.bin"
        archive_worker._atomic_copy(
            str(src), str(dst), 1024,
            load_pause_threshold=3.5,
            chunk_pause_seconds=0.05,
            sleep_fn=lambda s: sleep_calls.append(s),
        )
        assert dst.read_bytes() == b"X" * 4096
        assert sleep_calls == []

    def test_getloadavg_failure_falls_back_to_zero(
        self, tmp_path, monkeypatch,
    ):
        # On platforms or containers where getloadavg() raises
        # AttributeError or OSError, we must NOT propagate — fall
        # back to 0.0 so the chunk-pause branch never fires.
        def _raise_oserror():
            raise OSError("not available")
        monkeypatch.setattr(
            archive_worker.os, 'getloadavg', _raise_oserror, raising=False,
        )
        sleep_calls: List[float] = []
        src = tmp_path / "src.bin"
        src.write_bytes(b"X" * 4096)
        dst = tmp_path / "dst.bin"
        archive_worker._atomic_copy(
            str(src), str(dst), 1024,
            load_pause_threshold=3.5,
            chunk_pause_seconds=0.05,
            sleep_fn=lambda s: sleep_calls.append(s),
        )
        assert dst.read_bytes() == b"X" * 4096
        assert sleep_calls == []

    def test_time_budget_disabled_does_not_abort(self, tmp_path):
        # time_budget_seconds=0.0 → no deadline regardless of the clock.
        clock = [0.0]
        def fake_now():
            clock[0] += 1000.0  # explode the clock so any deadline trips
            return clock[0]
        src = tmp_path / "src.bin"
        src.write_bytes(b"X" * 2048)
        dst = tmp_path / "dst.bin"
        # Should NOT raise even though our clock jumps by 1000s/chunk.
        archive_worker._atomic_copy(
            str(src), str(dst), 1024, now_fn=fake_now,
        )
        assert dst.read_bytes() == b"X" * 2048

    def test_time_budget_aborts_and_cleans_partial(
        self, tmp_path,
    ):
        # Inject a clock that crosses the deadline mid-copy. The
        # exception must be _CopyTimeBudgetExceeded (not bare OSError)
        # so the caller can distinguish it. The .partial sidecar must
        # be removed on the abort path.
        clock = [100.0]
        def fake_now():
            # First call (started = now_fn()) returns 100.0.
            # Subsequent calls advance by 5s each, so after the second
            # chunk we are at 110.0 > deadline (105.0).
            ret = clock[0]
            clock[0] += 5.0
            return ret
        src = tmp_path / "src.bin"
        src.write_bytes(b"X" * 4096)
        dst = tmp_path / "dst.bin"
        partial = tmp_path / "dst.bin.partial"
        with pytest.raises(archive_worker._CopyTimeBudgetExceeded) as exc:
            archive_worker._atomic_copy(
                str(src), str(dst), 1024,
                time_budget_seconds=5.0,
                now_fn=fake_now,
            )
        # OSError subclass — guarantees the existing OSError handlers
        # in process_one_claim still match, but the more specific one
        # for time-budget can fire first.
        assert isinstance(exc.value, OSError)
        assert "5.0s budget" in str(exc.value)
        # Final dest never appears (rename never reached).
        assert not dst.exists()
        # The .partial sidecar was cleaned up by the except block.
        assert not partial.exists()

    def test_time_budget_check_happens_before_load_check(
        self, tmp_path, monkeypatch,
    ):
        # Even with load_pause_threshold high enough to fire, a
        # crossed deadline must take priority — we don't want to add
        # an extra sleep before raising.
        monkeypatch.setattr(
            archive_worker.os, 'getloadavg',
            lambda: (99.0, 99.0, 99.0), raising=False,
        )
        sleep_calls: List[float] = []
        clock = [100.0]
        def fake_now():
            ret = clock[0]
            clock[0] += 100.0
            return ret
        src = tmp_path / "src.bin"
        src.write_bytes(b"X" * 4096)
        dst = tmp_path / "dst.bin"
        with pytest.raises(archive_worker._CopyTimeBudgetExceeded):
            archive_worker._atomic_copy(
                str(src), str(dst), 1024,
                load_pause_threshold=3.5,
                chunk_pause_seconds=0.05,
                time_budget_seconds=10.0,
                now_fn=fake_now,
                sleep_fn=lambda s: sleep_calls.append(s),
            )
        # The time-budget check is BEFORE the load check in the chunk
        # body — first chunk crosses the deadline, abort raises with
        # zero sleep_fn invocations.
        assert sleep_calls == []


# ---------------------------------------------------------------------------
# TestProcessOneClaimSdioSafeguards (issue #104)
# ---------------------------------------------------------------------------


class TestProcessOneClaimSdioSafeguards:
    """``process_one_claim`` plumbs the safeguards through to
    ``_atomic_copy`` and treats ``_CopyTimeBudgetExceeded`` distinctly
    from other ``OSError`` subclasses: release back to ``pending``
    without bumping ``attempts``."""

    def test_time_budget_exceeded_releases_without_bumping_attempts(
        self, db, archive_root, teslacam_root, make_clip, monkeypatch,
    ):
        clip = make_clip("RecentClips/budget-front.mp4")
        enqueue_for_archive(clip, db_path=db)

        def _budget_exceeded(src, dst, chunk, **kwargs):
            raise archive_worker._CopyTimeBudgetExceeded(
                "synthetic budget overrun",
            )

        monkeypatch.setattr(archive_worker, '_atomic_copy', _budget_exceeded)
        # Drive the row through process_one_claim several times — it
        # must NEVER reach dead_letter from a time-budget abort, even
        # at max_attempts=3.
        for _ in range(5):
            row = claim_next_for_worker('w', db_path=db)
            assert row is not None
            outcome = archive_worker.process_one_claim(
                row, db, archive_root, teslacam_root,
                chunk_size=4096, max_attempts=3,
                time_budget_seconds=1.0,
            )
            assert outcome == 'pending'
        rows = list_queue(db_path=db)
        # attempts MUST stay at 0 — no burnt retries from a load
        # signal that's not the file's fault.
        assert rows[0]['status'] == 'pending'
        assert rows[0]['attempts'] == 0

    def test_safeguard_kwargs_are_forwarded_to_atomic_copy(
        self, db, archive_root, teslacam_root, make_clip, monkeypatch,
    ):
        # Capture the kwargs that process_one_claim passes through.
        # Issue #109: pin disk_usage to a low-fullness fixture so the
        # adaptive helpers added in #109 don't silently rescale the
        # base values on a host whose real filesystem is ≥ 80% full.
        # (Mirrors the pattern in TestProcessOneClaimAdaptiveWiring.)
        monkeypatch.setattr(
            archive_worker.shutil, 'disk_usage',
            lambda p: _FakeDiskUsage(used_pct=50.0),
        )
        clip = make_clip("RecentClips/forward-front.mp4")
        enqueue_for_archive(clip, db_path=db)
        captured: List[dict] = []

        def _spy(src, dst, chunk, **kwargs):
            captured.append(kwargs)
            # Behave like a successful copy so the row reaches 'copied'.
            # _atomic_copy creates parent dirs; mirror that here.
            parent = os.path.dirname(dst)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(src, 'rb') as f, open(dst, 'wb') as g:
                g.write(f.read())

        monkeypatch.setattr(archive_worker, '_atomic_copy', _spy)
        row = claim_next_for_worker('w', db_path=db)
        outcome = archive_worker.process_one_claim(
            row, db, archive_root, teslacam_root,
            chunk_size=4096, max_attempts=3,
            load_pause_threshold=3.5,
            chunk_pause_seconds=0.25,
            time_budget_seconds=60.0,
        )
        assert outcome == 'copied'
        assert len(captured) == 1
        assert captured[0]['load_pause_threshold'] == 3.5
        assert captured[0]['chunk_pause_seconds'] == 0.25
        assert captured[0]['time_budget_seconds'] == 60.0
        # Issue #109 — at <80% fullness, always-apply must be False
        # so the load-gated path is preserved.
        assert captured[0]['chunk_pause_always'] is False


# ---------------------------------------------------------------------------
# TestReadConfigOrDefaults (issue #104 — config plumbing)
# ---------------------------------------------------------------------------


class TestReadConfigOrDefaults:
    def test_defaults_returned_when_config_unavailable(self):
        # In the test environment ``config`` may not be importable
        # (no CONFIG_FILE pointed by env). The fallback returns the
        # 8-tuple of module-level defaults.
        result = archive_worker._read_config_or_defaults()
        assert isinstance(result, tuple)
        assert len(result) == 8, (
            "Issue #104 added two trailing tunables (chunk_pause, "
            "time_budget); _read_config_or_defaults must return 8 values."
        )
        # Last two are the new mid-copy safeguards.
        assert result[6] == archive_worker._CHUNK_PAUSE_SECONDS
        assert result[7] == archive_worker._PER_FILE_TIME_BUDGET_SECONDS


# ---------------------------------------------------------------------------
# TestMoovVerifyAfterCopy (Phase 2.4 — issue #97 item 2.4)
# ---------------------------------------------------------------------------
#
# These tests pin the contract: an .mp4 copy is only declared successful
# when the destination has both ``ftyp`` and ``moov`` boxes. A
# size-matching copy of an unplayable MP4 (Tesla still writing → no moov
# atom yet) must FAIL the copy so the queue retries — never land in
# ArchivedClips and pollute the indexer.
#
# We test ``_verify_destination_complete`` directly (small box-walk
# correctness) AND end-to-end via ``_atomic_copy`` (raises OSError, leaves
# no orphan partial, leaves no dest file).


class TestMoovVerifyAfterCopy:
    def test_minimal_valid_mp4_passes(self, tmp_path):
        good = tmp_path / "good.mp4"
        good.write_bytes(_build_minimal_mp4())
        assert archive_worker._verify_destination_complete(str(good)) is True

    def test_no_moov_fails(self, tmp_path):
        # ftyp + mdat only — Tesla mid-write looks like this.
        bad = tmp_path / "bad.mp4"
        ftyp = b'\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41'
        mdat = b'\x00\x00\x00\x10mdat' + b'\x00' * 8
        bad.write_bytes(ftyp + mdat)
        assert archive_worker._verify_destination_complete(str(bad)) is False

    def test_no_ftyp_fails(self, tmp_path):
        bad = tmp_path / "noftyp.mp4"
        bad.write_bytes(b'\x00' * 32)
        assert archive_worker._verify_destination_complete(str(bad)) is False

    def test_too_small_fails(self, tmp_path):
        bad = tmp_path / "tiny.mp4"
        bad.write_bytes(b'\x00' * 8)
        assert archive_worker._verify_destination_complete(str(bad)) is False

    def test_extended_64bit_box_size_handled(self, tmp_path):
        # box size==1 means: read next 8 bytes as 64-bit size.
        ftyp = b'\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41'
        # Build an extended-size mdat: size=1, type='mdat', then 64-bit
        # size of 32, then 16 bytes of payload.
        ext_mdat_size = 32  # full box including 16-byte header
        ext_mdat = (
            (1).to_bytes(4, 'big') + b'mdat'
            + ext_mdat_size.to_bytes(8, 'big')
            + b'\x00' * (ext_mdat_size - 16)
        )
        moov = b'\x00\x00\x00\x08moov'
        f = tmp_path / "ext.mp4"
        f.write_bytes(ftyp + ext_mdat + moov)
        assert archive_worker._verify_destination_complete(str(f)) is True

    def test_size_zero_box_at_end_handled(self, tmp_path):
        # box size==0 means: extends to EOF. If it IS moov AND we have
        # already seen mdat (pre-#110: moov alone was sufficient), valid.
        ftyp = b'\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41'
        mdat = b'\x00\x00\x00\x10mdat' + b'\x00' * 8
        moov_eof = (0).to_bytes(4, 'big') + b'moov' + b'\x00' * 100
        f = tmp_path / "moov_eof.mp4"
        f.write_bytes(ftyp + mdat + moov_eof)
        assert archive_worker._verify_destination_complete(str(f)) is True

    def test_size_zero_non_moov_fails(self, tmp_path):
        # mdat that extends to EOF — no moov can follow → reject.
        ftyp = b'\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41'
        mdat_eof = (0).to_bytes(4, 'big') + b'mdat' + b'\x00' * 100
        f = tmp_path / "mdat_eof.mp4"
        f.write_bytes(ftyp + mdat_eof)
        assert archive_worker._verify_destination_complete(str(f)) is False

    def test_box_claiming_past_eof_fails(self, tmp_path):
        # mdat box claims size 1 GiB but file is only 100 bytes.
        ftyp = b'\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41'
        liar = (1024 * 1024 * 1024).to_bytes(4, 'big') + b'mdat' + b'\x00' * 16
        f = tmp_path / "liar.mp4"
        f.write_bytes(ftyp + liar)
        assert archive_worker._verify_destination_complete(str(f)) is False

    def test_walk_is_bounded(self, tmp_path, monkeypatch):
        # Pathological input: thousands of 8-byte ``free`` boxes. The
        # walker must give up after the configured cap.
        ftyp = b'\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41'
        free_box = (8).to_bytes(4, 'big') + b'free'
        body = ftyp + (free_box * 10_000)  # No moov — should reject.
        f = tmp_path / "many_free.mp4"
        f.write_bytes(body)
        # With cap at 512, the walk reads ftyp + 511 free boxes, then
        # bails (returns False because moov never seen).
        assert archive_worker._verify_destination_complete(str(f)) is False

    def test_oserror_returns_false_safely(self, tmp_path):
        assert archive_worker._verify_destination_complete(
            str(tmp_path / "nonexistent.mp4")
        ) is False

    def test_atomic_copy_raises_when_source_lacks_moov(
            self, tmp_path,
    ):
        # End-to-end: source MP4 has size-matching content but no moov.
        # ``_atomic_copy`` must raise OSError and leave no .partial AND
        # no dest file behind.
        src = tmp_path / "source.mp4"
        src.write_bytes(
            b'\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41'
            + b'\x00\x00\x00\x10mdat' + b'\x00' * 8
        )
        dst = tmp_path / "dest.mp4"
        with pytest.raises(OSError, match="missing moov or mdat"):
            archive_worker._atomic_copy(
                str(src), str(dst), chunk_size=4096,
            )
        assert not (tmp_path / "dest.mp4.partial").exists(), \
            "partial must be cleaned up on moov-verify failure"
        assert not dst.exists(), \
            "dest must NOT exist after a moov-verify failure"


    def test_atomic_copy_succeeds_for_well_formed_mp4(self, tmp_path):
        src = tmp_path / "source.mp4"
        content = _build_minimal_mp4(payload=b"hello-world" * 50)
        src.write_bytes(content)
        dst = tmp_path / "dest.mp4"
        archive_worker._atomic_copy(
            str(src), str(dst), chunk_size=4096,
        )
        assert dst.exists()
        assert dst.read_bytes() == content
        assert not (tmp_path / "dest.mp4.partial").exists()

    def test_non_mp4_extension_skips_verification(self, tmp_path):
        # ``.ts`` and other archive types must NOT be moov-verified —
        # they aren't MP4s. A successful size-matching copy is enough.
        src = tmp_path / "source.ts"
        src.write_bytes(b"\x47" * 1024)  # MPEG-TS sync byte stream
        dst = tmp_path / "dest.ts"
        archive_worker._atomic_copy(
            str(src), str(dst), chunk_size=4096,
        )
        assert dst.exists()
        assert dst.read_bytes() == b"\x47" * 1024

    def test_process_one_claim_re_queues_on_moov_failure(
            self, db, archive_root, teslacam_root, make_clip,
    ):
        # End-to-end through process_one_claim: a source file that
        # passes the size check but fails moov-verify must flow through
        # the OSError handler → mark_failed → status 'pending' (with
        # one bumped attempt). The dest must not exist.
        bad_mp4 = (
            b'\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41'
            + b'\x00\x00\x00\x10mdat' + b'\x00' * 8
        )
        clip = make_clip("RecentClips/halfwritten-front.mp4", content=bad_mp4)
        enqueue_for_archive(clip, db_path=db)
        row = claim_next_for_worker('w', db_path=db)
        outcome = archive_worker.process_one_claim(
            row, db, archive_root, teslacam_root,
            chunk_size=4096, max_attempts=3,
        )
        assert outcome == 'pending', (
            "moov-missing copy must transition back to pending so it can "
            "be retried after Tesla finishes writing"
        )
        rows = list_queue(db_path=db)
        assert rows[0]['status'] == 'pending'
        assert rows[0]['attempts'] == 1
        assert 'moov' in (rows[0]['last_error'] or '')
        # No dest file landed in ArchivedClips.
        dest = os.path.join(
            archive_root, "RecentClips", "halfwritten-front.mp4",
        )
        assert not os.path.exists(dest), (
            "Incomplete MP4 must not leak into ArchivedClips"
        )


class TestMdatRequiredAfterCopy:
    """Issue #110 — verifier must reject MP4s that have ``moov`` but
    are missing ``mdat``. Tesla's RecentClips writer can produce this
    layout transiently; pre-#110 the verifier accepted it and the
    indexer dead-lettered the file with "No mdat box found"."""

    def test_no_mdat_with_moov_first_fails(self, tmp_path):
        # ftyp + moov ONLY — the issue #110 case. Pre-fix this passed.
        ftyp = b'\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41'
        moov = b'\x00\x00\x00\x08moov'
        f = tmp_path / "no_mdat.mp4"
        f.write_bytes(ftyp + moov)
        assert archive_worker._verify_destination_complete(str(f)) is False

    def test_mdat_then_moov_passes(self, tmp_path):
        # Standard layout: ftyp + mdat + moov (moov at end).
        ftyp = b'\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41'
        mdat = b'\x00\x00\x00\x10mdat' + b'\x00' * 8
        moov = b'\x00\x00\x00\x08moov'
        f = tmp_path / "std_layout.mp4"
        f.write_bytes(ftyp + mdat + moov)
        assert archive_worker._verify_destination_complete(str(f)) is True

    def test_moov_then_mdat_passes(self, tmp_path):
        # Tesla RecentClips layout: ftyp + moov + mdat (moov at start).
        # When BOTH boxes are present this is also valid.
        ftyp = b'\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41'
        moov = b'\x00\x00\x00\x08moov'
        mdat = b'\x00\x00\x00\x10mdat' + b'\x00' * 8
        f = tmp_path / "moov_first.mp4"
        f.write_bytes(ftyp + moov + mdat)
        assert archive_worker._verify_destination_complete(str(f)) is True

    def test_size_zero_mdat_at_end_with_prior_moov_passes(self, tmp_path):
        # mdat extends to EOF, moov already seen → valid.
        ftyp = b'\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41'
        moov = b'\x00\x00\x00\x08moov'
        mdat_eof = (0).to_bytes(4, 'big') + b'mdat' + b'\x00' * 100
        f = tmp_path / "mdat_eof_after_moov.mp4"
        f.write_bytes(ftyp + moov + mdat_eof)
        assert archive_worker._verify_destination_complete(str(f)) is True

    def test_size_zero_moov_at_end_with_prior_mdat_passes(self, tmp_path):
        # moov extends to EOF, mdat already seen → valid.
        ftyp = b'\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41'
        mdat = b'\x00\x00\x00\x10mdat' + b'\x00' * 8
        moov_eof = (0).to_bytes(4, 'big') + b'moov' + b'\x00' * 100
        f = tmp_path / "moov_eof_after_mdat.mp4"
        f.write_bytes(ftyp + mdat + moov_eof)
        assert archive_worker._verify_destination_complete(str(f)) is True

    def test_atomic_copy_raises_when_source_lacks_mdat(self, tmp_path):
        # End-to-end variant for the issue #110 layout: source has
        # ftyp + moov but no mdat. ``_atomic_copy`` must raise.
        src = tmp_path / "source.mp4"
        src.write_bytes(
            b'\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41'
            + b'\x00\x00\x00\x08moov'
        )
        dst = tmp_path / "dest.mp4"
        with pytest.raises(OSError, match="missing moov or mdat"):
            archive_worker._atomic_copy(
                str(src), str(dst), chunk_size=4096,
            )
        assert not (tmp_path / "dest.mp4.partial").exists()
        assert not dst.exists()


# ---------------------------------------------------------------------------
# Phase 4.4 (#101) — drain-rate ETA
# ---------------------------------------------------------------------------

class TestDrainRateETA:
    """``_compute_drain_rate`` + ``compute_eta_seconds`` + ``get_status``."""

    def setup_method(self):
        archive_worker._recent_copy_completions.clear()

    def teardown_method(self):
        archive_worker._recent_copy_completions.clear()

    def test_no_samples_returns_none(self):
        rate = archive_worker._compute_drain_rate()
        assert rate['rate_per_sec'] is None
        assert rate['samples'] == 0
        assert rate['stale'] is False

    def test_below_min_samples_returns_none(self):
        archive_worker._recent_copy_completions.append(100.0)
        archive_worker._recent_copy_completions.append(110.0)
        rate = archive_worker._compute_drain_rate(now=120.0)
        assert rate['rate_per_sec'] is None
        assert rate['samples'] == 2

    def test_three_samples_yields_rate(self):
        # 3 completions over 6 s = 2 gaps / 6 s = 0.333 files/sec.
        for t in (100.0, 103.0, 106.0):
            archive_worker._recent_copy_completions.append(t)
        rate = archive_worker._compute_drain_rate(now=107.0)
        assert rate['rate_per_sec'] == pytest.approx(2.0 / 6.0, rel=1e-3)
        assert rate['samples'] == 3
        assert rate['stale'] is False
        assert rate['window_age_sec'] == pytest.approx(6.0)

    def test_stale_window_returns_none_with_stale_flag(self):
        # Most recent sample is 10 minutes + 1 second old → stale.
        for t in (100.0, 103.0, 106.0):
            archive_worker._recent_copy_completions.append(t)
        now = 106.0 + 601.0
        rate = archive_worker._compute_drain_rate(now=now)
        assert rate['rate_per_sec'] is None
        assert rate['stale'] is True
        assert rate['samples'] == 3

    def test_rolling_window_caps_at_50(self):
        # Append 60 samples; deque keeps only the most recent 50.
        for t in range(60):
            archive_worker._recent_copy_completions.append(float(t))
        rate = archive_worker._compute_drain_rate(now=60.0)
        assert rate['samples'] == 50
        # 50 samples spanning 49 s = 49/49 = 1.0 file/sec.
        assert rate['rate_per_sec'] == pytest.approx(1.0)

    def test_compute_eta_no_queue_returns_none(self):
        assert archive_worker.compute_eta_seconds(0, 1.0) is None

    def test_compute_eta_no_rate_returns_none(self):
        assert archive_worker.compute_eta_seconds(100, None) is None
        assert archive_worker.compute_eta_seconds(100, 0.0) is None
        assert archive_worker.compute_eta_seconds(100, -0.5) is None

    def test_compute_eta_basic_division(self):
        # 1000 files at 2 files/sec = 500 s.
        assert archive_worker.compute_eta_seconds(1000, 2.0) == 500

    def test_compute_eta_caps_at_24h(self):
        # 1 file every hour with 30k pending = 30k hours → suppress.
        assert archive_worker.compute_eta_seconds(30000, 1.0 / 3600) is None

    def test_compute_eta_just_under_cap_is_returned(self):
        # 1 file/sec × (24h - 1s) → returned.
        seconds_under_cap = 24 * 3600 - 1
        assert archive_worker.compute_eta_seconds(
            seconds_under_cap, 1.0,
        ) == seconds_under_cap

    def test_compute_eta_sub_second_returns_none(self):
        """Sub-second ETAs (rate >> queue) must return None to avoid
        the asymmetric ``eta_seconds: 0`` + ``eta_human: None`` API
        combination. The user gets no signal from "<1 min" anyway."""
        # 1 file at 100 files/sec = 0.01 s → suppress.
        assert archive_worker.compute_eta_seconds(1, 100.0) is None
        # 5 files at 10 files/sec = 0.5 s → still suppress.
        assert archive_worker.compute_eta_seconds(5, 10.0) is None
        # 1 file at exactly 1 file/sec = 1 s → return 1.
        assert archive_worker.compute_eta_seconds(1, 1.0) == 1
        # 2 files at 1 file/sec = 2 s → return 2.
        assert archive_worker.compute_eta_seconds(2, 1.0) == 2

    def test_get_status_surfaces_eta_fields(self, tmp_path):
        # End-to-end: build a status with a real DB, populate the deque
        # by hand (no need to drive the full worker loop), and confirm
        # the status snapshot exposes both the rate and the ETA.
        db_path = str(tmp_path / "geodata.db")
        _init_db(db_path).close()
        archive_worker._db_path = db_path
        try:
            for t in (100.0, 103.0, 106.0):
                archive_worker._recent_copy_completions.append(t)
            # Patch time.time to keep the window fresh.
            with patch.object(archive_worker.time, 'time', return_value=107.0):
                snap = archive_worker.get_status()
            # No queue → eta_seconds is None even with a valid rate.
            assert snap['eta_seconds'] is None
            assert snap['drain_rate_per_sec'] == pytest.approx(2.0 / 6.0, rel=1e-3)
            assert snap['drain_rate_samples'] == 3
            assert snap['drain_rate_stale'] is False
        finally:
            archive_worker._db_path = None

    def test_get_status_with_queue_yields_eta(self, tmp_path):
        db_path = str(tmp_path / "geodata.db")
        _init_db(db_path).close()
        # Prime the queue with 10 pending rows.
        for i in range(10):
            f = tmp_path / f"clip{i}.mp4"
            f.write_bytes(b"x")
            enqueue_for_archive(str(f), db_path=db_path)
        archive_worker._db_path = db_path
        try:
            # Rate = 2 files/sec → ETA = 10/2 = 5 s.
            now = 100.0
            for t in (now - 4.0, now - 3.0, now - 2.0,
                      now - 1.0, now):
                archive_worker._recent_copy_completions.append(t)
            with patch.object(archive_worker.time, 'time', return_value=now + 0.1):
                snap = archive_worker.get_status()
            assert snap['queue_depth'] == 10
            assert snap['drain_rate_per_sec'] == pytest.approx(1.0, rel=0.05)
            assert snap['eta_seconds'] is not None
            assert 9 <= snap['eta_seconds'] <= 11

        finally:
            archive_worker._db_path = None


# ---------------------------------------------------------------------------
# Phase 4.5 (#101) — pause-state helpers expose threshold + total
# ---------------------------------------------------------------------------

class TestPauseStateExtensions:
    """Phase 4.5 extends ``get_disk_pause_state`` and
    ``get_load_pause_state`` with the configured thresholds and (for
    disk) the total bytes so the System Health card can render
    ``"load 4.2 > 3.5"`` and ``"SD card 96% full"`` instead of an
    opaque ``"Paused (load or disk)"``.

    These tests pin the dict shape; the human-readable formatting is
    pinned separately in ``test_system_health_blueprint.py``.
    """

    def setup_method(self):
        # Module-level ``_state`` leaks between tests in the file.
        # Reset the disk/load pause slots so we observe a clean default.
        with archive_worker._state_lock:
            archive_worker._state['last_disk_pause_at'] = None
            archive_worker._state['last_disk_pause_free_mb'] = None
            archive_worker._state['last_disk_pause_total_mb'] = None
            archive_worker._state['last_load_pause_at'] = None
            archive_worker._state['last_load_pause_loadavg'] = None
        archive_worker._disk_space_pause_until = 0.0
        archive_worker._load_pause_until = 0.0

    teardown_method = setup_method

    def test_get_disk_pause_state_includes_thresholds(self):
        # Default state (no pause has armed). Both thresholds are
        # positive integers resolved from config (or fallback
        # constants); the last_* fields are None.
        state = archive_worker.get_disk_pause_state()
        assert isinstance(state['critical_threshold_mb'], int)
        assert isinstance(state['warning_threshold_mb'], int)
        assert state['critical_threshold_mb'] > 0
        # Warning threshold must be >= critical (the watchdog turns
        # warn before it turns critical).
        assert state['warning_threshold_mb'] >= state['critical_threshold_mb']
        # Phase 4.5 fields default to None when no pause has fired.
        assert state['last_pause_at'] is None
        assert state['last_free_mb'] is None
        assert state['last_total_mb'] is None
        # Existing fields still present (regression guard).
        assert state['paused_until_epoch'] == 0.0
        assert state['is_paused_now'] is False

    def test_get_disk_pause_state_after_pause_includes_total(
        self, db, archive_root, monkeypatch,
    ):
        # Simulate the pause-arming side-effect by populating
        # ``_state`` directly the way ``process_one_claim`` does
        # under the disk-space guard. Phase 4.5 added the
        # ``last_disk_pause_total_mb`` slot so the formatter can
        # render "% full".
        with archive_worker._state_lock:
            archive_worker._state['last_disk_pause_at'] = 1234.5
            archive_worker._state['last_disk_pause_free_mb'] = 1024
            archive_worker._state['last_disk_pause_total_mb'] = 25600
        try:
            state = archive_worker.get_disk_pause_state()
            assert state['last_pause_at'] == 1234.5
            assert state['last_free_mb'] == 1024
            assert state['last_total_mb'] == 25600
        finally:
            with archive_worker._state_lock:
                archive_worker._state['last_disk_pause_at'] = None
                archive_worker._state['last_disk_pause_free_mb'] = None
                archive_worker._state['last_disk_pause_total_mb'] = None

    def test_get_load_pause_state_includes_threshold(self):
        # Phase 4.5 added ``threshold`` so the System Health card
        # can render ``"load 4.2 > 3.5"`` without re-reading config.
        state = archive_worker.get_load_pause_state()
        assert 'threshold' in state
        assert isinstance(state['threshold'], (int, float))
        assert state['threshold'] > 0
        # Threshold value must match what
        # ``_read_config_or_defaults()`` returns (so config edits flow
        # through without a service restart). Position [4] is
        # ``load_pause_threshold``.
        assert state['threshold'] == \
            archive_worker._read_config_or_defaults()[4]

    def test_get_load_pause_state_threshold_falls_back_on_config_error(
        self, monkeypatch,
    ):
        # If config import fails (e.g. config.yaml missing), the
        # helper must fall back to the module-level constant rather
        # than raising — the System Health card poll happens every
        # 5 s and a single bad poll should never break the page.
        def boom(*_a, **_kw):
            raise RuntimeError("config import failed")

        monkeypatch.setattr(
            archive_worker, '_read_config_or_defaults', boom,
        )
        state = archive_worker.get_load_pause_state()
        assert state['threshold'] == archive_worker._LOAD_PAUSE_THRESHOLD

    def test_start_worker_resets_disk_total_mb(self, db, archive_root):
        # Parity with the Phase 1 reset of ``last_disk_pause_free_mb``.
        # Without this the new total would leak across worker
        # restarts (e.g. mode switch → restart).
        with archive_worker._state_lock:
            archive_worker._state['last_disk_pause_total_mb'] = 12345

        archive_worker.start_worker(db, archive_root, teslacam_root=None)
        try:
            with archive_worker._state_lock:
                assert archive_worker._state['last_disk_pause_total_mb'] is None
        finally:
            archive_worker.stop_worker(timeout=5)


# ---------------------------------------------------------------------------
# Issue #109 — disk-fullness-adaptive throttling helpers + integration
# ---------------------------------------------------------------------------


class _FakeDiskUsage:
    """Mimic :func:`shutil.disk_usage` return value.

    Default total is 100 GiB so even at 99% used the free space stays
    well above the 100 MB ``disk_space_critical_mb`` guard floor.
    """

    def __init__(self, used_pct: float, total_bytes: int = 100 * 1024 * 1024 * 1024):
        self.total = total_bytes
        self.used = int(total_bytes * used_pct / 100.0)
        self.free = total_bytes - self.used


class TestDiskFullnessHelper:
    """Issue #109 — ``_disk_fullness_pct`` returns used%-of-total or None."""

    def test_returns_correct_percentage(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            archive_worker.shutil, 'disk_usage',
            lambda p: _FakeDiskUsage(used_pct=42.0),
        )
        assert archive_worker._disk_fullness_pct(str(tmp_path)) == pytest.approx(42.0)

    def test_returns_none_on_oserror(self, monkeypatch, tmp_path):
        def _raise(_p):
            raise OSError("simulated stat failure")
        monkeypatch.setattr(archive_worker.shutil, 'disk_usage', _raise)
        assert archive_worker._disk_fullness_pct(str(tmp_path)) is None

    def test_returns_none_on_zero_total(self, monkeypatch, tmp_path):
        # Defensive — a degenerate disk_usage report mustn't divide by 0.
        class _Zero:
            total = 0
            used = 0
            free = 0
        monkeypatch.setattr(
            archive_worker.shutil, 'disk_usage', lambda p: _Zero(),
        )
        assert archive_worker._disk_fullness_pct(str(tmp_path)) is None


class TestAdaptiveLoadThreshold:
    """Issue #109 mitigation #2 — load_pause_threshold scales DOWN as
    disk fills."""

    def test_below_80_returns_base_unchanged(self):
        assert archive_worker._adaptive_load_threshold(3.5, 50.0) == 3.5
        assert archive_worker._adaptive_load_threshold(3.5, 79.9) == 3.5

    def test_80_to_90_subtracts_half(self):
        assert archive_worker._adaptive_load_threshold(3.5, 80.0) == 3.0
        assert archive_worker._adaptive_load_threshold(3.5, 89.9) == 3.0

    def test_90_to_95_subtracts_one(self):
        assert archive_worker._adaptive_load_threshold(3.5, 90.0) == 2.5
        assert archive_worker._adaptive_load_threshold(3.5, 94.9) == 2.5

    def test_at_or_above_95_subtracts_one_and_a_half(self):
        assert archive_worker._adaptive_load_threshold(3.5, 95.0) == 2.0
        assert archive_worker._adaptive_load_threshold(3.5, 99.0) == 2.0
        assert archive_worker._adaptive_load_threshold(3.5, 100.0) == 2.0

    def test_floor_at_one_for_low_base(self):
        # Misconfigured low base must never let the guard fully disable.
        assert archive_worker._adaptive_load_threshold(1.5, 95.0) == 1.0
        assert archive_worker._adaptive_load_threshold(2.0, 90.0) == 1.0
        # base=2.5 at 90%: 2.5 - 1.0 = 1.5, no floor needed
        assert archive_worker._adaptive_load_threshold(2.5, 90.0) == 1.5

    def test_zero_base_disables_returns_zero(self):
        assert archive_worker._adaptive_load_threshold(0.0, 95.0) == 0.0

    def test_negative_base_returns_unchanged(self):
        # Caller treats <=0 as "disabled", helper must not mangle it.
        assert archive_worker._adaptive_load_threshold(-1.0, 95.0) == -1.0

    def test_unknown_fullness_returns_base_unchanged(self):
        assert archive_worker._adaptive_load_threshold(3.5, None) == 3.5


class TestAdaptiveChunkPause:
    """Issue #109 mitigation #4 — chunk_pause_seconds scales UP and
    flips to always-apply at high disk fullness."""

    def test_below_80_returns_base_and_load_gated(self):
        # Pre-#109 behavior: unchanged duration, gated on loadavg.
        pause, always = archive_worker._adaptive_chunk_pause(0.25, 50.0)
        assert pause == 0.25
        assert always is False

    def test_80_to_95_keeps_duration_but_always_applies(self):
        pause, always = archive_worker._adaptive_chunk_pause(0.25, 80.0)
        assert pause == 0.25
        assert always is True
        pause, always = archive_worker._adaptive_chunk_pause(0.25, 94.9)
        assert pause == 0.25
        assert always is True

    def test_at_or_above_95_doubles_duration_and_always_applies(self):
        pause, always = archive_worker._adaptive_chunk_pause(0.25, 95.0)
        assert pause == 0.5
        assert always is True
        pause, always = archive_worker._adaptive_chunk_pause(0.25, 99.0)
        assert pause == 0.5
        assert always is True

    def test_zero_base_disables_at_all_fullness_levels(self):
        # base==0 means user explicitly disabled the chunk pause —
        # respect that even at 99% fullness.
        pause, always = archive_worker._adaptive_chunk_pause(0.0, 99.0)
        assert pause == 0.0
        assert always is False

    def test_floor_for_very_small_base_when_always_apply(self):
        # 0.001 s base at 80% → max(0.001, 0.05) = 0.05
        pause, always = archive_worker._adaptive_chunk_pause(0.001, 80.0)
        assert pause == 0.05
        assert always is True
        # 0.001 s base at 95% → max(0.002, 0.05) = 0.05
        pause, always = archive_worker._adaptive_chunk_pause(0.001, 95.0)
        assert pause == 0.05
        assert always is True

    def test_unknown_fullness_returns_base_and_load_gated(self):
        pause, always = archive_worker._adaptive_chunk_pause(0.25, None)
        assert pause == 0.25
        assert always is False


class TestAtomicCopyChunkPauseAlways:
    """Issue #109 mitigation #4 wired through ``_atomic_copy``: when
    ``chunk_pause_always=True`` the per-chunk pause fires every chunk
    regardless of current loadavg."""

    def test_always_apply_sleeps_every_chunk(self, tmp_path):
        # 32 KiB file copied in 8 KiB chunks → 4 chunks → 4 sleeps,
        # regardless of loadavg (set to 0 here).
        src = tmp_path / "src.bin"
        src.write_bytes(b"x" * 32_768)
        dst = tmp_path / "dst.bin"
        sleeps: List[float] = []
        archive_worker._atomic_copy(
            str(src), str(dst), chunk_size=8192,
            load_pause_threshold=0.0,
            chunk_pause_seconds=0.5,
            chunk_pause_always=True,
            sleep_fn=lambda s: sleeps.append(s),
        )
        assert dst.read_bytes() == b"x" * 32_768
        assert sleeps == [0.5, 0.5, 0.5, 0.5], (
            "always_apply must sleep on every chunk"
        )

    def test_always_apply_skipped_when_pause_zero(self, tmp_path):
        # If base pause is 0 even in always-apply mode, no sleep.
        src = tmp_path / "src.bin"
        src.write_bytes(b"y" * 16_384)
        dst = tmp_path / "dst.bin"
        sleeps: List[float] = []
        archive_worker._atomic_copy(
            str(src), str(dst), chunk_size=8192,
            load_pause_threshold=0.0,
            chunk_pause_seconds=0.0,
            chunk_pause_always=True,
            sleep_fn=lambda s: sleeps.append(s),
        )
        assert sleeps == []

    def test_load_gated_path_unchanged_when_always_apply_false(self, tmp_path,
                                                                monkeypatch):
        # With chunk_pause_always=False, the pre-#109 load-gated path
        # runs: sleep only when loadavg > load_pause_threshold.
        # raising=False because Windows ``os`` lacks ``getloadavg``.
        monkeypatch.setattr(
            archive_worker.os, 'getloadavg',
            lambda: (5.0, 0.0, 0.0), raising=False,
        )
        src = tmp_path / "src.bin"
        src.write_bytes(b"z" * 16_384)
        dst = tmp_path / "dst.bin"
        sleeps: List[float] = []
        archive_worker._atomic_copy(
            str(src), str(dst), chunk_size=8192,
            load_pause_threshold=3.5,
            chunk_pause_seconds=0.25,
            chunk_pause_always=False,
            sleep_fn=lambda s: sleeps.append(s),
        )
        # 16384 / 8192 = 2 chunks → 2 sleeps because load 5.0 > 3.5.
        assert sleeps == [0.25, 0.25]

    def test_load_gated_path_skips_when_load_low(self, tmp_path, monkeypatch):
        # Same as above but loadavg below threshold → no sleeps.
        monkeypatch.setattr(
            archive_worker.os, 'getloadavg',
            lambda: (1.0, 0.0, 0.0), raising=False,
        )
        src = tmp_path / "src.bin"
        src.write_bytes(b"q" * 16_384)
        dst = tmp_path / "dst.bin"
        sleeps: List[float] = []
        archive_worker._atomic_copy(
            str(src), str(dst), chunk_size=8192,
            load_pause_threshold=3.5,
            chunk_pause_seconds=0.25,
            chunk_pause_always=False,
            sleep_fn=lambda s: sleeps.append(s),
        )
        assert sleeps == []


class TestProcessOneClaimAdaptiveWiring:
    """Integration — process_one_claim derives adaptive values from
    ``shutil.disk_usage`` and forwards them to ``_atomic_copy``."""

    def test_high_fullness_enables_always_apply_chunk_pause(
            self, monkeypatch, db, archive_root, tmp_path,
    ):
        # 95% disk fullness → adaptive chunk pause is doubled (0.5s)
        # AND always-apply. Verify _atomic_copy is invoked with those
        # values.
        monkeypatch.setattr(
            archive_worker.shutil, 'disk_usage',
            lambda p: _FakeDiskUsage(used_pct=95.0),
        )
        captured: dict = {}

        def _spy(*args, **kwargs):
            captured.update(kwargs)
            return 1024  # bytes written

        monkeypatch.setattr(archive_worker, '_atomic_copy', _spy)
        monkeypatch.setattr(
            archive_worker, 'compute_dest_path',
            lambda src, root, tcam: os.path.join(root, "dest.mp4"),
        )
        monkeypatch.setattr(
            archive_worker.archive_queue, 'mark_copied',
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            archive_worker, '_enqueue_indexed', lambda *a, **kw: None,
        )

        # Build a synthetic claimed row with fresh stable mtime so the
        # stable-write gate doesn't preempt the copy.
        src = tmp_path / "clip.mp4"
        src.write_bytes(b"X" * 1024)
        old_mtime = time.time() - 3600  # 1 hr old → past stable gate
        os.utime(str(src), (old_mtime, old_mtime))

        row = {
            'id': 1,
            'source_path': str(src),
            'expected_size': 1024,
            'expected_mtime': old_mtime,
        }
        outcome = archive_worker.process_one_claim(
            row, db, archive_root, teslacam_root=None,
            chunk_size=8192, max_attempts=3,
            load_pause_threshold=3.5,
            chunk_pause_seconds=0.25,
            time_budget_seconds=0.0,
        )
        assert outcome == 'copied'
        assert captured['chunk_pause_always'] is True, (
            "95% fullness must enable always-apply"
        )
        assert captured['chunk_pause_seconds'] == 0.5, (
            "95% fullness must double the chunk pause"
        )
        # adaptive load threshold = max(3.5 - 1.5, 1.0) = 2.0
        assert captured['load_pause_threshold'] == 2.0

    def test_low_fullness_preserves_pre_109_behavior(
            self, monkeypatch, db, archive_root, tmp_path,
    ):
        monkeypatch.setattr(
            archive_worker.shutil, 'disk_usage',
            lambda p: _FakeDiskUsage(used_pct=50.0),
        )
        captured: dict = {}

        def _spy(*args, **kwargs):
            captured.update(kwargs)
            return 1024

        monkeypatch.setattr(archive_worker, '_atomic_copy', _spy)
        monkeypatch.setattr(
            archive_worker, 'compute_dest_path',
            lambda src, root, tcam: os.path.join(root, "dest.mp4"),
        )
        monkeypatch.setattr(
            archive_worker.archive_queue, 'mark_copied',
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            archive_worker, '_enqueue_indexed', lambda *a, **kw: None,
        )

        src = tmp_path / "clip.mp4"
        src.write_bytes(b"Y" * 1024)
        old_mtime = time.time() - 3600
        os.utime(str(src), (old_mtime, old_mtime))

        row = {
            'id': 1,
            'source_path': str(src),
            'expected_size': 1024,
            'expected_mtime': old_mtime,
        }
        archive_worker.process_one_claim(
            row, db, archive_root, teslacam_root=None,
            chunk_size=8192, max_attempts=3,
            load_pause_threshold=3.5,
            chunk_pause_seconds=0.25,
            time_budget_seconds=0.0,
        )
        # 50% fullness → no adaptive scaling.
        assert captured['chunk_pause_always'] is False
        assert captured['chunk_pause_seconds'] == 0.25
        assert captured['load_pause_threshold'] == 3.5
