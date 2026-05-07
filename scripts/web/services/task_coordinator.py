"""Global task coordinator for heavy background operations.

Prevents the geo-indexer, video archiver, and cloud sync from running
simultaneously on the Pi Zero 2 W (512 MB RAM, 4 cores).  Only one
heavy task runs at a time; others are skipped or queued.

Usage::

    from services.task_coordinator import acquire_task, release_task, is_busy

    # Cyclic task that should yield to less frequent priority tasks.
    if acquire_task('indexer', yield_to_waiters=True):
        try:
            do_heavy_work()
        finally:
            release_task('indexer')

    # Less frequent task that needs to wait for a slot.
    if acquire_task('archive', wait_seconds=60.0):
        try:
            do_heavy_work()
        finally:
            release_task('archive')

Or as a context manager::

    with heavy_task('archive', wait_seconds=60.0) as acquired:
        if acquired:
            do_heavy_work()

Fairness model
--------------
A "waiter count" tracks how many tasks are currently blocking inside
``acquire_task(..., wait_seconds>0)``.  Cyclic tasks (the indexer)
that pass ``yield_to_waiters=True`` will refuse to take the lock if
any other task is waiting for it.  This prevents the indexer's
acquire/release cycle (~1 Hz with ~0.25 s gaps) from starving the
archive's 5-minute timer — a real production issue that caused
TeslaCam clips to be lost when Tesla rotated RecentClips before the
archive could grab the lock.
"""

import threading
import time
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_current_task: str | None = None
_task_started: float = 0.0

# Number of callers currently waiting inside ``acquire_task`` with
# ``wait_seconds>0``. Used by the fairness short-circuit so cyclic
# tasks can yield to priority tasks. Guarded by ``_lock``.
_waiter_count: int = 0

# How often a waiting caller polls the lock. Kept short so a blocked
# task (e.g. archive) can grab the slot during the indexer's brief
# inter-file gap (currently 0.25 s). Must be < indexer's
# ``_INTER_FILE_SLEEP_SECONDS`` for the fairness short-circuit to be
# the primary win mechanism rather than relying on lucky timing.
_WAIT_POLL_SECONDS = 0.1

# Maximum time a task can hold the lock before it's considered stale
_MAX_TASK_AGE_SECONDS = 1800  # 30 minutes


def acquire_task(task_name: str, wait_seconds: float = 0.0,
                 *, yield_to_waiters: bool = False) -> bool:
    """Try to become the active heavy task.

    By default, returns immediately: True if acquired, False if another
    non-stale task is already running (preserves the original
    fire-and-forget contract).

    ``wait_seconds`` (>0): block up to this many seconds for the lock
    to become available, polling every ``_WAIT_POLL_SECONDS``. While
    waiting, this caller is counted in the waiter tally so cyclic
    tasks with ``yield_to_waiters=True`` will refuse to acquire and
    let us in. Returns True on success, False on timeout.

    ``yield_to_waiters`` (True): refuse to acquire if any other task is
    currently inside ``acquire_task`` waiting for the lock. Used by
    the indexer so its tight acquire/release cycle does not starve the
    less frequent archive/cloud-sync tasks. Only takes effect when
    ``wait_seconds <= 0`` — a caller that is itself waiting for the
    lock cannot also yield to other waiters (it would yield to itself
    on every poll). For priority tasks that need to block, omit
    ``yield_to_waiters``.

    Stale locks (held longer than ``_MAX_TASK_AGE_SECONDS``) are
    cleared automatically.
    """
    global _current_task, _task_started, _waiter_count

    deadline = time.monotonic() + max(0.0, wait_seconds)
    am_waiting = False
    # Honour the documented contract: yield_to_waiters is only meaningful
    # for non-blocking acquisitions. A blocking caller cannot yield to
    # itself on every poll cycle.
    effective_yield = yield_to_waiters and wait_seconds <= 0

    try:
        while True:
            with _lock:
                # Fairness: cyclic tasks yield to priority waiters.
                if effective_yield and _waiter_count > 0:
                    return False

                # Existing task lock check + stale clear.
                if _current_task is not None:
                    age = time.time() - _task_started
                    if age > _MAX_TASK_AGE_SECONDS:
                        logger.warning(
                            "Clearing stale task lock: %s (held for %.0fs)",
                            _current_task, age,
                        )
                        _current_task = None

                if _current_task is None:
                    _current_task = task_name
                    _task_started = time.time()
                    if am_waiting:
                        _waiter_count = max(0, _waiter_count - 1)
                        am_waiting = False
                    logger.info("Task '%s' acquired lock", task_name)
                    return True

                # Lock is held. Decide whether to wait or give up now.
                if wait_seconds <= 0:
                    logger.info(
                        "Task '%s' skipped: '%s' is running (%.0fs)",
                        task_name, _current_task, age,
                    )
                    return False

                # Register as a waiter on first failed attempt so other
                # cyclic callers will yield to us during their next
                # acquire. Guarded by _lock; no double-counting.
                if not am_waiting:
                    _waiter_count += 1
                    am_waiting = True
                held_task = _current_task
                held_age = age
                # fall through to sleep outside the lock

            if time.monotonic() >= deadline:
                logger.info(
                    "Task '%s' giving up after %.1fs wait "
                    "(held by '%s' for %.0fs)",
                    task_name, wait_seconds, held_task, held_age,
                )
                return False

            time.sleep(_WAIT_POLL_SECONDS)
    finally:
        if am_waiting:
            with _lock:
                _waiter_count = max(0, _waiter_count - 1)


def release_task(task_name: str) -> None:
    """Release the heavy-task lock."""
    global _current_task

    with _lock:
        if _current_task == task_name:
            elapsed = time.time() - _task_started
            _current_task = None
            logger.info("Task '%s' released lock (%.1fs)", task_name, elapsed)
        else:
            logger.warning(
                "Task '%s' tried to release but '%s' holds the lock",
                task_name, _current_task,
            )


def is_busy() -> bool:
    """Return True if any heavy task is currently running."""
    with _lock:
        if _current_task is None:
            return False
        age = time.time() - _task_started
        if age > _MAX_TASK_AGE_SECONDS:
            return False  # stale, will be cleared on next acquire
        return True


def waiter_count() -> int:
    """Return the number of callers currently waiting for the lock.

    Exposed for status APIs and for tests that need to assert the
    fairness mechanism is engaged. Reads under the lock for an
    accurate snapshot.
    """
    with _lock:
        return _waiter_count


def current_task_info() -> dict:
    """Return info about the currently running task (for status APIs)."""
    with _lock:
        if _current_task is None:
            return {'busy': False, 'task': None, 'elapsed': 0,
                    'waiters': _waiter_count}
        return {
            'busy': True,
            'task': _current_task,
            'elapsed': round(time.time() - _task_started, 1),
            'waiters': _waiter_count,
        }


@contextmanager
def heavy_task(task_name: str, wait_seconds: float = 0.0,
               *, yield_to_waiters: bool = False):
    """Context manager for heavy tasks. Yields True if lock acquired.

    See :func:`acquire_task` for the semantics of ``wait_seconds`` and
    ``yield_to_waiters``.
    """
    acquired = acquire_task(
        task_name, wait_seconds, yield_to_waiters=yield_to_waiters,
    )
    try:
        yield acquired
    finally:
        if acquired:
            release_task(task_name)
