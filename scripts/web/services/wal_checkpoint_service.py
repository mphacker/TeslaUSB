"""Idle-time WAL checkpoint service (issue #184 Wave 3 — Phase G).

SQLite WAL mode batches write transactions into ``geodata.db-wal``,
then runs ``PRAGMA wal_checkpoint`` to fold them back into the main
DB file. With ``wal_autocheckpoint=200`` (set in
:mod:`services.mapping_migrations`) auto-checkpoints fire every
~800 KB — but they fire **inline** with whatever transaction crosses
the threshold. Under sustained queue churn that means the checkpoint
lands in the middle of an archive copy, fighting the SDIO bus with
the worker.

This service runs ``PRAGMA wal_checkpoint(TRUNCATE)`` opportunistic-
ally during idle windows (no other heavy task holds the
:mod:`services.task_coordinator` lock) so the checkpoint cost lands
when the system has nothing else to do. Pre-empting the auto-
checkpoint at idle reduces (but does not eliminate) the inline
checkpoints.

The thread is a daemon; it never blocks shutdown. It pauses
unconditionally when ``task_coordinator.is_busy()`` is true OR any
task is waiting in ``acquire_task`` (``waiter_count() > 0``). It
does NOT acquire the coordinator lock itself — checkpointing is a
read-mostly bookkeeping operation that runs alongside any reader,
and grabbing the lock would mask the indexer/archive workers'
fairness signals.

Configuration is via constants below; no user-facing knobs. The
30-second cadence and TRUNCATE mode are calibrated to land < 50 ms
checkpoints on a Pi Zero 2 W when the WAL is small.

Connection caching (issue #189): the per-DB SQLite connection is
opened once and reused across ticks. Each tick re-stats the DB
file under the cache lock and re-opens the connection if the
inode/device changed. ``check_same_thread=False`` + a per-
connection ``threading.Lock`` lets the daemon thread and the
synchronous test-only ``_trigger_for_test`` share the cached
handle safely. Any sqlite error during a checkpoint evicts the
cached connection so the next tick re-opens a fresh one —
defensive against transient I/O failures leaving a dead handle in
the cache forever.

Inode-invalidation rationale: the in-tree ``api_index_rebuild`` is
an in-place row deletion (the geodata.db file's inode is
preserved), so the invalidation hook is **purely defensive** —
covering hypothetical future code paths that might swap the DB
file (corruption-recovery import, repair-from-backup) without
having to remember to bounce the daemon. The hook is also a
correctness guard for the test suite, which routinely creates
fresh DBs in tmpdirs and re-uses the module-level cache state.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import Dict, List, NamedTuple, Optional

logger = logging.getLogger(__name__)


_CHECKPOINT_INTERVAL_SECONDS = 30.0
_BUSY_BACKOFF_SECONDS = 5.0
_MAX_RETRIES_PER_TICK = 1
_LOG_NONZERO_THRESHOLD_PAGES = 10


_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_state_lock = threading.Lock()
_db_paths: List[str] = []
_started = False


# ---------------------------------------------------------------------------
# Cached SQLite connections (issue #189).
# ---------------------------------------------------------------------------


class _CachedConn(NamedTuple):
    """Cache entry for one DB. ``ino``/``dev`` are the file-identity
    snapshot taken when the connection was opened; the next tick
    invalidates the cache if either changes (i.e. the DB file was
    recreated under us). ``lock`` serialises concurrent access from
    the daemon thread and the test-only ``_trigger_for_test``."""

    conn: sqlite3.Connection
    ino: int
    dev: int
    lock: threading.Lock


_conn_cache: Dict[str, _CachedConn] = {}
_conn_cache_lock = threading.Lock()


def _get_or_open_cached_conn(db_path: str) -> Optional[_CachedConn]:
    """Return a cached connection for ``db_path``, opening or
    re-opening it if the file's inode/device changed since the last
    open (DB recreated by rebuild/recovery/test fixture).

    Returns ``None`` if the path is missing or the open fails — the
    caller treats it as a skip, identical to the pre-#189 behaviour.

    Concurrency contract:

    * The ``os.stat()`` MUST run under ``_conn_cache_lock`` so the
      ``cached.ino == cur_ino`` comparison is consistent (the prior
      design did the stat outside the lock and risked a TOCTOU
      window where two threads disagreed about the file identity,
      potentially causing tick-by-tick re-open thrash if a file
      replacement raced the cache lock).
    * ``sqlite3.connect()`` is called OUTSIDE ``_conn_cache_lock``
      because it can block up to ``timeout=2.0`` s on a contended
      WAL — holding the cache lock that long would freeze a
      concurrent ``stop()`` / ``_evict_cached_conn`` call.
    * After a successful open, we re-acquire the cache lock and CAS
      the new entry in. If another thread raced ahead and registered
      a different connection for the same identity, we close ours
      and return theirs — the cache stays single-keyed on the path.
    * Stale entries are closed under their per-conn lock OUTSIDE
      the cache lock so a long-running ``conn.close()`` doesn't
      block other lookups.

    On Linux (production target) ``st_ino`` is the canonical file
    identity; on Windows NTFS (developer machines / CI) ``st_ino``
    is the file index, which is also stable across renames. We
    capture ``st_dev`` too so a same-inode collision across
    different mounts/devices doesn't fool the invalidation check.
    """
    # Phase 1: stat + cache check, both under the cache lock for
    # internal consistency. Fast path returns the cached entry
    # without any I/O outside the lock.
    with _conn_cache_lock:
        try:
            st = os.stat(db_path)
        except OSError:
            return None
        cur_ino, cur_dev = st.st_ino, st.st_dev
        cached = _conn_cache.get(db_path)
        if cached is not None and cached.ino == cur_ino \
                and cached.dev == cur_dev:
            return cached
        # Either missing entry or stale entry — drop the stale one
        # NOW so concurrent callers also see "missing" and walk the
        # open path (rather than returning a soon-to-be-closed
        # cached entry). Close the stale handle AFTER releasing the
        # cache lock to avoid holding it across conn.close().
        stale = cached
        if stale is not None:
            _conn_cache.pop(db_path, None)
            logger.info(
                "wal_checkpoint: %s inode changed "
                "(was ino=%s dev=%s, now ino=%s dev=%s); "
                "re-opening cached connection",
                os.path.basename(db_path),
                stale.ino, stale.dev, cur_ino, cur_dev,
            )

    # Phase 2: close the stale handle outside the cache lock, under
    # the stale entry's per-conn lock so a checkpoint mid-flight on
    # the same handle finishes cleanly first.
    if stale is not None:
        with stale.lock:
            try:
                stale.conn.close()
            except Exception:  # noqa: BLE001
                pass

    # Phase 3: open the new connection outside the cache lock —
    # sqlite3.connect() can block up to 2 s on a contended WAL.
    try:
        conn = sqlite3.connect(
            db_path, timeout=2.0, check_same_thread=False,
        )
        conn.execute("PRAGMA mmap_size=0")
        conn.execute("PRAGMA cache_size=-256")
    except sqlite3.Error as e:
        logger.warning(
            "wal_checkpoint: could not open cached connection "
            "to %s: %s",
            os.path.basename(db_path), e,
        )
        return None

    new_cached = _CachedConn(
        conn=conn, ino=cur_ino, dev=cur_dev,
        lock=threading.Lock(),
    )

    # Phase 4: CAS-register. Another thread may have opened a
    # connection for the same path while we were in Phase 3; the
    # cache stays single-keyed by closing the loser.
    loser_cached: Optional[_CachedConn] = None
    we_lost = False
    with _conn_cache_lock:
        existing = _conn_cache.get(db_path)
        if existing is not None and existing.ino == cur_ino \
                and existing.dev == cur_dev:
            # Lost the race — another thread registered first.
            we_lost = True
            keeper = existing
        else:
            # We won — register ours; if there's another (stale)
            # entry, it loses and gets closed below.
            loser_cached = existing
            _conn_cache[db_path] = new_cached
            keeper = new_cached

    # Close the loser outside the cache lock.
    if we_lost:
        # Our just-opened conn was never published, no per-conn
        # lock to acquire.
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    elif loser_cached is not None:
        # We won and displaced an existing-but-stale entry; close
        # under its per-conn lock so any in-flight checkpoint on
        # the loser finishes cleanly first.
        with loser_cached.lock:
            try:
                loser_cached.conn.close()
            except Exception:  # noqa: BLE001
                pass

    return keeper


def _evict_cached_conn(db_path: str) -> None:
    """Drop the cached connection for ``db_path`` and close it.

    Called from :func:`_checkpoint_one`'s error handler so a
    transient sqlite failure (locked, IO error, etc.) doesn't leave
    a wedged connection in the cache. The next tick will re-open
    fresh.

    The close is performed under the entry's per-connection lock so
    a concurrent checkpoint on the same handle finishes cleanly
    first (the lock is uncontended in production — only the daemon
    thread runs ``_checkpoint_one`` — but tests routinely run
    ``_trigger_for_test`` from the main thread alongside a started
    daemon, and we don't want a half-closed FD getting passed to
    ``execute()``).
    """
    with _conn_cache_lock:
        cached = _conn_cache.pop(db_path, None)
    if cached is not None:
        with cached.lock:
            try:
                cached.conn.close()
            except Exception:  # noqa: BLE001
                pass


def _close_all_cached_conns() -> None:
    """Close every cached connection. Called from :func:`stop`.

    Each close is performed under the entry's per-connection lock
    so any in-flight checkpoint on the daemon thread (after a
    timed-out ``stop()`` join) finishes cleanly before the close
    races it.
    """
    with _conn_cache_lock:
        entries = list(_conn_cache.items())
        _conn_cache.clear()
    for _db_path, cached in entries:
        with cached.lock:
            try:
                cached.conn.close()
            except Exception:  # noqa: BLE001
                pass


def _is_coordinator_idle() -> bool:
    """Return True if no heavy task holds the coordinator lock and no
    task is waiting in ``acquire_task``.

    Defensive against the coordinator module not being importable in
    a degraded runtime — returns False (back off) rather than risk
    competing for I/O.
    """
    try:
        from services import task_coordinator  # local import to keep startup cheap
    except Exception:  # noqa: BLE001
        return False
    try:
        if task_coordinator.is_busy():
            return False
        if task_coordinator.waiter_count() > 0:
            return False
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("wal_checkpoint: coordinator probe failed: %s", e)
        return False


def _checkpoint_one(db_path: str) -> None:
    """Run ``PRAGMA wal_checkpoint(TRUNCATE)`` against ``db_path``.

    Logs at INFO only when the checkpoint actually folded data
    (``checkpointed >= _LOG_NONZERO_THRESHOLD_PAGES``) so a quiescent
    system doesn't fill the journal. Connection is opened with the
    same conservative pragmas as
    :func:`services.mapping_migrations._init_db` so we don't re-mmap
    or grow the page cache.

    Issue #189: the SQLite connection is now cached across ticks
    via :func:`_get_or_open_cached_conn`. The inode-change check
    invalidates the cache if the DB file is replaced under us. In
    the current codebase, ``api_index_rebuild`` is in-place row
    deletion (the DB file's inode is preserved), so the
    invalidation hook is **purely defensive** — it covers
    hypothetical future swap-the-DB-file paths (corruption-
    recovery import, repair-from-backup) and acts as a correctness
    guard for the test suite, which routinely creates and deletes
    DBs in tmpdirs and reuses the module-level cache state. Any
    sqlite error during the checkpoint evicts the cached entry so
    the next tick re-opens fresh.
    """
    if not db_path or not os.path.isfile(db_path):
        return
    cached = _get_or_open_cached_conn(db_path)
    if cached is None:
        return
    try:
        with cached.lock:
            row = cached.conn.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
        if row is not None:
            busy, log_pages, checkpointed = row[0], row[1], row[2]
            if checkpointed and checkpointed >= _LOG_NONZERO_THRESHOLD_PAGES:
                logger.info(
                    "wal_checkpoint: %s busy=%s log_pages=%s checkpointed=%s",
                    os.path.basename(db_path), busy, log_pages, checkpointed,
                )
            elif busy:
                logger.debug(
                    "wal_checkpoint: %s busy=%s (skipped)",
                    os.path.basename(db_path), busy,
                )
    except sqlite3.Error as e:
        # Evict the cached connection so the next tick re-opens a
        # fresh one — defensive against the connection being left
        # in a wedged state by a transient I/O error.
        _evict_cached_conn(db_path)
        logger.debug(
            "wal_checkpoint: %s sqlite error (cached conn evicted): %s",
            os.path.basename(db_path), e,
        )
    except Exception as e:  # noqa: BLE001
        _evict_cached_conn(db_path)
        logger.warning(
            "wal_checkpoint: unexpected failure on %s "
            "(cached conn evicted): %s", db_path, e,
        )


def _run_loop() -> None:
    """Daemon loop. Sleeps ``_CHECKPOINT_INTERVAL_SECONDS`` between
    ticks; each tick checkpoints every registered DB only if the
    coordinator is idle, otherwise waits ``_BUSY_BACKOFF_SECONDS``
    and retries up to ``_MAX_RETRIES_PER_TICK`` times before giving
    up until the next tick.
    """
    logger.info(
        "wal_checkpoint_service started (interval=%.0fs, dbs=%s)",
        _CHECKPOINT_INTERVAL_SECONDS,
        [os.path.basename(p) for p in _db_paths],
    )
    while not _stop_event.is_set():
        # Sleep first — gives gadget_web boot time to settle before
        # the first checkpoint hits a freshly-migrated DB.
        if _stop_event.wait(_CHECKPOINT_INTERVAL_SECONDS):
            break
        attempted = False
        for retry in range(_MAX_RETRIES_PER_TICK + 1):
            if _is_coordinator_idle():
                attempted = True
                break
            if retry < _MAX_RETRIES_PER_TICK:
                if _stop_event.wait(_BUSY_BACKOFF_SECONDS):
                    return
        if not attempted:
            continue
        with _state_lock:
            paths_snapshot = list(_db_paths)
        for db_path in paths_snapshot:
            if _stop_event.is_set():
                return
            _checkpoint_one(db_path)
    logger.info("wal_checkpoint_service stopped")


def start(db_paths: List[str]) -> bool:
    """Start the daemon thread. Idempotent — second call is a no-op.

    ``db_paths`` is the list of SQLite DBs to checkpoint each tick.
    Non-existent paths are silently skipped at tick time so the
    caller can pass DBs that may be created later (e.g. a fresh
    ``cloud_sync.db`` that doesn't exist on first boot).
    """
    global _thread, _started
    with _state_lock:
        if _started and _thread is not None and _thread.is_alive():
            return False
        _stop_event.clear()
        _db_paths.clear()
        for p in db_paths:
            if p and p not in _db_paths:
                _db_paths.append(p)
        _thread = threading.Thread(
            target=_run_loop,
            name="wal_checkpoint_service",
            daemon=True,
        )
        _thread.start()
        _started = True
        return True


def stop(timeout: float = 5.0) -> None:
    """Signal the loop to exit and join up to ``timeout`` seconds.

    Used by tests; in production the daemon thread dies with the
    process. Always closes every cached SQLite connection so a
    test-driven start/stop cycle leaks no FDs.
    """
    global _started
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=timeout)
    _started = False
    _close_all_cached_conns()


def is_running() -> bool:
    """Return True if the daemon thread is alive."""
    return _thread is not None and _thread.is_alive()


def _trigger_for_test(db_path: str) -> None:
    """Synchronous checkpoint of one DB. Test-only entry point."""
    _checkpoint_one(db_path)

