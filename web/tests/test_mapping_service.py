from __future__ import annotations

import json
import os
import sqlite3
import struct
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import time
from typing import cast

import pytest
from teslausb_web.app import create_app
from teslausb_web.config import MappingSection, PathsSection, WebConfig, WebSection
from teslausb_web.services.mapping import (
    MappingService,
    MappingServiceConfig,
    make_mapping_service,
)
from teslausb_web.services.mapping.diagnose import _diagnose_nal_structure
from teslausb_web.services.mapping.discovery import (
    _find_archived_videos,
    _find_front_camera_videos,
    _iter_archived_with_mtime,
)
from teslausb_web.services.mapping.events import (
    WaypointSample,
    _debounce_events,
    _detect_events,
)
from teslausb_web.services.mapping.kv import _kv_get, _kv_set
from teslausb_web.services.mapping.paths import (
    _resolve_recording_time,
    _timestamp_from_filename,
    candidate_db_paths,
    canonical_key,
)
from teslausb_web.services.mapping.retry import _is_transient_db_error, _with_db_retry
from teslausb_web.services.mapping.sei import (
    FallbackSeiSidecar,
    FallbackTelemetryMessage,
    SeiSidecarProtocol,
    TelemetryMessageProtocol,
)
from teslausb_web.services.mapping.sentry import _infer_sentry_event, _read_event_json
from teslausb_web.services.mapping.stale_scan import _initial_stale_scan_delay
from teslausb_web.services.mapping.trips import _merge_adjacent_trips_for


@dataclass(frozen=True, slots=True)
class FakeParser:
    messages_by_path: dict[str, tuple[FallbackTelemetryMessage, ...]]
    mvhd_by_path: dict[str, datetime | None]
    sidecar_by_path: dict[str, FallbackSeiSidecar]

    def read_sei_sidecar(self, video_path: Path) -> SeiSidecarProtocol | None:
        return cast("SeiSidecarProtocol | None", self.sidecar_by_path.get(str(video_path)))

    def extract_mvhd_creation_time(self, video_path: Path) -> datetime | None:
        return self.mvhd_by_path.get(str(video_path))

    def extract_sei_messages(
        self,
        video_path: Path,
        *,
        sample_rate: int,
    ) -> tuple[TelemetryMessageProtocol, ...]:
        _ = sample_rate
        return cast(
            "tuple[TelemetryMessageProtocol, ...]",
            self.messages_by_path.get(str(video_path), ()),
        )


@pytest.fixture
def service_fixture(tmp_path: Path) -> MappingService:
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
            stale_scan_interval_seconds=0.05,
            stale_scan_jitter_seconds=0.01,
            initial_stale_scan_base_seconds=0.01,
            initial_stale_scan_jitter_seconds=0.01,
            stale_scan_debounce_seconds=0.05,
        ),
        parser=parser,
    )


def test_make_mapping_service_from_web_config(tmp_path: Path) -> None:
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32),
        paths=PathsSection(backing_root=tmp_path / "backing", state_dir=tmp_path / "state"),
        mapping=MappingSection(
            db_path=tmp_path / "state" / "mapping.db",
            backup_dir=tmp_path / "state" / "mapping-backups",
            media_root=tmp_path / "media",
            archive_root=tmp_path / "media" / "ArchivedClips",
            sample_rate=15,
        ),
    )

    service = make_mapping_service(cfg)

    assert isinstance(service, MappingService)
    assert service.config.sample_rate == 15
    assert service.config.media_root == tmp_path / "media"


def test_app_factory_registers_mapping_service(tmp_path: Path) -> None:
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32),
        paths=PathsSection(backing_root=tmp_path / "backing", state_dir=tmp_path / "state"),
        mapping=MappingSection(
            db_path=tmp_path / "state" / "mapping.db",
            backup_dir=tmp_path / "state" / "mapping-backups",
            media_root=tmp_path / "backing",
            archive_root=tmp_path / "backing" / "ArchivedClips",
        ),
    )

    app = create_app(cfg)

    assert isinstance(app.extensions["mapping_service"], MappingService)
    assert app.extensions["mapping_service_finalizer"].alive is True


