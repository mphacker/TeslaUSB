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

Connection / transaction contract (Phase 2.10 — issue #97 item 2.10)
--------------------------------------------------------------------

Every public helper opens its own SQLite connection via
:func:`_open_archive_conn`. **The connection is opened in autocommit
mode** (``isolation_level=None``) so callers control transaction
boundaries explicitly. The contract is:

* **Single-statement reads or writes** — call ``conn.execute(...)``
  directly. Each statement auto-commits. Wrap the open/use/close
  pattern in ``try/finally`` (NOT ``with conn:``) — see below.

* **Multi-statement atomic operations** — use the
  :func:`_atomic_archive_op` context manager. It opens an autocommit
  connection, issues ``BEGIN IMMEDIATE`` (acquiring the write lock up
  front to avoid SQLITE_BUSY deadlocks), yields the connection, and
  on exit issues ``COMMIT`` on success or ``ROLLBACK`` on any
  ``BaseException`` (so ``KeyboardInterrupt`` / ``SystemExit`` also
  trigger rollback). Connection is always closed on the way out.

**Anti-pattern (do NOT use):** ``with _open_archive_conn(db_path) as conn:``.
Python's ``sqlite3`` module's ``with conn:`` is a transaction commit/
rollback context manager — but on an autocommit connection it does
NOT begin a transaction, only commits/rollbacks (which are no-ops
because each statement already committed). It also does NOT close
the connection. So that pattern is misleading on every axis: it
suggests transaction semantics that don't exist and leaks the
connection until garbage collection. Use ``try/finally`` for single-
statement work and :func:`_atomic_archive_op` for multi-statement.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, Iterable, Iterator, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Priority inference (issue #178)
# ---------------------------------------------------------------------------
#
# Lower number = more urgent. The Phase 2b worker picks rows in
# (priority ASC, expected_mtime ASC) order — the partial index
# ``archive_queue_ready`` covers exactly that ORDER BY.
#
# **Issue #178 — priority swap.** Pre-#178 the order was inverted:
# ``RECENT_CLIPS=1, EVENTS=2``. The original reasoning ("Tesla rotates
# RecentClips out after ~60 min, so they're the most urgent") was
# correct when the worker copied every RecentClip. After the SEI-peek
# skip-stationary path shipped (issue #167; made unconditional in
# issue #184 Wave 1), most RecentClips on a parked car became
# low-value skip-decisions and the priority order starved real Sentry
# events: live evidence on cybertruckusb showed 71 SentryClips events
# untouched for 130+ minutes while the worker burned its SDIO budget
# on parked-Sentry RecentClips skip decisions. Events are the
# highest-value footage (something physically happened to the car);
# RecentClips driving footage is dashcam-grade and gets the second
# tier; the SEI-peek skip-stationary path handles the parked-no-event
# case at copy time so it never competes with events for the queue
# head.
#
# A v13 schema migration (``mapping_migrations.py``) flips existing
# non-terminal rows on the first run after upgrade so the in-flight
# backlog also benefits.

PRIORITY_EVENTS = 1           # SentryClips / SavedClips — events are the highest-value footage
PRIORITY_RECENT_CLIPS = 2     # Driving / dashcam footage; SEI-peek skips parked-no-event clips
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
    'skipped_stationary',
    'error',
    'dead_letter',
)


def _infer_priority(path: str) -> int:
    """Map a TeslaCam clip path to its archive priority.

    Uses the same lowercase folder-name heuristic as
    ``indexing_queue_service.priority_for_path`` so behavior is
    consistent across the indexing and archive subsystems.

    Checks are ordered highest-priority-first to mirror the
    "lower number = picked first" semantics post-#178: events are
    checked before RecentClips. Production paths are mutually
    exclusive between the three folders, so check order only
    affects synthetic edge cases — but the explicit ordering makes
    the function self-documenting for the priority swap.
    """
    norm = (path or '').replace('\\', '/').lower()
    if '/sentryclips/' in norm or '/savedclips/' in norm:
        return PRIORITY_EVENTS
    if '/recentclips/' in norm:
        return PRIORITY_RECENT_CLIPS
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

    **Autocommit mode** (``isolation_level=None``). Each ``conn.execute(...)``
    statement commits on its own, so single-statement helpers can rely on
    immediate durability without explicit COMMIT calls.

    For multi-statement atomic operations use :func:`_atomic_archive_op`,
    which wraps the body in ``BEGIN IMMEDIATE`` / ``COMMIT`` /
    ``ROLLBACK``. **Do NOT** use ``with _open_archive_conn(db_path) as conn:``
    — Python's ``sqlite3`` ``with conn:`` is a commit/rollback context
    manager that's a no-op on autocommit AND does not close the
    connection. See the module docstring for the full contract.

    Mirrors the per-connection pragmas used by
    ``indexing_queue_service._open_queue_conn`` so producers don't
    trip over contended locks and the WAL stays small under concurrent
    writers.
    """
    conn = sqlite3.connect(db_path, timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def _atomic_archive_op(db_path: str) -> Iterator[sqlite3.Connection]:
    """Open an autocommit conn, wrap the body in an explicit transaction.

    On enter: opens the connection via :func:`_open_archive_conn`,
    issues ``BEGIN IMMEDIATE`` (acquires the write lock up front so we
    never upgrade from a shared lock mid-transaction — that's a known
    SQLITE_BUSY deadlock vector under contention).

    On normal exit: issues ``COMMIT``.

    On any exception (including ``KeyboardInterrupt`` / ``SystemExit``):
    issues ``ROLLBACK`` and re-raises so a partial multi-statement
    update never lands in the database. Rollback failures are logged
    at debug level so the original exception remains the surfaced
    cause.

    Connection is always closed on the way out (even if BEGIN itself
    failed and we never entered the body).

    Use this for any helper that issues more than one statement that
    must succeed or fail as a unit — e.g., a SELECT followed by a
    conditional UPDATE in :func:`mark_failed`. For single-statement
    helpers, call :func:`_open_archive_conn` directly and use a
    ``try/finally`` to close.
    """
    conn = _open_archive_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error as rollback_err:
                logger.debug(
                    "_atomic_archive_op ROLLBACK failed: %s",
                    rollback_err,
                )
            raise
        conn.execute("COMMIT")
    finally:
        # Always close — covers all four exit paths: clean COMMIT,
        # body-raised + ROLLBACK, BEGIN-failed (no transaction to
        # roll back), and COMMIT-failed.
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _iso_now() -> str:
    """Return an ISO-8601 UTC timestamp string (matches LES / cloud_archive)."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Lost-banner dismiss tombstone (issue: PR #169 follow-up)
# ---------------------------------------------------------------------------
#
# When the operator dismisses the home-page "Footage may have been lost"
# banner, ``delete_source_gone`` removes every existing ``source_gone``
# row — but during a major catch-up backlog the worker keeps creating
# *new* ``source_gone`` rows within seconds (1 200+ pending clips of
# which many have already aged out of Tesla's RecentClips circular
# buffer). Without a server-side tombstone the user sees the banner
# pop right back, which reads as "dismiss is broken."
#
# We persist the dismissal timestamp to a small JSON file alongside the
# other GADGET_DIR runtime state (``fsck_status.json``,
# ``chime_schedules.json``, etc.) so a subsequent ``count_source_gone``
# poll can clamp its ``claimed_at`` lower bound to ``MAX(now-24h, dismissed_at)``.
# Net effect: the banner stays hidden until brand-new losses occur
# *after* the dismissal — which is the user's actual mental model.

_LOST_DISMISSED_FILENAME = 'archive_lost_dismissed.json'


def _lost_dismissed_path() -> str:
    """Return the absolute path of the tombstone JSON file.

    Lazy-imports ``GADGET_DIR`` (matches the pattern in
    :func:`_resolve_db_path`) so this module is still safe to import
    in unit tests where ``config`` may not be on ``sys.path``. Tests
    can override the path by passing ``state_path=`` to the public
    helpers.
    """
    from config import GADGET_DIR
    return os.path.join(GADGET_DIR, _LOST_DISMISSED_FILENAME)


def get_lost_dismissed_at(*, state_path: Optional[str] = None) -> Optional[str]:
    """Return the ISO-8601 timestamp of the last banner dismissal.

    Returns ``None`` when the tombstone file is missing, malformed,
    unreadable, or contains no timestamp. Callers MUST tolerate
    ``None`` (it just means "no floor — apply the 24 h window only").
    """
    import json

    path = state_path or _lost_dismissed_path()
    try:
        if not os.path.isfile(path):
            return None
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        logger.warning("get_lost_dismissed_at: failed to read %s: %s", path, e)
        return None

    ts = data.get('dismissed_at') if isinstance(data, dict) else None
    if not isinstance(ts, str) or not ts.strip():
        return None
    return ts


def set_lost_dismissed_at(timestamp: Optional[str] = None,
                          *, state_path: Optional[str] = None) -> Optional[str]:
    """Persist a banner-dismissal timestamp; return what was written.

    Atomic-write pattern: temp file + ``os.replace`` so a crash
    mid-write can never leave a half-truncated JSON file (which
    :func:`get_lost_dismissed_at` would silently treat as "no tombstone"
    — wrong, but at least not a crash).

    Pass ``timestamp=None`` to use ``_iso_now()`` (the typical caller
    path from :func:`delete_source_gone`); pass an explicit string to
    rewrite the floor (e.g. tests, or an admin "un-dismiss" action).

    Returns the timestamp actually written, or ``None`` on a write
    failure (the caller should treat the dismissal as still effective
    in-memory but warn that the suppression won't survive a restart).
    """
    import json

    path = state_path or _lost_dismissed_path()
    ts = timestamp if timestamp is not None else _iso_now()
    tmp = path + '.tmp'
    try:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'dismissed_at': ts}, f)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
        return ts
    except OSError as e:
        logger.warning("set_lost_dismissed_at: failed to write %s: %s", path, e)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None


