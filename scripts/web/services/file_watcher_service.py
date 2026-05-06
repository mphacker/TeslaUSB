"""
TeslaUSB File Watcher Service.

Monitors the USB RO mount and ArchivedClips directory for new video files.
On detection: queues files for geo-indexing and cloud sync.

Uses inotify when available (real-time, low CPU), falls back to polling
(scan every 5 minutes). Designed for Pi Zero 2 W (512MB RAM).

Lifecycle: ``start_watcher`` / ``stop_watcher`` / ``restart_watcher`` are
all safe to call from the Flask request thread or the mode-switch handler.
``stop_watcher`` joins the worker thread so callers can rely on no further
callbacks firing afterwards. A monotonic ``_watcher_generation`` counter is
incremented on every stop, and the worker thread captures its generation
at startup; any callback the thread tries to emit after the counter has
moved is silently dropped, so a slow shutdown (or a mode switch racing a
file-create event) cannot enqueue stale paths into a freshly-restarted
watcher.
"""

import logging
import os
import struct
import threading
import time
from typing import Callable, List, Optional, Set

logger = logging.getLogger(__name__)

# Polling interval when inotify is not available or mount changes
_POLL_INTERVAL_SECONDS = 300  # 5 minutes

# Minimum file age before processing (seconds) — files younger may be
# actively written by Tesla
_MIN_FILE_AGE_SECONDS = 60

# Maximum known_files set size before pruning (prevents unbounded memory growth)
_MAX_KNOWN_FILES = 10000

# How often to prune the known_files set (every N scans)
_PRUNE_EVERY_N_SCANS = 12  # ~1 hour at 5-min polling

# How long ``stop_watcher`` waits for the thread to exit before giving up.
_STOP_JOIN_TIMEOUT = 10.0

# How long ``restart_watcher`` waits for at least one watch path to become
# a directory again after a mount cycle.
_RESTART_MOUNT_WAIT = 30.0

# ---------------------------------------------------------------------------
# inotify constants (Linux). Defined here so the module imports cleanly on
# non-Linux dev hosts where ``ctypes.util.find_library('c')`` is missing.
# ---------------------------------------------------------------------------

_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_MOVED_FROM = 0x00000040
_IN_MOVED_TO = 0x00000080
_IN_CLOSE_WRITE = 0x00000008
_IN_ISDIR = 0x40000000  # Set on events for directories
_IN_NONBLOCK = 0o4000  # for inotify_init1
_INOTIFY_EVENT_HEADER = struct.calcsize('iIII')

# ---------------------------------------------------------------------------
# Background Thread State
# ---------------------------------------------------------------------------

_watcher_thread: Optional[threading.Thread] = None
_watcher_lock = threading.Lock()
_watcher_stop = threading.Event()

# Bumped every time the watcher is stopped or restarted. The worker captures
# its starting generation; callbacks only fire if the captured value still
# matches. This prevents stale events leaking past a restart.
_watcher_generation: int = 0

_status = {
    "running": False,
    "mode": "idle",  # "inotify" | "polling" | "idle"
    "last_scan": None,
    "files_detected": 0,
    "files_deleted": 0,
    "watch_paths": [],
}

# Callbacks registered by other services
_on_new_file_callbacks: List[Callable] = []
_on_deleted_file_callbacks: List[Callable] = []


def register_callback(callback: Callable):
    """Register a callback to be called when new video files are detected.

    Callback signature: callback(file_paths: List[str])
    """
    _on_new_file_callbacks.append(callback)


def register_delete_callback(callback: Callable):
    """Register a callback for video files that have been removed.

    Callback signature: callback(file_paths: List[str])
    Used by the indexer to purge stale rows when Tesla rotates the
    RecentClips circular buffer or the user deletes archived clips.
    """
    _on_deleted_file_callbacks.append(callback)


def get_watcher_status() -> dict:
    """Return current watcher status."""
    return dict(_status)


