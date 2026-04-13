"""Global task coordinator for heavy background operations.

Prevents the geo-indexer, video archiver, and cloud sync from running
simultaneously on the Pi Zero 2 W (512 MB RAM, 4 cores).  Only one
heavy task runs at a time; others are skipped or queued.

Usage::

    from services.task_coordinator import acquire_task, release_task, is_busy

    if acquire_task('indexer'):
        try:
            do_heavy_work()
        finally:
            release_task('indexer')

Or as a context manager::

    with heavy_task('archive') as acquired:
        if acquired:
            do_heavy_work()
"""

import threading
import time
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_current_task: str | None = None
_task_started: float = 0.0

# Maximum time a task can hold the lock before it's considered stale
_MAX_TASK_AGE_SECONDS = 1800  # 30 minutes


def acquire_task(task_name: str) -> bool:
    """Try to become the active heavy task.

    Returns True if acquired, False if another task is running.
    Automatically clears stale locks (older than 30 minutes).
    """
    global _current_task, _task_started

    with _lock:
        if _current_task is not None:
            age = time.time() - _task_started
            if age > _MAX_TASK_AGE_SECONDS:
                logger.warning(
                    "Clearing stale task lock: %s (held for %.0fs)",
                    _current_task, age,
                )
                _current_task = None
            else:
                logger.info(
                    "Task '%s' skipped: '%s' is running (%.0fs)",
                    task_name, _current_task, age,
                )
                return False

        _current_task = task_name
        _task_started = time.time()
        logger.info("Task '%s' acquired lock", task_name)
        return True


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


def current_task_info() -> dict:
    """Return info about the currently running task (for status APIs)."""
    with _lock:
        if _current_task is None:
            return {'busy': False, 'task': None, 'elapsed': 0}
        return {
            'busy': True,
            'task': _current_task,
            'elapsed': round(time.time() - _task_started, 1),
        }


@contextmanager
def heavy_task(task_name: str):
    """Context manager for heavy tasks. Yields True if lock acquired."""
    acquired = acquire_task(task_name)
    try:
        yield acquired
    finally:
        if acquired:
            release_task(task_name)
