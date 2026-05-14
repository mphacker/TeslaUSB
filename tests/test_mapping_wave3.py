"""Tests for issue #184 Wave 3 — Phase D (hot/cold waypoints split).

Covers:

* The v14 → v15 migration backfills cold telemetry from existing
  ``waypoints`` rows into the new ``waypoints_cold`` table and then
  drops the cold columns from ``waypoints``.
* Idempotency: re-running the migration on an already-v15 DB is a
  no-op.
* The runtime ``_index_video`` writer obeys the same default-only
  filter as the migration (parked-car waypoints don't bloat
  ``waypoints_cold``).
* The new ``query_trip_telemetry`` helper joins ``waypoints_cold``
  back to ``waypoints`` and returns a dict keyed by waypoint id.
* The lazy-loaded ``GET /api/trip/<id>/telemetry`` blueprint route
  returns the same shape and is gated on ``IMG_CAM_PATH``.
* ``query_trip_route`` no longer surfaces cold columns (they were
  the bulk of the per-row payload — this is the read-path saving
  the redesign exists to deliver).

These tests exist to prevent a regression that re-introduces cold
columns onto the hot table or causes the migration to lose data
on upgrade.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from services.mapping_migrations import (
    _COLD_COLUMNS,
    _SCHEMA_VERSION,
    _init_db,
    _waypoints_has_cold_columns,
)
from services.mapping_queries import (
    query_trip_route,
    query_trip_telemetry,
)
from services.mapping_service import (
    DEFAULT_THRESHOLDS,
    _index_video,
)

# Reuse the synthetic-MP4 helpers from test_mapping_service so we
# don't duplicate ~80 lines of MP4-box assembly. The helpers are
# pure functions and the import keeps both files in sync.
from tests.test_mapping_service import (
    _make_sei_protobuf,
    _make_synthetic_mp4,
    _unpack,
)


# ---------------------------------------------------------------------------
# Helpers — build a synthetic v14 DB so we can exercise the v15 migration.
# ---------------------------------------------------------------------------

# v14 schema for the waypoints table — mirrors the shape that shipped in
# Wave 2 (cold telemetry columns living on the hot row). We don't exec
# the entire production v14 schema here; we only need the tables touched
# by the migration plus ``schema_version`` so ``_init_db`` will resume
# the upgrade walk from v14.
_V14_WAYPOINTS_DDL = """
CREATE TABLE waypoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER,
    timestamp TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    heading REAL,
    speed_mps REAL,
    autopilot_state TEXT,
    video_path TEXT,
    frame_offset INTEGER,
    acceleration_x REAL DEFAULT 0,
    acceleration_y REAL DEFAULT 0,
    acceleration_z REAL DEFAULT 0,
    gear TEXT,
    steering_angle REAL DEFAULT 0,
    brake_applied INTEGER DEFAULT 0,
    blinker_on_left INTEGER DEFAULT 0,
    blinker_on_right INTEGER DEFAULT 0
)
"""

_V14_TRIPS_DDL = """
CREATE TABLE trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time TEXT NOT NULL,
    end_time TEXT,
    start_lat REAL,
    start_lon REAL,
    end_lat REAL,
    end_lon REAL,
    distance_km REAL DEFAULT 0,
    duration_seconds INTEGER DEFAULT 0,
    source_folder TEXT
)
"""

_V14_SCHEMA_VERSION_DDL = """
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT DEFAULT CURRENT_TIMESTAMP
)
"""


def _build_v14_db(db_path: str) -> None:
    """Materialize a v14 DB *exactly* as it would have looked on disk
    just before the v15 migration. Just enough tables for the
    migration to do its thing."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_V14_TRIPS_DDL)
        conn.execute(_V14_WAYPOINTS_DDL)
        conn.execute(_V14_SCHEMA_VERSION_DDL)
        conn.execute("INSERT INTO schema_version (version) VALUES (14)")
        conn.commit()
    finally:
        conn.close()


