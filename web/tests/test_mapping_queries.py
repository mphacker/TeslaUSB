from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from teslausb_web.config import MappingSection, PathsSection, WebConfig
from teslausb_web.services.mapping_migrations import MappingDatabaseError
from teslausb_web.services.mapping_queries import (
    AllRoutesTrip,
    DayRouteTrip,
    DrivingStats,
    EventChartData,
    MappingQueries,
    MappingQueriesConfig,
    MappingQueryError,
    PlayableTrip,
    Stats,
    _coerce_epsilon,
    _coerce_limit,
    _coerce_max_points,
    _coerce_min_distance,
    _coerce_offset,
    _is_gap_between,
    _normalized_relative_parts,
    _parse_iso_seconds,
    _simplify_polyline_rdp,
    make_mapping_queries,
)


@dataclass(frozen=True, slots=True)
class SampleDays:
    day_a: str
    day_b: str
    day_c: str


@dataclass(frozen=True, slots=True)
class SampleFixture:
    service: MappingQueries
    config: MappingQueriesConfig
    days: SampleDays


@dataclass(frozen=True, slots=True)
class GapCase:
    prev_ts: str | None
    prev_lat: float | None
    prev_lon: float | None
    curr_ts: str | None
    curr_lat: float | None
    curr_lon: float | None
    expected_gap: bool


@pytest.fixture
def sample_fixture(tmp_path: Path) -> SampleFixture:
    config = MappingQueriesConfig(
        db_path=tmp_path / "state" / "mapping.db",
        backup_dir=tmp_path / "state" / "mapping-backups",
        media_root=tmp_path / "media",
    )
    service = MappingQueries(config=config)
    days = _seed_sample_database(service, config.media_root)
    return SampleFixture(service=service, config=config, days=days)


@pytest.fixture
def empty_service(tmp_path: Path) -> MappingQueries:
    config = MappingQueriesConfig(
        db_path=tmp_path / "state" / "mapping.db",
        backup_dir=tmp_path / "state" / "mapping-backups",
        media_root=tmp_path / "media",
    )
    return MappingQueries(config=config)


@pytest.mark.parametrize(
    ("raw", "expected_state"),
    [
        ("2026-01-02T03:04:05", "value"),
        ("2026-01-02T03:04:05Z", "value"),
        ("not-a-timestamp", "none"),
        (None, "none"),
    ],
)
def test_parse_iso_seconds(raw: str | None, expected_state: str) -> None:
    parsed = _parse_iso_seconds(raw)

    assert (parsed is None) is (expected_state == "none")


@pytest.mark.parametrize(
    "case",
    [
        GapCase(
            "2026-01-01T00:00:00",
            None,
            None,
            "2026-01-01T00:02:00",
            None,
            None,
            expected_gap=True,
        ),
        GapCase(
            "2026-01-01T00:00:00",
            37.0,
            -122.0,
            "2026-01-01T00:00:30",
            37.0,
            -122.005,
            expected_gap=True,
        ),
        GapCase(
            "2026-01-01T00:00:00",
            37.0,
            -122.0,
            "2026-01-01T00:00:30",
            37.0001,
            -122.0001,
            expected_gap=False,
        ),
        GapCase("bad", None, None, "also-bad", None, None, expected_gap=False),
    ],
)
def test_is_gap_between(case: GapCase) -> None:
    result = _is_gap_between(
        case.prev_ts,
        case.prev_lat,
        case.prev_lon,
        case.curr_ts,
        case.curr_lat,
        case.curr_lon,
    )

    assert result is case.expected_gap


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 50),
        (0, 50),
        (-1, 50),
        (3, 3),
    ],
)
def test_coerce_limit(value: int | None, expected: int) -> None:
    assert _coerce_limit(value, default=50) == expected


@pytest.mark.parametrize(("value", "expected"), [(None, 0), (-1, 0), (2, 2)])
def test_coerce_offset(value: int | None, expected: int) -> None:
    assert _coerce_offset(value) == expected


@pytest.mark.parametrize(("value", "expected"), [(None, 0.0), (-1.0, 0.0), (1.5, 1.5)])
def test_coerce_min_distance(value: float | None, expected: float) -> None:
    assert _coerce_min_distance(value) == expected


@pytest.mark.parametrize(("value", "expected"), [(None, 0.0), (-1.0, 0.0), (4.0, 4.0)])
def test_coerce_epsilon(value: float | None, expected: float) -> None:
    assert _coerce_epsilon(value) == expected


