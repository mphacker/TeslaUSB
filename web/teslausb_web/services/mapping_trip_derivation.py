"""Trip derivation over the Rust worker DB.

The worker (``teslausb-worker``) owns ``/var/lib/teslausb/index.sqlite3``
with ``clips`` + ``waypoints`` tables (schema v2). Trips are NOT
persisted — they are derived on demand by grouping ``clips`` rows on
``clip_started_utc`` gaps. See ADR-0017.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

_BUCKET_TO_FOLDER: Final[dict[str, str]] = {
    "recent": "RecentClips",
    "sentry": "SentryClips",
    "saved": "SavedClips",
    "track": "TrackClips",
}

_EARTH_RADIUS_KM: Final[float] = 6371.0088
_MIN_RENDERABLE_POINTS: Final[int] = 2
_MIN_RDP_POINTS: Final[int] = 3
_MANDATORY_ENDPOINT_COUNT: Final[int] = 2
_GAP_MAX_SECONDS_DEFAULT: Final[float] = 60.0
_GAP_MAX_METERS_DEFAULT: Final[float] = 250.0


@dataclass(frozen=True, slots=True)
class WorkerClip:
    """A single ``clips`` row from the worker DB."""

    id: int
    relative_path: str
    bucket: str
    clip_started_utc: int
    indexed_at_utc: int
    gps_waypoint_count: int


@dataclass(frozen=True, slots=True)
class WorkerWaypoint:
    """A single ``waypoints`` row from the worker DB."""

    id: int
    clip_id: int
    frame_index: int
    timestamp_ms: float
    latitude_deg: float
    longitude_deg: float
    speed_mps: float
    heading_deg: float
    acceleration_x: float | None
    acceleration_y: float | None
    acceleration_z: float | None
    gear: str | None
    steering_angle: float | None
    brake_applied: bool
    blinker_on_left: bool
    blinker_on_right: bool
    autopilot_state: str | None


@dataclass(frozen=True, slots=True)
class TripGroup:
    """A derived trip: a maximal contiguous run of clips."""

    id: int
    clips: tuple[WorkerClip, ...]

    @property
    def first_clip(self) -> WorkerClip:
        return self.clips[0]

    @property
    def last_clip(self) -> WorkerClip:
        return self.clips[-1]


@dataclass(frozen=True, slots=True)
class TripMetrics:
    """Aggregated metrics derived from a trip's clips + waypoints."""

    distance_km: float
    duration_seconds: int
    start_epoch: float
    end_epoch: float
    start_lat: float | None
    start_lon: float | None
    end_lat: float | None
    end_lon: float | None


@dataclass(frozen=True, slots=True)
class AbsoluteWaypoint:
    """A waypoint annotated with its absolute UTC epoch time."""

    waypoint: WorkerWaypoint
    clip: WorkerClip
    abs_epoch: float

    @property
    def iso_timestamp(self) -> str:
        return epoch_to_iso(self.abs_epoch)


def bucket_to_folder(bucket: str) -> str:
    """Map a Rust worker bucket value to a Tesla source-folder name."""
    return _BUCKET_TO_FOLDER.get(bucket, bucket)


