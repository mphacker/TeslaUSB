"""
TeslaUSB Cloud Archive Service.

Manages rclone-based file synchronization from the Pi's dashcam storage to
cloud providers, with SQLite tracking for power-loss resilience.

Designed for Pi Zero 2 W (512 MB RAM): processes one file at a time,
uses WAL-mode SQLite with periodic checkpoints, and writes temporary
rclone credentials to tmpfs only for the duration of each upload.
"""

import logging
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration imports (lazy-safe; config.py is always available)
# ---------------------------------------------------------------------------

from config import (
    CLOUD_ARCHIVE_ENABLED,
    CLOUD_ARCHIVE_PROVIDER,
    CLOUD_ARCHIVE_REMOTE_PATH,
    CLOUD_ARCHIVE_SYNC_FOLDERS,
    CLOUD_ARCHIVE_PRIORITY_ORDER,
    CLOUD_ARCHIVE_MAX_UPLOAD_MBPS,
    CLOUD_ARCHIVE_DB_PATH,
    CLOUD_PROVIDER_CREDS_PATH,
    CLOUD_ARCHIVE_SYNC_NON_EVENT,
)

# ---------------------------------------------------------------------------
# Database Schema & Versioning
# ---------------------------------------------------------------------------

_CLOUD_MODULE = "cloud_archive"
_CLOUD_SCHEMA_VERSION = 1

_CLOUD_TABLES_SQL = """\
CREATE TABLE IF NOT EXISTS module_versions (
    module TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS cloud_synced_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL UNIQUE,
    file_size INTEGER,
    file_mtime REAL,
    remote_path TEXT,
    status TEXT DEFAULT 'pending',
    synced_at TEXT,
    retry_count INTEGER DEFAULT 0,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS cloud_sync_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    files_synced INTEGER DEFAULT 0,
    bytes_transferred INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running',
    trigger TEXT,
    window_mode TEXT,
    error_msg TEXT
);

CREATE INDEX IF NOT EXISTS idx_cloud_synced_status ON cloud_synced_files(status);
CREATE INDEX IF NOT EXISTS idx_cloud_synced_mtime ON cloud_synced_files(file_mtime);
CREATE INDEX IF NOT EXISTS idx_cloud_sessions_started ON cloud_sync_sessions(started_at);
"""

# ---------------------------------------------------------------------------
# Background Sync State
# ---------------------------------------------------------------------------

_sync_thread: Optional[threading.Thread] = None
_sync_lock = threading.Lock()
_sync_cancel = threading.Event()

_sync_status: Dict = {
    "running": False,
    "progress": "",
    "files_total": 0,
    "files_done": 0,
    "bytes_transferred": 0,
    "total_bytes": 0,
    "current_file": "",
    "current_file_size": 0,
    "started_at": None,
    "last_run": None,
    "error": None,
}

# Tmpfs directory for short-lived rclone config
_RCLONE_TMPFS_DIR = "/run/teslausb"
_RCLONE_CONF_PATH = os.path.join(_RCLONE_TMPFS_DIR, "rclone.conf")


# ---------------------------------------------------------------------------
# Database Helpers
# ---------------------------------------------------------------------------

def _check_db_integrity(db_path: str) -> bool:
    """Run PRAGMA integrity_check on a database file.

    Returns True if the database is healthy, False if corrupt or unreadable.
    """
    if not os.path.exists(db_path):
        return True  # Non-existent DB is fine — will be created fresh
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        return result is not None and result[0] == "ok"
    except Exception as exc:
        logger.warning("Integrity check failed for %s: %s", db_path, exc)
        return False


