from __future__ import annotations

import json
import os
import sqlite3
import struct
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from teslausb_web.services.mapping import MappingService, MappingServiceConfig
from teslausb_web.services.mapping.diagnose import (
    _diagnose_nal_structure,
    _diagnose_single_video,
    _gps_count,
    _sample_gps_payload,
    _sample_non_gps_payload,
)
from teslausb_web.services.mapping.discovery import (
    _find_front_camera_videos,
    _iter_archived_with_mtime,
)
from teslausb_web.services.mapping.indexer import (
    _base_datetime,
    _handle_no_gps_clip,
    _messages_for_sample_rate,
    _preflight_result,
    _read_sidecar,
    _row_waypoint_count,
    _should_record_result,
    index_single_file,
)
from teslausb_web.services.mapping.paths import (
    _resolve_recording_time,
    candidate_db_paths,
    canonical_key,
    relative_video_path,
)
from teslausb_web.services.mapping.purge import _has_surviving_copy, purge_deleted_videos
from teslausb_web.services.mapping.sei import (
    FallbackSeiParser,
    FallbackSeiSidecar,
    FallbackTelemetryMessage,
    SeiSidecarProtocol,
    TelemetryMessageProtocol,
    _as_bool,
    _as_float,
    _as_int,
    _as_optional_float,
    _as_optional_str,
    _parse_datetime,
    get_sei_parser,
)
from teslausb_web.services.mapping.sentry import _infer_sentry_event, _read_event_json
from teslausb_web.services.mapping.service import DiagnoseError, IndexOutcome, IndexResult
from teslausb_web.services.mapping.trips import (
    _first_mergeable_pair,
    _merge_all_adjacent_trip_pairs,
    _trip_duration_seconds,
    recompute_trip_stats,
)
from teslausb_web.services.mapping_migrations import MigrationsConfig, MigrationsRunner


@dataclass(frozen=True, slots=True)
class FakeParser:
    messages_by_path: dict[str, tuple[FallbackTelemetryMessage, ...]]
    mvhd_by_path: dict[str, datetime | None]
    sidecar_by_path: dict[str, FallbackSeiSidecar]
    raise_sidecar: bool = False
    raise_mvhd: bool = False
    raise_messages: bool = False

    def read_sei_sidecar(self, video_path: Path) -> SeiSidecarProtocol | None:
        if self.raise_sidecar:
            raise RuntimeError("sidecar boom")
        return cast("SeiSidecarProtocol | None", self.sidecar_by_path.get(str(video_path)))

    def extract_mvhd_creation_time(self, video_path: Path) -> datetime | None:
        if self.raise_mvhd:
            raise RuntimeError("mvhd boom")
        return self.mvhd_by_path.get(str(video_path))

    def extract_sei_messages(
        self,
        video_path: Path,
        *,
        sample_rate: int,
    ) -> tuple[TelemetryMessageProtocol, ...]:
        _ = sample_rate
        if self.raise_messages:
            raise RuntimeError("messages boom")
        return cast(
            "tuple[TelemetryMessageProtocol, ...]",
            self.messages_by_path.get(str(video_path), ()),
        )


@pytest.fixture
def mapping_service(tmp_path: Path) -> MappingService:
    media_root = tmp_path / "TeslaCam"
    archive_root = media_root / "ArchivedClips"
    archive_root.mkdir(parents=True)
    for folder in ("RecentClips", "SavedClips", "SentryClips"):
        (media_root / folder).mkdir(parents=True, exist_ok=True)
    parser = FakeParser(messages_by_path={}, mvhd_by_path={}, sidecar_by_path={})
    return MappingService(
        config=MappingServiceConfig(
            db_path=tmp_path / "state" / "mapping.db",
            backup_dir=tmp_path / "state" / "mapping-backups",
            media_root=media_root,
            archive_root=archive_root,
            index_too_new_seconds=120.0,
        ),
        parser=parser,
    )


def test_open_db_contexts_close_connections(
    tmp_path: Path, mapping_service: MappingService
) -> None:
    runner = MigrationsRunner(
        MigrationsConfig(
            db_path=tmp_path / "state" / "runner.db",
            backup_dir=tmp_path / "state" / "backups",
        )
    )

    with runner.open_db() as runner_connection:
        runner_connection.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        runner_connection.execute("SELECT 1")

    with mapping_service.open_db() as service_connection:
        service_connection.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        service_connection.execute("SELECT 1")


