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
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

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

# Event-style folder names — every entry under one of these on the
# remote is a per-event SUBDIRECTORY containing the 6 cam clips +
# event.json. Used by ``_reconcile_with_remote_legacy`` to know which
# folders are worth a ``--dirs-only`` listing. These names are dictated
# by Tesla firmware and never change at user request; they are
# independent of the user-configurable ``cloud_archive.sync_folders``
# setting (which controls what to UPLOAD, not what to RECONCILE — we
# always want to mark already-uploaded events ``synced`` so a folder
# the user temporarily unchecks doesn't trigger a re-upload when they
# re-check it).
_EVENT_FOLDER_NAMES = ("SentryClips", "SavedClips")


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
_CLOUD_SCHEMA_VERSION = 5

# Key in ``cloud_archive_meta`` holding the ISO-8601 UTC timestamp at
# which the user last reset the dashboard counters. ``get_sync_stats``
# uses this to filter the cumulative ``total_synced`` / ``total_bytes``
# values without touching the underlying ``cloud_synced_files`` rows
# (preserving dedup so already-synced files are never re-uploaded).
# Absent row / NULL value = no reset performed (counters show full
# lifetime totals).
_CLOUD_STATS_BASELINE_KEY = "stats_baseline_at"

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
    last_error TEXT,
    previous_last_error TEXT
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

-- v5: simple key/value table for user-controlled UI state. Currently
-- holds the dashboard counter reset timestamp (stats_baseline_at) so
-- the "Reset Stats" button on the cloud sync page can hide historical
-- synced counts WITHOUT deleting the dedup-critical cloud_synced_files
-- rows (which would cause everything-on-cloud to be re-uploaded).
CREATE TABLE IF NOT EXISTS cloud_archive_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_cloud_synced_status ON cloud_synced_files(status);
CREATE INDEX IF NOT EXISTS idx_cloud_synced_mtime ON cloud_synced_files(file_mtime);
CREATE INDEX IF NOT EXISTS idx_cloud_synced_synced_at ON cloud_synced_files(synced_at);
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
# 2. Wave 4 PR-F4 (issue #184): live-event uploads (Sentry/Saved) are
#    now first-class ``pipeline_queue`` rows at ``PRIORITY_LIVE_EVENT``
#    instead of a separate ``live_event_sync`` subsystem with its own
#    queue, worker, and yield-coordination. The unified worker's
#    natural priority ordering means a live event always leapfrogs
#    bulk catch-up rows on the very next claim — no separate LES
#    head-start, no inter-file yield dance, no second rclone
#    subprocess.
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


def _migrate_drop_live_event_queue_v4(
    conn: sqlite3.Connection, db_path: str,
) -> None:
    """Drop the orphaned ``live_event_queue`` table from cloud_sync.db.

    Issue #202 — Wave 4 PR-F4 (issue #184 / PR #201) deleted the
    standalone Live Event Sync subsystem (the service, blueprints,
    routes, and config block). The ``live_event_queue`` table created
    by that subsystem was deliberately left behind so any rows still
    pending at deploy time could survive a rollback. After at least
    one stable release post-PR-F4, the table is now safe to drop:
    every install has had time to either (a) drain the table to zero
    via the cloud worker (which now reads from ``pipeline_queue``
    priority-0 rows), or (b) leave it untouched at zero rows because
    LES was never enabled on that install.

    Defensive cross-DB check (the issue body explicitly asks for this):
    if the table contains any rows, verify each row is mirrored into
    ``pipeline_queue.legacy_table='live_event_queue'`` in geodata.db.
    For any unmirrored rows, log a WARNING with the row IDs and
    backfill them via :func:`pipeline_queue_service.dual_write_enqueue`
    BEFORE the DROP, so no live-event upload work is lost. This is a
    safety net — the canonical install verified pre-merge had 0 rows
    on both sides — but it remains the correct shape because some
    third-party install may have captured rows between PR-F4 deploy
    and this PR's deploy.

    Order:
      1. Existence check (no-op if table already absent).
      2. Defensive zero-row check + cross-DB mirror reconciliation
         (backfill missing mirrors AND re-verify after the loop —
         see :func:`_backfill_missing_live_event_mirrors` for the
         post-loop verification that closes the silent-data-loss
         path through ``dual_write_enqueue``'s ``sqlite3.Error →
         return False`` branch).
      3. Drop secondary indexes (``idx_les_status``,
         ``idx_les_next_retry``).
      4. Drop the table itself (autoindex from UNIQUE drops with it).

    The caller (``_init_cloud_tables``) wraps this in a
    ``try / except → conn.rollback() → migration_ok=False``
    envelope. On any exception here the version bump is skipped and
    the migration retries on next service start. Crucially, the call
    site is gated on ``migration_ok`` being True from prior versions
    so a v2 failure does not allow v4 work to land in a fresh
    implicit transaction that downstream commits would persist
    (review-pr finding #3).
    """
    if not _table_exists_local(conn, 'live_event_queue'):
        return

    row_count = conn.execute(
        "SELECT COUNT(*) FROM live_event_queue"
    ).fetchone()[0]

    if row_count > 0:
        logger.warning(
            "Cloud archive v4 migration: live_event_queue contains "
            "%d row(s); reconciling against pipeline_queue before DROP.",
            row_count,
        )
        _backfill_missing_live_event_mirrors(conn, db_path)

    # Drop secondary indexes first. The autoindex from the UNIQUE
    # constraint drops automatically with the table itself.
    conn.execute("DROP INDEX IF EXISTS idx_les_status")
    conn.execute("DROP INDEX IF EXISTS idx_les_next_retry")
    conn.execute("DROP TABLE IF EXISTS live_event_queue")
    logger.info(
        "Cloud archive v4 migration: dropped live_event_queue table "
        "and indexes from %s",
        db_path,
    )


def _table_exists_local(conn: sqlite3.Connection, table: str) -> bool:
    """Return True if ``table`` exists in the connected DB.

    Thin wrapper around :func:`pipeline_queue_service.table_exists`
    via lazy import — the lazy-import pattern is already used inside
    :func:`_backfill_missing_live_event_mirrors`, so the no-cycle
    constraint is satisfied. Falls back to an inline ``sqlite_master``
    lookup if the import fails (corrupt install / deferred package
    init during early gadget_web boot — same scenarios as the lazy
    import below).

    Public ``table_exists`` was promoted from ``_table_exists`` per
    review-pr finding N1; this wrapper now uses the public name and
    drops the prior ``# noqa: SLF001``.
    """
    try:
        from services import pipeline_queue_service as pqs
        return pqs.table_exists(conn, table)
    except Exception:  # noqa: BLE001
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            return row is not None
        except sqlite3.Error:
            return False


def _backfill_missing_live_event_mirrors(
    conn: sqlite3.Connection, db_path: str,
) -> None:
    """Cross-DB reconciliation: ensure every ``live_event_queue`` row
    has a matching ``pipeline_queue`` row before the DROP.

    Reads the LES rows from the connected cloud_sync.db, opens a
    short-lived geodata.db connection via
    :func:`pipeline_queue_service.dual_write_enqueue`, INSERTs any
    missing mirrors at ``priority=PRIORITY_LIVE_EVENT,
    stage='cloud_pending'`` (or ``stage='cloud_done', status='done'``
    when the legacy row was already ``'uploaded'``) so the unified
    cloud worker picks them up on its next claim. **Does not UPDATE
    existing mirrors** — see the stale-mirror handling below.

    Stale-mirror handling (review-pr finding #5): if a non-canonical
    install mirrored a row at LES ``status='pending'`` time and LES
    later finished the upload, the legacy row will say
    ``status='uploaded'`` while the mirror still says ``cloud_pending``.
    For each such row we UPDATE the mirror's stage to ``cloud_done``
    and status to ``done`` so the worker doesn't re-upload completed
    work.

    Status mapping mirrors the deleted ``_backfill_live_event_queue``:
    legacy ``uploaded`` → unified ``done`` at ``stage='cloud_done'``;
    legacy ``uploading`` → unified ``in_progress``; legacy ``failed``
    → unified ``failed``; everything else → ``failed`` (review-pr
    finding #4 — conservative default; an unknown legacy status
    must NOT accidentally cause re-upload).

    Post-loop verification (review-pr finding #1): after backfill,
    re-query ``pipeline_queue`` for the unmirrored set. If any
    legacy_id is still missing, raise ``RuntimeError`` so the caller
    rolls back and the DROP doesn't run. This converts
    ``dual_write_enqueue``'s ``sqlite3.Error → return False`` branch
    (which only logs a WARNING in pqs) into the documented
    abort-and-retry path.

    A failure here re-raises so the caller can rollback and leave
    the schema at v3 for retry.
    """
    rows = conn.execute(
        "SELECT id, event_dir, event_json_path, event_timestamp, "
        "event_reason, upload_scope, status FROM live_event_queue"
    ).fetchall()
    if not rows:
        return

    # Lazy import to avoid a load-time dependency cycle and to keep
    # cloud_archive importable even if pipeline_queue_service is
    # somehow unavailable (a corrupt install or deferred package
    # initialisation during early gadget_web boot).
    from services import pipeline_queue_service as pqs

    pipeline_db = pqs.resolve_pipeline_db()
    if not pipeline_db or not os.path.exists(pipeline_db):
        logger.warning(
            "Cloud archive v4 migration: pipeline_queue DB not "
            "available (%s); cannot mirror %d live_event_queue row(s) "
            "before DROP. The DROP is aborted to preserve the rows.",
            pipeline_db, len(rows),
        )
        raise RuntimeError(
            "pipeline_queue DB unavailable; cannot mirror "
            "live_event_queue rows before DROP"
        )

    legacy_ids = [r[0] for r in rows]
    source_paths = [r[2] for r in rows if r[2]]
    existing_ids, existing_paths = _query_existing_mirrors(
        pipeline_db, legacy_ids, source_paths,
    )

    def _is_mirrored(row) -> bool:
        # A row is mirrored if EITHER its legacy_id is present OR its
        # event_json_path matches an existing pipeline_queue row's
        # source_path. The path-based fallback (originally filed as
        # #207, fixed inline) catches dev-branch installs where a
        # residual mirror row has NULL legacy_id but the right path.
        return row[0] in existing_ids or (row[2] and row[2] in existing_paths)

    # ------------------------------------------------------------------
    # Stale-mirror UPDATE (finding #5): for rows that ARE mirrored but
    # whose legacy status has advanced past the mirror's stage/status,
    # update the mirror so the worker doesn't re-upload completed work.
    # Carry just the legacy_id (re-review N3 — the row is unused below).
    # ------------------------------------------------------------------
    stale_updates: List[int] = [
        r[0] for r in rows
        if _is_mirrored(r) and r[6] == 'uploaded'
    ]
    refreshed = 0
    if stale_updates:
        refreshed = _refresh_stale_mirrors_to_done(pipeline_db, stale_updates)

    unmirrored = [r for r in rows if not _is_mirrored(r)]
    if not unmirrored:
        logger.info(
            "Cloud archive v4 migration: all %d live_event_queue "
            "row(s) already mirrored into pipeline_queue (%d stale "
            "mirror(s) refreshed); safe to DROP.",
            len(rows), refreshed,
        )
        return

    logger.warning(
        "Cloud archive v4 migration: %d unmirrored live_event_queue "
        "row(s) found (ids=%s); backfilling into pipeline_queue at "
        "PRIORITY_LIVE_EVENT before DROP.",
        len(unmirrored), [r[0] for r in unmirrored],
    )

    # Default 'failed' is conservative (review-pr finding #4): an
    # unrecognized legacy status must NOT accidentally cause the
    # cloud worker to re-upload a row whose true state we don't know.
    status_map = {
        'pending': 'pending',
        'uploading': 'in_progress',
        'uploaded': 'done',
        'failed': 'failed',
    }
    for r in unmirrored:
        legacy_id, event_dir, event_json_path, event_timestamp, \
            event_reason, upload_scope, legacy_status = r
        unified_status = status_map.get(legacy_status, 'failed')
        # Mirror as a cloud_pending/cloud_done row at PRIORITY_LIVE_EVENT
        # so the unified cloud worker picks it up on its next claim.
        # The ``stage='cloud_done'`` mapping for already-uploaded rows
        # ensures we don't re-upload completed work.
        stage = (
            pqs.STAGE_CLOUD_DONE if legacy_status == 'uploaded'
            else pqs.STAGE_CLOUD_PENDING
        )
        ok = pqs.dual_write_enqueue(
            source_path=event_json_path,
            stage=stage,
            legacy_table='live_event_queue',
            legacy_id=legacy_id,
            priority=pqs.PRIORITY_LIVE_EVENT,
            payload={
                'event_dir': event_dir,
                'event_timestamp': event_timestamp,
                'event_reason': event_reason,
                'upload_scope': upload_scope,
            },
            status=unified_status,
            db_path=pipeline_db,
        )
        if not ok:
            # ok=False can mean either:
            #   (a) UNIQUE-conflict idempotency — another writer (or
            #       a prior partial migration) already inserted a row
            #       with the same composite. Detected by the post-loop
            #       verification below — that row IS mirrored.
            #   (b) sqlite3.Error inside dual_write_enqueue (logged
            #       there at WARNING). Detected by the post-loop
            #       verification — that row is STILL not mirrored,
            #       and we must abort to preserve it.
            logger.info(
                "Cloud archive v4 migration: dual_write_enqueue "
                "returned False for live_event_queue.id=%d (%r); "
                "post-loop verification will distinguish UNIQUE-"
                "conflict idempotency from a DB error.",
                legacy_id, event_json_path,
            )

    # --------------------------------------------------------------
    # Post-loop verification (review-pr finding #1): re-query the
    # pipeline_queue for every unmirrored legacy_id AND source_path.
    # Anything still missing on BOTH keys means dual_write_enqueue
    # silently returned False on a sqlite3.Error (not a UNIQUE
    # idempotency match) and the row would be lost on DROP. Raise
    # so the caller rolls back and the DROP is skipped — the LES
    # table and its rows remain intact for the next migration
    # attempt. Using both keys lets the orphan-mirror edge (#207
    # fix) be recognised as "already mirrored" instead of triggering
    # an abort loop.
    # --------------------------------------------------------------
    after_ids, after_paths = _query_existing_mirrors(
        pipeline_db,
        [r[0] for r in unmirrored],
        [r[2] for r in unmirrored if r[2]],
    )
    still_missing = [
        r[0] for r in unmirrored
        if r[0] not in after_ids
        and not (r[2] and r[2] in after_paths)
    ]
    if still_missing:
        logger.error(
            "Cloud archive v4 migration: backfill failed for %d "
            "live_event_queue row(s) (ids=%s); aborting DROP to "
            "preserve the rows. The migration will retry on the "
            "next service start.",
            len(still_missing), still_missing,
        )
        raise RuntimeError(
            f"backfill incomplete: {len(still_missing)} "
            f"live_event_queue row(s) still unmirrored "
            f"(ids={still_missing}); aborting DROP"
        )


# Chunk size for ``WHERE legacy_id IN (?,?,...)`` queries.
# SQLite's SQLITE_MAX_VARIABLE_NUMBER is 32766 (3.32+) or 999 (older
# builds). 500 stays well under both ceilings (review-pr finding #2)
# while keeping per-query overhead negligible.
_LIVE_EVENT_BACKFILL_CHUNK = 500


