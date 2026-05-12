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
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (lazy-loaded from config.py)
# ---------------------------------------------------------------------------

from config import (
    ARCHIVE_DIR,
    ARCHIVE_ENABLED,
    ARCHIVE_RETENTION_DAYS,
    ARCHIVE_MIN_FREE_SPACE_GB,
    ARCHIVE_MAX_SIZE_GB,
    ARCHIVE_ONLY_DRIVING,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum file age before copying (seconds). Files younger than this are
# assumed to still be actively written by Tesla and skipped this cycle.
# Tesla's RecentClips are ~60 s long, so by 90 s the clip is finalized.
# The real safety net against partial reads is ``_is_complete_mp4`` (the
# moov-atom check at line 405 / 585) — this age gate is belt-and-suspenders
# and a cheap fast-path that avoids running the moov scan on actively-rotating
# files. Lowered from 300 s in #70 (was the dominant source of archive lag).
_MIN_FILE_AGE_SECONDS = 90  # 1.5 minutes — see comment above

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

# Max files to copy per archive cycle. Prevents a large backlog from
# monopolising I/O and RAM for the whole archive interval, which can
# starve the web server and trigger the hardware watchdog on Pi Zero 2 W.
# Remaining files are picked up in the next cycle.
_MAX_FILES_PER_CYCLE = 50

# Grace period before pruning a clip as "non-driving". Gives the indexer
# time to extract GPS from the clip and the trip detector time to
# consolidate sequential waypoints into a trip. Clips younger than this
# are kept regardless of whether they fall in a known trip range — they
# may be from a drive that hasn't been indexed yet.
_PRUNE_GRACE_SECONDS = 6 * 3600  # 6 hours

# ---------------------------------------------------------------------------
# Background Thread State
# ---------------------------------------------------------------------------
#
# Phase 2b (issue #76): the legacy ``_archive_thread`` periodic timer
# is gone — the archive flow now runs entirely inside
# ``services.archive_worker``. ``_archive_cancel`` is kept so
# ``stop_archive_timer`` and any retention helper that still consults
# it (defensive abort flag) keep working unchanged.
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
    """Phase 2b (issue #76): Legacy timer is gone — start the worker instead.

    Pre-Phase-2b this spun up ``_archive_timer_loop`` which woke every
    ``ARCHIVE_INTERVAL_MINUTES`` and walked the RO RecentClips folder.
    The Phase 2b architecture is queue-driven: the
    ``archive_producer`` thread (started by ``web_control.startup``)
    enqueues into ``archive_queue`` from inotify + a 60-second rescan,
    and the ``archive_worker`` thread drains the queue one file at a
    time with stable-write gating, atomic copy, and dead-letter
    handling.

    This shim stays callable so existing call sites in
    ``web_control.startup`` keep working without a flag dance, but
    delegates to :func:`services.archive_worker.ensure_worker_started`.
    The retention/cleanup cascade that used to run after each timer
    tick is now driven by the periodic ``smart_cleanup_archive`` call
    that runs from elsewhere (and from the manual cleanup endpoint);
    see :func:`trigger_archive_cleanup`.
    """
    if not ARCHIVE_ENABLED:
        logger.info("RecentClips archive is disabled in config")
        return
    try:
        from services import archive_worker
        archive_worker.ensure_worker_started()
    except Exception as e:  # noqa: BLE001 — never let startup raise here
        logger.warning("archive_worker.ensure_worker_started failed: %s", e)


def stop_archive_timer() -> None:
    """Phase 2b: stop the new archive worker (no legacy timer to cancel)."""
    _archive_cancel.set()
    try:
        from services import archive_worker
        archive_worker.stop_worker()
    except Exception as e:  # noqa: BLE001
        logger.debug("archive_worker.stop_worker raised: %s", e)


def trigger_archive_now() -> bool:
    """Wake the Phase 2b archive worker (was: spawn a one-shot timer thread).

    Returns True if a wake was issued (ARCHIVE_ENABLED and the worker
    accepted the call), False if archiving is disabled or the worker
    couldn't be started. The actual draining happens asynchronously in
    the worker thread.

    Compatibility shim: callers (notably the NM dispatcher's
    ``/api/recent_archive/trigger`` endpoint and the legacy duplicate-
    guard tests) get a True/False return that tracks "did we kick the
    worker?" rather than the old "did we start a thread?" — which is
    semantically equivalent for every external caller.
    """
    if not ARCHIVE_ENABLED:
        return False
    try:
        from services import archive_worker
        # Lazy-start the worker if it isn't running yet.
        archive_worker.ensure_worker_started()
        archive_worker.wake()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("trigger_archive_now: failed to wake worker: %s", e)
        return False


# ---------------------------------------------------------------------------
# Phase 2b (issue #76): the legacy ``_archive_timer_loop`` /
# ``_run_archive`` / ``_do_archive_work`` / ``_discover_new_files``
# code paths have been removed in favor of the queue-driven
# ``archive_producer`` + ``archive_worker`` pair.
#
# The retention/cleanup helpers below (``_purge_corrupt_archives``,
# ``_prune_non_driving_archives``, ``_enforce_retention``,
# ``smart_cleanup_archive``, ``trigger_archive_cleanup``) remain
# callable from the API/cleanup endpoints — they don't depend on the
# old timer loop and operate purely on ``ARCHIVE_DIR``.
# ---------------------------------------------------------------------------


def _get_driving_time_ranges() -> Optional[List[Tuple[float, float]]]:
    """Load trip time ranges from geodata.db.

    Returns a sorted list of (start_epoch, end_epoch) tuples, or None
    if the database is unavailable (fall back to archiving everything).
    """
    try:
        from config import MAPPING_ENABLED, MAPPING_DB_PATH
        if not MAPPING_ENABLED:
            return None
        if not os.path.isfile(MAPPING_DB_PATH):
            return None

        import sqlite3
        conn = sqlite3.connect(MAPPING_DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT start_time, end_time FROM trips ORDER BY start_time"
            ).fetchall()
            if not rows:
                return None  # No trips indexed yet — archive everything

            ranges = []
            _PADDING = 120  # 2 min padding around each trip
            for r in rows:
                try:
                    st = datetime.fromisoformat(r['start_time']).timestamp() - _PADDING
                    et = datetime.fromisoformat(r['end_time']).timestamp() + _PADDING
                    ranges.append((st, et))
                except (ValueError, TypeError):
                    continue
            return ranges if ranges else None
        finally:
            conn.close()
    except Exception as e:
        logger.debug("Could not load driving ranges: %s", e)
        return None


def _timestamp_from_filename(name: str) -> Optional[float]:
    """Extract epoch timestamp from Tesla filename like '2026-04-08_20-00-03-front.mp4'."""
    try:
        # First 19 chars: '2026-04-08_20-00-03'
        ts_str = os.path.basename(name)[:19]
        dt = datetime.strptime(ts_str, '%Y-%m-%d_%H-%M-%S')
        return dt.timestamp()
    except (ValueError, IndexError):
        return None


def _is_during_driving(clip_ts: float, ranges: List[Tuple[float, float]]) -> bool:
    """Return True if clip_ts falls within any driving time range."""
    for start, end in ranges:
        if start <= clip_ts <= end:
            return True
        if start > clip_ts:
            break  # Ranges are sorted — no need to check further
    return False


def _prune_non_driving_archives() -> int:
    """Delete archived clips that the indexer has positively identified
    as containing no GPS data and no detected events.

    Honors the ARCHIVE_ONLY_DRIVING flag — does nothing if False.

    A clip is DELETED only when there is positive evidence it isn't a
    drive. Specifically, the front-camera clip must have an
    ``indexed_files`` row (so we know the indexer processed it) with
    ``waypoint_count == 0`` AND ``event_count == 0`` (so we know the
    extraction succeeded but found nothing). Clips that have never been
    indexed are kept unconditionally — we never delete on the absence of
    evidence, only on the presence of contrary evidence.

    A clip is also KEPT if any of:
      - It's younger than the grace period (give the indexer time)
      - Its timestamp falls within any known trip range (with padding)
      - It has waypoints in geodata.db (proven driving)
      - It has detected events in geodata.db (saved/sentry events)

    Decisions are made per timestamp prefix (YYYY-MM-DD_HH-MM-SS) so all
    six cameras for one recording moment are kept or deleted together.
    The front-camera clip is the source of truth (only front-cam is
    indexed); sibling cameras follow the front-cam decision. Side cameras
    that exist as orphans (no front-cam) are left alone — we only delete
    on positive evidence.

    Returns the number of files deleted.
    """
    if not ARCHIVE_ONLY_DRIVING:
        return 0
    if not os.path.isdir(ARCHIVE_DIR):
        return 0

    driving_ranges = _get_driving_time_ranges()
    if driving_ranges is None:
        return 0  # No trips yet — leave clips so they can be indexed

    # Build:
    #   keep_basenames        — clips with positive driving/event evidence
    #   proven_non_driving    — front-cam basenames the indexer proved have
    #                            no GPS and no events (safe to delete)
    keep_basenames = set()
    proven_non_driving = set()
    try:
        from config import MAPPING_ENABLED, MAPPING_DB_PATH
        if not (MAPPING_ENABLED and os.path.isfile(MAPPING_DB_PATH)):
            return 0
        import sqlite3
        conn = sqlite3.connect(MAPPING_DB_PATH, timeout=10)
        try:
            for row in conn.execute(
                "SELECT DISTINCT video_path FROM waypoints "
                "WHERE lat != 0 AND lon != 0"
            ):
                if row[0]:
                    keep_basenames.add(os.path.basename(row[0]))
            for row in conn.execute(
                "SELECT DISTINCT video_path FROM detected_events"
            ):
                if row[0]:
                    keep_basenames.add(os.path.basename(row[0]))
            for row in conn.execute(
                "SELECT file_path FROM indexed_files "
                "WHERE waypoint_count = 0 AND event_count = 0"
            ):
                if not row[0]:
                    continue
                basename = os.path.basename(row[0])
                if basename.endswith('-front.mp4'):
                    proven_non_driving.add(basename)
        finally:
            conn.close()
    except Exception as e:
        logger.debug("prune: failed loading classification sets: %s", e)
        return 0  # Can't determine safely — don't prune this cycle

    cutoff = time.time() - _PRUNE_GRACE_SECONDS

    try:
        names = list(os.listdir(ARCHIVE_DIR))
    except OSError:
        return 0

    # Decide per timestamp prefix using the front-camera clip
    decision: Dict[str, bool] = {}  # ts_prefix -> True (keep) / False (delete)
    for name in names:
        if not name.lower().endswith('.mp4'):
            continue
        if not name.endswith('-front.mp4'):
            continue

        clip_ts = _timestamp_from_filename(name)
        if clip_ts is None:
            continue
        ts_key = name[:19]

        # Grace period — keep recent clips no matter what
        if clip_ts > cutoff:
            decision[ts_key] = True
            continue

        # Indexer found waypoints/events for this clip
        if name in keep_basenames:
            decision[ts_key] = True
            continue

        # Falls inside a known driving trip's time window
        if _is_during_driving(clip_ts, driving_ranges):
            decision[ts_key] = True
            continue

        # Only DELETE when we have positive evidence that the indexer
        # processed this clip and found neither GPS nor events. Without
        # that evidence (no indexed_files row at all), keep the clip so
        # the indexer can try again on a future run.
        if name in proven_non_driving:
            decision[ts_key] = False
        else:
            decision[ts_key] = True

    # Second pass: delete all clips (any camera) whose timestamp prefix
    # is marked for deletion. Side/back cams without a front-cam decision
    # are NEVER deleted here — only positive evidence drives deletion.
    from services.file_safety import safe_delete_archive_video
    deleted = 0
    deleted_paths: List[str] = []
    for name in names:
        if not name.lower().endswith('.mp4'):
            continue
        if len(name) < 19:
            continue
        ts_key = name[:19]
        if decision.get(ts_key) is not False:
            continue
        fpath = os.path.join(ARCHIVE_DIR, name)
        if safe_delete_archive_video(fpath) > 0:
            deleted += 1
            deleted_paths.append(fpath)

    if deleted:
        logger.info(
            "Prune: deleted %d archived clips with positive non-driving evidence",
            deleted,
        )
        # Targeted geodata purge for the exact files we just deleted.
        try:
            from config import MAPPING_ENABLED, MAPPING_DB_PATH
            if MAPPING_ENABLED and os.path.isfile(MAPPING_DB_PATH):
                from services.mapping_service import purge_deleted_videos
                purge_deleted_videos(
                    MAPPING_DB_PATH, deleted_paths=deleted_paths,
                )
        except Exception as e:
            logger.debug("Prune geodata purge failed: %s", e)

    return deleted


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

    from services.file_safety import safe_delete_archive_video
    removed = 0
    try:
        for name in os.listdir(ARCHIVE_DIR):
            if not name.lower().endswith('.mp4'):
                continue
            fpath = os.path.join(ARCHIVE_DIR, name)
            if not os.path.isfile(fpath):
                continue
            if not _is_complete_mp4(fpath):
                if safe_delete_archive_video(fpath) > 0:
                    removed += 1
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

    After deleting files, purges stale entries from geodata.db so the
    map page doesn't show ghost trips with missing video files. The
    purge is targeted (the exact files we just deleted) rather than a
    full-tree scan — much cheaper on a Pi with thousands of clips.
    """
    if not os.path.isdir(ARCHIVE_DIR):
        return

    deleted_paths: List[str] = []

    # Age-based cleanup first (cheapest — no disk usage calculation)
    if ARCHIVE_RETENTION_DAYS > 0:
        cutoff = time.time() - (ARCHIVE_RETENTION_DAYS * 86400)
        deleted_paths.extend(_delete_files_older_than(cutoff))

    # Size-based cleanup (only when a hard cap is set)
    if ARCHIVE_MAX_SIZE_GB > 0:
        max_bytes = ARCHIVE_MAX_SIZE_GB * 1024 * 1024 * 1024
        deleted_paths.extend(_trim_archive_to_size(max_bytes))

    # Free-space floor
    min_free_bytes = ARCHIVE_MIN_FREE_SPACE_GB * 1024 * 1024 * 1024
    deleted_paths.extend(_trim_archive_for_free_space(min_free_bytes))

    # Targeted geodata purge for the exact files we just deleted.
    if deleted_paths:
        try:
            from config import MAPPING_ENABLED, MAPPING_DB_PATH
            if MAPPING_ENABLED and os.path.isfile(MAPPING_DB_PATH):
                from services.mapping_service import purge_deleted_videos
                result = purge_deleted_videos(
                    MAPPING_DB_PATH, deleted_paths=deleted_paths,
                )
                purged = result.get('purged_files', 0)
                if purged:
                    logger.info(
                        "Retention: purged %d stale geodata entries", purged,
                    )
        except Exception as e:
            logger.debug("Retention geodata purge failed (non-fatal): %s", e)


def _delete_files_older_than(cutoff_timestamp: float) -> List[str]:
    """Delete archived files older than the cutoff. Returns deleted paths."""
    from services.file_safety import safe_delete_archive_video
    deleted: List[str] = []
    if not os.path.isdir(ARCHIVE_DIR):
        return deleted

    try:
        for name in os.listdir(ARCHIVE_DIR):
            fpath = os.path.join(ARCHIVE_DIR, name)
            if not os.path.isfile(fpath):
                continue
            try:
                if os.stat(fpath).st_mtime < cutoff_timestamp:
                    if safe_delete_archive_video(fpath) > 0:
                        deleted.append(fpath)
            except OSError:
                continue
    except OSError:
        pass

    if deleted:
        logger.info("Retention: deleted %d files older than %d days",
                     len(deleted), ARCHIVE_RETENTION_DAYS)
    return deleted


def _trim_archive_to_size(max_bytes: int) -> List[str]:
    """Delete oldest files until archive is under max_bytes. Returns deleted paths."""
    from services.file_safety import safe_delete_archive_video
    files = _get_archived_files_sorted()
    total_size = sum(s for _, s, _ in files)
    deleted: List[str] = []

    while total_size > max_bytes and files:
        fpath, fsize, _ = files.pop(0)  # oldest first
        if safe_delete_archive_video(fpath) > 0:
            total_size -= fsize
            deleted.append(fpath)

    if deleted:
        logger.info("Retention: deleted %d files to stay under %d GB",
                     len(deleted), ARCHIVE_MAX_SIZE_GB)
    return deleted


def _trim_archive_for_free_space(min_free_bytes: int) -> List[str]:
    """Delete oldest archived files until SD card has enough free space.

    Returns the deleted paths so callers can do a targeted geodata
    purge instead of a full-tree scan.
    """
    from services.file_safety import safe_delete_archive_video
    files = _get_archived_files_sorted()
    deleted: List[str] = []

    while files:
        try:
            usage = shutil.disk_usage(ARCHIVE_DIR)
        except OSError:
            break
        if usage.free >= min_free_bytes:
            break
        fpath, _, _ = files.pop(0)
        if safe_delete_archive_video(fpath) > 0:
            deleted.append(fpath)

    if deleted:
        logger.info("Retention: deleted %d files to maintain %d GB free",
                     len(deleted), ARCHIVE_MIN_FREE_SPACE_GB)
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

    Uses the canonical-key candidate paths so we update every form the DB
    might have stored (bare basename, RecentClips/<basename>) and never
    accidentally match an unrelated path that simply happens to contain
    the basename as a substring.
    """
    try:
        import sqlite3
        from config import MAPPING_DB_PATH
        from services.mapping_service import canonical_key, candidate_db_paths

        if not os.path.isfile(MAPPING_DB_PATH):
            return

        conn = sqlite3.connect(MAPPING_DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row

        try:
            basename = os.path.basename(filename)
            new_rel = f"ArchivedClips/{basename}"
            old_paths = candidate_db_paths(canonical_key(basename))
            placeholders = ','.join('?' * len(old_paths))

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

            # Update waypoints.video_path — exact-match every candidate form
            # except the new path itself (avoid no-op self-update churn).
            non_archive = [p for p in old_paths if p != new_rel]
            if non_archive:
                non_archive_placeholders = ','.join('?' * len(non_archive))
                conn.execute(
                    f"UPDATE waypoints SET video_path = ? "
                    f"WHERE video_path IN ({non_archive_placeholders})",
                    (new_rel, *non_archive),
                )
                conn.execute(
                    f"UPDATE detected_events SET video_path = ? "
                    f"WHERE video_path IN ({non_archive_placeholders})",
                    (new_rel, *non_archive),
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
    """Return True if archive can continue (space limits OK).

    When ``ARCHIVE_MAX_SIZE_GB`` is 0 (the default), the archive grows
    freely — the only constraint is ``ARCHIVE_MIN_FREE_SPACE_GB`` which
    reserves space for the OS and other services.  A non-zero value
    imposes a hard cap on the archive folder itself.
    """
    # Check SD card free space (always enforced)
    try:
        usage = shutil.disk_usage(ARCHIVE_DIR)
        min_free = ARCHIVE_MIN_FREE_SPACE_GB * 1024 * 1024 * 1024
        if usage.free < min_free:
            return False
    except OSError:
        return False

    # Optional hard cap on archive folder size (0 = no cap)
    if ARCHIVE_MAX_SIZE_GB > 0:
        max_bytes = ARCHIVE_MAX_SIZE_GB * 1024 * 1024 * 1024
        archive_size = _get_archive_size()
        if archive_size >= max_bytes:
            return False

    return True


def _proactive_retention() -> None:
    """Run retention early when free space is getting low.

    Triggers at 2× the ``min_free_space_gb`` floor so there is always a
    buffer before the hard limit is reached.  This avoids the scenario
    where we hit the floor mid-copy and have to stop.  Uses the smart
    cleanup path (deletes least-valuable files first) when available,
    falling back to age-based + oldest-first retention.
    """
    try:
        usage = shutil.disk_usage(ARCHIVE_DIR)
    except OSError:
        return

    soft_threshold = ARCHIVE_MIN_FREE_SPACE_GB * 2 * 1024 * 1024 * 1024
    if usage.free >= soft_threshold:
        return  # Plenty of space — nothing to do

    free_gb = usage.free / (1024 ** 3)
    target_gb = ARCHIVE_MIN_FREE_SPACE_GB * 2
    logger.info(
        "Proactive retention: %.1f GB free < %.0f GB soft threshold, cleaning up",
        free_gb, target_gb,
    )

    # Try smart cleanup first (deletes least-valuable files)
    try:
        result = smart_cleanup_archive(
            ARCHIVE_DIR,
            min_free_gb=target_gb,
            max_size_gb=ARCHIVE_MAX_SIZE_GB,
        )
        if result["deleted_count"]:
            logger.info(
                "Proactive retention: smart cleanup freed %.1f MB (%d files)",
                result["freed_bytes"] / (1024 * 1024), result["deleted_count"],
            )
            return
    except Exception as e:
        logger.debug("Smart cleanup unavailable, using basic retention: %s", e)

    # Fall back to basic retention (oldest-first)
    _trim_archive_for_free_space(int(target_gb * 1024 ** 3))


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

    if free_gb >= min_free_gb and (max_size_gb <= 0 or archive_gb <= max_size_gb):
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
    max_archive_bytes = int(max_size_gb * 1024 ** 3) if max_size_gb > 0 else 0

    for _score, _mtime, f in scored:
        # Recheck conditions
        try:
            current_usage = shutil.disk_usage(archive_dir)
            current_free = current_usage.free
        except OSError:
            break

        current_archive_size = archive_size - result["freed_bytes"]
        size_ok = (max_archive_bytes <= 0) or (current_archive_size <= max_archive_bytes)
        if current_free >= min_free_bytes and size_ok:
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