"""Read-only query service over the Rust worker's ``index.sqlite3``.

The worker (``teslausb-worker``) is the *only* writer to the
``clips`` and ``waypoints`` tables. This module exposes the
dataclass + method surface the Flask blueprint and analytics
service depend on, deriving trips, events, and sentry rows from
the worker DB on the fly. See :doc:`/adr/0017-mapping-single-source-of-truth`.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from teslausb_web.services.mapping_event_derivation import (
    EVENT_SENTRY,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    DerivedEvent,
    derive_sentry_events,
    derive_trip_events,
    is_autopilot_engaged,
)
from teslausb_web.services.mapping_trip_derivation import (
    AbsoluteWaypoint,
    TripGroup,
    TripMetrics,
    bucket_to_folder,
    cap_indices_uniform,
    compute_trip_metrics,
    epoch_to_iso,
    flatten_trip_waypoints,
    group_trips,
    haversine_km,
    is_gap_between,
    load_clips,
    load_sentry_clips,
    load_waypoints_by_clip,
    simplify_polyline_rdp,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

_TRIP_GAP_SECONDS_DEFAULT: Final[int] = 300
_PLAYABLE_TRIPS_TTL_SECONDS: Final[float] = 60.0
_DEFAULT_TRIP_LIMIT: Final[int] = 50
_DEFAULT_EVENT_LIMIT: Final[int] = 100
_DEFAULT_DAY_LIMIT: Final[int] = 60
_DEFAULT_MIN_DISTANCE_KM: Final[float] = 0.05
_DEFAULT_EPSILON_METERS: Final[float] = 8.0
_DEFAULT_MAX_POINTS_PER_TRIP: Final[int] = 200
_MAX_PLAYABLE_CACHE_ENTRIES: Final[int] = 64
_PLAYABLE_CACHE_TRIMMED_SIZE: Final[int] = 32
_MIN_RENDERABLE_POINTS: Final[int] = 2
_GAP_MAX_SECONDS: Final[float] = 60.0
_GAP_MAX_METERS: Final[float] = 250.0
_CHART_LOOKBACK_DAYS: Final[int] = 30
_MPS_TO_MPH: Final[float] = 2.23694
_KM_TO_MI: Final[float] = 0.621371
_SECONDS_PER_HOUR: Final[float] = 3600.0
_SPEED_DRIVING_MIN_MPS: Final[float] = 0.5
_SEVERITY_PALETTE: Final[dict[str, str]] = {
    SEVERITY_CRITICAL: "#dc3545",
    SEVERITY_WARNING: "#ffc107",
    "info": "#17a2b8",
}


class MappingQueryError(RuntimeError):
    """A mapping query could not be executed or decoded safely."""


@dataclass(frozen=True, slots=True)
class MappingQueriesConfig:
    db_path: Path
    media_root: Path
    trip_gap_seconds: int = _TRIP_GAP_SECONDS_DEFAULT
    playable_trips_ttl_seconds: float = _PLAYABLE_TRIPS_TTL_SECONDS

    def __post_init__(self) -> None:
        if self.trip_gap_seconds <= 0:
            raise ValueError("trip_gap_seconds must be > 0")
        if self.playable_trips_ttl_seconds <= 0:
            raise ValueError("playable_trips_ttl_seconds must be > 0")


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
class TripTelemetryPoint:
    waypoint_id: int
    acceleration_x: float | None
    acceleration_y: float | None
    acceleration_z: float | None
    autopilot_state: str | None


@dataclass(frozen=True, slots=True)
class _PlayableTripsCacheEntry:
    computed_at: float
    payload: tuple[PlayableTrip, ...]


@dataclass(frozen=True, slots=True)
class _MaterialisedTrip:
    """A trip with its waypoints, metrics, and derived events resolved."""

    trip: TripGroup
    waypoints: tuple[AbsoluteWaypoint, ...]
    metrics: TripMetrics
    events: tuple[DerivedEvent, ...]


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """One full read of the worker DB, materialised once per query call."""

    trips: tuple[_MaterialisedTrip, ...]
    sentry_events: tuple[DerivedEvent, ...]


class MappingQueries:
    """Read-only query service over the Rust worker's index DB."""

    def __init__(
        self,
        *,
        config: MappingQueriesConfig,
        migrations_runner: object | None = None,  # Deprecated: worker owns the schema
    ) -> None:
        self._config = config
        del migrations_runner
        self._playable_trips_cache: dict[str, _PlayableTripsCacheEntry] = {}
        self._playable_trips_cache_lock = threading.RLock()

    @contextmanager
    def open_db(self) -> Iterator[sqlite3.Connection]:
        """Open a read-only connection to the worker's index DB."""
        connection = _connect_readonly(self._config.db_path)
        try:
            yield connection
        finally:
            connection.close()

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
        snapshot = self._load_snapshot()
        rows = [_materialised_trip_to_row(item) for item in snapshot.trips]
        filtered = _filter_trip_rows(
            rows,
            bbox=bbox,
            date_from=date_from,
            date_to=date_to,
            min_distance_km=_coerce_min_distance(min_distance_km),
        )
        ordered = sorted(filtered, key=lambda row: row.start_time, reverse=True)
        skip = _coerce_offset(offset)
        cap = _coerce_limit(limit, default=_DEFAULT_TRIP_LIMIT)
        return tuple(ordered[skip : skip + cap])

    def query_trip_route(self, trip_id: int) -> tuple[RouteWaypoint, ...]:
        snapshot = self._load_snapshot()
        materialised = _find_trip(snapshot.trips, trip_id)
        if materialised is None:
            return ()
        return _stamp_gap_waypoints(
            tuple(_route_waypoint_from_entry(entry) for entry in materialised.waypoints)
        )

    def query_trip_telemetry(self, trip_id: int) -> tuple[TripTelemetryPoint, ...]:
        snapshot = self._load_snapshot()
        materialised = _find_trip(snapshot.trips, trip_id)
        if materialised is None:
            return ()
        return tuple(
            TripTelemetryPoint(
                waypoint_id=entry.waypoint.id,
                acceleration_x=entry.waypoint.acceleration_x,
                acceleration_y=entry.waypoint.acceleration_y,
                acceleration_z=entry.waypoint.acceleration_z,
                autopilot_state=entry.waypoint.autopilot_state,
            )
            for entry in materialised.waypoints
        )

    def waypoints_for_video(self, video_path: str) -> tuple[int | None, tuple[RouteWaypoint, ...]]:
        if not video_path:
            return None, ()
        snapshot = self._load_snapshot()
        for materialised in snapshot.trips:
            for clip in materialised.trip.clips:
                if clip.relative_path == video_path:
                    return materialised.trip.id, _stamp_gap_waypoints(
                        tuple(_route_waypoint_from_entry(entry) for entry in materialised.waypoints)
                    )
        return None, ()

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
        snapshot = self._load_snapshot()
        all_events: list[DerivedEvent] = []
        for materialised in snapshot.trips:
            all_events.extend(materialised.events)
        all_events.extend(snapshot.sentry_events)
        rows = [_event_row_from_derived(event) for event in all_events]
        filtered = _filter_event_rows(
            rows,
            event_type=event_type,
            severity=severity,
            bbox=bbox,
            date_from=date_from,
            date_to=date_to,
            date=date,
        )
        ordered = sorted(filtered, key=lambda row: row.timestamp, reverse=True)
        skip = _coerce_offset(offset)
        cap = _coerce_limit(limit, default=_DEFAULT_EVENT_LIMIT)
        return tuple(ordered[skip : skip + cap])

    def query_days(
        self,
        *,
        limit: int | None = _DEFAULT_DAY_LIMIT,
        min_distance_km: float | None = _DEFAULT_MIN_DISTANCE_KM,
    ) -> tuple[DayRow, ...]:
        snapshot = self._load_snapshot()
        threshold = _coerce_min_distance(min_distance_km)
        per_day: dict[str, _DayAccumulator] = {}
        for materialised in snapshot.trips:
            if materialised.metrics.distance_km < threshold:
                continue
            day = _iso_day(materialised.metrics.start_epoch)
            entry = per_day.setdefault(day, _DayAccumulator())
            entry.observe_trip(materialised)
        for materialised in snapshot.trips:
            for event in materialised.events:
                day = event.timestamp[:10]
                entry = per_day.setdefault(day, _DayAccumulator())
                entry.observe_event(event)
        for sentry_event in snapshot.sentry_events:
            day = sentry_event.timestamp[:10]
            entry = per_day.setdefault(day, _DayAccumulator())
            entry.observe_event(sentry_event)
        rows = [accumulator.build(day) for day, accumulator in per_day.items()]
        rows.sort(key=lambda row: row.date, reverse=True)
        cap = _coerce_limit(limit, default=_DEFAULT_DAY_LIMIT)
        return tuple(rows[:cap])

    def query_day_routes(
        self,
        date_str: str,
        *,
        min_distance_km: float | None = _DEFAULT_MIN_DISTANCE_KM,
    ) -> tuple[DayRouteTrip, ...]:
        snapshot = self._load_snapshot()
        threshold = _coerce_min_distance(min_distance_km)
        matches = [
            materialised
            for materialised in snapshot.trips
            if _iso_day(materialised.metrics.start_epoch) == date_str
            and materialised.metrics.distance_km >= threshold
        ]
        matches.sort(key=lambda item: item.metrics.start_epoch, reverse=True)
        return tuple(_day_route_trip_from_materialised(item) for item in matches)

    def query_all_routes_simplified(
        self,
        *,
        min_distance_km: float | None = _DEFAULT_MIN_DISTANCE_KM,
        epsilon_m: float | None = _DEFAULT_EPSILON_METERS,
        max_points_per_trip: int | None = _DEFAULT_MAX_POINTS_PER_TRIP,
    ) -> tuple[AllRoutesTrip, ...]:
        snapshot = self._load_snapshot()
        threshold = _coerce_min_distance(min_distance_km)
        epsilon = _coerce_epsilon(epsilon_m)
        max_points = _coerce_max_points(max_points_per_trip)
        results: list[AllRoutesTrip] = []
        for materialised in snapshot.trips:
            if materialised.metrics.distance_km < threshold:
                continue
            built = _build_simplified_trip(
                materialised, epsilon_m=epsilon, max_points_per_trip=max_points
            )
            if built is not None:
                results.append(built)
        results.sort(key=lambda trip: trip.start_time, reverse=True)
        return tuple(results)

    def playable_trips_for_date(self, date_str: str) -> tuple[PlayableTrip, ...]:
        cached = self._get_playable_trips_cache(date_str)
        if cached is not None:
            return cached
        snapshot = self._load_snapshot()
        file_cache: dict[str, bool] = {}
        results = tuple(
            PlayableTrip(
                id=materialised.trip.id,
                is_playable=_trip_is_playable(
                    materialised.trip, self._config.media_root, file_cache
                ),
            )
            for materialised in snapshot.trips
            if _iso_day(materialised.metrics.start_epoch) == date_str
        )
        self._store_playable_trips_cache(date_str, results)
        return results

    def get_stats(self) -> Stats:
        snapshot = self._load_snapshot()
        clip_counts = self._fetch_clip_counts()
        total_waypoints = self._fetch_waypoint_count()
        total_events = sum(len(item.events) for item in snapshot.trips) + len(
            snapshot.sentry_events
        )
        total_distance = sum(item.metrics.distance_km for item in snapshot.trips)
        total_duration = sum(item.metrics.duration_seconds for item in snapshot.trips)
        breakdown = _event_breakdown(snapshot)
        return Stats(
            trip_count=len(snapshot.trips),
            waypoint_count=total_waypoints,
            event_count=total_events,
            indexed_file_count=clip_counts.total,
            mapped_file_count=clip_counts.with_gps,
            total_distance_km=round(total_distance, 2),
            total_duration_seconds=total_duration,
            event_breakdown=breakdown,
            indexer_status=None,
        )

    def get_driving_stats(self) -> DrivingStats:
        snapshot = self._load_snapshot()
        if not snapshot.trips:
            return DrivingStats(has_data=False)
        total_distance = sum(item.metrics.distance_km for item in snapshot.trips)
        total_duration = sum(item.metrics.duration_seconds for item in snapshot.trips)
        driving_speeds = [
            entry.waypoint.speed_mps
            for item in snapshot.trips
            for entry in item.waypoints
            if entry.waypoint.speed_mps > _SPEED_DRIVING_MIN_MPS
        ]
        avg_speed = sum(driving_speeds) / len(driving_speeds) if driving_speeds else 0.0
        max_speed = max(
            (entry.waypoint.speed_mps for item in snapshot.trips for entry in item.waypoints),
            default=0.0,
        )
        total_waypoints = sum(len(item.waypoints) for item in snapshot.trips)
        fsd_waypoints = sum(
            1
            for item in snapshot.trips
            for entry in item.waypoints
            if is_autopilot_engaged(entry.waypoint.autopilot_state)
        )
        all_events = [event for item in snapshot.trips for event in item.events]
        warning_events = sum(
            1 for event in all_events if event.severity in {SEVERITY_WARNING, SEVERITY_CRITICAL}
        )
        return DrivingStats(
            has_data=True,
            trip_count=len(snapshot.trips),
            total_distance_km=round(total_distance, 1),
            total_distance_mi=round(total_distance * _KM_TO_MI, 1),
            total_duration_hours=round(total_duration / _SECONDS_PER_HOUR, 1),
            avg_speed_mph=round(avg_speed * _MPS_TO_MPH, 1),
            max_speed_mph=round(max_speed * _MPS_TO_MPH, 1),
            fsd_usage_pct=round(_percentage(fsd_waypoints, total_waypoints), 1),
            total_events=len(all_events),
            warning_events=warning_events,
            events_per_100km=round(_events_per_100km(warning_events, total_distance), 1),
        )

    def get_event_chart_data(self) -> EventChartData:
        snapshot = self._load_snapshot()
        cutoff_day = _cutoff_day_string(_CHART_LOOKBACK_DAYS)
        all_events = [event for item in snapshot.trips for event in item.events]
        all_events.extend(snapshot.sentry_events)
        return EventChartData(
            by_type=_chart_by_type(all_events),
            by_severity=_chart_by_severity(all_events),
            over_time=_chart_over_time(all_events, cutoff_day),
            fsd_timeline=_chart_fsd_timeline(snapshot, cutoff_day),
        )

    def reset_playable_trips_cache_for_tests(self) -> None:
        with self._playable_trips_cache_lock:
            self._playable_trips_cache.clear()

    def _load_snapshot(self) -> _Snapshot:
        try:
            with self.open_db() as connection:
                clips = load_clips(connection, require_gps=True)
                sentry_clips = load_sentry_clips(connection)
                clip_ids = [clip.id for trip in group_trips(clips, 0) for clip in trip.clips]
                waypoints_by_clip = load_waypoints_by_clip(connection, clip_ids)
        except sqlite3.Error as exc:
            raise MappingQueryError(f"Failed to read worker DB: {exc}") from exc
        trips = group_trips(clips, self._config.trip_gap_seconds)
        materialised: list[_MaterialisedTrip] = []
        for trip in trips:
            flattened = flatten_trip_waypoints(trip, waypoints_by_clip)
            metrics = compute_trip_metrics(flattened)
            events = derive_trip_events(trip, flattened)
            materialised.append(
                _MaterialisedTrip(
                    trip=trip,
                    waypoints=flattened,
                    metrics=metrics,
                    events=events,
                )
            )
        sentry_events = derive_sentry_events(sentry_clips)
        return _Snapshot(trips=tuple(materialised), sentry_events=sentry_events)

    def _fetch_clip_counts(self) -> _ClipCounts:
        try:
            with self.open_db() as connection:
                row = connection.execute(
                    "SELECT COUNT(*) AS total, "
                    "       SUM(CASE WHEN gps_waypoint_count > 0 THEN 1 ELSE 0 END) AS with_gps "
                    "  FROM clips"
                ).fetchone()
        except sqlite3.Error as exc:
            raise MappingQueryError(f"Failed to count clips: {exc}") from exc
        total = int(row["total"] or 0)
        with_gps = int(row["with_gps"] or 0)
        return _ClipCounts(total=total, with_gps=with_gps)

    def _fetch_waypoint_count(self) -> int:
        try:
            with self.open_db() as connection:
                row = connection.execute("SELECT COUNT(*) AS total FROM waypoints").fetchone()
        except sqlite3.Error as exc:
            raise MappingQueryError(f"Failed to count waypoints: {exc}") from exc
        return int(row["total"] or 0)

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


