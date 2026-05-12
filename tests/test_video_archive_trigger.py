"""Tests for the Phase 2b ``video_archive_service`` shims (issue #76).

The legacy ``_archive_timer_loop`` periodic thread + ``_archive_pending``
duplicate-guard pattern is gone. ``video_archive_service`` is now a thin
compatibility layer over ``archive_worker``:

* ``start_archive_timer()`` → ``archive_worker.ensure_worker_started()``
* ``stop_archive_timer()``  → ``archive_worker.stop_worker()``
* ``trigger_archive_now()`` → ``archive_worker.wake()``

The old tests asserted on the deleted internals (``_run_archive``,
``_archive_pending``, ``_archive_timer_loop``, the ionice-on-the-process
bug). Those are no longer reachable code paths and have been deleted.
The Phase 2b worker has its own pause/resume/wake/dead-letter test
coverage in ``test_archive_worker.py``.
"""

from unittest.mock import patch

import pytest

from services import video_archive_service as vas


@pytest.fixture(autouse=True)
def _reset_state():
    """Snapshot/restore module-level state so test order doesn't matter.

    The shim layer no longer carries ``_archive_pending`` or any other
    duplicate-guard mutex (the worker is a singleton thread; that IS
    the guard). All we need to preserve is ``ARCHIVE_ENABLED`` so a
    test that toggles it doesn't bleed into the next.
    """
    saved_enabled = vas.ARCHIVE_ENABLED
    yield
    vas.ARCHIVE_ENABLED = saved_enabled


class TestTriggerArchiveNow:
    """``trigger_archive_now()`` is now a thin wrapper that delegates
    to ``archive_worker.wake()`` after a short config check."""

    def test_disabled_returns_false(self):
        # When ARCHIVE_ENABLED is False the wrapper short-circuits and
        # never touches the worker — returning False so callers know
        # nothing happened.
        vas.ARCHIVE_ENABLED = False
        with patch('services.archive_worker.wake') as mock_wake, \
             patch(
                 'services.archive_worker.ensure_worker_started',
             ) as mock_start:
            assert vas.trigger_archive_now() is False
            mock_wake.assert_not_called()
            mock_start.assert_not_called()

    def test_enabled_calls_worker_wake(self):
        vas.ARCHIVE_ENABLED = True
        with patch('services.archive_worker.wake') as mock_wake, \
             patch(
                 'services.archive_worker.ensure_worker_started',
             ) as mock_start:
            assert vas.trigger_archive_now() is True
            mock_start.assert_called_once()
            mock_wake.assert_called_once()

    def test_wake_failure_is_swallowed(self):
        # The wrapper is called from the NM dispatcher
        # (``helpers/refresh_cloud_token.py``). A worker-side
        # exception MUST NOT propagate up — the caller treats False
        # as "no archive started" and moves on to the cloud sync
        # trigger. Silent fail keeps WiFi-connect resilient.
        vas.ARCHIVE_ENABLED = True
        with patch(
            'services.archive_worker.ensure_worker_started',
        ), patch(
            'services.archive_worker.wake',
            side_effect=RuntimeError("synthetic"),
        ):
            # Should not raise. False return is acceptable.
            result = vas.trigger_archive_now()
            assert result is False


class TestStartStopShims:
    """``start_archive_timer`` and ``stop_archive_timer`` are now pure
    delegations to the worker; the legacy internal thread is gone."""

    def test_start_archive_timer_starts_worker(self):
        with patch(
            'services.archive_worker.ensure_worker_started',
        ) as mock_start:
            vas.start_archive_timer()
            mock_start.assert_called_once()

    def test_start_archive_timer_swallows_worker_failure(self):
        # Same resilience contract as trigger_archive_now: a worker
        # startup failure must not crash gadget_web's main thread.
        with patch(
            'services.archive_worker.ensure_worker_started',
            side_effect=RuntimeError("synthetic"),
        ):
            # Should not raise.
            vas.start_archive_timer()

    def test_stop_archive_timer_stops_worker(self):
        with patch('services.archive_worker.stop_worker') as mock_stop:
            vas.stop_archive_timer()
            mock_stop.assert_called_once()

    def test_stop_archive_timer_swallows_worker_failure(self):
        # Shutdown path needs to be resilient — a worker that's already
        # gone should not block the rest of the shutdown handler.
        with patch(
            'services.archive_worker.stop_worker',
            side_effect=RuntimeError("synthetic"),
        ):
            # Should not raise.
            vas.stop_archive_timer()


