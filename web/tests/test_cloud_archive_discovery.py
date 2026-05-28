from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from teslausb_web.services.cloud_archive.discovery import (
    _candidate_priority,
    _discover_events,
    _load_hard_brake_hits,
    _score_event_priority,
)
from teslausb_web.services.cloud_archive.settings import (
    CLOUD_PRIORITY_BULK,
    CLOUD_PRIORITY_HARSH_BRAKE,
    CLOUD_PRIORITY_LIVE_EVENT,
    NO_EVENT_SCORE_THRESHOLD,
    CloudArchiveConfig,
)

if TYPE_CHECKING:
    from pathlib import Path


def _make_config(tmp_path: Path, teslacam_path: Path, **overrides: object) -> CloudArchiveConfig:
    base: dict[str, object] = {
        "enabled": True,
        "db_path": tmp_path / "cloud.db",
        "teslacam_path": teslacam_path,
        "mapping_db_path": tmp_path / "mapping.db",
        "sync_folders": ("SentryClips", "SavedClips"),
        "priority_folders": ("SentryClips", "SavedClips"),
        "sync_non_event": True,
    }
    base.update(overrides)
    return CloudArchiveConfig(**base)


def _make_event_dir(base: Path, folder: str, name: str, *, with_event_json: bool = False) -> Path:
    event_dir = base / folder / name
    event_dir.mkdir(parents=True, exist_ok=True)
    (event_dir / f"{name}-front.mp4").write_bytes(b"video")
    if with_event_json:
        (event_dir / "event.json").write_text(json.dumps({"reason": "sentry"}), encoding="utf-8")
    return event_dir


def test_discover_events_finds_event_directories(tmp_path: Path) -> None:
    teslacam = tmp_path / "TeslaCam"
    _make_event_dir(teslacam, "SentryClips", "2026-01-01_10-00-00", with_event_json=True)
    config = _make_config(tmp_path, teslacam)

    events = _discover_events(config)

    assert len(events) == 1
    assert events[0].relative_path == "SentryClips/2026-01-01_10-00-00"
    assert events[0].size_bytes > 0


def test_discover_events_filters_non_event_when_disabled(tmp_path: Path) -> None:
    teslacam = tmp_path / "TeslaCam"
    _make_event_dir(teslacam, "SentryClips", "2026-01-01_10-00-00", with_event_json=False)
    config = _make_config(tmp_path, teslacam, sync_non_event=False)

    assert _discover_events(config) == ()


def test_discover_events_respects_priority_folders(tmp_path: Path) -> None:
    teslacam = tmp_path / "TeslaCam"
    _make_event_dir(teslacam, "SavedClips", "2026-01-01_10-00-00", with_event_json=True)
    _make_event_dir(teslacam, "SentryClips", "2026-01-01_11-00-00", with_event_json=True)
    config = _make_config(
        tmp_path,
        teslacam,
        sync_folders=("SavedClips", "SentryClips"),
        priority_folders=("SavedClips", "SentryClips"),
    )

    events = _discover_events(config)

    assert [event.relative_path for event in events] == [
        "SavedClips/2026-01-01_10-00-00",
        "SentryClips/2026-01-01_11-00-00",
    ]


def test_discover_events_skips_synced_rows(tmp_path: Path) -> None:
    teslacam = tmp_path / "TeslaCam"
    _make_event_dir(teslacam, "SentryClips", "2026-01-01_10-00-00", with_event_json=True)
    config = _make_config(tmp_path, teslacam)
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE cloud_synced_files (file_path TEXT, status TEXT)")
    connection.execute(
        "INSERT INTO cloud_synced_files (file_path, status) "
        "VALUES ('SentryClips/2026-01-01_10-00-00', 'synced')"
    )
    connection.commit()
    try:
        assert _discover_events(config, connection) == ()
    finally:
        connection.close()


def test_score_event_priority_uses_event_json_and_age(tmp_path: Path) -> None:
    event_dir = _make_event_dir(
        tmp_path, "SentryClips", "2026-01-01_10-00-00", with_event_json=True
    )
    score = _score_event_priority(event_dir)
    assert score < NO_EVENT_SCORE_THRESHOLD


def test_score_event_priority_without_event_json_is_lower_priority(tmp_path: Path) -> None:
    event_dir = _make_event_dir(
        tmp_path, "SavedClips", "2026-01-01_10-00-00", with_event_json=False
    )
    score = _score_event_priority(event_dir)
    assert score >= NO_EVENT_SCORE_THRESHOLD


def _make_recent_clip(teslacam: Path, name: str) -> Path:
    folder = teslacam / "RecentClips"
    folder.mkdir(parents=True, exist_ok=True)
    file_path = folder / name
    file_path.write_bytes(b"video")
    return file_path


