"""Archive health watchdog and retention prune (issue #76 — Phase 2c).

Single daemon thread that observes the archive subsystem and reports
health to the web UI. Two responsibilities, both interleaved into one
thread to stay under the Pi Zero 2 W resource budget:

1. **Health watchdog** (every 60 s by default): compute staleness of the
   most recent successful copy, classify a severity, and surface the
   summary via :func:`get_health` for the ``/api/archive/status`` JSON
   endpoint and the persistent UI banner.

2. **Retention prune** (daily, with 5–15 min jitter on first run): walk
   ``ArchivedClips/`` and ``os.remove()`` ``*.mp4`` files older than the
   configured retention. Files in ``.dead_letter/`` are never touched.
   For every deleted clip we call
   :func:`mapping_service.purge_deleted_videos` so the
   ``indexed_files`` row goes away — but **trips, waypoints, and
   detected_events are preserved** (only their ``video_path`` pointer
   is nulled). This contract is load-bearing; see
   ``copilot-instructions.md`` for the May 7 trip-loss regression that
   forced it.

**Hard contract (do NOT break — see copilot-instructions.md):**

* This module never imports or calls anything that touches the USB
  gadget — no ``mount``, ``umount``, ``losetup``, ``nsenter``,
  ``partition_mount_service``, ``quick_edit_part2``, or
  ``rebind_usb_gadget``. Tesla may be actively recording; ANY USB
  disruption from a background subsystem loses footage. The watchdog
  is a pure observer of ``archive_queue`` rows + local-FS disk usage.
* No heavy imports — ``os``, ``sqlite3``, ``logging``, ``shutil``,
  ``random``, ``threading``, ``time``, ``datetime`` only. Steady-state
  RSS budget is ~5 MB.
* Lock-before-sleep — when the retention prune holds the
  ``task_coordinator`` 'retention' slot it MUST release the lock
  before any ``_stop_event.wait()``.

Public API mirrors the indexer / archive_worker style::

    start_watchdog(db_path, archive_root) -> bool
    stop_watchdog(timeout=...)            -> bool
    is_running()                          -> bool
    wake()                                -> None
    get_health()                          -> dict
    force_prune_now()                     -> dict   # synchronous prune
    get_status()                          -> dict   # full snapshot
"""

from __future__ import annotations

import logging
import os
import random
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from services import archive_queue
from services import task_coordinator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables — module-level so tests can monkeypatch
# ---------------------------------------------------------------------------

# Default tick interval. Overridden by ``ARCHIVE_QUEUE_WATCHDOG_CHECK_INTERVAL_SECONDS``.
_DEFAULT_CHECK_INTERVAL = 60.0

# Severity thresholds (seconds since the last successful copy). Active
# only when the queue has pending work — an empty queue with no recent
# copy is normal (no clips to archive).
_STALE_WARNING_SECONDS = 5 * 60       # 5 min  → WARNING
_STALE_ERROR_SECONDS = 10 * 60        # 10 min → ERROR + banner
_STALE_CRITICAL_SECONDS = 20 * 60     # 20 min → CRITICAL + persistent banner

# Retention cadence: 24 h with 5–15 min jitter on first iteration so a
# fleet of Pis doesn't all prune at the same wall clock time.
_RETENTION_INTERVAL_SECONDS = 24 * 3600
_RETENTION_FIRST_RUN_JITTER_MIN_SECONDS = 5 * 60
_RETENTION_FIRST_RUN_JITTER_MAX_SECONDS = 15 * 60

# task_coordinator wait used by the retention prune. The retention prune
# is a periodic priority task — it BLOCK-waits up to this many seconds
# for the indexer/archive_worker to yield, then proceeds.
_RETENTION_COORDINATOR_WAIT_SECONDS = 60.0
_RETENTION_COORDINATOR_TASK = 'retention'

# Diagnostic subdirectory inside ``archive_root`` that the prune must
# never touch. Mirrors the worker's dead-letter sidecar location.
_DEAD_LETTER_DIRNAME = '.dead_letter'

# Default stop/join timeouts.
_DEFAULT_STOP_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Module state — every read/write through ``_state_lock``
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_wake_event = threading.Event()
_db_path: Optional[str] = None
_archive_root: Optional[str] = None
_check_interval: float = _DEFAULT_CHECK_INTERVAL

