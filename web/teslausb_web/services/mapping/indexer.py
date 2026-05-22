from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from .dedupe import _already_indexed_by_basename, _dedupe_existing_rows
from .events import DetectedEventRecord, WaypointSample, _detect_events, iter_event_rows
from .paths import _resolve_recording_time, relative_video_path
from .sei import SeiParserProtocol, SeiSidecarProtocol, TelemetryMessageProtocol, get_sei_parser
from .sentry import _infer_sentry_event
from .service import IndexOutcome, IndexResult
from .trips import _merge_adjacent_trips_for, recompute_trip_stats

if TYPE_CHECKING:
    import os
    from pathlib import Path

    from .service import MappingService

_COLD_ACCEL_THRESHOLD_MPS2 = 0.05
_COLD_STEERING_THRESHOLD_DEG = 0.5
_COLD_GEAR_NO_SIGNAL = frozenset({"UNKNOWN", "PARK"})
_NO_GPS_RECORD_AGE_SECONDS = 300


def index_single_file(service: MappingService, video_path: Path) -> IndexResult:
    preflight = _preflight_result(service, video_path)
    if preflight is not None:
        return preflight
    stat_result = video_path.stat()
    parser = get_sei_parser(service._parser_or_default())
    try:
        with service.open_db() as connection:
            row = connection.execute(
                "SELECT waypoint_count FROM indexed_files WHERE file_path = ?",
                (str(video_path),),
            ).fetchone()
            if row is not None and _row_waypoint_count(row) > 0:
                return IndexResult(IndexOutcome.ALREADY_INDEXED)
            result = _index_video(
                connection,
                service=service,
                parser=parser,
                video_path=video_path,
            )
            _record_indexed_file(connection, video_path, stat_result, result)
            return result
    except sqlite3.OperationalError as exc:
        return IndexResult(IndexOutcome.DB_BUSY, error=str(exc))
    except sqlite3.Error as exc:
        return IndexResult(IndexOutcome.PARSE_ERROR, error=str(exc))


def _index_video(
    connection: sqlite3.Connection,
    *,
    service: MappingService,
    parser: SeiParserProtocol,
    video_path: Path,
) -> IndexResult:
    sidecar = _read_sidecar(parser, video_path)
    rel_path = relative_video_path(
        video_path,
        media_root=service.config.media_root,
    )
    file_timestamp = _resolve_recording_time(video_path, parser=parser, sidecar=sidecar)
    dedupe_result = _dedupe_existing_rows(connection, rel_path, video_path)
    if dedupe_result is not None:
        return dedupe_result
    if _already_indexed_by_basename(connection, video_path.name):
        return IndexResult(IndexOutcome.ALREADY_INDEXED)
    try:
        messages = _messages_for_sample_rate(
            parser,
            video_path,
            sample_rate=service.config.sample_rate,
            sidecar=sidecar,
        )
    except Exception as exc:  # noqa: BLE001
        return IndexResult(IndexOutcome.PARSE_ERROR, error=str(exc))
    waypoints = _extract_waypoints(messages, rel_path=rel_path, file_timestamp=file_timestamp)
    if not waypoints:
        return _handle_no_gps_clip(connection, rel_path, file_timestamp, service)
    trip_id = _find_or_create_trip(connection, rel_path, waypoints, service.config.trip_gap_minutes)
    hot_ids = _insert_hot_waypoints(connection, trip_id, waypoints)
    _insert_cold_waypoints(connection, hot_ids, waypoints)
    events = _detect_events(waypoints, service.config.event_thresholds, rel_path)
    _insert_detected_events(connection, trip_id, events)
    merged_trip_id = _merge_adjacent_trips_for(
        connection,
        trip_id,
        float(service.config.trip_gap_minutes * 60),
    )
    recompute_trip_stats(connection, merged_trip_id)
    connection.commit()
    return IndexResult(IndexOutcome.INDEXED, waypoints=len(waypoints), events=len(events))


def _is_front_camera_video(video_path: Path) -> bool:
    name = video_path.name.lower()
    return video_path.suffix.lower() == ".mp4" and "-front" in name


def _preflight_result(service: MappingService, video_path: Path) -> IndexResult | None:
    if not _is_front_camera_video(video_path):
        return IndexResult(IndexOutcome.NOT_FRONT_CAMERA)
    try:
        stat_result = video_path.stat()
    except OSError:
        return IndexResult(IndexOutcome.FILE_MISSING)
    if _is_too_new(service, stat_result.st_mtime):
        return IndexResult(IndexOutcome.TOO_NEW)
    return None


def _is_too_new(service: MappingService, mtime: float) -> bool:
    age_seconds = datetime.now(tz=UTC).timestamp() - mtime
    return age_seconds < service.config.index_too_new_seconds


