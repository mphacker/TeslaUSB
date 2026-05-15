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

# Issue #208: yield the 'retention' task_coordinator lock every N files
# so a multi-thousand-file sweep doesn't block the indexer / archive
# worker for 5+ minutes (observed pre-fix max_hold = 311 s on 5904
# clips, which is 3.5x the 90 s hardware watchdog timeout). At 100 files
# per batch and ~30-50 ms per delete decision (stat + DB reconcile),
# each held window is ≤ 5 s — well within safety.
_RETENTION_YIELD_EVERY_N_FILES = 100
# Brief sleep between batches so other workers can actually grab the
# lock before we re-acquire. Long enough to let a waiter through, short
# enough that a 5904-file sweep adds < 4 s of yield overhead total.
_RETENTION_YIELD_SLEEP_SECONDS = 0.05

# Diagnostic subdirectory inside ``archive_root`` that the prune must
# never touch. Mirrors the worker's dead-letter sidecar location.
_DEAD_LETTER_DIRNAME = '.dead_letter'
# Issue #184 Wave 3 — Phase H. Archive worker stages all in-flight
# ``.partial`` files in this single directory. Retention/sweep
# walkers must skip it so partials aren't mistaken for archived
# clips and so the dir stays exclusively owned by archive_worker.
_STAGING_DIRNAME = '.staging'

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
    """Return the configured ArchivedClips retention in days.

    Phase 3a.2 (#98) resolution order — first non-zero / non-empty wins:

    1. ``cleanup.policies.ArchivedClips.retention_days`` (per-folder
       override in the unified config section).
    2. ``cleanup.default_retention_days`` (the unified default).
    3. ``cloud_archive.archived_clips_retention_days`` (legacy SD-card
       retention key — preserved for backward compat). NOTE:
       ``CLOUD_ARCHIVE_RETENTION_DAYS`` itself folds the even-older
       ``archive.retention_days`` legacy key in via ``config.py`` — so
       the resolver only checks two named tiers but covers three legacy
       keys in total.
    4. Hard-coded ``30`` if nothing else resolves.

    Reads ``config.yaml`` directly on every call so Settings → Storage &
    Retention edits take effect on the next prune pass without
    restarting the service. The ``config`` module's cached attributes
    (``CLEANUP_*``, ``CLOUD_ARCHIVE_RETENTION_DAYS``) are loaded once at
    startup and would otherwise mask saved YAML changes until restart.
    Falls back to the cached attributes if the direct read fails.
    """
    # Direct YAML read — bypass the cached config.* attributes so a save
    # from the Settings UI is visible on the next prune.
    try:
        import yaml
        from config import CONFIG_YAML
        with open(CONFIG_YAML, 'r') as f:
            cfg = yaml.safe_load(f) or {}
        cleanup = cfg.get('cleanup') if isinstance(cfg.get('cleanup'), dict) else {}
        policies = cleanup.get('policies') if isinstance(cleanup.get('policies'), dict) else {}
        archived = policies.get('ArchivedClips') if isinstance(policies, dict) else None
        if isinstance(archived, dict):
            try:
                override = int(archived.get('retention_days') or 0)
                if override > 0:
                    return override
            except (TypeError, ValueError):
                pass
        try:
            d = int(cleanup.get('default_retention_days') or 0)
            if d > 0:
                return d
        except (TypeError, ValueError):
            pass
        cloud_archive = cfg.get('cloud_archive') if isinstance(cfg.get('cloud_archive'), dict) else {}
        try:
            c = int(cloud_archive.get('archived_clips_retention_days') or 0)
            if c > 0:
                return c
        except (TypeError, ValueError):
            pass
        archive = cfg.get('archive') if isinstance(cfg.get('archive'), dict) else {}
        try:
            a = int(archive.get('retention_days') or 0)
            if a > 0:
                return a
        except (TypeError, ValueError):
            pass
    except Exception:  # noqa: BLE001
        # Direct YAML read failed for some reason; fall back to the
        # cached config module attributes so we never crash the prune.
        pass

    try:
        from config import CLEANUP_POLICIES
        archived = CLEANUP_POLICIES.get('ArchivedClips') if CLEANUP_POLICIES else None
        if isinstance(archived, dict):
            override = int(archived.get('retention_days') or 0)
            if override > 0:
                return override
    except Exception:  # noqa: BLE001
        pass
    try:
        from config import CLEANUP_DEFAULT_RETENTION_DAYS
        if int(CLEANUP_DEFAULT_RETENTION_DAYS) > 0:
            return int(CLEANUP_DEFAULT_RETENTION_DAYS)
    except Exception:  # noqa: BLE001
        pass
    try:
        from config import CLOUD_ARCHIVE_RETENTION_DAYS
        if int(CLOUD_ARCHIVE_RETENTION_DAYS) > 0:
            return int(CLOUD_ARCHIVE_RETENTION_DAYS)
    except Exception:  # noqa: BLE001
        pass
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
        # Issue #180 — informative rather than alarming. 5–10 min
        # without a copy is the normal signature of a load-pause
        # under heavy backlog (Pi Zero 2 W's SDIO bus contention),
        # not a "videos may be lost" emergency. Keep the metric so
        # the operator can correlate with system load.
        stale_sev = 'warning'
        stale_msg = (
            f"Archive worker slow: no copy in "
            f"{int(last_copy_age_seconds // 60)} min "
            f"({pending_count} queued). Often caused by SD-card load."
        )
    elif last_copy_age_seconds < _STALE_CRITICAL_SECONDS:
        # Issue #180 — escalate the wording rather than the alarm.
        # 10–20 min is concerning but not yet "videos are being
        # lost" — Tesla's RecentClips circular buffer is ~60 min,
        # so we still have time to drain.
        stale_sev = 'error'
        stale_msg = (
            f"Archive worker not making progress: no copy in "
            f"{int(last_copy_age_seconds // 60)} min "
            f"({pending_count} queued). Check system load and "
            f"SD-card health."
        )
    else:
        # 20 min+ stall with pending work IS the genuine emergency —
        # Tesla's RecentClips ring buffer is rolling clips out faster
        # than we can copy them. Keep the loud alarm for this case.
        stale_sev = 'critical'
        stale_msg = (
            f"Archive worker is STALLED: no copy in "
            f"{int(last_copy_age_seconds // 60)} min "
            f"({pending_count} queued) — videos are being lost!"
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

    # Issue #180 follow-up — only flag the snapshot as ``actionable``
    # when the operator can actually do something to fix the problem.
    # Most ERROR/CRITICAL severities are caused by transient backlog +
    # SDIO contention, where the worker is doing its best and the
    # right "action" is just to wait. Popping a banner the operator
    # has no remedy for is pure annoyance — the system-health card
    # surfaces the underlying numbers either way for diagnostics.
    #
    # Only two conditions are genuinely user-fixable:
    #   1. Worker is not running while clips are pending  → restart
    #      the gadget_web service / check journalctl.
    #   2. SD-card free space is below the CRITICAL threshold → free
    #      space or expand storage.
    #
    # A 20-minute+ stall on a *running* worker COULD be a real bug,
    # but the operator still can't diagnose it from the web UI; if
    # they can't act on it, we shouldn't yell at them about it. The
    # underlying staleness is still visible in the System Health card
    # message so it's not hidden — just demoted from a banner.
    worker_down_with_work = (not worker_running) and pending_count > 0
    disk_critical = disk_known and disk_free_mb < disk_critical_mb
    actionable = bool(worker_down_with_work or disk_critical)

    snap: Dict[str, Any] = {
        'severity': severity,
        'message': message,
        'actionable': actionable,
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
        # Prune .dead_letter and .staging so os.walk doesn't descend
        # into them (issue #184 Wave 3 — Phase H).
        dirnames[:] = [
            d for d in dirnames
            if d not in (_DEAD_LETTER_DIRNAME, _STAGING_DIRNAME)
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
    Geodata is reconciled only when the helper reports
    :data:`DeleteOutcome.DELETED` (so a 0-byte clip that was actually
    removed is still reconciled even though ``bytes_freed`` is 0).
    """
    from services.file_safety import safe_delete_archive_video, DeleteOutcome
    result = safe_delete_archive_video(path)
    if result.outcome is not DeleteOutcome.DELETED:
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
    return result.bytes_freed


def _yield_retention_lock() -> bool:
    """Issue #208: drop the 'retention' task slot, sleep briefly, re-acquire.

    Lets a waiting indexer / archive_worker get a turn between
    batches of N files. Returns True when the lock was reacquired
    (caller should keep going), False when reacquire failed (caller
    should bail with a partial summary — something else is genuinely
    holding the lock and we shouldn't starve it forever).

    Implementation note: we always sleep ``_RETENTION_YIELD_SLEEP_SECONDS``
    even if no waiter exists. Asking ``task_coordinator.waiter_count()``
    is cheap, but the sleep itself is the cooperation primitive — without
    it a quick release/reacquire round-trip would not give a competing
    thread enough time to actually wake from its ``threading.Event.wait``
    and grab the lock.
    """
    task_coordinator.release_task(_RETENTION_COORDINATOR_TASK)
    time.sleep(_RETENTION_YIELD_SLEEP_SECONDS)
    return task_coordinator.acquire_task(
        _RETENTION_COORDINATOR_TASK,
        wait_seconds=_RETENTION_COORDINATOR_WAIT_SECONDS,
    )


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

        # ``lock_held`` tracks whether we still own the 'retention'
        # task slot at any given point in the loop body. It is set
        # to False by the yield-bailout branch so the ``finally`` below
        # knows not to double-release a lock we already gave up.
        lock_held = True
        try:
            files_since_yield = 0
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
                # Issue #208: yield the lock every N files so a 5904-clip
                # sweep doesn't block the indexer / archive worker for
                # 5+ minutes (max_hold = 311 s observed pre-fix).
                files_since_yield += 1
                if files_since_yield >= _RETENTION_YIELD_EVERY_N_FILES:
                    files_since_yield = 0
                    if not _yield_retention_lock():
                        # Couldn't reacquire — something else is
                        # genuinely holding the lock. Bail with the
                        # partial summary we've built so far; next
                        # retention tick will pick up where we left off.
                        lock_held = False
                        summary['status'] = 'yielded_lost_lock'
                        logger.info(
                            "archive_retention: yielded 'retention' "
                            "lock after %d scanned/%d deleted and could "
                            "not reacquire within %.1fs — bailing with "
                            "partial summary; next tick will resume",
                            summary['scanned'], summary['deleted_count'],
                            _RETENTION_COORDINATOR_WAIT_SECONDS,
                        )
                        break
        finally:
            # Release BEFORE any further sleep / outside callers.
            # When the loop bailed via yielded_lost_lock we already do
            # NOT hold the lock; only release if we still do.
            if lock_held:
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


def reclaim_stationary_recent_clips(*,
                                    db_path: Optional[str] = None,
                                    archive_root: Optional[str] = None,
                                    min_age_hours: int = 1,
                                    ) -> Dict[str, Any]:
    """Issue #167 — delete already-archived stationary RecentClips.

    Walks ``<archive_root>/RecentClips/*.mp4`` and deletes every clip
    that the indexer classified as stationary (``waypoint_count = 0``
    AND ``event_count = 0`` in ``indexed_files``) AND has no
    SentryClips / SavedClips counterpart with the same basename. The
    counterpart check protects user-meaningful events: Tesla writes
    the same clip into both ``RecentClips/`` (rolling buffer) and the
    event subfolder when an event triggers, and we should never delete
    the only copy of a saved-event clip.

    Why this is safe to ship behind a single button (no per-file
    confirm):

    * Routes every delete through
      :func:`services.file_safety.safe_delete_archive_video` — the
      single doorway that enforces the ``*.img`` / protected-file
      guard.
    * Reconciles geodata via
      :func:`mapping_service.purge_deleted_videos` for each deleted
      clip, which only deletes the orphan ``indexed_files`` row and
      NULLs ``waypoints.video_path`` / ``detected_events.video_path``.
      The "trips are sacred" invariant is preserved.
    * Refuses to delete files newer than ``min_age_hours`` (default
      1 h) so a clip Tesla just wrote and the indexer hasn't seen yet
      can't be preemptively wiped.
    * Holds the ``task_coordinator`` 'retention' slot for the
      duration so the archive worker yields cleanly. Releases the
      slot before returning.
    * Single-flight: re-uses the same ``_retention_running`` guard as
      the daily prune, so a stacked Settings click can't pile up two
      reclaim passes.

    Caller args (both optional — startup state is the default):

    * ``db_path``: path to ``geodata.db``. Defaults to the value
      ``start_watchdog`` was called with.
    * ``archive_root``: SD-card archive root. Defaults to the value
      ``start_watchdog`` was called with.
    * ``min_age_hours``: don't touch files younger than this. Default
      1 h. Set to 0 ONLY for testing.

    Returns a summary dict::

        {'deleted_count': N, 'freed_bytes': M, 'scanned': K,
         'kept_too_new': T, 'kept_has_event_counterpart': E,
         'kept_unindexed': U, 'kept_has_gps': G,
         'kept_has_event_only': V,
         'duration_seconds': S}

    Bucket semantics:

    * ``kept_has_gps`` — clip has GPS waypoints (``waypoint_count > 0``).
      Driving footage; keep.
    * ``kept_has_event_only`` — clip has detected events but no GPS
      (``waypoint_count = 0 AND event_count > 0``). Stationary
      Sentry-mode footage Tesla flagged as an event; keep.
    * ``kept_unindexed`` — indexer hasn't seen the clip yet; keep
      until it has.
    * ``kept_has_event_counterpart`` — stationary clip that ALSO
      exists under SentryClips/SavedClips with the same basename;
      keep so the user-meaningful event copy is never the only one.
    * ``kept_too_new`` — clip mtime is newer than ``min_age_hours``.

    On any error (missing args, watchdog never started, ``acquire_task``
    timeout) returns the same dict with the relevant ``error`` /
    ``status`` field populated; never raises.
    """
    global _retention_running
    started = time.time()
    summary: Dict[str, Any] = {
        'deleted_count': 0,
        'freed_bytes': 0,
        'scanned': 0,
        'kept_too_new': 0,
        'kept_has_event_counterpart': 0,
        'kept_unindexed': 0,
        'kept_has_gps': 0,
        'kept_has_event_only': 0,
        'min_age_hours': int(min_age_hours),
        'duration_seconds': 0.0,
    }

    if db_path is None or archive_root is None:
        with _state_lock:
            if db_path is None:
                db_path = _db_path
            if archive_root is None:
                archive_root = _archive_root
    if not archive_root or not db_path:
        summary['error'] = 'watchdog not started'
        summary['duration_seconds'] = round(time.time() - started, 3)
        return summary

    # Issue #91 — single-flight guard. Same flag as the periodic prune
    # so a stacked "Run cleanup now" + "Reclaim stationary" + watchdog
    # tick can't pile up. Set BEFORE acquire_task so the second caller
    # short-circuits even if the first is still queued on the slot.
    # Checked before the missing-RecentClips early-return so a stacked
    # call still sees the in-flight status (the test contract — a
    # second call should never silently no-op).
    with _state_lock:
        if _retention_running:
            summary['status'] = 'already_running'
            summary['duration_seconds'] = round(time.time() - started, 3)
            logger.info(
                "reclaim_stationary: skipped — another prune is already "
                "in flight (returning status='already_running')"
            )
            return summary
        _retention_running = True

    recent_root = os.path.join(archive_root, 'RecentClips')
    if not os.path.isdir(recent_root):
        with _state_lock:
            _retention_running = False
        summary['duration_seconds'] = round(time.time() - started, 3)
        return summary

    cutoff_mtime = started - (max(int(min_age_hours), 0) * 3600)

    try:
        acquired = task_coordinator.acquire_task(
            _RETENTION_COORDINATOR_TASK,
            wait_seconds=_RETENTION_COORDINATOR_WAIT_SECONDS,
        )
        if not acquired:
            logger.info(
                "reclaim_stationary: skipped — could not acquire 'retention' "
                "task slot within %.1fs",
                _RETENTION_COORDINATOR_WAIT_SECONDS,
            )
            summary['duration_seconds'] = round(time.time() - started, 3)
            return summary

        try:
            stationary_set = _collect_indexed_recent_paths(
                db_path, archive_root, stationary_only=True,
            )
            indexed_set = _collect_indexed_recent_paths(
                db_path, archive_root, stationary_only=False,
            )
            event_only_set = _collect_indexed_recent_paths(
                db_path, archive_root,
                stationary_only=False, event_only=True,
            )
            event_basenames = _collect_event_basenames(archive_root)

            lock_held = True
            files_since_yield = 0
            for raw_path, mtime, _size in _iter_archive_mp4_files(
                recent_root,
            ):
                summary['scanned'] += 1
                if mtime > cutoff_mtime:
                    summary['kept_too_new'] += 1
                    continue
                # Realpath both sides of the membership check so a
                # symlinked archive_root (e.g. /mnt/sdcard/archives ->
                # /home/pi/ArchivedClips) doesn't silently no-op.
                # On the production Pi neither path is symlinked so
                # realpath is a cheap stat. Fall back to raw path on
                # OSError so a transient stat failure doesn't crash
                # the whole pass.
                try:
                    path = os.path.realpath(raw_path)
                except OSError:
                    path = raw_path
                if path not in stationary_set:
                    if path in event_only_set:
                        # Indexed, no GPS, but Tesla flagged it as an
                        # event (e.g. Sentry trigger while parked).
                        summary['kept_has_event_only'] += 1
                    elif path in indexed_set:
                        summary['kept_has_gps'] += 1
                    else:
                        summary['kept_unindexed'] += 1
                    continue
                basename = os.path.basename(path)
                if basename in event_basenames:
                    summary['kept_has_event_counterpart'] += 1
                    continue
                freed = _delete_one_mp4(path, db_path)
                if freed > 0 or not os.path.exists(path):
                    summary['deleted_count'] += 1
                    summary['freed_bytes'] += freed
                    logger.info(
                        "reclaim_stationary: removed %s (freed=%d bytes)",
                        path, freed,
                    )
                # Issue #208: yield the lock every N files so a
                # large RecentClips sweep doesn't block the indexer /
                # archive worker for minutes at a time.
                files_since_yield += 1
                if files_since_yield >= _RETENTION_YIELD_EVERY_N_FILES:
                    files_since_yield = 0
                    if not _yield_retention_lock():
                        lock_held = False
                        summary['status'] = 'yielded_lost_lock'
                        logger.info(
                            "reclaim_stationary: yielded 'retention' "
                            "lock after %d scanned/%d deleted and "
                            "could not reacquire within %.1fs — bailing "
                            "with partial summary; next tick will resume",
                            summary['scanned'], summary['deleted_count'],
                            _RETENTION_COORDINATOR_WAIT_SECONDS,
                        )
                        break
        finally:
            if lock_held:
                task_coordinator.release_task(_RETENTION_COORDINATOR_TASK)
            summary['duration_seconds'] = round(time.time() - started, 3)
    finally:
        with _state_lock:
            _retention_running = False

    logger.info(
        "reclaim_stationary: done — deleted=%d, freed_bytes=%d, "
        "scanned=%d, kept_too_new=%d, kept_event_counterpart=%d, "
        "kept_unindexed=%d, kept_has_gps=%d, kept_has_event_only=%d, "
        "duration=%.2fs",
        summary['deleted_count'], summary['freed_bytes'],
        summary['scanned'], summary['kept_too_new'],
        summary['kept_has_event_counterpart'],
        summary['kept_unindexed'], summary['kept_has_gps'],
        summary['kept_has_event_only'],
        summary['duration_seconds'],
    )
    return summary


def _collect_indexed_recent_paths(db_path: str,
                                  archive_root: str,
                                  *,
                                  stationary_only: bool = False,
                                  event_only: bool = False,
                                  ) -> set:
    """Return the set of realpath'd RecentClips paths in
    ``indexed_files`` matching the requested classification.

    One SELECT, filter in Python (the SQL ``LIKE`` over a
    ``%/RecentClips/%`` literal is buggy on Windows path separators
    and brittle if Tesla ever changes the folder name). Realpath is
    applied so callers can use ``in`` for membership against the
    realpath'd path the worker loop will produce.

    * ``stationary_only=True`` keeps only rows where
      ``waypoint_count = 0 AND event_count = 0`` (the deletion
      candidates).
    * ``event_only=True`` keeps only rows where
      ``waypoint_count = 0 AND event_count > 0`` (stationary clips
      Tesla flagged as events). Used for the
      ``kept_has_event_only`` bucket.
    * Both False = every RecentClips row regardless of classification
      (used for the ``kept_has_gps`` vs ``kept_unindexed`` distinction).

    Returns an empty set on any DB error.
    """
    out: set = set()
    if not db_path or not os.path.isfile(db_path):
        return out
    recent_root_norm = os.path.realpath(
        os.path.join(archive_root, 'RecentClips')
    ) + os.sep
    if stationary_only:
        where = "WHERE waypoint_count = 0 AND event_count = 0"
    elif event_only:
        where = "WHERE waypoint_count = 0 AND event_count > 0"
    else:
        where = ""
    conn = None
    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True,
                               timeout=5.0)
        for row in conn.execute(
            f"SELECT file_path FROM indexed_files {where}"
        ):
            file_path = row[0] or ''
            try:
                resolved = os.path.realpath(file_path)
            except OSError:
                continue
            if not resolved.startswith(recent_root_norm):
                continue
            out.add(resolved)
    except sqlite3.Error as e:
        logger.warning(
            "reclaim_stationary: indexed-paths lookup failed "
            "(stationary_only=%s, event_only=%s): %s",
            stationary_only, event_only, e,
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    return out


def _collect_event_basenames(archive_root: str) -> set:
    """Return the set of ``.mp4`` basenames present under
    ``SentryClips/`` or ``SavedClips/`` (recursive). Used to refuse
    deleting a RecentClips clip that ALSO appears as a saved-event
    clip — Tesla writes the same recording into both folders when an
    event triggers, and the saved-event copy is the user-meaningful
    one we must preserve even if the RecentClips copy is the only one
    on disk.

    Cheap O(n) directory walk; on a typical install n is a few thousand
    files, finishes in well under a second.
    """
    out: set = set()
    for sub in ('SentryClips', 'SavedClips'):
        root = os.path.join(archive_root, sub)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(
            root, followlinks=False,
        ):
            dirnames[:] = [
                d for d in dirnames
                if d not in (_DEAD_LETTER_DIRNAME, _STAGING_DIRNAME)
            ]
            for fn in filenames:
                if fn.lower().endswith('.mp4'):
                    out.add(fn)
    return out


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
