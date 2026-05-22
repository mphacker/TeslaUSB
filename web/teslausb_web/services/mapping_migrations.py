from __future__ import annotations

import logging
import math
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

_SCHEMA_VERSION: Final[int] = 17
_BACKUP_RETENTION: Final[int] = 3
_PIPELINE_CLAIM_COLS_VERSION: Final[int] = 17
_PIPELINE_QUEUE_VERSION: Final[int] = 16
_PREVIOUS_ERROR_COLUMNS_VERSION: Final[int] = 12
_SCHEMA_WITH_BLINKERS_VERSION: Final[int] = 2
_TRIP_GAP_MINUTES_DEFAULT: Final[int] = 5
_MERGE_MAX_ITERATIONS: Final[int] = 10_000
_COLD_ACCEL_THRESHOLD_MPS2: Final[float] = 0.05
_COLD_STEERING_THRESHOLD_DEG: Final[float] = 0.5
_COLD_GEAR_NO_SIGNAL: Final[frozenset[str]] = frozenset({"UNKNOWN", "PARK"})
_COLD_COLUMNS: Final[tuple[str, ...]] = (
    "acceleration_x",
    "acceleration_y",
    "acceleration_z",
    "gear",
    "steering_angle",
    "brake_applied",
    "blinker_on_left",
    "blinker_on_right",
)
_V15_HOT_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "trip_id",
    "timestamp",
    "lat",
    "lon",
    "heading",
    "speed_mps",
    "autopilot_state",
    "video_path",
    "frame_offset",
)
_V15_HOT_COLUMN_DDL: Final[dict[str, str]] = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "trip_id": "INTEGER REFERENCES trips(id) ON DELETE CASCADE",
    "timestamp": "TEXT NOT NULL",
    "lat": "REAL NOT NULL",
    "lon": "REAL NOT NULL",
    "heading": "REAL",
    "speed_mps": "REAL",
    "autopilot_state": "TEXT",
    "video_path": "TEXT",
    "frame_offset": "INTEGER",
}
_COLD_COLUMNS_CSV: Final[str] = (
    "acceleration_x, acceleration_y, acceleration_z, gear, steering_angle, "
    "brake_applied, blinker_on_left, blinker_on_right"
)
_SNAPSHOT_COLD_ROWS_SQL: Final[str] = (
    "CREATE TEMP TABLE _migrate_v15_cold_snap AS "
    "SELECT id, acceleration_x, acceleration_y, acceleration_z, gear, steering_angle, "
    "brake_applied, blinker_on_left, blinker_on_right FROM waypoints "
    "WHERE (acceleration_x IS NOT NULL AND ABS(acceleration_x) > 0.05) OR "
    "(acceleration_y IS NOT NULL AND ABS(acceleration_y) > 0.05) OR "
    "(acceleration_z IS NOT NULL AND ABS(acceleration_z) > 0.05) OR "
    "(gear IS NOT NULL AND gear NOT IN ('PARK', 'UNKNOWN')) OR "
    "(steering_angle IS NOT NULL AND ABS(steering_angle) > 0.5) OR "
    "brake_applied != 0 OR blinker_on_left != 0 OR blinker_on_right != 0"
)
_RESTORE_COLD_ROWS_SQL: Final[str] = (
    "INSERT OR IGNORE INTO waypoints_cold ("
    "id, acceleration_x, acceleration_y, acceleration_z, gear, steering_angle, "
    "brake_applied, blinker_on_left, blinker_on_right"
    ") SELECT id, acceleration_x, acceleration_y, acceleration_z, gear, steering_angle, "
    "brake_applied, blinker_on_left, blinker_on_right FROM temp._migrate_v15_cold_snap"
)
_V15_COPY_WAYPOINTS_SQL: Final[str] = (
    "INSERT INTO waypoints_new ("
    "id, trip_id, timestamp, lat, lon, heading, speed_mps, autopilot_state, "
    "video_path, frame_offset"
    ") SELECT id, trip_id, timestamp, lat, lon, heading, speed_mps, autopilot_state, "
    "video_path, frame_offset FROM waypoints"
)
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
    autopilot_state TEXT,
    video_path TEXT,
    frame_offset INTEGER
);

