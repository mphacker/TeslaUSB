"""Issue #142 — apply Phase 5.1 batched recompute to the v3 migration.

The Phase 5.1 / PR #141 fix to ``mapping_service._index_video``
collapsed the trip-distance recompute from ``1 + N`` SQL queries into
a single ``ORDER BY video_path, id`` query. The v3 migration in
``mapping_migrations._migrate_v2_to_v3`` was deliberately left as a
follow-up (PR #141 review-pr finding) and is fixed in this PR.

These tests pin the same two contracts the runtime path has:

1. **Behavior** — distance summed per-video, NOT across video
   boundaries (Tesla can write overlapping videos; a global sort
   interleaves them and creates phantom GPS jumps).

2. **Performance / shape** — exactly ONE waypoint-fetch query per
   trip, regardless of how many distinct ``video_path`` values the
   trip references.

The migration is a single 250-line function that's not callable in
isolation, so we exercise its recompute logic by:

  * Building a controlled v2-shape DB (waypoints + trips, no other
    schema features), THEN
  * Calling the full ``_migrate_v2_to_v3`` function and reading the
    ``trips.distance_km`` it writes back.

This is integration-level (not pure unit) but stays self-contained
and avoids reaching into any private helpers.
"""
from __future__ import annotations

import sqlite3

import pytest


# ---------------------------------------------------------------------------
# DB-shape helpers — minimal v2 schema used by the migration's reads.
# ---------------------------------------------------------------------------


def _build_v2_db(conn: sqlite3.Connection) -> None:
    """Build the schema the v2->v3 migration expects to read from.

    Mirrors the relevant subset of the production v2 schema — the
    migration only reads from ``trips`` and ``waypoints`` and writes
    back into ``trips``, so we don't need the full mapping schema.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY,
            start_time TEXT,
            end_time TEXT,
            start_lat REAL,
            start_lon REAL,
            end_lat REAL,
            end_lon REAL,
            distance_km REAL DEFAULT 0,
            duration_seconds INTEGER DEFAULT 0,
            source_folder TEXT
        );
        CREATE TABLE IF NOT EXISTS waypoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER,
            video_path TEXT,
            timestamp TEXT,
            lat REAL,
            lon REAL,
            speed_mph REAL,
            speed_mps REAL,
            FOREIGN KEY (trip_id) REFERENCES trips(id)
        );
        CREATE TABLE IF NOT EXISTS detected_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_path TEXT
        );
        CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
        INSERT OR REPLACE INTO schema_version (version) VALUES (2);
        """
    )
    conn.commit()


def _add_trip(conn, *, start_ts, end_ts, source_folder='RecentClips') -> int:
    cur = conn.execute(
        "INSERT INTO trips (start_time, end_time, source_folder, distance_km) "
        "VALUES (?, ?, ?, 0)",
        (start_ts, end_ts, source_folder),
    )
    conn.commit()
    return cur.lastrowid


