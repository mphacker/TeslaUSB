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

        # Run smart cleanup after each archive cycle
        try:
            smart_cleanup_archive(ARCHIVE_DIR, ARCHIVE_MIN_FREE_SPACE_GB, ARCHIVE_MAX_SIZE_GB)
        except Exception:
            logger.exception("Smart archive cleanup failed")

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

        # Clean up stale .tmp files from interrupted copies
        try:
            for name in os.listdir(ARCHIVE_DIR):
                if name.endswith('.tmp'):
                    os.unlink(os.path.join(ARCHIVE_DIR, name))
        except OSError:
            pass

        # Remove any corrupt archived files (incomplete MP4s from prior runs)
        _status["progress"] = "Checking existing archives..."
        _purge_corrupt_archives()

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

                # Post-copy validation: verify the copy is a complete MP4
                if dst_path.lower().endswith('.mp4') and not _is_complete_mp4(dst_path):
                    logger.warning("Archived file incomplete (no moov), removing: %s", rel_path)
                    try:
                        os.unlink(dst_path)
                    except OSError:
                        pass
                    continue

                copied += 1
                _status["bytes_copied"] += src_stat.st_size

                # Update geodata DB to point at the archived copy.
                # This avoids expensive re-indexing — the GPS data is
                # already extracted, we just change the file path.
                _update_geodata_paths(src_path, dst_path, rel_path)

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

        # Skip incomplete MP4s (Tesla still writing — moov box not yet finalized)
        if name.lower().endswith('.mp4') and not _is_complete_mp4(src):
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
# MP4 Validation
# ---------------------------------------------------------------------------


def _is_complete_mp4(filepath: str) -> bool:
    """Check if an MP4 file has both ftyp and moov boxes (is complete).

    Tesla writes the moov atom at the END of the file. If the file was
    copied while Tesla was still recording, the moov box will be missing
    and the file is unplayable.
    """
    try:
        with open(filepath, 'rb') as f:
            header = f.read(12)
            if len(header) < 12 or b'ftyp' not in header:
                return False

            # Scan for moov box — read box headers sequentially
            f.seek(0)
            file_size = os.path.getsize(filepath)
            pos = 0
            while pos < file_size - 8:
                f.seek(pos)
                box_header = f.read(8)
                if len(box_header) < 8:
                    break
                box_size = int.from_bytes(box_header[:4], 'big')
                box_type = box_header[4:8]

                if box_type == b'moov':
                    return True

                if box_size < 8:
                    break  # Invalid box
                pos += box_size

            return False  # moov not found
    except (OSError, IOError):
        return False


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
# Corrupt File Cleanup
# ---------------------------------------------------------------------------


def _purge_corrupt_archives() -> int:
    """Remove archived MP4 files that are incomplete (no moov box).

    These result from copying files that Tesla was still writing.
    Returns the count of files removed.
    """
    if not os.path.isdir(ARCHIVE_DIR):
        return 0

    removed = 0
    try:
        for name in os.listdir(ARCHIVE_DIR):
            if not name.lower().endswith('.mp4'):
                continue
            fpath = os.path.join(ARCHIVE_DIR, name)
            if not os.path.isfile(fpath):
                continue
            if not _is_complete_mp4(fpath):
                try:
                    os.unlink(fpath)
                    removed += 1
                except OSError:
                    continue
    except OSError:
        pass

    if removed:
        logger.info("Purged %d corrupt/incomplete archived files", removed)
    return removed


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
# Geodata DB Path Updates
# ---------------------------------------------------------------------------