def start_watcher(watch_paths: List[str]) -> bool:
    """Start the file watcher daemon thread.

    Args:
        watch_paths: List of directory paths to monitor for new .mp4 files.

    Returns:
        True if started, False if already running.
    """
    global _watcher_thread, _watcher_generation

    with _watcher_lock:
        if _watcher_thread and _watcher_thread.is_alive():
            logger.debug("Watcher already running")
            return False

        _watcher_stop.clear()
        _status["watch_paths"] = [p for p in watch_paths if os.path.isdir(p)]

        if not _status["watch_paths"]:
            logger.warning("No valid watch paths — watcher not started")
            return False

        # Capture the current generation — the worker uses this to decide
        # whether its callbacks are still relevant. Started threads see the
        # value at start time; later increments invalidate them.
        my_generation = _watcher_generation

        _watcher_thread = threading.Thread(
            target=_watcher_loop,
            args=(my_generation,),
            daemon=True,
            name="file-watcher",
        )
        _watcher_thread.start()
        _status["running"] = True
        logger.info("File watcher started for: %s", _status["watch_paths"])
        return True


def stop_watcher(timeout: float = _STOP_JOIN_TIMEOUT) -> bool:
    """Stop the file watcher and wait for the thread to exit.

    Returns True if the thread exited cleanly within ``timeout``, False if
    it timed out. The thread is daemonic so it cannot block process exit;
    callers may still proceed on a False return, but should be aware that
    a callback could fire one more time before the thread notices the
    generation bump.
    """
    global _watcher_thread, _watcher_generation

    with _watcher_lock:
        thread = _watcher_thread
        # Bump the generation FIRST so any callback already in flight is
        # dropped — even if the thread is wedged inside a slow filesystem
        # call right now.
        _watcher_generation += 1
        _watcher_stop.set()

    clean = True
    if thread and thread.is_alive():
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning(
                "File watcher thread did not exit within %.1fs "
                "(daemon — will be killed at process exit)",
                timeout,
            )
            clean = False

    with _watcher_lock:
        # Only clear the thread reference if WE own this stop. A racing
        # restart_watcher() may have already started a new thread; don't
        # blow away its handle.
        if _watcher_thread is thread:
            _watcher_thread = None
        _status["running"] = False
        _status["mode"] = "idle"
    logger.info("File watcher stopped (clean=%s)", clean)
    return clean


def restart_watcher(watch_paths: List[str],
                    mount_wait_seconds: float = _RESTART_MOUNT_WAIT) -> bool:
    """Stop, wait for at least one watch path to become available, and restart.

    Used after a mode switch (present↔edit) where the RO/RW mounts at
    ``/mnt/gadget/part1*`` transiently disappear during the swap. The mount
    wait prevents starting a watcher with zero valid paths if the script
    hasn't finished re-mounting yet.

    Returns True if the new watcher started; False otherwise.
    """
    stop_watcher()
    deadline = time.monotonic() + mount_wait_seconds
    while time.monotonic() < deadline:
        if any(os.path.isdir(p) for p in watch_paths):
            break
        time.sleep(0.5)
    return start_watcher(watch_paths)


def _notify_callbacks(new_files: List[str], my_generation: int):
    """Notify all registered new-file callbacks if our generation is current."""
    if not new_files:
        return
    if my_generation != _watcher_generation:
        # Someone called stop_watcher() while we were assembling this batch.
        # Drop it so we don't enqueue paths into a freshly-restarted worker.
        logger.debug("Dropping %d new-file callbacks (stale generation)",
                     len(new_files))
        return
    _status["files_detected"] += len(new_files)
    for cb in _on_new_file_callbacks:
        try:
            cb(new_files)
        except Exception as e:
            logger.error("Watcher new-file callback error: %s", e)


def _notify_delete_callbacks(deleted_files: List[str], my_generation: int):
    """Notify all registered delete callbacks if our generation is current."""
    if not deleted_files:
        return
    if my_generation != _watcher_generation:
        logger.debug("Dropping %d delete callbacks (stale generation)",
                     len(deleted_files))
        return
    _status["files_deleted"] += len(deleted_files)
    for cb in _on_deleted_file_callbacks:
        try:
            cb(deleted_files)
        except Exception as e:
            logger.error("Watcher delete callback error: %s", e)