class TestUpdateGeodataPaths:
    """When the archive worker copies a RecentClips file to ArchivedClips,
    ``_update_geodata_paths`` rewrites the geodata DB to point at the new
    location. The ``indexed_files`` row gets recreated under the new
    absolute path (it's the primary key) — earlier versions of this code
    dropped the ``indexed_at`` column from the INSERT, leaving the new
    row with ``indexed_at = NULL``. That cosmetic bug confused the daily
    stale-scan and any UI that displayed indexing recency. Pin the fix.
    """

    def test_indexed_at_is_preserved_across_archive(self, tmp_path):
        import sqlite3

        db_path = str(tmp_path / "geo.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE indexed_files (
                file_path TEXT PRIMARY KEY,
                file_size INTEGER,
                file_mtime REAL,
                indexed_at TEXT,
                waypoint_count INTEGER DEFAULT 0,
                event_count INTEGER DEFAULT 0
            );
            CREATE TABLE waypoints (
                id INTEGER PRIMARY KEY,
                trip_id INTEGER, timestamp TEXT, lat REAL, lon REAL,
                video_path TEXT, frame_offset INTEGER
            );
            CREATE TABLE detected_events (
                id INTEGER PRIMARY KEY,
                trip_id INTEGER, timestamp TEXT, lat REAL, lon REAL,
                event_type TEXT, severity REAL, description TEXT,
                video_path TEXT, frame_offset INTEGER, metadata TEXT
            );
            """
        )
        old_abs = "/mnt/gadget/part1-ro/TeslaCam/RecentClips/2026-05-10_12-00-00-front.mp4"
        new_abs = "/home/pi/ArchivedClips/2026-05-10_12-00-00-front.mp4"
        original_indexed_at = "2026-05-10T12:01:30.123456+00:00"
        conn.execute(
            "INSERT INTO indexed_files VALUES (?, ?, ?, ?, ?, ?)",
            (old_abs, 12345, 1747000000.0, original_indexed_at, 5, 1),
        )
        conn.commit()
        conn.close()

        # Patch the module-level constant the function reads, then run.
        with patch.object(vas, 'ARCHIVE_ENABLED', True), \
             patch('config.MAPPING_DB_PATH', db_path):
            vas._update_geodata_paths(
                old_abs, new_abs,
                "2026-05-10_12-00-00-front.mp4",
            )

        conn = sqlite3.connect(db_path)
        # Old row deleted, new row inserted under the ArchivedClips path.
        old_row = conn.execute(
            "SELECT * FROM indexed_files WHERE file_path = ?", (old_abs,)
        ).fetchone()
        assert old_row is None
        new_row = conn.execute(
            "SELECT file_size, file_mtime, indexed_at, waypoint_count, "
            "event_count FROM indexed_files WHERE file_path = ?",
            (new_abs,),
        ).fetchone()
        assert new_row is not None
        # Critical: indexed_at must be carried over, not NULL.
        assert new_row[2] == original_indexed_at, (
            "indexed_at was dropped during archive — "
            "INSERT regressed without the column"
        )
        # Other columns also preserved.
        assert new_row[0] == 12345
        assert new_row[1] == 1747000000.0
        assert new_row[3] == 5
        assert new_row[4] == 1
        conn.close()

