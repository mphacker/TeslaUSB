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

# Throttle window for the "Task X skipped: Y is running" INFO log.
# A cyclic caller (e.g. the indexer) hits the skip path every
# ``_BACKOFF_SLEEP_SECONDS`` (≈ 0.5 s); without throttling, a long
# archive run produced ~2 logs/sec for ~11 min during a production
# boot catchup (issue #72). One log per (task, blocker) pair per minute
# is plenty — the log shows you a contention is in progress, not a
# blow-by-blow account.
#
# Issue #172: the throttle map is NEVER cleared on successful acquire.
# The previous design did clear it (the theory being "new contention
# period deserves a fresh INFO"), but on a backlogged device the
# indexer↔archive interleave at sub-second granularity → every short
# acquire wiped the throttle → spam returned. The fix is to let
# entries naturally age out via the per-pair 60s window in
# ``_should_log_skipped``. A genuinely new (task, blocker) pair has no
# entry in the map so it still fires INFO on its first encounter.
_SKIPPED_LOG_THROTTLE_SECONDS = 60.0
# Per (task_name, blocker_name) -> monotonic time of last INFO log.
# Guarded by ``_lock`` so concurrent acquire_task callers can't
# double-fire the log.
_skipped_log_last: dict = {}

# Per-task acquire/release stats for the rolling 60s INFO summary
# (Phase 1, item 1.2 — May 11 crash forensics). The bare acquire and
# release log lines were ~2 lines/sec during normal indexer operation,
# bloating the journal so badly that ``journalctl`` queries took 90s
# during the crash investigation. Drop those to DEBUG and emit one
# summary line per task per minute instead. The summary is emitted
# from ``release_task`` (every release checks the per-task window),
# which keeps the work in the same critical section without spawning
# any extra threads or timers — important on Pi Zero 2 W.
_SUMMARY_INTERVAL_SECONDS = 60.0
# task_name -> {'acquires': int, 'total_hold': float, 'max_hold': float,
#               'last_emit': monotonic_seconds}. Guarded by ``_lock``.
_task_stats: dict = {}

# Issue #104 mitigation C: any per-task summary window where ``max_hold``
# crossed this threshold is logged at WARNING instead of INFO. The
# BCM2835 hardware watchdog on the Pi Zero 2 W fires after 90 s of no
# ping; a single task holding the lock for ≥ 60 s is a near-miss
# precursor to the SDIO-contention crash mode (see
# ``.github/copilot-instructions.md`` and issue #104). Surfacing it at
# default journalctl verbosity makes the precursor visible without
# requiring ``-p debug``.
WATCHDOG_NEAR_MISS_THRESHOLD_SECONDS = 60.0


def _record_release_stats(task_name: str, hold_seconds: float) -> None:
    """Update per-task stats and emit a summary if window elapsed.

    Caller MUST hold ``_lock``. The emit happens here (not in a
    background thread) so the work stays cheap and lock-local. A task
    that fires once a minute (e.g. archive) gets one INFO line per
    iteration, no spam. A task that fires 100x/minute (e.g. indexer)
    gets one INFO line summarizing the burst. Stats reset on emit.

    Issue #104 mitigation C: when ``max_hold`` for the window crosses
    :data:`WATCHDOG_NEAR_MISS_THRESHOLD_SECONDS`, the summary is logged
    at WARNING and tagged with " (NEAR-MISS hardware watchdog
    threshold)" so the precursor signal to the SDIO-contention crash
    mode is visible at default journalctl verbosity.
    """
    now = time.monotonic()
    stats = _task_stats.get(task_name)
    if stats is None:
        stats = {
            'acquires': 0,
            'total_hold': 0.0,
            'max_hold': 0.0,
            'last_emit': now,
        }
        _task_stats[task_name] = stats
    stats['acquires'] += 1
    stats['total_hold'] += hold_seconds
    if hold_seconds > stats['max_hold']:
        stats['max_hold'] = hold_seconds
    elapsed = now - stats['last_emit']
    if elapsed < _SUMMARY_INTERVAL_SECONDS:
        return
    # Emit + reset. The summary is the user-visible signal that the
    # task is alive and how much lock-time it consumed.
    avg = stats['total_hold'] / stats['acquires']
    near_miss = stats['max_hold'] >= WATCHDOG_NEAR_MISS_THRESHOLD_SECONDS
    log_level = logging.WARNING if near_miss else logging.INFO
    suffix = (
        " (NEAR-MISS hardware watchdog threshold)"
        if near_miss else ""
    )
    logger.log(
        log_level,
        "task_coordinator: '%s' summary — %d acquire(s) in %.1fs, "
        "avg hold %.2fs, max hold %.2fs%s",
        task_name, stats['acquires'], elapsed, avg, stats['max_hold'],
        suffix,
    )
    stats['acquires'] = 0
    stats['total_hold'] = 0.0
    stats['max_hold'] = 0.0
    stats['last_emit'] = now


