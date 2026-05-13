"""
TeslaUSB Mapping & Geo-Indexer Service.

Manages a SQLite database of GPS waypoints, trips, and detected driving events
extracted from Tesla dashcam SEI telemetry. Provides background indexing with
rule-based event detection.

Designed for Pi Zero 2 W: processes one video at a time, uses generators,
and stores results in a lightweight SQLite database.
"""

import functools
import json
import logging
import math
import os
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase 3c.2 (#100): re-export schema/migration symbols.
# ``_init_db`` is the connection factory used by every query function in
# this module and by ~100 test sites. Re-exporting it (along with
# ``_backup_db``, ``_SCHEMA_VERSION``, and ``_BACKUP_RETENTION``) keeps all
# existing call sites working unchanged after the move.
# New code should import from ``services.mapping_migrations`` directly.
# ---------------------------------------------------------------------------
from services.mapping_migrations import (  # noqa: E402,F401
    _BACKUP_RETENTION,
    _SCHEMA_SQL,
    _SCHEMA_VERSION,
    _backup_db,
    _init_db,
    _migrate_v2_to_v3,
    _migrate_v3_to_v4,
)


# ---------------------------------------------------------------------------
# Indexing Outcome Types
# ---------------------------------------------------------------------------

class IndexOutcome(Enum):
    """Possible outcomes when attempting to index a single video file.

    The queue worker dispatches on this value to decide whether to delete
    the queue row, retry later (with backoff or after the file ages), or
    purge stale DB rows. Every outcome maps to exactly one queue action,
    eliminating the historical ``(0, 0)`` ambiguity that meant any of
    seven different things (parse error, no GPS, too new, missing file,
    wrong camera, dedup skip, ...) and was unsafe for retry decisions.
    """

    INDEXED = 'indexed'                        # New waypoints/events written
    ALREADY_INDEXED = 'already_indexed'        # Canonical key present with data
    DUPLICATE_UPGRADED = 'duplicate_upgraded'  # RecentClips→ArchivedClips upgrade
    NO_GPS_RECORDED = 'no_gps_recorded'        # File parsed; no GPS; tracked
    NOT_FRONT_CAMERA = 'not_front_camera'      # Skip non-front-cam clip
    TOO_NEW = 'too_new'                        # mtime < 120s ago — retry later
    FILE_MISSING = 'file_missing'              # File no longer exists; purge DB
    PARSE_ERROR = 'parse_error'                # SEI parse exception
    DB_BUSY = 'db_busy'                        # SQLite locked; transient retry


# Outcomes after which the queue row can be deleted. PARSE_ERROR / TOO_NEW /
# DB_BUSY require backoff or scheduled retry, so they are not terminal.
_TERMINAL_OUTCOMES = frozenset({
    IndexOutcome.INDEXED,
    IndexOutcome.ALREADY_INDEXED,
    IndexOutcome.DUPLICATE_UPGRADED,
    IndexOutcome.NO_GPS_RECORDED,
    IndexOutcome.NOT_FRONT_CAMERA,
    IndexOutcome.FILE_MISSING,
})


@dataclass(frozen=True)
class IndexResult:
    """Structured outcome of indexing a single video file.

    Replaces the historical ``(waypoint_count, event_count)`` tuple. The
    ``outcome`` member is the source of truth for queue dispatch; the
    counts are informational (logging, status display).
    """

    outcome: IndexOutcome
    waypoints: int = 0
    events: int = 0
    error: Optional[str] = None

    @property
    def terminal(self) -> bool:
        """True iff the queue worker can safely delete this row.

        Note: ``FILE_MISSING`` is terminal for the queue (no point retrying)
        even though it triggers a separate DB cleanup pass. Worker dispatch
        is by-outcome, not by-property — ``terminal`` is a convenience for
        the common "delete this row" case.
        """
        return self.outcome in _TERMINAL_OUTCOMES

# Lazy-import SEI parser to avoid startup cost
_sei_parser = None


def _get_sei_parser():
    global _sei_parser
    if _sei_parser is None:
        from services import sei_parser
        _sei_parser = sei_parser
    return _sei_parser


def _is_transient_db_error(exc: BaseException) -> bool:
    """Return True if this is a transient SQLite error worth retrying.

    On a Pi Zero 2 W under concurrent indexer + web load, SQLite can return
    "disk I/O error" (SQLITE_IOERR) or "database is locked" (SQLITE_BUSY)
    when the SD card is slow to fsync or shared-memory mmap fails. These
    almost always succeed on a second attempt with a fresh connection.
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return ('disk i/o error' in msg or 'database is locked' in msg
            or 'unable to open database file' in msg)


def _with_db_retry(fn: Callable) -> Callable:
    """Decorator: retry once on transient SQLite errors.

    Ensures a single bad connection state (typically caused by mmap
    exhaustion or fsync hiccups during heavy indexer load) doesn't turn
    into a permanently failing endpoint. The retry uses a fresh
    connection because each decorated function calls ``_init_db`` itself.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if not _is_transient_db_error(e):
                raise
            logger.warning("Transient DB error in %s (%s); retrying once",
                           fn.__name__, e)
            time.sleep(0.2)
            return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Database Schema & Migrations (Phase 3c.2 — moved to services.mapping_migrations)
# ---------------------------------------------------------------------------
#
# The schema DDL, version constants, backup helper, ``_init_db`` connection
# factory, and the v2/v3/v4 migrations now live in
# ``services.mapping_migrations``. ``_init_db``, ``_backup_db`` and
# ``_SCHEMA_VERSION`` are re-exported from this module (top-level import
# below) so the (very many) existing internal call sites and test imports
# continue to work unchanged. New code should import from
# ``services.mapping_migrations`` directly.


# Default trip gap, also used by the migration. Kept here so the migration
# can run before any per-call ``trip_gap_minutes`` argument is available.
_TRIP_GAP_MINUTES_DEFAULT = 5

# Safety bound on the post-insert merge loop. The migration uses 10000;
# match it so the runtime helper can recover from severe accumulated
# fragmentation (e.g. after a long indexer outage where many small
# trip fragments built up). Hitting this bound indicates a pathological
# data set worth investigating.
_MERGE_MAX_ITERATIONS = 10000


def _merge_adjacent_trips_for(conn: sqlite3.Connection,
                              anchor_trip_id: int,
                              gap_seconds: float) -> int:
    """Merge any other trip whose [start_time, end_time] window is within
    ``gap_seconds`` of the anchor's window. The lower trip id wins so
    the survivor is stable and references stay valid.

    This is the runtime defense against trip fragmentation when the
    indexer processes a drive's clips out of order. The matching SQL in
    :func:`_index_video` picks one trip per insert; this helper runs
    afterwards and stitches together any trips the new clip's waypoints
    bridged. Mirrors the v2→v3 migration's merge phase but is scoped to
    one anchor's neighbourhood per call so it costs only a handful of
    queries per indexed file.

    Returns the surviving trip id (which equals ``anchor_trip_id`` when
    anchor is the lowest-id trip in its merged cluster, otherwise the
    smaller id of the merge pair).

    Important: the caller is responsible for calling ``conn.commit()``
    after this returns. The helper writes through the connection's
    current transaction so insert + merge + stats recompute remain
    atomic — readers see either the pre-insert state or the fully
    merged state, never a half-merged window.

    Foreign-key safety: the schema declares
    ``trip_id REFERENCES trips(id) ON DELETE CASCADE`` on both
    ``waypoints`` and ``detected_events``, so this helper MUST update
    the child rows BEFORE deleting the dropped trip — otherwise the
    cascade would destroy waypoints we wanted to preserve.
    """
    survivor = anchor_trip_id

    for _ in range(_MERGE_MAX_ITERATIONS):
        # Refresh the survivor's bounds from waypoints. The bounds may
        # have changed in two ways since the last iteration: the caller
        # just inserted new waypoints, or the previous loop iteration
        # absorbed another trip's waypoints. Without this refresh, a
        # chain merge (A ↔ B ↔ C) would stop after one step because
        # the survivor's stale ``end_time`` does not reach C.
        bounds = conn.execute(
            "SELECT MIN(timestamp) AS s, MAX(timestamp) AS e "
            "FROM waypoints WHERE trip_id = ?",
            (survivor,),
        ).fetchone()
        if not bounds or bounds['s'] is None:
            return survivor
        conn.execute(
            "UPDATE trips SET start_time = ?, end_time = ? WHERE id = ?",
            (bounds['s'], bounds['e'], survivor),
        )

        # Find the lowest-id mergeable neighbour. Same window logic as
        # the matching SQL in _index_video and the migration:
        #   neighbour.start - survivor.end ≤ gap   (neighbour after)
        #   survivor.start - neighbour.end ≤ gap   (neighbour before)
        # Negative values (overlap) also satisfy ≤ gap. Order by id so
        # we always pick the smallest mergeable neighbour first; the
        # absolute pair we merge is then (min(survivor, candidate),
        # max(survivor, candidate)).
        #
        # Integer-second arithmetic via ``strftime('%s', ...)`` is used
        # instead of ``(julianday(a) - julianday(b)) * 86400`` because
        # the latter has floating-point error: a true 300-second gap
        # can yield 300.000022 and fail the ``<= 300`` boundary check,
        # silently leaving phantom-fragmented trips unmerged. The
        # strftime form is precise to one second, which is safely
        # within the 5-minute trip-gap semantic tolerance.
        candidate = conn.execute(
            """
            SELECT id FROM trips
            WHERE id != :survivor
              AND start_time IS NOT NULL AND end_time IS NOT NULL
              AND (CAST(strftime('%s', start_time) AS INTEGER)
                   - CAST(strftime('%s', :end_t) AS INTEGER)) <= :gap
              AND (CAST(strftime('%s', :start_t) AS INTEGER)
                   - CAST(strftime('%s', end_time) AS INTEGER)) <= :gap
            ORDER BY id
            LIMIT 1
            """,
            {'survivor': survivor,
             'start_t': bounds['s'], 'end_t': bounds['e'],
             'gap': gap_seconds},
        ).fetchone()
        if not candidate:
            return survivor

        keep_id = min(survivor, candidate['id'])
        drop_id = max(survivor, candidate['id'])

        # Update children first, then delete the parent. Reversing this
        # order would trip the ON DELETE CASCADE and silently destroy
        # the very rows we are trying to preserve.
        conn.execute(
            "UPDATE waypoints SET trip_id = ? WHERE trip_id = ?",
            (keep_id, drop_id),
        )
        conn.execute(
            "UPDATE detected_events SET trip_id = ? WHERE trip_id = ?",
            (keep_id, drop_id),
        )
        conn.execute("DELETE FROM trips WHERE id = ?", (drop_id,))
        survivor = keep_id

    raise RuntimeError(
        f"_merge_adjacent_trips_for: exceeded {_MERGE_MAX_ITERATIONS} "
        "iterations — possible infinite loop or pathological data"
    )


def _merge_all_adjacent_trip_pairs(conn: sqlite3.Connection,
                                    gap_seconds: float) -> int:
    """Sweep the whole ``trips`` table and merge every pair whose
    windows are within ``gap_seconds`` of each other.

    Used by:
      * the v2→v3 migration (cleans up duplicate trips from earlier
        indexer bugs);
      * the v8→v9 migration (one-shot repair of phantom-fragmented
        trips left over from the matching-SQL boundary bug);
      * future startup repair passes.

    Always uses integer-epoch arithmetic via ``strftime('%s', ...)``
    instead of ``(julianday(a) - julianday(b)) * 86400`` because the
    latter has floating-point error that silently leaves true
    ``gap_seconds``-apart pairs unmerged (see ``_merge_adjacent_trips_for``
    for the in-depth explanation).

    Foreign-key safety: updates ``waypoints`` and ``detected_events``
    BEFORE deleting the dropped trip, since both tables declare
    ``ON DELETE CASCADE`` on ``trip_id``.

    Returns the number of merge operations performed. The caller is
    responsible for ``conn.commit()``.

    Raises ``RuntimeError`` if more than ``_MERGE_MAX_ITERATIONS`` pairs
    are merged — a safety bound that triggers the migration's SAVEPOINT
    rollback rather than silently continuing forever on pathological
    data.
    """
    merged = 0
    for _ in range(_MERGE_MAX_ITERATIONS):
        pair = conn.execute(
            """SELECT a.id AS keep_id, b.id AS drop_id
               FROM trips a
               JOIN trips b
                 ON a.id < b.id
                AND a.start_time IS NOT NULL AND a.end_time IS NOT NULL
                AND b.start_time IS NOT NULL AND b.end_time IS NOT NULL
                AND (CAST(strftime('%s', b.start_time) AS INTEGER)
                     - CAST(strftime('%s', a.end_time) AS INTEGER)) <= ?
                AND (CAST(strftime('%s', a.start_time) AS INTEGER)
                     - CAST(strftime('%s', b.end_time) AS INTEGER)) <= ?
               LIMIT 1""",
            (gap_seconds, gap_seconds),
        ).fetchone()
        if not pair:
            return merged
        keep_id, drop_id = pair['keep_id'], pair['drop_id']
        conn.execute(
            "UPDATE waypoints SET trip_id = ? WHERE trip_id = ?",
            (keep_id, drop_id),
        )
        conn.execute(
            "UPDATE detected_events SET trip_id = ? WHERE trip_id = ?",
            (keep_id, drop_id),
        )
        # Refresh the survivor's bounds so the next iteration considers
        # the merged window when looking for further mergeable pairs.
        bounds = conn.execute(
            "SELECT MIN(timestamp) AS s, MAX(timestamp) AS e "
            "FROM waypoints WHERE trip_id = ?",
            (keep_id,),
        ).fetchone()
        if bounds and bounds['s'] is not None:
            conn.execute(
                "UPDATE trips SET start_time = ?, end_time = ? "
                "WHERE id = ?",
                (bounds['s'], bounds['e'], keep_id),
            )
        conn.execute("DELETE FROM trips WHERE id = ?", (drop_id,))
        merged += 1

    raise RuntimeError(
        f"_merge_all_adjacent_trip_pairs: exceeded "
        f"{_MERGE_MAX_ITERATIONS} iterations — possible infinite loop "
        "or pathological duplicate set"
    )


# ---------------------------------------------------------------------------
# Event Detection Rules
# ---------------------------------------------------------------------------

# Default thresholds (can be overridden via config.yaml mapping.event_detection)
DEFAULT_THRESHOLDS = {
    'harsh_brake_threshold': -4.0,        # m/s² (longitudinal)
    'emergency_brake_threshold': -7.0,
    'hard_accel_threshold': 3.5,
    'sharp_turn_lateral_g': 4.0,          # m/s² (lateral)
    'speed_limit_mps': 35.76,             # ~80 mph
    'fsd_disengage_detect': True,
}


def _detect_events(
    waypoints: list,
    thresholds: dict,
    video_path: str,
) -> List[dict]:
    """Run rule-based event detection over a list of waypoint dicts.

    Returns list of event dicts ready for database insertion.
    """
    events = []
    prev_autopilot = None

    for i, wp in enumerate(waypoints):
        accel_x = wp.get('acceleration_x', 0.0)
        accel_y = wp.get('acceleration_y', 0.0)
        speed = wp.get('speed_mps', 0.0)
        autopilot = wp.get('autopilot_state', 'NONE')

        # --- Harsh / Emergency Braking ---
        if accel_x <= thresholds.get('emergency_brake_threshold', -7.0):
            events.append({
                'timestamp': wp['timestamp'],
                'lat': wp['lat'], 'lon': wp['lon'],
                'event_type': 'emergency_brake',
                'severity': 'critical',
                'description': f'Emergency braking: {accel_x:.1f} m/s²',
                'video_path': video_path,
                'frame_offset': wp.get('frame_offset', 0),
                'metadata': json.dumps({'accel_x': accel_x, 'speed_mps': speed}),
            })
        elif accel_x <= thresholds.get('harsh_brake_threshold', -4.0):
            events.append({
                'timestamp': wp['timestamp'],
                'lat': wp['lat'], 'lon': wp['lon'],
                'event_type': 'harsh_brake',
                'severity': 'warning',
                'description': f'Harsh braking: {accel_x:.1f} m/s²',
                'video_path': video_path,
                'frame_offset': wp.get('frame_offset', 0),
                'metadata': json.dumps({'accel_x': accel_x, 'speed_mps': speed}),
            })

        # --- Hard Acceleration ---
        if accel_x >= thresholds.get('hard_accel_threshold', 3.5):
            events.append({
                'timestamp': wp['timestamp'],
                'lat': wp['lat'], 'lon': wp['lon'],
                'event_type': 'hard_acceleration',
                'severity': 'info',
                'description': f'Hard acceleration: {accel_x:.1f} m/s²',
                'video_path': video_path,
                'frame_offset': wp.get('frame_offset', 0),
                'metadata': json.dumps({'accel_x': accel_x, 'speed_mps': speed}),
            })

        # --- Sharp Turn (lateral acceleration) ---
        if abs(accel_y) >= thresholds.get('sharp_turn_lateral_g', 4.0):
            events.append({
                'timestamp': wp['timestamp'],
                'lat': wp['lat'], 'lon': wp['lon'],
                'event_type': 'sharp_turn',
                'severity': 'warning',
                'description': f'Sharp turn: lateral {accel_y:.1f} m/s²',
                'video_path': video_path,
                'frame_offset': wp.get('frame_offset', 0),
                'metadata': json.dumps({'accel_y': accel_y, 'speed_mps': speed}),
            })

        # --- Speeding ---
        limit = thresholds.get('speed_limit_mps', 35.76)
        if limit > 0 and speed > limit:
            events.append({
                'timestamp': wp['timestamp'],
                'lat': wp['lat'], 'lon': wp['lon'],
                'event_type': 'speeding',
                'severity': 'info',
                'description': f'Speed: {speed * 2.237:.0f} mph',
                'video_path': video_path,
                'frame_offset': wp.get('frame_offset', 0),
                'metadata': json.dumps({'speed_mps': speed, 'limit_mps': limit}),
            })

        # --- FSD Disengagement ---
        if thresholds.get('fsd_disengage_detect', True) and prev_autopilot is not None:
            engaged = {'SELF_DRIVING', 'AUTOSTEER'}
            if prev_autopilot in engaged and autopilot not in engaged:
                events.append({
                    'timestamp': wp['timestamp'],
                    'lat': wp['lat'], 'lon': wp['lon'],
                    'event_type': 'fsd_disengage',
                    'severity': 'warning',
                    'description': f'FSD disengaged: {prev_autopilot} → {autopilot}',
                    'video_path': video_path,
                    'frame_offset': wp.get('frame_offset', 0),
                    'metadata': json.dumps({
                        'from': prev_autopilot, 'to': autopilot, 'speed_mps': speed,
                    }),
                })
            elif prev_autopilot not in engaged and autopilot in engaged:
                events.append({
                    'timestamp': wp['timestamp'],
                    'lat': wp['lat'], 'lon': wp['lon'],
                    'event_type': 'fsd_engage',
                    'severity': 'info',
                    'description': f'FSD engaged: {autopilot}',
                    'video_path': video_path,
                    'frame_offset': wp.get('frame_offset', 0),
                    'metadata': json.dumps({'state': autopilot, 'speed_mps': speed}),
                })

        prev_autopilot = autopilot

    # Debounce: merge events of same type within 5-second windows
    return _debounce_events(events, window_seconds=5.0)


def _debounce_events(events: list, window_seconds: float = 5.0) -> list:
    """Remove duplicate events of the same type within a time window."""
    if not events:
        return events

    result = []
    last_by_type = {}

    for ev in events:
        key = ev['event_type']
        ts = ev['timestamp']

        if key in last_by_type:
            last_ts = last_by_type[key]
            try:
                delta = abs(
                    datetime.fromisoformat(ts).timestamp()
                    - datetime.fromisoformat(last_ts).timestamp()
                )
                if delta < window_seconds:
                    continue  # Skip duplicate within window
            except (ValueError, TypeError):
                pass

        result.append(ev)
        last_by_type[key] = ts

    return result


# ---------------------------------------------------------------------------
# Haversine distance
# ---------------------------------------------------------------------------

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate great-circle distance between two GPS points in km."""
    R = 6371.0  # Earth radius in km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Polyline gap detection moved to services.mapping_queries (Phase 3c.3, #100)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Indexer status bridge (legacy)
# ---------------------------------------------------------------------------
#
# The original indexer was a single long-lived thread driven by a global
# ``_status`` dict. It has been replaced by ``services.indexing_worker``,
# which uses an SQLite-backed queue. The two helpers below are kept as
# thin compatibility shims for any caller that still hits the old API
# (currently just ``get_stats`` for ``/api/stats``).


def get_indexer_status() -> dict:
    """Return a worker-status snapshot.

    .. deprecated::
        Use :func:`services.indexing_worker.get_worker_status` instead.
        This shim exists so external callers (templates, third-party
        integrations) that still reach for the old dict shape keep
        working through the migration.
    """
    return _get_worker_status_for_stats()


def _get_worker_status_for_stats() -> dict:
    """Return a worker-status snapshot in the legacy ``_status`` shape.

    The ``/api/stats`` endpoint historically returned this dict so the
    Analytics page could surface "indexing in progress" hints. We
    bridge to the new worker module here so old consumers keep
    working without importing ``indexing_worker`` directly (which
    would create a circular import at module-load time).
    """
    try:
        # Lazy import: indexing_worker imports mapping_service, so
        # importing it at module load time would cycle.
        from services import indexing_worker
        ws = indexing_worker.get_worker_status()
    except Exception:  # noqa: BLE001 — never raise from a status getter
        return {
            'running': False, 'queue_depth': 0,
            'files_done_session': 0, 'active_file': None,
            'source': None, 'last_drained_at': None, 'last_error': None,
        }
    return {
        'running': bool(ws.get('active_file')),
        'queue_depth': ws.get('queue_depth', 0),
        'files_done_session': ws.get('files_done_session', 0),
        'active_file': ws.get('active_file'),
        'source': ws.get('source'),
        'last_drained_at': ws.get('last_drained_at'),
        'last_error': ws.get('last_error'),
    }


def _timestamp_from_filename(filename: str) -> Optional[str]:
    """Extract ISO timestamp from a Tesla video filename.

    Tesla format: ``YYYY-MM-DD_HH-MM-SS-camera.mp4``. The embedded
    timestamp is the car's onboard local clock at the moment Tesla
    began the recording — **not** UTC, and **not** guaranteed correct
    (Tesla's clock can drift by hours/days when GPS sync is lost).

    Use ``_resolve_recording_time`` instead for a value you can trust;
    this helper exists only as the fallback when the MP4 ``mvhd`` atom
    cannot be read.
    """
    base = os.path.basename(filename)
    # Extract the timestamp portion (first 19 chars: YYYY-MM-DD_HH-MM-SS)
    if len(base) >= 19 and base[4] == '-' and base[10] == '_':
        ts_part = base[:19]
        try:
            dt = datetime.strptime(ts_part, "%Y-%m-%d_%H-%M-%S")
            return dt.isoformat()
        except ValueError:
            pass
    return None


# Threshold for "Tesla onboard clock disagrees with mvhd UTC by more than
# this many seconds" — when crossed we log a WARNING so operators can spot
# Tesla clock-glitch incidents in the journal. The indexer always trusts
# mvhd regardless of the gap; the threshold only controls log verbosity.
_CLOCK_SKEW_WARN_SECONDS = 300


def _resolve_recording_time(video_path: str) -> Optional[str]:
    """Return the authoritative ISO start-of-recording timestamp for a clip.

    Strategy (in priority order):

    1. **MP4 ``mvhd`` atom (UTC, GPS-derived).** Tesla writes the
       per-recording start time into the standard MP4 ``mvhd``
       creation_time field in proper UTC, populated from the GPS
       satellite clock — this is independent of the car's onboard
       local clock. When Tesla's onboard clock has glitched (e.g.
       lost GPS time sync after a firmware update), the filename
       embeds the wrong local time but ``mvhd`` remains correct.
       Converted to naive local time so the rest of the pipeline
       (which has historically stored naive local strings) sees no
       semantic change.

    2. **Filename fallback.** Older firmware / corrupt MP4 / files
       Tesla never finished writing may lack a usable ``mvhd``. In
       that case fall back to the filename — same as the legacy
       behaviour. We log nothing here because it's the expected
       behaviour for partial / broken clips.

    Logs a WARNING when both sources are available and disagree by
    more than ``_CLOCK_SKEW_WARN_SECONDS`` (default 5 minutes), since
    that pattern indicates a Tesla onboard-clock glitch worth knowing
    about. The indexer always uses the mvhd value regardless of gap
    size — mvhd is the truth.
    """
    filename_ts = _timestamp_from_filename(video_path)
    try:
        mvhd_dt = _get_sei_parser().extract_mvhd_creation_time(video_path)
    except Exception as e:  # defensive: mvhd reader is best-effort
        logger.debug("mvhd read failed for %s: %s", video_path, e)
        mvhd_dt = None

    if mvhd_dt is None:
        return filename_ts

    # Convert UTC -> naive local. ``datetime.fromtimestamp`` (no tz arg)
    # uses the system local TZ; mirroring how _timestamp_from_filename
    # produces a naive value Tesla wrote in the car's local clock.
    try:
        local_naive = datetime.fromtimestamp(mvhd_dt.timestamp())
    except (OverflowError, OSError, ValueError):
        return filename_ts

    if filename_ts:
        try:
            filename_dt = datetime.fromisoformat(filename_ts)
            skew = abs((filename_dt - local_naive).total_seconds())
            if skew >= _CLOCK_SKEW_WARN_SECONDS:
                logger.warning(
                    "Tesla onboard-clock skew detected for %s: filename "
                    "says %s, mvhd UTC says %s (delta %.0fs); using mvhd. "
                    "This typically indicates the car lost GPS time sync.",
                    os.path.basename(video_path),
                    filename_ts,
                    local_naive.isoformat(),
                    skew,
                )
        except (ValueError, TypeError):
            pass

    return local_naive.isoformat()


def canonical_key(video_path: str) -> str:
    """Return a stable identity key for a Tesla dashcam video file.

    Two paths share a canonical key iff they refer to the same recording
    (identical SEI/GPS data), so the indexer can dedupe them and the
    queue / claim mechanism can use the key as a primary key.

    Rules:
      - RecentClips and ArchivedClips clips with the same basename are the
        same recording (Tesla writes to RecentClips; the archive job copies
        the file to the SD card). Key = basename.
      - SavedClips/SentryClips event-folder clips key on
        ``<source>/<event>/<basename>``. Two events can contain
        similarly-named clips, so the event folder is what disambiguates
        them.
      - Bare basename paths (no folder prefix, e.g. legacy DB rows or
        clips referenced from the SD-card archive root) key on the
        basename so they collide with their Recent/Archived siblings.

    Args:
        video_path: Absolute or relative path to a video file.
            Either path-separator style is accepted.

    Returns:
        Canonical key string.
    """
    norm = video_path.replace('\\', '/')
    basename = norm.rsplit('/', 1)[-1]
    parts = norm.split('/')

    # Walk the path looking for a SavedClips/SentryClips marker followed by
    # an event subfolder and a clip filename. The event folder is what
    # makes these clips distinct from same-basename clips in other events.
    for i, part in enumerate(parts):
        if part in ('SavedClips', 'SentryClips') and i + 2 < len(parts):
            event = parts[i + 1]
            return f"{part}/{event}/{basename}"

    return basename


def candidate_db_paths(canonical_key_value: str) -> List[str]:
    """Return every ``waypoints.video_path`` form that shares ``canonical_key_value``.

    For basename-only keys (RecentClips/ArchivedClips clips), expands to
    all relative-path forms the DB might have stored historically:
    bare basename (legacy), ``RecentClips/<basename>``, and
    ``ArchivedClips/<basename>``. For event-folder keys, the relative
    path is unique on its own.

    Mirrors the dedup logic in ``_index_video`` and is the single source
    of truth used by the queue worker, the catch-up scan, and
    ``_update_geodata_paths``.
    """
    if '/' not in canonical_key_value:
        return [
            canonical_key_value,
            f'RecentClips/{canonical_key_value}',
            f'ArchivedClips/{canonical_key_value}',
        ]
    return [canonical_key_value]


# ---------------------------------------------------------------------------
# Indexing queue API moved to services.indexing_queue_service (Phase 3c.1).
# ---------------------------------------------------------------------------



def _refresh_ro_mount(teslacam_path: str) -> None:
    """Cycle the read-only mount to refresh exFAT filesystem cache.

    When in present mode, Tesla writes to the USB image through the gadget
    while the Pi has a read-only mount of the same image.  exFAT caches
    directory entries and won't see new/changed files until the mount is
    refreshed.  A quick umount + mount cycle (~200ms) fixes this.
    """
    from services.mode_service import current_mode
    if current_mode() != 'present':
        return  # Only needed in present mode

    mount_point = os.path.dirname(teslacam_path)  # e.g. /mnt/gadget/part1-ro
    if not os.path.ismount(mount_point):
        return

    try:
        # Find the loop device backing this mount
        result = subprocess.run(
            ["sudo", "nsenter", "--mount=/proc/1/ns/mnt",
             "findmnt", "-n", "-o", "SOURCE", mount_point],
            capture_output=True, text=True, timeout=5,
        )
        source = result.stdout.strip()
        if not source:
            return

        # Umount and remount
        subprocess.run(
            ["sudo", "nsenter", "--mount=/proc/1/ns/mnt",
             "umount", mount_point],
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["sudo", "nsenter", "--mount=/proc/1/ns/mnt",
             "mount", "-o", "ro", source, mount_point],
            capture_output=True, timeout=10,
        )
        logger.info("Refreshed RO mount at %s", mount_point)
    except Exception as e:
        logger.warning("Failed to refresh RO mount (non-fatal): %s", e)


def _find_front_camera_videos(teslacam_path: str) -> Generator[str, None, None]:
    """Find all front-camera MP4 files in TeslaCam folders and ArchivedClips.

    Only indexes front camera since all cameras share the same GPS data.
    Yields absolute file paths.

    Priority order (highest first):
      1. ArchivedClips on the SD card — durable copies of past drives,
         oldest first, where the real GPS data lives.
      2. SavedClips and SentryClips event subfolders — user-marked clips.
      3. RecentClips — the rolling buffer. Most files written while parked
         (sentry mode) contain no GPS at all, so we process these last.

    .. note::

        After issue #76 Phase 2b the **indexer** no longer walks the RO
        USB mount: ``boot_catchup_scan`` uses
        :func:`_find_archived_videos` (ArchivedClips-only). This helper
        is kept intact for the diagnostics endpoint
        (``mapping_diagnostics_test``) and for any third-party caller
        that wants the legacy "everything we can see" view.
    """
    seen_basenames: set = set()

    # 1. ArchivedClips (SD card archive of past drives)
    try:
        from config import ARCHIVE_DIR, ARCHIVE_ENABLED
        if ARCHIVE_ENABLED and os.path.isdir(ARCHIVE_DIR):
            try:
                for f in sorted(os.listdir(ARCHIVE_DIR)):
                    if f.lower().endswith('.mp4') and '-front' in f.lower():
                        seen_basenames.add(f)
                        yield os.path.join(ARCHIVE_DIR, f)
            except OSError:
                pass
    except ImportError:
        pass

    # 2. SavedClips and SentryClips event folders
    for folder in ('SavedClips', 'SentryClips'):
        folder_path = os.path.join(teslacam_path, folder)
        if not os.path.isdir(folder_path):
            continue
        try:
            for event_dir in sorted(os.listdir(folder_path)):
                event_path = os.path.join(folder_path, event_dir)
                if not os.path.isdir(event_path):
                    continue
                for f in sorted(os.listdir(event_path)):
                    if f.lower().endswith('.mp4') and '-front' in f.lower():
                        yield os.path.join(event_path, f)
        except OSError:
            pass

    # 3. RecentClips last (skip basenames already covered by ArchivedClips)
    folder_path = os.path.join(teslacam_path, 'RecentClips')
    if os.path.isdir(folder_path):
        try:
            for f in sorted(os.listdir(folder_path)):
                if f.lower().endswith('.mp4') and '-front' in f.lower():
                    if f in seen_basenames:
                        continue
                    yield os.path.join(folder_path, f)
        except OSError:
            pass


def _find_archived_videos() -> Generator[str, None, None]:
    """Yield front-camera MP4s under ``ARCHIVE_DIR`` (and event subfolders).

    The Phase 2b indexer-side catch-up scanner. Walks ONLY the SD-card
    ``ArchivedClips`` tree — never the RO USB mount. The
    ``archive_producer`` thread (issue #76 Phase 2a) handles USB-side
    catch-up by enqueueing into ``archive_queue``; the
    ``archive_worker`` then copies them into ArchivedClips, where this
    helper picks them up if the indexer happened to be down at the
    moment of the worker's enqueue.

    Yields the same flat-files-then-event-subfolders order as
    :func:`_find_front_camera_videos`'s ArchivedClips section, plus
    any nested SavedClips/SentryClips that an operator may have
    rsync'd into the archive directory directly.
    """
    try:
        from config import ARCHIVE_DIR, ARCHIVE_ENABLED
    except ImportError:
        return
    if not ARCHIVE_ENABLED or not ARCHIVE_DIR or not os.path.isdir(ARCHIVE_DIR):
        return

    # Top-level mp4s (legacy archive layout — flat directory).
    try:
        for f in sorted(os.listdir(ARCHIVE_DIR)):
            full = os.path.join(ARCHIVE_DIR, f)
            if (
                os.path.isfile(full)
                and f.lower().endswith('.mp4')
                and '-front' in f.lower()
            ):
                yield full
    except OSError:
        return

    # Sub-trees for archived event clips (RecentClips/SavedClips/SentryClips
    # mirrored under ArchivedClips by the worker's compute_dest_path).
    for sub in ('RecentClips', 'SavedClips', 'SentryClips'):
        sub_path = os.path.join(ARCHIVE_DIR, sub)
        if not os.path.isdir(sub_path):
            continue
        try:
            for entry in sorted(os.listdir(sub_path)):
                entry_path = os.path.join(sub_path, entry)
                if os.path.isfile(entry_path):
                    if (
                        entry.lower().endswith('.mp4')
                        and '-front' in entry.lower()
                    ):
                        yield entry_path
                    continue
                if not os.path.isdir(entry_path):
                    continue
                # Event subfolder — yield its front-camera mp4s.
                try:
                    for f in sorted(os.listdir(entry_path)):
                        if f.lower().endswith('.mp4') and '-front' in f.lower():
                            yield os.path.join(entry_path, f)
                except OSError:
                    continue
        except OSError:
            continue


def _read_event_json(rel_path: str, teslacam_root: str) -> Optional[dict]:
    """Read Tesla's event.json from the SavedClips/SentryClips folder.

    Tesla writes an event.json into each SavedClips/SentryClips event
    folder. It contains accurate GPS (est_lat, est_lon), the trigger
    reason (e.g. user_interaction_honk, sentry_aware_object_detection),
    timestamp, city/street, and camera. This is far better than guessing
    location from the nearest waypoint.

    Returns the parsed dict on success, or None if not found / unreadable.
    """
    try:
        parts = rel_path.replace('\\', '/').split('/')
        if len(parts) < 2:
            return None
        # Folder is e.g. SavedClips/2026-04-23_19-17-39
        folder_path = os.path.join(teslacam_root, parts[0], parts[1])
        ej = os.path.join(folder_path, 'event.json')
        if not os.path.isfile(ej):
            return None
        with open(ej, 'r') as f:
            data = json.load(f)
        # Validate required fields
        try:
            lat = float(data.get('est_lat'))
            lon = float(data.get('est_lon'))
        except (TypeError, ValueError):
            return None
        # Must be finite, in valid lat/lon range, and not the (0,0) sentinel
        # that some Tesla firmware writes when GPS hasn't locked yet.
        import math
        if not (math.isfinite(lat) and math.isfinite(lon)):
            return None
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            return None
        if lat == 0 and lon == 0:
            return None
        data['_lat'] = lat
        data['_lon'] = lon
        return data
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.debug("Could not read event.json for %s: %s", rel_path, e)
        return None


def _infer_sentry_event(
    conn: sqlite3.Connection,
    rel_path: str,
    file_timestamp: Optional[str],
    teslacam_root: Optional[str] = None,
) -> bool:
    """Create a sentry/saved event for a clip without GPS in its SEI data.

    Preferred location source: Tesla's event.json (has accurate est_lat/lon
    and the trigger reason). Falls back to the most recent waypoint before
    the clip's timestamp if event.json is missing or unparseable.

    Returns True if an event was created, False otherwise.
    """
    if not file_timestamp:
        return False

    # Determine event type from folder
    event_type = 'sentry' if 'SentryClips' in rel_path else 'saved'
    folder_name = rel_path.replace('\\', '/').split('/')[0]
    parts = rel_path.replace('\\', '/').split('/')
    event_folder = parts[1] if len(parts) > 2 else parts[0]

    # Skip if a fresh event.json-based event already exists for this folder.
    # If we find an OLDER event with metadata that doesn't include
    # ``location_source: event_json``, delete it so we can replace it with
    # the more accurate version. This handles legacy DBs from earlier
    # versions that wrote events with different (or no) metadata, which the
    # v3->v4 migration's substring filter may not have matched.
    existing = conn.execute(
        """SELECT id, metadata FROM detected_events
           WHERE event_type = ? AND video_path LIKE ? LIMIT 1""",
        (event_type, f'%{event_folder}%')
    ).fetchone()
    if existing:
        # Parse metadata as JSON to robustly check the source. Substring
        # matching would break if json.dumps formatting changes (e.g.
        # whitespace/key order).
        is_event_json = False
        if existing['metadata']:
            try:
                meta_dict = json.loads(existing['metadata'])
                is_event_json = meta_dict.get('location_source') == 'event_json'
            except (ValueError, TypeError):
                pass
        if is_event_json:
            return False
        # Stale event from older code path — drop it so we can recreate
        # with the accurate event.json-derived data below.
        conn.execute("DELETE FROM detected_events WHERE id = ?", (existing['id'],))

    # Try event.json first (accurate Tesla-reported location)
    lat = lon = None
    location_source = None
    reason = None
    if teslacam_root:
        ej_data = _read_event_json(rel_path, teslacam_root)
        if ej_data:
            lat = ej_data['_lat']
            lon = ej_data['_lon']
            reason = ej_data.get('reason') or 'unknown'
            location_source = 'event_json'

    # Fall back to nearest waypoint (legacy behavior)
    if lat is None or lon is None:
        row = conn.execute(
            """SELECT lat, lon FROM waypoints
               WHERE timestamp <= ? AND lat != 0 AND lon != 0
               ORDER BY timestamp DESC LIMIT 1""",
            (file_timestamp,)
        ).fetchone()
        if not row:
            row = conn.execute(
                """SELECT lat, lon FROM waypoints
                   WHERE lat != 0 AND lon != 0
                   ORDER BY timestamp ASC LIMIT 1""",
                ()
            ).fetchone()
        if not row:
            logger.info("Cannot infer location for %s — no event.json and no waypoints", rel_path)
            return False
        lat = row['lat']
        lon = row['lon']
        location_source = 'nearest_waypoint'

    label = 'Sentry Mode' if event_type == 'sentry' else 'Saved Clip'
    if reason:
        description = f"{label} event ({reason}, location from {location_source})"
    else:
        description = f"{label} event (location from {location_source})"

    conn.execute(
        """INSERT INTO detected_events
           (trip_id, timestamp, lat, lon, event_type, severity,
            description, video_path, frame_offset, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            None,  # not associated with a trip
            file_timestamp,
            lat,
            lon,
            event_type,
            'info',
            description,
            rel_path,
            0,
            json.dumps({
                'location_source': location_source,
                'source_folder': folder_name,
                'reason': reason,
            }),
        )
    )
    conn.commit()
    logger.info("Created %s event for %s at %.4f,%.4f (source=%s)",
                event_type, event_folder, lat, lon, location_source)
    return True