@pytest.mark.parametrize(("value", "expected"), [(None, 2), (1, 2), (10, 10)])
def test_coerce_max_points(value: int | None, expected: int) -> None:
    assert _coerce_max_points(value) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("RecentClips/clip.mp4", ("RecentClips", "clip.mp4")),
        ("\\RecentClips\\clip.mp4", ("RecentClips", "clip.mp4")),
        ("../clip.mp4", ()),
        ("", ()),
    ],
)
def test_normalized_relative_parts(raw: str, expected: tuple[str, ...]) -> None:
    assert _normalized_relative_parts(raw) == expected


def test_simplify_polyline_rdp_keeps_endpoints_and_corner() -> None:
    indices = _simplify_polyline_rdp(
        [(0.0, 0.0), (0.0, 0.001), (0.001, 0.001), (0.002, 0.001)],
        8.0,
    )

    assert indices[0] == 0
    assert indices[-1] == 3
    assert 1 in indices or 2 in indices


def test_make_mapping_queries_from_web_config() -> None:
    cfg = WebConfig(
        paths=PathsSection(
            backing_root=Path("/srv/teslausb"),
            state_dir=Path("/var/lib/teslausb"),
            db_path=Path("/var/lib/teslausb/index.sqlite3"),
            ipc_socket=Path("/run/teslausb/worker.sock"),
            cache_invalidate_script=Path("/usr/local/bin/cache.sh"),
        ),
        mapping=MappingSection(
            db_path=Path("/var/lib/teslausb/mapping.db"),
            backup_dir=Path("/var/lib/teslausb/mapping-backups"),
            backup_retention=5,
        ),
    )

    service = make_mapping_queries(cfg)

    assert isinstance(service, MappingQueries)
    assert service._config.media_root == Path("/srv/teslausb")
    assert service._config.backup_retention == 5


def test_make_mapping_queries_from_explicit_config(sample_fixture: SampleFixture) -> None:
    service = make_mapping_queries(sample_fixture.config)

    assert isinstance(service, MappingQueries)
    assert service._config.db_path == sample_fixture.config.db_path