def _query_existing_mirrors(
    pipeline_db: str,
    legacy_ids: List[int],
    source_paths: Optional[List[str]] = None,
) -> Tuple[set, set]:
    """Return ``(existing_legacy_ids, existing_source_paths)`` — the set
    of ``legacy_id`` values AND the set of ``source_path`` values that
    already have a ``pipeline_queue`` row with
    ``legacy_table='live_event_queue'``.

    Looking up by both keys is defensive against an edge case observed
    on dev-branch installs (originally filed as #207, fixed inline
    here): a pre-existing mirror row that has the right
    ``source_path`` but a NULL ``legacy_id`` would otherwise be
    invisible to the legacy-id ``IN (...)`` lookup, causing the
    backfill INSERT to hit the
    ``(source_path, stage, legacy_table)`` UNIQUE constraint, the
    post-loop verification to keep failing, and the migration to
    abort-loop on every service start. SQLite NULL never matches
    ``IN (...)``, so we widen the query to OR-match on
    ``source_path`` for the same ``legacy_table``. Canonical installs
    (0 LES rows or all-mirrored-by-id) are unaffected.

    Chunks the input into batches of :data:`_LIVE_EVENT_BACKFILL_CHUNK`
    to stay under SQLite's variable-count ceiling on older builds
    (``SQLITE_MAX_VARIABLE_NUMBER`` is 999 on pre-3.32 SQLite).
    Single short-lived connection across all chunks. The two input
    lists are chunked independently; the result sets are unioned
    across chunks.
    """
    if not legacy_ids and not source_paths:
        return set(), set()

    pipeline_conn = sqlite3.connect(pipeline_db, timeout=10.0)
    try:
        existing_ids: set = set()
        existing_paths: set = set()

        if legacy_ids:
            for start in range(0, len(legacy_ids), _LIVE_EVENT_BACKFILL_CHUNK):
                chunk = legacy_ids[start:start + _LIVE_EVENT_BACKFILL_CHUNK]
                placeholders = ','.join('?' * len(chunk))
                for row in pipeline_conn.execute(
                    f"SELECT legacy_id, source_path FROM pipeline_queue "
                    f"WHERE legacy_table='live_event_queue' "
                    f"AND legacy_id IN ({placeholders})",
                    chunk,
                ).fetchall():
                    if row[0] is not None:
                        existing_ids.add(row[0])
                    if row[1]:
                        existing_paths.add(row[1])

        if source_paths:
            for start in range(0, len(source_paths), _LIVE_EVENT_BACKFILL_CHUNK):
                chunk = source_paths[start:start + _LIVE_EVENT_BACKFILL_CHUNK]
                placeholders = ','.join('?' * len(chunk))
                for row in pipeline_conn.execute(
                    f"SELECT legacy_id, source_path FROM pipeline_queue "
                    f"WHERE legacy_table='live_event_queue' "
                    f"AND source_path IN ({placeholders})",
                    chunk,
                ).fetchall():
                    if row[0] is not None:
                        existing_ids.add(row[0])
                    if row[1]:
                        existing_paths.add(row[1])

        return existing_ids, existing_paths
    finally:
        try:
            pipeline_conn.close()
        except sqlite3.Error:
            pass


def _refresh_stale_mirrors_to_done(
    pipeline_db: str,
    stale_updates: List[int],
) -> int:
    """UPDATE existing pipeline_queue mirrors whose legacy status has
    advanced to ``'uploaded'`` but whose mirror is still in
    ``cloud_pending``. Without this, the cloud worker re-uploads
    completed live-event clips after the v4 migration runs.

    Single short-lived connection, single transaction. WHERE clause
    matches on ``(legacy_table, legacy_id)`` and is gated on
    ``stage <> 'cloud_done'`` so a re-running migration is a no-op for
    rows that are already done.

    Also clears ``claimed_by`` and ``claimed_at`` (re-review N4): if a
    previous cloud-worker run had claimed the row (``status='in_progress'``)
    and crashed before completing, the mirror would have stale claim
    metadata that survives the migration. Clearing it matches the
    pattern used by ``complete_pipeline_row``.

    Returns the number of rows actually updated (re-review N2 — the
    log line uses this rather than the input length so a partial
    no-op retry doesn't inflate the count).
    """
    if not stale_updates:
        return 0

    pipeline_conn = sqlite3.connect(pipeline_db, timeout=10.0)
    try:
        total_changed = 0
        for legacy_id in stale_updates:
            cur = pipeline_conn.execute(
                """
                UPDATE pipeline_queue
                SET stage='cloud_done',
                    status='done',
                    completed_at=?,
                    claimed_by=NULL,
                    claimed_at=NULL
                WHERE legacy_table='live_event_queue'
                  AND legacy_id=?
                  AND stage <> 'cloud_done'
                """,
                (time.time(), legacy_id),
            )
            total_changed += cur.rowcount
        pipeline_conn.commit()
        logger.info(
            "Cloud archive v4 migration: refreshed %d stale "
            "live-event mirror(s) to cloud_done (%d candidate(s) "
            "considered).",
            total_changed, len(stale_updates),
        )
        return total_changed
    finally:
        try:
            pipeline_conn.close()
        except sqlite3.Error:
            pass


# Backward-compat alias. The function was renamed from
# ``_reconcile_live_event_queue_into_pipeline`` to
# ``_backfill_missing_live_event_mirrors`` per review-pr finding #5
# (the original name implied a full reconcile but the function only
# inserts missing mirrors; UPDATE-of-stale was added as part of the
# rename). External callers (tests) reference the old name.
_reconcile_live_event_queue_into_pipeline = (
    _backfill_missing_live_event_mirrors
)


def _dual_write_pipeline_cloud_synced(file_path: str,
                                      remote_path: Optional[str],
                                      status: str,
                                      file_size: Optional[int] = None,
                                      file_mtime: Optional[float] = None) -> None:
    """Best-effort dual-write of a single ``cloud_synced_files`` row to
    the unified ``pipeline_queue`` table (issue #184 Wave 4 — Phase I.1).

    For one-off transitions (the ``uploading`` and ``queued`` insert
    sites). Reconcile loops should use
    :func:`_dual_write_pipeline_cloud_synced_batch` instead — it
    collapses N per-row connections into one fsync.

    Cross-DB write — opens a fresh ``geodata.db`` connection inside
    :func:`pipeline_queue_service.dual_write_enqueue` and closes it.
    Failures are logged at WARNING and swallowed; the legacy
    ``cloud_synced_files`` row remains the source of truth in
    Phase I.1.

    The ``status`` parameter is the legacy status string ('pending',
    'queued', 'uploading', 'synced', 'failed'); we translate to the
    unified stage/status pair: ``synced`` rows become
    ``stage='cloud_done', status='done'``; ``uploading``/``syncing``
    become ``status='in_progress'``; everything else is ``'pending'``.
    """
    try:
        from services import pipeline_queue_service as pqs
        stage = (
            pqs.STAGE_CLOUD_DONE if status == 'synced'
            else pqs.STAGE_CLOUD_PENDING
        )
        unified_status = {
            'pending': 'pending',
            'queued': 'pending',
            'uploading': 'in_progress',
            'syncing': 'in_progress',
            'synced': 'done',
            'failed': 'failed',
        }.get(status, 'pending')
        pqs.dual_write_enqueue(
            source_path=file_path,
            stage=stage,
            legacy_table=pqs.LEGACY_TABLE_CLOUD_SYNCED,
            priority=pqs.PRIORITY_CLOUD_BULK,
            dest_path=remote_path,
            payload={
                'file_size': file_size,
                'file_mtime': file_mtime,
                'legacy_status': status,
            },
            status=unified_status,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "pipeline_queue cloud-synced dual-write skipped for %s: %s",
            file_path, e,
        )


def _dual_write_pipeline_cloud_synced_batch(
    items: List[Tuple[str, Optional[str], str]],
) -> None:
    """Batched dual-write for the reconcile loops.

    ``items`` is a list of ``(file_path, remote_path, legacy_status)``
    tuples. Single connection, single fsync — replaces N per-row
    connections that would each cost a full fsync (~25–35 s of extra
    SDIO work for a 1000-file reconcile on a Pi Zero 2 W). Must be
    called AFTER the legacy ``cloud_sync.db`` ``conn.commit()`` so a
    crash mid-batch doesn't leave ``pipeline_queue`` orphans.

    Translates each legacy status to the unified status. Failures
    are logged at WARNING and swallowed.
    """
    if not items:
        return
    try:
        from services import pipeline_queue_service as pqs
        rows = []
        for file_path, remote_path, legacy_status in items:
            stage = (
                pqs.STAGE_CLOUD_DONE if legacy_status == 'synced'
                else pqs.STAGE_CLOUD_PENDING
            )
            unified_status = {
                'pending': 'pending',
                'queued': 'pending',
                'uploading': 'in_progress',
                'syncing': 'in_progress',
                'synced': 'done',
                'failed': 'failed',
            }.get(legacy_status, 'pending')
            rows.append({
                'source_path': file_path,
                'dest_path': remote_path,
                'stage': stage,
                'legacy_table': pqs.LEGACY_TABLE_CLOUD_SYNCED,
                'priority': pqs.PRIORITY_CLOUD_BULK,
                'payload': {'legacy_status': legacy_status},
                'status': unified_status,
            })
        pqs.dual_write_enqueue_many(rows)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "pipeline_queue cloud-synced batched dual-write skipped: %s", e,
        )


def _dual_write_pipeline_cloud_synced_state(
    file_path: str,
    *,
    new_stage: Optional[str] = None,
    status: Optional[str] = None,
    attempts: Optional[int] = None,
    last_error: Optional[str] = None,
    completed_at: Optional[float] = None,
    next_retry_at: Optional[float] = None,
) -> None:
    """State-transition dual-write for cloud_synced_files (Wave 4 PR-B).

    Unlike :func:`_dual_write_pipeline_cloud_synced` (which calls
    ``dual_write_enqueue`` and is INSERT-OR-IGNORE), this updates an
    existing pipeline_queue row to reflect a state transition (claim
    success → 'in_progress' → 'done' / 'failed' / 'pending').

    Looked up by ``(stage='cloud_pending', source_path=file_path)``.
    Failures are swallowed at DEBUG.
    """
    if not file_path:
        return
    try:
        from services import pipeline_queue_service as pqs
        # The current row's stage is always cloud_pending (cloud_done
        # is reached only on success and is itself terminal). For the
        # source_path lookup we use the original cloud_pending stage.
        pqs.update_pipeline_row(
            stage=pqs.STAGE_CLOUD_PENDING,
            source_path=file_path,
            new_stage=new_stage,
            status=status,
            attempts=attempts,
            last_error=last_error,
            completed_at=completed_at,
            next_retry_at=next_retry_at,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug(
            "pipeline_queue cloud-synced state dual-write skipped "
            "for %s: %s", file_path, e,
        )


# ---------------------------------------------------------------------------
# Wave 4 PR-F2 (issue #184): unified ``pipeline_queue`` integration helpers
# ---------------------------------------------------------------------------
# Three opt-in flags lay the foundation for PR-F3's reader cutover:
#
#   * ``CLOUD_ARCHIVE_ENQUEUE_TO_PIPELINE`` — PRODUCER. When True,
#     ``_discover_events`` enqueues each event into ``pipeline_queue``
#     with ``stage='cloud_pending'`` (idempotent via the existing
#     UNIQUE index). The legacy disk-walk + ``cloud_synced_files``
#     path continues to drive uploads — the producer hook only
#     POPULATES the unified queue so PR-F3's ``claim_next_for_stage``
#     reader has rows to claim instead of an empty table.
#
#   * ``CLOUD_ARCHIVE_SHADOW_PIPELINE_QUEUE`` — OBSERVABILITY. When
#     True (and the producer is also True), ``_drain_once`` peeks at
#     the top-N ``cloud_pending`` rows in ``pipeline_queue`` before
#     each upload pass and logs WARNING if the legacy reader's first
#     pick is absent from the pipeline's top-N window. Pure
#     observability — no behavioural change. Skipped when the
#     producer is OFF (would always disagree — no rows to compare)
#     and when the reader is ON (we ARE the pipeline reader, moot
#     comparison).
#
#   * ``CLOUD_ARCHIVE_USE_PIPELINE_READER`` — RESERVED for PR-F3.
#     Read here only for the shadow-skip predicate; PR-F3 will wire
#     the actual reader switch.
#
# All three default OFF (except the shadow flag which defaults ON
# but is gated on the producer flag) so a fresh deploy is a no-op
# pending operator opt-in.
# ---------------------------------------------------------------------------

# Shadow-mode log-rate limits (mirror archive_worker for journal
# hygiene). The first ``_CLOUD_SHADOW_DISAGREEMENT_LOG_VERBATIM``
# mismatches log verbatim; after that we drop to one heartbeat
# WARNING per ``_CLOUD_SHADOW_DISAGREEMENT_LOG_EVERY`` mismatches
# with the running count.
_CLOUD_SHADOW_AGREEMENT_LOG_EVERY = 500
_CLOUD_SHADOW_DISAGREEMENT_LOG_VERBATIM = 10
_CLOUD_SHADOW_DISAGREEMENT_LOG_EVERY = 100
# Number of pipeline_queue candidates we peek at when comparing
# against the legacy disk-walk's first pick. The legacy ranker uses
# ``_score_event_priority`` (event > geo > none) while pipeline_queue
# orders by ``priority, enqueued_at, id``. Both readers should agree
# on the high-priority bucket; the top-N window absorbs intra-band
# reordering (e.g. multiple Sentry events enqueued in a single boot
# scan all land at PRIORITY_CLOUD_BULK and tie on priority).
_CLOUD_SHADOW_PEEK_CANDIDATE_COUNT = 8

_cloud_shadow_lock = threading.Lock()
_cloud_shadow_agreement_count = 0
_cloud_shadow_disagreement_count = 0
_cloud_pipeline_enqueue_count = 0


def _enqueue_to_pipeline_enabled() -> bool:
    """Return True iff the producer hook is enabled.

    Wrapped in a function so a config reload (or test override) is
    picked up on the next ``_discover_events`` call without
    restarting the worker thread. Lazy import so the module can
    still be imported in test contexts where ``config`` isn't
    bootstrapped.
    """
    try:
        from config import CLOUD_ARCHIVE_ENQUEUE_TO_PIPELINE
        return bool(CLOUD_ARCHIVE_ENQUEUE_TO_PIPELINE)
    except Exception:  # noqa: BLE001
        return False


def _shadow_pipeline_queue_enabled() -> bool:
    """Return True iff the shadow-mode flag is enabled.

    Shadow comparison is only meaningful when the producer hook is
    also enabled (otherwise pipeline_queue has no ``cloud_pending``
    rows to compare against the disk-walk pick). Callers MUST also
    check :func:`_enqueue_to_pipeline_enabled` before invoking the
    shadow path; this helper only checks the flag itself.
    """
    try:
        from config import CLOUD_ARCHIVE_SHADOW_PIPELINE_QUEUE
        return bool(CLOUD_ARCHIVE_SHADOW_PIPELINE_QUEUE)
    except Exception:  # noqa: BLE001
        return False


def _use_pipeline_reader_enabled() -> bool:
    """Return True iff PR-F3's reader cutover flag is enabled.

    PR-F2 only reads this for the shadow-skip predicate (when the
    reader is ON we ARE the pipeline reader, so comparing ourselves
    to ourselves is moot). PR-F3 will wire the actual reader switch.
    """
    try:
        from config import CLOUD_ARCHIVE_USE_PIPELINE_READER
        return bool(CLOUD_ARCHIVE_USE_PIPELINE_READER)
    except Exception:  # noqa: BLE001
        return False


def _enqueue_event_to_pipeline(
    rel_path: str,
    *,
    event_dir: Optional[str] = None,
    event_size: Optional[int] = None,
    score: Optional[int] = None,
    priority: Optional[int] = None,
    producer: str = 'cloud_archive._discover_events',
) -> bool:
    """Enqueue one event into ``pipeline_queue`` with cloud_pending stage.

    PRODUCER hook for the unified queue. Idempotent: the
    ``pipeline_queue`` UNIQUE index on ``(stage, source_path)``
    means re-enqueuing the same event is a no-op (INSERT OR IGNORE
    inside :func:`pipeline_queue_service.dual_write_enqueue`).

    Best-effort — failures NEVER propagate (logged at WARNING by
    ``dual_write_enqueue``). The legacy disk-walk + dual-write path
    continues to drive uploads regardless of whether this enqueue
    succeeds, so a failed producer hook only delays PR-F3's reader
    by one cycle (the next ``_discover_events`` call retries).

    Single-row entry point — used by:
      * The bulk discovery loop (one row at a time fallback)
      * Wave 4 PR-F4 LIVE-EVENT hook: the file_watcher
        ``register_event_json_callback`` enqueues with
        ``priority=PRIORITY_LIVE_EVENT`` so cloud_archive picks the
        event up before any bulk catch-up rows
      * Tests
    The bulk discovery path uses
    :func:`_enqueue_events_to_pipeline_batch` instead, which
    collapses N per-row connections into one fsync (~25-35 s of
    SDIO savings on a 1000-event reconcile per PR-E review).

    Args:
        rel_path: Canonical relative POSIX path of the event (matches
            ``cloud_synced_files.file_path`` form). MUST be the same
            string the existing dual-write uses so the two paths
            collide on the UNIQUE index instead of producing two
            rows.
        event_dir: Local source directory (informational; stored in
            payload for future debugging).
        event_size: Total size in bytes (informational; stored in
            payload).
        score: ``_score_event_priority`` result. Lower is more
            urgent. Stored in payload so PR-F3 can re-rank rows
            without re-scoring against the disk.
        priority: Override the default ``PRIORITY_CLOUD_BULK``.
            Pass ``PRIORITY_LIVE_EVENT`` (= 0) from the file_watcher
            event.json hook to leapfrog the bulk catch-up queue.
            ``None`` → use the default (PR-F2 bulk producer).
        producer: Free-form provenance string stored in the payload.
            Defaults to the bulk producer name. The PR-F4 live-event
            hook passes ``'file_watcher.event_json'``.

    Returns True iff a new pipeline_queue row was inserted (False
    on idempotent re-enqueue OR error). The boolean is for tests
    and telemetry only — the legacy upload path is unaffected.
    """
    global _cloud_pipeline_enqueue_count
    if not rel_path:
        return False
    try:
        from services import pipeline_queue_service as pqs
        chosen_priority = (
            priority if priority is not None else pqs.PRIORITY_CLOUD_BULK
        )
        inserted = pqs.dual_write_enqueue(
            source_path=rel_path,
            stage=pqs.STAGE_CLOUD_PENDING,
            legacy_table=pqs.LEGACY_TABLE_CLOUD_SYNCED,
            priority=chosen_priority,
            payload={
                'event_dir': event_dir,
                'event_size': event_size,
                'score': score,
                'producer': producer,
            },
            status='pending',
        )
        if inserted:
            with _cloud_shadow_lock:
                _cloud_pipeline_enqueue_count += 1
        return bool(inserted)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "pipeline_queue cloud-pending producer hook failed "
            "for %s: %s", rel_path, e,
        )
        return False