# Last cached health snapshot — refreshed each tick, served by
# :func:`get_health` so HTTP polling is O(1).
_last_health: Dict[str, Any] = {
    'severity': 'ok',
    'message': 'Archive watchdog has not yet run.',
    'last_successful_copy_at': None,
    'last_successful_copy_age_seconds': None,
    'worker_running': False,
    'paused': False,
    'dead_letter_count': 0,
    'pending_count': 0,
    'disk_free_mb': 0,
    'disk_known': True,
    'disk_warning': False,
    'checked_at': None,
}

# Retention bookkeeping.
_retention_state: Dict[str, Any] = {
    'last_prune_at': None,        # ISO timestamp of last completed prune
    'last_prune_deleted': 0,
    'last_prune_freed_bytes': 0,
    'last_prune_kept_unsynced': 0,  # Phase 1 item 1.3 — held back, awaiting cloud sync
    'last_prune_error': None,
    'next_prune_due_at': None,    # epoch seconds
}

# Issue #91 duplicate-trigger guard. Set to True at the start of any
# in-flight ``_run_retention_prune`` call, cleared in the outer
# ``finally``. A second concurrent caller (e.g. Settings UI "Prune now"
# landing while the watchdog tick is mid-walk, or a debounce-bypassed
# disk-critical cleanup spawning a daemon thread that races the UI
# click) sees the flag set and short-circuits with
# ``status='already_running'`` instead of block-waiting up to 60 s on
# ``task_coordinator.acquire_task('retention', wait_seconds=60.0)``.
# Always read/written under ``_state_lock``.
_retention_running: bool = False


# ---------------------------------------------------------------------------
# Public lifecycle API
# ---------------------------------------------------------------------------

def start_watchdog(db_path: str, archive_root: str, *,
                   check_interval_seconds: Optional[float] = None) -> bool:
    """Start the watchdog thread. Idempotent.

    ``archive_root`` is the directory whose disk-space we watch
    (typically ``ARCHIVE_DIR`` / ``~/ArchivedClips``). ``db_path`` is
    the SQLite DB containing the ``archive_queue`` table (typically
    ``MAPPING_DB_PATH`` / ``geodata.db``).
    """
    global _thread, _db_path, _archive_root, _check_interval
    with _state_lock:
        if _thread is not None and _thread.is_alive():
            logger.debug("archive_watchdog.start_watchdog: already running")
            return False
        _db_path = db_path
        _archive_root = archive_root
        if check_interval_seconds is not None:
            _check_interval = float(check_interval_seconds)
        else:
            _check_interval = _resolve_default_interval()
        _stop_event.clear()
        _wake_event.clear()
        # Stagger the first retention prune so a fleet doesn't all run
        # in lockstep on the same minute.
        jitter = random.uniform(
            _RETENTION_FIRST_RUN_JITTER_MIN_SECONDS,
            _RETENTION_FIRST_RUN_JITTER_MAX_SECONDS,
        )
        _retention_state['next_prune_due_at'] = time.time() + jitter
        thread = threading.Thread(
            target=_run_loop,
            args=(db_path, archive_root, _check_interval),
            name='archive-watchdog',
            daemon=True,
        )
        _thread = thread
    thread.start()
    logger.info(
        "Archive watchdog started (db=%s, root=%s, interval=%.1fs)",
        db_path, archive_root, _check_interval,
    )
    return True


def stop_watchdog(timeout: float = _DEFAULT_STOP_TIMEOUT) -> bool:
    """Signal the watchdog to stop and wait for it to exit. Idempotent."""
    global _thread
    with _state_lock:
        thread = _thread
    if thread is None:
        return True
    _stop_event.set()
    _wake_event.set()
    thread.join(timeout=timeout)
    exited = not thread.is_alive()
    if exited:
        with _state_lock:
            if _thread is thread:
                _thread = None
        logger.info("Archive watchdog stopped cleanly")
    else:
        logger.warning(
            "Archive watchdog did not exit within %.1fs", timeout,
        )
    return exited


def is_running() -> bool:
    with _state_lock:
        t = _thread
    return t is not None and t.is_alive()


def wake() -> None:
    """Cut short the current sleep so the next tick happens immediately.

    Cheap, lock-free, safe to call from any thread (including request
    handlers).
    """
    _wake_event.set()


# ---------------------------------------------------------------------------
# Health / severity classification
# ---------------------------------------------------------------------------

