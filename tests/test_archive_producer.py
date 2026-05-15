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


# ---------------------------------------------------------------------------
# Issue #208: SEI-peek decision cache
# ---------------------------------------------------------------------------

class TestPeekCache:
    """Cache should eliminate repeated SEI peeks on stationary RecentClips.

    Pre-fix the producer mmap-read every parked clip on every 60s sweep
    (~700 MB/min on a parked overnight Pi). The cache stores ``False``
    verdicts keyed by ``(path, mtime, size)`` so a re-scan with no file
    change skips the peek entirely.
    """

    def setup_method(self):
        archive_producer.reset_peek_cache()
        archive_producer.reset_skipped_stationary_tally()

    def teardown_method(self):
        # Reset both the cache AND the skipped-stationary tally so the
        # in-memory deque doesn't leak across tests (the storage_retention
        # blueprint's badge endpoint sums producer skips into its
        # 24-hour count, and would otherwise see our test skips as
        # real skips).
        archive_producer.reset_peek_cache()
        archive_producer.reset_skipped_stationary_tally()

    def test_first_call_runs_peek_subsequent_call_uses_cache(
            self, db, tmp_path, monkeypatch,
    ):
        recent = tmp_path / "TeslaCam" / "RecentClips"
        recent.mkdir(parents=True)
        path = str(recent / "2026-05-11_09-00-00-front.mp4")
        with open(path, 'wb') as f:
            f.write(b"x")
        old = time.time() - 60
        os.utime(path, (old, old))
        called = {'count': 0}

        def _fake_peek(_p):
            called['count'] += 1
            return False

        monkeypatch.setattr(
            archive_producer, '_peek_clip_for_gps', _fake_peek,
        )
        # First sweep: peek runs, verdict is cached.
        r1 = archive_producer.enqueue_with_peek([path], db_path=db)
        assert called['count'] == 1
        assert r1['skipped_stationary'] == 1
        # Second sweep with the file unchanged: peek MUST NOT run again.
        r2 = archive_producer.enqueue_with_peek([path], db_path=db)
        assert called['count'] == 1, (
            "cache hit must skip the SEI peek on the second sweep"
        )
        assert r2['skipped_stationary'] == 1

    def test_mtime_change_invalidates_cache_and_repeeks(
            self, db, tmp_path, monkeypatch,
    ):
        recent = tmp_path / "TeslaCam" / "RecentClips"
        recent.mkdir(parents=True)
        path = str(recent / "2026-05-11_09-00-00-front.mp4")
        with open(path, 'wb') as f:
            f.write(b"x")
        old = time.time() - 60
        os.utime(path, (old, old))
        called = {'count': 0}
        monkeypatch.setattr(
            archive_producer, '_peek_clip_for_gps',
            lambda _p: (called.update(count=called['count'] + 1) or False),
        )
        archive_producer.enqueue_with_peek([path], db_path=db)
        assert called['count'] == 1
        # Tesla rotates the slot — mtime changes.
        newer = time.time() - 30
        os.utime(path, (newer, newer))
        archive_producer.enqueue_with_peek([path], db_path=db)
        assert called['count'] == 2, (
            "mtime change must invalidate the cache entry"
        )
        stats = archive_producer.get_peek_cache_stats()
        assert stats['invalidations'] >= 1

    def test_size_change_invalidates_cache_and_repeeks(
            self, db, tmp_path, monkeypatch,
    ):
        recent = tmp_path / "TeslaCam" / "RecentClips"
        recent.mkdir(parents=True)
        path = str(recent / "2026-05-11_09-00-00-front.mp4")
        with open(path, 'wb') as f:
            f.write(b"x")
        old = time.time() - 60
        os.utime(path, (old, old))
        called = {'count': 0}
        monkeypatch.setattr(
            archive_producer, '_peek_clip_for_gps',
            lambda _p: (called.update(count=called['count'] + 1) or False),
        )
        archive_producer.enqueue_with_peek([path], db_path=db)
        assert called['count'] == 1
        # Same mtime but a different size — Tesla reused the slot with
        # a different-bitrate camera. Cache must invalidate.
        with open(path, 'wb') as f:
            f.write(b"x" * 4096)
        os.utime(path, (old, old))
        archive_producer.enqueue_with_peek([path], db_path=db)
        assert called['count'] == 2

    def test_true_verdict_is_not_cached(self, db, tmp_path, monkeypatch):
        # Caching ``True`` would be pointless (the file gets enqueued
        # and removed from RecentClips by the worker). Verify only False
        # is cached.
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
        archive_producer.enqueue_with_peek([path], db_path=db)
        stats = archive_producer.get_peek_cache_stats()
        assert stats['size'] == 0

    def test_none_verdict_is_not_cached(self, db, tmp_path, monkeypatch):
        # Caching ``None`` (parser failure) is harmful — the next sweep
        # would never retry. Verify only False is cached.
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
        archive_producer.enqueue_with_peek([path], db_path=db)
        stats = archive_producer.get_peek_cache_stats()
        assert stats['size'] == 0

    def test_cache_bounded_by_max_entries(self, monkeypatch):
        # Stuff more than _PEEK_CACHE_MAX_ENTRIES synthetic entries and
        # verify the cache stays bounded with a non-zero eviction count.
        archive_producer.reset_peek_cache()
        cap = archive_producer._PEEK_CACHE_MAX_ENTRIES
        for i in range(cap + 50):
            archive_producer._peek_cache_store(
                f'/fake/path/{i}.mp4', mtime=1000.0 + i, size=100 + i,
            )
        stats = archive_producer.get_peek_cache_stats()
        assert stats['size'] <= cap
        assert stats['evictions'] > 0

    def test_get_peek_cache_stats_returns_copy(self):
        archive_producer.reset_peek_cache()
        s1 = archive_producer.get_peek_cache_stats()
        s1['size'] = 99999
        s2 = archive_producer.get_peek_cache_stats()
        assert s2['size'] == 0, "stats dict mutation must not leak"


