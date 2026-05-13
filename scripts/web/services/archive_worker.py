"""Single-threaded archive worker (issue #76 — Phase 2b).

Drains the ``archive_queue`` table one file at a time. For each row:

1. Pick + claim the next ``pending`` row, ordered ``priority ASC,
   expected_mtime ASC NULLS LAST`` (RecentClips first, then
   Sentry/Saved, then everything else; closest-to-Tesla-rotation first
   within each band).
2. Run a "fully written" gate: re-stat the source; if the file is
   younger than 5 s AND its size or mtime drift past the values seen
   when it was enqueued, release the claim with refreshed metadata and
   try again on the next iteration. Tesla writes clips in chunks; we
   never want to copy a half-written clip.
3. Acquire the ``task_coordinator`` slot. The archive worker is a
   periodic priority task (per the task_coordinator contract docstring),
   so it uses the **blocking** ``acquire_task('archive', wait_seconds=N)``
   form — it waits a bounded time for the indexer to yield, then
   proceeds. NOT the indexer's ``yield_to_waiters=True`` cyclic form.
4. Compute the destination under ``ARCHIVE_DIR`` mirroring the
   ``TeslaCam/<sub>/<file>`` layout.
5. Atomic copy: write to ``dest_path + '.partial'`` in 1-MiB chunks,
   ``fsync``, ``rename`` to the final name, verify size matches the
   source.
6. On success, mark the row ``copied`` and enqueue the **destination**
   path into ``indexing_queue`` via
   ``indexing_queue_service.enqueue_for_indexing`` so the indexer
   picks it up next.
7. Failure handling:
   * ``FileNotFoundError`` (source rotated by Tesla mid-flight) → mark
     ``source_gone``. No retry, no dead-letter.
   * Any other ``OSError`` / ``shutil.Error`` / ``sqlite3.Error`` →
     bump ``attempts``; at ``attempts >= retry_max_attempts`` the row
     transitions to ``dead_letter`` and a sidecar text file lands at
     ``~/ArchivedClips/.dead_letter/<id>.txt`` for forensics.

**Hard contract (do NOT break — see copilot-instructions.md):**

* This module never imports or calls anything that touches the USB
  gadget — no ``mount``, ``umount``, ``losetup``, ``nsenter``,
  ``partition_mount_service``, ``quick_edit_part2``, or
  ``rebind_usb_gadget``. Tesla may be actively recording; ANY USB
  disruption from a background subsystem loses footage.
* The ``task_coordinator`` lock is **always released before any sleep**.
  Holding the lock across ``_stop_event.wait()`` was the May 7
  starvation bug — never re-introduce it.
* No heavy imports. ``os``, ``shutil``, ``sqlite3``, ``logging``,
  ``threading``, ``time`` only — the Pi Zero 2 W steady-state RSS
  budget for this thread is ~30 MB.

Public API mirrors :mod:`indexing_worker`::

    start_worker(db_path, archive_root, *, teslacam_root=None) -> bool
    stop_worker(timeout=...)               -> bool
    pause_worker(timeout=...)              -> bool
    resume_worker()                        -> None
    is_paused()                            -> bool
    is_running()                           -> bool
    ensure_worker_started()                -> bool
    wake()                                 -> None       # poke an idle loop
    get_status()                           -> dict
"""

from __future__ import annotations

import collections
import logging
import os
import shutil
import sys
import threading
import time
import uuid
from typing import Any, Callable, Deque, Dict, Optional

from services import archive_queue
from services import task_coordinator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables — kept module-level so tests can monkeypatch
# ---------------------------------------------------------------------------

# Sleep between successful copies (gives the kernel time to flush).
# The Pi Zero 2 W shares one SDIO controller between SD card and WiFi;
# tight back-to-back copies during catch-up can starve the watchdog
# daemon. Configurable via ``archive_queue.inter_file_sleep_seconds``
# (default 1.0 s) — read at startup by ``_read_config_or_defaults``.
_INTER_FILE_SLEEP_SECONDS = 1.0
# Default 1-min loadavg above which the worker pauses for
# ``_LOAD_PAUSE_SECONDS`` before claiming the next row. Configurable
# via ``archive_queue.load_pause_threshold``.
_LOAD_PAUSE_THRESHOLD = 3.5
# How long to sleep when the load threshold is exceeded. Configurable
# via ``archive_queue.load_pause_seconds``.
_LOAD_PAUSE_SECONDS = 30.0
# Sleep when the queue is empty. Wake() can shorten this on a producer hit.
_IDLE_SLEEP_SECONDS = 5.0
# Sleep on a transient claim error or task_coordinator timeout.
_BACKOFF_SLEEP_SECONDS = 0.5
# Stable-write age gate — files modified within this many seconds get
# re-queued so we don't copy a clip Tesla is still writing.
# Configurable via ``archive_queue.stable_write_age_seconds``. The
# module-level default below is the fallback when config isn't
# importable (unit-test envs without the full app); the runtime value
# is read by ``_stable_write_age_seconds()`` at call time.
_STABLE_WRITE_AGE_SECONDS = 5.0
# task_coordinator wait when acquiring the archive slot. The archive
# worker is a periodic priority task (per the task_coordinator
# docstring) — it BLOCK-waits for a slot rather than yielding cyclically.
_COORDINATOR_WAIT_SECONDS = 60.0
# Pause/stop defaults match the indexer.
_DEFAULT_PAUSE_TIMEOUT = 30.0
_DEFAULT_STOP_TIMEOUT = 30.0
# task_coordinator label for this worker. Distinct from 'indexer' so
# the fairness model can prioritize archive over indexing.
_COORDINATOR_TASK = 'archive'
# Default copy buffer; the worker reads it from config at start.
_DEFAULT_COPY_CHUNK_BYTES = 1024 * 1024
# Mid-copy SDIO-contention safeguards (issue #104).
# When 1-min loadavg crosses ``_LOAD_PAUSE_THRESHOLD`` between chunks,
# ``_atomic_copy`` sleeps for this duration before reading the next
# chunk. Cheap O(1) ``getloadavg`` syscall + tiny sleep yields the
# userspace ``watchdog`` daemon enough CPU + ``/dev/watchdog`` write
# bandwidth to ping the BCM2835 hardware watchdog (90s timeout).
# Configurable via ``archive_queue.chunk_pause_seconds`` (default 0.25).
_CHUNK_PAUSE_SECONDS = 0.25
# Per-file copy budget. If a single ``_atomic_copy`` exceeds this many
# wall-clock seconds, raise ``_CopyTimeBudgetExceeded`` so the caller
# releases the claim back to ``pending`` (without bumping ``attempts``)
# and the next iteration's between-files load-pause guard gets a chance
# to fire. ``0.0`` disables the budget. Configurable via
# ``archive_queue.per_file_time_budget_seconds`` (default 60.0).
_PER_FILE_TIME_BUDGET_SECONDS = 60.0

# Phase 4.4 (#101) — drain-rate ETA tunables.
# Number of recent ``copied`` completion timestamps kept for rate
# estimation. 50 keeps the rolling window honest without wasting RAM
# (50 × 8 bytes = 400 bytes). With a typical ~3 s/clip cadence, 50 copies
# spans ~2.5 min — long enough to smooth out short-term jitter, short
# enough to react when the pace shifts (e.g. SDIO contention slows things
# down).
_DRAIN_RATE_WINDOW_SIZE = 50
# If the most recent copy completion is older than this, the rate is
# considered "stale" and the ETA is suppressed. The worker may have been
# idle (queue empty), paused (load/disk), or simply between catch-up
# bursts — none of those are useful predictors of how fast the *next*
# burst will drain. Without this guard, a 2 h gap followed by a sudden
# 1 000-row enqueue would render an absurdly low rate estimate.
_DRAIN_RATE_FRESHNESS_SECONDS = 600.0  # 10 minutes
# Minimum number of completion samples needed before computing a rate.
# A rate computation needs at least 2 samples to derive an inter-sample
# span; we require 3 so we have at least 2 inter-sample gaps to average
# (single-gap variance is too high). The user sees ETA only after the
# worker has shown it can sustain the pace.
_DRAIN_RATE_MIN_SAMPLES = 3
# Cap the displayed ETA so a transient slow start (first few files of a
# huge backlog drained at sub-second rates after a long pause) doesn't
# show "est. 47 hours". Anything above this just shows ">N hours".
_DRAIN_RATE_ETA_CAP_SECONDS = 24 * 3600


