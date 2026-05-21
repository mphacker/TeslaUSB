from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .discovery import _iter_archived_with_mtime
from .kv import _kv_get, _kv_set
from .paths import canonical_key

if TYPE_CHECKING:
    from .service import MappingService

_BOOT_CATCHUP_WATERMARK_KEY = "boot_catchup_archived_max_mtime"
_RANDOM = secrets.SystemRandom()


def boot_catchup_scan(service: MappingService, *, source: str = "catchup") -> dict[str, int]:
    result = {
        "scanned": 0,
        "already_indexed": 0,
        "enqueued": 0,
        "skipped_by_watermark": 0,
    }
    watermark = _read_watermark(service)
    new_files: list[tuple[Path, float]] = []
    max_mtime = watermark
    for path, mtime in _iter_archived_with_mtime(service.config.archive_root):
        result["scanned"] += 1
        max_mtime = max(max_mtime, mtime)
        if mtime <= watermark:
            result["skipped_by_watermark"] += 1
            continue
        new_files.append((path, mtime))
    if not new_files:
        _write_watermark(service, max_mtime)
        return result
    with service.open_db() as connection:
        indexed_keys = _indexed_keys(connection)
        queued_keys = _queued_keys(connection)
        enqueued_rows = _enqueue_missing_files(
            connection,
            new_files,
            indexed_keys=indexed_keys,
            queued_keys=queued_keys,
            source=source,
            result=result,
        )
        if enqueued_rows > 0:
            connection.commit()
        _write_watermark_to_connection(connection, max_mtime)
    service._set_status(queue_depth=service._queue_depth())
    return result


def _initial_stale_scan_delay(service: MappingService) -> float:
    return service.config.initial_stale_scan_base_seconds + _RANDOM.uniform(
        0.0,
        service.config.initial_stale_scan_jitter_seconds,
    )


def _run_stale_scan_blocking(service: MappingService, *, source: str) -> dict[str, int] | None:
    del source
    with service._status_lock:
        service._last_stale_scan_at = service._monotonic()
    try:
        return service.purge_deleted_videos()
    except sqlite3.Error:
        return None


def trigger_stale_scan_now(
    service: MappingService,
    *,
    source: str = "manual",
    debounce_seconds: float | None = None,
) -> dict[str, float | str]:
    debounce = (
        service.config.stale_scan_debounce_seconds if debounce_seconds is None else debounce_seconds
    )
    now = service._monotonic()
    with service._status_lock:
        last_run = service._last_stale_scan_at
        if last_run > 0.0:
            age = now - last_run
            if age < debounce:
                return {"status": "debounced", "last_run_age_seconds": age}
        service._last_stale_scan_at = now
    thread = threading.Thread(
        target=_run_stale_scan_blocking,
        kwargs={"service": service, "source": source},
        name=f"mapping-stale-scan-{source}",
        daemon=True,
    )
    thread.start()
    return {"status": "fired"}


def start_daily_stale_scan(service: MappingService) -> bool:
    thread = service._daily_stale_scan_thread
    if thread is not None and thread.is_alive():
        return False
    stop_event = threading.Event()
    service._daily_stale_scan_stop = stop_event

    def _loop() -> None:
        if stop_event.wait(timeout=_initial_stale_scan_delay(service)):
            return
        while not stop_event.is_set():
            _run_stale_scan_blocking(service, source="scheduled")
            jitter = _RANDOM.uniform(
                -service.config.stale_scan_jitter_seconds,
                service.config.stale_scan_jitter_seconds,
            )
            wait_time = max(0.0, service.config.stale_scan_interval_seconds + jitter)
            if stop_event.wait(timeout=wait_time):
                return

    service._daily_stale_scan_thread = threading.Thread(
        target=_loop,
        name="mapping-daily-stale-scan",
        daemon=True,
    )
    service._daily_stale_scan_thread.start()
    return True


def stop_daily_stale_scan(service: MappingService, *, timeout: float | None = None) -> bool:
    if service._daily_stale_scan_stop is not None:
        service._daily_stale_scan_stop.set()
    thread = service._daily_stale_scan_thread
    join_timeout = service.config.shutdown_timeout_seconds if timeout is None else timeout
    if thread is not None and thread.is_alive():
        thread.join(timeout=join_timeout)
        if thread.is_alive():
            return False
    service._daily_stale_scan_thread = None
    return True


def _read_watermark(service: MappingService) -> float:
    try:
        with service.open_db() as connection:
            raw = _kv_get(connection, _BOOT_CATCHUP_WATERMARK_KEY)
    except sqlite3.Error:
        return 0.0
    try:
        return 0.0 if raw is None else float(raw)
    except ValueError:
        return 0.0


def _write_watermark(service: MappingService, watermark: float) -> None:
    try:
        with service.open_db() as connection:
            _write_watermark_to_connection(connection, watermark)
    except sqlite3.Error:
        return


def _write_watermark_to_connection(connection: sqlite3.Connection, watermark: float) -> None:
    _kv_set(connection, _BOOT_CATCHUP_WATERMARK_KEY, repr(watermark))


def _indexed_keys(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT file_path FROM indexed_files").fetchall()
    return {
        canonical_key(Path(row["file_path"])) for row in rows if isinstance(row["file_path"], str)
    }


def _queued_keys(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT canonical_key FROM indexing_queue").fetchall()
    return {str(row["canonical_key"]) for row in rows if isinstance(row["canonical_key"], str)}


def _enqueue_missing_files(  # noqa: PLR0913
    connection: sqlite3.Connection,
    files: list[tuple[Path, float]],
    *,
    indexed_keys: set[str],
    queued_keys: set[str],
    source: str,
    result: dict[str, int],
) -> int:
    rows_to_insert: list[tuple[str, str, int, float, float, int, str]] = []
    timestamp = time.time()
    for path, _mtime in files:
        key = canonical_key(path)
        if key in indexed_keys:
            result["already_indexed"] += 1
            continue
        if key in queued_keys:
            continue
        rows_to_insert.append((key, str(path), 50, timestamp, 0.0, 0, source))
        queued_keys.add(key)
    if not rows_to_insert:
        return 0
    connection.executemany(
        """
        INSERT OR IGNORE INTO indexing_queue (
            canonical_key,
            file_path,
            priority,
            enqueued_at,
            next_attempt_at,
            attempts,
            source
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows_to_insert,
    )
    result["enqueued"] = len(rows_to_insert)
    return len(rows_to_insert)