def _index_video(
    conn: sqlite3.Connection,
    video_path: str,
    teslacam_root: str,
    sample_rate: int,
    thresholds: dict,
    trip_gap_minutes: int,
) -> IndexResult:
    """Index a single video file: extract SEI, detect events, store in DB.

    Returns a structured :class:`IndexResult` describing what happened.
    The queue worker dispatches on ``result.outcome`` to decide retry /
    delete / cleanup behavior. Counts are informational.
    """
    parser = _get_sei_parser()

    # Compute a clean relative path for the DB.  ArchivedClips live outside
    # the TeslaCam tree, so os.path.relpath() produces a mangled "../../../"
    # traversal.  Detect that case and use "ArchivedClips/<filename>" instead.
    try:
        from config import ARCHIVE_DIR
        if ARCHIVE_DIR and os.path.abspath(video_path).startswith(os.path.abspath(ARCHIVE_DIR)):
            rel_path = f"ArchivedClips/{os.path.basename(video_path)}"
        else:
            rel_path = os.path.relpath(video_path, teslacam_root)
    except ImportError:
        rel_path = os.path.relpath(video_path, teslacam_root)
    file_timestamp = _resolve_recording_time(video_path)

    # --- Cross-folder dedup (fast path) ---
    # Tesla videos can exist in both RecentClips and ArchivedClips with the
    # same basename. They contain identical SEI, so don't re-parse the file.
    # If the existing copy is in a non-durable folder (RecentClips) and we're
    # now seeing the durable ArchivedClips copy, upgrade the stored video_path
    # without touching the (expensive) SEI extractor.
    #
    # Canonicalization rules live in ``canonical_key`` / ``candidate_db_paths``
    # so the queue worker, catch-up scan, and ``_update_geodata_paths`` all
    # see the same identity for a given clip. Sentry/Saved event subfolders
    # disambiguate by event name (their canonical key includes the event
    # folder), preventing false-matches across unrelated events.
    ckey = canonical_key(video_path)
    candidate_paths = candidate_db_paths(ckey)
    placeholders = ','.join('?' * len(candidate_paths))
    existing_paths = conn.execute(
        f"SELECT DISTINCT video_path FROM waypoints "
        f"WHERE video_path IN ({placeholders})",
        candidate_paths,
    ).fetchall()
    if existing_paths:
        if 'ArchivedClips' in rel_path and not any(
            'ArchivedClips' in (r['video_path'] or '') for r in existing_paths
        ):
            upgraded = conn.execute(
                f"UPDATE waypoints SET video_path = ? "
                f"WHERE video_path IN ({placeholders})",
                (rel_path, *candidate_paths),
            )
            conn.execute(
                f"UPDATE detected_events SET video_path = ? "
                f"WHERE video_path IN ({placeholders})",
                (rel_path, *candidate_paths),
            )
            conn.commit()
            logger.info(
                "Upgraded %d waypoint(s) to durable ArchivedClips path: %s",
                upgraded.rowcount, ckey,
            )
            return IndexResult(IndexOutcome.DUPLICATE_UPGRADED)
        logger.debug("Skipping %s: canonical key already indexed", rel_path)
        return IndexResult(IndexOutcome.ALREADY_INDEXED)

    # --- Defense-in-depth dedup via indexed_files ---
    # The waypoints check above misses when ``waypoints.video_path`` was
    # set to NULL by an earlier ``purge_deleted_videos`` run (which can
    # happen when a sibling copy of the clip was deleted from disk and
    # the targeted-purge "surviving copy" check missed a candidate).
    # When that gap is hit, the indexer would re-parse the clip and
    # insert a SECOND set of waypoints + detected_events with the same
    # SEI data, producing duplicate event pins on the map.
    #
    # ``indexed_files`` is the authoritative "we processed this physical
    # file" record and is keyed on the absolute file path. If ANY row
    # exists for this canonical key with ``waypoint_count > 0``, the
    # clip was already indexed at some point — even if the surviving
    # waypoint/event rows have lost their ``video_path``. Bail out
    # rather than re-insert. We match on basename suffix because the
    # absolute file path stored in ``indexed_files`` may differ from
    # the current call site (e.g., archive moves change the directory)
    # and the path separator differs by OS.
    #
    # Notes:
    # * Tesla filenames contain underscores (``2025-11-08_08-15-44-front.mp4``)
    #   which SQLite ``LIKE`` treats as a single-character wildcard. We
    #   pass an ``ESCAPE '\\'`` clause and escape ``_``, ``%`` in the
    #   basename so we don't over-match across distinct clips that happen
    #   to share a similar character pattern.
    # * The leading ``%`` prevents this query from using an index — but
    #   ``indexed_files`` has at most one row per indexed clip (low
    #   thousands across the lifetime of a Pi), so the full scan is
    #   fast and runs at most once per ``_index_video`` invocation.
    basename_only = os.path.basename(video_path)
    if basename_only:
        escaped = (
            basename_only.replace('\\', '\\\\')
                         .replace('%', '\\%')
                         .replace('_', '\\_')
        )
        prior = conn.execute(
            "SELECT 1 FROM indexed_files "
            "WHERE file_path LIKE ? ESCAPE '\\' "
            "AND waypoint_count > 0 LIMIT 1",
            ('%' + escaped,)
        ).fetchone()
        if prior:
            logger.debug(
                "Skipping %s: indexed_files shows prior index "
                "(video_path may have been NULLed by purge)",
                rel_path,
            )
            return IndexResult(IndexOutcome.ALREADY_INDEXED)

    # Extract SEI messages
    waypoint_dicts = []
    sei_count = 0
    no_gps_count = 0
    try:
        for msg in parser.extract_sei_messages(video_path, sample_rate=sample_rate):
            sei_count += 1
            if not msg.has_gps:
                no_gps_count += 1
                continue

            # Compute absolute timestamp from file timestamp + frame offset
            if file_timestamp:
                try:
                    base_dt = datetime.fromisoformat(file_timestamp)
                    ts = (base_dt + timedelta(milliseconds=msg.timestamp_ms)).isoformat()
                except (ValueError, TypeError):
                    ts = file_timestamp
            else:
                ts = datetime.now(timezone.utc).isoformat()

            waypoint_dicts.append({
                'timestamp': ts,
                'lat': msg.latitude_deg,
                'lon': msg.longitude_deg,
                'heading': msg.heading_deg,
                'speed_mps': msg.vehicle_speed_mps,
                'acceleration_x': msg.linear_acceleration_x,
                'acceleration_y': msg.linear_acceleration_y,
                'acceleration_z': msg.linear_acceleration_z,
                'gear': msg.gear_state,
                'autopilot_state': msg.autopilot_state,
                'steering_angle': msg.steering_wheel_angle,
                'brake_applied': 1 if msg.brake_applied else 0,
                'blinker_on_left': 1 if msg.blinker_on_left else 0,
                'blinker_on_right': 1 if msg.blinker_on_right else 0,
                'video_path': rel_path,
                'frame_offset': msg.frame_index,
            })
    except ImportError as e:
        # Protobuf module missing — abort indexer entirely so it's noticed
        logger.error("SEI parser missing protobuf module: %s", e)
        raise
    except Exception as e:
        logger.warning("Failed to parse SEI from %s: %s", rel_path, e)
        return IndexResult(IndexOutcome.PARSE_ERROR, error=str(e))

    if not waypoint_dicts:
        if sei_count == 0:
            logger.info("No SEI messages found in %s", rel_path)
        else:
            logger.info("%s: %d SEI messages but 0 had GPS (%d checked)",
                        rel_path, sei_count, no_gps_count)

        # For Sentry/Saved clips with no GPS, create an event using the
        # accurate Tesla event.json (preferred) or nearest waypoint as fallback
        if 'SentryClips' in rel_path or 'SavedClips' in rel_path:
            inferred = _infer_sentry_event(conn, rel_path, file_timestamp,
                                            teslacam_root=teslacam_root)
            if inferred:
                # 1 inferred event written; treat as indexed for queue purposes.
                return IndexResult(IndexOutcome.INDEXED, waypoints=0, events=1)
        return IndexResult(IndexOutcome.NO_GPS_RECORDED)

    # Determine source folder
    parts = rel_path.replace('\\', '/').split('/')
    source_folder = parts[0] if parts else 'Unknown'

    # Find or create trip — match on time proximity, regardless of source_folder.
    # Earlier code filtered by source_folder, which fragmented trips when
    # the same drive was ingested from RecentClips vs ArchivedClips, and
    # picked the wrong trip when videos were indexed out of order.
    #
    # ORDER BY: pick the trip with the smallest temporal gap to the new
    # clip (0 for any trip whose window overlaps the new clip's range).
    # An earlier "ORDER BY ABS(new_start - existing.start)" tie-breaker
    # caused phantom duplicate trips in production: when the new clip
    # fell BETWEEN two existing trips, that ranking could prefer the
    # later trip simply because its start_time was numerically closer
    # to the new clip's start (even though the clip should clearly
    # extend the earlier trip). The new ranking always picks the trip
    # whose interval the new clip actually adjoins. The post-insert
    # _merge_adjacent_trips_for is still called as defense in depth in
    # case the chosen trip is itself adjacent to another.
    first_wp = waypoint_dicts[0]
    last_wp = waypoint_dicts[-1]
    new_start = first_wp['timestamp']
    new_end = last_wp['timestamp']
    gap_seconds = trip_gap_minutes * 60

    existing_trip = conn.execute(
        """
        SELECT id FROM trips
        WHERE start_time IS NOT NULL AND end_time IS NOT NULL
          AND (CAST(strftime('%s', :ns) AS INTEGER)
               - CAST(strftime('%s', end_time) AS INTEGER)) <= :gap
          AND (CAST(strftime('%s', start_time) AS INTEGER)
               - CAST(strftime('%s', :ne) AS INTEGER)) <= :gap
        ORDER BY
          CASE
            WHEN CAST(strftime('%s', end_time) AS INTEGER)
                 < CAST(strftime('%s', :ns) AS INTEGER)
              THEN CAST(strftime('%s', :ns) AS INTEGER)
                   - CAST(strftime('%s', end_time) AS INTEGER)
            WHEN CAST(strftime('%s', start_time) AS INTEGER)
                 > CAST(strftime('%s', :ne) AS INTEGER)
              THEN CAST(strftime('%s', start_time) AS INTEGER)
                   - CAST(strftime('%s', :ne) AS INTEGER)
            ELSE 0
          END ASC,
          id ASC
        LIMIT 1
        """,
        {'ns': new_start, 'ne': new_end, 'gap': gap_seconds},
    ).fetchone()
    trip_id = existing_trip['id'] if existing_trip else None

    if trip_id is None:
        # Create new trip
        cursor = conn.execute(
            """INSERT INTO trips (start_time, start_lat, start_lon, source_folder, indexed_at)
               VALUES (?, ?, ?, ?, ?)""",
            (first_wp['timestamp'], first_wp['lat'], first_wp['lon'],
             source_folder, datetime.now(timezone.utc).isoformat())
        )
        trip_id = cursor.lastrowid

    # Insert waypoints
    conn.executemany(
        """INSERT INTO waypoints
           (trip_id, timestamp, lat, lon, heading, speed_mps,
            acceleration_x, acceleration_y, acceleration_z,
            gear, autopilot_state, steering_angle, brake_applied,
            blinker_on_left, blinker_on_right,
            video_path, frame_offset)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [(trip_id, wp['timestamp'], wp['lat'], wp['lon'], wp['heading'],
          wp['speed_mps'], wp['acceleration_x'], wp['acceleration_y'],
          wp['acceleration_z'], wp['gear'], wp['autopilot_state'],
          wp['steering_angle'], wp['brake_applied'],
          wp['blinker_on_left'], wp['blinker_on_right'],
          wp['video_path'], wp['frame_offset'])
         for wp in waypoint_dicts]
    )

    # Run event detection
    events = _detect_events(waypoint_dicts, thresholds, rel_path)
    if events:
        conn.executemany(
            """INSERT INTO detected_events
               (trip_id, timestamp, lat, lon, event_type, severity,
                description, video_path, frame_offset, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(trip_id, ev['timestamp'], ev['lat'], ev['lon'],
              ev['event_type'], ev['severity'], ev['description'],
              ev['video_path'], ev['frame_offset'], ev.get('metadata'))
             for ev in events]
        )

    # Defense-in-depth: merge any other trip whose window now adjoins
    # (or overlaps) this trip's extent. The matching SQL above picks
    # one trip per insert; if the new clip's waypoints bridged two
    # existing trips, only the chosen one was extended and the other
    # remained as a phantom fragment. _merge_adjacent_trips_for stitches
    # them together using the same gap rule and returns the surviving
    # id (which is preserved across calls because we always keep the
    # lower id). All FK children are re-pointed before the dropped
    # trip is deleted, so cascade does not destroy waypoints.
    trip_id = _merge_adjacent_trips_for(conn, trip_id, gap_seconds)

    # Recompute trip stats from the full waypoint set. The new video may
    # extend the trip in either direction (forward OR backward in time when
    # archive videos are indexed out of order), so we can't just append
    # to the existing distance. Distance is summed per video file in
    # frame/id order, because Tesla videos can overlap in time (e.g. saved
    # clips alongside RecentClips); a global timestamp sort would interleave
    # them and produce phantom GPS jumps.
    bounds = conn.execute(
        "SELECT MIN(timestamp) AS first_ts, MAX(timestamp) AS last_ts "
        "FROM waypoints WHERE trip_id = ?",
        (trip_id,),
    ).fetchone()
    if bounds and bounds['first_ts']:
        first_ts, last_ts = bounds['first_ts'], bounds['last_ts']
        first_row = conn.execute(
            "SELECT lat, lon FROM waypoints WHERE trip_id = ? "
            "AND timestamp = ? ORDER BY id LIMIT 1",
            (trip_id, first_ts),
        ).fetchone()
        last_row = conn.execute(
            "SELECT lat, lon FROM waypoints WHERE trip_id = ? "
            "AND timestamp = ? ORDER BY id DESC LIMIT 1",
            (trip_id, last_ts),
        ).fetchone()
        total_dist = 0.0
        # Phase 5.1 (#102) — collapse the original N+1 (1 query for
        # the distinct video list + 1 per video) into a single
        # ORDER BY video_path, id pass.
        #
        # Distance is summed per video (we MUST NOT haversine across
        # consecutive rows belonging to different videos): Tesla videos
        # can overlap in time (saved clips alongside RecentClips), so
        # a global ORDER BY timestamp would interleave them and produce
        # phantom GPS jumps. Walking ORDER BY video_path, id gives the
        # same per-video ordering the old per-video SELECT produced,
        # and the explicit ``video_path`` column lets us reset between
        # videos — equivalent semantics, one query instead of 1+N.
        all_wps = conn.execute(
            "SELECT video_path, lat, lon FROM waypoints "
            "WHERE trip_id = ? AND video_path IS NOT NULL "
            "ORDER BY video_path, id",
            (trip_id,),
        ).fetchall()
        prev = None
        prev_video = None
        for w in all_wps:
            video_path = w['video_path']
            if prev is not None and video_path == prev_video:
                total_dist += _haversine_km(
                    prev['lat'], prev['lon'],
                    w['lat'], w['lon'],
                )
            prev = w
            prev_video = video_path
        try:
            dur = max(0, int((
                datetime.fromisoformat(last_ts)
                - datetime.fromisoformat(first_ts)
            ).total_seconds()))
        except (ValueError, TypeError):
            dur = 0
        conn.execute(
            """UPDATE trips SET
               start_time = ?, end_time = ?,
               start_lat = ?, start_lon = ?,
               end_lat = ?, end_lon = ?,
               distance_km = ?, duration_seconds = ?
               WHERE id = ?""",
            (first_ts, last_ts,
             first_row['lat'] if first_row else None,
             first_row['lon'] if first_row else None,
             last_row['lat'] if last_row else None,
             last_row['lon'] if last_row else None,
             total_dist, dur, trip_id),
        )

    conn.commit()
    return IndexResult(
        IndexOutcome.INDEXED,
        waypoints=len(waypoint_dicts),
        events=len(events),
    )


