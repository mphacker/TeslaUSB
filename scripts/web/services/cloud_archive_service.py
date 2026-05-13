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
import posixpath
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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
    CLOUD_ARCHIVE_RESERVE_GB,
    CLOUD_ARCHIVE_RETRY_MAX_ATTEMPTS,
)

# Phase 2.6 — clamp range for ``cloud_archive.retry_max_attempts``. The
# Settings UI restricts input to 1-20; reads outside that range fall back
# to the import-time default rather than silently disabling the cap (0)
# or wasting bandwidth on unbounded retries (huge values).
_RETRY_MAX_ATTEMPTS_MIN = 1
_RETRY_MAX_ATTEMPTS_MAX = 20

# Phase 2.7 — cloud-path canonicalization. The cloud_synced_files table's
# ``file_path`` column was historically populated by several call sites
# with inconsistent forms: relative POSIX (``ArchivedClips/foo.mp4``,
# ``SentryClips/<event>``) from the bulk worker, but absolute filesystem
# paths from ``queue_event_for_sync`` (which used ``os.scandir().path``).
# The mix made dedup checks unreliable, broke ``WHERE file_path = ?``
# lookups across writers, and produced corrupt rows like
# ``ArchivedClips/foo.mp4/`` (trailing slash from a stray ``rclone lsf``
# response). The schema version is bumped to 2 and a one-shot migration
# rewrites every row to canonical relative form. New writes go through
# ``canonical_cloud_path`` so this can never regress.
_KNOWN_CLOUD_ROOTS = ("ArchivedClips", "RecentClips", "SentryClips",
                      "SavedClips", "TeslaTrackMode")


def canonical_cloud_path(file_path: str) -> str:
    """Normalize a cloud-sync ``file_path`` to canonical relative form.

    The canonical form is a POSIX-style path **relative to one of the
    well-known TeslaCam folders** (``ArchivedClips``, ``RecentClips``,
    ``SentryClips``, ``SavedClips``, ``TeslaTrackMode``):

    * ``/`` separators only (Windows backslashes converted defensively).
    * No leading slash.
    * No trailing slash.
    * No ``//`` or ``./`` components.
    * **No ``..`` components** — these are rejected with ``ValueError``
      because they have no legitimate place in a cloud-sync row name and
      ``remove_from_queue`` is reachable from raw user input
      (``cloud_archive.py`` POST handler), so a ``..`` collapse via
      ``posixpath.normpath`` could be exploited to reference a row the
      user shouldn't be able to address.

    Absolute paths under any of those known roots have everything before
    the root segment stripped. Examples::

        /home/pi/ArchivedClips/2026-01-01-front.mp4
            -> ArchivedClips/2026-01-01-front.mp4
        /mnt/gadget/part1-ro/TeslaCam/SentryClips/2026-01-01_10-00-00
            -> SentryClips/2026-01-01_10-00-00
        ArchivedClips/foo.mp4/      (corrupt trailing slash)
            -> ArchivedClips/foo.mp4
        /home/pi/ArchivedClips//bar.mp4
            -> ArchivedClips/bar.mp4

    Paths that don't contain a known root segment have their leading /
    and trailing / stripped but are otherwise preserved (this should
    never happen for legitimate cloud-sync rows; treat such paths as
    suspect but don't drop them).

    Empty / falsy input is returned unchanged so callers can pass
    optional values without a guard.

    Raises:
        ValueError: if ``file_path`` contains a ``..`` segment.
    """
    if not file_path:
        return file_path
    p = file_path.replace('\\', '/')

    # Reject path-traversal attempts BEFORE any normalization. We check
    # for the literal '..' segment surrounded by separators (or at the
    # ends) so a basename like 'foo..bar.mp4' is allowed but
    # 'ArchivedClips/../foo' is not. Doing this before normpath() is
    # critical: posixpath.normpath('/x/../etc/passwd') silently
    # collapses to '/etc/passwd'.
    for seg in p.split('/'):
        if seg == '..':
            raise ValueError(
                f"Path traversal segment '..' is not permitted in "
                f"cloud_synced_files.file_path: {file_path!r}"
            )

    # Find a known root segment and strip everything before it. We use
    # find('/<root>/') so 'ArchivedClips' inside a basename doesn't
    # accidentally match (e.g. a hypothetical filename
    # 'someArchivedClipsthing.mp4' would not be split).
    stripped = None
    for root in _KNOWN_CLOUD_ROOTS:
        # Match 'X/<root>/' so we keep the root segment itself.
        marker = f"/{root}/"
        idx = p.find(marker)
        if idx >= 0:
            stripped = p[idx + 1:]  # +1 to drop the leading slash
            break
        # Or if the whole prefix IS the root (path begins with the root).
        if p == root or p.startswith(f"{root}/"):
            stripped = p
            break
    if stripped is not None:
        p = stripped

    # Normalize separators: collapse //, drop ./ components, strip
    # trailing slash. posixpath.normpath does all of this; it would
    # ALSO collapse '..' but we've already rejected those above, so
    # there's no traversal risk from this call.
    p = posixpath.normpath(p)

    if p == '.':
        return ''
    # Strip leading slashes (defensive — normpath leaves a single one).
    while p.startswith('/'):
        p = p[1:]
    return p


# ---------------------------------------------------------------------------
# Database Schema & Versioning
# ---------------------------------------------------------------------------

_CLOUD_MODULE = "cloud_archive"
_CLOUD_SCHEMA_VERSION = 2

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

# Phase 3b (#99) — continuous worker model
# ---------------------------------------
# The cloud sync used to be a one-shot: every trigger (timer tick, NM
# dispatcher fire, manual UI button, mode switch) spawned a new daemon
# thread that ran ``_run_sync`` to completion and exited. That pattern
# had three structural problems:
#
# 1. Newly-archived clips couldn't upload until the *next* trigger.
#    Inotify saw the file, the indexer caught up — but cloud sync only
#    woke on the 24h safety timer or on a WiFi reconnect event.
# 2. The LES priority contract depended on ``trigger_auto_sync`` checking
#    ``has_ready_live_event_work`` BEFORE starting. Once the bulk sync
#    was running, the LES yield-between-files path was the only safety
#    net — fine, but stripping the start-time check off the timer
#    means LES has no head start.
# 3. Status was scattered across "sync running?" (in ``_sync_status``)
#    and "thread alive?" (in ``_sync_thread``) — two different sources
#    of truth that periodically disagreed.
#
# Replaced with the LES pattern: a single long-lived worker thread that
# blocks on ``_wake.wait(timeout=N)`` when idle (~0.1 % CPU baseline)
# and drains the queue when poked. Producers (file watcher, NM
# dispatcher, manual UI, mode switch) call ``wake()`` instead of
# ``start_sync()``; the worker is always there waiting. ``start_sync``
# remains as a backward-compat alias that ensures the worker is alive
# and pokes it.
_sync_thread: Optional[threading.Thread] = None  # legacy alias for _worker_thread
_worker_thread: Optional[threading.Thread] = None
_worker_lock = threading.Lock()
_worker_stop = threading.Event()
_wake = threading.Event()

# In-flight drain cancellation, separate from worker shutdown.
# ``stop_sync()`` sets this to interrupt the current upload pass;
# ``_worker_loop`` clears it after the drain returns so the worker
# stays alive and will respond to the next ``wake()``. ONLY ``stop()``
# (terminal worker shutdown) sets ``_worker_stop`` — confusing the two
# would kill the worker on every "Stop Sync" UI click and silently
# drop subsequent file-watcher / NM dispatcher / mode-switch wakes.
_drain_cancel = threading.Event()

# Idle wait between drain attempts when the worker has nothing to do.
# We wake on ``_wake.set()`` (file watcher, NM dispatcher, mode switch,
# manual UI) so this just sets the maximum latency between an unobserved
# state change (e.g. WiFi came back without a dispatcher event) and a
# fresh queue check. Five minutes matches the old timer's lower bound
# and is well below the SDIO load thresholds — a wake-on-empty drain is
# a few sub-ms DB queries.
_WAIT_WHEN_IDLE_SECONDS = 300.0

# When a drain bailed out because the task coordinator was busy or WiFi
# was down, retry sooner so we don't sit on a backlog. Still honors the
# wake event — any producer can shortcut this.
_WAIT_WHEN_BUSY_SECONDS = 60.0