def test_get_db_connection_wraps_database_errors(
    empty_service: MappingQueries,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_init_db(self: object) -> object:
        raise MappingDatabaseError("boom")

    monkeypatch.setattr(type(empty_service._migrations_runner), "init_db", broken_init_db)

    with pytest.raises(MappingQueryError, match="Failed to open mapping database"):
        empty_service.get_db_connection()


def test_query_trips_excludes_short_trips_by_default(sample_fixture: SampleFixture) -> None:
    trips = sample_fixture.service.query_trips()

    assert [trip.id for trip in trips] == [4, 3, 1]
    assert trips[0].event_count == 0
    assert trips[1].video_count == 1
    assert trips[2].event_count == 2


def test_query_trips_filters_bbox_and_dates(sample_fixture: SampleFixture) -> None:
    trips = sample_fixture.service.query_trips(
        bbox=(36.5, -122.5, 37.5, -121.5),
        date_from=f"{sample_fixture.days.day_a}T00:00:00",
        date_to=f"{sample_fixture.days.day_a}T23:59:59",
        min_distance_km=0.0,
    )

    assert [trip.id for trip in trips] == [2, 1]


def test_query_trips_applies_limit_and_offset(sample_fixture: SampleFixture) -> None:
    trips = sample_fixture.service.query_trips(limit=1, offset=1)

    assert [trip.id for trip in trips] == [3]


def test_query_trip_route_orders_waypoints_and_marks_gap(sample_fixture: SampleFixture) -> None:
    route = sample_fixture.service.query_trip_route(1)

    assert [waypoint.id for waypoint in route] == [10, 11, 12, 13]
    assert route[1].gap_after is True
    assert route[-1].gap_after is False


def test_query_events_filters_by_fields_and_orders_desc(sample_fixture: SampleFixture) -> None:
    events = sample_fixture.service.query_events(
        severity="warning",
        event_type="hard_brake",
        bbox=(36.5, -122.5, 37.5, -121.5),
        date=sample_fixture.days.day_a,
    )

    assert [(event.id, event.event_type) for event in events] == [(100, "hard_brake")]


def test_query_events_supports_pagination(sample_fixture: SampleFixture) -> None:
    events = sample_fixture.service.query_events(limit=2, offset=1)

    assert [event.id for event in events] == [102, 101]


def test_query_days_combines_trip_and_event_only_days(sample_fixture: SampleFixture) -> None:
    days = sample_fixture.service.query_days()

    assert [day.date for day in days] == [
        sample_fixture.days.day_c,
        sample_fixture.days.day_b,
        sample_fixture.days.day_a,
    ]
    assert days[0].trip_count == 0
    assert days[0].event_count == 1
    assert days[1].trip_count == 2
    assert days[1].event_count == 1


def test_query_days_honors_min_distance_and_limit(sample_fixture: SampleFixture) -> None:
    days = sample_fixture.service.query_days(limit=1, min_distance_km=1.0)

    assert len(days) == 1
    assert days[0].date == sample_fixture.days.day_c


def test_query_day_routes_groups_waypoints_and_excludes_empty_trip(
    sample_fixture: SampleFixture,
) -> None:
    trips = sample_fixture.service.query_day_routes(sample_fixture.days.day_b)

    assert [trip.id for trip in trips] == [3]
    assert isinstance(trips[0], DayRouteTrip)
    assert len(trips[0].waypoints) == 5


def test_query_day_routes_can_include_short_trip(sample_fixture: SampleFixture) -> None:
    trips = sample_fixture.service.query_day_routes(sample_fixture.days.day_a, min_distance_km=0.0)

    assert [trip.id for trip in trips] == [2, 1]
    assert trips[0].waypoints[0].video_path == "RecentClips/missing.mp4"


def test_query_all_routes_simplified_keeps_gap_boundaries(sample_fixture: SampleFixture) -> None:
    trips = sample_fixture.service.query_all_routes_simplified(min_distance_km=0.0, epsilon_m=0.0)
    trip_one = next(trip for trip in trips if trip.id == 1)

    assert isinstance(trip_one, AllRoutesTrip)
    assert len(trip_one.waypoints) == 4
    assert trip_one.waypoints[1].gap_after is True


def test_query_all_routes_simplified_caps_points_and_keeps_last_point(
    sample_fixture: SampleFixture,
) -> None:
    trips = sample_fixture.service.query_all_routes_simplified(
        min_distance_km=0.0,
        epsilon_m=0.0,
        max_points_per_trip=3,
    )
    trip_three = next(trip for trip in trips if trip.id == 3)

    assert len(trip_three.waypoints) == 3
    assert trip_three.waypoints[-1].lat == pytest.approx(38.0200)


def test_playable_trips_for_date_reports_playable_and_unplayable(
    sample_fixture: SampleFixture,
) -> None:
    playable = sample_fixture.service.playable_trips_for_date(sample_fixture.days.day_a)

    assert playable == (
        PlayableTrip(id=1, is_playable=True),
        PlayableTrip(id=2, is_playable=False),
    )


def test_playable_trips_for_date_uses_instance_cache(
    sample_fixture: SampleFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def tracking_resolver(path: str) -> bool:
        calls.append(path)
        return path == "RecentClips/drive1.mp4"

    monkeypatch.setattr(sample_fixture.service, "_resolve_video_path_on_disk", tracking_resolver)

    first = sample_fixture.service.playable_trips_for_date(sample_fixture.days.day_a)
    second = sample_fixture.service.playable_trips_for_date(sample_fixture.days.day_a)

    assert first == second
    assert calls == ["RecentClips/drive1.mp4", "RecentClips/missing.mp4"]


def test_playable_trips_for_date_cache_expires(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = MappingQueriesConfig(
        db_path=tmp_path / "state" / "mapping.db",
        backup_dir=tmp_path / "state" / "mapping-backups",
        media_root=tmp_path / "media",
        playable_trips_ttl_seconds=1.0,
    )
    service = MappingQueries(config=config)
    days = _seed_sample_database(service, config.media_root)
    calls: list[str] = []
    clock = iter((100.0, 100.5, 101.5, 101.5))

    def tracking_resolver(path: str) -> bool:
        calls.append(path)
        return path == "RecentClips/drive1.mp4"

    monkeypatch.setattr("teslausb_web.services.mapping_queries.time.monotonic", lambda: next(clock))
    monkeypatch.setattr(service, "_resolve_video_path_on_disk", tracking_resolver)

    service.playable_trips_for_date(days.day_a)
    service.playable_trips_for_date(days.day_a)
    service.playable_trips_for_date(days.day_a)

    assert calls == [
        "RecentClips/drive1.mp4",
        "RecentClips/missing.mp4",
        "RecentClips/drive1.mp4",
        "RecentClips/missing.mp4",
    ]


def test_resolve_video_path_on_disk_supports_archive_fallback(
    sample_fixture: SampleFixture,
) -> None:
    archived_copy = sample_fixture.config.media_root / "ArchivedClips" / "fallback-only.mp4"
    archived_copy.write_text("video", encoding="utf-8")

    assert (
        sample_fixture.service._resolve_video_path_on_disk("RecentClips/fallback-only.mp4") is True
    )


def test_resolve_video_path_on_disk_rejects_parent_segments(sample_fixture: SampleFixture) -> None:
    assert sample_fixture.service._resolve_video_path_on_disk("../escape.mp4") is False


def test_get_stats_returns_summary_and_stubbed_indexer_status(
    sample_fixture: SampleFixture,
) -> None:
    stats = sample_fixture.service.get_stats()

    assert isinstance(stats, Stats)
    assert stats.trip_count == 4
    assert stats.waypoint_count == 11
    assert stats.event_count == 4
    assert stats.indexed_file_count == 3
    assert stats.mapped_file_count == 2
    assert stats.indexer_status is None
    assert stats.event_breakdown[0].event_type == "hard_accel"


def test_get_driving_stats_handles_empty_database(empty_service: MappingQueries) -> None:
    stats = empty_service.get_driving_stats()

    assert stats == DrivingStats(has_data=False)


def test_get_driving_stats_computes_metrics(sample_fixture: SampleFixture) -> None:
    stats = sample_fixture.service.get_driving_stats()

    assert isinstance(stats, DrivingStats)
    assert stats.trip_count == 4
    assert stats.total_distance_km == pytest.approx(11.7)
    assert stats.total_duration_hours == pytest.approx(0.3)
    assert stats.max_speed_mph == pytest.approx(53.7)
    assert stats.fsd_usage_pct == pytest.approx(36.4)
    assert stats.warning_events == 2
    assert stats.events_per_100km == pytest.approx(17.1)


def test_get_event_chart_data_formats_chart_payload(sample_fixture: SampleFixture) -> None:
    charts = sample_fixture.service.get_event_chart_data()

    assert isinstance(charts, EventChartData)
    assert [point.label for point in charts.by_type] == [
        "Hard Accel",
        "Hard Brake",
        "Lane Departure",
        "Sentry",
    ]
    assert [(point.severity, point.color) for point in charts.by_severity] == [
        ("critical", "#dc3545"),
        ("warning", "#ffc107"),
        ("info", "#17a2b8"),
    ]
    assert [point.day for point in charts.over_time] == [
        sample_fixture.days.day_a,
        sample_fixture.days.day_b,
        sample_fixture.days.day_c,
    ]
    assert [(point.day, point.fsd, point.manual) for point in charts.fsd_timeline] == [
        (sample_fixture.days.day_a, 3, 3),
        (sample_fixture.days.day_b, 1, 4),
    ]


def test_get_db_connection_can_read_current_schema(sample_fixture: SampleFixture) -> None:
    with sample_fixture.service.open_db() as connection:
        row = connection.execute("SELECT COUNT(*) FROM trips").fetchone()

    assert row is not None
    assert int(row[0]) == 4


def _seed_sample_database(service: MappingQueries, media_root: Path) -> SampleDays:
    now = datetime.now(tz=UTC)
    day_a = (now - timedelta(days=5)).date().isoformat()
    day_b = (now - timedelta(days=2)).date().isoformat()
    day_c = (now - timedelta(days=1)).date().isoformat()
    _create_media_tree(media_root)
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
                    f"{day_a}T10:00:00",
                    f"{day_a}T10:02:20",
                    37.0,
                    -122.0,
                    37.0015,
                    -122.0015,
                    1.2,
                    140,
                    "RecentClips",
                    f"{day_a}T10:05:00",
                ),
                (
                    2,
                    f"{day_a}T12:00:00",
                    f"{day_a}T12:01:00",
                    37.2,
                    -122.2,
                    37.2001,
                    -122.2001,
                    0.02,
                    60,
                    "RecentClips",
                    f"{day_a}T12:05:00",
                ),
                (
                    3,
                    f"{day_b}T09:00:00",
                    f"{day_b}T09:10:00",
                    38.0,
                    -123.0,
                    38.0200,
                    -123.0040,
                    10.0,
                    600,
                    "ArchivedClips",
                    f"{day_b}T09:15:00",
                ),
                (
                    4,
                    f"{day_b}T13:00:00",
                    f"{day_b}T13:02:00",
                    39.0,
                    -124.0,
                    39.0010,
                    -124.0010,
                    0.5,
                    120,
                    "RecentClips",
                    f"{day_b}T13:05:00",
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
                    f"{day_a}T10:00:00",
                    37.0000,
                    -122.0000,
                    0.0,
                    10.0,
                    "MANUAL",
                    "RecentClips/drive1.mp4",
                    0,
                ),
                (
                    11,
                    1,
                    f"{day_a}T10:00:30",
                    37.0005,
                    -122.0005,
                    10.0,
                    12.0,
                    "AUTOSTEER",
                    "RecentClips/drive1.mp4",
                    30,
                ),
                (
                    12,
                    1,
                    f"{day_a}T10:02:00",
                    37.0010,
                    -122.0010,
                    20.0,
                    13.0,
                    "AUTOSTEER",
                    "RecentClips/drive1.mp4",
                    60,
                ),
                (
                    13,
                    1,
                    f"{day_a}T10:02:20",
                    37.0015,
                    -122.0015,
                    30.0,
                    14.0,
                    "SELF_DRIVING",
                    "RecentClips/drive1.mp4",
                    80,
                ),
                (
                    20,
                    2,
                    f"{day_a}T12:00:00",
                    37.2000,
                    -122.2000,
                    0.0,
                    1.0,
                    "MANUAL",
                    "RecentClips/missing.mp4",
                    0,
                ),
                (
                    21,
                    2,
                    f"{day_a}T12:00:20",
                    37.2001,
                    -122.2001,
                    5.0,
                    1.0,
                    "MANUAL",
                    "RecentClips/missing.mp4",
                    20,
                ),
                (
                    30,
                    3,
                    f"{day_b}T09:00:00",
                    38.0000,
                    -123.0000,
                    0.0,
                    20.0,
                    "MANUAL",
                    "ArchivedClips/archive1.mp4",
                    0,
                ),
                (
                    31,
                    3,
                    f"{day_b}T09:00:30",
                    38.0050,
                    -123.0010,
                    10.0,
                    21.0,
                    "MANUAL",
                    "ArchivedClips/archive1.mp4",
                    30,
                ),
                (
                    32,
                    3,
                    f"{day_b}T09:01:00",
                    38.0100,
                    -123.0020,
                    20.0,
                    22.0,
                    "AUTOSTEER",
                    "ArchivedClips/archive1.mp4",
                    60,
                ),
                (
                    33,
                    3,
                    f"{day_b}T09:01:30",
                    38.0150,
                    -123.0030,
                    30.0,
                    23.0,
                    "MANUAL",
                    "ArchivedClips/archive1.mp4",
                    90,
                ),
                (
                    34,
                    3,
                    f"{day_b}T09:02:00",
                    38.0200,
                    -123.0040,
                    40.0,
                    24.0,
                    "MANUAL",
                    "ArchivedClips/archive1.mp4",
                    120,
                ),
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
                    f"{day_a}T10:00:45",
                    37.0006,
                    -122.0006,
                    "hard_brake",
                    "warning",
                    "warning event",
                    "RecentClips/drive1.mp4",
                    35,
                    None,
                ),
                (
                    101,
                    1,
                    f"{day_a}T10:01:00",
                    37.0007,
                    -122.0007,
                    "hard_accel",
                    "info",
                    "info event",
                    "RecentClips/drive1.mp4",
                    40,
                    None,
                ),
                (
                    102,
                    3,
                    f"{day_b}T09:00:45",
                    38.0055,
                    -123.0011,
                    "sentry",
                    "info",
                    "sentry event",
                    "ArchivedClips/archive1.mp4",
                    45,
                    None,
                ),
                (
                    103,
                    None,
                    f"{day_c}T08:00:00",
                    36.5,
                    -121.5,
                    "lane_departure",
                    "critical",
                    "critical event",
                    None,
                    None,
                    '{"source": "test"}',
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
                (
                    str(media_root / "RecentClips" / "drive1.mp4"),
                    100,
                    1.0,
                    f"{day_a}T10:05:00",
                    4,
                    2,
                ),
                (
                    str(media_root / "ArchivedClips" / "archive1.mp4"),
                    100,
                    2.0,
                    f"{day_b}T09:15:00",
                    5,
                    1,
                ),
                (
                    str(media_root / "RecentClips" / "missing.mp4"),
                    100,
                    3.0,
                    f"{day_a}T12:05:00",
                    0,
                    0,
                ),
            ),
        )
        connection.commit()
    return SampleDays(day_a=day_a, day_b=day_b, day_c=day_c)


def _create_media_tree(media_root: Path) -> None:
    (media_root / "RecentClips").mkdir(parents=True, exist_ok=True)
    (media_root / "ArchivedClips").mkdir(parents=True, exist_ok=True)
    (media_root / "RecentClips" / "drive1.mp4").write_text("video", encoding="utf-8")
    (media_root / "ArchivedClips" / "archive1.mp4").write_text("video", encoding="utf-8")
