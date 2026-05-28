"""Upload pipeline and retry handling for cloud archive."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from random import SystemRandom
from typing import TYPE_CHECKING, Protocol

logger = logging.getLogger(__name__)

from teslausb_web.services.cloud_archive.discovery import EventCandidate, _discover_events
from teslausb_web.services.cloud_archive.kv import KV_KEY_LAST_SUCCESSFUL_SYNC, kv_set
from teslausb_web.services.cloud_archive.pipeline import (
    PipelineCloudSyncedRecord,
    PipelineCloudSyncedStateUpdate,
    _dual_write_pipeline_cloud_synced,
    _dual_write_pipeline_cloud_synced_state,
    _enqueue_events_to_pipeline_batch,
    _peek_pipeline_cloud_pending,
    _shadow_compare_cloud_picks,
)
from teslausb_web.services.cloud_archive.reconcile import _reconcile_with_remote
from teslausb_web.services.cloud_archive.settings import (
    CloudArchiveError,
    CloudArchiveStateError,
    _read_retry_max_attempts_setting,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path
    from threading import Event

    from teslausb_web.services.cloud_archive.service import CloudArchiveService
    from teslausb_web.services.cloud_rclone_service import CloudRcloneService

_RANDOM = SystemRandom()


class UploadFailedError(CloudArchiveError):
    """A cloud archive upload failed after the row state was prepared."""


@dataclass(frozen=True, slots=True)
class UploadResult:
    success: bool
    cancelled: bool = False
    bytes_transferred: int = 0
    error_message: str | None = None
    dead_lettered: bool = False
    status: str | None = None
    retry_count: int = 0


class _UploadServiceProtocol(Protocol):
    config: object
    rclone_service: CloudRcloneService
    state: object
    queries: object


def upload_path_via_rclone(
    rclone_service: CloudRcloneService,
    local_path: Path,
    remote_path: str,
    cancel_event: Event | None = None,
) -> UploadResult:
    if cancel_event is not None and cancel_event.is_set():
        return UploadResult(success=False, cancelled=True, status="pending")
    try:
        result = rclone_service.transfer(local_path, remote_path, operation="copy")
    except (OSError, RuntimeError, ValueError) as exc:
        raise UploadFailedError(str(exc)) from exc
    if result.cancelled:
        return UploadResult(success=False, cancelled=True, status="pending")
    bytes_transferred = _path_size(local_path)
    return UploadResult(success=True, bytes_transferred=bytes_transferred, status="synced")


def _path_size(local_path: Path) -> int:
    if local_path.is_dir():
        return sum(path.stat().st_size for path in local_path.rglob("*") if path.is_file())
    return local_path.stat().st_size if local_path.exists() else 0


def _mark_upload_failure(
    connection: sqlite3.Connection,
    relative_path: str,
    error_message: str,
    max_retry_attempts: int,
) -> UploadResult:
    connection.execute(
        (
            "UPDATE cloud_synced_files SET status = CASE WHEN retry_count + 1 >= ? "
            "THEN 'dead_letter' ELSE 'failed' END, previous_last_error = last_error, "
            "last_error = ?, retry_count = retry_count + 1 WHERE file_path = ?"
        ),
        (max_retry_attempts, error_message, relative_path),
    )
    row = connection.execute(
        "SELECT status, retry_count FROM cloud_synced_files WHERE file_path = ?",
        (relative_path,),
    ).fetchone()
    if row is None:
        raise CloudArchiveStateError(f"Missing queue row for {relative_path}")
    return UploadResult(
        success=False,
        error_message=error_message,
        dead_lettered=str(row["status"]) == "dead_letter",
        status=str(row["status"]),
        retry_count=int(row["retry_count"]),
    )


def _wait_with_events(service: CloudArchiveService, timeout_seconds: float) -> bool:
    deadline = service._monotonic() + timeout_seconds
    while service._monotonic() < deadline:
        if service.state.stop_event.is_set() or service.state.cancel_event.is_set():
            return True
        remaining = deadline - service._monotonic()
        service.state.stop_event.wait(timeout=min(0.25, remaining))
    return False


def _backoff_seconds(service: CloudArchiveService, retry_count: int) -> float:
    base = min(
        service.config.backoff_max_seconds,
        service.config.backoff_initial_seconds * (2 ** max(0, retry_count - 1)),
    )
    return float(min(service.config.backoff_max_seconds, base + float(_RANDOM.random())))


def _prepopulate_queue(
    connection: sqlite3.Connection,
    candidates: tuple[EventCandidate, ...],
) -> None:
    """Insert a ``pending`` row in ``cloud_synced_files`` for every candidate.

    Without this the queue UI only ever sees the single in-flight file,
    because the per-file ``INSERT … status='uploading'`` in
    :func:`_mark_candidate_uploading` is the only thing that materialises a
    row. Operators reported "queue shows nothing" with 200+ telemetry-bearing
    RecentClips waiting. Pre-populating gives an honest backlog and makes
    "remove from queue" / "clear queue" actually have something to act on.

    ``INSERT OR IGNORE`` keeps existing rows untouched — already-uploading,
    already-synced and dead-lettered files keep their current status; the
    discovery layer already filters synced / dead_letter out of ``candidates``,
    and an interrupted 'uploading' row is left alone (it will be flipped back
    to 'pending' the next time it is processed or by
    :func:`recover_interrupted_uploads` on the next worker start).
    """
    if not candidates:
        return
    queued_at = datetime.now(UTC).isoformat()
    rows = [
        (
            candidate.relative_path,
            candidate.size_bytes,
            queued_at,
            candidate.priority,
        )
        for candidate in candidates
    ]
    # INSERT OR IGNORE preserves the priority of any row that already
    # exists in another status (e.g. an in-flight 'uploading' row keeps
    # its earlier priority). For brand-new pending rows we record the
    # priority computed by discovery so the uploader's ORDER BY can
    # surface live events ahead of the bulk backlog.
    connection.executemany(
        "INSERT OR IGNORE INTO cloud_synced_files ("
        "file_path, file_size, status, retry_count, last_error, added_at, priority"
        ") VALUES (?, ?, 'pending', 0, NULL, ?, ?)",
        rows,
    )
    # If a candidate already has a 'pending' row with the default
    # priority=0 but discovery now says it's high-priority (e.g. the
    # Rust materializer flagged a hard brake on a clip we'd already
    # queued bulk), lift the priority so it jumps the queue.
    priority_upgrades = [
        (candidate.priority, candidate.relative_path)
        for candidate in candidates
        if candidate.priority > 0
    ]
    if priority_upgrades:
        connection.executemany(
            "UPDATE cloud_synced_files SET priority = ? "
            "WHERE file_path = ? AND status = 'pending' AND priority < ?",
            [(priority, path, priority) for priority, path in priority_upgrades],
        )
    connection.commit()


def _prepare_drain(
    service: CloudArchiveService,
    trigger: str,
) -> tuple[int, tuple[EventCandidate, ...]]:
    with service.open_db() as connection:
        started_at = datetime.now(UTC).isoformat()
        cursor = connection.execute(
            (
                "INSERT INTO cloud_sync_sessions ("
                "started_at, status, trigger, window_mode"
                ") VALUES (?, 'running', ?, 'wifi')"
            ),
            (started_at, trigger),
        )
        if cursor.lastrowid is None:
            raise CloudArchiveStateError("Failed to create sync session")
        session_id = int(cursor.lastrowid)
        connection.commit()
        try:
            _reconcile_with_remote(connection, service.rclone_service)
            candidates = _discover_events(service.config, connection)
            _prepopulate_queue(connection, candidates)
            _enqueue_events_to_pipeline_batch(service.config.mapping_db_path, candidates)
            pipeline_candidates = _peek_pipeline_cloud_pending(service.config.mapping_db_path)
            legacy_first = candidates[0].relative_path if candidates else None
            _shadow_compare_cloud_picks(
                service.state,
                legacy_path=legacy_first,
                pipeline_candidates=pipeline_candidates,
            )
        except Exception as exc:
            connection.execute(
                "UPDATE cloud_sync_sessions SET status = 'failed', "
                "ended_at = ?, error_msg = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), str(exc), session_id),
            )
            connection.commit()
            raise
    service.state.set_totals(candidates)
    return session_id, candidates


def _mark_candidate_uploading(
    service: CloudArchiveService,
    candidate: EventCandidate,
) -> float:
    file_mtime = candidate.local_path.stat().st_mtime
    queued_at = datetime.now(UTC).isoformat()
    with service.open_db() as connection:
        connection.execute(
            (
                "INSERT INTO cloud_synced_files ("
                "file_path, file_size, file_mtime, status, retry_count, last_error, added_at"
                ") VALUES (?, ?, ?, 'uploading', "
                "COALESCE((SELECT retry_count FROM cloud_synced_files WHERE file_path = ?), 0), "
                "NULL, COALESCE((SELECT added_at FROM cloud_synced_files WHERE file_path = ?), ?)) "
                "ON CONFLICT(file_path) DO UPDATE SET "
                "file_size = excluded.file_size, file_mtime = excluded.file_mtime, "
                "status = 'uploading', last_error = NULL"
            ),
            (
                candidate.relative_path,
                candidate.size_bytes,
                file_mtime,
                candidate.relative_path,
                candidate.relative_path,
                queued_at,
            ),
        )
        connection.commit()
    _dual_write_pipeline_cloud_synced(
        service.config.mapping_db_path,
        PipelineCloudSyncedRecord(
            file_path=candidate.relative_path,
            remote_path=candidate.relative_path,
            status="uploading",
            file_size=candidate.size_bytes,
            file_mtime=file_mtime,
        ),
    )
    return file_mtime


def _attempt_upload(
    service: CloudArchiveService,
    candidate: EventCandidate,
) -> UploadResult:
    try:
        return upload_path_via_rclone(
            service.rclone_service,
            candidate.local_path,
            candidate.relative_path,
            service.state.cancel_event,
        )
    except UploadFailedError as exc:
        return UploadResult(success=False, error_message=str(exc))


def _mark_cancelled_upload(
    service: CloudArchiveService,
    relative_path: str,
) -> None:
    with service.open_db() as connection:
        connection.execute(
            "UPDATE cloud_synced_files SET status = 'pending' WHERE file_path = ?",
            (relative_path,),
        )
        connection.commit()
    _dual_write_pipeline_cloud_synced_state(
        service.config.mapping_db_path,
        PipelineCloudSyncedStateUpdate(
            file_path=relative_path,
            status="pending",
        ),
    )


def _mark_successful_upload(
    service: CloudArchiveService,
    candidate: EventCandidate,
    result: UploadResult,
) -> None:
    finished_at = datetime.now(UTC).isoformat()
    with service.open_db() as connection:
        connection.execute(
            (
                "UPDATE cloud_synced_files SET status = 'synced', synced_at = ?, "
                "remote_path = ?, retry_count = 0, last_error = NULL WHERE file_path = ?"
            ),
            (
                finished_at,
                candidate.relative_path,
                candidate.relative_path,
            ),
        )
        kv_set(connection, KV_KEY_LAST_SUCCESSFUL_SYNC, finished_at)
        connection.commit()
    _dual_write_pipeline_cloud_synced_state(
        service.config.mapping_db_path,
        PipelineCloudSyncedStateUpdate(
            file_path=candidate.relative_path,
            new_stage="cloud_done",
            status="done",
            attempts=0,
            completed_at=service._monotonic(),
        ),
    )
    service.state.record_success(result.bytes_transferred)


def _mark_failed_upload_and_retry_state(
    service: CloudArchiveService,
    candidate: EventCandidate,
    result: UploadResult,
) -> UploadResult:
    error_message = result.error_message or "upload failed"
    with service.open_db() as connection:
        failure = _mark_upload_failure(
            connection,
            candidate.relative_path,
            error_message,
            _read_retry_max_attempts_setting(service.config, connection),
        )
        connection.commit()
    _dual_write_pipeline_cloud_synced_state(
        service.config.mapping_db_path,
        PipelineCloudSyncedStateUpdate(
            file_path=candidate.relative_path,
            status=failure.status,
            attempts=failure.retry_count,
            last_error=error_message,
        ),
    )
    service.state.record_failure(error_message)
    return failure


def _process_candidate_upload(
    service: CloudArchiveService,
    candidate: EventCandidate,
) -> UploadResult:
    _mark_candidate_uploading(service, candidate)
    result = _attempt_upload(service, candidate)
    if result.cancelled:
        _mark_cancelled_upload(service, candidate.relative_path)
        return result
    if result.success:
        _mark_successful_upload(service, candidate, result)
        return result
    return _mark_failed_upload_and_retry_state(service, candidate, result)


def _finish_sync_session(
    service: CloudArchiveService,
    session_id: int,
    files_synced: int,
    bytes_transferred: int,
) -> None:
    with service.open_db() as connection:
        connection.execute(
            (
                "UPDATE cloud_sync_sessions SET ended_at = ?, files_synced = ?, "
                "bytes_transferred = ?, status = ?, error_msg = ? WHERE id = ?"
            ),
            (
                datetime.now(UTC).isoformat(),
                files_synced,
                bytes_transferred,
                "cancelled" if service.state.cancel_event.is_set() else "completed",
                service.state.error,
                session_id,
            ),
        )
        connection.commit()


def _drain_once(service: CloudArchiveService, trigger: str) -> bool:
    service.state.begin_drain(trigger)
    files_synced = 0
    bytes_transferred = 0
    session_id: int | None = None
    try:
        session_id, candidates = _prepare_drain(service, trigger)
        if not candidates:
            return False

        # Pre-flight: refuse to upload bytes that would push the remote
        # below the configured reserve. Runs cleanup on-demand if
        # auto-cleanup is enabled. Failures fall open (allow upload).
        from teslausb_web.services.cloud_archive.cloud_cleanup import (
            ensure_remote_headroom,
        )

        total_bytes = sum(max(0, c.size_bytes) for c in candidates)
        guard = ensure_remote_headroom(service, total_bytes)
        running_free: int | None = guard.free_bytes
        reserve_bytes = guard.reserve_bytes
        if not guard.ok:
            logger.info(
                "cloud sync: reserve tight upfront "
                "(free=%s reserve=%s needed=%s reason=%s); will gate per file",
                guard.free_bytes,
                reserve_bytes,
                total_bytes,
                guard.reason,
            )

        for candidate in candidates:
            if service.state.stop_event.is_set() or service.state.cancel_event.is_set():
                break

            # Per-candidate reserve gate. Only applies when the backend
            # reports free space and a reserve is configured.
            if (
                running_free is not None
                and reserve_bytes > 0
                and candidate.size_bytes > 0
                and running_free - candidate.size_bytes < reserve_bytes
            ):
                sub_guard = ensure_remote_headroom(service, candidate.size_bytes)
                if sub_guard.free_bytes is not None:
                    running_free = sub_guard.free_bytes
                if not sub_guard.ok:
                    logger.warning(
                        "cloud sync: stopping drain — uploading %s (%d bytes) "
                        "would breach reserve (free=%s reserve=%s reason=%s)",
                        candidate.relative_path,
                        candidate.size_bytes,
                        running_free,
                        reserve_bytes,
                        sub_guard.reason,
                    )
                    break

            service.state.set_current(candidate)
            result = _process_candidate_upload(service, candidate)
            if result.cancelled:
                break
            if result.success:
                files_synced += 1
                bytes_transferred += result.bytes_transferred
                if running_free is not None:
                    running_free = max(0, running_free - result.bytes_transferred)
                continue
            if result.status == "failed" and _wait_with_events(
                service,
                _backoff_seconds(service, result.retry_count),
            ):
                break
        return files_synced > 0
    finally:
        if session_id is not None:
            _finish_sync_session(
                service,
                session_id,
                files_synced,
                bytes_transferred,
            )
        service.state.finish_drain()


def _run_sync(service: CloudArchiveService, trigger: str = "manual") -> bool:
    return _drain_once(service, trigger)
