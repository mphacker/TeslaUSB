"""
TeslaUSB Mapping & Geo-Indexer Service.

Manages a SQLite database of GPS waypoints, trips, and detected driving events
extracted from Tesla dashcam SEI telemetry. Provides background indexing with
rule-based event detection.

Designed for Pi Zero 2 W: processes one video at a time, uses generators,
and stores results in a lightweight SQLite database.
"""

import json
import logging
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Generator, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Lazy-import SEI parser to avoid startup cost
_sei_parser = None


def _get_sei_parser():
    global _sei_parser
    if _sei_parser is None:
        from services import sei_parser
        _sei_parser = sei_parser
    return _sei_parser


# ---------------------------------------------------------------------------
# Database Schema & Management
# ---------------------------------------------------------------------------

_SCHEMA_VERSION = 1

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

CREATE INDEX IF NOT EXISTS idx_waypoints_trip ON waypoints(trip_id);
CREATE INDEX IF NOT EXISTS idx_waypoints_coords ON waypoints(lat, lon);
CREATE INDEX IF NOT EXISTS idx_waypoints_timestamp ON waypoints(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_trip ON detected_events(trip_id);
CREATE INDEX IF NOT EXISTS idx_events_coords ON detected_events(lat, lon);
CREATE INDEX IF NOT EXISTS idx_events_type ON detected_events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON detected_events(timestamp);
"""


def _init_db(db_path: str) -> sqlite3.Connection:
    """Initialize the SQLite database with schema if needed."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # Check schema version
    try:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        current = row['version'] if row else 0
    except sqlite3.OperationalError:
        current = 0

    if current < _SCHEMA_VERSION:
        conn.executescript(_SCHEMA_SQL)
        conn.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
            (_SCHEMA_VERSION,)
        )
        conn.commit()
        logger.info("Geo-index database initialized (v%d) at %s", _SCHEMA_VERSION, db_path)

    return conn


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
# Background Indexer
# ---------------------------------------------------------------------------

_indexer_thread: Optional[threading.Thread] = None
_indexer_lock = threading.Lock()
_indexer_cancel = threading.Event()

# Status dict (read from any thread, written only by indexer thread)
_status: Dict = {
    'running': False,
    'progress': '',
    'files_total': 0,
    'files_done': 0,
    'current_file': '',
    'last_run': None,
    'error': None,
}


def get_indexer_status() -> dict:
    """Return a copy of the current indexer status."""
    return dict(_status)


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


def _find_front_camera_videos(teslacam_path: str) -> Generator[str, None, None]:
    """Find all front-camera MP4 files in TeslaCam folders.

    Only indexes front camera since all cameras share the same GPS data.
    Yields absolute file paths.
    """
    for folder in ('RecentClips', 'SavedClips', 'SentryClips'):
        folder_path = os.path.join(teslacam_path, folder)
        if not os.path.isdir(folder_path):
            continue

        if folder == 'RecentClips':
            # Flat structure: files directly in folder
            try:
                for f in sorted(os.listdir(folder_path)):
                    if f.lower().endswith('.mp4') and '-front' in f.lower():
                        yield os.path.join(folder_path, f)
            except OSError:
                pass
        else:
            # Event structure: files in subfolders
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