def _enqueue_events_to_pipeline_batch(
    scored_events: List[Tuple[Tuple[str, str, int], int]],
) -> int:
    """Batched producer hook for the discovery path.

    Replaces N per-row :func:`_enqueue_event_to_pipeline` calls
    with one :func:`pipeline_queue_service.dual_write_enqueue_many`
    call. The single-connection / single-fsync save is significant
    on Pi Zero 2 W: ``_dual_write_pipeline_cloud_synced_batch``
    quantifies the equivalent reconcile-loop cost as ~25-35 s of
    extra SDIO work per 1000 rows; the producer-hook savings here
    scale identically since the underlying ``executemany``
    primitive is shared.

    ``scored_events`` is the post-sort output of
    :func:`_discover_events`'s scoring step:
    ``[((event_dir, rel_path, event_size), score), ...]``.

    Returns the count of newly-inserted rows. Re-enqueues that hit
    the UNIQUE index (idempotent no-op) do NOT count. Failures are
    logged at WARNING by ``dual_write_enqueue_many`` and NEVER
    propagate; the legacy disk-walk path continues unaffected.

    The producer-telemetry counter is bumped by the actual insert
    count (not by the input length) so the metric stays meaningful
    after the unique-index dedup.
    """
    global _cloud_pipeline_enqueue_count
    if not scored_events:
        return 0
    try:
        from services import pipeline_queue_service as pqs
        rows = []
        for ((event_dir, rel_path, event_size), score) in scored_events:
            if not rel_path:
                continue
            rows.append({
                'source_path': rel_path,
                'stage': pqs.STAGE_CLOUD_PENDING,
                'legacy_table': pqs.LEGACY_TABLE_CLOUD_SYNCED,
                'priority': pqs.PRIORITY_CLOUD_BULK,
                'payload': {
                    'event_dir': event_dir,
                    'event_size': event_size,
                    'score': score,
                    'producer': 'cloud_archive._discover_events',
                },
                'status': 'pending',
            })
        if not rows:
            return 0
        inserted = pqs.dual_write_enqueue_many(rows)
        if inserted:
            with _cloud_shadow_lock:
                _cloud_pipeline_enqueue_count += int(inserted)
        return int(inserted)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "pipeline_queue cloud-pending batched producer hook "
            "failed (%d rows): %s", len(scored_events), e,
        )
        return 0


def enqueue_live_event_from_event_json(event_json_paths: List[str]) -> int:
    """Enqueue Sentry/Saved events at LIVE priority into pipeline_queue.

    Wave 4 PR-F4 (issue #184): replaces the standalone
    ``live_event_sync_service`` worker. The file_watcher's
    ``register_event_json_callback`` fires this helper the moment Tesla
    writes a new ``event.json``; the unified cloud_archive worker (now
    a ``pipeline_queue`` reader after PR-F3 + flag flip) picks the row
    up before any bulk catch-up rows because the row is enqueued at
    ``PRIORITY_LIVE_EVENT = 0`` (vs. ``PRIORITY_CLOUD_BULK = 4``).

    The caller is the file_watcher inotify callback. Callbacks are
    invoked from the watcher thread so this MUST be cheap and MUST
    NEVER raise: failures only delay the upload by one bulk-discovery
    cycle (the next ``_discover_events`` pass will pick up the same
    event at the bulk priority).

    Canonical key — IMPORTANT
    -------------------------
    The ``source_path`` we enqueue MUST exactly match the form
    :func:`_discover_events` produces for the same event so the
    ``pipeline_queue.idx_pipeline_source_unique`` UNIQUE index dedups
    correctly. ``_discover_events`` enqueues the **event directory** as
    ``canonical_cloud_path("SentryClips/<dir>")`` — NOT the event.json
    path inside it. This helper therefore derives the directory from
    the inotify event_json path and runs the SAME canonicalisation.
    Mismatch = double-upload (one row at LIVE priority, one at BULK) —
    the regression PR-F4's review caught and this docstring exists to
    prevent recurrence.

    For each ``event.json`` path:

    * Compute the event directory (``os.path.dirname``); skip if the
      dir vanished between the inotify event and this call.
    * Canonicalise the **event directory** (not the event.json path)
      via :func:`_canonical_rel_path_from_local` so the resulting
      ``source_path`` matches the bulk producer's form exactly.
    * Compute the event size by summing the file sizes in the event
      directory; falls back to ``0`` if the dir vanished mid-call.
    * Call :func:`_enqueue_event_to_pipeline` with
      ``priority=PRIORITY_LIVE_EVENT`` and a ``producer`` tag that
      identifies this code path in pipeline forensics.

    Returns the count of newly-inserted rows. Re-enqueues that hit
    the UNIQUE index (the same event dir processed twice, OR a bulk
    discovery beat us to it) count as no-ops.

    Resource budget: this helper does NOT spawn a thread, NOT touch
    rclone, NOT open any heavy library. The whole call is one fsync
    per event in the same SQLite WAL the producer already uses.
    Steady-state RSS unchanged.
    """
    if not event_json_paths:
        return 0
    try:
        from services import pipeline_queue_service as pqs
        live_priority = pqs.PRIORITY_LIVE_EVENT
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "PR-F4 live-event hook: pipeline_queue_service import "
            "failed: %s — events will be picked up by next bulk pass",
            e,
        )
        return 0

    inserted_total = 0
    for event_json_path in event_json_paths:
        try:
            if not event_json_path:
                continue
            # Tesla writes event.json as the LAST file in the dir, so
            # the dir contents are stable by the time we get here.
            event_dir = os.path.dirname(event_json_path)
            if not event_dir or not os.path.isdir(event_dir):
                continue
            # Canonical relative path of the event DIRECTORY (not the
            # event.json file). MUST match _discover_events'
            # canonical_cloud_path("SentryClips/<dir>") form exactly so
            # the UNIQUE index dedups. See "Canonical key" docstring
            # section above for why a mismatch causes double-upload.
            try:
                rel_path = _canonical_rel_path_from_local(event_dir)
            except Exception:  # noqa: BLE001
                # Fall back to bulk-pass: skip enqueue rather than
                # risk a malformed key colliding with unrelated rows.
                logger.warning(
                    "PR-F4 live-event hook: canonical key derivation "
                    "raised for %r — deferring to next bulk pass",
                    event_dir,
                )
                continue
            if not rel_path:
                continue
            try:
                event_size = sum(
                    os.path.getsize(os.path.join(event_dir, name))
                    for name in os.listdir(event_dir)
                    if os.path.isfile(os.path.join(event_dir, name))
                )
            except OSError:
                event_size = 0
            try:
                ok = _enqueue_event_to_pipeline(
                    rel_path,
                    event_dir=event_dir,
                    event_size=event_size,
                    score=None,
                    priority=live_priority,
                    producer='file_watcher.event_json',
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "PR-F4 live-event hook: enqueue failed for %s: %s",
                    event_json_path, e,
                )
                continue
            if ok:
                inserted_total += 1
                logger.info(
                    "PR-F4 live-event: enqueued %s at LIVE priority",
                    rel_path,
                )
        except Exception as e:  # noqa: BLE001
            # Outer guard so a single bad path never breaks the batch.
            logger.warning(
                "PR-F4 live-event hook: unexpected error for %r: %s",
                event_json_path, e,
            )

    # Wake the cloud_archive worker so it picks up the live event
    # immediately (the worker idles on threading.Event.wait() between
    # cycles; without a wake it'd sit until the idle timeout).
    if inserted_total > 0:
        try:
            _wake.set()
        except Exception:  # noqa: BLE001
            pass
    return inserted_total


def _canonical_rel_path_from_local(local_path: str) -> str:
    """Convert an absolute local file/dir path to the canonical relative
    POSIX form used by ``cloud_synced_files.file_path`` and the
    ``pipeline_queue.source_path`` UNIQUE index.

    Strategy: strip the configured TeslaCam root prefix (RO mount or
    ArchivedClips) so SentryClips/Saved events normalize to the same
    relative path regardless of which view of the file was the inotify
    trigger source. Mirrors what :func:`_discover_events` does at
    discovery time so the producer hook's path collides on the UNIQUE
    index instead of producing two rows.

    The basename-only fallback at the end exists so an unexpected path
    shape never crashes the file_watcher thread, but it is **not** a
    safe canonical key — every unrelated unknown path would collapse
    onto the same row (the basename of every event dir is just the
    timestamp prefix, so all events from the same minute across all
    sources would alias). The fallback therefore logs at WARNING so a
    misconfigured deploy (RO_MNT_DIR / ARCHIVE_DIR pointing somewhere
    the watcher isn't actually reading from) is visible in
    ``journalctl -u gadget_web``; the row is still enqueued so the
    bulk pass can correct the canonical form on the next discovery.
    """
    candidates: List[str] = []
    try:
        from config import RO_MNT_DIR
        candidates.append(os.path.join(RO_MNT_DIR, 'part1-ro', 'TeslaCam'))
    except Exception:  # noqa: BLE001
        pass
    try:
        from config import ARCHIVE_DIR
        candidates.append(ARCHIVE_DIR)
    except Exception:  # noqa: BLE001
        pass

    abs_path = os.path.abspath(local_path)
    for prefix in candidates:
        if not prefix:
            continue
        prefix_abs = os.path.abspath(prefix)
        # commonpath returns the prefix only when abs_path is inside
        # it; otherwise it raises ValueError or returns a shorter root.
        try:
            common = os.path.commonpath([prefix_abs, abs_path])
        except ValueError:
            continue
        if common == prefix_abs:
            rel = os.path.relpath(abs_path, prefix_abs)
            # Force POSIX separators so Linux paths match canonical
            # form even on Windows test runs.
            return rel.replace(os.sep, '/')
    # Last resort: return the basename so we don't crash the watcher
    # thread. NOT a safe canonical key — see docstring above. Logged
    # at WARNING because silent collapse would otherwise look like
    # successful enqueues that secretly all alias the same row.
    logger.warning(
        "PR-F4 live-event hook: %r is not under any known TeslaCam "
        "root (checked %r). Falling back to basename %r — UNIQUE-index "
        "collisions are likely. Verify RO_MNT_DIR and ARCHIVE_DIR "
        "config values match where the file_watcher is observing.",
        abs_path, candidates, os.path.basename(abs_path),
    )
    return os.path.basename(abs_path)