def _should_log_skipped(task_name: str, blocker_name: str) -> bool:
    """Return True if "skipped" should be logged at INFO level now.

    Throttled to once per ``_SKIPPED_LOG_THROTTLE_SECONDS`` per
    (task_name, blocker_name) pair. Caller MUST hold ``_lock``.
    """
    now = time.monotonic()
    key = (task_name, blocker_name)
    last = _skipped_log_last.get(key, 0.0)
    if now - last >= _SKIPPED_LOG_THROTTLE_SECONDS:
        _skipped_log_last[key] = now
        return True
    return False


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
                    # Issue #172: do NOT clear ``_skipped_log_last`` on
                    # successful acquire. The previous design cleared
                    # the throttle map here on the theory that a new
                    # contention period deserved a fresh INFO line —
                    # but on a backlogged Pi the indexer↔archive
                    # interleave at sub-second granularity, so every
                    # short successful acquire wiped the throttle and
                    # the very next "skipped" fired INFO again,
                    # producing ~6 lines/min for the same (task,
                    # blocker) pair. The per-pair 60 s window in
                    # :func:`_should_log_skipped` already gives the
                    # operator one INFO line per minute per pair which
                    # is exactly the documented project rule (see
                    # ``copilot-instructions.md`` "Logging routine
                    # cyclic-worker activity at INFO"). A genuinely
                    # NEW (task, blocker) pair has no entry in the
                    # throttle map and will fire INFO on its first
                    # encounter regardless. Stale entries cost ~64
                    # bytes each and there are at most a handful of
                    # pairs, so the dict stays trivially small.
                    # Per Phase 1 item 1.2: routine acquire/release
                    # were ~2 INFO lines/sec during normal operation,
                    # bloating the journal. Drop to DEBUG; the rolling
                    # summary in release_task carries the user-visible
                    # signal at INFO level once per minute per task.
                    logger.debug("Task '%s' acquired lock", task_name)
                    return True

                # Lock is held. Decide whether to wait or give up now.
                if wait_seconds <= 0:
                    if _should_log_skipped(task_name, _current_task):
                        logger.info(
                            "Task '%s' skipped: '%s' is running (%.0fs)",
                            task_name, _current_task, age,
                        )
                    else:
                        # Throttled — emit at DEBUG so it's available
                        # via journalctl -p debug if needed without
                        # spamming default-level logs at 2 lines/sec.
                        logger.debug(
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
            # Per Phase 1 item 1.2: the previous INFO log fired on
            # every release (~2 lines/sec under normal indexer load).
            # Drop to DEBUG; emit a rolling 60s summary at INFO via
            # _record_release_stats so the user still sees activity.
            logger.debug(
                "Task '%s' released lock (%.1fs)", task_name, elapsed,
            )
            _record_release_stats(task_name, elapsed)
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
