"""Tests for reclaim_stationary_recent_clips (issue #167).

Coverage:

* Stationary RecentClips (waypoint_count = 0 AND event_count = 0) get deleted.
* RecentClips with GPS waypoints are kept.
* RecentClips with detected events are kept.
* Unindexed RecentClips (no row in indexed_files) are kept — the indexer
  hasn't seen them yet, deleting blind would lose footage that might have GPS.
* Files newer than ``min_age_hours`` are kept.
* Stationary RecentClips with a SentryClips/SavedClips counterpart of the
  same basename are kept (Tesla writes the same recording into both folders;
  the saved-event copy is the user-meaningful one).
* SentryClips and SavedClips folders are NEVER touched.
* purge_deleted_videos is called for each deleted file (geodata reconciled).
* Trips / waypoints / detected_events rows are NEVER deleted (the May 7
  contract — only video_path is nulled).
* The protected-file guard (``*.img`` files) refuses to delete.
* Single-flight: a second call short-circuits while a first is in flight.
* Watchdog-not-started returns an error summary instead of raising.
* RecentClips folder missing returns a zero-summary instead of raising.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest

from services import archive_queue
from services import archive_watchdog
from services import archive_worker
from services import task_coordinator
from services.mapping_service import _init_db


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/test_archive_watchdog.py — same module-state contract)
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    """Initialize a fresh geodata.db with the canonical schema."""
    db_path = str(tmp_path / "geodata.db")
    _init_db(db_path).close()
    return db_path


@pytest.fixture
def archive_root(tmp_path):
    p = tmp_path / "ArchivedClips"
    p.mkdir()
    return str(p)


@pytest.fixture(autouse=True)
def _reset_module_state():
    archive_watchdog.stop_watchdog(timeout=5.0)
    archive_worker.stop_worker(timeout=5.0)
    archive_worker._disk_space_pause_until = 0.0
    with task_coordinator._lock:
        task_coordinator._current_task = None
        task_coordinator._task_started = 0.0
        task_coordinator._waiter_count = 0
    archive_watchdog._retention_running = False
    # Default the module-level archive_root / db_path so callers that
    # rely on the watchdog default (no explicit kwargs) don't blow up.
    with archive_watchdog._state_lock:
        archive_watchdog._archive_root = None
        archive_watchdog._db_path = None
    yield
    archive_watchdog.stop_watchdog(timeout=5.0)
    archive_worker.stop_worker(timeout=5.0)
    archive_worker._disk_space_pause_until = 0.0
    with task_coordinator._lock:
        task_coordinator._current_task = None
        task_coordinator._task_started = 0.0
        task_coordinator._waiter_count = 0
    archive_watchdog._retention_running = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_archive_mp4(root: str, rel: str, *,
                      mtime: float | None = None,
                      size: int = 100) -> str:
    full = os.path.normpath(os.path.join(root, rel))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'wb') as f:
        f.write(b"X" * size)
    if mtime is not None:
        os.utime(full, (mtime, mtime))
    return full


def _index(db_path: str, file_path: str, *,
           waypoint_count: int = 0, event_count: int = 0,
           file_size: int = 100):
    """Insert an indexed_files row for ``file_path``."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO indexed_files(file_path, file_size, "
            "indexed_at, waypoint_count, event_count) "
            "VALUES (?, ?, '2025-01-01T00:00:00Z', ?, ?)",
            (file_path, file_size, waypoint_count, event_count),
        )


# Pick an mtime well outside the default 1 h "too new" guard.
OLD_MTIME = time.time() - (2 * 3600)


# ---------------------------------------------------------------------------
# TestReclaimStationary — happy paths and the per-file decision matrix
# ---------------------------------------------------------------------------