def index_single_file(
    video_path: str,
    db_path: str,
    teslacam_root: str,
    sample_rate: int = 30,
    thresholds: Optional[dict] = None,
    trip_gap_minutes: int = 5,
) -> IndexResult:
    """Index a single video file on demand (e.g., after archiving).

    This is the public entry point for per-file indexing. It opens its own
    DB connection, classifies the file (front-cam? exists? too new? already
    indexed?), calls the internal :func:`_index_video` worker if needed, and
    records the result in ``indexed_files``.

    Returns a structured :class:`IndexResult`. The queue worker dispatches
    on ``result.outcome``; non-queue callers (e.g. inline archive indexing)
    typically only care that the call did not raise — counts are exposed
    via ``result.waypoints`` / ``result.events`` for logging.

    Does NOT acquire the task coordinator lock — the caller is responsible
    for ensuring no conflicting heavy tasks are running.
    """
    if thresholds is None:
        thresholds = dict(DEFAULT_THRESHOLDS)

    # Only index front-camera files (all cameras share the same GPS data)
    basename = os.path.basename(video_path).lower()
    if '-front' not in basename or not basename.endswith('.mp4'):
        return IndexResult(IndexOutcome.NOT_FRONT_CAMERA)

    try:
        stat = os.stat(video_path)
    except OSError:
        logger.debug("index_single_file: cannot stat %s", video_path)
        return IndexResult(IndexOutcome.FILE_MISSING)

    # Skip files still being written (< MAPPING_INDEX_TOO_NEW_SECONDS
    # old, default 120 s). Tesla writes the moov atom at the end of
    # each clip, and re-indexing while writes are in progress wastes
    # CPU and may produce truncated waypoint lists. Phase 5.9 (#102):
    # threshold is now configurable via mapping.index_too_new_seconds
    # in config.yaml — exposed via the Settings → Advanced sub-page.
    try:
        from config import MAPPING_INDEX_TOO_NEW_SECONDS as _too_new
    except Exception:  # noqa: BLE001
        _too_new = 120.0
    if (time.time() - stat.st_mtime) < _too_new:
        logger.debug("index_single_file: skipping %s (still being written)", video_path)
        return IndexResult(IndexOutcome.TOO_NEW)

    try:
        conn = _init_db(db_path)
    except sqlite3.OperationalError as e:
        if _is_transient_db_error(e):
            logger.debug("index_single_file: DB busy opening %s: %s", video_path, e)
            return IndexResult(IndexOutcome.DB_BUSY, error=str(e))
        raise

    try:
        # Check if already indexed with data
        row = conn.execute(
            "SELECT waypoint_count FROM indexed_files WHERE file_path = ?",
            (video_path,)
        ).fetchone()
        if row and row['waypoint_count'] and row['waypoint_count'] > 0:
            return IndexResult(IndexOutcome.ALREADY_INDEXED)

        result = _index_video(
            conn, video_path, teslacam_root, sample_rate, thresholds,
            trip_gap_minutes,
        )

        # Record in indexed_files for any terminal outcome that produced a
        # decision (good or "no GPS"). Skip TOO_NEW / DB_BUSY / PARSE_ERROR
        # so the worker retries them. The "older than 5 min" clause records
        # zero-waypoint terminal results for old files so the indexer doesn't
        # re-examine them on every catch-up scan.
        if result.outcome in (
            IndexOutcome.INDEXED,
            IndexOutcome.DUPLICATE_UPGRADED,
        ) or (
            result.outcome == IndexOutcome.NO_GPS_RECORDED
            and (time.time() - stat.st_mtime) > 300
        ):
            conn.execute(
                """INSERT OR REPLACE INTO indexed_files
                   (file_path, file_size, file_mtime, indexed_at,
                    waypoint_count, event_count)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (video_path, stat.st_size, stat.st_mtime,
                 datetime.now(timezone.utc).isoformat(),
                 result.waypoints, result.events)
            )
            conn.commit()

        return result

    except ImportError:
        raise  # Protobuf missing — let caller decide
    except sqlite3.OperationalError as e:
        if _is_transient_db_error(e):
            return IndexResult(IndexOutcome.DB_BUSY, error=str(e))
        logger.warning("index_single_file failed for %s: %s", video_path, e)
        return IndexResult(IndexOutcome.PARSE_ERROR, error=str(e))
    except Exception as e:
        logger.warning("index_single_file failed for %s: %s", video_path, e)
        return IndexResult(IndexOutcome.PARSE_ERROR, error=str(e))
    finally:
        conn.close()


def purge_deleted_videos(db_path: str, teslacam_path: Optional[str] = None,
                         deleted_paths: Optional[List[str]] = None) -> dict:
    """Reconcile geodata.db with the filesystem when video files are gone.

    The video file is the playback source — it is NOT the trip itself.
    A user's GPS history (waypoints), trip aggregates (trips), and
    detected events are independent records that must survive video
    loss. This function therefore:

    * **Deletes** the orphan ``indexed_files`` row (the file is gone, so
      the row is lying about indexing state).
    * **NULLs out** ``waypoints.video_path`` and
      ``detected_events.video_path`` for the missing clip — preserving
      the GPS coordinates, telemetry, and event detections while
      signaling to the UI that the playback link is broken.
    * **Never deletes** waypoints, detected_events, or trips.

    This contract is intentional and load-bearing. Earlier behavior
    cascade-deleted trips when their last waypoint was purged, which
    caused real driving history to disappear from the map every time
    the stale-scan caught up to RecentClips files Tesla had rotated
    out before the archive subsystem copied them to SD. See PR
    discussion on issue #75 / #76.

    If a user really wants to forget a trip, that needs to be a
    separate, explicit "Delete Trip" action — not a side effect of a
    background filesystem reconciliation.

    Two modes:

    * **Targeted**: pass ``deleted_paths`` (list of absolute or
      relative video paths) to reconcile only those specific entries.
    * **Full scan**: pass ``teslacam_path`` to walk every
      ``indexed_files`` entry and reconcile rows whose file no longer
      exists.

    Returns a dict with keys ``purged_files``, ``purged_waypoints``,
    ``purged_events``, ``purged_trips``. The ``waypoints``/``events``
    counts now reflect rows whose ``video_path`` was nulled (not
    deleted); ``purged_trips`` is always 0 and remains in the dict for
    backward compatibility.
    """
    conn = _init_db(db_path)
    purged_files = 0
    purged_waypoints = 0
    purged_events = 0
    purged_trips = 0

    try:
        if deleted_paths:
            # Targeted mode — remove entries matching the given paths.
            #
            # Critical safety: a single video may live in BOTH
            # ``RecentClips`` (Tesla's rolling buffer) and
            # ``ArchivedClips`` (our SD-card copy). When Tesla rotates a
            # clip out of RecentClips, the watcher fires a delete event
            # for that path — but the archived copy must survive. We
            # canonical-key dedupe and check candidate paths on disk
            # before purging waypoints/events.
            try:
                from config import ARCHIVE_DIR, ARCHIVE_ENABLED
                archive_dir = ARCHIVE_DIR if ARCHIVE_ENABLED else None
            except ImportError:
                archive_dir = None

            # If the caller didn't supply ``teslacam_path`` (e.g. the
            # watcher delete callback), look it up so the
            # surviving-copy probe can still check the USB drive. We
            # treat a lookup failure as "no surviving copy on USB" —
            # the archive_dir check will still fire if applicable.
            tc_for_check = teslacam_path
            if not tc_for_check:
                try:
                    from services.video_service import (
                        get_teslacam_path as _gtp,
                    )
                    tc_for_check = _gtp() or None
                except Exception:  # noqa: BLE001
                    tc_for_check = None

            for path in deleted_paths:
                basename = os.path.basename(path)
                if not basename:
                    continue
                key = canonical_key(path)
                if not key:
                    continue
                # Candidate ON-DISK locations for this canonical key.
                # If ANY of them still exists, the geodata still has a
                # backing video — skip purge entirely.
                surviving_files = []
                if tc_for_check:
                    surviving_files.extend([
                        os.path.join(tc_for_check, 'RecentClips', basename),
                        os.path.join(tc_for_check, 'SavedClips', basename),
                        os.path.join(tc_for_check, 'SentryClips', basename),
                    ])
                if archive_dir:
                    surviving_files.extend([
                        os.path.join(archive_dir, basename),
                        os.path.join(archive_dir, 'ArchivedClips', basename),
                    ])
                # Don't count the file we're being told was just deleted
                # — it's gone (the kernel told the watcher so).
                surviving_files = [p for p in surviving_files
                                   if os.path.abspath(p) !=
                                   os.path.abspath(path)]
                if any(os.path.isfile(p) for p in surviving_files):
                    logger.debug(
                        "Skipping purge for %s — surviving copy exists",
                        basename,
                    )
                    continue

                # No surviving copy — the video file is genuinely gone.
                # We must NOT cascade-delete the trip metadata: GPS
                # waypoints are the user's record of having driven
                # somewhere, and that record is independent of whether
                # the dashcam clip survives. (Tesla rotates RecentClips
                # at the 1-hour mark; if the archive subsystem missed
                # a clip, we still want the trip to appear on the map.)
                #
                # 1) indexed_files: delete the row — the file is gone,
                #    so this row is now lying.
                cur = conn.execute(
                    "DELETE FROM indexed_files WHERE file_path = ?",
                    (path,),
                )
                purged_files += cur.rowcount

                # 2) waypoints / detected_events: NULL out video_path
                #    instead of deleting the row. The GPS coordinates,
                #    speed, telemetry, and detected events are all
                #    real history — only the playback link is broken.
                #    The map UI checks for NULL video_path and disables
                #    the play affordance for orphaned waypoints.
                rel_paths = candidate_db_paths(key)
                if not rel_paths:
                    continue
                placeholders = ','.join('?' * len(rel_paths))
                wc = conn.execute(
                    f"UPDATE waypoints SET video_path = NULL "
                    f"WHERE video_path IN ({placeholders})",
                    rel_paths,
                ).rowcount
                purged_waypoints += wc
                ec = conn.execute(
                    f"UPDATE detected_events SET video_path = NULL "
                    f"WHERE video_path IN ({placeholders})",
                    rel_paths,
                ).rowcount
                purged_events += ec

                # 3) Trips: NEVER delete from this code path. A trip
                #    represents real driving that happened; losing the
                #    video doesn't unhappen the drive. Use a separate,
                #    user-initiated "Delete Trip" flow if a trip
                #    actually needs to be removed.

        elif teslacam_path:
            # Full scan mode — check every indexed file against disk.
            # Also check ArchivedClips on SD card before marking as missing.
            #
            # Phase 5.6 (#102): the full scan walks every row of
            # ``indexed_files`` (often >10k rows on a busy install). The
            # legacy implementation issued a single ``fetchall()`` and
            # held the connection open for the entire walk. On a busy
            # Pi Zero 2 W this blocked the indexer worker (which needs
            # the same SQLite file) for the duration of the scan and
            # held a process-resident list of every file_path string.
            #
            # We now stream in batches of ``BATCH_SIZE`` rows: each
            # batch reads under one cursor, processes, COMMITs the
            # path-fixup UPDATEs/DELETEs, then explicitly **yields the
            # SQLite lock** by closing + reopening the connection
            # between batches. The reopen is cheap (μs); the yield gap
            # lets the indexer / archive workers acquire the write lock
            # if they're waiting.
            try:
                from config import ARCHIVE_DIR, ARCHIVE_ENABLED
                archive_dir = ARCHIVE_DIR if ARCHIVE_ENABLED else None
            except ImportError:
                archive_dir = None

            BATCH_SIZE = 500
            INTER_BATCH_SLEEP = 0.05  # 50 ms — long enough to release
                                      # the SQLite lock to a contender
            last_rowid = 0
            missing: List[str] = []
            while True:
                # Fetch one bounded batch using a rowid cursor so
                # mid-walk DELETEs (our own path-fixup or another
                # worker's writes) don't cause us to skip rows.
                # ``rowid`` is the implicit SQLite primary key —
                # always indexed, no extra cost.
                batch = conn.execute(
                    "SELECT rowid, file_path FROM indexed_files "
                    "WHERE rowid > ? ORDER BY rowid LIMIT ?",
                    (last_rowid, BATCH_SIZE),
                ).fetchall()
                if not batch:
                    break

                for row in batch:
                    fp = row['file_path']
                    last_rowid = row['rowid']
                    if os.path.isfile(fp):
                        continue
                    # Check if file exists in ArchivedClips (by filename)
                    if archive_dir and os.path.isdir(archive_dir):
                        basename = os.path.basename(fp)
                        archive_path = os.path.join(archive_dir, basename)
                        if os.path.isfile(archive_path):
                            # Update indexed path to point to archive.
                            # If the archive path already has its own entry (from
                            # _update_geodata_paths), just delete the stale USB entry.
                            existing = conn.execute(
                                "SELECT 1 FROM indexed_files WHERE file_path = ?",
                                (archive_path,)
                            ).fetchone()
                            if existing:
                                conn.execute(
                                    "DELETE FROM indexed_files WHERE file_path = ?",
                                    (fp,)
                                )
                            else:
                                conn.execute(
                                    "UPDATE indexed_files SET file_path = ? WHERE file_path = ?",
                                    (archive_path, fp)
                                )
                            continue
                    missing.append(fp)

                # Commit any path-fixup writes from this batch and
                # release the SQLite lock by closing the connection.
                # Sleep a tick to give any waiting writer a real
                # chance to grab the lock before we reopen.
                conn.commit()
                conn.close()
                if INTER_BATCH_SLEEP > 0:
                    time.sleep(INTER_BATCH_SLEEP)
                conn = _init_db(db_path)

                # Safety: if the table is shorter than the batch we
                # asked for, we're done.
                if len(batch) < BATCH_SIZE:
                    break

            if missing:
                logger.info("Purging %d missing videos from geodata.db", len(missing))
                # Commit any path updates before the targeted purge (which
                # opens its own connection). Without this, the recursive call
                # deadlocks on the database.
                conn.commit()
                conn.close()
                return purge_deleted_videos(db_path, deleted_paths=missing)

        conn.commit()
        logger.info(
            "Reconciled geodata.db: purged %d indexed_files rows, "
            "orphaned %d waypoints and %d events (trips preserved)",
            purged_files, purged_waypoints, purged_events,
        )
    finally:
        conn.close()

    return {
        'purged_files': purged_files,
        'purged_waypoints': purged_waypoints,
        'purged_events': purged_events,
        'purged_trips': purged_trips,
    }


def boot_catchup_scan(db_path: str, teslacam_path: str = '',
                      *, source: str = 'catchup') -> Dict[str, int]:
    """Diff filesystem vs ``indexed_files`` and enqueue any orphans.

    **Phase 2b (issue #76)**: This now walks ONLY ``ARCHIVE_DIR``
    (``~/ArchivedClips``). The ``archive_producer`` thread handles
    USB-side catch-up by enqueueing into ``archive_queue``; the
    ``archive_worker`` then copies clips into ArchivedClips, where
    this scan picks them up if the indexer happened to be down at the
    moment of the worker's enqueue (e.g. a manual scp landed a clip
    while ``gadget_web`` was restarting).

    The ``teslacam_path`` parameter is accepted for backward
    compatibility but is **ignored** — there is intentionally no path
    from this function to the RO USB mount any more.

    Returns ``{scanned, already_indexed, enqueued}``. The
    ``active_file`` banner stays off during this call (no parsing); the
    banner only lights up when the worker actually picks up an orphan.
    """
    # ``teslacam_path`` is intentionally ignored — see docstring.
    del teslacam_path
    result = {'scanned': 0, 'already_indexed': 0, 'enqueued': 0}

    # Build the set of canonical_keys already represented in
    # indexed_files. We diff against canonical keys (not raw paths) so a
    # clip that exists in both Recent and Archived doesn't get
    # re-enqueued.
    try:
        conn = _init_db(db_path)
        try:
            indexed_paths = [
                row['file_path']
                for row in conn.execute(
                    "SELECT file_path FROM indexed_files"
                ).fetchall()
            ]
            queued_keys = {
                row[0]
                for row in conn.execute(
                    "SELECT canonical_key FROM indexing_queue"
                ).fetchall()
            }
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning("boot_catchup_scan: DB read failed: %s", e)
        return result

    indexed_keys = {canonical_key(p) for p in indexed_paths if p}
    indexed_keys.discard('')

    to_enqueue: List[Tuple[str, Optional[int]]] = []
    for fpath in _find_archived_videos():
        result['scanned'] += 1
        key = canonical_key(fpath)
        if not key:
            continue
        if key in indexed_keys:
            result['already_indexed'] += 1
            continue
        if key in queued_keys:
            # Already pending — don't churn the row.
            continue
        to_enqueue.append((fpath, None))
        # Track in-memory so the same canonical_key isn't appended
        # twice from two folders during this same scan.
        queued_keys.add(key)

    if to_enqueue:
        # Phase 3c.1 (#100): queue API moved to indexing_queue_service.
        # Lazy import to avoid load-order surprises (the queue module
        # imports ``canonical_key`` from this module at top-level).
        from services.indexing_queue_service import enqueue_many_for_indexing
        n = enqueue_many_for_indexing(db_path, to_enqueue, source=source)
        result['enqueued'] = n
    logger.info(
        "boot_catchup_scan: scanned=%d, already_indexed=%d, enqueued=%d",
        result['scanned'], result['already_indexed'], result['enqueued'],
    )
    return result


# ---------------------------------------------------------------------------
# Daily stale-data sweep
# ---------------------------------------------------------------------------

# Independent safety net for the case where ``purge_deleted_videos`` calls
# from the watcher / archive-retention paths missed something. Iterates
# every ``indexed_files`` row, ``os.path.isfile`` checks each, and removes
# rows whose underlying file no longer exists.
#
# **Initial delay (issue #75):** First fire is scheduled 5–10 min after
# boot — short enough that orphans left behind by the previous boot
# (e.g. files Tesla rotated out of RecentClips while the Pi was off)
# get cleaned up before the user opens the map page, but long enough
# that boot-time IO doesn't compete with USB gadget presentation.
# Subsequent fires happen ~daily with jitter so multiple Pis don't
# hammer the same minute.
#
# Out-of-cycle scans can be triggered with :func:`trigger_stale_scan_now`
# from high-signal events (after each archive cycle, on the first map
# page load after a restart). The trigger is debounced so concurrent
# triggers from different services collapse into a single scan.
_DAILY_STALE_SCAN_INTERVAL = 24 * 60 * 60  # 24 hours
_DAILY_STALE_SCAN_JITTER = 60 * 60         # +/- 1 hour
_INITIAL_STALE_SCAN_BASE = 5 * 60          # 5 minutes after boot
_INITIAL_STALE_SCAN_JITTER = 5 * 60        # +0..5 min spread
_TRIGGER_DEBOUNCE_SECONDS = 10 * 60        # 10 minutes between fires
_daily_stale_scan_thread: Optional[threading.Thread] = None
_daily_stale_scan_stop: Optional[threading.Event] = None
_stale_scan_state_lock = threading.Lock()
_last_stale_scan_at: float = 0.0  # time.monotonic() of last completed slot-claim


def _initial_stale_scan_delay() -> float:
    """Initial seconds to wait before the first stale scan after start.

    Returns a value in ``[300, 600]`` (5–10 min). Factored out so tests
    can verify the delay range without spinning up a real thread.
    """
    import random as _random
    return _INITIAL_STALE_SCAN_BASE + _random.randint(
        0, _INITIAL_STALE_SCAN_JITTER,
    )


def _run_stale_scan_blocking(db_path: str, teslacam_path_provider,
                             source: str) -> Optional[dict]:
    """Run one stale scan synchronously and update the last-run timestamp.

    Used by both the scheduled loop and the on-demand
    :func:`trigger_stale_scan_now` so they share debounce state.

    .. note::

        ``_last_stale_scan_at`` is updated **up front**, before the scan
        runs, and is **not rolled back on failure**. This is intentional:
        if ``purge_deleted_videos`` fails persistently (e.g. DB lock,
        disk pressure), subsequent triggers will silently debounce for
        the configured window so the system doesn't hammer a failing
        operation. Failures are still surfaced via ``logger.warning``.

    Args:
        db_path: Path to the geodata.db.
        teslacam_path_provider: Either a zero-arg callable or a string.
        source: Short label used in log messages (``'scheduled'``,
            ``'archive'``, ``'map_load'``, ``'manual'``, ...).

    Returns:
        The result dict from :func:`purge_deleted_videos`, or ``None``
        if the TeslaCam path is unavailable or the scan fails.
    """
    global _last_stale_scan_at
    # Claim the slot up front so concurrent triggers see the work as
    # in-flight and debounce themselves.
    with _stale_scan_state_lock:
        _last_stale_scan_at = time.monotonic()

    try:
        if callable(teslacam_path_provider):
            tc = teslacam_path_provider()
        else:
            tc = teslacam_path_provider
        if tc and os.path.isdir(tc):
            result = purge_deleted_videos(db_path, teslacam_path=tc)
            logger.info(
                "Stale scan (%s): purged %d indexed_files rows, "
                "orphaned %d waypoints and %d events "
                "(trips/waypoints preserved)",
                source,
                result.get('purged_files', 0),
                result.get('purged_waypoints', 0),
                result.get('purged_events', 0),
            )
            return result
        logger.debug(
            "Stale scan (%s): TeslaCam not accessible — skipping",
            source,
        )
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("Stale scan (%s) failed: %s", source, e)
        return None


def trigger_stale_scan_now(db_path: str, teslacam_path_provider,
                           source: str = 'manual',
                           debounce_seconds: float = (
                               _TRIGGER_DEBOUNCE_SECONDS),
                           ) -> dict:
    """Request an out-of-cycle stale scan, debounced.

    Returns immediately. The scan (if not debounced) runs on a daemon
    thread so callers — request handlers, archive cycle, etc. — never
    block on database IO.

    Args:
        db_path: Path to the geodata.db.
        teslacam_path_provider: Either a zero-arg callable returning
            the current TeslaCam path, or a string.
        source: Label for logs/diagnostics. Suggested values:
            ``'archive'`` (after archive cycle finishes),
            ``'map_load'`` (first map data hit after restart),
            ``'wifi_reconnect'``, ``'manual'`` (admin/test trigger).
        debounce_seconds: Minimum gap between fires (default 10 min).
            Lower this in tests if needed.

    Returns:
        Dict with keys:
            * ``status``: ``'fired'`` if a scan thread was spawned,
              ``'debounced'`` if the previous scan was too recent.
            * ``last_run_age_seconds`` (debounced only): age of the
              previous scan in seconds.
    """
    with _stale_scan_state_lock:
        last = _last_stale_scan_at
    if last > 0.0:
        age = time.monotonic() - last
        if age < debounce_seconds:
            logger.debug(
                "Stale scan trigger (%s) debounced "
                "(age=%.1fs < %.1fs)",
                source, age, debounce_seconds,
            )
            return {'status': 'debounced',
                    'last_run_age_seconds': age}

    def _runner():
        _run_stale_scan_blocking(
            db_path, teslacam_path_provider, source=source,
        )

    threading.Thread(
        target=_runner,
        name=f'stale-scan-{source}',
        daemon=True,
    ).start()
    return {'status': 'fired'}


def _reset_stale_scan_state_for_tests() -> None:
    """Clear last-run timestamp. Intended for unit tests only."""
    global _last_stale_scan_at
    with _stale_scan_state_lock:
        _last_stale_scan_at = 0.0


def start_daily_stale_scan(db_path: str, teslacam_path_provider) -> bool:
    """Start the background stale-scan thread (idempotent).

    ``teslacam_path_provider`` is a zero-arg callable that returns the
    current TeslaCam path (so we re-resolve on each tick — the path
    can change across mode switches).

    First fire is scheduled 5–10 min after start; subsequent fires
    happen ~daily with jitter. See module-level commentary above for
    rationale and out-of-cycle trigger details.

    Returns ``True`` if a thread was started, ``False`` if already
    running.
    """
    global _daily_stale_scan_thread, _daily_stale_scan_stop
    import random as _random

    if _daily_stale_scan_thread is not None and _daily_stale_scan_thread.is_alive():
        return False

    stop_event = threading.Event()
    _daily_stale_scan_stop = stop_event

    def _loop():
        first_delay = _initial_stale_scan_delay()
        if stop_event.wait(timeout=first_delay):
            return
        while not stop_event.is_set():
            _run_stale_scan_blocking(
                db_path, teslacam_path_provider, source='scheduled',
            )
            # Re-jitter for next cycle so failures don't lock-step.
            jitter = _random.randint(-_DAILY_STALE_SCAN_JITTER,
                                     _DAILY_STALE_SCAN_JITTER)
            if stop_event.wait(
                timeout=_DAILY_STALE_SCAN_INTERVAL + jitter,
            ):
                return

    _daily_stale_scan_thread = threading.Thread(
        target=_loop, name='daily-stale-scan', daemon=True,
    )
    _daily_stale_scan_thread.start()
    return True


def stop_daily_stale_scan(timeout: float = 5.0) -> bool:
    """Stop the daily stale-scan thread.

    Mostly for tests. The production thread is daemon and will be
    killed on process exit.
    """
    global _daily_stale_scan_thread, _daily_stale_scan_stop
    if _daily_stale_scan_stop is not None:
        _daily_stale_scan_stop.set()
    t = _daily_stale_scan_thread
    if t is not None and t.is_alive():
        t.join(timeout=timeout)
        if t.is_alive():
            return False
    _daily_stale_scan_thread = None
    return True


def diagnose_video(teslacam_path: str, max_videos: int = 3) -> dict:
    """Diagnose SEI parsing on sample videos for troubleshooting.

    Tests a few videos in detail, reporting file sizes, MP4 box structure,
    SEI NAL unit counts, GPS data presence, and any parse errors.
    Returns a dict with diagnostic info.
    """
    import struct as _struct

    parser = _get_sei_parser()
    results = {
        'teslacam_path': teslacam_path,
        'path_exists': os.path.isdir(teslacam_path),
        'videos': [],
        'summary': '',
    }

    if not results['path_exists']:
        results['summary'] = f'TeslaCam path does not exist: {teslacam_path}'
        return results

    # List folder structure
    folders = {}
    for folder in ('RecentClips', 'SavedClips', 'SentryClips'):
        fp = os.path.join(teslacam_path, folder)
        if os.path.isdir(fp):
            try:
                entries = os.listdir(fp)
                folders[folder] = len(entries)
            except OSError as e:
                folders[folder] = f'error: {e}'
        else:
            folders[folder] = 'not found'
    results['folders'] = folders

    # Get sample videos
    videos = list(_find_front_camera_videos(teslacam_path))
    results['total_front_videos'] = len(videos)

    for vp in videos[:max_videos]:
        diag = {'path': os.path.relpath(vp, teslacam_path)}
        try:
            stat = os.stat(vp)
            diag['file_size'] = stat.st_size
            diag['file_size_mb'] = round(stat.st_size / 1024 / 1024, 2)

            if stat.st_size < 8:
                diag['error'] = 'File too small'
                results['videos'].append(diag)
                continue

            with open(vp, 'rb') as f:
                header = f.read(min(32, stat.st_size))

            # Check MP4 magic bytes
            diag['first_16_bytes_hex'] = header[:16].hex()
            has_ftyp = b'ftyp' in header[:12]
            diag['has_ftyp'] = has_ftyp

            if not has_ftyp:
                diag['error'] = 'Not a valid MP4 (no ftyp box in first 12 bytes)'
                results['videos'].append(diag)
                continue

            # Deep NAL analysis — read the file and scan mdat
            nal_analysis = _diagnose_nal_structure(vp)
            diag.update(nal_analysis)

            # Try full SEI extraction with sample_rate=1 for max detail
            sei_msgs = []
            gps_msgs = []
            parse_error = None
            try:
                for msg in parser.extract_sei_messages(vp, sample_rate=1):
                    sei_msgs.append(msg)
                    if msg.has_gps:
                        gps_msgs.append(msg)
                    if len(sei_msgs) >= 10:
                        break  # Enough for diagnosis
            except Exception as e:
                parse_error = str(e)

            diag['sei_messages_sampled'] = len(sei_msgs)
            diag['gps_messages'] = len(gps_msgs)
            if parse_error:
                diag['parse_error'] = parse_error

            # Show first GPS point if found
            if gps_msgs:
                first = gps_msgs[0]
                diag['sample_gps'] = {
                    'lat': first.latitude_deg,
                    'lon': first.longitude_deg,
                    'speed_mph': round(first.speed_mph, 1),
                    'heading': first.heading_deg,
                    'gear': first.gear_state,
                }
            elif sei_msgs:
                # Show first SEI to see what data exists
                first = sei_msgs[0]
                diag['sample_sei_no_gps'] = {
                    'lat': first.latitude_deg,
                    'lon': first.longitude_deg,
                    'speed_mph': round(first.speed_mph, 1),
                    'frame': first.frame_index,
                }

        except Exception as e:
            diag['error'] = str(e)

        results['videos'].append(diag)

    # Summary
    total = len(videos)
    tested = len(results['videos'])
    gps_found = sum(1 for v in results['videos'] if v.get('gps_messages', 0) > 0)
    results['summary'] = (
        f'{total} front-camera videos found, {tested} tested: '
        f'{gps_found} have GPS data'
    )

    return results


def _diagnose_nal_structure(video_path: str) -> dict:
    """Deep-scan the NAL unit structure of a video for diagnostics."""
    import struct as _struct

    result = {}
    try:
        file_size = os.path.getsize(video_path)
        if file_size > 150 * 1024 * 1024:
            result['nal_error'] = f'File too large for diagnosis ({file_size} bytes)'
            return result

        with open(video_path, 'rb') as f:
            data = f.read()

        # Find mdat box
        from services.sei_parser import _find_box
        mdat = _find_box(data, 0, len(data), 'mdat')
        if mdat is None:
            result['nal_error'] = 'No mdat box found'
            return result

        result['mdat_size'] = mdat['size']
        result['mdat_first_32_hex'] = data[mdat['start']:mdat['start'] + 32].hex()

        # Scan NAL units
        cursor = mdat['start']
        end = mdat['end']
        nal_types = {}
        nal_count = 0
        sei_type6_count = 0
        sei_payloads = []
        bad_lengths = 0
        max_scan = 5000  # Limit to first 5000 NAL units

        while cursor + 4 <= end and nal_count < max_scan:
            nal_size = _struct.unpack('>I', data[cursor:cursor + 4])[0]
            cursor += 4

            if nal_size < 1 or cursor + nal_size > len(data):
                bad_lengths += 1
                if bad_lengths > 3:
                    result['nal_scan_stopped'] = (
                        f'Too many bad NAL lengths at offset {cursor - 4}'
                    )
                    break
                # Try advancing by 1 to resync
                cursor -= 3
                continue

            nal_type = data[cursor] & 0x1F
            nal_types[nal_type] = nal_types.get(nal_type, 0) + 1
            nal_count += 1

            if nal_type == 6:
                sei_type6_count += 1
                # Record the first few bytes of SEI payload for inspection
                if len(sei_payloads) < 5:
                    payload_preview = data[cursor:cursor + min(16, nal_size)].hex()
                    payload_type_byte = data[cursor + 1] if nal_size >= 2 else -1
                    sei_payloads.append({
                        'offset': cursor,
                        'size': nal_size,
                        'payload_type_byte': payload_type_byte,
                        'first_16_hex': payload_preview,
                    })

            cursor += nal_size

        result['nal_count'] = nal_count
        result['nal_types'] = {str(k): v for k, v in sorted(nal_types.items())}
        result['sei_type6_count'] = sei_type6_count
        result['bad_nal_lengths'] = bad_lengths
        if sei_payloads:
            result['sei_payload_samples'] = sei_payloads

        # Provide human-readable NAL type names
        nal_names = {
            0: 'Unspecified', 1: 'Non-IDR Slice', 2: 'Slice A',
            3: 'Slice B', 4: 'Slice C', 5: 'IDR Slice',
            6: 'SEI', 7: 'SPS', 8: 'PPS', 9: 'AUD',
            10: 'EndSeq', 11: 'EndStream', 12: 'Filler',
            19: 'AuxSlice', 32: 'VPS(HEVC)', 33: 'SPS(HEVC)',
            34: 'PPS(HEVC)',
        }
        result['nal_type_names'] = {
            f'{k} ({nal_names.get(k, "?")})': v
            for k, v in sorted(nal_types.items())
        }

    except Exception as e:
        result['nal_error'] = str(e)

    return result



# ---------------------------------------------------------------------------
# Read-only query helpers moved to services.mapping_queries (Phase 3c.3, #100)
# ---------------------------------------------------------------------------