def test_path_helpers_round_trip_timestamp_and_keys(service_fixture: MappingService) -> None:
    assert _timestamp_from_filename("2026-01-02_03-04-05-front.mp4") == "2026-01-02T03:04:05+00:00"
    assert _timestamp_from_filename("bad-name.mp4") is None
    assert canonical_key("SavedClips/2026-01-02_03-04-05/clip-front.mp4") == (
        "SavedClips/2026-01-02_03-04-05/clip-front.mp4"
    )
    assert candidate_db_paths("clip-front.mp4") == (
        "clip-front.mp4",
        "RecentClips/clip-front.mp4",
        "ArchivedClips/clip-front.mp4",
    )

    parser = FakeParser(
        messages_by_path={},
        mvhd_by_path={},
        sidecar_by_path={
            "video.mp4": FallbackSeiSidecar(
                sample_rate=30,
                sei_count=0,
                no_gps_count=0,
                mvhd_creation_time_utc=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
                messages=(),
            )
        },
    )
    resolved = _resolve_recording_time(Path("video.mp4"), parser=parser, sidecar=None)
    assert resolved is not None


def test_event_detection_and_debounce() -> None:
    waypoints = (
        WaypointSample(
            timestamp="2026-01-01T00:00:00",
            lat=37.0,
            lon=-122.0,
            speed_mps=40.0,
            acceleration_x=-8.0,
            autopilot_state="SELF_DRIVING",
            video_path="RecentClips/a-front.mp4",
        ),
        WaypointSample(
            timestamp="2026-01-01T00:00:01",
            lat=37.0,
            lon=-122.0,
            speed_mps=12.0,
            acceleration_y=5.0,
            autopilot_state="NONE",
            video_path="RecentClips/a-front.mp4",
        ),
    )
    detected = _detect_events(
        waypoints,
        {
            "harsh_brake_threshold": -4.0,
            "emergency_brake_threshold": -7.0,
            "hard_accel_threshold": 3.5,
            "sharp_turn_lateral_mps2": 4.0,
            "speed_limit_mps": 35.76,
        },
        "RecentClips/a-front.mp4",
    )
    types = {event.event_type for event in detected}
    assert {"emergency_brake", "speeding", "sharp_turn", "fsd_disengage"} <= types

    debounced = _debounce_events((detected[0], detected[0]), window_seconds=5.0)
    assert len(debounced) == 1


def test_discovery_prefers_archive_over_recent(service_fixture: MappingService) -> None:
    archive_file = service_fixture.config.archive_root / "2026-01-02_00-00-00-front.mp4"
    recent_file = service_fixture.config.media_root / "RecentClips" / archive_file.name
    saved_file = (
        service_fixture.config.media_root
        / "SavedClips"
        / "2026-01-02_00-00-00"
        / "2026-01-02_00-00-00-front.mp4"
    )
    saved_file.parent.mkdir(parents=True)
    for path in (archive_file, recent_file, saved_file):
        _write_sample_mp4(path)

    discovered = list(
        _find_front_camera_videos(
            service_fixture.config.media_root, service_fixture.config.archive_root
        )
    )

    assert archive_file in discovered
    assert saved_file in discovered
    assert recent_file not in discovered
    assert list(_find_archived_videos(service_fixture.config.archive_root)) == [archive_file]
    assert len(list(_iter_archived_with_mtime(service_fixture.config.archive_root))) == 1


def test_read_event_json_and_infer_sentry_event(service_fixture: MappingService) -> None:
    event_dir = service_fixture.config.media_root / "SentryClips" / "2026-01-02_00-00-00"
    event_dir.mkdir(parents=True)
    (event_dir / "event.json").write_text(
        json.dumps({"est_lat": 37.1, "est_lon": -122.2, "reason": "honk"}),
        encoding="utf-8",
    )

    payload = _read_event_json(
        "SentryClips/2026-01-02_00-00-00/2026-01-02_00-00-00-front.mp4",
        service_fixture.config.media_root,
    )

    assert payload is not None
    with service_fixture.get_db_connection() as connection:
        created = _infer_sentry_event(
            connection,
            "SentryClips/2026-01-02_00-00-00/2026-01-02_00-00-00-front.mp4",
            "2026-01-02T00:00:00",
            media_root=service_fixture.config.media_root,
        )
        connection.commit()
        row = connection.execute("SELECT event_type, lat, lon FROM detected_events").fetchone()

    assert created is True
    assert row is not None
    assert row["event_type"] == "sentry"
    assert row["lat"] == pytest.approx(37.1)


def test_index_single_file_indexes_waypoints_and_events(service_fixture: MappingService) -> None:
    video = service_fixture.config.media_root / "RecentClips" / "2026-01-02_00-00-00-front.mp4"
    _write_sample_mp4(video)
    service_fixture._parser = FakeParser(
        messages_by_path={
            str(video): (
                _message(0, lat=37.0, lon=-122.0, speed=20.0, accel_x=0.0, autopilot="NONE"),
                _message(
                    1000, lat=37.0001, lon=-122.0001, speed=40.0, accel_x=-8.0, autopilot="NONE"
                ),
            )
        },
        mvhd_by_path={str(video): datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC)},
        sidecar_by_path={},
    )

    result = service_fixture.index_single_file(video)
    stats = service_fixture.get_stats()

    assert result.outcome.value == "indexed"
    assert result.waypoints == 2
    assert result.events >= 1
    assert stats.trip_count == 1
    assert stats.indexer_status is not None