def _index_video(
    conn: sqlite3.Connection,
    video_path: str,
    teslacam_root: str,
    sample_rate: int,
    thresholds: dict,
    trip_gap_minutes: int,
) -> Tuple[int, int]:
    """Index a single video file: extract SEI, detect events, store in DB.

    Returns (waypoint_count, event_count).
    """
    parser = _get_sei_parser()
    rel_path = os.path.relpath(video_path, teslacam_root)
    file_timestamp = _timestamp_from_filename(video_path)

    # Extract SEI messages
    waypoint_dicts = []
    try:
        for msg in parser.extract_sei_messages(video_path, sample_rate=sample_rate):
            if not msg.has_gps:
                continue

            # Compute absolute timestamp from file timestamp + frame offset
            if file_timestamp:
                ts = file_timestamp  # Base timestamp from filename
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
                'video_path': rel_path,
                'frame_offset': msg.frame_index,
            })
    except Exception as e:
        logger.warning("Failed to parse SEI from %s: %s", rel_path, e)
        return 0, 0

    if not waypoint_dicts:
        return 0, 0

    # Determine source folder
    parts = rel_path.replace('\\', '/').split('/')
    source_folder = parts[0] if parts else 'Unknown'

    # Find or create trip (check if this video extends an existing trip)
    first_wp = waypoint_dicts[0]
    last_wp = waypoint_dicts[-1]

    # Look for a recent trip to extend (within trip_gap_minutes)
    gap_threshold = file_timestamp or first_wp['timestamp']
    existing_trip = conn.execute(
        """SELECT id, end_time, end_lat, end_lon FROM trips
           WHERE source_folder = ? AND end_time IS NOT NULL
           ORDER BY end_time DESC LIMIT 1""",
        (source_folder,)
    ).fetchone()

    trip_id = None
    if existing_trip:
        try:
            last_end = datetime.fromisoformat(existing_trip['end_time'])
            this_start = datetime.fromisoformat(gap_threshold)
            gap = abs((this_start - last_end).total_seconds())
            if gap <= trip_gap_minutes * 60:
                trip_id = existing_trip['id']
        except (ValueError, TypeError):
            pass

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
            video_path, frame_offset)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [(trip_id, wp['timestamp'], wp['lat'], wp['lon'], wp['heading'],
          wp['speed_mps'], wp['acceleration_x'], wp['acceleration_y'],
          wp['acceleration_z'], wp['gear'], wp['autopilot_state'],
          wp['steering_angle'], wp['brake_applied'], wp['video_path'],
          wp['frame_offset'])
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

    # Update trip end point and distance
    total_dist = 0.0
    for j in range(1, len(waypoint_dicts)):
        total_dist += _haversine_km(
            waypoint_dicts[j - 1]['lat'], waypoint_dicts[j - 1]['lon'],
            waypoint_dicts[j]['lat'], waypoint_dicts[j]['lon']
        )

    conn.execute(
        """UPDATE trips SET
           end_time = ?, end_lat = ?, end_lon = ?,
           distance_km = COALESCE(distance_km, 0) + ?,
           duration_seconds = CAST(
               (julianday(?) - julianday(start_time)) * 86400 AS INTEGER
           )
           WHERE id = ?""",
        (last_wp['timestamp'], last_wp['lat'], last_wp['lon'],
         total_dist, last_wp['timestamp'], trip_id)
    )

    conn.commit()
    return len(waypoint_dicts), len(events)


def _run_indexer(db_path: str, teslacam_path: str, sample_rate: int,
                 thresholds: dict, trip_gap_minutes: int):
    """Background indexer main loop. Scans for new videos and indexes them."""
    global _status

    _status.update({
        'running': True, 'progress': 'Scanning for videos...',
        'files_total': 0, 'files_done': 0, 'current_file': '', 'error': None,
    })

    try:
        conn = _init_db(db_path)

        # Find all front-camera videos
        all_videos = list(_find_front_camera_videos(teslacam_path))
        if not all_videos:
            _status.update({'running': False, 'progress': 'No videos found'})
            conn.close()
            return

        # Filter to unindexed files
        to_index = []
        for vp in all_videos:
            if _indexer_cancel.is_set():
                break
            try:
                stat = os.stat(vp)
                row = conn.execute(
                    "SELECT file_size, file_mtime FROM indexed_files WHERE file_path = ?",
                    (vp,)
                ).fetchone()
                if row and row['file_size'] == stat.st_size and row['file_mtime'] == stat.st_mtime:
                    continue  # Already indexed and unchanged
                to_index.append((vp, stat.st_size, stat.st_mtime))
            except OSError:
                continue

        _status['files_total'] = len(to_index)
        _status['progress'] = f'Indexing {len(to_index)} new videos...'
        logger.info("Geo-indexer: %d new videos to index (of %d total)",
                     len(to_index), len(all_videos))

        total_waypoints = 0
        total_events = 0

        for idx, (vp, fsize, fmtime) in enumerate(to_index):
            if _indexer_cancel.is_set():
                _status['progress'] = 'Cancelled'
                break

            rel = os.path.relpath(vp, teslacam_path)
            _status.update({
                'files_done': idx,
                'current_file': rel,
                'progress': f'Indexing {idx + 1}/{len(to_index)}: {rel}',
            })

            try:
                wc, ec = _index_video(
                    conn, vp, teslacam_path, sample_rate, thresholds,
                    trip_gap_minutes,
                )
                total_waypoints += wc
                total_events += ec

                # Record in indexed_files
                conn.execute(
                    """INSERT OR REPLACE INTO indexed_files
                       (file_path, file_size, file_mtime, indexed_at,
                        waypoint_count, event_count)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (vp, fsize, fmtime,
                     datetime.now(timezone.utc).isoformat(), wc, ec)
                )
                conn.commit()

            except Exception as e:
                logger.error("Failed to index %s: %s", rel, e)
                continue

        conn.close()

        _status.update({
            'running': False,
            'files_done': len(to_index),
            'current_file': '',
            'progress': f'Done: {total_waypoints} waypoints, {total_events} events '
                        f'from {len(to_index)} videos',
            'last_run': datetime.now(timezone.utc).isoformat(),
        })
        logger.info("Geo-indexer complete: %d waypoints, %d events from %d videos",
                     total_waypoints, total_events, len(to_index))

    except Exception as e:
        logger.error("Geo-indexer failed: %s", e)
        _status.update({'running': False, 'error': str(e)})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_indexer(
    db_path: str,
    teslacam_path: str,
    sample_rate: int = 30,
    thresholds: Optional[dict] = None,
    trip_gap_minutes: int = 5,
) -> Tuple[bool, str]:
    """Start the background geo-indexer.

    Args:
        db_path: Path to the SQLite database file.
        teslacam_path: Path to TeslaCam directory.
        sample_rate: Frame sampling rate (30 = ~1/sec at 30fps).
        thresholds: Event detection thresholds (uses defaults if None).
        trip_gap_minutes: Gap between waypoints to split into separate trips.

    Returns:
        (success, message) tuple.
    """
    global _indexer_thread

    if thresholds is None:
        thresholds = dict(DEFAULT_THRESHOLDS)

    with _indexer_lock:
        if _status.get('running'):
            return False, "Indexer already running"

        _indexer_cancel.clear()
        _indexer_thread = threading.Thread(
            target=_run_indexer,
            args=(db_path, teslacam_path, sample_rate, thresholds, trip_gap_minutes),
            daemon=True,
        )
        _indexer_thread.start()
        return True, "Indexer started"


def cancel_indexer() -> Tuple[bool, str]:
    """Request cancellation of the running indexer."""
    if not _status.get('running'):
        return False, "Indexer is not running"
    _indexer_cancel.set()
    return True, "Cancellation requested"


def get_db_connection(db_path: str) -> sqlite3.Connection:
    """Get a read-only connection to the geo-index database."""
    conn = _init_db(db_path)
    return conn


def query_trips(db_path: str, limit: int = 50, offset: int = 0,
                bbox: Optional[Tuple[float, float, float, float]] = None,
                date_from: Optional[str] = None,
                date_to: Optional[str] = None) -> List[dict]:
    """Query trips with optional bounding box and date filters."""
    conn = _init_db(db_path)
    try:
        sql = "SELECT * FROM trips WHERE 1=1"
        params = []

        if bbox:
            min_lat, min_lon, max_lat, max_lon = bbox
            sql += " AND start_lat BETWEEN ? AND ? AND start_lon BETWEEN ? AND ?"
            params.extend([min_lat, max_lat, min_lon, max_lon])

        if date_from:
            sql += " AND start_time >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND start_time <= ?"
            params.append(date_to)

        sql += " ORDER BY start_time DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def query_trip_route(db_path: str, trip_id: int) -> List[dict]:
    """Get all waypoints for a trip as a GeoJSON-ready list."""
    conn = _init_db(db_path)
    try:
        rows = conn.execute(
            """SELECT lat, lon, heading, speed_mps, autopilot_state,
                      video_path, frame_offset, timestamp
               FROM waypoints WHERE trip_id = ? ORDER BY id""",
            (trip_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def query_events(db_path: str, limit: int = 100, offset: int = 0,
                 event_type: Optional[str] = None,
                 severity: Optional[str] = None,
                 bbox: Optional[Tuple[float, float, float, float]] = None,
                 date_from: Optional[str] = None,
                 date_to: Optional[str] = None) -> List[dict]:
    """Query detected events with optional filters."""
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

        sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_stats(db_path: str) -> dict:
    """Get summary statistics from the geo-index database."""
    conn = _init_db(db_path)
    try:
        trip_count = conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0]
        waypoint_count = conn.execute("SELECT COUNT(*) FROM waypoints").fetchone()[0]
        event_count = conn.execute("SELECT COUNT(*) FROM detected_events").fetchone()[0]
        file_count = conn.execute("SELECT COUNT(*) FROM indexed_files").fetchone()[0]

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
            'total_distance_km': round(total_distance, 2),
            'total_duration_seconds': total_duration,
            'event_breakdown': event_breakdown,
            'indexer_status': get_indexer_status(),
        }
    finally:
        conn.close()


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