def _scan_for_new_files(paths: List[str], known_files: Set[str]) -> List[str]:
    """Scan directories for new .mp4 files not in known_files set.

    Uses os.scandir for memory efficiency (generator-based).
    """
    new_files = []
    now = time.time()

    for base_path in paths:
        if not os.path.isdir(base_path):
            continue
        try:
            for entry in os.scandir(base_path):
                if entry.is_dir(follow_symlinks=False):
                    # Scan subdirectories (TeslaCam has SentryClips/event_name/ structure)
                    try:
                        for sub in os.scandir(entry.path):
                            if sub.is_dir(follow_symlinks=False):
                                # Event folders (e.g., SentryClips/2026-01-01_12-00-00/)
                                try:
                                    for vid in os.scandir(sub.path):
                                        if (vid.name.lower().endswith('.mp4')
                                                and vid.path not in known_files):
                                            stat = vid.stat(follow_symlinks=False)
                                            if (now - stat.st_mtime) >= _MIN_FILE_AGE_SECONDS:
                                                new_files.append(vid.path)
                                                known_files.add(vid.path)
                                except PermissionError:
                                    pass
                            elif (sub.name.lower().endswith('.mp4')
                                    and sub.path not in known_files):
                                # Flat files in subfolder (e.g., RecentClips/*.mp4)
                                stat = sub.stat(follow_symlinks=False)
                                if (now - stat.st_mtime) >= _MIN_FILE_AGE_SECONDS:
                                    new_files.append(sub.path)
                                    known_files.add(sub.path)
                    except PermissionError:
                        pass
                elif (entry.name.lower().endswith('.mp4')
                        and entry.path not in known_files):
                    # Root-level mp4 (ArchivedClips pattern)
                    stat = entry.stat(follow_symlinks=False)
                    if (now - stat.st_mtime) >= _MIN_FILE_AGE_SECONDS:
                        new_files.append(entry.path)
                        known_files.add(entry.path)
        except PermissionError:
            pass
        except OSError as e:
            logger.warning("Scan error for %s: %s", base_path, e)

    return new_files


def _parse_inotify_events(data: bytes, wd_map: dict):
    """Yield ``(full_path, mask)`` tuples from a buffer of ``inotify_event``
    structs.

    Tolerates partial reads (returns once the buffer is exhausted) and
    unknown watch descriptors (skipped — likely a watch we removed).
    """
    offset = 0
    n = len(data)
    while offset + _INOTIFY_EVENT_HEADER <= n:
        wd, mask, _cookie, name_len = struct.unpack_from(
            'iIII', data, offset
        )
        offset += _INOTIFY_EVENT_HEADER
        name_bytes = data[offset:offset + name_len]
        offset += name_len
        dir_path = wd_map.get(wd)
        if not dir_path:
            continue
        # Names are null-padded to align the next struct; strip the padding.
        name = name_bytes.split(b'\0', 1)[0].decode('utf-8', errors='replace')
        if not name:
            # Directory-level event with no filename — ignored (we track
            # files individually).
            continue
        yield (os.path.join(dir_path, name), mask)