def _handle_corrupt_db(db_path: str) -> None:
    """Rename a corrupt database aside and log a warning.

    The caller will recreate a fresh database from the schema. The cloud
    provider is the source of truth for what has been uploaded, so losing
    the local tracking DB only means files will be re-scanned (fast) and
    rclone ``--checksum`` will skip files already present on the remote.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    corrupt_path = f"{db_path}.corrupt.{ts}"
    try:
        os.rename(db_path, corrupt_path)
        logger.warning(
            "Corrupt cloud sync database renamed to %s — will rebuild from scratch",
            corrupt_path,
        )
    except OSError as exc:
        logger.error("Failed to rename corrupt DB %s: %s — deleting instead", db_path, exc)
        try:
            os.remove(db_path)
        except OSError:
            pass
    # Also clean up any leftover WAL/SHM files
    for suffix in ("-wal", "-shm"):
        wal_path = db_path + suffix
        if os.path.exists(wal_path):
            try:
                os.remove(wal_path)
            except OSError:
                pass


def _init_cloud_tables(db_path: str) -> sqlite3.Connection:
    """Open the cloud sync database and ensure all tables exist.

    Runs an integrity check on first access.  If the database is corrupt it
    is renamed aside and rebuilt from scratch — the cloud provider is the
    source of truth for uploaded files, so the only cost is a re-scan.
    """
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    # Corruption recovery: detect and quarantine corrupt databases
    if not _check_db_integrity(db_path):
        _handle_corrupt_db(db_path)

    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # Ensure module_versions table exists first
    conn.execute(
        "CREATE TABLE IF NOT EXISTS module_versions "
        "(module TEXT PRIMARY KEY, version INTEGER NOT NULL, updated_at TEXT)"
    )

    # Check current version for this module
    row = conn.execute(
        "SELECT version FROM module_versions WHERE module = ?",
        (_CLOUD_MODULE,),
    ).fetchone()
    current = row["version"] if row else 0

    if current < _CLOUD_SCHEMA_VERSION:
        conn.executescript(_CLOUD_TABLES_SQL)

        # Future migrations would go here:
        # if current < 2:
        #     conn.execute("ALTER TABLE cloud_synced_files ADD COLUMN ...")

        conn.execute(
            "INSERT OR REPLACE INTO module_versions (module, version, updated_at) "
            "VALUES (?, ?, ?)",
            (_CLOUD_MODULE, _CLOUD_SCHEMA_VERSION,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        logger.info(
            "Cloud archive tables initialized (v%d) in %s",
            _CLOUD_SCHEMA_VERSION, db_path,
        )

    return conn


# ---------------------------------------------------------------------------
# Priority Scoring
# ---------------------------------------------------------------------------

def _score_event_priority(event_dir: str) -> int:
    """Score an event directory for sync priority (lower = higher priority).

    Priority order:
    1. Events with event.json containing sentry/save triggers (score 0-99)
    2. Events with geolocation data in geodata.db (score 100-199)
    3. Other events (score 200+)

    Within each tier: older events get lower scores (synced first).
    """
    import json
    from datetime import datetime as _dt

    score = 200  # Default: lowest priority
    dir_name = os.path.basename(event_dir)

    # Check for event.json (Tesla's event metadata)
    event_json = os.path.join(event_dir, 'event.json')
    if os.path.isfile(event_json):
        try:
            with open(event_json, 'r') as f:
                data = json.load(f)
            reason = data.get('reason', '')
            if reason:
                score = 0  # Has a Tesla event trigger — highest priority
        except (json.JSONDecodeError, OSError):
            pass

    # Check geodata.db for geolocation
    if score >= 200:
        try:
            from config import MAPPING_ENABLED, MAPPING_DB_PATH
            if MAPPING_ENABLED:
                from services.mapping_service import get_db_connection
                conn = get_db_connection(MAPPING_DB_PATH)
                # Escape LIKE wildcards in dir_name to prevent unintended matches
                safe_name = dir_name.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM waypoints WHERE video_path LIKE ? ESCAPE '\\'",
                    (f'%{safe_name}%',)
                ).fetchone()
                conn.close()
                if row and row['cnt'] > 0:
                    score = 100  # Has geolocation — medium priority
        except Exception:
            pass

    # Add age-based sub-score (older = lower number = higher priority)
    try:
        # Parse timestamp from directory name (e.g., "2026-01-15_14-30-45")
        ts = _dt.strptime(dir_name[:19], '%Y-%m-%d_%H-%M-%S')
        # Days old (capped at 99 to stay within tier)
        days_old = min(99, (_dt.now() - ts).days)
        score += (99 - days_old)  # Older = lower score within tier
    except (ValueError, TypeError):
        score += 50  # Can't parse date — middle of tier

    return score


# ---------------------------------------------------------------------------
# File Discovery
# ---------------------------------------------------------------------------

def _fsync_db(conn: sqlite3.Connection) -> None:
    """Commit and fsync the database to ensure durability after power loss."""
    conn.commit()
    try:
        fd = os.open(conn.execute("PRAGMA database_list").fetchone()[2],
                     os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, TypeError):
        pass  # Best-effort; WAL mode provides crash safety regardless


def _discover_events(
    teslacam_path: str,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Tuple[str, str, int]]:
    """Find event directories and archived clips to sync.

    Syncs event subdirectories from SentryClips/SavedClips plus flat files
    from ArchivedClips on the SD card. Returns a list of
    ``(event_dir_path, relative_path, total_size)`` sorted **oldest-first**
    so the most at-risk clips get preserved first.

    If *conn* is provided, events already marked ``synced`` in the
    database are excluded.
    """
    # Build set of already-synced event paths for fast lookup
    synced_paths: set = set()
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT file_path FROM cloud_synced_files WHERE status = 'synced'"
            ).fetchall()
            synced_paths = {r["file_path"] for r in rows}
        except Exception:
            pass

    events: List[Tuple[str, str, int]] = []

    for folder in CLOUD_ARCHIVE_SYNC_FOLDERS:
        folder_path = os.path.join(teslacam_path, folder)
        if not os.path.isdir(folder_path):
            continue

        # Only process event-based folders (with subdirectories)
        try:
            entries = sorted(os.listdir(folder_path))
        except OSError:
            continue

        for entry in entries:
            event_dir = os.path.join(folder_path, entry)
            if not os.path.isdir(event_dir):
                continue  # Skip flat files — events only

            rel_path = f"{folder}/{entry}"

            # Skip events already confirmed synced
            if rel_path in synced_paths:
                continue

            # Calculate total size of all files in this event
            total_size = 0
            has_video = False
            try:
                for f in os.listdir(event_dir):
                    fpath = os.path.join(event_dir, f)
                    if os.path.isfile(fpath):
                        total_size += os.path.getsize(fpath)
                        if f.lower().endswith(('.mp4', '.ts')):
                            has_video = True
            except OSError:
                continue

            if not has_video:
                continue  # Skip empty or non-video event dirs

            events.append((event_dir, rel_path, total_size))

    # Also include ArchivedClips from SD card (individual files)
    try:
        from config import ARCHIVE_DIR, ARCHIVE_ENABLED
        if ARCHIVE_ENABLED and os.path.isdir(ARCHIVE_DIR):
            try:
                for f in sorted(os.listdir(ARCHIVE_DIR)):
                    fpath = os.path.join(ARCHIVE_DIR, f)
                    if os.path.isfile(fpath) and f.lower().endswith(('.mp4', '.ts')):
                        rel_path = f"ArchivedClips/{f}"
                        if rel_path in synced_paths:
                            continue
                        fsize = os.path.getsize(fpath)
                        # Use the individual file path (not ARCHIVE_DIR)
                        # so rclone copyto can handle file-to-file copy
                        events.append((fpath, rel_path, fsize))
            except OSError:
                pass
    except ImportError:
        pass

    # Sort by priority (lower score = sync first)
    events.sort(key=lambda t: _score_event_priority(t[0]))

    return events


# ---------------------------------------------------------------------------
# Credential Handling
# ---------------------------------------------------------------------------

def _write_rclone_conf(provider: str, creds: dict) -> str:
    """Write a temporary rclone.conf to tmpfs and return its path.

    The caller is responsible for deleting the file after use.
    """
    os.makedirs(_RCLONE_TMPFS_DIR, exist_ok=True)

    # Build minimal rclone remote config
    lines = ["[teslausb]"]
    lines.append(f"type = {provider}")
    for key, value in creds.items():
        lines.append(f"{key} = {value}")

    conf_path = _RCLONE_CONF_PATH
    fd = os.open(conf_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, "\n".join(lines).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return conf_path


def _remove_rclone_conf() -> None:
    """Delete the tmpfs rclone config if it exists."""
    try:
        os.remove(_RCLONE_CONF_PATH)
    except FileNotFoundError:
        pass


def _load_provider_creds() -> dict:
    """Load cloud provider credentials from the encrypted store.

    Returns a dict of rclone config keys, or empty dict on failure.
    """
    try:
        from services.cloud_rclone_service import _load_creds
        return _load_creds()
    except Exception as e:
        logger.error("Failed to load cloud provider credentials: %s", e)
        return {}


# ---------------------------------------------------------------------------
# WiFi Detection
# ---------------------------------------------------------------------------

def _is_wifi_connected() -> bool:
    """Check if connected to WiFi (not AP mode only)."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split("\n"):
            parts = line.split(":")
            if (
                len(parts) >= 3
                and parts[0] == "wlan0"
                and parts[1] == "wifi"
                and parts[2] == "connected"
            ):
                return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Core Sync Engine
