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

**Issue #184 Wave 2 — Phase B**: the SEI peek that decides whether a
RecentClips clip is stationary now runs **before** the row is enqueued
(via :func:`enqueue_with_peek`). Stationary clips never become a
queue row, so the worker's pick-claim-stat-skip-mark-row cycle (~6 SD
writes per row) collapses to zero SD writes for the parked-overnight
common case. The skipped count is tallied in an in-memory deque
(:func:`get_skipped_stationary_count`) so the Settings badge still has
a number to show without hitting the DB. The worker-side peek stays as
defense-in-depth for legacy rows already in the queue at upgrade time
and for tests that bypass the producer.

Public API:

* :func:`start_producer(teslacam_root, db_path, *, rescan_interval_seconds, boot_catchup_enabled)` — start the thread (idempotent).
* :func:`stop_producer(timeout)` — signal stop and join. Safe across mode switches.
* :func:`get_producer_status()` — small dict for the observability stub.
* :func:`run_boot_catchup_once(teslacam_root, db_path)` — synchronous
  helper exposed for tests; never call from the request thread.
* :func:`enqueue_with_peek(paths, db_path)` — enqueue a batch with the
  Phase B SEI peek applied to RecentClips candidates.
* :func:`get_skipped_stationary_count(hours)` — return the in-memory
  count of clips skipped at the producer in the last N hours.
* :func:`reset_skipped_stationary_tally()` — clear the in-memory deque
  (for tests and the "clear badge" admin action).