def _shadow_compare_cloud_picks(
    *,
    legacy_path: Optional[str],
    pipeline_candidates: Tuple[str, ...] = (),
) -> None:
    """Compare the legacy disk-walk's first pick against the pipeline top-N.

    Mirrors :func:`archive_worker._shadow_compare_picks` for the cloud
    queue. The legacy ``_discover_events`` ranker uses
    ``_score_event_priority`` (event > geo > none) while
    ``pipeline_queue`` orders by ``priority, enqueued_at, id``.

    Cloud-specific subtlety: every ``cloud_pending`` row enqueued by
    the producer hook lands at the same ``PRIORITY_CLOUD_BULK`` value
    (the per-event score is stored in ``payload_json`` for PR-F3 but
    NOT used as the queue's primary sort key). So the pipeline reader's
    intra-band order collapses to ``enqueued_at, id`` — purely the
    discovery-time sequence. That diverges in two benign ways from
    the legacy disk-walk's ranking:

    * **Score reordering.** The legacy reader sorts by score; a high-
      priority Sentry event arriving after a long backlog band of
      lower-score clips lands at the BOTTOM of the pipeline top-N
      (newest enqueued_at) but at the TOP of the legacy pick. PR-F3's
      claim path will need to honour the score-from-payload to match
      the legacy ranker; until then this manifests as a top-N miss.
    * **Discovery cadence.** The legacy disk-walk re-sorts every
      ``_drain_once``; pipeline rows preserve their original
      enqueued_at across drains, so an event re-enqueued by an
      idempotent producer doesn't bubble up.

    To absorb both, agreement requires only that the legacy pick
    appear ANYWHERE in the pipeline's top-N window. Only when it's
    absent (a real producer-hook gap — a file the legacy walker
    found that the producer somehow missed enqueueing) does the
    WARNING fire.

    Empty queue case: both ``legacy_path`` is ``None`` AND
    ``pipeline_candidates`` is empty ⇒ both readers say "queue
    empty" ⇒ counted as agreement, no log. If the legacy reader has
    work but the pipeline doesn't, that's a benign ordering case
    (the producer enqueued but pipeline_queue's WAL hasn't
    propagated yet — extremely rare); we treat it as a benign miss
    rather than a gap.

    Disagreement logging is rate-limited identically to the archive
    shadow: first ``_CLOUD_SHADOW_DISAGREEMENT_LOG_VERBATIM``
    mismatches log verbatim, then one heartbeat WARNING per
    ``_CLOUD_SHADOW_DISAGREEMENT_LOG_EVERY``.
    """
    global _cloud_shadow_agreement_count, _cloud_shadow_disagreement_count
    candidate_set = (
        frozenset(p for p in pipeline_candidates if p)
        if pipeline_candidates else frozenset()
    )
    if legacy_path is None:
        with _cloud_shadow_lock:
            _cloud_shadow_agreement_count += 1
            count = _cloud_shadow_agreement_count
        if count % _CLOUD_SHADOW_AGREEMENT_LOG_EVERY == 0:
            logger.info(
                "Wave 4 PR-F2 cloud shadow: pipeline_queue agreed "
                "with cloud_archive on %d consecutive picks",
                count,
            )
        return
    if legacy_path in candidate_set:
        with _cloud_shadow_lock:
            _cloud_shadow_agreement_count += 1
            count = _cloud_shadow_agreement_count
        if count % _CLOUD_SHADOW_AGREEMENT_LOG_EVERY == 0:
            logger.info(
                "Wave 4 PR-F2 cloud shadow: pipeline_queue agreed "
                "with cloud_archive on %d consecutive picks "
                "(top-%d window)",
                count, _CLOUD_SHADOW_PEEK_CANDIDATE_COUNT,
            )
        return
    with _cloud_shadow_lock:
        _cloud_shadow_disagreement_count += 1
        d_count = _cloud_shadow_disagreement_count
    if d_count <= _CLOUD_SHADOW_DISAGREEMENT_LOG_VERBATIM:
        logger.warning(
            "Wave 4 PR-F2 cloud shadow: cloud_archive picked %r but "
            "it is absent from the top-%d pipeline_queue candidates "
            "(disagreement #%d). pipeline top-%d=%r. Cloud "
            "pipeline_queue rows all share priority "
            "PRIORITY_CLOUD_BULK so the intra-band order is purely "
            "enqueued_at — a high-score event arriving after a long "
            "backlog band can legitimately land below the top-%d "
            "window even though the producer hook fired correctly. "
            "A miss is therefore EITHER a real producer-hook gap "
            "(the file is absent from pipeline_queue entirely — "
            "investigate before PR-F3) OR a transient score-reorder "
            "near the window boundary (will self-correct as the "
            "queue drains). Cross-check with COUNT(*) FROM "
            "pipeline_queue WHERE source_path = the missed path to "
            "tell the two cases apart.",
            legacy_path, _CLOUD_SHADOW_PEEK_CANDIDATE_COUNT, d_count,
            _CLOUD_SHADOW_PEEK_CANDIDATE_COUNT,
            tuple(pipeline_candidates),
            _CLOUD_SHADOW_PEEK_CANDIDATE_COUNT,
        )
    elif d_count % _CLOUD_SHADOW_DISAGREEMENT_LOG_EVERY == 0:
        logger.warning(
            "Wave 4 PR-F2 cloud shadow: cloud_archive / "
            "pipeline_queue disagreement count = %d (suppressing "
            "per-event WARNINGs after the first %d; first %d are "
            "above). Last legacy pick: %r.",
            d_count, _CLOUD_SHADOW_DISAGREEMENT_LOG_VERBATIM,
            _CLOUD_SHADOW_DISAGREEMENT_LOG_VERBATIM, legacy_path,
        )


def get_cloud_shadow_telemetry() -> Dict[str, int]:
    """Return the in-memory cloud shadow + producer counters as a snapshot.

    Process-local, reset on restart. Used by tests and by the
    Settings page to confirm shadow mode is firing in production.
    Keys:

    * ``cloud_shadow_agreement_count`` — consecutive picks where
      legacy and pipeline agreed (or both were empty).
    * ``cloud_shadow_disagreement_count`` — picks where legacy's
      choice was absent from the pipeline's top-N.
    * ``cloud_pipeline_enqueue_count`` — successful (rowcount > 0)
      producer-hook enqueues this process. Re-enqueues that hit the
      UNIQUE index (idempotent no-op) do NOT increment.
    """
    with _cloud_shadow_lock:
        return {
            'cloud_shadow_agreement_count': _cloud_shadow_agreement_count,
            'cloud_shadow_disagreement_count': _cloud_shadow_disagreement_count,
            'cloud_pipeline_enqueue_count': _cloud_pipeline_enqueue_count,
        }


def _reset_cloud_shadow_telemetry_for_tests() -> None:
    """Reset shadow + producer counters. Test-only helper.

    Production code MUST NOT call this — the counters are intended
    to monotonically increment for the process lifetime so the
    Settings page can show "since boot" totals.
    """
    global _cloud_shadow_agreement_count, _cloud_shadow_disagreement_count
    global _cloud_pipeline_enqueue_count
    with _cloud_shadow_lock:
        _cloud_shadow_agreement_count = 0
        _cloud_shadow_disagreement_count = 0
        _cloud_pipeline_enqueue_count = 0


def _peek_pipeline_cloud_pending(limit: int = _CLOUD_SHADOW_PEEK_CANDIDATE_COUNT
                                 ) -> Tuple[str, ...]:
    """Return the top-N source paths from pipeline_queue cloud_pending.

    Best-effort wrapper around
    :func:`pipeline_queue_service.peek_top_n_paths_for_stage`.
    Failures return an empty tuple (logged at DEBUG); the caller
    treats that as "pipeline queue empty" and the shadow comparison
    continues with no false-positive disagreement.
    """
    try:
        from services import pipeline_queue_service as pqs
        return tuple(
            pqs.peek_top_n_paths_for_stage(
                stage=pqs.STAGE_CLOUD_PENDING,
                limit=limit,
            )
        )
    except Exception as e:  # noqa: BLE001
        logger.debug(
            "shadow peek_top_n_paths_for_stage(cloud_pending) "
            "failed: %s", e,
        )
        return ()


# ---------------------------------------------------------------------------
# Wave 4 PR-F3 (issue #184): cloud reader-switch helpers
# ---------------------------------------------------------------------------
# PR-F3 mirrors PR-F1's archive-worker reader switch for cloud_archive.
# When ``CLOUD_ARCHIVE_USE_PIPELINE_READER`` is True (default OFF), the
# drain pass replaces the disk-walk + ``cloud_synced_files`` filter with
# a batch ``claim_next_for_stage`` against ``pipeline_queue``. The
# upload loop body is structurally unchanged — it still iterates a list
# of ``(event_dir, rel_path, event_size)`` tuples — so the existing
# state-transition dual-write hooks (PR-B) drive the pipeline_queue row
# from ``in_progress`` (set by claim) to ``done`` (set by the
# ``cloud_synced_files`` UPDATE on success) without any new wiring.
#
# Cloud rows are mirrored from ``cloud_synced_files`` LAZILY: the
# producer in ``_discover_events`` enqueues with
# ``source_path = canonical_cloud_path("SentryClips/<event_dir>")`` (the
# event DIRECTORY, NOT the event.json file inside it) but does NOT set
# ``legacy_id`` because the corresponding ``cloud_synced_files`` row is
# not created until upload starts. The PR-F4 live-event hook
# (:func:`enqueue_live_event_from_event_json`) MUST canonicalise the
# event directory the same way so the ``idx_pipeline_source_unique``
# index dedups across producers — see that helper's "Canonical key"
# docstring section. That's also why the release-claim helper uses the
# cloud-specific :func:`pqs.release_pipeline_claim_by_source_path`
# (added by PR-F3) instead of the legacy_id-keyed PR-F1 helper.
# ---------------------------------------------------------------------------

# Default upper bound on the number of rows claimed in a single drain
# pass. The legacy disk-walk has no equivalent — it sees everything in
# ``_discover_events`` then trims by cloud capacity. The reader path
# claims a bounded batch so a cancel/error doesn't strand an unbounded
# number of in_progress rows. ``recover_stale_claims_pipeline`` will
# eventually release stragglers if the worker crashes mid-batch, but
# bounding the batch keeps the recovery surface small.
_CLOUD_PIPELINE_READER_BATCH_SIZE = 32


def _claim_via_pipeline_reader_cloud(
    worker_id: str,
    db_path: Optional[str] = None,
    limit: int = _CLOUD_PIPELINE_READER_BATCH_SIZE,
) -> List[Tuple[str, str, int]]:
    """Claim up to ``limit`` cloud_pending rows from pipeline_queue.

    Wave 4 PR-F3 (issue #184): the cloud-side mirror of
    :func:`archive_worker._claim_via_pipeline_reader`.

    Returns a list of ``(event_dir, rel_path, event_size)`` tuples
    shaped exactly like ``_discover_events`` so the existing
    ``_drain_once`` upload loop can iterate the result without any
    branch-by-branch changes.

    Each successful claim atomically sets ``status='in_progress'``,
    bumps ``attempts``, and persists ``claimed_by`` / ``claimed_at``.
    Caller responsibilities:
      * On upload success → existing PR-B dual-write hook fires from
        ``cloud_synced_files`` UPDATE → pipeline row goes to
        ``status='done'``. No PR-F3-specific code required.
      * On upload failure → existing PR-B failure-mirror dual-write
        hook updates ``last_error`` / ``attempts``. No PR-F3-specific
        code required.
      * On early cancel (cancel event fires mid-batch, leaving N
        unprocessed claims) → caller MUST call
        :func:`_release_cloud_pipeline_claims` with the unprocessed
        ``rel_path`` list so those rows return to ``status='pending'``
        and don't accrue ``attempts`` for work that never started.

    Defensive cases (data-shape gaps that should never happen in
    production but are handled instead of silently corrupting the
    queue):
      * Pipeline row missing ``source_path`` → moved to
        ``status='dead_letter'`` (unrecoverable; an empty path is not
        something a recovery cycle can fix). One WARNING per
        occurrence — operators inspect via
        ``/api/pipeline_queue/dead_letter``.
      * Pipeline row's ``payload`` missing ``event_dir`` → released
        back to ``pending`` so the next ``_discover_events`` pass
        can re-enqueue with the correct payload. One WARNING per
        occurrence.

    The cloud reader claims a BATCH up front (vs. the per-row claim
    archive_worker uses) because cloud_archive's drain loop is built
    around iterating a discovered list, not "claim → process → claim
    again". Re-shaping the loop to per-row claim would be an
    invasive refactor with no measurable upside (cloud's per-event
    dwell time is dominated by the rclone subprocess, not the claim
    overhead).
    """
    try:
        from services import pipeline_queue_service as pqs
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "PR-F3 cloud reader: pipeline_queue_service import "
            "failed (%s) — falling back to no-op", e,
        )
        return []

    claimed: List[Tuple[str, str, int]] = []
    for _ in range(max(int(limit), 0)):
        try:
            row = pqs.claim_next_for_stage(
                stage=pqs.STAGE_CLOUD_PENDING,
                claimed_by=worker_id,
                db_path=db_path,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "PR-F3 cloud reader: claim_next_for_stage raised: %s",
                e,
            )
            break
        if row is None:
            break
        rel_path = (row.get('source_path') or '').strip()
        if not rel_path:
            # Unrecoverable data-shape gap — empty source_path can
            # never be matched by ``release_pipeline_claim_by_source_path``
            # (which keys on the ``(stage, source_path)`` UNIQUE index)
            # nor by ``recover_stale_claims_pipeline`` (which would
            # release the row back to pending only for the same gap
            # to fire again next claim — a recycle loop). Move the
            # row to ``dead_letter`` immediately by primary-key id
            # via the dedicated helper. Operators can inspect the
            # dead-letter rows via the upcoming
            # ``/api/pipeline_queue/dead_letter`` endpoint and either
            # manually backfill ``source_path`` or drop the row.
            #
            # This mirrors PR-F1's archive-side fix for the
            # legacy_id-missing case (see ``archive_worker._claim_via
            # _pipeline_reader`` ~L2168) so both reader paths behave
            # the same way under unrecoverable data-shape gaps.
            row_id = row.get('id')
            try:
                pqs.dead_letter_pipeline_row_by_id(
                    row_id=row_id,
                    last_error=(
                        'PR-F3: pipeline_queue cloud_pending row has '
                        'empty source_path (unrecoverable data-shape '
                        'gap); manual intervention required'
                    ),
                    db_path=db_path,
                )
            except Exception:  # noqa: BLE001
                pass
            logger.warning(
                "PR-F3 cloud reader: claimed row id=%s has empty "
                "source_path — moved to dead_letter (unrecoverable)",
                row_id,
            )
            continue
        payload = row.get('payload') or {}
        event_dir = (payload.get('event_dir') or '').strip()
        try:
            event_size = int(payload.get('event_size') or 0)
        except (TypeError, ValueError):
            event_size = 0
        if not event_dir:
            # Recoverable: the next _discover_events pass will
            # re-enqueue with the correct payload (idempotent via the
            # UNIQUE index on (stage, source_path)). Release the
            # claim back to pending so attempts isn't bumped for work
            # that didn't start.
            try:
                pqs.release_pipeline_claim_by_source_path(
                    stage=pqs.STAGE_CLOUD_PENDING,
                    source_path=rel_path,
                    last_error=(
                        'PR-F3: pipeline_queue payload missing '
                        'event_dir; released for re-enqueue'
                    ),
                    db_path=db_path,
                )
            except Exception:  # noqa: BLE001
                pass
            logger.warning(
                "PR-F3 cloud reader: claimed row for %r has no "
                "event_dir in payload — released for re-enqueue",
                rel_path,
            )
            continue
        claimed.append((event_dir, rel_path, event_size))
    return claimed