def epoch_to_iso(epoch_seconds: float) -> str:
    """Format an epoch-seconds value as an ISO 8601 UTC string."""
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).isoformat()


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon pairs in kilometres."""
    radius_lat1 = math.radians(lat1)
    radius_lat2 = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_lat / 2.0) ** 2
        + math.cos(radius_lat1) * math.cos(radius_lat2) * math.sin(delta_lon / 2.0) ** 2
    )
    return 2.0 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def load_clips(
    connection: sqlite3.Connection,
    *,
    require_gps: bool = True,
) -> tuple[WorkerClip, ...]:
    """Load every clip with a known start time, ordered chronologically."""
    sql = (
        "SELECT id, relative_path, bucket, clip_started_utc, indexed_at_utc, "
        "       gps_waypoint_count "
        "  FROM clips "
        " WHERE clip_started_utc IS NOT NULL "
    )
    if require_gps:
        sql += "   AND gps_waypoint_count > 0 "
    sql += " ORDER BY clip_started_utc ASC, id ASC"
    rows = connection.execute(sql).fetchall()
    return tuple(_clip_from_row(row) for row in rows)


def load_clips_for_trip(
    connection: sqlite3.Connection,
    trip_id: int,
) -> tuple[WorkerClip, ...]:
    """Load only the clips belonging to one trip (via `clip_trip_map`).

    Uses the `clip_trip_map_by_trip` index — O(N_trip), not O(N_total).
    Returns clips in trip-chronological order.
    """
    rows = connection.execute(
        "SELECT c.id, c.relative_path, c.bucket, c.clip_started_utc, "
        "       c.indexed_at_utc, c.gps_waypoint_count "
        "  FROM clips c "
        "  JOIN clip_trip_map m ON m.clip_id = c.id "
        " WHERE m.trip_id = ? "
        " ORDER BY c.clip_started_utc ASC, c.id ASC",
        (trip_id,),
    ).fetchall()
    return tuple(_clip_from_row(row) for row in rows)


def load_sentry_clips(connection: sqlite3.Connection) -> tuple[WorkerClip, ...]:
    """Load every sentry-bucket clip that has no GPS waypoints."""
    rows = connection.execute(
        "SELECT id, relative_path, bucket, clip_started_utc, indexed_at_utc, "
        "       gps_waypoint_count "
        "  FROM clips "
        " WHERE bucket = 'sentry' AND gps_waypoint_count = 0 "
        "   AND clip_started_utc IS NOT NULL "
        " ORDER BY clip_started_utc DESC, id DESC"
    ).fetchall()
    return tuple(_clip_from_row(row) for row in rows)


def load_waypoints_by_clip(
    connection: sqlite3.Connection,
    clip_ids: Sequence[int],
) -> dict[int, tuple[WorkerWaypoint, ...]]:
    """Bulk-load waypoints for a set of clips, grouped by clip id."""
    if not clip_ids:
        return {}
    placeholders = ",".join("?" for _ in clip_ids)
    sql = (
        "SELECT id, clip_id, frame_index, timestamp_ms, latitude_deg, longitude_deg, "  # noqa: S608
        "       speed_mps, heading_deg, acceleration_x, acceleration_y, acceleration_z, "
        "       gear, steering_angle, brake_applied, blinker_on_left, blinker_on_right, "
        "       autopilot_state "
        "  FROM waypoints "
        f" WHERE clip_id IN ({placeholders}) "
        " ORDER BY clip_id ASC, frame_index ASC, id ASC"
    )
    grouped: dict[int, list[WorkerWaypoint]] = {clip_id: [] for clip_id in clip_ids}
    for row in connection.execute(sql, tuple(clip_ids)).fetchall():
        waypoint = _waypoint_from_row(row)
        grouped[waypoint.clip_id].append(waypoint)
    return {clip_id: tuple(rows) for clip_id, rows in grouped.items()}


def group_trips(clips: Sequence[WorkerClip], gap_seconds: int) -> tuple[TripGroup, ...]:
    """Group ``clips`` into trips on ``clip_started_utc`` gaps."""
    if not clips:
        return ()
    trips: list[list[WorkerClip]] = [[clips[0]]]
    for previous, current in pairwise(clips):
        if current.clip_started_utc - previous.clip_started_utc > gap_seconds:
            trips.append([current])
        else:
            trips[-1].append(current)
    return tuple(TripGroup(id=group[0].id, clips=tuple(group)) for group in trips)


def flatten_trip_waypoints(
    trip: TripGroup,
    waypoints_by_clip: dict[int, tuple[WorkerWaypoint, ...]],
) -> tuple[AbsoluteWaypoint, ...]:
    """Return the trip's waypoints ordered by absolute time, with abs-epoch attached."""
    annotated: list[AbsoluteWaypoint] = []
    for clip in trip.clips:
        for waypoint in waypoints_by_clip.get(clip.id, ()):
            abs_epoch = clip.clip_started_utc + waypoint.timestamp_ms / 1000.0
            annotated.append(AbsoluteWaypoint(waypoint=waypoint, clip=clip, abs_epoch=abs_epoch))
    annotated.sort(key=lambda entry: (entry.clip.clip_started_utc, entry.waypoint.frame_index))
    return tuple(annotated)