def _resolve_default_interval() -> float:
    """Look up the configured check interval at start time.

    Looked up dynamically so tests can monkeypatch the config import.
    Falls back to :data:`_DEFAULT_CHECK_INTERVAL` when the config
    module isn't importable (unit-test environments).
    """
    try:
        from config import ARCHIVE_QUEUE_WATCHDOG_CHECK_INTERVAL_SECONDS
        return float(ARCHIVE_QUEUE_WATCHDOG_CHECK_INTERVAL_SECONDS)
    except Exception:  # noqa: BLE001
        return _DEFAULT_CHECK_INTERVAL


def _resolve_disk_thresholds() -> tuple:
    """Return (warning_mb, critical_mb) from config or sensible defaults."""
    try:
        from config import (
            CLOUD_ARCHIVE_DISK_SPACE_WARNING_MB,
            CLOUD_ARCHIVE_DISK_SPACE_CRITICAL_MB,
        )
        return (
            int(CLOUD_ARCHIVE_DISK_SPACE_WARNING_MB),
            int(CLOUD_ARCHIVE_DISK_SPACE_CRITICAL_MB),
        )
    except Exception:  # noqa: BLE001
        return (500, 100)


def _resolve_retention_days() -> int:
    """Return the configured ArchivedClips retention in days."""
    try:
        from config import CLOUD_ARCHIVE_RETENTION_DAYS
        return int(CLOUD_ARCHIVE_RETENTION_DAYS)
    except Exception:  # noqa: BLE001
        return 30


def _resolve_delete_unsynced() -> bool:
    """Return whether the retention prune may delete clips that aren't yet
    backed up to the cloud (Phase 1 item 1.3 — "retention respects cloud").

    * ``True``  → age-only deletion. A clip past the retention cutoff is
      eligible for deletion regardless of its cloud-sync status.
    * ``False`` → "keep until backed up". A clip past the retention
      cutoff is **kept** if it has not yet been confirmed uploaded to
      the cloud (status='synced' in ``cloud_synced_files``).

    Default behavior when the config key is unset (``None`` /
    ``CLOUD_ARCHIVE_DELETE_UNSYNCED is None``):

    * Cloud configured (provider non-empty AND credentials file present)
      → return ``False`` (protect un-uploaded clips by default).
    * Cloud not configured → return ``True`` (no upload mechanism, so
      age-based deletion is the only option).

    Resolved fresh on every prune so a config-yaml change takes effect
    on the next pass without restarting the service.
    """
    try:
        from config import CLOUD_ARCHIVE_DELETE_UNSYNCED
    except Exception:  # noqa: BLE001
        CLOUD_ARCHIVE_DELETE_UNSYNCED = None  # noqa: N806
    if CLOUD_ARCHIVE_DELETE_UNSYNCED is None:
        return not _is_cloud_configured()
    return bool(CLOUD_ARCHIVE_DELETE_UNSYNCED)


def _is_cloud_configured() -> bool:
    """Return True iff a cloud provider is set AND its creds file exists.

    Used by :func:`_resolve_delete_unsynced` to decide the auto-default,
    and by :func:`_run_retention_prune` to short-circuit the cloud
    check when there's no cloud anyway. Never raises.
    """
    try:
        from config import CLOUD_ARCHIVE_PROVIDER, CLOUD_PROVIDER_CREDS_PATH
        return bool(CLOUD_ARCHIVE_PROVIDER) and os.path.isfile(
            CLOUD_PROVIDER_CREDS_PATH
        )
    except Exception:  # noqa: BLE001
        return False


def _resolve_cloud_db_path() -> Optional[str]:
    """Return the cloud_sync.db path, or None if config import fails."""
    try:
        from config import CLOUD_ARCHIVE_DB_PATH
        return CLOUD_ARCHIVE_DB_PATH
    except Exception:  # noqa: BLE001
        return None