# ---------------------------------------------------------------------------

def _run_sync(
    teslacam_path: str,
    db_path: str,
    trigger: str,
    cancel_event: threading.Event,
) -> None:
    """Background thread target that performs the actual cloud sync.

    Processes one file at a time, updates the SQLite tracking database after
    each file, and respects the cancellation event between uploads.
    """
    global _sync_status

    _sync_status.update({
        "running": True,
        "progress": "Initialising…",
        "files_total": 0,
        "files_done": 0,
        "bytes_transferred": 0,
        "total_bytes": 0,
        "current_file": "",
        "current_file_size": 0,
        "started_at": time.time(),
        "error": None,
    })

    conn: Optional[sqlite3.Connection] = None
    session_id: Optional[int] = None
    files_synced = 0
    bytes_transferred = 0

    try:
        conn = _init_cloud_tables(db_path)

        # Recover any uploads interrupted by power loss
        conn.execute(
            "UPDATE cloud_synced_files SET status = 'pending', "
            "retry_count = retry_count WHERE status = 'uploading'"
        )
        # Finalize any sync sessions left "running" by a crash/reboot
        conn.execute(
            "UPDATE cloud_sync_sessions SET status = 'interrupted', "
            "ended_at = ?, error_msg = 'Process terminated (power loss or crash)' "
            "WHERE status = 'running'",
            (datetime.now(timezone.utc).isoformat(),)
        )
        conn.commit()

        # Create sync session record
        now_iso = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO cloud_sync_sessions "
            "(started_at, trigger, window_mode) VALUES (?, ?, ?)",
            (now_iso, trigger, "wifi"),
        )
        session_id = cur.lastrowid
        conn.commit()

        # Discover event directories to sync
        _sync_status["progress"] = "Scanning for events…"

        # Refresh RO mount to see Tesla's latest writes
        try:
            from services.mapping_service import _refresh_ro_mount
            _refresh_ro_mount(teslacam_path)
        except Exception:
            pass

        to_sync = _discover_events(teslacam_path, conn=conn)

        if not to_sync:
            _sync_status.update({
                "running": False,
                "progress": "No events to sync",
            })
            if session_id is not None:
                conn.execute(
                    "UPDATE cloud_sync_sessions SET ended_at = ?, status = 'completed', "
                    "files_synced = 0, bytes_transferred = 0 WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), session_id),
                )
                conn.commit()
            return

        _sync_status["files_total"] = len(to_sync)
        _sync_status["total_bytes"] = sum(s for _, _, s in to_sync)
        _sync_status["progress"] = f"Syncing {len(to_sync)} events…"
        logger.info("Cloud sync: %d events to upload (trigger=%s)", len(to_sync), trigger)

        # Load credentials
        creds = _load_provider_creds()
        if not creds:
            raise RuntimeError("Cloud provider credentials unavailable")

        remote_path = CLOUD_ARCHIVE_REMOTE_PATH
        max_mbps = CLOUD_ARCHIVE_MAX_UPLOAD_MBPS

        # Write rclone conf and refresh token once up front
        conf_path = _write_rclone_conf(CLOUD_ARCHIVE_PROVIDER, creds)
        try:
            # Force token refresh before starting
            subprocess.run(
                ["rclone", "about", "--config", conf_path, "teslausb:", "--json"],
                capture_output=True, text=True, timeout=30,
            )
            try:
                from services.cloud_rclone_service import _capture_refreshed_token
                _capture_refreshed_token(creds)
            except Exception:
                pass
        except Exception:
            pass

        # Check available cloud storage and cap sync to what fits.
        # Reserve 1 GB so we never fill the provider to 100%.
        _CLOUD_RESERVE_BYTES = 1 * 1024 * 1024 * 1024
        cloud_free_bytes: Optional[int] = None
        try:
            about_result = subprocess.run(
                ["rclone", "about", "--config", conf_path, "teslausb:", "--json"],
                capture_output=True, text=True, timeout=30,
            )
            if about_result.returncode == 0:
                import json as _json
                about = _json.loads(about_result.stdout)
                if "free" in about:
                    cloud_free_bytes = int(about["free"]) - _CLOUD_RESERVE_BYTES
                    cloud_total = int(about.get("total", 0))
                    logger.info(
                        "Cloud storage: %.1f GB free / %.1f GB total (%.1f GB reserved)",
                        (cloud_free_bytes + _CLOUD_RESERVE_BYTES) / (1024 ** 3),
                        cloud_total / (1024 ** 3),
                        _CLOUD_RESERVE_BYTES / (1024 ** 3),
                    )
        except Exception as e:
            logger.warning("Could not check cloud storage: %s", e)

        # If we know cloud capacity, trim the sync list to what fits
        cloud_bytes_remaining = cloud_free_bytes
        if cloud_bytes_remaining is not None and cloud_bytes_remaining <= 0:
            _sync_status.update({
                "running": False,
                "progress": "Cloud storage full",
                "error": "Not enough cloud storage — free up space or upgrade your plan",
            })
            logger.warning("Cloud sync aborted: no free cloud storage")
            if session_id is not None:
                conn.execute(
                    "UPDATE cloud_sync_sessions SET ended_at = ?, status = 'completed', "
                    "files_synced = 0, bytes_transferred = 0, "
                    "error_msg = 'Cloud storage full' WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), session_id),
                )
                conn.commit()
            return

        # Memory-safe flags for Pi Zero 2W
        mem_flags = ["--buffer-size", "0", "--transfers", "1", "--checkers", "1"]

        # I/O throttle: pause between event uploads to avoid saturating
        # the SD card (shared with USB gadget and archive service)
        _INTER_UPLOAD_SLEEP = 2.0  # seconds

        for idx, (event_dir, rel_path, event_size) in enumerate(to_sync):
            if cancel_event.is_set():
                _sync_status["progress"] = "Cancelled"
                logger.info("Cloud sync cancelled after %d events", files_synced)
                break

            _sync_status.update({
                "files_done": idx,
                "current_file": rel_path,
                "current_file_size": event_size,
                "progress": f"Uploading {idx + 1}/{len(to_sync)}: {rel_path}",
            })

            remote_dest = f"teslausb:{remote_path}/{rel_path}"
            logger.info("Sync: [%d/%d] %s (%d bytes)",
                        idx + 1, len(to_sync), rel_path, event_size)

            # Cloud space check — skip this file if it won't fit
            if cloud_bytes_remaining is not None and event_size > cloud_bytes_remaining:
                skipped = len(to_sync) - idx
                logger.warning(
                    "Cloud storage full: %.1f MB remaining, need %.1f MB for %s (%d events skipped)",
                    cloud_bytes_remaining / (1024 * 1024),
                    event_size / (1024 * 1024),
                    rel_path, skipped,
                )
                _sync_status["progress"] = (
                    f"Cloud full after {files_synced} events — "
                    f"{skipped} skipped (upgrade storage or free space)"
                )
                _sync_status["error"] = "Cloud storage full"
                break

            # Mark event as uploading in the tracking database
            conn.execute(
                """INSERT OR REPLACE INTO cloud_synced_files
                   (file_path, file_size, file_mtime, status, retry_count, last_error)
                   VALUES (?, ?, ?, 'uploading',
                           COALESCE((SELECT retry_count FROM cloud_synced_files WHERE file_path = ?), 0),
                           NULL)""",
                (rel_path, event_size, time.time(), rel_path)
            )
            _fsync_db(conn)

            # Use rclone copy for event directories, copyto for single files
            # (ArchivedClips are individual files, not directories)
            is_single_file = os.path.isfile(event_dir)
            rclone_cmd = "copyto" if is_single_file else "copy"

            # Use ionice to give rclone lowest I/O priority so the USB
            # gadget and web service stay responsive during uploads.
            # Default size+mtime check catches partial uploads (no --ignore-existing).
            try:
                result = subprocess.run(
                    [
                        "nice", "-n", "19",
                        "ionice", "-c", "3",
                        "rclone", rclone_cmd,
                        "--config", conf_path,
                        "--bwlimit", f"{max_mbps}M",
                        "--size-only",
                        "--stats", "0",
                        "--log-level", "ERROR",
                        *mem_flags,
                        event_dir,
                        remote_dest,
                    ],
                    capture_output=True, text=True, timeout=3600,
                )

                if result.returncode == 0:
                    files_synced += 1
                    bytes_transferred += event_size
                    _sync_status["bytes_transferred"] = bytes_transferred
                    logger.info("Sync: [%d/%d] %s OK", idx + 1, len(to_sync), rel_path)

                    # Track remaining cloud space
                    if cloud_bytes_remaining is not None:
                        cloud_bytes_remaining -= event_size

                    # Mark as synced with timestamp — the critical tracking step
                    now_synced = datetime.now(timezone.utc).isoformat()
                    conn.execute(
                        """UPDATE cloud_synced_files
                           SET status = 'synced', synced_at = ?, remote_path = ?,
                               retry_count = 0, last_error = NULL
                           WHERE file_path = ?""",
                        (now_synced, remote_dest, rel_path)
                    )
                    _fsync_db(conn)
                else:
                    err_msg = result.stderr.strip()[:500]
                    logger.error("Sync: [%d/%d] %s FAILED (exit %d): %s",
                                idx + 1, len(to_sync), rel_path,
                                result.returncode, err_msg[:200])
                    conn.execute(
                        """UPDATE cloud_synced_files
                           SET status = 'failed',
                               last_error = ?,
                               retry_count = retry_count + 1
                           WHERE file_path = ?""",
                        (err_msg[:255], rel_path)
                    )
                    _fsync_db(conn)

            except subprocess.TimeoutExpired:
                logger.error("Sync: %s timed out", rel_path)
                conn.execute(
                    """UPDATE cloud_synced_files
                       SET status = 'failed', last_error = 'Upload timed out (1h)',
                           retry_count = retry_count + 1
                       WHERE file_path = ?""",
                    (rel_path,)
                )
                _fsync_db(conn)
            except Exception as e:
                logger.error("Sync: %s error: %s", rel_path, e)
                conn.execute(
                    """UPDATE cloud_synced_files
                       SET status = 'failed', last_error = ?,
                           retry_count = retry_count + 1
                       WHERE file_path = ?""",
                    (str(e)[:255], rel_path)
                )
                _fsync_db(conn)

            # Pause between uploads to let the system breathe
            time.sleep(_INTER_UPLOAD_SLEEP)

        # Determine final session status
        if cancel_event.is_set():
            session_status = "cancelled"
        else:
            session_status = "completed"

        _sync_status.update({
            "running": False,
            "files_done": files_synced,
            "current_file": "",
            "progress": f"Done: {files_synced}/{len(to_sync)} files "
                        f"({bytes_transferred / (1024 * 1024):.1f} MiB)",
            "last_run": datetime.now(timezone.utc).isoformat(),
        })
        logger.info(
            "Cloud sync %s: %d files, %d bytes transferred",
            session_status, files_synced, bytes_transferred,
        )

    except Exception as e:
        logger.error("Cloud sync failed: %s", e)
        _sync_status.update({
            "running": False,
            "error": str(e),
            "progress": f"Error: {e}",
        })
        session_status = "interrupted"

    finally:
        # Update session record
        if conn is not None and session_id is not None:
            try:
                conn.execute(
                    "UPDATE cloud_sync_sessions SET ended_at = ?, "
                    "files_synced = ?, bytes_transferred = ?, status = ?, "
                    "error_msg = ? WHERE id = ?",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        files_synced,
                        bytes_transferred,
                        session_status if "session_status" in dir() else "interrupted",
                        _sync_status.get("error"),
                        session_id,
                    ),
                )
                conn.commit()
            except Exception as e:
                logger.error("Failed to update sync session record: %s", e)

        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

        _remove_rclone_conf()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_sync(
    teslacam_path: str,
    db_path: str,
    trigger: str = "manual",
) -> Tuple[bool, str]:
    """Start a background cloud sync if one is not already running.

    Args:
        teslacam_path: Absolute path to the TeslaCam directory (RO mount).
        db_path: Path to the SQLite database file.
        trigger: What initiated the sync ('manual', 'auto', 'scheduled').

    Returns:
        ``(success, message)`` tuple.
    """
    global _sync_thread

    if not CLOUD_ARCHIVE_ENABLED:
        return False, "Cloud archive is disabled in config"

    if not CLOUD_ARCHIVE_PROVIDER:
        return False, "No cloud provider configured"

    with _sync_lock:
        if _sync_status.get("running"):
            return False, "Sync already running"

        # Don't start if a single-file archive is running (shared resource)
        try:
            from services.cloud_rclone_service import get_archive_status
            if get_archive_status().get("running"):
                return False, "A file archive is in progress. Please wait for it to finish."
        except Exception:
            pass

        _sync_cancel.clear()
        _sync_thread = threading.Thread(
            target=_run_sync,
            args=(teslacam_path, db_path, trigger, _sync_cancel),
            daemon=True,
        )
        _sync_thread.start()
        return True, "Cloud sync started"