# ---------------------------------------------------------------------------
# Module state — all access through _state_lock
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_worker_thread: Optional[threading.Thread] = None
_worker_id: Optional[str] = None
_stop_event = threading.Event()
_pause_event = threading.Event()
_idle_event = threading.Event()
_idle_event.set()
# Wake event lets producers (or the NM dispatcher trigger) shorten the
# idle sleep to "right now" without spinning the worker.
_wake_event = threading.Event()
_db_path: Optional[str] = None
_archive_root: Optional[str] = None
_teslacam_root: Optional[str] = None
# Phase 4.4: rolling window of recent successful copy completion epochs
# (``time.time()``). Trimmed to ``_DRAIN_RATE_WINDOW_SIZE`` via
# ``deque(maxlen=...)``. Read under ``_state_lock``; any append happens
# from the single worker thread under the same lock for cleanliness even
# though deque append is itself thread-safe — uniform locking keeps the
# rate computation self-consistent with the snapshot read.
_recent_copy_completions: Deque[float] = collections.deque(
    maxlen=_DRAIN_RATE_WINDOW_SIZE,
)
_state: Dict[str, Any] = {
    'active_file': None,
    'last_outcome': None,
    'last_error': None,
    'files_done_session': 0,
    'last_drained_at': None,
    'last_disk_pause_at': None,
    'last_disk_pause_free_mb': None,
    'last_disk_pause_total_mb': None,
    'last_load_pause_at': None,
    'last_load_pause_loadavg': None,
}

# Disk-space self-pause epoch (seconds). When set in the future, the
# worker loop idles instead of claiming new rows. Set when the disk
# falls below the configured critical threshold during
# :func:`process_one_claim`; cleared automatically once the deadline
# passes (the watchdog will re-evaluate on its next tick).
_disk_space_pause_until: float = 0.0
# Default duration of the disk-space pause (seconds). Resolved lazily
# from ``cloud_archive.disk_space_pause_seconds`` at first use so the
# config import order stays simple; tests monkeypatch this directly.
_DEFAULT_DISK_SPACE_PAUSE_SECONDS: float = 300.0

# Load-pause self-pause epoch (seconds). When set in the future, the
# worker loop is idling because 1-min loadavg crossed the configured
# threshold (SDIO bus contention guard — see copilot-instructions.md).
# Mirrors the disk-pause pattern so the status endpoint can show
# *why* the worker isn't draining.
_load_pause_until: float = 0.0

# Debounce timer for the disk-critical → cleanup wire-up (Phase 1
# item 1.5). When ``_check_disk_space_guard`` reports 'critical', we
# kick off ``archive_watchdog.force_prune_now()`` in a daemon thread
# so the worker can release its claim and idle immediately, but only
# once per ``_DISK_CRITICAL_CLEANUP_DEBOUNCE_SECONDS`` — without
# this, every claim attempt during the disk-pause window would
# re-trigger the cleanup. Read/written under ``_state_lock``.
_DISK_CRITICAL_CLEANUP_DEBOUNCE_SECONDS = 60.0
_last_disk_critical_cleanup_at: float = 0.0


def _maybe_trigger_critical_cleanup(archive_root: str) -> bool:
    """Trigger ``archive_watchdog.force_prune_now()`` if debounce permits.

    Phase 1 item 1.5: when disk space crosses the critical threshold,
    don't wait up to 24 h for the daily retention timer — kick a prune
    now so the worker can resume draining. Spawned in a daemon thread
    so this function returns immediately; the worker continues to its
    pause loop without blocking on the prune.

    Debounced to one trigger per
    ``_DISK_CRITICAL_CLEANUP_DEBOUNCE_SECONDS`` (60 s). Even if the
    disk-critical signal fires every iteration during the pause
    window, we only call force_prune_now once per minute.

    Lazy import of ``archive_watchdog`` keeps the dependency one-way
    at module load (archive_watchdog does not import archive_worker).

    Returns True if a cleanup thread was actually spawned, False if
    debounced or unavailable.
    """
    global _last_disk_critical_cleanup_at
    now = time.monotonic()
    with _state_lock:
        last = _last_disk_critical_cleanup_at
        if now - last < _DISK_CRITICAL_CLEANUP_DEBOUNCE_SECONDS:
            return False
        _last_disk_critical_cleanup_at = now

    def _do_cleanup():
        try:
            from services import archive_watchdog
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "archive_worker: disk-critical cleanup — could not "
                "import archive_watchdog: %s", e,
            )
            return
        try:
            summary = archive_watchdog.force_prune_now()
            logger.info(
                "archive_worker: disk-critical cleanup complete — "
                "deleted=%d, freed=%d bytes, scanned=%d, %.2fs",
                int(summary.get('deleted_count', 0)),
                int(summary.get('freed_bytes', 0)),
                int(summary.get('scanned', 0)),
                float(summary.get('duration_seconds', 0.0)),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "archive_worker: disk-critical cleanup raised: %s", e,
            )

    logger.info(
        "archive_worker: disk-critical at %s — triggering immediate "
        "retention cleanup (debounced, daemon thread)", archive_root,
    )
    threading.Thread(
        target=_do_cleanup,
        name='archive-worker-critical-cleanup',
        daemon=True,
    ).start()
    return True


def _resolve_disk_space_pause_seconds() -> float:
    """Return the configured disk-space pause duration in seconds.

    Falls back to ``_DEFAULT_DISK_SPACE_PAUSE_SECONDS`` (which tests
    can monkeypatch) when the config attribute is missing or not a
    finite positive number.
    """
    try:
        from config import CLOUD_ARCHIVE_DISK_SPACE_PAUSE_SECONDS as cfg
        cfg_val = float(cfg)
        if cfg_val > 0:
            return cfg_val
    except (ImportError, TypeError, ValueError):
        pass
    return _DEFAULT_DISK_SPACE_PAUSE_SECONDS


# ---------------------------------------------------------------------------
# Public lifecycle API
# ---------------------------------------------------------------------------

def start_worker(db_path: str, archive_root: str, *,
                 teslacam_root: Optional[str] = None) -> bool:
    """Start the worker thread. Idempotent.

    ``archive_root`` is the directory where copied clips land
    (typically ``~/ArchivedClips``). ``teslacam_root`` is the RO USB
    mount root used to compute the relative subpath; it falls back to
    ``services.video_service.get_teslacam_path()`` at call time if
    omitted.
    """
    global _worker_thread, _worker_id, _db_path, _archive_root, _teslacam_root
    with _state_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            logger.warning(
                "archive_worker.start_worker: refusing — existing thread "
                "still alive (id=%s).", _worker_id,
            )
            return False
        _db_path = db_path
        _archive_root = archive_root
        _teslacam_root = teslacam_root
        _worker_id = f"archive-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        _stop_event.clear()
        _pause_event.clear()
        _wake_event.clear()
        _idle_event.set()
        _state['files_done_session'] = 0
        _state['last_drained_at'] = None
        _state['last_error'] = None
        _state['last_outcome'] = None
        _state['active_file'] = None
        _state['last_disk_pause_at'] = None
        _state['last_disk_pause_free_mb'] = None
        _state['last_disk_pause_total_mb'] = None
        _state['last_load_pause_at'] = None
        _state['last_load_pause_loadavg'] = None
        # Phase 4.4: drop any rate samples from the previous instance.
        # A worker restart usually means we paused for a transition
        # (mode switch, manual stop) and the old samples are no longer
        # representative.
        _recent_copy_completions.clear()
        # Reset the disk-space self-pause; the next iteration will
        # re-arm it if disk space is still critical.
        global _disk_space_pause_until, _load_pause_until
        _disk_space_pause_until = 0.0
        _load_pause_until = 0.0
        thread = threading.Thread(
            target=_run_worker_loop,
            args=(db_path, archive_root, teslacam_root, _worker_id),
            name='archive-worker',
            daemon=True,
        )
        _worker_thread = thread
    thread.start()
    logger.info("Archive worker started (id=%s)", _worker_id)
    return True


