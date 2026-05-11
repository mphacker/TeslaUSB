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
  ``threading``, ``datetime``. Heavy libraries (cv2/av/PIL/numpy/requests)
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
import threading
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


# Module-level lock for the single-row enqueue path. SQLite itself is
# threadsafe at the connection level, but using a Python lock around
# the rowcount probe lets us return a precise ``True`` / ``False`` for
# "newly inserted vs. already present" even under concurrent producers
# without needing an explicit transaction.
_enqueue_lock = threading.Lock()


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
        with _enqueue_lock:
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