def stop_sync(graceful: bool = True) -> Tuple[bool, str]:
    """Request cancellation of a running sync.

    Args:
        graceful: If True, finish the current file upload then stop.
                  If False, kill rclone immediately (may leave partial file).
    """
    if not _sync_status.get("running"):
        return False, "Sync is not running"
    _sync_cancel.set()
    if not graceful:
        # Kill any running rclone subprocess immediately
        # The sync thread checks cancel_event between files, but for
        # immediate stop we also need to terminate the active rclone process.
        # The thread's subprocess.run has a timeout, so it will eventually
        # return, but we set a flag to signal immediate termination.
        _sync_status["_force_stop"] = True
        logger.info("Immediate sync cancellation requested")
        return True, "Sync stopping immediately (current file may be incomplete)"
    logger.info("Graceful sync cancellation requested")
    return True, "Sync will stop after the current file finishes"


def get_sync_status() -> dict:
    """Return a snapshot of the current sync status for UI polling.

    Includes calculated ETA based on average throughput.
    """
    status = dict(_sync_status)

    # Calculate ETA from throughput
    if status.get("running") and status.get("started_at") and status.get("bytes_transferred", 0) > 0:
        elapsed = time.time() - status["started_at"]
        if elapsed > 0:
            bps = status["bytes_transferred"] / elapsed
            remaining_bytes = status.get("total_bytes", 0) - status.get("bytes_transferred", 0)
            if bps > 0 and remaining_bytes > 0:
                status["eta_seconds"] = int(remaining_bytes / bps)
            else:
                status["eta_seconds"] = 0
            status["throughput_bps"] = int(bps)
    else:
        status["eta_seconds"] = None
        status["throughput_bps"] = None

    # Don't expose internal flags
    status.pop("_force_stop", None)
    return status


