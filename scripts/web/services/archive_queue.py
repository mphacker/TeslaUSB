"""TeslaUSB Archive Queue — Phase 2a producer-side API (issue #76).

Persistent SQLite-backed queue of clips waiting to be copied from the
USB RO mount (RecentClips / SentryClips / SavedClips) to the SD-card
``ArchivedClips/`` directory. Lives in ``geodata.db`` alongside
``indexing_queue`` so a single migration / backup story covers both.

**Phase 2a is producer-only.** Three independent producers populate the
queue (the inotify file watcher, a 60-second full-directory rescan, and
a boot catch-up scan); nothing drains it yet. Rows accumulate harmlessly
until the Phase 2b worker lands. The Phase 2c watchdog + observability
endpoints will read the same queue.

Design constraints (Pi Zero 2 W, 512 MB RAM):

* **Lightweight imports only** — ``os``, ``sqlite3``, ``logging``,
  ``datetime``. Heavy libraries (cv2/av/PIL/numpy/requests)
  must never enter this module.
* **One connection per call** — every public function opens its own
  SQLite connection so the API is thread-safe by construction. No shared
  module-level connection.
* **Idempotent enqueue** — every producer can fire the same path many
  times; ``INSERT OR IGNORE`` on the ``source_path UNIQUE`` constraint
  makes this O(1) and lock-free at the application layer.
* **Best-effort metadata** — ``expected_size`` / ``expected_mtime`` are
  captured via ``os.stat()``; if the stat fails (file already rotated,
  permission denied, RO mount transiently gone) the row is still
  inserted with NULL metadata so the Phase 2b worker can decide what to
  do (it will detect ``source_gone`` on the actual copy attempt).

Public API
----------

* :func:`enqueue_for_archive` — single path, returns True iff a new row
  was inserted.
* :func:`enqueue_many_for_archive` — batch variant, returns the count
  of newly-inserted rows.
* :func:`get_queue_status` — counts per status (used by the Phase 2a
  observability stub and the Phase 2c watchdog).
* :func:`list_queue` — inspection helper for tests and Phase 2c UI.

The schema itself lives in :mod:`services.mapping_service` (the
``archive_queue`` CREATE statement is part of ``_SCHEMA_SQL`` so the
v9 → v10 migration creates it automatically). This module only reads
and writes rows.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Priority inference (from the issue spec)
# ---------------------------------------------------------------------------
#
# Lower number = more urgent. The Phase 2b worker picks rows in
# (priority ASC, expected_mtime ASC) order — the partial index
# ``archive_queue_ready`` covers exactly that ORDER BY.

PRIORITY_RECENT_CLIPS = 1     # Tesla rotates these out after ~60 min — highest urgency
PRIORITY_EVENTS = 2           # SentryClips / SavedClips — user wants these soon
PRIORITY_OTHER = 3            # Default for anything else (e.g. ArchivedClips back-fill)

# Status values stored in the ``status`` column. Phase 2a only ever
# writes ``pending``; the rest exist so :func:`get_queue_status` can
# return zeros for them today and the Phase 2b worker can use them
# without another migration.
_KNOWN_STATUSES = (
    'pending',
    'claimed',
    'copied',
    'source_gone',
    'error',
    'dead_letter',
)


def _infer_priority(path: str) -> int:
    """Map a TeslaCam clip path to its archive priority.

    Uses the same lowercase folder-name heuristic as
    ``mapping_service.priority_for_path`` so behavior is consistent
    across the indexing and archive subsystems.
    """
    norm = (path or '').replace('\\', '/').lower()
    if '/recentclips/' in norm:
        return PRIORITY_RECENT_CLIPS
    if '/sentryclips/' in norm or '/savedclips/' in norm:
        return PRIORITY_EVENTS
    return PRIORITY_OTHER


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _resolve_db_path(db_path: Optional[str]) -> str:
    """Return ``db_path`` if given, otherwise the default mapping DB.

    ``MAPPING_DB_PATH`` is computed from ``GADGET_DIR`` so it's only
    meaningful inside the Flask app process. We import lazily so this
    module is still safe to import in unit tests where ``config`` may
    not be on the path.
    """
    if db_path:
        return db_path
    from config import MAPPING_DB_PATH
    return MAPPING_DB_PATH


def _open_archive_conn(db_path: str) -> sqlite3.Connection:
    """Open a tuned SQLite connection for archive_queue ops.

    Mirrors the per-connection pragmas used by
    ``mapping_service._open_queue_conn`` so producers don't trip over
    contended locks and the WAL stays small under concurrent writers.
    """
    conn = sqlite3.connect(db_path, timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _iso_now() -> str:
    """Return an ISO-8601 UTC timestamp string (matches LES / cloud_archive)."""
    return datetime.now(timezone.utc).isoformat()


def _safe_stat(path: str):
    """Return ``os.stat`` result or None on failure.

    Producers use this so a transient stat failure (file rotated mid-
    enqueue, RO mount remounted, permission denied) does not raise out
    of the producer thread. The row is still inserted with NULL
    ``expected_size`` / ``expected_mtime`` and the Phase 2b worker will
    re-stat and dispatch (likely to ``source_gone``).
    """
    try:
        return os.stat(path)
    except OSError:
        return None


# SQLite's connection-level threadsafety guarantees that ``cur.rowcount``
# after ``INSERT OR IGNORE`` reflects only that cursor's own outcome:
# the winning connection sees ``rowcount == 1``, the losing connection
# sees ``rowcount == 0``. Each enqueue opens its own connection (via
# the ``_open_archive_conn`` context manager), so no Python-level lock
# is needed to make the single-row return value reliable. The bulk
# path (``enqueue_many_for_archive``) uses ``conn.total_changes`` for
# the same reason — same guarantee, no lock.


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enqueue_for_archive(source_path: str, *,
                        priority: Optional[int] = None,
                        db_path: Optional[str] = None) -> bool:
    """Idempotently enqueue ``source_path`` for archival.

    Returns ``True`` if a new row was inserted, ``False`` if the path
    was already in the queue (any status) or was rejected (empty path).

    Args:
        source_path: Absolute path under the RO USB mount (or a test
            fixture path). Must be non-empty.
        priority: Override the inferred priority. ``None`` (default)
            means infer from the path: 1 for RecentClips, 2 for
            SentryClips/SavedClips, 3 otherwise.
        db_path: Override the default ``geodata.db`` path. ``None``
            (default) resolves via :data:`config.MAPPING_DB_PATH`.

    Behavior on failure:
      * Empty / falsy ``source_path`` → return False, log nothing.
      * ``os.stat`` failure → row is still inserted with NULL
        ``expected_size`` / ``expected_mtime``. The worker's stat
        gate will catch the missing file later.
      * SQLite error → return False, log a warning. Producer threads
        keep running.
    """
    if not source_path:
        return False
    if priority is None:
        priority = _infer_priority(source_path)
    db_path = _resolve_db_path(db_path)
    st = _safe_stat(source_path)
    expected_size = st.st_size if st is not None else None
    expected_mtime = st.st_mtime if st is not None else None
    enqueued_at = _iso_now()
    try:
        with _open_archive_conn(db_path) as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO archive_queue
                    (source_path, priority, status,
                     enqueued_at, expected_size, expected_mtime)
                VALUES (?, ?, 'pending', ?, ?, ?)
                """,
                (source_path, int(priority), enqueued_at,
                 expected_size, expected_mtime),
            )
            inserted = cur.rowcount == 1
        return inserted
    except sqlite3.Error as e:
        logger.warning("enqueue_for_archive failed for %s: %s",
                       source_path, e)
        return False