def test_fallback_sei_parser_reads_sidecars_and_coerces_payloads(tmp_path: Path) -> None:
    video = tmp_path / "clip-front.mp4"
    payload = {
        "sample_rate": 15,
        "sei_count": 2,
        "no_gps_count": 1,
        "mvhd_creation_time_utc": "2026-01-02T03:04:05Z",
        "messages": [
            {
                "has_gps": True,
                "timestamp_ms": 123,
                "latitude_deg": 37,
                "longitude_deg": -122.0,
                "heading_deg": 90,
                "vehicle_speed_mps": 10,
                "gear_state": "DRIVE",
                "autopilot_state": "NONE",
                "steering_wheel_angle": 1.5,
                "brake_applied": False,
                "blinker_on_left": True,
                "blinker_on_right": False,
                "frame_index": 4,
            },
            {"has_gps": False, "timestamp_ms": True, "gear_state": "   ", "frame_index": 9},
        ],
    }
    (tmp_path / "clip-front.mp4.json").write_text(json.dumps(payload), encoding="utf-8")

    parser = FallbackSeiParser()
    sidecar = parser.read_sei_sidecar(video)

    assert sidecar is not None
    assert sidecar.sample_rate == 15
    assert sidecar.mvhd_creation_time_utc == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert len(sidecar.messages) == 2
    assert sidecar.messages[0].speed_mph == pytest.approx(22.3694)
    assert sidecar.messages[1].speed_mph == 0.0
    assert parser.extract_mvhd_creation_time(video) == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert parser.extract_sei_messages(video, sample_rate=30) == ()
    assert get_sei_parser(parser) is parser
    assert isinstance(get_sei_parser(), FallbackSeiParser)
    bool_value = True
    assert _parse_datetime("2026-01-02T03:04:05") == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert _parse_datetime("bad") is None
    assert _as_bool("x", default=False) is False
    assert _as_int(bool_value, default=9) == 9
    assert _as_float("x", 1.25) == 1.25
    assert _as_optional_float("x") is None
    assert _as_optional_str("   ") is None


def test_fallback_sei_parser_rejects_non_mapping_json(tmp_path: Path) -> None:
    video = tmp_path / "clip-front.mp4"
    (tmp_path / "clip-front.mp4.sei.json").write_text("[1, 2, 3]", encoding="utf-8")

    parser = FallbackSeiParser()

    assert parser.read_sei_sidecar(video) is None
    assert parser.extract_mvhd_creation_time(video) is None


def test_sentry_helpers_validate_event_json_and_replace_inferred_rows(
    mapping_service: MappingService,
) -> None:
    sentry_dir = mapping_service.config.media_root / "SentryClips" / "2026-01-02_00-00-00"
    sentry_dir.mkdir(parents=True)
    (sentry_dir / "event.json").write_text(
        json.dumps({"est_lat": 37.2, "est_lon": -122.3, "reason": "tap"}),
        encoding="utf-8",
    )
    rel_path = "SentryClips/2026-01-02_00-00-00/2026-01-02_00-00-00-front.mp4"

    payload = _read_event_json(rel_path, mapping_service.config.media_root)

    assert payload is not None
    assert _read_event_json("short.mp4", mapping_service.config.media_root) is None

    with mapping_service.open_db() as connection:
        connection.execute(
            "INSERT INTO detected_events "
            "(id, timestamp, event_type, video_path, metadata) VALUES (?, ?, ?, ?, ?)",
            (
                1,
                "2026-01-02T00:00:00",
                "sentry",
                rel_path,
                '{"location_source":"nearest_waypoint"}',
            ),
        )
        created = _infer_sentry_event(
            connection,
            rel_path,
            "2026-01-02T00:00:00",
            media_root=mapping_service.config.media_root,
        )
        row = connection.execute(
            "SELECT COUNT(*) AS count, description, metadata FROM detected_events"
        ).fetchone()
        skipped = _infer_sentry_event(
            connection,
            rel_path,
            "2026-01-02T00:00:00",
            media_root=mapping_service.config.media_root,
        )

    assert created is True
    assert skipped is False
    assert row is not None
    assert row["count"] == 1
    assert "tap" in str(row["description"])
    assert "event_json" in str(row["metadata"])


