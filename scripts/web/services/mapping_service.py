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
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
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

_SCHEMA_VERSION = 2

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
        # Migrations for existing databases
        if current < 2:
            # v2: add blinker columns to waypoints
            for col in ('blinker_on_left', 'blinker_on_right'):
                try:
                    conn.execute(f"ALTER TABLE waypoints ADD COLUMN {col} INTEGER DEFAULT 0")
                except sqlite3.OperationalError:
                    pass  # Column already exists
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
    Yields absolute file paths. Also scans the SD card archive directory
    for clips that Tesla may have already deleted from RecentClips.
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

    # Also scan ArchivedClips on SD card (flat structure, same as RecentClips)
    try:
        from config import ARCHIVE_DIR, ARCHIVE_ENABLED
        if ARCHIVE_ENABLED and os.path.isdir(ARCHIVE_DIR):
            # Track filenames already yielded from RecentClips to avoid duplicates
            seen_names = set()
            for folder in ('RecentClips',):
                fp = os.path.join(teslacam_path, folder)
                if os.path.isdir(fp):
                    try:
                        for f in os.listdir(fp):
                            seen_names.add(f)
                    except OSError:
                        pass

            try:
                for f in sorted(os.listdir(ARCHIVE_DIR)):
                    if f.lower().endswith('.mp4') and '-front' in f.lower():
                        if f not in seen_names:
                            yield os.path.join(ARCHIVE_DIR, f)
            except OSError:
                pass
    except ImportError:
        pass