def _is_synced_to_cloud(file_path: str, archive_root: str,
                       cloud_db_path: str) -> bool:
    """Return True iff ``file_path`` is recorded as 'synced' in the cloud DB.

    The ``cloud_synced_files`` table currently has rows in mixed
    formats (some absolute, some relative — see plan item 2.7
    "p2-cloud-path-canonicalization"). To remain correct under that
    mismatch, we look up both representations:

    * ``file_path`` as-is (the absolute path the prune walker found)
    * ``file_path`` as relative to ``archive_root``

    Returns True only when at least one row matches AND its status is
    'synced'. Returns False on any DB error (fail-safe — when in doubt,
    keep the file).
    """
    if not cloud_db_path or not os.path.isfile(cloud_db_path):
        # No cloud DB → nothing has been recorded as synced. Conservative.
        return False
    candidates = [file_path]
    try:
        rel = os.path.relpath(file_path, archive_root)
        if rel and rel != file_path:
            candidates.append(rel)
            # Some legacy rows may be stored with forward slashes on Windows
            # path separators; normalize for cross-platform safety.
            candidates.append(rel.replace(os.sep, '/'))
    except ValueError:
        pass
    placeholders = ','.join('?' * len(candidates))
    query = (
        f"SELECT 1 FROM cloud_synced_files "
        f"WHERE file_path IN ({placeholders}) "
        f"AND status = 'synced' LIMIT 1"
    )
    try:
        conn = sqlite3.connect(cloud_db_path, timeout=5.0)
        try:
            row = conn.execute(query, candidates).fetchone()
            return row is not None
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.debug(
            "archive_retention: cloud-sync check failed for %s: %s",
            file_path, e,
        )
        return False


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        # Accept both 'YYYY-MM-DDTHH:MM:SS+00:00' and trailing 'Z'.
        cleaned = ts.replace('Z', '+00:00')
        return datetime.fromisoformat(cleaned).timestamp()
    except (ValueError, TypeError):
        return None


def _safe_disk_usage(path: str):
    """Return ``shutil.disk_usage`` or None on failure (path missing, etc.)."""
    try:
        return shutil.disk_usage(path)
    except OSError:
        return None


def _classify_severity(*,
                       worker_running: bool,
                       pending_count: int,
                       last_copy_age_seconds: Optional[float],
                       disk_free_mb: int,
                       disk_warning_mb: int,
                       disk_critical_mb: int,
                       disk_known: bool = True) -> tuple:
    """Return ``(severity, message)`` for the watchdog tick.

    Pure function so tests can drive every branch without mocking the
    DB or filesystem. Disk-space severity overrides staleness severity
    when it's higher (CRITICAL beats ERROR beats WARNING beats OK).

    ``disk_known`` is False when ``shutil.disk_usage`` raised OSError
    (e.g. ``archive_root`` briefly inaccessible). In that case the
    disk overlay is skipped entirely so a transient stat failure does
    not pop a misleading "0 MB free, CRITICAL" banner. The companion
    ``archive_worker._check_disk_space_guard`` likewise fails open on
    OSError — the watchdog now matches that "fail-quiet on stat
    error" behavior.
    """
    # Staleness severity. Only escalates when there is pending work in
    # the queue — an idle worker with an empty queue is normal.
    if pending_count == 0 or last_copy_age_seconds is None:
        stale_sev = 'ok'
        stale_msg = (
            "Archive worker is idle (no pending clips)."
            if pending_count == 0 else
            "Archive worker has not yet copied a clip."
        )
    elif last_copy_age_seconds < _STALE_WARNING_SECONDS:
        stale_sev = 'ok'
        stale_msg = (
            f"Archive worker is healthy "
            f"({pending_count} pending, last copy "
            f"{int(last_copy_age_seconds)}s ago)."
        )
    elif last_copy_age_seconds < _STALE_ERROR_SECONDS:
        stale_sev = 'warning'
        stale_msg = (
            f"Archive worker is slow: no copy in "
            f"{int(last_copy_age_seconds // 60)} min "
            f"({pending_count} pending)."
        )
    elif last_copy_age_seconds < _STALE_CRITICAL_SECONDS:
        stale_sev = 'error'
        stale_msg = (
            f"Archive worker may be stalled: no copy in "
            f"{int(last_copy_age_seconds // 60)} min "
            f"({pending_count} pending) — videos may be lost!"
        )
    else:
        stale_sev = 'critical'
        stale_msg = (
            f"Archive worker is STALLED: no copy in "
            f"{int(last_copy_age_seconds // 60)} min "
            f"({pending_count} pending) — videos are being lost!"
        )

    # Worker-down with pending work is critical regardless of staleness.
    if (not worker_running) and pending_count > 0:
        stale_sev = 'critical'
        stale_msg = (
            f"Archive worker is NOT RUNNING with {pending_count} clips "
            f"pending — videos are being lost!"
        )

    # Disk-space severity overlay. Skip entirely when the disk-usage
    # stat failed (``disk_known=False``) so a transient OSError does
    # not surface as "0 MB free, CRITICAL".
    if not disk_known:
        disk_sev = 'ok'
        disk_msg = ''
    elif disk_free_mb < disk_critical_mb:
        disk_sev = 'critical'
        disk_msg = (
            f"SD card free space is CRITICAL: {disk_free_mb} MB "
            f"(< {disk_critical_mb} MB threshold). New archive copies "
            "are blocked."
        )
    elif disk_free_mb < disk_warning_mb:
        disk_sev = 'warning'
        disk_msg = (
            f"SD card free space is low: {disk_free_mb} MB "
            f"(< {disk_warning_mb} MB threshold)."
        )
    else:
        disk_sev = 'ok'
        disk_msg = ''

    # Resolve the higher-severity message.
    rank = {'ok': 0, 'warning': 1, 'error': 2, 'critical': 3}
    if rank[disk_sev] > rank[stale_sev]:
        return disk_sev, disk_msg
    if rank[disk_sev] == rank[stale_sev] and disk_sev != 'ok':
        return stale_sev, f"{stale_msg} {disk_msg}".strip()
    return stale_sev, stale_msg


