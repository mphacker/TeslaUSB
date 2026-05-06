"""Single-threaded indexing worker.

Drains the ``indexing_queue`` table one file at a time, with low OS
priority and gentle inter-file pauses so the gadget and Flask UI stay
responsive on the Pi Zero 2 W.

Public API (called from ``web_control.py``, ``mode_control.py``, and
the ``/api/index/status`` endpoint)::

    start_worker(db_path, teslacam_root)
    stop_worker(timeout=15)
    pause_worker(timeout=15) -> bool       # True if worker became idle
    resume_worker()
    get_worker_status() -> dict            # for status API + UI banner
    is_paused() -> bool

UI banner rule: show whenever ``status['active_file']`` is truthy.

Lifecycle invariants
--------------------
* Exactly one worker thread is alive between ``start_worker`` and
  ``stop_worker``. ``start_worker`` is idempotent.
* Pause is cooperative: the worker observes ``_pause_event`` only at
  iteration boundaries (before claiming a new file). When mid-file,
  the in-flight file always finishes before pause takes effect.
* Every claim is owner-guarded. If the worker thread dies or stalls,
  ``recover_stale_claims`` releases its rows after 30 minutes; the
  fresh worker won't accidentally clobber a still-running predecessor
  thanks to ``claimed_by`` + ``claimed_at`` guards on
  complete/release/defer.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from services import mapping_service
from services import task_coordinator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables (kept module-level so tests can monkeypatch)
# ---------------------------------------------------------------------------

# Sleep between successful file completions. Gives the kernel time to
# flush and the web server room to breathe before the next CPU-heavy
# parse begins.
_INTER_FILE_SLEEP_SECONDS = 0.25
# Sleep when the queue is empty (or contention forces us to back off).
_IDLE_SLEEP_SECONDS = 1.0
# Sleep on a transient claim error or when task_coordinator is busy.
_BACKOFF_SLEEP_SECONDS = 0.5
# How long pause_worker waits for the in-flight file to complete before
# returning False. 15 s is comfortably longer than any single MP4 parse.
_DEFAULT_PAUSE_TIMEOUT = 15.0
# How long stop_worker waits for the thread to exit cleanly.
_DEFAULT_STOP_TIMEOUT = 15.0
# task_coordinator label used by the worker. Keep distinct from
# 'indexer' (the legacy full-scan runner) so logs are unambiguous.
_COORDINATOR_TASK = 'indexer'


# ---------------------------------------------------------------------------
# Module state — all access through _state_lock
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_worker_thread: Optional[threading.Thread] = None
_worker_id: Optional[str] = None
_stop_event = threading.Event()
_pause_event = threading.Event()
# Set whenever the worker is between files (after dispatch finishes
# OR on startup before the first claim). Cleared while a file is
# being processed. ``pause_worker`` waits on this event.
_idle_event = threading.Event()
_idle_event.set()
_db_path: Optional[str] = None
_teslacam_root: Optional[str] = None
_state: Dict[str, Any] = {
    'active_file': None,        # absolute path, drives UI banner
    'active_canonical_key': None,
    'active_claimed_at': None,
    'source': None,             # 'watcher' / 'archive' / 'manual' / 'catchup'
    'files_done_session': 0,
    'last_drained_at': None,
    'last_error': None,
    'last_outcome': None,
}


# ---------------------------------------------------------------------------
# Public lifecycle API
# ---------------------------------------------------------------------------

def start_worker(db_path: str, teslacam_root: str) -> bool:
    """Start the worker thread. Idempotent.

    Returns True if a new thread was started, False if one was already
    running.
    """
    global _worker_thread, _worker_id, _db_path, _teslacam_root
    with _state_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            logger.warning(
                "start_worker: refusing — existing thread still alive "
                "(id=%s). Prior stop_worker likely timed out.",
                _worker_id,
            )
            return False
        _db_path = db_path
        _teslacam_root = teslacam_root
        _worker_id = f"worker-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        _stop_event.clear()
        _pause_event.clear()
        _idle_event.set()
        _state['files_done_session'] = 0
        _state['last_drained_at'] = None
        _state['last_error'] = None
        _state['last_outcome'] = None
        thread = threading.Thread(
            target=_run_worker_loop,
            args=(db_path, teslacam_root, _worker_id),
            name='indexing-worker',
            daemon=True,
        )
        _worker_thread = thread
    thread.start()
    logger.info("Indexing worker started (id=%s)", _worker_id)
    return True


def stop_worker(timeout: float = _DEFAULT_STOP_TIMEOUT) -> bool:
    """Signal the worker to stop and wait for it to exit.

    Returns True if the thread exited within ``timeout`` seconds (or if
    no thread was running). On timeout, ``_worker_thread`` is left
    pointing at the still-alive thread so that subsequent
    ``start_worker`` calls correctly refuse to start a second thread
    racing the first over shared queue state.
    """
    global _worker_thread
    with _state_lock:
        thread = _worker_thread
    if thread is None:
        return True
    _stop_event.set()
    # Also clear pause so a paused worker can notice the stop.
    _pause_event.clear()
    thread.join(timeout=timeout)
    exited = not thread.is_alive()
    if exited:
        with _state_lock:
            if _worker_thread is thread:
                _worker_thread = None
        logger.info("Indexing worker stopped cleanly")
    else:
        # CRITICAL: do NOT clear _worker_thread when the join timed
        # out. Clearing it would let start_worker spin up a second
        # thread that races the still-alive first thread over the
        # task_coordinator slot and the indexing_queue claim rows.
        logger.warning(
            "Indexing worker did not exit within %.1fs (still alive); "
            "leaving thread reference in place to block restart",
            timeout,
        )
    return exited


def pause_worker(timeout: float = _DEFAULT_PAUSE_TIMEOUT) -> bool:
    """Pause the worker between files.

    Sets the pause flag and waits up to ``timeout`` seconds for the
    worker to become idle (i.e., to finish whatever file it's currently
    processing). Returns True if the worker is now idle, False if the
    timeout elapsed while a file was still being parsed.

    Callers (typically the mode-switch handler) should refuse to
    proceed when this returns False — unmounting while the worker has
    a file open will cause busy-unmount errors and may corrupt the
    in-flight write.
    """
    if not _is_running():
        # Nothing to pause; treat as idle so callers don't spin.
        _pause_event.set()
        return True
    _pause_event.set()
    became_idle = _idle_event.wait(timeout=timeout)
    if not became_idle:
        logger.warning(
            "pause_worker: worker still mid-file after %.1fs (active=%s)",
            timeout, _state.get('active_file'),
        )
    return became_idle


def resume_worker() -> None:
    """Clear the pause flag so the worker can start claiming again."""
    _pause_event.clear()


def is_paused() -> bool:
    return _pause_event.is_set()


def is_running() -> bool:
    return _is_running()


def ensure_worker_started() -> bool:
    """Lazy-start the worker if it isn't running and the mount is now ready.

    Used by callers that fire after boot (mode-switch resume,
    file-watcher restart, status API) to recover from the case where
    ``start_worker`` couldn't be called at boot because the TeslaCam
    mount wasn't ready yet. No-op when MAPPING is disabled. Safe to
    call repeatedly — :func:`start_worker` is idempotent and will
    refuse if a thread is already alive.

    Returns True if a worker is running on exit (either we started one
    or one was already alive).
    """
    if _is_running():
        return True
    try:
        from config import MAPPING_ENABLED, MAPPING_DB_PATH
        if not MAPPING_ENABLED:
            return False
        from services.video_service import get_teslacam_path
        tc = get_teslacam_path()
        if not tc:
            return False
        return start_worker(MAPPING_DB_PATH, tc)
    except Exception as e:  # noqa: BLE001
        logger.debug("ensure_worker_started: deferred start failed: %s", e)
        return False


def _is_running() -> bool:
    with _state_lock:
        t = _worker_thread
    return t is not None and t.is_alive()


def get_worker_status() -> Dict[str, Any]:
    """Snapshot for the /api/index/status endpoint and the UI banner.

    Combines in-memory worker state with a fresh
    :func:`mapping_service.get_queue_status` snapshot so callers get
    everything in one round-trip.
    """
    with _state_lock:
        snap = {
            'worker_running': (
                _worker_thread is not None and _worker_thread.is_alive()
            ),
            'worker_id': _worker_id,
            'paused': _pause_event.is_set(),
            'idle': _idle_event.is_set(),
            'active_file': _state['active_file'],
            'source': _state['source'],
            'files_done_session': _state['files_done_session'],
            'last_drained_at': _state['last_drained_at'],
            'last_error': _state['last_error'],
            'last_outcome': _state['last_outcome'],
        }
        db_path = _db_path
    if db_path:
        try:
            snap.update(mapping_service.get_queue_status(db_path))
        except Exception as e:  # noqa: BLE001 — status must never raise
            logger.warning("get_queue_status failed inside status: %s", e)
            snap['queue_depth'] = None
            snap['queue_status_error'] = str(e)
    return snap


# ---------------------------------------------------------------------------
# Dispatch (pure function, easy to unit-test without a thread)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorkerAction:
    """Outcome of processing one claimed queue row.

    Captured as a value so tests can assert on the *intent* of the
    worker (delete the row, defer 60s, etc.) without having to mock
    out SQLite write paths.
    """
    action: str          # 'complete' | 'defer' | 'release' | 'noop'
    next_attempt_at: Optional[float] = None
    bump_attempts: bool = False
    purge_path: Optional[str] = None
    last_error: Optional[str] = None
    outcome: Optional[mapping_service.IndexOutcome] = None


def process_claimed_item(
    row: Dict[str, Any],
    db_path: str,
    teslacam_root: str,
    *,
    indexer: Optional[Callable[..., mapping_service.IndexResult]] = None,
    now_fn: Callable[[], float] = time.time,
) -> WorkerAction:
    """Decide what to do with a claimed queue row.

    Pure function: takes the claimed row, calls the indexer, and
    returns a :class:`WorkerAction` describing the next queue mutation.
    The thread loop is responsible for actually applying the action so
    we can keep this function side-effect-free for tests.

    Any exception raised by the indexer is caught and converted to a
    bump-attempts defer with exponential backoff — we never let the
    worker thread die because a parser hit something unexpected.

    ``indexer`` defaults to ``mapping_service.index_single_file`` but
    is looked up at call time (not definition time) so test
    monkeypatches against the module attribute work as expected.
    """
    if indexer is None:
        indexer = mapping_service.index_single_file
    file_path = row['file_path']
    attempts = int(row.get('attempts') or 0)
    canonical_key_value = row['canonical_key']

    try:
        result = indexer(file_path, db_path, teslacam_root)
    except Exception as e:  # noqa: BLE001 — convert to defer
        logger.exception(
            "Indexer raised unexpectedly for %s; deferring", file_path,
        )
        backoff = mapping_service.compute_backoff(attempts)
        return WorkerAction(
            action='defer',
            next_attempt_at=now_fn() + backoff,
            bump_attempts=True,
            last_error=f'unhandled: {e!r}',
            outcome=mapping_service.IndexOutcome.PARSE_ERROR,
        )

    outcome = result.outcome
    IO = mapping_service.IndexOutcome

    if outcome in (
        IO.INDEXED, IO.ALREADY_INDEXED, IO.DUPLICATE_UPGRADED,
        IO.NO_GPS_RECORDED, IO.NOT_FRONT_CAMERA,
    ):
        return WorkerAction(action='complete', outcome=outcome)

    if outcome == IO.FILE_MISSING:
        # Both delete the queue row AND tell the caller to purge any
        # stale waypoints/events for this path.
        return WorkerAction(
            action='complete', purge_path=file_path, outcome=outcome,
        )

    if outcome == IO.TOO_NEW:
        # If we can stat the file, schedule retry just past the
        # 120-second floor that index_single_file enforces. Otherwise
        # treat as missing — the file vanished between TOO_NEW and now.
        try:
            mtime = os.path.getmtime(file_path)
        except OSError:
            return WorkerAction(
                action='complete', purge_path=file_path,
                outcome=IO.FILE_MISSING,
            )
        # +125s gives us a 5-second margin past the 120s freshness check
        # so the next claim doesn't immediately TOO_NEW again.
        return WorkerAction(
            action='defer', next_attempt_at=mtime + 125.0,
            bump_attempts=False, outcome=outcome,
        )

    if outcome == IO.PARSE_ERROR:
        backoff = mapping_service.compute_backoff(attempts)
        return WorkerAction(
            action='defer', next_attempt_at=now_fn() + backoff,
            bump_attempts=True, last_error=result.error,
            outcome=outcome,
        )

    if outcome == IO.DB_BUSY:
        # Release without attempts bump — the row will be picked up on
        # the next tick once whoever holds the DB lock has released it.
        return WorkerAction(
            action='release', last_error=result.error, outcome=outcome,
        )

    # Defensive fallback for any future enum value not handled above.
    logger.warning(
        "process_claimed_item: unknown outcome %r for %s",
        outcome, file_path,
    )
    return WorkerAction(action='release', outcome=outcome)


def _apply_action(action: WorkerAction, row: Dict[str, Any],
                  db_path: str) -> None:
    """Apply a WorkerAction to the queue (and to indexed_files, on
    FILE_MISSING). Owner-guarded so a stale worker can't disturb a
    re-claimed row."""
    canonical_key_value = row['canonical_key']
    claimed_by = row.get('claimed_by')
    claimed_at = row.get('claimed_at')

    if action.action == 'complete':
        mapping_service.complete_queue_item(
            db_path, canonical_key_value,
            claimed_by=claimed_by, claimed_at=claimed_at,
        )
        if action.purge_path:
            try:
                mapping_service.purge_deleted_videos(
                    db_path, deleted_paths=[action.purge_path],
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "purge_deleted_videos failed for %s: %s",
                    action.purge_path, e,
                )
    elif action.action == 'defer':
        mapping_service.defer_queue_item(
            db_path, canonical_key_value,
            next_attempt_at=action.next_attempt_at or 0.0,
            bump_attempts=action.bump_attempts,
            last_error=action.last_error,
            claimed_by=claimed_by, claimed_at=claimed_at,
        )
    elif action.action == 'release':
        mapping_service.release_claim(
            db_path, canonical_key_value,
            claimed_by=claimed_by, claimed_at=claimed_at,
        )
    elif action.action == 'noop':
        pass
    else:
        logger.warning("Unknown WorkerAction.action=%r", action.action)


# ---------------------------------------------------------------------------
# Worker thread loop
# ---------------------------------------------------------------------------

def _set_worker_state(**fields: Any) -> None:
    with _state_lock:
        _state.update(fields)


def _record_active(file_path: str, source: Optional[str],
                   canonical_key_value: str,
                   claimed_at: Optional[float]) -> None:
    with _state_lock:
        _state['active_file'] = file_path
        _state['source'] = source
        _state['active_canonical_key'] = canonical_key_value
        _state['active_claimed_at'] = claimed_at
    _idle_event.clear()


def _record_idle(last_outcome: Optional[mapping_service.IndexOutcome] = None,
                 last_error: Optional[str] = None) -> None:
    with _state_lock:
        _state['active_file'] = None
        _state['source'] = None
        _state['active_canonical_key'] = None
        _state['active_claimed_at'] = None
        if last_outcome is not None:
            _state['last_outcome'] = last_outcome.name
        if last_error is not None:
            _state['last_error'] = last_error
    _idle_event.set()


def _apply_low_priority() -> None:
    """Best-effort drop to nice 19 + SCHED_BATCH on Linux.

    No-ops on platforms (Windows/macOS) where the syscalls are missing
    or fail. Any exception is swallowed — priority adjustment is a
    nice-to-have, not a correctness requirement.
    """
    try:
        os.nice(19)
    except (OSError, AttributeError, ValueError):
        pass
    if sys.platform.startswith('linux'):
        try:
            # SCHED_BATCH = 3 on Linux; not exposed in stdlib for all
            # Python builds, so we use the raw constant.
            SCHED_BATCH = 3
            if hasattr(os, 'sched_setscheduler') and hasattr(os, 'sched_param'):
                os.sched_setscheduler(  # type: ignore[attr-defined]
                    0, SCHED_BATCH, os.sched_param(0),  # type: ignore[attr-defined]
                )
        except (OSError, PermissionError, AttributeError):
            pass


def _run_worker_loop(db_path: str, teslacam_root: str, worker_id: str) -> None:
    """The thread target. One file at a time, until stop is signaled."""
    _apply_low_priority()
    try:
        released = mapping_service.recover_stale_claims(db_path)
        if released:
            logger.info(
                "Worker %s released %d stale claims at startup",
                worker_id, released,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("recover_stale_claims failed at startup: %s", e)

    while not _stop_event.is_set():
        # Honor pause requests. The worker is idle here (between files),
        # so simply not advancing is sufficient — no claim is held.
        if _pause_event.is_set():
            _idle_event.set()
            if _stop_event.wait(timeout=_INTER_FILE_SLEEP_SECONDS):
                break
            continue

        # Don't fight other heavy tasks — archive and cloud sync take
        # priority for vehicle-safety reasons (RecentClips preservation
        # > indexing latency).
        if not task_coordinator.acquire_task(_COORDINATOR_TASK):
            if _stop_event.wait(timeout=_BACKOFF_SLEEP_SECONDS):
                break
            continue

        row: Optional[Dict[str, Any]] = None
        try:
            try:
                row = mapping_service.claim_next_queue_item(
                    db_path, worker_id,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("claim_next_queue_item raised: %s", e)
                _set_worker_state(last_error=f'claim: {e!r}')
                if _stop_event.wait(timeout=_BACKOFF_SLEEP_SECONDS):
                    break
                continue

            if row is None:
                # Queue genuinely empty — record drain timestamp so the
                # status API can show "last drained N seconds ago".
                _set_worker_state(last_drained_at=time.time())
                if _stop_event.wait(timeout=_IDLE_SLEEP_SECONDS):
                    break
                continue

            _process_one(row, db_path, teslacam_root)
        finally:
            task_coordinator.release_task(_COORDINATOR_TASK)
            _record_idle()

        # Inter-file pause — keep the gadget responsive.
        if _stop_event.wait(timeout=_INTER_FILE_SLEEP_SECONDS):
            break


def _process_one(row: Dict[str, Any], db_path: str,
                 teslacam_root: str) -> None:
    """Process a single claimed row. Owner-guarded queue mutations.

    Wrapped in try/except so any unhandled exception during dispatch
    converts into an owner-guarded release rather than leaking the
    claim until the 30-minute stale-recovery timer.
    """
    canonical_key_value = row['canonical_key']
    file_path = row['file_path']
    source = row.get('source')
    claimed_at = row.get('claimed_at')

    _record_active(file_path, source, canonical_key_value, claimed_at)
    try:
        action = process_claimed_item(row, db_path, teslacam_root)
        _apply_action(action, row, db_path)
        with _state_lock:
            if action.outcome == mapping_service.IndexOutcome.INDEXED:
                _state['files_done_session'] += 1
            if action.outcome is not None:
                _state['last_outcome'] = action.outcome.name
            if action.last_error:
                _state['last_error'] = action.last_error
            elif action.action == 'complete':
                # Successful processing clears the last_error sticky.
                _state['last_error'] = None
    except Exception as e:  # noqa: BLE001 — never let the loop die
        logger.exception(
            "Worker dispatch failed for %s; releasing claim", file_path,
        )
        try:
            mapping_service.release_claim(
                db_path, canonical_key_value,
                claimed_by=row.get('claimed_by'),
                claimed_at=claimed_at,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Owner-guarded release_claim also failed for %s; "
                "stale-recovery will pick this up",
                canonical_key_value,
            )
        _set_worker_state(last_error=f'dispatch: {e!r}')