@pytest.mark.parametrize(
    "payload",
    [
        {"est_lat": 0.0, "est_lon": 0.0},
        {"est_lat": 91.0, "est_lon": -122.0},
        {"est_lat": 37.0, "est_lon": float("inf")},
        {"est_lat": "37.0", "est_lon": -122.0},
        [1, 2, 3],
    ],
)
def test_sentry_read_event_json_rejects_invalid_payloads(
    mapping_service: MappingService,
    payload: object,
) -> None:
    event_dir = mapping_service.config.media_root / "SavedClips" / "2026-01-03_00-00-00"
    event_dir.mkdir(parents=True)
    (event_dir / "event.json").write_text(json.dumps(payload), encoding="utf-8")

    assert (
        _read_event_json(
            "SavedClips/2026-01-03_00-00-00/2026-01-03_00-00-00-front.mp4",
            mapping_service.config.media_root,
        )
        is None
    )


def test_sentry_infers_saved_event_from_nearest_waypoint(mapping_service: MappingService) -> None:
    rel_path = "SavedClips/2026-01-04_00-00-00/2026-01-04_00-00-00-front.mp4"
    with mapping_service.open_db() as connection:
        connection.execute(
            "INSERT INTO waypoints (timestamp, lat, lon, video_path) VALUES (?, ?, ?, ?)",
            ("2026-01-04T00:00:00", 37.4, -122.4, "RecentClips/nearby-front.mp4"),
        )
        assert _infer_sentry_event(connection, rel_path, None, media_root=None) is False
        created = _infer_sentry_event(
            connection,
            rel_path,
            "2026-01-04T00:00:01",
            media_root=None,
        )
        row = connection.execute(
            "SELECT event_type, description, lat, lon FROM detected_events WHERE video_path = ?",
            (rel_path,),
        ).fetchone()

    assert created is True
    assert row is not None
    assert row["event_type"] == "saved"
    assert row["description"] == "Saved Clip event (location from nearest_waypoint)"
    assert row["lat"] == pytest.approx(37.4)
    assert row["lon"] == pytest.approx(-122.4)


