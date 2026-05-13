"""
TeslaUSB Indexing Queue Service.

Owns the persistent SQLite-backed indexing queue. Producers (file watcher,
archive worker, boot catch-up, manual reindex) call ``enqueue_for_indexing``
or ``enqueue_many_for_indexing``; the single ``services.indexing_worker``
consumer drains the queue one file at a time via ``claim_next_queue_item``
and then ``complete_queue_item`` / ``defer_queue_item`` / ``release_claim``
based on the parse outcome.

Phase 3c.1 (#100): extracted from ``mapping_service.py`` to give the queue
API its own home. ``mapping_service`` keeps the indexing core itself
(``_index_video``, ``index_single_file``, ``purge_deleted_videos``,
trip-merge, event-detection, daily stale scan, boot catch-up). The two
files are read-only consumers of each other's public API:

* ``indexing_queue_service`` imports ``canonical_key`` from
  ``mapping_service`` for path → key normalization.
* ``mapping_service`` does NOT import from ``indexing_queue_service``
  (no circular dependency).

Power-loss / Pi Zero 2 W safety:

* Every write goes through ``_open_queue_conn`` which configures
  ``WAL`` + ``synchronous = NORMAL`` + ``busy_timeout = 15000``, so a
  power loss leaves either the prior or the new state — no torn rows.
* ``claim_next_queue_item`` uses ``BEGIN IMMEDIATE`` so two workers
  (or worker + inline producer) can never see the same canonical_key.
* ``complete_queue_item`` / ``release_claim`` / ``defer_queue_item``
  accept a ``(claimed_by, claimed_at)`` owner-guard pair so a stuck
  worker can never mutate a row that's been re-claimed by a fresh
  worker after the stale-claim recovery sweep.
* ``recover_stale_claims`` runs once on worker startup and clears any
  claim older than ``_STALE_CLAIM_SECONDS`` (default 30 min) so a
  crashed worker's locks don't permanently shadow rows.

See ``.github/copilot-instructions.md`` § Video Indexing for the
fairness contract with ``task_coordinator`` and the producer/consumer
discipline that prevents the constantly-flashing "Indexing…" banner.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from services.mapping_service import canonical_key

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — priority bands, retry/backoff tuning, dead-letter sentinel.
# ---------------------------------------------------------------------------

# Priority is "lower wins". Defaults reflect the plan's user-experience
# ordering: event clips first (user wants to see the incident), then
# archived trips (already on local SD, won't disappear), then recent
# clips (least urgent — Tesla's circular buffer will overwrite them
# eventually but the watcher catches them in real time).
_PRIORITY_SENTRY_SAVED = 10
_PRIORITY_ARCHIVE = 20
_PRIORITY_RECENT = 30
_PRIORITY_DEFAULT = 50

# Worker tuning — kept module-level so tests can monkeypatch them.
_PARSE_ERROR_MAX_ATTEMPTS = 3
_PARSE_ERROR_BASE_BACKOFF = 60.0
_PARSE_ERROR_MAX_BACKOFF = 3600.0
# Rows whose claim is older than this are considered orphaned (worker
# crashed mid-file) and can be released for re-attempt.
_STALE_CLAIM_SECONDS = 1800.0
# Sentinel: permanently failed rows are deferred this far into the
# future. Surfaces them in dead-letter queries via attempts column.
_DEAD_LETTER_DEFER_SECONDS = 365 * 24 * 3600.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def priority_for_path(file_path: str) -> int:
    """Map a clip path to its indexing-queue priority.

    Uses the same folder-name heuristic as ``canonical_key`` so the
    classification is consistent across producers (watcher, archive,
    catch-up) and the worker.
    """
    norm = (file_path or '').replace('\\', '/').lower()
    if '/savedclips/' in norm or '/sentryclips/' in norm:
        return _PRIORITY_SENTRY_SAVED
    if '/archivedclips/' in norm:
        return _PRIORITY_ARCHIVE
    if '/recentclips/' in norm:
        return _PRIORITY_RECENT
    return _PRIORITY_DEFAULT


def _open_queue_conn(db_path: str) -> sqlite3.Connection:
    """Open a tuned SQLite connection for queue ops.

    Mirrors the per-connection settings used by ``_init_db`` so writers
    don't trip over contended locks. Caller owns close.
    """
    conn = sqlite3.connect(db_path, timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


# ---------------------------------------------------------------------------
# Producer API
# ---------------------------------------------------------------------------


def enqueue_for_indexing(db_path: str, file_path: str, *,
                         priority: Optional[int] = None,
                         source: str = 'manual',
                         next_attempt_at: Optional[float] = None) -> bool:
    """Add or upgrade a single file in the indexing queue.

    Returns True if a row was inserted or updated, False if the file
    was rejected (path empty, canonical_key empty). Idempotent — a
    second enqueue of the same canonical_key only lowers the priority
    if the new value is more urgent.

    ``next_attempt_at`` lets producers defer the first claim atomically
    with the insert. The archive flow uses this to give the inline
    indexer a head start so the worker doesn't race the inline call
    and clobber a fresh claim. Only applied on INSERT — an existing
    row already has its own schedule that we should respect.

    Safe to call from any producer (watcher, archive, manual button).
    Never blocks the caller for I/O — the actual parse happens in the
    worker thread.
    """
    if not file_path:
        return False
    key = canonical_key(file_path)
    if not key:
        return False
    if priority is None:
        priority = priority_for_path(file_path)
    now = time.time()
    next_at = float(next_attempt_at) if next_attempt_at is not None else 0.0
    try:
        with _open_queue_conn(db_path) as conn:
            conn.execute(
                """
                INSERT INTO indexing_queue
                    (canonical_key, file_path, priority,
                     enqueued_at, next_attempt_at, source)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_key) DO UPDATE SET
                    priority = MIN(priority, excluded.priority),
                    file_path = CASE
                        WHEN claimed_by IS NULL
                            THEN excluded.file_path
                        ELSE file_path
                    END,
                    source = CASE
                        WHEN claimed_by IS NULL
                            THEN excluded.source
                        ELSE source
                    END
                """,
                (key, file_path, priority, now, next_at, source),
            )
        return True
    except sqlite3.Error as e:
        logger.warning("enqueue_for_indexing failed for %s: %s", file_path, e)
        return False


def enqueue_many_for_indexing(db_path: str,
                              items: List[Tuple[str, Optional[int]]],
                              source: str = 'catchup') -> int:
    """Batch enqueue. ``items`` is a list of ``(file_path, priority)``.

    A None priority means "use ``priority_for_path``". Returns the
    number of items that were actually written (skipping empty paths).
    Single transaction so a 200-orphan boot catch-up costs ~10 ms.
    """
    if not items:
        return 0
    now = time.time()
    rows: List[Tuple[str, str, int, float, str]] = []
    for file_path, prio in items:
        if not file_path:
            continue
        key = canonical_key(file_path)
        if not key:
            continue
        if prio is None:
            prio = priority_for_path(file_path)
        rows.append((key, file_path, prio, now, source))
    if not rows:
        return 0
    try:
        with _open_queue_conn(db_path) as conn:
            conn.executemany(
                """
                INSERT INTO indexing_queue
                    (canonical_key, file_path, priority,
                     enqueued_at, next_attempt_at, source)
                VALUES (?, ?, ?, ?, 0, ?)
                ON CONFLICT(canonical_key) DO UPDATE SET
                    priority = MIN(priority, excluded.priority),
                    file_path = CASE
                        WHEN claimed_by IS NULL
                            THEN excluded.file_path
                        ELSE file_path
                    END,
                    source = CASE
                        WHEN claimed_by IS NULL
                            THEN excluded.source
                        ELSE source
                    END
                """,
                rows,
            )
        return len(rows)
    except sqlite3.Error as e:
        logger.warning("enqueue_many_for_indexing failed: %s", e)
        return 0


# ---------------------------------------------------------------------------
# Worker API — claim / complete / defer / release / stale-recovery
# ---------------------------------------------------------------------------


def recover_stale_claims(db_path: str,
                         max_age_seconds: float = _STALE_CLAIM_SECONDS) -> int:
    """Release claims older than ``max_age_seconds``.

    Called once at worker startup so a previous crash can't permanently
    lock a row. Returns the number of claims released.
    """
    cutoff = time.time() - max_age_seconds
    try:
        with _open_queue_conn(db_path) as conn:
            cur = conn.execute(
                """
                UPDATE indexing_queue
                   SET claimed_by = NULL, claimed_at = NULL
                 WHERE claimed_by IS NOT NULL
                   AND claimed_at < ?
                """,
                (cutoff,),
            )
            released = cur.rowcount or 0
        if released:
            logger.warning(
                "Released %d stale indexing claims (>%ds old)",
                released, int(max_age_seconds),
            )
        return released
    except sqlite3.Error as e:
        logger.warning("recover_stale_claims failed: %s", e)
        return 0


def claim_next_queue_item(db_path: str,
                          worker_id: str) -> Optional[Dict[str, Any]]:
    """Atomically claim the next ready, highest-priority queue item.

    Returns a dict with the row's columns, or None if nothing is ready.
    Uses ``BEGIN IMMEDIATE`` so two workers (or worker + archive inline
    indexer) can never see the same canonical_key.

    The returned dict includes ``claimed_by`` and ``claimed_at`` (the
    timestamp at which we claimed). Pass these back to
    ``complete_queue_item`` / ``release_claim`` / ``defer_queue_item``
    as ``claimed_by=`` and ``claimed_at=`` so a stale worker can never
    mutate a row a fresh worker has re-claimed.

    "Ready" = ``claimed_by IS NULL AND next_attempt_at <= now()`` AND
    ``attempts < _PARSE_ERROR_MAX_ATTEMPTS`` (dead-letter rows are
    skipped automatically).
    """
    now = time.time()
    try:
        conn = _open_queue_conn(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT canonical_key, file_path, priority, enqueued_at,
                       next_attempt_at, attempts, last_error, source
                  FROM indexing_queue
                 WHERE claimed_by IS NULL
                   AND next_attempt_at <= ?
                   AND attempts < ?
                 ORDER BY priority ASC, enqueued_at ASC
                 LIMIT 1
                """,
                (now, _PARSE_ERROR_MAX_ATTEMPTS),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            conn.execute(
                """
                UPDATE indexing_queue
                   SET claimed_by = ?, claimed_at = ?
                 WHERE canonical_key = ?
                """,
                (worker_id, now, row['canonical_key']),
            )
            conn.execute("COMMIT")
            result = dict(row)
            # Include the claim token so the worker can pass it back as
            # an owner-guard for complete/release/defer.
            result['claimed_by'] = worker_id
            result['claimed_at'] = now
            return result
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning("claim_next_queue_item failed: %s", e)
        return None


