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
    WorkerClip,
    bucket_to_folder,
    cap_indices_uniform,
    compute_trip_metrics,
    epoch_to_iso,
    flatten_trip_waypoints,
    group_trips,
    haversine_km,
    is_gap_between,
    load_clips,
    load_clips_for_trip,
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
_SNAPSHOT_CACHE_TTL_SECONDS: Final[float] = 60.0
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
class DayPayload:
    """Single-shot payload for the map page: trips + events for one date.

    Built directly from materialised SQL tables; no global snapshot. The
    `latest_date` field is returned so the UI can offer a one-click jump to
    the most recent recorded day when the user navigated to an empty date.
    """

    date: str
    trips: tuple[DayRouteTrip, ...]
    events: tuple[EventRow, ...]
    latest_date: str | None


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


@dataclass(frozen=True, slots=True)
class _SnapshotCacheEntry:
    """A cached snapshot keyed by the worker DB's mtime+size."""

    db_mtime_ns: int
    db_size: int
    computed_at: float
    snapshot: _Snapshot


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
        self._snapshot_cache: _SnapshotCacheEntry | None = None
        self._snapshot_lock = threading.RLock()
        # date -> (mtime_ns, size, computed_at, payload). We serve from
        # cache while the DB is unchanged AND while the cached entry is
        # younger than the TTL even if the DB has rolled forward — the
        # worker writes constantly during recording, so mtime-only
        # invalidation would defeat the cache. The TTL bounds staleness.
        self._day_payload_cache: dict[
            str, tuple[int, int, float, DayPayload]
        ] = {}
        self._day_payload_lock = threading.RLock()

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
        # Direct per-trip SQL path: snapshot rebuilds load ~95K waypoints
        # across every trip; a single-trip route page only ever needs the
        # waypoints belonging to ONE trip. Going via the snapshot turned
        # this endpoint into 10-80 s under load. Direct query is O(N_trip)
        # and uses the (clip_trip_map_by_trip, waypoints_by_clip) indexes.
        entries = self._load_trip_waypoints_direct(trip_id)
        if entries is None:
            # Materialised tables not present (older DB / pre-bootstrap);
            # fall back to the snapshot path.
            snapshot = self._load_snapshot()
            materialised = _find_trip(snapshot.trips, trip_id)
            if materialised is None:
                return ()
            entries = materialised.waypoints
        if not entries:
            return ()
        return _stamp_gap_waypoints(tuple(_route_waypoint_from_entry(entry) for entry in entries))

    def query_trip_telemetry(self, trip_id: int) -> tuple[TripTelemetryPoint, ...]:
        # Direct per-trip SQL path; see query_trip_route for rationale.
        entries = self._load_trip_waypoints_direct(trip_id)
        if entries is None:
            snapshot = self._load_snapshot()
            materialised = _find_trip(snapshot.trips, trip_id)
            if materialised is None:
                return ()
            entries = materialised.waypoints
        return tuple(
            TripTelemetryPoint(
                waypoint_id=entry.waypoint.id,
                acceleration_x=entry.waypoint.acceleration_x,
                acceleration_y=entry.waypoint.acceleration_y,
                acceleration_z=entry.waypoint.acceleration_z,
                autopilot_state=entry.waypoint.autopilot_state,
            )
            for entry in entries
        )

    def _load_trip_waypoints_direct(self, trip_id: int) -> tuple[AbsoluteWaypoint, ...] | None:
        """Return one trip's waypoints in absolute-time order via direct SQL.

        Bypasses the global snapshot. Loads only the clips for the requested
        trip (`clip_trip_map_by_trip` index) and only those clips' waypoints
        (`waypoints_by_clip` index).

        ``trip_id`` follows the public-API contract preserved by
        ``_snapshot_from_materialised``: it is the *first clip's id* of the
        trip, not the materialised ``trips.id``. We resolve it via
        ``clip_trip_map`` (clip_id -> trip_id) before loading the trip's
        clips.

        Returns ``None`` when the `clip_trip_map` table is missing (older
        DB schema or pre-bootstrap) — the caller falls back to the snapshot
        path so behaviour is preserved. Returns ``()`` when the table exists
        but the api-id resolves to no clips (genuinely unknown trip).
        """
        try:
            with self.open_db() as connection:
                row = connection.execute(
                    "SELECT trip_id FROM clip_trip_map WHERE clip_id = ?",
                    (trip_id,),
                ).fetchone()
                if row is None:
                    return ()
                real_trip_id = int(row[0])
                clips = load_clips_for_trip(connection, real_trip_id)
                if not clips:
                    return ()
                waypoints_by_clip = load_waypoints_by_clip(connection, [clip.id for clip in clips])
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise MappingQueryError(f"Failed to load trip waypoints: {exc}") from exc
        except sqlite3.Error as exc:
            raise MappingQueryError(f"Failed to load trip waypoints: {exc}") from exc
        # Preserve the legacy TripGroup.id == first_clip.id contract so that
        # downstream gap-stamping and event-matching keys are unchanged.
        trip = TripGroup(id=clips[0].id, clips=clips)
        return flatten_trip_waypoints(trip, waypoints_by_clip)

    def query_latest_date(self) -> str | None:
        """Return the most-recent trip's UTC date as ``YYYY-MM-DD``, or None.

        Trivially cheap (single MAX() on `trips.start_utc`). Used by the
        page route to redirect bare ``/`` requests to the latest day so the
        user lands directly on rendered data instead of an empty map.
        """
        try:
            with self.open_db() as connection:
                row = connection.execute(
                    "SELECT date(MAX(start_utc), 'unixepoch') FROM trips",
                ).fetchone()
        except sqlite3.OperationalError:
            return None
        except sqlite3.Error as exc:
            raise MappingQueryError(f"Failed to query latest date: {exc}") from exc
        if row is None or row[0] is None:
            return None
        return str(row[0])

    def query_day_payload(self, date_str: str) -> DayPayload:
        """Cached single-day payload (RDP-simplified + cap)."""
        mtime_ns, size = self._stat_db()
        now = time.monotonic()
        with self._day_payload_lock:
            cached = self._day_payload_cache.get(date_str)
            if cached is not None:
                cached_mtime, cached_size, computed_at, payload = cached
                same_db = cached_mtime == mtime_ns and cached_size == size
                fresh = (now - computed_at) < _SNAPSHOT_CACHE_TTL_SECONDS
                if same_db or fresh:
                    return payload
        payload = self._compute_day_payload(date_str)
        with self._day_payload_lock:
            self._day_payload_cache[date_str] = (
                mtime_ns,
                size,
                time.monotonic(),
                payload,
            )
        return payload

    def _compute_day_payload(self, date_str: str) -> DayPayload:
        """Return trips + events for a single date via direct SQL.

        This is the fast path the map page uses on initial load: ONE
        round-trip to the DB, only the requested day's data, no global
        snapshot. Uses the existing ``trips_by_start_utc`` and
        ``clip_trip_map_by_trip`` and ``events_by_ts`` indexes.

        Returns an empty ``DayPayload`` (still populated with
        ``latest_date``) when the date has no trips so the front-end can
        render an empty state.

        Falls back to the snapshot path when the materialised ``trips``
        table is missing (older DBs / pre-bootstrap test fixtures) so
        existing tests and recovery flows continue to work.
        """
        try:
            with self.open_db() as connection:
                trip_rows = connection.execute(
                    "SELECT id, start_utc, end_utc, start_clip_id, end_clip_id, "
                    "       start_lat, start_lon, end_lat, end_lon, "
                    "       distance_km, duration_seconds, video_count, bucket "
                    "  FROM trips "
                    " WHERE date(start_utc, 'unixepoch') = ? "
                    " ORDER BY start_utc DESC, id DESC",
                    (date_str,),
                ).fetchall()
                latest = self._latest_date_locked(connection)
                if not trip_rows:
                    # No trips for this day; still load any standalone
                    # events recorded against the date (e.g. sentry-only
                    # day) so the front-end can show event markers even
                    # without trip routes.
                    event_rows = connection.execute(
                        "SELECT id, trip_id, clip_id, event_type, severity, "
                        "       timestamp_utc, latitude_deg, longitude_deg, "
                        "       description, frame_index "
                        "  FROM detected_events "
                        " WHERE date(timestamp_utc, 'unixepoch') = ? "
                        " ORDER BY timestamp_utc DESC, id DESC",
                        (date_str,),
                    ).fetchall()
                    events = self._build_events_for_day({}, event_rows)
                    return DayPayload(
                        date=date_str,
                        trips=(),
                        events=events,
                        latest_date=latest,
                    )
                real_trip_ids = [int(r["id"]) for r in trip_rows]
                placeholders = ",".join("?" for _ in real_trip_ids)
                clip_rows = connection.execute(
                    "SELECT c.id, c.relative_path, c.bucket, c.clip_started_utc, "  # noqa: S608
                    "       c.indexed_at_utc, c.gps_waypoint_count, m.trip_id "
                    "  FROM clips c "
                    "  JOIN clip_trip_map m ON m.clip_id = c.id "
                    f" WHERE m.trip_id IN ({placeholders}) "
                    " ORDER BY m.trip_id, c.clip_started_utc ASC, c.id ASC",
                    real_trip_ids,
                ).fetchall()
                clips_by_trip: dict[int, list[WorkerClip]] = {}
                all_clip_ids: list[int] = []
                for row in clip_rows:
                    tid = int(row["trip_id"])
                    clip = WorkerClip(
                        id=int(row["id"]),
                        relative_path=str(row["relative_path"]),
                        bucket=str(row["bucket"]),
                        clip_started_utc=int(row["clip_started_utc"]),
                        indexed_at_utc=int(row["indexed_at_utc"]),
                        gps_waypoint_count=int(row["gps_waypoint_count"]),
                    )
                    clips_by_trip.setdefault(tid, []).append(clip)
                    all_clip_ids.append(clip.id)
                waypoints_by_clip = load_waypoints_by_clip(connection, all_clip_ids)
                event_rows = connection.execute(
                    "SELECT id, trip_id, clip_id, event_type, severity, "
                    "       timestamp_utc, latitude_deg, longitude_deg, "
                    "       description, frame_index "
                    "  FROM detected_events "
                    " WHERE date(timestamp_utc, 'unixepoch') = ? "
                    " ORDER BY timestamp_utc DESC, id DESC",
                    (date_str,),
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return self._day_payload_via_snapshot(date_str)
            raise MappingQueryError(f"Failed to load day payload: {exc}") from exc
        except sqlite3.Error as exc:
            raise MappingQueryError(f"Failed to load day payload: {exc}") from exc
        trips_out: list[DayRouteTrip] = []
        for row in trip_rows:
            tid = int(row["id"])
            trip_clips_list = clips_by_trip.get(tid, [])
            if not trip_clips_list:
                continue
            trip_clips = tuple(trip_clips_list)
            trip_group = TripGroup(id=trip_clips[0].id, clips=trip_clips)
            flattened = flatten_trip_waypoints(trip_group, waypoints_by_clip)
            metrics = _metrics_from_trip_row(row, flattened)
            stamped = _simplified_route_waypoints(
                flattened,
                epsilon_m=_DEFAULT_EPSILON_METERS,
                max_points=_DEFAULT_MAX_POINTS_PER_TRIP,
            )
            trips_out.append(
                DayRouteTrip(
                    id=trip_clips[0].id,  # legacy: first-clip-id contract
                    start_time=epoch_to_iso(metrics.start_epoch),
                    end_time=epoch_to_iso(metrics.end_epoch),
                    distance_km=round(metrics.distance_km, 3),
                    duration_seconds=metrics.duration_seconds,
                    start_lat=metrics.start_lat,
                    start_lon=metrics.start_lon,
                    end_lat=metrics.end_lat,
                    end_lon=metrics.end_lon,
                    source_folder=bucket_to_folder(trip_clips[0].bucket),
                    waypoints=stamped,
                ),
            )
        events_out = self._build_events_for_day(clips_by_trip, event_rows)
        return DayPayload(
            date=date_str,
            trips=tuple(trips_out),
            events=events_out,
            latest_date=latest,
        )

    @staticmethod
    def _latest_date_locked(connection: sqlite3.Connection) -> str | None:
        row = connection.execute(
            "SELECT date(MAX(start_utc), 'unixepoch') FROM trips",
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return str(row[0])

    @staticmethod
    def _build_events_for_day(
        clips_by_trip: dict[int, list[WorkerClip]],
        event_rows: Sequence[sqlite3.Row],
    ) -> tuple[EventRow, ...]:
        """Convert raw event rows into EventRow tuples for the day.

        Maps the materialised ``trips.id`` back to the legacy public-API
        trip id (first clip's id), and resolves ``video_path`` from the
        event's ``clip_id`` against the per-trip clips already loaded.
        """
        out: list[EventRow] = []
        for row in event_rows:
            real_trip_id = int(row["trip_id"]) if row["trip_id"] is not None else None
            trip_clips_for_event: tuple[WorkerClip, ...] = ()
            legacy_trip_id: int | None = None
            if real_trip_id is not None:
                clips_for_trip = clips_by_trip.get(real_trip_id, [])
                trip_clips_for_event = tuple(clips_for_trip)
                if clips_for_trip:
                    legacy_trip_id = clips_for_trip[0].id
            derived = _derived_event_from_row(row, trip_clips_for_event)
            out.append(
                EventRow(
                    id=derived.id,
                    trip_id=legacy_trip_id,
                    timestamp=derived.timestamp,
                    lat=derived.lat,
                    lon=derived.lon,
                    event_type=derived.event_type,
                    severity=derived.severity,
                    description=derived.description,
                    video_path=derived.video_path,
                    frame_offset=derived.frame_offset,
                    metadata=None,
                ),
            )
        return tuple(out)

    def _day_payload_via_snapshot(self, date_str: str) -> DayPayload:
        """Fallback day-payload path for older DB schemas / test fixtures."""
        trips = self.query_day_routes(date_str, min_distance_km=0.0)
        events = self.query_events(date=date_str, limit=5000)
        days = self.query_days(limit=1, min_distance_km=0.0)
        latest = days[0].date if days else None
        return DayPayload(date=date_str, trips=trips, events=events, latest_date=latest)

    def waypoints_for_video(self, video_path: str) -> tuple[int | None, tuple[RouteWaypoint, ...]]:
        if not video_path:
            return None, ()
        target = _video_url_path(video_path)
        snapshot = self._load_snapshot()
        for materialised in snapshot.trips:
            for clip in materialised.trip.clips:
                if _video_url_path(clip.relative_path) == target:
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
        mtime_ns, size = self._stat_db()
        now = time.monotonic()
        with self._snapshot_lock:
            cached = self._snapshot_cache
            if (
                cached is not None
                and cached.db_mtime_ns == mtime_ns
                and cached.db_size == size
                and (now - cached.computed_at) < _SNAPSHOT_CACHE_TTL_SECONDS
            ):
                return cached.snapshot
        snapshot = self._build_snapshot()
        with self._snapshot_lock:
            # Re-stat under the lock so we record the mtime that was
            # actually observed when the snapshot was built. If another
            # caller raced ahead with a newer snapshot, keep theirs.
            current = self._snapshot_cache
            if current is not None and current.computed_at > now:
                return current.snapshot
            self._snapshot_cache = _SnapshotCacheEntry(
                db_mtime_ns=mtime_ns,
                db_size=size,
                computed_at=time.monotonic(),
                snapshot=snapshot,
            )
        return snapshot

    def _stat_db(self) -> tuple[int, int]:
        try:
            stat = self._config.db_path.stat()
        except OSError:
            return (0, 0)
        return (stat.st_mtime_ns, stat.st_size)

    def _build_snapshot(self) -> _Snapshot:
        try:
            with self.open_db() as connection:
                clips = load_clips(connection, require_gps=True)
                sentry_clips = load_sentry_clips(connection)
                clip_ids = [clip.id for trip in group_trips(clips, 0) for clip in trip.clips]
                waypoints_by_clip = load_waypoints_by_clip(connection, clip_ids)
                materialised_data = _load_materialised(connection)
        except sqlite3.Error as exc:
            raise MappingQueryError(f"Failed to read worker DB: {exc}") from exc
        if materialised_data is not None:
            return _snapshot_from_materialised(
                clips=clips,
                sentry_clips=sentry_clips,
                waypoints_by_clip=waypoints_by_clip,
                trip_rows=materialised_data.trips,
                trip_clip_map=materialised_data.trip_clip_map,
                trip_events=materialised_data.trip_events,
                sentry_events=materialised_data.sentry_events,
            )
        logger.warning(
            "materialised trips/events tables empty; falling back to Python "
            "derivation (worker bootstrap likely still running)",
        )
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

    def reset_snapshot_cache_for_tests(self) -> None:
        """Force the next `_load_snapshot` to rebuild from the DB."""
        with self._snapshot_lock:
            self._snapshot_cache = None

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


@dataclass(frozen=True, slots=True)
class _MaterialisedData:
    """Raw rows pulled from the worker's derived tables."""

    trips: tuple[sqlite3.Row, ...]
    trip_clip_map: dict[int, list[int]]
    trip_events: dict[int, list[sqlite3.Row]]
    sentry_events: tuple[sqlite3.Row, ...]


def _load_materialised(connection: sqlite3.Connection) -> _MaterialisedData | None:
    """Read the worker-materialised trips/events tables.

    Returns ``None`` if the ``trips`` table is empty or absent (signals
    the caller to fall back to Python derivation). The "absent" case
    happens in older test fixtures that only seed the v2-era
    ``clips``/``waypoints`` schema.
    """
    has_trips = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trips'",
    ).fetchone()
    if has_trips is None:
        return None
    trip_rows = connection.execute(
        "SELECT id, start_utc, end_utc, start_clip_id, end_clip_id, "
        "       start_lat, start_lon, end_lat, end_lon, "
        "       distance_km, duration_seconds, video_count, bucket "
        "  FROM trips ORDER BY start_utc ASC, id ASC",
    ).fetchall()
    if not trip_rows:
        return None
    trip_clip_map: dict[int, list[int]] = {}
    for row in connection.execute(
        "SELECT clip_id, trip_id FROM clip_trip_map ORDER BY trip_id, clip_id",
    ):
        trip_clip_map.setdefault(int(row["trip_id"]), []).append(int(row["clip_id"]))
    trip_events: dict[int, list[sqlite3.Row]] = {}
    for row in connection.execute(
        "SELECT id, trip_id, clip_id, event_type, severity, timestamp_utc, "
        "       latitude_deg, longitude_deg, description, frame_index "
        "  FROM detected_events "
        " WHERE trip_id IS NOT NULL "
        " ORDER BY trip_id, timestamp_utc, id",
    ):
        trip_events.setdefault(int(row["trip_id"]), []).append(row)
    sentry_events = tuple(
        connection.execute(
            "SELECT id, clip_id, event_type, severity, timestamp_utc, "
            "       latitude_deg, longitude_deg, description "
            "  FROM detected_events "
            " WHERE trip_id IS NULL AND event_type = 'sentry' "
            " ORDER BY timestamp_utc DESC, id DESC",
        ).fetchall(),
    )
    return _MaterialisedData(
        trips=tuple(trip_rows),
        trip_clip_map=trip_clip_map,
        trip_events=trip_events,
        sentry_events=sentry_events,
    )


def _snapshot_from_materialised(  # noqa: PLR0913
    *,
    clips: Sequence,
    sentry_clips: Sequence,
    waypoints_by_clip: dict,
    trip_rows: Sequence[sqlite3.Row],
    trip_clip_map: dict[int, list[int]],
    trip_events: dict[int, list[sqlite3.Row]],
    sentry_events: Sequence[sqlite3.Row],
) -> _Snapshot:
    """Build a `_Snapshot` from materialised rows; skip Python derivation."""
    clip_by_id = {clip.id: clip for clip in clips}
    sentry_clip_by_id = {clip.id: clip for clip in sentry_clips}
    materialised: list[_MaterialisedTrip] = []
    for trip_row in trip_rows:
        trip_id = int(trip_row["id"])
        clip_ids_for_trip = trip_clip_map.get(trip_id, [])
        trip_clips = tuple(clip_by_id[cid] for cid in clip_ids_for_trip if cid in clip_by_id)
        if not trip_clips:
            continue
        # Python's TripGroup.id is the first clip id (legacy); preserve
        # that contract so downstream code (chart keys, day routes) is
        # unchanged.
        trip_group = TripGroup(id=trip_clips[0].id, clips=trip_clips)
        flattened = flatten_trip_waypoints(trip_group, waypoints_by_clip)
        metrics = _metrics_from_trip_row(trip_row, flattened)
        events = tuple(
            _derived_event_from_row(row, trip_clips) for row in trip_events.get(trip_id, ())
        )
        materialised.append(
            _MaterialisedTrip(
                trip=trip_group,
                waypoints=flattened,
                metrics=metrics,
                events=events,
            ),
        )
    sentry_derived = tuple(
        _derived_event_from_row(row, (sentry_clip_by_id.get(int(row["clip_id"])),))
        for row in sentry_events
        if int(row["clip_id"]) in sentry_clip_by_id
    )
    return _Snapshot(trips=tuple(materialised), sentry_events=sentry_derived)


def _metrics_from_trip_row(
    row: sqlite3.Row,
    flattened: Sequence[AbsoluteWaypoint],
) -> TripMetrics:
    start_epoch = float(row["start_utc"])
    end_epoch = float(row["end_utc"])
    start_lat = row["start_lat"]
    start_lon = row["start_lon"]
    end_lat = row["end_lat"]
    end_lon = row["end_lon"]
    # If the worker hadn't materialised endpoint lat/lon yet (older row),
    # fall back to the flattened waypoints. Cheap because we already
    # have the list.
    if start_lat is None and flattened:
        start_lat = flattened[0].waypoint.latitude_deg
        start_lon = flattened[0].waypoint.longitude_deg
    if end_lat is None and flattened:
        end_lat = flattened[-1].waypoint.latitude_deg
        end_lon = flattened[-1].waypoint.longitude_deg
    return TripMetrics(
        distance_km=float(row["distance_km"] or 0.0),
        duration_seconds=int(row["duration_seconds"] or 0),
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        start_lat=start_lat,
        start_lon=start_lon,
        end_lat=end_lat,
        end_lon=end_lon,
    )


def _video_url_path(relative_path: str | None) -> str | None:
    """Strip the leading ``TeslaCam/`` segment for URL emission.

    Worker stores ``relative_path`` rooted at ``backing_root`` so
    every clip path begins with ``TeslaCam/``. The videos blueprint,
    however, allow-lists ``backing_root/TeslaCam`` as its single
    root and joins the URL ``<path:filepath>`` underneath it — so
    sending the raw DB value would resolve to
    ``<backing_root>/TeslaCam/TeslaCam/...`` and 404.

    Strip exactly one leading ``TeslaCam/`` so the emitted
    ``video_path`` matches the videos blueprint's contract. The
    raw DB value is preserved for internal use (``_video_path_exists``,
    cleanup queries) which join under ``backing_root`` directly.
    """
    if not relative_path:
        return relative_path
    normalised = relative_path.replace("\\", "/").lstrip("/")
    prefix = "TeslaCam/"
    if normalised.startswith(prefix):
        return normalised[len(prefix) :]
    return normalised


def _derived_event_from_row(
    row: sqlite3.Row,
    trip_clips: Sequence,
) -> DerivedEvent:
    """Materialise a `DerivedEvent` from a `detected_events` row."""
    clip_id = row["clip_id"]
    video_path: str | None = None
    if clip_id is not None and trip_clips:
        for clip in trip_clips:
            if clip is not None and clip.id == int(clip_id):
                video_path = _video_url_path(clip.relative_path)
                break
    frame_index = row["frame_index"] if "frame_index" in row.keys() else None  # noqa: SIM118
    timestamp_utc = float(row["timestamp_utc"])
    return DerivedEvent(
        id=int(row["id"]),
        trip_id=int(row["trip_id"]) if row["trip_id"] is not None else None,
        timestamp=epoch_to_iso(timestamp_utc),
        lat=row["latitude_deg"],
        lon=row["longitude_deg"],
        event_type=str(row["event_type"]),
        severity=str(row["severity"]),
        description=str(row["description"] or ""),
        video_path=video_path,
        frame_offset=int(frame_index) if frame_index is not None else None,
    )


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
        video_path=_video_url_path(entry.clip.relative_path),
        frame_offset=entry.waypoint.frame_index,
    )