def _release_cloud_pipeline_claims(
    rel_paths: Sequence[str],
    last_error: str,
    db_path: Optional[str] = None,
) -> int:
    """Release in-progress cloud_pending claims back to ``pending``.

    Wave 4 PR-F3 (issue #184): used by ``_drain_once`` when the
    upload loop exits early (cancel, cloud-full, exception) leaving
    N unprocessed claims in ``status='in_progress'``. Without this
    release the rows would sit until ``recover_stale_claims_pipeline``
    times them out, which is wasteful (and bumps ``attempts`` for
    work that never started).

    ``rel_paths`` is intentionally typed as :class:`Sequence` (not
    :class:`Iterable`) so callers cannot pass a single-shot
    generator — the empty-check below would consume it before the
    iteration loop, silently skipping every release. Callers always
    pass a list (the ``unprocessed_pipeline_claims`` straggler list
    from ``_drain_once``); a tuple would also be safe.

    Returns the count of rows actually released (rowcount > 0).
    Never raises — silent at DEBUG on per-row failures so a transient
    sqlite glitch can't abort the drain wind-down.
    """
    # ``len(rel_paths) == 0`` and ``not rel_paths`` are equivalent
    # for ``Sequence`` and both safely short-circuit; we use the
    # explicit length check to make the Sequence contract obvious
    # at the call site.
    if not rel_paths:
        return 0
    released = 0
    try:
        from services import pipeline_queue_service as pqs
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "PR-F3 cloud reader: pipeline_queue_service import "
            "failed during release (%s) — claims will rely on "
            "stale-claim recovery", e,
        )
        return 0
    for rel_path in rel_paths:
        if not rel_path:
            continue
        try:
            ok = pqs.release_pipeline_claim_by_source_path(
                stage=pqs.STAGE_CLOUD_PENDING,
                source_path=rel_path,
                last_error=last_error,
                db_path=db_path,
            )
            if ok:
                released += 1
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "PR-F3 cloud reader: release for %r failed: %s",
                rel_path, e,
            )
    return released


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

        # v3 (#132): ``previous_last_error`` column for multi-cycle
        # failure history. The ALTER is **independent** of the v2 path
        # canonicalization — ``_mark_upload_failure`` writes this column
        # on every failure, so it MUST exist even when v2 keeps failing
        # on a corrupt-row install. Run unconditionally; ALTER is
        # idempotent (duplicate-column OperationalError is caught).
        # The version bump below is still gated on ``migration_ok`` so
        # we don't claim full v3 status while v2 work is incomplete.
        if current < 3:
            try:
                conn.execute(
                    "ALTER TABLE cloud_synced_files "
                    "ADD COLUMN previous_last_error TEXT"
                )
            except sqlite3.OperationalError:
                pass

        # v4 (#202): drop the orphaned ``live_event_queue`` table left
        # behind when the standalone Live Event Sync subsystem was
        # deleted in Wave 4 PR-F4 (issue #184). Gated on
        # ``migration_ok`` (review-pr finding #3): if v2 failed and
        # rolled back, we must NOT do v4 work in a fresh implicit
        # transaction that downstream commits would persist while the
        # version bump is correctly skipped. The inner try/except
        # below is the per-version retry envelope.
        if current < 4 and migration_ok:
            try:
                _migrate_drop_live_event_queue_v4(conn, db_path)
            except Exception as e:  # noqa: BLE001
                migration_ok = False
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                logger.error(
                    "Cloud archive v4 migration failed (%s); rolled "
                    "back partial work and leaving schema at v%d. "
                    "Migration will retry on next service start.",
                    e, current,
                )

        # v5 (#218 follow-up): adds the ``cloud_archive_meta`` key/value
        # table and the ``idx_cloud_synced_synced_at`` index. Both are
        # created idempotently by the ``executescript(_CLOUD_TABLES_SQL)``
        # call at the top of this block, so no separate migration
        # function is needed. The table backs the dashboard "Reset
        # Stats" button (stats_baseline_at row) WITHOUT touching the
        # dedup-critical cloud_synced_files rows that prevent
        # already-synced clips from being re-uploaded. The new index
        # makes the baseline-filtered SUM(file_size) / COUNT(*) queries
        # in ``get_sync_stats`` cheap even after years of sync history.

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
            # Wave 4 PR-B: capture interrupted-upload paths BEFORE
            # the UPDATE so pipeline_queue can be reset to 'pending'
            # too (it holds 'in_progress' from the original claim
            # dual-write).
            interrupted_paths = [
                r["file_path"] for r in conn.execute(
                    "SELECT file_path FROM cloud_synced_files "
                    "WHERE status = 'uploading'"
                ).fetchall()
            ]
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
                for fp in interrupted_paths:
                    _dual_write_pipeline_cloud_synced_state(
                        fp,
                        status='pending',
                    )
        except Exception as e:
            logger.warning("Startup recovery failed: %s", e)

    return conn


# ---------------------------------------------------------------------------
# Priority Scoring
# ---------------------------------------------------------------------------

def _load_geo_hits(db_path: Optional[str] = None) -> Optional[Set[str]]:
    """Pre-fetch the set of "anchors" — strings that legacy
    ``_score_event_priority`` would match via ``LIKE '%dir_name%'`` —
    extracted from every non-NULL ``waypoints.video_path`` row.

    Phase 5.2 — replaces the per-event ``get_db_connection() → SELECT … LIKE``
    pattern in :func:`_score_event_priority`. For a queue with N candidate
    events the legacy code opened N fresh SQLite connections and ran one
    full-scan ``LIKE '%dir%'`` query each. This helper does ONE connection
    and ONE ``SELECT DISTINCT video_path`` then derives every possible
    ``dir_name`` anchor in Python (set-build) so the per-event check
    collapses to an O(1) ``in`` lookup.

    The legacy LIKE substring pattern actually accepts THREE anchor shapes
    because :func:`_discover_events` calls the scorer with two different
    ``event_dir`` shapes:

      * **Nested event dirs** (``SentryClips/2026-05-12_10-00-00``) —
        ``dir_name`` is a 19-char timestamp.
      * **Flat ArchivedClips files** (``ArchivedClips/2026-05-12_10-00-00-front.mp4``) —
        ``dir_name`` is the full filename (``os.path.basename`` of the
        FILE, not a directory).

    And ``waypoints.video_path`` in production is stored as
    ``ArchivedClips/<basename>`` — never the original nested
    ``SentryClips/<event-dir>/<file>`` form. So we MUST look at the
    file's basename and the leading-19-char timestamp prefix, not
    just the parent-dir basename. (PR #143 reviewer caught this: the
    parent-dir-only build always produced ``"ArchivedClips"`` and
    silently demoted geo-tier events to the no-geo tier, which under
    ``sync_non_event_videos: false`` would drop them from the queue.)

    For each ``video_path`` we add three anchors to the set:
      1. ``os.path.basename(os.path.dirname(vp))`` — catches the
         nested-event-dir naming pattern (``RecentClips`` /
         ``ArchivedClips`` are always added but never collide with a
         real event-dir name, so they're harmless noise).
      2. ``os.path.basename(vp)`` — catches the flat-ArchivedClips
         file-basename naming pattern.
      3. The leading 19-char ``YYYY-MM-DD_HH-MM-SS`` timestamp prefix
         of the file basename — catches the nested-event-dir case
         even when the indexer has rewritten the path to flat form
         (which is the production situation).

    Uses raw ``sqlite3.connect`` rather than
    ``mapping_queries.get_db_connection`` because the latter runs the
    full v6 schema initialization on every call. This helper is
    read-only and called once per discover pass — schema migration is
    a different lifecycle and belongs in the indexing path, not the
    cloud picker. Skipping it also means ``_load_geo_hits`` doesn't
    crash when geodata.db is missing or partially initialized: it
    returns ``None`` so the scorer falls back to the legacy per-event
    query path.

    Returns:
      * a ``set`` of anchor strings (may be empty if waypoints table
        has no rows) — caller should use ``in`` for the per-event check.
      * ``None`` if mapping is disabled, the DB doesn't exist, or the
        query raises (no waypoints table, locked, etc.) — caller MUST
        treat ``None`` as "fall back to per-event lookup" so behaviour
        matches the legacy code path.
    """
    try:
        from config import MAPPING_ENABLED, MAPPING_DB_PATH
    except ImportError:
        return None
    if not MAPPING_ENABLED:
        return None
    path = db_path or MAPPING_DB_PATH

    # Refuse to create the DB if it doesn't exist — that would mask
    # real configuration problems and create an empty file the indexer
    # would then try to migrate. ``None`` is the right "I have no info"
    # response.
    if not path or not os.path.isfile(path):
        return None

    try:
        conn = sqlite3.connect(path, timeout=5.0)
    except Exception:
        return None

    hits: Set[str] = set()
    try:
        rows = conn.execute(
            "SELECT DISTINCT video_path FROM waypoints "
            "WHERE video_path IS NOT NULL"
        ).fetchall()
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # YYYY-MM-DD_HH-MM-SS Tesla event timestamp pattern (19 chars).
    # Pre-compiled in the function body — we only get here once per
    # discover pass, so the compile cost is negligible and keeps the
    # helper self-contained.
    import re
    _TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}")

    for row in rows:
        # Plain sqlite3 connection (no row_factory) → tuple access.
        try:
            vp = row[0]
        except (IndexError, TypeError):
            continue
        if not vp:
            continue

        # Anchor 1: parent-dir basename. Catches the nested
        # ``SentryClips/<event-dir>/<file>`` naming convention. In
        # current production this is almost always ``ArchivedClips`` /
        # ``RecentClips`` (because the indexer rewrites paths to the
        # flat form), which is harmless noise: those names never
        # collide with a real Tesla event-dir basename.
        parent = os.path.basename(os.path.dirname(vp))
        if parent:
            hits.add(parent)

        # Anchor 2: file basename. Catches flat ArchivedClips files
        # where ``_discover_events`` calls the scorer with a full
        # filename as the ``event_dir`` argument.
        base = os.path.basename(vp)
        if base:
            hits.add(base)

            # Anchor 3: leading 19-char Tesla-timestamp prefix of the
            # file basename. Catches the nested-event-dir case (where
            # ``dir_name`` is just the 19-char timestamp) even when
            # the indexer has rewritten the path to the flat form
            # (which is the production situation per PR #143 review).
            m = _TS_RE.match(base)
            if m:
                hits.add(m.group(0))

    return hits


def _score_event_priority(
    event_dir: str,
    geo_hits: Optional[Set[str]] = None,
) -> int:
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
        # Phase 5.2 — fast path: caller pre-fetched the geo-hit set in a
        # single ``SELECT DISTINCT video_path`` query. Skip the per-event
        # SQLite connection entirely. ``geo_hits is None`` means "no
        # batched lookup available" (mapping disabled, import failed,
        # query raised) → fall through to the legacy per-event query so
        # behaviour matches direct callers (tests).
        if geo_hits is not None:
            if dir_name in geo_hits:
                score = 100
        else:
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


# Valid sync-folder names. ``RecentClips`` was supported historically
# but is excluded from the allow-list because Tesla rotates that folder
# hourly — uploading the rolling buffer wastes bandwidth and the clips
# disappear before the next sync run anyway. ``ArchivedClips`` (the
# SD-card-resident copy preserved by the archive subsystem) is the
# correct target for "all driving footage" uploads.
_VALID_SYNC_FOLDERS = ("SentryClips", "SavedClips", "ArchivedClips")

# Multiplier applied to the folder-priority index when composing the
# per-event sort key in ``_discover_events``. Must be strictly larger
# than the maximum content score returned by ``_score_event_priority``
# (currently 200 + 99 age = 299) so the user-configured folder order
# is guaranteed to dominate the sort regardless of clip age or trigger
# type.
_FOLDER_PRIORITY_MULTIPLIER = 1000


def _normalize_folder_list(values: object) -> List[str]:
    """Coerce a config value into a clean folder list.

    * Filters out non-string entries (defensive against malformed YAML).
    * Normalises legacy ``RecentClips`` entries to ``ArchivedClips`` —
      RecentClips rotates hourly so it was never a useful sync target;
      operators with old config.yaml installs (RecentClips checked)
      should silently start syncing the SD-card archive instead.
    * Drops anything not in ``_VALID_SYNC_FOLDERS`` after normalisation.
    * Deduplicates while preserving order (so ``priority_order``
      semantics survive the rewrite).
    """
    if not isinstance(values, (list, tuple)):
        return []
    seen = []
    for v in values:
        if not isinstance(v, str):
            continue
        folder = v.strip()
        if folder == "RecentClips":
            folder = "ArchivedClips"
        if folder in _VALID_SYNC_FOLDERS and folder not in seen:
            seen.append(folder)
    return seen


def _read_sync_folders_setting() -> List[str]:
    """Re-read ``cloud_archive.sync_folders`` from config.yaml.

    Same per-call YAML re-read pattern as
    :func:`_read_sync_non_event_setting` so a Settings change takes
    effect on the next sync iteration without restarting
    ``gadget_web.service``. The returned list is filtered through
    :func:`_normalize_folder_list` so legacy ``RecentClips`` values are
    silently rewritten to ``ArchivedClips`` and unknown folder names
    are dropped. Empty result falls back to the import-time default to
    avoid a config typo silently disabling all sync.
    """
    try:
        import yaml
        from config import CONFIG_YAML
        with open(CONFIG_YAML, 'r') as f:
            cfg = yaml.safe_load(f) or {}
        raw = cfg.get('cloud_archive', {}).get('sync_folders', None)
        normalised = _normalize_folder_list(raw) if raw is not None else []
        if normalised:
            return normalised
    except Exception:
        pass
    fallback = _normalize_folder_list(list(CLOUD_ARCHIVE_SYNC_FOLDERS))
    return fallback or list(_VALID_SYNC_FOLDERS)