def complete_queue_item(db_path: str, canonical_key_value: str,
                        *, claimed_by: Optional[str] = None,
                        claimed_at: Optional[float] = None) -> bool:
    """Remove a row after a terminal-outcome processing.

    Used for INDEXED, ALREADY_INDEXED, DUPLICATE_UPGRADED,
    NO_GPS_RECORDED, NOT_FRONT_CAMERA, FILE_MISSING. Returns True if a
    row was deleted.

    If ``claimed_by`` and ``claimed_at`` are provided, the delete is
    guarded so it only takes effect if the row is still owned by the
    same claim. This prevents a stuck/timed-out worker from deleting a
    row that's been re-claimed by a fresh worker. Pass them whenever
    you have them (the worker always does); omit only for catch-up /
    one-shot scripts that don't claim.
    """
    if not canonical_key_value:
        return False
    try:
        with _open_queue_conn(db_path) as conn:
            if claimed_by is None:
                cur = conn.execute(
                    "DELETE FROM indexing_queue WHERE canonical_key = ?",
                    (canonical_key_value,),
                )
            else:
                cur = conn.execute(
                    """
                    DELETE FROM indexing_queue
                     WHERE canonical_key = ?
                       AND claimed_by = ?
                       AND claimed_at = ?
                    """,
                    (canonical_key_value, claimed_by, claimed_at),
                )
            return (cur.rowcount or 0) > 0
    except sqlite3.Error as e:
        logger.warning(
            "complete_queue_item failed for %s: %s",
            canonical_key_value, e,
        )
        return False