def test_index_single_file_upgrades_recent_to_archived_path(
    service_fixture: MappingService,
) -> None:
    recent = service_fixture.config.media_root / "RecentClips" / "2026-01-02_00-00-00-front.mp4"
    archived = service_fixture.config.archive_root / recent.name
    _write_sample_mp4(recent)
    _write_sample_mp4(archived)
    messages = (_message(0, lat=37.0, lon=-122.0, speed=20.0, accel_x=0.0, autopilot="NONE"),)
    service_fixture._parser = FakeParser(
        messages_by_path={str(recent): messages, str(archived): messages},
        mvhd_by_path={
            str(recent): datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC),
            str(archived): datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC),
        },
        sidecar_by_path={},
    )

    first = service_fixture.index_single_file(recent)
    second = service_fixture.index_single_file(archived)
    with service_fixture.get_db_connection() as connection:
        row = connection.execute("SELECT DISTINCT video_path FROM waypoints").fetchone()

    assert first.outcome.value == "indexed"
    assert second.outcome.value == "duplicate_upgraded"
    assert row is not None
    assert row["video_path"] == "ArchivedClips/2026-01-02_00-00-00-front.mp4"


def test_index_single_file_creates_sentry_event_when_no_gps(
    service_fixture: MappingService,
) -> None:
    video = (
        service_fixture.config.media_root
        / "SavedClips"
        / "2026-01-02_00-00-00"
        / "2026-01-02_00-00-00-front.mp4"
    )
    video.parent.mkdir(parents=True)
    _write_sample_mp4(video)
    (video.parent / "event.json").write_text(
        json.dumps({"est_lat": 37.2, "est_lon": -122.3, "reason": "tap"}),
        encoding="utf-8",
    )
    service_fixture._parser = FakeParser(
        messages_by_path={str(video): (_message(0, lat=0.0, lon=0.0, has_gps=False),)},
        mvhd_by_path={str(video): datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC)},
        sidecar_by_path={},
    )

    result = service_fixture.index_single_file(video)

    assert result.outcome.value == "indexed"
    assert result.events == 1


def test_purge_deleted_videos_nulls_paths_but_keeps_trip(service_fixture: MappingService) -> None:
    video = service_fixture.config.media_root / "RecentClips" / "2026-01-02_00-00-00-front.mp4"
    _write_sample_mp4(video)
    service_fixture._parser = FakeParser(
        messages_by_path={str(video): (_message(0, lat=37.0, lon=-122.0),)},
        mvhd_by_path={str(video): datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC)},
        sidecar_by_path={},
    )
    assert service_fixture.index_single_file(video).outcome.value == "indexed"
    video.unlink()

    result = service_fixture.purge_deleted_videos(deleted_paths=(video,))
    with service_fixture.get_db_connection() as connection:
        trip_count = connection.execute("SELECT COUNT(*) AS count FROM trips").fetchone()["count"]
        row = connection.execute("SELECT video_path FROM waypoints").fetchone()

    assert result["purged_files"] == 1
    assert trip_count == 1
    assert row is not None
    assert row["video_path"] is None


def test_boot_catchup_scan_enqueues_new_archived_files(service_fixture: MappingService) -> None:
    archived = service_fixture.config.archive_root / "2026-01-02_00-00-00-front.mp4"
    _write_sample_mp4(archived)

    first = service_fixture.boot_catchup_scan()
    second = service_fixture.boot_catchup_scan()
    with service_fixture.get_db_connection() as connection:
        row = connection.execute("SELECT COUNT(*) AS count FROM indexing_queue").fetchone()

    assert first["enqueued"] == 1
    assert second["skipped_by_watermark"] >= 1
    assert row is not None
    assert row["count"] == 1


def test_retry_and_kv_helpers() -> None:
    attempts = 0

    @_with_db_retry
    def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE kv_meta (key TEXT PRIMARY KEY, value TEXT)")
    _kv_set(connection, "status", "ready")

    assert _is_transient_db_error(sqlite3.OperationalError("disk i/o error")) is True
    assert flaky() == "ok"
    assert _kv_get(connection, "status") == "ready"