def _read_priority_order_setting() -> List[str]:
    """Re-read ``cloud_archive.priority_order`` from config.yaml.

    Same per-call YAML re-read pattern as
    :func:`_read_sync_non_event_setting`. The returned list is filtered
    through :func:`_normalize_folder_list`. Empty result falls back to
    the live ``sync_folders`` order (any folder being synced is at
    least as important as not being in the priority list at all).
    """
    try:
        import yaml
        from config import CONFIG_YAML
        with open(CONFIG_YAML, 'r') as f:
            cfg = yaml.safe_load(f) or {}
        raw = cfg.get('cloud_archive', {}).get('priority_order', None)
        normalised = _normalize_folder_list(raw) if raw is not None else []
        if normalised:
            return normalised
    except Exception:
        pass
    # Final fallback: import-time default, then if even that's empty
    # use sync_folders so priority sort still does something useful.
    fallback = _normalize_folder_list(list(CLOUD_ARCHIVE_PRIORITY_ORDER))
    return fallback or _read_sync_folders_setting()


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
) -> Optional[Tuple[str, int]]:
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

    Returns ``(post_status, post_retry_count)`` so the caller can mirror
    the new state into ``pipeline_queue`` AFTER ``_fsync_db(conn)`` has
    committed the legacy row. Wave 4 PR-B invariant: legacy commit FIRST,
    then mirror — never the other way around. Returns ``None`` when the
    UPDATE matched zero rows.
    """
    cap = _read_retry_max_attempts_setting()
    cur = conn.execute(
        """UPDATE cloud_synced_files
           SET status = CASE
                   WHEN retry_count + 1 >= ? THEN 'dead_letter'
                   ELSE 'failed'
               END,
               previous_last_error = last_error,
               last_error = ?,
               retry_count = retry_count + 1
           WHERE file_path = ?""",
        (cap, err_msg, rel_path),
    )
    if not cur.rowcount:
        return None
    # Re-read the row to know which terminal state we landed in so
    # the log message is accurate. Cheap (single indexed lookup).
    post = conn.execute(
        "SELECT status, retry_count FROM cloud_synced_files "
        "WHERE file_path = ?",
        (rel_path,),
    ).fetchone()
    if not post:
        return None
    if post["status"] == 'dead_letter':
        logger.warning(
            "Cloud sync: %s reached retry cap (%d attempts) — "
            "moved to dead_letter. Recover via Failed Jobs page.",
            rel_path, post["retry_count"],
        )
    return str(post["status"]), int(post["retry_count"] or 0)


def _is_path_skipped(
    conn: Optional[sqlite3.Connection],
    rel_path: str,
) -> bool:
    """Phase 5.3 — streaming dedup check.

    Replaces the legacy ``synced_paths`` in-memory set (which loaded EVERY
    ``cloud_synced_files`` row matching ``status IN ('synced', 'dead_letter')``
    into Python — ~8 MB on a year-old database). Now does one indexed
    point-lookup per candidate event:

    * The ``file_path TEXT NOT NULL UNIQUE`` constraint creates an implicit
      unique index, so the lookup is O(log n) on the underlying B-tree.
    * SQLite's prepared-statement cache on the connection means the SQL text
      is parsed once and reused for every subsequent call.
    * Memory overhead is the per-row tuple/dict the cursor returns — bounded
      and freed immediately, vs the unbounded set that grew with database age.

    Returns ``False`` when *conn* is ``None`` (matches legacy behaviour: no
    connection means "we can't tell, don't filter") or when the query raises
    (best-effort — the picker continues even if the DB is briefly busy).
    """
    if conn is None:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM cloud_synced_files "
            "WHERE file_path = ? AND status IN ('synced', 'dead_letter') "
            "LIMIT 1",
            (rel_path,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        # Best-effort dedup: SQLite errors (closed conn, locked DB, etc.)
        # mean we can't tell — let the picker through. Programmer errors
        # (TypeError from a wrong-type rel_path, AttributeError from a
        # broken proxy) are NOT swallowed so they surface during dev.
        return False


def _folder_priority_index(folder: str, priority_order: List[str]) -> int:
    """Return the position of *folder* in *priority_order*, or len() if absent.

    Used by ``_discover_events`` to multiply against the per-event content
    score so the user-configured folder order is the PRIMARY sort axis.
    Folders not in the priority list are sorted to the end of the queue
    (they'll still upload, but only after every configured folder is
    drained).
    """
    try:
        return priority_order.index(folder)
    except ValueError:
        return len(priority_order)


def _folder_of_event_rel(rel_path: str) -> str:
    """Extract the parent folder name from a canonical relative path.

    ``"SentryClips/2026-05-12_10-00-00"`` → ``"SentryClips"``
    ``"ArchivedClips/foo.mp4"``           → ``"ArchivedClips"``

    Returns ``""`` if the path has no leading folder component (which
    should not happen for paths produced by ``canonical_cloud_path``
    but is handled defensively so a malformed entry sorts to the end).
    """
    if not rel_path:
        return ""
    first_slash = rel_path.find("/")
    if first_slash < 0:
        return ""
    return rel_path[:first_slash]


def _discover_events(
    teslacam_path: str,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Tuple[str, str, int]]:
    """Find event directories and archived clips to sync.

    Syncs event subdirectories from SentryClips/SavedClips plus flat files
    from ArchivedClips on the SD card. Returns a list of
    ``(event_dir_path, relative_path, total_size)`` sorted by the
    user-configured ``cloud_archive.priority_order`` (folder-level
    primary axis) and then by ``_score_event_priority`` within each
    folder (event-trigger > geo-located > other, oldest-first within
    each band).

    If *conn* is provided, events already marked ``synced`` or ``dead_letter``
    in the database are excluded via ``_is_path_skipped`` (Phase 5.3
    streaming dedup — one indexed point-lookup per candidate, no in-memory
    snapshot of the table).

    ``ArchivedClips`` is gated on membership in
    ``CLOUD_ARCHIVE_SYNC_FOLDERS``. Removing it from the Settings page
    therefore stops including SD-card archived clips in the upload
    queue (mirroring the behaviour of unchecking ``SentryClips`` or
    ``SavedClips``). The legacy "always-on" behaviour was a foot-gun
    because the UI exposed a checkbox that had no effect on
    ArchivedClips.
    """
    # Phase 5.3 — streaming dedup. Each candidate event is checked with a
    # single indexed ``_is_path_skipped`` lookup; we no longer load the
    # entire ``cloud_synced_files`` history into a Python set up-front.
    # ``conn=None`` keeps the legacy "don't filter" semantics (helpers /
    # tests that don't pass a connection see every event).

    events: List[Tuple[str, str, int]] = []

    # Re-read sync_folders from the live config so a Settings save
    # takes effect on the next discovery without restarting the
    # service. CLOUD_ARCHIVE_SYNC_FOLDERS is snapshotted at config.py
    # import time, so we re-read here for the freshest view.
    sync_folders = _read_sync_folders_setting()

    for folder in sync_folders:
        # ArchivedClips lives on the SD card (ARCHIVE_DIR), not under
        # ``teslacam_path``. Handle it in the dedicated block below so
        # we walk the right directory tree.
        if folder == "ArchivedClips":
            continue

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

            # Skip events already confirmed synced (Phase 5.3 streaming check)
            if _is_path_skipped(conn, rel_path):
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

    # ArchivedClips on the SD card — flat files. Only include when the
    # user has checked ArchivedClips in the Settings ``sync_folders``
    # list. The legacy code unconditionally appended these clips even
    # when the user had unchecked every folder; that silently uploaded
    # archived footage the operator had explicitly opted out of and is
    # exactly the foot-gun this gate fixes.
    if "ArchivedClips" in sync_folders:
        try:
            from config import ARCHIVE_DIR, ARCHIVE_ENABLED
            if ARCHIVE_ENABLED and os.path.isdir(ARCHIVE_DIR):
                try:
                    for f in sorted(os.listdir(ARCHIVE_DIR)):
                        fpath = os.path.join(ARCHIVE_DIR, f)
                        if os.path.isfile(fpath) and f.lower().endswith(('.mp4', '.ts')):
                            rel_path = canonical_cloud_path(f"ArchivedClips/{f}")
                            if _is_path_skipped(conn, rel_path):
                                continue
                            fsize = os.path.getsize(fpath)
                            # Use the individual file path (not ARCHIVE_DIR)
                            # so rclone copyto can handle file-to-file copy
                            events.append((fpath, rel_path, fsize))
                except OSError:
                    pass
        except ImportError:
            pass

    # Phase 5.2 — pre-fetch the geo-hit set ONCE so the scorer doesn't
    # open a fresh SQLite connection per event. For a queue of N candidate
    # events the legacy per-event ``LIKE '%dir%'`` pattern was N
    # connection-open + N full-scan queries; now it's ONE
    # ``SELECT DISTINCT video_path`` + N O(1) set lookups. ``None``
    # signals fallback to per-event lookup (mapping disabled / import
    # failed / query raised) so behaviour matches direct callers.
    geo_hits = _load_geo_hits()

    # Score every candidate once so we can both filter and sort without
    # invoking the (relatively expensive) scorer twice. Score >= 200 means
    # neither an event.json trigger nor any waypoint geolocation hit was
    # found — i.e. routine driving footage.
    scored: List[Tuple[Tuple[str, str, int], int]] = [
        (t, _score_event_priority(t[0], geo_hits=geo_hits)) for t in events
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

    # Apply the user-configured folder priority as the PRIMARY sort axis.
    # ``priority_order`` is a list like ``['SentryClips', 'SavedClips',
    # 'ArchivedClips']`` — items in earlier positions get a smaller
    # composite score and are uploaded first. Within each folder the
    # existing per-event content score (event-trigger > geo-located >
    # other, oldest-first within each band) preserves the
    # "preserve-the-most-at-risk-first" intent.
    #
    # Composite score = folder_index * _FOLDER_PRIORITY_MULTIPLIER + content_score.
    # _FOLDER_PRIORITY_MULTIPLIER (1000) is strictly larger than the
    # maximum content score (200 + age cap of 99 = 299), so the folder
    # axis is guaranteed to dominate even for ancient clips in a
    # lower-priority folder.
    priority_order = _read_priority_order_setting()
    composite = [
        (
            t,
            _folder_priority_index(
                _folder_of_event_rel(t[1]), priority_order,
            ) * _FOLDER_PRIORITY_MULTIPLIER + s,
        )
        for (t, s) in scored
    ]
    composite.sort(key=lambda x: x[1])
    scored = composite
    result = [t for (t, _s) in scored]

    # Wave 4 PR-F2 (issue #184): PRODUCER hook for unified pipeline_queue.
    # When the operator opts in via ``cloud_archive.enqueue_to_pipeline``,
    # mirror every discovered event into pipeline_queue with
    # ``stage='cloud_pending'``. Idempotent (UNIQUE index) — re-enqueues
    # are no-ops. Failures are logged at WARNING by the producer helper
    # and NEVER block the disk-walk path; the legacy reader continues
    # to drive uploads regardless of producer-hook outcome. PR-F3 will
    # then flip the reader to claim from pipeline_queue.
    #
    # Batched (single connection / single fsync) — the per-row variant
    # would cost ~25-35 s of extra SDIO work per 1000 events on a Pi
    # Zero 2 W (per ``_dual_write_pipeline_cloud_synced_batch`` docstring).
    # On a fresh-install backlog this matters; on incremental drains
    # it's still measurably cheaper than N round-trips.
    if _enqueue_to_pipeline_enabled():
        _enqueue_events_to_pipeline_batch(scored)

    return result


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

    Issue #165: when ``creds`` carries an explicit ``"type"`` key (the
    generic-rclone-remote flow puts the real backend type there), it
    wins over the ``provider`` argument — that argument is then just
    a label for the existing OAuth providers (``"onedrive"``, etc.).
    Keys beginning with ``_`` are private metadata
    (e.g. ``_obscure_keys``, ``_source``) and never reach the conf
    file; rclone would treat them as unknown options and fail.
    """
    os.makedirs(_RCLONE_TMPFS_DIR, exist_ok=True)

    # Resolve the rclone backend type. The creds dict's "type" wins
    # because (a) the generic flow stores the truth there and (b) for
    # legacy OAuth creds, save_credentials writes the same value to
    # creds["type"] anyway, so this is a no-op for them.
    rclone_type = creds.get("type") or provider

    # Build minimal rclone remote config. Skip "type" in the loop
    # (we just emitted it) and skip private metadata keys.
    #
    # Defense in depth (PR #218 review): keys/values that contain a
    # forbidden control character would let an attacker inject extra
    # config lines (e.g. an sftp ``ssh`` directive → command exec as
    # root, an s3 ``endpoint`` override → upload redirection).
    # ``save_credentials_generic`` already rejects these at save time;
    # this is the last guard before rclone reads the file. We use a
    # local helper instead of importing from cloud_rclone_service
    # because cloud_rclone_service imports from THIS module and a
    # circular import would break startup.
    _BAD_CHARS = ("\n", "\r", "\x00")
    lines = ["[teslausb]", f"type = {rclone_type}"]
    for key, value in creds.items():
        if not isinstance(key, str):
            continue
        if key == "type" or key.startswith("_"):
            continue
        s_value = "" if value is None else str(value)
        if any(c in key for c in _BAD_CHARS) or any(c in s_value for c in _BAD_CHARS):
            logger.error(
                "Refusing to write creds entry to rclone.conf "
                "(control character in key=%r or value)", key,
            )
            continue
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