def _try_inotify(paths: List[str], known_files: Set[str],
                 my_generation: int) -> bool:
    """Try to use inotify for real-time monitoring. Returns False if unavailable."""
    try:
        import ctypes
        import ctypes.util

        libc_name = ctypes.util.find_library('c')
        if not libc_name:
            return False
        libc = ctypes.CDLL(libc_name, use_errno=True)

        watch_mask = (
            _IN_CREATE | _IN_MOVED_TO | _IN_CLOSE_WRITE
            | _IN_DELETE | _IN_MOVED_FROM
        )

        fd = libc.inotify_init1(_IN_NONBLOCK)
        if fd < 0:
            return False

        wd_map = {}
        for path in paths:
            if not os.path.isdir(path):
                continue
            wd = libc.inotify_add_watch(fd, path.encode(), watch_mask)
            if wd >= 0:
                wd_map[wd] = path
            else:
                logger.debug("inotify_add_watch failed for %s (errno=%d)",
                             path, ctypes.get_errno())
            # Also watch subdirectories (one level)
            try:
                for entry in os.scandir(path):
                    if entry.is_dir(follow_symlinks=False):
                        wd2 = libc.inotify_add_watch(
                            fd, entry.path.encode(), watch_mask,
                        )
                        if wd2 >= 0:
                            wd_map[wd2] = entry.path
            except (PermissionError, OSError):
                pass

        if not wd_map:
            os.close(fd)
            return False

        _status["mode"] = "inotify"
        logger.info("inotify watching %d directories (mask=create/move/close/"
                    "delete)", len(wd_map))

        import select as sel
        buf_size = 4096

        try:
            while not _watcher_stop.is_set():
                # Wait up to 30 seconds for events, then do a periodic scan
                ready, _, _ = sel.select([fd], [], [], 30.0)

                if _watcher_stop.is_set():
                    break

                deletions: List[str] = []
                if ready:
                    try:
                        data = os.read(fd, buf_size)
                    except OSError:
                        break
                    # Parse to extract delete events; create/move events are
                    # handled by the rescan below (which respects the
                    # _MIN_FILE_AGE_SECONDS guard so we don't grab files
                    # Tesla is still writing). We also catch new subdir
                    # creations here so newly-created event folders
                    # under SavedClips/SentryClips get their own watches
                    # for real-time delete detection.
                    for full_path, mask in _parse_inotify_events(data, wd_map):
                        # Directory creation/move-in: add a watch so
                        # files appearing inside fire IN_CLOSE_WRITE/
                        # IN_DELETE in real time. We re-check is_dir
                        # because the event ordering can be ambiguous.
                        if (mask & _IN_ISDIR
                                and mask & (_IN_CREATE | _IN_MOVED_TO)):
                            try:
                                if os.path.isdir(full_path):
                                    new_wd = libc.inotify_add_watch(
                                        fd, full_path.encode(),
                                        watch_mask,
                                    )
                                    if new_wd >= 0:
                                        wd_map[new_wd] = full_path
                                        logger.debug(
                                            "Added inotify watch for "
                                            "new subdir %s", full_path,
                                        )
                            except OSError:
                                pass
                            continue
                        if not full_path.lower().endswith('.mp4'):
                            continue
                        if mask & (_IN_DELETE | _IN_MOVED_FROM):
                            deletions.append(full_path)
                            known_files.discard(full_path)

                if deletions:
                    logger.info("Detected %d file deletion(s)", len(deletions))
                    _notify_delete_callbacks(deletions, my_generation)

                # Periodic scan (catches files inotify missed and new subdirs)
                new_files = _scan_for_new_files(paths, known_files)
                if new_files:
                    logger.info("Detected %d new files", len(new_files))
                    _notify_callbacks(new_files, my_generation)
                _status["last_scan"] = time.strftime("%Y-%m-%d %H:%M:%S")
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        return True

    except (ImportError, OSError, AttributeError):
        return False


def _watcher_loop(my_generation: int):
    """Main watcher loop — tries inotify, falls back to polling."""
    paths = _status["watch_paths"]
    known_files: Set[str] = set()
    scan_count = 0

    # Initial scan to build known file set (don't trigger callbacks for
    # existing files — only newly-arrived ones).
    _scan_for_new_files(paths, known_files)
    _status["last_scan"] = time.strftime("%Y-%m-%d %H:%M:%S")
    logger.info("Initial scan: %d existing files tracked", len(known_files))

    # Try inotify first (blocks until stop or error)
    if _try_inotify(paths, known_files, my_generation):
        _status["running"] = False
        return

    # Fallback: polling mode
    _status["mode"] = "polling"
    logger.info("Falling back to polling mode (every %ds)", _POLL_INTERVAL_SECONDS)

    while not _watcher_stop.is_set():
        _watcher_stop.wait(_POLL_INTERVAL_SECONDS)
        if _watcher_stop.is_set():
            break

        new_files = _scan_for_new_files(paths, known_files)
        if new_files:
            logger.info("Polling detected %d new files", len(new_files))
            _notify_callbacks(new_files, my_generation)

        # Polling-mode delete detection: any file we knew about that no
        # longer exists is a deletion. Cheap once known_files is bounded.
        deletions = [p for p in known_files if not os.path.isfile(p)]
        if deletions:
            logger.info("Polling detected %d file deletion(s)", len(deletions))
            for p in deletions:
                known_files.discard(p)
            _notify_delete_callbacks(deletions, my_generation)

        _status["last_scan"] = time.strftime("%Y-%m-%d %H:%M:%S")

        # Periodically prune known_files to prevent unbounded memory growth.
        # The delete loop above already drops missing files; this catches
        # the case where the set grows too fast for the prune interval.
        scan_count += 1
        if scan_count >= _PRUNE_EVERY_N_SCANS or len(known_files) > _MAX_KNOWN_FILES:
            before = len(known_files)
            known_files = {f for f in known_files if os.path.isfile(f)}
            pruned = before - len(known_files)
            if pruned > 0:
                logger.info("Pruned %d stale entries from known_files (now %d)",
                            pruned, len(known_files))
            scan_count = 0

    _status["running"] = False