def _update_geodata_paths(old_abs: str, new_abs: str, filename: str) -> None:
    """Update geodata.db to point at the archived copy of a video.

    When a RecentClips file is archived to the SD card, we update the DB
    paths rather than re-indexing (the GPS data is already extracted).

    - indexed_files.file_path: absolute path (primary key — requires delete+insert)
    - waypoints.video_path: relative path (e.g. "RecentClips/...-front.mp4")
    - detected_events.video_path: same relative format
    """
    try:
        import sqlite3
        from config import MAPPING_DB_PATH

        if not os.path.isfile(MAPPING_DB_PATH):
            return

        conn = sqlite3.connect(MAPPING_DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row

        try:
            # Build the old and new relative paths for waypoints/events
            # Old: "RecentClips/2026-...-front.mp4"
            # New: use a synthetic "ArchivedClips/..." relative path
            basename = os.path.basename(filename)
            old_rel_pattern = f"%{basename}"
            new_rel = f"ArchivedClips/{basename}"

            # Update indexed_files (file_path is PRIMARY KEY, so delete+insert)
            row = conn.execute(
                "SELECT * FROM indexed_files WHERE file_path = ?",
                (old_abs,)
            ).fetchone()

            if row:
                conn.execute("DELETE FROM indexed_files WHERE file_path = ?", (old_abs,))
                conn.execute(
                    "INSERT INTO indexed_files (file_path, file_size, file_mtime, waypoint_count, event_count) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (new_abs, row['file_size'], row['file_mtime'],
                     row['waypoint_count'], row['event_count']),
                )

            # Update waypoints.video_path
            conn.execute(
                "UPDATE waypoints SET video_path = ? WHERE video_path LIKE ?",
                (new_rel, old_rel_pattern),
            )

            # Update detected_events.video_path
            conn.execute(
                "UPDATE detected_events SET video_path = ? WHERE video_path LIKE ?",
                (new_rel, old_rel_pattern),
            )

            conn.commit()
        finally:
            conn.close()

    except Exception as e:
        # Non-fatal — the file is archived even if DB update fails.
        # The purge logic will fix paths on the next full scan.
        logger.debug("Failed to update geodata paths for %s: %s", filename, e)


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


# ---------------------------------------------------------------------------
# Smart Archive Cleanup
# ---------------------------------------------------------------------------