def _seed_waypoints(db_path: str, *, trip_id: int = 1, count: int = 3,
                    cold_data: bool = True) -> None:
    """Insert ``count`` waypoints. When ``cold_data`` is True, every
    row carries non-default cold telemetry; when False, every row uses
    SQL defaults (so the migration must still run cleanly but
    shouldn't backfill anything)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO trips (id, start_time, end_time, distance_km, "
            "                   duration_seconds, source_folder) "
            "VALUES (?, '2026-05-04T08:00:00', '2026-05-04T08:10:00', "
            "        2.5, 600, 'RecentClips')",
            (trip_id,),
        )
        for i in range(count):
            if cold_data:
                conn.execute(
                    """INSERT INTO waypoints
                        (trip_id, timestamp, lat, lon, heading, speed_mps,
                         autopilot_state, video_path, frame_offset,
                         acceleration_x, acceleration_y, acceleration_z,
                         gear, steering_angle, brake_applied,
                         blinker_on_left, blinker_on_right)
                       VALUES (?, ?, ?, ?, 90.0, 25.0, 'NONE',
                               'clip.mp4', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (trip_id, f'2026-05-04T08:0{i}:00',
                     37.7749 + i * 0.001, -122.4194 + i * 0.001, i * 30,
                     -1.5 + i, 0.5, 0.1,
                     'D', 12.0 + i, 1 if i % 2 else 0, i % 2, (i + 1) % 2),
                )
            else:
                conn.execute(
                    """INSERT INTO waypoints
                        (trip_id, timestamp, lat, lon, heading, speed_mps,
                         autopilot_state, video_path, frame_offset)
                       VALUES (?, ?, ?, ?, 90.0, 25.0, 'NONE',
                               'clip.mp4', ?)""",
                    (trip_id, f'2026-05-04T08:0{i}:00',
                     37.7749 + i * 0.001, -122.4194 + i * 0.001, i * 30),
                )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# v14 → v15 migration
# ---------------------------------------------------------------------------