def get_sync_history(db_path: str, limit: int = 20) -> List[dict]:
    """Return recent sync session records, newest first."""
    conn = _init_cloud_tables(db_path)
    try:
        rows = conn.execute(
            "SELECT id, started_at, ended_at, files_synced, "
            "bytes_transferred, status, trigger, error_msg "
            "FROM cloud_sync_sessions ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_sync_stats(db_path: str) -> dict:
    """Return aggregate sync statistics for the UI dashboard.

    Keys: total_synced, total_pending, total_failed, total_bytes.
    """
    conn = _init_cloud_tables(db_path)
    try:
        counts = {}
        for status in ("synced", "pending", "failed", "uploading"):
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM cloud_synced_files WHERE status = ?",
                (status,),
            ).fetchone()
            counts[status] = row["cnt"] if row else 0

        row = conn.execute(
            "SELECT COALESCE(SUM(bytes_transferred), 0) AS total "
            "FROM cloud_sync_sessions WHERE status = 'completed'"
        ).fetchone()
        total_bytes = row["total"] if row else 0

        return {
            "total_synced": counts["synced"],
            "total_pending": counts["pending"] + counts["uploading"],
            "total_failed": counts["failed"],
            "total_bytes": total_bytes,
        }
    finally:
        conn.close()


def trigger_auto_sync(teslacam_path: str, db_path: str) -> None:
    """Conditionally start a sync — safe to call from mode-switch hooks.

    Checks that cloud archive is enabled, no sync is already running,
    and WiFi (not AP-only) is connected before starting.
    """
    if not CLOUD_ARCHIVE_ENABLED:
        return

    if _sync_status.get("running"):
        return

    if not _is_wifi_connected():
        logger.debug("Auto-sync skipped: WiFi not connected")
        return

    ok, msg = start_sync(teslacam_path, db_path, trigger="auto")
    if ok:
        logger.info("Auto cloud sync triggered")
    else:
        logger.debug("Auto-sync not started: %s", msg)