def test_purge_deleted_videos_scans_missing_rows_and_checks_surviving_copies(
    mapping_service: MappingService,
) -> None:
    foldered_live = (
        mapping_service.config.media_root
        / "SavedClips"
        / "2026-01-05_00-00-00"
        / "2026-01-05_00-00-00-front.mp4"
    )
    foldered_live.parent.mkdir(parents=True)
    foldered_live.write_bytes(b"x")
    foldered_archive = (
        mapping_service.config.archive_root
        / "SavedClips"
        / foldered_live.parent.name
        / foldered_live.name
    )
    foldered_archive.parent.mkdir(parents=True)
    foldered_archive.write_bytes(b"x")
    recent_live = (
        mapping_service.config.media_root / "RecentClips" / "2026-01-05_00-00-01-front.mp4"
    )
    recent_live.write_bytes(b"x")
    flat_archive = mapping_service.config.archive_root / recent_live.name
    flat_archive.write_bytes(b"x")
    missing = mapping_service.config.media_root / "RecentClips" / "2026-01-05_00-00-02-front.mp4"

    with mapping_service.open_db() as connection:
        connection.execute(
            "INSERT INTO indexed_files "
            "(file_path, indexed_at, waypoint_count, event_count) VALUES (?, ?, ?, ?)",
            (str(missing), "2026-01-05T00:10:00", 1, 1),
        )
        connection.execute(
            "INSERT INTO waypoints (timestamp, lat, lon, video_path) VALUES (?, ?, ?, ?)",
            ("2026-01-05T00:00:00", 37.0, -122.0, "RecentClips/2026-01-05_00-00-02-front.mp4"),
        )
        connection.execute(
            "INSERT INTO detected_events (timestamp, event_type, video_path) VALUES (?, ?, ?)",
            ("2026-01-05T00:00:00", "saved", "RecentClips/2026-01-05_00-00-02-front.mp4"),
        )
        connection.commit()

    result = purge_deleted_videos(mapping_service)

    assert _has_surviving_copy(mapping_service, foldered_archive) is True
    assert _has_surviving_copy(mapping_service, flat_archive) is True
    assert result == {
        "purged_files": 1,
        "purged_waypoints": 1,
        "purged_events": 1,
        "purged_trips": 0,
    }
    with mapping_service.open_db() as connection:
        row = connection.execute(
            "SELECT video_path FROM waypoints ORDER BY id DESC LIMIT 1"
        ).fetchone()
        event_row = connection.execute(
            "SELECT video_path FROM detected_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        indexed = connection.execute("SELECT COUNT(*) AS count FROM indexed_files").fetchone()

    assert row is not None
    assert row["video_path"] is None
    assert event_row is not None
    assert event_row["video_path"] is None
    assert indexed is not None
    assert indexed["count"] == 0


def test_discovery_walks_nested_archives_and_skips_stat_failures(
    mapping_service: MappingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archived = (
        mapping_service.config.archive_root
        / "SavedClips"
        / "2026-01-06_00-00-00"
        / "2026-01-06_00-00-00-front.mp4"
    )
    sentry = (
        mapping_service.config.media_root
        / "SentryClips"
        / "2026-01-06_00-00-00"
        / "2026-01-06_00-00-00-front.mp4"
    )
    archived.parent.mkdir(parents=True)
    sentry.parent.mkdir(parents=True)
    _write_sample_mp4(archived)
    _write_sample_mp4(sentry)

    discovered = list(
        _find_front_camera_videos(
            mapping_service.config.media_root, mapping_service.config.archive_root
        )
    )
    assert list(_find_front_camera_videos(Path("missing"), Path("missing") / "ArchivedClips")) == []
    original_stat = Path.stat

    def fake_stat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        if path == archived:
            raise OSError("boom")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(
        "teslausb_web.services.mapping.discovery._find_archived_videos", lambda _: iter((archived,))
    )
    monkeypatch.setattr(Path, "stat", fake_stat)
    archived_with_mtime = list(_iter_archived_with_mtime(mapping_service.config.archive_root))

    assert archived in discovered
    assert sentry in discovered
    assert archived_with_mtime == []


def test_paths_helpers_warn_on_clock_skew_and_resolve_relative_paths(
    mapping_service: MappingService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    video = mapping_service.config.media_root / "RecentClips" / "2026-01-07_00-00-00-front.mp4"
    archive_video = mapping_service.config.archive_root / video.name
    other_video = mapping_service.config.media_root.parent / "loose-front.mp4"
    parser = FakeParser(
        messages_by_path={},
        mvhd_by_path={str(video): datetime(2026, 1, 7, 0, 20, 0, tzinfo=UTC)},
        sidecar_by_path={},
        raise_sidecar=True,
    )

    caplog.set_level("WARNING", logger="teslausb_web.services.mapping.paths")
    resolved = _resolve_recording_time(video, parser=parser, sidecar=None)
    fallback = _resolve_recording_time(
        mapping_service.config.media_root / "RecentClips" / "2026-01-07_00-00-01-front.mp4",
        parser=FakeParser(
            messages_by_path={},
            mvhd_by_path={},
            sidecar_by_path={},
            raise_sidecar=True,
            raise_mvhd=True,
        ),
        sidecar=None,
    )

    assert resolved is not None
    assert "Tesla onboard-clock skew detected" in caplog.text
    assert fallback == "2026-01-07T00:00:01+00:00"
    assert canonical_key("C:\\TeslaCam\\SavedClips\\2026-01-07_00-00-00\\clip-front.mp4") == (
        "SavedClips/2026-01-07_00-00-00/clip-front.mp4"
    )
    assert candidate_db_paths("SavedClips/2026-01-07_00-00-00/clip-front.mp4") == (
        "SavedClips/2026-01-07_00-00-00/clip-front.mp4",
    )
    assert (
        relative_video_path(
            video,
            media_root=mapping_service.config.media_root,
            archive_root=mapping_service.config.archive_root,
            archived_clips_dirname=mapping_service.config.archived_clips_dirname,
        )
        == "RecentClips/2026-01-07_00-00-00-front.mp4"
    )
    assert (
        relative_video_path(
            archive_video,
            media_root=mapping_service.config.media_root,
            archive_root=mapping_service.config.archive_root,
            archived_clips_dirname=mapping_service.config.archived_clips_dirname,
        )
        == "ArchivedClips/2026-01-07_00-00-00-front.mp4"
    )
    assert (
        relative_video_path(
            other_video,
            media_root=mapping_service.config.media_root,
            archive_root=mapping_service.config.archive_root,
            archived_clips_dirname=mapping_service.config.archived_clips_dirname,
        )
        == "loose-front.mp4"
    )


def test_diagnose_helpers_cover_small_invalid_non_gps_and_large_files(
    mapping_service: MappingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = mapping_service.config.media_root / "DoesNotExist"
    tiny = mapping_service.config.media_root / "RecentClips" / "tiny-front.mp4"
    invalid = mapping_service.config.media_root / "RecentClips" / "invalid-front.mp4"
    valid = mapping_service.config.media_root / "RecentClips" / "2026-01-08_00-00-00-front.mp4"
    huge = mapping_service.config.media_root / "RecentClips" / "huge-front.mp4"
    tiny.write_bytes(b"1234")
    invalid.write_bytes(b"not-an-mp4")
    _write_sample_mp4(valid)
    huge.write_bytes(b"12345678")
    parser = FakeParser(
        messages_by_path={str(valid): (_message(0, lat=0.0, lon=0.0, has_gps=False),)},
        mvhd_by_path={},
        sidecar_by_path={},
    )
    original_stat = Path.stat

    def fake_stat(path: Path, *args: object, **kwargs: object) -> os.stat_result | SimpleNamespace:
        if path == huge:
            return SimpleNamespace(st_size=151 * 1024 * 1024)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)

    assert mapping_service.diagnose_video(teslacam_path=missing)["summary"].startswith(
        "TeslaCam path does not exist"
    )
    assert _diagnose_single_video(tiny, parser)["error"] == "File too small"
    assert (
        _diagnose_single_video(invalid, parser)["error"]
        == "Not a valid MP4 (no ftyp box in first 12 bytes)"
    )
    assert _diagnose_single_video(valid, parser)["sample_sei_no_gps"] == {
        "lat": 0.0,
        "lon": 0.0,
        "speed_mph": 22.4,
        "frame": 0,
    }
    assert _diagnose_nal_structure(invalid) == {"nal_error": "No mdat box found"}
    with pytest.raises(DiagnoseError, match="File too large"):
        _diagnose_nal_structure(huge)
    assert _sample_gps_payload(_message(0, lat=37.5, lon=-122.5)) == {
        "lat": 37.5,
        "lon": -122.5,
        "speed_mph": 22.4,
        "heading": 90.0,
        "gear": "DRIVE",
    }
    assert _sample_non_gps_payload(_message(500, lat=0.0, lon=0.0, has_gps=False))["frame"] == 0
    assert _gps_count({"gps_messages": "bad"}) == 0


def test_trip_helpers_merge_pairs_and_recompute_stats(mapping_service: MappingService) -> None:
    with mapping_service.open_db() as connection:
        connection.executemany(
            "INSERT INTO trips "
            "(id, start_time, end_time, source_folder, indexed_at) VALUES (?, ?, ?, ?, ?)",
            (
                (
                    1,
                    "2026-01-09T00:00:00",
                    "2026-01-09T00:00:01",
                    "RecentClips",
                    "2026-01-09T00:05:00",
                ),
                (
                    2,
                    "2026-01-09T00:00:02",
                    "2026-01-09T00:00:03",
                    "RecentClips",
                    "2026-01-09T00:05:00",
                ),
            ),
        )
        connection.executemany(
            "INSERT INTO waypoints "
            "(trip_id, timestamp, lat, lon, video_path) VALUES (?, ?, ?, ?, ?)",
            (
                (1, "2026-01-09T00:00:00", 37.0, -122.0, "RecentClips/drive-front.mp4"),
                (1, "2026-01-09T00:00:01", 37.0001, -122.0001, "RecentClips/drive-front.mp4"),
                (2, "2026-01-09T00:00:02", 37.0002, -122.0002, "RecentClips/drive-front.mp4"),
            ),
        )
        connection.execute(
            "INSERT INTO detected_events "
            "(trip_id, timestamp, event_type, video_path) VALUES (?, ?, ?, ?)",
            (2, "2026-01-09T00:00:02", "saved", "RecentClips/drive-front.mp4"),
        )

        assert _first_mergeable_pair(connection, 300.0) == (1, 2)
        assert _merge_all_adjacent_trip_pairs(connection, 300.0) == 1
        recompute_trip_stats(connection, 1)
        row = connection.execute(
            "SELECT end_time, distance_km, duration_seconds FROM trips WHERE id = 1"
        ).fetchone()
        event_row = connection.execute("SELECT trip_id FROM detected_events").fetchone()

    assert row is not None
    assert row["end_time"] == "2026-01-09T00:00:02"
    assert float(row["distance_km"]) > 0.0
    assert row["duration_seconds"] == 2
    assert event_row is not None
    assert event_row["trip_id"] == 1
    assert _trip_duration_seconds("bad", "still-bad") == 0
    assert _trip_duration_seconds("2026-01-09T00:01:00", "2026-01-09T00:00:00") == 0


def test_indexer_helpers_cover_preflight_and_error_paths(
    mapping_service: MappingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    non_front = (
        mapping_service.config.media_root / "RecentClips" / "2026-01-10_00-00-00-left_repeater.mp4"
    )
    missing = mapping_service.config.media_root / "RecentClips" / "2026-01-10_00-00-01-front.mp4"
    too_new = mapping_service.config.media_root / "RecentClips" / "2026-01-10_00-00-02-front.mp4"
    video = mapping_service.config.media_root / "RecentClips" / "2026-01-10_00-00-03-front.mp4"
    _write_sample_mp4(non_front)
    _write_sample_mp4(too_new, old_enough=False)
    _write_sample_mp4(video)
    parser = FakeParser(
        messages_by_path={str(video): (_message(0, lat=37.0, lon=-122.0),)},
        mvhd_by_path={},
        sidecar_by_path={
            str(video): FallbackSeiSidecar(30, 1, 0, None, (_message(0, lat=37.0, lon=-122.0),))
        },
        raise_sidecar=True,
    )
    mapping_service._parser = parser
    in_memory = sqlite3.connect(":memory:")
    in_memory.row_factory = sqlite3.Row
    row = in_memory.execute("SELECT 'abc' AS waypoint_count").fetchone()
    in_memory.close()

    assert _preflight_result(mapping_service, non_front) == IndexResult(
        IndexOutcome.NOT_FRONT_CAMERA
    )
    assert _preflight_result(mapping_service, missing) == IndexResult(IndexOutcome.FILE_MISSING)
    assert _preflight_result(mapping_service, too_new) == IndexResult(IndexOutcome.TOO_NEW)
    assert _read_sidecar(parser, video) is None
    assert (
        _messages_for_sample_rate(
            parser, video, sample_rate=30, sidecar=parser.sidecar_by_path[str(video)]
        )
        == parser.sidecar_by_path[str(video)].messages
    )
    assert _base_datetime("bad") is None
    assert row is not None
    assert _row_waypoint_count(row) == 0
    assert (
        _should_record_result(
            IndexResult(IndexOutcome.NO_GPS_RECORDED), SimpleNamespace(st_mtime=0.0)
        )
        is True
    )
    assert (
        _should_record_result(IndexResult(IndexOutcome.NO_GPS_RECORDED), SimpleNamespace()) is False
    )

    with mapping_service.open_db() as connection:
        assert _handle_no_gps_clip(
            connection,
            "RecentClips/2026-01-10_00-00-04-front.mp4",
            "2026-01-10T00:00:04",
            mapping_service,
        ) == IndexResult(IndexOutcome.NO_GPS_RECORDED)

    @contextmanager
    def busy_open_db() -> object:
        raise sqlite3.OperationalError("database is locked")
        yield None

    @contextmanager
    def broken_open_db() -> object:
        raise sqlite3.DatabaseError("boom")
        yield None

    monkeypatch.setattr(mapping_service, "open_db", busy_open_db)
    assert index_single_file(mapping_service, video).outcome == IndexOutcome.DB_BUSY
    monkeypatch.setattr(mapping_service, "open_db", broken_open_db)
    assert index_single_file(mapping_service, video).outcome == IndexOutcome.PARSE_ERROR


def _message(
    timestamp_ms: int,
    *,
    lat: float,
    lon: float,
    speed: float = 10.0,
    has_gps: bool = True,
) -> FallbackTelemetryMessage:
    return FallbackTelemetryMessage(
        has_gps=has_gps,
        timestamp_ms=timestamp_ms,
        latitude_deg=lat,
        longitude_deg=lon,
        heading_deg=90.0,
        vehicle_speed_mps=speed,
        linear_acceleration_x=0.0,
        linear_acceleration_y=0.0,
        linear_acceleration_z=0.0,
        gear_state="DRIVE",
        autopilot_state="NONE",
        steering_wheel_angle=0.0,
        brake_applied=False,
        blinker_on_left=False,
        blinker_on_right=False,
        frame_index=timestamp_ms // 1000,
    )


def _write_sample_mp4(path: Path, *, old_enough: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ftyp = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
    mdat_payload = b"\x00\x00\x00\x02\x06\xff\x00\x00\x00\x02\x05\xaa"
    mdat_size = 8 + len(mdat_payload)
    path.write_bytes(ftyp + struct.pack(">I", mdat_size) + b"mdat" + mdat_payload)
    if old_enough:
        old_mtime = datetime.now(tz=UTC).timestamp() - 3600
        os.utime(path, (old_mtime, old_mtime))
