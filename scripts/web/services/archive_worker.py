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
5. Atomic copy: stage to ``<archive_root>/.staging/<hash>-<name>.partial``
   in 1-MiB chunks, ``fsync``, ``os.replace`` to the final name,
   verify size matches the source. The single staging dir keeps the
   archive tree free of in-flight bytes (so directory traversals
   never trip on partials) and reduces orphan-sweep cost from
   ``os.walk`` to one ``os.scandir`` (issue #184 Wave 3 — Phase H).
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
import hashlib
import logging
import os
import shutil
import sys
import threading
import time
import uuid
from typing import Any, Callable, Deque, Dict, Optional, Tuple

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
# RecentClips-specific stable-write age. Tesla writes RecentClips in
# ~60-second segments; the moov atom is appended at the END of the
# segment, so a copy snapshot taken any time before Tesla closes the
# file is missing moov and fails ``_verify_destination_complete``.
# Wait at least this long after the last mtime change before attempting
# the copy. Sentry/Saved event clips are written atomically when the
# event ends (no "still being written" window) and keep the base
# threshold above. Configurable via
# ``archive_queue.recent_clips_stable_write_age_seconds``.
_RECENT_CLIPS_STABLE_WRITE_AGE_SECONDS = 90.0
# Cap on how many times the worker will defer a row via the
# ``_CopyMoovIncomplete`` (Tesla still-writing) handler before
# escalating to the regular failure path (mark_failed → bump
# ``attempts`` → eventual dead_letter at ``max_attempts``). Without
# a cap, a genuinely-corrupted-but-stable RecentClips file (rare but
# possible: bad SD block, Tesla crashed mid-segment then never
# rotated the slot) would defer forever, re-running the full
# source-read + partial-staging-write + delete cycle on every pick
# (~30–360 MB SDIO per iteration). 10 defers × ~30 s backoff =
# ~5 minutes — well past Tesla's 60 s segment-close window, so any
# row still failing after that is genuinely broken, not still
# being written. Reset on process restart (in-memory only — see
# ``_moov_defer_counts`` below).
_MOOV_DEFER_CAP = 10
# Cap on the size of the per-source_path defer counter dict to prevent
# unbounded memory growth across long-running drives. Tesla writes
# ~hundreds of RecentClips per drive; over weeks the dict could
# accumulate. When the LRU exceeds this size, the oldest entry is
# evicted. 2048 × ~150 bytes/entry ≈ 300 KB — negligible on Pi Zero 2 W.
_MOOV_DEFER_LRU_SIZE = 2048
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

        # Issue #184 Wave 2 — Phase F: also trigger an out-of-cycle
        # stale scan so any indexed_files rows whose underlying clip
        # was just rotated/pruned are reaped immediately, not in
        # ~30 days when the periodic sweep would otherwise run.
        # ``trigger_stale_scan_now`` is itself debounced (10 min) so
        # repeated disk-critical signals collapse into a single scan.
        try:
            from services.mapping_service import trigger_stale_scan_now
            from services.video_service import get_teslacam_path
            from config import MAPPING_DB_PATH
            trigger_stale_scan_now(
                MAPPING_DB_PATH, get_teslacam_path,
                source='disk_critical',
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "archive_worker: disk-critical stale-scan trigger "
                "skipped: %s", e,
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
            ARCHIVE_DIR, MAPPING_DB_PATH,
        )
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
    # Issue #167 sub-deliverable 2 — observability for the
    # skip-at-source counter. Always present in the snapshot (zero
    # when the flag is off) so the Settings UI can show a "skipped
    # N stationary clips today" badge without conditional plumbing.
    snap['skipped_stationary_count'] = counts.get('skipped_stationary', 0)
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


# Issue #184 Wave 3 — Phase H. Staging directory for atomic copies.
# All ``.partial`` files now live here, never inside the destination
# tree, so:
#   * directory traversals (indexer, retention prune, file watcher)
#     never see in-flight bytes;
#   * orphan cleanup at startup is a single ``os.scandir`` of one
#     directory, not a recursive walk of the whole archive.
# The staging dir lives on the SAME filesystem as the archive root
# so ``os.replace`` is atomic.
_STAGING_DIRNAME = '.staging'


def _staging_root(archive_root: str) -> str:
    """Return the path to the staging dir under ``archive_root``.

    Caller is responsible for ``os.makedirs(exist_ok=True)`` before
    writing.
    """
    return os.path.join(archive_root, _STAGING_DIRNAME)


def _staging_partial_path(archive_root: str, dest_path: str) -> str:
    """Compute a unique ``.partial`` path inside the staging dir.

    The basename includes a stable hash of the absolute destination
    path so that two simultaneously-attempted copies of the same
    source to different destinations (legacy migration scenarios)
    can't clobber each other. The hash is short (10 hex chars) to
    keep the staging filename readable.
    """
    abs_dest = os.path.abspath(dest_path)
    digest = hashlib.sha1(
        abs_dest.encode('utf-8', errors='replace'),
    ).hexdigest()[:10]
    base = os.path.basename(dest_path)
    return os.path.join(
        _staging_root(archive_root), f"{digest}-{base}.partial",
    )


def _sweep_partial_orphans(archive_root: str) -> int:
    """Remove orphan ``*.partial`` files from the staging directory.

    Phase H rewrite (issue #184 Wave 3): partials live in
    ``<archive_root>/.staging/`` rather than scattered across the
    archive tree, so this is a single ``os.scandir`` instead of a
    full ``os.walk``. The legacy archive-tree walk is preserved as
    a fallback for one-time migration of any leftovers from before
    this PR landed (e.g., the May 11 SDIO-watchdog reboots).

    Returns the number of orphans removed (0 on a clean tree).
    Best-effort: per-file failures log a warning and continue.

    Safety: only one archive worker exists at a time (enforced by
    ``start_worker``), and the worker doesn't begin claiming rows
    until this sweep completes — so we cannot delete a ``.partial``
    that another writer is currently producing.
    """
    if not archive_root or not os.path.isdir(archive_root):
        return 0
    removed = 0
    staging = _staging_root(archive_root)
    if os.path.isdir(staging):
        try:
            with os.scandir(staging) as it:
                for entry in it:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if not entry.name.endswith('.partial'):
                        continue
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    try:
                        os.remove(entry.path)
                        removed += 1
                        logger.info(
                            "archive_worker: removed staged orphan "
                            "partial %s (%d bytes)",
                            entry.path, size,
                        )
                    except OSError as e:
                        logger.warning(
                            "archive_worker: failed to remove staged "
                            "orphan partial %s: %s", entry.path, e,
                        )
        except OSError as e:
            logger.warning(
                "archive_worker: failed to scan staging dir %s: %s",
                staging, e,
            )

    # One-time migration fallback: clean up any pre-Wave-3 partials
    # still scattered across the archive tree. Once everyone has
    # rebooted on Wave 3 there will be none, and this becomes a
    # zero-cost scandir of an empty match set.
    legacy_removed = 0
    for dirpath, dirnames, filenames in os.walk(
        archive_root, followlinks=False,
    ):
        # Don't descend into .dead_letter or .staging — sidecar .txt
        # files only, but keep the policy symmetric with the
        # watchdog's prune. .staging was already swept above.
        dirnames[:] = [
            d for d in dirnames if d not in ('.dead_letter', _STAGING_DIRNAME)
        ]
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
                legacy_removed += 1
                logger.info(
                    "archive_worker: removed legacy in-tree partial "
                    "%s (%d bytes)", full, size,
                )
            except OSError as e:
                logger.warning(
                    "archive_worker: failed to remove legacy in-tree "
                    "partial %s: %s", full, e,
                )
    return removed + legacy_removed


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


class _CopyMoovIncomplete(OSError):
    """Raised by ``_atomic_copy`` when the destination MP4 verifier
    fails because Tesla is still writing the source segment.

    Distinct from ordinary ``OSError`` so the caller can defer (release
    the claim, refresh expected_size/expected_mtime, return 'pending')
    WITHOUT bumping ``attempts``. Tesla writes RecentClips in ~60-second
    segments and appends the ``moov`` atom only when it closes the
    file; any copy snapshotted before that close legitimately has
    ftyp + partial mdat + no moov. Treating these as failed attempts
    dead-letters perfectly recoverable rows after 3 retries even though
    the file is fine — Tesla just hadn't finished yet.

    A genuinely truncated file (Tesla crashed mid-write, bad SD block)
    will continue to fail moov-verify forever, but the natural
    backstop is the source-rotation path: Tesla overwrites RecentClips
    slots when the circular buffer wraps, expected_size/expected_mtime
    drifts, and the row eventually transitions through
    ``mark_source_gone`` when the producer's stat() resolves None.

    Backstop #2 (this is the safety net for the "stable+corrupt" case):
    ``_process_one_file`` keeps a per-source_path defer count in
    ``_moov_defer_counts``; after ``_MOOV_DEFER_CAP`` defers it falls
    through to the regular ``mark_failed`` path so the row can
    eventually dead_letter rather than re-amplifying SDIO IO forever.
    """


# Per-source_path counter for ``_CopyMoovIncomplete`` defers, used by
# ``_process_one_file`` to enforce ``_MOOV_DEFER_CAP``. OrderedDict
# acts as a simple LRU: when an entry is read, it's moved to the end;
# when ``len`` exceeds ``_MOOV_DEFER_LRU_SIZE`` the oldest entry is
# popped. In-memory only — restarts reset the counter, which is the
# desired behavior (a process restart is itself a recovery event).
# Module-level so the singleton archive worker thread can read/mutate
# without lock contention; not safe for use from multiple threads.
_moov_defer_counts: 'collections.OrderedDict[str, int]' = collections.OrderedDict()


def _bump_moov_defer_count(source_path: str) -> int:
    """Increment and return the moov-incomplete defer count for
    ``source_path``. Maintains LRU eviction of ``_moov_defer_counts``
    so the dict can't grow unbounded across long-running drives.
    """
    count = _moov_defer_counts.get(source_path, 0) + 1
    _moov_defer_counts[source_path] = count
    _moov_defer_counts.move_to_end(source_path)
    while len(_moov_defer_counts) > _MOOV_DEFER_LRU_SIZE:
        _moov_defer_counts.popitem(last=False)
    return count


def _reset_moov_defer_count(source_path: str) -> None:
    """Drop the per-path moov-defer counter (called on a successful
    copy or a transition to a terminal status). Best-effort — no-op
    if the path isn't tracked.
    """
    _moov_defer_counts.pop(source_path, None)


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
                 chunk_pause_always: bool = False,
                 time_budget_seconds: float = 0.0,
                 staging_root: Optional[str] = None,
                 now_fn: Callable[[], float] = time.monotonic,
                 sleep_fn: Callable[[float], None] = time.sleep) -> int:
    """Copy ``source_path`` → ``dest_path`` atomically. Returns size.

    Pattern (Phase H, issue #184 Wave 3): write to a uniquely-named
    ``.partial`` file inside ``staging_root`` (a single ``.staging``
    directory under the archive root), fsync, then ``os.replace``
    into the final destination. The staging dir lives on the same
    filesystem as the destination so the rename is atomic.

    When ``staging_root`` is ``None`` (back-compat for direct test
    callers), partials still go to ``dest_path + '.partial'`` — the
    old in-tree pattern. Production callers always pass
    ``staging_root`` so partials never appear in the archive tree.

    Verifies the rendered size matches the source's stat() size; any
    mismatch raises ``OSError`` so the caller bumps attempts.

    Mid-copy SDIO-contention safeguards (issue #104, extended in #109):

    * If ``chunk_pause_always`` is True (issue #109 — disk fullness
      ≥ 80%), sleep ``chunk_pause_seconds`` between EVERY chunk
      regardless of current loadavg. At very high fullness ext4
      thrashing makes every write slow; by the time loadavg crosses
      ``load_pause_threshold`` the SDIO bus is already saturated.
      Proactively yielding every chunk gives the userspace
      ``watchdog`` daemon a reliable scheduling slot.
    * Otherwise (``chunk_pause_always`` is False, the pre-#109
      behavior): if ``load_pause_threshold > 0``, between chunks
      sample ``os.getloadavg()[0]`` and sleep ``chunk_pause_seconds``
      when it exceeds the threshold. The Pi Zero 2 W shares one SDIO
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
    if staging_root:
        # Phase H: stage the .partial under <archive_root>/.staging/
        # using a hash-prefixed basename so two simultaneous copies
        # of the same destination (legacy migration scenarios) cannot
        # collide. Must be on the same filesystem as ``dest_path`` for
        # ``os.replace`` to be atomic; ``staging_root`` is always a
        # subdir of the archive root so this holds.
        os.makedirs(staging_root, exist_ok=True)
        abs_dest = os.path.abspath(dest_path)
        digest = hashlib.sha1(
            abs_dest.encode('utf-8', errors='replace'),
        ).hexdigest()[:10]
        partial = os.path.join(
            staging_root,
            f"{digest}-{os.path.basename(dest_path)}.partial",
        )
    else:
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
                # Mid-copy load-aware backoff (issue #104 mitigation A,
                # extended by issue #109 mitigation #4 with always-apply
                # mode at high disk fullness).
                if chunk_pause_always:
                    if chunk_pause_seconds > 0:
                        sleep_fn(chunk_pause_seconds)
                elif load_pause_threshold > 0:
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
                raise _CopyMoovIncomplete(
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


# ---------------------------------------------------------------------------
# Issue #197 — inline SEI parse + sidecar write (Wave 4 PR-E2 / Phase I.3)
# ---------------------------------------------------------------------------
# Default indexer sample_rate. Must match
# ``mapping_service.index_single_file``'s default so the sidecar the
# archive worker writes is consumable by the indexer without falling
# back to a fresh mmap parse. If you change one, change the other.
_INLINE_SEI_SAMPLE_RATE = 30

# Wall-clock soft cap on the inline SEI walk + sidecar write.
# Reaching or exceeding this elevates the success log to WARNING so
# operators see "the page-cache hot-path assumption broke for this
# clip" in journalctl at default verbosity. NOT a hard cap — the
# walk completes either way; this is purely an observability signal.
# Calibrated against the 90-second hardware watchdog timeout: 5s
# leaves plenty of headroom for the rest of ``process_one_claim``
# and the worker's outer guards (``per_file_time_budget_seconds``
# defaults to 60s for the copy itself).
_SIDECAR_WRITE_WARN_SECONDS = 5.0


def _write_inline_sei_sidecar(dest_path: str) -> None:
    """Walk the just-copied file's SEI and persist a sidecar JSON.

    Called once per successful ``_atomic_copy``, BEFORE
    ``_enqueue_indexed`` adds the file to the indexer queue. The
    file's pages are still hot in the kernel page cache (we just
    wrote them), so the SEI walk costs only the protobuf decode
    work — no extra SD reads. The sidecar lets the indexer's later
    pass skip both the ``mvhd`` walk and the ``extract_sei_messages``
    walk, eliminating ~2x file reads per clip and the associated
    page-cache miss.

    **Time-budget context (issue #104 + review of PR #205).** This
    helper runs OUTSIDE the per-file ``chunk_pause_seconds`` /
    ``per_file_time_budget_seconds`` guards that ``_atomic_copy``
    enforces. That is intentional and safe because:

    * The work here is CPU-bound (protobuf decode on small NAL
      buffers), not I/O-bound — the SD card is not the bottleneck,
      so the SDIO-bus-saturation failure mode the time-budget
      guards exist for does not apply.
    * The file's pages are already resident in the page cache from
      the immediately-prior ``_atomic_copy``, so no fresh SD reads
      occur during the walk.
    * The walk samples every ``_INLINE_SEI_SAMPLE_RATE`` (30) SEI
      NAL — total work is O(file_size / 30), which on a worst-case
      80 MB Tesla clip is well under a second on the Pi Zero 2 W.
    * The downstream `_enqueue_indexed` call is a single SQLite
      INSERT — sub-millisecond.

    To keep that contract observable in production, we measure the
    wall-clock and emit a WARNING if the helper exceeds
    ``_SIDECAR_WRITE_WARN_SECONDS``. That gives operators an early
    signal if the assumption above breaks (e.g. a future change
    drops the sample-rate, or a corrupt clip causes pathological
    parser behaviour) without forcing a hard cap that would silently
    drop the optimization on every slow clip.

    Best-effort: any failure is logged at WARNING and silently
    swallowed. The downstream indexer's existing fallback path
    (``read_sei_sidecar`` returning None → mmap parse) handles
    missing sidecars transparently, so a sidecar-write failure
    only loses the I/O optimization, never data.
    """
    start = time.monotonic()
    try:
        from services import sei_parser
    except Exception as e:  # noqa: BLE001
        logger.debug(
            "inline-sei: sei_parser unavailable for %s (%s); "
            "skipping sidecar write", dest_path, e,
        )
        return

    try:
        sidecar = sei_parser.write_sei_sidecar(
            dest_path, sample_rate=_INLINE_SEI_SAMPLE_RATE,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "inline-sei: sidecar write threw on %s "
            "(indexer will mmap-parse): %s", dest_path, e,
        )
        return

    elapsed = time.monotonic() - start
    if sidecar is None:
        # write_sei_sidecar already logged the failure cause at
        # DEBUG/WARNING; no need to double-log here.
        return

    mvhd_repr = (
        sidecar.mvhd_creation_time_utc.isoformat()
        if sidecar.mvhd_creation_time_utc is not None
        else 'unknown'
    )
    if elapsed >= _SIDECAR_WRITE_WARN_SECONDS:
        # Near-miss against the watchdog timeout — the page-cache
        # hot-path assumption above appears to no longer hold for
        # this clip. Surface at WARNING so it shows up in journalctl
        # at the default verbosity.
        logger.warning(
            "inline-sei: sidecar write took %.2fs (>= %.1fs warn "
            "threshold) for %s — page-cache fast-path may have "
            "missed; %d messages, mvhd=%s",
            elapsed, _SIDECAR_WRITE_WARN_SECONDS,
            os.path.basename(dest_path),
            sidecar.sei_count, mvhd_repr,
        )
    else:
        logger.info(
            "inline-sei: parsed %d messages (%d GPS, %d no-GPS), "
            "mvhd=%s, elapsed=%.2fs for %s",
            sidecar.sei_count, len(sidecar.messages),
            sidecar.no_gps_count, mvhd_repr, elapsed,
            os.path.basename(dest_path),
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


# ---------------------------------------------------------------------------
# Issue #109 — disk-fullness-adaptive throttling helpers
# ---------------------------------------------------------------------------
#
# At very high disk fullness (95%+ on ext4) the free-block-group search
# inflates per-write cost 10–100×, so the same 1-min loadavg represents
# far more SDIO contention than it would at 50% disk. The post-#106
# guards (load_pause_threshold + chunk_pause + per-file budget) are
# calibrated against healthy disk performance — they pause/abort
# correctly, but the in-flight copy can still hold the SDIO bus long
# enough to starve the userspace ``watchdog`` daemon's 90 s ping
# window before the next iteration's guard fires.
#
# These helpers scale the throttling MORE AGGRESSIVELY as fullness
# climbs, so the worker pauses sooner AND every chunk yields some
# SDIO time without waiting for load to spike.

def _disk_fullness_pct(path: str) -> Optional[float]:
    """Return ``used / total * 100`` for the filesystem at ``path``.

    Returns ``None`` if the stat fails (transient filesystem hiccup,
    path missing, etc.). Callers MUST treat ``None`` as "no adaptive
    adjustment" so a stat failure can't accidentally lock the worker
    out — the pre-#109 guards still apply.
    """
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    if usage.total <= 0:
        return None
    return (usage.used / usage.total) * 100.0


def _adaptive_load_threshold(base: float, fullness_pct: Optional[float]) -> float:
    """Issue #109 mitigation #2 — scale ``load_pause_threshold`` down
    by disk fullness.

    The same loadavg of 4.0 is a very different situation at 50% disk
    (healthy ext4, fast writes) vs 98% disk (ext4 thrashing, every
    write 10–100× slower). At high fullness, pause sooner so a single
    in-flight copy doesn't starve the watchdog daemon.

    Returns ``base`` unchanged when:
      * ``base <= 0`` (guard disabled by config),
      * ``fullness_pct`` is ``None`` (stat failed),
      * fullness is below 80% (healthy regime).

    Otherwise subtracts 0.5 / 1.0 / 1.5 at 80 / 90 / 95% fullness and
    floors at 1.0 — even a misconfigured low base can never fully
    disable the guard.
    """
    if base <= 0 or fullness_pct is None:
        return base
    if fullness_pct >= 95.0:
        return max(base - 1.5, 1.0)
    if fullness_pct >= 90.0:
        return max(base - 1.0, 1.0)
    if fullness_pct >= 80.0:
        return max(base - 0.5, 1.0)
    return base


def _adaptive_chunk_pause(
        base: float, fullness_pct: Optional[float]) -> Tuple[float, bool]:
    """Issue #109 mitigation #4 — scale ``chunk_pause_seconds`` and
    flip its trigger to "always-apply" at high disk fullness.

    Pre-#109 the per-chunk pause inside ``_atomic_copy`` only fires
    when ``loadavg > load_pause_threshold``. At very high fullness the
    damage is done by the time loadavg crosses; we need a proactive
    pause that yields SDIO time on EVERY chunk regardless of current
    load.

    Returns ``(pause_seconds, always_apply)``:
      * ``< 80%`` fullness or ``base <= 0`` or unknown fullness:
        ``(base, False)`` — current behavior, gated on loadavg.
      * ``80–95%`` fullness: ``(base, True)`` — same duration but
        applied EVERY chunk (proactive yield).
      * ``≥ 95%`` fullness: ``(base * 2, True)`` — doubled and
        always-applied (heaviest yield).

    Floors at 0.05 s when always-applied so a misconfigured 0.0 base
    can't flat-out disable the proactive yield.
    """
    if base <= 0 or fullness_pct is None:
        return (base, False)
    if fullness_pct >= 95.0:
        return (max(base * 2.0, 0.05), True)
    if fullness_pct >= 80.0:
        return (max(base, 0.05), True)
    return (base, False)


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


def _recent_clips_stable_write_age_seconds() -> float:
    """Return RecentClips-specific stable-write threshold.

    Tesla writes RecentClips in ~60-second segments and only appends
    the ``moov`` atom when it closes the segment. The base 5-second
    gate is far too short — a 5-second mtime-stable window mid-segment
    is normal SDIO behavior and tricks the worker into copying a
    half-written file. 90 seconds guarantees Tesla has finished any
    in-progress segment before we copy.

    Sentry/Saved event clips are written atomically when the event
    completes (no mid-write window) and use the base threshold.

    Configurable via ``archive_queue.recent_clips_stable_write_age_seconds``.
    """
    try:
        from config import (
            ARCHIVE_QUEUE_RECENT_CLIPS_STABLE_WRITE_AGE_SECONDS,
        )
        return float(ARCHIVE_QUEUE_RECENT_CLIPS_STABLE_WRITE_AGE_SECONDS)
    except Exception:  # noqa: BLE001
        return _RECENT_CLIPS_STABLE_WRITE_AGE_SECONDS


def _stable_write_age_seconds_for(row: Dict[str, Any]) -> float:
    """Return the stable-write threshold for ``row``.

    RecentClips need a much longer settle window than Sentry/Saved
    because Tesla writes them in ~60-second segments with the moov
    atom appended at the end. Returns ``max(base, recent_threshold)``
    so a config override that pushes the base above the RecentClips
    default still applies.
    """
    base = _stable_write_age_seconds()
    if _is_recent_clips_priority(row):
        return max(base, _recent_clips_stable_write_age_seconds())
    return base


# ---------------------------------------------------------------------------
# Issue #167 sub-deliverable 2 — skip-at-source for stationary RecentClips
# Issue #176 — fast-peek tuning
# ---------------------------------------------------------------------------

# Cap on SEI messages scanned by ``_clip_has_gps_signal`` before declaring
# "no GPS". Tesla writes one SEI NAL per frame at ~30 fps; with sample_rate=30
# one SeiMessage is yielded per ~1 s of video. The cap is mainly a defensive
# bound for degenerate inputs (e.g., a corrupted clip that yields tens of
# thousands of SEI NALs); the dominant termination signal for a moving clip
# is the early ``return True`` on the first GPS-bearing message, and for a
# stationary clip is ``_SKIP_GPS_PEEK_MAX_WALK_BYTES`` (below).
_SKIP_GPS_PEEK_MAX_MESSAGES = 90
# Sample rate for the peek. ``30`` matches what the indexer uses — one SEI
# per ~1 s of video — so the I/O footprint of the peek is on the same order
# as one indexer pass.
_SKIP_GPS_PEEK_SAMPLE_RATE = 30
# Issue #176 — hard cap on bytes walked through the ``mdat`` box during the
# stationary peek. Tesla writes ZERO SEI NAL units in parked Sentry clips
# (no telemetry to record), so the message-count cap above never fires on
# parked footage — the parser would otherwise walk the entire ``mdat`` box
# (~25-50 MB on Tesla cameras) and page in the whole file via mmap to
# confirm "no SEI exists". Bench results on cybertruckusb.local (Pi
# Zero 2 W) showed 1.2-3.7 s of cold-cache wall time per parked clip in
# the unlimited case versus ~20 ms with a 2 MB cap — a ~200x speedup that
# eliminates the load-pause throttle the worker was hitting every 7 clips.
# 2 MB is enough to comfortably catch the first GPS-bearing SEI in any
# moving clip (Tesla writes 1 SEI per frame at 30 fps; at ~3 Mbit/s that's
# ~30 SEI messages in 1 s of video ≈ ~370 KB into ``mdat``). A driving
# clip that has zero GPS lock for the first 5 seconds would be
# misclassified as stationary, but the user already opted in to skipping
# stationary clips and that edge case is far rarer than the stall it cures.
_SKIP_GPS_PEEK_MAX_WALK_BYTES = 2 * 1024 * 1024


def _clip_has_gps_signal(source_path: str) -> Optional[bool]:
    """Return whether a RecentClips candidate has any GPS-bearing SEI.

    Issue #167 sub-deliverable 2 — fast SEI peek used by
    :func:`process_one_claim` to decide whether to skip a stationary
    overnight RecentClips clip before copying it. Issue #184 Wave 1
    made the skip-stationary behavior unconditional — there is no
    longer an opt-in toggle; the peek runs for every RecentClips
    candidate.

    Returns:
      * ``True``  — at least one SEI message with non-zero lat/lon was
        found in the first ``_SKIP_GPS_PEEK_MAX_MESSAGES`` samples.
        The clip is "moving"; the worker should copy it.
      * ``False`` — every sampled SEI message had ``has_gps == False``,
        or the file has no SEI messages at all (e.g., a non-camera
        recording), or the file has fewer than the cap. The clip is
        "stationary"; the worker can mark it ``skipped_stationary``.
      * ``None``  — we couldn't decisively determine GPS presence
        (parse error, file missing, etc.). The caller MUST treat
        ``None`` as "fall through to normal copy" so ambiguous clips
        are never silently dropped — the existing copy-then-index
        path will handle them safely.

    Memory and I/O model: re-uses the same ``mmap``-backed
    :func:`sei_parser.extract_sei_messages` generator the indexer
    uses, with an early ``break`` on the first GPS-bearing message
    AND a ``max_walk_bytes`` cap so a parked clip with zero SEI does
    NOT have to page in the entire ``mdat`` box to confirm absence.
    Issue #176 — without the byte cap, the parser walks the whole
    ``mdat`` box (~25-50 MB) for every parked clip because the
    message-count cap never fires when Tesla emits zero SEI messages
    in stationary footage. Bench: 1.2-3.7 s per peek without cap,
    ~20 ms per peek with a 2 MB cap.
    """
    try:
        from services import sei_parser
    except Exception as e:  # noqa: BLE001
        # Parser missing in this environment (e.g., a unit-test stub
        # without the parser path). Be safe — fall through to copy.
        logger.debug(
            "_clip_has_gps_signal: sei_parser unavailable (%s); "
            "deferring to copy", e,
        )
        return None

    scanned = 0
    try:
        for msg in sei_parser.extract_sei_messages(
                source_path,
                sample_rate=_SKIP_GPS_PEEK_SAMPLE_RATE,
                max_walk_bytes=_SKIP_GPS_PEEK_MAX_WALK_BYTES):
            scanned += 1
            if msg.has_gps:
                return True
            if scanned >= _SKIP_GPS_PEEK_MAX_MESSAGES:
                break
        # Generator exhausted (or hit cap) without a GPS-bearing
        # message. Two sub-cases — both decisive:
        #   * scanned == 0 — no SEI at all. For a Tesla dashcam clip
        #     this is vanishingly rare (every Tesla clip has SEI),
        #     and a clip with no SEI is by definition not a clip
        #     we'd want to map. Treat as stationary (skip).
        #   * scanned > 0 — clip has SEI but no GPS. This is the
        #     stationary signature. Skip.
        return False
    except FileNotFoundError:
        # Tesla rotated the source between the stable-write gate and
        # the peek. Caller will re-stat and mark source_gone.
        return None
    except Exception as e:  # noqa: BLE001
        # Any parse error (corrupt MP4, mmap failure, protobuf decode
        # blow-up) — fall through to the existing copy path so we
        # never lose data on a parser bug.
        logger.warning(
            "_clip_has_gps_signal: peek failed for %s (%s); "
            "deferring to copy", source_path, e,
        )
        return None


def _is_recent_clips_priority(row: Dict[str, Any]) -> bool:
    """Return True iff the queue row is a RecentClips candidate.

    Phase 2a tagged each row at enqueue time with the priority value
    from :func:`archive_queue._infer_priority`, so we can decide
    purely from the row dict without re-walking the path.
    """
    try:
        return int(row.get('priority', 0)) == archive_queue.PRIORITY_RECENT_CLIPS
    except (TypeError, ValueError):
        return False


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
      * ``'skipped_stationary'`` — issue #167 sub-deliverable 2 (made
                             unconditional in issue #184 Wave 1):
                             RecentClips clip with no GPS-bearing SEI
                             message; the worker always skips parked-no-event
                             RecentClips at source (no retry, terminal)
      * ``'pending'``      — released back to pending (stable-write
                             gate, disk pause, time-budget abort,
                             transient error with attempts left)
      * ``'dead_letter'``  — attempts exhausted

    The ``load_pause_threshold``, ``chunk_pause_seconds``, and
    ``time_budget_seconds`` keyword args are forwarded to
    :func:`_atomic_copy` for the issue #104 mid-copy SDIO-contention
    safeguards. Defaults are conservative (off / 0.25 s / off) so
    callers that don't opt in get pre-#104 behavior.

    Issue #109: the ``load_pause_threshold`` and ``chunk_pause_seconds``
    actually forwarded to ``_atomic_copy`` are derived from the BASE
    values via :func:`_adaptive_load_threshold` and
    :func:`_adaptive_chunk_pause` so the throttling becomes more
    aggressive as the SD card fills (less load tolerance, longer
    always-applied chunk pauses at 80%+ fullness).

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
    # Per-row stable-write threshold: RecentClips need ~90s (Tesla
    # writes them in ~60s segments and appends the moov atom only at
    # close); Sentry/Saved use the base 5s (they're written atomically
    # when the event ends). Without this distinction, the worker
    # copies half-written RecentClips, fails moov-verify, retries,
    # and dead-letters perfectly recoverable rows after 3 attempts.
    if age < _stable_write_age_seconds_for(row) and needs_settling_check:
        # Update the snapshot so the next pick uses fresh values.
        archive_queue.release_claim(
            row_id,
            expected_size=st.st_size,
            expected_mtime=st.st_mtime,
            db_path=db_path,
        )
        return 'pending'

    # Issue #167 sub-deliverable 2 — skip-at-source for stationary
    # RecentClips. Issue #184 Wave 1 made this unconditional: every
    # RecentClips candidate is SEI-peeked. Runs AFTER the stable-write
    # gate (so we never peek at a half-written file) and BEFORE the
    # disk-space guard (so a successful skip can't be falsely paused
    # by a 'critical' disk verdict — the skip frees no bytes itself
    # but neither does it consume any). Sentry/Saved event clips have
    # priority 1 and never enter this branch — they bypass the SEI
    # peek entirely and follow the normal copy path.
    # ``_clip_has_gps_signal`` returns ``None`` for any ambiguous case
    # (parse error, mmap failure, file vanished mid-peek), and we fall
    # through to the normal copy path so a parser bug can never
    # silently drop a clip we should have copied.
    if _is_recent_clips_priority(row):
        gps_signal = _clip_has_gps_signal(source_path)
        if gps_signal is False:
            archive_queue.mark_skipped_stationary(row_id, db_path=db_path)
            logger.info(
                "archive_worker: skipped stationary RecentClips %s "
                "(no GPS-bearing SEI in first %d KB / %d msgs)",
                source_path,
                _SKIP_GPS_PEEK_MAX_WALK_BYTES // 1024,
                _SKIP_GPS_PEEK_MAX_MESSAGES,
            )
            return 'skipped_stationary'
        # gps_signal True → has GPS, fall through to normal copy.
        # gps_signal None → couldn't decide, fall through to copy
        # (data-preservation default).

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
        # Issue #109 — derive adaptive throttling values from current
        # SD-card fullness. ``shutil.disk_usage`` is one syscall; cheap
        # to do once per file. At 80%+ we raise the always-apply flag;
        # at 95%+ we both raise the flag AND double the chunk pause.
        # The forwarded ``load_pause_threshold`` is also lowered so
        # later iterations of the worker loop (which re-reads it
        # per iteration) pause sooner — but inside _atomic_copy the
        # always-apply chunk pause supersedes the load-gated path.
        _fullness = _disk_fullness_pct(archive_root)
        _adaptive_load = _adaptive_load_threshold(
            load_pause_threshold, _fullness,
        )
        _adaptive_pause, _pause_always = _adaptive_chunk_pause(
            chunk_pause_seconds, _fullness,
        )
        _atomic_copy(
            source_path, dest_path, chunk_size,
            load_pause_threshold=_adaptive_load,
            chunk_pause_seconds=_adaptive_pause,
            chunk_pause_always=_pause_always,
            time_budget_seconds=time_budget_seconds,
            staging_root=_staging_root(archive_root),
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
    except _CopyMoovIncomplete as e:
        # Tesla writes RecentClips in ~60-second segments and only
        # appends the moov atom when it closes the file. A copy
        # snapshotted before that close legitimately has ftyp +
        # partial mdat + no moov; the file isn't broken, Tesla just
        # hasn't finished. Release back to pending WITHOUT bumping
        # attempts (so we never dead_letter a perfectly recoverable
        # row) and refresh expected_size/expected_mtime so the next
        # pick lands AFTER the per-row stable-write gate
        # (``_stable_write_age_seconds_for``) has had a chance to fire.
        # Genuinely truncated files still terminate naturally: Tesla
        # rotates RecentClips slots, the producer's stat() resolves
        # to a different size/mtime, and the row reaches
        # ``mark_source_gone`` when the file finally vanishes.
        defer_count = _bump_moov_defer_count(source_path)
        if defer_count > _MOOV_DEFER_CAP:
            # Backstop for the rare "stable + corrupt" case (bad SD
            # block, Tesla crashed mid-segment then never rotated):
            # release the moov-incomplete protection, fall through to
            # mark_failed, bump attempts, and let the regular
            # max_attempts → dead_letter path engage. Without this cap
            # the worker would re-read + re-stage + re-delete the
            # corrupt file forever (~30–360 MB SDIO per iteration).
            logger.warning(
                "archive_worker: moov-incomplete on %s deferred "
                "%d times (cap=%d) — escalating to mark_failed; "
                "file is likely genuinely truncated, not still "
                "being written: %r",
                source_path, defer_count, _MOOV_DEFER_CAP, e,
            )
            _reset_moov_defer_count(source_path)
            new_status = archive_queue.mark_failed(
                row_id,
                f"copy: moov-incomplete after {defer_count} defers: {e!r}",
                max_attempts=max_attempts, db_path=db_path,
            )
            if new_status == 'dead_letter':
                row_for_sidecar = dict(row)
                row_for_sidecar['dest_path'] = dest_path
                row_for_sidecar['last_error'] = (
                    f"copy: moov-incomplete after {defer_count} defers: {e!r}"
                )
                row_for_sidecar['attempts'] = int(
                    row.get('attempts') or 0,
                ) + 1
                _write_dead_letter_sidecar(archive_root, row_for_sidecar)
            return new_status
        # Per project convention ("don't log per-tick events at INFO"):
        # the defer can fire many times for the same source_path while
        # Tesla finishes a segment. Keep these at DEBUG so journalctl
        # at default verbosity stays useful; escalation to WARNING
        # happens at the cap above.
        logger.debug(
            "archive_worker: moov-incomplete on %s "
            "(source still being written, defer #%d/%d); "
            "deferring without burning an attempt: %r",
            source_path, defer_count, _MOOV_DEFER_CAP, e,
        )
        st = _safe_stat(source_path)
        if st is None:
            _reset_moov_defer_count(source_path)
            archive_queue.mark_source_gone(row_id, db_path=db_path)
            return 'source_gone'
        archive_queue.release_claim(
            row_id,
            expected_size=st.st_size,
            expected_mtime=st.st_mtime,
            db_path=db_path,
        )
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
    # Drop any moov-defer counter for this source so a row that
    # recovered after N < cap defers doesn't leave stale state behind.
    _reset_moov_defer_count(source_path)
    archive_queue.mark_copied(row_id, dest_path, db_path=db_path)
    # Issue #197: while the just-copied file's pages are still hot
    # in the kernel page cache, parse SEI + mvhd inline and write
    # a sidecar JSON. The indexer's later pass reads the sidecar
    # instead of mmap-parsing the .mp4 a second time.
    #
    # Best-effort by contract — wrap in defense-in-depth try/except
    # so a sidecar bug or unexpected exception never marks the
    # archive failed. The helper has its own internal try/except,
    # but a future refactor (or a monkeypatched stub in tests)
    # could let an exception escape; this outer guard preserves
    # the "sidecar is an optimization, never a correctness gate"
    # contract regardless.
    try:
        _write_inline_sei_sidecar(dest_path)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "inline-sei: sidecar write escaped helper for %s "
            "(indexer will mmap-parse): %s", dest_path, e,
        )
    _enqueue_indexed(dest_path, db_path)
    return 'copied'


# ---------------------------------------------------------------------------
# Wave 4 PR-E (issue #184): pipeline_queue shadow comparison
# ---------------------------------------------------------------------------
# Pure observability — peek at what the unified ``pipeline_queue``
# reader would pick BEFORE each legacy ``archive_queue`` claim, log
# WARNING when they disagree. Validates the dual-write end-to-end
# against real production traffic so we can confirm the unified
# reader is safe to switch on (PR-F) before flipping the cutover.
# Counters live at module scope (process-local, reset on restart) so
# operators can read totals via the upcoming status endpoint without
# grepping the journal for every line.

# Throttle for "agreement" log lines so a healthy queue doesn't
# flood the journal — log every Nth match at INFO so operators can
# see the shadow path is still firing, while disagreements log at
# WARNING with both paths but are also rate-limited so a degenerate
# dual-write state (e.g. catch-up of a 1000+ clip backlog) cannot
# emit one WARNING per worker iteration. The first
# ``_SHADOW_DISAGREEMENT_LOG_VERBATIM`` mismatches log verbatim;
# after that we drop to one heartbeat WARNING per
# ``_SHADOW_DISAGREEMENT_LOG_EVERY`` mismatches with the running
# count so journal hygiene is preserved.
_SHADOW_AGREEMENT_LOG_EVERY = 500
_SHADOW_DISAGREEMENT_LOG_VERBATIM = 10
_SHADOW_DISAGREEMENT_LOG_EVERY = 100
# Number of pipeline_queue candidates we peek at when comparing
# against the legacy reader's pick. The legacy reader orders by
# ``priority, expected_mtime, id`` while pipeline_queue orders by
# ``priority, enqueued_at, id`` — a documented and accepted
# divergence (see ``pipeline_queue_service.claim_next_for_stage``
# docstring). Treating the legacy pick as "agreed" iff it appears
# anywhere in the pipeline_queue's top-N candidates absorbs that
# benign reordering and only flags a real dual-write gap (legacy
# picked a path the pipeline doesn't even know about).
_SHADOW_PEEK_CANDIDATE_COUNT = 8

_shadow_lock = threading.Lock()
_shadow_agreement_count = 0
_shadow_disagreement_count = 0


def _shadow_pipeline_queue_enabled() -> bool:
    """Return True iff the operator has the shadow-mode flag on.

    Wrapped in a function so a config reload (or test override) is
    picked up on the next worker iteration without restarting the
    thread. Lazy import so the worker module can still be imported
    in test contexts where ``config`` isn't bootstrapped.
    """
    try:
        from config import ARCHIVE_QUEUE_SHADOW_PIPELINE_QUEUE
        return bool(ARCHIVE_QUEUE_SHADOW_PIPELINE_QUEUE)
    except Exception:  # noqa: BLE001
        return False


def _shadow_compare_picks(
    *,
    legacy_path: Optional[str],
    pipeline_candidates: Tuple[str, ...] = (),
) -> None:
    """Compare the legacy pick against the pipeline_queue top-N.

    The legacy ``archive_queue`` reader orders by
    ``priority, expected_mtime, id``; ``pipeline_queue`` orders by
    ``priority, enqueued_at, id`` (see
    ``pipeline_queue_service.claim_next_for_stage`` docstring). The
    secondary-key divergence is documented and accepted — comparing
    only the top-1 of each reader would systematically WARN on
    benign reorderings (e.g. boot catch-up enqueues a batch in
    directory-walk order while Tesla wrote them in mtime order).

    To avoid that noise we treat the legacy pick as **agreed** iff
    it appears anywhere in ``pipeline_candidates`` (top-N pipeline
    rows for the same stage). Only when the legacy pick is **absent**
    from the top-N does the WARNING fire — that's a real dual-write
    gap (a row exists in archive_queue but not, or far down, in
    pipeline_queue) worth investigating before PR-F cuts over the
    reader.

    Empty queue case: both ``legacy_path`` is ``None`` AND
    ``pipeline_candidates`` is empty ⇒ both readers say "queue
    empty" ⇒ counted as agreement, no log.

    Disagreement logging is rate-limited: the first
    ``_SHADOW_DISAGREEMENT_LOG_VERBATIM`` mismatches log verbatim
    with both paths; after that one heartbeat WARNING per
    ``_SHADOW_DISAGREEMENT_LOG_EVERY`` mismatches surfaces the
    running count so journal hygiene is preserved on Pi Zero 2 W
    even during a degenerate catch-up.
    """
    global _shadow_agreement_count, _shadow_disagreement_count
    candidate_set = (
        frozenset(p for p in pipeline_candidates if p)
        if pipeline_candidates else frozenset()
    )
    if legacy_path is None:
        # Legacy says "no work". Agreement iff pipeline_queue also
        # has nothing ready. If pipeline_queue has rows but legacy
        # doesn't, treat as a (benign) ordering case rather than a
        # gap — there's no legacy pick to mismatch against, and the
        # row will surface on the next iteration.
        with _shadow_lock:
            _shadow_agreement_count += 1
            count = _shadow_agreement_count
        if count % _SHADOW_AGREEMENT_LOG_EVERY == 0:
            logger.info(
                "Wave 4 PR-E shadow: pipeline_queue agreed with "
                "archive_queue on %d consecutive picks",
                count,
            )
        return
    if legacy_path in candidate_set:
        with _shadow_lock:
            _shadow_agreement_count += 1
            count = _shadow_agreement_count
        if count % _SHADOW_AGREEMENT_LOG_EVERY == 0:
            logger.info(
                "Wave 4 PR-E shadow: pipeline_queue agreed with "
                "archive_queue on %d consecutive picks "
                "(top-%d window)",
                count, _SHADOW_PEEK_CANDIDATE_COUNT,
            )
        return
    with _shadow_lock:
        _shadow_disagreement_count += 1
        d_count = _shadow_disagreement_count
    if d_count <= _SHADOW_DISAGREEMENT_LOG_VERBATIM:
        logger.warning(
            "Wave 4 PR-E shadow: archive_queue picked %r but it is "
            "absent from the top-%d pipeline_queue candidates "
            "(disagreement #%d). pipeline top-%d=%r. The legacy "
            "and pipeline readers use different secondary sort "
            "keys (expected_mtime vs. enqueued_at) so small "
            "reorderings within a priority band are expected — a "
            "miss from the top-N window indicates a real "
            "dual-write gap. Investigate before PR-F cuts over "
            "the reader.",
            legacy_path, _SHADOW_PEEK_CANDIDATE_COUNT, d_count,
            _SHADOW_PEEK_CANDIDATE_COUNT,
            tuple(pipeline_candidates),
        )
    elif d_count % _SHADOW_DISAGREEMENT_LOG_EVERY == 0:
        logger.warning(
            "Wave 4 PR-E shadow: archive_queue / pipeline_queue "
            "disagreement count = %d (suppressing per-event "
            "WARNINGs after the first %d; first %d are above). "
            "Last legacy pick: %r.",
            d_count, _SHADOW_DISAGREEMENT_LOG_VERBATIM,
            _SHADOW_DISAGREEMENT_LOG_VERBATIM, legacy_path,
        )


def get_shadow_telemetry() -> Dict[str, int]:
    """Return the in-memory shadow comparison counters as a snapshot.

    Process-local, reset on restart. Used by tests and by the
    Settings page to confirm the shadow mode is firing in
    production.
    """
    with _shadow_lock:
        return {
            'shadow_agreement_count': _shadow_agreement_count,
            'shadow_disagreement_count': _shadow_disagreement_count,
        }


# ---------------------------------------------------------------------------
# Wave 4 PR-F1 (issue #184): unified-queue reader cutover
# ---------------------------------------------------------------------------
# Switches the worker's claim site from ``archive_queue.claim_next_for_worker``
# to ``pipeline_queue_service.claim_next_for_stage`` when the
# ``archive_queue.use_pipeline_reader`` config flag is on. The legacy
# ``archive_queue`` row is mirrored to ``status='claimed'`` immediately
# after the pipeline claim succeeds so:
#
#   * single-worker invariants hold (archive_queue.pending count
#     reflects reality even before mark_copied/mark_failed fires);
#   * the existing dual-write hooks on archive_queue.mark_copied /
#     mark_failed / release_claim continue to mirror state changes
#     back to pipeline_queue with no further wiring;
#   * a flag-flip back to OFF immediately reverts to the legacy path
#     with no DB rollback needed (both tables stay consistent
#     because dual-write was active throughout).
#
# When the flag is on the shadow comparison is skipped — we ARE the
# pipeline reader now, so comparing to the legacy reader is moot
# (PR-F2 will add an inverse shadow that compares legacy-pick against
# the unified worker's actual pick).


def _use_pipeline_reader_enabled() -> bool:
    """Return True iff the operator has the reader-cutover flag on.

    Wrapped in a function so a config reload (or test override) is
    picked up on the next worker iteration without restarting the
    thread. Lazy import so the worker module can still be imported
    in test contexts where ``config`` isn't bootstrapped.
    """
    try:
        from config import ARCHIVE_QUEUE_USE_PIPELINE_READER
        return bool(ARCHIVE_QUEUE_USE_PIPELINE_READER)
    except Exception:  # noqa: BLE001
        return False


def _adapt_pipeline_row_to_legacy_shape(
    pipeline_row: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Convert a ``pipeline_queue`` row into the dict shape that
    ``process_one_claim`` and the existing legacy ``mark_*`` helpers
    expect.

    The pipeline row carries:
      * ``id`` — the pipeline_queue PK (NOT what process_one_claim
        wants).
      * ``legacy_id`` — back-pointer to ``archive_queue.id``. **This
        is what the legacy ``mark_*`` helpers expect as ``id``.**
      * ``source_path`` / ``dest_path`` — pass through as-is.
      * ``priority`` / ``attempts`` / ``enqueued_at`` / ``last_error``
        — pass through as-is.
      * ``payload`` (deserialised) — carries ``expected_size`` and
        ``expected_mtime`` (the legacy stable-write gate inputs).

    Returns ``None`` when ``pipeline_row`` is ``None`` so the caller
    can use the same "is None?" idiom the legacy claim path used.
    Returns ``None`` when ``legacy_id`` is missing — that would mean
    the row was enqueued without a back-pointer (data corruption);
    we refuse to process it via the legacy ``mark_*`` helpers
    because they key on ``archive_queue.id`` which we don't have.
    """
    if pipeline_row is None:
        return None
    legacy_id = pipeline_row.get('legacy_id')
    if legacy_id is None:
        return None
    payload = pipeline_row.get('payload') or {}
    return {
        'id': int(legacy_id),
        'source_path': pipeline_row.get('source_path'),
        'dest_path': (
            pipeline_row.get('dest_path')
            or payload.get('dest_path')
        ),
        'expected_size': payload.get('expected_size'),
        'expected_mtime': payload.get('expected_mtime'),
        'priority': pipeline_row.get('priority'),
        'attempts': pipeline_row.get('attempts'),
        'status': 'claimed',
        'claimed_at': pipeline_row.get('claimed_at'),
        'claimed_by': pipeline_row.get('claimed_by'),
        'enqueued_at': pipeline_row.get('enqueued_at'),
        'last_error': pipeline_row.get('last_error'),
    }


def _claim_via_pipeline_reader(
    worker_id: str,
    db_path: str,
) -> Optional[Dict[str, Any]]:
    """Claim the next ``archive_pending`` row via ``pipeline_queue``,
    mirror to ``archive_queue``, and return a legacy-shaped row dict.

    Three failure paths, all return ``None`` (worker treats as "no
    work this iteration", same as legacy ``claim_next_for_worker``):

    1. ``pipeline_queue.claim_next_for_stage`` returns ``None`` —
       queue empty, missing DB, or sqlite error. Already logged
       inside the helper (DEBUG / WARNING as appropriate).
    2. The pipeline row has no ``legacy_id`` (data-shape gap).
       Logged at WARNING because it indicates a dual-write
       enqueue that didn't supply the back-pointer — should never
       happen in production but handled defensively. The pipeline
       row stays ``in_progress`` with ``last_error`` updated to
       describe the gap; the next ``recover_stale_claims_pipeline``
       cycle will release it once ``claimed_at`` ages past the
       stale window.
    3. ``archive_queue.claim_specific_pending`` returns ``None`` —
       the legacy row was deleted, already-claimed by a stale
       process, or never existed. We release the pipeline claim back
       to ``pending`` so the next iteration can reconcile, and log
       at WARNING with the legacy_id for forensics.

    Success path: pipeline_queue row is in_progress, archive_queue
    row is claimed, returned dict is legacy-shaped. The downstream
    ``mark_copied`` / ``mark_failed`` / ``release_claim`` calls all
    take the legacy id and dual-write back to pipeline_queue via
    the existing PR-B hooks — no further wiring required.
    """
    try:
        from services import pipeline_queue_service as pqs
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "PR-F1 reader switch: pipeline_queue_service import "
            "failed (%s) — falling back to no-op", e,
        )
        return None
    pipeline_row = pqs.claim_next_for_stage(
        stage='archive_pending',
        claimed_by=worker_id,
        db_path=db_path,
    )
    if pipeline_row is None:
        return None
    legacy_id = pipeline_row.get('legacy_id')
    if legacy_id is None:
        # Wave 4 PR-F1 review fix #1 (PR #198): the dual-write
        # enqueue did not set legacy_id. This is a permanent
        # data-shape corruption that retrying cannot fix — the
        # back-pointer was never set, so no number of recovery
        # cycles will produce one. Move the row to ``dead_letter``
        # immediately (instead of leaving it in_progress and letting
        # ``recover_stale_claims_pipeline`` keep recycling it back to
        # pending in a tight loop). Operators can inspect the
        # dead-letter rows via the upcoming /api/pipeline_queue/
        # dead_letter endpoint and either manually backfill the
        # legacy_id or drop the row.
        try:
            pqs.update_pipeline_row(
                stage='archive_pending',
                source_path=pipeline_row.get('source_path') or '',
                status='dead_letter',
                last_error=(
                    'PR-F1: pipeline_queue row missing legacy_id '
                    '(unrecoverable data-shape corruption); manual '
                    'intervention required'
                ),
                db_path=db_path,
            )
        except Exception:  # noqa: BLE001
            pass
        logger.warning(
            "PR-F1 reader switch: pipeline_queue row id=%s has no "
            "legacy_id — moved to dead_letter (unrecoverable). "
            "Source: %r",
            pipeline_row.get('id'), pipeline_row.get('source_path'),
        )
        return None
    legacy_row = archive_queue.claim_specific_pending(
        int(legacy_id), worker_id, db_path=db_path,
    )
    if legacy_row is None:
        # Wave 4 PR-F1 review fix #2 (PR #198): legacy row missing
        # or already-claimed. Release the pipeline_queue claim back
        # to ``pending`` AND clear ``claimed_by`` / ``claimed_at`` so
        # the row presents as a clean ``pending`` row to operators
        # and to ``recover_stale_claims_pipeline``. (Leaving the
        # claim metadata stale on a ``pending`` row would look like
        # a stuck active claim AND would NOT be picked up by
        # recovery — which filters on ``status='in_progress'``.)
        # The next worker iteration will re-attempt; since the
        # legacy row is missing, the same code path will fire
        # again — but the legacy reader (or stale-recovery in the
        # legacy queue) will eventually reconcile the gap.
        try:
            pqs.release_pipeline_claim(
                legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
                legacy_id=int(legacy_id),
                last_error=(
                    'PR-F1: archive_queue row not claimable '
                    '(deleted, already claimed, or status changed) '
                    '— pipeline claim released'
                ),
                db_path=db_path,
            )
        except Exception:  # noqa: BLE001
            pass
        logger.warning(
            "PR-F1 reader switch: archive_queue row id=%s not "
            "claimable (deleted, already claimed, or status changed) "
            "— released pipeline_queue claim and skipping",
            legacy_id,
        )
        return None
    # Wave 4 PR-F1 review fix #4 (PR #198): note that
    # ``pipeline_queue.attempts`` was already bumped by
    # ``claim_next_for_stage`` above, but ``archive_queue.attempts``
    # was NOT bumped by ``claim_specific_pending`` (the legacy claim
    # path historically does not increment attempts on claim). The
    # two counters intentionally drift on the success path — the
    # legacy ``attempts`` is bumped only on ``mark_failed``, while
    # the pipeline ``attempts`` reflects every claim. Operators
    # cross-comparing the two during the cutover window will see
    # this divergence; it is not a dual-write bug.
    adapted = _adapt_pipeline_row_to_legacy_shape(pipeline_row)
    # The adapter takes its expected_size/expected_mtime from the
    # pipeline payload, but the legacy row may carry fresher values
    # (release_claim refreshes them on stable-write gate failures).
    # Prefer the legacy row's values when present so the
    # process_one_claim stable-write gate sees the same data the
    # legacy claim path would have.
    if adapted is not None:
        if legacy_row.get('expected_size') is not None:
            adapted['expected_size'] = legacy_row.get('expected_size')
        if legacy_row.get('expected_mtime') is not None:
            adapted['expected_mtime'] = legacy_row.get('expected_mtime')
        # Authoritative legacy_row fields override the adapted
        # snapshot for diagnostic correctness (claimed_at/by are
        # what the DB actually persisted).
        adapted['claimed_at'] = legacy_row.get('claimed_at')
        adapted['claimed_by'] = legacy_row.get('claimed_by')
    return adapted


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

    # Wave 4 PR-E (issue #184 / #193): also recover stale claims in
    # the unified ``pipeline_queue``. Idempotent — returns 0 when
    # nothing to do (the common case during the dual-write window
    # because the pipeline_queue claim path isn't wired in production
    # yet). NEVER raises. Decoupled from the legacy recovery above so
    # one failing doesn't suppress the other.
    try:
        from services import pipeline_queue_service as pqs
        pq_released = pqs.recover_stale_claims_pipeline()
        if pq_released:
            logger.info(
                "Archive worker %s released %d stale pipeline_queue claims at startup",
                worker_id, pq_released,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "recover_stale_claims_pipeline failed at startup: %s", e,
        )

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
            # Issue #109 — adapt the trigger threshold to current SD
            # fullness BEFORE comparing. At 95%+ disk the same loadavg
            # of 4.0 represents far more SDIO contention than at 50%.
            _adaptive_threshold = _adaptive_load_threshold(
                load_pause_threshold, _disk_fullness_pct(archive_root),
            )
            if load1 > _adaptive_threshold:
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
                        "archive_worker: 1-min loadavg %.2f > %.2f "
                        "(adaptive; base=%.2f) — pausing %.0fs to "
                        "relieve SDIO/CPU contention",
                        load1, _adaptive_threshold, load_pause_threshold,
                        load_pause_seconds,
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
                    "archive_worker: 1-min loadavg %.2f back below %.2f "
                    "(adaptive; base=%.2f) — resuming archive drain",
                    load1, _adaptive_threshold, load_pause_threshold,
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
            # Wave 4 PR-F1 (issue #184): when the cutover flag is on,
            # claim from pipeline_queue and mirror to archive_queue.
            # Skip the shadow comparison — we ARE the pipeline reader
            # now, so comparing against the legacy reader is moot.
            # Default OFF preserves the legacy claim path (and its
            # PR-E shadow comparison) so a fresh deploy is a no-op.
            #
            # TODO(PR-F2 / future): consider an *inverse* shadow when
            # the flag is ON — peek the legacy reader's pick (via a
            # non-mutating SELECT against archive_queue, mirroring
            # ``peek_next_for_stage``) and compare against the
            # pipeline pick we just claimed. Same purpose as PR-E's
            # shadow but in the opposite direction, giving operators
            # confidence during the cutover window. Out of scope for
            # PR-F1 because (a) the single-worker invariant means a
            # divergence couldn't actually starve the legacy queue,
            # and (b) the legacy reader has no equivalent of
            # ``peek_top_n_paths_for_stage`` yet — would need a new
            # ``archive_queue.peek_next_for_worker`` helper. Track
            # via the relevant sub-PR (see issue body's Wave 4 plan).
            if _use_pipeline_reader_enabled():
                try:
                    row = _claim_via_pipeline_reader(worker_id, db_path)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "_claim_via_pipeline_reader raised: %s", e,
                    )
                    _set_state(last_error=f'claim (pipeline): {e!r}')
                    claim_failed = True
                    row = None
                if row is None and not claim_failed:
                    _set_state(last_drained_at=time.time())
            else:
                # Wave 4 PR-E (issue #184): shadow-mode peek at
                # ``pipeline_queue`` BEFORE the legacy claim so we can
                # compare the two readers' picks against the same queue
                # state. Pure observability — the result drives a log
                # line only; no behavioural change. Done before the
                # legacy claim because the dual-write fires on UPDATE,
                # which would move the pipeline_queue row to in_progress
                # and invalidate the comparison.
                #
                # We peek the top-N (not just top-1) so the documented
                # secondary-key divergence between archive_queue
                # (expected_mtime) and pipeline_queue (enqueued_at)
                # doesn't generate noisy WARNINGs on benign reorderings
                # within a priority band — see ``_shadow_compare_picks``
                # docstring for the rationale.
                shadow_candidates: Tuple[str, ...] = ()
                if _shadow_pipeline_queue_enabled():
                    try:
                        from services import pipeline_queue_service as pqs
                        shadow_candidates = pqs.peek_top_n_paths_for_stage(
                            stage='archive_pending',
                            limit=_SHADOW_PEEK_CANDIDATE_COUNT,
                        )
                    except Exception as e:  # noqa: BLE001
                        # Shadow path must NEVER affect the worker. Log
                        # at DEBUG so a misconfigured DB path doesn't
                        # spam WARNING on every iteration.
                        logger.debug(
                            "shadow peek_top_n_paths_for_stage failed: "
                            "%s", e,
                        )

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

                # Wave 4 PR-E: compare the legacy pick against the
                # pipeline_queue top-N. Agreement = legacy_path appears
                # in the candidate set (or both empty). Disagreement =
                # legacy picked a path absent from the top-N — a real
                # dual-write gap.
                if shadow_candidates or row is not None or \
                        _shadow_pipeline_queue_enabled():
                    _shadow_compare_picks(
                        legacy_path=row.get('source_path') if row else None,
                        pipeline_candidates=shadow_candidates,
                    )

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
