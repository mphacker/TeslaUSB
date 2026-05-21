from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from teslausb_web.services.mapping_migrations import _haversine_km

if TYPE_CHECKING:
    import sqlite3

_MERGE_MAX_ITERATIONS = 10_000


def _merge_adjacent_trips_for(
    connection: sqlite3.Connection,
    anchor_trip_id: int,
    gap_seconds: float,
) -> int:
    survivor = anchor_trip_id
    for _ in range(_MERGE_MAX_ITERATIONS):
        bounds = _trip_bounds(connection, survivor)
        if bounds is None:
            return survivor
        _write_trip_bounds(connection, survivor, bounds)
        candidate = _merge_candidate(connection, survivor, bounds, gap_seconds)
        if candidate is None:
            return survivor
        survivor = _merge_trip_pair(connection, survivor, candidate)
    msg = f"_merge_adjacent_trips_for exceeded {_MERGE_MAX_ITERATIONS} iterations"
    raise RuntimeError(msg)


def _merge_all_adjacent_trip_pairs(
    connection: sqlite3.Connection,
    gap_seconds: float,
) -> int:
    for merged, _ in enumerate(range(_MERGE_MAX_ITERATIONS)):
        pair = _first_mergeable_pair(connection, gap_seconds)
        if pair is None:
            return merged
        keep_id, drop_id = pair
        _repoint_trip_children(connection, keep_id, drop_id)
        bounds = _trip_bounds(connection, keep_id)
        if bounds is not None:
            _write_trip_bounds(connection, keep_id, bounds)
        connection.execute("DELETE FROM trips WHERE id = ?", (drop_id,))
    msg = f"_merge_all_adjacent_trip_pairs exceeded {_MERGE_MAX_ITERATIONS} iterations"
    raise RuntimeError(msg)


def _trip_bounds(connection: sqlite3.Connection, trip_id: int) -> tuple[str, str] | None:
    row = connection.execute(
        "SELECT MIN(timestamp) AS start_time, MAX(timestamp) AS end_time "
        "FROM waypoints WHERE trip_id = ?",
        (trip_id,),
    ).fetchone()
    if row is None or row["start_time"] is None or row["end_time"] is None:
        return None
    return str(row["start_time"]), str(row["end_time"])


def _write_trip_bounds(
    connection: sqlite3.Connection,
    trip_id: int,
    bounds: tuple[str, str],
) -> None:
    connection.execute(
        "UPDATE trips SET start_time = ?, end_time = ? WHERE id = ?",
        (bounds[0], bounds[1], trip_id),
    )


def _merge_candidate(
    connection: sqlite3.Connection,
    survivor: int,
    bounds: tuple[str, str],
    gap_seconds: float,
) -> int | None:
    row = connection.execute(
        """
        SELECT id
          FROM trips
         WHERE id != :survivor
           AND start_time IS NOT NULL
           AND end_time IS NOT NULL
           AND (CAST(strftime('%s', start_time) AS INTEGER)
                - CAST(strftime('%s', :end_time) AS INTEGER)) <= :gap
           AND (CAST(strftime('%s', :start_time) AS INTEGER)
                - CAST(strftime('%s', end_time) AS INTEGER)) <= :gap
         ORDER BY id ASC
         LIMIT 1
        """,
        {
            "survivor": survivor,
            "start_time": bounds[0],
            "end_time": bounds[1],
            "gap": gap_seconds,
        },
    ).fetchone()
    return None if row is None else int(row["id"])


def _first_mergeable_pair(
    connection: sqlite3.Connection,
    gap_seconds: float,
) -> tuple[int, int] | None:
    row = connection.execute(
        """
        SELECT a.id AS keep_id, b.id AS drop_id
          FROM trips a
          JOIN trips b
            ON a.id < b.id
           AND a.start_time IS NOT NULL
           AND a.end_time IS NOT NULL
           AND b.start_time IS NOT NULL
           AND b.end_time IS NOT NULL
           AND (CAST(strftime('%s', b.start_time) AS INTEGER)
                - CAST(strftime('%s', a.end_time) AS INTEGER)) <= ?
           AND (CAST(strftime('%s', a.start_time) AS INTEGER)
                - CAST(strftime('%s', b.end_time) AS INTEGER)) <= ?
         LIMIT 1
        """,
        (gap_seconds, gap_seconds),
    ).fetchone()
    if row is None:
        return None
    return int(row["keep_id"]), int(row["drop_id"])


