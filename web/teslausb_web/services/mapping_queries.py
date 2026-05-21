"""B-1 read-only SQL helpers for the mapping database.

Phase 5.13b ports v1's mapping query surface into a constructor-injected
service class. The module is intentionally larger than the soft 500 LOC
ceiling because it owns the full read-only mapping query surface, but it is
sectioned into small dataclasses and helper functions so each function stays
narrow.

Porting notes:
- Uses :class:`teslausb_web.services.mapping_migrations.MigrationsRunner`
  for connection setup; this module does not own schema initialization.
- Reuses :func:`teslausb_web.services.mapping_migrations._haversine_km` and
  converts to meters in :func:`_haversine_m` to avoid duplicating the geo math.
- ``Stats.indexer_status`` is intentionally ``None`` in Phase 5.13b because
  the worker-status integration lives in the Phase 5.13c service layer.
- Filesystem resolution is rooted at ``MappingQueriesConfig.media_root`` and
  deliberately omits v1's mode-aware partition logic.
"""

from __future__ import annotations

import contextlib
import logging
import math
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from teslausb_web.services.mapping_migrations import (
    _BACKUP_RETENTION,
    MappingDatabaseError,
    MappingMigrationError,
    MigrationsConfig,
    MigrationsRunner,
    _haversine_km,
    make_migrations_runner,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

_GAP_MAX_SECONDS_DEFAULT: Final[float] = 60.0
_GAP_MAX_METERS_DEFAULT: Final[float] = 250.0
_PLAYABLE_TRIPS_TTL_SECONDS: Final[float] = 60.0
_ARCHIVED_CLIPS_DIRNAME: Final[str] = "ArchivedClips"
_DEFAULT_TRIP_LIMIT: Final[int] = 50
_DEFAULT_EVENT_LIMIT: Final[int] = 100
_DEFAULT_DAY_LIMIT: Final[int] = 60
_DEFAULT_MIN_DISTANCE_KM: Final[float] = 0.05
_DEFAULT_EPSILON_METERS: Final[float] = 8.0
_DEFAULT_MAX_POINTS_PER_TRIP: Final[int] = 200
_MAX_PLAYABLE_CACHE_ENTRIES: Final[int] = 64
_PLAYABLE_CACHE_TRIMMED_SIZE: Final[int] = 32
_MIN_RENDERABLE_POINTS: Final[int] = 2
_MIN_RDP_POINTS: Final[int] = 3
_MANDATORY_ENDPOINT_COUNT: Final[int] = 2


class MappingQueryError(RuntimeError):
    """A mapping query could not be executed or decoded safely."""


@dataclass(frozen=True, slots=True)
class MappingQueriesConfig:
    db_path: Path
    backup_dir: Path
    media_root: Path
    backup_retention: int = _BACKUP_RETENTION
    playable_trips_ttl_seconds: float = _PLAYABLE_TRIPS_TTL_SECONDS
    archived_clips_dirname: str = _ARCHIVED_CLIPS_DIRNAME

    def __post_init__(self) -> None:
        if self.backup_retention <= 0:
            raise ValueError("backup_retention must be > 0")
        if self.playable_trips_ttl_seconds <= 0:
            raise ValueError("playable_trips_ttl_seconds must be > 0")
        if not self.archived_clips_dirname.strip():
            raise ValueError("archived_clips_dirname must be non-empty")
        if "/" in self.archived_clips_dirname or "\\" in self.archived_clips_dirname:
            raise ValueError("archived_clips_dirname must be a single path segment")


@dataclass(frozen=True, slots=True)
class TripRow:
    id: int
    start_time: str
    end_time: str | None
    start_lat: float | None
    start_lon: float | None
    end_lat: float | None
    end_lon: float | None
    distance_km: float
    duration_seconds: int
    source_folder: str | None
    indexed_at: str | None
    event_count: int
    video_count: int


@dataclass(frozen=True, slots=True)
class RouteWaypoint:
    id: int
    timestamp: str
    lat: float
    lon: float
    heading: float | None
    speed_mps: float | None
    autopilot_state: str | None
    video_path: str | None
    frame_offset: int | None
    gap_after: bool = False


@dataclass(frozen=True, slots=True)
class EventRow:
    id: int
    trip_id: int | None
    timestamp: str
    lat: float | None
    lon: float | None
    event_type: str
    severity: str
    description: str | None
    video_path: str | None
    frame_offset: int | None
    metadata: str | None


@dataclass(frozen=True, slots=True)
class DayRow:
    date: str
    trip_count: int
    total_distance_km: float
    event_count: int
    sentry_count: int
    first_start: str | None
    last_end: str | None


@dataclass(frozen=True, slots=True)
class DayRouteTrip:
    id: int
    start_time: str
    end_time: str | None
    distance_km: float
    duration_seconds: int
    start_lat: float | None
    start_lon: float | None
    end_lat: float | None
    end_lon: float | None
    source_folder: str | None
    waypoints: tuple[RouteWaypoint, ...]


@dataclass(frozen=True, slots=True)
class SimplifiedRoutePoint:
    lat: float
    lon: float
    speed_mps: float | None
    gap_after: bool = False


@dataclass(frozen=True, slots=True)
class AllRoutesTrip:
    id: int
    date: str
    start_time: str
    end_time: str | None
    start_lat: float | None
    start_lon: float | None
    end_lat: float | None
    end_lon: float | None
    distance_km: float
    duration_seconds: int
    waypoints: tuple[SimplifiedRoutePoint, ...]


@dataclass(frozen=True, slots=True)
class PlayableTrip:
    id: int
    is_playable: bool


@dataclass(frozen=True, slots=True)
class EventTypeCount:
    event_type: str
    count: int


@dataclass(frozen=True, slots=True)
class Stats:
    trip_count: int
    waypoint_count: int
    event_count: int
    indexed_file_count: int
    mapped_file_count: int
    total_distance_km: float
    total_duration_seconds: int
    event_breakdown: tuple[EventTypeCount, ...]
    indexer_status: str | None


@dataclass(frozen=True, slots=True)
class DrivingStats:
    has_data: bool
    trip_count: int = 0
    total_distance_km: float = 0.0
    total_distance_mi: float = 0.0
    total_duration_hours: float = 0.0
    avg_speed_mph: float = 0.0
    max_speed_mph: float = 0.0
    fsd_usage_pct: float = 0.0
    total_events: int = 0
    warning_events: int = 0
    events_per_100km: float = 0.0


@dataclass(frozen=True, slots=True)
class ChartCount:
    label: str
    value: int


@dataclass(frozen=True, slots=True)
class SeverityChartPoint:
    severity: str
    value: int
    color: str


@dataclass(frozen=True, slots=True)
class EventChartPoint:
    day: str
    value: int


@dataclass(frozen=True, slots=True)
class FsdTimelinePoint:
    day: str
    fsd: int
    manual: int


@dataclass(frozen=True, slots=True)
class EventChartData:
    by_type: tuple[ChartCount, ...]
    by_severity: tuple[SeverityChartPoint, ...]
    over_time: tuple[EventChartPoint, ...]
    fsd_timeline: tuple[FsdTimelinePoint, ...]


@dataclass(frozen=True, slots=True)
class _PlayableTripsCacheEntry:
    computed_at: float
    payload: tuple[PlayableTrip, ...]


@dataclass(frozen=True, slots=True)
class _RawPolylinePoint:
    timestamp: str
    lat: float
    lon: float
    speed_mps: float | None


@dataclass(slots=True)
class _DayRouteTripBuilder:
    id: int
    start_time: str
    end_time: str | None
    distance_km: float
    duration_seconds: int
    start_lat: float | None
    start_lon: float | None
    end_lat: float | None
    end_lon: float | None
    source_folder: str | None
    waypoints: list[RouteWaypoint]

    def build(self) -> DayRouteTrip:
        return DayRouteTrip(
            id=self.id,
            start_time=self.start_time,
            end_time=self.end_time,
            distance_km=self.distance_km,
            duration_seconds=self.duration_seconds,
            start_lat=self.start_lat,
            start_lon=self.start_lon,
            end_lat=self.end_lat,
            end_lon=self.end_lon,
            source_folder=self.source_folder,
            waypoints=_stamp_gap_waypoints(self.waypoints),
        )


@dataclass(slots=True)
class _AllRoutesTripBuilder:
    id: int
    date: str
    start_time: str
    end_time: str | None
    start_lat: float | None
    start_lon: float | None
    end_lat: float | None
    end_lon: float | None
    distance_km: float
    duration_seconds: int
    waypoints: list[_RawPolylinePoint]

    def build(self, *, epsilon_m: float, max_points_per_trip: int) -> AllRoutesTrip | None:
        simplified = _simplify_trip_points(self.waypoints, epsilon_m, max_points_per_trip)
        if len(simplified) < _MIN_RENDERABLE_POINTS:
            return None
        return AllRoutesTrip(
            id=self.id,
            date=self.date,
            start_time=self.start_time,
            end_time=self.end_time,
            start_lat=self.start_lat,
            start_lon=self.start_lon,
            end_lat=self.end_lat,
            end_lon=self.end_lon,
            distance_km=self.distance_km,
            duration_seconds=self.duration_seconds,
            waypoints=simplified,
        )


class MappingQueries:
    """Read-only query service for the mapping database."""

    def __init__(
        self,
        *,
        config: MappingQueriesConfig,
        migrations_runner: MigrationsRunner | None = None,
    ) -> None:
        self._config = config
        self._migrations_runner = migrations_runner or MigrationsRunner(
            MigrationsConfig(
                db_path=config.db_path,
                backup_dir=config.backup_dir,
                backup_retention=config.backup_retention,
            )
        )
        self._playable_trips_cache: dict[str, _PlayableTripsCacheEntry] = {}
        self._playable_trips_cache_lock = threading.RLock()

    def get_db_connection(self) -> sqlite3.Connection:
        """Return a configured SQLite connection from the migrations runner."""
        try:
            return self._migrations_runner.init_db()
        except (MappingDatabaseError, MappingMigrationError, sqlite3.Error) as exc:
            raise MappingQueryError(f"Failed to open mapping database: {exc}") from exc

    def query_trips(  # noqa: PLR0913
        self,
        *,
        limit: int | None = _DEFAULT_TRIP_LIMIT,
        offset: int | None = 0,
        bbox: tuple[float, float, float, float] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        min_distance_km: float | None = _DEFAULT_MIN_DISTANCE_KM,
    ) -> tuple[TripRow, ...]:
        sql, params = _build_query_trips_sql(
            limit=_coerce_limit(limit, default=_DEFAULT_TRIP_LIMIT),
            offset=_coerce_offset(offset),
            bbox=bbox,
            date_from=date_from,
            date_to=date_to,
            min_distance_km=_coerce_min_distance(min_distance_km),
        )
        return tuple(_trip_row_from_row(row) for row in self._fetch_rows(sql, tuple(params)))

    def query_trip_route(self, trip_id: int) -> tuple[RouteWaypoint, ...]:
        rows = self._fetch_rows(
            """
            SELECT id, timestamp, lat, lon, heading, speed_mps, autopilot_state, video_path,
                   frame_offset
              FROM waypoints
             WHERE trip_id = ?
             ORDER BY timestamp ASC, id ASC
            """,
            (trip_id,),
        )
        return _stamp_gap_waypoints(_route_waypoints_from_rows(rows))

    def query_events(  # noqa: PLR0913
        self,
        *,
        limit: int | None = _DEFAULT_EVENT_LIMIT,
        offset: int | None = 0,
        event_type: str | None = None,
        severity: str | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        date: str | None = None,
    ) -> tuple[EventRow, ...]:
        sql, params = _build_query_events_sql(
            limit=_coerce_limit(limit, default=_DEFAULT_EVENT_LIMIT),
            offset=_coerce_offset(offset),
            event_type=event_type,
            severity=severity,
            bbox=bbox,
            date_from=date_from,
            date_to=date_to,
            date=date,
        )
        return tuple(_event_row_from_row(row) for row in self._fetch_rows(sql, tuple(params)))

    def query_days(
        self,
        *,
        limit: int | None = _DEFAULT_DAY_LIMIT,
        min_distance_km: float | None = _DEFAULT_MIN_DISTANCE_KM,
    ) -> tuple[DayRow, ...]:
        rows = self._fetch_rows(
            _QUERY_DAYS_SQL,
            (
                _coerce_min_distance(min_distance_km),
                _coerce_limit(limit, default=_DEFAULT_DAY_LIMIT),
            ),
        )
        return tuple(_day_row_from_row(row) for row in rows)

    def query_day_routes(
        self,
        date_str: str,
        *,
        min_distance_km: float | None = _DEFAULT_MIN_DISTANCE_KM,
    ) -> tuple[DayRouteTrip, ...]:
        rows = self._fetch_rows(
            _QUERY_DAY_ROUTES_SQL,
            (date_str, _coerce_min_distance(min_distance_km)),
        )
        return _day_route_trips_from_rows(rows)

    def query_all_routes_simplified(
        self,
        *,
        min_distance_km: float | None = _DEFAULT_MIN_DISTANCE_KM,
        epsilon_m: float | None = _DEFAULT_EPSILON_METERS,
        max_points_per_trip: int | None = _DEFAULT_MAX_POINTS_PER_TRIP,
    ) -> tuple[AllRoutesTrip, ...]:
        rows = self._fetch_rows(
            _QUERY_ALL_ROUTES_SQL,
            (_coerce_min_distance(min_distance_km),),
        )
        return _all_routes_from_rows(
            rows,
            epsilon_m=_coerce_epsilon(epsilon_m),
            max_points_per_trip=_coerce_max_points(max_points_per_trip),
        )

    def playable_trips_for_date(self, date_str: str) -> tuple[PlayableTrip, ...]:
        cached = self._get_playable_trips_cache(date_str)
        if cached is not None:
            return cached
        rows = self._fetch_rows(_PLAYABLE_TRIPS_SQL, (date_str,))
        result = _playable_trips_from_rows(rows, self._resolve_video_path_on_disk)
        self._store_playable_trips_cache(date_str, result)
        return result

    def get_stats(self) -> Stats:
        summary = self._fetch_row(
            """
            SELECT (SELECT COUNT(*) FROM trips) AS trip_count,
                   (SELECT COUNT(*) FROM waypoints) AS waypoint_count,
                   (SELECT COUNT(*) FROM detected_events) AS event_count,
                   (SELECT COUNT(*) FROM indexed_files) AS indexed_file_count,
                   (
                       SELECT COUNT(*) FROM indexed_files WHERE waypoint_count > 0
                   ) AS mapped_file_count,
                   (SELECT COALESCE(SUM(distance_km), 0.0) FROM trips) AS total_distance_km,
                   (SELECT COALESCE(SUM(duration_seconds), 0) FROM trips) AS total_duration_seconds
            """
        )
        breakdown = tuple(
            _event_type_count_from_row(row)
            for row in self._fetch_rows(
                """
                SELECT event_type, COUNT(*) AS count
                  FROM detected_events
                 GROUP BY event_type
                 ORDER BY event_type
                """
            )
        )
        return Stats(
            trip_count=_require_int(summary, "trip_count"),
            waypoint_count=_require_int(summary, "waypoint_count"),
            event_count=_require_int(summary, "event_count"),
            indexed_file_count=_require_int(summary, "indexed_file_count"),
            mapped_file_count=_require_int(summary, "mapped_file_count"),
            total_distance_km=round(_require_number(summary, "total_distance_km"), 2),
            total_duration_seconds=_require_int(summary, "total_duration_seconds"),
            event_breakdown=breakdown,
            indexer_status=None,
        )

    def get_driving_stats(self) -> DrivingStats:
        summary = self._fetch_row(_DRIVING_STATS_SQL)
        trip_count = _require_int(summary, "trip_count")
        if trip_count == 0:
            return DrivingStats(has_data=False)
        total_distance = _require_number(summary, "total_distance_km")
        total_duration = _require_int(summary, "total_duration_seconds")
        avg_speed = _require_number(summary, "avg_speed_mps")
        max_speed = _require_number(summary, "max_speed_mps")
        total_events = _require_int(summary, "total_events")
        warning_events = _require_int(summary, "warning_events")
        total_waypoints = _require_int(summary, "total_waypoints")
        fsd_waypoints = _require_int(summary, "fsd_waypoints")
        return DrivingStats(
            has_data=True,
            trip_count=trip_count,
            total_distance_km=round(total_distance, 1),
            total_distance_mi=round(total_distance * 0.621371, 1),
            total_duration_hours=round(total_duration / 3600.0, 1),
            avg_speed_mph=round(avg_speed * 2.23694, 1),
            max_speed_mph=round(max_speed * 2.23694, 1),
            fsd_usage_pct=round(_percentage(fsd_waypoints, total_waypoints), 1),
            total_events=total_events,
            warning_events=warning_events,
            events_per_100km=round(_events_per_100km(warning_events, total_distance), 1),
        )

    def get_event_chart_data(self) -> EventChartData:
        cutoff_day = _cutoff_day_string(30)
        by_type = tuple(
            _chart_count_from_row(row)
            for row in self._fetch_rows(
                """
                SELECT REPLACE(event_type, '_', ' ') AS label, COUNT(*) AS value
                  FROM detected_events
                 GROUP BY event_type
                 ORDER BY value DESC, event_type ASC
                """
            )
        )
        by_severity = tuple(
            _severity_chart_point_from_row(row) for row in self._fetch_rows(_EVENTS_BY_SEVERITY_SQL)
        )
        over_time = tuple(
            _event_chart_point_from_row(row)
            for row in self._fetch_rows(_EVENTS_OVER_TIME_SQL, (cutoff_day,))
        )
        fsd_timeline = tuple(
            _fsd_timeline_point_from_row(row)
            for row in self._fetch_rows(_FSD_TIMELINE_SQL, (cutoff_day,))
        )
        return EventChartData(
            by_type=by_type,
            by_severity=by_severity,
            over_time=over_time,
            fsd_timeline=fsd_timeline,
        )

    def reset_playable_trips_cache_for_tests(self) -> None:
        with self._playable_trips_cache_lock:
            self._playable_trips_cache.clear()

    def _fetch_rows(
        self,
        sql: str,
        params: tuple[object, ...] = (),
    ) -> tuple[sqlite3.Row, ...]:
        with contextlib.closing(self.get_db_connection()) as connection:
            return tuple(connection.execute(sql, params).fetchall())

    def _fetch_row(
        self,
        sql: str,
        params: tuple[object, ...] = (),
    ) -> sqlite3.Row:
        with contextlib.closing(self.get_db_connection()) as connection:
            row = connection.execute(sql, params).fetchone()
        if not isinstance(row, sqlite3.Row):
            raise MappingQueryError("Expected query row but got none")
        return row

    def _resolve_video_path_on_disk(self, video_path: str) -> bool:
        parts = _normalized_relative_parts(video_path)
        if not parts:
            return False
        media_root = self._config.media_root
        archived_root = media_root / self._config.archived_clips_dirname
        if parts[0] == self._config.archived_clips_dirname:
            return _path_exists(media_root.joinpath(*parts))
        if _path_exists(media_root.joinpath(*parts)):
            return True
        return _path_exists(archived_root / parts[-1])

    def _get_playable_trips_cache(self, date_str: str) -> tuple[PlayableTrip, ...] | None:
        with self._playable_trips_cache_lock:
            cached = self._playable_trips_cache.get(date_str)
            if cached is None:
                return None
            age = time.monotonic() - cached.computed_at
            if age >= self._config.playable_trips_ttl_seconds:
                return None
            return cached.payload

    def _store_playable_trips_cache(self, date_str: str, payload: tuple[PlayableTrip, ...]) -> None:
        with self._playable_trips_cache_lock:
            self._playable_trips_cache[date_str] = _PlayableTripsCacheEntry(
                computed_at=time.monotonic(),
                payload=payload,
            )
            if len(self._playable_trips_cache) > _MAX_PLAYABLE_CACHE_ENTRIES:
                self._trim_playable_trips_cache()

    def _trim_playable_trips_cache(self) -> None:
        oldest = sorted(
            self._playable_trips_cache.items(),
            key=lambda item: item[1].computed_at,
        )[: len(self._playable_trips_cache) - _PLAYABLE_CACHE_TRIMMED_SIZE]
        for cache_key, _ in oldest:
            self._playable_trips_cache.pop(cache_key, None)


_QUERY_DAYS_SQL = """
WITH trip_days AS (
    SELECT substr(start_time, 1, 10) AS day,
           COUNT(*) AS trip_count,
           COALESCE(SUM(distance_km), 0.0) AS total_distance_km,
           0 AS event_count,
           0 AS sentry_count,
           MIN(start_time) AS first_start,
           MAX(COALESCE(end_time, start_time)) AS last_end
      FROM trips
     WHERE start_time IS NOT NULL
       AND COALESCE(distance_km, 0) >= ?
     GROUP BY day
),
event_days AS (
    SELECT substr(timestamp, 1, 10) AS day,
           0 AS trip_count,
           0.0 AS total_distance_km,
           COUNT(*) AS event_count,
           SUM(CASE WHEN event_type = 'sentry' THEN 1 ELSE 0 END) AS sentry_count,
           NULL AS first_start,
           NULL AS last_end
      FROM detected_events
     WHERE timestamp IS NOT NULL
     GROUP BY day
)
SELECT day AS date,
       SUM(trip_count) AS trip_count,
       SUM(total_distance_km) AS total_distance_km,
       SUM(event_count) AS event_count,
       SUM(sentry_count) AS sentry_count,
       MIN(first_start) AS first_start,
       MAX(last_end) AS last_end
  FROM (
      SELECT * FROM trip_days
      UNION ALL
      SELECT * FROM event_days
  )
 WHERE day IS NOT NULL
 GROUP BY day
 ORDER BY day DESC
 LIMIT ?
"""

_QUERY_DAY_ROUTES_SQL = """
SELECT t.id AS trip_id,
       t.start_time AS start_time,
       t.end_time AS end_time,
       t.distance_km AS distance_km,
       t.duration_seconds AS duration_seconds,
       t.start_lat AS start_lat,
       t.start_lon AS start_lon,
       t.end_lat AS end_lat,
       t.end_lon AS end_lon,
       t.source_folder AS source_folder,
       w.id AS waypoint_id,
       w.timestamp AS waypoint_timestamp,
       w.lat AS waypoint_lat,
       w.lon AS waypoint_lon,
       w.heading AS waypoint_heading,
       w.speed_mps AS waypoint_speed_mps,
       w.autopilot_state AS waypoint_autopilot_state,
       w.video_path AS waypoint_video_path,
       w.frame_offset AS waypoint_frame_offset
  FROM trips t
  JOIN waypoints w ON w.trip_id = t.id
 WHERE substr(t.start_time, 1, 10) = ?
   AND COALESCE(t.distance_km, 0) >= ?
 ORDER BY t.start_time DESC, w.timestamp ASC, w.id ASC
"""

_QUERY_ALL_ROUTES_SQL = """
SELECT t.id AS trip_id,
       substr(t.start_time, 1, 10) AS date,
       t.start_time AS start_time,
       t.end_time AS end_time,
       t.start_lat AS start_lat,
       t.start_lon AS start_lon,
       t.end_lat AS end_lat,
       t.end_lon AS end_lon,
       t.distance_km AS distance_km,
       t.duration_seconds AS duration_seconds,
       w.timestamp AS waypoint_timestamp,
       w.lat AS waypoint_lat,
       w.lon AS waypoint_lon,
       w.speed_mps AS waypoint_speed_mps
  FROM trips t
  JOIN waypoints w ON w.trip_id = t.id
 WHERE t.start_time IS NOT NULL
   AND COALESCE(t.distance_km, 0) >= ?
   AND w.lat IS NOT NULL
   AND w.lon IS NOT NULL
 ORDER BY t.start_time DESC, w.timestamp ASC, w.id ASC
"""

_PLAYABLE_TRIPS_SQL = """
SELECT t.id AS trip_id,
       w.video_path AS video_path
  FROM trips t
  LEFT JOIN waypoints w
    ON w.trip_id = t.id
   AND w.video_path IS NOT NULL
   AND w.video_path != ''
 WHERE substr(t.start_time, 1, 10) = ?
 ORDER BY t.id ASC, w.video_path ASC
"""

_DRIVING_STATS_SQL = """
SELECT (SELECT COUNT(*) FROM trips) AS trip_count,
       (SELECT COALESCE(SUM(distance_km), 0.0) FROM trips) AS total_distance_km,
       (SELECT COALESCE(SUM(duration_seconds), 0) FROM trips) AS total_duration_seconds,
       (SELECT COALESCE(AVG(speed_mps), 0.0) FROM waypoints WHERE speed_mps > 0.5) AS avg_speed_mps,
       (SELECT COALESCE(MAX(speed_mps), 0.0) FROM waypoints) AS max_speed_mps,
       (SELECT COUNT(*) FROM waypoints) AS total_waypoints,
       (SELECT COUNT(*) FROM waypoints
         WHERE autopilot_state IN ('SELF_DRIVING', 'AUTOSTEER')) AS fsd_waypoints,
       (SELECT COUNT(*) FROM detected_events) AS total_events,
       (SELECT COUNT(*) FROM detected_events
         WHERE severity IN ('warning', 'critical')) AS warning_events
"""

_EVENTS_BY_SEVERITY_SQL = """
SELECT severity,
       COUNT(*) AS value,
       CASE severity
           WHEN 'critical' THEN '#dc3545'
           WHEN 'warning' THEN '#ffc107'
           ELSE '#17a2b8'
       END AS color
  FROM detected_events
 GROUP BY severity
 ORDER BY CASE severity WHEN 'critical' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END, severity ASC
"""

_EVENTS_OVER_TIME_SQL = """
SELECT substr(timestamp, 1, 10) AS day,
       COUNT(*) AS value
  FROM detected_events
 WHERE substr(timestamp, 1, 10) >= ?
 GROUP BY day
 ORDER BY day ASC
"""

_FSD_TIMELINE_SQL = """
SELECT substr(timestamp, 1, 10) AS day,
       SUM(CASE WHEN autopilot_state IN ('SELF_DRIVING', 'AUTOSTEER') THEN 1 ELSE 0 END) AS fsd,
       SUM(
           CASE WHEN autopilot_state NOT IN ('SELF_DRIVING', 'AUTOSTEER') THEN 1 ELSE 0 END
       ) AS manual
  FROM waypoints
 WHERE substr(timestamp, 1, 10) >= ?
 GROUP BY day
 ORDER BY day ASC
"""


def make_mapping_queries(cfg: WebConfig | MappingQueriesConfig) -> MappingQueries:
    """Build the mapping query service from app config or explicit settings."""
    if isinstance(cfg, MappingQueriesConfig):
        return MappingQueries(config=cfg)
    migrations_runner = make_migrations_runner(cfg)
    return MappingQueries(
        config=MappingQueriesConfig(
            db_path=cfg.mapping.db_path,
            backup_dir=cfg.mapping.backup_dir,
            backup_retention=cfg.mapping.backup_retention,
            media_root=cfg.paths.backing_root,
        ),
        migrations_runner=migrations_runner,
    )


def _build_query_trips_sql(  # noqa: PLR0913
    *,
    limit: int,
    offset: int,
    bbox: tuple[float, float, float, float] | None,
    date_from: str | None,
    date_to: str | None,
    min_distance_km: float,
) -> tuple[str, list[object]]:
    sql = [
        "SELECT t.id, t.start_time, t.end_time, t.start_lat, t.start_lon, t.end_lat, t.end_lon,",
        "       t.distance_km, t.duration_seconds, t.source_folder, t.indexed_at,",
        "       (SELECT COUNT(*) FROM detected_events de WHERE de.trip_id = t.id) AS event_count,",
        "       (SELECT COUNT(DISTINCT w.video_path) FROM waypoints w",
        "         WHERE w.trip_id = t.id AND w.video_path IS NOT NULL) AS video_count",
        "  FROM trips t",
        " WHERE 1=1",
    ]
    params: list[object] = []
    if min_distance_km > 0:
        sql.append("   AND COALESCE(t.distance_km, 0) >= ?")
        params.append(min_distance_km)
    if bbox is not None:
        _append_bbox_trip_filters(sql, params, bbox)
    if date_from is not None:
        sql.append("   AND t.start_time >= ?")
        params.append(date_from)
    if date_to is not None:
        sql.append("   AND t.start_time <= ?")
        params.append(date_to)
    sql.append(" ORDER BY t.start_time DESC LIMIT ? OFFSET ?")
    params.extend((limit, offset))
    return "\n".join(sql), params


def _append_bbox_trip_filters(
    sql: list[str],
    params: list[object],
    bbox: tuple[float, float, float, float],
) -> None:
    min_lat, min_lon, max_lat, max_lon = bbox
    sql.append("   AND t.start_lat BETWEEN ? AND ? AND t.start_lon BETWEEN ? AND ?")
    params.extend((min_lat, max_lat, min_lon, max_lon))


def _build_query_events_sql(  # noqa: PLR0913
    *,
    limit: int,
    offset: int,
    event_type: str | None,
    severity: str | None,
    bbox: tuple[float, float, float, float] | None,
    date_from: str | None,
    date_to: str | None,
    date: str | None,
) -> tuple[str, list[object]]:
    sql = [
        "SELECT id, trip_id, timestamp, lat, lon, event_type, severity, description,",
        "       video_path, frame_offset, metadata",
        "  FROM detected_events",
        " WHERE 1=1",
    ]
    params: list[object] = []
    if event_type is not None:
        sql.append("   AND event_type = ?")
        params.append(event_type)
    if severity is not None:
        sql.append("   AND severity = ?")
        params.append(severity)
    if bbox is not None:
        _append_bbox_event_filters(sql, params, bbox)
    if date_from is not None:
        sql.append("   AND timestamp >= ?")
        params.append(date_from)
    if date_to is not None:
        sql.append("   AND timestamp <= ?")
        params.append(date_to)
    if date is not None:
        sql.append("   AND substr(timestamp, 1, 10) = ?")
        params.append(date)
    sql.append(" ORDER BY timestamp DESC LIMIT ? OFFSET ?")
    params.extend((limit, offset))
    return "\n".join(sql), params


def _append_bbox_event_filters(
    sql: list[str],
    params: list[object],
    bbox: tuple[float, float, float, float],
) -> None:
    min_lat, min_lon, max_lat, max_lon = bbox
    sql.append("   AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?")
    params.extend((min_lat, max_lat, min_lon, max_lon))


def _trip_row_from_row(row: sqlite3.Row) -> TripRow:
    return TripRow(
        id=_require_int(row, "id"),
        start_time=_require_str(row, "start_time"),
        end_time=_optional_str(row, "end_time"),
        start_lat=_optional_number(row, "start_lat"),
        start_lon=_optional_number(row, "start_lon"),
        end_lat=_optional_number(row, "end_lat"),
        end_lon=_optional_number(row, "end_lon"),
        distance_km=_require_number(row, "distance_km"),
        duration_seconds=_require_int(row, "duration_seconds"),
        source_folder=_optional_str(row, "source_folder"),
        indexed_at=_optional_str(row, "indexed_at"),
        event_count=_require_int(row, "event_count"),
        video_count=_require_int(row, "video_count"),
    )


def _route_waypoints_from_rows(rows: Sequence[sqlite3.Row]) -> list[RouteWaypoint]:
    return [_route_waypoint_from_row(row) for row in rows]


def _route_waypoint_from_row(row: sqlite3.Row) -> RouteWaypoint:
    return RouteWaypoint(
        id=_require_int(row, "id"),
        timestamp=_require_str(row, "timestamp"),
        lat=_require_number(row, "lat"),
        lon=_require_number(row, "lon"),
        heading=_optional_number(row, "heading"),
        speed_mps=_optional_number(row, "speed_mps"),
        autopilot_state=_optional_str(row, "autopilot_state"),
        video_path=_optional_str(row, "video_path"),
        frame_offset=_optional_int(row, "frame_offset"),
    )


def _event_row_from_row(row: sqlite3.Row) -> EventRow:
    return EventRow(
        id=_require_int(row, "id"),
        trip_id=_optional_int(row, "trip_id"),
        timestamp=_require_str(row, "timestamp"),
        lat=_optional_number(row, "lat"),
        lon=_optional_number(row, "lon"),
        event_type=_require_str(row, "event_type"),
        severity=_require_str(row, "severity"),
        description=_optional_str(row, "description"),
        video_path=_optional_str(row, "video_path"),
        frame_offset=_optional_int(row, "frame_offset"),
        metadata=_optional_str(row, "metadata"),
    )


def _day_row_from_row(row: sqlite3.Row) -> DayRow:
    return DayRow(
        date=_require_str(row, "date"),
        trip_count=_require_int(row, "trip_count"),
        total_distance_km=_require_number(row, "total_distance_km"),
        event_count=_require_int(row, "event_count"),
        sentry_count=_require_int(row, "sentry_count"),
        first_start=_optional_str(row, "first_start"),
        last_end=_optional_str(row, "last_end"),
    )


def _day_route_trips_from_rows(rows: Sequence[sqlite3.Row]) -> tuple[DayRouteTrip, ...]:
    builders: dict[int, _DayRouteTripBuilder] = {}
    order: list[int] = []
    for row in rows:
        trip_id = _require_int(row, "trip_id")
        builder = builders.get(trip_id)
        if builder is None:
            builder = _day_route_trip_builder_from_row(row)
            builders[trip_id] = builder
            order.append(trip_id)
        builder.waypoints.append(_prefixed_route_waypoint_from_row(row))
    return tuple(builders[trip_id].build() for trip_id in order)


def _day_route_trip_builder_from_row(row: sqlite3.Row) -> _DayRouteTripBuilder:
    return _DayRouteTripBuilder(
        id=_require_int(row, "trip_id"),
        start_time=_require_str(row, "start_time"),
        end_time=_optional_str(row, "end_time"),
        distance_km=_require_number(row, "distance_km"),
        duration_seconds=_require_int(row, "duration_seconds"),
        start_lat=_optional_number(row, "start_lat"),
        start_lon=_optional_number(row, "start_lon"),
        end_lat=_optional_number(row, "end_lat"),
        end_lon=_optional_number(row, "end_lon"),
        source_folder=_optional_str(row, "source_folder"),
        waypoints=[],
    )


def _prefixed_route_waypoint_from_row(row: sqlite3.Row) -> RouteWaypoint:
    return RouteWaypoint(
        id=_require_int(row, "waypoint_id"),
        timestamp=_require_str(row, "waypoint_timestamp"),
        lat=_require_number(row, "waypoint_lat"),
        lon=_require_number(row, "waypoint_lon"),
        heading=_optional_number(row, "waypoint_heading"),
        speed_mps=_optional_number(row, "waypoint_speed_mps"),
        autopilot_state=_optional_str(row, "waypoint_autopilot_state"),
        video_path=_optional_str(row, "waypoint_video_path"),
        frame_offset=_optional_int(row, "waypoint_frame_offset"),
    )


def _all_routes_from_rows(
    rows: Sequence[sqlite3.Row],
    *,
    epsilon_m: float,
    max_points_per_trip: int,
) -> tuple[AllRoutesTrip, ...]:
    builders: dict[int, _AllRoutesTripBuilder] = {}
    order: list[int] = []
    for row in rows:
        trip_id = _require_int(row, "trip_id")
        builder = builders.get(trip_id)
        if builder is None:
            builder = _all_routes_trip_builder_from_row(row)
            builders[trip_id] = builder
            order.append(trip_id)
        builder.waypoints.append(_raw_polyline_point_from_row(row))
    return tuple(
        built
        for trip_id in order
        if (
            built := builders[trip_id].build(
                epsilon_m=epsilon_m, max_points_per_trip=max_points_per_trip
            )
        )
        is not None
    )


def _all_routes_trip_builder_from_row(row: sqlite3.Row) -> _AllRoutesTripBuilder:
    return _AllRoutesTripBuilder(
        id=_require_int(row, "trip_id"),
        date=_require_str(row, "date"),
        start_time=_require_str(row, "start_time"),
        end_time=_optional_str(row, "end_time"),
        start_lat=_optional_number(row, "start_lat"),
        start_lon=_optional_number(row, "start_lon"),
        end_lat=_optional_number(row, "end_lat"),
        end_lon=_optional_number(row, "end_lon"),
        distance_km=_require_number(row, "distance_km"),
        duration_seconds=_require_int(row, "duration_seconds"),
        waypoints=[],
    )


def _raw_polyline_point_from_row(row: sqlite3.Row) -> _RawPolylinePoint:
    return _RawPolylinePoint(
        timestamp=_require_str(row, "waypoint_timestamp"),
        lat=_require_number(row, "waypoint_lat"),
        lon=_require_number(row, "waypoint_lon"),
        speed_mps=_optional_number(row, "waypoint_speed_mps"),
    )


def _playable_trips_from_rows(
    rows: Sequence[sqlite3.Row],
    resolver: Callable[[str], bool],
) -> tuple[PlayableTrip, ...]:
    by_trip: dict[int, set[str]] = {}
    for row in rows:
        trip_id = _require_int(row, "trip_id")
        video_path = _optional_str(row, "video_path")
        by_trip.setdefault(trip_id, set())
        if video_path:
            by_trip[trip_id].add(video_path)
    file_cache: dict[str, bool] = {}
    return tuple(
        PlayableTrip(id=trip_id, is_playable=_trip_is_playable(paths, resolver, file_cache))
        for trip_id, paths in by_trip.items()
    )


def _trip_is_playable(
    paths: set[str],
    resolver: Callable[[str], bool],
    file_cache: dict[str, bool],
) -> bool:
    for path in paths:
        cached = file_cache.get(path)
        if cached is None:
            cached = resolver(path)
            file_cache[path] = cached
        if cached:
            return True
    return False


def _event_type_count_from_row(row: sqlite3.Row) -> EventTypeCount:
    return EventTypeCount(
        event_type=_require_str(row, "event_type"),
        count=_require_int(row, "count"),
    )


def _chart_count_from_row(row: sqlite3.Row) -> ChartCount:
    return ChartCount(
        label=_title_case_label(_require_str(row, "label")),
        value=_require_int(row, "value"),
    )


def _severity_chart_point_from_row(row: sqlite3.Row) -> SeverityChartPoint:
    return SeverityChartPoint(
        severity=_require_str(row, "severity"),
        value=_require_int(row, "value"),
        color=_require_str(row, "color"),
    )


def _event_chart_point_from_row(row: sqlite3.Row) -> EventChartPoint:
    return EventChartPoint(
        day=_require_str(row, "day"),
        value=_require_int(row, "value"),
    )


def _fsd_timeline_point_from_row(row: sqlite3.Row) -> FsdTimelinePoint:
    return FsdTimelinePoint(
        day=_require_str(row, "day"),
        fsd=_require_int(row, "fsd"),
        manual=_require_int(row, "manual"),
    )


def _title_case_label(label: str) -> str:
    return label.title()


def _stamp_gap_waypoints(waypoints: Sequence[RouteWaypoint]) -> tuple[RouteWaypoint, ...]:
    stamped = list(waypoints)
    for index in range(len(stamped) - 1):
        if _waypoints_have_gap(stamped[index], stamped[index + 1]):
            stamped[index] = replace(stamped[index], gap_after=True)
    return tuple(stamped)


def _waypoints_have_gap(current: RouteWaypoint, nxt: RouteWaypoint) -> bool:
    return _is_gap_between(
        current.timestamp,
        current.lat,
        current.lon,
        nxt.timestamp,
        nxt.lat,
        nxt.lon,
    )


def _simplify_trip_points(
    points: Sequence[_RawPolylinePoint],
    epsilon_m: float,
    max_points_per_trip: int,
) -> tuple[SimplifiedRoutePoint, ...]:
    if len(points) < _MIN_RENDERABLE_POINTS:
        return ()
    segments = _split_segments(points)
    simplified = _simplify_segments(segments, epsilon_m)
    return _cap_simplified_points(simplified, max_points_per_trip)


def _split_segments(
    points: Sequence[_RawPolylinePoint],
) -> tuple[tuple[_RawPolylinePoint, ...], ...]:
    current: list[_RawPolylinePoint] = [points[0]]
    segments: list[tuple[_RawPolylinePoint, ...]] = []
    for index in range(1, len(points)):
        previous = points[index - 1]
        point = points[index]
        if _raw_points_have_gap(previous, point):
            segments.append(tuple(current))
            current = [point]
        else:
            current.append(point)
    segments.append(tuple(current))
    return tuple(segments)


def _raw_points_have_gap(previous: _RawPolylinePoint, current: _RawPolylinePoint) -> bool:
    return _is_gap_between(
        previous.timestamp,
        previous.lat,
        previous.lon,
        current.timestamp,
        current.lat,
        current.lon,
    )


def _simplify_segments(
    segments: Sequence[Sequence[_RawPolylinePoint]],
    epsilon_m: float,
) -> tuple[SimplifiedRoutePoint, ...]:
    output: list[SimplifiedRoutePoint] = []
    for index, segment in enumerate(segments):
        simplified_segment = _simplify_segment(segment, epsilon_m)
        if index < len(segments) - 1 and simplified_segment:
            simplified_segment[-1] = replace(simplified_segment[-1], gap_after=True)
        output.extend(simplified_segment)
    return tuple(output)


def _simplify_segment(
    segment: Sequence[_RawPolylinePoint],
    epsilon_m: float,
) -> list[SimplifiedRoutePoint]:
    if len(segment) == 1:
        point = segment[0]
        return [SimplifiedRoutePoint(lat=point.lat, lon=point.lon, speed_mps=point.speed_mps)]
    indices = _simplify_polyline_rdp([(point.lat, point.lon) for point in segment], epsilon_m)
    return [
        SimplifiedRoutePoint(
            lat=segment[index].lat,
            lon=segment[index].lon,
            speed_mps=segment[index].speed_mps,
        )
        for index in indices
    ]


def _cap_simplified_points(
    points: tuple[SimplifiedRoutePoint, ...],
    max_points_per_trip: int,
) -> tuple[SimplifiedRoutePoint, ...]:
    if len(points) <= max_points_per_trip:
        return points
    mandatory = sorted(
        {0, len(points) - 1, *[i for i, point in enumerate(points) if point.gap_after]}
    )
    mandatory = _compress_mandatory_indices(mandatory, max_points_per_trip)
    remaining = [i for i in range(len(points)) if i not in set(mandatory)]
    extra = _select_uniform_indices(remaining, max_points_per_trip - len(mandatory))
    selected = sorted((*mandatory, *extra))
    return tuple(points[index] for index in selected)


def _compress_mandatory_indices(indices: list[int], max_points_per_trip: int) -> list[int]:
    if len(indices) <= max_points_per_trip:
        return indices
    selected = _select_uniform_indices(
        indices[1:-1],
        max_points_per_trip - _MANDATORY_ENDPOINT_COUNT,
    )
    return [indices[0], *selected, indices[-1]]


def _select_uniform_indices(candidates: Sequence[int], count: int) -> list[int]:
    if count <= 0 or not candidates:
        return []
    if len(candidates) <= count:
        return list(candidates)
    step = len(candidates) / count
    return [candidates[min(int(index * step), len(candidates) - 1)] for index in range(count)]


def _parse_iso_seconds(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    candidate = f"{timestamp[:-1]}+00:00" if timestamp.endswith("Z") else timestamp
    try:
        return datetime.fromisoformat(candidate).timestamp()
    except ValueError:
        return None


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return _haversine_km(lat1, lon1, lat2, lon2) * 1000.0


def _is_gap_between(  # noqa: PLR0913
    prev_ts: str | None,
    prev_lat: float | None,
    prev_lon: float | None,
    curr_ts: str | None,
    curr_lat: float | None,
    curr_lon: float | None,
    *,
    max_seconds: float = _GAP_MAX_SECONDS_DEFAULT,
    max_meters: float = _GAP_MAX_METERS_DEFAULT,
) -> bool:
    previous_seconds = _parse_iso_seconds(prev_ts)
    current_seconds = _parse_iso_seconds(curr_ts)
    if (
        previous_seconds is not None
        and current_seconds is not None
        and abs(current_seconds - previous_seconds) > max_seconds
    ):
        return True
    if prev_lat is None or prev_lon is None or curr_lat is None or curr_lon is None:
        return False
    return _haversine_m(prev_lat, prev_lon, curr_lat, curr_lon) > max_meters


def _simplify_polyline_rdp(latlons: Sequence[tuple[float, float]], epsilon_m: float) -> list[int]:
    if len(latlons) < _MIN_RDP_POINTS:
        return list(range(len(latlons)))
    projected = _project_polyline_to_xy(latlons)
    keep = [False] * len(latlons)
    keep[0] = True
    keep[-1] = True
    stack: list[tuple[int, int]] = [(0, len(latlons) - 1)]
    while stack:
        start, end = stack.pop()
        farthest = _farthest_point_index(projected, start, end, epsilon_m * epsilon_m)
        if farthest is None:
            continue
        keep[farthest] = True
        stack.append((start, farthest))
        stack.append((farthest, end))
    return [index for index, kept in enumerate(keep) if kept]


def _project_polyline_to_xy(latlons: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    mean_lat = sum(lat for lat, _ in latlons) / len(latlons)
    cos_lat = math.cos(math.radians(mean_lat))
    deg_lat_m = 111_320.0
    deg_lon_m = deg_lat_m * cos_lat
    return [(lon * deg_lon_m, lat * deg_lat_m) for lat, lon in latlons]


def _farthest_point_index(
    projected: Sequence[tuple[float, float]],
    start: int,
    end: int,
    epsilon_sq: float,
) -> int | None:
    if end <= start + 1:
        return None
    max_distance_sq = 0.0
    farthest: int | None = None
    for index in range(start + 1, end):
        distance_sq = _distance_sq_to_segment(projected[index], projected[start], projected[end])
        if distance_sq > max_distance_sq:
            max_distance_sq = distance_sq
            farthest = index
    return farthest if max_distance_sq > epsilon_sq else None


def _distance_sq_to_segment(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    x1, y1 = start
    x2, y2 = end
    px, py = point
    dx = x2 - x1
    dy = y2 - y1
    denom = dx * dx + dy * dy
    if denom == 0.0:
        return (px - x1) ** 2 + (py - y1) ** 2
    numerator = dy * px - dx * py + x2 * y1 - y2 * x1
    return (numerator * numerator) / denom


def _normalized_relative_parts(path_text: str) -> tuple[str, ...]:
    normalized = path_text.replace("\\", "/").lstrip("/")
    parts = tuple(part for part in normalized.split("/") if part)
    if not parts or any(part == ".." for part in parts):
        return ()
    return parts


def _path_exists(path: Path) -> bool:
    return path.is_file()


def _coerce_limit(value: int | None, *, default: int) -> int:
    if value is None or value <= 0:
        return default
    return value


def _coerce_offset(value: int | None) -> int:
    if value is None or value < 0:
        return 0
    return value


def _coerce_min_distance(value: float | None) -> float:
    if value is None or value < 0:
        return 0.0
    return value


def _coerce_epsilon(value: float | None) -> float:
    if value is None or value < 0:
        return 0.0
    return value


def _coerce_max_points(value: int | None) -> int:
    if value is None or value < _MIN_RENDERABLE_POINTS:
        return _MIN_RENDERABLE_POINTS
    return value


def _cutoff_day_string(days: int) -> str:
    return (datetime.now(tz=UTC) - timedelta(days=days)).date().isoformat()


def _percentage(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return part / whole * 100.0


def _events_per_100km(event_count: int, distance_km: float) -> float:
    if distance_km <= 0:
        return 0.0
    return event_count / distance_km * 100.0


def _require_str(row: sqlite3.Row, key: str) -> str:
    value = row[key]
    if isinstance(value, str):
        return value
    raise MappingQueryError(f"Expected {key} to be str, got {type(value).__name__}")


def _optional_str(row: sqlite3.Row, key: str) -> str | None:
    value = row[key]
    if value is None or isinstance(value, str):
        return value
    raise MappingQueryError(f"Expected {key} to be str | None, got {type(value).__name__}")


def _require_int(row: sqlite3.Row, key: str) -> int:
    value = row[key]
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise MappingQueryError(f"Expected {key} to be int, got {type(value).__name__}")


def _optional_int(row: sqlite3.Row, key: str) -> int | None:
    value = row[key]
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise MappingQueryError(f"Expected {key} to be int | None, got {type(value).__name__}")


def _require_number(row: sqlite3.Row, key: str) -> float:
    value = row[key]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise MappingQueryError(f"Expected {key} to be numeric, got {type(value).__name__}")


def _optional_number(row: sqlite3.Row, key: str) -> float | None:
    value = row[key]
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise MappingQueryError(f"Expected {key} to be numeric | None, got {type(value).__name__}")


__all__ = (
    "AllRoutesTrip",
    "ChartCount",
    "DayRouteTrip",
    "DayRow",
    "DrivingStats",
    "EventChartData",
    "EventChartPoint",
    "EventRow",
    "EventTypeCount",
    "FsdTimelinePoint",
    "MappingQueries",
    "MappingQueriesConfig",
    "MappingQueryError",
    "PlayableTrip",
    "RouteWaypoint",
    "SeverityChartPoint",
    "SimplifiedRoutePoint",
    "Stats",
    "TripRow",
    "make_mapping_queries",
)
