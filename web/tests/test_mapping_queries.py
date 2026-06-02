"""Tests for the read-only mapping_queries layer (ADR-0017).

These tests build a synthetic copy of the Rust worker DB (schema v2)
in-memory, then exercise every public method on :class:`MappingQueries`
to ensure trip grouping, event derivation and aggregate stats all
work end-to-end without the legacy Python indexer/migrations.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, NoReturn

import pytest
from teslausb_web.services import mapping_queries
from teslausb_web.services.mapping_queries import (
    MappingQueries,
    MappingQueriesConfig,
    MappingQueryError,
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

_MATERIALISED_DDL = """
CREATE TABLE trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_utc INTEGER NOT NULL,
    end_utc INTEGER NOT NULL,
    start_clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    end_clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    start_lat REAL,
    start_lon REAL,
    end_lat REAL,
    end_lon REAL,
    distance_km REAL NOT NULL DEFAULT 0,
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    waypoint_count INTEGER NOT NULL DEFAULT 0,
    event_count INTEGER NOT NULL DEFAULT 0,
    video_count INTEGER NOT NULL DEFAULT 0,
    bucket TEXT NOT NULL DEFAULT 'recent'
);
CREATE TABLE detected_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER REFERENCES trips(id) ON DELETE CASCADE,
    clip_id INTEGER REFERENCES clips(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    timestamp_utc INTEGER NOT NULL,
    latitude_deg REAL,
    longitude_deg REAL,
    speed_mps REAL,
    metadata_json TEXT,
    description TEXT NOT NULL DEFAULT '',
    frame_index INTEGER
);
CREATE TABLE clip_trip_map (
    clip_id INTEGER PRIMARY KEY REFERENCES clips(id) ON DELETE CASCADE,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE
);
"""

_CLIP_EVENTS_DDL = """
CREATE TABLE clip_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_json_relative_path TEXT NOT NULL UNIQUE,
    event_dir_relative_path TEXT NOT NULL,
    bucket TEXT NOT NULL,
    primary_clip_id INTEGER REFERENCES clips(id) ON DELETE SET NULL,
    timestamp_utc INTEGER NOT NULL,
    est_lat REAL,
    est_lon REAL,
    reason TEXT,
    city TEXT,
    camera TEXT,
    indexed_at_utc INTEGER NOT NULL
);
"""


def _fail_derivation(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("Python derivation fallback must not run")


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


def _create_empty_materialised_tables(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_MATERIALISED_DDL)
        conn.commit()
    finally:
        conn.close()


def _create_clip_events_table(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_CLIP_EVENTS_DDL)
        conn.commit()
    finally:
        conn.close()


def _clip_id_for_path(db_path: Path, relative_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM clips WHERE relative_path = ?",
            (relative_path,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise AssertionError(f"clip not found: {relative_path}")
    return int(row[0])


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
# Materialised-table hot path
# ---------------------------------------------------------------------------


class TestMaterialisedHotPath:
    def test_empty_materialised_tables_do_not_run_python_derivation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        db_path = tmp_path / "index.sqlite3"
        _seed_db(
            db_path,
            [
                {
                    "relative_path": "RecentClips/present-front.mp4",
                    "bucket": "recent",
                    "clip_started_utc": _epoch(2024, 6, 4, 9),
                    "waypoints": [{"lat": 37.0, "lon": -122.0}],
                }
            ],
        )
        _create_empty_materialised_tables(db_path)
        caplog.set_level("WARNING")
        monkeypatch.setattr(mapping_queries, "group_trips", _fail_derivation)
        monkeypatch.setattr(mapping_queries, "derive_trip_events", _fail_derivation)
        monkeypatch.setattr(mapping_queries, "derive_sentry_events", _fail_derivation)
        queries = MappingQueries(
            config=MappingQueriesConfig(db_path=db_path, media_root=tmp_path / "media")
        )

        assert queries.query_trips(limit=10, offset=0, min_distance_km=0.0) == ()
        assert queries.query_events(limit=10, offset=0) == ()
        assert queries.query_days(limit=10, min_distance_km=0.0) == ()
        assert queries.query_latest_date() is None
        assert queries.query_day_payload("2024-06-04").trips == ()
        assert queries.get_event_chart_data().by_type == ()
        assert "Python derivation fallback" not in caplog.text

    def test_missing_materialised_tables_keep_prebootstrap_derivation(
        self,
        worker_db_factory: WorkerDbFactory,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level("WARNING")
        original_group_trips = mapping_queries.group_trips
        group_trips_calls = 0

        def counted_group_trips(
            clips: Sequence[mapping_queries.WorkerClip], gap_seconds: int
        ) -> tuple[mapping_queries.TripGroup, ...]:
            nonlocal group_trips_calls
            group_trips_calls += 1
            return original_group_trips(clips, gap_seconds)

        monkeypatch.setattr(mapping_queries, "group_trips", counted_group_trips)
        queries = worker_db_factory(
            [
                {
                    "relative_path": "RecentClips/prebootstrap-front.mp4",
                    "bucket": "recent",
                    "clip_started_utc": _epoch(2024, 6, 5, 9),
                    "waypoints": [{"lat": 37.0, "lon": -122.0}],
                }
            ]
        )

        assert len(queries.query_trips(limit=10, offset=0, min_distance_km=0.0)) == 1
        assert group_trips_calls == 1
        assert "materialised trips/events tables missing" in caplog.text

    def test_clip_events_surface_as_events_and_latest_day(self, tmp_path: Path) -> None:
        db_path = tmp_path / "index.sqlite3"
        clip_path = "SavedClips/2024-06-06_10-00-00/2024-06-06_10-00-00-front.mp4"
        event_epoch = _epoch(2024, 6, 6, 10, 0)
        _seed_db(
            db_path,
            [
                {
                    "relative_path": clip_path,
                    "bucket": "saved",
                    "clip_started_utc": event_epoch - 5,
                    "waypoints": [],
                }
            ],
        )
        _create_empty_materialised_tables(db_path)
        _create_clip_events_table(db_path)
        clip_id = _clip_id_for_path(db_path, clip_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO clip_events(event_json_relative_path, event_dir_relative_path, "
                "bucket, primary_clip_id, timestamp_utc, est_lat, est_lon, reason, city, "
                "camera, indexed_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "SavedClips/2024-06-06_10-00-00/event.json",
                    "SavedClips/2024-06-06_10-00-00",
                    "saved",
                    clip_id,
                    event_epoch,
                    37.25,
                    -122.25,
                    "user_honk",
                    "palo_alto",
                    "front",
                    event_epoch + 1,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        queries = MappingQueries(
            config=MappingQueriesConfig(db_path=db_path, media_root=tmp_path / "media")
        )

        events = queries.query_events(date="2024-06-06", limit=10)
        assert len(events) == 1
        event = events[0]
        assert event.event_type == "saved"
        assert event.lat == 37.25
        assert event.lon == -122.25
        assert event.video_path == clip_path
        assert event.frame_offset == 180
        assert event.description == "User Honk | Palo Alto | Front"
        assert queries.query_events(event_type="saved", limit=10) == events
        assert (
            queries.query_events(
                bbox=(37.0, -123.0, 38.0, -122.0),
                limit=10,
            )
            == events
        )

        payload = queries.query_day_payload("2024-06-06")
        assert payload.trips == ()
        assert payload.events == events
        days = queries.query_days(limit=10, min_distance_km=0.0)
        assert days[0].date == "2024-06-06"
        assert days[0].event_count == 1
        assert queries.query_latest_date() == "2024-06-06"

    def test_clip_event_buckets_by_display_timezone(self, tmp_path: Path) -> None:
        # Regression for the operator bug (ADR-0025): a honk + drive on the
        # evening of June 1 in America/Detroit (EDT) is stored as its true
        # UTC instant 2024-06-02 00:10Z. Bucketing by UTC files it under
        # June 2 (the bug); bucketing by the operator's zone files it under
        # June 1. The tz must thread all the way through query_events,
        # query_day_payload, query_days and query_latest_date.
        db_path = tmp_path / "index.sqlite3"
        # 2024-06-02 00:10 UTC == 2024-06-01 20:10 EDT.
        event_epoch = _epoch(2024, 6, 2, 0, 10)
        clip_path = "SavedClips/2024-06-01_20-10-00/2024-06-01_20-10-00-front.mp4"
        _seed_db(
            db_path,
            [
                {
                    "relative_path": clip_path,
                    "bucket": "saved",
                    "clip_started_utc": event_epoch - 5,
                    "waypoints": [],
                }
            ],
        )
        _create_empty_materialised_tables(db_path)
        _create_clip_events_table(db_path)
        clip_id = _clip_id_for_path(db_path, clip_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO clip_events(event_json_relative_path, event_dir_relative_path, "
                "bucket, primary_clip_id, timestamp_utc, est_lat, est_lon, reason, city, "
                "camera, indexed_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "SavedClips/2024-06-01_20-10-00/event.json",
                    "SavedClips/2024-06-01_20-10-00",
                    "saved",
                    clip_id,
                    event_epoch,
                    42.33,
                    -83.05,
                    "user_honk",
                    "detroit",
                    "front",
                    event_epoch + 1,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        queries = MappingQueries(
            config=MappingQueriesConfig(db_path=db_path, media_root=tmp_path / "media")
        )

        detroit = "America/Detroit"
        # Operator's local zone: the event belongs to June 1.
        assert len(queries.query_events(date="2024-06-01", tz_name=detroit)) == 1
        assert queries.query_events(date="2024-06-02", tz_name=detroit) == ()
        assert queries.query_day_payload("2024-06-01", tz_name=detroit).events != ()
        detroit_days = queries.query_days(limit=10, min_distance_km=0.0, tz_name=detroit)
        assert detroit_days[0].date == "2024-06-01"
        assert queries.query_latest_date(detroit) == "2024-06-01"

        # UTC (the back-compat default): the same event buckets to June 2.
        assert len(queries.query_events(date="2024-06-02")) == 1
        assert queries.query_events(date="2024-06-01") == ()
        utc_days = queries.query_days(limit=10, min_distance_km=0.0)
        assert utc_days[0].date == "2024-06-02"
        assert queries.query_latest_date() == "2024-06-02"

    def test_detected_event_buckets_by_trip_day_across_local_midnight(
        self, tmp_path: Path
    ) -> None:
        # Regression (GPT-5.5 review, ADR-0025): a trip-attached detected
        # event whose own timestamp drifts across local midnight must bucket
        # by its owning trip's start day in /api/events?date= and in
        # DayPayload.latest_date, identical to the day payload and the day
        # rollup. Otherwise the same honk renders under one day on the map
        # (trip day) and another in the events list (event-timestamp day).
        db_path = tmp_path / "index.sqlite3"
        trip_start = _epoch(2024, 6, 2, 3, 50)  # 2024-06-01 23:50 EDT (June 1 local)
        event_epoch = _epoch(2024, 6, 2, 4, 15)  # 2024-06-02 00:15 EDT (June 2 local)
        clip_path = "RecentClips/2024-06-01_23-50-00/2024-06-01_23-50-00-front.mp4"
        _seed_db(
            db_path,
            [
                {
                    "relative_path": clip_path,
                    "bucket": "recent",
                    "clip_started_utc": trip_start,
                    "waypoints": [],
                }
            ],
        )
        _create_empty_materialised_tables(db_path)
        _create_clip_events_table(db_path)
        clip_id = _clip_id_for_path(db_path, clip_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO trips(start_utc, end_utc, start_clip_id, end_clip_id, "
                "distance_km, duration_seconds, video_count, bucket) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (trip_start, event_epoch, clip_id, clip_id, 5.0, 1500, 1, "recent"),
            )
            trip_id = int(conn.execute("SELECT id FROM trips").fetchone()[0])
            conn.execute(
                "INSERT INTO detected_events(trip_id, clip_id, event_type, severity, "
                "timestamp_utc, latitude_deg, longitude_deg, description) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (trip_id, clip_id, "honk", "info", event_epoch, 42.33, -83.05, ""),
            )
            conn.commit()
        finally:
            conn.close()
        queries = MappingQueries(
            config=MappingQueriesConfig(db_path=db_path, media_root=tmp_path / "media")
        )

        detroit = "America/Detroit"
        # Trip-attached event buckets by the trip's start day (June 1 local),
        # NOT by its own timestamp (June 2 local).
        assert len(queries.query_events(date="2024-06-01", tz_name=detroit)) == 1
        assert queries.query_events(date="2024-06-02", tz_name=detroit) == ()
        # The day payload already bucketed by trip day; the events list and
        # latest_date (via _latest_date_locked) must now agree.
        assert queries.query_day_payload("2024-06-01", tz_name=detroit).events != ()
        assert (
            queries.query_day_payload("2024-06-01", tz_name=detroit).latest_date == "2024-06-01"
        )

    def test_clip_event_without_primary_clip_has_no_video_path(
        self, tmp_path: Path
    ) -> None:
        # A clip_event whose primary clip was overwritten by Tesla keeps its
        # event.json row but the FK is nulled (ON DELETE SET NULL). The event
        # must still surface (so the day/pin count is honest) but with a null
        # video_path, so the timeline renders no folder-style Play/Download/
        # Delete buttons that would dead-click. Only the map button (coords)
        # is offered. This guards the folderBacked gating in
        # video_panel_timelines.js.
        db_path = tmp_path / "index.sqlite3"
        event_epoch = _epoch(2024, 6, 7, 11, 0)
        _seed_db(db_path, [])
        _create_empty_materialised_tables(db_path)
        _create_clip_events_table(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO clip_events(event_json_relative_path, "
                "event_dir_relative_path, bucket, primary_clip_id, "
                "timestamp_utc, est_lat, est_lon, reason, city, camera, "
                "indexed_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "SavedClips/2024-06-07_11-00-00/event.json",
                    "SavedClips/2024-06-07_11-00-00",
                    "saved",
                    None,
                    event_epoch,
                    37.5,
                    -122.5,
                    "user_honk",
                    "palo_alto",
                    "front",
                    event_epoch + 1,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        queries = MappingQueries(
            config=MappingQueriesConfig(db_path=db_path, media_root=tmp_path / "media")
        )

        events = queries.query_events(date="2024-06-07", limit=10)
        assert len(events) == 1
        event = events[0]
        assert event.event_type == "saved"
        assert event.video_path is None
        assert event.frame_offset is None
        assert event.lat == 37.5
        assert event.lon == -122.5
        assert queries.query_day_payload("2024-06-07").events == events

    def test_missing_clip_events_table_is_empty(self, tmp_path: Path) -> None:
        db_path = tmp_path / "index.sqlite3"
        _seed_db(db_path, [])
        _create_empty_materialised_tables(db_path)
        queries = MappingQueries(
            config=MappingQueriesConfig(db_path=db_path, media_root=tmp_path / "media")
        )

        assert queries.query_events(event_type="saved", limit=10) == ()
        assert queries.query_day_payload("2024-06-06").events == ()
        assert queries.query_days(limit=10, min_distance_km=0.0) == ()


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


# ---------------------------------------------------------------------------
# Error paths & extra coverage (no blueprint required)
# ---------------------------------------------------------------------------


class TestErrors:
    def test_missing_db_raises_mapping_query_error(self, tmp_path: Path) -> None:
        cfg = MappingQueriesConfig(
            db_path=tmp_path / "does-not-exist.sqlite3",
            media_root=tmp_path,
        )
        queries = MappingQueries(config=cfg)
        with pytest.raises(MappingQueryError, match="Failed to open worker DB"):
            queries.query_trips()

    def test_make_mapping_queries_from_webconfig(self, tmp_path: Path) -> None:
        from teslausb_web.config import (
            MappingSection,
            PathsSection,
            StorageRetentionSection,
            WebConfig,
            WebSection,
        )
        from teslausb_web.services.mapping_queries import make_mapping_queries

        backing = tmp_path / "backing"
        backing.mkdir()
        cfg = WebConfig(
            web=WebSection(secret_key="x" * 32),
            paths=PathsSection(
                backing_root=backing,
                state_dir=tmp_path / "state",
                cache_invalidate_script=tmp_path / "invalidate.sh",
            ),
            storage_retention=StorageRetentionSection(
                policy_path=tmp_path / "state" / "retention_policy.json"
            ),
            mapping=MappingSection(
                db_path=tmp_path / "index.sqlite3",
                media_root=backing,
            ),
            source_path=None,
        )
        queries = make_mapping_queries(cfg)
        assert isinstance(queries, MappingQueries)

    def test_make_mapping_queries_from_explicit_config(self, tmp_path: Path) -> None:
        from teslausb_web.services.mapping_queries import make_mapping_queries

        backing = tmp_path / "backing"
        backing.mkdir()
        cfg = MappingQueriesConfig(
            db_path=tmp_path / "index.sqlite3",
            media_root=backing,
        )
        queries = make_mapping_queries(cfg)
        assert isinstance(queries, MappingQueries)


class TestPlayableCache:
    def test_cache_hit_returns_same_payload(self, worker_db_factory: WorkerDbFactory) -> None:
        q = worker_db_factory([])
        first = q.playable_trips_for_date("2024-06-01")
        second = q.playable_trips_for_date("2024-06-01")
        # Cache hit returns the exact same tuple instance.
        assert first is second

    def test_cache_trims_when_oversized(self, worker_db_factory: WorkerDbFactory) -> None:
        q = worker_db_factory([])
        # 65 distinct valid dates pushes the cache past
        # _MAX_PLAYABLE_CACHE_ENTRIES (64).
        base = date(2024, 1, 1)
        for offset in range(65):
            q.playable_trips_for_date((base + timedelta(days=offset)).isoformat())
        # After trimming, the internal map must be <= 64 entries.
        assert len(q._playable_trips_cache) <= 64  # whitebox check


class TestLatestDate:
    """``query_latest_date`` is the bare-``/`` landing redirect target.

    It must agree with the day view about which days have data — including
    event-only days — and must degrade through the same SQL→snapshot ladder
    as every other query (here exercised via the schema-v2 fixture, which
    has no materialised ``trips`` table and so runs the snapshot path).
    """

    def test_none_when_empty(self, worker_db_factory: WorkerDbFactory) -> None:
        assert worker_db_factory([]).query_latest_date() is None

    def test_returns_trip_day_when_only_trips(self, worker_db_factory: WorkerDbFactory) -> None:
        q = worker_db_factory(
            [
                {
                    "relative_path": "RecentClips/a-front.mp4",
                    "bucket": "recent",
                    "clip_started_utc": _epoch(2024, 6, 2, 9),
                    "waypoints": [{"lat": 37.0, "lon": -122.0, "speed_mps": 10.0}],
                }
            ]
        )
        assert q.query_latest_date() == "2024-06-02"

    def test_event_only_later_day_wins_over_trip(self, worker_db_factory: WorkerDbFactory) -> None:
        # A driving trip on the 1st, then a sentry-only event two days
        # later. The latest date must be the event-only day, not the trip
        # day — the original trips-only MAX() regressed this.
        q = worker_db_factory(
            [
                {
                    "relative_path": "RecentClips/trip-front.mp4",
                    "bucket": "recent",
                    "clip_started_utc": _epoch(2024, 6, 1, 10),
                    "waypoints": [{"lat": 37.0, "lon": -122.0, "speed_mps": 10.0}],
                },
                {
                    "relative_path": "SentryClips/2024-06-03_20-00-00/front.mp4",
                    "bucket": "sentry",
                    "clip_started_utc": _epoch(2024, 6, 3, 20),
                    "waypoints": [],
                },
            ]
        )
        assert q.query_latest_date() == "2024-06-03"
