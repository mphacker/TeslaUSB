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
    CLOUD_ARCHIVE_KEEP_LOCAL,
    CLOUD_ARCHIVE_DB_PATH,
    CLOUD_PROVIDER_CREDS_PATH,
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
    "current_file": "",
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
# File Discovery
# ---------------------------------------------------------------------------

def _discover_files(
    teslacam_path: str,
    db_path: str,
) -> List[Tuple[str, int, float]]:
    """Scan TeslaCam folders and return files not yet synced or changed.

    Returns a list of ``(absolute_path, file_size, file_mtime)`` tuples
    sorted by priority order then newest-first within each folder.
    """
    conn = _init_cloud_tables(db_path)
    try:
        results_by_folder: Dict[str, List[Tuple[str, int, float]]] = {
            f: [] for f in CLOUD_ARCHIVE_PRIORITY_ORDER
        }

        for folder in CLOUD_ARCHIVE_SYNC_FOLDERS:
            folder_path = os.path.join(teslacam_path, folder)
            if not os.path.isdir(folder_path):
                continue

            for dirpath, _dirs, filenames in os.walk(folder_path):
                for fname in filenames:
                    fpath = os.path.join(dirpath, fname)
                    try:
                        stat = os.stat(fpath)
                    except OSError:
                        continue

                    row = conn.execute(
                        "SELECT file_size, file_mtime, status "
                        "FROM cloud_synced_files WHERE file_path = ?",
                        (fpath,),
                    ).fetchone()

                    if row:
                        # Already synced and unchanged → skip
                        if (
                            row["status"] == "synced"
                            and row["file_size"] == stat.st_size
                            and row["file_mtime"] == stat.st_mtime
                        ):
                            continue
                        # Failed too many times → skip (max 5 retries)
                        if row["status"] == "failed" and row["retry_count"] >= 5:
                            continue

                    bucket = folder if folder in results_by_folder else None
                    if bucket is None:
                        # Folder not in priority list — append at end
                        results_by_folder.setdefault(folder, [])
                        bucket = folder
                    results_by_folder[bucket].append(
                        (fpath, stat.st_size, stat.st_mtime)
                    )

        # Sort each folder newest-first, then concatenate by priority order
        ordered: List[Tuple[str, int, float]] = []
        for folder in CLOUD_ARCHIVE_PRIORITY_ORDER:
            items = results_by_folder.get(folder, [])
            items.sort(key=lambda t: t[2], reverse=True)
            ordered.extend(items)

        # Append remaining folders not in priority list
        for folder, items in results_by_folder.items():
            if folder not in CLOUD_ARCHIVE_PRIORITY_ORDER:
                items.sort(key=lambda t: t[2], reverse=True)
                ordered.extend(items)

        return ordered
    finally:
        conn.close()


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
    if not os.path.isfile(CLOUD_PROVIDER_CREDS_PATH):
        logger.warning("Cloud provider credentials file not found: %s",
                        CLOUD_PROVIDER_CREDS_PATH)
        return {}

    try:
        # Lazy import to avoid circular dependency and startup cost
        import json
        from services.tesla_api_service import decrypt_credentials
        raw = decrypt_credentials(CLOUD_PROVIDER_CREDS_PATH)
        if isinstance(raw, dict):
            return raw
        return json.loads(raw) if raw else {}
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
        "current_file": "",
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

        # Discover files to upload
        _sync_status["progress"] = "Scanning for new files…"
        to_sync = _discover_files(teslacam_path, db_path)

        if not to_sync:
            _sync_status.update({
                "running": False,
                "progress": "No new files to sync",
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
        _sync_status["progress"] = f"Syncing {len(to_sync)} files…"
        logger.info("Cloud sync: %d files to upload (trigger=%s)", len(to_sync), trigger)

        # Load credentials and write rclone.conf once up front to validate
        creds = _load_provider_creds()
        if not creds:
            raise RuntimeError("Cloud provider credentials unavailable")

        provider = CLOUD_ARCHIVE_PROVIDER
        remote_path = CLOUD_ARCHIVE_REMOTE_PATH
        max_mbps = CLOUD_ARCHIVE_MAX_UPLOAD_MBPS

        for idx, (fpath, fsize, fmtime) in enumerate(to_sync):
            if cancel_event.is_set():
                _sync_status["progress"] = "Cancelled"
                logger.info("Cloud sync cancelled after %d files", files_synced)
                break

            rel = os.path.relpath(fpath, teslacam_path)
            _sync_status.update({
                "files_done": idx,
                "current_file": rel,
                "progress": f"Uploading {idx + 1}/{len(to_sync)}: {rel}",
            })

            # Upsert file record as 'uploading'
            conn.execute(
                "INSERT INTO cloud_synced_files "
                "(file_path, file_size, file_mtime, remote_path, status) "
                "VALUES (?, ?, ?, ?, 'uploading') "
                "ON CONFLICT(file_path) DO UPDATE SET "
                "status = 'uploading', file_size = excluded.file_size, "
                "file_mtime = excluded.file_mtime, "
                "remote_path = excluded.remote_path",
                (fpath, fsize, fmtime, f"{remote_path}/{rel}"),
            )
            conn.commit()

            # Write temporary rclone.conf
            conf_path = _write_rclone_conf(provider, creds)
            try:
                remote_dest = f"teslausb:{remote_path}/{rel}"
                cmd = [
                    "rclone", "copyto",
                    "--config", conf_path,
                    "--bwlimit", f"{max_mbps}M",
                    "--stats", "0",
                    "--log-level", "ERROR",
                    fpath,
                    remote_dest,
                ]
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=3600,
                )

                if result.returncode == 0:
                    conn.execute(
                        "UPDATE cloud_synced_files SET status = 'synced', "
                        "synced_at = ?, last_error = NULL WHERE file_path = ?",
                        (datetime.now(timezone.utc).isoformat(), fpath),
                    )
                    conn.commit()
                    # WAL checkpoint to persist data against power loss
                    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

                    files_synced += 1
                    bytes_transferred += fsize
                    _sync_status["bytes_transferred"] = bytes_transferred
                else:
                    err_msg = result.stderr.strip()[:500] if result.stderr else "Unknown rclone error"
                    logger.error("rclone failed for %s (exit %d): %s",
                                 rel, result.returncode, err_msg)
                    conn.execute(
                        "UPDATE cloud_synced_files SET status = 'failed', "
                        "retry_count = retry_count + 1, last_error = ? "
                        "WHERE file_path = ?",
                        (err_msg, fpath),
                    )
                    conn.commit()

            except subprocess.TimeoutExpired:
                logger.error("rclone timed out for %s", rel)
                conn.execute(
                    "UPDATE cloud_synced_files SET status = 'failed', "
                    "retry_count = retry_count + 1, last_error = 'Upload timed out (1h)' "
                    "WHERE file_path = ?",
                    (fpath,),
                )
                conn.commit()

            except Exception as e:
                logger.error("Upload error for %s: %s", rel, e)
                conn.execute(
                    "UPDATE cloud_synced_files SET status = 'failed', "
                    "retry_count = retry_count + 1, last_error = ? "
                    "WHERE file_path = ?",
                    (str(e)[:500], fpath),
                )
                conn.commit()

            finally:
                _remove_rclone_conf()

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

        _sync_cancel.clear()
        _sync_thread = threading.Thread(
            target=_run_sync,
            args=(teslacam_path, db_path, trigger, _sync_cancel),
            daemon=True,
        )
        _sync_thread.start()
        return True, "Cloud sync started"


def stop_sync() -> Tuple[bool, str]:
    """Request graceful cancellation of a running sync."""
    if not _sync_status.get("running"):
        return False, "Sync is not running"
    _sync_cancel.set()
    return True, "Cancellation requested"


def get_sync_status() -> dict:
    """Return a snapshot of the current sync status for UI polling."""
    return dict(_sync_status)


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