def _merge_trip_pair(connection: sqlite3.Connection, left_trip_id: int, right_trip_id: int) -> int:
    keep_id = min(left_trip_id, right_trip_id)
    drop_id = max(left_trip_id, right_trip_id)
    _repoint_trip_children(connection, keep_id, drop_id)
    connection.execute("DELETE FROM trips WHERE id = ?", (drop_id,))
    return keep_id


def _repoint_trip_children(connection: sqlite3.Connection, keep_id: int, drop_id: int) -> None:
    connection.execute("UPDATE waypoints SET trip_id = ? WHERE trip_id = ?", (keep_id, drop_id))
    connection.execute(
        "UPDATE detected_events SET trip_id = ? WHERE trip_id = ?",
        (keep_id, drop_id),
    )


def recompute_trip_stats(connection: sqlite3.Connection, trip_id: int) -> None:
    bounds = _trip_bounds(connection, trip_id)
    if bounds is None:
        return
    first_row = _trip_endpoint(connection, trip_id, bounds[0], descending=False)
    last_row = _trip_endpoint(connection, trip_id, bounds[1], descending=True)
    total_distance_km = _trip_distance_km(connection, trip_id)
    duration_seconds = _trip_duration_seconds(bounds[0], bounds[1])
    connection.execute(
        """
        UPDATE trips
           SET start_time = ?,
               end_time = ?,
               start_lat = ?,
               start_lon = ?,
               end_lat = ?,
               end_lon = ?,
               distance_km = ?,
               duration_seconds = ?
         WHERE id = ?
        """,
        (
            bounds[0],
            bounds[1],
            None if first_row is None else first_row[0],
            None if first_row is None else first_row[1],
            None if last_row is None else last_row[0],
            None if last_row is None else last_row[1],
            total_distance_km,
            duration_seconds,
            trip_id,
        ),
    )


def _trip_endpoint(
    connection: sqlite3.Connection,
    trip_id: int,
    timestamp: str,
    *,
    descending: bool,
) -> tuple[float, float] | None:
    if descending:
        row = connection.execute(
            "SELECT lat, lon FROM waypoints WHERE trip_id = ? AND timestamp = ? "
            "ORDER BY id DESC LIMIT 1",
            (trip_id, timestamp),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT lat, lon FROM waypoints WHERE trip_id = ? AND timestamp = ? "
            "ORDER BY id ASC LIMIT 1",
            (trip_id, timestamp),
        ).fetchone()
    if row is None:
        return None
    return float(row["lat"]), float(row["lon"])


def _trip_distance_km(connection: sqlite3.Connection, trip_id: int) -> float:
    rows = connection.execute(
        "SELECT video_path, lat, lon FROM waypoints "
        "WHERE trip_id = ? AND video_path IS NOT NULL ORDER BY video_path, id",
        (trip_id,),
    ).fetchall()
    total_distance = 0.0
    previous: tuple[float, float] | None = None
    previous_video: str | None = None
    for row in rows:
        current = (float(row["lat"]), float(row["lon"]))
        current_video = str(row["video_path"])
        if previous is not None and current_video == previous_video:
            total_distance += _haversine_km(previous[0], previous[1], current[0], current[1])
        previous = current
        previous_video = current_video
    return total_distance


def _trip_duration_seconds(start_time: str, end_time: str) -> int:
    try:
        delta = datetime.fromisoformat(end_time) - datetime.fromisoformat(start_time)
    except ValueError:
        return 0
    return max(0, int(delta.total_seconds()))
