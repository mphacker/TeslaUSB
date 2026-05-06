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
import shutil
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
# Database Schema & Management
# ---------------------------------------------------------------------------

_SCHEMA_VERSION = 8
_BACKUP_RETENTION = 3  # Keep this many migration backups before pruning oldest

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time TEXT NOT NULL,
    end_time TEXT,
    start_lat REAL,
    start_lon REAL,
    end_lat REAL,
    end_lon REAL,
    distance_km REAL DEFAULT 0.0,
    duration_seconds INTEGER DEFAULT 0,
    source_folder TEXT,
    indexed_at TEXT
);

CREATE TABLE IF NOT EXISTS waypoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER REFERENCES trips(id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    heading REAL,
    speed_mps REAL,
    acceleration_x REAL,
    acceleration_y REAL,
    acceleration_z REAL,
    gear TEXT,
    autopilot_state TEXT,
    steering_angle REAL,
    brake_applied INTEGER DEFAULT 0,
    blinker_on_left INTEGER DEFAULT 0,
    blinker_on_right INTEGER DEFAULT 0,
    video_path TEXT,
    frame_offset INTEGER
);

CREATE TABLE IF NOT EXISTS detected_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER REFERENCES trips(id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    lat REAL,
    lon REAL,
    event_type TEXT NOT NULL,
    severity TEXT DEFAULT 'info',
    description TEXT,
    video_path TEXT,
    frame_offset INTEGER,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS indexed_files (
    file_path TEXT PRIMARY KEY,
    file_size INTEGER,
    file_mtime REAL,
    indexed_at TEXT,
    waypoint_count INTEGER DEFAULT 0,
    event_count INTEGER DEFAULT 0
);

-- Persistent indexing work queue. One row per pending clip, keyed by
-- canonical_key (RecentClips/ArchivedClips dedup, Saved/Sentry events
-- disambiguated by event folder — see canonical_key()). The single
-- worker thread (services.indexing_worker) drains this; producers
-- (file watcher, archive job, manual button, boot catch-up) just
-- INSERT rows. claimed_by/claimed_at let the worker take an exclusive
-- claim atomically; stale claims (>30 min) are auto-released so a
-- crashed worker can't permanently lock a row.
CREATE TABLE IF NOT EXISTS indexing_queue (
    canonical_key TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50,
    enqueued_at REAL NOT NULL,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    claimed_by TEXT,
    claimed_at REAL,
    source TEXT
);

CREATE INDEX IF NOT EXISTS idx_waypoints_trip ON waypoints(trip_id);
CREATE INDEX IF NOT EXISTS idx_waypoints_coords ON waypoints(lat, lon);
CREATE INDEX IF NOT EXISTS idx_waypoints_timestamp ON waypoints(timestamp);
CREATE INDEX IF NOT EXISTS idx_waypoints_video_path ON waypoints(video_path);
-- Covering index for query_trips' video_count subquery: lets SQLite count
-- DISTINCT video_path per trip without touching the main waypoints table.
-- Without this, /api/trips fans out to 1 + 2N queries (where N = page size)
-- and visibly stalls the map page on databases with thousands of waypoints.
CREATE INDEX IF NOT EXISTS idx_waypoints_trip_video
    ON waypoints(trip_id, video_path);
-- Day-based aggregate (/api/days) and per-day route lookup
-- (/api/day/<date>/routes) both filter by substr(start_time,1,10).
-- v7: ``idx_trips_start_time`` keeps a sortable-text scan available
-- for callers that filter by ``start_time >= ?`` (e.g. /api/trips with
-- date_from/date_to).
CREATE INDEX IF NOT EXISTS idx_trips_start_time ON trips(start_time);
CREATE INDEX IF NOT EXISTS idx_events_trip ON detected_events(trip_id);
CREATE INDEX IF NOT EXISTS idx_events_coords ON detected_events(lat, lon);
CREATE INDEX IF NOT EXISTS idx_events_type ON detected_events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON detected_events(timestamp);
-- v8: expression indexes on substr(<ts>, 1, 10) for the day-based
-- queries (/api/days, /api/day/<date>/routes, /api/events?date=).
-- Plain idx_trips_start_time / idx_events_timestamp are NOT used by
-- SQLite when the WHERE clause wraps the column in substr() — verified
-- via EXPLAIN QUERY PLAN. Without these expression indexes the day
-- view degrades to a full table scan on every nav, which is unusable
-- on a Pi Zero 2 W with a few thousand trips.
CREATE INDEX IF NOT EXISTS idx_trips_day
    ON trips(substr(start_time, 1, 10));
CREATE INDEX IF NOT EXISTS idx_events_day
    ON detected_events(substr(timestamp, 1, 10));
-- Worker pick-next index: partial index over only unclaimed, ready-to-run
-- rows. Keeps the atomic-claim subquery O(log n) regardless of queue depth.
CREATE INDEX IF NOT EXISTS idx_queue_ready
    ON indexing_queue(priority, enqueued_at)
    WHERE claimed_by IS NULL;
-- Stale-claim recovery scan: lets the worker quickly find rows whose
-- claim has aged out (>30 min, indicating the previous worker crashed).
CREATE INDEX IF NOT EXISTS idx_queue_claimed_at
    ON indexing_queue(claimed_at)
    WHERE claimed_by IS NOT NULL;
"""


def _backup_db(db_path: str, target_version: int) -> Optional[str]:
    """Make a copy of the DB before a destructive migration.

    Returns the backup path on success, None on failure (migration still proceeds).
    Old backups beyond ``_BACKUP_RETENTION`` are pruned.
    """
    if not os.path.isfile(db_path):
        return None
    try:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = f"{db_path}.bak.v{target_version}.{ts}"
        shutil.copy2(db_path, backup_path)
        logger.info("Backed up geo-index DB to %s", backup_path)

        # Prune older backups
        backups = sorted(
            f for f in os.listdir(os.path.dirname(db_path) or '.')
            if f.startswith(os.path.basename(db_path) + '.bak.')
        )
        if len(backups) > _BACKUP_RETENTION:
            for old in backups[:-_BACKUP_RETENTION]:
                try:
                    os.remove(os.path.join(os.path.dirname(db_path), old))
                except OSError:
                    pass
        return backup_path
    except Exception as e:
        logger.warning("Failed to back up DB before migration: %s", e)
        return None


def _init_db(db_path: str) -> sqlite3.Connection:
    """Initialize the SQLite database with schema if needed."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    # Tuned for Pi Zero 2 W (512 MB RAM) where mmap exhaustion under
    # concurrent indexer + web load was producing spurious "disk I/O error"
    # responses from SQLite. The combination of a small per-connection page
    # cache, no file mmap, capped WAL size, and frequent autocheckpoint
    # keeps each connection's memory footprint bounded so we never run out
    # of address space when many waitress threads open connections in
    # parallel during a heavy indexer run.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA cache_size=-2000")        # 2 MB per connection
    conn.execute("PRAGMA mmap_size=0")             # disable file-mmap (saves vmem)
    conn.execute("PRAGMA temp_store=MEMORY")       # avoid temp files on slow SD
    conn.execute("PRAGMA journal_size_limit=4194304")   # cap WAL at 4 MB
    conn.execute("PRAGMA wal_autocheckpoint=200")  # checkpoint every ~800 KB
    conn.execute("PRAGMA foreign_keys=ON")

    # Check schema version. Older code used INSERT OR REPLACE on a PRIMARY KEY
    # column, which actually added a new row each time, so older DBs may have
    # multiple rows. Use MAX() to read the effective version.
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = row['v'] if row and row['v'] is not None else 0
    except sqlite3.OperationalError:
        current = 0

    if current < _SCHEMA_VERSION:
        # Backup before any destructive migration (only when an existing DB
        # is being upgraded, not on first install)
        if current > 0:
            _backup_db(db_path, _SCHEMA_VERSION)

        conn.executescript(_SCHEMA_SQL)
        # Migrations for existing databases
        if current < 2:
            # v2: add blinker columns to waypoints
            for col in ('blinker_on_left', 'blinker_on_right'):
                try:
                    conn.execute(f"ALTER TABLE waypoints ADD COLUMN {col} INTEGER DEFAULT 0")
                except sqlite3.OperationalError:
                    pass  # Column already exists
        if current > 0 and current < 3:
            # v3: clean up duplicate trips/waypoints from earlier indexer bugs.
            # Wrapped in a savepoint so a failure during migration doesn't leave
            # the schema_version bumped without the data fixes applied.
            try:
                conn.execute("SAVEPOINT migrate_v3")
                _migrate_v2_to_v3(conn)
                conn.execute("RELEASE SAVEPOINT migrate_v3")
            except Exception as e:
                conn.execute("ROLLBACK TO SAVEPOINT migrate_v3")
                conn.execute("RELEASE SAVEPOINT migrate_v3")
                logger.error("Migration v2->v3 failed, leaving schema at v2: %s", e)
                conn.commit()
                return conn  # Skip schema_version bump so it retries next startup
        if current > 0 and current < 4:
            # v4: re-evaluate Sentry/Saved clips with Tesla's event.json
            # (which has accurate GPS) instead of the prior nearest-waypoint
            # guess. We do this by clearing their indexed_files rows so the
            # next indexer run re-processes them through the new code path.
            try:
                conn.execute("SAVEPOINT migrate_v4")
                _migrate_v3_to_v4(conn)
                conn.execute("RELEASE SAVEPOINT migrate_v4")
            except Exception as e:
                conn.execute("ROLLBACK TO SAVEPOINT migrate_v4")
                conn.execute("RELEASE SAVEPOINT migrate_v4")
                logger.error("Migration v3->v4 failed, leaving schema at v3: %s", e)
                conn.commit()
                return conn
        # v5: covering index ``idx_waypoints_trip_video`` for the
        # ``/api/trips`` page-load N+1 fix. The index is created by the
        # ``executescript(_SCHEMA_SQL)`` call above (CREATE INDEX IF NOT
        # EXISTS), so no separate data migration is needed — the schema
        # version bump is the trigger.
        # v6: ``indexing_queue`` table for the queue-based indexer
        # redesign. Created by the executescript call; no data migration
        # because there's nothing in the queue on the first upgrade — the
        # boot catch-up scan will repopulate from indexed_files diff.
        # v7: ``idx_trips_start_time`` for callers that range-scan
        # by ``start_time`` directly (e.g. /api/trips with date_from
        # bounds). Created by the executescript above.
        # v8: expression indexes ``idx_trips_day`` and
        # ``idx_events_day`` on ``substr(<ts>, 1, 10)`` so the day-
        # based map queries can avoid full scans. Plain timestamp
        # indexes do NOT cover ``substr(col, 1, 10) = ?``. The
        # expression indexes are created by the executescript above
        # (CREATE INDEX IF NOT EXISTS is idempotent); no data
        # migration required.
        conn.execute("DELETE FROM schema_version")
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            (_SCHEMA_VERSION,)
        )
        conn.commit()
        logger.info("Geo-index database initialized (v%d) at %s", _SCHEMA_VERSION, db_path)

    return conn


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Clean up duplicate trips and waypoints from earlier indexer bugs.

    Earlier versions of the indexer:
      * Created separate trips for the same physical drive when the videos
        were ingested from different source folders (RecentClips vs ArchivedClips).
      * Stored duplicate waypoints with the same ``(timestamp, lat, lon)`` but
        different ``video_path`` strings (one per copy of the video).
      * Recorded ``source_folder='..'`` for ArchivedClips because of a path
        normalization bug.

    This one-time migration:
      1. Repairs ``source_folder='..'`` rows by inferring from waypoint paths.
      2. Merges trips whose time windows overlap or are within
         ``_TRIP_GAP_MINUTES_DEFAULT`` minutes of each other (regardless of
         source_folder).
      3. Dedupes waypoints within each trip by ``(timestamp, lat, lon)``,
         preferring the row whose ``video_path`` references ArchivedClips
         (most durable storage).
      4. Recomputes ``start_time``, ``end_time``, start/end coords,
         ``distance_km`` and ``duration_seconds`` for every trip; deletes
         trips left with no waypoints.
    """
    gap_seconds = _TRIP_GAP_MINUTES_DEFAULT * 60
    log_parts: List[str] = []

    # --- Phase 1: source_folder='..' ---
    bad = conn.execute(
        "SELECT id FROM trips WHERE source_folder = '..' OR source_folder LIKE '..%'"
    ).fetchall()
    fixed_src = 0
    for r in bad:
        wp = conn.execute(
            "SELECT video_path FROM waypoints "
            "WHERE trip_id = ? AND video_path IS NOT NULL ORDER BY id LIMIT 1",
            (r['id'],),
        ).fetchone()
        if wp and wp['video_path']:
            vp = wp['video_path'].replace('\\', '/')
            if 'ArchivedClips' in vp:
                folder = 'ArchivedClips'
            elif '/' in vp:
                folder = vp.split('/')[0]
            else:
                folder = 'Unknown'
            conn.execute(
                "UPDATE trips SET source_folder = ? WHERE id = ?",
                (folder, r['id']),
            )
            fixed_src += 1
    log_parts.append(f"fixed {fixed_src} '..' source_folder rows")

    # --- Phase 2: merge overlapping/close trips ---
    # Repeatedly find any pair of trips whose windows are within gap_seconds
    # of each other (in either direction) and merge the higher-id into the lower.
    merged = 0
    iterations = 0
    while True:
        iterations += 1
        if iterations > 10000:
            # Don't silently continue — that would leave duplicates AND
            # bump schema_version, making this migration unrunnable on the
            # next startup. Raising here triggers the SAVEPOINT rollback
            # in _init_db, leaves schema at v2, and surfaces the failure
            # in the logs so we can investigate.
            raise RuntimeError(
                "v2->v3 trip merge loop exceeded 10000 iterations; "
                "possible infinite loop or pathological duplicate set"
            )
        pair = conn.execute(
            """SELECT a.id AS keep_id, b.id AS drop_id
               FROM trips a
               JOIN trips b
                 ON a.id < b.id
                AND a.start_time IS NOT NULL AND a.end_time IS NOT NULL
                AND b.start_time IS NOT NULL AND b.end_time IS NOT NULL
                AND ((julianday(b.start_time) - julianday(a.end_time)) * 86400) <= ?
                AND ((julianday(a.start_time) - julianday(b.end_time)) * 86400) <= ?
               LIMIT 1""",
            (gap_seconds, gap_seconds),
        ).fetchone()
        if not pair:
            break
        keep_id, drop_id = pair['keep_id'], pair['drop_id']
        conn.execute("UPDATE waypoints SET trip_id = ? WHERE trip_id = ?",
                     (keep_id, drop_id))
        conn.execute("UPDATE detected_events SET trip_id = ? WHERE trip_id = ?",
                     (keep_id, drop_id))
        conn.execute("DELETE FROM trips WHERE id = ?", (drop_id,))
        merged += 1
    log_parts.append(f"merged {merged} overlapping trip pairs")

    # --- Phase 3: dedupe waypoints within a trip ---
    dups = conn.execute(
        """SELECT trip_id, timestamp, lat, lon, COUNT(*) AS cnt
           FROM waypoints
           WHERE trip_id IS NOT NULL
           GROUP BY trip_id, timestamp, lat, lon
           HAVING COUNT(*) > 1"""
    ).fetchall()
    deduped = 0
    for d in dups:
        ids = conn.execute(
            """SELECT id, video_path FROM waypoints
               WHERE trip_id = ? AND timestamp = ? AND lat = ? AND lon = ?
               ORDER BY
                 CASE WHEN video_path LIKE '%ArchivedClips%' THEN 0 ELSE 1 END,
                 id""",
            (d['trip_id'], d['timestamp'], d['lat'], d['lon']),
        ).fetchall()
        # Keep the first (durable / lowest id), delete the rest
        drop_ids = [(r['id'],) for r in ids[1:]]
        if drop_ids:
            conn.executemany("DELETE FROM waypoints WHERE id = ?", drop_ids)
            deduped += len(drop_ids)
    log_parts.append(f"deduped {deduped} duplicate waypoints")

    # --- Phase 4: recompute trip stats; drop empty trips ---
    # Distance is computed per video file (in frame/id order) and summed,
    # because Tesla videos can overlap in time (e.g. when a saved clip is
    # triggered alongside RecentClips). Sorting all waypoints globally by
    # timestamp would interleave overlapping recordings and produce huge
    # phantom jumps. start_time/end_time still come from min/max timestamp.
    trips = conn.execute("SELECT id FROM trips").fetchall()
    recomputed = 0
    dropped_empty = 0
    for t in trips:
        bounds = conn.execute(
            "SELECT MIN(timestamp) AS first_ts, MAX(timestamp) AS last_ts "
            "FROM waypoints WHERE trip_id = ?",
            (t['id'],),
        ).fetchone()
        if not bounds or not bounds['first_ts']:
            conn.execute("DELETE FROM trips WHERE id = ?", (t['id'],))
            dropped_empty += 1
            continue
        first_ts, last_ts = bounds['first_ts'], bounds['last_ts']
        first_row = conn.execute(
            "SELECT lat, lon FROM waypoints WHERE trip_id = ? "
            "AND timestamp = ? ORDER BY id LIMIT 1",
            (t['id'], first_ts),
        ).fetchone()
        last_row = conn.execute(
            "SELECT lat, lon FROM waypoints WHERE trip_id = ? "
            "AND timestamp = ? ORDER BY id DESC LIMIT 1",
            (t['id'], last_ts),
        ).fetchone()
        # Distance summed per video file
        total_dist = 0.0
        videos = conn.execute(
            "SELECT DISTINCT video_path FROM waypoints "
            "WHERE trip_id = ? AND video_path IS NOT NULL",
            (t['id'],),
        ).fetchall()
        for v in videos:
            wps = conn.execute(
                "SELECT lat, lon FROM waypoints "
                "WHERE trip_id = ? AND video_path = ? ORDER BY id",
                (t['id'], v['video_path']),
            ).fetchall()
            for j in range(1, len(wps)):
                total_dist += _haversine_km(
                    wps[j-1]['lat'], wps[j-1]['lon'],
                    wps[j]['lat'], wps[j]['lon'],
                )
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
             total_dist, dur, t['id']),
        )
        recomputed += 1
    log_parts.append(
        f"recomputed stats for {recomputed} trips; dropped {dropped_empty} empty"
    )

    logger.info("Migration v2->v3: %s", "; ".join(log_parts))


# Default trip gap, also used by the migration. Kept here so the migration
# can run before any per-call ``trip_gap_minutes`` argument is available.
_TRIP_GAP_MINUTES_DEFAULT = 5


# ---------------------------------------------------------------------------
# Event Detection Rules
# ---------------------------------------------------------------------------

def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """Re-evaluate Sentry/Saved clips with Tesla's event.json.

    Earlier versions inferred Sentry/Saved event locations from the
    nearest waypoint, which was inaccurate (often pointed at a different
    physical location). Tesla actually writes a precise event.json with
    est_lat/est_lon in each event folder.

    To pick this up for clips already in the database, we delete:
      1. The existing inferred-location detected_events rows
         (they have metadata.inferred_location=true), and
      2. The indexed_files rows for SavedClips/SentryClips clips with
         zero waypoints — so the next indexer run re-processes them
         through the new event.json-aware code path.

    Driving clips (those with waypoints) are left untouched.
    """
    # Drop old inferred events so they get recreated from event.json
    cur = conn.execute(
        "DELETE FROM detected_events "
        "WHERE event_type IN ('saved', 'sentry') "
        "AND metadata IS NOT NULL "
        "AND (metadata LIKE '%inferred_location%' "
        "     OR metadata LIKE '%nearest_waypoint%')"
    )
    deleted_events = cur.rowcount
    logger.info("v3->v4: cleared %d stale inferred events", deleted_events)

    # Clear indexed_files rows for SavedClips/SentryClips zero-waypoint
    # entries so they get re-indexed with event.json reading
    cur = conn.execute(
        "DELETE FROM indexed_files "
        "WHERE waypoint_count = 0 "
        "AND (file_path LIKE '%/SavedClips/%' "
        "     OR file_path LIKE '%/SentryClips/%' "
        "     OR file_path LIKE '%\\SavedClips\\%' "
        "     OR file_path LIKE '%\\SentryClips\\%')"
    )
    cleared_files = cur.rowcount
    logger.info("v3->v4: cleared %d Sentry/Saved indexed_files entries for re-processing",
                cleared_files)


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
    """Extract ISO timestamp from Tesla video filename.

    Tesla format: YYYY-MM-DD_HH-MM-SS-camera.mp4
    Returns ISO format: YYYY-MM-DDTHH:MM:SS
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
# Indexing queue API (services.indexing_worker is the consumer)
# ---------------------------------------------------------------------------

