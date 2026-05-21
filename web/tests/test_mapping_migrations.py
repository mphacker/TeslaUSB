from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest
from teslausb_web.services import mapping_migrations
from teslausb_web.services.mapping_migrations import (
    _SCHEMA_SQL,
    _SCHEMA_VERSION,
    MigrationsConfig,
    _backup_db,
    _init_db,
    make_migrations_runner,
)

if TYPE_CHECKING:
    from pathlib import Path

_LEGACY_SCHEMA_SQL = """
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY
);
CREATE TABLE trips (
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
CREATE TABLE waypoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER REFERENCES trips(id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    heading REAL,
    speed_mps REAL,
    autopilot_state TEXT,
    video_path TEXT,
    frame_offset INTEGER,
    acceleration_x REAL,
    acceleration_y REAL,
    acceleration_z REAL,
    gear TEXT,
    steering_angle REAL,
    brake_applied INTEGER DEFAULT 0,
    blinker_on_left INTEGER DEFAULT 0,
    blinker_on_right INTEGER DEFAULT 0
);
CREATE TABLE detected_events (
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
CREATE TABLE indexed_files (
    file_path TEXT PRIMARY KEY,
    file_size INTEGER,
    file_mtime REAL,
    indexed_at TEXT,
    waypoint_count INTEGER DEFAULT 0,
    event_count INTEGER DEFAULT 0
);
"""
_V16_SCHEMA_SQL = """
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY
);
CREATE TABLE trips (
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
CREATE TABLE waypoints (
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
CREATE TABLE waypoints_cold (
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
CREATE TABLE detected_events (
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
CREATE TABLE indexed_files (
    file_path TEXT PRIMARY KEY,
    file_size INTEGER,
    file_mtime REAL,
    indexed_at TEXT,
    waypoint_count INTEGER DEFAULT 0,
    event_count INTEGER DEFAULT 0
);
CREATE TABLE indexing_queue (
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
CREATE TABLE archive_queue (
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
CREATE TABLE kv_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE pipeline_queue (
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
    UNIQUE(source_path, stage, legacy_table)
);
"""


@pytest.fixture
def config(tmp_path: Path) -> MigrationsConfig:
    state_dir = tmp_path / "state"
    return MigrationsConfig(
        db_path=state_dir / "mapping.db",
        backup_dir=state_dir / "mapping-backups",
        backup_retention=3,
    )


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def _schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
    assert row is not None
    return int(row[0])


def _write_snapshot(db_path: Path, version: int) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        if version <= 14:
            connection.executescript(_LEGACY_SCHEMA_SQL)
        elif version == 16:
            connection.executescript(_V16_SCHEMA_SQL)
        else:
            connection.executescript(_SCHEMA_SQL)
        connection.execute("DELETE FROM schema_version")
        connection.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        connection.commit()
    finally:
        connection.close()