@dataclass(frozen=True, slots=True)
class _ClipCounts:
    total: int
    with_gps: int


@dataclass(slots=True)
class _DayAccumulator:
    trip_count: int = 0
    total_distance_km: float = 0.0
    event_count: int = 0
    sentry_count: int = 0
    first_start: str | None = None
    last_end: str | None = None

    def observe_trip(self, materialised: _MaterialisedTrip) -> None:
        self.trip_count += 1
        self.total_distance_km += materialised.metrics.distance_km
        start_iso = epoch_to_iso(materialised.metrics.start_epoch)
        end_iso = epoch_to_iso(materialised.metrics.end_epoch)
        if self.first_start is None or start_iso < self.first_start:
            self.first_start = start_iso
        if self.last_end is None or end_iso > self.last_end:
            self.last_end = end_iso

    def observe_event(self, event: DerivedEvent) -> None:
        self.event_count += 1
        if event.event_type == EVENT_SENTRY:
            self.sentry_count += 1

    def build(self, day: str) -> DayRow:
        return DayRow(
            date=day,
            trip_count=self.trip_count,
            total_distance_km=round(self.total_distance_km, 2),
            event_count=self.event_count,
            sentry_count=self.sentry_count,
            first_start=self.first_start,
            last_end=self.last_end,
        )