# Priority is "lower wins". Defaults reflect the plan's user-experience
# ordering: event clips first (user wants to see the incident), then
# archived trips (already on local SD, won't disappear), then recent
# clips (least urgent — Tesla's circular buffer will overwrite them
# eventually but the watcher catches them in real time).
_PRIORITY_SENTRY_SAVED = 10
_PRIORITY_ARCHIVE = 20
_PRIORITY_RECENT = 30
_PRIORITY_DEFAULT = 50

# Worker tuning — kept module-level so tests can monkeypatch them.
_PARSE_ERROR_MAX_ATTEMPTS = 3
_PARSE_ERROR_BASE_BACKOFF = 60.0
_PARSE_ERROR_MAX_BACKOFF = 3600.0
# Rows whose claim is older than this are considered orphaned (worker
# crashed mid-file) and can be released for re-attempt.
_STALE_CLAIM_SECONDS = 1800.0
# Sentinel: permanently failed rows are deferred this far into the
# future. Surfaces them in dead-letter queries via attempts column.
_DEAD_LETTER_DEFER_SECONDS = 365 * 24 * 3600.0


def priority_for_path(file_path: str) -> int:
    """Map a clip path to its indexing-queue priority.

    Uses the same folder-name heuristic as ``canonical_key`` so the
    classification is consistent across producers (watcher, archive,
    catch-up) and the worker.
    """
    norm = (file_path or '').replace('\\', '/').lower()
    if '/savedclips/' in norm or '/sentryclips/' in norm:
        return _PRIORITY_SENTRY_SAVED
    if '/archivedclips/' in norm:
        return _PRIORITY_ARCHIVE
    if '/recentclips/' in norm:
        return _PRIORITY_RECENT
    return _PRIORITY_DEFAULT