def enqueue_many_for_archive(source_paths: Iterable[str], *,
                             priority: Optional[int] = None,
                             db_path: Optional[str] = None) -> int:
    """Batch enqueue. Returns the count of newly-inserted rows.

    Same semantics as :func:`enqueue_for_archive` for each path; uses a
    single SQLite transaction so a 200-file boot catch-up costs ~10 ms.
    Empty paths and duplicates within ``source_paths`` are silently
    skipped (the UNIQUE constraint handles duplicates atomically).

    Args:
        source_paths: Iterable of absolute paths.
        priority: Force the same priority for every path. ``None``
            (default) means infer per-path via :func:`_infer_priority`.
        db_path: Override the default ``geodata.db`` path.
    """
    paths = [p for p in source_paths if p]
    if not paths:
        return 0
    db_path = _resolve_db_path(db_path)
    enqueued_at = _iso_now()
    rows = []
    for p in paths:
        prio = priority if priority is not None else _infer_priority(p)
        st = _safe_stat(p)
        rows.append((
            p,
            int(prio),
            enqueued_at,
            st.st_size if st is not None else None,
            st.st_mtime if st is not None else None,
        ))
    try:
        with _open_archive_conn(db_path) as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO archive_queue
                    (source_path, priority, status,
                     enqueued_at, expected_size, expected_mtime)
                VALUES (?, ?, 'pending', ?, ?, ?)
                """,
                rows,
            )
            after = conn.total_changes
        return max(0, after - before)
    except sqlite3.Error as e:
        logger.warning("enqueue_many_for_archive failed: %s", e)
        return 0


def get_queue_status(db_path: Optional[str] = None) -> Dict[str, int]:
    """Return per-status counts for the queue.

    Always returns every key in :data:`_KNOWN_STATUSES` (zero for
    statuses that have no rows) plus a ``total`` field. Used by the
    Phase 2a observability stub and the Phase 2c watchdog.
    """
    counts: Dict[str, int] = {s: 0 for s in _KNOWN_STATUSES}
    counts['total'] = 0
    db_path = _resolve_db_path(db_path)
    try:
        with _open_archive_conn(db_path) as conn:
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM archive_queue GROUP BY status"
            ).fetchall():
                status = row['status'] or 'pending'
                n = int(row['n'] or 0)
                # Fold any unknown status (shouldn't happen, but be
                # defensive — older rows after a downgrade, manual SQL,
                # etc.) into the total but not into the named buckets.
                if status in counts:
                    counts[status] = n
                counts['total'] += n
    except sqlite3.Error as e:
        logger.warning("get_queue_status failed: %s", e)
    return counts


def list_queue(limit: int = 50,
               status: Optional[str] = None,
               db_path: Optional[str] = None) -> List[Dict]:
    """Return up to ``limit`` rows for inspection.

    Sorted by (priority ASC, expected_mtime ASC NULLS LAST, id ASC) so
    the head of the list matches the order the worker will pick rows.
    ``status`` is an optional exact-match filter; when ``None`` every
    status is returned.

    Returns a list of plain dicts so callers can JSON-serialize without
    fussing over ``sqlite3.Row``.
    """
    if limit <= 0:
        return []
    db_path = _resolve_db_path(db_path)
    try:
        with _open_archive_conn(db_path) as conn:
            if status is not None:
                cursor = conn.execute(
                    """
                    SELECT * FROM archive_queue
                    WHERE status = ?
                    ORDER BY priority ASC,
                             expected_mtime IS NULL,
                             expected_mtime ASC,
                             id ASC
                    LIMIT ?
                    """,
                    (status, int(limit)),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT * FROM archive_queue
                    ORDER BY priority ASC,
                             expected_mtime IS NULL,
                             expected_mtime ASC,
                             id ASC
                    LIMIT ?
                    """,
                    (int(limit),),
                )
            return [dict(r) for r in cursor.fetchall()]
    except sqlite3.Error as e:
        logger.warning("list_queue failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Worker-side helpers (Phase 2b — consumed by ``archive_worker``)
# ---------------------------------------------------------------------------
#
# These helpers wrap the state-transition SQL the Phase 2b worker needs
# so the SQL stays in this module (alongside the producer-side queries)
# and the worker stays a thin loop.
#
# State machine:
#
#                 pending  <----release_claim------+
#                    |                              |
#                    v                              |
#               (claim_next_for_worker)             |
#                    |                              |
#                    v                              |
#                 claimed --(error, attempts<max)---+
#                    |
#         +----------+----------+--------------------+
#         |          |          |                    |
#         v          v          v                    v
#       copied  source_gone  dead_letter         (retry-able error)
#       (final)   (final)     (final)
#
# All transitions go through these helpers so the worker can be reviewed
# in isolation without grepping for stray UPDATE statements.


def claim_next_for_worker(claimed_by: str, *,
                          db_path: Optional[str] = None) -> Optional[Dict]:
    """Atomically claim the next ready row for the worker.

    Returns the claimed row as a plain ``dict`` (so the worker can
    serialize it without ``sqlite3.Row``), or ``None`` if the queue is
    empty.

    The pick-and-claim is an atomic ``UPDATE ... WHERE status='pending'``
    using ``RETURNING *`` (SQLite ≥ 3.35), which lets two workers race
    safely: only one wins each row. We fall back to the older
    SELECT-then-UPDATE-with-rowcount pattern if the SQLite build is
    missing RETURNING.

    The pick order matches the partial index ``archive_queue_ready``:
    ``priority ASC, expected_mtime ASC NULLS LAST, id ASC``. RecentClips
    (P1) drains before Sentry/Saved (P2) which drains before everything
    else (P3); within a priority band, files closer to Tesla's rotation
    deadline go first (oldest mtime).

    Args:
        claimed_by: Stamped into ``claimed_by`` for diagnostics
            (typically a thread-name + PID string).
        db_path: Override the default ``geodata.db`` path.
    """
    db_path = _resolve_db_path(db_path)
    claimed_at = _iso_now()
    try:
        with _open_archive_conn(db_path) as conn:
            try:
                cur = conn.execute(
                    """
                    UPDATE archive_queue
                       SET status = 'claimed',
                           claimed_at = ?,
                           claimed_by = ?
                     WHERE id = (
                        SELECT id FROM archive_queue
                         WHERE status = 'pending'
                         ORDER BY priority ASC,
                                  expected_mtime IS NULL,
                                  expected_mtime ASC,
                                  id ASC
                         LIMIT 1
                     )
                       AND status = 'pending'
                    RETURNING *
                    """,
                    (claimed_at, claimed_by),
                )
                row = cur.fetchone()
                if row is not None:
                    return dict(row)
                return None
            except sqlite3.OperationalError:
                # Older SQLite — no RETURNING clause. Fall back to
                # SELECT-then-UPDATE; the conditional WHERE keeps the
                # claim atomic even if another worker raced us.
                pass

            row = conn.execute(
                """
                SELECT * FROM archive_queue
                 WHERE status = 'pending'
                 ORDER BY priority ASC,
                          expected_mtime IS NULL,
                          expected_mtime ASC,
                          id ASC
                 LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            cur = conn.execute(
                """
                UPDATE archive_queue
                   SET status = 'claimed',
                       claimed_at = ?,
                       claimed_by = ?
                 WHERE id = ? AND status = 'pending'
                """,
                (claimed_at, claimed_by, row['id']),
            )
            if cur.rowcount != 1:
                return None
            claimed = dict(row)
            claimed['status'] = 'claimed'
            claimed['claimed_at'] = claimed_at
            claimed['claimed_by'] = claimed_by
            return claimed
    except sqlite3.Error as e:
        logger.warning("claim_next_for_worker failed: %s", e)
        return None


def mark_copied(row_id: int, dest_path: str, *,
                db_path: Optional[str] = None) -> bool:
    """Mark a claimed row as successfully copied.

    Terminal transition. Sets ``status='copied'``, fills ``copied_at``
    and ``dest_path``, and clears ``last_error``. Returns True iff a
    row was updated (False on SQLite error or unknown id).
    """
    if not row_id:
        return False
    db_path = _resolve_db_path(db_path)
    copied_at = _iso_now()
    try:
        with _open_archive_conn(db_path) as conn:
            cur = conn.execute(
                """
                UPDATE archive_queue
                   SET status = 'copied',
                       copied_at = ?,
                       dest_path = ?,
                       last_error = NULL
                 WHERE id = ?
                """,
                (copied_at, dest_path, int(row_id)),
            )
            return cur.rowcount == 1
    except sqlite3.Error as e:
        logger.warning("mark_copied failed for id=%s: %s", row_id, e)
        return False


def mark_source_gone(row_id: int, *,
                     db_path: Optional[str] = None) -> bool:
    """Mark a row as ``source_gone``.

    Terminal transition for the case where the source file has been
    rotated out by Tesla before we got to copy it. No retry, no
    dead-letter sidecar — this is normal behavior on RecentClips after
    ~60 minutes of no clean shutdown.
    """
    if not row_id:
        return False
    db_path = _resolve_db_path(db_path)
    try:
        with _open_archive_conn(db_path) as conn:
            cur = conn.execute(
                """
                UPDATE archive_queue
                   SET status = 'source_gone',
                       last_error = NULL
                 WHERE id = ?
                """,
                (int(row_id),),
            )
            return cur.rowcount == 1
    except sqlite3.Error as e:
        logger.warning("mark_source_gone failed for id=%s: %s", row_id, e)
        return False


def release_claim(row_id: int, *,
                  expected_size: Optional[int] = None,
                  expected_mtime: Optional[float] = None,
                  db_path: Optional[str] = None) -> bool:
    """Release a claim back to ``pending`` without burning an attempt.

    Used in three places:
      * The "fully written" stable-mtime gate — when the source file is
        still being written by Tesla, we requeue it and try again on
        the next iteration.
      * Lock contention — if ``task_coordinator.acquire_task`` times
        out, we shouldn't penalize the row for our own scheduling.
      * Pause/stop — when the worker shuts down mid-claim, the row
        goes back to ``pending`` so the next worker (or restart) can
        re-claim it cleanly.

    Optionally refreshes ``expected_size`` / ``expected_mtime`` so the
    next pick-and-claim sees the latest stat() values.
    """
    if not row_id:
        return False
    db_path = _resolve_db_path(db_path)
    try:
        with _open_archive_conn(db_path) as conn:
            if expected_size is not None or expected_mtime is not None:
                cur = conn.execute(
                    """
                    UPDATE archive_queue
                       SET status = 'pending',
                           claimed_at = NULL,
                           claimed_by = NULL,
                           expected_size = COALESCE(?, expected_size),
                           expected_mtime = COALESCE(?, expected_mtime)
                     WHERE id = ?
                    """,
                    (expected_size, expected_mtime, int(row_id)),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE archive_queue
                       SET status = 'pending',
                           claimed_at = NULL,
                           claimed_by = NULL
                     WHERE id = ?
                    """,
                    (int(row_id),),
                )
            return cur.rowcount == 1
    except sqlite3.Error as e:
        logger.warning("release_claim failed for id=%s: %s", row_id, e)
        return False


def mark_failed(row_id: int, error: str, *,
                max_attempts: int = 3,
                db_path: Optional[str] = None) -> str:
    """Record a failed attempt; transition to dead_letter at the cap.

    Returns the new status (``'pending'`` if attempts remain, or
    ``'dead_letter'`` once ``attempts >= max_attempts``). On SQLite
    failure returns ``'error'`` and leaves the row alone — the caller
    should release the claim and let the next iteration retry.

    ``last_error`` is truncated to 4 KB so a runaway exception trace
    can't bloat the DB.
    """
    if not row_id:
        return 'error'
    db_path = _resolve_db_path(db_path)
    truncated = (error or '')[:4096]
    try:
        with _open_archive_conn(db_path) as conn:
            row = conn.execute(
                "SELECT attempts FROM archive_queue WHERE id = ?",
                (int(row_id),),
            ).fetchone()
            if row is None:
                return 'error'
            new_attempts = int(row['attempts'] or 0) + 1
            if new_attempts >= int(max_attempts):
                conn.execute(
                    """
                    UPDATE archive_queue
                       SET status = 'dead_letter',
                           attempts = ?,
                           last_error = ?,
                           claimed_at = NULL,
                           claimed_by = NULL
                     WHERE id = ?
                    """,
                    (new_attempts, truncated, int(row_id)),
                )
                return 'dead_letter'
            conn.execute(
                """
                UPDATE archive_queue
                   SET status = 'pending',
                       attempts = ?,
                       last_error = ?,
                       claimed_at = NULL,
                       claimed_by = NULL
                 WHERE id = ?
                """,
                (new_attempts, truncated, int(row_id)),
            )
            return 'pending'
    except sqlite3.Error as e:
        logger.warning("mark_failed failed for id=%s: %s", row_id, e)
        return 'error'


def get_pending_counts_by_priority(db_path: Optional[str] = None) -> Dict[int, int]:
    """Return a mapping of ``priority -> pending_row_count``.

    Always includes the canonical priorities (1, 2, 3) in the result so
    callers don't need to deal with missing keys. Phase 2c surfaces this
    in ``/api/archive/status`` as ``queue_depth_p1/p2/p3`` so the UI can
    show RecentClips backlog separately from event/other backlogs.
    """
    counts: Dict[int, int] = {1: 0, 2: 0, 3: 0}
    db_path = _resolve_db_path(db_path)
    try:
        with _open_archive_conn(db_path) as conn:
            for row in conn.execute(
                """
                SELECT priority, COUNT(*) AS n FROM archive_queue
                 WHERE status = 'pending'
                 GROUP BY priority
                """
            ).fetchall():
                prio = int(row['priority'] or 3)
                counts[prio] = int(row['n'] or 0)
    except sqlite3.Error as e:
        logger.warning("get_pending_counts_by_priority failed: %s", e)
    return counts


def get_last_copied_at(db_path: Optional[str] = None) -> Optional[str]:
    """Return the ISO timestamp of the most recent successful copy.

    Used by :mod:`services.archive_watchdog` to compute staleness
    severity. Returns ``None`` when no row has been copied yet (fresh
    install, freshly-cleared queue) — the watchdog treats that as ``ok``
    when the queue is empty and the worker is running.
    """
    db_path = _resolve_db_path(db_path)
    try:
        with _open_archive_conn(db_path) as conn:
            row = conn.execute(
                """
                SELECT MAX(copied_at) AS m FROM archive_queue
                 WHERE status = 'copied' AND copied_at IS NOT NULL
                """
            ).fetchone()
            if row is None:
                return None
            return row['m']
    except sqlite3.Error as e:
        logger.warning("get_last_copied_at failed: %s", e)
        return None


def recover_stale_claims(*,
                         max_age_seconds: float = 600.0,
                         db_path: Optional[str] = None) -> int:
    """Reset ``claimed`` rows older than ``max_age_seconds`` to pending.

    Run once on worker start so a hard crash mid-copy (gadget_web killed
    during a quick_edit, OOM, power cut) doesn't leave rows stuck in
    ``claimed`` forever. Returns the count of rows recovered.
    """
    db_path = _resolve_db_path(db_path)
    try:
        with _open_archive_conn(db_path) as conn:
            # Compare ISO-8601 strings lexicographically — works
            # because they're all UTC and same format.
            cutoff = datetime.now(timezone.utc).timestamp() - max_age_seconds
            cutoff_iso = datetime.fromtimestamp(
                cutoff, tz=timezone.utc).isoformat()
            cur = conn.execute(
                """
                UPDATE archive_queue
                   SET status = 'pending',
                       claimed_at = NULL,
                       claimed_by = NULL
                 WHERE status = 'claimed'
                   AND (claimed_at IS NULL OR claimed_at < ?)
                """,
                (cutoff_iso,),
            )
            return cur.rowcount or 0
    except sqlite3.Error as e:
        logger.warning("recover_stale_claims failed: %s", e)
        return 0