def make_mapping_queries(cfg: WebConfig | MappingQueriesConfig) -> MappingQueries:
    """Build the mapping query service from app config or explicit settings."""
    if isinstance(cfg, MappingQueriesConfig):
        return MappingQueries(config=cfg)
    return MappingQueries(
        config=MappingQueriesConfig(
            db_path=cfg.mapping.db_path,
            media_root=cfg.paths.backing_root,
            trip_gap_seconds=cfg.mapping.trip_gap_minutes * 60,
            playable_trips_ttl_seconds=_PLAYABLE_TRIPS_TTL_SECONDS,
        ),
    )


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise MappingQueryError(f"Failed to open worker DB at {db_path}: {exc}") from exc
    connection.row_factory = sqlite3.Row
    return connection


def _materialised_trip_to_row(materialised: _MaterialisedTrip) -> TripRow:
    metrics = materialised.metrics
    first_clip = materialised.trip.first_clip
    return TripRow(
        id=materialised.trip.id,
        start_time=epoch_to_iso(metrics.start_epoch),
        end_time=epoch_to_iso(metrics.end_epoch),
        start_lat=metrics.start_lat,
        start_lon=metrics.start_lon,
        end_lat=metrics.end_lat,
        end_lon=metrics.end_lon,
        distance_km=round(metrics.distance_km, 3),
        duration_seconds=metrics.duration_seconds,
        source_folder=bucket_to_folder(first_clip.bucket),
        indexed_at=epoch_to_iso(first_clip.indexed_at_utc),
        event_count=len(materialised.events),
        video_count=len(materialised.trip.clips),
    )