def _make_mapping_db_with_waypoint(db_path: Path, video_path: str) -> None:
    connection = sqlite3.connect(str(db_path))
    try:
        connection.executescript(
            """
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
                heading_deg REAL NOT NULL,
                brake_applied INTEGER NOT NULL DEFAULT 0,
                blinker_on_left INTEGER NOT NULL DEFAULT 0,
                blinker_on_right INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        cursor = connection.execute(
            "INSERT INTO clips (relative_path, bucket, indexed_at_utc, "
            "waypoint_count, gps_waypoint_count) VALUES (?, 'recent', 0, 1, 1)",
            (video_path,),
        )
        clip_id = cursor.lastrowid
        connection.execute(
            "INSERT INTO waypoints (clip_id, frame_index, timestamp_ms, "
            "latitude_deg, longitude_deg, speed_mps, heading_deg) "
            "VALUES (?, 0, 0, 0, 0, 0, 0)",
            (clip_id,),
        )
        connection.commit()
    finally:
        connection.close()


def test_discover_events_picks_up_recent_clips_with_telemetry(tmp_path: Path) -> None:
    teslacam = tmp_path / "TeslaCam"
    _make_recent_clip(teslacam, "2026-02-01_10-00-00-front.mp4")
    _make_recent_clip(teslacam, "2026-02-01_10-01-00-front.mp4")
    mapping_db = tmp_path / "mapping.db"
    _make_mapping_db_with_waypoint(
        mapping_db, "RecentClips/2026-02-01_10-00-00-front.mp4"
    )
    config = _make_config(
        tmp_path,
        teslacam,
        mapping_db_path=mapping_db,
        sync_folders=("SentryClips", "SavedClips", "RecentClips"),
        sync_recent_with_telemetry=True,
        sync_non_event=False,
    )

    events = _discover_events(config)

    paths = [event.relative_path for event in events]
    assert "RecentClips/2026-02-01_10-00-00-front.mp4" in paths
    assert "RecentClips/2026-02-01_10-01-00-front.mp4" not in paths


def test_discover_events_skips_recent_clips_when_telemetry_toggle_off(
    tmp_path: Path,
) -> None:
    teslacam = tmp_path / "TeslaCam"
    _make_recent_clip(teslacam, "2026-02-01_10-00-00-front.mp4")
    mapping_db = tmp_path / "mapping.db"
    _make_mapping_db_with_waypoint(
        mapping_db, "RecentClips/2026-02-01_10-00-00-front.mp4"
    )
    config = _make_config(
        tmp_path,
        teslacam,
        mapping_db_path=mapping_db,
        sync_folders=("SentryClips", "SavedClips", "RecentClips"),
        sync_recent_with_telemetry=False,
    )

    assert _discover_events(config) == ()


def test_discover_events_skips_recent_clips_without_recentclips_in_sync_folders(
    tmp_path: Path,
) -> None:
    teslacam = tmp_path / "TeslaCam"
    _make_recent_clip(teslacam, "2026-02-01_10-00-00-front.mp4")
    mapping_db = tmp_path / "mapping.db"
    _make_mapping_db_with_waypoint(
        mapping_db, "RecentClips/2026-02-01_10-00-00-front.mp4"
    )
    config = _make_config(
        tmp_path,
        teslacam,
        mapping_db_path=mapping_db,
        sync_folders=("SentryClips", "SavedClips"),
        sync_recent_with_telemetry=True,
    )

    assert _discover_events(config) == ()

def _make_mapping_db_with_hard_brake(db_path, video_path: str) -> None:
    import sqlite3
    connection = sqlite3.connect(str(db_path))
    try:
        connection.executescript(
            """
            CREATE TABLE clips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                relative_path TEXT NOT NULL UNIQUE,
                bucket TEXT NOT NULL,
                indexed_at_utc INTEGER NOT NULL
            );
            CREATE TABLE detected_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                severity REAL,
                timestamp_utc INTEGER NOT NULL
            );
            """
        )
        cursor = connection.execute(
            "INSERT INTO clips (relative_path, bucket, indexed_at_utc) VALUES (?, 'recent', 0)",
            (video_path,),
        )
        clip_id = cursor.lastrowid
        connection.execute(
            "INSERT INTO detected_events (clip_id, event_type, severity, timestamp_utc) "
            "VALUES (?, 'harsh_braking', -4.5, 0)",
            (clip_id,),
        )
        connection.commit()
    finally:
        connection.close()


def test_load_hard_brake_hits_returns_basename_and_timestamp(tmp_path) -> None:
    mapping_db = tmp_path / "mapping.db"
    _make_mapping_db_with_hard_brake(
        mapping_db, "RecentClips/2026-03-01_12-00-00-front.mp4"
    )
    hits = _load_hard_brake_hits(mapping_db)
    assert hits is not None
    assert "2026-03-01_12-00-00-front.mp4" in hits
    assert "2026-03-01_12-00-00" in hits


def test_load_hard_brake_hits_returns_none_when_table_missing(tmp_path) -> None:
    mapping_db = tmp_path / "mapping.db"
    import sqlite3
    sqlite3.connect(str(mapping_db)).close()  # empty DB, no detected_events
    assert _load_hard_brake_hits(mapping_db) is None


def test_candidate_priority_decision_matrix() -> None:
    hits = frozenset({"2026-03-01_12-00-00-front.mp4", "2026-03-01_12-00-00"})
    # Sentry and Saved are always priority regardless of hits set.
    assert _candidate_priority("SentryClips/2026-03-01_12-00-00", hits) == CLOUD_PRIORITY_LIVE_EVENT
    assert _candidate_priority("SavedClips/2026-03-01_12-00-00", hits) == CLOUD_PRIORITY_LIVE_EVENT
    assert _candidate_priority("SentryClips/2026-03-01_12-00-00", None) == CLOUD_PRIORITY_LIVE_EVENT
    # RecentClips matching basename or timestamp -> priority.
    assert (
        _candidate_priority("RecentClips/2026-03-01_12-00-00-front.mp4", hits)
        == CLOUD_PRIORITY_HARSH_BRAKE
    )
    assert (
        _candidate_priority("RecentClips/2026-03-01_12-00-00-rear.mp4", hits)
        == CLOUD_PRIORITY_HARSH_BRAKE
    )
    # RecentClips without a hit -> bulk priority.
    assert (
        _candidate_priority("RecentClips/2099-01-01_00-00-00-front.mp4", hits)
        == CLOUD_PRIORITY_BULK
    )
    # Hits=None disables hard-brake bump entirely.
    assert (
        _candidate_priority("RecentClips/2026-03-01_12-00-00-front.mp4", None)
        == CLOUD_PRIORITY_BULK
    )