def _row_waypoint_count(row: sqlite3.Row) -> int:
    value = row["waypoint_count"]
    return int(value) if isinstance(value, int) else 0


def _read_sidecar(
    parser: SeiParserProtocol,
    video_path: Path,
) -> SeiSidecarProtocol | None:
    try:
        return parser.read_sei_sidecar(video_path)
    except Exception:  # noqa: BLE001
        return None


def _extract_waypoints(
    messages: tuple[TelemetryMessageProtocol, ...],
    *,
    rel_path: str,
    file_timestamp: str | None,
) -> tuple[WaypointSample, ...]:
    base_timestamp = _base_datetime(file_timestamp)
    return tuple(
        _waypoint_from_message(message, rel_path, base_timestamp)
        for message in messages
        if message.has_gps
    )


def _messages_for_sample_rate(
    parser: SeiParserProtocol,
    video_path: Path,
    *,
    sample_rate: int,
    sidecar: SeiSidecarProtocol | None,
) -> tuple[TelemetryMessageProtocol, ...]:
    if sidecar is not None and sidecar.sample_rate == sample_rate:
        return sidecar.messages
    return parser.extract_sei_messages(video_path, sample_rate=sample_rate)


def _base_datetime(file_timestamp: str | None) -> datetime | None:
    if file_timestamp is None:
        return None
    try:
        return datetime.fromisoformat(file_timestamp)
    except ValueError:
        return None


def _waypoint_from_message(
    message: TelemetryMessageProtocol,
    rel_path: str,
    base_timestamp: datetime | None,
) -> WaypointSample:
    timestamp = _timestamp_for_message(message, base_timestamp)
    return WaypointSample(
        timestamp=timestamp,
        lat=message.latitude_deg,
        lon=message.longitude_deg,
        heading=message.heading_deg,
        speed_mps=message.vehicle_speed_mps,
        acceleration_x=message.linear_acceleration_x,
        acceleration_y=message.linear_acceleration_y,
        acceleration_z=message.linear_acceleration_z,
        gear=message.gear_state,
        autopilot_state=message.autopilot_state,
        steering_angle=message.steering_wheel_angle,
        brake_applied=message.brake_applied,
        blinker_on_left=message.blinker_on_left,
        blinker_on_right=message.blinker_on_right,
        video_path=rel_path,
        frame_offset=message.frame_index,
    )


def _timestamp_for_message(
    message: TelemetryMessageProtocol,
    base_timestamp: datetime | None,
) -> str:
    if base_timestamp is None:
        return datetime.now(tz=UTC).isoformat()
    return (base_timestamp + timedelta(milliseconds=message.timestamp_ms)).isoformat()


def _handle_no_gps_clip(
    connection: sqlite3.Connection,
    rel_path: str,
    file_timestamp: str | None,
    service: MappingService,
) -> IndexResult:
    if "SentryClips" not in rel_path and "SavedClips" not in rel_path:
        return IndexResult(IndexOutcome.NO_GPS_RECORDED)
    created = _infer_sentry_event(
        connection,
        rel_path,
        file_timestamp,
        media_root=service.config.media_root,
    )
    if not created:
        return IndexResult(IndexOutcome.NO_GPS_RECORDED)
    connection.commit()
    return IndexResult(IndexOutcome.INDEXED, events=1)


def _find_or_create_trip(
    connection: sqlite3.Connection,
    rel_path: str,
    waypoints: tuple[WaypointSample, ...],
    trip_gap_minutes: int,
) -> int:
    existing = _matching_trip(
        connection,
        waypoints[0].timestamp,
        waypoints[-1].timestamp,
        trip_gap_minutes,
    )
    if existing is not None:
        return existing
    row = connection.execute(
        "INSERT INTO trips (start_time, start_lat, start_lon, source_folder, indexed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            waypoints[0].timestamp,
            waypoints[0].lat,
            waypoints[0].lon,
            rel_path.split("/", 1)[0],
            datetime.now(tz=UTC).isoformat(),
        ),
    )
    if row.lastrowid is None:
        msg = "Failed to allocate trip row"
        raise sqlite3.OperationalError(msg)
    return int(row.lastrowid)


def _matching_trip(
    connection: sqlite3.Connection,
    start_time: str,
    end_time: str,
    trip_gap_minutes: int,
) -> int | None:
    gap_seconds = trip_gap_minutes * 60
    row = connection.execute(
        """
        SELECT id
          FROM trips
         WHERE start_time IS NOT NULL
           AND end_time IS NOT NULL
           AND (CAST(strftime('%s', :start_time) AS INTEGER)
                - CAST(strftime('%s', end_time) AS INTEGER)) <= :gap
           AND (CAST(strftime('%s', start_time) AS INTEGER)
                - CAST(strftime('%s', :end_time) AS INTEGER)) <= :gap
         ORDER BY id ASC
         LIMIT 1
        """,
        {"start_time": start_time, "end_time": end_time, "gap": gap_seconds},
    ).fetchone()
    return None if row is None else int(row["id"])