def _filter_trip_rows(
    rows: Sequence[TripRow],
    *,
    bbox: tuple[float, float, float, float] | None,
    date_from: str | None,
    date_to: str | None,
    min_distance_km: float,
) -> list[TripRow]:
    out: list[TripRow] = []
    for row in rows:
        if row.distance_km < min_distance_km:
            continue
        if date_from is not None and row.start_time < date_from:
            continue
        if date_to is not None and row.start_time > date_to:
            continue
        if bbox is not None and not _trip_row_in_bbox(row, bbox):
            continue
        out.append(row)
    return out


def _trip_row_in_bbox(row: TripRow, bbox: tuple[float, float, float, float]) -> bool:
    min_lat, min_lon, max_lat, max_lon = bbox
    if row.start_lat is None or row.start_lon is None:
        return False
    return min_lat <= row.start_lat <= max_lat and min_lon <= row.start_lon <= max_lon


def _filter_event_rows(  # noqa: PLR0913
    rows: Sequence[EventRow],
    *,
    event_type: str | None,
    severity: str | None,
    bbox: tuple[float, float, float, float] | None,
    date_from: str | None,
    date_to: str | None,
    date: str | None,
) -> list[EventRow]:
    out: list[EventRow] = []
    for row in rows:
        if event_type is not None and row.event_type != event_type:
            continue
        if severity is not None and row.severity != severity:
            continue
        if date is not None and row.timestamp[:10] != date:
            continue
        if date_from is not None and row.timestamp < date_from:
            continue
        if date_to is not None and row.timestamp > date_to:
            continue
        if bbox is not None and not _event_row_in_bbox(row, bbox):
            continue
        out.append(row)
    return out