def stop_worker(timeout: float = _DEFAULT_STOP_TIMEOUT) -> bool:
    """Signal the worker to stop and wait for it to exit.

    Like the indexer's stop_worker: on join timeout we leave
    ``_worker_thread`` in place to block restart — racing two threads
    over the same archive_queue claim rows would be worse than waiting.
    """
    global _worker_thread
    with _state_lock:
        thread = _worker_thread
    if thread is None:
        return True
    _stop_event.set()
    _pause_event.clear()
    _wake_event.set()
    thread.join(timeout=timeout)
    exited = not thread.is_alive()
    if exited:
        with _state_lock:
            if _worker_thread is thread:
                _worker_thread = None
        logger.info("Archive worker stopped cleanly")
    else:
        logger.warning(
            "Archive worker did not exit within %.1fs; "
            "leaving thread reference in place to block restart",
            timeout,
        )
    return exited


def pause_worker(timeout: float = _DEFAULT_PAUSE_TIMEOUT) -> bool:
    """Pause the worker between iterations.

    Mirrors the indexer's pause semantics: returns True if the worker
    is now idle, False on timeout. The caller (mode-switch handler,
    quick_edit caller) should refuse to proceed on False.
    """
    if not _is_running():
        _pause_event.set()
        return True
    _pause_event.set()
    _wake_event.set()
    became_idle = _idle_event.wait(timeout=timeout)
    if not became_idle:
        logger.warning(
            "archive_worker.pause_worker: still mid-file after %.1fs (active=%s)",
            timeout, _state.get('active_file'),
        )
    return became_idle


def resume_worker() -> None:
    """Clear the pause flag so the worker can claim again."""
    _pause_event.clear()
    _wake_event.set()


def is_paused() -> bool:
    return _pause_event.is_set()


def is_running() -> bool:
    return _is_running()


def wake() -> None:
    """Poke the worker out of an idle sleep.

    Producers (the inotify-callback path, the 60-s rescan thread, and
    the NM dispatcher's HTTP wake endpoint) call this after enqueueing
    so a freshly-arrived clip is picked up within milliseconds rather
    than waiting up to ``_IDLE_SLEEP_SECONDS``. Cheap / lock-free /
    safe to call from any thread.
    """
    _wake_event.set()


def ensure_worker_started() -> bool:
    """Lazy-start the worker if it isn't running.

    Mirrors :func:`indexing_worker.ensure_worker_started`. No-op if the
    archive subsystem is disabled, or if the necessary config is
    missing. Returns True iff a worker is running on exit.
    """
    if _is_running():
        return True
    try:
        from config import (
            ARCHIVE_QUEUE_ENABLED, ARCHIVE_DIR, MAPPING_DB_PATH,
        )
        if not ARCHIVE_QUEUE_ENABLED:
            return False
        from services.video_service import get_teslacam_path
        tc = get_teslacam_path()
        return start_worker(MAPPING_DB_PATH, ARCHIVE_DIR, teslacam_root=tc)
    except Exception as e:  # noqa: BLE001
        logger.debug("ensure_worker_started: deferred start failed: %s", e)
        return False


def _is_running() -> bool:
    with _state_lock:
        t = _worker_thread
    return t is not None and t.is_alive()


