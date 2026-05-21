from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from teslausb_web.services.cloud_archive.discovery import EventCandidate
from teslausb_web.services.cloud_archive.pipeline import (
    _dual_write_pipeline_cloud_synced_batch,
    _enqueue_events_to_pipeline_batch,
    enqueue_live_event_from_event_json,
    get_cloud_shadow_telemetry,
)
from teslausb_web.services.cloud_archive.settings import CloudArchiveConfig
from teslausb_web.services.cloud_archive.worker import WorkerState

if TYPE_CHECKING:
    from pathlib import Path


def _create_pipeline_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE pipeline_queue ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "source_path TEXT NOT NULL, dest_path TEXT, stage TEXT NOT NULL, status TEXT NOT NULL, "
            "priority INTEGER NOT NULL, attempts INTEGER NOT NULL, enqueued_at REAL NOT NULL, "
            "payload_json TEXT, legacy_id INTEGER, legacy_table TEXT NOT NULL, claimed_by TEXT, "
            "claimed_at REAL, last_error TEXT, completed_at REAL, next_retry_at REAL, "
            "UNIQUE(source_path, stage, legacy_table))"
        )
        connection.commit()
    finally:
        connection.close()


def _make_config(tmp_path: Path, teslacam: Path, mapping_db: Path) -> CloudArchiveConfig:
    return CloudArchiveConfig(
        enabled=True,
        db_path=tmp_path / "cloud.db",
        teslacam_path=teslacam,
        mapping_db_path=mapping_db,
    )


def test_dual_write_pipeline_cloud_synced_batch_inserts_rows(tmp_path: Path) -> None:
    mapping_db = tmp_path / "mapping.db"
    _create_pipeline_db(mapping_db)

    inserted = _dual_write_pipeline_cloud_synced_batch(
        mapping_db,
        (("SentryClips/one", "SentryClips/one", "uploading", 10, 1.0),),
    )

    assert inserted == 1
    connection = sqlite3.connect(mapping_db)
    try:
        row = connection.execute("SELECT stage, status FROM pipeline_queue").fetchone()
    finally:
        connection.close()
    assert row == ("cloud_pending", "in_progress")


def test_enqueue_events_to_pipeline_batch_records_candidates(tmp_path: Path) -> None:
    teslacam = tmp_path / "TeslaCam"
    event_dir = teslacam / "SentryClips" / "2026-01-01_10-00-00"
    event_dir.mkdir(parents=True)
    mapping_db = tmp_path / "mapping.db"
    _create_pipeline_db(mapping_db)

    inserted = _enqueue_events_to_pipeline_batch(
        mapping_db,
        (
            EventCandidate(
                local_path=event_dir,
                relative_path="SentryClips/2026-01-01_10-00-00",
                size_bytes=123,
                score=0,
            ),
        ),
    )

    assert inserted == 1


def test_enqueue_live_event_from_event_json_updates_state(tmp_path: Path) -> None:
    teslacam = tmp_path / "TeslaCam"
    event_dir = teslacam / "SentryClips" / "2026-01-01_10-00-00"
    event_dir.mkdir(parents=True)
    (event_dir / "event.json").write_text("{}", encoding="utf-8")
    (event_dir / "clip.mp4").write_bytes(b"video")
    mapping_db = tmp_path / "mapping.db"
    _create_pipeline_db(mapping_db)
    config = _make_config(tmp_path, teslacam, mapping_db)
    state = WorkerState()

    inserted = enqueue_live_event_from_event_json(config, state, [str(event_dir / "event.json")])

    assert inserted == 1
    assert state.pipeline_enqueue_count == 1
    assert state.wake_event.is_set()


def test_get_cloud_shadow_telemetry_reads_state_counts() -> None:
    state = WorkerState()
    state.note_shadow_agreement()
    state.note_shadow_disagreement()
    state.note_pipeline_enqueue(3)

    telemetry = get_cloud_shadow_telemetry(state)

    assert telemetry.agreement_count == 1
    assert telemetry.disagreement_count == 1
    assert telemetry.pipeline_enqueue_count == 3