# ---------------------------------------------------------------------------
# Issue #214 — VFS cache invalidation before periodic scan
# ---------------------------------------------------------------------------

class TestRefreshRoMountBeforeScan:
    """Pin issue #214 fix: every ``_scan_once`` MUST invalidate the
    kernel's dentry/inode cache for the RO USB mount BEFORE walking
    the directory tree, otherwise Tesla's gadget-block-layer writes
    are invisible to ``readdir`` and clips are lost when Tesla's
    RecentClips circular buffer rotates them out before we ever see
    them. See issue #214 for the forensic incident report.
    """

    def test_refresh_called_before_iter_archive_candidates(
        self, db, teslacam, monkeypatch,
    ):
        """The cache refresh MUST run before we read the directory.
        If we read first and refresh after, the readdir gets a stale
        snapshot.
        """
        call_order: list[str] = []

        def fake_refresh(path):
            call_order.append(f'refresh:{path}')

        real_iter = archive_producer._iter_archive_candidates

        def tracked_iter(path):
            call_order.append(f'iter:{path}')
            return real_iter(path)

        # Patch the symbol that _scan_once will look up via lazy import.
        import services.mapping_service as ms
        monkeypatch.setattr(ms, '_refresh_ro_mount', fake_refresh)
        monkeypatch.setattr(
            archive_producer, '_iter_archive_candidates', tracked_iter,
        )

        archive_producer._scan_once(teslacam, db)

        assert call_order, "_scan_once must call something"
        assert call_order[0] == f'refresh:{teslacam}', (
            f"refresh must be the first call, got order={call_order}"
        )
        assert any(c.startswith('iter:') for c in call_order), (
            f"iter must run after refresh, got order={call_order}"
        )

    def test_refresh_failure_is_non_fatal(
        self, db, teslacam, monkeypatch,
    ):
        """A broken refresh (e.g. broken sudoers, missing module)
        must NEVER freeze the producer. The scan must continue
        against whatever the kernel cache shows — losing 60 s of
        responsiveness is infinitely better than losing the whole
        producer thread.
        """
        def boom(_path):
            raise RuntimeError("simulated broken sudoers")

        import services.mapping_service as ms
        monkeypatch.setattr(ms, '_refresh_ro_mount', boom)

        # Must complete without raising; result should match the
        # baseline 5-clip teslacam fixture (3 SentryClips/SavedClips
        # events + 2 RecentClips at age-0 → all 5 enqueued).
        result = archive_producer._scan_once(teslacam, db)
        assert result['seen'] == 5
        assert result['enqueued'] == 5

    def test_refresh_is_called_on_every_scan(
        self, db, teslacam, monkeypatch,
    ):
        """The producer's whole reason to exist is the periodic
        rescan. The cache refresh must fire on EVERY call, not just
        once at startup — otherwise long-running processes drift
        back into the stale-cache failure mode after the first scan.
        """
        call_count = {'n': 0}

        def counter(_path):
            call_count['n'] += 1

        import services.mapping_service as ms
        monkeypatch.setattr(ms, '_refresh_ro_mount', counter)

        for _ in range(5):
            archive_producer._scan_once(teslacam, db)

        assert call_count['n'] == 5, (
            f"refresh must fire on every scan, got {call_count['n']} "
            "calls for 5 scans"
        )

    def test_refresh_receives_teslacam_root_argument(
        self, db, teslacam, monkeypatch,
    ):
        """The scan passes ``teslacam_root`` to the refresh helper
        for caller-intent documentation. ``_refresh_ro_mount`` itself
        ignores the argument (drop_caches is process-global) but the
        contract is that the caller documents which mount it cares
        about — pin the wiring so a future refactor doesn't drop the
        argument and silently break the per-mount log line.
        """
        captured: list[str] = []

        def capture(path):
            captured.append(path)

        import services.mapping_service as ms
        monkeypatch.setattr(ms, '_refresh_ro_mount', capture)

        archive_producer._scan_once(teslacam, db)

        assert captured == [teslacam], (
            f"refresh must receive teslacam_root, got {captured}"
        )
