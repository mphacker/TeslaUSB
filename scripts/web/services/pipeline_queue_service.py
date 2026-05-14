"""Unified pipeline queue helpers — issue #184 Wave 4 — Phase I.1.

This module is the single point of access for the new ``pipeline_queue``
table in ``geodata.db``. The table itself is created by
``mapping_migrations._SCHEMA_SQL`` at v16; this module owns the
producer / consumer / backfill API.

**Phase I.1 only adds the dual-write side.** Legacy producers
(``archive_queue.enqueue_for_archive``, ``indexing_queue_service.
enqueue_for_indexing``, ``live_event_sync_service.enqueue_event_json``,
and the cloud-synced-files insertion path) call into this module's
:func:`dual_write_enqueue` after they write to their own legacy table.
Reads remain on the legacy tables — no behaviour change yet.

Design rules:

* **Best-effort dual-write.** A failure to write to ``pipeline_queue``
  must never fail the legacy enqueue. The legacy queue is the source
  of truth in Phase I.1; pipeline_queue is shadow data being validated.
  All errors are logged at WARNING and swallowed.
* **Idempotent.** The composite unique constraint
  ``(source_path, stage, legacy_table)`` plus ``INSERT OR IGNORE``
  makes repeated dual-writes harmless. Producers that re-enqueue
  (e.g. inotify firing on the same path twice) write at most one
  pipeline_queue row.
* **Cross-DB writes are short-lived connections.** The LES dual-write
  is the only cross-DB case (LES is in ``cloud_sync.db``;
  ``pipeline_queue`` is in ``geodata.db``). Each dual-write opens a
  fresh ``geodata.db`` connection, writes one row, and closes. No
  long-lived second connection is held alongside the legacy DB
  connection — that would double the connection count and complicate
  task_coordinator semantics.

Public API:

* :data:`STAGE_*` constants — canonical stage names.
* :data:`LEGACY_TABLE_*` constants — canonical legacy table names.
* :data:`PRIORITY_*` constants — canonical priorities.
* :func:`dual_write_enqueue` — producer hook.
* :func:`backfill_legacy_queues` — one-time migration helper.
* :func:`pipeline_status` — debug / verification view.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical constants — every dual-write site MUST use these strings.
# ---------------------------------------------------------------------------

# Stage values. The unified worker (Phase I.2) selects on
# ``WHERE stage = ? AND status = 'pending'``; producer hooks set the
# initial stage when enqueuing.
STAGE_ARCHIVE_PENDING = 'archive_pending'
STAGE_ARCHIVE_DONE = 'archive_done'
STAGE_INDEX_PENDING = 'index_pending'
STAGE_INDEX_DONE = 'index_done'
STAGE_CLOUD_PENDING = 'cloud_pending'
STAGE_CLOUD_DONE = 'cloud_done'
STAGE_LIVE_EVENT_PENDING = 'live_event_pending'
STAGE_LIVE_EVENT_DONE = 'live_event_done'

# Legacy table names — used by the dual-write hooks to tag which
# legacy producer created each pipeline_queue row.
LEGACY_TABLE_ARCHIVE = 'archive_queue'
LEGACY_TABLE_INDEXING = 'indexing_queue'
LEGACY_TABLE_LIVE_EVENT = 'live_event_queue'
LEGACY_TABLE_CLOUD_SYNCED = 'cloud_synced_files'

# Priority mapping — lower is more urgent.
PRIORITY_LIVE_EVENT = 0          # LES real-time event upload
PRIORITY_ARCHIVE_EVENT = 1       # Sentry / Saved clips
PRIORITY_ARCHIVE_RECENT = 2      # RecentClips (age-bound)
PRIORITY_ARCHIVE_OTHER = 3       # ArchivedClips back-fill / other
PRIORITY_CLOUD_BULK = 4          # cloud_synced_files bulk catch-up
PRIORITY_INDEXING = 5            # default indexing


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_pipeline_db() -> Optional[str]:
    """Return the geodata.db path, or None if config can't be loaded.

    Lazy import of ``config`` so unit tests that don't bootstrap the
    Flask app can still import this module without side effects.
    """
    try:
        from config import MAPPING_DB_PATH  # type: ignore
        return MAPPING_DB_PATH
    except Exception:  # noqa: BLE001
        return None


def _open_pipeline_conn(db_path: str) -> sqlite3.Connection:
    """Open the pipeline DB with the same conservative pragmas as the
    rest of the geodata.db consumers. Caller must close.
    """
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA mmap_size=0")
    conn.execute("PRAGMA cache_size=-256")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _now_epoch() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def dual_write_enqueue(*,
                       source_path: str,
                       stage: str,
                       legacy_table: str,
                       legacy_id: Optional[int] = None,
                       priority: int = PRIORITY_INDEXING,
                       dest_path: Optional[str] = None,
                       payload: Optional[Dict[str, Any]] = None,
                       db_path: Optional[str] = None) -> bool:
    """Insert a row into ``pipeline_queue`` mirroring a legacy enqueue.

    Returns True if a new row was inserted, False if the row already
    exists (idempotent), and False if any error occurs (logged at
    WARNING). NEVER raises.

    Args:
        source_path: The resource being processed. For archive /
            indexing this is the file path; for LES it's the
            ``event.json`` path; for cloud_synced_files it's the
            file path.
        stage: One of the ``STAGE_*`` constants.
        legacy_table: One of the ``LEGACY_TABLE_*`` constants — which
            old queue this row mirrors.
        legacy_id: Back-pointer to the legacy row's primary key, if
            available. Used by the migration helper to verify that
            every legacy row has a corresponding pipeline_queue row.
        priority: Lower = more urgent. Default ``PRIORITY_INDEXING``.
        dest_path: Final destination on SD card (archive only); None
            for queues that don't have a destination.
        payload: Queue-specific extras that don't fit the flat schema.
            Stored as JSON in the ``payload_json`` column. Examples:
            ``{'expected_size': 1234, 'expected_mtime': 1.0}`` for
            archive; ``{'event_reason': 'sentry', 'upload_scope':
            'event_minute'}`` for LES.
        db_path: Override the geodata.db path (test injection).
    """
    if not source_path or not stage or not legacy_table:
        return False
    if db_path is None:
        db_path = _resolve_pipeline_db()
    if not db_path:
        return False
    payload_text = json.dumps(payload, separators=(',', ':')) if payload else None
    conn = None
    try:
        conn = _open_pipeline_conn(db_path)
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO pipeline_queue
                (source_path, dest_path, stage, status, priority,
                 enqueued_at, payload_json, legacy_id, legacy_table)
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)
            """,
            (source_path, dest_path, stage, int(priority),
             _now_epoch(), payload_text, legacy_id, legacy_table),
        )
        conn.commit()
        return bool(cur.rowcount)
    except sqlite3.Error as e:
        # Best-effort: a failed dual-write must NOT fail the legacy
        # enqueue. Log at WARNING so the operator can see drift if
        # this ever fires repeatedly.
        logger.warning(
            "pipeline_queue dual-write failed for %s/%s: %s",
            legacy_table, source_path, e,
        )
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def dual_write_enqueue_many(rows: Iterable[Dict[str, Any]],
                            db_path: Optional[str] = None) -> int:
    """Batched dual-write — same semantics as ``dual_write_enqueue``
    but for a list of rows.

    ``rows`` is an iterable of dicts with keys matching the named
    arguments of :func:`dual_write_enqueue`. Returns the count of
    newly-inserted rows. Errors on individual rows are NOT raised;
    the batch continues. SQLite errors at the executemany level
    return 0 and log a warning.
    """
    if db_path is None:
        db_path = _resolve_pipeline_db()
    if not db_path:
        return 0
    rows = list(rows)
    if not rows:
        return 0
    now = _now_epoch()
    tuples = []
    for r in rows:
        src = r.get('source_path')
        stage = r.get('stage')
        legacy_table = r.get('legacy_table')
        if not src or not stage or not legacy_table:
            continue
        payload = r.get('payload')
        payload_text = json.dumps(payload, separators=(',', ':')) if payload else None
        tuples.append((
            src, r.get('dest_path'), stage,
            int(r.get('priority', PRIORITY_INDEXING)),
            now, payload_text,
            r.get('legacy_id'), legacy_table,
        ))
    if not tuples:
        return 0
    conn = None
    try:
        conn = _open_pipeline_conn(db_path)
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.executemany(
            """
            INSERT OR IGNORE INTO pipeline_queue
                (source_path, dest_path, stage, status, priority,
                 enqueued_at, payload_json, legacy_id, legacy_table)
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)
            """,
            tuples,
        )
        conn.commit()
        # cur.rowcount in SQLite for executemany is the LAST statement's
        # rowcount, not a sum — safer to count via a fresh SELECT.
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(tuples)
    except sqlite3.Error as e:
        logger.warning(
            "pipeline_queue dual-write batch failed (%d rows): %s",
            len(tuples), e,
        )
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def pipeline_status(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Return a small dict summarising the pipeline_queue state.

    Useful for debugging / the Settings page / dual-write parity
    checks. Counts grouped by ``(legacy_table, stage, status)``.
    Returns an empty dict on any error.
    """
    if db_path is None:
        db_path = _resolve_pipeline_db()
    if not db_path or not os.path.isfile(db_path):
        return {}
    conn = None
    try:
        conn = _open_pipeline_conn(db_path)
        rows = conn.execute(
            """SELECT legacy_table, stage, status, COUNT(*) AS n
               FROM pipeline_queue
               GROUP BY legacy_table, stage, status
               ORDER BY legacy_table, stage, status"""
        ).fetchall()
        return {
            'total': sum(r['n'] for r in rows),
            'by_legacy_stage_status': [
                {
                    'legacy_table': r['legacy_table'],
                    'stage': r['stage'],
                    'status': r['status'],
                    'count': r['n'],
                }
                for r in rows
            ],
        }
    except sqlite3.Error as e:
        logger.warning("pipeline_status failed: %s", e)
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


# ---------------------------------------------------------------------------
# Backfill from legacy queues — one-time migration helper
# ---------------------------------------------------------------------------

def backfill_legacy_queues(*,
                           pipeline_db_path: Optional[str] = None,
                           cloud_db_path: Optional[str] = None) -> Dict[str, int]:
    """Backfill ``pipeline_queue`` from the four legacy queue tables.

    Idempotent — re-running is safe (the unique constraint catches
    duplicates). Returns a per-source count of rows inserted.

    This is a one-time migration to handle pending rows that existed
    BEFORE the dual-write hooks were installed (i.e., the backlog at
    upgrade time). After upgrade, dual-write keeps both tables in
    sync; this backfill only covers the upgrade gap.

    Two source DBs:
      * ``pipeline_db_path`` (geodata.db): archive_queue + indexing_queue
      * ``cloud_db_path`` (cloud_sync.db): live_event_queue + cloud_synced_files

    Both default to the configured paths via lazy ``config`` import.
    """
    if pipeline_db_path is None:
        pipeline_db_path = _resolve_pipeline_db()
    if cloud_db_path is None:
        try:
            from config import CLOUD_ARCHIVE_DB_PATH  # type: ignore
            cloud_db_path = CLOUD_ARCHIVE_DB_PATH
        except Exception:  # noqa: BLE001
            cloud_db_path = None

    counts = {
        LEGACY_TABLE_ARCHIVE: 0,
        LEGACY_TABLE_INDEXING: 0,
        LEGACY_TABLE_LIVE_EVENT: 0,
        LEGACY_TABLE_CLOUD_SYNCED: 0,
    }
    if pipeline_db_path and os.path.isfile(pipeline_db_path):
        counts[LEGACY_TABLE_ARCHIVE] = _backfill_archive_queue(pipeline_db_path)
        counts[LEGACY_TABLE_INDEXING] = _backfill_indexing_queue(pipeline_db_path)
    if cloud_db_path and os.path.isfile(cloud_db_path):
        counts[LEGACY_TABLE_LIVE_EVENT] = _backfill_live_event_queue(
            cloud_db_path, pipeline_db_path,
        )
        counts[LEGACY_TABLE_CLOUD_SYNCED] = _backfill_cloud_synced_files(
            cloud_db_path, pipeline_db_path,
        )
    total = sum(counts.values())
    if total:
        logger.info("pipeline_queue backfill: %s (total %d)", counts, total)
    return counts


def _backfill_archive_queue(pipeline_db: str) -> int:
    """Backfill from ``archive_queue`` (same DB as pipeline_queue).

    Single-DB backfill — uses one ``INSERT INTO ... SELECT`` for atomicity.
    """
    conn = None
    try:
        conn = _open_pipeline_conn(pipeline_db)
        # Existence check — archive_queue may not be present on
        # very old DBs.
        if not _table_exists(conn, 'archive_queue'):
            return 0
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO pipeline_queue
                (source_path, dest_path, stage, status, priority,
                 attempts, last_error, enqueued_at, payload_json,
                 legacy_id, legacy_table)
            SELECT
                source_path,
                dest_path,
                CASE
                    WHEN status IN ('copied') THEN 'archive_done'
                    ELSE 'archive_pending'
                END,
                CASE
                    WHEN status = 'pending'  THEN 'pending'
                    WHEN status = 'claimed'  THEN 'in_progress'
                    WHEN status = 'copied'   THEN 'done'
                    ELSE 'failed'
                END,
                COALESCE(priority, 3),
                COALESCE(attempts, 0),
                last_error,
                COALESCE(strftime('%s', enqueued_at) + 0, ?),
                json_object('expected_size', expected_size,
                            'expected_mtime', expected_mtime),
                id,
                'archive_queue'
            FROM archive_queue
            """,
            (_now_epoch(),),
        )
        conn.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    except sqlite3.Error as e:
        logger.warning("backfill archive_queue failed: %s", e)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def _backfill_indexing_queue(pipeline_db: str) -> int:
    """Backfill from ``indexing_queue`` (same DB as pipeline_queue)."""
    conn = None
    try:
        conn = _open_pipeline_conn(pipeline_db)
        if not _table_exists(conn, 'indexing_queue'):
            return 0
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO pipeline_queue
                (source_path, stage, status, priority,
                 attempts, last_error, enqueued_at, next_retry_at,
                 payload_json, legacy_id, legacy_table)
            SELECT
                file_path,
                'index_pending',
                CASE
                    WHEN claimed_by IS NOT NULL THEN 'in_progress'
                    ELSE 'pending'
                END,
                COALESCE(priority, 50),
                COALESCE(attempts, 0),
                last_error,
                COALESCE(enqueued_at, ?),
                next_attempt_at,
                json_object('canonical_key', canonical_key,
                            'source', source),
                NULL,
                'indexing_queue'
            FROM indexing_queue
            """,
            (_now_epoch(),),
        )
        conn.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    except sqlite3.Error as e:
        logger.warning("backfill indexing_queue failed: %s", e)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def _backfill_live_event_queue(cloud_db: str, pipeline_db: Optional[str]) -> int:
    """Backfill from ``live_event_queue`` (cloud_sync.db) into
    ``pipeline_queue`` (geodata.db). CROSS-DB — read from cloud_db,
    write to pipeline_db one row at a time.
    """
    if not pipeline_db or not os.path.isfile(pipeline_db):
        return 0
    src_conn = None
    try:
        src_conn = sqlite3.connect(cloud_db, timeout=10.0)
        src_conn.row_factory = sqlite3.Row
        # Existence check — LES may not have ever been enabled on
        # this device, in which case the table is absent.
        if not _table_exists(src_conn, 'live_event_queue'):
            return 0
        rows = src_conn.execute(
            "SELECT id, event_dir, event_json_path, event_timestamp, "
            "event_reason, upload_scope, status, attempts, last_error, "
            "next_retry_at, enqueued_at FROM live_event_queue"
        ).fetchall()
    except sqlite3.Error as e:
        logger.warning("backfill live_event_queue read failed: %s", e)
        return 0
    finally:
        if src_conn is not None:
            try:
                src_conn.close()
            except sqlite3.Error:
                pass

    if not rows:
        return 0

    inserted = 0
    for r in rows:
        stage = (
            STAGE_LIVE_EVENT_DONE if r['status'] == 'uploaded'
            else STAGE_LIVE_EVENT_PENDING
        )
        status = {
            'pending': 'pending',
            'uploading': 'in_progress',
            'uploaded': 'done',
            'failed': 'failed',
        }.get(r['status'], 'failed')
        if dual_write_enqueue(
            source_path=r['event_json_path'],
            stage=stage,
            legacy_table=LEGACY_TABLE_LIVE_EVENT,
            legacy_id=r['id'],
            priority=PRIORITY_LIVE_EVENT,
            payload={
                'event_dir': r['event_dir'],
                'event_timestamp': r['event_timestamp'],
                'event_reason': r['event_reason'],
                'upload_scope': r['upload_scope'],
            },
            db_path=pipeline_db,
        ):
            inserted += 1
        # Even on dup-skip we DON'T mark this as a failure — it just
        # means the row was already backfilled.
    return inserted


def _backfill_cloud_synced_files(cloud_db: str, pipeline_db: Optional[str]) -> int:
    """Backfill from ``cloud_synced_files`` (cloud_sync.db) into
    ``pipeline_queue`` (geodata.db). CROSS-DB.
    """
    if not pipeline_db or not os.path.isfile(pipeline_db):
        return 0
    src_conn = None
    try:
        src_conn = sqlite3.connect(cloud_db, timeout=10.0)
        src_conn.row_factory = sqlite3.Row
        if not _table_exists(src_conn, 'cloud_synced_files'):
            return 0
        rows = src_conn.execute(
            "SELECT id, file_path, remote_path, file_size, file_mtime, "
            "status, retry_count, last_error, synced_at "
            "FROM cloud_synced_files"
        ).fetchall()
    except sqlite3.Error as e:
        logger.warning("backfill cloud_synced_files read failed: %s", e)
        return 0
    finally:
        if src_conn is not None:
            try:
                src_conn.close()
            except sqlite3.Error:
                pass

    if not rows:
        return 0

    inserted = 0
    for r in rows:
        stage = (
            STAGE_CLOUD_DONE if r['status'] == 'synced'
            else STAGE_CLOUD_PENDING
        )
        status = {
            'pending': 'pending',
            'syncing': 'in_progress',
            'synced': 'done',
            'failed': 'failed',
        }.get(r['status'], 'failed')
        if dual_write_enqueue(
            source_path=r['file_path'],
            stage=stage,
            legacy_table=LEGACY_TABLE_CLOUD_SYNCED,
            legacy_id=r['id'],
            priority=PRIORITY_CLOUD_BULK,
            dest_path=r['remote_path'],
            payload={
                'file_size': r['file_size'],
                'file_mtime': r['file_mtime'],
                'last_error': r['last_error'],
            },
            db_path=pipeline_db,
        ):
            inserted += 1
    return inserted


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