def _add_waypoints(conn, trip_id, video_path, points):
    """``points`` is a list of ``(timestamp, lat, lon)`` tuples."""
    for ts, lat, lon in points:
        conn.execute(
            "INSERT INTO waypoints (trip_id, video_path, timestamp, lat, lon) "
            "VALUES (?, ?, ?, ?, ?)",
            (trip_id, video_path, ts, lat, lon),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Tests — semantic equivalence + boundary protection + query-shape
# ---------------------------------------------------------------------------


class TestMigrationRecomputeBatching:

    def test_distance_matches_sum_of_per_video_haversines(self, tmp_path):
        """Pin the semantic invariant: the migration's recomputed
        ``distance_km`` is the SUM of per-video haversine chains.
        """
        from services.mapping_migrations import _migrate_v2_to_v3
        from services.mapping_service import _haversine_km

        conn = sqlite3.connect(str(tmp_path / "mig.db"))
        conn.row_factory = sqlite3.Row
        _build_v2_db(conn)

        trip_id = _add_trip(
            conn,
            start_ts='2024-01-01T10:00:00',
            end_ts='2024-01-01T10:10:00',
        )

        # Two clips, three points each, on a known geographic line.
        clip_a = [
            ('2024-01-01T10:00:00', 37.7749, -122.4194),
            ('2024-01-01T10:00:01', 37.7750, -122.4194),
            ('2024-01-01T10:00:02', 37.7751, -122.4194),
        ]
        clip_b = [
            ('2024-01-01T10:05:00', 37.7800, -122.4180),
            ('2024-01-01T10:05:01', 37.7801, -122.4180),
            ('2024-01-01T10:05:02', 37.7802, -122.4180),
        ]
        _add_waypoints(conn, trip_id, 'RecentClips/a.mp4', clip_a)
        _add_waypoints(conn, trip_id, 'RecentClips/b.mp4', clip_b)

        expected = 0.0
        for clip in (clip_a, clip_b):
            for j in range(1, len(clip)):
                expected += _haversine_km(
                    clip[j - 1][1], clip[j - 1][2],
                    clip[j][1], clip[j][2],
                )

        _migrate_v2_to_v3(conn)
        actual = conn.execute(
            "SELECT distance_km FROM trips WHERE id = ?", (trip_id,)
        ).fetchone()['distance_km']

        # Within 1 mm — float math only.
        assert abs(actual - expected) < 1e-6, (
            f"distance_km {actual} != per-video haversine sum {expected}"
        )

    def test_does_not_haversine_across_video_boundaries(self, tmp_path):
        """Two clips stored with sequential ids but writing GPS jumps
        across the boundary. The migration MUST NOT include the
        cross-clip jump in distance_km — that would be a phantom
        movement caused by Tesla's overlapping clip writes.
        """
        from services.mapping_migrations import _migrate_v2_to_v3
        from services.mapping_service import _haversine_km

        conn = sqlite3.connect(str(tmp_path / "mig.db"))
        conn.row_factory = sqlite3.Row
        _build_v2_db(conn)

        trip_id = _add_trip(
            conn,
            start_ts='2024-01-01T10:00:00',
            end_ts='2024-01-01T10:10:00',
        )

        clip_a = [
            ('2024-01-01T10:00:00', 37.7749, -122.4194),
            ('2024-01-01T10:00:01', 37.7750, -122.4194),
        ]
        # Clip B starts in a completely different city (~5000 km away).
        # If the migration global-sorted across boundaries, the jump
        # from a's last point to b's first point would dominate.
        clip_b = [
            ('2024-01-01T10:05:00', 40.7128, -74.0060),  # NYC!
            ('2024-01-01T10:05:01', 40.7129, -74.0060),
        ]
        _add_waypoints(conn, trip_id, 'RecentClips/a.mp4', clip_a)
        _add_waypoints(conn, trip_id, 'RecentClips/b.mp4', clip_b)

        expected_per_clip = 0.0
        for clip in (clip_a, clip_b):
            for j in range(1, len(clip)):
                expected_per_clip += _haversine_km(
                    clip[j - 1][1], clip[j - 1][2],
                    clip[j][1], clip[j][2],
                )
        # The cross-boundary haversine would add ~4100 km; the
        # per-clip sum is < 1 km.
        cross = _haversine_km(
            clip_a[-1][1], clip_a[-1][2],
            clip_b[0][1], clip_b[0][2],
        )
        assert cross > 1000, "test setup error — boundary jump too small"

        _migrate_v2_to_v3(conn)
        actual = conn.execute(
            "SELECT distance_km FROM trips WHERE id = ?", (trip_id,)
        ).fetchone()['distance_km']

        # Must be near per-clip sum, not include the 4100km jump.
        assert abs(actual - expected_per_clip) < 1e-6, (
            f"migration phantom-jumped across video boundaries: "
            f"got {actual} km, expected {expected_per_clip} km "
            f"(cross-boundary jump would have been {cross} km)"
        )

    def test_query_shape_is_single_waypoint_fetch_per_trip(self, tmp_path):
        """Performance contract: the migration MUST issue exactly ONE
        ``SELECT ... FROM waypoints`` query against the recompute
        block per trip, regardless of how many distinct
        ``video_path`` values the trip has.

        Implementation: install ``set_trace_callback`` and count the
        number of executions matching the new batched shape
        ('SELECT video_path, lat, lon FROM waypoints'). The legacy
        1+N shape would produce 1 (DISTINCT) + N (per-video fetch)
        statements; the new shape produces exactly 1.
        """
        from services.mapping_migrations import _migrate_v2_to_v3

        conn = sqlite3.connect(str(tmp_path / "mig.db"))
        conn.row_factory = sqlite3.Row
        _build_v2_db(conn)

        trip_id = _add_trip(
            conn,
            start_ts='2024-01-01T10:00:00',
            end_ts='2024-01-01T10:10:00',
        )

        # Five distinct video_path values — legacy code would issue
        # 1 + 5 = 6 waypoint queries; new code issues exactly 1.
        for i, name in enumerate(['a', 'b', 'c', 'd', 'e']):
            _add_waypoints(conn, trip_id, f'RecentClips/{name}.mp4', [
                (f'2024-01-01T10:0{i}:00', 37.0 + i * 0.001, -122.0),
                (f'2024-01-01T10:0{i}:01', 37.0 + i * 0.001 + 0.0001, -122.0),
            ])

        # Statement counter — anchored to the BATCHED shape only.
        # Legacy shape ('SELECT lat, lon FROM waypoints WHERE trip_id =
        # ? AND video_path = ?') would NOT match.
        seen: list[str] = []

        def trace(sql: str) -> None:
            sql_norm = ' '.join(sql.split())
            if 'SELECT video_path, lat, lon FROM waypoints' in sql_norm:
                seen.append(sql_norm)

        conn.set_trace_callback(trace)
        _migrate_v2_to_v3(conn)
        conn.set_trace_callback(None)

        assert len(seen) == 1, (
            f"Expected exactly 1 batched waypoint query, got {len(seen)}: "
            f"{seen}"
        )

    def test_legacy_1_plus_n_shape_is_gone(self):
        """Source-shape tripwire — the legacy ``1 + N`` fragments must
        not reappear in the migration. Pinned at the source level so
        a future refactor can't silently regress.
        """
        import inspect
        from services.mapping_migrations import _migrate_v2_to_v3

        src = inspect.getsource(_migrate_v2_to_v3)
        # The legacy DISTINCT must be gone.
        assert 'SELECT DISTINCT video_path FROM waypoints' not in src, (
            "Legacy 1+N pattern resurfaced in _migrate_v2_to_v3"
        )
        # The legacy per-video fetch must be gone.
        assert 'WHERE trip_id = ? AND video_path = ?' not in src, (
            "Legacy per-video waypoint fetch resurfaced"
        )
        # The new batched shape MUST be present.
        assert 'SELECT video_path, lat, lon FROM waypoints' in src, (
            "Batched recompute shape missing"
        )

    def test_empty_trip_has_zero_distance(self, tmp_path):
        """Defensive: a trip with no waypoints (edge case after
        dedupe) still gets ``distance_km = 0`` and doesn't crash."""
        from services.mapping_migrations import _migrate_v2_to_v3

        conn = sqlite3.connect(str(tmp_path / "mig.db"))
        conn.row_factory = sqlite3.Row
        _build_v2_db(conn)

        # Add ONE waypoint so the trip survives the "drop empty"
        # phase but the recompute loop still has nothing meaningful
        # to sum (single point → 0 km).
        trip_id = _add_trip(
            conn,
            start_ts='2024-01-01T10:00:00',
            end_ts='2024-01-01T10:00:01',
        )
        _add_waypoints(conn, trip_id, 'RecentClips/x.mp4', [
            ('2024-01-01T10:00:00', 37.7749, -122.4194),
        ])

        _migrate_v2_to_v3(conn)
        row = conn.execute(
            "SELECT distance_km FROM trips WHERE id = ?", (trip_id,)
        ).fetchone()
        # Single waypoint → no haversine pairs → 0 km.
        assert row is not None, "single-waypoint trip was unexpectedly dropped"
        assert row['distance_km'] == 0.0
