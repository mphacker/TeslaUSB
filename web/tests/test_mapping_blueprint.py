# ruff: noqa: ANN001, ANN201  # pytest injects fixtures dynamically in test signatures.
"""Tests for the mapping blueprint."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest
from teslausb_web.app import create_app
from teslausb_web.blueprints.mapping import (
    MappingFilesystemError,
    MappingRequestError,
    _coerce_limit,
    _coerce_max_points,
    _coerce_non_negative_float,
    _get_queries,
    _get_service,
    _invalidate_caches,
    _mapping_response,
    _normalize_video_path,
    _parse_bbox,
    _redirect_to_mapping,
    _require_iso_date,
    _safe_segment,
)
from teslausb_web.config import MappingSection, PathsSection, WebConfig, WebSection
from teslausb_web.services.mapping import (
    DiagnoseError,
    IndexerError,
    MappingService,
    MappingServiceError,
)
from teslausb_web.services.mapping_migrations import MigrationsConfig, MigrationsRunner
from teslausb_web.services.mapping_queries import MappingQueries, MappingQueryError

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class SeededMappingData:
    day_a: str
    day_b: str
    day_c: str
    recent_event: str
    archived_event: str
    saved_event: str
    sentry_event: str
    archived_fallback_event: str


class StubMappingService(MappingService):
    def __init__(self, db_path: Path) -> None:
        self._queries = cast("MappingQueries", object())
        self._db_path = db_path
        runner = MigrationsRunner(
            MigrationsConfig(
                db_path=db_path,
                backup_dir=db_path.parent / "mapping-backups",
            )
        )
        connection = runner.init_db()
        connection.close()
        self.boot_result: dict[str, int] = {"enqueued": 2, "already_indexed": 1}
        self.index_status: dict[str, object] = {
            "running": False,
            "queue_depth": 0,
            "files_done_session": 0,
            "active_file": None,
            "source": None,
            "last_drained_at": None,
            "last_error": None,
            "last_result": None,
        }
        self.diagnose_result: dict[str, object] = {"summary": "ok", "total_front_videos": 1}
        self.boot_error: Exception | None = None
        self.status_error: Exception | None = None
        self.diagnose_error: Exception | None = None
        self.boot_calls: list[str] = []
        self.diagnose_calls: list[int] = []

    @property
    def queries(self) -> MappingQueries | object:
        return self._queries

    def get_indexer_status(self) -> dict[str, object]:
        if self.status_error is not None:
            raise self.status_error
        return self.index_status

    def boot_catchup_scan(self, *, source: str = "manual") -> dict[str, int]:
        self.boot_calls.append(source)
        if self.boot_error is not None:
            raise self.boot_error
        return self.boot_result

    def diagnose_video(
        self,
        teslacam_path: str | Path | None = None,
        *,
        max_videos: int = 3,
    ) -> dict[str, object]:
        _ = teslacam_path
        self.diagnose_calls.append(max_videos)
        if self.diagnose_error is not None:
            raise self.diagnose_error
        return {**self.diagnose_result, "max_videos": max_videos}

    @contextmanager
    def open_db(self):
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()


@pytest.fixture
def app(tmp_path: Path):
    media_root = tmp_path / "TeslaCam"
    archive_root = media_root / "ArchivedClips"
    for folder in ("RecentClips", "SavedClips", "SentryClips", "ArchivedClips"):
        (media_root / folder).mkdir(parents=True, exist_ok=True)
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32, max_upload_mb=8, max_chunk_mb=1),
        paths=PathsSection(
            backing_root=media_root,
            state_dir=tmp_path / "state",
            cache_invalidate_script=tmp_path / "invalidate.sh",
        ),
        mapping=MappingSection(
            db_path=tmp_path / "state" / "mapping.db",
            backup_dir=tmp_path / "state" / "mapping-backups",
            media_root=media_root,
            archive_root=archive_root,
        ),
        source_path=None,
    )
    flask_app = create_app(cfg)
    flask_app.testing = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def service(app) -> MappingService:
    mapping_service = app.extensions["mapping_service"]
    assert isinstance(mapping_service, MappingService)
    return mapping_service


@pytest.fixture
def queries(service: MappingService) -> MappingQueries:
    return service.queries


@pytest.fixture
def invalidator(app):
    return app.extensions["cache_invalidator"]


@pytest.fixture
def seeded_data(service: MappingService) -> SeededMappingData:
    data = SeededMappingData(
        day_a="2026-01-02",
        day_b="2026-01-03",
        day_c="2026-01-04",
        recent_event="2026-01-02_10-00-00",
        archived_event="2026-01-03_09-00-00",
        saved_event="2026-01-05_00-00-00",
        sentry_event="2026-01-06_00-00-00",
        archived_fallback_event="2026-01-07_00-00-00",
    )
    _seed_mapping_database(service, data)
    return data


def _seed_mapping_database(service: MappingService, data: SeededMappingData) -> None:
    media_root = service.config.media_root
    recent_front = media_root / "RecentClips" / f"{data.recent_event}-front.mp4"
    recent_back = media_root / "RecentClips" / f"{data.recent_event}-back.mp4"
    archived_front = media_root / "ArchivedClips" / f"{data.archived_event}-front.mp4"
    archived_fallback = media_root / "ArchivedClips" / f"{data.archived_fallback_event}-front.mp4"
    saved_dir = media_root / "SavedClips" / data.saved_event
    sentry_dir = media_root / "SentryClips" / data.sentry_event
    saved_dir.mkdir(parents=True, exist_ok=True)
    sentry_dir.mkdir(parents=True, exist_ok=True)
    recent_front.write_bytes(b"front-video")
    recent_back.write_bytes(b"back-video")
    archived_front.write_bytes(b"archived-video")
    archived_fallback.write_bytes(b"fallback-video")
    (saved_dir / f"{data.saved_event}-front.mp4").write_bytes(b"saved-front")
    (saved_dir / f"{data.saved_event}-back.mp4").write_bytes(b"saved-back")
    (sentry_dir / f"{data.sentry_event}-front.mp4").write_bytes(b"sentry-front")
    (sentry_dir / f"{data.sentry_event}-left_repeater.mp4").write_bytes(b"sentry-left")
    with service.open_db() as connection:
        connection.executemany(
            """
            INSERT INTO trips (
                id, start_time, end_time, start_lat, start_lon, end_lat, end_lon,
                distance_km, duration_seconds, source_folder, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    1,
                    f"{data.day_a}T10:00:00",
                    f"{data.day_a}T10:02:20",
                    37.0,
                    -122.0,
                    37.0015,
                    -122.0015,
                    1.2,
                    140,
                    "RecentClips",
                    f"{data.day_a}T10:05:00",
                ),
                (
                    2,
                    f"{data.day_b}T09:00:00",
                    f"{data.day_b}T09:02:00",
                    38.0,
                    -123.0,
                    38.02,
                    -123.004,
                    10.0,
                    600,
                    "ArchivedClips",
                    f"{data.day_b}T09:10:00",
                ),
                (
                    3,
                    f"{data.day_a}T12:00:00",
                    f"{data.day_a}T12:01:00",
                    37.2,
                    -122.2,
                    37.2001,
                    -122.2001,
                    0.02,
                    60,
                    "RecentClips",
                    f"{data.day_a}T12:05:00",
                ),
            ),
        )
        connection.executemany(
            """
            INSERT INTO waypoints (
                id, trip_id, timestamp, lat, lon, heading, speed_mps, autopilot_state,
                video_path, frame_offset
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    10,
                    1,
                    f"{data.day_a}T10:00:00",
                    37.0,
                    -122.0,
                    0.0,
                    10.0,
                    "MANUAL",
                    recent_front.relative_to(media_root).as_posix(),
                    0,
                ),
                (
                    11,
                    1,
                    f"{data.day_a}T10:00:30",
                    37.0005,
                    -122.0005,
                    10.0,
                    12.0,
                    "AUTOSTEER",
                    recent_front.relative_to(media_root).as_posix(),
                    30,
                ),
                (
                    12,
                    1,
                    f"{data.day_a}T10:02:00",
                    37.001,
                    -122.001,
                    20.0,
                    13.0,
                    "AUTOSTEER",
                    recent_front.relative_to(media_root).as_posix(),
                    60,
                ),
                (
                    13,
                    1,
                    f"{data.day_a}T10:02:20",
                    37.0015,
                    -122.0015,
                    30.0,
                    14.0,
                    "SELF_DRIVING",
                    recent_front.relative_to(media_root).as_posix(),
                    80,
                ),
                (
                    20,
                    2,
                    f"{data.day_b}T09:00:00",
                    38.0,
                    -123.0,
                    0.0,
                    20.0,
                    "MANUAL",
                    archived_front.relative_to(media_root).as_posix(),
                    0,
                ),
                (
                    21,
                    2,
                    f"{data.day_b}T09:00:30",
                    38.005,
                    -123.001,
                    10.0,
                    21.0,
                    "MANUAL",
                    archived_front.relative_to(media_root).as_posix(),
                    30,
                ),
                (
                    22,
                    2,
                    f"{data.day_b}T09:01:00",
                    38.01,
                    -123.002,
                    20.0,
                    22.0,
                    "AUTOSTEER",
                    archived_front.relative_to(media_root).as_posix(),
                    60,
                ),
                (
                    23,
                    2,
                    f"{data.day_b}T09:01:30",
                    38.015,
                    -123.003,
                    30.0,
                    23.0,
                    "MANUAL",
                    archived_front.relative_to(media_root).as_posix(),
                    90,
                ),
                (
                    24,
                    2,
                    f"{data.day_b}T09:02:00",
                    38.02,
                    -123.004,
                    40.0,
                    24.0,
                    "MANUAL",
                    archived_front.relative_to(media_root).as_posix(),
                    120,
                ),
                (
                    30,
                    3,
                    f"{data.day_a}T12:00:00",
                    37.2,
                    -122.2,
                    0.0,
                    1.0,
                    "MANUAL",
                    f"RecentClips/{data.archived_fallback_event}-front.mp4",
                    0,
                ),
                (
                    31,
                    3,
                    f"{data.day_a}T12:00:20",
                    37.2001,
                    -122.2001,
                    5.0,
                    1.0,
                    "MANUAL",
                    f"RecentClips/{data.archived_fallback_event}-front.mp4",
                    20,
                ),
            ),
        )
        connection.executemany(
            """
            INSERT INTO waypoints_cold (
                id, acceleration_x, acceleration_y, acceleration_z, gear,
                steering_angle, brake_applied, blinker_on_left, blinker_on_right
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (10, 0.1, 0.2, 0.3, "DRIVE", 1.5, 0, 0, 1),
                (11, 0.4, 0.5, 0.6, "DRIVE", 2.5, 1, 1, 0),
            ),
        )
        connection.executemany(
            """
            INSERT INTO detected_events (
                id, trip_id, timestamp, lat, lon, event_type, severity, description,
                video_path, frame_offset, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    100,
                    1,
                    f"{data.day_a}T10:00:45",
                    37.0006,
                    -122.0006,
                    "hard_brake",
                    "warning",
                    "warning event",
                    recent_front.relative_to(media_root).as_posix(),
                    35,
                    None,
                ),
                (
                    101,
                    1,
                    f"{data.day_a}T10:01:00",
                    37.0007,
                    -122.0007,
                    "hard_accel",
                    "info",
                    "info event",
                    recent_front.relative_to(media_root).as_posix(),
                    40,
                    None,
                ),
                (
                    102,
                    2,
                    f"{data.day_b}T09:00:45",
                    38.0055,
                    -123.0011,
                    "sentry",
                    "info",
                    "sentry event",
                    archived_front.relative_to(media_root).as_posix(),
                    45,
                    None,
                ),
                (
                    103,
                    None,
                    f"{data.day_c}T08:00:00",
                    36.5,
                    -121.5,
                    "lane_departure",
                    "critical",
                    "critical event",
                    None,
                    None,
                    '{"source": "test"}',
                ),
                (
                    104,
                    1,
                    f"{data.day_a}T10:01:30",
                    37.0008,
                    -122.0008,
                    "saved",
                    "info",
                    "saved event",
                    f"SavedClips/{data.saved_event}/{data.saved_event}-front.mp4",
                    50,
                    None,
                ),
            ),
        )
        connection.executemany(
            """
            INSERT INTO indexed_files (
                file_path, file_size, file_mtime, indexed_at, waypoint_count, event_count
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                (str(recent_front), 100, 1.0, f"{data.day_a}T10:05:00", 4, 3),
                (str(archived_front), 100, 2.0, f"{data.day_b}T09:10:00", 5, 1),
                (
                    str(media_root / "RecentClips" / f"{data.archived_fallback_event}-front.mp4"),
                    100,
                    3.0,
                    f"{data.day_a}T12:05:00",
                    2,
                    0,
                ),
            ),
        )
        connection.executemany(
            """
            INSERT INTO indexing_queue (
                canonical_key, file_path, priority, enqueued_at, next_attempt_at,
                attempts, claimed_by, claimed_at, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    "queued-key",
                    "RecentClips/pending-front.mp4",
                    50,
                    1.0,
                    0.0,
                    0,
                    None,
                    None,
                    "manual",
                ),
                (
                    "claimed-key",
                    "RecentClips/claimed-front.mp4",
                    50,
                    2.0,
                    0.0,
                    0,
                    "worker-1",
                    2.5,
                    "manual",
                ),
            ),
        )
        connection.commit()


def _install_stub_service(app, stub: StubMappingService) -> MappingService:
    original = app.extensions["mapping_service"]
    app.extensions["mapping_service"] = stub
    return original


def test_app_registers_mapping_blueprint_and_service(app) -> None:
    assert "mapping" in app.blueprints
    assert isinstance(app.extensions["mapping_service"], MappingService)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ArchivedClips/clip.mp4", "ArchivedClips/clip.mp4"),
        ("cache/ArchivedClips/clip.mp4", "ArchivedClips/clip.mp4"),
        (None, None),
        ("RecentClips/clip.mp4", "RecentClips/clip.mp4"),
    ],
)
def test_helper_normalize_video_path(raw: str | None, expected: str | None) -> None:
    assert _normalize_video_path(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 50), (0, 50), (-1, 50), (3, 3), (500, 7)],
)
def test_helper_coerce_limit(raw: int | None, expected: int) -> None:
    cap = 7 if raw == 500 else None
    assert _coerce_limit(raw, default=50, cap=cap) == expected


@pytest.mark.parametrize(("raw", "expected"), [(None, 0.5), (-1.0, 0.5), (2.0, 2.0)])
def test_helper_coerce_non_negative_float(raw: float | None, expected: float) -> None:
    assert _coerce_non_negative_float(raw, default=0.5) == expected


@pytest.mark.parametrize(("raw", "expected"), [(None, 200), (1, 200), (10, 10), (5000, 1000)])
def test_helper_coerce_max_points(raw: int | None, expected: int) -> None:
    assert _coerce_max_points(raw) == expected


@pytest.mark.parametrize("raw", ["2026-01-02", "1999-12-31"])
def test_helper_require_iso_date_accepts_valid_dates(raw: str) -> None:
    assert _require_iso_date(raw) == raw


@pytest.mark.parametrize("raw", ["2026-1-2", "../2026-01-02", ""])
def test_helper_require_iso_date_rejects_invalid_dates(raw: str) -> None:
    with pytest.raises(MappingRequestError, match="YYYY-MM-DD"):
        _require_iso_date(raw)


@pytest.mark.parametrize("raw", ["RecentClips", "event-name", "file.mp4"])
def test_helper_safe_segment_accepts_simple_values(raw: str) -> None:
    assert _safe_segment(raw, field_name="segment") == raw


@pytest.mark.parametrize("raw", ["../RecentClips", "SavedClips/test", "", ".."])
def test_helper_safe_segment_rejects_path_tokens(raw: str) -> None:
    with pytest.raises(MappingRequestError):
        _safe_segment(raw, field_name="segment")


def test_helper_invalidate_caches_is_noop_without_extension(app) -> None:
    invalidator = app.extensions.pop("cache_invalidator")
    _invalidate_caches(app)
    app.extensions["cache_invalidator"] = invalidator


def test_helper_get_service_rejects_misconfigured_extension(app) -> None:
    with app.app_context():
        original = app.extensions["mapping_service"]
        app.extensions["mapping_service"] = object()
        with pytest.raises(RuntimeError, match="mapping_service"):
            _get_service()
        app.extensions["mapping_service"] = original


def test_helper_get_queries_rejects_misconfigured_queries(app, tmp_path: Path) -> None:
    stub = StubMappingService(tmp_path / "state" / "broken.db")
    original = _install_stub_service(app, stub)
    with app.app_context(), pytest.raises(RuntimeError, match="queries"):
        _get_queries()
    app.extensions["mapping_service"] = original


def test_helper_redirect_and_mapping_response_redirect_for_non_xhr(app) -> None:
    with app.test_request_context("/mapping/api/index/cancel?_=9", method="POST"):
        assert _redirect_to_mapping().headers["Location"] == "/mapping/"
        assert _redirect_to_mapping(cache_bust="9").headers["Location"] == "/mapping/?_=9"
        response = _mapping_response(
            success=False,
            message="boom",
            status=HTTPStatus.BAD_REQUEST,
        )
    assert response.status_code == HTTPStatus.FOUND
    assert response.headers["Location"] == "/mapping/?_=9"


def test_helper_parse_bbox_from_request_context(app) -> None:
    with app.test_request_context("/mapping/api/trips?min_lat=1&min_lon=2&max_lat=3&max_lon=4"):
        assert _parse_bbox() == (1.0, 2.0, 3.0, 4.0)
    with app.test_request_context("/mapping/api/trips?min_lat=x&min_lon=2&max_lat=3&max_lon=4"):
        assert _parse_bbox() is None


def test_map_view_renders_mapping_template(client) -> None:
    response = client.get("/mapping/")
    html = response.get_data(as_text=True)
    assert response.status_code == HTTPStatus.OK
    assert '<h1 class="mapping-title">Mapping</h1>' in html
    assert "Edit Mode" not in html
    assert "Present Mode" not in html
    assert "quick_edit" not in html
    assert "cdn." not in html
    assert "unpkg" not in html
    assert "jsdelivr" not in html
    assert "lucide-sprite.svg" in html
    assert "vendor/leaflet/leaflet.css" in html
    assert "js/mapping.js" in html


def test_api_trips_returns_seeded_rows(client, seeded_data: SeededMappingData) -> None:
    response = client.get("/mapping/api/trips")
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert [trip["id"] for trip in payload["trips"]] == [2, 1]
    assert payload["trips"][0]["source_folder"] == "ArchivedClips"


def test_api_trips_honors_bbox_and_date_filters(client, seeded_data: SeededMappingData) -> None:
    response = client.get(
        "/mapping/api/trips?min_lat=36.5&min_lon=-122.5&max_lat=37.5&max_lon=-121.5"
        f"&date_from={seeded_data.day_a}T00:00:00&date_to={seeded_data.day_a}T23:59:59&min_distance=0"
    )
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert [trip["id"] for trip in payload["trips"]] == [3, 1]


def test_api_trips_translates_query_error(client, queries: MappingQueries) -> None:
    with patch.object(queries, "query_trips", side_effect=MappingQueryError("boom")):
        response = client.get("/mapping/api/trips")
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "boom"


def test_api_trip_route_returns_geojson(client, seeded_data: SeededMappingData) -> None:
    response = client.get("/mapping/api/trip/2/route")
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert payload["geometry"]["type"] == "LineString"
    assert (
        payload["properties"]["waypoints"][0]["video_path"]
        == f"ArchivedClips/{seeded_data.archived_event}-front.mp4"
    )


def test_api_trip_route_returns_not_found_for_missing_trip(client) -> None:
    response = client.get("/mapping/api/trip/999/route")
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.get_json()["error"] == "Trip not found"


def test_api_trip_telemetry_returns_cold_columns(client, seeded_data: SeededMappingData) -> None:
    response = client.get("/mapping/api/trip/1/telemetry")
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert payload["telemetry"]["10"]["gear"] == "DRIVE"
    assert payload["telemetry"]["11"]["brake_applied"] == 1
    assert payload["telemetry"]["12"]["acceleration_x"] is None


def test_api_trip_telemetry_returns_empty_payload_for_missing_trip(client) -> None:
    response = client.get("/mapping/api/trip/404/telemetry")
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["telemetry"] == {}


def test_api_waypoints_for_clip_returns_trip_waypoints(
    client, seeded_data: SeededMappingData
) -> None:
    response = client.get(
        f"/mapping/api/waypoints-for-clip?path=RecentClips/{seeded_data.recent_event}-front.mp4"
    )
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert payload["trip_id"] == 1
    assert len(payload["waypoints"]) == 4


def test_api_waypoints_for_clip_falls_back_to_matching_base_name(
    client, seeded_data: SeededMappingData
) -> None:
    response = client.get(
        f"/mapping/api/waypoints-for-clip?path=RecentClips/{seeded_data.recent_event}-back.mp4"
    )
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert payload["trip_id"] == 1


def test_api_waypoints_for_clip_returns_empty_for_missing_path(client) -> None:
    response = client.get("/mapping/api/waypoints-for-clip?path=")
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["waypoints"] == []


def test_api_events_returns_filtered_payload(client, seeded_data: SeededMappingData) -> None:
    response = client.get(f"/mapping/api/events?date={seeded_data.day_b}")
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert len(payload["events"]) == 1
    assert (
        payload["events"][0]["video_path"]
        == f"ArchivedClips/{seeded_data.archived_event}-front.mp4"
    )


def test_api_events_rejects_malformed_date(client) -> None:
    response = client.get("/mapping/api/events?date=2026-1-2")
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "date must be YYYY-MM-DD"


def test_api_events_caps_large_limit_for_overview(
    client, queries: MappingQueries, seeded_data: SeededMappingData
) -> None:
    with patch.object(queries, "query_events", wraps=queries.query_events) as wrapped:
        response = client.get(
            f"/mapping/api/events?date={seeded_data.day_a}&overview=1&limit=99999"
        )
    assert response.status_code == HTTPStatus.OK
    assert wrapped.call_args.kwargs["limit"] == 5000


def test_api_days_returns_recent_days(client, seeded_data: SeededMappingData) -> None:
    response = client.get("/mapping/api/days")
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert [day["date"] for day in payload["days"]] == [
        seeded_data.day_c,
        seeded_data.day_b,
        seeded_data.day_a,
    ]


def test_api_days_caps_limit_and_min_distance(client, queries: MappingQueries) -> None:
    with patch.object(queries, "query_days", wraps=queries.query_days) as wrapped:
        response = client.get("/mapping/api/days?limit=999&min_distance=-1")
    assert response.status_code == HTTPStatus.OK
    assert wrapped.call_args.kwargs["limit"] == 365
    assert wrapped.call_args.kwargs["min_distance_km"] == 0.05


def test_api_day_routes_returns_expanded_trips(client, seeded_data: SeededMappingData) -> None:
    response = client.get(f"/mapping/api/day/{seeded_data.day_a}/routes")
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert payload["date"] == seeded_data.day_a
    assert [trip["trip_id"] for trip in payload["trips"]] == [1]


def test_api_day_routes_rejects_bad_date(client) -> None:
    response = client.get("/mapping/api/day/not-a-date/routes")
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "date must be YYYY-MM-DD"


def test_api_trips_playable_returns_trip_map(client, seeded_data: SeededMappingData) -> None:
    response = client.get(f"/mapping/api/trips/playable?date={seeded_data.day_a}")
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert payload["trips"]["1"] is True
    assert payload["trips"]["3"] is True


def test_api_trips_playable_rejects_missing_date(client) -> None:
    response = client.get("/mapping/api/trips/playable")
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["error"] == "date must be YYYY-MM-DD"


def test_api_all_routes_returns_simplified_routes(client, seeded_data: SeededMappingData) -> None:
    response = client.get("/mapping/api/all-routes")
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert [trip["trip_id"] for trip in payload["trips"]] == [2, 1]


def test_api_all_routes_caps_max_points_and_does_not_schedule_cache(
    client, invalidator, queries: MappingQueries
) -> None:
    with (
        patch.object(invalidator, "schedule") as schedule_mock,
        patch.object(
            queries, "query_all_routes_simplified", wraps=queries.query_all_routes_simplified
        ) as wrapped,
    ):
        response = client.get("/mapping/api/all-routes?max_points=9999")
    assert response.status_code == HTTPStatus.OK
    assert wrapped.call_args.kwargs["max_points_per_trip"] == 1000
    schedule_mock.assert_not_called()


def test_api_stats_returns_service_summary(client, seeded_data: SeededMappingData) -> None:
    response = client.get("/mapping/api/stats")
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert payload["trip_count"] == 3
    assert payload["event_count"] == 5
    assert payload["indexer_status"] is not None


def test_api_index_status_returns_service_state(client, seeded_data: SeededMappingData) -> None:
    response = client.get("/mapping/api/index/status")
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert payload["queue_depth"] == 1


def test_api_index_trigger_uses_stub_service_and_schedules_cache(
    app, client, invalidator, tmp_path: Path
) -> None:
    stub = StubMappingService(tmp_path / "state" / "trigger.db")
    original = _install_stub_service(app, stub)
    try:
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/mapping/api/index/trigger", headers={"X-Requested-With": "XMLHttpRequest"}
            )
        payload = response.get_json()
        assert response.status_code == HTTPStatus.OK
        assert payload["summary"] == {"enqueued": 2, "already_indexed": 1}
        assert stub.boot_calls == ["manual"]
        schedule_mock.assert_called_once()
    finally:
        app.extensions["mapping_service"] = original


def test_api_index_trigger_returns_service_unavailable_when_media_root_missing(client) -> None:
    with patch(
        "teslausb_web.blueprints.mapping._require_mapping_media_root",
        side_effect=MappingFilesystemError("TeslaCam not accessible"),
    ):
        response = client.post(
            "/mapping/api/index/trigger", headers={"X-Requested-With": "XMLHttpRequest"}
        )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.get_json()["error"] == "TeslaCam not accessible"


def test_api_index_trigger_translates_indexer_error(app, client, tmp_path: Path) -> None:
    stub = StubMappingService(tmp_path / "state" / "trigger-error.db")
    stub.boot_error = IndexerError("boom")
    original = _install_stub_service(app, stub)
    try:
        response = client.post(
            "/mapping/api/index/trigger", headers={"X-Requested-With": "XMLHttpRequest"}
        )
    finally:
        app.extensions["mapping_service"] = original
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "boom"


def test_api_index_rebuild_requires_confirmation(client) -> None:
    response = client.post(
        "/mapping/api/index/rebuild", json={}, headers={"X-Requested-With": "XMLHttpRequest"}
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.get_json()["message"] == "Confirmation required (set confirm=true)."


def test_api_index_rebuild_clears_rows_and_schedules_cache(
    app, client, invalidator, tmp_path: Path
) -> None:
    stub = StubMappingService(tmp_path / "state" / "rebuild.db")
    original = _install_stub_service(app, stub)
    try:
        queue_sql = (
            "INSERT INTO indexing_queue (canonical_key, file_path, priority, enqueued_at, "
            "next_attempt_at, attempts, claimed_by, claimed_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        indexed_sql = (
            "INSERT INTO indexed_files (file_path, file_size, file_mtime, indexed_at, "
            "waypoint_count, event_count) VALUES (?, ?, ?, ?, ?, ?)"
        )
        with stub.open_db() as connection:
            connection.execute(
                queue_sql,
                ("queued", "RecentClips/x.mp4", 50, 1.0, 0.0, 0, None, None, "manual"),
            )
            connection.execute(
                indexed_sql, ("RecentClips/x.mp4", 1, 1.0, "2026-01-01T00:00:00", 0, 0)
            )
            connection.commit()
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/mapping/api/index/rebuild",
                json={"confirm": True},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        with stub.open_db() as connection:
            queue_count = connection.execute("SELECT COUNT(*) FROM indexing_queue").fetchone()[0]
            file_count = connection.execute("SELECT COUNT(*) FROM indexed_files").fetchone()[0]
        assert response.status_code == HTTPStatus.OK
        assert queue_count == 0
        assert file_count == 0
        schedule_mock.assert_called_once()
    finally:
        app.extensions["mapping_service"] = original


def test_api_index_rebuild_translates_service_error(app, client, tmp_path: Path) -> None:
    stub = StubMappingService(tmp_path / "state" / "rebuild-error.db")
    stub.boot_error = MappingServiceError("boom")
    original = _install_stub_service(app, stub)
    try:
        response = client.post(
            "/mapping/api/index/rebuild",
            json={"confirm": True},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
    finally:
        app.extensions["mapping_service"] = original
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "boom"


def test_api_index_cancel_clears_only_pending_rows(
    app, client, invalidator, tmp_path: Path
) -> None:
    stub = StubMappingService(tmp_path / "state" / "cancel.db")
    original = _install_stub_service(app, stub)
    try:
        queue_sql = (
            "INSERT INTO indexing_queue (canonical_key, file_path, priority, enqueued_at, "
            "next_attempt_at, attempts, claimed_by, claimed_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        with stub.open_db() as connection:
            connection.executemany(
                queue_sql,
                (
                    ("queued", "RecentClips/a.mp4", 50, 1.0, 0.0, 0, None, None, "manual"),
                    ("claimed", "RecentClips/b.mp4", 50, 2.0, 0.0, 0, "worker", 2.5, "manual"),
                ),
            )
            connection.commit()
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/mapping/api/index/cancel", headers={"X-Requested-With": "XMLHttpRequest"}
            )
        with stub.open_db() as connection:
            rows = connection.execute(
                "SELECT canonical_key, claimed_by FROM indexing_queue ORDER BY canonical_key"
            ).fetchall()
        assert response.status_code == HTTPStatus.OK
        assert response.get_json()["message"] == "Cleared 1 queued item(s)."
        assert [(row[0], row[1]) for row in rows] == [("claimed", "worker")]
        schedule_mock.assert_called_once()
    finally:
        app.extensions["mapping_service"] = original


def test_api_index_cancel_translates_database_error(app, client, tmp_path: Path) -> None:
    stub = StubMappingService(tmp_path / "state" / "cancel-error.db")
    original = _install_stub_service(app, stub)
    try:
        with patch.object(stub, "open_db", side_effect=sqlite3.Error("boom")):
            response = client.post(
                "/mapping/api/index/cancel", headers={"X-Requested-With": "XMLHttpRequest"}
            )
    finally:
        app.extensions["mapping_service"] = original
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"].startswith("Cancel failed")


def test_api_index_diagnose_uses_stub_service(app, client, tmp_path: Path) -> None:
    stub = StubMappingService(tmp_path / "state" / "diagnose.db")
    original = _install_stub_service(app, stub)
    try:
        response = client.get("/mapping/api/index/diagnose?max=99")
    finally:
        app.extensions["mapping_service"] = original
    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["max_videos"] == 10
    assert stub.diagnose_calls == [10]


def test_api_index_diagnose_translates_diagnose_error(app, client, tmp_path: Path) -> None:
    stub = StubMappingService(tmp_path / "state" / "diagnose-error.db")
    stub.diagnose_error = DiagnoseError("boom")
    original = _install_stub_service(app, stub)
    try:
        response = client.get("/mapping/api/index/diagnose")
    finally:
        app.extensions["mapping_service"] = original
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "boom"


def test_api_driving_stats_returns_summary(client, seeded_data: SeededMappingData) -> None:
    response = client.get("/mapping/api/driving-stats")
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert payload["has_data"] is True
    assert payload["trip_count"] == 3


def test_api_event_charts_returns_chart_payload(client, seeded_data: SeededMappingData) -> None:
    response = client.get("/mapping/api/event-charts")
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert any(item["label"] == "Sentry" for item in payload["by_type"])


def test_api_sentry_events_enriches_source_folder_and_event_folder(
    client, seeded_data: SeededMappingData
) -> None:
    response = client.get("/mapping/api/sentry-events")
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert payload["events"][0]["timestamp"] >= payload["events"][-1]["timestamp"]
    saved = next(event for event in payload["events"] if event["event_type"] == "saved")
    archived = next(event for event in payload["events"] if event["event_type"] == "sentry")
    assert saved["event_folder"] == seeded_data.saved_event
    assert archived["source_folder"] == "ArchivedClips"


def test_api_sentry_events_translates_query_error(client, queries: MappingQueries) -> None:
    with patch.object(queries, "query_events", side_effect=MappingQueryError("boom")):
        response = client.get("/mapping/api/sentry-events")
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.get_json()["error"] == "boom"


def test_api_event_details_returns_counts_for_event_folder(
    client, seeded_data: SeededMappingData
) -> None:
    response = client.get(f"/mapping/api/event-details/SavedClips/{seeded_data.saved_event}")
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert payload["clip_count"] == 2
    assert payload["camera_count"] == 2
    assert payload["size_mb"] > 0


def test_api_event_details_returns_counts_for_flat_archive(
    client, seeded_data: SeededMappingData
) -> None:
    response = client.get(f"/mapping/api/event-details/ArchivedClips/{seeded_data.archived_event}")
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert payload["clip_count"] == 1
    assert payload["camera_count"] == 1
    assert payload["size_mb"] > 0


def test_api_event_details_returns_not_found_for_missing_folder(client) -> None:
    response = client.get("/mapping/api/event-details/DoesNotExist/test")
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.get_json()["error"] == "Folder not found: DoesNotExist"


def test_api_event_clips_returns_event_folder_listing(
    client, seeded_data: SeededMappingData
) -> None:
    response = client.get(f"/mapping/api/event-clips/SavedClips/{seeded_data.saved_event}")
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert payload["structure"] == "events"
    assert payload["front_clips"] == [
        f"SavedClips/{seeded_data.saved_event}/{seeded_data.saved_event}-front.mp4"
    ]


def test_api_event_clips_falls_back_to_archived_flat_clip(
    client, seeded_data: SeededMappingData
) -> None:
    response = client.get(
        f"/mapping/api/event-clips/RecentClips/{seeded_data.archived_fallback_event}"
    )
    payload = response.get_json()
    assert response.status_code == HTTPStatus.OK
    assert payload["folder"] == "ArchivedClips"
    assert payload["front_clips"] == [
        f"ArchivedClips/{seeded_data.archived_fallback_event}-front.mp4"
    ]


def test_api_event_clips_returns_404_payload_for_missing_clip(client) -> None:
    response = client.get("/mapping/api/event-clips/RecentClips/missing-event")
    payload = response.get_json()
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert payload["front_clips"] == []
    assert payload["folder"] == "RecentClips"


def test_api_event_clips_rejects_invalid_segment(client) -> None:
    response = client.get("/mapping/api/event-clips/%2E%2E/test")
    assert response.status_code == HTTPStatus.BAD_REQUEST


def test_get_routes_do_not_schedule_cache_invalidation(
    client, invalidator, seeded_data: SeededMappingData
) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.get(f"/mapping/api/day/{seeded_data.day_a}/routes")
    assert response.status_code == HTTPStatus.OK
    schedule_mock.assert_not_called()
