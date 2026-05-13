"""
Read-only query helpers for the mapping/geo-index DB.

Phase 3c.3 (#100): extracted from ``mapping_service.py`` to keep all
read-only DB access in one place. The Flask blueprints and the cloud
archive service consume these helpers; the indexer itself does not
touch them.

Includes:
  - :func:`get_db_connection` — convenience wrapper around
    :func:`mapping_migrations._init_db`
  - Per-resource queries: ``query_trips``, ``query_trip_route``,
    ``query_events``, ``query_days``, ``query_day_routes``
  - The All-time map endpoint: ``query_all_routes_simplified``
  - Trip-playability: ``playable_trips_for_date`` (with 60s cache)
  - Stats endpoints: ``get_stats``, ``get_driving_stats``,
    ``get_event_chart_data``
  - Polyline gap detection helpers: ``_haversine_m``,
    ``_parse_iso_seconds``, ``_is_gap_between``,
    ``_simplify_polyline_rdp``
  - Filesystem resolution helper: ``_resolve_video_path_on_disk``
  - The 60-second LRU-ish ``_PLAYABLE_TRIPS_CACHE``

Dependency direction (one-way, no cycle):
    Imports ``_init_db`` from ``mapping_migrations`` and a small set
    of runtime helpers from ``mapping_service`` (``_haversine_km``,
    ``_get_worker_status_for_stats``). ``mapping_service`` does NOT
    import anything from this module — none of the indexer code
    paths use these query helpers.

Power-loss safety:
    All queries are SELECT-only and run on the same WAL-mode
    connection that the indexer uses. The 60s
    ``_PLAYABLE_TRIPS_CACHE`` is purely in-memory and rebuilt on
    process restart, so it cannot get out of sync with the DB
    across crashes.
"""

