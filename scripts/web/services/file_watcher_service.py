"""
TeslaUSB File Watcher Service.

Monitors the USB RO mount and ArchivedClips directory for new video files.
On detection: queues files for geo-indexing and cloud sync.

Uses inotify when available (real-time, low CPU), falls back to polling
(scan every 5 minutes). Designed for Pi Zero 2 W (512MB RAM).
"""

import logging
import os
import threading
import time
from typing import Callable, List, Optional, Set

logger = logging.getLogger(__name__)

# Polling interval when inotify is not available or mount changes
_POLL_INTERVAL_SECONDS = 300  # 5 minutes

# Minimum file age before processing (seconds) — files younger may be
# actively written by Tesla
_MIN_FILE_AGE_SECONDS = 60

# ---------------------------------------------------------------------------
# Background Thread State
# ---------------------------------------------------------------------------

_watcher_thread: Optional[threading.Thread] = None
_watcher_lock = threading.Lock()
_watcher_stop = threading.Event()

_status = {
    "running": False,
    "mode": "idle",  # "inotify" | "polling" | "idle"
    "last_scan": None,
    "files_detected": 0,
    "watch_paths": [],
}

# Callbacks registered by other services
_on_new_file_callbacks: List[Callable] = []


def register_callback(callback: Callable):
    """Register a callback to be called when new video files are detected.

    Callback signature: callback(file_paths: List[str])
    """
    _on_new_file_callbacks.append(callback)


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
    global _watcher_thread

    with _watcher_lock:
        if _watcher_thread and _watcher_thread.is_alive():
            logger.debug("Watcher already running")
            return False

        _watcher_stop.clear()
        _status["watch_paths"] = [p for p in watch_paths if os.path.isdir(p)]

        if not _status["watch_paths"]:
            logger.warning("No valid watch paths — watcher not started")
            return False

        _watcher_thread = threading.Thread(
            target=_watcher_loop,
            daemon=True,
            name="file-watcher",
        )
        _watcher_thread.start()
        _status["running"] = True
        logger.info("File watcher started for: %s", _status["watch_paths"])
        return True


def stop_watcher():
    """Stop the file watcher."""
    _watcher_stop.set()
    _status["running"] = False
    _status["mode"] = "idle"
    logger.info("File watcher stopped")


def _notify_callbacks(new_files: List[str]):
    """Notify all registered callbacks about new files."""
    if not new_files:
        return
    _status["files_detected"] += len(new_files)
    for cb in _on_new_file_callbacks:
        try:
            cb(new_files)
        except Exception as e:
            logger.error("Watcher callback error: %s", e)


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


def _try_inotify(paths: List[str], known_files: Set[str]) -> bool:
    """Try to use inotify for real-time monitoring. Returns False if unavailable."""
    try:
        import ctypes
        import ctypes.util
        import struct

        libc_name = ctypes.util.find_library('c')
        if not libc_name:
            return False
        libc = ctypes.CDLL(libc_name, use_errno=True)

        IN_CREATE = 0x00000100
        IN_MOVED_TO = 0x00000080
        IN_CLOSE_WRITE = 0x00000008
        WATCH_MASK = IN_CREATE | IN_MOVED_TO | IN_CLOSE_WRITE
        EVENT_SIZE = struct.calcsize('iIII')

        fd = libc.inotify_init1(0o4000)  # IN_NONBLOCK
        if fd < 0:
            return False

        wd_map = {}
        for path in paths:
            if not os.path.isdir(path):
                continue
            wd = libc.inotify_add_watch(fd, path.encode(), WATCH_MASK)
            if wd >= 0:
                wd_map[wd] = path
            # Also watch subdirectories (one level)
            try:
                for entry in os.scandir(path):
                    if entry.is_dir(follow_symlinks=False):
                        wd2 = libc.inotify_add_watch(fd, entry.path.encode(), WATCH_MASK)
                        if wd2 >= 0:
                            wd_map[wd2] = entry.path
            except (PermissionError, OSError):
                pass

        if not wd_map:
            os.close(fd)
            return False

        _status["mode"] = "inotify"
        logger.info("inotify watching %d directories", len(wd_map))

        import select as sel
        buf_size = 4096

        while not _watcher_stop.is_set():
            # Wait up to 30 seconds for events, then do a periodic scan
            ready, _, _ = sel.select([fd], [], [], 30.0)

            if _watcher_stop.is_set():
                break

            if ready:
                try:
                    data = os.read(fd, buf_size)
                    # Process inotify events — just trigger a scan
                    # (parsing individual events is complex; a directory scan
                    # after any event is simpler and catches everything)
                except OSError:
                    break

            # Periodic scan (catches files inotify missed and new subdirectories)
            new_files = _scan_for_new_files(paths, known_files)
            if new_files:
                logger.info("Detected %d new files", len(new_files))
                _notify_callbacks(new_files)
            _status["last_scan"] = time.strftime("%Y-%m-%d %H:%M:%S")

        os.close(fd)
        return True

    except (ImportError, OSError, AttributeError):
        return False


def _watcher_loop():
    """Main watcher loop — tries inotify, falls back to polling."""
    paths = _status["watch_paths"]
    known_files: Set[str] = set()

    # Initial scan to build known file set (don't trigger callbacks for existing files)
    _scan_for_new_files(paths, known_files)
    _status["last_scan"] = time.strftime("%Y-%m-%d %H:%M:%S")
    logger.info("Initial scan: %d existing files tracked", len(known_files))

    # Try inotify first (blocks until stop or error)
    if _try_inotify(paths, known_files):
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
            _notify_callbacks(new_files)
        _status["last_scan"] = time.strftime("%Y-%m-%d %H:%M:%S")

    _status["running"] = False
