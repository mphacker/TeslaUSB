"""
TeslaUSB RecentClips Archive Service.

Copies RecentClips from the read-only USB mount to the Pi's SD card
before Tesla's 1-hour circular buffer deletes them. Zero USB disruption —
the gadget stays connected and Tesla continues recording.

Designed for Pi Zero 2 W (512 MB RAM): copies one file at a time using
buffered 64 KB I/O, with generator-based scanning and memory pressure
monitoring between files.
"""

import logging
import os
import shutil
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Generator, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (lazy-loaded from config.py)
# ---------------------------------------------------------------------------

from config import (
    ARCHIVE_DIR,
    ARCHIVE_ENABLED,
    ARCHIVE_INTERVAL_MINUTES,
    ARCHIVE_RETENTION_DAYS,
    ARCHIVE_MIN_FREE_SPACE_GB,
    ARCHIVE_MAX_SIZE_GB,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum file age before copying (seconds). Files younger than this may
# still be actively written by Tesla.
_MIN_FILE_AGE_SECONDS = 300  # 5 minutes

# Buffered copy chunk size — keeps memory usage predictable on Pi Zero 2 W
_COPY_CHUNK_SIZE = 65536  # 64 KB

# I/O throttle: max bytes per second during copy. The archive reads from
# the same SD card that the USB gadget uses for Tesla. Copying too fast
# starves the gadget (endpoint stalls) and can trigger a watchdog reboot.
# 2 MB/s leaves ample bandwidth for the gadget (~20-30 MB/s SD card).
_MAX_COPY_BYTES_PER_SEC = 2 * 1024 * 1024  # 2 MB/s

# Minimum available RAM+swap (bytes) to continue archiving
_MIN_MEMORY_BYTES = 50 * 1024 * 1024  # 50 MB

# Pause between file copies to let the system breathe
_INTER_FILE_SLEEP = 2.0  # 2 seconds

# ---------------------------------------------------------------------------
# Background Thread State
# ---------------------------------------------------------------------------

_archive_thread: Optional[threading.Thread] = None
_archive_lock = threading.Lock()
_archive_cancel = threading.Event()

_status: Dict = {
    "running": False,
    "progress": "",
    "files_total": 0,
    "files_done": 0,
    "bytes_copied": 0,
    "current_file": "",
    "started_at": None,
    "last_run": None,
    "last_run_files": 0,
    "archive_size_mb": 0,
    "error": None,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_archive_status() -> dict:
    """Return current archive status (safe to call from any thread)."""
    return dict(_status)


def get_archive_dir() -> str:
    """Return the archive directory path."""
    return ARCHIVE_DIR


def start_archive_timer() -> None:
    """Start the periodic background archive thread.

    Safe to call multiple times — only one timer runs at a time.
    """
    if not ARCHIVE_ENABLED:
        logger.info("RecentClips archive is disabled in config")
        return

    global _archive_thread
    with _archive_lock:
        if _archive_thread and _archive_thread.is_alive():
            return
        _archive_cancel.clear()
        _archive_thread = threading.Thread(
            target=_archive_timer_loop,
            name="archive-timer",
            daemon=True,
        )
        _archive_thread.start()
        logger.info("RecentClips archive timer started (every %d min)",
                     ARCHIVE_INTERVAL_MINUTES)


def stop_archive_timer() -> None:
    """Stop the background archive timer."""
    _archive_cancel.set()


def trigger_archive_now() -> bool:
    """Trigger a one-shot archive run (non-blocking).

    Returns True if an archive was started, False if one is already running
    or if archiving is disabled.
    """
    if not ARCHIVE_ENABLED:
        return False
    if _status.get("running"):
        return False

    t = threading.Thread(
        target=_run_archive,
        name="archive-oneshot",
        daemon=True,
    )
    t.start()
    return True


# ---------------------------------------------------------------------------
# Timer Loop
# ---------------------------------------------------------------------------


def _archive_timer_loop() -> None:
    """Periodically run archive + retention."""
    interval = ARCHIVE_INTERVAL_MINUTES * 60

    # Set lowest I/O priority so archive doesn't starve the USB gadget.
    # The gadget shares the same SD card I/O bus.
    try:
        import subprocess
        subprocess.run(
            ["ionice", "-c", "3", "-p", str(os.getpid())],
            timeout=5, capture_output=True,
        )
        logger.info("Archive thread set to idle I/O priority (ionice -c 3)")
    except Exception:
        pass  # ionice not available — rate limiting still protects us

    # Initial delay — let boot finish and other services settle
    for _ in range(120):  # 2 minutes
        if _archive_cancel.is_set():
            return
        time.sleep(1)

    while not _archive_cancel.is_set():
        try:
            _run_archive()
        except Exception:
            logger.exception("Archive run failed unexpectedly")

        # Sleep in 1-second ticks so cancel is responsive
        for _ in range(interval):
            if _archive_cancel.is_set():
                return
            time.sleep(1)


# ---------------------------------------------------------------------------
# Core Archive Logic
# ---------------------------------------------------------------------------


def _run_archive() -> None:
    """One complete archive run: discover → copy → retention."""
    global _status

    if _status.get("running"):
        return

    # Don't archive while cloud sync is running — both compete for SD card I/O
    try:
        from services.cloud_archive_service import get_sync_status
        if get_sync_status().get("running"):
            logger.info("Archive skipped: cloud sync is active")
            return
    except Exception:
        pass

    _status.update({
        "running": True,
        "progress": "Starting...",
        "files_total": 0,
        "files_done": 0,
        "bytes_copied": 0,
        "current_file": "",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "error": None,
    })

    try:
        # Find the RecentClips source path
        teslacam = _get_teslacam_ro_path()
        if not teslacam:
            _status.update({"running": False, "progress": "No TeslaCam path"})
            return

        recent_clips = os.path.join(teslacam, "RecentClips")
        if not os.path.isdir(recent_clips):
            _status.update({"running": False, "progress": "No RecentClips folder"})
            return

        # Ensure archive directory exists
        os.makedirs(ARCHIVE_DIR, exist_ok=True)

        # Discover files to copy
        _status["progress"] = "Scanning RecentClips..."
        to_copy = list(_discover_new_files(recent_clips))

        if not to_copy:
            _status.update({
                "running": False,
                "progress": "Up to date",
                "last_run": datetime.now(timezone.utc).isoformat(),
                "last_run_files": 0,
            })
            _update_archive_size()
            return

        _status["files_total"] = len(to_copy)
        _status["progress"] = f"Copying {len(to_copy)} files..."
        logger.info("Archive: %d new files to copy from RecentClips", len(to_copy))

        copied = 0
        for idx, (src_path, rel_path) in enumerate(to_copy):
            if _archive_cancel.is_set():
                _status["progress"] = "Cancelled"
                break

            # Memory pressure check
            if not _check_memory():
                logger.warning("Archive paused: low memory")
                _status["progress"] = "Paused (low memory)"
                break

            # Disk space check
            if not _check_disk_space():
                logger.info("Archive stopped: disk space limits reached")
                _status["progress"] = "Stopped (disk space)"
                break

            _status.update({
                "files_done": idx,
                "current_file": rel_path,
            })

            try:
                dst_path = os.path.join(ARCHIVE_DIR, rel_path)
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)

                _buffered_copy(src_path, dst_path)
                # Preserve original modification time
                src_stat = os.stat(src_path)
                os.utime(dst_path, (src_stat.st_atime, src_stat.st_mtime))

                copied += 1
                _status["bytes_copied"] += src_stat.st_size

            except OSError as e:
                logger.warning("Failed to copy %s: %s", rel_path, e)
                continue

            time.sleep(_INTER_FILE_SLEEP)

        _status.update({
            "files_done": copied,
            "last_run": datetime.now(timezone.utc).isoformat(),
            "last_run_files": copied,
        })
        logger.info("Archive: copied %d files", copied)

        # Run retention cleanup
        _status["progress"] = "Retention cleanup..."
        _enforce_retention()

        _update_archive_size()
        _status["progress"] = f"Done — {copied} files archived"

    except Exception as e:
        logger.exception("Archive run error")
        _status["error"] = str(e)
        _status["progress"] = f"Error: {e}"
    finally:
        _status["running"] = False


# ---------------------------------------------------------------------------
# File Discovery
# ---------------------------------------------------------------------------


def _discover_new_files(recent_clips: str) -> Generator[Tuple[str, str], None, None]:
    """Yield (src_abs_path, relative_path) for files not yet archived.

    Skips files younger than _MIN_FILE_AGE_SECONDS (may be actively written).
    Uses generators to avoid building large in-memory lists.
    """
    now = time.time()

    try:
        entries = sorted(os.listdir(recent_clips))
    except OSError:
        return

    for name in entries:
        src = os.path.join(recent_clips, name)
        try:
            stat = os.stat(src)
        except OSError:
            continue

        # Skip directories, non-video files
        if not os.path.isfile(src):
            continue
        if not name.lower().endswith(('.mp4', '.avi', '.mov', '.mkv')):
            continue

        # Skip actively-written files
        if (now - stat.st_mtime) < _MIN_FILE_AGE_SECONDS:
            continue

        # Skip zero-byte files
        if stat.st_size == 0:
            continue

        # Check if already archived (same name and size)
        dst = os.path.join(ARCHIVE_DIR, name)
        if os.path.isfile(dst):
            try:
                dst_stat = os.stat(dst)
                if dst_stat.st_size == stat.st_size:
                    continue  # Already archived
            except OSError:
                pass

        yield (src, name)


# ---------------------------------------------------------------------------
# File Copy
# ---------------------------------------------------------------------------


def _buffered_copy(src: str, dst: str) -> None:
    """Copy a file using rate-limited buffered reads.

    Throttled to _MAX_COPY_BYTES_PER_SEC to avoid saturating the SD card
    I/O bus. The USB gadget shares the same bus — unthrottled copies cause
    endpoint stalls (dwc2 ep1in stalled) and can trigger watchdog reboots.
    """
    dst_tmp = dst + ".tmp"
    try:
        bytes_this_second = 0
        second_start = time.monotonic()

        with open(src, "rb") as fin, open(dst_tmp, "wb") as fout:
            while True:
                chunk = fin.read(_COPY_CHUNK_SIZE)
                if not chunk:
                    break
                fout.write(chunk)
                bytes_this_second += len(chunk)

                # Rate limiting: sleep if we've exceeded the budget for this second
                elapsed = time.monotonic() - second_start
                if bytes_this_second >= _MAX_COPY_BYTES_PER_SEC:
                    sleep_for = 1.0 - elapsed
                    if sleep_for > 0:
                        time.sleep(sleep_for)
                    bytes_this_second = 0
                    second_start = time.monotonic()

            fout.flush()
            os.fsync(fout.fileno())
        os.replace(dst_tmp, dst)
    except Exception:
        # Clean up partial file
        try:
            os.unlink(dst_tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Retention Enforcement
# ---------------------------------------------------------------------------


def _enforce_retention() -> None:
    """Delete oldest archived files when limits are exceeded.

    Enforcement cascade (highest priority first):
    1. min_free_space_gb — hard floor, protects OS and IMG growth
    2. max_size_gb — cap on archive folder size
    3. retention_days — age-based cleanup
    """
    if not os.path.isdir(ARCHIVE_DIR):
        return

    # Age-based cleanup first (cheapest — no disk usage calculation)
    if ARCHIVE_RETENTION_DAYS > 0:
        cutoff = time.time() - (ARCHIVE_RETENTION_DAYS * 86400)
        _delete_files_older_than(cutoff)

    # Size-based cleanup
    max_bytes = ARCHIVE_MAX_SIZE_GB * 1024 * 1024 * 1024
    _trim_archive_to_size(max_bytes)

    # Free-space floor
    min_free_bytes = ARCHIVE_MIN_FREE_SPACE_GB * 1024 * 1024 * 1024
    _trim_archive_for_free_space(min_free_bytes)


def _delete_files_older_than(cutoff_timestamp: float) -> int:
    """Delete archived files older than the cutoff. Returns count deleted."""
    deleted = 0
    if not os.path.isdir(ARCHIVE_DIR):
        return 0

    try:
        for name in os.listdir(ARCHIVE_DIR):
            fpath = os.path.join(ARCHIVE_DIR, name)
            if not os.path.isfile(fpath):
                continue
            try:
                if os.stat(fpath).st_mtime < cutoff_timestamp:
                    os.unlink(fpath)
                    deleted += 1
            except OSError:
                continue
    except OSError:
        pass

    if deleted:
        logger.info("Retention: deleted %d files older than %d days",
                     deleted, ARCHIVE_RETENTION_DAYS)
    return deleted


def _trim_archive_to_size(max_bytes: int) -> int:
    """Delete oldest files until archive is under max_bytes. Returns count deleted."""
    files = _get_archived_files_sorted()
    total_size = sum(s for _, s, _ in files)
    deleted = 0

    while total_size > max_bytes and files:
        fpath, fsize, _ = files.pop(0)  # oldest first
        try:
            os.unlink(fpath)
            total_size -= fsize
            deleted += 1
        except OSError:
            continue

    if deleted:
        logger.info("Retention: deleted %d files to stay under %d GB",
                     deleted, ARCHIVE_MAX_SIZE_GB)
    return deleted


def _trim_archive_for_free_space(min_free_bytes: int) -> int:
    """Delete oldest archived files until SD card has enough free space."""
    files = _get_archived_files_sorted()
    deleted = 0

    while files:
        try:
            usage = shutil.disk_usage(ARCHIVE_DIR)
        except OSError:
            break
        if usage.free >= min_free_bytes:
            break
        fpath, _, _ = files.pop(0)
        try:
            os.unlink(fpath)
            deleted += 1
        except OSError:
            continue

    if deleted:
        logger.info("Retention: deleted %d files to maintain %d GB free",
                     deleted, ARCHIVE_MIN_FREE_SPACE_GB)
    return deleted


def _get_archived_files_sorted() -> List[Tuple[str, int, float]]:
    """Return list of (path, size, mtime) sorted oldest first."""
    files = []
    if not os.path.isdir(ARCHIVE_DIR):
        return files

    try:
        for name in os.listdir(ARCHIVE_DIR):
            fpath = os.path.join(ARCHIVE_DIR, name)
            if not os.path.isfile(fpath):
                continue
            try:
                st = os.stat(fpath)
                files.append((fpath, st.st_size, st.st_mtime))
            except OSError:
                continue
    except OSError:
        pass

    files.sort(key=lambda x: x[2])  # oldest first
    return files


# ---------------------------------------------------------------------------
# System Checks
# ---------------------------------------------------------------------------


def _check_memory() -> bool:
    """Return True if enough memory is available to continue archiving."""
    try:
        with open("/proc/meminfo", "r") as f:
            mem = {}
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    mem[parts[0].rstrip(":")] = int(parts[1]) * 1024  # kB → bytes
        available = mem.get("MemAvailable", 0) + mem.get("SwapFree", 0)
        return available > _MIN_MEMORY_BYTES
    except (OSError, ValueError):
        return True  # Assume OK if we can't read meminfo


def _check_disk_space() -> bool:
    """Return True if archive can continue (space and size limits OK)."""
    # Check SD card free space
    try:
        usage = shutil.disk_usage(ARCHIVE_DIR)
        min_free = ARCHIVE_MIN_FREE_SPACE_GB * 1024 * 1024 * 1024
        if usage.free < min_free:
            return False
    except OSError:
        return False

    # Check archive folder size cap
    max_bytes = ARCHIVE_MAX_SIZE_GB * 1024 * 1024 * 1024
    archive_size = _get_archive_size()
    if archive_size >= max_bytes:
        return False

    return True


def _get_archive_size() -> int:
    """Return total size of archived files in bytes."""
    total = 0
    if not os.path.isdir(ARCHIVE_DIR):
        return 0
    try:
        for name in os.listdir(ARCHIVE_DIR):
            fpath = os.path.join(ARCHIVE_DIR, name)
            try:
                if os.path.isfile(fpath):
                    total += os.stat(fpath).st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def _update_archive_size() -> None:
    """Update the status dict with current archive size."""
    size = _get_archive_size()
    _status["archive_size_mb"] = round(size / (1024 * 1024), 1)


# ---------------------------------------------------------------------------
# Path Resolution
# ---------------------------------------------------------------------------


def _get_teslacam_ro_path() -> Optional[str]:
    """Get the TeslaCam read-only mount path (present mode only)."""
    from services.mode_service import current_mode
    from config import MNT_DIR, RO_MNT_DIR

    mode = current_mode()
    if mode == "present":
        ro_path = os.path.join(RO_MNT_DIR, "part1-ro", "TeslaCam")
        if os.path.isdir(ro_path):
            return ro_path
    elif mode == "edit":
        # In edit mode, RecentClips is at the RW mount path
        rw_path = os.path.join(MNT_DIR, "part1", "TeslaCam")
        if os.path.isdir(rw_path):
            return rw_path
    return None