def smart_cleanup_archive(
    archive_dir: str,
    min_free_gb: float = 10.0,
    max_size_gb: float = 50.0,
) -> dict:
    """Smart cleanup of ArchivedClips when SD card space is low.

    Priority order for deletion:
    1. Videos without events AND without GPS coordinates (least valuable)
    2. Oldest videos (by file modification time)
    Never: Delete videos that are queued for or currently syncing to cloud.

    Returns dict with deleted_count, freed_bytes, and details.
    """
    import sqlite3
    from services.file_safety import safe_remove

    result = {
        "deleted_count": 0,
        "freed_bytes": 0,
        "skipped_cloud_queue": 0,
        "details": [],
    }

    if not os.path.isdir(archive_dir):
        return result

    # Check if cleanup is needed
    try:
        usage = shutil.disk_usage(archive_dir)
    except OSError:
        logger.warning("Smart cleanup: cannot read disk usage for %s", archive_dir)
        return result

    free_gb = usage.free / (1024 ** 3)
    archive_size = _get_archive_size()
    archive_gb = archive_size / (1024 ** 3)

    if free_gb >= min_free_gb and archive_gb <= max_size_gb:
        logger.debug("Smart cleanup: no action needed (%.1f GB free, %.1f GB archive)", free_gb, archive_gb)
        return result

    logger.info(
        "Smart cleanup: starting (%.1f GB free < %.1f GB min, or %.1f GB archive > %.1f GB max)",
        free_gb, min_free_gb, archive_gb, max_size_gb,
    )

    # Scan all .mp4 files
    files_to_score = []
    try:
        for entry in os.scandir(archive_dir):
            if not entry.name.lower().endswith('.mp4'):
                continue
            try:
                st = entry.stat()
                files_to_score.append({
                    "path": entry.path,
                    "name": entry.name,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                })
            except OSError:
                continue
    except OSError:
        logger.warning("Smart cleanup: cannot scan %s", archive_dir)
        return result

    if not files_to_score:
        return result

    # Check geodata.db for GPS data and events
    geo_db_path = None
    try:
        from config import MAPPING_DB_PATH
        if os.path.isfile(MAPPING_DB_PATH):
            geo_db_path = MAPPING_DB_PATH
    except Exception:
        pass

    files_with_gps = set()
    files_with_events = set()
    if geo_db_path:
        try:
            conn = sqlite3.connect(geo_db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            try:
                for f in files_to_score:
                    row = conn.execute(
                        "SELECT 1 FROM waypoints WHERE video_path LIKE ? LIMIT 1",
                        ('%' + f["name"] + '%',)
                    ).fetchone()
                    if row:
                        files_with_gps.add(f["name"])
            except sqlite3.OperationalError:
                pass  # Table may not exist

            try:
                for f in files_to_score:
                    row = conn.execute(
                        "SELECT 1 FROM detected_events WHERE video_path LIKE ? LIMIT 1",
                        ('%' + f["name"] + '%',)
                    ).fetchone()
                    if row:
                        files_with_events.add(f["name"])
            except sqlite3.OperationalError:
                pass  # Table may not exist
            conn.close()
        except Exception:
            logger.debug("Smart cleanup: could not query geodata.db")

    # Check cloud sync status - skip files queued/uploading
    cloud_queued_files = set()
    try:
        from config import CLOUD_ARCHIVE_DB_PATH
        if os.path.isfile(CLOUD_ARCHIVE_DB_PATH):
            cconn = sqlite3.connect(CLOUD_ARCHIVE_DB_PATH, timeout=5)
            cconn.row_factory = sqlite3.Row
            try:
                for f in files_to_score:
                    row = cconn.execute(
                        "SELECT status FROM cloud_synced_files WHERE file_path LIKE ? AND status IN ('queued', 'uploading', 'pending') LIMIT 1",
                        ('%' + f["name"] + '%',)
                    ).fetchone()
                    if row:
                        cloud_queued_files.add(f["name"])
            except sqlite3.OperationalError:
                pass
            cconn.close()
    except Exception:
        pass

    # Assign scores and filter out cloud-queued files
    scored = []
    for f in files_to_score:
        name = f["name"]
        if name in cloud_queued_files:
            result["skipped_cloud_queue"] += 1
            continue

        has_gps = name in files_with_gps
        has_events = name in files_with_events
        score = 0
        if has_gps:
            score += 50
        if has_events:
            score += 50
        scored.append((score, f["mtime"], f))

    # Sort: lowest score first, then oldest first within same score
    scored.sort(key=lambda x: (x[0], x[1]))

    # Delete files until space constraints are met
    min_free_bytes = int(min_free_gb * 1024 ** 3)
    max_archive_bytes = int(max_size_gb * 1024 ** 3)

    for _score, _mtime, f in scored:
        # Recheck conditions
        try:
            current_usage = shutil.disk_usage(archive_dir)
            current_free = current_usage.free
        except OSError:
            break

        current_archive_size = archive_size - result["freed_bytes"]
        if current_free >= min_free_bytes and current_archive_size <= max_archive_bytes:
            break

        if safe_remove(f["path"]):
            result["deleted_count"] += 1
            result["freed_bytes"] += f["size"]
            result["details"].append(f["name"])
            logger.info("Smart cleanup: deleted %s (score=%d, size=%d)", f["name"], _score, f["size"])

    if result["deleted_count"]:
        logger.info(
            "Smart cleanup: deleted %d files, freed %.1f MB (skipped %d cloud-queued)",
            result["deleted_count"],
            result["freed_bytes"] / (1024 * 1024),
            result["skipped_cloud_queue"],
        )
        _update_archive_size()

    return result


def trigger_archive_cleanup() -> dict:
    """Manually trigger archive cleanup. Returns result."""
    return smart_cleanup_archive(ARCHIVE_DIR, ARCHIVE_MIN_FREE_SPACE_GB, ARCHIVE_MAX_SIZE_GB)