def _compute_health(db_path: str, archive_root: str) -> Dict[str, Any]:
    """Read the queue + disk + worker state and return a health snapshot."""
    counts = archive_queue.get_queue_status(db_path)
    pending_count = int(counts.get('pending', 0))
    dead_letter_count = int(counts.get('dead_letter', 0))
    last_copy_iso = archive_queue.get_last_copied_at(db_path)
    last_copy_ts = _parse_iso(last_copy_iso)
    age = (time.time() - last_copy_ts) if last_copy_ts else None

    # Worker liveness via the public archive_worker API.
    try:
        from services import archive_worker
        worker_running = archive_worker.is_running()
        worker_paused = archive_worker.is_paused()
    except Exception as e:  # noqa: BLE001
        logger.debug("archive_worker introspection failed: %s", e)
        worker_running = False
        worker_paused = False

    usage = _safe_disk_usage(archive_root)
    disk_known = usage is not None
    disk_free_mb = int(usage.free // (1024 * 1024)) if usage else 0
    disk_total_mb = int(usage.total // (1024 * 1024)) if usage else 0
    disk_used_mb = max(disk_total_mb - disk_free_mb, 0)
    disk_warning_mb, disk_critical_mb = _resolve_disk_thresholds()

    severity, message = _classify_severity(
        worker_running=worker_running,
        pending_count=pending_count,
        last_copy_age_seconds=age,
        disk_free_mb=disk_free_mb,
        disk_warning_mb=disk_warning_mb,
        disk_critical_mb=disk_critical_mb,
        disk_known=disk_known,
    )

    snap: Dict[str, Any] = {
        'severity': severity,
        'message': message,
        'last_successful_copy_at': last_copy_iso,
        'last_successful_copy_age_seconds': int(age) if age is not None else None,
        'worker_running': bool(worker_running),
        'paused': bool(worker_paused),
        'dead_letter_count': dead_letter_count,
        'pending_count': pending_count,
        'disk_free_mb': disk_free_mb,
        'disk_total_mb': disk_total_mb,
        'disk_used_mb': disk_used_mb,
        'disk_warning_mb': disk_warning_mb,
        'disk_critical_mb': disk_critical_mb,
        'disk_known': disk_known,
        'disk_warning': (
            disk_known
            and severity != 'ok'
            and disk_free_mb < disk_warning_mb
        ),
        'checked_at': _iso_now(),
    }
    return snap


def get_health() -> Dict[str, Any]:
    """Return the most recent cached health snapshot.

    Cheap (returns a copy of the in-memory dict). Updated by the
    background loop every ``check_interval`` seconds; an HTTP polling
    UI never blocks on a DB query.
    """
    with _state_lock:
        return dict(_last_health)


def get_status() -> Dict[str, Any]:
    """Return health + retention state in one snapshot."""
    with _state_lock:
        snap = dict(_last_health)
        snap['retention'] = dict(_retention_state)
        snap['retention']['retention_days'] = _resolve_retention_days()
        snap['retention']['delete_unsynced'] = _resolve_delete_unsynced()
        snap['retention']['cloud_configured'] = _is_cloud_configured()
        snap['watchdog_running'] = (
            _thread is not None and _thread.is_alive()
        )
        snap['check_interval_seconds'] = _check_interval
    return snap


# ---------------------------------------------------------------------------
# Retention prune
# ---------------------------------------------------------------------------

def _iter_archive_mp4_files(archive_root: str):
    """Yield (abs_path, mtime, size_bytes) for every .mp4 under archive_root.

    Walks the tree without following symlinks; skips the
    ``.dead_letter`` diagnostic subdirectory entirely so user-visible
    forensic info isn't auto-deleted.
    """
    if not archive_root or not os.path.isdir(archive_root):
        return
    for dirpath, dirnames, filenames in os.walk(archive_root, followlinks=False):
        # Prune .dead_letter so os.walk doesn't descend into it.
        dirnames[:] = [
            d for d in dirnames if d != _DEAD_LETTER_DIRNAME
        ]
        for fn in filenames:
            if not fn.lower().endswith('.mp4'):
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            yield full, st.st_mtime, st.st_size


def _delete_one_mp4(path: str, db_path: str) -> int:
    """Atomically delete one mp4 + reconcile geodata.

    Returns the freed byte count (0 on failure). Uses
    :func:`mapping_service.purge_deleted_videos` to reconcile the
    indexed_files row WITHOUT touching trips/waypoints/events. See the
    docstring on ``purge_deleted_videos`` for why that contract is
    load-bearing.

    Routes the actual delete through
    :func:`services.file_safety.safe_delete_archive_video` — the single
    doorway that enforces the protected-file guard (Phase 2.1).
    """
    from services.file_safety import safe_delete_archive_video
    freed = safe_delete_archive_video(path)
    if freed == 0:
        return 0
    # Reconcile geodata (best-effort — failure here doesn't undo the delete).
    try:
        from services.mapping_service import purge_deleted_videos
        purge_deleted_videos(db_path, deleted_paths=[path])
    except Exception as e:  # noqa: BLE001
        logger.debug(
            "archive_retention: purge_deleted_videos failed for %s: %s",
            path, e,
        )
    return freed


def _run_retention_prune(archive_root: str, db_path: str,
                         retention_days: int) -> Dict[str, Any]:
    """Walk ``archive_root`` and delete .mp4 files older than retention.

    Returns a summary dict suitable for logging and the
    ``/api/archive/prune_now`` response::

        {'deleted_count': N, 'freed_bytes': M, 'scanned': K,
         'kept_unsynced_count': U, 'cutoff_iso': 'YYYY-MM-DD...',
         'duration_seconds': S}

    Phase 1 item 1.3 — when ``_resolve_delete_unsynced()`` returns
    ``False`` AND a cloud provider is configured, files past the
    retention cutoff are checked against ``cloud_synced_files``: those
    not yet recorded as ``status='synced'`` are kept (and counted in
    ``kept_unsynced_count``) so an extended WiFi outage cannot cause
    silent loss of un-uploaded footage.

    Holds the ``task_coordinator`` 'retention' slot for the duration so
    the archive worker yields cleanly. Releases the slot before
    returning.

    Issue #91: a module-level ``_retention_running`` flag short-circuits
    a second concurrent call so a stacked Settings UI click + watchdog
    tick + disk-critical cleanup can't pile up 60-second waits on the
    ``task_coordinator`` 'retention' slot. Short-circuited callers get
    a summary with ``status='already_running'``.
    """
    global _retention_running
    started = time.time()
    cutoff = started - (max(int(retention_days), 1) * 86400)
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    delete_unsynced = _resolve_delete_unsynced()
    cloud_configured = _is_cloud_configured()
    cloud_db_path = _resolve_cloud_db_path() if cloud_configured else None
    enforce_cloud_check = (not delete_unsynced) and cloud_configured
    summary: Dict[str, Any] = {
        'deleted_count': 0,
        'freed_bytes': 0,
        'scanned': 0,
        'kept_unsynced_count': 0,
        'cutoff_iso': cutoff_iso,
        'retention_days': int(retention_days),
        'delete_unsynced': bool(delete_unsynced),
        'cloud_configured': bool(cloud_configured),
        'duration_seconds': 0.0,
    }
    if not archive_root or not os.path.isdir(archive_root):
        summary['duration_seconds'] = round(time.time() - started, 3)
        return summary

    # Issue #91 — duplicate-trigger guard. Atomic check-and-set so two
    # concurrent callers can't both pass. The flag MUST be set before
    # ``acquire_task`` (which can block 60 s); otherwise the second
    # caller would still queue on the lock.
    with _state_lock:
        if _retention_running:
            summary['status'] = 'already_running'
            summary['duration_seconds'] = round(time.time() - started, 3)
            logger.info(
                "archive_retention: skipped — another prune is already "
                "in flight (returning status='already_running')"
            )
            return summary
        _retention_running = True

    try:
        acquired = task_coordinator.acquire_task(
            _RETENTION_COORDINATOR_TASK,
            wait_seconds=_RETENTION_COORDINATOR_WAIT_SECONDS,
        )
        if not acquired:
            logger.info(
                "archive_retention: skipped — could not acquire 'retention' "
                "task slot within %.1fs",
                _RETENTION_COORDINATOR_WAIT_SECONDS,
            )
            summary['duration_seconds'] = round(time.time() - started, 3)
            return summary

        try:
            for path, mtime, _size in _iter_archive_mp4_files(archive_root):
                summary['scanned'] += 1
                if mtime > cutoff:
                    continue
                age_days = (time.time() - mtime) / 86400.0
                if enforce_cloud_check:
                    if not _is_synced_to_cloud(path, archive_root, cloud_db_path):
                        summary['kept_unsynced_count'] += 1
                        logger.warning(
                            "archive_retention: KEPT %s past retention "
                            "(age=%.1f days) — not yet synced to cloud",
                            path, age_days,
                        )
                        continue
                freed = _delete_one_mp4(path, db_path)
                if freed > 0 or not os.path.exists(path):
                    summary['deleted_count'] += 1
                    summary['freed_bytes'] += freed
                    logger.info(
                        "archive_retention: removed %s (age=%.1f days, "
                        "freed=%d bytes)",
                        path, age_days, freed,
                    )
        finally:
            # Release BEFORE any further sleep / outside callers.
            task_coordinator.release_task(_RETENTION_COORDINATOR_TASK)
            summary['duration_seconds'] = round(time.time() - started, 3)
    finally:
        # Always clear the duplicate-trigger guard, even on exception
        # or short-circuited acquire_task. Otherwise a single failed
        # prune would lock out every subsequent attempt.
        with _state_lock:
            _retention_running = False

    if summary['kept_unsynced_count'] > 0:
        logger.info(
            "archive_retention: kept %d clip(s) past retention because "
            "they have not yet been backed up to the cloud "
            "(toggle 'Delete clips even if not backed up' in Settings → "
            "Cloud Sync to override)",
            summary['kept_unsynced_count'],
        )
    return summary


def force_prune_now() -> Dict[str, Any]:
    """Run a retention prune synchronously. Returns the summary dict.

    Exposed via ``POST /api/archive/prune_now`` and the Settings →
    Storage panel. Called inline on the request thread; for
    ArchivedClips of a few hundred files this completes in under a
    second.
    """
    with _state_lock:
        archive_root = _archive_root
        db_path = _db_path
    if not archive_root or not db_path:
        return {
            'deleted_count': 0,
            'freed_bytes': 0,
            'scanned': 0,
            'error': 'watchdog not started',
        }
    retention_days = _resolve_retention_days()
    summary = _run_retention_prune(archive_root, db_path, retention_days)
    # Issue #91: when short-circuited because another prune is already
    # running, do NOT touch ``_retention_state`` — overwriting the
    # in-flight first run's eventual results with zeros would corrupt
    # the Settings panel's "last prune" display. Caller (the blueprint)
    # propagates the ``status`` field to the front end.
    if summary.get('status') == 'already_running':
        return summary
    # Update bookkeeping so the Settings panel reflects the manual run.
    with _state_lock:
        _retention_state['last_prune_at'] = _iso_now()
        _retention_state['last_prune_deleted'] = int(summary['deleted_count'])
        _retention_state['last_prune_freed_bytes'] = int(summary['freed_bytes'])
        _retention_state['last_prune_kept_unsynced'] = int(
            summary.get('kept_unsynced_count', 0)
        )
        _retention_state['last_prune_error'] = None
        _retention_state['next_prune_due_at'] = (
            time.time() + _RETENTION_INTERVAL_SECONDS
        )
    return summary


# ---------------------------------------------------------------------------
# Watchdog thread loop
# ---------------------------------------------------------------------------

def _maybe_run_retention(archive_root: str, db_path: str) -> None:
    """Run retention prune if the daily interval has elapsed.

    Called from the watchdog tick. Updates ``_retention_state`` either
    way so the Settings panel can show "next prune in N hours".
    """
    with _state_lock:
        due_at = _retention_state.get('next_prune_due_at')
    if due_at is None or time.time() < float(due_at):
        return
    retention_days = _resolve_retention_days()
    try:
        summary = _run_retention_prune(archive_root, db_path, retention_days)
        # Issue #91: when short-circuited because another prune is in
        # flight (e.g. a concurrent UI ``Prune now`` click), don't
        # update bookkeeping or advance ``next_prune_due_at``. The
        # in-flight prune will update both when it completes; we just
        # need to retry the tick promptly (the watchdog loops every
        # ``check_interval`` seconds anyway, and the in-flight run's
        # completion will reset ``next_prune_due_at`` to "now + 24h").
        if summary.get('status') == 'already_running':
            return
        with _state_lock:
            _retention_state['last_prune_at'] = _iso_now()
            _retention_state['last_prune_deleted'] = int(
                summary['deleted_count']
            )
            _retention_state['last_prune_freed_bytes'] = int(
                summary['freed_bytes']
            )
            _retention_state['last_prune_kept_unsynced'] = int(
                summary.get('kept_unsynced_count', 0)
            )
            _retention_state['last_prune_error'] = None
            _retention_state['next_prune_due_at'] = (
                time.time() + _RETENTION_INTERVAL_SECONDS
            )
        logger.info(
            "archive_retention: prune complete (deleted=%d, freed=%d "
            "bytes, scanned=%d, kept_unsynced=%d, %.2fs)",
            summary['deleted_count'], summary['freed_bytes'],
            summary['scanned'],
            summary.get('kept_unsynced_count', 0),
            summary['duration_seconds'],
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("archive_retention: prune failed")
        with _state_lock:
            _retention_state['last_prune_error'] = str(e)
            # Retry tomorrow even on failure — don't loop on a broken FS.
            _retention_state['next_prune_due_at'] = (
                time.time() + _RETENTION_INTERVAL_SECONDS
            )


def _log_severity_change(prev: Optional[str], new: str, message: str) -> None:
    """Log severity transitions at appropriate levels.

    Logged only on transition (not every tick) so the journal stays
    readable. The first run from None always logs at INFO so we have
    a "watchdog started" landmark.
    """
    if prev == new:
        return
    if new == 'critical':
        logger.critical("archive_watchdog: %s", message)
    elif new == 'error':
        logger.error("archive_watchdog: %s", message)
    elif new == 'warning':
        logger.warning("archive_watchdog: %s", message)
    else:
        logger.info("archive_watchdog: %s", message)


def _run_loop(db_path: str, archive_root: str, interval_seconds: float) -> None:
    """The thread target. One pass per interval until stop is signaled."""
    prev_severity: Optional[str] = None
    while not _stop_event.is_set():
        try:
            snap = _compute_health(db_path, archive_root)
            with _state_lock:
                _last_health.update(snap)
            _log_severity_change(
                prev_severity, snap['severity'], snap['message'],
            )
            prev_severity = snap['severity']
        except sqlite3.Error as e:
            logger.warning("archive_watchdog: DB error during tick: %s", e)
        except Exception as e:  # noqa: BLE001
            logger.exception("archive_watchdog: unexpected tick failure")

        # Retention is interleaved into the watchdog cadence to avoid
        # spawning a second thread on the Pi Zero 2 W.
        try:
            _maybe_run_retention(archive_root, db_path)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "archive_watchdog: retention interleave failed"
            )

        # Sleep with wake-event support so callers can force an
        # immediate re-check (e.g. after disk-space recovery). We wait
        # on the wake event; ``stop_watchdog()`` also sets it so a
        # shutdown unblocks instantly. The stop check after the wait
        # ensures we exit promptly when both events are set.
        woke = _wake_event.wait(timeout=interval_seconds)
        if _stop_event.is_set():
            break
        if woke:
            _wake_event.clear()