def _event_row_in_bbox(row: EventRow, bbox: tuple[float, float, float, float]) -> bool:
    min_lat, min_lon, max_lat, max_lon = bbox
    if row.lat is None or row.lon is None:
        return False
    return min_lat <= row.lat <= max_lat and min_lon <= row.lon <= max_lon


def _route_waypoint_from_entry(entry: AbsoluteWaypoint) -> RouteWaypoint:
    return RouteWaypoint(
        id=entry.waypoint.id,
        timestamp=entry.iso_timestamp,
        lat=entry.waypoint.latitude_deg,
        lon=entry.waypoint.longitude_deg,
        heading=entry.waypoint.heading_deg,
        speed_mps=entry.waypoint.speed_mps,
        autopilot_state=entry.waypoint.autopilot_state,
        video_path=entry.clip.relative_path,
        frame_offset=entry.waypoint.frame_index,
    )


def _event_row_from_derived(event: DerivedEvent) -> EventRow:
    return EventRow(
        id=event.id,
        trip_id=event.trip_id,
        timestamp=event.timestamp,
        lat=event.lat,
        lon=event.lon,
        event_type=event.event_type,
        severity=event.severity,
        description=event.description,
        video_path=event.video_path,
        frame_offset=event.frame_offset,
        metadata=None,
    )


