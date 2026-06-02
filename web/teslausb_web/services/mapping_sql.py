"""Shared SQL row types and low-level clauses for mapping services.

This module owns primitives used by more than one mapping query subsystem
without depending on higher-level orchestration modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import sqlite3

_SEVERITY_INFO: Final[str] = "info"


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


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _append_time_bbox_clauses(  # noqa: PLR0913
    clauses: list[str],
    params: list[object],
    alias: str,
    lat_column: str,
    lon_column: str,
    bbox: tuple[float, float, float, float] | None,
    date_from: str | None,
    date_to: str | None,
    date: str | None,
) -> None:
    if date is not None:
        clauses.append(f"date({alias}.timestamp_utc, 'unixepoch') = ?")
        params.append(date)
    if date_from is not None:
        clauses.append(
            f"strftime('%Y-%m-%dT%H:%M:%S+00:00', {alias}.timestamp_utc, 'unixepoch') >= ?"
        )
        params.append(date_from)
    if date_to is not None:
        clauses.append(
            f"strftime('%Y-%m-%dT%H:%M:%S+00:00', {alias}.timestamp_utc, 'unixepoch') <= ?"
        )
        params.append(date_to)
    if bbox is not None:
        min_lat, min_lon, max_lat, max_lon = bbox
        clauses.append(
            f"{alias}.{lat_column} IS NOT NULL AND {alias}.{lon_column} IS NOT NULL "
            f"AND {alias}.{lat_column} BETWEEN ? AND ? "
            f"AND {alias}.{lon_column} BETWEEN ? AND ?"
        )
        params.extend([min_lat, max_lat, min_lon, max_lon])


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float | str):
        return float(value)
    raise TypeError(f"Expected numeric SQLite value, got {type(value).__name__}")