def _insert_hot_waypoints(
    connection: sqlite3.Connection,
    trip_id: int,
    waypoints: tuple[WaypointSample, ...],
) -> tuple[int, ...]:
    inserted: list[int] = []
    for waypoint in waypoints:
        row = connection.execute(
            """
            INSERT INTO waypoints (
                trip_id,
                timestamp,
                lat,
                lon,
                heading,
                speed_mps,
                autopilot_state,
                video_path,
                frame_offset
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                trip_id,
                waypoint.timestamp,
                waypoint.lat,
                waypoint.lon,
                waypoint.heading,
                waypoint.speed_mps,
                waypoint.autopilot_state,
                waypoint.video_path,
                waypoint.frame_offset,
            ),
        ).fetchone()
        if row is not None:
            inserted.append(int(row["id"]))
    return tuple(inserted)


def _insert_cold_waypoints(
    connection: sqlite3.Connection,
    hot_ids: tuple[int, ...],
    waypoints: tuple[WaypointSample, ...],
) -> None:
    rows = tuple(
        _cold_row(waypoint_id, waypoint)
        for waypoint_id, waypoint in zip(hot_ids, waypoints, strict=False)
        if _needs_cold_row(waypoint)
    )
    if not rows:
        return
    connection.executemany(
        """
        INSERT OR REPLACE INTO waypoints_cold (
            id,
            acceleration_x,
            acceleration_y,
            acceleration_z,
            gear,
            steering_angle,
            brake_applied,
            blinker_on_left,
            blinker_on_right
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _needs_cold_row(waypoint: WaypointSample) -> bool:
    gear_signal = waypoint.gear is not None and waypoint.gear not in _COLD_GEAR_NO_SIGNAL
    return any(
        (
            _has_accel_signal(waypoint.acceleration_x),
            _has_accel_signal(waypoint.acceleration_y),
            _has_accel_signal(waypoint.acceleration_z),
            _has_steering_signal(waypoint.steering_angle),
            gear_signal,
            waypoint.brake_applied,
            waypoint.blinker_on_left,
            waypoint.blinker_on_right,
        )
    )


def _cold_row(waypoint_id: int, waypoint: WaypointSample) -> tuple[object, ...]:
    return (
        waypoint_id,
        waypoint.acceleration_x,
        waypoint.acceleration_y,
        waypoint.acceleration_z,
        waypoint.gear,
        waypoint.steering_angle,
        1 if waypoint.brake_applied else 0,
        1 if waypoint.blinker_on_left else 0,
        1 if waypoint.blinker_on_right else 0,
    )


def _has_accel_signal(value: float | None) -> bool:
    return value is not None and abs(value) > _COLD_ACCEL_THRESHOLD_MPS2


def _has_steering_signal(value: float | None) -> bool:
    return value is not None and abs(value) > _COLD_STEERING_THRESHOLD_DEG


def _insert_detected_events(
    connection: sqlite3.Connection,
    trip_id: int,
    events: tuple[DetectedEventRecord, ...],
) -> None:
    if not events:
        return
    connection.executemany(
        """
        INSERT INTO detected_events (
            trip_id,
            timestamp,
            lat,
            lon,
            event_type,
            severity,
            description,
            video_path,
            frame_offset,
            metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple((trip_id, *row) for row in iter_event_rows(events)),
    )


def _record_indexed_file(
    connection: sqlite3.Connection,
    video_path: Path,
    stat_result: os.stat_result,
    result: IndexResult,
) -> None:
    if not _should_record_result(result, stat_result):
        return
    connection.execute(
        """
        INSERT OR REPLACE INTO indexed_files (
            file_path,
            file_size,
            file_mtime,
            indexed_at,
            waypoint_count,
            event_count
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            str(video_path),
            int(stat_result.st_size),
            float(stat_result.st_mtime),
            datetime.now(tz=UTC).isoformat(),
            result.waypoints,
            result.events,
        ),
    )
    connection.commit()


def _should_record_result(result: IndexResult, stat_result: os.stat_result) -> bool:
    if result.outcome in {IndexOutcome.INDEXED, IndexOutcome.DUPLICATE_UPGRADED}:
        return True
    if result.outcome != IndexOutcome.NO_GPS_RECORDED:
        return False
    if not hasattr(stat_result, "st_mtime"):
        return False
    age_seconds = datetime.now(tz=UTC).timestamp() - float(stat_result.st_mtime)
    return age_seconds > _NO_GPS_RECORD_AGE_SECONDS
