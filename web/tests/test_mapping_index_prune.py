"""Regression tests for immediate worker-index pruning after web deletes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from teslausb_web.services.mapping_index_prune import (
    prune_deleted_clips,
    prune_deleted_event_folder,
)

_SCHEMA_DDL = """
CREATE TABLE clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_path TEXT NOT NULL UNIQUE,
    bucket TEXT NOT NULL,
    clip_started_utc INTEGER,
    indexed_at_utc INTEGER NOT NULL,
    waypoint_count INTEGER NOT NULL DEFAULT 0,
    gps_waypoint_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE waypoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    frame_index INTEGER NOT NULL,
    timestamp_ms REAL NOT NULL,
    latitude_deg REAL NOT NULL,
    longitude_deg REAL NOT NULL,
    speed_mps REAL NOT NULL,
    heading_deg REAL NOT NULL
);
CREATE TABLE detected_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER,
    clip_id INTEGER REFERENCES clips(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    timestamp_utc INTEGER NOT NULL,
    latitude_deg REAL,
    longitude_deg REAL,
    speed_mps REAL,
    metadata_json TEXT,
    description TEXT NOT NULL DEFAULT '',
    frame_index INTEGER
);
CREATE TABLE clip_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_json_relative_path TEXT NOT NULL UNIQUE,
    event_dir_relative_path TEXT NOT NULL,
    bucket TEXT NOT NULL,
    primary_clip_id INTEGER REFERENCES clips(id) ON DELETE SET NULL,
    timestamp_utc INTEGER NOT NULL,
    est_lat REAL,
    est_lon REAL,
    reason TEXT,
    city TEXT,
    camera TEXT,
    indexed_at_utc INTEGER NOT NULL
);
"""


def _create_worker_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(_SCHEMA_DDL)


def _insert_clip(connection: sqlite3.Connection, relative_path: str) -> int:
    cursor = connection.execute(
        "INSERT INTO clips(relative_path, bucket, clip_started_utc, indexed_at_utc) "
        "VALUES (?, 'sentry', 1736944245, 1736944246)",
        (relative_path,),
    )
    clip_id = cursor.lastrowid
    if clip_id is None:
        raise AssertionError("SQLite did not return a clip id")
    connection.execute(
        "INSERT INTO waypoints(clip_id, frame_index, timestamp_ms, latitude_deg, "
        "longitude_deg, speed_mps, heading_deg) VALUES (?, 0, 0.0, 30.0, -97.0, 1.0, 2.0)",
        (clip_id,),
    )
    connection.execute(
        "INSERT INTO detected_events(clip_id, event_type, severity, timestamp_utc) "
        "VALUES (?, 'sentry', 'info', 1736944245)",
        (clip_id,),
    )
    return int(clip_id)


def _table_count(db_path: Path, table_name: str) -> int:
    queries = {
        "clip_events": "SELECT COUNT(*) FROM clip_events",
        "clips": "SELECT COUNT(*) FROM clips",
        "detected_events": "SELECT COUNT(*) FROM detected_events",
        "waypoints": "SELECT COUNT(*) FROM waypoints",
    }
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(queries[table_name]).fetchone()
    if row is None:
        raise AssertionError(f"missing count for {table_name}")
    return int(row[0])


def test_prune_deleted_clips_removes_clip_and_cascaded_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "index.sqlite3"
    backing_root = tmp_path / "backing"
    clip_path = "TeslaCam/RecentClips/2025-03-01_09-15-30-front.mp4"
    _create_worker_db(db_path)
    with sqlite3.connect(db_path) as connection:
        _insert_clip(connection, clip_path)

    result = prune_deleted_clips(db_path, backing_root, (Path(clip_path),))

    assert result.clips_deleted == 1
    assert result.waypoints_deleted == 1
    assert result.detected_events_deleted == 1
    assert _table_count(db_path, "clips") == 0
    assert _table_count(db_path, "waypoints") == 0
    assert _table_count(db_path, "detected_events") == 0


def test_prune_deleted_event_folder_removes_clip_event_row(tmp_path: Path) -> None:
    db_path = tmp_path / "index.sqlite3"
    backing_root = tmp_path / "backing"
    event_dir = Path("TeslaCam/SentryClips/2025-01-15_12-30-45")
    clip_path = event_dir / "2025-01-15_12-30-45-front.mp4"
    unrelated_dir = "TeslaCam/SentryClips/2025-01-16_12-30-45"
    _create_worker_db(db_path)
    with sqlite3.connect(db_path) as connection:
        clip_id = _insert_clip(connection, clip_path.as_posix())
        connection.execute(
            "INSERT INTO clip_events(event_json_relative_path, event_dir_relative_path, "
            "bucket, primary_clip_id, timestamp_utc, indexed_at_utc) "
            "VALUES (?, ?, 'sentry', ?, 1736944245, 1736944246)",
            ((event_dir / "event.json").as_posix(), event_dir.as_posix(), clip_id),
        )
        connection.execute(
            "INSERT INTO clip_events(event_json_relative_path, event_dir_relative_path, "
            "bucket, timestamp_utc, indexed_at_utc) "
            "VALUES (?, ?, 'sentry', 1737030645, 1737030646)",
            (f"{unrelated_dir}/event.json", unrelated_dir),
        )

    result = prune_deleted_event_folder(db_path, backing_root, event_dir, (clip_path,))

    assert result.clips_deleted == 1
    assert result.clip_events_deleted == 1
    assert result.waypoints_deleted == 1
    assert _table_count(db_path, "clips") == 0
    assert _table_count(db_path, "waypoints") == 0
    assert _table_count(db_path, "detected_events") == 0
    assert _table_count(db_path, "clip_events") == 1


def test_prune_missing_db_is_noop(tmp_path: Path) -> None:
    result = prune_deleted_clips(
        tmp_path / "missing.sqlite3",
        tmp_path / "backing",
        (Path("TeslaCam/RecentClips/missing-front.mp4"),),
    )

    assert result.clips_deleted == 0
    assert result.clip_events_deleted == 0
    assert result.detected_events_deleted == 0
    assert result.waypoints_deleted == 0