def test_trip_merge_and_recompute_stats(service_fixture: MappingService) -> None:
    with service_fixture.get_db_connection() as connection:
        connection.execute(
            "INSERT INTO trips (id, start_time, end_time, source_folder, indexed_at) "
            "VALUES (1, '2026-01-01T00:00:00', '2026-01-01T00:00:01', "
            "'RecentClips', '2026-01-01T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO trips (id, start_time, end_time, source_folder, indexed_at) "
            "VALUES (2, '2026-01-01T00:00:02', '2026-01-01T00:00:03', "
            "'RecentClips', '2026-01-01T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO waypoints (trip_id, timestamp, lat, lon, video_path) "
            "VALUES (1, '2026-01-01T00:00:00', 37.0, -122.0, 'RecentClips/a-front.mp4')"
        )
        connection.execute(
            "INSERT INTO waypoints (trip_id, timestamp, lat, lon, video_path) "
            "VALUES (2, '2026-01-01T00:00:02', 37.0001, -122.0001, 'RecentClips/a-front.mp4')"
        )
        merged = _merge_adjacent_trips_for(connection, 2, 300.0)
        connection.commit()
        trip_count = connection.execute("SELECT COUNT(*) AS count FROM trips").fetchone()["count"]

    assert merged == 1
    assert trip_count == 1


def test_stale_scan_trigger_and_thread_shutdown(
    service_fixture: MappingService, monkeypatch: pytest.MonkeyPatch
) -> None:
    fired = threading.Event()

    def fake_scan(service: MappingService, *, source: str) -> dict[str, int]:
        _ = source
        fired.set()
        return {"purged_files": 0, "purged_waypoints": 0, "purged_events": 0, "purged_trips": 0}

    monkeypatch.setattr(
        "teslausb_web.services.mapping.stale_scan._run_stale_scan_blocking", fake_scan
    )
    monkeypatch.setattr(
        "teslausb_web.services.mapping.stale_scan._initial_stale_scan_delay",
        lambda _service: 0.0,
    )

    immediate = service_fixture.trigger_stale_scan_now(source="manual", debounce_seconds=10.0)
    assert immediate["status"] == "fired"
    assert fired.wait(timeout=1.0)
    debounced = service_fixture.trigger_stale_scan_now(source="manual", debounce_seconds=10.0)
    assert debounced["status"] == "debounced"
    assert service_fixture.start_daily_stale_scan() is True
    assert service_fixture.stop_daily_stale_scan(timeout=1.0) is True
    assert service_fixture.shutdown(timeout=1.0) is True


def test_initial_stale_scan_delay_uses_configured_window(service_fixture: MappingService) -> None:
    delay = _initial_stale_scan_delay(service_fixture)
    assert service_fixture.config.initial_stale_scan_base_seconds <= delay
    assert delay <= (
        service_fixture.config.initial_stale_scan_base_seconds
        + service_fixture.config.initial_stale_scan_jitter_seconds
    )


def test_diagnose_video_and_nal_structure(service_fixture: MappingService) -> None:
    video = service_fixture.config.media_root / "RecentClips" / "2026-01-02_00-00-00-front.mp4"
    _write_sample_mp4(video)
    service_fixture._parser = FakeParser(
        messages_by_path={str(video): (_message(0, lat=37.0, lon=-122.0),)},
        mvhd_by_path={},
        sidecar_by_path={},
    )

    nal = _diagnose_nal_structure(video)
    diag = service_fixture.diagnose_video(max_videos=1)

    sei_count = nal["sei_type6_count"]
    total_videos = diag["total_front_videos"]

    assert isinstance(sei_count, int)
    assert sei_count >= 1
    assert total_videos == 1


def _message(  # noqa: PLR0913
    timestamp_ms: int,
    *,
    lat: float,
    lon: float,
    speed: float = 10.0,
    accel_x: float = 0.0,
    autopilot: str = "NONE",
    has_gps: bool = True,
) -> FallbackTelemetryMessage:
    return FallbackTelemetryMessage(
        has_gps=has_gps,
        timestamp_ms=timestamp_ms,
        latitude_deg=lat,
        longitude_deg=lon,
        heading_deg=90.0,
        vehicle_speed_mps=speed,
        linear_acceleration_x=accel_x,
        linear_acceleration_y=0.0,
        linear_acceleration_z=0.0,
        gear_state="DRIVE",
        autopilot_state=autopilot,
        steering_wheel_angle=0.0,
        brake_applied=False,
        blinker_on_left=False,
        blinker_on_right=False,
        frame_index=timestamp_ms // 1000,
    )


def _write_sample_mp4(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ftyp = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
    mdat_payload = b"\x00\x00\x00\x02\x06\xff\x00\x00\x00\x02\x05\xaa"
    mdat_size = 8 + len(mdat_payload)
    path.write_bytes(ftyp + struct.pack(">I", mdat_size) + b"mdat" + mdat_payload)
    old_mtime = time() - 3600
    path.touch()
    os.utime(path, (old_mtime, old_mtime))