class TestReclaimStationary:
    def test_stationary_recent_clip_is_deleted(self, db, archive_root):
        path = _make_archive_mp4(
            archive_root, "RecentClips/stationary.mp4", mtime=OLD_MTIME,
        )
        _index(db, path, waypoint_count=0, event_count=0)
        result = archive_watchdog.reclaim_stationary_recent_clips(
            db_path=db, archive_root=archive_root, min_age_hours=1,
        )
        assert result['deleted_count'] == 1
        assert result['freed_bytes'] > 0
        assert not os.path.exists(path)

    def test_recent_clip_with_gps_waypoints_is_kept(self, db, archive_root):
        path = _make_archive_mp4(
            archive_root, "RecentClips/has_gps.mp4", mtime=OLD_MTIME,
        )
        _index(db, path, waypoint_count=42, event_count=0)
        result = archive_watchdog.reclaim_stationary_recent_clips(
            db_path=db, archive_root=archive_root, min_age_hours=1,
        )
        assert result['deleted_count'] == 0
        assert result['kept_has_gps'] == 1
        assert os.path.isfile(path)

    def test_recent_clip_with_events_is_kept_in_event_only_bucket(
            self, db, archive_root):
        """Stationary clip with events lands in `kept_has_event_only`,
        not the `kept_has_gps` bucket. The bucket distinction matters
        to operators reading the JSON: a Sentry-mode-while-parked
        event is NOT GPS data.
        """
        path = _make_archive_mp4(
            archive_root, "RecentClips/has_event.mp4", mtime=OLD_MTIME,
        )
        _index(db, path, waypoint_count=0, event_count=3)
        result = archive_watchdog.reclaim_stationary_recent_clips(
            db_path=db, archive_root=archive_root, min_age_hours=1,
        )
        assert result['deleted_count'] == 0
        assert result['kept_has_event_only'] == 1
        assert result['kept_has_gps'] == 0
        assert os.path.isfile(path)

    def test_unindexed_recent_clip_is_kept(self, db, archive_root):
        path = _make_archive_mp4(
            archive_root, "RecentClips/unseen.mp4", mtime=OLD_MTIME,
        )
        # Deliberately do NOT index — the indexer hasn't seen it yet.
        result = archive_watchdog.reclaim_stationary_recent_clips(
            db_path=db, archive_root=archive_root, min_age_hours=1,
        )
        assert result['deleted_count'] == 0
        assert result['kept_unindexed'] == 1
        assert os.path.isfile(path)

    def test_too_new_clip_is_kept(self, db, archive_root):
        # 30 minutes old — within the 1-hour default guard.
        path = _make_archive_mp4(
            archive_root, "RecentClips/fresh.mp4",
            mtime=time.time() - 1800,
        )
        _index(db, path, waypoint_count=0, event_count=0)
        result = archive_watchdog.reclaim_stationary_recent_clips(
            db_path=db, archive_root=archive_root, min_age_hours=1,
        )
        assert result['deleted_count'] == 0
        assert result['kept_too_new'] == 1
        assert os.path.isfile(path)

    def test_min_age_hours_zero_allows_any_age(self, db, archive_root):
        # 1 second old — would normally be guarded.
        path = _make_archive_mp4(
            archive_root, "RecentClips/just_made.mp4",
            mtime=time.time() - 1,
        )
        _index(db, path, waypoint_count=0, event_count=0)
        result = archive_watchdog.reclaim_stationary_recent_clips(
            db_path=db, archive_root=archive_root, min_age_hours=0,
        )
        assert result['deleted_count'] == 1
        assert not os.path.exists(path)

    def test_sentry_counterpart_keeps_stationary_recent_clip(
            self, db, archive_root):
        # Tesla wrote the SAME recording into RecentClips AND SentryClips.
        # The recent copy looks stationary (no GPS) — but the Sentry
        # copy is the user-meaningful event recording. Keep the recent
        # copy so the user sees consistent storage usage and never
        # has a deleted-but-nominally-stationary clip.
        recent = _make_archive_mp4(
            archive_root, "RecentClips/2025-01-01_12-00-00-front.mp4",
            mtime=OLD_MTIME,
        )
        sentry = _make_archive_mp4(
            archive_root,
            "SentryClips/2025-01-01_12-00-00/2025-01-01_12-00-00-front.mp4",
            mtime=OLD_MTIME,
        )
        _index(db, recent, waypoint_count=0, event_count=0)
        result = archive_watchdog.reclaim_stationary_recent_clips(
            db_path=db, archive_root=archive_root, min_age_hours=1,
        )
        assert result['deleted_count'] == 0
        assert result['kept_has_event_counterpart'] == 1
        assert os.path.isfile(recent)
        assert os.path.isfile(sentry)

    def test_saved_counterpart_keeps_stationary_recent_clip(
            self, db, archive_root):
        recent = _make_archive_mp4(
            archive_root, "RecentClips/2025-02-02_14-00-00-front.mp4",
            mtime=OLD_MTIME,
        )
        saved = _make_archive_mp4(
            archive_root,
            "SavedClips/2025-02-02_14-00-00/2025-02-02_14-00-00-front.mp4",
            mtime=OLD_MTIME,
        )
        _index(db, recent, waypoint_count=0, event_count=0)
        result = archive_watchdog.reclaim_stationary_recent_clips(
            db_path=db, archive_root=archive_root, min_age_hours=1,
        )
        assert result['deleted_count'] == 0
        assert result['kept_has_event_counterpart'] == 1
        assert os.path.isfile(recent)
        assert os.path.isfile(saved)

    def test_sentry_clips_folder_is_never_walked_for_deletion(
            self, db, archive_root):
        """Even a stationary indexed SentryClips clip must never be touched."""
        sentry = _make_archive_mp4(
            archive_root,
            "SentryClips/2025-03-03_08-00-00/2025-03-03_08-00-00-front.mp4",
            mtime=OLD_MTIME,
        )
        _index(db, sentry, waypoint_count=0, event_count=0)
        result = archive_watchdog.reclaim_stationary_recent_clips(
            db_path=db, archive_root=archive_root, min_age_hours=1,
        )
        assert result['deleted_count'] == 0
        assert os.path.isfile(sentry)

    def test_saved_clips_folder_is_never_walked_for_deletion(
            self, db, archive_root):
        saved = _make_archive_mp4(
            archive_root,
            "SavedClips/2025-04-04_18-00-00/2025-04-04_18-00-00-front.mp4",
            mtime=OLD_MTIME,
        )
        _index(db, saved, waypoint_count=0, event_count=0)
        result = archive_watchdog.reclaim_stationary_recent_clips(
            db_path=db, archive_root=archive_root, min_age_hours=1,
        )
        assert result['deleted_count'] == 0
        assert os.path.isfile(saved)

    def test_freed_bytes_matches_actual_size(self, db, archive_root):
        size = 4096
        path = _make_archive_mp4(
            archive_root, "RecentClips/sized.mp4",
            mtime=OLD_MTIME, size=size,
        )
        _index(db, path, waypoint_count=0, event_count=0)
        result = archive_watchdog.reclaim_stationary_recent_clips(
            db_path=db, archive_root=archive_root, min_age_hours=1,
        )
        assert result['deleted_count'] == 1
        assert result['freed_bytes'] == size

    def test_returns_summary_with_required_fields(self, db, archive_root):
        result = archive_watchdog.reclaim_stationary_recent_clips(
            db_path=db, archive_root=archive_root, min_age_hours=1,
        )
        for field in (
            'deleted_count', 'freed_bytes', 'scanned',
            'kept_too_new', 'kept_has_event_counterpart',
            'kept_unindexed', 'kept_has_gps', 'kept_has_event_only',
            'min_age_hours', 'duration_seconds',
        ):
            assert field in result, f"missing summary field: {field}"
        assert result['min_age_hours'] == 1