def _simplified_route_waypoints(
    flattened: Sequence[AbsoluteWaypoint],
    *,
    epsilon_m: float,
    max_points: int,
) -> tuple[RouteWaypoint, ...]:
    """Return RDP-simplified, gap-stamped RouteWaypoints for the day payload.

    Avoids shipping every raw frame to the browser (a 30-minute trip
    yields ~22k frames). Splits on travel-gaps so RDP can't shortcut
    across them, simplifies each segment with the standard epsilon,
    uniformly caps the total to ``max_points``, then sets ``gap_after``
    on the last point of each non-final segment so the Leaflet renderer
    breaks the polyline correctly.
    """
    if not flattened:
        return ()
    segments: list[list[AbsoluteWaypoint]] = [[flattened[0]]]
    for index in range(1, len(flattened)):
        if is_gap_between(flattened[index - 1], flattened[index]):
            segments.append([flattened[index]])
        else:
            segments[-1].append(flattened[index])
    picks: list[tuple[AbsoluteWaypoint, bool]] = []
    for seg_idx, segment in enumerate(segments):
        if len(segment) >= _MIN_RENDERABLE_POINTS:
            indices = simplify_polyline_rdp(
                [(e.waypoint.latitude_deg, e.waypoint.longitude_deg) for e in segment],
                epsilon_m,
            )
            chosen = [segment[i] for i in indices]
        else:
            chosen = list(segment)
        for j, entry in enumerate(chosen):
            mark_gap = j == len(chosen) - 1 and seg_idx < len(segments) - 1
            picks.append((entry, mark_gap))
    if len(picks) > max_points:
        keep = set(cap_indices_uniform(list(range(len(picks))), max_points))
        picks = [pick for idx, pick in enumerate(picks) if idx in keep]
    out: list[RouteWaypoint] = []
    for entry, gap_after in picks:
        wp = _route_waypoint_from_entry(entry)
        if gap_after:
            wp = replace(wp, gap_after=True)
        out.append(wp)
    return tuple(out)


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
        video_path=_video_url_path(event.video_path),
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