def _open_queue_conn(db_path: str) -> sqlite3.Connection:
    """Open a tuned SQLite connection for queue ops.

    Mirrors the per-connection settings used by ``_init_db`` so writers
    don't trip over contended locks. Caller owns close.
    """
    conn = sqlite3.connect(db_path, timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def enqueue_for_indexing(db_path: str, file_path: str, *,
                         priority: Optional[int] = None,
                         source: str = 'manual',
                         next_attempt_at: Optional[float] = None) -> bool:
    """Add or upgrade a single file in the indexing queue.

    Returns True if a row was inserted or updated, False if the file
    was rejected (path empty, canonical_key empty). Idempotent — a
    second enqueue of the same canonical_key only lowers the priority
    if the new value is more urgent.

    ``next_attempt_at`` lets producers defer the first claim atomically
    with the insert. The archive flow uses this to give the inline
    indexer a head start so the worker doesn't race the inline call
    and clobber a fresh claim. Only applied on INSERT — an existing
    row already has its own schedule that we should respect.

    Safe to call from any producer (watcher, archive, manual button).
    Never blocks the caller for I/O — the actual parse happens in the
    worker thread.
    """
    if not file_path:
        return False
    key = canonical_key(file_path)
    if not key:
        return False
    if priority is None:
        priority = priority_for_path(file_path)
    now = time.time()
    next_at = float(next_attempt_at) if next_attempt_at is not None else 0.0
    try:
        with _open_queue_conn(db_path) as conn:
            conn.execute(
                """
                INSERT INTO indexing_queue
                    (canonical_key, file_path, priority,
                     enqueued_at, next_attempt_at, source)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_key) DO UPDATE SET
                    priority = MIN(priority, excluded.priority),
                    file_path = CASE
                        WHEN claimed_by IS NULL
                            THEN excluded.file_path
                        ELSE file_path
                    END,
                    source = CASE
                        WHEN claimed_by IS NULL
                            THEN excluded.source
                        ELSE source
                    END
                """,
                (key, file_path, priority, now, next_at, source),
            )
        return True
    except sqlite3.Error as e:
        logger.warning("enqueue_for_indexing failed for %s: %s", file_path, e)
        return False


def enqueue_many_for_indexing(db_path: str,
                              items: List[Tuple[str, Optional[int]]],
                              source: str = 'catchup') -> int:
    """Batch enqueue. ``items`` is a list of ``(file_path, priority)``.

    A None priority means "use ``priority_for_path``". Returns the
    number of items that were actually written (skipping empty paths).
    Single transaction so a 200-orphan boot catch-up costs ~10 ms.
    """
    if not items:
        return 0
    now = time.time()
    rows: List[Tuple[str, str, int, float, str]] = []
    for file_path, prio in items:
        if not file_path:
            continue
        key = canonical_key(file_path)
        if not key:
            continue
        if prio is None:
            prio = priority_for_path(file_path)
        rows.append((key, file_path, prio, now, source))
    if not rows:
        return 0
    try:
        with _open_queue_conn(db_path) as conn:
            conn.executemany(
                """
                INSERT INTO indexing_queue
                    (canonical_key, file_path, priority,
                     enqueued_at, next_attempt_at, source)
                VALUES (?, ?, ?, ?, 0, ?)
                ON CONFLICT(canonical_key) DO UPDATE SET
                    priority = MIN(priority, excluded.priority),
                    file_path = CASE
                        WHEN claimed_by IS NULL
                            THEN excluded.file_path
                        ELSE file_path
                    END,
                    source = CASE
                        WHEN claimed_by IS NULL
                            THEN excluded.source
                        ELSE source
                    END
                """,
                rows,
            )
        return len(rows)
    except sqlite3.Error as e:
        logger.warning("enqueue_many_for_indexing failed: %s", e)
        return 0


def recover_stale_claims(db_path: str,
                         max_age_seconds: float = _STALE_CLAIM_SECONDS) -> int:
    """Release claims older than ``max_age_seconds``.

    Called once at worker startup so a previous crash can't permanently
    lock a row. Returns the number of claims released.
    """
    cutoff = time.time() - max_age_seconds
    try:
        with _open_queue_conn(db_path) as conn:
            cur = conn.execute(
                """
                UPDATE indexing_queue
                   SET claimed_by = NULL, claimed_at = NULL
                 WHERE claimed_by IS NOT NULL
                   AND claimed_at < ?
                """,
                (cutoff,),
            )
            released = cur.rowcount or 0
        if released:
            logger.warning(
                "Released %d stale indexing claims (>%ds old)",
                released, int(max_age_seconds),
            )
        return released
    except sqlite3.Error as e:
        logger.warning("recover_stale_claims failed: %s", e)
        return 0


def claim_next_queue_item(db_path: str,
                          worker_id: str) -> Optional[Dict[str, Any]]:
    """Atomically claim the next ready, highest-priority queue item.

    Returns a dict with the row's columns, or None if nothing is ready.
    Uses ``BEGIN IMMEDIATE`` so two workers (or worker + archive inline
    indexer) can never see the same canonical_key.

    The returned dict includes ``claimed_by`` and ``claimed_at`` (the
    timestamp at which we claimed). Pass these back to
    ``complete_queue_item`` / ``release_claim`` / ``defer_queue_item``
    as ``claimed_by=`` and ``claimed_at=`` so a stale worker can never
    mutate a row a fresh worker has re-claimed.

    "Ready" = ``claimed_by IS NULL AND next_attempt_at <= now()`` AND
    ``attempts < _PARSE_ERROR_MAX_ATTEMPTS`` (dead-letter rows are
    skipped automatically).
    """
    now = time.time()
    try:
        conn = _open_queue_conn(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT canonical_key, file_path, priority, enqueued_at,
                       next_attempt_at, attempts, last_error, source
                  FROM indexing_queue
                 WHERE claimed_by IS NULL
                   AND next_attempt_at <= ?
                   AND attempts < ?
                 ORDER BY priority ASC, enqueued_at ASC
                 LIMIT 1
                """,
                (now, _PARSE_ERROR_MAX_ATTEMPTS),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            conn.execute(
                """
                UPDATE indexing_queue
                   SET claimed_by = ?, claimed_at = ?
                 WHERE canonical_key = ?
                """,
                (worker_id, now, row['canonical_key']),
            )
            conn.execute("COMMIT")
            result = dict(row)
            # Include the claim token so the worker can pass it back as
            # an owner-guard for complete/release/defer.
            result['claimed_by'] = worker_id
            result['claimed_at'] = now
            return result
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning("claim_next_queue_item failed: %s", e)
        return None


def complete_queue_item(db_path: str, canonical_key_value: str,
                        *, claimed_by: Optional[str] = None,
                        claimed_at: Optional[float] = None) -> bool:
    """Remove a row after a terminal-outcome processing.

    Used for INDEXED, ALREADY_INDEXED, DUPLICATE_UPGRADED,
    NO_GPS_RECORDED, NOT_FRONT_CAMERA, FILE_MISSING. Returns True if a
    row was deleted.

    If ``claimed_by`` and ``claimed_at`` are provided, the delete is
    guarded so it only takes effect if the row is still owned by the
    same claim. This prevents a stuck/timed-out worker from deleting a
    row that's been re-claimed by a fresh worker. Pass them whenever
    you have them (the worker always does); omit only for catch-up /
    one-shot scripts that don't claim.
    """
    if not canonical_key_value:
        return False
    try:
        with _open_queue_conn(db_path) as conn:
            if claimed_by is None:
                cur = conn.execute(
                    "DELETE FROM indexing_queue WHERE canonical_key = ?",
                    (canonical_key_value,),
                )
            else:
                cur = conn.execute(
                    """
                    DELETE FROM indexing_queue
                     WHERE canonical_key = ?
                       AND claimed_by = ?
                       AND claimed_at = ?
                    """,
                    (canonical_key_value, claimed_by, claimed_at),
                )
            return (cur.rowcount or 0) > 0
    except sqlite3.Error as e:
        logger.warning(
            "complete_queue_item failed for %s: %s",
            canonical_key_value, e,
        )
        return False


def release_claim(db_path: str, canonical_key_value: str,
                  *, claimed_by: Optional[str] = None,
                  claimed_at: Optional[float] = None) -> bool:
    """Release a claim without progressing the row.

    Used for transient failures (DB_BUSY, worker pause/resume) where
    we want another tick — or another worker — to retry without
    incrementing ``attempts``.

    If ``claimed_by`` and ``claimed_at`` are provided, the release is
    guarded so a stale worker can't accidentally release a row
    re-claimed by a fresh worker.
    """
    if not canonical_key_value:
        return False
    try:
        with _open_queue_conn(db_path) as conn:
            if claimed_by is None:
                cur = conn.execute(
                    """
                    UPDATE indexing_queue
                       SET claimed_by = NULL, claimed_at = NULL
                     WHERE canonical_key = ?
                    """,
                    (canonical_key_value,),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE indexing_queue
                       SET claimed_by = NULL, claimed_at = NULL
                     WHERE canonical_key = ?
                       AND claimed_by = ?
                       AND claimed_at = ?
                    """,
                    (canonical_key_value, claimed_by, claimed_at),
                )
            return (cur.rowcount or 0) > 0
    except sqlite3.Error as e:
        logger.warning(
            "release_claim failed for %s: %s",
            canonical_key_value, e,
        )
        return False


def defer_queue_item(db_path: str, canonical_key_value: str,
                     next_attempt_at: float, *,
                     bump_attempts: bool = False,
                     last_error: Optional[str] = None,
                     claimed_by: Optional[str] = None,
                     claimed_at: Optional[float] = None) -> bool:
    """Re-schedule a row for a later attempt.

    ``bump_attempts=False`` for TOO_NEW (we know exactly when the file
    will be old enough to parse — no failure occurred).
    ``bump_attempts=True`` for PARSE_ERROR with an exponential backoff
    computed by the caller.

    Always releases the claim so the next call to ``claim_next_queue_item``
    can re-pick the row when ``next_attempt_at`` is reached.

    If ``claimed_by`` and ``claimed_at`` are provided, the update is
    guarded — a stale worker can't move the goalposts on a row that's
    been re-claimed and possibly already finished.
    """
    if not canonical_key_value:
        return False
    try:
        with _open_queue_conn(db_path) as conn:
            params: tuple
            if bump_attempts:
                set_clause = (
                    "claimed_by = NULL, claimed_at = NULL, "
                    "next_attempt_at = ?, "
                    "attempts = attempts + 1, "
                    "last_error = ?"
                )
            else:
                set_clause = (
                    "claimed_by = NULL, claimed_at = NULL, "
                    "next_attempt_at = ?, "
                    "last_error = ?"
                )
            if claimed_by is None:
                cur = conn.execute(
                    f"UPDATE indexing_queue SET {set_clause} "
                    f"WHERE canonical_key = ?",
                    (next_attempt_at, last_error, canonical_key_value),
                )
            else:
                cur = conn.execute(
                    f"UPDATE indexing_queue SET {set_clause} "
                    f"WHERE canonical_key = ? "
                    f"  AND claimed_by = ? "
                    f"  AND claimed_at = ?",
                    (next_attempt_at, last_error, canonical_key_value,
                     claimed_by, claimed_at),
                )
        if cur.rowcount == 0:
            # Owner-guarded miss: row was re-claimed by another worker
            # (or the row was deleted out from under us). Surface this
            # so the caller logs / metrics catch it instead of silently
            # masking a stale-claim bug.
            if claimed_by is not None:
                logger.warning(
                    "defer_queue_item: owner-guard miss for %s "
                    "(claimed_by=%s) — row re-claimed or deleted",
                    canonical_key_value, claimed_by,
                )
            return False
        return True
    except sqlite3.Error as e:
        logger.warning(
            "defer_queue_item failed for %s: %s",
            canonical_key_value, e,
        )
        return False


def compute_backoff(attempts: int) -> float:
    """Exponential backoff with cap. Pure function — easy to unit test.

    ``attempts`` is the *failure count BEFORE this one* (so the first
    retry waits ``BASE``, the second waits ``2*BASE``, etc.).
    """
    if attempts < 0:
        attempts = 0
    delay = _PARSE_ERROR_BASE_BACKOFF * (2 ** attempts)
    return min(delay, _PARSE_ERROR_MAX_BACKOFF)


def get_queue_status(db_path: str) -> Dict[str, Any]:
    """Snapshot of queue health for the /api/index/status endpoint.

    Returns ``{queue_depth, claimed_count, dead_letter_count,
    next_ready_at, last_error}``. Cheap (single SQL with aggregates).
    """
    try:
        with _open_queue_conn(db_path) as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN claimed_by IS NULL
                              AND attempts < ?
                             THEN 1 ELSE 0 END) AS queue_depth,
                    SUM(CASE WHEN claimed_by IS NOT NULL
                             THEN 1 ELSE 0 END) AS claimed_count,
                    SUM(CASE WHEN attempts >= ?
                             THEN 1 ELSE 0 END) AS dead_letter_count,
                    MIN(CASE WHEN claimed_by IS NULL
                              AND attempts < ?
                             THEN next_attempt_at END) AS next_ready_at
                  FROM indexing_queue
                """,
                (_PARSE_ERROR_MAX_ATTEMPTS,
                 _PARSE_ERROR_MAX_ATTEMPTS,
                 _PARSE_ERROR_MAX_ATTEMPTS),
            ).fetchone()
        return {
            'queue_depth': int(row['queue_depth'] or 0),
            'claimed_count': int(row['claimed_count'] or 0),
            'dead_letter_count': int(row['dead_letter_count'] or 0),
            'next_ready_at': float(row['next_ready_at'])
                              if row['next_ready_at'] is not None else None,
        }
    except sqlite3.Error as e:
        logger.warning("get_queue_status failed: %s", e)
        return {
            'queue_depth': 0,
            'claimed_count': 0,
            'dead_letter_count': 0,
            'next_ready_at': None,
            'error': str(e),
        }


def clear_pending_queue(db_path: str) -> int:
    """Remove only **unclaimed** rows from the indexing queue.

    Used by ``/api/index/cancel`` so the currently-claimed file (if
    any) is allowed to finish — its claim row stays in the table until
    the worker's owner-guarded delete on completion. Returns the count
    of rows actually removed.
    """
    try:
        with _open_queue_conn(db_path) as conn:
            cur = conn.execute(
                "DELETE FROM indexing_queue WHERE claimed_by IS NULL"
            )
            return cur.rowcount or 0
    except sqlite3.Error as e:
        logger.warning("clear_pending_queue failed: %s", e)
        return 0


def clear_all_queue(db_path: str) -> int:
    """Remove every row from the indexing queue, including claimed ones.

    Used by the manual "Rebuild map index (advanced)" action **after**
    the worker has been paused — otherwise the worker may be mid-INSERT
    into waypoints/detected_events for a row this delete would erase
    out from under it. Callers MUST pause the worker first. Returns
    count of rows removed.
    """
    try:
        with _open_queue_conn(db_path) as conn:
            cur = conn.execute("DELETE FROM indexing_queue")
            return cur.rowcount or 0
    except sqlite3.Error as e:
        logger.warning("clear_all_queue failed: %s", e)
        return 0


# Backward-compat alias — same dangerous semantics as the original
# (deletes claimed rows). New code should pick one of the two above.
clear_queue = clear_all_queue


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
    file_timestamp = _timestamp_from_filename(video_path)

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
    first_wp = waypoint_dicts[0]
    last_wp = waypoint_dicts[-1]
    new_start = first_wp['timestamp']
    new_end = last_wp['timestamp']
    gap_seconds = trip_gap_minutes * 60

    existing_trip = conn.execute(
        """SELECT id FROM trips
           WHERE start_time IS NOT NULL AND end_time IS NOT NULL
             AND ((julianday(?) - julianday(end_time)) * 86400) <= ?
             AND ((julianday(start_time) - julianday(?)) * 86400) <= ?
           ORDER BY ABS((julianday(?) - julianday(start_time)) * 86400)
           LIMIT 1""",
        (new_start, gap_seconds, new_end, gap_seconds, new_start),
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
        videos = conn.execute(
            "SELECT DISTINCT video_path FROM waypoints "
            "WHERE trip_id = ? AND video_path IS NOT NULL",
            (trip_id,),
        ).fetchall()
        for v in videos:
            vwps = conn.execute(
                "SELECT lat, lon FROM waypoints "
                "WHERE trip_id = ? AND video_path = ? ORDER BY id",
                (trip_id, v['video_path']),
            ).fetchall()
            for j in range(1, len(vwps)):
                total_dist += _haversine_km(
                    vwps[j-1]['lat'], vwps[j-1]['lon'],
                    vwps[j]['lat'], vwps[j]['lon'],
                )
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

    # Skip files still being written (< 2 min old). Tesla writes the moov
    # atom at the end of each clip, and re-indexing while writes are in
    # progress wastes CPU and may produce truncated waypoint lists.
    if (time.time() - stat.st_mtime) < 120:
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
    """Remove geodata.db entries for videos that no longer exist on disk.

    Can operate in two modes:
    - **Targeted**: Pass ``deleted_paths`` (list of absolute or relative video
      paths) to remove only those specific entries.
    - **Full scan**: Pass ``teslacam_path`` to scan every ``indexed_files``
      entry and remove those whose file no longer exists.

    Returns dict with counts of purged rows.
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

                # No surviving copy — safe to purge.
                # 1) indexed_files: exact-match the absolute path that
                #    was reported. (The other absolute forms were
                #    rewritten by ``_update_geodata_paths`` when the
                #    archive moved the file, so an exact match is
                #    correct here.)
                cur = conn.execute(
                    "DELETE FROM indexed_files WHERE file_path = ?",
                    (path,),
                )
                purged_files += cur.rowcount

                # 2) waypoints / detected_events: video_path stores
                #    relative DB paths from canonical_key. Exact-match
                #    every candidate so we don't substring-match an
                #    unrelated clip that happens to share the basename.
                rel_paths = candidate_db_paths(key)
                if not rel_paths:
                    continue
                placeholders = ','.join('?' * len(rel_paths))
                trip_ids = [
                    r['trip_id']
                    for r in conn.execute(
                        f"SELECT DISTINCT trip_id FROM waypoints "
                        f"WHERE video_path IN ({placeholders})",
                        rel_paths,
                    ).fetchall()
                ]
                wc = conn.execute(
                    f"DELETE FROM waypoints "
                    f"WHERE video_path IN ({placeholders})",
                    rel_paths,
                ).rowcount
                purged_waypoints += wc
                ec = conn.execute(
                    f"DELETE FROM detected_events "
                    f"WHERE video_path IN ({placeholders})",
                    rel_paths,
                ).rowcount
                purged_events += ec

                # 3) Trips with no remaining waypoints get removed.
                for tid in trip_ids:
                    if tid is None:
                        continue
                    remaining = conn.execute(
                        "SELECT COUNT(*) FROM waypoints WHERE trip_id = ?",
                        (tid,),
                    ).fetchone()[0]
                    if remaining == 0:
                        conn.execute(
                            "DELETE FROM trips WHERE id = ?", (tid,),
                        )
                        purged_trips += 1

        elif teslacam_path:
            # Full scan mode — check every indexed file against disk.
            # Also check ArchivedClips on SD card before marking as missing.
            try:
                from config import ARCHIVE_DIR, ARCHIVE_ENABLED
                archive_dir = ARCHIVE_DIR if ARCHIVE_ENABLED else None
            except ImportError:
                archive_dir = None

            rows = conn.execute(
                "SELECT file_path FROM indexed_files"
            ).fetchall()
            missing = []
            for row in rows:
                fp = row['file_path']
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
            "Purged from geodata.db: %d files, %d waypoints, %d events, %d trips",
            purged_files, purged_waypoints, purged_events, purged_trips,
        )
    finally:
        conn.close()

    return {
        'purged_files': purged_files,
        'purged_waypoints': purged_waypoints,
        'purged_events': purged_events,
        'purged_trips': purged_trips,
    }