def compute_trip_metrics(waypoints: Sequence[AbsoluteWaypoint]) -> TripMetrics:
    """Compute aggregate metrics for the (already-ordered) trip waypoints."""
    if not waypoints:
        return TripMetrics(
            distance_km=0.0,
            duration_seconds=0,
            start_epoch=0.0,
            end_epoch=0.0,
            start_lat=None,
            start_lon=None,
            end_lat=None,
            end_lon=None,
        )
    start_epoch = waypoints[0].abs_epoch
    end_epoch = waypoints[-1].abs_epoch
    distance = 0.0
    for previous, current in pairwise(waypoints):
        distance += haversine_km(
            previous.waypoint.latitude_deg,
            previous.waypoint.longitude_deg,
            current.waypoint.latitude_deg,
            current.waypoint.longitude_deg,
        )
    return TripMetrics(
        distance_km=distance,
        duration_seconds=max(0, round(end_epoch - start_epoch)),
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        start_lat=waypoints[0].waypoint.latitude_deg,
        start_lon=waypoints[0].waypoint.longitude_deg,
        end_lat=waypoints[-1].waypoint.latitude_deg,
        end_lon=waypoints[-1].waypoint.longitude_deg,
    )


def is_gap_between(
    previous: AbsoluteWaypoint,
    current: AbsoluteWaypoint,
    *,
    max_seconds: float = _GAP_MAX_SECONDS_DEFAULT,
    max_meters: float = _GAP_MAX_METERS_DEFAULT,
) -> bool:
    """True if two consecutive waypoints should be rendered as disjoint."""
    if abs(current.abs_epoch - previous.abs_epoch) > max_seconds:
        return True
    distance_m = (
        haversine_km(
            previous.waypoint.latitude_deg,
            previous.waypoint.longitude_deg,
            current.waypoint.latitude_deg,
            current.waypoint.longitude_deg,
        )
        * 1000.0
    )
    return distance_m > max_meters


def simplify_polyline_rdp(
    latlons: Sequence[tuple[float, float]],
    epsilon_m: float,
) -> list[int]:
    """Ramer-Douglas-Peucker — return the indices to keep."""
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


def cap_indices_uniform(indices: Sequence[int], max_count: int) -> list[int]:
    """Down-sample ``indices`` uniformly so its length never exceeds ``max_count``."""
    max_count = max(max_count, _MIN_RENDERABLE_POINTS)
    if len(indices) <= max_count:
        return list(indices)
    mandatory = [indices[0], indices[-1]]
    interior = list(indices[1:-1])
    extras_count = max_count - _MANDATORY_ENDPOINT_COUNT
    if extras_count <= 0 or not interior:
        return mandatory
    step = len(interior) / extras_count
    selected_interior = [
        interior[min(int(i * step), len(interior) - 1)] for i in range(extras_count)
    ]
    return sorted({*mandatory, *selected_interior})


def _project_polyline_to_xy(
    latlons: Sequence[tuple[float, float]],
) -> list[tuple[float, float]]:
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


def _clip_from_row(row: sqlite3.Row) -> WorkerClip:
    return WorkerClip(
        id=int(row["id"]),
        relative_path=str(row["relative_path"]),
        bucket=str(row["bucket"]),
        clip_started_utc=int(row["clip_started_utc"]),
        indexed_at_utc=int(row["indexed_at_utc"]),
        gps_waypoint_count=int(row["gps_waypoint_count"]),
    )


def _waypoint_from_row(row: sqlite3.Row) -> WorkerWaypoint:
    return WorkerWaypoint(
        id=int(row["id"]),
        clip_id=int(row["clip_id"]),
        frame_index=int(row["frame_index"]),
        timestamp_ms=float(row["timestamp_ms"]),
        latitude_deg=float(row["latitude_deg"]),
        longitude_deg=float(row["longitude_deg"]),
        speed_mps=float(row["speed_mps"]),
        heading_deg=float(row["heading_deg"]),
        acceleration_x=_optional_float(row["acceleration_x"]),
        acceleration_y=_optional_float(row["acceleration_y"]),
        acceleration_z=_optional_float(row["acceleration_z"]),
        gear=_optional_str(row["gear"]),
        steering_angle=_optional_float(row["steering_angle"]),
        brake_applied=bool(row["brake_applied"]),
        blinker_on_left=bool(row["blinker_on_left"]),
        blinker_on_right=bool(row["blinker_on_right"]),
        autopilot_state=_optional_str(row["autopilot_state"]),
    )


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raise TypeError(f"expected numeric SQLite column, got {type(value).__name__}")


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


__all__ = (
    "AbsoluteWaypoint",
    "TripGroup",
    "TripMetrics",
    "WorkerClip",
    "WorkerWaypoint",
    "bucket_to_folder",
    "cap_indices_uniform",
    "compute_trip_metrics",
    "epoch_to_iso",
    "flatten_trip_waypoints",
    "group_trips",
    "haversine_km",
    "is_gap_between",
    "load_clips",
    "load_sentry_clips",
    "load_waypoints_by_clip",
    "simplify_polyline_rdp",
)
