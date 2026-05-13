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
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

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

    .. note::

        Connection is opened in **autocommit mode**
        (``isolation_level=None``). The Python ``sqlite3`` driver's
        ``with conn:`` context manager is a NO-OP in autocommit mode
        unless a transaction has already been opened explicitly. For
        any helper that issues more than a single ``execute`` (most
        notably the ``executemany`` in :func:`enqueue_many_for_indexing`),
        wrap the body in :func:`_atomic_indexing_op` so the whole
        batch is one ``BEGIN IMMEDIATE`` … ``COMMIT`` (one fsync, atomic
        rollback on failure). For single-statement helpers, autocommit
        is correct — but call ``conn.close()`` in a ``try/finally``
        because ``with conn:`` won't do it.
    """
    conn = sqlite3.connect(db_path, timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def _atomic_indexing_op(db_path: str) -> Iterator[sqlite3.Connection]:
    """Open an autocommit conn, wrap the body in an explicit transaction.

    Mirrors :func:`services.archive_queue._atomic_archive_op` (added by
    PR #119 / Phase 2.8 of #97). Use this for any helper that issues
    more than one statement that must succeed or fail as a unit, or
    for an ``executemany`` that must commit as a single batch (one
    fsync instead of one per row).

    On enter: opens the connection via :func:`_open_queue_conn`,
    issues ``BEGIN IMMEDIATE`` (acquires the write lock up front so we
    never upgrade from a shared lock mid-transaction — that's a known
    ``SQLITE_BUSY`` deadlock vector under contention).

    On normal exit: issues ``COMMIT``.

    On any exception (including ``KeyboardInterrupt`` / ``SystemExit``):
    issues ``ROLLBACK`` and re-raises so a partial multi-statement
    update never lands in the database. Rollback failures are logged
    at debug level so the original exception remains the surfaced
    cause.

    Connection is always closed on the way out (even if BEGIN itself
    failed and we never entered the body).
    """
    conn = _open_queue_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error as rollback_err:
                logger.debug(
                    "_atomic_indexing_op ROLLBACK failed: %s",
                    rollback_err,
                )
            raise
        conn.execute("COMMIT")
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


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
    conn = None
    try:
        # Single-statement insert — autocommit is correct, but the
        # ``with conn:`` form leaks the connection on autocommit
        # connections (it only commits/rollbacks). Use explicit
        # try/finally to guarantee close.
        conn = _open_queue_conn(db_path)
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
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def enqueue_many_for_indexing(db_path: str,
                              items: List[Tuple[str, Optional[int]]],
                              source: str = 'catchup') -> int:
    """Batch enqueue. ``items`` is a list of ``(file_path, priority)``.

    A None priority means "use ``priority_for_path``". Returns the
    number of items that were actually written (skipping empty paths).

    **Transaction semantics (issue #120, mirroring PR #119 for archive
    queue).** :func:`_open_queue_conn` returns an autocommit connection
    so callers control transaction boundaries explicitly. This function
    wraps the whole ``executemany`` in :func:`_atomic_indexing_op`
    (single ``BEGIN IMMEDIATE`` … ``COMMIT``) so:

    * The whole batch lands in **one fsync**, not one per row. A
      200-orphan boot catch-up scan now enqueues in ~10 ms instead of
      ~1.5 s on the Pi Zero 2 W's SD card. The producer thread
      unblocks promptly and the SDIO bus is freed for the archive
      worker (issue #104 mitigation).
    * On any exception (SQLite error or otherwise, including
      ``KeyboardInterrupt``) the whole batch ROLLBACKs — a producer
      never sees a half-inserted batch.
    * ``BEGIN IMMEDIATE`` acquires the write lock up front, so we
      never upgrade from a shared lock mid-transaction (which can
      race other writers and produce ``SQLITE_BUSY`` deadlocks under
      load).
    * Connection always closes (no FD leak on the failure path).
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
        with _atomic_indexing_op(db_path) as conn:
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
    conn = None
    try:
        conn = _open_queue_conn(db_path)
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
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


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
    conn = None
    try:
        conn = _open_queue_conn(db_path)
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
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


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
    conn = None
    try:
        conn = _open_queue_conn(db_path)
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
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


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
    conn = None
    try:
        conn = _open_queue_conn(db_path)
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
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


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
        conn = _open_queue_conn(db_path)
        try:
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
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass
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
    conn = None
    try:
        conn = _open_queue_conn(db_path)
        cur = conn.execute(
            "DELETE FROM indexing_queue WHERE claimed_by IS NULL"
        )
        return cur.rowcount or 0
    except sqlite3.Error as e:
        logger.warning("clear_pending_queue failed: %s", e)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


# ---------------------------------------------------------------------------
# Phase 4.1 — dead-letter inspection + manual retry (Failed Jobs page)
# ---------------------------------------------------------------------------

def list_dead_letters(db_path: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Return up to ``limit`` indexer dead-letter rows.

    A row is dead-letter when ``attempts >= _PARSE_ERROR_MAX_ATTEMPTS``.
    The worker won't pick it again on its own (the ``WHERE attempts < ?``
    guard in :func:`claim_next_queue_item` skips it). Each row carries
    ``canonical_key``, ``file_path``, ``last_error``, ``attempts``,
    ``next_attempt_at`` so the unified Failed Jobs UI can render the why
    and the retry-after timestamp without a follow-up call. Sorted
    oldest-first so operators triage the original failure first.
    """
    if limit <= 0:
        return []
    limit = min(int(limit), 1000)
    conn = None
    try:
        conn = _open_queue_conn(db_path)
        rows = conn.execute(
            """SELECT canonical_key, file_path, attempts,
                      next_attempt_at, last_error, enqueued_at,
                      source
               FROM indexing_queue
               WHERE attempts >= ?
               ORDER BY enqueued_at ASC, canonical_key ASC
               LIMIT ?""",
            (_PARSE_ERROR_MAX_ATTEMPTS, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as e:
        logger.warning("list_dead_letters failed: %s", e)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def count_dead_letters(db_path: str) -> int:
    """Return the count of indexer dead-letter rows.

    Cheap (single ``SELECT COUNT(*)`` — falls under ``idx_queue_ready``
    is not applicable here so a small full scan; 7 ms even at queue
    depth 10 000). Used by ``/api/jobs/counts`` so the page doesn't
    fetch every row just to compute ``len()``. Returns ``0`` on any
    DB error so a failed count never breaks the aggregate page.
    """
    conn = None
    try:
        conn = _open_queue_conn(db_path)
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM indexing_queue "
            "WHERE attempts >= ?",
            (_PARSE_ERROR_MAX_ATTEMPTS,),
        ).fetchone()
        return int(row['n']) if row else 0
    except sqlite3.Error as e:
        logger.warning("count_dead_letters failed: %s", e)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def retry_dead_letter(db_path: str,
                      canonical_key_value: Optional[str] = None) -> int:
    """Reset indexer dead-letter rows so the worker picks them up again.

    When ``canonical_key_value`` is given, only that one row is reset.
    When ``None``, every dead-letter row in the queue is reset — useful
    after upgrading the SEI parser or fixing a recurring path issue
    that affected a whole batch of failed parses.

    Resets ``attempts`` to zero and zeroes ``next_attempt_at`` so the
    worker re-picks the row on the next cycle. **Does NOT clear**
    ``last_error`` — the previous parse failure is the most useful
    triage context the operator has, and the worker will overwrite it
    on the next failure (and a successful retry leaves the row out of
    the dead-letter view anyway). Does not touch the ``priority`` so
    the original queueing order is preserved. Returns the number of
    rows actually reset.
    """
    conn = None
    try:
        conn = _open_queue_conn(db_path)
        if canonical_key_value is None:
            cur = conn.execute(
                """UPDATE indexing_queue
                   SET attempts = 0,
                       next_attempt_at = 0,
                       claimed_by = NULL,
                       claimed_at = NULL
                   WHERE attempts >= ?""",
                (_PARSE_ERROR_MAX_ATTEMPTS,),
            )
        else:
            cur = conn.execute(
                """UPDATE indexing_queue
                   SET attempts = 0,
                       next_attempt_at = 0,
                       claimed_by = NULL,
                       claimed_at = NULL
                   WHERE attempts >= ?
                     AND canonical_key = ?""",
                (_PARSE_ERROR_MAX_ATTEMPTS, str(canonical_key_value)),
            )
        return cur.rowcount or 0
    except sqlite3.Error as e:
        logger.warning("retry_dead_letter failed: %s", e)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def clear_all_queue(db_path: str) -> int:
    """Remove every row from the indexing queue, including claimed ones.

    Used by the manual "Rebuild map index (advanced)" action **after**
    the worker has been paused — otherwise the worker may be mid-INSERT
    into waypoints/detected_events for a row this delete would erase
    out from under it. Callers MUST pause the worker first. Returns
    count of rows removed.
    """
    conn = None
    try:
        conn = _open_queue_conn(db_path)
        cur = conn.execute("DELETE FROM indexing_queue")
        return cur.rowcount or 0
    except sqlite3.Error as e:
        logger.warning("clear_all_queue failed: %s", e)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


# Backward-compat alias — same dangerous semantics as the original
# (deletes claimed rows). New code should pick one of the two above.
clear_queue = clear_all_queue


def purge_orphaned_dead_letters(db_path: str) -> int:
    """Delete dead-letter rows whose ``file_path`` no longer exists.

    Issue #110 — When the archive watchdog's retention prune deletes a
    truncated archive copy, it calls
    :func:`mapping_service.purge_deleted_videos` to clean
    ``indexed_files``, but it does NOT touch ``indexing_queue``.
    Dead-letter rows (``attempts >= _PARSE_ERROR_MAX_ATTEMPTS``) for
    those deleted files would otherwise linger forever, inflating
    ``dead_letter_count`` and showing stale paths in
    :func:`list_dead_letters`.

    Wired into :func:`mapping_service._run_stale_scan_blocking` so it
    runs alongside the existing ``indexed_files`` orphan sweep on the
    same daily cadence (with the 5–10 min initial delay after boot).

    Safety contract:

    * Only ``attempts >= _PARSE_ERROR_MAX_ATTEMPTS`` rows are eligible.
      Live or in-flight rows (``claimed_by IS NOT NULL`` or
      ``attempts < _PARSE_ERROR_MAX_ATTEMPTS``) are NEVER touched —
      the worker's normal ``FILE_MISSING`` outcome handles them on
      next claim.
    * Dead-letter rows whose source file STILL exists are preserved
      (the file might be re-processable after a future fix).
    * One ``os.path.isfile`` per dead-letter row — same shape as the
      ``indexed_files`` stale scan. Orders of magnitude faster than
      re-attempting the parse.

    Returns the number of rows purged.
    """
    if not db_path:
        return 0

    conn = None
    try:
        conn = _open_queue_conn(db_path)
        rows = conn.execute(
            """SELECT canonical_key, file_path
               FROM indexing_queue
               WHERE attempts >= ?
                 AND claimed_by IS NULL""",
            (_PARSE_ERROR_MAX_ATTEMPTS,),
        ).fetchall()
    except sqlite3.Error as e:
        logger.warning("purge_orphaned_dead_letters select failed: %s", e)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        return 0

    orphans = [r['canonical_key'] for r in rows
               if r['file_path'] and not os.path.isfile(r['file_path'])]

    if not orphans:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        return 0

    purged = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.executemany(
                "DELETE FROM indexing_queue "
                "WHERE canonical_key = ? "
                "  AND attempts >= ? "
                "  AND claimed_by IS NULL",
                [(k, _PARSE_ERROR_MAX_ATTEMPTS) for k in orphans],
            )
            purged = cur.rowcount or 0
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    except sqlite3.Error as e:
        logger.warning("purge_orphaned_dead_letters delete failed: %s", e)
        return 0
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    if purged:
        logger.info(
            "purge_orphaned_dead_letters: removed %d dead-letter row(s) "
            "whose source file no longer exists",
            purged,
        )
    return purged