"""

from __future__ import annotations

import collections
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

# Issue #184 Wave 2 — Phase B: minimum file age before the producer
# will run an SEI peek on a RecentClips candidate. Mirrors the
# worker's stable-write gate so we never peek at a half-written file
# and misclassify it as stationary because GPS lock hasn't been
# written yet. Reads the same config value at call time so tests can
# monkeypatch.
_STABLE_WRITE_AGE_FALLBACK = 5.0


def _stable_write_age_seconds() -> float:
    """Return ``ARCHIVE_QUEUE_STABLE_WRITE_AGE_SECONDS`` from config.

    Falls back to ``_STABLE_WRITE_AGE_FALLBACK`` (5 s) if config isn't
    importable (unit-test environments without the full app).
    """
    try:
        from config import ARCHIVE_QUEUE_STABLE_WRITE_AGE_SECONDS
        return float(ARCHIVE_QUEUE_STABLE_WRITE_AGE_SECONDS)
    except Exception:  # noqa: BLE001
        return _STABLE_WRITE_AGE_FALLBACK


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
    'last_skipped_stationary': 0,
    'last_error': None,
    'started_at': None,
}


# Issue #184 Wave 2 — Phase B: in-memory rolling tally of skip-at-source
# decisions. Deque of monotonic timestamps (``time.time()``). We bound
# the maxlen at 10 000 so a runaway condition can't grow this without
# limit; in practice a parked Pi sees ~144 stationary clips per day
# (RecentClips writes one per minute), so 10 000 is ~70 days of headroom
# at full saturation. Resets on service restart — that's acceptable for
# a 24-hour badge and avoids the SD writes the legacy DB-backed counter
# was incurring.
_SKIPPED_TALLY_MAX = 10000
_skipped_tally_lock = threading.Lock()
_skipped_tally: 'collections.deque[float]' = collections.deque(
    maxlen=_SKIPPED_TALLY_MAX,
)


def _is_running() -> bool:
    with _state_lock:
        t = _thread
    return t is not None and t.is_alive()


def reset_skipped_stationary_tally() -> None:
    """Clear the in-memory skip tally. Test / admin helper."""
    with _skipped_tally_lock:
        _skipped_tally.clear()


def get_skipped_stationary_count(hours: int = 24) -> int:
    """Return how many clips were skipped at the producer in the last N hours.

    Issue #184 Wave 2 — Phase B: replaces the DB-backed
    ``count_skipped_stationary_recent`` for the post-Phase-B steady
    state. The Settings badge reads this PLUS the legacy DB count so
    historical rows from before the upgrade still show.

    Walks the deque from oldest, evicts anything older than the
    horizon, and returns the remaining size. O(N) in evicted entries
    but those entries are dropped permanently so amortized O(1) per
    skip.
    """
    if hours <= 0:
        return 0
    horizon = time.time() - (int(hours) * 3600)
    with _skipped_tally_lock:
        while _skipped_tally and _skipped_tally[0] < horizon:
            _skipped_tally.popleft()
        return len(_skipped_tally)


def _record_skip() -> None:
    """Append a skip timestamp to the in-memory tally."""
    with _skipped_tally_lock:
        _skipped_tally.append(time.time())


def _peek_clip_for_gps(source_path: str) -> Optional[bool]:
    """Producer-side wrapper around the worker's SEI peek.

    Mirrors :func:`archive_worker._clip_has_gps_signal` so the producer
    doesn't have to import the worker module at load time (one-way
    dependency: the worker may import producer-side helpers, never
    the reverse). Returns the same tri-state: True (has GPS), False
    (no GPS / skip), None (parse error / fall through).

    The peek tunables (``MAX_MESSAGES``, ``SAMPLE_RATE``,
    ``MAX_WALK_BYTES``) are pulled from the worker module at call time
    so a tuning change in one place takes effect in both. Lazy import
    keeps the producer's load-time footprint light and avoids a
    circular dependency (worker imports producer, never the other way
    at module load).
    """
    try:
        from services import sei_parser
    except Exception as e:  # noqa: BLE001
        logger.debug(
            "archive_producer._peek_clip_for_gps: sei_parser unavailable "
            "(%s); deferring to worker", e,
        )
        return None

    try:
        from services.archive_worker import (
            _SKIP_GPS_PEEK_MAX_MESSAGES as _MAX_MESSAGES,
            _SKIP_GPS_PEEK_SAMPLE_RATE as _SAMPLE_RATE,
            _SKIP_GPS_PEEK_MAX_WALK_BYTES as _MAX_WALK_BYTES,
        )
    except Exception:  # noqa: BLE001
        # Worker not importable in this context (e.g., a focused
        # producer-only test); fall back to the same defaults the
        # worker uses today. Keep these in sync with archive_worker.py.
        _MAX_MESSAGES = 90
        _SAMPLE_RATE = 30
        _MAX_WALK_BYTES = 2 * 1024 * 1024

    scanned = 0
    try:
        for msg in sei_parser.extract_sei_messages(
                source_path,
                sample_rate=_SAMPLE_RATE,
                max_walk_bytes=_MAX_WALK_BYTES):
            scanned += 1
            if msg.has_gps:
                return True
            if scanned >= _MAX_MESSAGES:
                break
        return False
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug(
            "archive_producer._peek_clip_for_gps: peek failed for %s "
            "(%s); deferring to worker", source_path, e,
        )
        return None


def enqueue_with_peek(paths: Iterable[str],
                      db_path: Optional[str] = None) -> Dict[str, int]:
    """Enqueue ``paths`` into ``archive_queue`` with the Phase B SEI peek.

    For each path:

    * If the path is **not** a RecentClips candidate (i.e., it's a
      Sentry/Saved event clip), enqueue it immediately. Event clips
      bypass the SEI peek.
    * If the path **is** a RecentClips candidate, ``stat()`` it. If
      the file is younger than ``_stable_write_age_seconds()`` (Tesla
      may still be writing it), enqueue immediately and let the
      worker's stable-write gate handle freshness — a fresh file
      could legitimately be missing GPS just because Tesla hasn't
      written the lock-acquired SEI yet. If the file is old enough,
      run the SEI peek. ``False`` means stationary → record the skip
      in the in-memory tally and DO NOT enqueue. ``True`` or ``None``
      → enqueue normally.

    Returns ``{enqueued, skipped_stationary, considered}`` so callers
    can update their own counters.
    """
    pending_enqueue: List[str] = []
    skipped = 0
    considered = 0
    stable_age = _stable_write_age_seconds()
    for raw in paths:
        if not raw:
            continue
        considered += 1
        priority = archive_queue.infer_priority(raw)
        if priority != archive_queue.PRIORITY_RECENT_CLIPS:
            pending_enqueue.append(raw)
            continue
        # RecentClips — apply the freshness gate, then peek.
        try:
            mtime = os.path.getmtime(raw)
        except OSError:
            # Source vanished between watcher fire and our stat;
            # silently drop — next scan will not see it either.
            continue
        if (time.time() - mtime) < stable_age:
            # Too fresh — defer to the worker so we never misclassify
            # a half-written clip as stationary.
            pending_enqueue.append(raw)
            continue
        verdict = _peek_clip_for_gps(raw)
        if verdict is False:
            _record_skip()
            skipped += 1
            logger.debug(
                "archive_producer: skipped stationary RecentClips at "
                "source: %s", raw,
            )
            continue
        # True or None — enqueue.
        pending_enqueue.append(raw)

    enqueued = 0
    if pending_enqueue:
        try:
            enqueued = archive_queue.enqueue_many_for_archive(
                pending_enqueue, db_path=db_path,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "archive_producer.enqueue_with_peek: "
                "enqueue_many_for_archive failed: %s", e,
            )
    return {
        'enqueued': enqueued,
        'skipped_stationary': skipped,
        'considered': considered,
    }


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
    """Run one scan iteration. Returns ``{seen, enqueued, skipped_stationary}``.

    Logs only when something was newly enqueued or skipped (avoid log
    spam from the steady-state every-60-s rescan).

    Issue #184 Wave 2 — Phase B: routes the batch through
    :func:`enqueue_with_peek` so RecentClips clips with no GPS-bearing
    SEI never become a queue row.
    """
    seen = _iter_archive_candidates(teslacam_root)
    if not seen:
        return {'seen': 0, 'enqueued': 0, 'skipped_stationary': 0}
    result = enqueue_with_peek(seen, db_path=db_path)
    enqueued = int(result.get('enqueued', 0))
    skipped = int(result.get('skipped_stationary', 0))
    if enqueued > 0 or skipped > 0:
        logger.info(
            "archive_producer: scan enqueued=%d, skipped_stationary=%d "
            "(saw %d total)", enqueued, skipped, len(seen),
        )
    return {
        'seen': len(seen),
        'enqueued': enqueued,
        'skipped_stationary': skipped,
    }


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
                _state['last_skipped_stationary'] = int(
                    result.get('skipped_stationary', 0)
                )
                _state['last_error'] = None
        except Exception as e:  # noqa: BLE001  -- never let producer die
            logger.exception("archive_producer scan iteration failed")
            with _state_lock:
                _state['last_error'] = str(e)

        if _stop_event.wait(rescan_interval_seconds):
            break

    with _state_lock:
        _state['running'] = False