class TestV14ToV15Migration:
    """Cold telemetry must move to the cold table; the hot table must
    shed those columns; existing data must survive untouched."""

    def test_migration_backfills_cold_table_and_drops_columns(self, tmp_path):
        db_path = str(tmp_path / "geodata.db")
        _build_v14_db(db_path)
        _seed_waypoints(db_path, trip_id=1, count=3, cold_data=True)

        # Trigger the upgrade walk.
        conn = _init_db(db_path)
        try:
            ver = conn.execute(
                "SELECT MAX(version) AS v FROM schema_version"
            ).fetchone()['v']
            assert ver == _SCHEMA_VERSION

            # Hot table must NOT have any cold columns.
            assert _waypoints_has_cold_columns(conn) is False

            # Cold table populated for all 3 waypoints.
            cold_rows = conn.execute(
                "SELECT id, acceleration_x, gear, steering_angle, "
                "brake_applied, blinker_on_left, blinker_on_right "
                "FROM waypoints_cold ORDER BY id"
            ).fetchall()
            assert len(cold_rows) == 3
            # Spot-check one row: row index 1 (i=1 in seed) had
            # acceleration_x = -0.5, gear='D', steering_angle = 13.0.
            assert cold_rows[1]['acceleration_x'] == pytest.approx(-0.5)
            assert cold_rows[1]['gear'] == 'D'
            assert cold_rows[1]['steering_angle'] == pytest.approx(13.0)

            # Hot table still has all 3 waypoints with hot fields intact.
            hot_rows = conn.execute(
                "SELECT id, lat, lon, speed_mps FROM waypoints ORDER BY id"
            ).fetchall()
            assert len(hot_rows) == 3
            assert hot_rows[0]['speed_mps'] == pytest.approx(25.0)
        finally:
            conn.close()

    def test_migration_skips_backfill_for_default_only_rows(self, tmp_path):
        # When every cold field equals its default, the migration MUST
        # NOT INSERT rows into ``waypoints_cold`` — that would bloat
        # the cold table with zero-value rows for every routine
        # waypoint and defeat the point of the split.
        db_path = str(tmp_path / "geodata.db")
        _build_v14_db(db_path)
        _seed_waypoints(db_path, trip_id=1, count=3, cold_data=False)

        conn = _init_db(db_path)
        try:
            cold_count = conn.execute(
                "SELECT COUNT(*) AS c FROM waypoints_cold"
            ).fetchone()['c']
            assert cold_count == 0

            # Hot table still has 3 rows.
            hot_count = conn.execute(
                "SELECT COUNT(*) AS c FROM waypoints"
            ).fetchone()['c']
            assert hot_count == 3
        finally:
            conn.close()

    def test_migration_is_idempotent(self, tmp_path):
        # Running ``_init_db`` a second time on an already-migrated DB
        # must be a no-op — the v15 migration short-circuits when the
        # cold columns are gone.
        db_path = str(tmp_path / "geodata.db")
        _build_v14_db(db_path)
        _seed_waypoints(db_path, trip_id=1, count=2, cold_data=True)

        # First run does the work.
        _init_db(db_path).close()

        # Snapshot cold table.
        c1 = sqlite3.connect(db_path)
        first = c1.execute(
            "SELECT id, acceleration_x FROM waypoints_cold ORDER BY id"
        ).fetchall()
        c1.close()

        # Second run must not change anything.
        _init_db(db_path).close()

        c2 = sqlite3.connect(db_path)
        try:
            second = c2.execute(
                "SELECT id, acceleration_x FROM waypoints_cold ORDER BY id"
            ).fetchall()
            assert [tuple(r) for r in second] == [tuple(r) for r in first]
        finally:
            c2.close()

    def test_migration_skips_jitter_and_park(self, tmp_path):
        # PR #187 review #3 + #4 regression test. v14→v15 must use the
        # same noise-floor + PARK semantics as the runtime path —
        # otherwise a v14→v15 upgrade backfills cold rows for every
        # parked-Sentry waypoint, while a fresh-install v15 would
        # produce zero rows for the same data. Asymmetry would
        # silently corrupt analytics dashboards.
        db_path = str(tmp_path / "geodata.db")
        _build_v14_db(db_path)
        # Seed 4 waypoints directly: jitter-only, PARK-only,
        # UNKNOWN-only, and a real-driving row.
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO trips (id, start_time, source_folder) "
                "VALUES (1, '2026-05-04T08:00:00', 'RecentClips')"
            )
            # 1: pure jitter (accel below 0.05 threshold, gear UNKNOWN)
            conn.execute(
                """INSERT INTO waypoints
                    (trip_id, timestamp, lat, lon, heading, speed_mps,
                     autopilot_state, video_path, frame_offset,
                     acceleration_x, acceleration_y, acceleration_z,
                     gear, steering_angle, brake_applied,
                     blinker_on_left, blinker_on_right)
                   VALUES (1, '2026-05-04T08:00:00', 37.0, -122.0, 0,
                           0, 'NONE', 'a.mp4', 0,
                           0.01, -0.02, 0.03,
                           'UNKNOWN', 0.2, 0, 0, 0)"""
            )
            # 2: PARK with no other signal — should NOT backfill
            conn.execute(
                """INSERT INTO waypoints
                    (trip_id, timestamp, lat, lon, heading, speed_mps,
                     autopilot_state, video_path, frame_offset,
                     acceleration_x, acceleration_y, acceleration_z,
                     gear, steering_angle, brake_applied,
                     blinker_on_left, blinker_on_right)
                   VALUES (1, '2026-05-04T08:00:01', 37.0, -122.0, 0,
                           0, 'NONE', 'a.mp4', 1,
                           0.0, 0.0, 0.0,
                           'PARK', 0.0, 0, 0, 0)"""
            )
            # 3: UNKNOWN + zero — should NOT backfill
            conn.execute(
                """INSERT INTO waypoints
                    (trip_id, timestamp, lat, lon, heading, speed_mps,
                     autopilot_state, video_path, frame_offset,
                     acceleration_x, acceleration_y, acceleration_z,
                     gear, steering_angle, brake_applied,
                     blinker_on_left, blinker_on_right)
                   VALUES (1, '2026-05-04T08:00:02', 37.0, -122.0, 0,
                           0, 'NONE', 'a.mp4', 2,
                           0.0, 0.0, 0.0,
                           'UNKNOWN', 0.0, 0, 0, 0)"""
            )
            # 4: real driving — DOES backfill
            conn.execute(
                """INSERT INTO waypoints
                    (trip_id, timestamp, lat, lon, heading, speed_mps,
                     autopilot_state, video_path, frame_offset,
                     acceleration_x, acceleration_y, acceleration_z,
                     gear, steering_angle, brake_applied,
                     blinker_on_left, blinker_on_right)
                   VALUES (1, '2026-05-04T08:00:03', 37.0, -122.0, 0,
                           20, 'NONE', 'a.mp4', 3,
                           1.5, 0.0, 0.0,
                           'DRIVE', 5.0, 0, 0, 0)"""
            )
            conn.commit()
        finally:
            conn.close()

        conn = _init_db(db_path)
        try:
            cold = conn.execute(
                "SELECT id, gear, acceleration_x FROM waypoints_cold "
                "ORDER BY id"
            ).fetchall()
            # Only the real-driving row (id=4) should be in cold.
            assert len(cold) == 1, (
                f"only DRIVE row should backfill; got: "
                f"{[dict(r) for r in cold]}"
            )
            assert cold[0]['gear'] == 'DRIVE'
            assert cold[0]['acceleration_x'] == 1.5
        finally:
            conn.close()

    def test_fresh_install_at_v15_has_no_cold_cols_on_hot_table(self, tmp_path):
        db_path = str(tmp_path / "geodata.db")
        conn = _init_db(db_path)
        try:
            # Cold cols must be absent from waypoints.
            assert _waypoints_has_cold_columns(conn) is False
            # waypoints_cold table exists.
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='waypoints_cold'"
            ).fetchone()
            assert row is not None and row['name'] == 'waypoints_cold'
            # Schema version is current.
            ver = conn.execute(
                "SELECT MAX(version) AS v FROM schema_version"
            ).fetchone()['v']
            assert ver == _SCHEMA_VERSION
        finally:
            conn.close()

    def test_cold_columns_constant_matches_migration_cols(self):
        # Defensive: any future cold-column addition must be wired
        # through ``_COLD_COLUMNS`` so the migration drops it.
        expected = {
            'acceleration_x', 'acceleration_y', 'acceleration_z',
            'gear', 'steering_angle', 'brake_applied',
            'blinker_on_left', 'blinker_on_right',
        }
        assert set(_COLD_COLUMNS) == expected