def _infer_sentry_event(
    conn: sqlite3.Connection,
    rel_path: str,
    file_timestamp: Optional[str],
) -> bool:
    """Create a sentry/saved event at inferred location for clips without GPS.

    Looks up the most recent waypoint before the clip's timestamp to determine
    where the car was parked when the event occurred.

    Returns True if an event was created, False if location couldn't be inferred.
    """
    if not file_timestamp:
        return False

    # Find the most recent waypoint before this clip's timestamp
    row = conn.execute(
        """SELECT lat, lon, trip_id FROM waypoints
           WHERE timestamp <= ? AND lat != 0 AND lon != 0
           ORDER BY timestamp DESC LIMIT 1""",
        (file_timestamp,)
    ).fetchone()

    if not row:
        # Try any waypoint at all (clip might predate all trips)
        row = conn.execute(
            """SELECT lat, lon, trip_id FROM waypoints
               WHERE lat != 0 AND lon != 0
               ORDER BY timestamp ASC LIMIT 1""",
            ()
        ).fetchone()

    if not row:
        logger.info("Cannot infer location for %s — no waypoints in database", rel_path)
        return False

    # Determine event type from folder
    event_type = 'sentry' if 'SentryClips' in rel_path else 'saved'
    folder_name = rel_path.replace('\\', '/').split('/')[0]

    # Extract event folder name for grouping (e.g., "2026-03-13_20-45-25")
    parts = rel_path.replace('\\', '/').split('/')
    event_folder = parts[1] if len(parts) > 2 else parts[0]

    # Check if we already have an event for this folder+location
    existing = conn.execute(
        """SELECT id FROM detected_events
           WHERE event_type = ? AND video_path LIKE ? LIMIT 1""",
        (event_type, f'%{event_folder}%')
    ).fetchone()

    if existing:
        return False  # Already created for this event folder

    description = (
        f"{'Sentry Mode' if event_type == 'sentry' else 'Saved Clip'} event "
        f"(location inferred from nearest trip)"
    )

    conn.execute(
        """INSERT INTO detected_events
           (trip_id, timestamp, lat, lon, event_type, severity,
            description, video_path, frame_offset, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row['trip_id'],
            file_timestamp,
            row['lat'],
            row['lon'],
            event_type,
            'info',
            description,
            rel_path,
            0,
            json.dumps({'inferred_location': True, 'source_folder': folder_name}),
        )
    )
    conn.commit()
    logger.info("Created inferred %s event for %s at %.4f,%.4f",
                event_type, event_folder, row['lat'], row['lon'])
    return True


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
        return 0, 0

    if not waypoint_dicts:
        if sei_count == 0:
            logger.info("No SEI messages found in %s", rel_path)
        else:
            logger.info("%s: %d SEI messages but 0 had GPS (%d checked)",
                        rel_path, sei_count, no_gps_count)

        # For Sentry/Saved clips with no GPS, infer location from nearest trip
        if 'SentryClips' in rel_path or 'SavedClips' in rel_path:
            inferred = _infer_sentry_event(conn, rel_path, file_timestamp)
            if inferred:
                return 0, 1  # 0 waypoints, 1 event
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
        # Refresh the RO mount to see Tesla's latest writes.
        # In present mode, exFAT caches filesystem metadata and won't see
        # new/changed files until the mount is cycled.
        _refresh_ro_mount(teslacam_path)

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
                    "SELECT file_size, file_mtime, waypoint_count FROM indexed_files WHERE file_path = ?",
                    (vp,)
                ).fetchone()
                if row and row['file_size'] == stat.st_size and row['file_mtime'] == stat.st_mtime:
                    # Skip if already indexed with waypoints; re-try if 0 waypoints
                    if row['waypoint_count'] and row['waypoint_count'] > 0:
                        continue
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
                # Skip files still being written by Tesla (mtime < 2 min ago)
                if (time.time() - fmtime) < 120:
                    logger.debug("Skipping %s (still being written)", rel)
                    continue

                wc, ec = _index_video(
                    conn, vp, teslacam_path, sample_rate, thresholds,
                    trip_gap_minutes,
                )
                total_waypoints += wc
                total_events += ec

                # Record in indexed_files (only if we got data, or it's an
                # older file unlikely to change — skip incomplete recordings)
                if wc > 0 or ec > 0 or (time.time() - fmtime) > 300:
                    conn.execute(
                        """INSERT OR REPLACE INTO indexed_files
                           (file_path, file_size, file_mtime, indexed_at,
                            waypoint_count, event_count)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (vp, fsize, fmtime,
                         datetime.now(timezone.utc).isoformat(), wc, ec)
                    )
                    conn.commit()

            except ImportError as e:
                # Protobuf missing — stop indexer, report clearly
                logger.error("Indexer aborted: %s", e)
                _status.update({
                    'running': False,
                    'error': str(e),
                    'progress': f'Error: {e}',
                })
                return
            except Exception as e:
                logger.error("Failed to index %s: %s", rel, e)
                continue

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

        # Purge stale entries — remove DB records for files that no longer exist
        try:
            purge_result = purge_deleted_videos(db_path, teslacam_path=teslacam_path)
            if purge_result.get('purged_files', 0) > 0:
                logger.info("Purged stale data: %d files, %d waypoints, %d events, %d trips",
                            purge_result.get('purged_files', 0),
                            purge_result.get('purged_waypoints', 0),
                            purge_result.get('purged_events', 0),
                            purge_result.get('purged_trips', 0))
        except Exception as e:
            logger.warning("Stale data purge failed (non-fatal): %s", e)

    except Exception as e:
        logger.error("Geo-indexer failed: %s", e)
        _status.update({'running': False, 'error': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass


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
            # Targeted mode — remove entries matching the given paths
            for path in deleted_paths:
                # indexed_files stores absolute paths; also try matching by
                # video_path column in waypoints which stores relative paths.
                row = conn.execute(
                    "SELECT file_path FROM indexed_files WHERE file_path = ? "
                    "OR file_path LIKE ?",
                    (path, f'%{os.path.basename(path)}%')
                ).fetchone()
                if row:
                    conn.execute(
                        "DELETE FROM indexed_files WHERE file_path = ?",
                        (row['file_path'],)
                    )
                    purged_files += 1

            # Build a pattern for waypoint cleanup — match on basename of
            # front-camera files (all camera angles share the same waypoints
            # via the front-camera video_path).
            for path in deleted_paths:
                basename = os.path.basename(path)
                # Only front-camera files have waypoints
                if '-front' not in basename.lower():
                    continue
                # waypoints.video_path is relative (e.g. "RecentClips/2026-...")
                rows = conn.execute(
                    "SELECT DISTINCT trip_id FROM waypoints "
                    "WHERE video_path LIKE ?",
                    (f'%{basename}%',)
                ).fetchall()
                trip_ids = [r['trip_id'] for r in rows]

                wc = conn.execute(
                    "DELETE FROM waypoints WHERE video_path LIKE ?",
                    (f'%{basename}%',)
                ).rowcount
                purged_waypoints += wc

                ec = conn.execute(
                    "DELETE FROM detected_events WHERE video_path LIKE ?",
                    (f'%{basename}%',)
                ).rowcount
                purged_events += ec

                # Remove trips that now have zero waypoints
                for tid in trip_ids:
                    remaining = conn.execute(
                        "SELECT COUNT(*) FROM waypoints WHERE trip_id = ?",
                        (tid,)
                    ).fetchone()[0]
                    if remaining == 0:
                        conn.execute("DELETE FROM trips WHERE id = ?", (tid,))
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
                        # Update indexed path to point to archive
                        conn.execute(
                            "UPDATE indexed_files SET file_path = ? WHERE file_path = ?",
                            (archive_path, fp)
                        )
                        continue
                missing.append(fp)

            if missing:
                logger.info("Purging %d missing videos from geodata.db", len(missing))
                # Recurse with targeted mode for the missing files
                result = purge_deleted_videos(db_path, deleted_paths=missing)
                conn.close()
                return result

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


def trigger_auto_index(db_path: str, teslacam_path: str,
                       sample_rate: int = 30,
                       thresholds: Optional[dict] = None,
                       trip_gap_minutes: int = 5) -> None:
    """Trigger indexing in the background if TeslaCam path is accessible.

    Safe to call from startup or mode-switch hooks. Does nothing if the
    indexer is already running or the path is not available.
    """
    if not teslacam_path or not os.path.isdir(teslacam_path):
        logger.debug("Auto-index skipped: TeslaCam path not accessible")
        return

    if _status.get('running'):
        logger.debug("Auto-index skipped: indexer already running")
        return

    success, msg = start_indexer(
        db_path=db_path,
        teslacam_path=teslacam_path,
        sample_rate=sample_rate,
        thresholds=thresholds,
        trip_gap_minutes=trip_gap_minutes,
    )
    if success:
        logger.info("Auto-index triggered: %s", msg)
    else:
        logger.debug("Auto-index not started: %s", msg)


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
        trips = [dict(r) for r in rows]

        # Enrich trips with event counts
        for trip in trips:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM detected_events WHERE trip_id = ?",
                (trip['id'],)
            ).fetchone()
            trip['event_count'] = row['cnt'] if row else 0

        return trips
    finally:
        conn.close()


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