def _stamp_gap_waypoints(waypoints: Sequence[RouteWaypoint]) -> tuple[RouteWaypoint, ...]:
    stamped = list(waypoints)
    for index in range(len(stamped) - 1):
        if _waypoints_have_gap(stamped[index], stamped[index + 1]):
            stamped[index] = replace(stamped[index], gap_after=True)
    return tuple(stamped)


def _waypoints_have_gap(current: RouteWaypoint, nxt: RouteWaypoint) -> bool:
    current_epoch = _parse_iso_seconds(current.timestamp)
    next_epoch = _parse_iso_seconds(nxt.timestamp)
    if current_epoch is None or next_epoch is None:
        return False
    delta_seconds = abs(next_epoch - current_epoch)
    if delta_seconds > _GAP_MAX_SECONDS:
        return True
    return haversine_km(current.lat, current.lon, nxt.lat, nxt.lon) * 1000.0 > _GAP_MAX_METERS


def _parse_iso_seconds(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    candidate = f"{timestamp[:-1]}+00:00" if timestamp.endswith("Z") else timestamp
    try:
        return datetime.fromisoformat(candidate).timestamp()
    except ValueError:
        return None


def _find_trip(
    materialised_trips: Sequence[_MaterialisedTrip], trip_id: int
) -> _MaterialisedTrip | None:
    for item in materialised_trips:
        if item.trip.id == trip_id:
            return item
    return None


def _day_route_trip_from_materialised(materialised: _MaterialisedTrip) -> DayRouteTrip:
    waypoints = _stamp_gap_waypoints(
        tuple(_route_waypoint_from_entry(entry) for entry in materialised.waypoints)
    )
    metrics = materialised.metrics
    return DayRouteTrip(
        id=materialised.trip.id,
        start_time=epoch_to_iso(metrics.start_epoch),
        end_time=epoch_to_iso(metrics.end_epoch),
        distance_km=round(metrics.distance_km, 3),
        duration_seconds=metrics.duration_seconds,
        start_lat=metrics.start_lat,
        start_lon=metrics.start_lon,
        end_lat=metrics.end_lat,
        end_lon=metrics.end_lon,
        source_folder=bucket_to_folder(materialised.trip.first_clip.bucket),
        waypoints=waypoints,
    )


def _build_simplified_trip(
    materialised: _MaterialisedTrip,
    *,
    epsilon_m: float,
    max_points_per_trip: int,
) -> AllRoutesTrip | None:
    if len(materialised.waypoints) < _MIN_RENDERABLE_POINTS:
        return None
    segments = _split_segments_by_gap(materialised.waypoints)
    simplified_points = _simplify_segments(segments, epsilon_m)
    if len(simplified_points) < _MIN_RENDERABLE_POINTS:
        return None
    capped = _cap_simplified_points(simplified_points, max_points_per_trip)
    metrics = materialised.metrics
    return AllRoutesTrip(
        id=materialised.trip.id,
        date=_iso_day(metrics.start_epoch),
        start_time=epoch_to_iso(metrics.start_epoch),
        end_time=epoch_to_iso(metrics.end_epoch),
        start_lat=metrics.start_lat,
        start_lon=metrics.start_lon,
        end_lat=metrics.end_lat,
        end_lon=metrics.end_lon,
        distance_km=round(metrics.distance_km, 3),
        duration_seconds=metrics.duration_seconds,
        waypoints=capped,
    )


def _split_segments_by_gap(
    waypoints: Sequence[AbsoluteWaypoint],
) -> tuple[tuple[AbsoluteWaypoint, ...], ...]:
    current: list[AbsoluteWaypoint] = [waypoints[0]]
    segments: list[tuple[AbsoluteWaypoint, ...]] = []
    for index in range(1, len(waypoints)):
        previous = waypoints[index - 1]
        point = waypoints[index]
        if is_gap_between(previous, point):
            segments.append(tuple(current))
            current = [point]
        else:
            current.append(point)
    segments.append(tuple(current))
    return tuple(segments)


def _simplify_segments(
    segments: Sequence[Sequence[AbsoluteWaypoint]],
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
    segment: Sequence[AbsoluteWaypoint], epsilon_m: float
) -> list[SimplifiedRoutePoint]:
    if len(segment) == 1:
        wp = segment[0].waypoint
        return [
            SimplifiedRoutePoint(lat=wp.latitude_deg, lon=wp.longitude_deg, speed_mps=wp.speed_mps)
        ]
    indices = simplify_polyline_rdp(
        [(entry.waypoint.latitude_deg, entry.waypoint.longitude_deg) for entry in segment],
        epsilon_m,
    )
    return [
        SimplifiedRoutePoint(
            lat=segment[index].waypoint.latitude_deg,
            lon=segment[index].waypoint.longitude_deg,
            speed_mps=segment[index].waypoint.speed_mps,
        )
        for index in indices
    ]


def _cap_simplified_points(
    points: tuple[SimplifiedRoutePoint, ...], max_points_per_trip: int
) -> tuple[SimplifiedRoutePoint, ...]:
    if len(points) <= max_points_per_trip:
        return points
    indices = cap_indices_uniform(list(range(len(points))), max_points_per_trip)
    return tuple(points[index] for index in indices)


def _trip_is_playable(trip: TripGroup, media_root: Path, file_cache: dict[str, bool]) -> bool:
    for clip in trip.clips:
        cached = file_cache.get(clip.relative_path)
        if cached is None:
            cached = _video_path_exists(clip.relative_path, media_root)
            file_cache[clip.relative_path] = cached
        if cached:
            return True
    return False


def _video_path_exists(relative_path: str, media_root: Path) -> bool:
    parts = _normalized_relative_parts(relative_path)
    if not parts:
        return False
    return media_root.joinpath(*parts).is_file()


def _normalized_relative_parts(path_text: str) -> tuple[str, ...]:
    normalized = path_text.replace("\\", "/").lstrip("/")
    parts = tuple(part for part in normalized.split("/") if part)
    if not parts or any(part == ".." for part in parts):
        return ()
    return parts


def _event_breakdown(snapshot: _Snapshot) -> tuple[EventTypeCount, ...]:
    counts: dict[str, int] = {}
    for materialised in snapshot.trips:
        for event in materialised.events:
            counts[event.event_type] = counts.get(event.event_type, 0) + 1
    for event in snapshot.sentry_events:
        counts[event.event_type] = counts.get(event.event_type, 0) + 1
    return tuple(
        EventTypeCount(event_type=event_type, count=count)
        for event_type, count in sorted(counts.items())
    )


def _chart_by_type(events: Sequence[DerivedEvent]) -> tuple[ChartCount, ...]:
    counts: dict[str, int] = {}
    for event in events:
        counts[event.event_type] = counts.get(event.event_type, 0) + 1
    items = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(ChartCount(label=_title_case_label(name), value=count) for name, count in items)


def _chart_by_severity(events: Sequence[DerivedEvent]) -> tuple[SeverityChartPoint, ...]:
    counts: dict[str, int] = {}
    for event in events:
        counts[event.severity] = counts.get(event.severity, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (_severity_sort_key(item[0]), item[0]))
    return tuple(
        SeverityChartPoint(
            severity=severity,
            value=value,
            color=_SEVERITY_PALETTE.get(severity, _SEVERITY_PALETTE["info"]),
        )
        for severity, value in ordered
    )


def _chart_over_time(
    events: Sequence[DerivedEvent], cutoff_day: str
) -> tuple[EventChartPoint, ...]:
    counts: dict[str, int] = {}
    for event in events:
        day = event.timestamp[:10]
        if day < cutoff_day:
            continue
        counts[day] = counts.get(day, 0) + 1
    return tuple(EventChartPoint(day=day, value=value) for day, value in sorted(counts.items()))


def _chart_fsd_timeline(snapshot: _Snapshot, cutoff_day: str) -> tuple[FsdTimelinePoint, ...]:
    fsd: dict[str, int] = {}
    manual: dict[str, int] = {}
    for materialised in snapshot.trips:
        for entry in materialised.waypoints:
            day = entry.iso_timestamp[:10]
            if day < cutoff_day:
                continue
            if is_autopilot_engaged(entry.waypoint.autopilot_state):
                fsd[day] = fsd.get(day, 0) + 1
            else:
                manual[day] = manual.get(day, 0) + 1
    days = sorted({*fsd, *manual})
    return tuple(
        FsdTimelinePoint(day=day, fsd=fsd.get(day, 0), manual=manual.get(day, 0)) for day in days
    )


def _severity_sort_key(severity: str) -> int:
    return {SEVERITY_CRITICAL: 1, SEVERITY_WARNING: 2}.get(severity, 3)


def _title_case_label(label: str) -> str:
    return label.replace("_", " ").title()


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


def _iso_day(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).date().isoformat()


def _percentage(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return part / whole * 100.0


def _events_per_100km(event_count: int, distance_km: float) -> float:
    if distance_km <= 0:
        return 0.0
    return event_count / distance_km * 100.0


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
    "TripTelemetryPoint",
    "make_mapping_queries",
)
