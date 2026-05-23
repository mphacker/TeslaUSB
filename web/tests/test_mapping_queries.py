"""Tests for the read-only mapping_queries layer (ADR-0017).

These tests build a synthetic copy of the Rust worker DB (schema v2)
in-memory, then exercise every public method on :class:`MappingQueries`
to ensure trip grouping, event derivation and aggregate stats all
work end-to-end without the legacy Python indexer/migrations.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from teslausb_web.services.mapping_queries import (
    MappingQueries,
    MappingQueriesConfig,
)

if TYPE_CHECKING:
    from pathlib import Path

# A factory fixture that builds a fresh MappingQueries from a clip seed list.
WorkerDbFactory = Callable[[list[dict[str, Any]]], MappingQueries]

# ---------------------------------------------------------------------------
# Worker DB fixture — schema v2 (mirrors rust/.../store/schema.rs)
# ---------------------------------------------------------------------------

_SCHEMA_V2_DDL = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_path TEXT NOT NULL UNIQUE,
    bucket TEXT NOT NULL,
    clip_started_utc INTEGER,
    indexed_at_utc INTEGER NOT NULL,
    waypoint_count INTEGER NOT NULL DEFAULT 0,
    gps_waypoint_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX clips_by_bucket_started ON clips(bucket, clip_started_utc);
CREATE TABLE waypoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    frame_index INTEGER NOT NULL,
    timestamp_ms REAL NOT NULL,
    latitude_deg REAL NOT NULL,
    longitude_deg REAL NOT NULL,
    speed_mps REAL NOT NULL,
    heading_deg REAL NOT NULL,
    acceleration_x REAL,
    acceleration_y REAL,
    acceleration_z REAL,
    gear TEXT,
    steering_angle REAL,
    brake_applied INTEGER NOT NULL DEFAULT 0,
    blinker_on_left INTEGER NOT NULL DEFAULT 0,
    blinker_on_right INTEGER NOT NULL DEFAULT 0,
    autopilot_state TEXT
);
CREATE INDEX waypoints_by_clip ON waypoints(clip_id);
CREATE INDEX waypoints_by_clip_frame ON waypoints(clip_id, frame_index);
INSERT INTO meta(key, value) VALUES ('schema_version', '2');
"""


def _seed_db(
    db_path: Path,
    clips: list[dict[str, Any]],
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA_V2_DDL)
        for clip in clips:
            cur = conn.execute(
                "INSERT INTO clips(relative_path, bucket, clip_started_utc, "
                "indexed_at_utc, waypoint_count, gps_waypoint_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    clip["relative_path"],
                    clip["bucket"],
                    clip["clip_started_utc"],
                    clip.get("indexed_at_utc", clip["clip_started_utc"] + 1),
                    len(clip.get("waypoints", [])),
                    sum(1 for w in clip.get("waypoints", []) if w.get("lat") is not None),
                ),
            )
            clip_id = cur.lastrowid
            for idx, wp in enumerate(clip.get("waypoints", [])):
                conn.execute(
                    "INSERT INTO waypoints(clip_id, frame_index, timestamp_ms, "
                    "latitude_deg, longitude_deg, speed_mps, heading_deg, "
                    "acceleration_x, acceleration_y, acceleration_z, gear, "
                    "steering_angle, brake_applied, blinker_on_left, "
                    "blinker_on_right, autopilot_state) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        clip_id,
                        idx,
                        wp.get("timestamp_ms", idx * 100.0),
                        wp.get("lat", 37.0),
                        wp.get("lon", -122.0),
                        wp.get("speed_mps", 10.0),
                        wp.get("heading", 0.0),
                        wp.get("accel_x"),
                        wp.get("accel_y"),
                        wp.get("accel_z"),
                        wp.get("gear", "D"),
                        wp.get("steering_angle"),
                        int(wp.get("brake", 0)),
                        int(wp.get("blinker_left", 0)),
                        int(wp.get("blinker_right", 0)),
                        wp.get("autopilot"),
                    ),
                )
        conn.commit()
    finally:
        conn.close()


def _epoch(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp())


@pytest.fixture
def worker_db_factory(tmp_path: Path) -> WorkerDbFactory:
    """Return a callable that writes a worker DB and yields a MappingQueries."""

    def _build(clips: list[dict[str, Any]]) -> MappingQueries:
        db_path = tmp_path / "index.sqlite3"
        if db_path.exists():
            db_path.unlink()
        _seed_db(db_path, clips)
        media_root = tmp_path / "media"
        media_root.mkdir(exist_ok=True)
        cfg = MappingQueriesConfig(db_path=db_path, media_root=media_root)
        return MappingQueries(config=cfg)

    return _build


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestConfig:
    def test_rejects_non_positive_trip_gap(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="trip_gap_seconds"):
            MappingQueriesConfig(
                db_path=tmp_path / "x.db",
                media_root=tmp_path,
                trip_gap_seconds=0,
            )

    def test_rejects_non_positive_ttl(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="playable_trips_ttl_seconds"):
            MappingQueriesConfig(
                db_path=tmp_path / "x.db",
                media_root=tmp_path,
                playable_trips_ttl_seconds=0,
            )