def _epoch_from_iso(iso: Optional[str]) -> Optional[int]:
    """Convert ISO-8601 timestamp to integer epoch seconds; return None on parse failure.

    Accepts both formats produced by :func:`_iso_now` (offset-aware
    ``+00:00``) and SQLite's own ``datetime('now')`` (naive UTC).
    Wrapped in try/except so a corrupt tombstone file can't blow up
    the caller — :func:`count_source_gone_recent` then degrades to
    "no tombstone, 24 h window only."
    """
    if not iso:
        return None
    try:
        # ``fromisoformat`` accepts both ``2026-01-02T03:04:05+00:00`` and
        # ``2026-01-02 03:04:05``; the only real-world failure mode is a
        # truncated/garbage write, in which case we want to fall back to
        # "no floor" rather than crash.
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None


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
# sees ``rowcount == 0``. Each enqueue opens its own connection via
# :func:`_open_archive_conn`, so no Python-level lock is needed to make
# the single-row return value reliable. The bulk path
# (``enqueue_many_for_archive``) uses ``conn.total_changes`` deltas
# inside an explicit ``BEGIN IMMEDIATE`` … ``COMMIT`` for the same
# reason — same guarantee, no lock.


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
    conn = None
    try:
        conn = _open_archive_conn(db_path)
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
        return cur.rowcount == 1
    except sqlite3.Error as e:
        logger.warning("enqueue_for_archive failed for %s: %s",
                       source_path, e)
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def enqueue_many_for_archive(source_paths: Iterable[str], *,
                             priority: Optional[int] = None,
                             db_path: Optional[str] = None) -> int:
    """Batch enqueue. Returns the count of newly-inserted rows.

    Same semantics as :func:`enqueue_for_archive` for each path. Empty
    paths and duplicates within ``source_paths`` are silently skipped
    (the ``source_path UNIQUE`` constraint handles duplicates atomically).

    **Transaction semantics (Phase 2.8 — issue #97).** The connection
    helper :func:`_open_archive_conn` is opened in autocommit mode
    (``isolation_level=None``) so callers control transaction boundaries
    explicitly. This function wraps the whole ``executemany`` in a
    single ``BEGIN IMMEDIATE`` … ``COMMIT`` so:

    * The whole batch lands in **one fsync**, not one per row. A
      120-file Tesla flush enqueues in ~10 ms instead of ~1.2 s
      (≈100× speedup), unblocking the producer thread quickly.
    * On any exception (SQLite error or otherwise) we ``ROLLBACK`` —
      the batch is atomic. A producer never sees a half-inserted batch.
    * ``BEGIN IMMEDIATE`` acquires the write lock up front, so we never
      upgrade from a shared lock mid-transaction (which can race other
      writers and produce ``SQLITE_BUSY`` deadlocks under load).

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
        with _atomic_archive_op(db_path) as conn:
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


def count_source_gone_recent(hours: int = 24,
                             *, db_path: Optional[str] = None,
                             dismissed_at: Optional[str] = None,
                             ignore_dismissed: bool = False) -> int:
    """Return the number of ``source_gone`` rows in the last N hours.

    Phase 4.3 (issue #101) — surface "files Tesla rotated out before we
    could copy them" as a Settings banner. The truth signal is the
    archive_queue rows that the worker terminated as ``source_gone``
    (file vanished between enqueue and copy attempt — Tesla's
    RecentClips circular buffer rolled them off).

    Uses ``claimed_at`` as the timestamp filter. Rationale: by the time
    the worker calls :func:`mark_source_gone`, ``claimed_at`` has been
    set on the row (the worker always claims a row before stat'ing the
    source), and the source-gone determination happens within seconds
    of the claim. So ``claimed_at`` is the closest proxy for "when did
    Tesla lose this clip from our perspective". The :func:`mark_source_gone`
    precondition (``WHERE status='claimed'``) guarantees ``claimed_at``
    is never NULL on a ``source_gone`` row, so the counter cannot
    silently undercount because of an out-of-flow caller.

    **Dismissal floor (PR #169 follow-up)**: if a banner-dismissal
    tombstone is set (see :func:`set_lost_dismissed_at`), the lower
    bound is clamped to ``MAX(now-hours, dismissed_at)`` so previously-
    acknowledged losses don't repopulate the count when the worker
    immediately marks more files ``source_gone`` (catch-up backlog
    burst). When ``dismissed_at`` is ``None`` and ``ignore_dismissed``
    is False, the helper looks up the tombstone via
    :func:`get_lost_dismissed_at` (default behavior — what the banner
    poll wants). Pass ``ignore_dismissed=True`` to bypass the floor
    entirely (forward-looking override for any future caller that
    wants the raw, dismissal-agnostic count — e.g. a forensic admin
    view, or wiring the Failed Jobs page through this helper instead
    of the worker-snapshot ``source_gone_count`` it currently reads).

    Cheap COUNT(*) — uses the v11 partial index
    ``archive_queue_source_gone_claimed`` (``ON archive_queue(claimed_at)
    WHERE status = 'source_gone'``), which keeps the lookup O(log n)
    even though the ``archive_queue`` table grows monotonically (no
    retention today). On a v10 DB that hasn't been re-initialized yet
    this falls back to a full table scan, which is still acceptable
    because the table is bounded by row count rather than by free
    disk space (a few thousand ``source_gone`` rows COUNT in well
    under 100 ms even on the SD card). The ``hours <= 0`` guard plus
    the silent-on-error contract make this safe to call on every
    health-card poll.

    Returns 0 if the DB is missing, the table doesn't exist yet, or any
    SQLite error fires — never raises.
    """
    if hours <= 0:
        return 0
    db_path = _resolve_db_path(db_path)

    # Resolve the dismissal floor. ``ignore_dismissed=True`` is the
    # explicit "no floor at all" override (used by Failed Jobs page);
    # otherwise an explicit ``dismissed_at`` kwarg wins over the
    # on-disk tombstone (used by tests + by future per-user dismiss).
    if ignore_dismissed:
        floor_iso = None
    elif dismissed_at is not None:
        floor_iso = dismissed_at
    else:
        try:
            floor_iso = get_lost_dismissed_at()
        except Exception:  # noqa: BLE001
            floor_iso = None
    floor_epoch = _epoch_from_iso(floor_iso)

    conn = None
    try:
        conn = _open_archive_conn(db_path)
        # ``CAST(strftime('%s', x) AS INTEGER)`` works for both ISO-8601
        # text (the format ``_iso_now`` writes — includes ``T`` and
        # ``+00:00``) and SQLite's own ``datetime('now')`` format.
        # Using integer-second arithmetic instead of julianday floats
        # avoids the precision foot-gun documented in the project-wide
        # gotchas (see ``copilot-instructions.md``).
        if floor_epoch is None:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n
                  FROM archive_queue
                 WHERE status = 'source_gone'
                   AND claimed_at IS NOT NULL
                   AND CAST(strftime('%s', claimed_at) AS INTEGER) >=
                       CAST(strftime('%s', 'now') AS INTEGER) - ?
                """,
                (int(hours) * 3600,),
            ).fetchone()
        else:
            # Apply the dismissal floor on top of the 24 h window.
            # SQLite's ``MAX()`` is a scalar across two arguments here.
            row = conn.execute(
                """
                SELECT COUNT(*) AS n
                  FROM archive_queue
                 WHERE status = 'source_gone'
                   AND claimed_at IS NOT NULL
                   AND CAST(strftime('%s', claimed_at) AS INTEGER) >=
                       MAX(
                           CAST(strftime('%s', 'now') AS INTEGER) - ?,
                           ?
                       )
                """,
                (int(hours) * 3600, floor_epoch),
            ).fetchone()
        return int(row['n'] or 0) if row else 0
    except sqlite3.Error as e:
        logger.warning("count_source_gone_recent failed: %s", e)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def delete_source_gone(*, older_than_hours: Optional[int] = None,
                       db_path: Optional[str] = None,
                       set_dismissal_tombstone: bool = True,
                       state_path: Optional[str] = None) -> int:
    """Permanently delete ``source_gone`` rows from ``archive_queue``.

    Issue #163 — companion to :func:`count_source_gone_recent`. The
    "Footage may have been lost" home-page banner counts these rows
    and is intentionally persistent for 24 h so the operator notices
    the loss; this function powers the **Dismiss** affordance on that
    banner so the operator can clear the count once they've
    acknowledged it (instead of staring at the red banner for a full
    24 h after the catch-up backlog drains).

    ``source_gone`` is **terminal** — the source clip vanished before
    the worker could copy it; there is no retry path, no downstream
    table consumes the row, and the worker never re-claims it.
    Deleting these rows therefore has zero functional impact on the
    archive worker, indexer, cloud-sync, or any other subsystem; only
    the banner number changes.

    When ``older_than_hours`` is ``None`` (the default — what the
    Dismiss button passes), every ``source_gone`` row is deleted
    regardless of age. Callers that want to preserve very old rows for
    forensics can pass an integer to delete only rows older than that
    many hours (mirrors the ``hours`` parameter of
    :func:`count_source_gone_recent`).

    **Dismissal tombstone (PR #169 follow-up)**: when called from the
    Dismiss button (``older_than_hours=None``), this also writes the
    current timestamp via :func:`set_lost_dismissed_at` BEFORE the
    DELETE so a worker burst racing the dismiss cannot repopulate the
    banner — :func:`count_source_gone_recent` will floor the window at
    that timestamp on subsequent polls. Pass
    ``set_dismissal_tombstone=False`` to skip the tombstone write
    (e.g. for forensic / older-than-hours purges that aren't user
    acknowledgments). Tombstone-write failure is non-fatal: the DELETE
    still happens; the operator just won't get the suppression
    benefit if the worker repopulates between now and the next
    successful tombstone write.

    Returns the number of rows actually deleted (``0`` if nothing
    matched). Returns ``0`` on any DB error so a UI Dismiss click
    never blows up the request handler.
    """
    db_path = _resolve_db_path(db_path)

    # Write the tombstone BEFORE the DELETE so we never race the
    # worker. Only the unconditional dismiss path (``older_than_hours
    # is None``) is treated as a user acknowledgment; an
    # ``older_than_hours``-scoped purge is a forensic operation, not a
    # banner dismiss, so we skip the tombstone there.
    if set_dismissal_tombstone and older_than_hours is None:
        try:
            set_lost_dismissed_at(state_path=state_path)
        except Exception:  # noqa: BLE001
            logger.warning("delete_source_gone: tombstone write raised; continuing")

    conn = None
    try:
        conn = _open_archive_conn(db_path)
        if older_than_hours is None:
            cur = conn.execute(
                "DELETE FROM archive_queue WHERE status = 'source_gone'"
            )
        else:
            cur = conn.execute(
                """
                DELETE FROM archive_queue
                 WHERE status = 'source_gone'
                   AND claimed_at IS NOT NULL
                   AND CAST(strftime('%s', claimed_at) AS INTEGER) <
                       CAST(strftime('%s', 'now') AS INTEGER) - ?
                """,
                (int(older_than_hours) * 3600,),
            )
        conn.commit()
        return cur.rowcount or 0
    except sqlite3.Error as e:
        logger.warning("delete_source_gone failed: %s", e)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def get_queue_status(db_path: Optional[str] = None) -> Dict[str, int]:
    """Return per-status counts for the queue.

    Always returns every key in :data:`_KNOWN_STATUSES` (zero for
    statuses that have no rows) plus a ``total`` field. Used by the
    Phase 2a observability stub and the Phase 2c watchdog.
    """
    counts: Dict[str, int] = {s: 0 for s in _KNOWN_STATUSES}
    counts['total'] = 0
    db_path = _resolve_db_path(db_path)
    conn = None
    try:
        conn = _open_archive_conn(db_path)
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
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
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
    conn = None
    try:
        conn = _open_archive_conn(db_path)
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
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


# ---------------------------------------------------------------------------
# Phase 4.1 — dead-letter inspection + manual retry (Failed Jobs page)
# ---------------------------------------------------------------------------

def list_dead_letters(limit: int = 100,
                      db_path: Optional[str] = None) -> List[Dict]:
    """Return up to ``limit`` ``dead_letter`` rows ordered oldest-first.

    Thin wrapper over :func:`list_queue` that fixes the status filter
    so the unified Failed Jobs blueprint (Phase 4.1) doesn't have to
    pass it explicitly. Sorted oldest-first by id (the order rows were
    promoted) so operators see the original failure first when
    triaging.
    """
    if limit <= 0:
        return []
    db_path = _resolve_db_path(db_path)
    conn = None
    try:
        conn = _open_archive_conn(db_path)
        cursor = conn.execute(
            """
            SELECT * FROM archive_queue
            WHERE status = 'dead_letter'
            ORDER BY id ASC
            LIMIT ?
            """,
            (int(limit),),
        )
        return [dict(r) for r in cursor.fetchall()]
    except sqlite3.Error as e:
        logger.warning("list_dead_letters failed: %s", e)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def count_dead_letters(db_path: Optional[str] = None) -> int:
    """Return the count of ``dead_letter`` rows in ``archive_queue``.

    Cheap (single ``SELECT COUNT(*)`` over the ``status`` column —
    schema indexes ``status`` in v10). Used by the unified
    ``/api/jobs/counts`` endpoint and the future status-dot poller so
    they don't have to fetch every row just to compute ``len()``.
    Returns ``0`` on any DB error so a failed count never breaks the
    aggregate page.
    """
    db_path = _resolve_db_path(db_path)
    conn = None
    try:
        conn = _open_archive_conn(db_path)
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM archive_queue "
            "WHERE status = 'dead_letter'"
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


def retry_dead_letter(row_id: Optional[int] = None,
                      db_path: Optional[str] = None) -> int:
    """Reset ``dead_letter`` rows back to ``pending`` for re-pickup.

    When ``row_id`` is given, only that one row is reset. When
    ``None``, every dead-letter row in the table is reset — useful
    after fixing a transient SD-card / namespace issue that affected
    a whole batch of failed copies.

    Resets ``attempts`` to zero and clears any stale claim so the
    worker re-picks the row on the next cycle. **Does NOT clear**
    ``last_error`` — the previous failure reason is the most useful
    triage context the operator has, and the worker will overwrite it
    on the next failure (and a successful retry leaves the row out of
    the dead-letter view anyway). Returns the number of rows actually
    reset (``0`` if nothing matched).
    """
    db_path = _resolve_db_path(db_path)
    conn = None
    try:
        conn = _open_archive_conn(db_path)
        if row_id is None:
            cur = conn.execute(
                """UPDATE archive_queue
                   SET status = 'pending', attempts = 0,
                       claimed_by = NULL,
                       claimed_at = NULL
                   WHERE status = 'dead_letter'"""
            )
        else:
            cur = conn.execute(
                """UPDATE archive_queue
                   SET status = 'pending', attempts = 0,
                       claimed_by = NULL,
                       claimed_at = NULL
                   WHERE status = 'dead_letter' AND id = ?""",
                (int(row_id),),
            )
        conn.commit()
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


def delete_dead_letter(row_id: Optional[int] = None,
                       db_path: Optional[str] = None) -> int:
    """Permanently delete ``dead_letter`` rows from ``archive_queue``.

    When ``row_id`` is given, only that one row is removed. When
    ``None``, every dead-letter row in the table is removed — the
    "Delete all" path on the Failed Jobs page (#161).

    The companion to :func:`retry_dead_letter`: same WHERE filter
    (``status = 'dead_letter'``), but ``DELETE`` instead of
    ``UPDATE``. Use when retry isn't going to help — the source file
    is permanently gone, or the row's failure reason is structural and
    re-archiving will just fail again. The inotify file watcher / boot
    catch-up scan may legitimately re-enqueue the same source path
    later (with ``attempts=0``); that's the producer doing its job
    and the user can delete again if needed.

    Returns the number of rows actually deleted (``0`` if nothing
    matched). Returns ``0`` on any DB error so a UI delete-all click
    never blows up the request handler.
    """
    db_path = _resolve_db_path(db_path)
    conn = None
    try:
        conn = _open_archive_conn(db_path)
        if row_id is None:
            cur = conn.execute(
                "DELETE FROM archive_queue WHERE status = 'dead_letter'"
            )
        else:
            cur = conn.execute(
                "DELETE FROM archive_queue "
                "WHERE status = 'dead_letter' AND id = ?",
                (int(row_id),),
            )
        conn.commit()
        return cur.rowcount or 0
    except sqlite3.Error as e:
        logger.warning("delete_dead_letter failed: %s", e)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


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
    ``priority ASC, expected_mtime ASC NULLS LAST, id ASC``. Sentry/
    Saved events (P1) drain before RecentClips (P2) which drain before
    everything else (P3) — issue #178. Within a priority band, files
    closer to Tesla's rotation deadline go first (oldest mtime).

    Args:
        claimed_by: Stamped into ``claimed_by`` for diagnostics
            (typically a thread-name + PID string).
        db_path: Override the default ``geodata.db`` path.
    """
    db_path = _resolve_db_path(db_path)
    claimed_at = _iso_now()
    # claim_next_for_worker is logically multi-statement in the
    # RETURNING-fallback branch (SELECT then conditional UPDATE), so
    # wrap the whole thing in an atomic transaction so the fallback
    # is genuinely atomic — not just relying on the conditional WHERE
    # to paper over a race.
    try:
        with _atomic_archive_op(db_path) as conn:
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
                # SELECT-then-UPDATE; the surrounding transaction
                # plus the conditional WHERE keeps the claim atomic
                # even if another worker raced us.
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
    conn = None
    try:
        conn = _open_archive_conn(db_path)
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
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def mark_source_gone(row_id: int, *,
                     db_path: Optional[str] = None) -> bool:
    """Mark a row as ``source_gone``.

    Terminal transition for the case where the source file has been
    rotated out by Tesla before we got to copy it. No retry, no
    dead-letter sidecar — this is normal behavior on RecentClips after
    ~60 minutes of no clean shutdown.

    Precondition: the row MUST already be ``claimed`` (i.e., a worker
    has called :func:`claim_next_for_worker` and the row has a
    populated ``claimed_at``). The Phase 4.3 files-lost banner relies
    on ``claimed_at`` as the timestamp filter for the 24-hour window;
    a hypothetical out-of-flow caller that marks a fresh ``pending``
    row as ``source_gone`` would silently undercount because that row
    has ``claimed_at IS NULL``. Enforcing the precondition in the
    UPDATE's ``WHERE`` clause guarantees we never produce an
    unattributable row. Returns ``False`` (rowcount == 0) if the row
    is not in ``claimed`` state, which is also what the worker
    expects when another worker has already terminated the row.
    """
    if not row_id:
        return False
    db_path = _resolve_db_path(db_path)
    conn = None
    try:
        conn = _open_archive_conn(db_path)
        cur = conn.execute(
            """
            UPDATE archive_queue
               SET status = 'source_gone',
                   last_error = NULL
             WHERE id = ?
               AND status = 'claimed'
            """,
            (int(row_id),),
        )
        return cur.rowcount == 1
    except sqlite3.Error as e:
        logger.warning("mark_source_gone failed for id=%s: %s", row_id, e)
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def mark_skipped_stationary(row_id: int, *,
                            db_path: Optional[str] = None) -> bool:
    """Mark a row as ``skipped_stationary``.

    Issue #167 sub-deliverable 2 — terminal transition for the case
    where the archive worker peeked at the source clip's SEI metadata
    and found no GPS-bearing message (no movement / no GPS lock),
    indicating the clip is overnight Sentry-while-parked footage that
    the worker unconditionally skips at source (issue #184 Wave 1
    made this behavior intrinsic — there is no longer an opt-in
    config flag). No retry, no dead-letter — the decision is final.

    Mirrors :func:`mark_source_gone` exactly: same precondition (the
    row MUST be ``claimed`` so we never produce an unattributable
    row), same return semantics (True iff the UPDATE matched),
    same error swallowing. The two terminal "we did not copy this
    clip" buckets stay parallel so observability code can report them
    side-by-side.

    The :func:`count_skipped_stationary_recent` companion uses
    ``claimed_at`` as the timestamp filter so a 24-hour Settings
    badge can show "how many clips did we save the SD card from in the
    last day". Enforcing ``status='claimed'`` here keeps that count
    accurate.
    """
    if not row_id:
        return False
    db_path = _resolve_db_path(db_path)
    conn = None
    try:
        conn = _open_archive_conn(db_path)
        cur = conn.execute(
            """
            UPDATE archive_queue
               SET status = 'skipped_stationary',
                   last_error = NULL
             WHERE id = ?
               AND status = 'claimed'
            """,
            (int(row_id),),
        )
        return cur.rowcount == 1
    except sqlite3.Error as e:
        logger.warning(
            "mark_skipped_stationary failed for id=%s: %s", row_id, e,
        )
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def count_skipped_stationary_recent(hours: int = 24,
                                    *, db_path: Optional[str] = None) -> int:
    """Return the number of ``skipped_stationary`` rows in the last N hours.

    Issue #167 sub-deliverable 2 — companion to
    :func:`mark_skipped_stationary`. Mirrors
    :func:`count_source_gone_recent` exactly so the Settings page can
    show a "stationary clips skipped in the last 24h" badge alongside
    the existing "files lost" badge.

    Uses ``claimed_at`` as the timestamp filter for the same reason
    ``count_source_gone_recent`` does: by the time the worker calls
    :func:`mark_skipped_stationary`, ``claimed_at`` has been
    populated by :func:`claim_next_for_worker`, and the skip
    determination happens in the same call. The
    :func:`mark_skipped_stationary` precondition
    (``WHERE status='claimed'``) guarantees ``claimed_at`` is never
    NULL on a ``skipped_stationary`` row, so the counter cannot
    silently undercount.

    Cheap COUNT(*) — even without a dedicated partial index the table
    is bounded and the query stays well under 100 ms on the SD card.
    Returns 0 if the DB is missing, the table doesn't exist yet, or
    any SQLite error fires — never raises.
    """
    if hours <= 0:
        return 0
    db_path = _resolve_db_path(db_path)
    conn = None
    try:
        conn = _open_archive_conn(db_path)
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
              FROM archive_queue
             WHERE status = 'skipped_stationary'
               AND claimed_at IS NOT NULL
               AND CAST(strftime('%s', claimed_at) AS INTEGER) >=
                   CAST(strftime('%s', 'now') AS INTEGER) - ?
            """,
            (int(hours) * 3600,),
        ).fetchone()
        return int(row['n'] or 0) if row else 0
    except sqlite3.Error as e:
        logger.warning("count_skipped_stationary_recent failed: %s", e)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def delete_skipped_stationary(*, older_than_hours: Optional[int] = None,
                              db_path: Optional[str] = None) -> int:
    """Permanently delete ``skipped_stationary`` rows from ``archive_queue``.

    Issue #167 sub-deliverable 2 — companion to
    :func:`mark_skipped_stationary` and mirror of
    :func:`delete_source_gone`. Used by the Settings "Clear skipped"
    affordance so the operator can wipe the running tally once they've
    acknowledged it.

    ``skipped_stationary`` is **terminal** — the source clip was
    intentionally not copied; there is no retry path, no downstream
    table consumes the row, and the worker never re-claims it.
    Deleting these rows therefore has zero functional impact on the
    archive worker, indexer, cloud-sync, or any other subsystem; only
    the Settings badge changes.

    When ``older_than_hours`` is ``None`` (the default), every
    ``skipped_stationary`` row is deleted regardless of age. Returns
    the number of rows actually deleted. Returns 0 on any DB error so
    a UI Clear click never blows up the request handler.
    """
    db_path = _resolve_db_path(db_path)
    conn = None
    try:
        conn = _open_archive_conn(db_path)
        if older_than_hours is None:
            cur = conn.execute(
                "DELETE FROM archive_queue WHERE status = 'skipped_stationary'"
            )
        else:
            cur = conn.execute(
                """
                DELETE FROM archive_queue
                 WHERE status = 'skipped_stationary'
                   AND claimed_at IS NOT NULL
                   AND CAST(strftime('%s', claimed_at) AS INTEGER) <
                       CAST(strftime('%s', 'now') AS INTEGER) - ?
                """,
                (int(older_than_hours) * 3600,),
            )
        conn.commit()
        return cur.rowcount or 0
    except sqlite3.Error as e:
        logger.warning("delete_skipped_stationary failed: %s", e)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


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
    conn = None
    try:
        conn = _open_archive_conn(db_path)
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
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


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
    # mark_failed is genuinely multi-statement: it SELECTs the current
    # attempts count, branches, then UPDATEs. Under autocommit alone
    # another writer could update ``attempts`` between our SELECT and
    # UPDATE, causing the UPDATE to use a stale base count. Wrap in
    # _atomic_archive_op so the SELECT + UPDATE land in one transaction
    # under a single write lock (BEGIN IMMEDIATE).
    try:
        with _atomic_archive_op(db_path) as conn:
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
                           previous_last_error = last_error,
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
                       previous_last_error = last_error,
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
    show event backlog separately from RecentClips/other backlogs.
    Per issue #178: P1=events, P2=RecentClips, P3=other.
    """
    counts: Dict[int, int] = {1: 0, 2: 0, 3: 0}
    db_path = _resolve_db_path(db_path)
    conn = None
    try:
        conn = _open_archive_conn(db_path)
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
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    return counts


def get_last_copied_at(db_path: Optional[str] = None) -> Optional[str]:
    """Return the ISO timestamp of the most recent successful copy.

    Used by :mod:`services.archive_watchdog` to compute staleness
    severity. Returns ``None`` when no row has been copied yet (fresh
    install, freshly-cleared queue) — the watchdog treats that as ``ok``
    when the queue is empty and the worker is running.
    """
    db_path = _resolve_db_path(db_path)
    conn = None
    try:
        conn = _open_archive_conn(db_path)
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
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def recover_stale_claims(*,
                         max_age_seconds: float = 600.0,
                         db_path: Optional[str] = None) -> int:
    """Reset ``claimed`` rows older than ``max_age_seconds`` to pending.

    Run once on worker start so a hard crash mid-copy (gadget_web killed
    during a quick_edit, OOM, power cut) doesn't leave rows stuck in
    ``claimed`` forever. Returns the count of rows recovered.
    """
    db_path = _resolve_db_path(db_path)
    conn = None
    try:
        conn = _open_archive_conn(db_path)
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
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
