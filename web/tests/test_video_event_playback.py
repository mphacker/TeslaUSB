"""Regression tests for the maps/clips event play/locate + honk-seek fixes.

Covers three operator-reported bugs:

* Bug #1 — event cards must carry finite ``lat``/``lon`` so the UI can
  render a separate locate button (serializer must emit them).
* Bug #2 — the honk/event moment in ``event.json`` is authoritative over
  the (later) enclosing folder name, and playback must open the clip that
  actually contains the moment and seek into it.

All pure-function level; no Flask involved.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from teslausb_web.blueprints.videos import _serialize_event_summary
from teslausb_web.services.video_service import event_playback_target
from teslausb_web.services.video_service._filesystem import (
    _parse_latlon,
    _resolve_event_timestamp,
)
from teslausb_web.services.video_service._models import CameraVideos, EventSummary

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Bug #2 — event.json timestamp is authoritative over the folder name.
# ---------------------------------------------------------------------------


def test_resolve_event_timestamp_prefers_event_json() -> None:
    # Folder name records when Tesla *closed* the event (18s later).
    folder_name = "2026-06-01_20-10-53"
    metadata: dict[str, object] = {"timestamp": "2026-06-01T20:10:35"}
    resolved = _resolve_event_timestamp(metadata, folder_name, fallback_mtime=0.0)
    # Authoritative moment is 20:10:35, NOT the 20:10:53 folder name.
    assert resolved == _resolve_event_timestamp(
        {"timestamp": "2026-06-01T20:10:35"}, "irrelevant", 0.0
    )
    from datetime import UTC, datetime

    expected = datetime(2026, 6, 1, 20, 10, 35, tzinfo=UTC).timestamp()
    assert resolved == expected


def test_resolve_event_timestamp_falls_back_to_folder_name() -> None:
    from datetime import UTC, datetime

    resolved = _resolve_event_timestamp({}, "2026-06-01_20-10-53", fallback_mtime=0.0)
    expected = datetime(2026, 6, 1, 20, 10, 53, tzinfo=UTC).timestamp()
    assert resolved == expected


def test_resolve_event_timestamp_falls_back_to_mtime_when_unparseable() -> None:
    resolved = _resolve_event_timestamp({}, "not-a-timestamp", fallback_mtime=1234.5)
    assert resolved == 1234.5


# ---------------------------------------------------------------------------
# Bug #1 — est_lat/est_lon parsing (Tesla writes them as strings).
# ---------------------------------------------------------------------------


def test_parse_latlon_accepts_string_coordinates() -> None:
    lat, lon = _parse_latlon({"est_lat": "42.5414", "est_lon": "-83.1234"})
    assert lat == 42.5414
    assert lon == -83.1234


def test_parse_latlon_rejects_null_island() -> None:
    assert _parse_latlon({"est_lat": "0", "est_lon": "0"}) == (None, None)


def test_parse_latlon_rejects_missing_or_malformed() -> None:
    assert _parse_latlon({}) == (None, None)
    assert _parse_latlon({"est_lat": "abc", "est_lon": "1.0"}) == (None, None)
    assert _parse_latlon({"est_lat": "nan", "est_lon": "1.0"}) == (None, None)


# ---------------------------------------------------------------------------
# Bug #2 — event_playback_target picks the clip containing the moment.
# ---------------------------------------------------------------------------


def _write_event_json(event_dir: Path, timestamp: str) -> None:
    (event_dir / "event.json").write_text(
        json.dumps({"timestamp": timestamp}), encoding="utf-8"
    )


def test_event_playback_target_seeks_into_last_clip_for_honk(tmp_path: Path) -> None:
    event_dir = tmp_path / "2026-06-01_20-10-53"
    event_dir.mkdir()
    # Honk lands ~31s into the last (third) minute clip.
    _write_event_json(event_dir, "2026-06-01T20:10:35")
    front_clips = [
        "2026-06-01_20-08-00-front.mp4",
        "2026-06-01_20-09-00-front.mp4",
        "2026-06-01_20-10-04-front.mp4",
    ]
    index, seek = event_playback_target(event_dir, event_dir.name, front_clips)
    assert index == 2
    assert seek == 31.0


def test_event_playback_target_fails_safe_without_event_json(tmp_path: Path) -> None:
    event_dir = tmp_path / "2026-06-01_20-10-53"
    event_dir.mkdir()
    front_clips = ["2026-06-01_20-10-53-front.mp4"]
    # No event.json — folder name parses, clip starts at the same second.
    index, seek = event_playback_target(event_dir, event_dir.name, front_clips)
    assert index == 0
    assert seek == 0.0


def test_event_playback_target_empty_clip_list(tmp_path: Path) -> None:
    event_dir = tmp_path / "2026-06-01_20-10-53"
    event_dir.mkdir()
    assert event_playback_target(event_dir, event_dir.name, []) == (0, 0.0)


def test_event_playback_target_rejects_implausible_seek(tmp_path: Path) -> None:
    event_dir = tmp_path / "2026-06-01_20-10-53"
    event_dir.mkdir()
    # Event 10 minutes after the only clip start -> seek exceeds the cap.
    _write_event_json(event_dir, "2026-06-01T20:20:00")
    front_clips = ["2026-06-01_20-10-00-front.mp4"]
    index, seek = event_playback_target(event_dir, event_dir.name, front_clips)
    assert index == 0
    assert seek == 0.0


# ---------------------------------------------------------------------------
# Bug #1 — serializer emits lat/lon for the card locate button.
# ---------------------------------------------------------------------------


def _event_summary(*, lat: float | None, lon: float | None) -> EventSummary:
    return EventSummary(
        name="2026-06-01_20-10-53",
        timestamp=0.0,
        datetime_str="2026-06-01 20:10:53",
        size_mb=12.5,
        camera_videos=CameraVideos(front="2026-06-01_20-10-53-front.mp4"),
        lat=lat,
        lon=lon,
    )


def test_serialize_event_summary_emits_latlon() -> None:
    out = _serialize_event_summary(_event_summary(lat=42.5414, lon=-83.1234))
    assert out["lat"] == 42.5414
    assert out["lon"] == -83.1234


def test_serialize_event_summary_omits_latlon_when_absent() -> None:
    out = _serialize_event_summary(_event_summary(lat=None, lon=None))
    assert "lat" not in out
    assert "lon" not in out