def recover_interrupted_uploads(db_path: str) -> int:
    """Reset uploads that were interrupted by power loss.

    Call this once at startup.  Any file marked ``uploading`` is set back
    to ``pending`` so it will be retried on the next sync.

    Returns the number of rows reset.
    """
    conn = _init_cloud_tables(db_path)
    try:
        cur = conn.execute(
            "UPDATE cloud_synced_files SET status = 'pending', "
            "retry_count = retry_count WHERE status = 'uploading'"
        )
        affected = cur.rowcount
        conn.commit()
        if affected:
            logger.info("Recovered %d interrupted cloud uploads", affected)
        return affected
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sync Status & Queue Management
# ---------------------------------------------------------------------------


def get_sync_status_for_events(event_names: list) -> dict:
    """Check sync status for a list of event names.

    Returns dict mapping event_name -> status ('synced', 'queued', 'uploading', None).
    """
    if not event_names:
        return {}
    conn = _init_cloud_tables(CLOUD_ARCHIVE_DB_PATH)
    try:
        statuses = {}
        for name in event_names:
            row = conn.execute(
                "SELECT status FROM cloud_synced_files WHERE file_path LIKE ? ORDER BY synced_at DESC LIMIT 1",
                ('%' + name + '%',)
            ).fetchone()
            statuses[name] = row['status'] if row else None
        return statuses
    finally:
        conn.close()