# ---------------------------------------------------------------------------
# query_trip_route — must return ONLY hot columns (and ``id`` for
# the lazy-load merge in the JS overlay).
# ---------------------------------------------------------------------------

class TestQueryTripRouteShape:
    def test_route_excludes_cold_columns(self, tmp_path):
        db_path = str(tmp_path / "geodata.db")
        _build_v14_db(db_path)
        _seed_waypoints(db_path, trip_id=1, count=3, cold_data=True)
        _init_db(db_path).close()  # apply v15 migration

        route = query_trip_route(db_path, trip_id=1)
        assert len(route) == 3
        wp = route[0]
        # ``id`` is required so the JS can merge cold telemetry.
        assert 'id' in wp
        # Hot columns are present.
        for col in ('lat', 'lon', 'speed_mps', 'heading',
                    'autopilot_state', 'video_path', 'frame_offset',
                    'timestamp'):
            assert col in wp
        # Cold columns must NOT be in the dict.
        for col in _COLD_COLUMNS:
            assert col not in wp, (
                f"query_trip_route surfaced cold column {col!r}; "
                "this would defeat the Wave 3 read-path savings"
            )


# ---------------------------------------------------------------------------
# query_trip_telemetry — the new lazy-load helper backing
# /api/trip/<id>/telemetry.
# ---------------------------------------------------------------------------

