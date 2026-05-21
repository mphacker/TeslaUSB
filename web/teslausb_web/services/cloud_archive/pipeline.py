"""Pipeline-queue integration helpers for cloud archive."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from teslausb_web.services.cloud_archive.paths import _canonical_rel_path_from_local
from teslausb_web.services.cloud_archive.settings import (
    DEFAULT_PIPELINE_BATCH_SIZE,
    CloudArchiveConfig,
    CloudArchiveStateError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from teslausb_web.services.cloud_archive.discovery import EventCandidate
    from teslausb_web.services.cloud_archive.worker import WorkerState

STAGE_CLOUD_PENDING: Final[str] = "cloud_pending"
STAGE_CLOUD_DONE: Final[str] = "cloud_done"
LEGACY_TABLE_CLOUD_SYNCED: Final[str] = "cloud_synced_files"
LEGACY_TABLE_LIVE_EVENT_QUEUE: Final[str] = "live_event_queue"
PRIORITY_LIVE_EVENT: Final[int] = 0
PRIORITY_CLOUD_BULK: Final[int] = 4
_SHADOW_PEEK_COUNT: Final[int] = 8


@dataclass(frozen=True, slots=True)
class ShadowTelemetry:
    agreement_count: int
    disagreement_count: int
    pipeline_enqueue_count: int


@dataclass(frozen=True, slots=True)
class PipelineClaim:
    local_path: Path
    relative_path: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class PipelineCloudSyncedRecord:
    file_path: str
    remote_path: str | None
    status: str
    file_size: int | None = None
    file_mtime: float | None = None


@dataclass(frozen=True, slots=True)
class PipelineCloudSyncedStateUpdate:
    file_path: str
    new_stage: str | None = None
    status: str | None = None
    attempts: int | None = None
    last_error: str | None = None
    completed_at: float | None = None
    next_retry_at: float | None = None


@dataclass(frozen=True, slots=True)
class PipelineEventEnqueueRequest:
    relative_path: str
    event_dir: str | None = None
    event_size: int | None = None
    score: int | None = None
    priority: int = PRIORITY_CLOUD_BULK
    producer: str = "cloud_archive._discover_events"


@contextmanager
def _open_pipeline_db(mapping_db_path: Path | None) -> Iterator[sqlite3.Connection | None]:
    if mapping_db_path is None or not mapping_db_path.exists():
        yield None
        return
    connection = sqlite3.connect(str(mapping_db_path), timeout=10.0)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _pipeline_table_exists(connection: sqlite3.Connection | None) -> bool:
    if connection is None:
        return False
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pipeline_queue'"
    ).fetchone()
    return row is not None


def _legacy_status_to_pipeline(status: str) -> tuple[str, str]:
    if status == "synced":
        return STAGE_CLOUD_DONE, "done"
    if status in {"uploading", "syncing"}:
        return STAGE_CLOUD_PENDING, "in_progress"
    if status in {"failed", "dead_letter"}:
        return STAGE_CLOUD_PENDING, status
    return STAGE_CLOUD_PENDING, "pending"


def _require_pipeline_connection(
    connection: sqlite3.Connection | None,
) -> sqlite3.Connection:
    if connection is None:
        raise CloudArchiveStateError("Pipeline database connection is unavailable")
    return connection


def _dual_write_pipeline_cloud_synced(
    mapping_db_path: Path | None,
    record: PipelineCloudSyncedRecord,
) -> bool:
    return (
        _dual_write_pipeline_cloud_synced_batch(
            mapping_db_path,
            (
                (
                    record.file_path,
                    record.remote_path,
                    record.status,
                    record.file_size,
                    record.file_mtime,
                ),
            ),
        )
        > 0
    )


def _dual_write_pipeline_cloud_synced_batch(
    mapping_db_path: Path | None,
    items: Sequence[tuple[str, str | None, str, int | None, float | None]],
) -> int:
    inserted = 0
    with _open_pipeline_db(mapping_db_path) as connection:
        if not _pipeline_table_exists(connection):
            return 0
        pipeline_db = _require_pipeline_connection(connection)
        for file_path, remote_path, status, file_size, file_mtime in items:
            stage, unified_status = _legacy_status_to_pipeline(status)
            payload = json.dumps(
                {
                    "legacy_status": status,
                    "file_size": file_size,
                    "file_mtime": file_mtime,
                },
                sort_keys=True,
            )
            rowcount = pipeline_db.execute(
                (
                    "INSERT INTO pipeline_queue ("
                    "source_path, dest_path, stage, status, priority, attempts, "
                    "enqueued_at, payload_json, legacy_table"
                    ") VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?) "
                    "ON CONFLICT(source_path, stage, legacy_table) DO UPDATE SET "
                    "dest_path = excluded.dest_path, status = excluded.status, "
                    "payload_json = excluded.payload_json"
                ),
                (
                    file_path,
                    remote_path,
                    stage,
                    unified_status,
                    PRIORITY_CLOUD_BULK,
                    time.time(),
                    payload,
                    LEGACY_TABLE_CLOUD_SYNCED,
                ),
            ).rowcount
            inserted += int(rowcount > 0)
        pipeline_db.commit()
    return inserted


def _dual_write_pipeline_cloud_synced_state(
    mapping_db_path: Path | None,
    update: PipelineCloudSyncedStateUpdate,
) -> bool:
    if all(
        value is None
        for value in (
            update.new_stage,
            update.status,
            update.attempts,
            update.last_error,
            update.completed_at,
            update.next_retry_at,
        )
    ):
        return False
    with _open_pipeline_db(mapping_db_path) as connection:
        if not _pipeline_table_exists(connection):
            return False
        pipeline_db = _require_pipeline_connection(connection)
        rowcount = pipeline_db.execute(
            (
                "UPDATE pipeline_queue SET stage = COALESCE(?, stage), "
                "status = COALESCE(?, status), attempts = COALESCE(?, attempts), "
                "last_error = COALESCE(?, last_error), completed_at = COALESCE(?, completed_at), "
                "next_retry_at = COALESCE(?, next_retry_at), claimed_by = NULL, claimed_at = NULL "
                "WHERE stage = ? AND source_path = ? AND legacy_table = ?"
            ),
            (
                update.new_stage,
                update.status,
                update.attempts,
                update.last_error,
                update.completed_at,
                update.next_retry_at,
                STAGE_CLOUD_PENDING,
                update.file_path,
                LEGACY_TABLE_CLOUD_SYNCED,
            ),
        ).rowcount
        pipeline_db.commit()
    return rowcount > 0


def _enqueue_event_to_pipeline(
    mapping_db_path: Path | None,
    request: PipelineEventEnqueueRequest,
) -> bool:
    with _open_pipeline_db(mapping_db_path) as connection:
        if not _pipeline_table_exists(connection):
            return False
        pipeline_db = _require_pipeline_connection(connection)
        payload = json.dumps(
            {
                "event_dir": request.event_dir,
                "event_size": request.event_size,
                "score": request.score,
                "producer": request.producer,
            },
            sort_keys=True,
        )
        rowcount = pipeline_db.execute(
            (
                "INSERT INTO pipeline_queue ("
                "source_path, stage, status, priority, attempts, enqueued_at, "
                "payload_json, legacy_table"
                ") VALUES (?, ?, 'pending', ?, 0, ?, ?, ?) "
                "ON CONFLICT(source_path, stage, legacy_table) DO NOTHING"
            ),
            (
                request.relative_path,
                STAGE_CLOUD_PENDING,
                request.priority,
                time.time(),
                payload,
                LEGACY_TABLE_CLOUD_SYNCED,
            ),
        ).rowcount
        pipeline_db.commit()
    return rowcount > 0


def _enqueue_events_to_pipeline_batch(
    mapping_db_path: Path | None,
    candidates: Sequence[EventCandidate],
) -> int:
    inserted = 0
    for candidate in candidates:
        inserted += int(
            _enqueue_event_to_pipeline(
                mapping_db_path,
                PipelineEventEnqueueRequest(
                    relative_path=candidate.relative_path,
                    event_dir=str(candidate.local_path),
                    event_size=candidate.size_bytes,
                    score=candidate.score,
                ),
            )
        )
    return inserted


def enqueue_live_event_from_event_json(
    config: CloudArchiveConfig,
    state: WorkerState,
    event_json_paths: Sequence[str],
) -> int:
    inserted = 0
    for raw_path in event_json_paths:
        event_dir = Path(raw_path).parent
        if not event_dir.is_dir():
            continue
        relative_path = _canonical_rel_path_from_local(event_dir, config.teslacam_path)
        try:
            total_size = sum(
                child.stat().st_size for child in event_dir.iterdir() if child.is_file()
            )
        except OSError:
            continue
        if _enqueue_event_to_pipeline(
            config.mapping_db_path,
            PipelineEventEnqueueRequest(
                relative_path=relative_path,
                event_dir=str(event_dir),
                event_size=total_size,
                priority=PRIORITY_LIVE_EVENT,
                producer="file_watcher.event_json",
            ),
        ):
            inserted += 1
    if inserted > 0:
        state.note_pipeline_enqueue(inserted)
        state.wake_event.set()
    return inserted


def _shadow_compare_cloud_picks(
    state: WorkerState,
    *,
    legacy_path: str | None,
    pipeline_candidates: tuple[str, ...],
) -> None:
    if legacy_path is None or legacy_path in set(pipeline_candidates):
        state.note_shadow_agreement()
        return
    state.note_shadow_disagreement()


def get_cloud_shadow_telemetry(state: WorkerState) -> ShadowTelemetry:
    return ShadowTelemetry(
        agreement_count=state.shadow_agreement_count,
        disagreement_count=state.shadow_disagreement_count,
        pipeline_enqueue_count=state.pipeline_enqueue_count,
    )


def _peek_pipeline_cloud_pending(
    mapping_db_path: Path | None,
    limit: int = _SHADOW_PEEK_COUNT,
) -> tuple[str, ...]:
    with _open_pipeline_db(mapping_db_path) as connection:
        if not _pipeline_table_exists(connection):
            return ()
        pipeline_db = _require_pipeline_connection(connection)
        rows = pipeline_db.execute(
            (
                "SELECT source_path FROM pipeline_queue WHERE stage = ? AND status = 'pending' "
                "ORDER BY priority ASC, enqueued_at ASC, id ASC LIMIT ?"
            ),
            (STAGE_CLOUD_PENDING, limit),
        ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _claim_via_pipeline_reader_cloud(
    config: CloudArchiveConfig,
    worker_id: str,
    limit: int = DEFAULT_PIPELINE_BATCH_SIZE,
) -> tuple[PipelineClaim, ...]:
    with _open_pipeline_db(config.mapping_db_path) as connection:
        if not _pipeline_table_exists(connection):
            return ()
        pipeline_db = _require_pipeline_connection(connection)
        rows = pipeline_db.execute(
            (
                "SELECT id, source_path, payload_json FROM pipeline_queue WHERE stage = ? "
                "AND status = 'pending' ORDER BY priority ASC, enqueued_at ASC, id ASC LIMIT ?"
            ),
            (STAGE_CLOUD_PENDING, limit),
        ).fetchall()
        claims: list[PipelineClaim] = []
        for row in rows:
            payload = json.loads(str(row["payload_json"] or "{}"))
            payload_dir = payload.get("event_dir")
            local_path = (
                Path(str(payload_dir))
                if isinstance(payload_dir, str) and payload_dir
                else config.teslacam_path / str(row["source_path"])
            )
            size_value = payload.get("event_size")
            size_bytes = int(size_value) if isinstance(size_value, (int, float)) else 0
            updated = pipeline_db.execute(
                (
                    "UPDATE pipeline_queue SET status = 'in_progress', attempts = attempts + 1, "
                    "claimed_by = ?, claimed_at = ? WHERE id = ? AND status = 'pending'"
                ),
                (worker_id, time.time(), row["id"]),
            ).rowcount
            if updated:
                claims.append(
                    PipelineClaim(
                        local_path=local_path,
                        relative_path=str(row["source_path"]),
                        size_bytes=size_bytes,
                    )
                )
        pipeline_db.commit()
    return tuple(claims)


def _release_cloud_pipeline_claims(
    mapping_db_path: Path | None,
    relative_paths: Sequence[str],
    last_error: str,
) -> int:
    if not relative_paths:
        return 0
    with _open_pipeline_db(mapping_db_path) as connection:
        if not _pipeline_table_exists(connection):
            return 0
        pipeline_db = _require_pipeline_connection(connection)
        released = 0
        for relative_path in relative_paths:
            released += pipeline_db.execute(
                (
                    "UPDATE pipeline_queue SET status = 'pending', claimed_by = NULL, "
                    "claimed_at = NULL, last_error = ? WHERE stage = ? AND source_path = ? "
                    "AND status = 'in_progress'"
                ),
                (last_error, STAGE_CLOUD_PENDING, relative_path),
            ).rowcount
        pipeline_db.commit()
    return released