def _backoff_wait(timeout: float) -> bool:
    """Sleep up to ``timeout`` seconds, returning early on wake/stop.

    Returns True if either ``_wake`` or ``_worker_stop`` was set during
    the wait (caller should re-check state); False on natural timeout.

    IMPORTANT: does NOT clear ``_wake``. Only the loop-top
    ``_wake.wait()`` / ``_wake.clear()`` pair owns the wake event.
    Using this helper inside backoffs preserves a producer's wake so
    the next loop iteration drains promptly instead of discarding it.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if _worker_stop.is_set() or _wake.is_set():
            return True
        # Poll _wake every 0.25s so we can still see a stop signal
        # promptly in environments where _worker_stop is set during
        # the wait (we wouldn't be woken otherwise).
        if _wake.wait(timeout=min(0.25, remaining)):
            return True

_sync_lock = threading.Lock()  # legacy: kept for status-read snapshots
_sync_cancel = threading.Event()  # legacy: aliased to _worker_stop below
_sync_rclone_proc: Optional[subprocess.Popen] = None
_startup_recovery_done = False

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
    # Phase 3b — surface worker liveness so the UI can distinguish
    # "no worker" (configuration / startup failure) from "worker idle"
    # (queue empty, doing nothing). Both look the same in terms of
    # ``running: False`` but they need different UI affordances.
    "worker_running": False,
    "wake_count": 0,
    "drain_count": 0,
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


# ---------------------------------------------------------------------------
# Phase 2.7 v2 migration: canonicalize cloud_synced_files.file_path
# ---------------------------------------------------------------------------

# Status priority for merging two rows that collapse to the same canonical
# path. Higher value wins. ``synced`` always beats anything else (it's the
# only state that records a successful upload — losing it would make the
# bulk worker re-upload). ``dead_letter`` outranks ``failed`` because it
# represents a row that has already exhausted its automatic retries —
# demoting it back to ``failed`` would re-burn bandwidth on something
# the operator has implicitly given up on.
_MIGRATE_STATUS_PRIORITY = {
    'synced': 5,
    'dead_letter': 4,
    'failed': 3,
    'uploading': 2,
    'pending': 1,
    'queued': 0,
}


def _migrate_canonicalize_paths_v2(
    conn: sqlite3.Connection, db_path: str,
) -> Tuple[int, int]:
    """Rewrite all ``cloud_synced_files.file_path`` rows to canonical form.

    Returns ``(rewrites, merges)`` so the caller can log a summary.

    Strategy:
    1. Snapshot the DB to ``{db_path}.bak.v2-canonical-paths`` BEFORE
       any writes. Power-loss during the migration leaves both copies on
       disk; the operator can ``mv`` the .bak back without losing data.
    2. Walk every row, compute canonical form via
       :func:`canonical_cloud_path`.
    3. If the new path is identical to the old, skip.
    4. Otherwise attempt the UPDATE. On UNIQUE conflict (another row
       already has the canonical form), MERGE: keep the row with the
       higher status priority and delete the loser.

    The whole operation runs inside a single transaction so a crash
    leaves either the old form or the new form — never a half-migrated
    mix. SQLite holds the WAL until commit, so an incomplete commit on
    power-loss replays correctly on next open.
    """
    if not os.path.exists(db_path):
        # In-memory or about-to-be-created DB: nothing to migrate.
        return (0, 0)
    # Snapshot first.  shutil.copy2 preserves mtime so the operator
    # can see when the migration ran. Don't copy-2 over an existing
    # backup file (a re-attempted migration after a partial crash);
    # the FIRST snapshot is the source of truth.
    backup_path = f"{db_path}.bak.v2-canonical-paths"
    if not os.path.exists(backup_path):
        try:
            shutil.copy2(db_path, backup_path)
            # Best-effort: also copy WAL/SHM if they exist, so the
            # backup is a coherent snapshot.
            for suffix in ("-wal", "-shm"):
                src = db_path + suffix
                if os.path.exists(src):
                    shutil.copy2(src, backup_path + suffix)
            logger.info(
                "Cloud archive v2 migration: snapshotted DB to %s",
                backup_path,
            )
        except OSError as e:
            logger.warning(
                "Cloud archive v2 migration: backup to %s failed (%s); "
                "proceeding without snapshot",
                backup_path, e,
            )

    rewrites = 0
    merges = 0
    # Defensive: tests may pass a connection without row_factory set.
    # The production caller (_init_cloud_tables) always sets it, but we
    # don't want this routine to be picky about its connection state.
    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, file_path, status FROM cloud_synced_files"
        ).fetchall()
    finally:
        conn.row_factory = prior_factory

    for row in rows:
        new_path = canonical_cloud_path(row["file_path"])
        if not new_path or new_path == row["file_path"]:
            continue
        try:
            conn.execute(
                "UPDATE cloud_synced_files SET file_path = ? WHERE id = ?",
                (new_path, row["id"]),
            )
            rewrites += 1
        except sqlite3.IntegrityError:
            # Another row already holds the canonical form. Resolve by
            # status priority: keep the higher-ranked row, delete the
            # other. Re-run the canonical_cloud_path so we look up by
            # the same key the conflicting row was inserted with.
            conn.row_factory = sqlite3.Row
            try:
                existing = conn.execute(
                    "SELECT id, status FROM cloud_synced_files "
                    "WHERE file_path = ?",
                    (new_path,),
                ).fetchone()
            finally:
                conn.row_factory = prior_factory
            if existing is None:
                # Defensive: the conflict row vanished mid-migration
                # (parallel writer? shouldn't happen — service is
                # single-threaded for this DB). Try the update again.
                conn.execute(
                    "UPDATE cloud_synced_files SET file_path = ? WHERE id = ?",
                    (new_path, row["id"]),
                )
                rewrites += 1
                continue
            existing_pri = _MIGRATE_STATUS_PRIORITY.get(
                existing["status"], 0,
            )
            our_pri = _MIGRATE_STATUS_PRIORITY.get(row["status"], 0)
            if our_pri > existing_pri:
                # Promote ours: delete existing, retry the rename.
                conn.execute(
                    "DELETE FROM cloud_synced_files WHERE id = ?",
                    (existing["id"],),
                )
                conn.execute(
                    "UPDATE cloud_synced_files SET file_path = ? WHERE id = ?",
                    (new_path, row["id"]),
                )
            else:
                # Keep existing: drop our duplicate.
                conn.execute(
                    "DELETE FROM cloud_synced_files WHERE id = ?",
                    (row["id"],),
                )
            merges += 1
            logger.info(
                "Cloud archive v2 migration: merged duplicate row "
                "old=%r new=%r (kept status=%s)",
                row["file_path"], new_path,
                existing["status"] if our_pri <= existing_pri
                else row["status"],
            )

    if rewrites or merges:
        logger.info(
            "Cloud archive v2 migration: rewrote %d row(s), merged %d duplicate(s)",
            rewrites, merges,
        )
    return (rewrites, merges)


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

        # Phase 2.7 (v2) — canonicalize all cloud_synced_files.file_path
        # values. Mixed forms (relative POSIX from the bulk worker,
        # absolute from queue_event_for_sync, plus rare corrupt rows
        # like trailing-slash) made dedup unreliable across writers.
        # The migration is idempotent: rows already in canonical form
        # are skipped, and rows that collapse to the same canonical
        # path are merged keeping the higher-priority status (a synced
        # row beats a pending one).
        #
        # Atomicity: the migration's UPDATEs/DELETEs and the version
        # bump that follows MUST commit together. If the migration
        # raises, we ``conn.rollback()`` to undo any partial rewrites
        # and SKIP the version bump entirely so the migration retries
        # on the next process start. The previous behaviour (log and
        # fall through) left the DB with one rewritten row + the rest
        # legacy + version=2 — the migration would never run again and
        # the dedup contract would be silently broken.
        migration_ok = True
        if current < 2:
            try:
                _migrate_canonicalize_paths_v2(conn, db_path)
            except Exception as e:
                migration_ok = False
                try:
                    conn.rollback()
                except Exception:
                    pass
                logger.error(
                    "Cloud archive v2 migration failed (%s); rolled "
                    "back partial rewrites and leaving schema at v%d. "
                    "Migration will retry on next service start. New "
                    "writes will still be canonical.",
                    e, current,
                )

        if migration_ok:
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

    # On first DB access after process start, recover any sessions or
    # uploads left in a transient state by a crash or service restart.
    global _startup_recovery_done
    if not _startup_recovery_done:
        _startup_recovery_done = True
        try:
            n_sessions = conn.execute(
                "UPDATE cloud_sync_sessions SET status = 'interrupted', "
                "ended_at = ?, error_msg = 'Process restarted' "
                "WHERE status = 'running'",
                (datetime.now(timezone.utc).isoformat(),)
            ).rowcount
            n_uploads = conn.execute(
                "UPDATE cloud_synced_files SET status = 'pending', "
                "retry_count = retry_count WHERE status = 'uploading'"
            ).rowcount
            if n_sessions or n_uploads:
                conn.commit()
                logger.info(
                    "Startup recovery: %d stale sessions, %d interrupted uploads reset",
                    n_sessions, n_uploads,
                )
        except Exception as e:
            logger.warning("Startup recovery failed: %s", e)

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
                from services.mapping_queries import get_db_connection
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


def _read_sync_non_event_setting() -> bool:
    """Re-read ``cloud_archive.sync_non_event_videos`` from config.yaml.

    Phase 2.3 — ``config.CLOUD_ARCHIVE_SYNC_NON_EVENT`` is snapshotted at
    module-import time, and the Settings save handler at
    ``cloud_archive.py:_update_config_yaml`` only writes YAML; it does not
    mutate the config module attribute. Re-importing the symbol therefore
    returns the stale boot-time value and a Settings toggle has no effect
    until ``gadget_web.service`` restarts. To honour the documented
    ""effective on next sync iteration without restart"" contract we read
    the live YAML directly here.

    ``_discover_events`` runs at most once per sync iteration (minutes
    apart), so a single ~1ms YAML read is invisible to performance and
    avoids the heavier ``systemd-run`` restart pattern used by LES.

    On any IO/parse error we fall back to the import-time value so the
    picker never crashes the worker — matching the safe-default
    behaviour the rest of the service uses for config edge cases.
    """
    try:
        import yaml
        from config import CONFIG_YAML
        with open(CONFIG_YAML, 'r') as f:
            cfg = yaml.safe_load(f) or {}
        return bool(
            cfg.get('cloud_archive', {}).get('sync_non_event_videos', False)
        )
    except Exception:
        return CLOUD_ARCHIVE_SYNC_NON_EVENT


def _read_retry_max_attempts_setting() -> int:
    """Re-read ``cloud_archive.retry_max_attempts`` from config.yaml.

    Phase 2.6 — same per-call YAML re-read pattern as
    :func:`_read_sync_non_event_setting`. The Settings save handler
    only writes YAML; without this re-read, a Settings change would
    have no effect until ``gadget_web.service`` restarts.

    Range-clamped to ``_RETRY_MAX_ATTEMPTS_MIN`` ..
    ``_RETRY_MAX_ATTEMPTS_MAX`` so a hand-edited config.yaml with a
    nonsense value (0, negative, or absurdly large) cannot disable the
    cap entirely or cause a row to retry forever. The Settings UI
    enforces the same range via ``min``/``max`` attributes on the
    number input.

    Falls back to ``CLOUD_ARCHIVE_RETRY_MAX_ATTEMPTS`` (the import-time
    default) on any IO/parse error so the failure-handling code path
    never raises.
    """
    try:
        import yaml
        from config import CONFIG_YAML
        with open(CONFIG_YAML, 'r') as f:
            cfg = yaml.safe_load(f) or {}
        raw = cfg.get('cloud_archive', {}).get(
            'retry_max_attempts', CLOUD_ARCHIVE_RETRY_MAX_ATTEMPTS,
        )
        value = int(raw)
        if value < _RETRY_MAX_ATTEMPTS_MIN or value > _RETRY_MAX_ATTEMPTS_MAX:
            return CLOUD_ARCHIVE_RETRY_MAX_ATTEMPTS
        return value
    except Exception:
        return CLOUD_ARCHIVE_RETRY_MAX_ATTEMPTS


def _mark_upload_failure(
    conn: sqlite3.Connection, rel_path: str, err_msg: str,
) -> None:
    """Mark ``rel_path`` failed; promote to ``dead_letter`` when capped.

    Phase 2.6 — atomically increments ``retry_count`` and decides whether
    the row should remain ``'failed'`` (auto-retry on next sync iteration)
    or be promoted to ``'dead_letter'`` (excluded from auto-picking,
    requires manual recovery via Failed Jobs page in Phase 4).

    The cap is read fresh from config.yaml on every call so a Settings
    change takes effect on the next failure without restarting the
    service. The decision uses ``CASE`` inside the ``UPDATE`` so the cap
    check and ``retry_count`` increment happen in the same statement —
    no read-modify-write race window.

    A promotion is always logged at WARNING level so the operator can
    see in journalctl which files have been permanently abandoned by
    auto-sync. The previous (uncapped) behaviour silently retried
    every cycle forever.
    """
    cap = _read_retry_max_attempts_setting()
    cur = conn.execute(
        """UPDATE cloud_synced_files
           SET status = CASE
                   WHEN retry_count + 1 >= ? THEN 'dead_letter'
                   ELSE 'failed'
               END,
               last_error = ?,
               retry_count = retry_count + 1
           WHERE file_path = ?""",
        (cap, err_msg, rel_path),
    )
    if cur.rowcount:
        # Re-read the row to know which terminal state we landed in so
        # the log message is accurate. Cheap (single indexed lookup).
        post = conn.execute(
            "SELECT status, retry_count FROM cloud_synced_files "
            "WHERE file_path = ?",
            (rel_path,),
        ).fetchone()
        if post and post["status"] == 'dead_letter':
            logger.warning(
                "Cloud sync: %s reached retry cap (%d attempts) — "
                "moved to dead_letter. Recover via Failed Jobs page.",
                rel_path, post["retry_count"],
            )


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
    # Build set of event paths that are off-limits for auto-picking:
    #   * status='synced' — already uploaded, never re-pick
    #   * status='dead_letter' — Phase 2.6: hit retry cap; manual recovery
    #     only (Failed Jobs page in Phase 4). Re-picking would re-burn
    #     bandwidth on files that have proven they will not succeed.
    synced_paths: set = set()
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT file_path FROM cloud_synced_files "
                "WHERE status IN ('synced', 'dead_letter')"
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

            rel_path = canonical_cloud_path(f"{folder}/{entry}")

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
                        rel_path = canonical_cloud_path(f"ArchivedClips/{f}")
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

    # Score every candidate once so we can both filter and sort without
    # invoking the (relatively expensive) scorer twice. Score >= 200 means
    # neither an event.json trigger nor any waypoint geolocation hit was
    # found — i.e. routine driving footage.
    scored: List[Tuple[Tuple[str, str, int], int]] = [
        (t, _score_event_priority(t[0])) for t in events
    ]

    # Phase 2.3 — When ``sync_non_event_videos`` is False the picker MUST
    # actually drop the non-event/non-geo tier from the queue (the previous
    # behaviour merely demoted them to a lower priority, so they still got
    # uploaded — which silently consumed the user's bandwidth on top of the
    # event clips they actually wanted backed up).
    #
    # We must NOT re-import ``CLOUD_ARCHIVE_SYNC_NON_EVENT`` here: it is
    # snapshotted at config.py import time and the Settings save handler
    # only writes YAML — so the import would always return the stale
    # boot-time value. ``_read_sync_non_event_setting`` reads the live
    # YAML so a Settings toggle takes effect on the next sync iteration
    # without a service restart.
    sync_non_event_now = _read_sync_non_event_setting()
    if not sync_non_event_now:
        before = len(scored)
        scored = [(t, s) for (t, s) in scored if s < 200]
        dropped = before - len(scored)
        if dropped:
            logger.info(
                "Cloud sync: filtered %d non-event/non-geo clip(s) "
                "(sync_non_event_videos=false)", dropped,
            )

    scored.sort(key=lambda x: x[1])
    return [t for (t, _s) in scored]


# ---------------------------------------------------------------------------
# Credential Handling
# ---------------------------------------------------------------------------

def _write_rclone_conf(provider: str, creds: dict,
                       conf_name: Optional[str] = None) -> str:
    """Write a temporary rclone.conf to tmpfs and return its path.

    The caller is responsible for deleting the file after use by passing
    the returned path to :func:`_remove_rclone_conf`.

    ``conf_name`` lets callers pin a unique filename so cloud_archive
    and Live Event Sync don't collide on the shared tmpfs path during a
    yield/re-acquire cycle. When omitted the legacy fixed path
    ``/run/teslausb/rclone.conf`` is used (preserves existing
    cloud_archive behavior; LES MUST pass a unique name).
    """
    os.makedirs(_RCLONE_TMPFS_DIR, exist_ok=True)

    # Build minimal rclone remote config
    lines = ["[teslausb]"]
    lines.append(f"type = {provider}")
    for key, value in creds.items():
        lines.append(f"{key} = {value}")

    if conf_name:
        # Disallow path traversal — only a bare filename is acceptable.
        safe_name = os.path.basename(conf_name)
        conf_path = os.path.join(_RCLONE_TMPFS_DIR, safe_name)
    else:
        conf_path = _RCLONE_CONF_PATH
    fd = os.open(conf_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, "\n".join(lines).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return conf_path


def _remove_rclone_conf(conf_path: Optional[str] = None) -> None:
    """Delete the tmpfs rclone config if it exists.

    When ``conf_path`` is omitted the legacy fixed path is removed. LES
    MUST pass the explicit path it received from
    :func:`_write_rclone_conf` so a yield from cloud_archive doesn't
    accidentally delete cloud_archive's still-in-use config.

    Defense in depth (I-5): the resolved ``conf_path`` must lie inside
    :data:`_RCLONE_TMPFS_DIR`. All current callers derive their path
    from :func:`_write_rclone_conf` (which scopes to that directory),
    so this check is a no-op today; it guarantees a future caller can
    never turn this helper into an arbitrary-file-delete primitive.
    """
    target = conf_path or _RCLONE_CONF_PATH
    try:
        target_real = os.path.realpath(target)
        dir_real = os.path.realpath(_RCLONE_TMPFS_DIR)
        if os.path.commonpath([dir_real, target_real]) != dir_real:
            logger.warning(
                "Refusing to remove rclone conf outside %s: %s",
                dir_real, target,
            )
            return
    except ValueError:
        # commonpath raises ValueError when paths are on different
        # drives (Windows) or otherwise can't be compared. Refuse
        # rather than risk an unintended delete.
        logger.warning(
            "Refusing to remove rclone conf with unresolvable path: %s",
            target,
        )
        return
    try:
        os.remove(target)
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
# Cloud Reconciliation
# ---------------------------------------------------------------------------

def _reconcile_with_remote(
    conn: sqlite3.Connection,
    conf_path: str,
    remote_path: str,
    mem_flags: list,
) -> int:
    """Mark locally-pending files as synced if they already exist on the remote.

    Uses ``rclone lsf`` to list directories and files on the remote,
    then updates matching DB entries from pending/failed → synced, and
    inserts new 'synced' entries for remote files not yet tracked in the DB
    (e.g., files uploaded before tracking was implemented).
    Returns the number of entries reconciled.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    reconciled = 0

    # List event directories on remote (SentryClips/*, SavedClips/*)
    for folder in CLOUD_ARCHIVE_SYNC_FOLDERS:
        try:
            result = subprocess.run(
                ["rclone", "lsf", "--config", conf_path,
                 "--dirs-only", *mem_flags,
                 f"teslausb:{remote_path}/{folder}/"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                continue
            remote_dirs = {d.rstrip('/') for d in result.stdout.strip().split('\n') if d.strip()}
            if not remote_dirs:
                continue

            for dirname in remote_dirs:
                rel_path = canonical_cloud_path(f"{folder}/{dirname}")
                remote_dest = f"teslausb:{remote_path}/{rel_path}"

                # Update existing pending/failed entries
                cur = conn.execute(
                    """UPDATE cloud_synced_files
                       SET status = 'synced', synced_at = ?,
                           remote_path = ?, last_error = NULL
                       WHERE file_path = ? AND status IN ('pending', 'failed')""",
                    (now_iso, remote_dest, rel_path)
                )
                if cur.rowcount > 0:
                    reconciled += cur.rowcount
                    continue

                # If not in DB at all, insert as synced (pre-tracking upload)
                existing = conn.execute(
                    "SELECT status FROM cloud_synced_files WHERE file_path = ?",
                    (rel_path,)
                ).fetchone()
                if not existing:
                    conn.execute(
                        """INSERT INTO cloud_synced_files
                           (file_path, status, synced_at, remote_path)
                           VALUES (?, 'synced', ?, ?)""",
                        (rel_path, now_iso, remote_dest)
                    )
                    reconciled += 1
        except subprocess.TimeoutExpired:
            logger.warning("Reconcile timeout listing %s", folder)
        except Exception as e:
            logger.warning("Reconcile error for %s: %s", folder, e)

    # List ArchivedClips files on remote
    try:
        result = subprocess.run(
            ["rclone", "lsf", "--config", conf_path,
             *mem_flags,
             f"teslausb:{remote_path}/ArchivedClips/"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            # Strip trailing slashes too — rclone lsf may return directory
            # entries when a folder gets mistakenly created on the remote
            # (PRE-2.7 this produced corrupt rows like
            # ``ArchivedClips/foo.mp4/`` that broke later dedup checks).
            remote_files = {
                f.strip().rstrip('/')
                for f in result.stdout.strip().split('\n') if f.strip()
            }
            for filename in remote_files:
                if not filename:
                    continue
                rel_path = canonical_cloud_path(f"ArchivedClips/{filename}")
                remote_dest = f"teslausb:{remote_path}/{rel_path}"

                cur = conn.execute(
                    """UPDATE cloud_synced_files
                       SET status = 'synced', synced_at = ?,
                           remote_path = ?, last_error = NULL
                       WHERE file_path = ? AND status IN ('pending', 'failed')""",
                    (now_iso, remote_dest, rel_path)
                )
                if cur.rowcount > 0:
                    reconciled += cur.rowcount
                    continue

                existing = conn.execute(
                    "SELECT status FROM cloud_synced_files WHERE file_path = ?",
                    (rel_path,)
                ).fetchone()
                if not existing:
                    conn.execute(
                        """INSERT INTO cloud_synced_files
                           (file_path, status, synced_at, remote_path)
                           VALUES (?, 'synced', ?, ?)""",
                        (rel_path, now_iso, remote_dest)
                    )
                    reconciled += 1
    except Exception as e:
        logger.warning("Reconcile error for ArchivedClips: %s", e)

    if reconciled:
        conn.commit()
        logger.info("Cloud reconciliation: marked %d already-uploaded entries as synced", reconciled)

    return reconciled


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
# Reusable rclone upload helper (shared with Live Event Sync)
# ---------------------------------------------------------------------------

# Memory-safe rclone flags for Pi Zero 2 W. Module-level constant so both
# the cloud-sync loop and the Live Event Sync worker pin the same envelope.
RCLONE_MEM_FLAGS: List[str] = [
    "--buffer-size", "0",
    "--transfers", "1",
    "--checkers", "1",
]


def upload_path_via_rclone(
    local_path: str,
    remote_dest: str,
    conf_path: str,
    max_upload_mbps: int,
    timeout_seconds: int = 3600,
    proc_callback=None,
    mem_flags: Optional[List[str]] = None,
) -> Tuple[int, str]:
    """Upload a file or directory via rclone, returning (returncode, stderr).

    Picks ``copyto`` for files and ``copy`` for directories. Wraps the
    call in ``nice -n 19`` + ``ionice -c 3`` so the gadget endpoint and
    web service stay responsive.

    The caller passes a ``proc_callback`` to track the live subprocess for
    cancellation: it is invoked with the ``subprocess.Popen`` instance
    immediately after spawn, and again with ``None`` when the process
    exits. Pass ``None`` to disable tracking.

    Designed for one upload at a time. The Pi Zero 2 W cannot afford
    parallel rclone subprocesses, so callers must ensure only one
    upload is in flight via the global task_coordinator.
    """
    if mem_flags is None:
        mem_flags = RCLONE_MEM_FLAGS

    is_single_file = os.path.isfile(local_path)
    rclone_cmd = "copyto" if is_single_file else "copy"

    proc: Optional[subprocess.Popen] = None
    try:
        proc = subprocess.Popen(
            [
                "nice", "-n", "19",
                "ionice", "-c", "3",
                "rclone", rclone_cmd,
                "--config", conf_path,
                "--bwlimit", f"{max_upload_mbps}M",
                "--size-only",
                "--stats", "0",
                "--log-level", "ERROR",
                *mem_flags,
                local_path,
                remote_dest,
            ],
            # stdout → DEVNULL: rclone prints nothing useful with
            # --stats 0 and --log-level ERROR, and capturing it would
            # accumulate in Python memory against the Pi Zero 2 W
            # peak-RSS budget on long uploads.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc_callback is not None:
            try:
                proc_callback(proc)
            except Exception as e:
                logger.warning("proc_callback raised: %s", e)
        try:
            _, stderr = proc.communicate(timeout=timeout_seconds)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            returncode = -1
            stderr = f"Upload timed out ({timeout_seconds}s)"
    finally:
        if proc_callback is not None:
            try:
                proc_callback(None)
            except Exception:
                pass

    # Cap stderr to a bounded tail so a chatty rclone failure can't
    # blow the Pi Zero 2 W RSS budget. 8 KB is plenty of context for
    # diagnosing the failure; longer outputs are truncated.
    out = stderr or ""
    if len(out) > 8192:
        out = "...(truncated)...\n" + out[-8000:]
    return returncode, out


# Public re-exports for shared use by the Live Event Sync subsystem.
# Underscore-prefixed names are kept for internal call-sites that already
# use them; the public aliases just remove the underscore so other
# services can ``from services.cloud_archive_service import ...`` cleanly.
write_rclone_conf = _write_rclone_conf
remove_rclone_conf = _remove_rclone_conf
load_provider_creds = _load_provider_creds
is_wifi_connected = _is_wifi_connected


# ---------------------------------------------------------------------------
# Core Sync Engine
# ---------------------------------------------------------------------------

def _drain_once(
    teslacam_path: str,
    db_path: str,
    trigger: str,
) -> bool:
    """Single drain pass: discover events, upload one at a time, exit.

    Phase 3b (#99): split out from the old ``_run_sync`` so the
    long-lived ``_worker_loop`` can call it on every wake without
    spawning a new thread. The function body is structurally
    identical to the old implementation — same task-coordinator
    contract, same LES yield-between-files contract, same per-row
    DB updates — only the entry signature changed:

    * In-flight cancellation is read from ``_drain_cancel`` (set by
      ``stop_sync()``) **OR** ``_worker_stop`` (set by ``stop()``
      for terminal worker shutdown). The worker only reuses the
      latter for permanent shutdown so a "Stop Sync" UI click can
      cancel the current upload pass without killing the worker
      thread itself.
    * Returns ``True`` only when at least one file was uploaded,
      ``False`` for no-op drains (empty queue, cloud full, lock
      contention, WiFi down). The worker uses this to decide
      whether to immediately re-wake (more work might be ready)
      or sleep (no work was found this pass — don't hot-loop).
    """
    global _sync_status

    # Compose an "either" event so per-file checks below short-circuit
    # on either ``_drain_cancel`` (set by ``stop_sync``) or
    # ``_worker_stop`` (set by ``stop``). Only ``.is_set()`` is exercised
    # by the drain loop today; we deliberately do NOT expose ``set``,
    # ``clear``, or ``wait`` because those would have asymmetric
    # semantics (which underlying event do we mutate?) and become a
    # footgun for future code. If a future caller needs to mutate the
    # composite, it should operate on the underlying events directly.
    class _EitherEvent:
        __slots__ = ("_a", "_b")

        def __init__(self, a, b):
            self._a = a
            self._b = b

        def is_set(self):
            return self._a.is_set() or self._b.is_set()

    cancel_event = _EitherEvent(_drain_cancel, _worker_stop)

    # Acquire the global heavy-task lock so the indexer and archiver
    # don't run concurrently (Pi Zero has limited CPU/IO).
    #
    # Phase 2.9 (#97 item 2.9): track ``lock_held`` so the ``finally``
    # block only releases when we actually hold the lock. Without this
    # flag, the yield-to-LES path (below) that fails to re-acquire would
    # cause ``release_task`` to log a spurious
    # ``"tried to release but X holds the lock"`` warning. The warning
    # is harmless (coordinator handles it gracefully) but appears as a
    # yellow flag in the logs and confuses anyone reading them.
    from services.task_coordinator import acquire_task, release_task
    if not acquire_task('cloud_sync'):
        _sync_status.update({
            "running": False,
            "progress": "Skipped: another task is running",
        })
        return False
    lock_held = True

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
        # Startup recovery (stale sessions/uploads) is handled by
        # _init_cloud_tables() on first call after process start.

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
            # No work performed — return False so the worker idles
            # instead of immediately re-waking itself into a hot loop.
            return False

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
        # Reserve configured amount so we never fill the provider to 100%.
        cloud_reserve_bytes = int(CLOUD_ARCHIVE_RESERVE_GB * 1024 * 1024 * 1024)
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
                    cloud_free_bytes = int(about["free"]) - cloud_reserve_bytes
                    cloud_total = int(about.get("total", 0))
                    logger.info(
                        "Cloud storage: %.1f GB free / %.1f GB total (%.1f GB reserved)",
                        (cloud_free_bytes + cloud_reserve_bytes) / (1024 ** 3),
                        cloud_total / (1024 ** 3),
                        cloud_reserve_bytes / (1024 ** 3),
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
            # Cloud is full — no work performed. Return False so the
            # worker idles. A user freeing space + clicking "Sync Now"
            # will re-wake; the periodic idle timeout also retries.
            return False

        # Memory-safe flags for Pi Zero 2W
        mem_flags = ["--buffer-size", "0", "--transfers", "1", "--checkers", "1"]

        # Reconcile DB with cloud: mark files already on remote as synced.
        # This catches files uploaded before tracking was added, or by a
        # previous run that crashed before updating the DB.
        _sync_status["progress"] = "Reconciling with cloud…"
        try:
            _reconcile_with_remote(conn, conf_path, remote_path, mem_flags)
        except Exception as e:
            logger.warning("Cloud reconciliation failed (non-fatal): %s", e)

        # I/O throttle: pause between event uploads to avoid saturating
        # the SD card (shared with USB gadget and archive service)
        _INTER_UPLOAD_SLEEP = 2.0  # seconds

        for idx, (event_dir, rel_path, event_size) in enumerate(to_sync):
            if cancel_event.is_set():
                _sync_status["progress"] = "Cancelled"
                logger.info("Cloud sync cancelled after %d events", files_synced)
                break

            _sync_status.update({
                "files_done": files_synced,
                "current_file": rel_path,
                "current_file_size": event_size,
                "progress": f"Uploading {files_synced + 1}/{len(to_sync)}: {rel_path}",
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

            # Use the shared rclone helper. It handles copy-vs-copyto,
            # nice/ionice, bwlimit, timeout, and stderr capture.
            # Default size+mtime check catches partial uploads.
            def _track_proc(proc):
                global _sync_rclone_proc
                _sync_rclone_proc = proc

            try:
                returncode, stderr = upload_path_via_rclone(
                    event_dir,
                    remote_dest,
                    conf_path,
                    max_mbps,
                    timeout_seconds=3600,
                    proc_callback=_track_proc,
                    mem_flags=mem_flags,
                )

                if cancel_event.is_set():
                    # Process was killed by stop_sync — don't mark as failed
                    logger.info("Sync: %s interrupted by stop request", rel_path)
                    conn.execute(
                        "UPDATE cloud_synced_files SET status = 'pending' WHERE file_path = ?",
                        (rel_path,)
                    )
                    _fsync_db(conn)
                    break

                if returncode == 0:
                    files_synced += 1
                    bytes_transferred += event_size
                    _sync_status["bytes_transferred"] = bytes_transferred
                    _sync_status["files_done"] = files_synced
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
                    err_msg = (stderr or "").strip()[:500]
                    logger.error("Sync: [%d/%d] %s FAILED (exit %d): %s",
                                idx + 1, len(to_sync), rel_path,
                                returncode, err_msg[:200])
                    _mark_upload_failure(
                        conn, rel_path, err_msg[:255],
                    )
                    _fsync_db(conn)

            except Exception as e:
                logger.error("Sync: %s error: %s", rel_path, e)
                _mark_upload_failure(
                    conn, rel_path, str(e)[:255],
                )
                _fsync_db(conn)

            # Yield to Live Event Sync if it has READY pending event work.
            # LES gets priority over normal cloud_archive uploads when both
            # want WiFi. The helper checks status, next_retry_at, attempts,
            # and the daily data cap so a stuck row never blocks us forever.
            try:
                from services.live_event_sync_service import (
                    has_ready_live_event_work,
                )
                _les_pending = has_ready_live_event_work(db_path)
            except Exception:
                _les_pending = False
            if _les_pending:
                logger.info(
                    "Cloud sync yielding to Live Event Sync (queue has ready events)",
                )
                # Drop the heavy-task lock so LES worker can grab it.
                # We re-acquire on the next loop iteration.
                from services.task_coordinator import (
                    acquire_task as _acq, release_task as _rel,
                )
                _rel('cloud_sync')
                lock_held = False
                # Wait for LES to drain (or up to 5 minutes per yield).
                yield_deadline = time.time() + 300
                while time.time() < yield_deadline:
                    if cancel_event.is_set():
                        break
                    time.sleep(2)
                    try:
                        if not has_ready_live_event_work(db_path):
                            break
                    except Exception:
                        break
                # Re-acquire the lock; if a different task grabbed it
                # while we yielded, we treat that as cooperative and
                # bail out — the next dispatcher fire will resume.
                if not _acq('cloud_sync'):
                    logger.info(
                        "Cloud sync: another task acquired lock during yield; "
                        "stopping this run (will resume on next trigger)",
                    )
                    break
                lock_held = True

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
        # Phase 3b — return value is consumed by ``_worker_loop`` to
        # decide between short-sleep (busy / contended / partial) and
        # long-sleep (genuinely empty queue). A drain that processed
        # at least one file or that ran a full reconcile counts as
        # "did real work" and may have woken up new work, so we tell
        # the loop to short-sleep and immediately re-check.
        drain_did_work = (files_synced > 0)

    except Exception as e:
        logger.error("Cloud sync failed: %s", e)
        _sync_status.update({
            "running": False,
            "error": str(e),
            "progress": f"Error: {e}",
        })
        session_status = "interrupted"
        drain_did_work = False

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
        # Phase 2.9: only release if we still hold the lock. The
        # yield-to-LES path above can leave us without the lock if the
        # re-acquire fails; releasing in that state would log a spurious
        # warning from task_coordinator.
        if lock_held:
            release_task('cloud_sync')

    return bool(drain_did_work)


# Backward-compat alias: anything that was importing ``_run_sync``
# (legacy tests, third-party tooling) still works. New code should
# call ``_drain_once`` directly or — better — ``wake()`` to let the
# worker loop schedule the drain. The old ``cancel_event`` parameter
# is accepted for API compatibility but ignored — cancellation now
# always flows through the module-level ``_worker_stop`` event.
def _run_sync(  # noqa: D401 — wraps _drain_once for legacy callers
    teslacam_path: str,
    db_path: str,
    trigger: str,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """Deprecated: use ``_drain_once`` (or ``wake()`` for async).

    The legacy ``cancel_event`` parameter is accepted for backward
    compatibility but ignored. Real cancellation is via ``stop_sync()``
    / ``_worker_stop``.
    """
    _drain_once(teslacam_path, db_path, trigger)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Public API — Phase 3b continuous worker
# ---------------------------------------------------------------------------

def _worker_loop(teslacam_path: str, db_path: str) -> None:
    """Long-lived worker that drains the cloud sync queue on demand.

    Runs in a single daemon thread for the lifetime of the gadget_web
    process. Idles on ``_wake.wait(timeout=N)`` when there's nothing
    to do (~0.1 % CPU baseline) and runs a single ``_drain_once``
    pass when poked. Producers (file watcher, NM dispatcher, mode
    switch, manual UI) call ``wake()`` instead of starting a fresh
    thread.

    Containment: every iteration's ``_drain_once`` call is wrapped in
    try/except so a single bad pass cannot kill the worker. The thread
    only exits when ``_worker_stop`` is set.

    Wake-event discipline: ONLY the loop-top ``_wake.wait`` consumes
    the wake event. All other waits inside the loop (post-exception
    backoff, gate-skip backoff for WiFi-down / LES-pending /
    archive-running) use ``_worker_stop.wait`` so a producer's wake
    that lands during the backoff isn't discarded — it's still set
    when the loop returns to the top.
    """
    logger.info(
        "Cloud archive worker started (teslacam=%s)", teslacam_path,
    )
    _sync_status["worker_running"] = True
    try:
        # On startup, recover any rows that were marked ``uploading``
        # when the previous process died. This is cheap (single UPDATE)
        # and idempotent.
        try:
            recover_interrupted_uploads(db_path)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Cloud archive startup recovery failed (continuing): %s", e,
            )

        # Wake immediately so an existing pending queue starts draining
        # without waiting for the first idle timeout.
        _wake.set()

        while not _worker_stop.is_set():
            # Block until somebody wakes us (file watcher, NM
            # dispatcher, mode switch, manual UI, stop, idle timeout).
            # Using a timed wait so an unobserved state change (WiFi
            # came back without a dispatcher event) eventually catches
            # up — but a producer wake short-circuits the wait.
            _wake.wait(timeout=_WAIT_WHEN_IDLE_SECONDS)
            _wake.clear()
            if _worker_stop.is_set():
                break
            _sync_status["wake_count"] = _sync_status.get("wake_count", 0) + 1

            # Skip drain entirely if disabled or no provider — but
            # stay alive so a settings change doesn't require a
            # service restart. A subsequent wake after the user
            # reconfigures will succeed.
            if not CLOUD_ARCHIVE_ENABLED:
                continue
            if not CLOUD_ARCHIVE_PROVIDER:
                continue

            # Yield to LES BEFORE acquiring our own lock — same
            # contract as the legacy ``trigger_auto_sync``.
            try:
                from services.live_event_sync_service import (
                    has_ready_live_event_work,
                )
                if has_ready_live_event_work(db_path):
                    logger.debug(
                        "Cloud archive worker yielding wake to LES "
                        "(ready events in queue)",
                    )
                    # Backoff that preserves _wake so a producer's
                    # wake during the LES window isn't discarded.
                    _backoff_wait(_WAIT_WHEN_BUSY_SECONDS)
                    if _worker_stop.is_set():
                        break
                    continue
            except Exception:  # noqa: BLE001
                pass

            # Skip if WiFi is down — we'll wake again on the next
            # NM dispatcher event when WiFi comes back. The idle
            # timeout also catches "WiFi came back silently".
            if not _is_wifi_connected():
                logger.debug("Cloud archive worker: WiFi down, idling")
                continue

            # Skip if a single-file archive is running (shared rclone
            # subprocess + bandwidth contention).
            try:
                from services.cloud_rclone_service import get_archive_status
                if get_archive_status().get("running"):
                    logger.debug(
                        "Cloud archive worker: single-file archive in "
                        "progress, deferring drain",
                    )
                    _backoff_wait(_WAIT_WHEN_BUSY_SECONDS)
                    if _worker_stop.is_set():
                        break
                    continue
            except Exception:  # noqa: BLE001
                pass

            try:
                _sync_status["drain_count"] = (
                    _sync_status.get("drain_count", 0) + 1
                )
                _drain_once(teslacam_path, db_path, "auto")
                # NOTE: deliberately do NOT self-rewake here. Producers
                # (file watcher, NM dispatcher, mode switch, manual UI)
                # already call wake() when new work arrives — those
                # wakes fire even while the worker is mid-drain and
                # are picked up by the next loop-top _wake.wait().
                # Self-rewaking would amplify any bug where
                # _drain_once mistakenly returns the wrong state into
                # a hot-loop that floods the SDIO bus on the Pi
                # Zero 2 W (PR #126 review Finding #1).
            except Exception as e:  # noqa: BLE001
                # Containment: never let a bad drain kill the worker.
                logger.exception("Cloud archive drain iteration failed: %s", e)
                _sync_status["error"] = str(e)[:500]
                # Short backoff before retrying so we don't hot-loop
                # on a persistent failure. _backoff_wait preserves
                # _wake so a producer's wake during the backoff
                # short-circuits it and triggers a fresh attempt.
                _backoff_wait(_WAIT_WHEN_BUSY_SECONDS)
                if _worker_stop.is_set():
                    break
            finally:
                # Clear the in-flight cancel so the next drain isn't
                # pre-cancelled. ``stop_sync()`` may have set this to
                # interrupt the just-completed pass; once the drain
                # has returned the signal has done its job.
                _drain_cancel.clear()
                _sync_cancel.clear()

    finally:
        _sync_status["worker_running"] = False
        logger.info("Cloud archive worker stopped")


def start(
    teslacam_path: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    """Start the cloud archive worker thread. Idempotent.

    Phase 3b (#99): replaces the one-shot ``start_sync`` thread spawn.
    Called once from ``gadget_web`` startup; subsequent producers
    poke the running worker via :func:`wake`.

    Args:
        teslacam_path: TeslaCam directory (RO mount). If omitted,
            resolved via ``services.video_service.get_teslacam_path``.
        db_path: Path to the cloud sync DB. Defaults to
            ``CLOUD_ARCHIVE_DB_PATH`` from config.

    Returns:
        ``True`` if a new worker thread was started, ``False`` if the
        worker was already alive or cloud archive is disabled.
    """
    global _worker_thread, _sync_thread

    if not CLOUD_ARCHIVE_ENABLED:
        logger.info("Cloud archive disabled in config — not starting worker")
        return False

    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return False

        if db_path is None:
            db_path = CLOUD_ARCHIVE_DB_PATH
        if teslacam_path is None:
            try:
                from services.video_service import get_teslacam_path
                teslacam_path = get_teslacam_path()
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Cannot resolve TeslaCam path; worker not started: %s", e,
                )
                return False
            if not teslacam_path:
                logger.warning(
                    "TeslaCam path empty; cloud worker not started",
                )
                return False

        _worker_stop.clear()
        _wake.clear()
        _worker_thread = threading.Thread(
            target=_worker_loop,
            args=(teslacam_path, db_path),
            name="cloud-archive-worker",
            daemon=True,
        )
        # Legacy alias so callers reading ``_sync_thread`` see the
        # same object (used by a few status helpers and tests).
        _sync_thread = _worker_thread
        _worker_thread.start()
        return True


def stop(timeout: float = 5.0) -> bool:
    """Stop the worker thread. Best-effort; daemon survives at process exit.

    Sets ``_worker_stop``, wakes the worker out of any pending wait,
    terminates the active rclone subprocess (if any), and joins the
    thread up to ``timeout`` seconds.

    Returns ``True`` if the thread exited cleanly (or was never alive),
    ``False`` if it didn't join within ``timeout``.
    """
    global _worker_thread, _sync_rclone_proc

    _worker_stop.set()
    _wake.set()

    proc = _sync_rclone_proc
    if proc is not None:
        try:
            proc.terminate()
            logger.info("Sent SIGTERM to rclone during stop (pid=%d)", proc.pid)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                logger.info("Sent SIGKILL to rclone during stop (pid=%d)", proc.pid)
        except (OSError, ProcessLookupError):
            pass

    thread = _worker_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
        return not thread.is_alive()
    return True


def wake() -> None:
    """Wake the worker so it runs a drain pass on the next iteration.

    Idempotent and cheap (a single ``threading.Event.set``). Called by
    every producer:

    * File watcher new-mp4 callback (a freshly archived clip is now
      visible to the queue producer).
    * NetworkManager dispatcher (WiFi connect → ``/cloud/api/wake``).
    * Mode-switch hook (Tesla USB just came back online; re-check the
      queue in case Tesla wrote new events while gadget mode was off).
    * Manual UI ``Sync Now`` button (``/cloud/api/sync_now`` calls
      ``start_sync`` which in turn calls ``wake``).
    * Periodic safety: the worker's ``_wake.wait(timeout=300)`` ensures
      we re-check the queue at least every 5 minutes even if no
      producer fired.

    Safe to call before :func:`start` — the wake event will be honored
    on the next worker startup.
    """
    _wake.set()


def start_sync(
    teslacam_path: str,
    db_path: str,
    trigger: str = "manual",
) -> Tuple[bool, str]:
    """Backward-compat wrapper: ensure the worker is alive and poke it.

    Phase 3b (#99): no longer spawns a per-trigger thread. Producers
    that previously called ``start_sync`` now end up calling
    :func:`wake` after lazily starting the worker on first use. The
    return value is preserved so existing callers (the ``/cloud/api/
    sync_now`` blueprint, mode-switch hooks, NM dispatcher) keep
    working without changes.

    Args:
        teslacam_path: TeslaCam directory (RO mount).
        db_path: Path to the cloud sync DB.
        trigger: Diagnostic label (``manual``, ``auto``, ``wifi``).

    Returns:
        ``(success, message)`` tuple. Success is ``True`` when the
        wake was delivered (or when the worker is already draining).
        ``False`` only when cloud archive is disabled or no provider
        is configured.
    """
    if not CLOUD_ARCHIVE_ENABLED:
        return False, "Cloud archive is disabled in config"
    if not CLOUD_ARCHIVE_PROVIDER:
        return False, "No cloud provider configured"

    # Lazy-start the worker if this is the very first sync trigger.
    # Production startup calls ``start()`` explicitly; this is a
    # belt-and-suspenders for tests / scripts that import the module
    # directly.
    #
    # ``start()`` returning False with the worker actually alive is
    # success (a concurrent caller won the race). Treating it as a
    # hard failure produced spurious "Failed to start cloud archive
    # worker" messages on the UI under any concurrent trigger.
    started = start(teslacam_path=teslacam_path, db_path=db_path)
    if not started:
        with _worker_lock:
            worker_alive = (
                _worker_thread is not None and _worker_thread.is_alive()
            )
        if not worker_alive:
            return False, "Failed to start cloud archive worker"

    wake()
    logger.info("Cloud sync wake signal delivered (trigger=%s)", trigger)

    if _sync_status.get("running"):
        return True, "Cloud sync wake delivered (drain in progress)"
    return True, "Cloud sync wake delivered"


def stop_sync(graceful: bool = True) -> Tuple[bool, str]:
    """Stop a running drain by killing the active rclone process.

    Phase 3b (#99): the worker thread itself stays alive (use
    :func:`stop` for full shutdown). Only the in-flight rclone
    subprocess is terminated, and ``_drain_cancel`` is set so the
    drain bails out at the next inter-file checkpoint. The worker
    clears ``_drain_cancel`` itself when the drain returns and
    resumes idling normally so subsequent ``wake()`` calls (from
    file watcher / NM dispatcher / mode switch / manual UI) are
    still honored.

    Critically does NOT touch ``_worker_stop`` — that event is
    reserved for terminal worker shutdown by :func:`stop`. Setting
    it here would race the worker's loop-top
    ``while not _worker_stop.is_set()`` check and silently kill the
    worker thread, causing every subsequent ``wake()`` to be
    dropped on the floor (bug fixed in PR #126 review).

    Always terminates immediately — a single event upload can take
    20+ minutes, so waiting is impractical. The partial file on the
    remote will be overwritten on the next drain (--size-only detects
    mismatch).
    """
    global _sync_rclone_proc

    if not _sync_status.get("running"):
        return False, "Sync is not running"

    # Set the in-flight cancel flag so ``_drain_once`` sees
    # cancellation at its next inter-file check. The worker clears
    # this on its own once the drain returns — see ``_worker_loop``.
    _drain_cancel.set()
    _sync_cancel.set()  # legacy alias for any external callers

    proc = _sync_rclone_proc
    if proc is not None:
        try:
            proc.terminate()
            logger.info("Sent SIGTERM to rclone (pid=%d)", proc.pid)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                logger.info("Sent SIGKILL to rclone (pid=%d)", proc.pid)
        except (OSError, ProcessLookupError):
            pass

    logger.info("Sync stop requested (worker stays alive)")
    return True, "Sync stopping"


def get_sync_status() -> dict:
    """Return a snapshot of the current sync status for UI polling.

    Returns only in-memory data — no DB queries. DB totals are updated
    by the sync thread after each upload completes (see _sync_status
    updates in _run_sync).
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

    Keys: total_synced, total_pending, total_failed, total_dead_letter,
    total_bytes.

    ``total_failed`` is the SUM of ``failed`` and ``dead_letter`` rows
    so the dashboard counter does NOT silently DECREASE when a row hits
    the Phase 2.6 retry cap and is promoted from ``failed`` →
    ``dead_letter``. Without this, a permanently broken clip that
    promotes after retry 5 would make problems look like they
    self-resolved on the dashboard.

    ``total_dead_letter`` is also exposed as a subset so a future
    Failed Jobs page (Phase 4) can break the count down by terminal
    state without changing this aggregate.
    """
    conn = _init_cloud_tables(db_path)
    try:
        counts = {}
        for status in ("synced", "pending", "failed", "uploading",
                       "dead_letter"):
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM cloud_synced_files WHERE status = ?",
                (status,),
            ).fetchone()
            counts[status] = row["cnt"] if row else 0

        # Sum bytes from individual synced files (more accurate than session
        # totals which are lost when sessions are interrupted by restart).
        row = conn.execute(
            "SELECT COALESCE(SUM(file_size), 0) AS total "
            "FROM cloud_synced_files WHERE status = 'synced'"
        ).fetchone()
        total_bytes = row["total"] if row else 0

        # Use the higher of DB pending count vs in-memory discovery count.
        # The DB may not have entries for all events on disk (events only get
        # DB rows when first attempted). The in-memory files_total from
        # _discover_events() is the true count of work remaining.
        db_pending = counts["pending"] + counts["uploading"]
        mem_total = _sync_status.get("files_total", 0)
        mem_done = _sync_status.get("files_done", 0)
        mem_pending = max(0, mem_total - mem_done) if _sync_status.get("running") else 0
        effective_pending = max(db_pending, mem_pending)

        return {
            "total_synced": counts["synced"],
            "total_pending": effective_pending,
            "total_failed": counts["failed"] + counts["dead_letter"],
            "total_dead_letter": counts["dead_letter"],
            "total_bytes": total_bytes,
        }
    finally:
        conn.close()


def trigger_auto_sync(teslacam_path: str, db_path: str) -> None:
    """Backward-compat wrapper: poke the continuous worker.

    Phase 3b (#99): the "if not running, kick a one-shot" pattern is
    gone. The worker is always alive (started once by gadget_web at
    process startup); producers just need to wake it. The internal
    LES yield + WiFi check + already-running check now happen *inside*
    ``_worker_loop`` rather than at the producer side.

    Kept as a public alias so existing call sites
    (``mode_control._trigger_cloud_sync_after_mode_switch``,
    ``web_control`` startup, anyone who imported the symbol) keep
    working without changes.
    """
    # Lazy-start safety: every other producer assumes the worker is
    # alive. If it isn't (e.g. config flipped at runtime), spinning
    # it up here is cheap and safe. ``start()`` is idempotent and
    # internally locks, so a concurrent call here is a no-op rather
    # than a race.
    if CLOUD_ARCHIVE_ENABLED and CLOUD_ARCHIVE_PROVIDER:
        start(teslacam_path=teslacam_path, db_path=db_path)
    wake()


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
                # Phase 2.7 — store and look up by canonical relative
                # path. Pre-2.7 this used ``entry.path`` (an absolute
                # filesystem path) which was inconsistent with the bulk
                # worker's relative ``f"{folder}/{event_dir}"`` form.
                # The canonical form is what the bulk worker stores AND
                # what the v2 migration rewrote existing rows to, so
                # this lookup now sees the same row the worker created
                # and the dedup check actually dedups.
                canonical = canonical_cloud_path(entry.path)
                existing = conn.execute(
                    "SELECT status FROM cloud_synced_files WHERE file_path = ?",
                    (canonical,)
                ).fetchone()
                if existing and existing['status'] in ('synced', 'uploading'):
                    continue

                stat = entry.stat()
                conn.execute(
                    """INSERT OR REPLACE INTO cloud_synced_files
                       (file_path, file_size, file_mtime, status, retry_count)
                       VALUES (?, ?, ?, 'queued', 0)""",
                    (canonical, stat.st_size, stat.st_mtime)
                )
                queued += 1

        conn.commit()
        if queued:
            return True, "Added {} files to sync queue".format(queued)
        return True, "All files already synced or queued"
    finally:
        conn.close()


def get_sync_queue() -> dict:
    """Return the current sync queue (queued/pending/uploading files).

    Note:
        Rows in ``failed`` state are intentionally excluded from this view —
        the UI surfaces only the active pipeline (queued / pending /
        uploading). To scrub historical failures from the underlying
        table, use :func:`remove_from_queue` or :func:`clear_queue`,
        both of which match every non-``synced`` row.
    """
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
    """Remove a single item from the sync queue.

    Deletes any non-``synced`` row matching ``file_path``.  The local queue
    is local data that the user owns, so deletion is allowed regardless of
    cloud provider configuration, sync worker state, or row status — including
    rows stuck in ``uploading`` (e.g. when the sync was interrupted before the
    worker could reset the row back to ``pending``), ``failed`` rows, and
    Phase 2.6 ``dead_letter`` rows that hit the retry cap.

    ``synced`` rows are preserved so deleting from the queue cannot wipe the
    historical record of files already uploaded; those rows are not exposed
    via :func:`get_sync_queue` anyway.

    The ``file_path`` argument is canonicalized via
    :func:`canonical_cloud_path` before lookup so callers passing either
    the legacy absolute form or the canonical relative form match the
    same row (post-2.7 migration the DB only contains canonical rows,
    but the API can still receive legacy paths).

    A path containing ``..`` segments is rejected via
    :func:`canonical_cloud_path` (raises ``ValueError``) — caught here
    and surfaced to the caller as ``(False, "Invalid path")`` so the
    blueprint returns a sane error to the UI rather than crashing.
    """
    try:
        canonical = canonical_cloud_path(file_path)
    except ValueError:
        return False, "Invalid path"
    conn = _init_cloud_tables(CLOUD_ARCHIVE_DB_PATH)
    try:
        result = conn.execute(
            "DELETE FROM cloud_synced_files WHERE file_path = ? AND status != 'synced'",
            (canonical,),
        )
        conn.commit()
        if result.rowcount:
            return True, "Removed from queue"
        return True, "Not in queue"
    finally:
        conn.close()


def clear_queue() -> Tuple[bool, str]:
    """Clear every non-``synced`` item from the sync queue.

    Includes ``queued``, ``pending``, ``uploading``, ``failed``, and Phase 2.6
    ``dead_letter`` rows so the user can always reset the queue — even after
    stopping the sync worker or disconnecting the cloud provider, both of
    which can leave rows stuck in ``uploading`` state.  ``synced`` history
    rows are preserved.
    """
    conn = _init_cloud_tables(CLOUD_ARCHIVE_DB_PATH)
    try:
        result = conn.execute(
            "DELETE FROM cloud_synced_files WHERE status != 'synced'"
        )
        conn.commit()
        return True, "Cleared {} items from queue".format(result.rowcount)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 4.1 — dead-letter inspection + manual retry (Failed Jobs page)
# ---------------------------------------------------------------------------

def list_dead_letters(limit: int = 100) -> List[Dict[str, Any]]:
    """Return up to ``limit`` ``dead_letter`` rows for the Failed Jobs page.

    Each row carries ``file_path``, ``last_error``, ``retry_count``, and
    ``file_size`` so the unified UI can render the why and how-big without
    a follow-up call. Sorted oldest-first by id (the order rows were
    promoted to dead-letter), which matches the order operators want to
    triage them — earliest failure usually exposes a config or auth
    problem the rest are downstream of.

    Returns plain dicts so the blueprint can ``jsonify`` them directly.
    """
    if limit <= 0:
        return []
    limit = min(int(limit), 1000)
    conn = _init_cloud_tables(CLOUD_ARCHIVE_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT id, file_path, file_size, retry_count, last_error "
            "FROM cloud_synced_files "
            "WHERE status = 'dead_letter' "
            "ORDER BY id ASC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def retry_dead_letter(file_path: Optional[str] = None) -> int:
    """Reset ``dead_letter`` rows back to ``pending`` for re-pickup.

    When ``file_path`` is given, only that one row is reset (looked up
    via :func:`canonical_cloud_path` so legacy absolute forms still
    match the stored canonical form). When ``None``, every dead-letter
    row in the table is reset — useful for "Retry all" after fixing a
    cloud auth or quota outage that affected the whole batch.

    Resets ``retry_count`` to zero and clears ``last_error`` so the
    retry budget starts fresh; the next failure is treated as the
    first one again. Wakes the cloud worker so the row gets picked up
    immediately if WiFi + cloud are healthy. Returns the number of
    rows actually reset (``0`` if nothing matched).
    """
    if file_path is not None:
        try:
            target = canonical_cloud_path(file_path)
        except ValueError:
            return 0
    else:
        target = None
    conn = _init_cloud_tables(CLOUD_ARCHIVE_DB_PATH)
    try:
        if target is None:
            cur = conn.execute(
                "UPDATE cloud_synced_files "
                "SET status = 'pending', retry_count = 0, "
                "    last_error = NULL "
                "WHERE status = 'dead_letter'"
            )
        else:
            cur = conn.execute(
                "UPDATE cloud_synced_files "
                "SET status = 'pending', retry_count = 0, "
                "    last_error = NULL "
                "WHERE status = 'dead_letter' AND file_path = ?",
                (target,),
            )
        conn.commit()
        n = cur.rowcount or 0
    finally:
        conn.close()
    if n > 0:
        try:
            wake()
        except Exception:  # noqa: BLE001
            logger.debug("retry_dead_letter: wake() raised; ignoring",
                         exc_info=True)
    return n