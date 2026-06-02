"""Query and convert clip_events rows for the mapping service.

The worker materialises clip-level sentry and saved events in the
``clip_events`` table. This module keeps that SQL and row conversion
separate from the MappingQueries orchestration layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import sqlite3

from teslausb_web.services.mapping_event_derivation import EVENT_SENTRY
from teslausb_web.services.mapping_sql import (
    _SEVERITY_INFO,
    EventRow,
    _append_time_bbox_clauses,
    _optional_float,
    _table_exists,
    _video_url_path,
)
from teslausb_web.services.mapping_trip_derivation import epoch_to_iso
from teslausb_web.services.mapping_tz import UTC_TZ_NAME

_DEFAULT_EVENT_OVERVIEW_LIMIT: Final[int] = 5000
_CLIP_EVENT_TABLE: Final[str] = "clip_events"
_EVENT_SAVED: Final[str] = "saved"
_EVENT_CLIP: Final[str] = "clip_event"
_CLIP_EVENT_FRAME_RATE: Final[float] = 36.0
_CLIP_EVENT_TYPE_SQL: Final[str] = (
    "CASE ce.bucket WHEN 'sentry' THEN 'sentry' WHEN 'saved' THEN 'saved' ELSE 'clip_event' END"
)


def _query_clip_event_day_counts(connection: sqlite3.Connection) -> tuple[sqlite3.Row, ...]:
    """Return raw ``(timestamp_utc, bucket)`` rows for local-day bucketing.

    Day assignment is timezone-dependent, so this returns one row per
    clip event (not a SQL ``GROUP BY date``); the caller buckets each
    into its local calendar day. ``bucket`` carries the event class so
    the caller can split sentry from non-sentry counts.
    """
    if not _table_exists(connection, _CLIP_EVENT_TABLE):
        return ()
    return tuple(
        connection.execute(
            "SELECT timestamp_utc, bucket FROM clip_events",
        ).fetchall()
    )


def _query_clip_events_for_day(
    connection: sqlite3.Connection, date_str: str, tz_name: str = UTC_TZ_NAME
) -> tuple[sqlite3.Row, ...]:
    return _query_clip_events(
        connection,
        event_type=None,
        severity=None,
        bbox=None,
        date_from=None,
        date_to=None,
        date=date_str,
        limit=_DEFAULT_EVENT_OVERVIEW_LIMIT,
        tz_name=tz_name,
    )


def _query_clip_events(  # noqa: PLR0913
    connection: sqlite3.Connection,
    *,
    event_type: str | None,
    severity: str | None,
    bbox: tuple[float, float, float, float] | None,
    date_from: str | None,
    date_to: str | None,
    date: str | None,
    limit: int,
    tz_name: str = UTC_TZ_NAME,
) -> tuple[sqlite3.Row, ...]:
    if not _table_exists(connection, _CLIP_EVENT_TABLE):
        return ()
    if severity is not None and severity != _SEVERITY_INFO:
        return ()
    clauses, params = _clip_event_clauses(event_type, bbox, date_from, date_to, date, tz_name)
    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    return tuple(connection.execute(_clip_event_sql(where_sql), params).fetchall())


def _clip_event_clauses(  # noqa: PLR0913
    event_type: str | None,
    bbox: tuple[float, float, float, float] | None,
    date_from: str | None,
    date_to: str | None,
    date: str | None,
    tz_name: str = UTC_TZ_NAME,
) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if event_type is not None:
        clauses.append(f"{_CLIP_EVENT_TYPE_SQL} = ?")
        params.append(event_type)
    _append_time_bbox_clauses(
        clauses, params, "ce", "est_lat", "est_lon", bbox, date_from, date_to, date, tz_name
    )
    return clauses, params


def _clip_event_sql(where_sql: str) -> str:
    return (
        "SELECT ce.id, ce.bucket, ce.timestamp_utc, ce.est_lat, ce.est_lon, "  # noqa: S608
        "       ce.reason, ce.city, ce.camera, "
        "       pc.relative_path AS primary_clip_path, "
        "       pc.clip_started_utc AS primary_clip_started_utc, "
        "       fc.id AS legacy_trip_id "
        "  FROM clip_events ce "
        "  LEFT JOIN clips pc ON pc.id = ce.primary_clip_id "
        "  LEFT JOIN clip_trip_map m ON m.clip_id = ce.primary_clip_id "
        "  LEFT JOIN trips t ON t.id = m.trip_id "
        "  LEFT JOIN clips fc ON fc.id = t.start_clip_id "
        f"{where_sql} "
        " ORDER BY ce.timestamp_utc DESC, ce.id DESC LIMIT ?"
    )


def _clip_event_row_from_sql(row: sqlite3.Row) -> EventRow:
    return EventRow(
        id=-int(row["id"]),
        trip_id=int(row["legacy_trip_id"]) if row["legacy_trip_id"] is not None else None,
        timestamp=epoch_to_iso(int(row["timestamp_utc"])),
        lat=_optional_float(row["est_lat"]),
        lon=_optional_float(row["est_lon"]),
        event_type=_clip_event_type(str(row["bucket"])),
        severity=_SEVERITY_INFO,
        description=_clip_event_description(row["reason"], row["city"], row["camera"]),
        video_path=_video_url_path(row["primary_clip_path"]),
        frame_offset=_clip_event_frame_offset(row),
        metadata=None,
    )


def _clip_event_type(bucket: str) -> str:
    if bucket == EVENT_SENTRY:
        return EVENT_SENTRY
    if bucket == _EVENT_SAVED:
        return _EVENT_SAVED
    return _EVENT_CLIP


def _clip_event_description(reason: object, city: object, camera: object) -> str:
    parts = [_humanized_text(reason, default="Event clip")]
    for value in (city, camera):
        text = _humanized_text(value, default="")
        if text:
            parts.append(text)
    return " | ".join(parts)


def _humanized_text(value: object, *, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    return text.replace("_", " ").replace("-", " ").title()


def _clip_event_frame_offset(row: sqlite3.Row) -> int | None:
    if row["primary_clip_path"] is None or row["primary_clip_started_utc"] is None:
        return None
    delta_seconds = float(row["timestamp_utc"]) - float(row["primary_clip_started_utc"])
    return max(0, round(delta_seconds * _CLIP_EVENT_FRAME_RATE))