CREATE TABLE IF NOT EXISTS waypoints_cold (
    id INTEGER PRIMARY KEY,
    acceleration_x REAL,
    acceleration_y REAL,
    acceleration_z REAL,
    gear TEXT,
    steering_angle REAL,
    brake_applied INTEGER DEFAULT 0,
    blinker_on_left INTEGER DEFAULT 0,
    blinker_on_right INTEGER DEFAULT 0,
    FOREIGN KEY (id) REFERENCES waypoints(id) ON DELETE CASCADE
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

CREATE TABLE IF NOT EXISTS indexing_queue (
    canonical_key TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50,
    enqueued_at REAL NOT NULL,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    previous_last_error TEXT,
    claimed_by TEXT,
    claimed_at REAL,
    source TEXT
);

CREATE INDEX IF NOT EXISTS idx_waypoints_trip ON waypoints(trip_id);
CREATE INDEX IF NOT EXISTS idx_waypoints_coords ON waypoints(lat, lon);
CREATE INDEX IF NOT EXISTS idx_waypoints_timestamp ON waypoints(timestamp);
CREATE INDEX IF NOT EXISTS idx_waypoints_video_path ON waypoints(video_path);
CREATE INDEX IF NOT EXISTS idx_waypoints_trip_video ON waypoints(trip_id, video_path);
CREATE INDEX IF NOT EXISTS idx_trips_start_time ON trips(start_time);
CREATE INDEX IF NOT EXISTS idx_events_trip ON detected_events(trip_id);
CREATE INDEX IF NOT EXISTS idx_events_coords ON detected_events(lat, lon);
CREATE INDEX IF NOT EXISTS idx_events_type ON detected_events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON detected_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_trips_day ON trips(substr(start_time, 1, 10));
CREATE INDEX IF NOT EXISTS idx_events_day ON detected_events(substr(timestamp, 1, 10));
CREATE INDEX IF NOT EXISTS idx_queue_ready
    ON indexing_queue(priority, enqueued_at)
    WHERE claimed_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_queue_claimed_at
    ON indexing_queue(claimed_at)
    WHERE claimed_by IS NOT NULL;

CREATE TABLE IF NOT EXISTS archive_queue (
    id INTEGER PRIMARY KEY,
    source_path TEXT UNIQUE NOT NULL,
    dest_path TEXT,
    priority INTEGER DEFAULT 3,
    status TEXT DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    last_error TEXT,
    previous_last_error TEXT,
    enqueued_at TEXT NOT NULL,
    claimed_at TEXT,
    claimed_by TEXT,
    copied_at TEXT,
    expected_size INTEGER,
    expected_mtime REAL
);
CREATE INDEX IF NOT EXISTS archive_queue_ready
    ON archive_queue(status, priority, expected_mtime)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS archive_queue_source_gone_claimed
    ON archive_queue(claimed_at)
    WHERE status = 'source_gone';

CREATE TABLE IF NOT EXISTS kv_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL,
    dest_path TEXT,
    stage TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    priority INTEGER DEFAULT 5,
    attempts INTEGER DEFAULT 0,
    last_error TEXT,
    next_retry_at REAL,
    enqueued_at REAL NOT NULL,
    completed_at REAL,
    payload_json TEXT,
    legacy_id INTEGER,
    legacy_table TEXT,
    claimed_by TEXT,
    claimed_at REAL,
    UNIQUE(source_path, stage, legacy_table)
);
CREATE INDEX IF NOT EXISTS idx_pipeline_ready
    ON pipeline_queue(stage, status, priority, enqueued_at)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_pipeline_legacy
    ON pipeline_queue(legacy_table, legacy_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_stale_claims
    ON pipeline_queue(claimed_at)
    WHERE status = 'in_progress' AND claimed_at IS NOT NULL;
"""


class MappingMigrationError(RuntimeError):
    """A schema migration could not be completed."""


class MappingDatabaseError(RuntimeError):
    """The mapping database could not be opened or configured."""


@dataclass(frozen=True, slots=True)
class MigrationsConfig:
    db_path: Path
    backup_dir: Path
    backup_retention: int = _BACKUP_RETENTION

    def __post_init__(self) -> None:
        if self.backup_retention <= 0:
            raise ValueError("backup_retention must be > 0")


@dataclass(frozen=True, slots=True)
class MigrationsRunner:
    config: MigrationsConfig

    def init_db(self) -> sqlite3.Connection:
        return _init_db(self.config)

    @contextmanager
    def open_db(self) -> Iterator[sqlite3.Connection]:
        connection = self.init_db()
        try:
            yield connection
        finally:
            connection.close()

    def backup_db(self, *, target_version: int = _SCHEMA_VERSION) -> Path | None:
        return _backup_db(self.config, target_version)


def make_migrations_runner(cfg: WebConfig | MigrationsConfig) -> MigrationsRunner:
    if isinstance(cfg, MigrationsConfig):
        return MigrationsRunner(cfg)
    return MigrationsRunner(
        MigrationsConfig(
            db_path=cfg.mapping.db_path,
            backup_dir=cfg.mapping.backup_dir,
            backup_retention=cfg.mapping.backup_retention,
        )
    )


def _connect_database(db_path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(db_path, timeout=15)
    except sqlite3.Error as exc:
        raise MappingDatabaseError(f"Failed to open mapping database {db_path}: {exc}") from exc
    connection.row_factory = sqlite3.Row
    try:
        _configure_connection(connection)
    except sqlite3.Error as exc:
        connection.close()
        msg = f"Failed to configure mapping database {db_path}: {exc}"
        raise MappingDatabaseError(msg) from exc
    return connection


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=15000")
    connection.execute("PRAGMA cache_size=-2000")
    connection.execute("PRAGMA mmap_size=0")
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute("PRAGMA journal_size_limit=4194304")
    connection.execute("PRAGMA wal_autocheckpoint=200")
    connection.execute("PRAGMA foreign_keys=ON")


def _backup_timestamp() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")


def _backup_db(config: MigrationsConfig, target_version: int) -> Path | None:
    if not config.db_path.is_file():
        return None
    try:
        config.backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = config.backup_dir / (
            f"{config.db_path.name}.bak.v{target_version}.{_backup_timestamp()}"
        )
        shutil.copy2(config.db_path, backup_path)
        _prune_backups(config)
        logger.info("Backed up mapping DB to %s", backup_path)
        return backup_path
    except OSError as exc:
        logger.warning("Failed to back up mapping DB before migration: %s", exc)
        return None


def _prune_backups(config: MigrationsConfig) -> None:
    backups = sorted(config.backup_dir.glob(f"{config.db_path.name}.bak.v*"))
    for old_backup in backups[: -config.backup_retention]:
        try:
            old_backup.unlink()
        except OSError:
            logger.warning("Failed to remove old mapping backup %s", old_backup)


def _current_schema_version(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None:
        return 0
    value = row["version"]
    return value if isinstance(value, int) else 0


def _init_db(config: MigrationsConfig) -> sqlite3.Connection:
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = _connect_database(config.db_path)
    current_version = _current_schema_version(connection)
    if current_version >= _SCHEMA_VERSION:
        return connection
    if current_version > 0:
        _backup_db(config, _SCHEMA_VERSION)
    try:
        _prepare_pipeline_claim_columns(connection, current_version)
        connection.executescript(_SCHEMA_SQL)
        _apply_pending_migrations(connection, current_version)
        _set_schema_version(connection, _SCHEMA_VERSION)
        connection.commit()
    except MappingMigrationError:
        connection.rollback()
        raise
    except sqlite3.Error as exc:
        connection.rollback()
        raise MappingDatabaseError(f"Failed to initialize mapping database: {exc}") from exc
    logger.info("Mapping database initialized (v%d) at %s", _SCHEMA_VERSION, config.db_path)
    return connection


def _prepare_pipeline_claim_columns(connection: sqlite3.Connection, current_version: int) -> None:
    if current_version <= 0 or current_version >= _PIPELINE_CLAIM_COLS_VERSION:
        return
    try:
        rows = connection.execute("PRAGMA table_info(pipeline_queue)").fetchall()
        cols = {str(row[1]) for row in rows}
        if cols and "claimed_by" not in cols:
            connection.execute("ALTER TABLE pipeline_queue ADD COLUMN claimed_by TEXT")
        if cols and "claimed_at" not in cols:
            connection.execute("ALTER TABLE pipeline_queue ADD COLUMN claimed_at REAL")
    except sqlite3.Error as exc:
        logger.debug("Pre-script v16->v17 ALTER TABLE skipped: %s", exc)


def _apply_pending_migrations(connection: sqlite3.Connection, current_version: int) -> None:
    _maybe_add_v2_blinker_columns(connection, current_version)
    _maybe_run_savepoint_migration(
        connection,
        name="migrate_v3",
        current_version=current_version,
        target_version=3,
        migration=_migrate_v2_to_v3,
    )
    _maybe_run_savepoint_migration(
        connection,
        name="migrate_v4",
        current_version=current_version,
        target_version=4,
        migration=_migrate_v3_to_v4,
    )
    _maybe_run_savepoint_migration(
        connection,
        name="migrate_v9",
        current_version=current_version,
        target_version=9,
        migration=_migrate_v8_to_v9,
    )
    _maybe_add_previous_error_columns(connection, current_version)
    _maybe_run_savepoint_migration(
        connection,
        name="migrate_v13",
        current_version=current_version,
        target_version=13,
        migration=_migrate_v12_to_v13,
    )
    _maybe_run_savepoint_migration(
        connection,
        name="migrate_v15",
        current_version=current_version,
        target_version=15,
        migration=_migrate_v14_to_v15,
    )
    if 0 < current_version < _PIPELINE_QUEUE_VERSION:
        logger.info("Migration v15->v16: pipeline_queue table ready")
    if 0 < current_version < _PIPELINE_CLAIM_COLS_VERSION:
        logger.info("Migration v16->v17: pipeline_queue claim-bookkeeping columns ready")


def _maybe_add_v2_blinker_columns(connection: sqlite3.Connection, current_version: int) -> None:
    if not (0 < current_version < _SCHEMA_WITH_BLINKERS_VERSION):
        return
    for column in ("blinker_on_left", "blinker_on_right"):
        try:
            connection.execute(f"ALTER TABLE waypoints ADD COLUMN {column} INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            logger.debug("Skipping existing waypoints column %s", column)


def _maybe_add_previous_error_columns(connection: sqlite3.Connection, current_version: int) -> None:
    if not (0 < current_version < _PREVIOUS_ERROR_COLUMNS_VERSION):
        return
    for table_name in ("archive_queue", "indexing_queue"):
        try:
            connection.execute(f"ALTER TABLE {table_name} ADD COLUMN previous_last_error TEXT")
        except sqlite3.OperationalError:
            logger.debug("Skipping existing %s.previous_last_error", table_name)


def _maybe_run_savepoint_migration(
    connection: sqlite3.Connection,
    *,
    name: str,
    current_version: int,
    target_version: int,
    migration: Callable[[sqlite3.Connection], None],
) -> None:
    if not (0 < current_version < target_version):
        return
    connection.execute(f"SAVEPOINT {name}")
    try:
        migration(connection)
    except Exception as exc:
        connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
        connection.execute(f"RELEASE SAVEPOINT {name}")
        raise MappingMigrationError(f"Migration to schema v{target_version} failed: {exc}") from exc
    connection.execute(f"RELEASE SAVEPOINT {name}")


def _set_schema_version(connection: sqlite3.Connection, version: int) -> None:
    connection.execute("DELETE FROM schema_version")
    connection.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
    repaired = _repair_bad_source_folders(connection)
    merged = _merge_all_adjacent_trip_pairs(connection, _TRIP_GAP_MINUTES_DEFAULT * 60)
    deduped = _dedupe_trip_waypoints(connection)
    recomputed, dropped = _recompute_all_trip_stats(connection)
    logger.info(
        "Migration v2->v3: fixed %d bad source folders; merged %d trip pairs; "
        "deduped %d waypoints; recomputed %d trips; dropped %d empty trips",
        repaired,
        merged,
        deduped,
        recomputed,
        dropped,
    )


def _repair_bad_source_folders(connection: sqlite3.Connection) -> int:
    fixed = 0
    rows = connection.execute(
        "SELECT id FROM trips WHERE source_folder = '..' OR source_folder LIKE '..%'"
    ).fetchall()
    for row in rows:
        waypoint = connection.execute(
            "SELECT video_path FROM waypoints WHERE trip_id = ? AND video_path IS NOT NULL "
            "ORDER BY id LIMIT 1",
            (row["id"],),
        ).fetchone()
        video_path = None if waypoint is None else waypoint["video_path"]
        if isinstance(video_path, str):
            connection.execute(
                "UPDATE trips SET source_folder = ? WHERE id = ?",
                (_infer_source_folder(video_path), row["id"]),
            )
            fixed += 1
    return fixed


def _infer_source_folder(video_path: str) -> str:
    normalized = video_path.replace("\\", "/")
    if "/" in normalized:
        return normalized.split("/", 1)[0]
    return "Unknown"


def _merge_all_adjacent_trip_pairs(connection: sqlite3.Connection, gap_seconds: float) -> int:
    for merged_count in range(_MERGE_MAX_ITERATIONS):
        pair = connection.execute(
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
        if pair is None:
            return merged_count
        keep_id = pair["keep_id"]
        drop_id = pair["drop_id"]
        connection.execute(
            "UPDATE waypoints SET trip_id = ? WHERE trip_id = ?",
            (keep_id, drop_id),
        )
        connection.execute(
            "UPDATE detected_events SET trip_id = ? WHERE trip_id = ?",
            (keep_id, drop_id),
        )
        _refresh_trip_bounds(connection, int(keep_id))
        connection.execute("DELETE FROM trips WHERE id = ?", (drop_id,))
    raise MappingMigrationError("Trip merge iteration safety bound exceeded")


def _refresh_trip_bounds(connection: sqlite3.Connection, trip_id: int) -> None:
    bounds = connection.execute(
        "SELECT MIN(timestamp) AS start_time, MAX(timestamp) AS end_time "
        "FROM waypoints WHERE trip_id = ?",
        (trip_id,),
    ).fetchone()
    if bounds is None:
        return
    start_time = bounds["start_time"]
    end_time = bounds["end_time"]
    if isinstance(start_time, str) and isinstance(end_time, str):
        connection.execute(
            "UPDATE trips SET start_time = ?, end_time = ? WHERE id = ?",
            (start_time, end_time, trip_id),
        )


def _dedupe_trip_waypoints(connection: sqlite3.Connection) -> int:
    removed = 0
    duplicates = connection.execute(
        """SELECT trip_id, timestamp, lat, lon, COUNT(*) AS duplicate_count
           FROM waypoints
           WHERE trip_id IS NOT NULL
           GROUP BY trip_id, timestamp, lat, lon
           HAVING COUNT(*) > 1"""
    ).fetchall()
    for duplicate in duplicates:
        rows = connection.execute(
            """SELECT id, video_path FROM waypoints
               WHERE trip_id = ? AND timestamp = ? AND lat = ? AND lon = ?
               ORDER BY id""",
            (
                duplicate["trip_id"],
                duplicate["timestamp"],
                duplicate["lat"],
                duplicate["lon"],
            ),
        ).fetchall()
        drop_ids = [(row["id"],) for row in rows[1:]]
        if drop_ids:
            connection.executemany("DELETE FROM waypoints WHERE id = ?", drop_ids)
            removed += len(drop_ids)
    return removed


def _recompute_all_trip_stats(connection: sqlite3.Connection) -> tuple[int, int]:
    recomputed = 0
    dropped = 0
    trips = connection.execute("SELECT id FROM trips").fetchall()
    for row in trips:
        if _recompute_trip_stats(connection, int(row["id"])):
            recomputed += 1
        else:
            dropped += 1
    return recomputed, dropped


def _recompute_trip_stats(connection: sqlite3.Connection, trip_id: int) -> bool:
    bounds = connection.execute(
        "SELECT MIN(timestamp) AS first_ts, MAX(timestamp) AS last_ts "
        "FROM waypoints WHERE trip_id = ?",
        (trip_id,),
    ).fetchone()
    if (
        bounds is None
        or not isinstance(bounds["first_ts"], str)
        or not isinstance(bounds["last_ts"], str)
    ):
        connection.execute("DELETE FROM trips WHERE id = ?", (trip_id,))
        return False
    first_ts = bounds["first_ts"]
    last_ts = bounds["last_ts"]
    first_row = connection.execute(
        "SELECT lat, lon FROM waypoints WHERE trip_id = ? AND timestamp = ? ORDER BY id LIMIT 1",
        (trip_id, first_ts),
    ).fetchone()
    last_row = connection.execute(
        "SELECT lat, lon FROM waypoints WHERE trip_id = ? AND timestamp = ? "
        "ORDER BY id DESC LIMIT 1",
        (trip_id, last_ts),
    ).fetchone()
    connection.execute(
        """UPDATE trips
              SET start_time = ?, end_time = ?, start_lat = ?, start_lon = ?,
                  end_lat = ?, end_lon = ?, distance_km = ?, duration_seconds = ?
            WHERE id = ?""",
        (
            first_ts,
            last_ts,
            None if first_row is None else first_row["lat"],
            None if first_row is None else first_row["lon"],
            None if last_row is None else last_row["lat"],
            None if last_row is None else last_row["lon"],
            _trip_distance_km(connection, trip_id),
            _trip_duration_seconds(first_ts, last_ts),
            trip_id,
        ),
    )
    return True


def _trip_distance_km(connection: sqlite3.Connection, trip_id: int) -> float:
    total_distance = 0.0
    previous_row: sqlite3.Row | None = None
    previous_video: str | None = None
    rows = connection.execute(
        "SELECT video_path, lat, lon FROM waypoints WHERE trip_id = ? AND video_path IS NOT NULL "
        "ORDER BY video_path, id",
        (trip_id,),
    ).fetchall()
    for row in rows:
        video_path = row["video_path"]
        if (
            previous_row is not None
            and isinstance(video_path, str)
            and video_path == previous_video
        ):
            total_distance += _haversine_km(
                float(previous_row["lat"]),
                float(previous_row["lon"]),
                float(row["lat"]),
                float(row["lon"]),
            )
        previous_row = row
        previous_video = video_path if isinstance(video_path, str) else None
    return total_distance


def _trip_duration_seconds(first_ts: str, last_ts: str) -> int:
    try:
        started = datetime.fromisoformat(first_ts)
        ended = datetime.fromisoformat(last_ts)
    except ValueError:
        return 0
    return max(0, int((ended - started).total_seconds()))


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
    deleted_events = connection.execute(
        "DELETE FROM detected_events WHERE event_type IN ('saved', 'sentry') "
        "AND metadata IS NOT NULL AND (metadata LIKE '%inferred_location%' "
        "OR metadata LIKE '%nearest_waypoint%')"
    ).rowcount
    cleared_files = connection.execute(
        "DELETE FROM indexed_files WHERE waypoint_count = 0 AND (file_path LIKE '%/SavedClips/%' "
        "OR file_path LIKE '%/SentryClips/%' OR file_path LIKE '%\\SavedClips\\%' "
        "OR file_path LIKE '%\\SentryClips\\%')"
    ).rowcount
    logger.info(
        "Migration v3->v4: cleared %d stale events and %d indexed_files rows",
        deleted_events,
        cleared_files,
    )


def _migrate_v8_to_v9(connection: sqlite3.Connection) -> None:
    merged = _merge_all_adjacent_trip_pairs(connection, _TRIP_GAP_MINUTES_DEFAULT * 60)
    logger.info("Migration v8->v9: merged %d phantom-fragmented trip pairs", merged)


def _migrate_v12_to_v13(connection: sqlite3.Connection) -> None:
    flipped = connection.execute(
        """UPDATE archive_queue
              SET priority = CASE priority WHEN 1 THEN 2 WHEN 2 THEN 1 ELSE priority END
            WHERE status IN ('pending', 'claimed', 'error')
              AND priority IN (1, 2)"""
    ).rowcount
    logger.info("Migration v12->v13: flipped priority on %d archive_queue rows", flipped)


def _waypoints_has_cold_columns(connection: sqlite3.Connection) -> bool:
    try:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(waypoints)").fetchall()
        }
    except sqlite3.Error:
        return False
    return any(column in columns for column in _COLD_COLUMNS)


def _migrate_v14_to_v15(connection: sqlite3.Connection) -> None:
    if not _waypoints_has_cold_columns(connection):
        logger.info("Migration v14->v15: waypoints already uses the split schema")
        return
    _snapshot_cold_rows(connection)
    try:
        _drop_cold_cols_via_rewrite(connection)
    except sqlite3.Error as exc:
        logger.warning(
            "Migration v14->v15 rewrite failed (%s); falling back to per-column drops",
            exc,
        )
        _drop_cold_cols_via_per_column_alter(connection)
    _restore_cold_rows(connection)
    _recreate_waypoint_indexes(connection)
    _assert_foreign_keys_clean(connection)


def _snapshot_cold_rows(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE IF EXISTS temp._migrate_v15_cold_snap")
    connection.execute(_SNAPSHOT_COLD_ROWS_SQL)


def _drop_cold_cols_via_rewrite(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE IF EXISTS waypoints_new")
    connection.execute("PRAGMA defer_foreign_keys = ON")
    sequence_value = _waypoints_sequence_high_water(connection)
    connection.execute(f"CREATE TABLE waypoints_new ({_waypoints_hot_table_sql()})")
    connection.execute(_V15_COPY_WAYPOINTS_SQL)
    connection.execute("DROP TABLE waypoints")
    connection.execute("ALTER TABLE waypoints_new RENAME TO waypoints")
    _restore_waypoints_sequence(connection, sequence_value)


def _waypoints_hot_table_sql() -> str:
    return ", ".join(f"{column} {_V15_HOT_COLUMN_DDL[column]}" for column in _V15_HOT_COLUMNS)


def _waypoints_sequence_high_water(connection: sqlite3.Connection) -> int:
    sequence_row = connection.execute(
        "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name='waypoints'), 0)"
    ).fetchone()
    max_id_row = connection.execute("SELECT COALESCE(MAX(id), 0) FROM waypoints").fetchone()
    sequence_value = 0 if sequence_row is None else int(sequence_row[0])
    max_id = 0 if max_id_row is None else int(max_id_row[0])
    return max(sequence_value, max_id)


def _restore_waypoints_sequence(connection: sqlite3.Connection, sequence_value: int) -> None:
    if sequence_value <= 0:
        return
    updated = connection.execute(
        "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'waypoints'",
        (sequence_value,),
    ).rowcount
    if updated == 0:
        connection.execute(
            "INSERT INTO sqlite_sequence (name, seq) VALUES ('waypoints', ?)",
            (sequence_value,),
        )


def _drop_cold_cols_via_per_column_alter(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE IF EXISTS waypoints_new")
    for column in _COLD_COLUMNS:
        try:
            connection.execute(f"ALTER TABLE waypoints DROP COLUMN {column}")
        except sqlite3.OperationalError:
            logger.warning("Migration v14->v15 fallback could not drop %s", column)


def _restore_cold_rows(connection: sqlite3.Connection) -> None:
    connection.execute(_RESTORE_COLD_ROWS_SQL)
    connection.execute("DROP TABLE temp._migrate_v15_cold_snap")


def _recreate_waypoint_indexes(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE INDEX IF NOT EXISTS idx_waypoints_trip ON waypoints(trip_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_waypoints_coords ON waypoints(lat, lon)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_waypoints_timestamp ON waypoints(timestamp)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_waypoints_video_path ON waypoints(video_path)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_waypoints_trip_video ON waypoints(trip_id, video_path)"
    )


def _assert_foreign_keys_clean(connection: sqlite3.Connection) -> None:
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise sqlite3.IntegrityError(
            f"v14->v15 foreign_key_check returned {len(violations)} row(s)"
        )


__all__ = (
    "_BACKUP_RETENTION",
    "_SCHEMA_SQL",
    "_SCHEMA_VERSION",
    "MappingDatabaseError",
    "MappingMigrationError",
    "MigrationsConfig",
    "MigrationsRunner",
    "_backup_db",
    "_init_db",
    "make_migrations_runner",
)
