"""Mapping blueprint for the B-1 web UI."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    render_template,
    request,
    url_for,
)

from teslausb_web.services.map_view_prefs_service import (
    MapViewPreferencesError,
    MapViewPreferencesService,
)
from teslausb_web.services.mapping_queries import (
    ChartCount,
    DayPayload,
    DayRouteTrip,
    DayRow,
    DrivingStats,
    EventChartData,
    EventChartPoint,
    EventRow,
    EventTypeCount,
    FsdTimelinePoint,
    MappingQueries,
    MappingQueryError,
    RouteWaypoint,
    SeverityChartPoint,
    Stats,
    TripRow,
    TripTelemetryPoint,
)
from teslausb_web.services.mapping_tz import normalize_tz
from teslausb_web.services.video_service import event_playback_target

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

mapping_bp = Blueprint("mapping", __name__, url_prefix="")

_DATE_RE: Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DEFAULT_TRIP_LIMIT: Final[int] = 50
_DEFAULT_EVENT_LIMIT: Final[int] = 100
_DEFAULT_EVENT_LIMIT_CAP: Final[int] = 1000
_DEFAULT_EVENT_OVERVIEW_CAP: Final[int] = 5000
_DEFAULT_DAY_MIN_DISTANCE_KM: Final[float] = 0.05
_DAYS_LIMIT_DEFAULT: Final[int] = 60
_DAYS_LIMIT_MAX: Final[int] = 365
_FRONT_CLIP_SUFFIX: Final[str] = "-front.mp4"
_EVENT_FOLDER_PART_COUNT: Final[int] = 3


class MappingBlueprintError(RuntimeError):
    """Base error raised by blueprint-only helpers."""


class MappingRequestError(MappingBlueprintError):
    """The request payload or path parameters were invalid."""


class MappingNotFoundError(MappingBlueprintError):
    """A requested folder, route, or clip was not found."""


class MappingFilesystemError(MappingBlueprintError):
    """Filesystem inspection failed unexpectedly."""


@dataclass(frozen=True, slots=True)
class _PageRequest:
    page: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class _EventDetails:
    clip_count: int
    camera_count: int
    size_mb: float


@dataclass(frozen=True, slots=True)
class _EventClipListing:
    folder: str
    event: str
    structure: str
    first_front: str
    front_clips: tuple[str, ...]
    event_clip_index: int = 0
    event_seek_seconds: float = 0.0


def _cfg() -> WebConfig:
    return cast("WebConfig", current_app.config["teslausb_config"])


def _get_queries() -> MappingQueries:
    queries = current_app.extensions["mapping_queries"]
    if not isinstance(queries, MappingQueries):
        raise RuntimeError("mapping_queries extension is not configured")
    return queries


def _map_view_prefs() -> MapViewPreferencesService:
    prefs_service = current_app.extensions.get("map_view_prefs_service")
    if not isinstance(prefs_service, MapViewPreferencesService):
        raise RuntimeError("map_view_prefs_service extension is not configured")
    return prefs_service


def _resolve_display_tz() -> str:
    """Resolve the day-bucketing zone: saved override > browser tz > UTC.

    The operator's explicit Settings override (an IANA name) wins so the
    map is deterministic across every device. When unset ("Auto"), the
    browser's reported ``tz`` query param is used. Both are normalised to
    a valid IANA name; anything unknown degrades to UTC.
    """
    try:
        override = _map_view_prefs().get_preferences().display_timezone
    except (MapViewPreferencesError, RuntimeError) as exc:
        logger.warning("display-timezone preference lookup failed: %s", exc)
        override = ""
    if override:
        return normalize_tz(override)
    return normalize_tz(request.args.get("tz"))


def _json_error_payload(message: str) -> Response:
    return jsonify({"success": False, "error": message})


def _parse_bbox() -> tuple[float, float, float, float] | None:
    keys = ("min_lat", "min_lon", "max_lat", "max_lon")
    values = [request.args.get(key) for key in keys]
    if any(value in {None, ""} for value in values):
        return None
    try:
        min_lat, min_lon, max_lat, max_lon = (float(value) for value in values if value is not None)
    except ValueError:
        return None
    return min_lat, min_lon, max_lat, max_lon


def _require_iso_date(date_text: str) -> str:
    if _DATE_RE.fullmatch(date_text) is None:
        raise MappingRequestError("date must be YYYY-MM-DD")
    try:
        date.fromisoformat(date_text)
    except ValueError as exc:
        raise MappingRequestError("date must be a real calendar day") from exc
    return date_text


def _coerce_limit(value: int | None, *, default: int, cap: int | None = None) -> int:
    resolved = default if value is None or value <= 0 else value
    if cap is not None and resolved > cap:
        return cap
    return resolved


def _coerce_non_negative_float(value: float | None, *, default: float) -> float:
    if value is None or value < 0:
        return default
    return value


def _parse_page_request(*, default_limit: int, cap: int | None = None) -> _PageRequest:
    limit = _coerce_limit(
        request.args.get("limit", default_limit, type=int),
        default=default_limit,
        cap=cap,
    )
    page = request.args.get("page", 1, type=int) or 1
    page = max(1, page)
    offset_arg = request.args.get("offset", type=int)
    offset = max(0, offset_arg) if offset_arg is not None else (page - 1) * limit
    return _PageRequest(page=page, limit=limit, offset=offset)


def _safe_segment(segment: str, *, field_name: str) -> str:
    candidate = segment.strip()
    if not candidate:
        raise MappingRequestError(f"{field_name} is required")
    if Path(candidate).name != candidate or candidate in {".", ".."}:
        raise MappingRequestError(f"Invalid {field_name}")
    return candidate


def _serialize_trip(trip: TripRow) -> dict[str, object]:
    return {
        "id": trip.id,
        "start_time": trip.start_time,
        "end_time": trip.end_time,
        "start_lat": trip.start_lat,
        "start_lon": trip.start_lon,
        "end_lat": trip.end_lat,
        "end_lon": trip.end_lon,
        "distance_km": trip.distance_km,
        "duration_seconds": trip.duration_seconds,
        "source_folder": trip.source_folder,
        "indexed_at": trip.indexed_at,
        "event_count": trip.event_count,
        "video_count": trip.video_count,
    }


def _serialize_waypoint(waypoint: RouteWaypoint) -> dict[str, object]:
    return {
        "id": waypoint.id,
        "timestamp": waypoint.timestamp,
        "lat": waypoint.lat,
        "lon": waypoint.lon,
        "heading": waypoint.heading,
        "speed_mps": waypoint.speed_mps,
        "autopilot_state": waypoint.autopilot_state,
        "video_path": waypoint.video_path,
        "frame_offset": waypoint.frame_offset,
        "gap_after": waypoint.gap_after,
    }


def _serialize_event(event: EventRow) -> dict[str, object]:
    return {
        "id": event.id,
        "trip_id": event.trip_id,
        "timestamp": event.timestamp,
        "lat": event.lat,
        "lon": event.lon,
        "event_type": event.event_type,
        "severity": event.severity,
        "description": event.description,
        "video_path": event.video_path,
        "frame_offset": event.frame_offset,
        "metadata": event.metadata,
    }


def _serialize_day(day: DayRow) -> dict[str, object]:
    return {
        "date": day.date,
        "trip_count": day.trip_count,
        "video_count": day.video_count,
        "total_distance_km": day.total_distance_km,
        "event_count": day.event_count,
        "sentry_count": day.sentry_count,
        "first_start": day.first_start,
        "last_end": day.last_end,
    }


def _serialize_day_route_trip(trip: DayRouteTrip) -> dict[str, object]:
    return {
        "trip_id": trip.id,
        "start_time": trip.start_time,
        "end_time": trip.end_time,
        "distance_km": trip.distance_km,
        "duration_seconds": trip.duration_seconds,
        "start_lat": trip.start_lat,
        "start_lon": trip.start_lon,
        "end_lat": trip.end_lat,
        "end_lon": trip.end_lon,
        "source_folder": trip.source_folder,
        "waypoints": [_serialize_waypoint(waypoint) for waypoint in trip.waypoints],
    }


def _serialize_day_payload(payload: DayPayload) -> dict[str, object]:
    return {
        "date": payload.date,
        "latest_date": payload.latest_date,
        "trips": [_serialize_day_route_trip(trip) for trip in payload.trips],
        "events": [_serialize_event(event) for event in payload.events],
    }


def _serialize_event_type_count(count: EventTypeCount) -> dict[str, object]:
    return {"event_type": count.event_type, "count": count.count}


def _serialize_stats(stats: Stats) -> dict[str, object]:
    return {
        "trip_count": stats.trip_count,
        "waypoint_count": stats.waypoint_count,
        "event_count": stats.event_count,
        "indexed_file_count": stats.indexed_file_count,
        "mapped_file_count": stats.mapped_file_count,
        "total_distance_km": stats.total_distance_km,
        "total_duration_seconds": stats.total_duration_seconds,
        "event_breakdown": [_serialize_event_type_count(item) for item in stats.event_breakdown],
        "indexer_status": stats.indexer_status,
    }


def _serialize_driving_stats(stats: DrivingStats) -> dict[str, object]:
    return {
        "has_data": stats.has_data,
        "trip_count": stats.trip_count,
        "total_distance_km": stats.total_distance_km,
        "total_distance_mi": stats.total_distance_mi,
        "total_duration_hours": stats.total_duration_hours,
        "avg_speed_mph": stats.avg_speed_mph,
        "max_speed_mph": stats.max_speed_mph,
        "fsd_usage_pct": stats.fsd_usage_pct,
        "total_events": stats.total_events,
        "warning_events": stats.warning_events,
        "events_per_100mi": stats.events_per_100mi,
    }


def _serialize_chart_count(point: ChartCount) -> dict[str, object]:
    return {"label": point.label, "value": point.value}


def _serialize_severity_chart_point(point: SeverityChartPoint) -> dict[str, object]:
    return {"severity": point.severity, "value": point.value, "color": point.color}


def _serialize_event_chart_point(point: EventChartPoint) -> dict[str, object]:
    return {"day": point.day, "value": point.value}


def _serialize_fsd_timeline_point(point: FsdTimelinePoint) -> dict[str, object]:
    return {"day": point.day, "fsd": point.fsd, "manual": point.manual}


def _serialize_event_chart_data(data: EventChartData) -> dict[str, object]:
    return {
        "by_type": [_serialize_chart_count(item) for item in data.by_type],
        "by_severity": [_serialize_severity_chart_point(item) for item in data.by_severity],
        "over_time": [_serialize_event_chart_point(item) for item in data.over_time],
        "fsd_timeline": [_serialize_fsd_timeline_point(item) for item in data.fsd_timeline],
    }


def _serialize_telemetry_point(point: TripTelemetryPoint) -> dict[str, object]:
    return {
        "id": point.waypoint_id,
        "acceleration_x": point.acceleration_x,
        "acceleration_y": point.acceleration_y,
        "acceleration_z": point.acceleration_z,
        "gear": point.gear,
        "steering_angle": point.steering_angle,
        "brake_applied": point.brake_applied,
        "blinker_on_left": point.blinker_on_left,
        "blinker_on_right": point.blinker_on_right,
        "autopilot_state": point.autopilot_state,
    }


def _mapping_media_root() -> Path:
    return _cfg().mapping.media_root


def _require_mapping_media_root() -> Path:
    root = _mapping_media_root()
    if not root.is_dir():
        raise MappingFilesystemError("TeslaCam not accessible")
    return root


def _folder_root(folder: str) -> Path:
    safe_folder = _safe_segment(folder, field_name="folder")
    base = _require_mapping_media_root() / safe_folder
    if not base.is_dir():
        raise MappingNotFoundError(f"Folder not found: {safe_folder}")
    return base


def _clip_camera_name(path: Path, event_name: str) -> str:
    suffix = path.stem.removeprefix(f"{event_name}-")
    return suffix or path.stem


def _event_clip_files(folder: str, event_name: str) -> tuple[Path, ...]:
    root = _folder_root(folder)
    safe_event = _safe_segment(event_name, field_name="event_name")
    event_dir = root / safe_event
    if event_dir.is_dir():
        return tuple(
            sorted(path for path in event_dir.iterdir() if path.is_file() and path.suffix == ".mp4")
        )
    return tuple(sorted(path for path in root.glob(f"{safe_event}-*.mp4") if path.is_file()))


def _event_details(folder: str, event_name: str) -> _EventDetails:
    files = _event_clip_files(folder, event_name)
    if not files:
        return _EventDetails(clip_count=0, camera_count=0, size_mb=0.0)
    camera_names = {_clip_camera_name(path, event_name) for path in files}
    size_bytes = sum(path.stat().st_size for path in files)
    return _EventDetails(
        clip_count=len(files),
        camera_count=len(camera_names),
        size_mb=size_bytes / (1024 * 1024),
    )


def _event_clip_listing(folder: str, event_name: str) -> _EventClipListing:
    safe_folder = _safe_segment(folder, field_name="folder")
    safe_event = _safe_segment(event_name, field_name="event_name")
    root = _folder_root(safe_folder)
    event_dir = root / safe_event
    if event_dir.is_dir():
        front_files = tuple(
            sorted(
                path.name
                for path in event_dir.iterdir()
                if path.is_file() and path.name.endswith(".mp4") and "-front" in path.name
            )
        )
        clip_index, seek_seconds = event_playback_target(event_dir, safe_event, front_files)
        return _EventClipListing(
            folder=safe_folder,
            event=safe_event,
            structure="events",
            first_front=front_files[0] if front_files else "",
            front_clips=tuple(f"{safe_folder}/{safe_event}/{name}" for name in front_files),
            event_clip_index=clip_index,
            event_seek_seconds=seek_seconds,
        )
    flat_clip = root / f"{safe_event}{_FRONT_CLIP_SUFFIX}"
    if flat_clip.is_file():
        clip_name = flat_clip.name
        return _EventClipListing(
            folder=safe_folder,
            event=safe_event,
            structure="flat",
            first_front=clip_name,
            front_clips=(f"{safe_folder}/{clip_name}",),
        )
    raise MappingNotFoundError(
        "Video file no longer exists. Tesla may have overwritten it. Try re-indexing."
    )


def _waypoints_for_clip(video_path: str) -> tuple[int | None, tuple[RouteWaypoint, ...]]:
    return _get_queries().waypoints_for_video(video_path)


def _trip_telemetry(trip_id: int) -> dict[str, dict[str, object]]:
    points = _get_queries().query_trip_telemetry(trip_id)
    return {str(point.waypoint_id): _serialize_telemetry_point(point) for point in points}


def _sentry_event_payload(event: EventRow) -> dict[str, object]:
    payload = _serialize_event(event)
    video_path = event.video_path or ""
    parts = video_path.replace("\\", "/").split("/")
    payload["source_folder"] = parts[0] if parts else ""
    payload["event_folder"] = parts[1] if len(parts) >= _EVENT_FOLDER_PART_COUNT else ""
    return payload


def _handle_request_error(exc: Exception) -> ResponseReturnValue:
    return _json_error_payload(str(exc)), HTTPStatus.BAD_REQUEST


def _handle_not_found(exc: Exception) -> ResponseReturnValue:
    return _json_error_payload(str(exc)), HTTPStatus.NOT_FOUND


def _handle_query_error(exc: Exception) -> ResponseReturnValue:
    logger.warning("mapping query failed: %s", exc)
    return _json_error_payload(str(exc)), HTTPStatus.INTERNAL_SERVER_ERROR


@mapping_bp.route("/")
def map_view() -> ResponseReturnValue:
    requested_date = request.args.get("date", "").strip()
    prefs_service = _map_view_prefs()
    map_preferences = prefs_service.get_preferences()
    # The "latest day" is timezone-dependent. On first paint the server
    # cannot know the browser's zone (no JS has run yet), so we only
    # pre-seed ``latest_date`` when an explicit Settings override makes
    # the server-side zone authoritative. In Auto mode we render the
    # shell with no initial date and let init.js + /api/days resolve the
    # latest *local* day once the browser zone is known — pre-seeding a
    # UTC latest day there would flash the wrong day for an operator
    # whose evening drive crossed midnight UTC. An explicit ``?date=`` in
    # the URL is always honoured.
    latest_date: str | None = None
    if map_preferences.display_timezone:
        try:
            latest_date = _get_queries().query_latest_date(
                normalize_tz(map_preferences.display_timezone)
            )
        except (MappingQueryError, RuntimeError) as exc:
            logger.warning("latest-date lookup failed: %s", exc)
            latest_date = None
    bootstrap = {
        "api": {
            "days": url_for("mapping.api_days"),
            "day_routes_template": url_for("mapping.api_day_routes", date="__DATE__"),
            "day_payload_template": url_for("mapping.api_day_payload", date="__DATE__"),
            "trips": url_for("mapping.api_trips"),
            "playable_trips": url_for("mapping.api_trips_playable"),
            "trip_route_template": url_for("mapping.api_trip_route", trip_id=0).replace(
                "0", "__TRIP_ID__"
            ),
            "trip_telemetry_template": url_for("mapping.api_trip_telemetry", trip_id=0).replace(
                "0", "__TRIP_ID__"
            ),
            "events": url_for("mapping.api_events"),
            "stats": url_for("mapping.api_stats"),
            "driving_stats": url_for("mapping.api_driving_stats"),
            "event_charts": url_for("mapping.api_event_charts"),
            "sentry_events": url_for("mapping.api_sentry_events"),
            "waypoints_for_clip": url_for("mapping.api_waypoints_for_clip"),
            "event_details_template": url_for(
                "mapping.api_event_details", folder="__FOLDER__", event_name="__EVENT__"
            ),
            "event_clips_template": url_for(
                "mapping.api_event_clips", folder="__FOLDER__", event_name="__EVENT__"
            ),
        },
        "assets": {
            "sprite": url_for("static", filename="icons/lucide-sprite.svg"),
            "leaflet_icon_path": url_for("static", filename="vendor/leaflet/images/"),
            "tile_cache_sw": url_for("_tile_cache_service_worker"),
        },
        "view": {
            "date": requested_date,
            "latest_date": latest_date or "",
            "speed_units": map_preferences.speed_units.value,
            "display_timezone": map_preferences.display_timezone,
            "video_stream_template": "/videos/stream/__PATH__",
        },
        "features": {
            "cloud_archive_enabled": _cfg().features.cloud_archive_enabled,
        },
    }
    return render_template(
        "mapping.html",
        page="map",
        expandable=True,
        mapping_bootstrap=bootstrap,
    )


@mapping_bp.route("/api/trips")
def api_trips() -> ResponseReturnValue:
    page_request = _parse_page_request(default_limit=_DEFAULT_TRIP_LIMIT)
    min_distance = _coerce_non_negative_float(
        request.args.get("min_distance", _DEFAULT_DAY_MIN_DISTANCE_KM, type=float),
        default=_DEFAULT_DAY_MIN_DISTANCE_KM,
    )
    try:
        trips = _get_queries().query_trips(
            limit=page_request.limit + 1,
            offset=page_request.offset,
            bbox=_parse_bbox(),
            date_from=request.args.get("date_from"),
            date_to=request.args.get("date_to"),
            min_distance_km=min_distance,
        )
    except (MappingQueryError, RuntimeError) as exc:
        return _handle_query_error(exc)
    visible_trips = trips[: page_request.limit]
    return jsonify(
        {
            "trips": [_serialize_trip(trip) for trip in visible_trips],
            "has_next": len(trips) > page_request.limit,
            "next_page": page_request.page + 1,
            "page": page_request.page,
            "limit": page_request.limit,
            "total_video_count": sum(trip.video_count for trip in visible_trips),
        }
    )


@mapping_bp.route("/api/trip/<int:trip_id>/route")
def api_trip_route(trip_id: int) -> ResponseReturnValue:
    try:
        waypoints = _get_queries().query_trip_route(trip_id)
    except (MappingQueryError, RuntimeError) as exc:
        return _handle_query_error(exc)
    if not waypoints:
        return _json_error_payload("Trip not found"), HTTPStatus.NOT_FOUND
    return jsonify(
        {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[waypoint.lon, waypoint.lat] for waypoint in waypoints],
            },
            "properties": {
                "trip_id": trip_id,
                "waypoint_count": len(waypoints),
                "waypoints": [_serialize_waypoint(waypoint) for waypoint in waypoints],
            },
        }
    )


@mapping_bp.route("/api/trip/<int:trip_id>/telemetry")
def api_trip_telemetry(trip_id: int) -> ResponseReturnValue:
    try:
        telemetry = _trip_telemetry(trip_id)
    except MappingQueryError as exc:
        return _handle_query_error(exc)
    return jsonify({"trip_id": trip_id, "telemetry": telemetry})


@mapping_bp.route("/api/waypoints-for-clip")
def api_waypoints_for_clip() -> ResponseReturnValue:
    try:
        trip_id, waypoints = _waypoints_for_clip(request.args.get("path", ""))
    except MappingQueryError as exc:
        return _handle_query_error(exc)
    payload: dict[str, object] = {
        "waypoints": [_serialize_waypoint(waypoint) for waypoint in waypoints]
    }
    if trip_id is not None:
        payload["trip_id"] = trip_id
    return jsonify(payload)


@mapping_bp.route("/api/events")
def api_events() -> ResponseReturnValue:
    date = request.args.get("date")
    raw_limit = request.args.get("limit", _DEFAULT_EVENT_LIMIT, type=int)
    try:
        if date is not None:
            _require_iso_date(date)
        limit_cap = (
            _DEFAULT_EVENT_OVERVIEW_CAP
            if date or request.args.get("overview", type=int)
            else _DEFAULT_EVENT_LIMIT_CAP
        )
        events = _get_queries().query_events(
            limit=_coerce_limit(raw_limit, default=_DEFAULT_EVENT_LIMIT, cap=limit_cap),
            offset=request.args.get("offset", 0, type=int),
            event_type=request.args.get("type"),
            severity=request.args.get("severity"),
            bbox=_parse_bbox(),
            date_from=request.args.get("date_from"),
            date_to=request.args.get("date_to"),
            date=date,
            tz_name=_resolve_display_tz(),
        )
    except MappingRequestError as exc:
        return _handle_request_error(exc)
    except (MappingQueryError, RuntimeError) as exc:
        return _handle_query_error(exc)
    return jsonify({"events": [_serialize_event(event) for event in events]})


@mapping_bp.route("/api/days")
def api_days() -> ResponseReturnValue:
    try:
        days = _get_queries().query_days(
            limit=_coerce_limit(
                request.args.get("limit", _DAYS_LIMIT_DEFAULT, type=int),
                default=_DAYS_LIMIT_DEFAULT,
                cap=_DAYS_LIMIT_MAX,
            ),
            min_distance_km=_coerce_non_negative_float(
                request.args.get("min_distance", _DEFAULT_DAY_MIN_DISTANCE_KM, type=float),
                default=_DEFAULT_DAY_MIN_DISTANCE_KM,
            ),
            tz_name=_resolve_display_tz(),
        )
    except (MappingQueryError, RuntimeError) as exc:
        return _handle_query_error(exc)
    return jsonify({"days": [_serialize_day(day) for day in days]})


@mapping_bp.route("/api/day/<date>/routes")
def api_day_routes(date: str) -> ResponseReturnValue:
    try:
        trips = _get_queries().query_day_routes(
            _require_iso_date(date),
            min_distance_km=_coerce_non_negative_float(
                request.args.get("min_distance", _DEFAULT_DAY_MIN_DISTANCE_KM, type=float),
                default=_DEFAULT_DAY_MIN_DISTANCE_KM,
            ),
            tz_name=_resolve_display_tz(),
        )
    except MappingRequestError as exc:
        return _handle_request_error(exc)
    except (MappingQueryError, RuntimeError) as exc:
        return _handle_query_error(exc)
    return jsonify({"date": date, "trips": [_serialize_day_route_trip(trip) for trip in trips]})


@mapping_bp.route("/api/day/<date>/payload")
def api_day_payload(date: str) -> ResponseReturnValue:
    """Single-shot payload for the map page: trips + events for one date.

    Replaces the four parallel calls the page used to fire on initial
    load (``/api/day/<date>/routes``, ``/api/events?date=``,
    ``/api/stats``, ``/api/driving-stats``) with one direct-SQL call.
    """
    try:
        payload = _get_queries().query_day_payload(
            _require_iso_date(date), tz_name=_resolve_display_tz()
        )
    except MappingRequestError as exc:
        return _handle_request_error(exc)
    except (MappingQueryError, RuntimeError) as exc:
        return _handle_query_error(exc)
    return jsonify(_serialize_day_payload(payload))


@mapping_bp.route("/api/trips/playable")
def api_trips_playable() -> ResponseReturnValue:
    try:
        date = _require_iso_date(request.args.get("date", ""))
        trips = _get_queries().playable_trips_for_date(date, tz_name=_resolve_display_tz())
    except MappingRequestError as exc:
        return _handle_request_error(exc)
    except (MappingQueryError, RuntimeError) as exc:
        return _handle_query_error(exc)
    return jsonify({"date": date, "trips": {str(trip.id): trip.is_playable for trip in trips}})


@mapping_bp.route("/api/stats")
def api_stats() -> ResponseReturnValue:
    try:
        stats = _get_queries().get_stats()
    except (MappingQueryError, RuntimeError) as exc:
        return _handle_query_error(exc)
    return jsonify(_serialize_stats(stats))


@mapping_bp.route("/api/driving-stats")
def api_driving_stats() -> ResponseReturnValue:
    try:
        stats = _get_queries().get_driving_stats()
    except (MappingQueryError, RuntimeError) as exc:
        return _handle_query_error(exc)
    return jsonify(_serialize_driving_stats(stats))


@mapping_bp.route("/api/event-charts")
def api_event_charts() -> ResponseReturnValue:
    try:
        data = _get_queries().get_event_chart_data(_resolve_display_tz())
    except (MappingQueryError, RuntimeError) as exc:
        return _handle_query_error(exc)
    return jsonify(_serialize_event_chart_data(data))


@mapping_bp.route("/api/sentry-events")
def api_sentry_events() -> ResponseReturnValue:
    page_request = _parse_page_request(default_limit=_DEFAULT_EVENT_LIMIT)
    try:
        events = _get_queries().query_events(
            limit=page_request.limit + 1,
            offset=page_request.offset,
            tz_name=_resolve_display_tz(),
        )
    except (MappingQueryError, RuntimeError) as exc:
        return _handle_query_error(exc)
    visible_events = events[: page_request.limit]
    return jsonify(
        {
            "events": [_sentry_event_payload(event) for event in visible_events],
            "has_next": len(events) > page_request.limit,
            "next_page": page_request.page + 1,
            "page": page_request.page,
            "limit": page_request.limit,
        }
    )


@mapping_bp.route("/api/event-details/<folder>/<event_name>")
def api_event_details(folder: str, event_name: str) -> ResponseReturnValue:
    try:
        details = _event_details(folder, event_name)
    except MappingRequestError as exc:
        return _handle_request_error(exc)
    except MappingNotFoundError as exc:
        return _handle_not_found(exc)
    except OSError as exc:
        return _handle_query_error(MappingFilesystemError(f"Failed to read event details: {exc}"))
    return jsonify(
        {
            "clip_count": details.clip_count,
            "camera_count": details.camera_count,
            "size_mb": details.size_mb,
        }
    )


@mapping_bp.route("/api/event-clips/<folder>/<event_name>")
def api_event_clips(folder: str, event_name: str) -> ResponseReturnValue:
    try:
        listing = _event_clip_listing(folder, event_name)
    except MappingRequestError as exc:
        return _handle_request_error(exc)
    except MappingNotFoundError as exc:
        return (
            jsonify(
                {
                    "error": str(exc),
                    "folder": _safe_segment(folder, field_name="folder"),
                    "event": _safe_segment(event_name, field_name="event_name"),
                    "front_clips": [],
                }
            ),
            HTTPStatus.NOT_FOUND,
        )
    return jsonify(
        {
            "folder": listing.folder,
            "event": listing.event,
            "structure": listing.structure,
            "first_front": listing.first_front,
            "front_clips": list(listing.front_clips),
            "event_clip_index": listing.event_clip_index,
            "event_seek_seconds": listing.event_seek_seconds,
        }
    )