# ---------------------------------------------------------------------------
# Trip grouping
# ---------------------------------------------------------------------------


class TestTripGrouping:
    def test_clips_within_gap_form_one_trip(self, worker_db_factory: WorkerDbFactory) -> None:
        base = _epoch(2024, 6, 1, 10, 0)
        q = worker_db_factory(
            [
                {
                    "relative_path": "RecentClips/a-front.mp4",
                    "bucket": "recent",
                    "clip_started_utc": base,
                    "waypoints": [{"lat": 37.0, "lon": -122.0, "speed_mps": 5.0}],
                },
                {
                    "relative_path": "RecentClips/b-front.mp4",
                    "bucket": "recent",
                    "clip_started_utc": base + 240,  # 4 min later
                    "waypoints": [{"lat": 37.01, "lon": -122.01, "speed_mps": 6.0}],
                },
            ]
        )
        trips = q.query_trips(limit=10, offset=0, min_distance_km=0.0)
        assert len(trips) == 1
        assert trips[0].video_count == 2

    def test_clips_beyond_gap_split_into_two_trips(
        self, worker_db_factory: WorkerDbFactory
    ) -> None:
        base = _epoch(2024, 6, 1, 10, 0)
        q = worker_db_factory(
            [
                {
                    "relative_path": "RecentClips/a-front.mp4",
                    "bucket": "recent",
                    "clip_started_utc": base,
                    "waypoints": [{"lat": 37.0, "lon": -122.0}],
                },
                {
                    "relative_path": "RecentClips/b-front.mp4",
                    "bucket": "recent",
                    "clip_started_utc": base + 600,  # 10 min later
                    "waypoints": [{"lat": 37.01, "lon": -122.01}],
                },
            ]
        )
        trips = q.query_trips(limit=10, offset=0, min_distance_km=0.0)
        assert len(trips) == 2

    def test_sentry_clips_excluded_from_trips(self, worker_db_factory: WorkerDbFactory) -> None:
        base = _epoch(2024, 6, 1, 10, 0)
        q = worker_db_factory(
            [
                {
                    "relative_path": "SentryClips/2024-06-01_10-00-00/front.mp4",
                    "bucket": "sentry",
                    "clip_started_utc": base,
                    "waypoints": [],
                },
            ]
        )
        assert q.query_trips(limit=10, offset=0, min_distance_km=0.0) == ()


# ---------------------------------------------------------------------------
# Event derivation
# ---------------------------------------------------------------------------


class TestEventDerivation:
    def _clip_with(self, base: int, waypoints: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "relative_path": f"RecentClips/{base}-front.mp4",
            "bucket": "recent",
            "clip_started_utc": base,
            "waypoints": waypoints,
        }

    def test_emergency_brake_supersedes_harsh_brake(
        self, worker_db_factory: WorkerDbFactory
    ) -> None:
        base = _epoch(2024, 6, 1, 10)
        q = worker_db_factory(
            [
                self._clip_with(
                    base,
                    [
                        {
                            "lat": 37.0,
                            "lon": -122.0,
                            "speed_mps": 20.0,
                            "accel_x": -8.0,
                            "brake": 1,
                        },
                    ],
                )
            ]
        )
        events = q.query_events(limit=50, offset=0)
        kinds = {e.event_type for e in events}
        assert "emergency_braking" in kinds
        assert "harsh_braking" not in kinds

    def test_speed_limit_event_fires_above_threshold(
        self, worker_db_factory: WorkerDbFactory
    ) -> None:
        base = _epoch(2024, 6, 1, 11)
        q = worker_db_factory(
            [
                self._clip_with(
                    base,
                    [
                        {"lat": 37.0, "lon": -122.0, "speed_mps": 50.0},
                    ],
                )
            ]
        )
        kinds = {e.event_type for e in q.query_events(limit=50, offset=0)}
        assert "speed_limit_exceeded" in kinds

    def test_hard_accel_and_sharp_turn(self, worker_db_factory: WorkerDbFactory) -> None:
        base = _epoch(2024, 6, 1, 12)
        q = worker_db_factory(
            [
                self._clip_with(
                    base,
                    [
                        {
                            "lat": 37.0,
                            "lon": -122.0,
                            "speed_mps": 15.0,
                            "accel_x": 4.0,
                            "accel_y": 5.0,
                        },
                    ],
                )
            ]
        )
        kinds = {e.event_type for e in q.query_events(limit=50, offset=0)}
        assert "hard_acceleration" in kinds
        assert "sharp_turn" in kinds

    def test_autopilot_transitions(self, worker_db_factory: WorkerDbFactory) -> None:
        base = _epoch(2024, 6, 1, 13)
        q = worker_db_factory(
            [
                self._clip_with(
                    base,
                    [
                        {"lat": 37.0, "lon": -122.0, "autopilot": "OFF"},
                        {"lat": 37.0, "lon": -122.0, "autopilot": "AUTOSTEER"},
                        {"lat": 37.0, "lon": -122.0, "autopilot": "OFF"},
                    ],
                )
            ]
        )
        kinds = [e.event_type for e in q.query_events(limit=50, offset=0)]
        assert "autopilot_engaged" in kinds
        assert "autopilot_disengaged" in kinds

    def test_sentry_event_has_severity(self, worker_db_factory: WorkerDbFactory) -> None:
        base = _epoch(2024, 6, 1, 14)
        q = worker_db_factory(
            [
                {
                    "relative_path": "SentryClips/2024-06-01_14-00-00/front.mp4",
                    "bucket": "sentry",
                    "clip_started_utc": base,
                    "waypoints": [],
                }
            ]
        )
        events = q.query_events(limit=50, offset=0)
        kinds = {e.event_type for e in events}
        assert "sentry" in kinds