class TestQueryTripTelemetry:
    def test_returns_dict_keyed_by_waypoint_id(self, tmp_path):
        db_path = str(tmp_path / "geodata.db")
        _build_v14_db(db_path)
        _seed_waypoints(db_path, trip_id=1, count=3, cold_data=True)
        _init_db(db_path).close()

        telem = query_trip_telemetry(db_path, trip_id=1)
        assert isinstance(telem, dict)
        assert len(telem) == 3

        # Keys are waypoint ids (ints — though JSON serializes them as
        # strings on the wire, the in-process dict stays int-keyed).
        for wp_id, payload in telem.items():
            assert isinstance(wp_id, int)
            for col in _COLD_COLUMNS:
                assert col in payload, (
                    f"telemetry payload missing cold column {col!r}"
                )

    def test_returns_empty_for_default_only_trip(self, tmp_path):
        db_path = str(tmp_path / "geodata.db")
        _build_v14_db(db_path)
        _seed_waypoints(db_path, trip_id=1, count=3, cold_data=False)
        _init_db(db_path).close()

        telem = query_trip_telemetry(db_path, trip_id=1)
        assert telem == {}

    def test_returns_empty_for_unknown_trip(self, tmp_path):
        db_path = str(tmp_path / "geodata.db")
        _init_db(db_path).close()
        assert query_trip_telemetry(db_path, trip_id=999) == {}


# ---------------------------------------------------------------------------
# _index_video runtime path — must NOT bloat waypoints_cold with a
# row per parked-car waypoint.
# ---------------------------------------------------------------------------

