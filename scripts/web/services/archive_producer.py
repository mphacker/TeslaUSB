"""TeslaUSB Archive Queue Producer — Phase 2a (issue #76).

Single daemon thread that periodically walks the TeslaCam RO mount and
calls :func:`services.archive_queue.enqueue_many_for_archive` for every
``.mp4`` it finds under ``RecentClips/``, ``SentryClips/`` (event
subfolders), and ``SavedClips/`` (event subfolders). Idempotent —
``INSERT OR IGNORE`` on the queue's UNIQUE constraint makes re-walks
cheap.

This is the "belt and suspenders" half of the Phase 2a producer set. The
other half is the inotify file watcher (``file_watcher_service``), which
fires individual paths in real time. The producer thread covers:

1. **Boot catch-up** — anything Tesla wrote while ``gadget_web`` was
   down (crash, restart, or normal boot lag) gets enqueued on the
   first iteration.
2. **Inotify gaps** — kernel buffer overflows, transient mount events,
   or simply missed events (the watcher's mp4 callback uses a 60-s
   age gate; the rescan picks up files Tesla finished writing > 60 s
   ago).
3. **VFS cache drift** — when Tesla writes via the gadget block layer,
   the Pi's view of the directory is occasionally stale until the
   next ``readdir``. The periodic rescan forces that ``readdir``.

**Phase 2a is producer-only.** Rows accumulate in ``archive_queue`` but
no worker drains them until Phase 2b. The producer thread therefore
performs zero copy work, no network I/O, and never touches the gadget
or any mount — pure read-side observer.

Public API:

* :func:`start_producer(teslacam_root, db_path, *, rescan_interval_seconds, boot_catchup_enabled)` — start the thread (idempotent).
* :func:`stop_producer(timeout)` — signal stop and join. Safe across mode switches.
* :func:`get_producer_status()` — small dict for the observability stub.
* :func:`run_boot_catchup_once(teslacam_root, db_path)` — synchronous
  helper exposed for tests; never call from the request thread.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Dict, Iterable, List, Optional

from services import archive_queue

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables (kept module-level so tests can monkeypatch)
# ---------------------------------------------------------------------------

# Subdirectories of TeslaCam that we walk on every scan. The order is
# the priority order — RecentClips first because those are the most
# time-sensitive.
_WATCH_SUBDIRS = ('RecentClips', 'SentryClips', 'SavedClips')

# Default rescan interval (seconds). Overridable via the
# ``rescan_interval_seconds`` arg to :func:`start_producer` (which the
# Flask app pulls from ``config.yaml``).
_DEFAULT_RESCAN_INTERVAL = 60.0


# ---------------------------------------------------------------------------
# Module state — every read/write through ``_state_lock``
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_state: Dict = {
    'running': False,
    'teslacam_root': None,
    'db_path': None,
    'rescan_interval_seconds': _DEFAULT_RESCAN_INTERVAL,
    'boot_catchup_enabled': True,
    'iterations': 0,
    'last_scan_at': None,
    'last_enqueued': 0,
    'last_seen': 0,
    'last_error': None,
    'started_at': None,
}


def _is_running() -> bool:
    with _state_lock:
        t = _thread
    return t is not None and t.is_alive()


# ---------------------------------------------------------------------------
# Directory walking
# ---------------------------------------------------------------------------

def _iter_archive_candidates(teslacam_root: str) -> List[str]:
    """Return every ``.mp4`` under the watched subdirectories.

    Walks one level into ``RecentClips`` (flat files) and two levels
    into ``SentryClips`` / ``SavedClips`` (event-folder per recording).
    Uses ``os.scandir`` for memory efficiency.

    Permission errors and missing subdirectories are silently skipped —
    Phase 2a runs against a possibly-unmounted RO bind so any of the
    three subdirs can transiently be absent. Returning a partial list
    is correct: the next iteration (60 s later) will pick them up.

    Returns absolute paths in stable insertion order so logs don't
    shuffle between scans.
    """
    out: List[str] = []
    if not teslacam_root or not os.path.isdir(teslacam_root):
        return out

    for sub in _WATCH_SUBDIRS:
        sub_path = os.path.join(teslacam_root, sub)
        if not os.path.isdir(sub_path):
            continue
        try:
            entries = list(os.scandir(sub_path))
        except (PermissionError, OSError):
            continue
        for entry in entries:
            try:
                if entry.is_file(follow_symlinks=False):
                    if entry.name.lower().endswith('.mp4'):
                        out.append(entry.path)
                elif entry.is_dir(follow_symlinks=False):
                    # Event subfolder — walk one more level for clip files.
                    try:
                        for clip in os.scandir(entry.path):
                            if (clip.is_file(follow_symlinks=False)
                                    and clip.name.lower().endswith('.mp4')):
                                out.append(clip.path)
                    except (PermissionError, OSError):
                        continue
            except OSError:
                # entry.is_file()/is_dir() can race a delete; skip and
                # move on. Next scan will see the new state.
                continue
    return out


def _scan_once(teslacam_root: str, db_path: str) -> Dict[str, int]:
    """Run one scan iteration. Returns ``{seen, enqueued}``.

    Logs only when something was newly enqueued (avoid log spam from
    the steady-state every-60-s rescan).
    """
    seen = _iter_archive_candidates(teslacam_root)
    if not seen:
        return {'seen': 0, 'enqueued': 0}
    enqueued = archive_queue.enqueue_many_for_archive(seen, db_path=db_path)
    if enqueued > 0:
        logger.info(
            "archive_producer: scan enqueued %d new clip(s) (saw %d total)",
            enqueued, len(seen),
        )
    return {'seen': len(seen), 'enqueued': enqueued}


def run_boot_catchup_once(teslacam_root: str,
                          db_path: Optional[str] = None) -> Dict[str, int]:
    """Synchronous one-shot scan. Exposed for tests and direct callers.

    Most callers should use :func:`start_producer` and let the thread
    handle both the boot catch-up and the periodic rescans. This
    helper exists so unit tests can drive a single scan without
    spinning up a thread.
    """
    return _scan_once(teslacam_root, db_path or '')


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def start_producer(teslacam_root: str,
                   db_path: Optional[str] = None,
                   *,
                   rescan_interval_seconds: float = _DEFAULT_RESCAN_INTERVAL,
                   boot_catchup_enabled: bool = True,
                   boot_scan_defer_seconds: float = 0.0) -> bool:
    """Start the producer thread. Idempotent.

    Returns True if a new thread was started, False if one was already
    running.

    Args:
        teslacam_root: Absolute path to the TeslaCam RO mount root
            (typically ``/mnt/gadget/part1-ro/TeslaCam``).
        db_path: Override for the queue DB path. ``None`` resolves
            via :data:`config.MAPPING_DB_PATH` inside the queue module.
        rescan_interval_seconds: Seconds between successive scans.
            The default (60 s) matches the issue spec.
        boot_catchup_enabled: When True (default) the first iteration
            runs (after ``boot_scan_defer_seconds``) on startup. When
            False the thread waits ``rescan_interval_seconds`` before
            its first scan — useful for tests that want to exercise
            just the periodic path.
        boot_scan_defer_seconds: When >0 and ``boot_catchup_enabled``,
            wait this many seconds before the first scan. The default
            (0) preserves the original immediate-scan behavior; the
            web app passes a non-zero value (typically 30 s) so the
            producer's directory walk doesn't pile onto the post-start
            initialization storm that previously triggered hardware
            watchdog reboots on the Pi Zero 2 W.
    """
    global _thread
    with _state_lock:
        if _thread is not None and _thread.is_alive():
            logger.debug("archive_producer.start_producer: already running")
            return False
        _stop_event.clear()
        _state['running'] = True
        _state['teslacam_root'] = teslacam_root
        _state['db_path'] = db_path
        _state['rescan_interval_seconds'] = float(rescan_interval_seconds)
        _state['boot_catchup_enabled'] = bool(boot_catchup_enabled)
        _state['boot_scan_defer_seconds'] = float(boot_scan_defer_seconds)
        _state['iterations'] = 0
        _state['last_scan_at'] = None
        _state['last_enqueued'] = 0
        _state['last_seen'] = 0
        _state['last_error'] = None
        _state['started_at'] = time.time()
        _thread = threading.Thread(
            target=_run_loop,
            args=(teslacam_root, db_path,
                  float(rescan_interval_seconds),
                  bool(boot_catchup_enabled),
                  float(boot_scan_defer_seconds)),
            name='archive-producer',
            daemon=True,
        )
        # Start inside the lock so a concurrent stop_producer cannot
        # observe _thread before .start() and call join() on an
        # unstarted thread (RuntimeError). Phase 2b will add more
        # lifecycle entry points (admin endpoint, mode-switch hook),
        # so making the start atomic now keeps the contract simple.
        _thread.start()
    logger.info(
        "archive_producer started (root=%s, interval=%.1fs, "
        "boot_catchup=%s, boot_defer=%.1fs)",
        teslacam_root, rescan_interval_seconds,
        boot_catchup_enabled, boot_scan_defer_seconds,
    )
    return True


def stop_producer(timeout: float = 10.0) -> bool:
    """Signal the producer to stop and wait for it to exit.

    Returns True if the thread exited cleanly (or wasn't running),
    False on timeout.
    """
    global _thread
    with _state_lock:
        thread = _thread
    if thread is None:
        return True
    _stop_event.set()
    thread.join(timeout=timeout)
    exited = not thread.is_alive()
    if exited:
        with _state_lock:
            if _thread is thread:
                _thread = None
            _state['running'] = False
        logger.info("archive_producer stopped cleanly")
    else:
        logger.warning(
            "archive_producer did not exit within %.1fs", timeout,
        )
    return exited


def get_producer_status() -> Dict:
    """Snapshot of producer state for the observability endpoint."""
    with _state_lock:
        snap = dict(_state)
    snap['running'] = _is_running()
    return snap


def _run_loop(teslacam_root: str, db_path: Optional[str],
              rescan_interval_seconds: float,
              boot_catchup_enabled: bool,
              boot_scan_defer_seconds: float = 0.0) -> None:
    """Producer thread body. Catches every exception so a single bad
    scan can't kill the thread.
    """
    if not boot_catchup_enabled:
        # Skip the immediate first-pass; wait the full interval first.
        if _stop_event.wait(rescan_interval_seconds):
            with _state_lock:
                _state['running'] = False
            return
    elif boot_scan_defer_seconds > 0:
        # Boot catch-up is enabled, but defer the first scan so the
        # producer's directory walk doesn't pile onto the post-start
        # initialization storm (file_watcher initial scan + worker
        # resuming a backlog drain). Without this defer, a single
        # service restart could spike SDIO contention enough to
        # starve the watchdog daemon and trigger a hardware reboot
        # on the Pi Zero 2 W (see copilot-instructions.md).
        if _stop_event.wait(boot_scan_defer_seconds):
            with _state_lock:
                _state['running'] = False
            return

    while not _stop_event.is_set():
        try:
            result = _scan_once(teslacam_root, db_path or '')
            with _state_lock:
                _state['iterations'] += 1
                _state['last_scan_at'] = time.time()
                _state['last_seen'] = int(result.get('seen', 0))
                _state['last_enqueued'] = int(result.get('enqueued', 0))
                _state['last_error'] = None
        except Exception as e:  # noqa: BLE001  -- never let producer die
            logger.exception("archive_producer scan iteration failed")
            with _state_lock:
                _state['last_error'] = str(e)

        if _stop_event.wait(rescan_interval_seconds):
            break

    with _state_lock:
        _state['running'] = False