import logging
import math
import os
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from services.mapping_migrations import _init_db
from services.mapping_service import (
    _get_worker_status_for_stats,
    _haversine_km,
    _with_db_retry,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Polyline gap detection
# ---------------------------------------------------------------------------

# A "gap" between consecutive waypoints means we should NOT draw a
# polyline segment connecting them — the car either wasn't recording
# (Tesla skipped clips during a parking break, or the dashcam was off)
# or the SEI metadata is corrupted. Without gap detection, day-view and
# All-time renderers draw a long straight diagonal across the map from
# one side of the gap to the other (observed bug Apr 26 2026: a 5.8 km
# straight line between two ends of a 6-minute parking break the trip
# detector incorrectly merged into one trip).
#
# Two complementary thresholds, OR'd together — either alone catches
# different real-world failure modes:
#
#   * MAX_GAP_SECONDS = 60 — Tesla front-camera clips are 60 s; a gap
#     longer than that means at least one clip is missing entirely.
#     Catches park-break gaps even when the car barely moved.
#
#   * MAX_GAP_METERS = 250 — Tesla SEI samples at ~1 Hz; even at 80 mph
#     (~36 m/s) consecutive samples are <40 m apart. A jump >250 m
#     between two adjacent waypoints is geographically impossible and
#     usually indicates SEI clock skew across overlapping clips, dropped
#     frames, or interleaved data from a re-indexed late-arriving clip
#     whose timestamps disagree with the rest of the trip.
#
# Both numbers were chosen conservatively: real driving never trips
# them, and any miss would be visible to the user anyway.

GAP_MAX_SECONDS_DEFAULT = 60.0
GAP_MAX_METERS_DEFAULT = 250.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two GPS points, in meters."""
    return _haversine_km(lat1, lon1, lat2, lon2) * 1000.0


def _parse_iso_seconds(ts: Optional[str]) -> Optional[float]:
    """Parse an ISO-8601 timestamp to epoch seconds; ``None`` on failure.

    Tolerates timezone-naive and ``Z``-suffixed forms — both are
    produced by the indexer depending on whether the SEI carried tz.
    """
    if not ts:
        return None
    try:
        if ts.endswith('Z'):
            ts = ts[:-1] + '+00:00'
        return datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return None


def _is_gap_between(prev_ts: Optional[str], prev_lat: Optional[float],
                    prev_lon: Optional[float], curr_ts: Optional[str],
                    curr_lat: Optional[float], curr_lon: Optional[float],
                    max_seconds: float = GAP_MAX_SECONDS_DEFAULT,
                    max_meters: float = GAP_MAX_METERS_DEFAULT) -> bool:
    """Return True iff (prev → curr) crosses a gap that should split a
    polyline.

    Either threshold is enough on its own; both are checked because
    they catch different failure modes (see module-level constants).
    Missing coordinates or unparseable timestamps are treated as no
    gap on that axis — never as a positive — so a malformed waypoint
    can't accidentally break a long continuous polyline.
    """
    pa = _parse_iso_seconds(prev_ts)
    pb = _parse_iso_seconds(curr_ts)
    if pa is not None and pb is not None and abs(pb - pa) > max_seconds:
        return True
    if (prev_lat is not None and prev_lon is not None
            and curr_lat is not None and curr_lon is not None):
        if _haversine_m(prev_lat, prev_lon, curr_lat, curr_lon) > max_meters:
            return True
    return False


def _simplify_polyline_rdp(latlons, epsilon_m: float = 8.0) -> List[int]:
    """Apply Ramer-Douglas-Peucker simplification to a (lat, lon)
    polyline, returning the indices of the points to keep.

    The algorithm projects (lat, lon) to a local equirectangular
    meters frame centered on the polyline's mean latitude, then
    keeps every point whose perpendicular distance to the
    simplified line exceeds ``epsilon_m``. This preserves road
    corners (their perpendicular distance to a chord across them
    is large) and collapses straight stretches (zero perpendicular
    distance). 8 m is the default because it sits comfortably above
    typical Tesla GPS noise (~3-5 m) yet is tight enough that any
    visible road feature survives at any zoom relevant to the All
    time map view.

    Iterative (stack-based) to avoid Python's default recursion
    limit on multi-thousand-point trips. Always returns at minimum
    ``[0, n-1]`` (the endpoints) for any polyline of length >= 2;
    for ``n < 2`` returns ``list(range(n))``.

    Used by :func:`query_all_routes_simplified` to fix the visible
    "polyline cuts across the road" bug that stride sampling
    produced on long trips with sharp turns.
    """
    n = len(latlons)
    if n < 3:
        return list(range(n))

    # Project (lat, lon) to local meters via equirectangular
    # approximation centered on the polyline's mean latitude.
    # Plenty accurate for trips up to ~100 km — we'll never see
    # longer in a single Tesla trip recording.
    mean_lat = sum(p[0] for p in latlons) / n
    cos_lat = math.cos(math.radians(mean_lat))
    deg_lat_m = 111320.0  # ~ meters per degree of latitude
    deg_lon_m = deg_lat_m * cos_lat
    xy = [(p[1] * deg_lon_m, p[0] * deg_lat_m) for p in latlons]

    keep = [False] * n
    keep[0] = True
    keep[-1] = True
    eps2 = epsilon_m * epsilon_m
    stack = [(0, n - 1)]
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        x1, y1 = xy[start]
        x2, y2 = xy[end]
        dx = x2 - x1
        dy = y2 - y1
        denom = dx * dx + dy * dy
        max_d2 = 0.0
        max_i = start
        if denom == 0.0:
            # Degenerate segment (start point == end point — happens
            # for loop trips that finish where they started). Use
            # plain euclidean distance to the start so we still find
            # the farthest excursion and split there.
            for i in range(start + 1, end):
                xi, yi = xy[i]
                ddx = xi - x1
                ddy = yi - y1
                d2 = ddx * ddx + ddy * ddy
                if d2 > max_d2:
                    max_d2 = d2
                    max_i = i
        else:
            for i in range(start + 1, end):
                xi, yi = xy[i]
                # Squared perpendicular distance from (xi, yi) to
                # the line through (x1, y1) and (x2, y2). Computing
                # d^2 instead of d skips a per-point sqrt — we only
                # need to compare against eps^2.
                num = dy * xi - dx * yi + x2 * y1 - y2 * x1
                d2 = (num * num) / denom
                if d2 > max_d2:
                    max_d2 = d2
                    max_i = i
        if max_d2 > eps2:
            keep[max_i] = True
            stack.append((start, max_i))
            stack.append((max_i, end))
    return [i for i, k in enumerate(keep) if k]


@_with_db_retry
def get_db_connection(db_path: str) -> sqlite3.Connection:
    """Get a read-only connection to the geo-index database."""
    conn = _init_db(db_path)
    return conn


@_with_db_retry
def query_trips(db_path: str, limit: int = 50, offset: int = 0,
                bbox: Optional[Tuple[float, float, float, float]] = None,
                date_from: Optional[str] = None,
                date_to: Optional[str] = None,
                min_distance_km: float = 0.05) -> List[dict]:
    """Query trips with optional bounding box and date filters.

    ``min_distance_km`` defaults to 50 m, which hides parking-lot blips and
    isolated sentry recordings from the trip nav. Pass ``0`` to include all
    trips regardless of distance.

    Performance: ``event_count`` and ``video_count`` are computed via
    correlated subqueries in the same SELECT so the whole call is a single
    SQL statement regardless of page size. The earlier per-trip Python
    loop fired 1 + 2*page_size queries (401 for a 200-trip page) and was
    the dominant cost of opening the map page on databases with thousands
    of waypoints.
    """
    conn = _init_db(db_path)
    try:
        sql = (
            "SELECT t.*, "
            "       (SELECT COUNT(*) FROM detected_events de "
            "          WHERE de.trip_id = t.id) AS event_count, "
            "       (SELECT COUNT(DISTINCT w.video_path) FROM waypoints w "
            "          WHERE w.trip_id = t.id "
            "            AND w.video_path IS NOT NULL) AS video_count "
            "  FROM trips t "
            " WHERE 1=1"
        )
        params: List = []

        if min_distance_km and min_distance_km > 0:
            sql += " AND COALESCE(t.distance_km, 0) >= ?"
            params.append(min_distance_km)

        if bbox:
            min_lat, min_lon, max_lat, max_lon = bbox
            sql += (" AND t.start_lat BETWEEN ? AND ? "
                    "AND t.start_lon BETWEEN ? AND ?")
            params.extend([min_lat, max_lat, min_lon, max_lon])

        if date_from:
            sql += " AND t.start_time >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND t.start_time <= ?"
            params.append(date_to)

        sql += " ORDER BY t.start_time DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@_with_db_retry
def query_trip_route(db_path: str, trip_id: int) -> List[dict]:
    """Get all waypoints for a trip as a GeoJSON-ready list.

    Sorted by ``timestamp ASC`` (with ``id ASC`` as tiebreaker) so
    polylines and HUD interpolation walk the trip in true chrono-
    logical order even when waypoints from a late-indexed video or
    a v2->v3 trip-merge land with non-monotonic ids.
    """
    conn = _init_db(db_path)
    try:
        rows = conn.execute(
            """SELECT lat, lon, heading, speed_mps, autopilot_state,
                      video_path, frame_offset, timestamp,
                      steering_angle, brake_applied, gear,
                      acceleration_x, acceleration_y,
                      blinker_on_left, blinker_on_right
               FROM waypoints WHERE trip_id = ?
               ORDER BY timestamp ASC, id ASC""",
            (trip_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@_with_db_retry
def query_events(db_path: str, limit: int = 100, offset: int = 0,
                 event_type: Optional[str] = None,
                 severity: Optional[str] = None,
                 bbox: Optional[Tuple[float, float, float, float]] = None,
                 date_from: Optional[str] = None,
                 date_to: Optional[str] = None,
                 date: Optional[str] = None) -> List[dict]:
    """Query detected events with optional filters.

    ``date`` is a single-day filter (YYYY-MM-DD). It uses
    ``substr(timestamp, 1, 10) = ?`` so that timezone-naive ISO
    strings (the format Tesla writes into filenames and that the
    indexer copies into ``waypoints.timestamp`` /
    ``detected_events.timestamp``) bucket correctly. SQLite's
    ``date()`` function would mis-bucket any row that ever gained a
    ``Z`` or ``+offset`` suffix, so ``substr`` is the safer
    contract. ``date`` and ``date_from``/``date_to`` are
    independent: passing all three narrows progressively.
    """
    conn = _init_db(db_path)
    try:
        sql = "SELECT * FROM detected_events WHERE 1=1"
        params = []

        if event_type:
            sql += " AND event_type = ?"
            params.append(event_type)
        if severity:
            sql += " AND severity = ?"
            params.append(severity)
        if bbox:
            min_lat, min_lon, max_lat, max_lon = bbox
            sql += " AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
            params.extend([min_lat, max_lat, min_lon, max_lon])
        if date_from:
            sql += " AND timestamp >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND timestamp <= ?"
            params.append(date_to)
        if date:
            sql += " AND substr(timestamp, 1, 10) = ?"
            params.append(date)

        sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@_with_db_retry
def query_days(db_path: str, limit: int = 60,
               min_distance_km: float = 0.05) -> List[dict]:
    """Aggregate trips and events by local-day for the day navigator.

    Returns one row per day that has either at least one qualifying
    trip (``distance_km >= min_distance_km``) or at least one
    detected event. Rows are ordered most-recent-day first.

    Each returned dict has:
      * ``date`` — ISO ``YYYY-MM-DD`` string
      * ``trip_count`` — qualifying trip count for the day
      * ``total_distance_km`` — sum of qualifying trip distances
      * ``event_count`` — total detected events
      * ``sentry_count`` — events with ``event_type='sentry'``
      * ``first_start`` — earliest trip ``start_time`` of the day
        (NULL if the day is event-only)
      * ``last_end`` — latest trip ``end_time`` (or ``start_time`` if
        end is missing) — NULL if the day is event-only

    Day-bucketing rule: ``substr(<column>, 1, 10)``. NEVER
    ``date(<column>)`` — see :func:`query_events` for rationale.

    Important: trips are filtered the same way ``/api/trips`` filters
    them (``COALESCE(distance_km, 0) >= min_distance_km``, default
    50 m). Without this, the day card would advertise "3 trips" while
    the map only shows 1 because the other two are below the
    distance threshold.

    Performance: a single CTE-based query on indexed columns
    (``idx_trips_day``, ``idx_events_day`` — expression indexes on
    ``substr(<column>, 1, 10)`` introduced in schema v8). Expected
    runtime O(days × trips_per_day) — well under 50 ms even with
    thousands of trips.
    """
    if min_distance_km is None or min_distance_km < 0:
        min_distance_km = 0.0
    if limit is None or limit <= 0:
        limit = 60

    conn = _init_db(db_path)
    try:
        sql = """
            WITH trip_days AS (
                SELECT substr(start_time, 1, 10)            AS day,
                       COUNT(*)                             AS trip_count,
                       COALESCE(SUM(distance_km), 0)        AS total_distance_km,
                       0                                    AS event_count,
                       0                                    AS sentry_count,
                       MIN(start_time)                      AS first_start,
                       MAX(COALESCE(end_time, start_time))  AS last_end
                  FROM trips
                 WHERE start_time IS NOT NULL
                   AND COALESCE(distance_km, 0) >= ?
                 GROUP BY day
            ),
            event_days AS (
                SELECT substr(timestamp, 1, 10)             AS day,
                       0                                    AS trip_count,
                       0.0                                  AS total_distance_km,
                       COUNT(*)                             AS event_count,
                       SUM(CASE WHEN event_type='sentry' THEN 1 ELSE 0 END) AS sentry_count,
                       NULL                                 AS first_start,
                       NULL                                 AS last_end
                  FROM detected_events
                 WHERE timestamp IS NOT NULL
                 GROUP BY day
            )
            SELECT day                                      AS date,
                   SUM(trip_count)                          AS trip_count,
                   SUM(total_distance_km)                   AS total_distance_km,
                   SUM(event_count)                         AS event_count,
                   SUM(sentry_count)                        AS sentry_count,
                   MIN(first_start)                         AS first_start,
                   MAX(last_end)                            AS last_end
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
        rows = conn.execute(sql, (min_distance_km, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@_with_db_retry
def query_day_routes(db_path: str, date_str: str,
                     min_distance_km: float = 0.05) -> Dict[str, Any]:
    """Return all trip routes (with waypoints) that started on ``date_str``.

    ``date_str`` must be ISO ``YYYY-MM-DD``; the caller is expected
    to validate the format before calling. Day-bucketing is by
    ``substr(start_time, 1, 10)`` — a midnight-spanning trip belongs
    to the day it started, not the day it ended (matches
    :func:`query_days`).

    Returns ``{'trips': [...]}`` where each trip has the same
    metadata fields as :func:`query_trips` plus a ``waypoints`` list
    sorted by ``timestamp ASC`` (with ``id ASC`` as a tiebreaker).
    Sorting by id alone is NOT sufficient: when the v2->v3 trip-
    merge migration combines two originally-separate trips, or when
    a late-arriving video gets indexed into an existing trip (boot
    catch-up scan, file watcher, ArchivedClips re-discovery), the
    new waypoints land with higher ids but their timestamps fall in
    the middle of the existing trip's time range. Walking those in
    id-order draws long straight diagonals across the map. Sorting
    by timestamp restores the true chronological sequence.

    Waypoints are NOT post-processed — callers (i.e. the blueprint)
    are responsible for path normalization (``ArchivedClips`` prefix
    stripping) so the service stays free of presentation concerns.

    Performance: one INNER JOIN; ``idx_trips_day`` (expression index
    on ``substr(start_time, 1, 10)``, schema v8) makes the date filter
    O(log n), then ``idx_waypoints_trip`` covers the join. All trip
    waypoints come back in one round trip, then we group in Python.
    For the worst-case 10-trip day with 500 waypoints each (5000
    rows), this is well under 100 ms on a Pi Zero 2 W.

    Trips with zero waypoints are excluded by the INNER JOIN —
    those wouldn't render on the map anyway. The day card's
    ``trip_count`` from :func:`query_days` may therefore exceed
    ``len(result['trips'])`` if some trips were drift artifacts
    with no GPS — that's surfaced in the UI as expected.
    """
    if min_distance_km is None or min_distance_km < 0:
        min_distance_km = 0.0

    conn = _init_db(db_path)
    try:
        sql = """
            SELECT t.id                AS trip_id,
                   t.start_time        AS start_time,
                   t.end_time          AS end_time,
                   t.distance_km       AS distance_km,
                   t.duration_seconds  AS duration_seconds,
                   t.start_lat         AS start_lat,
                   t.start_lon         AS start_lon,
                   t.end_lat           AS end_lat,
                   t.end_lon           AS end_lon,
                   t.source_folder     AS source_folder,
                   w.id                AS waypoint_id,
                   w.timestamp         AS w_timestamp,
                   w.lat               AS w_lat,
                   w.lon               AS w_lon,
                   w.heading           AS w_heading,
                   w.speed_mps         AS w_speed_mps,
                   w.acceleration_x    AS w_acceleration_x,
                   w.acceleration_y    AS w_acceleration_y,
                   w.gear              AS w_gear,
                   w.autopilot_state   AS w_autopilot_state,
                   w.steering_angle    AS w_steering_angle,
                   w.brake_applied     AS w_brake_applied,
                   w.blinker_on_left   AS w_blinker_on_left,
                   w.blinker_on_right  AS w_blinker_on_right,
                   w.video_path        AS w_video_path,
                   w.frame_offset      AS w_frame_offset
              FROM trips t
              JOIN waypoints w ON w.trip_id = t.id
             WHERE substr(t.start_time, 1, 10) = ?
               AND COALESCE(t.distance_km, 0) >= ?
             ORDER BY t.start_time DESC, w.timestamp ASC, w.id ASC
        """
        rows = conn.execute(sql, (date_str, min_distance_km)).fetchall()

        # Group rows by trip_id, preserving the SELECT order (start_time DESC).
        trips_by_id: Dict[int, dict] = {}
        order: List[int] = []
        for row in rows:
            trip_id = row['trip_id']
            trip = trips_by_id.get(trip_id)
            if trip is None:
                trip = {
                    'trip_id': trip_id,
                    'start_time': row['start_time'],
                    'end_time': row['end_time'],
                    'distance_km': row['distance_km'],
                    'duration_seconds': row['duration_seconds'],
                    'start_lat': row['start_lat'],
                    'start_lon': row['start_lon'],
                    'end_lat': row['end_lat'],
                    'end_lon': row['end_lon'],
                    'source_folder': row['source_folder'],
                    'waypoints': [],
                }
                trips_by_id[trip_id] = trip
                order.append(trip_id)
            trip['waypoints'].append({
                'id': row['waypoint_id'],
                'timestamp': row['w_timestamp'],
                'lat': row['w_lat'],
                'lon': row['w_lon'],
                'heading': row['w_heading'],
                'speed_mps': row['w_speed_mps'],
                'acceleration_x': row['w_acceleration_x'],
                'acceleration_y': row['w_acceleration_y'],
                'gear': row['w_gear'],
                'autopilot_state': row['w_autopilot_state'],
                'steering_angle': row['w_steering_angle'],
                'brake_applied': row['w_brake_applied'],
                'blinker_on_left': row['w_blinker_on_left'],
                'blinker_on_right': row['w_blinker_on_right'],
                'video_path': row['w_video_path'],
                'frame_offset': row['w_frame_offset'],
            })

        # Walk each trip's waypoints and stamp ``gap_after = True`` on
        # every waypoint that is followed by a polyline-breaking gap
        # (see ``_is_gap_between`` for the criteria). The frontend
        # renderer ends the current polyline whenever it sees this
        # flag, so a 6-minute parking break or an SEI clock-skew zigzag
        # no longer renders as a long straight diagonal across the map.
        # The flag is omitted when there's no gap so payload size on
        # the wire is unchanged for clean trips.
        for trip in trips_by_id.values():
            wps = trip['waypoints']
            for i in range(len(wps) - 1):
                if _is_gap_between(
                    wps[i].get('timestamp'), wps[i].get('lat'), wps[i].get('lon'),
                    wps[i + 1].get('timestamp'), wps[i + 1].get('lat'), wps[i + 1].get('lon'),
                ):
                    wps[i]['gap_after'] = True

        return {'trips': [trips_by_id[tid] for tid in order]}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Trip-playability check (Issue #77)
# ---------------------------------------------------------------------------
#
# The map's disambiguation popup needs to know whether a trip's videos still
# exist on disk so it can hide "ghost" trips whose RecentClips footage Tesla
# has overwritten. The check is filesystem-bound, so we cache the per-day
# result for 60 s — clip rotation by Tesla is on the order of hours, and the
# stale-scan / file-watcher subsystems publish authoritative state changes
# inside that window anyway.
#
# Cache shape: ``{date_str: (computed_at_monotonic, {trip_id: bool})}``.
# Thread-safe so concurrent Flask request handlers can share results.

_PLAYABLE_TRIPS_CACHE: Dict[str, Tuple[float, Dict[int, bool]]] = {}
_PLAYABLE_TRIPS_CACHE_LOCK = threading.Lock()
_PLAYABLE_TRIPS_TTL_SECONDS = 60.0


def _resolve_video_path_on_disk(video_path: str,
                                teslacam_path: Optional[str],
                                archive_dir: Optional[str]) -> bool:
    """Return ``True`` if ``video_path`` resolves to a real file on disk.

    Mirrors the resolution that ``videos.stream_video`` performs at
    playback time, so this check matches what playback would actually
    serve. Order:

      1. If the path begins with ``ArchivedClips/``, look in
         ``archive_dir`` (the SD-card archive root).
      2. Else, look at ``<teslacam_path>/<relative path>`` (handles
         ``RecentClips/foo.mp4``, ``SavedClips/<event>/foo.mp4``,
         ``SentryClips/<event>/foo.mp4``, and bare basenames).
      3. Fall back to ``<archive_dir>/<basename>`` so a clip that was
         rotated out of RecentClips but copied to ArchivedClips by the
         archive job is still considered playable.

    Args:
        video_path: Relative ``waypoints.video_path`` value from the DB.
            Must be a non-empty string.
        teslacam_path: TeslaCam mount root (RO in present mode, RW in
            edit mode), or ``None`` if the gadget is mid-transition.
        archive_dir: ARCHIVE_DIR from config when ARCHIVE_ENABLED is
            true, else ``None``.

    Returns:
        ``True`` iff at least one candidate file exists.
    """
    if not video_path:
        return False

    norm = video_path.replace('\\', '/').lstrip('/')
    parts = [p for p in norm.split('/') if p]
    if not parts:
        return False

    # Defense-in-depth: ``waypoints.video_path`` is written by our own
    # indexer and is always one of the canonical relative shapes
    # (``RecentClips/<basename>``, ``ArchivedClips/<basename>``,
    # ``SavedClips/<event>/<basename>``, ``SentryClips/<event>/<basename>``,
    # or a bare basename). A path with ``..`` segments or an absolute
    # form would only appear from a corrupt row or a legacy path —
    # reject the natural-location lookup in that case to keep this
    # function from probing arbitrary filesystem locations. The archive
    # basename fallback below still finds genuinely-archived clips.
    if any(p == '..' for p in parts):
        # Only probe the archive when the basename is a real filename;
        # ``parts[-1]`` of ``..`` (e.g. ``foo/..``) would otherwise
        # cause ``os.path.join`` to probe ``archive_dir``'s parent.
        # ``os.path.isfile`` on a directory returns False so this is
        # not exploitable, but the probe pattern is sketchy — gate it.
        if archive_dir and parts[-1] not in ('..', ''):
            archive_path = os.path.join(archive_dir, parts[-1])
            if os.path.isfile(archive_path):
                return True
        return False

    # Direct ArchivedClips reference: serve from SD-card archive root.
    if parts[0] == 'ArchivedClips':
        if archive_dir:
            archive_path = os.path.join(archive_dir, parts[-1])
            if os.path.isfile(archive_path):
                return True
        return False

    # Natural location under the TeslaCam mount.
    if teslacam_path:
        candidate = os.path.join(teslacam_path, *parts)
        if os.path.isfile(candidate):
            return True

    # ArchivedClips fallback: a RecentClips clip that was rotated out
    # may still exist as an archived copy on the SD card.
    if archive_dir:
        archive_path = os.path.join(archive_dir, parts[-1])
        if os.path.isfile(archive_path):
            return True

    return False


def playable_trips_for_date(db_path: str, date_str: str,
                            teslacam_path: Optional[str],
                            archive_dir: Optional[str],
                            ttl_seconds: float = (
                                _PLAYABLE_TRIPS_TTL_SECONDS),
                            ) -> Dict[int, bool]:
    """Return ``{trip_id: has_playable_video}`` for every trip on ``date_str``.

    A trip is considered playable iff at least one of its waypoints'
    ``video_path`` values resolves to a real file on disk (see
    :func:`_resolve_video_path_on_disk` for the resolution rules).

    Results are cached per-date for ``ttl_seconds`` (default 60 s)
    behind a module-level lock so concurrent Flask request handlers
    share the work. Within a trip, unique ``video_path`` values are
    stat'd at most once and the iteration short-circuits on the first
    hit — a typical trip has a single video referenced by all its
    waypoints, so the average cost is one ``isfile`` per trip.

    Args:
        db_path: Path to the geodata.db.
        date_str: ISO ``YYYY-MM-DD`` day to evaluate. Caller must
            validate format before calling.
        teslacam_path: TeslaCam mount root, or ``None`` if unavailable.
        archive_dir: ARCHIVE_DIR or ``None`` when archive is disabled.
        ttl_seconds: Cache TTL. Tests can pass a smaller value.

    Returns:
        Dict mapping ``trip_id`` (int) → ``bool``. Trips with no
        ``video_path`` waypoints are reported as ``False``. Empty dict
        when no trips exist on the given date.
    """
    now = time.monotonic()
    with _PLAYABLE_TRIPS_CACHE_LOCK:
        cached = _PLAYABLE_TRIPS_CACHE.get(date_str)
        if cached is not None:
            computed_at, payload = cached
            if (now - computed_at) < ttl_seconds:
                return dict(payload)

    conn = _init_db(db_path)
    try:
        # Single LEFT JOIN: every trip on the date appears at least
        # once. Trips with no video_path waypoints come back as a
        # single row with video_path NULL — we'll surface them as
        # ``False`` in the result map. This avoids a second round
        # trip and keeps the iteration order stable.
        rows = conn.execute(
            """
            SELECT t.id AS trip_id,
                   w.video_path AS video_path
              FROM trips t
              LEFT JOIN waypoints w
                ON w.trip_id = t.id
               AND w.video_path IS NOT NULL
               AND w.video_path != ''
             WHERE substr(t.start_time, 1, 10) = ?
            """,
            (date_str,),
        ).fetchall()
    finally:
        conn.close()

    # Group unique video_paths per trip so we stat each clip at most
    # once. The LEFT JOIN guarantees every qualifying trip has at
    # least one row, even if all its waypoints had a NULL video_path
    # (those rows have ``video_path == None`` which we drop here).
    by_trip: Dict[int, set] = {}
    for row in rows:
        trip_id = row['trip_id']
        if trip_id not in by_trip:
            by_trip[trip_id] = set()
        vp = row['video_path']
        if vp:
            by_trip[trip_id].add(vp)

    # Per-clip stat cache scoped to this call so two trips referencing
    # the same clip (rare but possible after a v3 trip merge) don't
    # double-stat.
    file_cache: Dict[str, bool] = {}

    def _is_playable(p: str) -> bool:
        v = file_cache.get(p)
        if v is None:
            v = _resolve_video_path_on_disk(
                p, teslacam_path, archive_dir,
            )
            file_cache[p] = v
        return v

    result: Dict[int, bool] = {}
    for trip_id, paths in by_trip.items():
        playable = False
        for p in paths:
            if _is_playable(p):
                playable = True
                break
        result[trip_id] = playable

    # Skip caching when ``teslacam_path`` is None (mid-mode-transition):
    # every recent-only RecentClips trip would resolve to ``False`` here,
    # and caching that all-False payload would hide real trips from the
    # disambiguation chooser for the full TTL even after the mount is
    # back up. Recompute on the next call instead — the work is cheap
    # (single LEFT JOIN + already-cached stats).
    if teslacam_path is not None:
        with _PLAYABLE_TRIPS_CACHE_LOCK:
            _PLAYABLE_TRIPS_CACHE[date_str] = (time.monotonic(), result)
            # Bound cache growth: a single device only ever has a few
            # hundred days of history, but stale entries are pure ballast.
            if len(_PLAYABLE_TRIPS_CACHE) > 64:
                # Drop the oldest entries; cheap heuristic, no LRU needed.
                oldest = sorted(
                    _PLAYABLE_TRIPS_CACHE.items(),
                    key=lambda kv: kv[1][0],
                )[: len(_PLAYABLE_TRIPS_CACHE) - 32]
                for k, _ in oldest:
                    _PLAYABLE_TRIPS_CACHE.pop(k, None)

    return dict(result)


def _reset_playable_trips_cache_for_tests() -> None:
    """Clear the playable-trips cache. Intended for unit tests only."""
    with _PLAYABLE_TRIPS_CACHE_LOCK:
        _PLAYABLE_TRIPS_CACHE.clear()


@_with_db_retry
def query_all_routes_simplified(
    db_path: str,
    min_distance_km: float = 0.05,
    epsilon_m: float = 8.0,
    max_points_per_trip: int = 200,
) -> List[dict]:
    """Return every indexed trip with shape-aware simplified
    waypoints for the "All time" map overview.

    Each trip's waypoints are simplified using the Ramer-Douglas-
    Peucker algorithm (:func:`_simplify_polyline_rdp`) with
    ``epsilon_m`` as the per-point perpendicular-distance tolerance
    (default 8 m, which is above typical GPS noise yet tight enough
    that any road feature is visually preserved at any zoom). The
    earlier stride-sampling implementation cut straight across road
    curves between kept points, producing visibly wrong polylines on
    long trips — RDP fixes that by keeping the points whose
    perpendicular distance to the simplified path exceeds the
    tolerance, so corners survive and straight stretches collapse
    naturally.

    ``max_points_per_trip`` (default 200) is a safety cap applied
    after RDP; only pathologically zigzag trips would ever hit it.
    Trips below ``min_distance_km`` and trips with fewer than 2
    valid waypoints are excluded — same parity guarantees as
    :func:`query_trips` and :func:`query_day_routes`.

    Returns trips ordered by ``start_time`` DESC. Each trip carries
    enough metadata for the client to drill into the correct day on
    polyline click (``date``) plus the simplified waypoint list
    (only ``lat``, ``lon``, ``speed_mps`` — per-clip drilldown is
    delegated to :func:`query_day_routes` when the user opens a day).

    Performance: one SQL round trip fetches every waypoint for every
    qualifying trip; RDP per trip is O(n log n) on average (O(n^2)
    worst case for pathological zigzags). For a 22-trip / ~10k-
    waypoint database this returns in ~150 ms on a Pi Zero 2 W,
    producing ~30 points per typical trip — substantially fewer
    points than the old stride sampler AND a visually correct
    polyline.
    """
    if min_distance_km is None or min_distance_km < 0:
        min_distance_km = 0.0
    if epsilon_m is None or epsilon_m < 0:
        epsilon_m = 0.0
    if max_points_per_trip is None or max_points_per_trip < 2:
        max_points_per_trip = 2

    conn = _init_db(db_path)
    try:
        # Single fetch of every waypoint for every qualifying trip,
        # ordered so trips group together newest-first and waypoints
        # within a trip stay chronological. RDP needs the full
        # sequence (per trip) — there's no SQL-side simplification
        # we can do that preserves shape.
        sql = """
            SELECT t.id                AS trip_id,
                   t.start_time        AS start_time,
                   t.end_time          AS end_time,
                   t.start_lat         AS start_lat,
                   t.start_lon         AS start_lon,
                   t.end_lat           AS end_lat,
                   t.end_lon           AS end_lon,
                   t.distance_km       AS distance_km,
                   t.duration_seconds  AS duration_seconds,
                   substr(t.start_time, 1, 10) AS date,
                   w.timestamp         AS w_timestamp,
                   w.lat               AS lat,
                   w.lon               AS lon,
                   w.speed_mps         AS speed_mps
              FROM trips t
              JOIN waypoints w ON w.trip_id = t.id
             WHERE t.start_time IS NOT NULL
               AND COALESCE(t.distance_km, 0) >= ?
               AND w.lat IS NOT NULL
               AND w.lon IS NOT NULL
             ORDER BY t.start_time DESC, w.timestamp ASC, w.id ASC
        """
        rows = conn.execute(sql, (min_distance_km,)).fetchall()

        trips_by_id: Dict[int, dict] = {}
        order: List[int] = []
        raw_by_id: Dict[int, List[tuple]] = {}
        for row in rows:
            trip_id = row['trip_id']
            trip = trips_by_id.get(trip_id)
            if trip is None:
                trip = {
                    'trip_id': trip_id,
                    'date': row['date'],
                    'start_time': row['start_time'],
                    'end_time': row['end_time'],
                    'start_lat': row['start_lat'],
                    'start_lon': row['start_lon'],
                    'end_lat': row['end_lat'],
                    'end_lon': row['end_lon'],
                    'distance_km': row['distance_km'],
                    'duration_seconds': row['duration_seconds'],
                }
                trips_by_id[trip_id] = trip
                order.append(trip_id)
                raw_by_id[trip_id] = []
            raw_by_id[trip_id].append(
                (row['w_timestamp'], row['lat'], row['lon'], row['speed_mps'])
            )

        # Split each trip into gap-free segments BEFORE applying RDP,
        # then RDP per segment, then concatenate the simplified
        # waypoints with a ``gap_after`` flag at every segment boundary.
        # Two reasons we split before RDP rather than just flagging the
        # raw boundary points and trusting RDP to keep them:
        #   1. RDP picks points by perpendicular distance from a chord
        #      that crosses the gap. The chord IS the bug: it picks the
        #      gap endpoints as outliers, then keeps an apparently
        #      "smooth" line that visibly cuts across the map.
        #   2. Per-segment RDP gives each real segment its own epsilon
        #      budget, so a short pre-gap subroute (say a 3-point loop
        #      around a parking lot) doesn't get crushed by the noise
        #      floor of a multi-mile freeway segment in the same trip.
        # Trips with <2 valid waypoints are dropped — they can't render
        # a polyline.
        result: List[dict] = []
        for trip_id in order:
            raw = raw_by_id[trip_id]
            if len(raw) < 2:
                continue

            segments: List[List[tuple]] = []
            current: List[tuple] = [raw[0]]
            for i in range(1, len(raw)):
                a, b = raw[i - 1], raw[i]
                if _is_gap_between(a[0], a[1], a[2], b[0], b[1], b[2]):
                    segments.append(current)
                    current = [b]
                else:
                    current.append(b)
            segments.append(current)

            output: List[dict] = []
            for seg_idx, seg in enumerate(segments):
                if len(seg) < 2:
                    # Single-point segment can't render but its endpoint
                    # should still terminate any prior polyline at the
                    # gap; emit it carrying the gap flag so the renderer
                    # closes the prior segment cleanly.
                    seg_out = [{
                        'lat': seg[0][1], 'lon': seg[0][2],
                        'speed_mps': seg[0][3],
                    }]
                else:
                    latlons = [(p[1], p[2]) for p in seg]
                    kept = _simplify_polyline_rdp(latlons, epsilon_m=epsilon_m)
                    seg_out = [
                        {'lat': seg[i][1], 'lon': seg[i][2],
                         'speed_mps': seg[i][3]}
                        for i in kept
                    ]
                if seg_idx < len(segments) - 1 and seg_out:
                    seg_out[-1]['gap_after'] = True
                output.extend(seg_out)

            if len(output) > max_points_per_trip:
                # Pathological case: even after per-segment RDP we have
                # too many points. Stride down to the cap, but always
                # keep the very last point so the polyline terminates
                # at the actual trip end. Note: stride-sampling can
                # drop a ``gap_after`` flagged point — re-stamp the
                # flag onto whichever simplified point now precedes
                # each gap so the renderer still breaks correctly.
                step = max(1, len(output) // max_points_per_trip)
                gap_after_lats = {
                    (p['lat'], p['lon']) for p in output if p.get('gap_after')
                }
                stride_kept = output[::step]
                if stride_kept[-1] is not output[-1]:
                    stride_kept.append(output[-1])
                # Re-apply gap_after on the last surviving point of each
                # original gap-bounded segment.
                seen_gaps = set()
                for p in stride_kept:
                    if (p['lat'], p['lon']) in gap_after_lats:
                        p['gap_after'] = True
                        seen_gaps.add((p['lat'], p['lon']))
                # If a gap point was dropped entirely, fall back to
                # flagging the nearest surviving simplified point that
                # was originally before that gap.
                missed = gap_after_lats - seen_gaps
                if missed:
                    flat_indexed = list(enumerate(output))
                    for ml, mlon in missed:
                        # Find the original index of the missed gap point.
                        orig_idx = next(
                            (i for i, p in flat_indexed
                             if p['lat'] == ml and p['lon'] == mlon),
                            None,
                        )
                        if orig_idx is None:
                            continue
                        # Walk back to the nearest stride-kept point at
                        # or before that original index.
                        for sp in reversed(stride_kept):
                            si = next(
                                (i for i, p in flat_indexed if p is sp),
                                None,
                            )
                            if si is not None and si <= orig_idx:
                                sp['gap_after'] = True
                                break
                output = stride_kept

            trip = trips_by_id[trip_id]
            trip['waypoints'] = output
            result.append(trip)

        return result
    finally:
        conn.close()


@_with_db_retry
def get_stats(db_path: str) -> dict:
    """Get summary statistics from the geo-index database."""
    conn = _init_db(db_path)
    try:
        trip_count = conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0]
        waypoint_count = conn.execute("SELECT COUNT(*) FROM waypoints").fetchone()[0]
        event_count = conn.execute("SELECT COUNT(*) FROM detected_events").fetchone()[0]
        file_count = conn.execute("SELECT COUNT(*) FROM indexed_files").fetchone()[0]
        # Count only files that produced GPS waypoints (meaningful for map display)
        mapped_file_count = conn.execute(
            "SELECT COUNT(*) FROM indexed_files WHERE waypoint_count > 0"
        ).fetchone()[0]

        total_distance = conn.execute(
            "SELECT COALESCE(SUM(distance_km), 0) FROM trips"
        ).fetchone()[0]
        total_duration = conn.execute(
            "SELECT COALESCE(SUM(duration_seconds), 0) FROM trips"
        ).fetchone()[0]

        event_breakdown = {}
        for row in conn.execute(
            "SELECT event_type, COUNT(*) as cnt FROM detected_events GROUP BY event_type"
        ).fetchall():
            event_breakdown[row['event_type']] = row['cnt']

        return {
            'trip_count': trip_count,
            'waypoint_count': waypoint_count,
            'event_count': event_count,
            'indexed_file_count': file_count,
            'mapped_file_count': mapped_file_count,
            'total_distance_km': round(total_distance, 2),
            'total_duration_seconds': total_duration,
            'event_breakdown': event_breakdown,
            'indexer_status': _get_worker_status_for_stats(),
        }
    finally:
        conn.close()


@_with_db_retry
def get_driving_stats(db_path: str) -> dict:
    """Get driving behavior statistics for the analytics dashboard."""
    conn = _init_db(db_path)
    try:
        trip_count = conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0]
        if trip_count == 0:
            return {'has_data': False}

        total_distance = conn.execute(
            "SELECT COALESCE(SUM(distance_km), 0) FROM trips"
        ).fetchone()[0]
        total_duration = conn.execute(
            "SELECT COALESCE(SUM(duration_seconds), 0) FROM trips"
        ).fetchone()[0]
        avg_speed = conn.execute(
            "SELECT COALESCE(AVG(speed_mps), 0) FROM waypoints WHERE speed_mps > 0.5"
        ).fetchone()[0]
        max_speed = conn.execute(
            "SELECT COALESCE(MAX(speed_mps), 0) FROM waypoints"
        ).fetchone()[0]

        # FSD usage
        total_wp = conn.execute("SELECT COUNT(*) FROM waypoints").fetchone()[0]
        fsd_wp = conn.execute(
            "SELECT COUNT(*) FROM waypoints WHERE autopilot_state IN ('SELF_DRIVING', 'AUTOSTEER')"
        ).fetchone()[0]
        fsd_pct = round((fsd_wp / total_wp * 100) if total_wp > 0 else 0, 1)

        # Events per 100 km (driving score proxy)
        event_count = conn.execute("SELECT COUNT(*) FROM detected_events").fetchone()[0]
        warning_count = conn.execute(
            "SELECT COUNT(*) FROM detected_events WHERE severity IN ('warning', 'critical')"
        ).fetchone()[0]
        events_per_100km = round(
            (warning_count / total_distance * 100) if total_distance > 0 else 0, 1
        )

        return {
            'has_data': True,
            'trip_count': trip_count,
            'total_distance_km': round(total_distance, 1),
            'total_distance_mi': round(total_distance * 0.621371, 1),
            'total_duration_hours': round(total_duration / 3600, 1),
            'avg_speed_mph': round(avg_speed * 2.23694, 1),
            'max_speed_mph': round(max_speed * 2.23694, 1),
            'fsd_usage_pct': fsd_pct,
            'total_events': event_count,
            'warning_events': warning_count,
            'events_per_100km': events_per_100km,
        }
    finally:
        conn.close()


@_with_db_retry
def get_event_chart_data(db_path: str) -> dict:
    """Get event data formatted for Chart.js rendering."""
    conn = _init_db(db_path)
    try:
        # Events by type
        type_rows = conn.execute(
            """SELECT event_type, COUNT(*) as cnt
               FROM detected_events GROUP BY event_type ORDER BY cnt DESC"""
        ).fetchall()
        by_type = {
            'labels': [r['event_type'].replace('_', ' ').title() for r in type_rows],
            'values': [r['cnt'] for r in type_rows],
        }

        # Events by severity
        sev_rows = conn.execute(
            """SELECT severity, COUNT(*) as cnt
               FROM detected_events GROUP BY severity ORDER BY
               CASE severity WHEN 'critical' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END"""
        ).fetchall()
        by_severity = {
            'labels': [r['severity'].title() for r in sev_rows],
            'values': [r['cnt'] for r in sev_rows],
            'colors': [
                '#dc3545' if r['severity'] == 'critical'
                else '#ffc107' if r['severity'] == 'warning'
                else '#17a2b8'
                for r in sev_rows
            ],
        }

        # Events over time (by day, last 30 days)
        time_rows = conn.execute(
            """SELECT DATE(timestamp) as day, COUNT(*) as cnt
               FROM detected_events
               WHERE timestamp >= DATE('now', '-30 days')
               GROUP BY day ORDER BY day"""
        ).fetchall()
        over_time = {
            'labels': [r['day'] for r in time_rows],
            'values': [r['cnt'] for r in time_rows],
        }

        # FSD engage vs manual over time (by day)
        fsd_rows = conn.execute(
            """SELECT DATE(timestamp) as day,
                      SUM(CASE WHEN autopilot_state IN ('SELF_DRIVING','AUTOSTEER') THEN 1 ELSE 0 END) as fsd,
                      SUM(CASE WHEN autopilot_state NOT IN ('SELF_DRIVING','AUTOSTEER') THEN 1 ELSE 0 END) as manual
               FROM waypoints
               WHERE timestamp >= DATE('now', '-30 days')
               GROUP BY day ORDER BY day"""
        ).fetchall()
        fsd_timeline = {
            'labels': [r['day'] for r in fsd_rows],
            'fsd': [r['fsd'] for r in fsd_rows],
            'manual': [r['manual'] for r in fsd_rows],
        }

        return {
            'by_type': by_type,
            'by_severity': by_severity,
            'over_time': over_time,
            'fsd_timeline': fsd_timeline,
        }
    finally:
        conn.close()