def boot_catchup_scan(db_path: str, teslacam_path: str,
                      *, source: str = 'catchup') -> Dict[str, int]:
    """Diff filesystem vs ``indexed_files`` and enqueue any orphans.

    Replaces the legacy "auto-index on startup" full re-scan. Cheap by
    design: one ``os.listdir`` walk via :func:`_find_front_camera_videos`
    + one bulk SELECT of every indexed canonical_key + an in-memory diff
    + one batch INSERT into ``indexing_queue``. No video parsing happens
    here — that's the worker's job.

    Returns ``{scanned, already_indexed, enqueued}``. The
    ``active_file`` banner stays off during this call (no parsing); the
    banner only lights up when the worker actually picks up an orphan.
    """
    result = {'scanned': 0, 'already_indexed': 0, 'enqueued': 0}
    if not teslacam_path or not os.path.isdir(teslacam_path):
        logger.debug("boot_catchup_scan: TeslaCam path not accessible")
        return result

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
    for fpath in _find_front_camera_videos(teslacam_path):
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
# rows whose underlying file no longer exists. Designed to run once per
# day with jitter so multiple Pis don't hammer the same minute.
_DAILY_STALE_SCAN_INTERVAL = 24 * 60 * 60  # 24 hours
_DAILY_STALE_SCAN_JITTER = 60 * 60         # +/- 1 hour
_daily_stale_scan_thread: Optional[threading.Thread] = None
_daily_stale_scan_stop: Optional[threading.Event] = None