def get_status() -> Dict[str, Any]:
    """Snapshot for ``/api/archive/status`` (Phase 2c will surface this).

    Combines in-memory worker state with a fresh
    :func:`archive_queue.get_queue_status` snapshot.
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
            'last_outcome': _state['last_outcome'],
            'last_error': _state['last_error'],
            'files_done_session': _state['files_done_session'],
            'last_drained_at': _state['last_drained_at'],
        }
        db_path = _db_path
    counts = {}
    if db_path:
        try:
            counts = archive_queue.get_queue_status(db_path)
        except Exception as e:  # noqa: BLE001 — status must never raise
            logger.warning("get_queue_status failed inside status: %s", e)
            counts = {'queue_status_error': str(e)}
    snap['queue_depth'] = counts.get('pending', 0)
    snap['claimed_count'] = counts.get('claimed', 0)
    snap['dead_letter_count'] = counts.get('dead_letter', 0)
    snap['source_gone_count'] = counts.get('source_gone', 0)
    snap['copied_count'] = counts.get('copied', 0)
    snap['error_count'] = counts.get('error', 0)
    snap['disk_pause'] = get_disk_pause_state()
    snap['load_pause'] = get_load_pause_state()
    # Phase 4.4 — drain-rate ETA. Flatten the rate/samples/stale/ETA
    # fields directly into ``snap`` so JS consumers can read them with
    # a single dict access (no nested lookup, matches the surrounding
    # flat schema like ``queue_depth``, ``error_count``).
    drain = _compute_drain_rate()
    snap['drain_rate_per_sec'] = drain['rate_per_sec']
    snap['drain_rate_samples'] = drain['samples']
    snap['drain_rate_stale'] = drain['stale']
    snap['eta_seconds'] = compute_eta_seconds(
        snap['queue_depth'], drain['rate_per_sec'],
    )
    return snap


# ---------------------------------------------------------------------------
# Helpers (pure / easy to test)
# ---------------------------------------------------------------------------

def compute_dest_path(source_path: str, archive_root: str,
                      teslacam_root: Optional[str]) -> str:
    """Map ``source_path`` under ``teslacam_root`` to its archive home.

    Examples (with ``archive_root='/home/pi/ArchivedClips'``,
    ``teslacam_root='/mnt/gadget/part1-ro/TeslaCam'``)::

        .../TeslaCam/RecentClips/2024-01-01_10-00-00-front.mp4
            -> /home/pi/ArchivedClips/RecentClips/2024-01-01_10-00-00-front.mp4

        .../TeslaCam/SentryClips/2024-01-01_10-00-00/2024-01-01_10-00-00-front.mp4
            -> /home/pi/ArchivedClips/SentryClips/2024-01-01_10-00-00/...

    If the source isn't under ``teslacam_root`` (e.g. a manually-dropped
    test fixture), we fall back to placing it under
    ``archive_root/<basename>`` so the worker still has somewhere safe
    to write. This matches the legacy ``video_archive_service``
    behavior.
    """
    if not source_path:
        raise ValueError("source_path required")
    archive_root = os.path.abspath(archive_root)
    src_abs = os.path.abspath(source_path)
    if teslacam_root:
        tc_abs = os.path.abspath(teslacam_root).rstrip(os.sep) + os.sep
        if src_abs.startswith(tc_abs):
            rel = src_abs[len(tc_abs):]
            return os.path.join(archive_root, rel)
    # Fallback: put it at the top of ArchivedClips with its basename.
    return os.path.join(archive_root, os.path.basename(src_abs))


def _safe_stat(path: str):
    try:
        return os.stat(path)
    except OSError:
        return None


def _sweep_partial_orphans(archive_root: str) -> int:
    """Remove ``*.partial`` files orphaned by a prior crash.

    ``_atomic_copy`` writes to ``dest_path + '.partial'`` and only
    renames once the size-verified write succeeds. A power loss or
    hardware reset (e.g., the May 11 SDIO-watchdog reboots) leaves the
    partial behind forever — it's missed by retention (different
    extension), counted toward disk usage, and confuses the indexer if
    it ever sees the path.

    Walks ``archive_root`` once at worker startup, skipping the
    ``.dead_letter`` diagnostic dir. Returns the number of orphans
    removed (0 on a clean tree). Best-effort: per-file failures log a
    warning and continue.

    Safety: only one archive worker exists at a time (enforced by
    ``start_worker``), and the worker doesn't begin claiming rows
    until this sweep completes — so we cannot delete a ``.partial``
    that another writer is currently producing. Stat failures (file
    vanished, permissions) are skipped without raising.
    """
    if not archive_root or not os.path.isdir(archive_root):
        return 0
    removed = 0
    for dirpath, dirnames, filenames in os.walk(
        archive_root, followlinks=False,
    ):
        # Don't descend into .dead_letter — sidecar .txt files only,
        # but keep the policy symmetric with the watchdog's prune.
        dirnames[:] = [d for d in dirnames if d != '.dead_letter']
        for fn in filenames:
            if not fn.endswith('.partial'):
                continue
            full = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            try:
                os.remove(full)
                removed += 1
                logger.info(
                    "archive_worker: removed orphan partial %s (%d bytes)",
                    full, size,
                )
            except OSError as e:
                logger.warning(
                    "archive_worker: failed to remove orphan partial "
                    "%s: %s", full, e,
                )
    return removed


class _CopyTimeBudgetExceeded(OSError):
    """Raised by ``_atomic_copy`` when ``time_budget_seconds`` is hit.

    Distinct from ordinary ``OSError`` so the caller in
    ``process_one_claim`` can recognize a "system overloaded; back off
    and retry" signal versus a real I/O failure that should burn an
    attempt and eventually transition to ``dead_letter``. See issue
    #104 mitigation B for the full rationale: a pathological copy
    that consistently overruns its budget is a sign of SDIO
    contention, not a defective file — the row goes back to
    ``pending`` so the next iteration's between-files load-pause
    guard fires.
    """


# ---------------------------------------------------------------------------
# Phase 2.4 — moov-atom verification after copy
# ---------------------------------------------------------------------------

# Maximum bytes we'll consume from the box-header walk before giving up.
# A normal Tesla MP4 has 4-5 top-level boxes (ftyp, free, mdat, moov)
# so the walk reads ~24-32 bytes. Pathological / non-MP4 files might
# produce a runaway walk; this cap stops it after ~512 box-header reads
# (4 KB of seeks). Keeps the verifier strictly bounded regardless of
# input file shape.
_MOOV_VERIFY_MAX_HEADER_READS = 512


def _verify_destination_complete(dest_path: str) -> bool:
    """Return True iff ``dest_path`` is an MP4 with ``ftyp``, ``moov``,
    AND ``mdat`` boxes all present.

    Phase 2.4 — A "successful" copy of an unplayable MP4 is worse than
    a failed copy: the bad file looks complete (size matches), gets
    indexed (with errors), shows up in the UI, and refuses to play.
    Tesla writes the ``moov`` atom at the END of the file, so a copy
    that started before Tesla finished writing will have everything up
    to and including ``mdat`` but be missing ``moov``.

    Issue #110 — Tesla's RecentClips writer also produces clips with
    ``moov`` near the START of the file (before ``mdat``). A copy
    snapshotted between the moov and mdat writes has moov but no mdat,
    and the pre-#110 verifier (which returned True on the first moov
    box) accepted these. The indexer's SEI parser then bailed with
    "No mdat box found" and the row eventually dead-lettered. Both
    boxes are now required.

    Implementation notes (Pi Zero 2 W constraints):

    * **Streaming, not full-file load.** We read 8-byte box headers and
      ``seek`` past each box's payload. Total IO for a typical Tesla
      clip is ~24-32 bytes regardless of file size — no risk of mmap
      pressure on multi-GB recordings.
    * Handles 32-bit, 64-bit (``size==1``), and to-EOF (``size==0``)
      box sizes per ISO BMFF. The pre-existing ``_is_complete_mp4``
      in ``video_archive_service`` did NOT handle the 64-bit / 0
      cases — this verifier intentionally does, so a future Tesla
      firmware that emits 64-bit box sizes won't trigger spurious
      moov-missing failures.
    * A bounded number of header reads (``_MOOV_VERIFY_MAX_HEADER_READS``)
      prevents a malformed / non-MP4 input from spinning the walk
      forever.
    * Any IO error is treated as ""not verified"" (returns False) so
      the caller falls back to the retry path — matches the
      conservative ""verify-or-fail"" contract the issue specifies.
    """
    try:
        file_size = os.path.getsize(dest_path)
        if file_size < 16:
            return False  # Too small to contain ftyp + any other box.

        with open(dest_path, 'rb') as f:
            # ftyp must be the very first box per the MP4 spec.
            head = f.read(12)
            if len(head) < 12 or head[4:8] != b'ftyp':
                return False

            # Walk top-level boxes from offset 0 looking for moov + mdat.
            f.seek(0)
            pos = 0
            reads = 0
            seen_moov = False
            seen_mdat = False
            while pos + 8 <= file_size:
                if reads >= _MOOV_VERIFY_MAX_HEADER_READS:
                    # Bounded walk — pathological input.
                    return False
                reads += 1

                f.seek(pos)
                header = f.read(8)
                if len(header) < 8:
                    return False

                size = int.from_bytes(header[:4], 'big')
                box_type = header[4:8]

                if size == 1:
                    # Extended 64-bit size follows the type field.
                    if pos + 16 > file_size:
                        return False
                    ext = f.read(8)
                    if len(ext) < 8:
                        return False
                    size = int.from_bytes(ext, 'big')
                    if size < 16:
                        return False  # Malformed extended box.
                elif size == 0:
                    # Box extends to end of file. If it IS one of the
                    # required boxes, mark it seen — but nothing can
                    # follow, so we must already have the OTHER required
                    # box for the file to be complete.
                    if box_type == b'moov':
                        seen_moov = True
                    elif box_type == b'mdat':
                        seen_mdat = True
                    return seen_moov and seen_mdat
                elif size < 8:
                    return False  # Malformed normal box.

                if box_type == b'moov':
                    # Sanity-check the box doesn't claim to extend past EOF.
                    if pos + size > file_size:
                        return False
                    seen_moov = True
                elif box_type == b'mdat':
                    if pos + size > file_size:
                        return False
                    seen_mdat = True
                else:
                    # Defensive — a non-required box claiming to extend
                    # past EOF is truncated; nothing useful follows.
                    if pos + size > file_size:
                        return False

                if seen_moov and seen_mdat:
                    return True

                pos += size

            # Walked to EOF without seeing both required boxes.
            return seen_moov and seen_mdat
    except (OSError, IOError):
        return False


def _atomic_copy(source_path: str, dest_path: str,
                 chunk_size: int, *,
                 load_pause_threshold: float = 0.0,
                 chunk_pause_seconds: float = 0.25,
                 time_budget_seconds: float = 0.0,
                 now_fn: Callable[[], float] = time.monotonic,
                 sleep_fn: Callable[[float], None] = time.sleep) -> int:
    """Copy ``source_path`` → ``dest_path`` atomically. Returns size.

    Pattern: ``dest_path + '.partial'`` → write in chunks → fsync →
    rename. The temp file is unlinked on any failure so a crash mid-
    copy doesn't leave an orphan in ArchivedClips.

    Verifies the rendered size matches the source's stat() size; any
    mismatch raises ``OSError`` so the caller bumps attempts.

    Mid-copy SDIO-contention safeguards (issue #104):

    * If ``load_pause_threshold > 0``, between chunks sample
      ``os.getloadavg()[0]`` and sleep ``chunk_pause_seconds`` when
      it exceeds the threshold. The Pi Zero 2 W shares one SDIO
      controller between SD card and WiFi; sustained heavy archive
      I/O can starve the userspace ``watchdog`` daemon long enough
      to trigger the BCM2835 hardware watchdog (90s timeout). This
      yields the daemon enough CPU + ``/dev/watchdog`` write
      bandwidth to keep pinging the kernel. The ``getloadavg``
      syscall is O(1) (~1 µs) and the branch is taken only when
      the box is actually overloaded — zero overhead in the normal
      case.
    * If ``time_budget_seconds > 0``, raise
      :class:`_CopyTimeBudgetExceeded` (an ``OSError`` subclass) if
      the copy takes longer than that many seconds. The caller
      catches this BEFORE the generic ``OSError`` handler and
      releases the claim back to ``pending`` *without* bumping
      ``attempts`` — the next iteration's between-files load-pause
      guard gets a chance to fire. A clip that consistently times
      out is a sign of pathological I/O, not a file defect; we let
      the system breathe rather than push it to ``dead_letter``.

    ``now_fn`` and ``sleep_fn`` are injectable for tests.
    """
    parent = os.path.dirname(dest_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    partial = dest_path + '.partial'
    expected = os.path.getsize(source_path)
    written = 0
    started = now_fn()
    deadline = (
        started + time_budget_seconds
        if time_budget_seconds > 0 else 0.0
    )
    try:
        # ``shutil.copyfile`` does buffered chunked copies under the
        # hood; we wrap manually so we can fsync + size-verify and
        # interpose the per-chunk SDIO-contention safeguards.
        with open(source_path, 'rb') as src, open(partial, 'wb') as dst:
            while True:
                chunk = src.read(chunk_size)
                if not chunk:
                    break
                dst.write(chunk)
                written += len(chunk)
                # Per-file time-budget check (issue #104 mitigation B).
                # Done BEFORE the load-aware backoff so a budget-exceeded
                # condition fires deterministically even when load is
                # also high (otherwise we'd add an extra sleep before
                # the abort).
                if deadline > 0.0 and now_fn() >= deadline:
                    raise _CopyTimeBudgetExceeded(
                        f"copy exceeded {time_budget_seconds:.1f}s budget "
                        f"after {written}/{expected} bytes"
                    )
                # Mid-copy load-aware backoff (issue #104 mitigation A).
                if load_pause_threshold > 0:
                    try:
                        load1 = os.getloadavg()[0]
                    except (AttributeError, OSError):
                        load1 = 0.0
                    if load1 > load_pause_threshold:
                        sleep_fn(chunk_pause_seconds)
            dst.flush()
            try:
                os.fsync(dst.fileno())
            except OSError:
                # Best-effort; some filesystems (tmpfs, network mounts)
                # don't support fsync. Don't fail the copy on that.
                pass
        if written != expected:
            raise OSError(
                f"size mismatch: wrote {written}, expected {expected}"
            )
        # Phase 2.4 — verify the copied destination is a complete MP4
        # (has both ftyp and moov atoms). A "successful" size-matching
        # copy of an unplayable file is worse than a failed copy: the
        # bad file looks complete, gets indexed (with errors), shows
        # up in the UI, and refuses to play. Only run on .mp4 files
        # so .ts segments and other non-MP4 archives are unaffected.
        if dest_path.lower().endswith('.mp4'):
            if not _verify_destination_complete(partial):
                raise OSError(
                    f"destination MP4 missing moov or mdat box — "
                    f"source may still be writing: {source_path}"
                )
        # Copy mtime so downstream consumers (indexer, ZIP exporter)
        # see the original timestamp.
        try:
            shutil.copystat(source_path, partial)
        except OSError:
            pass
        os.replace(partial, dest_path)
        return written
    except Exception:
        # Clean up the partial on any failure path.
        try:
            os.remove(partial)
        except OSError:
            pass
        raise


def _write_dead_letter_sidecar(archive_root: str,
                               row: Dict[str, Any]) -> None:
    """Write ``~/ArchivedClips/.dead_letter/<id>.txt`` for forensics.

    Best-effort — a failure to write the sidecar is logged but doesn't
    re-trigger a retry on the underlying queue row.
    """
    try:
        sidecar_dir = os.path.join(archive_root, '.dead_letter')
        os.makedirs(sidecar_dir, exist_ok=True)
        sidecar_path = os.path.join(sidecar_dir, f"{row['id']}.txt")
        with open(sidecar_path, 'w', encoding='utf-8') as f:
            f.write(f"id: {row.get('id')}\n")
            f.write(f"source_path: {row.get('source_path')}\n")
            f.write(f"dest_path: {row.get('dest_path')}\n")
            f.write(f"attempts: {row.get('attempts')}\n")
            f.write(f"enqueued_at: {row.get('enqueued_at')}\n")
            f.write(f"last_error: {row.get('last_error')}\n")
    except OSError as e:
        logger.warning(
            "Failed to write dead_letter sidecar for id=%s: %s",
            row.get('id'), e,
        )


def _enqueue_indexed(dest_path: str, db_path: str) -> None:
    """Enqueue the archived dest into indexing_queue.

    Looked up at call time (not import time) so tests can monkeypatch
    ``indexing_queue_service.enqueue_for_indexing`` cleanly. Failure
    here doesn't roll back the archive — the indexer's boot catch-up
    scan will pick the file up later anyway.

    Phase 3c.1 (#100): the indexing queue API moved to
    ``services.indexing_queue_service``. Tests that previously
    monkey-patched ``mapping_service.enqueue_for_indexing`` should
    target the new module instead.
    """
    try:
        from services import indexing_queue_service as queue_svc
        if hasattr(queue_svc, 'enqueue_for_indexing'):
            # queue_svc.enqueue_for_indexing is positional
            # (db_path, file_path) — keep the call site aligned.
            queue_svc.enqueue_for_indexing(
                db_path, dest_path, source='archive',
            )
        elif hasattr(queue_svc, 'enqueue_many_for_indexing'):
            queue_svc.enqueue_many_for_indexing(
                db_path, [(dest_path, None)], source='archive',
            )
        else:
            logger.warning(
                "indexing_queue_service has no enqueue API; skipping",
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "enqueue_for_indexing failed for %s: %s", dest_path, e,
        )


def _apply_low_priority() -> None:
    """Drop the calling thread to lowest CPU + I/O priority (Linux only).

    Same per-thread approach as the indexer: ``SCHED_IDLE`` via
    ``sched_setscheduler(0, ...)`` + ``ionice -c 3 -p <native_tid>``.
    No-op on non-Linux platforms. Failures are silently ignored —
    priority adjustment is a nice-to-have, not a correctness rule.
    """
    if not sys.platform.startswith('linux'):
        return
    try:
        SCHED_IDLE = 5
        if hasattr(os, 'sched_setscheduler') and hasattr(os, 'sched_param'):
            os.sched_setscheduler(  # type: ignore[attr-defined]
                0, SCHED_IDLE, os.sched_param(0),  # type: ignore[attr-defined]
            )
    except (OSError, PermissionError, AttributeError):
        pass
    try:
        import subprocess
        tid = threading.get_native_id()
        subprocess.run(
            ["ionice", "-c", "3", "-p", str(tid)],
            timeout=5, capture_output=True, check=False,
        )
    except (FileNotFoundError, OSError, AttributeError):
        pass
    except Exception:  # noqa: BLE001
        # subprocess.TimeoutExpired and friends — non-fatal.
        pass


def _resolve_disk_thresholds_mb() -> tuple:
    """Return ``(warning_mb, critical_mb)`` for the disk-space guard.

    Looked up at call time so tests can monkeypatch the config import.
    Falls back to (500, 100) when the config module isn't importable
    (unit-test environments).
    """
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


def _check_disk_space_guard(archive_root: str) -> str:
    """Classify free disk space at ``archive_root``.

    Returns one of ``'ok'``, ``'warning'``, ``'critical'``. ``'critical'``
    means a copy MUST NOT proceed. ``'warning'`` is informational; the
    copy proceeds but logs a WARNING line. Stat failures return ``'ok'``
    so a transient FS hiccup doesn't lock the archive subsystem out.
    """
    try:
        usage = shutil.disk_usage(archive_root)
    except OSError:
        return 'ok'
    free_mb = int(usage.free // (1024 * 1024))
    warn_mb, crit_mb = _resolve_disk_thresholds_mb()
    if free_mb < crit_mb:
        logger.critical(
            "Archive disk-space CRITICAL: %d MB free at %s "
            "(< %d MB threshold) — refusing new copies for %.0fs",
            free_mb, archive_root, crit_mb,
            _resolve_disk_space_pause_seconds(),
        )
        return 'critical'
    if free_mb < warn_mb:
        logger.warning(
            "Archive disk-space LOW: %d MB free at %s "
            "(< %d MB threshold) — proceeding with copy",
            free_mb, archive_root, warn_mb,
        )
        return 'warning'
    return 'ok'


def get_disk_pause_state() -> Dict[str, Any]:
    """Return the current disk-space pause state for status endpoints.

    Phase 4.5 (#101) — surfaces the *reason* the disk-space guard
    armed the pause so the UI can render "Paused: SD card X% full"
    instead of an opaque "Paused" string. ``last_pause_at`` and
    ``last_free_mb`` are set the first time the critical-threshold
    guard fires (via :func:`process_one_claim`); they remain ``None``
    on a freshly started worker that has never seen a critical hit.

    ``critical_threshold_mb`` and ``warning_threshold_mb`` are the
    currently-configured trip points (resolved at call time so config
    edits show up without a service restart).
    """
    warn_mb, crit_mb = _resolve_disk_thresholds_mb()
    with _state_lock:
        return {
            'paused_until_epoch': float(_disk_space_pause_until),
            'is_paused_now': _disk_space_pause_until > time.time(),
            'last_pause_at': _state.get('last_disk_pause_at'),
            'last_free_mb': _state.get('last_disk_pause_free_mb'),
            'last_total_mb': _state.get('last_disk_pause_total_mb'),
            'critical_threshold_mb': int(crit_mb),
            'warning_threshold_mb': int(warn_mb),
        }


def get_load_pause_state() -> Dict[str, Any]:
    """Return the current load-pause state for status endpoints.

    ``last_loadavg`` is the most recent reading that triggered the
    pause (None until the guard fires for the first time). Mirrors
    :func:`get_disk_pause_state` so the UI can show *why* the worker
    isn't draining.

    Phase 4.5 (#101) — also surfaces the configured
    ``threshold`` (resolved at call time) so the message can render
    "Paused: load 4.2 > 3.5".
    """
    # Threshold is the 5th element of the _read_config_or_defaults
    # tuple; resolve outside the state lock to keep the critical
    # section small.
    try:
        threshold = float(_read_config_or_defaults()[4])
    except Exception:  # noqa: BLE001
        threshold = float(_LOAD_PAUSE_THRESHOLD)
    with _state_lock:
        return {
            'paused_until_epoch': float(_load_pause_until),
            'is_paused_now': _load_pause_until > time.time(),
            'last_pause_at': _state.get('last_load_pause_at'),
            'last_loadavg': _state.get('last_load_pause_loadavg'),
            'threshold': threshold,
        }


def _compute_drain_rate(now: Optional[float] = None) -> Dict[str, Any]:
    """Compute the recent drain rate + ETA from the rolling window.

    Phase 4.4 (#101) — surface "how long until the backlog clears" so
    the user knows whether to wait around or come back later. Returns a
    dict with these keys (always present so the API contract is stable):

      * ``rate_per_sec``    — float files/sec, or ``None`` when there
                              aren't enough fresh samples to estimate.
      * ``samples``         — int, number of completion timestamps in
                              the rolling window currently used for the
                              estimate (may be < window size after a
                              restart or trim by freshness gate).
      * ``window_age_sec``  — float seconds spanned by the samples
                              (latest minus earliest), or ``None``.
      * ``stale``           — bool, True when the most recent sample is
                              older than :data:`_DRAIN_RATE_FRESHNESS_SECONDS`.
                              Stale rates are NOT used for ETA because
                              an idle window is not a useful predictor
                              of the next burst's drain pace.

    The caller computes ETA from this + the queue depth so the gating
    logic (queue threshold, freshness, sample count) is colocated with
    the consumer's UI rules, not buried in the worker.

    Reads under :data:`_state_lock` so the snapshot is consistent with
    the worker's own append. Touching ``time.time()`` outside the lock
    keeps the lock window tiny.
    """
    now = now if now is not None else time.time()
    with _state_lock:
        samples = list(_recent_copy_completions)
    n = len(samples)
    if n < _DRAIN_RATE_MIN_SAMPLES:
        return {
            'rate_per_sec': None,
            'samples': n,
            'window_age_sec': None,
            'stale': False,
        }
    most_recent_age = now - samples[-1]
    if most_recent_age > _DRAIN_RATE_FRESHNESS_SECONDS:
        # Stale window — worker has been idle/paused. The historical
        # rate is no longer meaningful for the current backlog.
        return {
            'rate_per_sec': None,
            'samples': n,
            'window_age_sec': samples[-1] - samples[0],
            'stale': True,
        }
    span = samples[-1] - samples[0]
    if span <= 0:
        # All N samples in the same wall-clock instant (impossible in
        # practice, but defensive against tests with patched clocks).
        return {
            'rate_per_sec': None,
            'samples': n,
            'window_age_sec': 0.0,
            'stale': False,
        }
    # n - 1 inter-completion gaps in `span` seconds → files/sec.
    rate = (n - 1) / span
    return {
        'rate_per_sec': rate,
        'samples': n,
        'window_age_sec': span,
        'stale': False,
    }


def compute_eta_seconds(queue_depth: int,
                        drain_rate_per_sec: Optional[float]) -> Optional[int]:
    """Return ETA seconds for ``queue_depth`` files at ``drain_rate_per_sec``.

    Returns ``None`` when:
      * queue is empty (no ETA needed),
      * no rate is available (e.g. fresh worker, < 3 samples, stale window),
      * rate is non-positive (defensive),
      * computed ETA is < 1 second (avoids the misleading
        ``eta_seconds: 0`` + ``eta_human: None`` asymmetry; the
        ``_format_eta_human`` helper would also render this as
        "<1 min" which adds no signal),
      * computed ETA exceeds :data:`_DRAIN_RATE_ETA_CAP_SECONDS`.

    The cap exists to suppress absurd values from short-window
    estimates of huge backlogs (e.g. 5 fresh copies per second × a
    10 000-file queue → reasonable; but 1 copy / hour after a long
    pause × 10 000 = 10 000 hours, which is misleading because the
    pace is virtually guaranteed to recover). Surfacing "more than
    24 h" as ``None`` lets the UI fall back to "estimate not available
    yet" rather than render a silly headline number.
    """
    if not queue_depth:
        return None
    if drain_rate_per_sec is None or drain_rate_per_sec <= 0:
        return None
    eta = queue_depth / drain_rate_per_sec
    if eta < 1:
        return None
    if eta > _DRAIN_RATE_ETA_CAP_SECONDS:
        return None
    return int(round(eta))


def _set_state(**fields: Any) -> None:
    with _state_lock:
        _state.update(fields)


def _record_active(file_path: str) -> None:
    with _state_lock:
        _state['active_file'] = file_path
    _idle_event.clear()


def _record_idle(*, last_outcome: Optional[str] = None,
                 last_error: Optional[str] = None) -> None:
    with _state_lock:
        _state['active_file'] = None
        if last_outcome is not None:
            _state['last_outcome'] = last_outcome
        if last_error is not None:
            _state['last_error'] = last_error
    _idle_event.set()


def _stable_write_age_seconds() -> float:
    """Return ``ARCHIVE_QUEUE_STABLE_WRITE_AGE_SECONDS`` from config,
    falling back to the module-level ``_STABLE_WRITE_AGE_SECONDS``.

    Looked up at call time so tests can monkeypatch the config module
    after import. Phase 5.9 — issue #102.
    """
    try:
        from config import ARCHIVE_QUEUE_STABLE_WRITE_AGE_SECONDS
        return float(ARCHIVE_QUEUE_STABLE_WRITE_AGE_SECONDS)
    except Exception:  # noqa: BLE001
        return _STABLE_WRITE_AGE_SECONDS


def _read_config_or_defaults():
    """Return tunables from config.

    Returns: ``(chunk_bytes, max_attempts, idle, inter_file,
    load_threshold, load_pause, chunk_pause, time_budget)``.

    Looked up at call time so tests can monkeypatch the config module
    after import. Falls back to module-level defaults if config isn't
    importable (unit-test environments without the full app). The
    last two values are the issue #104 mid-copy safeguards
    (``chunk_pause_seconds`` and ``per_file_time_budget_seconds``).
    """
    try:
        from config import (
            ARCHIVE_QUEUE_COPY_CHUNK_BYTES,
            ARCHIVE_QUEUE_RETRY_MAX_ATTEMPTS,
            ARCHIVE_QUEUE_WORKER_CHECK_INTERVAL_SECONDS,
            ARCHIVE_QUEUE_INTER_FILE_SLEEP_SECONDS,
            ARCHIVE_QUEUE_LOAD_PAUSE_THRESHOLD,
            ARCHIVE_QUEUE_LOAD_PAUSE_SECONDS,
            ARCHIVE_QUEUE_CHUNK_PAUSE_SECONDS,
            ARCHIVE_QUEUE_PER_FILE_TIME_BUDGET_SECONDS,
        )
        return (
            int(ARCHIVE_QUEUE_COPY_CHUNK_BYTES),
            int(ARCHIVE_QUEUE_RETRY_MAX_ATTEMPTS),
            float(ARCHIVE_QUEUE_WORKER_CHECK_INTERVAL_SECONDS),
            float(ARCHIVE_QUEUE_INTER_FILE_SLEEP_SECONDS),
            float(ARCHIVE_QUEUE_LOAD_PAUSE_THRESHOLD),
            float(ARCHIVE_QUEUE_LOAD_PAUSE_SECONDS),
            float(ARCHIVE_QUEUE_CHUNK_PAUSE_SECONDS),
            float(ARCHIVE_QUEUE_PER_FILE_TIME_BUDGET_SECONDS),
        )
    except Exception:  # noqa: BLE001
        return (
            _DEFAULT_COPY_CHUNK_BYTES, 3, _IDLE_SLEEP_SECONDS,
            _INTER_FILE_SLEEP_SECONDS,
            _LOAD_PAUSE_THRESHOLD,
            _LOAD_PAUSE_SECONDS,
            _CHUNK_PAUSE_SECONDS,
            _PER_FILE_TIME_BUDGET_SECONDS,
        )


# ---------------------------------------------------------------------------
# Per-row processing (testable without a thread)
# ---------------------------------------------------------------------------

def process_one_claim(row: Dict[str, Any], db_path: str,
                      archive_root: str,
                      teslacam_root: Optional[str], *,
                      chunk_size: int,
                      max_attempts: int,
                      load_pause_threshold: float = 0.0,
                      chunk_pause_seconds: float = 0.25,
                      time_budget_seconds: float = 0.0,
                      now_fn: Callable[[], float] = time.time) -> str:
    """Process a single claimed row. Returns the new status string.

    Possible return values:
      * ``'copied'``       — file copied + indexer enqueued
      * ``'source_gone'``  — source vanished (no retry, terminal)
      * ``'pending'``      — released back to pending (stable-write
                             gate, disk pause, time-budget abort,
                             transient error with attempts left)
      * ``'dead_letter'``  — attempts exhausted

    The ``load_pause_threshold``, ``chunk_pause_seconds``, and
    ``time_budget_seconds`` keyword args are forwarded to
    :func:`_atomic_copy` for the issue #104 mid-copy SDIO-contention
    safeguards. Defaults are conservative (off / 0.25 s / off) so
    callers that don't opt in get pre-#104 behavior.

    Pure dispatch logic kept separate from the loop so tests can drive
    it directly without a thread or task_coordinator.
    """
    row_id = int(row['id'])
    source_path = row['source_path']

    # Stable-write gate. If the file is too fresh AND its size or mtime
    # have shifted since enqueue, requeue with refreshed metadata.
    st = _safe_stat(source_path)
    if st is None:
        archive_queue.mark_source_gone(row_id, db_path=db_path)
        return 'source_gone'
    age = now_fn() - st.st_mtime
    expected_size = row.get('expected_size')
    expected_mtime = row.get('expected_mtime')
    # Phase 2.5 — When the queue row has NULL ``expected_size`` /
    # ``expected_mtime`` (e.g., enqueue happened while Tesla was still
    # writing and the producer's ``stat()`` raced against the partial
    # write, OR a legacy schema row predates the metadata columns), we
    # have NO baseline to compare against. The pre-2.5 code computed
    # ``metadata_drifted = False`` in that case and FELL THROUGH to the
    # copy step, potentially copying a half-written file. With moov-
    # verify (2.4) such files now fail post-copy, but it's wasteful to
    # do the IO and immediately retry. Treat NULL metadata as
    # "needs settling check" so the freshness gate fires: defer if the
    # file is too young, proceed if it has been settled long enough.
    metadata_unknown = (expected_size is None or expected_mtime is None)
    metadata_drifted = (
        (expected_size is not None and expected_size != st.st_size)
        or (expected_mtime is not None and expected_mtime != st.st_mtime)
    )
    needs_settling_check = metadata_drifted or metadata_unknown
    if age < _stable_write_age_seconds() and needs_settling_check:
        # Update the snapshot so the next pick uses fresh values.
        archive_queue.release_claim(
            row_id,
            expected_size=st.st_size,
            expected_mtime=st.st_mtime,
            db_path=db_path,
        )
        return 'pending'

    # Disk-space pre-archive guard. We do this AFTER the stable-write
    # gate (which requires only stat() on the source) but BEFORE any
    # write attempt to ``archive_root``. A 'critical' verdict releases
    # the claim back to pending without burning an attempt and arms a
    # module-level pause so the worker stops claiming for ~5 minutes;
    # the watchdog re-evaluates on its next tick. 'warning' is logged
    # but the copy proceeds — we only refuse new copies on critical.
    global _disk_space_pause_until
    disk_verdict = _check_disk_space_guard(archive_root)
    if disk_verdict == 'critical':
        archive_queue.release_claim(row_id, db_path=db_path)
        _disk_space_pause_until = (
            now_fn() + _resolve_disk_space_pause_seconds()
        )
        try:
            usage = shutil.disk_usage(archive_root)
            free_mb = int(usage.free // (1024 * 1024))
            total_mb = int(usage.total // (1024 * 1024))
        except OSError:
            free_mb = -1
            total_mb = -1
        with _state_lock:
            _state['last_disk_pause_at'] = time.time()
            _state['last_disk_pause_free_mb'] = free_mb
            _state['last_disk_pause_total_mb'] = total_mb
        # Phase 1 item 1.5: kick the retention prune NOW (debounced)
        # so we don't sit at "Archive paused" for up to 24 h waiting
        # for the daily retention timer.
        _maybe_trigger_critical_cleanup(archive_root)
        return 'pending'

    # Compute destination + atomic copy.
    try:
        dest_path = compute_dest_path(source_path, archive_root, teslacam_root)
    except ValueError as e:
        archive_queue.mark_failed(
            row_id, f"compute_dest: {e!r}",
            max_attempts=max_attempts, db_path=db_path,
        )
        return 'error'

    try:
        _atomic_copy(
            source_path, dest_path, chunk_size,
            load_pause_threshold=load_pause_threshold,
            chunk_pause_seconds=chunk_pause_seconds,
            time_budget_seconds=time_budget_seconds,
        )
    except FileNotFoundError:
        # Tesla rotated the source between stat() and open() — normal,
        # not retryable.
        archive_queue.mark_source_gone(row_id, db_path=db_path)
        return 'source_gone'
    except _CopyTimeBudgetExceeded as e:
        # Issue #104 mitigation B: per-file time budget is a "system
        # overloaded; back off and retry" signal, not an I/O failure.
        # Release back to pending WITHOUT bumping attempts so the row
        # can never reach dead_letter from load alone. The next
        # iteration's between-files load-pause guard will fire and
        # give the SDIO bus + watchdog daemon a clear runway.
        logger.warning(
            "archive_worker: copy of %s aborted to relieve SDIO "
            "contention (%s); releasing back to pending",
            source_path, e,
        )
        archive_queue.release_claim(row_id, db_path=db_path)
        return 'pending'
    except (OSError, shutil.Error) as e:
        new_status = archive_queue.mark_failed(
            row_id, f"copy: {e!r}",
            max_attempts=max_attempts, db_path=db_path,
        )
        if new_status == 'dead_letter':
            row_for_sidecar = dict(row)
            row_for_sidecar['dest_path'] = dest_path
            row_for_sidecar['last_error'] = f"copy: {e!r}"
            row_for_sidecar['attempts'] = int(
                row.get('attempts') or 0,
            ) + 1
            _write_dead_letter_sidecar(archive_root, row_for_sidecar)
        return new_status

    # Success — mark copied AND enqueue into the indexer queue.
    archive_queue.mark_copied(row_id, dest_path, db_path=db_path)
    _enqueue_indexed(dest_path, db_path)
    return 'copied'


# ---------------------------------------------------------------------------
# Worker thread loop
# ---------------------------------------------------------------------------

def _run_worker_loop(db_path: str, archive_root: str,
                     teslacam_root: Optional[str],
                     worker_id: str) -> None:
    """The thread target. One file at a time, until stop is signaled."""
    # ``_load_pause_until`` is read AND written below (leading edge sets it,
    # trailing edge clears it). Declare global at function scope per
    # Python convention rather than burying it inside a conditional.
    global _load_pause_until

    _apply_low_priority()
    try:
        # Phase 5.9 (#102): pull the stale-claim age from config so
        # users can tune via Settings → Advanced.
        try:
            from config import ARCHIVE_QUEUE_STALE_CLAIM_MAX_AGE_SECONDS
            _stale_age = float(ARCHIVE_QUEUE_STALE_CLAIM_MAX_AGE_SECONDS)
        except Exception:  # noqa: BLE001
            _stale_age = 600.0
        released = archive_queue.recover_stale_claims(
            db_path=db_path,
            max_age_seconds=_stale_age,
        )
        if released:
            logger.info(
                "Archive worker %s released %d stale claims at startup",
                worker_id, released,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("recover_stale_claims failed at startup: %s", e)

    # Sweep .partial orphans left behind by a prior crash. Runs once
    # at worker startup, before the loop begins claiming rows; safe
    # because only one worker exists at a time. See
    # ``_sweep_partial_orphans`` docstring for the safety argument.
    try:
        orphans = _sweep_partial_orphans(archive_root)
        if orphans:
            logger.info(
                "Archive worker %s removed %d orphan .partial file(s) "
                "at startup",
                worker_id, orphans,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Archive worker %s: orphan .partial sweep failed: %s",
            worker_id, e,
        )

    chunk_size, max_attempts, idle_sleep, inter_file_sleep, \
        load_pause_threshold, load_pause_seconds, \
        chunk_pause_seconds, time_budget_seconds = _read_config_or_defaults()

    while not _stop_event.is_set():
        # Honor pause requests at the iteration boundary.
        if _pause_event.is_set():
            _idle_event.set()
            if _stop_event.wait(timeout=inter_file_sleep):
                break
            continue

        # SDIO-contention guard. The Pi Zero 2 W shares one SDIO
        # controller between SD card and WiFi; sustained heavy archive
        # I/O can starve the watchdog daemon and trigger a hardware
        # reset. When the system is already under load (typically the
        # combination of archive + indexer + Tesla concurrent writes),
        # back off so other tasks can drain. Threshold and pause length
        # are configurable; ``getloadavg`` is a cheap O(1) syscall.
        #
        # Two UX rules apply here:
        #
        #   1. Log INFO once on entering the pause and once on resume,
        #      NOT on every iteration. Producers calling ``wake()``
        #      under sustained high load would otherwise spam
        #      ``journalctl`` every few seconds (see PR #93 review).
        #   2. Use ``_stop_event.wait`` (NOT ``_wait_with_wake``) so
        #      a producer's wake() can't shorten the back-off — the
        #      whole point of the pause is to give the SDIO bus and
        #      watchdog daemon a clear runway. Producers will get
        #      their files drained on the next iteration anyway.
        if load_pause_threshold > 0:
            try:
                load1 = os.getloadavg()[0]
            except (AttributeError, OSError):
                load1 = 0.0
            if load1 > load_pause_threshold:
                # Only log INFO on the leading edge of the pause
                # window so back-to-back high-load iterations don't
                # spam the journal. ``_load_pause_until`` is the
                # epoch the current pause window expires; if it's
                # already in the future we're still inside the same
                # window and stay quiet.
                already_paused = _load_pause_until > time.time()
                _load_pause_until = time.time() + load_pause_seconds
                if not already_paused:
                    # Pin ``last_pause_at`` to the moment the pause
                    # actually started — within a sustained pause
                    # window the field must NOT tick forward on
                    # every iteration (parity with disk-pause, which
                    # arms ``last_disk_pause_at`` only on first hit).
                    with _state_lock:
                        _state['last_load_pause_at'] = time.time()
                        _state['last_load_pause_loadavg'] = float(load1)
                    logger.info(
                        "archive_worker: 1-min loadavg %.2f > %.2f — "
                        "pausing %.0fs to relieve SDIO/CPU contention",
                        load1, load_pause_threshold, load_pause_seconds,
                    )
                _idle_event.set()
                # Stop-only wait. Producers' wake() must NOT cut this
                # short — we are deliberately giving the SDIO bus
                # and the watchdog daemon a clear runway.
                if _stop_event.wait(timeout=load_pause_seconds):
                    break
                continue
            elif _load_pause_until > 0 and _load_pause_until <= time.time():
                # Trailing edge: log once when we leave the pause
                # window so the user can see "back to normal".
                logger.info(
                    "archive_worker: 1-min loadavg %.2f back below %.2f — "
                    "resuming archive drain",
                    load1, load_pause_threshold,
                )
                _load_pause_until = 0.0

        # Honor the disk-space self-pause. ``process_one_claim`` arms
        # ``_disk_space_pause_until`` when free space crosses the
        # critical threshold; the loop idles here until the deadline
        # passes (the watchdog tick will then re-evaluate).
        if _disk_space_pause_until > time.time():
            _idle_event.set()
            remaining = _disk_space_pause_until - time.time()
            _wait_with_wake(min(remaining, idle_sleep))
            continue

        # Acquire the task slot. The archive worker is a periodic
        # priority task, so it BLOCK-waits for a slot. If the wait
        # times out (indexer hogging the lock past 60 s — should be
        # impossible given the indexer's yield_to_waiters=True mode,
        # but defensively handled) we back off and try again next
        # iteration. We must NOT bump the row's attempts counter for
        # our own scheduling failure.
        if not task_coordinator.acquire_task(
                _COORDINATOR_TASK, wait_seconds=_COORDINATOR_WAIT_SECONDS):
            if _stop_event.wait(timeout=_BACKOFF_SLEEP_SECONDS):
                break
            continue

        row: Optional[Dict[str, Any]] = None
        new_status: Optional[str] = None
        claim_failed = False
        try:
            try:
                row = archive_queue.claim_next_for_worker(
                    worker_id, db_path=db_path,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("claim_next_for_worker raised: %s", e)
                _set_state(last_error=f'claim: {e!r}')
                claim_failed = True
                row = None

            if row is None and not claim_failed:
                _set_state(last_drained_at=time.time())

            if row is not None:
                # If pause arrived between claim and process, release
                # the claim cleanly without burning an attempt.
                if _pause_event.is_set():
                    archive_queue.release_claim(
                        int(row['id']), db_path=db_path,
                    )
                    new_status = 'pending'
                else:
                    _record_active(row['source_path'])
                    try:
                        new_status = process_one_claim(
                            row, db_path, archive_root, teslacam_root,
                            chunk_size=chunk_size,
                            max_attempts=max_attempts,
                            load_pause_threshold=load_pause_threshold,
                            chunk_pause_seconds=chunk_pause_seconds,
                            time_budget_seconds=time_budget_seconds,
                        )
                        if new_status == 'copied':
                            with _state_lock:
                                _state['files_done_session'] += 1
                                # Phase 4.4: record the completion for
                                # drain-rate ETA. Bounded deque means the
                                # oldest sample falls off automatically.
                                _recent_copy_completions.append(time.time())
                    except Exception as e:  # noqa: BLE001
                        logger.exception(
                            "Archive worker dispatch failed for %s; "
                            "releasing claim", row.get('source_path'),
                        )
                        try:
                            archive_queue.release_claim(
                                int(row['id']), db_path=db_path,
                            )
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "release_claim also failed; "
                                "stale-recovery will pick this up",
                            )
                        new_status = 'error'
                        _set_state(last_error=f'dispatch: {e!r}')
        finally:
            task_coordinator.release_task(_COORDINATOR_TASK)
            if new_status is not None:
                _record_idle(last_outcome=new_status)
            else:
                _record_idle()

        # All sleeps happen AFTER the lock is released. Wake events
        # let producers shorten the idle wait without spinning.
        if claim_failed:
            _wait_with_wake(_BACKOFF_SLEEP_SECONDS)
        elif row is None:
            _wait_with_wake(idle_sleep)
        else:
            # Inter-file pause. Don't honor wake() here — we just
            # finished work; we want the kernel to flush before the
            # next read-heavy copy.
            if _stop_event.wait(timeout=inter_file_sleep):
                break


def _wait_with_wake(seconds: float) -> None:
    """Sleep up to ``seconds`` seconds; cut short on stop or wake.

    Clears the wake event after consuming it so the next iteration
    starts fresh. Called only when the lock is NOT held.
    """
    deadline = time.time() + seconds
    remaining = seconds
    while remaining > 0:
        if _stop_event.wait(timeout=min(remaining, 1.0)):
            return
        if _wake_event.is_set():
            _wake_event.clear()
            return
        remaining = deadline - time.time()
