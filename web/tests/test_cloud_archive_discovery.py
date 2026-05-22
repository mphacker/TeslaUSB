from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from teslausb_web.services.cloud_archive.discovery import _discover_events, _score_event_priority
from teslausb_web.services.cloud_archive.settings import (
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