def start_daily_stale_scan(db_path: str, teslacam_path_provider) -> bool:
    """Start the background daily stale-scan thread (idempotent).

    ``teslacam_path_provider`` is a zero-arg callable that returns the
    current TeslaCam path (so we re-resolve on each tick — the path
    can change across mode switches).

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
        # Initial wait: between 1h and 1h+24h after boot — boot itself
        # is busy with USB gadget binding, so give the system time to
        # settle before scanning.
        first_delay = 60 * 60 + _random.randint(0, _DAILY_STALE_SCAN_INTERVAL)
        if stop_event.wait(timeout=first_delay):
            return
        while not stop_event.is_set():
            try:
                tc = teslacam_path_provider()
                if tc and os.path.isdir(tc):
                    result = purge_deleted_videos(db_path, teslacam_path=tc)
                    logger.info(
                        "Daily stale scan: purged files=%d waypoints=%d "
                        "events=%d trips=%d",
                        result.get('purged_files', 0),
                        result.get('purged_waypoints', 0),
                        result.get('purged_events', 0),
                        result.get('purged_trips', 0),
                    )
                else:
                    logger.debug(
                        "Daily stale scan: TeslaCam not accessible — "
                        "skipping this cycle",
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning("Daily stale scan failed: %s", e)
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
    """Get all waypoints for a trip as a GeoJSON-ready list."""
    conn = _init_db(db_path)
    try:
        rows = conn.execute(
            """SELECT lat, lon, heading, speed_mps, autopilot_state,
                      video_path, frame_offset, timestamp,
                      steering_angle, brake_applied, gear,
                      acceleration_x, acceleration_y,
                      blinker_on_left, blinker_on_right
               FROM waypoints WHERE trip_id = ? ORDER BY id""",
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
    sorted by waypoint id (ascending = chronological). Waypoints
    are NOT post-processed — callers (i.e. the blueprint) are
    responsible for path normalization (``ArchivedClips`` prefix
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
             ORDER BY t.start_time DESC, w.id ASC
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

        return {'trips': [trips_by_id[tid] for tid in order]}
    finally:
        conn.close()


@_with_db_retry
def query_all_routes_simplified(
    db_path: str,
    min_distance_km: float = 0.05,
    max_points_per_trip: int = 50,
) -> List[dict]:
    """Return every indexed trip with subsampled waypoints for the
    "All time" map overview.

    Each trip keeps at most ``max_points_per_trip`` waypoints (its
    first, its last, and an evenly-spaced sample of the middle) so
    polylines stay within the Pi Zero 2 W's render budget while
    still tracing the route at regional zoom levels. Trips below
    ``min_distance_km`` are excluded — same default as
    :func:`query_trips` and :func:`query_day_routes` so the All
    time overlay never advertises trips that other views hide as
    parking-lot blips. Trips with fewer than two valid (lat, lon)
    waypoints are also excluded — they can't render a polyline.

    Returns a list of trips ordered by ``start_time`` DESC. Each
    trip carries enough metadata for the client to drill into the
    correct day on click (``date``) plus the subsampled waypoint
    list (only ``lat``, ``lon``, ``speed_mps`` — no per-clip
    drilldown data, since the All time view delegates that to
    :func:`query_day_routes` when the user opens a day).

    Performance: a single CTE-based query computes ROW_NUMBER per
    trip and selects only the kept rows server-side, so we don't
    fetch every waypoint just to throw most away in Python. For a
    25-trip / ~10k-waypoint database this returns ~750 rows in well
    under 100 ms on a Pi Zero 2 W. Requires SQLite >= 3.25 for
    window functions; the project's runtime baseline is 3.46+.
    """
    if min_distance_km is None or min_distance_km < 0:
        min_distance_km = 0.0
    if max_points_per_trip is None or max_points_per_trip < 2:
        max_points_per_trip = 2

    conn = _init_db(db_path)
    try:
        # The CTE filters trips first (idx_trips_start_time covers
        # the ORDER BY), then numbers the surviving waypoints per
        # trip. The outer SELECT keeps row 1, the last row, and
        # every Nth row in between so the polyline still traces the
        # general shape. Stride sampling (instead of full
        # Douglas-Peucker) is the right tradeoff here: at the
        # regional zoom the All time view uses, the visual
        # difference is invisible — and if the user wants the real
        # shape, they click the polyline to drill into the day.
        sql = """
            WITH valid_trips AS (
                SELECT id                AS trip_id,
                       start_time,
                       end_time,
                       start_lat,
                       start_lon,
                       end_lat,
                       end_lon,
                       distance_km,
                       duration_seconds,
                       substr(start_time, 1, 10) AS date
                  FROM trips
                 WHERE start_time IS NOT NULL
                   AND COALESCE(distance_km, 0) >= ?
            ),
            numbered AS (
                SELECT w.trip_id,
                       w.id            AS wp_id,
                       w.lat,
                       w.lon,
                       w.speed_mps,
                       ROW_NUMBER() OVER (
                           PARTITION BY w.trip_id ORDER BY w.id
                       ) AS rn,
                       COUNT(*) OVER (
                           PARTITION BY w.trip_id
                       ) AS total
                  FROM waypoints w
                  JOIN valid_trips v ON v.trip_id = w.trip_id
                 WHERE w.lat IS NOT NULL
                   AND w.lon IS NOT NULL
            )
            SELECT v.trip_id,
                   v.start_time,
                   v.end_time,
                   v.start_lat,
                   v.start_lon,
                   v.end_lat,
                   v.end_lon,
                   v.distance_km,
                   v.duration_seconds,
                   v.date,
                   n.lat,
                   n.lon,
                   n.speed_mps
              FROM valid_trips v
              JOIN numbered n ON n.trip_id = v.trip_id
             WHERE n.rn = 1
                OR n.rn = n.total
                OR (n.rn - 1) % MAX(1, (n.total + ? - 1) / ?) = 0
             ORDER BY v.start_time DESC, n.rn ASC
        """
        rows = conn.execute(
            sql,
            (min_distance_km, max_points_per_trip, max_points_per_trip),
        ).fetchall()

        trips_by_id: Dict[int, dict] = {}
        order: List[int] = []
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
                    'waypoints': [],
                }
                trips_by_id[trip_id] = trip
                order.append(trip_id)
            trip['waypoints'].append({
                'lat': row['lat'],
                'lon': row['lon'],
                'speed_mps': row['speed_mps'],
            })

        # Drop trips that ended up with <2 surviving waypoints
        # (can't render a polyline). This can happen when a trip
        # has only one valid GPS fix even though it cleared the
        # distance threshold via an estimated path.
        return [
            trips_by_id[tid] for tid in order
            if len(trips_by_id[tid]['waypoints']) >= 2
        ]
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
