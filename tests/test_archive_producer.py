"""Tests for the archive_queue producer thread (services.archive_producer).

Phase 2a producer for issue #76. These tests cover:

* Directory walk: catches all .mp4 in RecentClips (flat) and event
  subfolders of SentryClips/SavedClips.
* Walk handles missing root, missing subdirs, permission errors.
* Synchronous one-shot scan (run_boot_catchup_once).
* Producer thread lifecycle: start (idempotent), stop, status snapshot.
* Producer respects ``boot_catchup_enabled=False`` (no immediate scan).
* Producer survives an exception inside one scan iteration.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from services import archive_producer, archive_queue
from services.mapping_service import _init_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _stop_any_producer():
    """Make sure no leftover producer thread is running."""
    archive_producer.stop_producer(timeout=5.0)
    yield
    archive_producer.stop_producer(timeout=5.0)


@pytest.fixture
def db(tmp_path):
    db_path = str(tmp_path / "geodata.db")
    _init_db(db_path).close()
    return db_path


@pytest.fixture
def teslacam(tmp_path):
    """Synthesize a TeslaCam tree:

    TeslaCam/
      RecentClips/
        2026-05-11_09-00-00-front.mp4
        2026-05-11_09-01-00-front.mp4
      SentryClips/
        2026-05-11_09-30-00/
          front.mp4
          back.mp4
      SavedClips/
        2026-05-11_10-00-00/
          front.mp4
    """
    root = tmp_path / "TeslaCam"
    recent = root / "RecentClips"; recent.mkdir(parents=True)
    (recent / "2026-05-11_09-00-00-front.mp4").write_bytes(b"x")
    (recent / "2026-05-11_09-01-00-front.mp4").write_bytes(b"x")
    sentry_event = root / "SentryClips" / "2026-05-11_09-30-00"
    sentry_event.mkdir(parents=True)
    (sentry_event / "front.mp4").write_bytes(b"x")
    (sentry_event / "back.mp4").write_bytes(b"x")
    saved_event = root / "SavedClips" / "2026-05-11_10-00-00"
    saved_event.mkdir(parents=True)
    (saved_event / "front.mp4").write_bytes(b"x")
    return str(root)


# ---------------------------------------------------------------------------
# Directory walk
# ---------------------------------------------------------------------------

class TestIterArchiveCandidates:
    def test_collects_all_mp4_under_three_subdirs(self, teslacam):
        paths = archive_producer._iter_archive_candidates(teslacam)
        assert len(paths) == 5
        names = sorted(os.path.basename(p) for p in paths)
        assert names == [
            '2026-05-11_09-00-00-front.mp4',
            '2026-05-11_09-01-00-front.mp4',
            'back.mp4',
            'front.mp4',
            'front.mp4',
        ]

    def test_missing_root_returns_empty(self, tmp_path):
        ghost = str(tmp_path / "no_such_dir")
        assert archive_producer._iter_archive_candidates(ghost) == []

    def test_empty_root_returns_empty(self, tmp_path):
        empty = tmp_path / "TeslaCam"; empty.mkdir()
        assert archive_producer._iter_archive_candidates(str(empty)) == []

    def test_partial_tree_does_not_crash(self, tmp_path):
        # Only RecentClips exists; SentryClips/SavedClips missing.
        root = tmp_path / "TeslaCam"
        recent = root / "RecentClips"; recent.mkdir(parents=True)
        (recent / "a.mp4").write_bytes(b"x")
        out = archive_producer._iter_archive_candidates(str(root))
        assert len(out) == 1

    def test_ignores_non_mp4_files(self, teslacam):
        # Drop a stray non-mp4 file in RecentClips and an event folder
        recent = os.path.join(teslacam, 'RecentClips')
        with open(os.path.join(recent, 'thumb.jpg'), 'wb') as f:
            f.write(b"not a video")
        with open(os.path.join(teslacam, 'SentryClips',
                               '2026-05-11_09-30-00', 'event.json'), 'w') as f:
            f.write('{}')
        paths = archive_producer._iter_archive_candidates(teslacam)
        assert all(p.lower().endswith('.mp4') for p in paths)
        assert len(paths) == 5

    def test_case_insensitive_extension(self, tmp_path):
        root = tmp_path / "TeslaCam"
        recent = root / "RecentClips"; recent.mkdir(parents=True)
        (recent / "x.MP4").write_bytes(b"x")
        (recent / "y.Mp4").write_bytes(b"x")
        paths = archive_producer._iter_archive_candidates(str(root))
        assert len(paths) == 2

    def test_empty_root_arg(self):
        assert archive_producer._iter_archive_candidates('') == []
        assert archive_producer._iter_archive_candidates(None) == []  # type: ignore


# ---------------------------------------------------------------------------
# Synchronous scan helper
# ---------------------------------------------------------------------------

class TestRunBootCatchupOnce:
    def test_enqueues_all_clips_first_run(self, db, teslacam):
        result = archive_producer.run_boot_catchup_once(teslacam, db_path=db)
        assert result == {'seen': 5, 'enqueued': 5, 'skipped_stationary': 0}
        assert archive_queue.get_queue_status(db_path=db)['pending'] == 5

    def test_second_run_enqueues_zero_due_to_dedup(self, db, teslacam):
        archive_producer.run_boot_catchup_once(teslacam, db_path=db)
        result = archive_producer.run_boot_catchup_once(teslacam, db_path=db)
        assert result == {'seen': 5, 'enqueued': 0, 'skipped_stationary': 0}
        assert archive_queue.get_queue_status(db_path=db)['pending'] == 5

    def test_picks_up_new_clip_between_runs(self, db, teslacam):
        archive_producer.run_boot_catchup_once(teslacam, db_path=db)
        # Tesla writes a new RecentClips file
        new_clip = os.path.join(teslacam, 'RecentClips',
                                '2026-05-11_09-02-00-front.mp4')
        with open(new_clip, 'wb') as f:
            f.write(b"new")
        result = archive_producer.run_boot_catchup_once(teslacam, db_path=db)
        assert result == {'seen': 6, 'enqueued': 1, 'skipped_stationary': 0}

    def test_priorities_are_inferred(self, db, teslacam):
        archive_producer.run_boot_catchup_once(teslacam, db_path=db)
        rows = archive_queue.list_queue(limit=100, db_path=db)
        priorities = sorted(r['priority'] for r in rows)
        # Issue #178: events (P1) and RecentClips (P2). The catch-up
        # fixture seeds 3 SentryClips events and 2 RecentClips, so
        # the sorted priority list is [1, 1, 1, 2, 2].
        assert priorities == [1, 1, 1, 2, 2]


# ---------------------------------------------------------------------------
# Producer thread lifecycle
# ---------------------------------------------------------------------------

class TestProducerLifecycle:
    def test_start_then_stop(self, db, teslacam):
        # Use long interval — we just want the boot-catchup pass to run.
        assert archive_producer.start_producer(
            teslacam, db_path=db,
            rescan_interval_seconds=60.0,
            boot_catchup_enabled=True,
        ) is True

        # Wait for the boot catch-up to complete (up to 5 s)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if archive_queue.get_queue_status(db_path=db)['pending'] == 5:
                break
            time.sleep(0.1)

        status = archive_producer.get_producer_status()
        assert status['running'] is True
        assert status['teslacam_root'] == teslacam
        assert status['iterations'] >= 1
        assert status['last_seen'] == 5

        assert archive_producer.stop_producer(timeout=5.0) is True
        assert archive_producer.get_producer_status()['running'] is False

    def test_start_is_idempotent(self, db, teslacam):
        assert archive_producer.start_producer(
            teslacam, db_path=db,
            rescan_interval_seconds=60.0,
        ) is True
        # Second call returns False, doesn't spawn a second thread.
        assert archive_producer.start_producer(
            teslacam, db_path=db,
            rescan_interval_seconds=60.0,
        ) is False
        archive_producer.stop_producer(timeout=5.0)

    def test_stop_when_not_running_returns_true(self):
        # No thread alive; stop is a no-op.
        assert archive_producer.stop_producer(timeout=1.0) is True

    def test_boot_catchup_disabled_skips_first_pass(self, db, teslacam):
        # With a long interval and boot_catchup off, no scan should
        # have run by the time we stop.
        archive_producer.start_producer(
            teslacam, db_path=db,
            rescan_interval_seconds=60.0,
            boot_catchup_enabled=False,
        )
        time.sleep(0.5)  # Give the thread a moment to settle
        status = archive_producer.get_producer_status()
        # Iterations didn't increment because boot_catchup is gated
        # off and the first interval is 60 s.
        assert status['iterations'] == 0
        assert archive_queue.get_queue_status(db_path=db)['pending'] == 0
        archive_producer.stop_producer(timeout=5.0)

    def test_boot_scan_defer_postpones_first_scan(self, db, teslacam):
        # boot_scan_defer_seconds > 0 should delay the first scan even
        # when boot_catchup is enabled. Use a long defer + short total
        # observation window to confirm the producer hasn't scanned yet.
        archive_producer.start_producer(
            teslacam, db_path=db,
            rescan_interval_seconds=60.0,
            boot_catchup_enabled=True,
            boot_scan_defer_seconds=5.0,
        )
        time.sleep(0.5)  # Well under the 5s defer
        status = archive_producer.get_producer_status()
        assert status['iterations'] == 0, (
            "First scan should be deferred by boot_scan_defer_seconds; "
            "running it immediately defeats the SDIO contention guard."
        )
        assert archive_queue.get_queue_status(db_path=db)['pending'] == 0
        archive_producer.stop_producer(timeout=5.0)

    def test_boot_scan_defer_zero_preserves_immediate_scan(self, db, teslacam):
        # With defer=0, the original immediate-scan behavior must be
        # preserved (back-compat for callers that don't pass the arg).
        archive_producer.start_producer(
            teslacam, db_path=db,
            rescan_interval_seconds=60.0,
            boot_catchup_enabled=True,
            boot_scan_defer_seconds=0.0,
        )
        time.sleep(0.8)  # Enough for the first scan to complete
        status = archive_producer.get_producer_status()
        assert status['iterations'] >= 1, (
            "With defer=0 the producer must scan immediately; "
            "regressed back-compat for the start_producer signature."
        )
        archive_producer.stop_producer(timeout=5.0)

    def test_periodic_rescan_picks_up_new_files(self, db, teslacam):
        # Short interval so we can observe two scans
        archive_producer.start_producer(
            teslacam, db_path=db,
            rescan_interval_seconds=0.3,
            boot_catchup_enabled=True,
        )

        # Wait for first scan
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if archive_queue.get_queue_status(db_path=db)['pending'] == 5:
                break
            time.sleep(0.05)
        assert archive_queue.get_queue_status(db_path=db)['pending'] == 5

        # Drop a new clip; next scan should catch it
        new_clip = os.path.join(teslacam, 'RecentClips', 'new.mp4')
        with open(new_clip, 'wb') as f:
            f.write(b"new")

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if archive_queue.get_queue_status(db_path=db)['pending'] == 6:
                break
            time.sleep(0.1)
        assert archive_queue.get_queue_status(db_path=db)['pending'] == 6

        archive_producer.stop_producer(timeout=5.0)

    def test_scan_exception_does_not_kill_thread(self, db, teslacam,
                                                 monkeypatch):
        # Monkeypatch _scan_once so the first call raises, the second
        # succeeds. Thread must still be alive after the exception.
        calls = {'n': 0}
        original_scan = archive_producer._scan_once

        def failing_scan(root, db_path):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError("synthetic scan failure")
            return original_scan(root, db_path)

        monkeypatch.setattr(archive_producer, '_scan_once', failing_scan)

        archive_producer.start_producer(
            teslacam, db_path=db,
            rescan_interval_seconds=0.2,
            boot_catchup_enabled=True,
        )

        # Wait for at least 2 iterations (first fails, second succeeds)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if calls['n'] >= 2 and archive_queue.get_queue_status(
                db_path=db
            )['pending'] == 5:
                break
            time.sleep(0.05)

        status = archive_producer.get_producer_status()
        assert status['running'] is True
        assert calls['n'] >= 2
        # Earlier failure recorded then cleared on success
        archive_producer.stop_producer(timeout=5.0)


# ---------------------------------------------------------------------------
# Producer status snapshot
# ---------------------------------------------------------------------------

class TestProducerStatus:
    def test_status_initial_state(self):
        # No thread started yet — running=False, no fields populated.
        status = archive_producer.get_producer_status()
        assert status['running'] is False

    def test_status_after_start_includes_config(self, db, teslacam):
        archive_producer.start_producer(
            teslacam, db_path=db,
            rescan_interval_seconds=42.0,
            boot_catchup_enabled=False,
        )
        status = archive_producer.get_producer_status()
        assert status['teslacam_root'] == teslacam
        assert status['rescan_interval_seconds'] == 42.0
        assert status['boot_catchup_enabled'] is False
        archive_producer.stop_producer(timeout=5.0)


# ---------------------------------------------------------------------------
# Issue #184 Wave 2 — Phase B: SEI peek at the producer
# ---------------------------------------------------------------------------


class TestEnqueueWithPeek:
    """Phase B moves the stationary-clip skip from the worker to the
    producer. Tests cover the three peek outcomes (True / False / None)
    and the freshness gate that defers fresh files to the worker."""

    @pytest.fixture(autouse=True)
    def _reset_tally(self):
        archive_producer.reset_skipped_stationary_tally()
        yield
        archive_producer.reset_skipped_stationary_tally()

    def test_event_clips_skip_peek_and_enqueue_directly(self, db, tmp_path,
                                                         monkeypatch):
        # Sentry/Saved event clips bypass the SEI peek entirely.
        # Force the peek function to assert it's NOT called for these.
        called = {'count': 0}

        def _fail_peek(_path):
            called['count'] += 1
            return False  # if we wrongly called it, force a skip

        monkeypatch.setattr(
            archive_producer, '_peek_clip_for_gps', _fail_peek,
        )
        sentry_clip = tmp_path / "TeslaCam" / "SentryClips" / "evt"
        sentry_clip.mkdir(parents=True)
        path = str(sentry_clip / "2026-05-11_09-00-00-front.mp4")
        with open(path, 'wb') as f:
            f.write(b"x")
        result = archive_producer.enqueue_with_peek([path], db_path=db)
        assert result['enqueued'] == 1
        assert result['skipped_stationary'] == 0
        assert called['count'] == 0

    def test_recentclips_with_no_gps_is_skipped(self, db, tmp_path,
                                                  monkeypatch):
        # SEI peek returns False → producer drops the clip and bumps
        # the in-memory tally.
        recent = tmp_path / "TeslaCam" / "RecentClips"
        recent.mkdir(parents=True)
        path = str(recent / "2026-05-11_09-00-00-front.mp4")
        with open(path, 'wb') as f:
            f.write(b"x")
        # Backdate the file so the freshness gate doesn't bypass the peek.
        old = time.time() - 60
        os.utime(path, (old, old))
        monkeypatch.setattr(
            archive_producer, '_peek_clip_for_gps', lambda _p: False,
        )
        result = archive_producer.enqueue_with_peek([path], db_path=db)
        assert result['enqueued'] == 0
        assert result['skipped_stationary'] == 1
        assert archive_producer.get_skipped_stationary_count(24) == 1
        # Confirm no row was written.
        from services import archive_queue
        assert archive_queue.get_queue_status(db_path=db)['pending'] == 0

    def test_recentclips_with_gps_is_enqueued(self, db, tmp_path,
                                                monkeypatch):
        recent = tmp_path / "TeslaCam" / "RecentClips"
        recent.mkdir(parents=True)
        path = str(recent / "2026-05-11_09-00-00-front.mp4")
        with open(path, 'wb') as f:
            f.write(b"x")
        old = time.time() - 60
        os.utime(path, (old, old))
        monkeypatch.setattr(
            archive_producer, '_peek_clip_for_gps', lambda _p: True,
        )
        result = archive_producer.enqueue_with_peek([path], db_path=db)
        assert result['enqueued'] == 1
        assert result['skipped_stationary'] == 0

    def test_recentclips_with_unknown_verdict_is_enqueued(self, db, tmp_path,
                                                            monkeypatch):
        # Peek returns None (parse error) — must fall through to enqueue
        # so a parser bug never silently drops a clip.
        recent = tmp_path / "TeslaCam" / "RecentClips"
        recent.mkdir(parents=True)
        path = str(recent / "2026-05-11_09-00-00-front.mp4")
        with open(path, 'wb') as f:
            f.write(b"x")
        old = time.time() - 60
        os.utime(path, (old, old))
        monkeypatch.setattr(
            archive_producer, '_peek_clip_for_gps', lambda _p: None,
        )
        result = archive_producer.enqueue_with_peek([path], db_path=db)
        assert result['enqueued'] == 1
        assert result['skipped_stationary'] == 0

    def test_fresh_recentclips_bypass_peek(self, db, tmp_path, monkeypatch):
        # File mtime is now() — younger than stable_write_age. Producer
        # must enqueue without calling the peek so the worker's stable-
        # write gate can handle freshness.
        recent = tmp_path / "TeslaCam" / "RecentClips"
        recent.mkdir(parents=True)
        path = str(recent / "2026-05-11_09-00-00-front.mp4")
        with open(path, 'wb') as f:
            f.write(b"x")
        called = {'count': 0}

        def _peek_should_not_run(_path):
            called['count'] += 1
            return False

        monkeypatch.setattr(
            archive_producer, '_peek_clip_for_gps', _peek_should_not_run,
        )
        result = archive_producer.enqueue_with_peek([path], db_path=db)
        assert called['count'] == 0
        assert result['enqueued'] == 1
        assert result['skipped_stationary'] == 0

    def test_skipped_stationary_count_horizon_evicts_old_entries(self):
        # Manually push timestamps from 25 hours ago into the deque.
        from services.archive_producer import (
            _skipped_tally, _skipped_tally_lock,
        )
        ancient = time.time() - 25 * 3600
        with _skipped_tally_lock:
            _skipped_tally.append(ancient)
        # 24-hour horizon must drop the ancient entry.
        assert archive_producer.get_skipped_stationary_count(24) == 0

    def test_reset_skipped_stationary_tally_clears(self, db, tmp_path,
                                                     monkeypatch):
        recent = tmp_path / "TeslaCam" / "RecentClips"
        recent.mkdir(parents=True)
        path = str(recent / "2026-05-11_09-00-00-front.mp4")
        with open(path, 'wb') as f:
            f.write(b"x")
        old = time.time() - 60
        os.utime(path, (old, old))
        monkeypatch.setattr(
            archive_producer, '_peek_clip_for_gps', lambda _p: False,
        )
        archive_producer.enqueue_with_peek([path], db_path=db)
        assert archive_producer.get_skipped_stationary_count(24) == 1
        archive_producer.reset_skipped_stationary_tally()
        assert archive_producer.get_skipped_stationary_count(24) == 0