# ---------------------------------------------------------------------------
# TestReclaimGeodataContract — preserve trips/waypoints/events (May 7)
# ---------------------------------------------------------------------------


class TestReclaimGeodataContract:
    def test_purge_deleted_videos_called_per_deleted_file(
            self, db, archive_root, monkeypatch):
        paths = [
            _make_archive_mp4(
                archive_root, "RecentClips/a.mp4", mtime=OLD_MTIME,
            ),
            _make_archive_mp4(
                archive_root, "RecentClips/b.mp4", mtime=OLD_MTIME,
            ),
        ]
        for p in paths:
            _index(db, p, waypoint_count=0, event_count=0)

        purged: list = []
        from services import mapping_service

        def _spy(db_path, *, deleted_paths):
            purged.append([os.path.normpath(p) for p in deleted_paths])
        monkeypatch.setattr(
            mapping_service, 'purge_deleted_videos', _spy,
        )

        result = archive_watchdog.reclaim_stationary_recent_clips(
            db_path=db, archive_root=archive_root, min_age_hours=1,
        )
        assert result['deleted_count'] == 2
        assert len(purged) == 2
        flat = [p for sub in purged for p in sub]
        assert set(flat) == {os.path.normpath(p) for p in paths}

    def test_trips_waypoints_events_preserved_when_video_reclaimed(
            self, db, archive_root):
        """May 7 contract: reclaim must NOT cascade-delete trip data."""
        path = _make_archive_mp4(
            archive_root, "RecentClips/tripclip.mp4", mtime=OLD_MTIME,
        )
        rel_video_path = "RecentClips/tripclip.mp4"
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO trips(start_time, end_time, source_folder) "
                "VALUES ('2025-01-01T10:00:00Z','2025-01-01T11:00:00Z','test')"
            )
            trip_id = conn.execute(
                "SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO waypoints(trip_id, lat, lon, "
                "timestamp, video_path) VALUES (?, 37.0, -122.0, "
                "'2025-01-01T10:00:01Z', ?)",
                (trip_id, rel_video_path),
            )
            conn.execute(
                "INSERT INTO detected_events(trip_id, event_type, "
                "timestamp, lat, lon, video_path) VALUES "
                "(?, 'sentry', '2025-01-01T10:00:01Z', 37.0, -122.0, ?)",
                (trip_id, rel_video_path),
            )
        _index(db, path, waypoint_count=0, event_count=0)

        with sqlite3.connect(db) as conn:
            trip_before = conn.execute(
                "SELECT COUNT(*) FROM trips").fetchone()[0]
            wpt_before = conn.execute(
                "SELECT COUNT(*) FROM waypoints").fetchone()[0]
            evt_before = conn.execute(
                "SELECT COUNT(*) FROM detected_events").fetchone()[0]

        result = archive_watchdog.reclaim_stationary_recent_clips(
            db_path=db, archive_root=archive_root, min_age_hours=1,
        )
        assert result['deleted_count'] == 1

        with sqlite3.connect(db) as conn:
            trip_after = conn.execute(
                "SELECT COUNT(*) FROM trips").fetchone()[0]
            wpt_after = conn.execute(
                "SELECT COUNT(*) FROM waypoints").fetchone()[0]
            evt_after = conn.execute(
                "SELECT COUNT(*) FROM detected_events").fetchone()[0]
            wpt_video_path = conn.execute(
                "SELECT video_path FROM waypoints WHERE trip_id=?",
                (trip_id,),
            ).fetchone()[0]
            evt_video_path = conn.execute(
                "SELECT video_path FROM detected_events WHERE trip_id=?",
                (trip_id,),
            ).fetchone()[0]
            idx_after = conn.execute(
                "SELECT COUNT(*) FROM indexed_files").fetchone()[0]

        assert trip_after == trip_before, \
            "Reclaim must NOT delete trips (May 7 contract)"
        assert wpt_after == wpt_before, \
            "Reclaim must NOT delete waypoints (May 7 contract)"
        assert evt_after == evt_before, \
            "Reclaim must NOT delete detected_events (May 7 contract)"
        assert wpt_video_path is None
        assert evt_video_path is None
        assert idx_after == 0