def _list_remote_tree(
    conf_path: str,
    remote_path: str,
    mem_flags: list,
) -> Optional[Dict[str, Set[str]]]:
    """Phase 5.4 — single-call batched remote listing.

    Replaces 3 separate ``rclone lsf`` invocations (one per parent folder
    in ``CLOUD_ARCHIVE_SYNC_FOLDERS`` plus one for ``ArchivedClips``) with
    a single ``rclone lsf --recursive --max-depth=2`` call. Each rclone
    invocation costs roughly 100–500 ms in subprocess + auth-handshake +
    network round-trip overhead; collapsing them shaves ~1 s off every
    reconcile pass and matches the spec in #102 (item 5.4).

    Returns a dict keyed by parent folder name (e.g. ``"SentryClips"``,
    ``"SavedClips"``, ``"ArchivedClips"``) mapping to a set of relative
    entries directly under that parent. For event folders the entries
    are sub-directory names (with the trailing slash that ``rclone lsf``
    appends — used by the caller to distinguish dirs from files);
    for ``ArchivedClips`` the entries are file basenames.

    Returns ``None`` if the rclone call fails — the caller falls through
    to the legacy per-folder path so reconciliation still happens (just
    slower). Returns an empty dict if the remote is reachable but empty.
    """
    # Reconciliation must scan EVERY folder that could possibly contain
    # previously-uploaded rows, not just the folders the operator is
    # currently configured to sync. If the user unchecks ``SentryClips``
    # today we still need to discover existing SentryClips rows on the
    # remote so they get marked ``synced`` rather than re-uploaded the
    # next time the box is checked. Use the canonical ``_KNOWN_CLOUD_ROOTS``
    # list — it includes legacy ``RecentClips`` for installs that ever
    # uploaded the rolling buffer before the folder choice was
    # narrowed.
    interest = list(_KNOWN_CLOUD_ROOTS)
    try:
        result = subprocess.run(
            ["rclone", "lsf", "--config", conf_path,
             "--recursive", "--max-depth", "2",
             *mem_flags,
             f"teslausb:{remote_path}/"],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Reconcile timeout listing remote tree (>120s)")
        return None
    except Exception as e:
        logger.warning("Reconcile error listing remote tree: %s", e)
        return None

    if result.returncode != 0:
        logger.warning(
            "rclone lsf --recursive returned %d; falling back to per-folder",
            result.returncode,
        )
        return None

    out: Dict[str, Set[str]] = {p: set() for p in interest}
    for raw in result.stdout.split("\n"):
        line = raw.strip()
        if not line:
            continue
        # rclone lsf --recursive emits paths relative to the listed dir,
        # e.g. "SentryClips/2026-05-12_10-00-00/" or
        # "ArchivedClips/2026-05-12_10-00-00-front.mp4". --max-depth=2
        # ensures we get exactly one slash inside ArchivedClips and at
        # most one slash inside event folders (the trailing one for dirs).
        parts = line.split("/", 1)
        if len(parts) != 2:
            continue
        parent, rest = parts
        if parent not in out:
            continue
        if not rest:
            # Bare parent dir entry — skip.
            continue
        out[parent].add(rest)
    return out


def _reconcile_with_remote(
    conn: sqlite3.Connection,
    conf_path: str,
    remote_path: str,
    mem_flags: list,
) -> int:
    """Mark locally-pending files as synced if they already exist on the remote.

    Phase 5.4 — uses ``_list_remote_tree`` for a single batched
    ``rclone lsf --recursive --max-depth=2`` call, replacing the legacy
    one-call-per-parent loop. Falls back to the legacy per-folder path
    if the batched call fails so reconciliation still happens.

    Updates matching DB entries from pending/failed → synced, and
    inserts new 'synced' entries for remote files not yet tracked in the DB
    (e.g., files uploaded before tracking was implemented).
    Returns the number of entries reconciled.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    reconciled = 0
    # Defer dual-writes until AFTER ``conn.commit()`` so a crash
    # mid-loop can't leave orphan ``pipeline_queue`` rows referencing
    # legacy IDs that were never persisted. Single batched call also
    # collapses N per-row geodata.db connections (~25 ms each on Pi
    # Zero 2 W's SD) into one fsync — critical because reconcile
    # fires on WiFi-up, when the SDIO bus is already busy.
    pending_pipeline: List[Tuple[str, Optional[str], str]] = []

    tree = _list_remote_tree(conf_path, remote_path, mem_flags)
    if tree is None:
        # Fallback: legacy per-folder listing keeps reconcile working
        # even when the batched call fails (network blip, rclone version
        # mismatch, etc.). Same behaviour as before this PR.
        return _reconcile_with_remote_legacy(
            conn, conf_path, remote_path, mem_flags,
        )

    # List event directories on remote (SentryClips/*, SavedClips/*) — now
    # served from the single batched listing. Folder set is fixed by Tesla
    # firmware (``_EVENT_FOLDER_NAMES``); reconciliation must scan these
    # regardless of which folders the user currently has checked in
    # Settings — see ``_EVENT_FOLDER_NAMES`` docstring for why.
    for folder in _EVENT_FOLDER_NAMES:
        try:
            entries = tree.get(folder, set())
            # event-dir entries have a trailing slash from rclone lsf;
            # files (which shouldn't appear in event parents but might
            # from earlier corruption) get filtered out here.
            remote_dirs = {e.rstrip('/') for e in entries if e.endswith('/')}
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
                    pending_pipeline.append((rel_path, remote_dest, 'synced'))
                    continue

                # If not in DB at all, insert as synced (event dirs)
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
                    pending_pipeline.append((rel_path, remote_dest, 'synced'))
        except Exception as e:
            logger.warning("Reconcile error for %s: %s", folder, e)

    # ArchivedClips files — also from the same batched listing.
    try:
        entries = tree.get("ArchivedClips", set())
        # Strip trailing slashes too — rclone lsf may return directory
        # entries when a folder gets mistakenly created on the remote
        # (PRE-2.7 this produced corrupt rows like
        # ``ArchivedClips/foo.mp4/`` that broke later dedup checks).
        remote_files = {e.rstrip('/') for e in entries if e.strip()}
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
                pending_pipeline.append((rel_path, remote_dest, 'synced'))
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
                pending_pipeline.append((rel_path, remote_dest, 'synced'))
    except Exception as e:
        logger.warning("Reconcile error for ArchivedClips: %s", e)

    if reconciled:
        conn.commit()
        logger.info("Cloud reconciliation: marked %d already-uploaded entries as synced", reconciled)

    # Flush deferred pipeline_queue dual-writes AFTER the legacy
    # commit succeeds. One connection / one fsync for the whole batch.
    if pending_pipeline:
        _dual_write_pipeline_cloud_synced_batch(pending_pipeline)

    return reconciled


def _reconcile_with_remote_legacy(
    conn: sqlite3.Connection,
    conf_path: str,
    remote_path: str,
    mem_flags: list,
) -> int:
    """Pre-Phase-5.4 reconcile: one ``rclone lsf`` call per parent folder.

    Kept as a fallback for ``_reconcile_with_remote`` when the single
    batched ``--recursive --max-depth=2`` call fails — slower but
    proven against every rclone version this device has shipped with.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    reconciled = 0
    pending_pipeline: List[Tuple[str, Optional[str], str]] = []

    # List event directories on remote (SentryClips/*, SavedClips/*).
    # Folder set is fixed by Tesla firmware (``_EVENT_FOLDER_NAMES``); see
    # the constant docstring for why this is NOT
    # ``CLOUD_ARCHIVE_SYNC_FOLDERS``.
    for folder in _EVENT_FOLDER_NAMES:
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
                    pending_pipeline.append((rel_path, remote_dest, 'synced'))
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
                    pending_pipeline.append((rel_path, remote_dest, 'synced'))
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
                    pending_pipeline.append((rel_path, remote_dest, 'synced'))
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
                    pending_pipeline.append((rel_path, remote_dest, 'synced'))
    except Exception as e:
        logger.warning("Reconcile error for ArchivedClips: %s", e)

    if reconciled:
        conn.commit()
        logger.info(
            "Cloud reconciliation (legacy fallback): marked %d already-uploaded entries as synced",
            reconciled,
        )

    # Flush deferred pipeline_queue dual-writes AFTER the legacy
    # commit succeeds. One connection / one fsync for the whole batch.
    if pending_pipeline:
        _dual_write_pipeline_cloud_synced_batch(pending_pipeline)

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
    # Wave 4 PR-F3 (issue #184): the reader-switch tracking
    # variables. Initialised here (outside the try block) so the
    # finally block can always reference them, even if the try body
    # raised before the reader-switch branch ran.
    used_pipeline_reader = False
    unprocessed_pipeline_claims: List[str] = []

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

        # Wave 4 PR-F3 (issue #184): reader-switch branch.
        #
        # When ``CLOUD_ARCHIVE_USE_PIPELINE_READER`` is ON, the work
        # list comes from claiming rows out of ``pipeline_queue``
        # instead of walking the disk. The producer hook in
        # ``_discover_events`` (PR-F2) feeds the queue, so the two
        # flags are effectively coupled in production: turning the
        # reader on without the producer would yield an empty queue
        # and starve uploads. The shadow path (PR-F2) is auto-skipped
        # when reader is ON because comparing the legacy reader's
        # pick against ourselves is moot.
        #
        # Each claimed row goes to ``status='in_progress'`` and bumps
        # ``attempts``. The existing per-event upload loop's
        # state-transition dual-writes (PR-B) drive the row to
        # ``status='done'`` on success and ``status='pending'/
        # 'failed'/'dead_letter'`` on failure — no PR-F3-specific
        # success/failure wiring required. Only the EARLY-CANCEL case
        # needs a release pass (handled in the ``finally`` block at
        # the end of this function so cancel/exception/cloud-full all
        # release stragglers).
        used_pipeline_reader = _use_pipeline_reader_enabled()
        if used_pipeline_reader:
            try:
                to_sync = _claim_via_pipeline_reader_cloud(
                    worker_id='cloud_archive',
                    db_path=None,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "_claim_via_pipeline_reader_cloud raised: %s — "
                    "falling back to legacy disk-walk for this drain",
                    e,
                )
                to_sync = _discover_events(teslacam_path, conn=conn)
                used_pipeline_reader = False
            else:
                # Track the claimed paths so the finally block can
                # release any stragglers if the upload loop exits
                # early (cancel, cloud-full, exception).
                unprocessed_pipeline_claims = [
                    rel_path for _, rel_path, _ in to_sync
                ]
        else:
            to_sync = _discover_events(teslacam_path, conn=conn)

        # Wave 4 PR-F2 (issue #184): SHADOW comparison against the
        # unified ``pipeline_queue`` cloud_pending stage. Cheap (one
        # ``SELECT source_path FROM pipeline_queue WHERE
        # stage='cloud_pending' ... LIMIT N``) and pure observability —
        # no behavioural change. Gated on:
        #   * Producer flag ON (otherwise pipeline has no rows to
        #     compare; the helper does NOT log when both are empty so
        #     the noise floor is zero).
        #   * Reader flag OFF (when ON we ARE the pipeline reader, so
        #     comparing ourselves to ourselves is moot).
        # Failures are swallowed at DEBUG by ``_peek_pipeline_cloud_pending``
        # so a transient pipeline_queue read error never blocks the
        # legacy upload path.
        if (_enqueue_to_pipeline_enabled() and
                _shadow_pipeline_queue_enabled() and
                not used_pipeline_reader):
            try:
                shadow_candidates = _peek_pipeline_cloud_pending()
                legacy_first = to_sync[0][1] if to_sync else None
                _shadow_compare_cloud_picks(
                    legacy_path=legacy_first,
                    pipeline_candidates=shadow_candidates,
                )
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "Cloud shadow comparison swallowed exception: %s", e,
                )

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

        # Phase 5.7: a single ``rclone about`` call serves BOTH
        # purposes — token refresh AND capacity check. The legacy
        # implementation issued two back-to-back ``rclone about``
        # subprocess calls (one to force a token refresh, one to
        # parse free/total bytes), each ~1-3 s on a slow uplink.
        # The provider returns identical data both times; we now
        # capture once and reuse.
        cloud_reserve_bytes = int(CLOUD_ARCHIVE_RESERVE_GB * 1024 * 1024 * 1024)
        cloud_free_bytes: Optional[int] = None
        try:
            about_result = subprocess.run(
                ["rclone", "about", "--config", conf_path, "teslausb:", "--json"],
                capture_output=True, text=True, timeout=30,
            )
            # Side-effect of ``rclone about``: rclone refreshes the
            # OAuth token and writes it back into ``conf_path``.
            # Capture the refreshed token into the live creds dict so
            # subsequent calls use the new token, even if this single
            # ``about`` invocation failed to parse a free byte count
            # (some providers omit ``free`` from the JSON).
            try:
                from services.cloud_rclone_service import _capture_refreshed_token
                _capture_refreshed_token(creds)
            except Exception:
                pass

            if about_result.returncode == 0:
                import json as _json
                try:
                    about = _json.loads(about_result.stdout)
                except (_json.JSONDecodeError, ValueError) as e:
                    logger.warning(
                        "Could not parse rclone about JSON: %s", e,
                    )
                    about = {}
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
                # Wave 4 PR-F3 (issue #184): the cloud-full skip
                # leaves the current row + all remaining rows
                # unprocessed. The break drops to the loop exit so
                # the finally block can release the entire residual
                # ``unprocessed_pipeline_claims`` list back to
                # pending — including this row, which we have NOT
                # yet removed from the tracking list.
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
            _dual_write_pipeline_cloud_synced(
                rel_path, None, 'uploading',
                file_size=event_size, file_mtime=time.time(),
            )

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
                    _dual_write_pipeline_cloud_synced_state(
                        rel_path, status='pending',
                    )
                    # Wave 4 PR-F3 (issue #184): the dual-write above
                    # already returned this row to status='pending';
                    # don't let the finally block double-release it
                    # (which would overwrite the dual-write's
                    # last_error with a misleading "drain ended
                    # early" stamp).
                    if used_pipeline_reader:
                        try:
                            unprocessed_pipeline_claims.remove(rel_path)
                        except ValueError:
                            pass
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
                    # Wave 4 PR-B (review #191 Info #7): capture
                    # pipeline_queue's completed_at BEFORE the legacy
                    # UPDATE so the mirror reflects the moment the
                    # legacy commit/fsync actually happened.
                    completed_at = time.time()
                    conn.execute(
                        """UPDATE cloud_synced_files
                           SET status = 'synced', synced_at = ?, remote_path = ?,
                               retry_count = 0, last_error = NULL
                           WHERE file_path = ?""",
                        (now_synced, remote_dest, rel_path)
                    )
                    _fsync_db(conn)
                    # Wave 4 PR-B: terminal — promote pipeline_queue row
                    # to cloud_done / done. Done after fsync so a crash
                    # mid-flight can't leave the mirror ahead of the
                    # legacy row.
                    _dual_write_pipeline_cloud_synced_state(
                        rel_path,
                        new_stage='cloud_done',
                        status='done',
                        attempts=0,
                        last_error='',
                        completed_at=completed_at,
                    )
                else:
                    err_msg = (stderr or "").strip()[:500]
                    logger.error("Sync: [%d/%d] %s FAILED (exit %d): %s",
                                idx + 1, len(to_sync), rel_path,
                                returncode, err_msg[:200])
                    truncated_err = err_msg[:255]
                    post = _mark_upload_failure(
                        conn, rel_path, truncated_err,
                    )
                    _fsync_db(conn)
                    # Wave 4 PR-B: mirror the failure into pipeline_queue
                    # ONLY after _fsync_db has committed the legacy row.
                    # The stage stays under ``cloud_pending`` even on
                    # dead_letter — the ``status='dead_letter'`` value
                    # is what excludes the row from the auto-picker.
                    if post is not None:
                        post_status, post_attempts = post
                        _dual_write_pipeline_cloud_synced_state(
                            rel_path,
                            status=post_status,
                            attempts=post_attempts,
                            last_error=truncated_err,
                        )

            except Exception as e:
                logger.error("Sync: %s error: %s", rel_path, e)
                truncated_err = str(e)[:255]
                post = _mark_upload_failure(
                    conn, rel_path, truncated_err,
                )
                _fsync_db(conn)
                if post is not None:
                    post_status, post_attempts = post
                    _dual_write_pipeline_cloud_synced_state(
                        rel_path,
                        status=post_status,
                        attempts=post_attempts,
                        last_error=truncated_err,
                    )

            # Wave 4 PR-F3 (issue #184): mark this row as "no longer
            # claimed by this drain" so the finally block won't
            # release it back to pending. The PR-B dual-write hooks
            # above have already moved the row to its terminal state
            # (status='done' on success, status='failed' / 'pending'
            # / 'dead_letter' on failure via _mark_upload_failure).
            # The release pass in the finally block only acts on
            # rows STILL in_progress — which is exactly the ones we
            # claimed but didn't actually process (cancel / cloud-
            # full / unhandled exception).
            if used_pipeline_reader:
                try:
                    unprocessed_pipeline_claims.remove(rel_path)
                except ValueError:
                    # Path wasn't in the list (legacy reader path,
                    # or duplicate processing). Safe to ignore.
                    pass

            # Wave 4 PR-F4 (issue #184): the inter-file LES yield has
            # been removed. Live events are now first-class
            # ``pipeline_queue`` rows enqueued at
            # ``PRIORITY_LIVE_EVENT = 0`` by the file_watcher
            # event.json hook (see :func:`enqueue_live_event_from_event_json`).
            # The reader's natural priority order means a live event
            # arriving mid-drain is picked up on the very next claim
            # — no separate worker, no separate queue, no inter-file
            # yielding logic needed. (The previous implementation
            # released the task_coordinator lock and slept up to 5
            # minutes per yield, which added measurable latency to
            # every drain even when LES had nothing to do.)
            #
            # Pause between uploads to let the system breathe.
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
        # Wave 4 PR-F3 (issue #184): if the reader switch was on for
        # this drain and the upload loop exited early (cancel mid-
        # batch, cloud-full mid-batch, exception), some claimed rows
        # may still be ``status='in_progress'``. Release them back
        # to pending so the next drain pass can re-claim them
        # without waiting for the stale-claim recovery cycle. Each
        # successful event removed itself from
        # ``unprocessed_pipeline_claims`` after its terminal
        # state-transition dual-write fired, so anything left in
        # the list is by definition unprocessed.
        if used_pipeline_reader and unprocessed_pipeline_claims:
            try:
                released = _release_cloud_pipeline_claims(
                    list(unprocessed_pipeline_claims),
                    last_error=(
                        'PR-F3: drain ended before upload '
                        f'(reason={_sync_status.get("progress") or "unknown"})'
                    ),
                )
                logger.info(
                    "PR-F3 cloud reader: released %d/%d "
                    "unprocessed pipeline claims at drain end",
                    released, len(unprocessed_pipeline_claims),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "PR-F3 cloud reader: straggler-release at "
                    "drain end raised: %s — claims will rely on "
                    "stale-claim recovery", e,
                )

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

            # Wave 4 PR-F4 (issue #184): the LES yield path here has
            # been removed. Live events are now first-class
            # ``pipeline_queue`` rows enqueued at
            # ``PRIORITY_LIVE_EVENT = 0`` by the file_watcher
            # event.json hook (see :func:`enqueue_live_event_from_event_json`).
            # The reader's natural priority order means the worker
            # always claims a live-event row before any bulk
            # ``PRIORITY_CLOUD_BULK`` row — no separate worker, no
            # separate queue, no yield-and-wait dance needed.

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


# ---------------------------------------------------------------------------
# Dashboard counter "Reset Stats" — baseline-timestamp model
# ---------------------------------------------------------------------------