def _write_rich_v2_snapshot(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(_LEGACY_SCHEMA_SQL)
        connection.executescript(
            """
            CREATE TABLE archive_queue (
                id INTEGER PRIMARY KEY,
                source_path TEXT UNIQUE NOT NULL,
                dest_path TEXT,
                priority INTEGER DEFAULT 3,
                status TEXT DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,
                last_error TEXT,
                enqueued_at TEXT NOT NULL,
                claimed_at TEXT,
                claimed_by TEXT,
                copied_at TEXT,
                expected_size INTEGER,
                expected_mtime REAL
            );
            """
        )
        connection.execute("INSERT INTO schema_version (version) VALUES (2)")
        connection.executemany(
            "INSERT INTO trips (id, start_time, end_time, source_folder, indexed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                (1, "2024-01-01T00:00:00", "2024-01-01T00:00:10", "..", "2024-01-01T00:05:00"),
                (
                    2,
                    "2024-01-01T00:00:20",
                    "2024-01-01T00:00:30",
                    "RecentClips",
                    "2024-01-01T00:05:00",
                ),
            ),
        )
        connection.executemany(
            """INSERT INTO waypoints (
                   id, trip_id, timestamp, lat, lon, heading, speed_mps, autopilot_state,
                   video_path, frame_offset, acceleration_x, acceleration_y, acceleration_z,
                   gear, steering_angle, brake_applied, blinker_on_left, blinker_on_right
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                (
                    1,
                    1,
                    "2024-01-01T00:00:00",
                    10.0,
                    20.0,
                    0.0,
                    1.0,
                    "manual",
                    "ArchivedClips\\drive-1.mp4",
                    0,
                    0.20,
                    0.0,
                    0.0,
                    "DRIVE",
                    1.0,
                    0,
                    0,
                    0,
                ),
                (
                    2,
                    1,
                    "2024-01-01T00:00:00",
                    10.0,
                    20.0,
                    0.0,
                    1.0,
                    "manual",
                    "RecentClips\\drive-1.mp4",
                    1,
                    0.0,
                    0.0,
                    0.0,
                    "PARK",
                    0.0,
                    0,
                    0,
                    0,
                ),
                (
                    3,
                    1,
                    "2024-01-01T00:00:10",
                    10.0,
                    20.01,
                    0.0,
                    1.0,
                    "manual",
                    "ArchivedClips\\drive-1.mp4",
                    2,
                    0.0,
                    0.0,
                    0.0,
                    "PARK",
                    0.0,
                    0,
                    0,
                    0,
                ),
                (
                    4,
                    2,
                    "2024-01-01T00:00:20",
                    10.0,
                    20.02,
                    0.0,
                    1.0,
                    "manual",
                    "RecentClips\\drive-2.mp4",
                    3,
                    0.0,
                    0.0,
                    0.0,
                    "PARK",
                    0.0,
                    0,
                    0,
                    0,
                ),
            ),
        )
        connection.executemany(
            """INSERT INTO detected_events (
                   id, trip_id, timestamp, lat, lon, event_type, severity, description,
                   video_path, frame_offset, metadata
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                (
                    1,
                    2,
                    "2024-01-01T00:00:20",
                    10.0,
                    20.02,
                    "hard_brake",
                    "info",
                    "kept event",
                    "RecentClips\\drive-2.mp4",
                    0,
                    None,
                ),
                (
                    2,
                    1,
                    "2024-01-01T00:00:00",
                    10.0,
                    20.0,
                    "saved",
                    "info",
                    "stale inferred event",
                    "SavedClips\\drive-1.mp4",
                    0,
                    '{"inferred_location": true}',
                ),
            ),
        )
        connection.execute(
            """INSERT INTO indexed_files (
                   file_path, file_size, file_mtime, indexed_at, waypoint_count, event_count
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "C:\\TeslaCam\\SavedClips\\drive-1.mp4",
                100,
                1.0,
                "2024-01-01T00:05:00",
                0,
                1,
            ),
        )
        connection.executemany(
            "INSERT INTO archive_queue (id, source_path, priority, status, enqueued_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                (1, "a.mp4", 1, "pending", "2024-01-01T00:00:00"),
                (2, "b.mp4", 2, "error", "2024-01-01T00:00:00"),
            ),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize("version", range(2, _SCHEMA_VERSION + 1))
def test_migrates_every_snapshot_version_to_v17(config: MigrationsConfig, version: int) -> None:
    _write_snapshot(config.db_path, version)

    connection = _init_db(config)
    try:
        assert _schema_version(connection) == _SCHEMA_VERSION
        assert "claimed_by" in _table_columns(connection, "pipeline_queue")
        assert "claimed_at" in _table_columns(connection, "pipeline_queue")
        assert "acceleration_x" not in _table_columns(connection, "waypoints")
        assert "acceleration_x" in _table_columns(connection, "waypoints_cold")
    finally:
        connection.close()


def test_fresh_db_init_builds_current_schema_and_pragmas(config: MigrationsConfig) -> None:
    runner = make_migrations_runner(config)

    connection = runner.init_db()
    try:
        assert _schema_version(connection) == _SCHEMA_VERSION
        assert tuple(connection.execute("PRAGMA journal_mode").fetchone() or ()) == ("wal",)
        assert tuple(connection.execute("PRAGMA busy_timeout").fetchone() or ()) == (15000,)
        assert "pipeline_queue" in {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        connection.close()


def test_v2_snapshot_runs_data_migrations(config: MigrationsConfig) -> None:
    _write_rich_v2_snapshot(config.db_path)

    connection = _init_db(config)
    try:
        assert _schema_version(connection) == _SCHEMA_VERSION
        assert tuple(connection.execute("SELECT COUNT(*) FROM trips").fetchone() or ()) == (1,)
        assert tuple(connection.execute("SELECT source_folder FROM trips").fetchone() or ()) == (
            "ArchivedClips",
        )
        assert tuple(connection.execute("SELECT COUNT(*) FROM waypoints").fetchone() or ()) == (3,)
        assert tuple(
            connection.execute(
                "SELECT trip_id FROM detected_events WHERE event_type = 'hard_brake'"
            ).fetchone()
            or ()
        ) == (1,)
        assert tuple(
            connection.execute(
                "SELECT COUNT(*) FROM detected_events WHERE event_type = 'saved'"
            ).fetchone()
            or ()
        ) == (0,)
        indexed_files_count = connection.execute("SELECT COUNT(*) FROM indexed_files").fetchone()
        assert tuple(indexed_files_count or ()) == (0,)
        assert tuple(
            connection.execute("SELECT priority FROM archive_queue WHERE id = 1").fetchone() or ()
        ) == (2,)
        assert tuple(
            connection.execute("SELECT priority FROM archive_queue WHERE id = 2").fetchone() or ()
        ) == (1,)
        cold_rows = connection.execute("SELECT COUNT(*) FROM waypoints_cold").fetchone()
        assert tuple(cold_rows or ()) == (1,)
        assert tuple(connection.execute("SELECT id FROM waypoints_cold").fetchone() or ()) == (1,)
        assert tuple(
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_sequence WHERE name = 'waypoints'"
            ).fetchone()
            or ()
        ) == (1,)
    finally:
        connection.close()


def test_backup_retention_prunes_old_backups(
    config: MigrationsConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    config.db_path.write_text("db", encoding="utf-8")
    timestamps = iter(
        (
            "20240101-000000",
            "20240101-000001",
            "20240101-000002",
            "20240101-000003",
        )
    )
    monkeypatch.setattr(mapping_migrations, "_backup_timestamp", lambda: next(timestamps))

    for _ in range(4):
        assert _backup_db(config, _SCHEMA_VERSION) is not None

    backups = sorted(config.backup_dir.glob(f"{config.db_path.name}.bak.v*"))
    assert [path.name for path in backups] == [
        "mapping.db.bak.v17.20240101-000001",
        "mapping.db.bak.v17.20240101-000002",
        "mapping.db.bak.v17.20240101-000003",
    ]


def test_init_db_is_idempotent_on_current_database(config: MigrationsConfig) -> None:
    first = _init_db(config)
    first.execute("INSERT INTO kv_meta (key, value) VALUES ('boot', '1')")
    first.commit()
    first.close()

    second = _init_db(config)
    try:
        assert _schema_version(second) == _SCHEMA_VERSION
        assert tuple(
            second.execute("SELECT value FROM kv_meta WHERE key = 'boot'").fetchone() or ()
        ) == ("1",)
        assert not config.backup_dir.exists()
    finally:
        second.close()