def release_claim(db_path: str, canonical_key_value: str,
                  *, claimed_by: Optional[str] = None,
                  claimed_at: Optional[float] = None) -> bool:
    """Release a claim without progressing the row.

    Used for transient failures (DB_BUSY, worker pause/resume) where
    we want another tick — or another worker — to retry without
    incrementing ``attempts``.

    If ``claimed_by`` and ``claimed_at`` are provided, the release is
    guarded so a stale worker can't accidentally release a row
    re-claimed by a fresh worker.
    """
    if not canonical_key_value:
        return False
    try:
        with _open_queue_conn(db_path) as conn:
            if claimed_by is None:
                cur = conn.execute(
                    """
                    UPDATE indexing_queue
                       SET claimed_by = NULL, claimed_at = NULL
                     WHERE canonical_key = ?
                    """,
                    (canonical_key_value,),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE indexing_queue
                       SET claimed_by = NULL, claimed_at = NULL
                     WHERE canonical_key = ?
                       AND claimed_by = ?
                       AND claimed_at = ?
                    """,
                    (canonical_key_value, claimed_by, claimed_at),
                )
            return (cur.rowcount or 0) > 0
    except sqlite3.Error as e:
        logger.warning(
            "release_claim failed for %s: %s",
            canonical_key_value, e,
        )
        return False


def defer_queue_item(db_path: str, canonical_key_value: str,
                     next_attempt_at: float, *,
                     bump_attempts: bool = False,
                     last_error: Optional[str] = None,
                     claimed_by: Optional[str] = None,
                     claimed_at: Optional[float] = None) -> bool:
    """Reschedule a row for retry at ``next_attempt_at``.

    Used for transient outcomes (TOO_NEW, PARSE_ERROR, DB_BUSY) where
    the file might be parseable later. Releases the claim, optionally
    bumps the attempts counter (PARSE_ERROR uses this; TOO_NEW does
    not), and stamps ``last_error`` for surfacing in the dead-letter
    list.

    If ``claimed_by`` and ``claimed_at`` are provided, the update is
    guarded so a stale worker can't overwrite a row re-claimed by a
    fresh worker.
    """
    if not canonical_key_value:
        return False
    try:
        with _open_queue_conn(db_path) as conn:
            if claimed_by is None:
                if bump_attempts:
                    sql = """
                        UPDATE indexing_queue
                           SET claimed_by = NULL,
                               claimed_at = NULL,
                               next_attempt_at = ?,
                               attempts = attempts + 1,
                               last_error = ?
                         WHERE canonical_key = ?
                    """
                else:
                    sql = """
                        UPDATE indexing_queue
                           SET claimed_by = NULL,
                               claimed_at = NULL,
                               next_attempt_at = ?,
                               last_error = ?
                         WHERE canonical_key = ?
                    """
                cur = conn.execute(
                    sql, (next_attempt_at, last_error, canonical_key_value),
                )
            else:
                if bump_attempts:
                    sql = """
                        UPDATE indexing_queue
                           SET claimed_by = NULL,
                               claimed_at = NULL,
                               next_attempt_at = ?,
                               attempts = attempts + 1,
                               last_error = ?
                         WHERE canonical_key = ?
                           AND claimed_by = ?
                           AND claimed_at = ?
                    """
                else:
                    sql = """
                        UPDATE indexing_queue
                           SET claimed_by = NULL,
                               claimed_at = NULL,
                               next_attempt_at = ?,
                               last_error = ?
                         WHERE canonical_key = ?
                           AND claimed_by = ?
                           AND claimed_at = ?
                    """
                cur = conn.execute(
                    sql,
                    (next_attempt_at, last_error,
                     canonical_key_value, claimed_by, claimed_at),
                )
        if (cur.rowcount or 0) == 0:
            return False
        return True
    except sqlite3.Error as e:
        logger.warning(
            "defer_queue_item failed for %s: %s",
            canonical_key_value, e,
        )
        return False


def compute_backoff(attempts: int) -> float:
    """Exponential backoff with cap. Pure function — easy to unit test.

    ``attempts`` is the *failure count BEFORE this one* (so the first
    retry waits ``BASE``, the second waits ``2*BASE``, etc.).
    """
    if attempts < 0:
        attempts = 0
    delay = _PARSE_ERROR_BASE_BACKOFF * (2 ** attempts)
    return min(delay, _PARSE_ERROR_MAX_BACKOFF)


# ---------------------------------------------------------------------------
# Status / cleanup API
# ---------------------------------------------------------------------------


def get_queue_status(db_path: str) -> Dict[str, Any]:
    """Snapshot of queue health for the /api/index/status endpoint.

    Returns ``{queue_depth, claimed_count, dead_letter_count,
    next_ready_at, last_error}``. Cheap (single SQL with aggregates).
    """
    try:
        with _open_queue_conn(db_path) as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN claimed_by IS NULL
                              AND attempts < ?
                             THEN 1 ELSE 0 END) AS queue_depth,
                    SUM(CASE WHEN claimed_by IS NOT NULL
                             THEN 1 ELSE 0 END) AS claimed_count,
                    SUM(CASE WHEN attempts >= ?
                             THEN 1 ELSE 0 END) AS dead_letter_count,
                    MIN(CASE WHEN claimed_by IS NULL
                              AND attempts < ?
                             THEN next_attempt_at END) AS next_ready_at
                  FROM indexing_queue
                """,
                (_PARSE_ERROR_MAX_ATTEMPTS,
                 _PARSE_ERROR_MAX_ATTEMPTS,
                 _PARSE_ERROR_MAX_ATTEMPTS),
            ).fetchone()
        return {
            'queue_depth': int(row['queue_depth'] or 0),
            'claimed_count': int(row['claimed_count'] or 0),
            'dead_letter_count': int(row['dead_letter_count'] or 0),
            'next_ready_at': float(row['next_ready_at'])
                              if row['next_ready_at'] is not None else None,
        }
    except sqlite3.Error as e:
        logger.warning("get_queue_status failed: %s", e)
        return {
            'queue_depth': 0,
            'claimed_count': 0,
            'dead_letter_count': 0,
            'next_ready_at': None,
            'error': str(e),
        }