def get_stats_baseline(db_path: str) -> Optional[str]:
    """Return the ISO-8601 UTC timestamp of the last counter reset, or ``None``.

    Stored in the ``cloud_archive_meta`` table under the
    ``stats_baseline_at`` key. ``get_sync_stats`` filters the cumulative
    ``total_synced`` count and ``total_bytes`` sum by ``synced_at >
    baseline``, so the UI can show a fresh starting point WITHOUT
    deleting the dedup-critical ``cloud_synced_files`` rows that prevent
    already-synced clips from being re-uploaded on the next sync pass.

    Returns ``None`` when no reset has been performed (counters then
    show full lifetime totals).
    """
    conn = _init_cloud_tables(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM cloud_archive_meta WHERE key = ?",
            (_CLOUD_STATS_BASELINE_KEY,),
        ).fetchone()
        if not row or row["value"] is None:
            return None
        value = str(row["value"]).strip()
        return value or None
    finally:
        conn.close()


def reset_stats_baseline(db_path: str) -> Tuple[bool, str]:
    """Record "now" as the dashboard counter baseline.

    The reset is non-destructive: ``cloud_synced_files`` rows are
    untouched, so the next sync cycle's dedup check still sees every
    file that was previously uploaded and skips them. The UI's
    ``total_synced`` and ``total_bytes`` counters will start counting
    from zero again, but the cloud archive itself is unchanged.

    Returns ``(True, message)`` with the persisted baseline timestamp on
    success, or ``(False, error_message)`` on any failure.
    """
    baseline_at = datetime.now(timezone.utc).isoformat()
    conn = _init_cloud_tables(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO cloud_archive_meta (key, value) "
            "VALUES (?, ?)",
            (_CLOUD_STATS_BASELINE_KEY, baseline_at),
        )
        conn.commit()
        logger.info(
            "Cloud sync stats baseline reset to %s (cloud_synced_files "
            "rows preserved for dedup)",
            baseline_at,
        )
        return True, baseline_at
    except sqlite3.Error as exc:  # noqa: BLE001
        logger.exception("Failed to reset cloud sync stats baseline")
        return False, str(exc)
    finally:
        conn.close()


def get_sync_stats(db_path: str) -> dict:
    """Return aggregate sync statistics for the UI dashboard.

    Keys: total_synced, total_pending, total_failed, total_dead_letter,
    total_bytes, stats_baseline_at.

    ``total_failed`` is the SUM of ``failed`` and ``dead_letter`` rows
    so the dashboard counter does NOT silently DECREASE when a row hits
    the Phase 2.6 retry cap and is promoted from ``failed`` →
    ``dead_letter``. Without this, a permanently broken clip that
    promotes after retry 5 would make problems look like they
    self-resolved on the dashboard.

    ``total_dead_letter`` is also exposed as a subset so a future
    Failed Jobs page (Phase 4) can break the count down by terminal
    state without changing this aggregate.

    ``total_synced`` and ``total_bytes`` are filtered by
    ``stats_baseline_at`` when set: only rows whose ``synced_at`` is
    strictly greater than the baseline are counted. ``total_pending``
    and ``total_failed`` are NOT filtered because they reflect current
    work / current failures, not cumulative history — resetting them
    would lie about the current state of the queue.
    """
    conn = _init_cloud_tables(db_path)
    try:
        # Read baseline once per call so the COUNT and SUM stay
        # consistent against the same cutoff.
        baseline_row = conn.execute(
            "SELECT value FROM cloud_archive_meta WHERE key = ?",
            (_CLOUD_STATS_BASELINE_KEY,),
        ).fetchone()
        baseline = baseline_row["value"] if baseline_row else None
        baseline = (baseline or "").strip() or None

        counts = {}
        for status in ("pending", "failed", "uploading", "dead_letter"):
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM cloud_synced_files WHERE status = ?",
                (status,),
            ).fetchone()
            counts[status] = row["cnt"] if row else 0

        # Synced count + bytes honor the baseline. ``synced_at`` is
        # written by ``_mark_upload_success`` as ISO-8601 UTC, so
        # lexicographic comparison is correct. Rows with NULL
        # ``synced_at`` (legacy / pre-fix data) are conservatively
        # included so the post-reset counter never under-counts work
        # the user actually saw complete.
        if baseline:
            synced_row = conn.execute(
                "SELECT COUNT(*) AS cnt, COALESCE(SUM(file_size), 0) AS total "
                "FROM cloud_synced_files "
                "WHERE status = 'synced' "
                "  AND (synced_at IS NULL OR synced_at > ?)",
                (baseline,),
            ).fetchone()
        else:
            synced_row = conn.execute(
                "SELECT COUNT(*) AS cnt, COALESCE(SUM(file_size), 0) AS total "
                "FROM cloud_synced_files WHERE status = 'synced'"
            ).fetchone()
        counts["synced"] = synced_row["cnt"] if synced_row else 0
        total_bytes = synced_row["total"] if synced_row else 0

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
            "stats_baseline_at": baseline,
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
    affected_paths: List[str] = []
    try:
        # Wave 4 PR-B: capture paths BEFORE the UPDATE so the
        # corresponding pipeline_queue rows can be reset to 'pending'
        # too. ``BEGIN IMMEDIATE`` acquires the reserved write lock
        # before the SELECT so no concurrent writer can flip a row
        # into ``'uploading'`` between the SELECT and the UPDATE
        # (which would otherwise leave the new row's pipeline_queue
        # mirror stale).
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT file_path FROM cloud_synced_files "
            "WHERE status = 'uploading'"
        ).fetchall()
        cur = conn.execute(
            "UPDATE cloud_synced_files SET status = 'pending', "
            "retry_count = retry_count WHERE status = 'uploading'"
        )
        affected = cur.rowcount
        conn.commit()
        if affected:
            logger.info("Recovered %d interrupted cloud uploads", affected)
            affected_paths.extend(r["file_path"] for r in rows)
        return affected
    finally:
        conn.close()
        for fp in affected_paths:
            _dual_write_pipeline_cloud_synced_state(
                fp, status='pending',
            )


# ---------------------------------------------------------------------------
# Sync Status & Queue Management
# ---------------------------------------------------------------------------


def get_sync_status_for_events(event_names: list) -> dict:
    """Check sync status for a list of event names.

    Returns dict mapping event_name -> status ('synced', 'queued',
    'uploading', None).

    Phase 5.5 — single-query batch lookup. The legacy implementation
    issued one ``SELECT ... LIKE '%name%' ORDER BY synced_at DESC LIMIT 1``
    per event, which is N round-trips for an N-event status request
    (typical UI request: 30 events → 30 queries → ~3-9 ms baseline +
    SQLite overhead × 30). Now we issue ONE query that returns every
    matching row and bucket-match in Python.

    The match semantics (case-sensitive substring of ``file_path``) are
    preserved: an event_name like ``2026-05-12_10-00-00`` still matches
    rows whose ``file_path`` contains that string. The "most recent
    match wins" semantics are preserved by ordering ``synced_at DESC``
    server-side and taking the FIRST hit per event_name in Python.
    """
    if not event_names:
        return {}
    # Defensive cap. ``event_names`` is unbounded at the caller (the
    # blueprint accepts whatever the client posts). One OR-clause is a
    # few bytes of SQL text plus one row per match in fetchall(), so
    # 500 names is the practical safety ceiling on a Pi Zero 2 W's
    # 512 MB RAM. Names beyond the cap simply land as None — the UI
    # already treats unknown statuses as "not synced".
    _MAX_BATCH = 500
    if len(event_names) > _MAX_BATCH:
        logger.warning(
            "get_sync_status_for_events: %d names requested, capping at %d",
            len(event_names),
            _MAX_BATCH,
        )
        # Pre-build the FULL skeleton so capped-out names still appear.
        statuses: dict = {name: None for name in event_names}
        event_names = event_names[:_MAX_BATCH]
    else:
        statuses = {name: None for name in event_names}

    conn = _init_cloud_tables(CLOUD_ARCHIVE_DB_PATH)
    try:
        # ONE query: union the per-name LIKE patterns. SQLite does not
        # support a parameterized "any of these patterns" operator, so we
        # OR N parameterized LIKE clauses — the SQL text length grows
        # linearly with N but the round-trip count stays at 1.
        like_clauses = " OR ".join(["file_path LIKE ?"] * len(event_names))
        params = ["%" + name + "%" for name in event_names]
        sql = (
            "SELECT file_path, status FROM cloud_synced_files "
            f"WHERE {like_clauses} "
            "ORDER BY synced_at DESC"
        )
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            # Best-effort: a malformed query (shouldn't happen) or DB
            # error returns the empty-skeleton dict so the UI degrades
            # gracefully instead of 500ing.
            return statuses

        # Bucket-match in Python. ``rows`` is ordered most-recent-first
        # so the FIRST match per event_name is the row we want — same
        # semantics as the legacy ``LIMIT 1`` per name. Stop scanning
        # once every name has been resolved.
        unresolved = set(event_names)
        for row in rows:
            if not unresolved:
                break
            file_path = row["file_path"]
            # A row may match more than one event_name (e.g. two events
            # with overlapping timestamps); each name takes its own most-
            # recent match independently.
            matched = [n for n in unresolved if n in file_path]
            for name in matched:
                statuses[name] = row["status"]
                unresolved.discard(name)
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
        # Defer dual-writes until AFTER ``conn.commit()`` so a crash
        # mid-loop can't leave orphan ``pipeline_queue`` rows pointing
        # at legacy IDs that were never persisted.
        pending_pipeline: List[Tuple[str, Optional[str], str]] = []
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
                pending_pipeline.append((canonical, None, 'queued'))

        conn.commit()
        if pending_pipeline:
            _dual_write_pipeline_cloud_synced_batch(pending_pipeline)
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
            "SELECT id, file_path, file_size, retry_count, last_error, "
            "previous_last_error "
            "FROM cloud_synced_files "
            "WHERE status = 'dead_letter' "
            "ORDER BY id ASC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_dead_letters() -> int:
    """Return the count of ``dead_letter`` rows in ``cloud_synced_files``.

    Cheap (single ``SELECT COUNT(*)`` over the ``idx_cloud_synced_status``
    index defined on ``cloud_synced_files(status)``). Used by
    ``/api/jobs/counts`` so the unified page doesn't fetch every row
    just to compute ``len()``. Returns ``0`` on any DB error so a
    failed count never breaks the aggregate page.
    """
    conn = _init_cloud_tables(CLOUD_ARCHIVE_DB_PATH)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM cloud_synced_files "
            "WHERE status = 'dead_letter'"
        ).fetchone()
        return int(row[0]) if row else 0
    except Exception as e:  # noqa: BLE001
        logger.warning("count_dead_letters failed: %s", e)
        return 0
    finally:
        conn.close()


def retry_dead_letter(file_path: Optional[str] = None) -> int:
    """Reset ``dead_letter`` rows back to ``pending`` for re-pickup.

    When ``file_path`` is given, only that one row is reset (looked up
    via :func:`canonical_cloud_path` so legacy absolute forms still
    match the stored canonical form). When ``None``, every dead-letter
    row in the table is reset — useful for "Retry all" after fixing a
    cloud auth or quota outage that affected the whole batch.

    Resets ``retry_count`` to zero so the cap-promotion logic in
    :func:`_mark_upload_failure` starts the next attempt fresh.
    **Does NOT clear** ``last_error`` — the previous failure reason
    is the most useful triage context the operator has, and the next
    failure overwrites it via ``_mark_upload_failure`` anyway (a
    successful retry leaves the row out of the dead-letter view).
    Wakes the cloud worker so the row gets picked up immediately if
    WiFi + cloud are healthy. Returns the number of rows actually
    reset (``0`` if nothing matched).
    """
    if file_path is not None:
        try:
            target = canonical_cloud_path(file_path)
        except ValueError:
            return 0
    else:
        target = None
    conn = _init_cloud_tables(CLOUD_ARCHIVE_DB_PATH)
    affected_paths: List[str] = []
    try:
        # Wave 4 PR-B: ``BEGIN IMMEDIATE`` acquires the reserved write
        # lock before the SELECT so no concurrent writer can flip a
        # row into ``'dead_letter'`` between the SELECT and UPDATE
        # (which would otherwise leave the new dead-letter row's
        # pipeline_queue mirror stale).
        conn.execute("BEGIN IMMEDIATE")
        if target is None:
            # Capture matching paths BEFORE the UPDATE so we can mirror
            # the reset into pipeline_queue afterwards.
            rows = conn.execute(
                "SELECT file_path FROM cloud_synced_files "
                "WHERE status = 'dead_letter'"
            ).fetchall()
            cur = conn.execute(
                "UPDATE cloud_synced_files "
                "SET status = 'pending', retry_count = 0 "
                "WHERE status = 'dead_letter'"
            )
            if cur.rowcount:
                affected_paths.extend(r["file_path"] for r in rows)
        else:
            row = conn.execute(
                "SELECT file_path FROM cloud_synced_files "
                "WHERE status = 'dead_letter' AND file_path = ?",
                (target,),
            ).fetchone()
            cur = conn.execute(
                "UPDATE cloud_synced_files "
                "SET status = 'pending', retry_count = 0 "
                "WHERE status = 'dead_letter' AND file_path = ?",
                (target,),
            )
            if row and cur.rowcount:
                affected_paths.append(row["file_path"])
        conn.commit()
        n = cur.rowcount or 0
    finally:
        conn.close()
    # Wave 4 PR-B: mirror the reset-to-pending into pipeline_queue.
    # Done outside the legacy connection lock so a pipeline lock
    # cannot delay the legacy unlock.
    for fp in affected_paths:
        _dual_write_pipeline_cloud_synced_state(
            fp,
            status='pending',
            attempts=0,
            next_retry_at=0.0,
        )
    if n > 0:
        try:
            wake()
        except Exception:  # noqa: BLE001
            logger.debug("retry_dead_letter: wake() raised; ignoring",
                         exc_info=True)
    return n


def delete_dead_letter(file_path: Optional[str] = None) -> int:
    """Permanently delete ``dead_letter`` rows from ``cloud_synced_files``.

    When ``file_path`` is given, only that one row is removed (looked
    up via :func:`canonical_cloud_path` so legacy absolute forms still
    match the stored canonical form). When ``None``, every dead-letter
    row in the table is removed — the "Delete all" path on the Failed
    Jobs page (#161).

    The companion to :func:`retry_dead_letter`: same WHERE filter
    (``status = 'dead_letter'``), but ``DELETE`` instead of ``UPDATE``.
    Use when the source video has been deleted from disk (the most
    common case — Tesla rotated it out, the user emptied an event
    folder, etc.) or when retrying just keeps failing the same way.

    The inotify file watcher / archive worker may legitimately
    re-enqueue the same file_path later if it reappears on disk; that's
    the producer doing its job and the new row starts fresh with
    ``retry_count=0``. Returns the number of rows actually deleted
    (``0`` if nothing matched). Returns ``0`` on any DB error so a UI
    delete-all click never blows up the request handler.
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
                "DELETE FROM cloud_synced_files "
                "WHERE status = 'dead_letter'"
            )
        else:
            cur = conn.execute(
                "DELETE FROM cloud_synced_files "
                "WHERE status = 'dead_letter' AND file_path = ?",
                (target,),
            )
        conn.commit()
        return cur.rowcount or 0
    except sqlite3.Error as e:
        logger.warning("delete_dead_letter failed: %s", e)
        return 0
    finally:
        conn.close()