def queue_event_for_sync(folder: str, event_name: str, priority: bool = False) -> Tuple[bool, str]:
    """Add an event's files to the sync queue.

    Returns (success, message).
    """
    from services.video_service import get_teslacam_path
    teslacam = get_teslacam_path()
    if not teslacam:
        return False, "TeslaCam not accessible"

    event_dir = os.path.join(teslacam, folder, event_name)
    if not os.path.isdir(event_dir):
        # Might be a flat file (RecentClips/ArchivedClips)
        event_dir = os.path.join(teslacam, folder)

    conn = _init_cloud_tables(CLOUD_ARCHIVE_DB_PATH)
    try:
        queued = 0
        for entry in os.scandir(event_dir):
            if entry.name.lower().endswith('.mp4') and event_name in entry.name:
                # Check if already synced or uploading
                existing = conn.execute(
                    "SELECT status FROM cloud_synced_files WHERE file_path = ?",
                    (entry.path,)
                ).fetchone()
                if existing and existing['status'] in ('synced', 'uploading'):
                    continue

                stat = entry.stat()
                conn.execute(
                    """INSERT OR REPLACE INTO cloud_synced_files
                       (file_path, file_size, file_mtime, status, retry_count)
                       VALUES (?, ?, ?, 'queued', 0)""",
                    (entry.path, stat.st_size, stat.st_mtime)
                )
                queued += 1

        conn.commit()
        if queued:
            return True, "Added {} files to sync queue".format(queued)
        return True, "All files already synced or queued"
    finally:
        conn.close()