# ---------------------------------------------------------------------------
# TestReclaimSafetyGuards — img protection, error paths, single-flight
# ---------------------------------------------------------------------------


class TestReclaimSafetyGuards:
    def test_img_files_excluded_at_walk_stage(self, db, archive_root):
        """``_iter_archive_mp4_files`` filters by ``.mp4`` extension,
        so a stray ``.img`` file in RecentClips never enters the
        candidate loop. This is layer 1 of two-layer protection;
        ``test_protected_file_doorway_invoked_for_img_candidate`` below
        covers layer 2 (the ``safe_delete_archive_video`` doorway).
        """
        path = _make_archive_mp4(
            archive_root, "RecentClips/usb_cam.img", mtime=OLD_MTIME,
        )
        _index(db, path, waypoint_count=0, event_count=0)
        result = archive_watchdog.reclaim_stationary_recent_clips(
            db_path=db, archive_root=archive_root, min_age_hours=1,
        )
        assert result['deleted_count'] == 0
        assert os.path.isfile(path)

    def test_protected_file_doorway_invoked_for_protected_candidate(
            self, db, archive_root, monkeypatch):
        """Layer 2 protection: even if a ``.mp4`` candidate makes it
        into the loop, ``safe_delete_archive_video`` returns
        ``DeleteOutcome.PROTECTED`` for protected paths, and the
        function MUST treat that as "not deleted" (no delete count,
        no freed bytes, no purge_deleted_videos call).

        Exercises the doorway by monkeypatching it to return PROTECTED
        for one of the candidates. Verifies the count/bytes/purge
        accounting stays consistent.
        """
        protected_path = _make_archive_mp4(
            archive_root, "RecentClips/protected.mp4",
            mtime=OLD_MTIME, size=2048,
        )
        normal_path = _make_archive_mp4(
            archive_root, "RecentClips/normal.mp4",
            mtime=OLD_MTIME, size=4096,
        )
        _index(db, protected_path, waypoint_count=0, event_count=0)
        _index(db, normal_path, waypoint_count=0, event_count=0)

        from services import file_safety
        real_delete = file_safety.safe_delete_archive_video

        def _patched_delete(path):
            # Refuse the protected candidate at the doorway.
            normalized = os.path.normpath(path)
            if normalized.endswith('protected.mp4'):
                return file_safety.DeleteResult(
                    outcome=file_safety.DeleteOutcome.PROTECTED,
                    bytes_freed=0,
                )
            return real_delete(path)

        # Patch the symbol that ``_delete_one_mp4`` looks up at call time.
        monkeypatch.setattr(
            file_safety, 'safe_delete_archive_video', _patched_delete,
        )

        purged: list = []
        from services import mapping_service

        def _spy(db_path, *, deleted_paths):
            purged.extend(os.path.normpath(p) for p in deleted_paths)
        monkeypatch.setattr(
            mapping_service, 'purge_deleted_videos', _spy,
        )

        result = archive_watchdog.reclaim_stationary_recent_clips(
            db_path=db, archive_root=archive_root, min_age_hours=1,
        )
        # Only the non-protected candidate was deleted.
        assert result['deleted_count'] == 1
        assert result['freed_bytes'] == 4096
        assert os.path.isfile(protected_path), \
            "Protected file MUST survive even when listed as a candidate"
        assert not os.path.exists(normal_path)
        # purge_deleted_videos called exactly once — for the deleted
        # file only, never for the doorway-rejected one.
        assert len(purged) == 1
        assert purged[0].endswith('normal.mp4')

    def test_watchdog_not_started_returns_error(self, archive_root):
        # No db_path supplied AND module-level _db_path is None.
        result = archive_watchdog.reclaim_stationary_recent_clips(
            archive_root=archive_root,
        )
        assert result['deleted_count'] == 0
        assert 'error' in result

    def test_recent_clips_folder_missing_returns_zero_summary(
            self, db, tmp_path):
        # archive_root exists but contains no RecentClips/ subfolder.
        empty_root = str(tmp_path / "empty_archive")
        os.makedirs(empty_root)
        result = archive_watchdog.reclaim_stationary_recent_clips(
            db_path=db, archive_root=empty_root, min_age_hours=1,
        )
        assert result['deleted_count'] == 0
        assert result['scanned'] == 0
        assert 'error' not in result

    def test_single_flight_short_circuits_concurrent_call(
            self, db, archive_root):
        """A second call while the first is in flight returns
        ``status='already_running'`` instead of stacking another prune.
        """
        # Set the module-level guard manually to simulate an in-flight prune.
        with archive_watchdog._state_lock:
            archive_watchdog._retention_running = True
        try:
            result = archive_watchdog.reclaim_stationary_recent_clips(
                db_path=db, archive_root=archive_root, min_age_hours=1,
            )
            assert result.get('status') == 'already_running'
            assert result['deleted_count'] == 0
        finally:
            with archive_watchdog._state_lock:
                archive_watchdog._retention_running = False

    def test_module_level_defaults_used_when_kwargs_omitted(
            self, db, archive_root):
        """If ``start_watchdog`` was called, kwargs default to those values."""
        path = _make_archive_mp4(
            archive_root, "RecentClips/default_call.mp4", mtime=OLD_MTIME,
        )
        _index(db, path, waypoint_count=0, event_count=0)
        # Simulate start_watchdog setting module state without actually
        # starting the thread (we don't want a real thread in tests).
        with archive_watchdog._state_lock:
            archive_watchdog._db_path = db
            archive_watchdog._archive_root = archive_root
        try:
            result = archive_watchdog.reclaim_stationary_recent_clips(
                min_age_hours=1,
            )
            assert result['deleted_count'] == 1
            assert not os.path.exists(path)
        finally:
            with archive_watchdog._state_lock:
                archive_watchdog._db_path = None
                archive_watchdog._archive_root = None

    def test_symlinked_archive_root_does_not_silently_no_op(
            self, db, tmp_path):
        """Path-normalization regression guard: an archive_root passed
        as a symlink must not break the in-loop ``stationary_set``
        membership check.

        Failure mode that we're guarding against: ``indexed_files``
        rows store the realpath'd absolute path (because the indexer
        runs ``os.path.realpath``), so the collector would build a set
        of realpath'd paths. If the worker loop iterates ``os.walk``
        results that contain the symlink prefix instead, every
        membership check returns False and the function silently
        no-ops. Both sides realpath now.
        """
        if not hasattr(os, 'symlink'):
            pytest.skip("symlinks not supported on this platform")
        # Real archive root with the actual files.
        real_root = tmp_path / "real_archives"
        real_root.mkdir()
        # Symlink that callers might pass in instead.
        link_root = tmp_path / "link_archives"
        try:
            os.symlink(str(real_root), str(link_root),
                       target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            pytest.skip(f"cannot create symlink in this env: {e}")
        # Make a stationary RecentClips file under the REAL root.
        real_path = _make_archive_mp4(
            str(real_root), "RecentClips/sym.mp4", mtime=OLD_MTIME,
        )
        # Index it under the SYMLINK path (simulating an indexer
        # invocation with a symlinked archive_root). The collector
        # realpaths these on read so they end up in the same set.
        sym_indexed_path = os.path.join(
            str(link_root), "RecentClips", "sym.mp4",
        )
        _index(db, sym_indexed_path, waypoint_count=0, event_count=0)
        # Now invoke with the SYMLINK as archive_root. The walk yields
        # link-prefixed paths; the collector realpaths to real-prefixed.
        # Without the realpath fix (Finding 1), membership returns
        # False and nothing gets deleted.
        result = archive_watchdog.reclaim_stationary_recent_clips(
            db_path=db, archive_root=str(link_root), min_age_hours=1,
        )
        assert result['deleted_count'] == 1, \
            "Symlinked archive_root must not silently no-op"
        assert not os.path.exists(real_path)