def clear_pending_queue(db_path: str) -> int:
    """Remove only **unclaimed** rows from the indexing queue.

    Used by ``/api/index/cancel`` so the currently-claimed file (if
    any) is allowed to finish — its claim row stays in the table until
    the worker's owner-guarded delete on completion. Returns the count
    of rows actually removed.
    """
    try:
        with _open_queue_conn(db_path) as conn:
            cur = conn.execute(
                "DELETE FROM indexing_queue WHERE claimed_by IS NULL"
            )
            return cur.rowcount or 0
    except sqlite3.Error as e:
        logger.warning("clear_pending_queue failed: %s", e)
        return 0


def clear_all_queue(db_path: str) -> int:
    """Remove every row from the indexing queue, including claimed ones.

    Used by the manual "Rebuild map index (advanced)" action **after**
    the worker has been paused — otherwise the worker may be mid-INSERT
    into waypoints/detected_events for a row this delete would erase
    out from under it. Callers MUST pause the worker first. Returns
    count of rows removed.
    """
    try:
        with _open_queue_conn(db_path) as conn:
            cur = conn.execute("DELETE FROM indexing_queue")
            return cur.rowcount or 0
    except sqlite3.Error as e:
        logger.warning("clear_all_queue failed: %s", e)
        return 0


# Backward-compat alias — same dangerous semantics as the original
# (deletes claimed rows). New code should pick one of the two above.
clear_queue = clear_all_queue
