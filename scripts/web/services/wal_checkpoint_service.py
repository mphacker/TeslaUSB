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
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import List, Optional

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

    Design note (PR #187 Info #8): we deliberately open a fresh
    ``sqlite3.connect()`` per tick per DB rather than caching a
    module-level connection. Per-tick cost on a Pi Zero 2 W is ~5 ms
    × 2 DBs × every 30 s ≈ 0.05 % CPU — negligible. The benefit of
    fresh connections is that we carry no long-lived state across
    DB-file lifecycle events: a future feature that swaps
    ``geodata.db`` after a corruption-recovery import (or the v15
    migration's table-rebuild path) cannot leave us holding a stale
    file descriptor. If profiling ever shows this loop as hot, cache
    a per-DB connection and add a "rebind on file mtime change"
    invalidation hook — but until then the simpler design wins.
    """
    if not db_path or not os.path.isfile(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        conn.execute("PRAGMA mmap_size=0")
        conn.execute("PRAGMA cache_size=-256")  # 256 KB — checkpoint reads are streaming
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
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
        # Don't escalate — the next tick will retry. This is a
        # bookkeeping optimization; errors here never affect
        # correctness.
        logger.debug(
            "wal_checkpoint: %s sqlite error: %s",
            os.path.basename(db_path), e,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "wal_checkpoint: unexpected failure on %s: %s", db_path, e,
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


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
    process.
    """
    global _started
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=timeout)
    _started = False


def is_running() -> bool:
    """Return True if the daemon thread is alive."""
    return _thread is not None and _thread.is_alive()


def _trigger_for_test(db_path: str) -> None:
    """Synchronous checkpoint of one DB. Test-only entry point."""
    _checkpoint_one(db_path)