class TestIndexVideoColdSplit:
    def _seed_clip(self, tmp_path, payloads, name='2025-11-08_08-15-44-front.mp4'):
        teslacam = tmp_path / "TeslaCam" / "RecentClips"
        teslacam.mkdir(parents=True, exist_ok=True)
        clip = teslacam / name
        clip.write_bytes(_make_synthetic_mp4(payloads))
        return clip, str(tmp_path / "TeslaCam")

    def test_parked_car_waypoints_create_no_cold_rows(self, tmp_path):
        # Force gear=99 → SEI parser maps it to 'UNKNOWN' (anything
        # outside ``_GEAR_NAMES``). Combined with zero accel/steering
        # and no brake/blinker, this represents a SEI frame that
        # carries no usable telemetry — Tesla shipped a partial frame
        # or a corrupted one. The runtime path MUST skip the
        # ``waypoints_cold`` INSERT for these rows so non-signal
        # frames don't bloat the cold table.
        db_path = str(tmp_path / "test.db")
        conn = _init_db(db_path)
        try:
            payloads = [
                _make_sei_protobuf(lat=37.7749, lon=-122.4194, speed=25.0,
                                   gear=99, accel_x=0.0, accel_y=0.0),
                _make_sei_protobuf(lat=37.7750, lon=-122.4195, speed=25.0,
                                   gear=99, accel_x=0.0, accel_y=0.0),
                _make_sei_protobuf(lat=37.7751, lon=-122.4196, speed=25.0,
                                   gear=99, accel_x=0.0, accel_y=0.0),
            ]
            clip, root = self._seed_clip(tmp_path, payloads)
            wc, _ = _unpack(_index_video(
                conn, str(clip), root,
                sample_rate=1, thresholds=DEFAULT_THRESHOLDS,
                trip_gap_minutes=5,
            ))
            assert wc == 3

            # Hot table got all 3.
            hot = conn.execute("SELECT COUNT(*) AS n FROM waypoints").fetchone()['n']
            assert hot == 3
            # Cold table got NONE — no signal worth storing.
            cold = conn.execute(
                "SELECT COUNT(*) AS n FROM waypoints_cold"
            ).fetchone()['n']
            assert cold == 0, (
                "no-signal SEI frames must NOT bloat waypoints_cold; "
                "the runtime filter is broken if this fails"
            )
        finally:
            conn.close()

    def test_real_world_sei_creates_cold_rows(self, tmp_path):
        # A normal driving frame (gear='DRIVE', non-zero accel) MUST
        # produce a cold row — the in-clip HUD reads gear/accel/brake
        # from the cold table and the user expects them visible.
        db_path = str(tmp_path / "test.db")
        conn = _init_db(db_path)
        try:
            payloads = [
                _make_sei_protobuf(lat=37.7749, lon=-122.4194, speed=25.0,
                                   gear=1, accel_x=-1.5),
                _make_sei_protobuf(lat=37.7750, lon=-122.4195, speed=22.0,
                                   gear=1, accel_x=-2.0),
                _make_sei_protobuf(lat=37.7751, lon=-122.4196, speed=20.0,
                                   gear=1, accel_x=-3.0),
            ]
            clip, root = self._seed_clip(tmp_path, payloads)
            wc, _ = _unpack(_index_video(
                conn, str(clip), root,
                sample_rate=1, thresholds=DEFAULT_THRESHOLDS,
                trip_gap_minutes=5,
            ))
            assert wc == 3

            cold = conn.execute(
                "SELECT id, acceleration_x, gear FROM waypoints_cold ORDER BY id"
            ).fetchall()
            # Every waypoint had real telemetry — all 3 in cold.
            assert len(cold) == 3
            for row in cold:
                assert row['gear'] == 'DRIVE'
                assert row['acceleration_x'] != 0
        finally:
            conn.close()

    def test_jitter_below_threshold_creates_no_cold_rows(self, tmp_path):
        # PR #187 review Warning #1 regression test. A real Tesla IMU
        # never reports exactly 0.0 — sensor noise floor is typically
        # ±0.001–±0.05 m/s². If the runtime filter were ``!= 0`` (the
        # original Wave 3 implementation), every parked-car Sentry
        # frame would create a cold row and the hot/cold split would
        # provide ZERO read-path benefit. The threshold must be tight
        # enough to skip jitter but loose enough that any real coast
        # / cruise / brake creates cold rows.
        db_path = str(tmp_path / "test.db")
        conn = _init_db(db_path)
        try:
            # gear='UNKNOWN' (gear_state=99 falls outside _GEAR_NAMES);
            # accel below 0.05 m/s² threshold. Steering is hardcoded to
            # 0.0 in the test helper; the runtime path treats 0.0 as
            # below the 0.5° threshold so it doesn't contribute signal.
            payloads = [
                _make_sei_protobuf(lat=37.7749, lon=-122.4194, speed=0.0,
                                   gear=99, accel_x=0.005, accel_y=-0.01),
                _make_sei_protobuf(lat=37.7749, lon=-122.4194, speed=0.0,
                                   gear=99, accel_x=-0.02, accel_y=0.03),
                _make_sei_protobuf(lat=37.7749, lon=-122.4194, speed=0.0,
                                   gear=99, accel_x=0.04, accel_y=0.02),
            ]
            clip, root = self._seed_clip(tmp_path, payloads)
            wc, _ = _unpack(_index_video(
                conn, str(clip), root,
                sample_rate=1, thresholds=DEFAULT_THRESHOLDS,
                trip_gap_minutes=5,
            ))
            assert wc == 3

            cold = conn.execute(
                "SELECT COUNT(*) AS n FROM waypoints_cold"
            ).fetchone()['n']
            assert cold == 0, (
                "sensor jitter below noise threshold MUST NOT bloat "
                "waypoints_cold; runtime filter is too loose if this fails"
            )
        finally:
            conn.close()

    def test_jitter_above_threshold_creates_cold_rows(self, tmp_path):
        # Companion to the test above: as soon as jitter clearly exceeds
        # the noise floor, we DO want a cold row — that's a real coast/
        # brake transition, not sensor wander.
        db_path = str(tmp_path / "test.db")
        conn = _init_db(db_path)
        try:
            # accel comfortably above 0.05 m/s² threshold.
            payloads = [
                _make_sei_protobuf(lat=37.7749, lon=-122.4194, speed=15.0,
                                   gear=1, accel_x=0.15),
                _make_sei_protobuf(lat=37.7750, lon=-122.4195, speed=15.0,
                                   gear=1, accel_x=0.20),
                _make_sei_protobuf(lat=37.7751, lon=-122.4196, speed=15.0,
                                   gear=1, accel_x=0.25),
            ]
            clip, root = self._seed_clip(tmp_path, payloads)
            _index_video(
                conn, str(clip), root,
                sample_rate=1, thresholds=DEFAULT_THRESHOLDS,
                trip_gap_minutes=5,
            )
            cold = conn.execute(
                "SELECT COUNT(*) AS n FROM waypoints_cold"
            ).fetchone()['n']
            assert cold == 3, "real coast/brake must create cold rows"
        finally:
            conn.close()

    def test_park_gear_alone_creates_no_cold_rows(self, tmp_path):
        # Sentry events on a parked car emit gear='PARK' for every
        # 30 Hz × 60 s = 1 800 waypoints in the clip. Recording 1 800
        # identical "still parked" cold rows per parked event would
        # dwarf the few thousand driving rows that actually carry
        # telemetry signal — defeating the design goal documented in
        # the v15 ``waypoints_cold`` table comment ("parked-car
        # waypoints (all-null telemetry) consume zero cold pages").
        db_path = str(tmp_path / "test.db")
        conn = _init_db(db_path)
        try:
            payloads = [
                _make_sei_protobuf(lat=37.7749, lon=-122.4194, speed=0.0,
                                   gear=0, accel_x=0.0, accel_y=0.0),
                _make_sei_protobuf(lat=37.7749, lon=-122.4194, speed=0.0,
                                   gear=0, accel_x=0.0, accel_y=0.0),
            ]
            clip, root = self._seed_clip(tmp_path, payloads)
            _index_video(
                conn, str(clip), root,
                sample_rate=1, thresholds=DEFAULT_THRESHOLDS,
                trip_gap_minutes=5,
            )
            cold = conn.execute(
                "SELECT COUNT(*) AS n FROM waypoints_cold"
            ).fetchone()['n']
            assert cold == 0, (
                "PARK alone with no other signal MUST NOT create cold "
                "rows — see _COLD_GEAR_NO_SIGNAL in mapping_service"
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Blueprint: /api/trip/<id>/telemetry
# ---------------------------------------------------------------------------

@pytest.fixture
def telemetry_app(tmp_path, monkeypatch):
    """Hermetic Flask app with the mapping blueprint mounted, a v15
    DB seeded with cold telemetry, and the IMG_CAM_PATH gate
    pointed at an existing tmp file."""
    from flask import Flask
    from blueprints import mapping as mapping_module

    db_path = str(tmp_path / "geodata.db")
    _build_v14_db(db_path)
    _seed_waypoints(db_path, trip_id=1, count=3, cold_data=True)
    _init_db(db_path).close()

    img_path = tmp_path / "usb_cam.img"
    img_path.write_bytes(b"\x00")

    monkeypatch.setattr(mapping_module, 'IMG_CAM_PATH', str(img_path))
    monkeypatch.setattr(mapping_module, 'MAPPING_DB_PATH', db_path)

    app = Flask(__name__)
    app.secret_key = 'test'
    app.register_blueprint(mapping_module.mapping_bp)
    app.config['TESTING'] = True
    app.db_path = db_path
    return app


@pytest.fixture
def telemetry_client(telemetry_app):
    return telemetry_app.test_client()


class TestApiTripTelemetry:
    def test_returns_telemetry_for_trip(self, telemetry_client):
        r = telemetry_client.get('/api/trip/1/telemetry')
        assert r.status_code == 200
        body = r.get_json()
        assert body['trip_id'] == 1
        # JSON serialization stringifies dict int-keys.
        assert isinstance(body['telemetry'], dict)
        assert len(body['telemetry']) == 3
        # Every payload has the cold columns.
        sample = next(iter(body['telemetry'].values()))
        for col in _COLD_COLUMNS:
            assert col in sample

    def test_returns_empty_for_unknown_trip(self, telemetry_client):
        r = telemetry_client.get('/api/trip/999/telemetry')
        assert r.status_code == 200
        body = r.get_json()
        assert body['trip_id'] == 999
        assert body['telemetry'] == {}

    def test_route_gated_on_image_file(self, telemetry_client,
                                       telemetry_app, monkeypatch):
        # Removing the cam image must block the route — same gate as
        # the rest of the mapping blueprint. We send the request as
        # AJAX so the gate returns 503 JSON instead of attempting a
        # redirect to ``mode_control.index`` (which isn't registered
        # in the hermetic test app).
        from blueprints import mapping as mapping_module
        os.remove(mapping_module.IMG_CAM_PATH)
        r = telemetry_client.get(
            '/api/trip/1/telemetry',
            headers={'X-Requested-With': 'XMLHttpRequest'},
        )
        assert r.status_code == 503