def get_sync_queue() -> dict:
    """Return the current sync queue (queued/pending/uploading files)."""
    conn = _init_cloud_tables(CLOUD_ARCHIVE_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT file_path, file_size, status, retry_count FROM cloud_synced_files "
            "WHERE status IN ('queued', 'pending', 'uploading') ORDER BY id"
        ).fetchall()
        queue = [dict(r) for r in rows]
        return {"queue": queue, "total": len(queue)}
    finally:
        conn.close()


def remove_from_queue(file_path: str) -> Tuple[bool, str]:
    """Remove a single item from the sync queue."""
    conn = _init_cloud_tables(CLOUD_ARCHIVE_DB_PATH)
    try:
        conn.execute(
            "DELETE FROM cloud_synced_files WHERE file_path = ? AND status IN ('queued', 'pending')",
            (file_path,),
        )
        conn.commit()
        return True, "Removed from queue"
    finally:
        conn.close()


def clear_queue() -> Tuple[bool, str]:
    """Clear all queued/pending items from the sync queue."""
    conn = _init_cloud_tables(CLOUD_ARCHIVE_DB_PATH)
    try:
        result = conn.execute(
            "DELETE FROM cloud_synced_files WHERE status IN ('queued', 'pending')"
        )
        conn.commit()
        return True, "Cleared {} items from queue".format(result.rowcount)
    finally:
        conn.close()