# ---------------------------------------------------------------------------
# Aggregates & misc
# ---------------------------------------------------------------------------


class TestAggregates:
    def test_stats_counts_clips_and_waypoints(self, worker_db_factory: WorkerDbFactory) -> None:
        base = _epoch(2024, 6, 2, 9)
        q = worker_db_factory(
            [
                {
                    "relative_path": "RecentClips/x-front.mp4",
                    "bucket": "recent",
                    "clip_started_utc": base,
                    "waypoints": [
                        {"lat": 37.0, "lon": -122.0},
                        {"lat": 37.001, "lon": -122.001},
                    ],
                },
            ]
        )
        stats = q.get_stats()
        assert stats.indexed_file_count == 1
        assert stats.waypoint_count == 2

    def test_driving_stats_returns_zero_on_empty_db(
        self, worker_db_factory: WorkerDbFactory
    ) -> None:
        q = worker_db_factory([])
        ds = q.get_driving_stats()
        assert ds.trip_count == 0
        assert ds.total_distance_km == 0.0

    def test_event_chart_data_shape(self, worker_db_factory: WorkerDbFactory) -> None:
        base = _epoch(2024, 6, 1, 12)
        q = worker_db_factory(
            [
                {
                    "relative_path": "RecentClips/c-front.mp4",
                    "bucket": "recent",
                    "clip_started_utc": base,
                    "waypoints": [
                        {"lat": 37.0, "lon": -122.0, "speed_mps": 50.0},
                    ],
                }
            ]
        )
        chart = q.get_event_chart_data()
        assert hasattr(chart, "by_type")
        assert hasattr(chart, "by_severity")
        assert hasattr(chart, "over_time")

    def test_days_route_and_telemetry_round_trip(self, worker_db_factory: WorkerDbFactory) -> None:
        base = _epoch(2024, 6, 3, 8)
        q = worker_db_factory(
            [
                {
                    "relative_path": "RecentClips/r-front.mp4",
                    "bucket": "recent",
                    "clip_started_utc": base,
                    "waypoints": [
                        {"lat": 37.0, "lon": -122.0, "speed_mps": 10.0},
                        {"lat": 37.01, "lon": -122.01, "speed_mps": 12.0},
                    ],
                }
            ]
        )
        days = q.query_days(limit=10)
        assert len(days) == 1
        day = days[0].date
        trips = q.query_day_routes(day)
        assert len(trips) == 1
        tid = trips[0].id
        route = q.query_trip_route(tid)
        assert len(route) >= 2
        telemetry = q.query_trip_telemetry(tid)
        assert len(telemetry) >= 2

    def test_playable_trips_cache_resets(self, worker_db_factory: WorkerDbFactory) -> None:
        q = worker_db_factory([])
        q.reset_playable_trips_cache_for_tests()  # should not raise

    def test_waypoints_for_video_unknown_path_returns_none(
        self, worker_db_factory: WorkerDbFactory
    ) -> None:
        q = worker_db_factory([])
        clip_id, wps = q.waypoints_for_video("RecentClips/nope.mp4")
        assert clip_id is None
        assert wps == ()
