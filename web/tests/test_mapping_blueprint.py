"""Tests for ``teslausb_web.blueprints.mapping`` (ADR-0017 read layer).

The mapping blueprint was deleted along with the dual-parser
architecture in commit ``4f92365`` but never rewritten. This file
covers every surviving route end-to-end against a synthetic copy
of the Rust worker DB (schema v2), plus the documented error paths
(bad date, unknown trip, unsafe segment).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import pytest
from teslausb_web.app import create_app
from teslausb_web.config import (
    MappingSection,
    PathsSection,
    StorageRetentionSection,
    WebConfig,
    WebSection,
)

if TYPE_CHECKING:
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient


# ---------------------------------------------------------------------------
# Worker DB fixture (schema v2 — mirrors rust/.../store/schema.rs)
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


def _epoch(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp())


def _seed_db(db_path: Path, clips: list[dict[str, Any]]) -> None:
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


def _build_trip_clip(  # noqa: PLR0913 — synthetic seed; many independent waypoint axes
    *,
    folder: str,
    base_name: str,
    clip_started_utc: int,
    waypoint_count: int = 10,
    base_lat: float = 37.0,
    base_lon: float = -122.0,
    speed_mps: float = 15.0,
    autopilot: str | None = None,
    accel_x: float | None = None,
    accel_y: float | None = None,
) -> dict[str, Any]:
    waypoints: list[dict[str, Any]] = [
        {
            "timestamp_ms": i * 1000.0,
            "lat": base_lat + i * 0.0005,
            "lon": base_lon + i * 0.0005,
            "speed_mps": speed_mps,
            "heading": 90.0,
            "autopilot": autopilot,
            "accel_x": accel_x if i == waypoint_count // 2 else None,
            "accel_y": accel_y if i == waypoint_count // 2 else None,
        }
        for i in range(waypoint_count)
    ]
    return {
        "relative_path": f"{folder}/{base_name}-front.mp4",
        "bucket": "recent",
        "clip_started_utc": clip_started_utc,
        "waypoints": waypoints,
    }


def _dataset() -> list[dict[str, Any]]:
    """Two trips of 3 clips each, plus one sentry clip."""
    day1 = _epoch(2024, 6, 1, 8, 0)
    day2 = _epoch(2024, 6, 2, 14, 0)
    # Trip A — 3 contiguous clips, 60 s apart. Includes a harsh brake
    # (clip 0 wp 5) and an AP engage->disengage transition (clip 1).
    clips: list[dict[str, Any]] = [
        _build_trip_clip(
            folder="RecentClips/2024-06-01_08-00-00",
            base_name=f"2024-06-01_08-0{index}-00",
            clip_started_utc=day1 + index * 60,
            waypoint_count=10,
            base_lat=37.0 + index * 0.005,
            base_lon=-122.0 + index * 0.005,
            speed_mps=12.0 + index,
            autopilot="AUTOSTEER" if index == 1 else None,
            accel_x=-5.0 if index == 0 else None,
        )
        for index in range(3)
    ]
    # Trip B — second day, 3 clips, includes a speed-limit-exceeded
    # waypoint (50 m/s > 35.76 m/s) and a sharp-turn lateral accel.
    clips.extend(
        _build_trip_clip(
            folder="RecentClips/2024-06-02_14-00-00",
            base_name=f"2024-06-02_14-0{index}-00",
            clip_started_utc=day2 + index * 60,
            waypoint_count=10,
            base_lat=40.0 + index * 0.005,
            base_lon=-74.0 + index * 0.005,
            speed_mps=50.0 if index == 1 else 20.0,
            accel_y=5.5 if index == 2 else None,
        )
        for index in range(3)
    )
    # Sentry clip with zero GPS waypoints — derives a sentry event.
    clips.append(
        {
            "relative_path": "SentryClips/2024-06-01_18-00-00/front.mp4",
            "bucket": "sentry",
            "clip_started_utc": _epoch(2024, 6, 1, 18, 0),
            "waypoints": [],
        }
    )
    return clips


# ---------------------------------------------------------------------------
# App / client fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def media_root(tmp_path: Path) -> Path:
    root = tmp_path / "backing"
    root.mkdir()
    # Materialise representative video files so playable-trips returns
    # True for trip A and the event-clips/event-details routes work
    # against a real directory tree.
    recent = root / "RecentClips" / "2024-06-01_08-00-00"
    recent.mkdir(parents=True)
    for index in range(3):
        (recent / f"2024-06-01_08-0{index}-00-front.mp4").write_bytes(b"x" * 1024)
    sentry = root / "SentryClips" / "2024-06-01_18-00-00"
    sentry.mkdir(parents=True)
    (sentry / "2024-06-01_18-00-00-front.mp4").write_bytes(b"y" * 2048)
    return root


@pytest.fixture
def app(tmp_path: Path, media_root: Path) -> Flask:
    db_path = tmp_path / "index.sqlite3"
    _seed_db(db_path, _dataset())
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    overrides_path = state_dir / "mapping_settings.json"
    view_prefs_path = state_dir / "map_view_prefs.json"
    overrides_path.write_text(
        '{"schema_version": 1, "trip_gap_minutes": 5, "speed_limit_mph": 80}\n',
        encoding="utf-8",
    )
    view_prefs_path.write_text(
        '{"schema_version": 1, "speed_units": "kph"}\n',
        encoding="utf-8",
    )
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32),
        paths=PathsSection(
            backing_root=media_root,
            state_dir=state_dir,
            cache_invalidate_script=tmp_path / "invalidate.sh",
        ),
        storage_retention=StorageRetentionSection(policy_path=state_dir / "retention_policy.json"),
        mapping=MappingSection(
            db_path=db_path,
            media_root=media_root,
            overrides_path=overrides_path,
            view_prefs_path=view_prefs_path,
        ),
        source_path=None,
    )
    flask_app = create_app(cfg)
    flask_app.testing = True
    return flask_app


@pytest.fixture
def empty_app(tmp_path: Path) -> Flask:
    """An app whose worker DB exists but has zero clips."""
    db_path = tmp_path / "index.sqlite3"
    _seed_db(db_path, [])
    media = tmp_path / "backing"
    media.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32),
        paths=PathsSection(
            backing_root=media,
            state_dir=state_dir,
            cache_invalidate_script=tmp_path / "invalidate.sh",
        ),
        storage_retention=StorageRetentionSection(policy_path=state_dir / "retention_policy.json"),
        mapping=MappingSection(
            db_path=db_path,
            media_root=media,
            overrides_path=state_dir / "mapping_settings.json",
            view_prefs_path=state_dir / "map_view_prefs.json",
        ),
        source_path=None,
    )
    flask_app = create_app(cfg)
    flask_app.testing = True
    return flask_app


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def empty_client(empty_app: Flask) -> FlaskClient:
    return empty_app.test_client()


# ---------------------------------------------------------------------------
# URL map
# ---------------------------------------------------------------------------


class TestUrlMap:
    def test_all_mapping_routes_are_registered(self, app: Flask) -> None:
        endpoints = {
            r.endpoint for r in app.url_map.iter_rules() if r.endpoint.startswith("mapping.")
        }
        assert endpoints == {
            "mapping.map_view",
            "mapping.api_trips",
            "mapping.api_trip_route",
            "mapping.api_trip_telemetry",
            "mapping.api_waypoints_for_clip",
            "mapping.api_events",
            "mapping.api_days",
            "mapping.api_day_routes",
            "mapping.api_day_payload",
            "mapping.api_trips_playable",
            "mapping.api_stats",
            "mapping.api_driving_stats",
            "mapping.api_event_charts",
            "mapping.api_sentry_events",
            "mapping.api_event_details",
            "mapping.api_event_clips",
        }


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------


class TestMapView:
    def test_index_does_not_redirect_and_exposes_latest_day(
        self, client: FlaskClient
    ) -> None:
        # Bare "/" no longer server-redirects to a UTC-computed day (that
        # would flash the wrong day for an operator whose evening drive
        # crossed midnight UTC). In the default "Auto" mode (no saved
        # timezone override) the server cannot know the browser zone on
        # first paint, so it embeds an EMPTY latest_date and lets
        # init.js + /api/days resolve the latest *local* day. Pre-seeding a
        # UTC day here is exactly the flash-the-wrong-day bug.
        response = client.get("/")
        assert response.status_code == HTTPStatus.OK
        body = response.get_data(as_text=True)
        assert '"date": ""' in body
        assert '"latest_date": ""' in body

    def test_index_preseeds_latest_day_when_override_set(self, app: Flask) -> None:
        # With an explicit Settings timezone override the server zone is
        # authoritative on first paint (the client buckets identically), so
        # the server CAN pre-seed the latest day with no flash risk.
        prefs = app.extensions["map_view_prefs_service"]
        prefs.save_preferences(speed_units="kph", display_timezone="UTC")
        response = app.test_client().get("/")
        assert response.status_code == HTTPStatus.OK
        body = response.get_data(as_text=True)
        assert '"latest_date": "2024-06-02"' in body
        assert '"display_timezone": "UTC"' in body

    def test_index_page_renders(self, client: FlaskClient) -> None:
        response = client.get("/?date=2024-06-02")
        assert response.status_code == HTTPStatus.OK
        body = response.get_data(as_text=True)
        # The template injects the bootstrap config that contains every
        # JSON-API URL. A few sentinel keys must be present.
        assert "__DATE__" in body
        assert "__TRIP_ID__" in body
        assert '"speed_units": "kph"' in body
        assert "js/mapping/speed_units.js" in body

    def test_index_renders_without_redirect_when_empty(self, empty_client: FlaskClient) -> None:
        # No data -> nothing to redirect to; the empty map must still render.
        response = empty_client.get("/")
        assert response.status_code == HTTPStatus.OK
        assert "__DATE__" in response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Trip routes
# ---------------------------------------------------------------------------


class TestTrips:
    def test_api_trips_returns_seeded_trips(self, client: FlaskClient) -> None:
        response = client.get("/api/trips")
        assert response.status_code == HTTPStatus.OK
        payload = response.get_json()
        assert isinstance(payload, dict)
        trips = payload["trips"]
        assert len(trips) == 2
        # Most-recent first.
        assert trips[0]["start_time"] > trips[1]["start_time"]
        assert trips[0]["video_count"] == 3
        for trip in trips:
            assert {"id", "start_time", "distance_km", "event_count"} <= trip.keys()

    def test_api_trips_respects_date_filter(self, client: FlaskClient) -> None:
        response = client.get("/api/trips?date_from=2024-06-02&date_to=2024-06-03")
        trips = response.get_json()["trips"]
        assert len(trips) == 1
        assert trips[0]["start_time"].startswith("2024-06-02")

    def test_api_trips_respects_bbox(self, client: FlaskClient) -> None:
        # Trip A is at ~37/-122; trip B is at ~40/-74. Restrict to NYC.
        response = client.get(
            "/api/trips?min_lat=39&min_lon=-75&max_lat=41&max_lon=-73",
        )
        trips = response.get_json()["trips"]
        assert len(trips) == 1
        assert trips[0]["start_lat"] is not None
        assert trips[0]["start_lat"] > 39

    def test_api_trips_drops_short_trips(self, client: FlaskClient) -> None:
        response = client.get("/api/trips?min_distance=10000")
        assert response.get_json()["trips"] == []

    def test_api_trips_limit_and_offset(self, client: FlaskClient) -> None:
        first = client.get("/api/trips?limit=1&offset=0").get_json()["trips"]
        second = client.get("/api/trips?limit=1&offset=1").get_json()["trips"]
        assert len(first) == 1
        assert len(second) == 1
        assert first[0]["id"] != second[0]["id"]

    def test_api_trips_page_reports_has_next(self, client: FlaskClient) -> None:
        first = client.get("/api/trips?limit=1&page=1").get_json()
        second = client.get("/api/trips?limit=1&page=2").get_json()
        assert first["has_next"] is True
        assert first["next_page"] == 2
        assert len(first["trips"]) == 1
        assert second["has_next"] is False
        assert second["trips"][0]["id"] != first["trips"][0]["id"]


class TestTripRoute:
    def test_returns_geojson_feature_for_known_trip(self, client: FlaskClient) -> None:
        trip_id = client.get("/api/trips").get_json()["trips"][0]["id"]
        response = client.get(f"/api/trip/{trip_id}/route")
        assert response.status_code == HTTPStatus.OK
        payload = response.get_json()
        assert payload["type"] == "Feature"
        assert payload["geometry"]["type"] == "LineString"
        assert payload["properties"]["trip_id"] == trip_id
        assert payload["properties"]["waypoint_count"] >= 10

    def test_unknown_trip_returns_404(self, client: FlaskClient) -> None:
        response = client.get("/api/trip/9999999/route")
        assert response.status_code == HTTPStatus.NOT_FOUND
        assert response.get_json()["success"] is False


class TestTripTelemetry:
    def test_returns_telemetry_for_known_trip(self, client: FlaskClient) -> None:
        trip_id = client.get("/api/trips").get_json()["trips"][0]["id"]
        response = client.get(f"/api/trip/{trip_id}/telemetry")
        assert response.status_code == HTTPStatus.OK
        payload = response.get_json()
        assert payload["trip_id"] == trip_id
        assert isinstance(payload["telemetry"], dict)
        assert len(payload["telemetry"]) >= 10

    def test_unknown_trip_returns_empty_telemetry(self, client: FlaskClient) -> None:
        response = client.get("/api/trip/9999999/telemetry")
        assert response.status_code == HTTPStatus.OK
        assert response.get_json()["telemetry"] == {}


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class TestEvents:
    def test_events_listing_includes_speed_and_brake(self, client: FlaskClient) -> None:
        response = client.get("/api/events?limit=200")
        assert response.status_code == HTTPStatus.OK
        kinds = {event["event_type"] for event in response.get_json()["events"]}
        assert "harsh_braking" in kinds
        assert "speed_limit_exceeded" in kinds
        assert "sharp_turn" in kinds
        assert "sentry" in kinds

    def test_events_filter_by_type(self, client: FlaskClient) -> None:
        response = client.get("/api/events?type=sentry&limit=10")
        events = response.get_json()["events"]
        assert events
        assert all(e["event_type"] == "sentry" for e in events)

    def test_events_filter_by_severity(self, client: FlaskClient) -> None:
        response = client.get("/api/events?severity=warning&limit=200")
        events = response.get_json()["events"]
        assert all(e["severity"] == "warning" for e in events)

    def test_events_filter_by_date(self, client: FlaskClient) -> None:
        response = client.get("/api/events?date=2024-06-02&limit=200")
        events = response.get_json()["events"]
        assert events
        assert all(e["timestamp"].startswith("2024-06-02") for e in events)

    def test_events_filter_by_bbox(self, client: FlaskClient) -> None:
        response = client.get(
            "/api/events?min_lat=39&min_lon=-75&max_lat=41&max_lon=-73&limit=200",
        )
        events = response.get_json()["events"]
        # All returned events with a location must fall in the NYC bbox.
        for event in events:
            if event["lat"] is not None:
                assert event["lat"] > 39

    def test_events_filter_by_date_range(self, client: FlaskClient) -> None:
        response = client.get(
            "/api/events?date_from=2024-06-02&date_to=2024-06-03&limit=200",
        )
        events = response.get_json()["events"]
        assert events
        assert all(e["timestamp"].startswith("2024-06-02") for e in events)

    def test_events_rejects_invalid_date(self, client: FlaskClient) -> None:
        response = client.get("/api/events?date=06-02-2024")
        assert response.status_code == HTTPStatus.BAD_REQUEST
        assert response.get_json()["success"] is False


# ---------------------------------------------------------------------------
# Days
# ---------------------------------------------------------------------------


class TestDays:
    def test_api_days_returns_one_per_trip_day(self, client: FlaskClient) -> None:
        response = client.get("/api/days")
        assert response.status_code == HTTPStatus.OK
        days = response.get_json()["days"]
        # Two trip days + the sentry-only day (2024-06-01 has both).
        dates = {day["date"] for day in days}
        assert "2024-06-01" in dates
        assert "2024-06-02" in dates

    def test_api_days_caps_to_limit(self, client: FlaskClient) -> None:
        response = client.get("/api/days?limit=1")
        assert len(response.get_json()["days"]) == 1


class TestDayRoutes:
    def test_returns_trips_with_waypoints(self, client: FlaskClient) -> None:
        response = client.get("/api/day/2024-06-01/routes")
        assert response.status_code == HTTPStatus.OK
        payload = response.get_json()
        assert payload["date"] == "2024-06-01"
        assert len(payload["trips"]) == 1
        trip = payload["trips"][0]
        assert trip["waypoints"]

    def test_rejects_invalid_date(self, client: FlaskClient) -> None:
        response = client.get("/api/day/not-a-date/routes")
        assert response.status_code == HTTPStatus.BAD_REQUEST


# ---------------------------------------------------------------------------
# Playable trips
# ---------------------------------------------------------------------------


class TestPlayableTrips:
    def test_returns_true_for_trips_with_files_on_disk(self, client: FlaskClient) -> None:
        response = client.get("/api/trips/playable?date=2024-06-01")
        assert response.status_code == HTTPStatus.OK
        payload = response.get_json()
        assert payload["date"] == "2024-06-01"
        assert payload["trips"]
        assert all(value is True for value in payload["trips"].values())

    def test_returns_false_when_no_files_present(self, client: FlaskClient) -> None:
        # Trip B's files were never created on disk in the fixture.
        response = client.get("/api/trips/playable?date=2024-06-02")
        payload = response.get_json()
        assert payload["trips"]
        assert all(value is False for value in payload["trips"].values())

    def test_rejects_invalid_date(self, client: FlaskClient) -> None:
        response = client.get("/api/trips/playable?date=junk")
        assert response.status_code == HTTPStatus.BAD_REQUEST


# ---------------------------------------------------------------------------
# Stats / charts
# ---------------------------------------------------------------------------


class TestStats:
    def test_returns_aggregates(self, client: FlaskClient) -> None:
        response = client.get("/api/stats")
        assert response.status_code == HTTPStatus.OK
        payload = response.get_json()
        assert payload["trip_count"] == 2
        assert payload["indexed_file_count"] == 7
        assert payload["waypoint_count"] == 60
        assert payload["mapped_file_count"] == 6
        assert "event_breakdown" in payload


class TestDrivingStats:
    def test_has_data_with_seeded_dataset(self, client: FlaskClient) -> None:
        response = client.get("/api/driving-stats")
        assert response.status_code == HTTPStatus.OK
        payload = response.get_json()
        assert payload["has_data"] is True
        assert payload["trip_count"] == 2
        assert payload["max_speed_mph"] > 0

    def test_empty_db_returns_no_data(self, empty_client: FlaskClient) -> None:
        response = empty_client.get("/api/driving-stats")
        assert response.status_code == HTTPStatus.OK
        assert response.get_json()["has_data"] is False


class TestEventCharts:
    def test_returns_chart_shape(self, client: FlaskClient) -> None:
        response = client.get("/api/event-charts")
        assert response.status_code == HTTPStatus.OK
        payload = response.get_json()
        assert "by_type" in payload
        assert "by_severity" in payload
        assert "over_time" in payload
        assert "fsd_timeline" in payload


class TestSentryEvents:
    def test_returns_sentry_payload_with_folder_split(self, client: FlaskClient) -> None:
        response = client.get("/api/sentry-events")
        assert response.status_code == HTTPStatus.OK
        events = response.get_json()["events"]
        sentry_events = [event for event in events if event["event_type"] == "sentry"]
        assert sentry_events
        assert sentry_events[0]["source_folder"] == "SentryClips"
        assert sentry_events[0]["event_folder"] == "2024-06-01_18-00-00"

    def test_sentry_events_page_reports_has_next(self, client: FlaskClient) -> None:
        first = client.get("/api/sentry-events?limit=1&page=1").get_json()
        second = client.get("/api/sentry-events?limit=1&page=2").get_json()
        assert first["has_next"] is True
        assert first["next_page"] == 2
        assert len(first["events"]) == 1
        assert len(second["events"]) == 1
        assert second["events"][0]["id"] != first["events"][0]["id"]


# ---------------------------------------------------------------------------
# Waypoints-for-clip
# ---------------------------------------------------------------------------


class TestWaypointsForClip:
    def test_returns_trip_id_for_known_path(self, client: FlaskClient) -> None:
        response = client.get(
            "/api/waypoints-for-clip?"
            "path=RecentClips/2024-06-01_08-00-00/2024-06-01_08-00-00-front.mp4",
        )
        assert response.status_code == HTTPStatus.OK
        payload = response.get_json()
        assert payload["trip_id"]
        assert payload["waypoints"]

    def test_empty_path_returns_empty_payload(self, client: FlaskClient) -> None:
        response = client.get("/api/waypoints-for-clip?path=")
        assert response.status_code == HTTPStatus.OK
        payload = response.get_json()
        assert payload["waypoints"] == []
        assert "trip_id" not in payload


# ---------------------------------------------------------------------------
# Event details / clips on disk
# ---------------------------------------------------------------------------


class TestEventDetails:
    def test_returns_flat_clip_count(self, client: FlaskClient) -> None:
        response = client.get(
            "/api/event-details/RecentClips/2024-06-01_08-00-00",
        )
        assert response.status_code == HTTPStatus.OK
        payload = response.get_json()
        # Materialised three front.mp4 files under that folder.
        assert payload["clip_count"] >= 1

    def test_rejects_unsafe_folder(self, client: FlaskClient) -> None:
        response = client.get("/api/event-details/..%2Fescape/whatever")
        assert response.status_code in {HTTPStatus.BAD_REQUEST, HTTPStatus.NOT_FOUND}

    def test_missing_folder_returns_404(self, client: FlaskClient) -> None:
        response = client.get("/api/event-details/DoesNotExist/whatever")
        assert response.status_code == HTTPStatus.NOT_FOUND


class TestEventClips:
    def test_lists_sentry_event_clips(self, client: FlaskClient) -> None:
        response = client.get("/api/event-clips/SentryClips/2024-06-01_18-00-00")
        assert response.status_code == HTTPStatus.OK
        payload = response.get_json()
        assert payload["structure"] == "events"
        assert payload["front_clips"]

    def test_missing_event_returns_404_with_empty_clips(self, client: FlaskClient) -> None:
        response = client.get("/api/event-clips/RecentClips/2099-01-01")
        assert response.status_code == HTTPStatus.NOT_FOUND
        payload = response.get_json()
        assert payload["front_clips"] == []
