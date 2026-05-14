"""
TeslaUSB Live Event Sync Service (LES).

Real-time uploader for Sentry/Saved events. When Tesla writes a new
``event.json``, this service enqueues the surrounding camera files and
uploads them to the configured cloud provider as soon as WiFi is
available — without waiting for the bulk ``cloud_archive_service``
sync to run.

Design constraints (Pi Zero 2 W, 512 MB RAM):

* **Zero impact on the USB gadget endpoint.** Never touches mounts,
  loop devices, the gadget config, or the quick_edit lock.
* **One additional thread**, blocked on ``threading.Event.wait()`` when
  idle (no polling). Idle CPU < 0.1%.
* **No heavy imports** — only ``sqlite3``, ``os``, ``subprocess``,
  ``threading``, ``json``, ``re``, ``time``, ``urllib``. No
  ``cv2``/``av``/``PIL``/``numpy``/``requests`` anywhere in the LES
  code path.
* **One rclone subprocess at a time, ever.** Coordinates with the
  bulk cloud sync via the global ``task_coordinator``; LES gets
  priority when both want WiFi.
* **Persistent across WiFi outages.** Queue rows survive reboots.
  ``startup_recovery`` resets stale ``uploading`` rows to ``pending``.
* **Failure containment.** Exceptions are caught at the worker loop
  boundary and logged; LES failure must never take down
  ``gadget_web.service``.

Public API:

* :func:`start` / :func:`stop`            — lifecycle control
* :func:`enqueue_event_json` / :func:`enqueue_event_dir` — producers
* :func:`wake`                            — used by the WiFi-connect dispatcher
* :func:`get_status`                      — for the status API blueprint
* :func:`retry_failed`                    — for the manual retry endpoint
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration imports
# ---------------------------------------------------------------------------

from config import (
    CLOUD_ARCHIVE_DB_PATH,
    CLOUD_ARCHIVE_PROVIDER,
    CLOUD_ARCHIVE_REMOTE_PATH,
    CLOUD_ARCHIVE_MAX_UPLOAD_MBPS,
    CLOUD_PROVIDER_CREDS_PATH,
    LIVE_EVENT_SYNC_ENABLED,
    LIVE_EVENT_WATCH_FOLDERS,
    LIVE_EVENT_UPLOAD_SCOPE,
    LIVE_EVENT_RETRY_MAX_ATTEMPTS,
    LIVE_EVENT_RETRY_BACKOFF_SECONDS,
    LIVE_EVENT_DAILY_DATA_CAP_MB,
    LIVE_EVENT_NOTIFY_WEBHOOK_URL,
)

# ---------------------------------------------------------------------------
# Module state — kept tiny so the worker thread RSS stays under 2 MB.
# ---------------------------------------------------------------------------

_worker_thread: Optional[threading.Thread] = None
_worker_lock = threading.Lock()
_worker_stop = threading.Event()

# Wake the worker on enqueue, WiFi-connect, or shutdown. ``Event.wait()``
# blocks the worker between events; setting this triggers a single
# drain pass.
_wake = threading.Event()

# Tracks the currently-active rclone subprocess so :func:`stop` can
# terminate it on shutdown.
_active_proc_lock = threading.Lock()
_active_proc: Optional[subprocess.Popen] = None

# Last-known state, exported via :func:`get_status`. All reads/writes go
# through ``_status_lock`` so the API thread sees a consistent snapshot.
_status_lock = threading.Lock()
_status: Dict = {
    "enabled": LIVE_EVENT_SYNC_ENABLED,
    "running": False,
    "active_file": None,         # Event dir currently being uploaded — name matches archive/indexer/cloud subsystems
    "last_uploaded_at": None,
    "last_uploaded_event": None,
    "last_error": None,
    "last_error_at": None,
    "data_uploaded_today_bytes": 0,
    "data_cap_reached": False,
}

# Tesla camera angle suffix → match files by filename pattern. Using
# CAMERA_ANGLES from config keeps this in sync with the rest of the app.
from config import CAMERA_ANGLES

# Filename minute prefix: "YYYY-MM-DD_HH-MM"
_MINUTE_PREFIX_RE = re.compile(r'^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2})')

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

# Live Event Sync uses its own table inside the existing cloud_sync.db
# (NOT a separate database) so backups, integrity checks, and
# corruption recovery already cover it.
_LES_TABLES_SQL = """\
CREATE TABLE IF NOT EXISTS live_event_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_dir       TEXT    NOT NULL,
    event_json_path TEXT    NOT NULL,
    event_timestamp TEXT,
    event_reason    TEXT,
    upload_scope    TEXT    DEFAULT 'event_minute',
    status          TEXT    DEFAULT 'pending',
    enqueued_at     TEXT    NOT NULL,
    uploaded_at     TEXT,
    next_retry_at   REAL,
    attempts        INTEGER DEFAULT 0,
    last_error      TEXT,
    previous_last_error TEXT,
    bytes_uploaded  INTEGER DEFAULT 0,
    UNIQUE(event_dir)
);

CREATE INDEX IF NOT EXISTS idx_les_status ON live_event_queue(status);
CREATE INDEX IF NOT EXISTS idx_les_next_retry ON live_event_queue(next_retry_at);
"""


def _open_db() -> sqlite3.Connection:
    """Open the cloud_sync.db with WAL + a short busy-timeout."""
    conn = sqlite3.connect(CLOUD_ARCHIVE_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


_schema_alter_done = False


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Initialize LES tables (idempotent) and apply pending ALTERs.

    LES doesn't run a versioned migration table — the schema is small
    and ALTERs are run inline. To avoid the cost (and per-call
    journal noise) of the ALTER on every connection open, we use a
    process-level flag plus a one-time ``PRAGMA table_info`` check:
    once we've confirmed ``previous_last_error`` exists, every
    subsequent caller skips the ALTER attempt entirely. The flag
    resets per-process, so a fresh service start re-checks once.
    """
    global _schema_alter_done
    conn.executescript(_LES_TABLES_SQL)
    if _schema_alter_done:
        conn.commit()
        return
    # Issue #132: ``previous_last_error`` column. PRAGMA-check first
    # to avoid blind ALTER attempts on every connection. ALTER stays
    # wrapped in try/except as defense-in-depth (a concurrent
    # process could ALTER between our check and execute).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(live_event_queue)")}
    if 'previous_last_error' not in cols:
        try:
            conn.execute(
                "ALTER TABLE live_event_queue "
                "ADD COLUMN previous_last_error TEXT"
            )
        except sqlite3.OperationalError:
            pass
    _schema_alter_done = True
    conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _startup_recovery(conn: sqlite3.Connection) -> int:
    """Reset stale ``uploading`` rows AND repair retry-exhausted ``pending`` rows.

    * ``uploading`` rows from a crashed/restarted process → ``pending``
      (with their attempt count preserved so we don't double-count).
    * ``pending`` rows whose ``attempts >= retry_max_attempts`` (e.g.,
      because a config change lowered the cap, or an older code path
      bumped attempts speculatively) are moved to ``failed`` so they
      stop blocking ``has_ready_live_event_work()`` forever.
    """
    cur = conn.execute(
        "UPDATE live_event_queue SET status = 'pending', "
        "last_error = COALESCE(last_error, 'Process restarted') "
        "WHERE status = 'uploading'"
    )
    n_uploading = cur.rowcount

    cur2 = conn.execute(
        "UPDATE live_event_queue SET status = 'failed', "
        "last_error = COALESCE(last_error, 'Retries exhausted at startup') "
        "WHERE status = 'pending' AND attempts >= ?",
        (LIVE_EVENT_RETRY_MAX_ATTEMPTS,),
    )
    n_exhausted = cur2.rowcount

    if n_uploading or n_exhausted:
        conn.commit()
        if n_uploading:
            logger.info(
                "LES startup recovery: %d uploading rows reset to pending",
                n_uploading,
            )
        if n_exhausted:
            logger.warning(
                "LES startup recovery: %d pending rows over retry cap moved "
                "to failed", n_exhausted,
            )
    return n_uploading + n_exhausted


def _sweep_orphaned_rclone_confs() -> int:
    """Remove orphaned ``rclone-les-*.conf`` files left in the tmpfs dir.

    The unique-per-attempt rclone config is normally cleaned up in the
    ``finally`` block of :func:`_process_one`. If a previous worker
    process was killed via SIGKILL (OOM, power loss before sync, etc.)
    those finally blocks didn't run and the config file leaks under
    ``/run/teslausb/``. The tmpfs is wiped on reboot so the leak is
    bounded, but cleaning up at startup keeps the directory tidy and
    matches the spirit of :func:`_startup_recovery`.

    Files belonging to the current pid are left alone — they may still
    be in use by a concurrently-running worker thread.

    Returns the number of files removed. Never raises.
    """
    try:
        from services.cloud_archive_service import _RCLONE_TMPFS_DIR
    except Exception:
        # cloud_archive_service unavailable or doesn't expose the
        # constant — nothing to sweep.
        return 0

    try:
        names = os.listdir(_RCLONE_TMPFS_DIR)
    except OSError:
        return 0

    own_pid = os.getpid()
    removed = 0
    for name in names:
        if not fnmatch.fnmatch(name, 'rclone-les-*.conf'):
            continue
        # Filename pattern: rclone-les-<pid>-<tid>.conf
        try:
            mid = name[len('rclone-les-'):-len('.conf')]
            pid_str = mid.split('-', 1)[0]
            file_pid = int(pid_str)
        except (ValueError, IndexError):
            # Doesn't match the expected pattern — leave it alone.
            continue
        if file_pid == own_pid:
            continue
        try:
            os.remove(os.path.join(_RCLONE_TMPFS_DIR, name))
            removed += 1
        except OSError:
            pass

    if removed:
        logger.info(
            "LES startup sweep: removed %d orphaned rclone-les-*.conf file(s)",
            removed,
        )
    return removed


def _prune_old_uploaded(conn: sqlite3.Connection,
                        max_age_days: int = 7) -> int:
    """Delete ``uploaded`` rows older than ``max_age_days``.

    Keeps the queue table small. Cheap one-shot at startup.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    cur = conn.execute(
        "DELETE FROM live_event_queue "
        "WHERE status = 'uploaded' AND uploaded_at < ?",
        (cutoff_iso,),
    )
    n = cur.rowcount
    if n:
        conn.commit()
        logger.info("LES prune: removed %d old uploaded rows", n)
    return n


# ---------------------------------------------------------------------------
# Queue ops
# ---------------------------------------------------------------------------


def _teslacam_roots() -> List[str]:
    """Return all known TeslaCam mount roots (RO and RW), realpathed.

    Used to verify ``event_dir`` paths fall under an actual Tesla camera
    mount and not somewhere else on the filesystem. Cached briefly via
    a module-level helper would be premature — these are dirt cheap
    realpath calls and the function is only called from enqueue.
    """
    roots: List[str] = []
    try:
        from services.video_service import get_teslacam_path
        try:
            p = get_teslacam_path()
            if p and os.path.isdir(p):
                roots.append(os.path.realpath(p))
        except Exception:
            pass
    except ImportError:
        pass
    # Fallback / additional locations: RO mount and ArchivedClips. We
    # add all realpath-resolved candidates so symlink games can't trick
    # the check.
    try:
        from config import RO_MNT_DIR
        ro_tc = os.path.realpath(
            os.path.join(RO_MNT_DIR, 'part1-ro', 'TeslaCam'),
        )
        if os.path.isdir(ro_tc) and ro_tc not in roots:
            roots.append(ro_tc)
    except Exception:
        pass
    try:
        from config import ARCHIVE_DIR
        ad = os.path.realpath(ARCHIVE_DIR)
        if os.path.isdir(ad) and ad not in roots:
            roots.append(ad)
    except Exception:
        pass
    return roots


def _is_event_in_watched_folder(event_dir: str) -> bool:
    """Return True iff ``event_dir`` is a real Tesla event directory.

    Strict containment: ``event_dir`` must resolve (via ``realpath``)
    to a path of the form ``<teslacam_root>/<watch_folder>/<event_name>``
    where ``watch_folder`` is one of ``LIVE_EVENT_WATCH_FOLDERS`` and
    ``event_name`` is exactly one path component. This prevents symlink
    escapes and rejects malformed inputs like
    ``/tmp/SentryClips/event.json`` that contain the folder name but
    aren't under TeslaCam.
    """
    if not event_dir:
        return False
    try:
        real = os.path.realpath(event_dir)
    except (OSError, ValueError):
        return False
    roots = _teslacam_roots()
    if not roots:
        # We have no anchor to validate against. Refuse rather than
        # accept arbitrary paths — better to drop a callback than to
        # upload from somewhere we shouldn't.
        return False
    for root in roots:
        try:
            common = os.path.commonpath([root, real])
        except ValueError:
            # Different drives on Windows etc. — definitely not a match.
            continue
        if common != root:
            continue
        # rel must be exactly "<watch_folder>/<event_name>" (2 components).
        rel = os.path.relpath(real, root).replace("\\", "/")
        parts = [p for p in rel.split("/") if p and p != '.']
        if len(parts) != 2:
            continue
        if parts[0] in LIVE_EVENT_WATCH_FOLDERS:
            return True
    return False


def _read_event_json(path: str) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(timestamp, reason)`` from ``event.json``, or ``(None, None)``.

    Bounded read (≤ 8 KB — Tesla's event.json is ~hundreds of bytes).
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            blob = f.read(8192)
        data = json.loads(blob)
        return (data.get('timestamp'), data.get('reason'))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.debug("Could not parse %s: %s", path, e)
        return (None, None)


def _dual_write_pipeline_live_event(event_json_path: str,
                                    event_dir: str,
                                    event_timestamp: Optional[str],
                                    event_reason: Optional[str],
                                    upload_scope: str,
                                    legacy_id: Optional[int]) -> None:
    """Best-effort dual-write to ``pipeline_queue`` (geodata.db).

    Cross-DB write — opens a fresh geodata.db connection, writes one
    row, closes. Failure here is logged at WARNING and swallowed —
    the legacy ``live_event_queue`` row is the source of truth in
    Phase I.1, so a missing pipeline_queue row only means the
    Phase I.2 unified worker doesn't see it (legacy worker still
    handles it).
    """
    try:
        from services import pipeline_queue_service as pqs
        pqs.dual_write_enqueue(
            source_path=event_json_path,
            stage=pqs.STAGE_LIVE_EVENT_PENDING,
            legacy_table=pqs.LEGACY_TABLE_LIVE_EVENT,
            legacy_id=legacy_id,
            priority=pqs.PRIORITY_LIVE_EVENT,
            payload={
                'event_dir': event_dir,
                'event_timestamp': event_timestamp,
                'event_reason': event_reason,
                'upload_scope': upload_scope,
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "pipeline_queue LES dual-write skipped for %s: %s",
            event_dir, e,
        )


def _dual_write_pipeline_live_event_state(
    legacy_id: int,
    *,
    new_stage: Optional[str] = None,
    status: Optional[str] = None,
    attempts: Optional[int] = None,
    last_error: Optional[str] = None,
    completed_at: Optional[float] = None,
    next_retry_at: Optional[float] = None,
) -> None:
    """State-transition dual-write for live_event_queue (Wave 4 PR-B).

    Mirrors a legacy ``live_event_queue`` row's state transition into
    ``pipeline_queue``. Looked up by ``legacy_table='live_event_queue'``
    + ``legacy_id`` (the integer row id from live_event_queue).
    Failures are swallowed at DEBUG.
    """
    if not legacy_id:
        return
    try:
        from services import pipeline_queue_service as pqs
        pqs.update_pipeline_row_by_legacy_id(
            legacy_table=pqs.LEGACY_TABLE_LIVE_EVENT,
            legacy_id=int(legacy_id),
            new_stage=new_stage,
            status=status,
            attempts=attempts,
            last_error=last_error,
            completed_at=completed_at,
            next_retry_at=next_retry_at,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug(
            "pipeline_queue LES state dual-write skipped for id=%s: %s",
            legacy_id, e,
        )


def enqueue_event_json(event_json_paths: List[str]) -> int:
    """Producer: enqueue events from a list of ``event.json`` paths.

    Used as the ``register_event_json_callback`` on the file watcher.
    De-duplication is the queue's job: ``UNIQUE(event_dir)`` plus
    ``INSERT OR IGNORE`` makes repeated enqueues no-ops. The caller
    does not need to filter.

    Returns the number of newly-enqueued rows.
    """
    if not LIVE_EVENT_SYNC_ENABLED:
        return 0
    if not event_json_paths:
        return 0

    inserted = 0
    pending_dual_writes = []  # (path, dir, ts, reason, scope, id)
    try:
        conn = _open_db()
        try:
            _ensure_schema(conn)
            now_iso = _now_iso()
            for ej_path in event_json_paths:
                event_dir = os.path.dirname(ej_path)
                if not _is_event_in_watched_folder(event_dir):
                    continue
                if not os.path.isfile(ej_path):
                    continue
                ts, reason = _read_event_json(ej_path)
                cur = conn.execute(
                    """INSERT OR IGNORE INTO live_event_queue
                       (event_dir, event_json_path, event_timestamp,
                        event_reason, upload_scope, status, enqueued_at)
                       VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                    (event_dir, ej_path, ts, reason,
                     LIVE_EVENT_UPLOAD_SCOPE, now_iso),
                )
                if cur.rowcount:
                    inserted += 1
                    new_id = cur.lastrowid
                    logger.info(
                        "LES enqueued: %s reason=%s", event_dir, reason,
                    )
                    # Defer the dual-write: if we crash between this
                    # point and ``conn.commit()`` below, the legacy
                    # row vanishes (it was never committed) and we
                    # MUST NOT leave an orphan pipeline_queue row
                    # with a now-stale ``legacy_id``. Phase I.2's
                    # worker uses ``legacy_id`` to cross-reference;
                    # an orphan would point at a non-existent row.
                    pending_dual_writes.append((
                        ej_path, event_dir, ts, reason,
                        LIVE_EVENT_UPLOAD_SCOPE, new_id,
                    ))
            if inserted:
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        # Never propagate errors back into the file watcher — that
        # would silence MP4 callbacks for the indexer.
        logger.error("LES enqueue_event_json failed: %s", e)
        return 0

    # Dual-write to pipeline_queue ONLY after the legacy commit
    # succeeded — guarantees no orphans on crash mid-loop.
    for ej_path, event_dir, ts, reason, scope, new_id in pending_dual_writes:
        _dual_write_pipeline_live_event(
            ej_path, event_dir, ts, reason, scope, new_id,
        )

    if inserted:
        _wake.set()
    return inserted


def enqueue_event_dir(event_dir: str) -> bool:
    """Producer: enqueue from a known event directory (manual API path)."""
    ej = os.path.join(event_dir, 'event.json')
    if not os.path.isfile(ej):
        return False
    return enqueue_event_json([ej]) > 0


def _claim_next_pending(conn: sqlite3.Connection) -> Optional[Dict]:
    """Return the next pending row eligible to upload, or ``None``.

    Marks the row ``uploading`` so concurrent workers can't double-claim
    it, but does NOT increment ``attempts``. The attempt counter is only
    bumped once we hold the global ``task_coordinator`` lock AND are
    about to invoke rclone (see :func:`_record_attempt_start`). This
    prevents a busy task_coordinator from silently consuming retries.
    """
    now_ts = time.time()
    row = conn.execute(
        """SELECT * FROM live_event_queue
           WHERE status = 'pending'
             AND (next_retry_at IS NULL OR next_retry_at <= ?)
             AND attempts < ?
           ORDER BY enqueued_at ASC
           LIMIT 1""",
        (now_ts, LIVE_EVENT_RETRY_MAX_ATTEMPTS),
    ).fetchone()
    if row is None:
        return None
    cur = conn.execute(
        "UPDATE live_event_queue SET status = 'uploading' "
        "WHERE id = ? AND status = 'pending'",
        (row['id'],),
    )
    if cur.rowcount == 0:
        return None  # Someone else grabbed it
    conn.commit()
    # Wave 4 PR-B: mirror the claim transition into pipeline_queue
    # AFTER the legacy commit so a pipeline lock can't delay the
    # legacy unlock.
    _dual_write_pipeline_live_event_state(
        int(row['id']),
        status='in_progress',
    )
    return dict(row)


def _release_claim_to_pending(conn: sqlite3.Connection, row_id: int) -> None:
    """Return a claimed row to ``pending`` without consuming a retry attempt.

    Used when the worker can't proceed (task_coordinator busy, WiFi
    dropped between claim and upload, etc.). Idempotent.
    """
    cur = conn.execute(
        "UPDATE live_event_queue SET status = 'pending' "
        "WHERE id = ? AND status = 'uploading'",
        (row_id,),
    )
    conn.commit()
    if cur.rowcount:
        _dual_write_pipeline_live_event_state(
            int(row_id),
            status='pending',
        )


def _record_attempt_start(conn: sqlite3.Connection, row_id: int) -> int:
    """Increment ``attempts`` and return the new (post-increment) value.

    Called only after we have the task_coordinator lock and just before
    we actually invoke rclone. The returned value is the authoritative
    attempt number to use for retry-exhaustion decisions in
    :func:`_mark_failed`.

    Returns 0 if the row no longer exists (caller must treat as
    already-deleted and skip). This can happen if an operator runs
    ``DELETE FROM live_event_queue`` while a worker is mid-claim;
    returning 0 prevents the caller from recording a phantom failure
    against a non-existent row.
    """
    conn.execute(
        "UPDATE live_event_queue SET attempts = attempts + 1 WHERE id = ?",
        (row_id,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT attempts FROM live_event_queue WHERE id = ?",
        (row_id,),
    ).fetchone()
    new_attempts = int(row['attempts']) if row else 0
    if new_attempts:
        _dual_write_pipeline_live_event_state(
            int(row_id),
            attempts=new_attempts,
        )
    return new_attempts


def _mark_uploaded(conn: sqlite3.Connection, row_id: int,
                   bytes_uploaded: int) -> None:
    conn.execute(
        "UPDATE live_event_queue SET status = 'uploaded', "
        "uploaded_at = ?, last_error = NULL, "
        "bytes_uploaded = ? WHERE id = ?",
        (_now_iso(), bytes_uploaded, row_id),
    )
    conn.commit()
    # Wave 4 PR-B: terminal — promote pipeline_queue row to
    # ``live_event_done`` / ``done``.
    _dual_write_pipeline_live_event_state(
        int(row_id),
        new_stage='live_event_done',
        status='done',
        completed_at=time.time(),
        last_error='',
    )


def _mark_failed(conn: sqlite3.Connection, row_id: int, attempts: int,
                 err: str, transient: bool = False,
                 retry_in_seconds: Optional[int] = None) -> None:
    """Mark a row failed; schedule retry or move to 'failed' if exhausted.

    ``attempts`` is the post-increment value (i.e., the attempt that
    just failed). When ``transient`` is True the row is scheduled for a
    short retry without counting against ``LIVE_EVENT_RETRY_MAX_ATTEMPTS``
    and without changing the attempt count — used for "MP4s still being
    written" and other recoverable wait states.

    ``retry_in_seconds`` overrides the backoff schedule (used by
    transient deferrals). When omitted the standard backoff for this
    attempt index is used.
    """
    if transient:
        backoff = retry_in_seconds if retry_in_seconds is not None else 60
        next_retry = time.time() + backoff
        # Roll back the attempt counter so the row isn't penalized.
        conn.execute(
            "UPDATE live_event_queue SET status = 'pending', "
            "attempts = MAX(attempts - 1, 0), "
            "previous_last_error = last_error, "
            "last_error = ?, next_retry_at = ? WHERE id = ?",
            (err[:500], next_retry, row_id),
        )
        logger.info("LES row %d transient defer in %ds: %s",
                    row_id, backoff, err[:200])
        conn.commit()
        # Wave 4 PR-B: mirror transient defer (attempts rolled back
        # to max(N-1, 0)). Re-read for the authoritative post-rollback
        # count so the mirror doesn't drift below zero.
        try:
            r = conn.execute(
                "SELECT attempts FROM live_event_queue WHERE id = ?",
                (row_id,),
            ).fetchone()
            new_attempts = int(r['attempts']) if r else 0
        except sqlite3.Error:
            new_attempts = None  # type: ignore[assignment]
        _dual_write_pipeline_live_event_state(
            int(row_id),
            status='pending',
            attempts=new_attempts,
            last_error=err[:500],
            next_retry_at=next_retry,
        )
        return

    if attempts >= LIVE_EVENT_RETRY_MAX_ATTEMPTS:
        conn.execute(
            "UPDATE live_event_queue SET status = 'failed', "
            "previous_last_error = last_error, "
            "last_error = ? WHERE id = ?",
            (err[:500], row_id),
        )
        logger.error("LES row %d exhausted retries: %s", row_id, err[:200])
        conn.commit()
        # Wave 4 PR-B: terminal failure — promote to ``live_event_done``
        # stage with status='failed' so the row is no longer picked by
        # the unified worker (Phase I.2).
        _dual_write_pipeline_live_event_state(
            int(row_id),
            new_stage='live_event_done',
            status='failed',
            attempts=int(attempts),
            last_error=err[:500],
            completed_at=time.time(),
        )
        return
    # Pick the backoff for this attempt (clamp to last entry).
    idx = max(0, attempts - 1)
    if LIVE_EVENT_RETRY_BACKOFF_SECONDS:
        idx = min(idx, len(LIVE_EVENT_RETRY_BACKOFF_SECONDS) - 1)
        backoff = (retry_in_seconds if retry_in_seconds is not None
                   else LIVE_EVENT_RETRY_BACKOFF_SECONDS[idx])
    else:
        backoff = retry_in_seconds if retry_in_seconds is not None else 60
    next_retry = time.time() + backoff
    conn.execute(
        "UPDATE live_event_queue SET status = 'pending', "
        "previous_last_error = last_error, "
        "last_error = ?, next_retry_at = ? WHERE id = ?",
        (err[:500], next_retry, row_id),
    )
    logger.warning("LES row %d retry %d in %ds: %s",
                   row_id, attempts, backoff, err[:200])
    conn.commit()
    # Wave 4 PR-B: mirror retry-defer.
    _dual_write_pipeline_live_event_state(
        int(row_id),
        status='pending',
        attempts=int(attempts),
        last_error=err[:500],
        next_retry_at=next_retry,
    )


def retry_failed(row_id: Optional[int] = None) -> int:
    """Reset failed rows to pending. Returns count reset.

    If ``row_id`` is given, only that row is reset; otherwise all
    failed rows are reset. Also re-arms ``pending`` rows whose
    ``attempts`` already exceeded the cap (defensive — should not
    happen, but if a config change lowered ``retry_max_attempts`` we
    don't want stuck rows). Wakes the worker on success.

    **Preserves ``last_error`` and ``previous_last_error``** so the
    Failed Jobs UI can keep showing why the row failed before retry —
    matches the contract used by ``archive_queue.retry_dead_letter``,
    ``cloud_archive_service.retry_failed``, and
    ``indexing_queue_service.requeue_dead_letter``. The next genuine
    failure will rotate the columns through ``_mark_failed`` as
    usual; a successful retry takes the row out of the failed view
    so the stale error is no longer rendered.
    """
    affected_ids: List[int] = []
    try:
        conn = _open_db()
        try:
            _ensure_schema(conn)
            if row_id is not None:
                # Capture id BEFORE the UPDATE so we can dual-write
                # afterwards. Using a guarded SELECT mirrors the
                # ``WHERE status = 'failed'`` filter on the UPDATE.
                row = conn.execute(
                    "SELECT id FROM live_event_queue "
                    "WHERE id = ? AND status = 'failed'",
                    (row_id,),
                ).fetchone()
                cur = conn.execute(
                    "UPDATE live_event_queue SET status = 'pending', "
                    "attempts = 0, next_retry_at = NULL "
                    "WHERE id = ? AND status = 'failed'",
                    (row_id,),
                )
                if row is not None and cur.rowcount:
                    affected_ids.append(int(row['id']))
            else:
                rows = conn.execute(
                    "SELECT id FROM live_event_queue "
                    "WHERE status = 'failed'"
                ).fetchall()
                cur = conn.execute(
                    "UPDATE live_event_queue SET status = 'pending', "
                    "attempts = 0, next_retry_at = NULL "
                    "WHERE status = 'failed'"
                )
                if cur.rowcount:
                    affected_ids.extend(int(r['id']) for r in rows)
            n = cur.rowcount
            if n:
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error("LES retry_failed failed: %s", e)
        return 0
    # Wave 4 PR-B: mirror the reset-to-pending into pipeline_queue.
    # Done AFTER the legacy commit + close so a pipeline lock can't
    # delay the legacy unlock.
    for affected in affected_ids:
        _dual_write_pipeline_live_event_state(
            affected,
            status='pending',
            attempts=0,
            next_retry_at=0.0,
        )
    if n:
        _wake.set()
    return n


def delete_failed(row_id: Optional[int] = None) -> int:
    """Permanently delete failed rows from ``live_event_queue``.

    When ``row_id`` is given, only that one row is removed. When
    ``None``, every failed row in the queue is removed — the
    "Delete all" path on the Failed Jobs page (#161).

    The companion to :func:`retry_failed`: same WHERE filter
    (``status = 'failed'``), but ``DELETE`` instead of ``UPDATE``.
    Use when retry isn't going to help — the source ``event.json`` /
    minute is permanently gone, the cloud destination is permanently
    rejecting the upload, etc.

    The ``register_event_json_callback`` watcher will re-enqueue the
    event if Tesla writes the same ``event.json`` again, but in
    practice Tesla doesn't rewrite events for the same minute, so a
    delete here is effectively permanent. Returns the number of rows
    actually deleted (``0`` if nothing matched). Returns ``0`` on any
    DB error so a UI delete-all click never blows up the request
    handler.
    """
    try:
        conn = _open_db()
        try:
            _ensure_schema(conn)
            if row_id is not None:
                cur = conn.execute(
                    "DELETE FROM live_event_queue "
                    "WHERE id = ? AND status = 'failed'",
                    (row_id,),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM live_event_queue WHERE status = 'failed'"
                )
            n = cur.rowcount
            if n:
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error("LES delete_failed failed: %s", e)
        return 0
    return n or 0


# ---------------------------------------------------------------------------
# File selection — what gets uploaded for an event
# ---------------------------------------------------------------------------


def select_event_files(event_dir: str, mode: str,
                       event_timestamp: Optional[str]) -> List[str]:
    """Return absolute paths of files to upload for this event.

    ``event_minute``: the ``event.json`` plus the 6 camera files whose
    filename minute-prefix (``YYYY-MM-DD_HH-MM``) matches the event
    timestamp. If the timestamp can't be parsed, falls back to the
    minute prefix derived from the event_dir name.

    ``event_folder``: the ``event.json`` plus every ``.mp4`` in the
    event directory.

    Bounded — never recurses into subdirectories.
    """
    files: List[str] = []
    ej = os.path.join(event_dir, 'event.json')
    if os.path.isfile(ej):
        files.append(ej)

    try:
        entries = os.listdir(event_dir)
    except OSError as e:
        logger.warning("LES select_event_files: cannot list %s: %s",
                       event_dir, e)
        return files

    if mode == 'event_folder':
        for name in entries:
            if name.lower().endswith('.mp4'):
                files.append(os.path.join(event_dir, name))
        return files

    # event_minute (default): match files whose minute prefix brackets
    # the event timestamp.
    minute_key = _resolve_event_minute_key(event_timestamp, event_dir)
    if minute_key is None:
        # Fallback: include all .mp4 files (rare — better to include too
        # much than miss the event).
        logger.warning(
            "LES could not resolve minute key for %s — falling back to "
            "all .mp4 in event dir",
            event_dir,
        )
        for name in entries:
            if name.lower().endswith('.mp4'):
                files.append(os.path.join(event_dir, name))
        return files

    cam_suffixes = tuple(f'-{cam}.mp4' for cam in CAMERA_ANGLES)
    for name in entries:
        m = _MINUTE_PREFIX_RE.match(name)
        if not m:
            continue
        if m.group(1) != minute_key:
            continue
        if not name.lower().endswith(cam_suffixes):
            continue
        files.append(os.path.join(event_dir, name))

    return files


def _resolve_event_minute_key(event_timestamp: Optional[str],
                              event_dir: str) -> Optional[str]:
    """Best-effort: derive ``YYYY-MM-DD_HH-MM`` from timestamp or dir name."""
    # Try parsing the event.json timestamp first (ISO 8601).
    if event_timestamp:
        try:
            # Tesla writes "2024-05-07T14:32:10" — accept with or without offset.
            ts = event_timestamp.split('+')[0].rstrip('Z')
            dt = datetime.fromisoformat(ts)
            return dt.strftime('%Y-%m-%d_%H-%M')
        except (ValueError, TypeError):
            pass

    # Fall back to the directory name (Tesla names dirs by start time).
    base = os.path.basename(event_dir.rstrip('/').rstrip('\\'))
    m = _MINUTE_PREFIX_RE.match(base)
    if m:
        return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Daily data cap
# ---------------------------------------------------------------------------


def _today_uploaded_bytes(conn: sqlite3.Connection) -> int:
    """Sum bytes_uploaded for rows uploaded today (UTC)."""
    today_iso = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    row = conn.execute(
        "SELECT COALESCE(SUM(bytes_uploaded), 0) AS total "
        "FROM live_event_queue "
        "WHERE status = 'uploaded' AND uploaded_at >= ?",
        (today_iso,),
    ).fetchone()
    return int(row['total']) if row else 0


def _data_cap_exceeded(conn: sqlite3.Connection) -> bool:
    if LIVE_EVENT_DAILY_DATA_CAP_MB <= 0:
        return False
    cap_bytes = LIVE_EVENT_DAILY_DATA_CAP_MB * 1024 * 1024
    return _today_uploaded_bytes(conn) >= cap_bytes


# ---------------------------------------------------------------------------
# Cross-subsystem coordination helper (called by cloud_archive_service)
# ---------------------------------------------------------------------------


def has_ready_live_event_work(db_path: Optional[str] = None) -> bool:
    """Return True iff there's an LES row that cloud_archive should yield to.

    Called by ``cloud_archive_service`` between files and at
    ``trigger_auto_sync`` time. The check is intentionally strict so a
    single stuck row doesn't suppress cloud_archive forever:

    * LES must be enabled in config.
    * Daily data cap must NOT be reached (LES paused itself = cloud_archive
      may proceed).
    * Either ``uploading`` (definitely in flight) OR ``pending`` AND
      eligible (``next_retry_at`` past AND ``attempts < retry_max_attempts``).
      Rows in backoff or exhausted-retries do NOT block cloud_archive.

    Sub-millisecond when there is no work (uses indexed columns + LIMIT 1).
    Safe to call from any thread; opens a fresh short-lived connection.
    """
    if not LIVE_EVENT_SYNC_ENABLED:
        return False
    path = db_path or CLOUD_ARCHIVE_DB_PATH
    try:
        conn = sqlite3.connect(path, timeout=5)
        try:
            # uploading is always priority — we must wait for it to finish.
            row = conn.execute(
                "SELECT 1 FROM live_event_queue "
                "WHERE status = 'uploading' LIMIT 1"
            ).fetchone()
            if row is not None:
                return True
            # If the daily cap is reached, LES is intentionally paused
            # for the rest of the day; let cloud_archive proceed.
            if _data_cap_exceeded(conn):
                return False
            now_ts = time.time()
            row = conn.execute(
                "SELECT 1 FROM live_event_queue "
                "WHERE status = 'pending' "
                "  AND (next_retry_at IS NULL OR next_retry_at <= ?) "
                "  AND attempts < ? "
                "LIMIT 1",
                (now_ts, LIVE_EVENT_RETRY_MAX_ATTEMPTS),
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except sqlite3.OperationalError:
        # Table doesn't exist yet — first run before LES schema init.
        return False
    except Exception as e:  # noqa: BLE001
        logger.debug("has_ready_live_event_work: %s", e)
        return False


# ---------------------------------------------------------------------------
# Notifications (optional webhook)
# ---------------------------------------------------------------------------


def _post_webhook(event_row: Dict, files_uploaded: int,
                  bytes_uploaded: int) -> None:
    """Best-effort POST to the configured notify webhook URL.

    Sends only relative event identifiers (e.g.
    ``SentryClips/2024-05-07_14-32-10``) — never the absolute local
    path, which would leak SD-card mount layout to whatever third-party
    receives the webhook.
    """
    url = LIVE_EVENT_NOTIFY_WEBHOOK_URL
    if not url:
        return
    # Defence-in-depth: urllib.request.urlopen will happily honor
    # file://, ftp://, data://, etc. The webhook URL is admin-supplied
    # via config.yaml so this is a typo-defense, not an external
    # exploit vector — but we restrict to http/https either way.
    try:
        scheme = (urlparse(url).scheme or '').lower()
    except Exception:  # noqa: BLE001
        scheme = ''
    if scheme not in ('http', 'https'):
        if not getattr(_post_webhook, '_warned_bad_scheme', False):
            logger.warning(
                "LES webhook URL has unsupported scheme %r (must be "
                "http or https); skipping delivery.",
                scheme,
            )
            _post_webhook._warned_bad_scheme = True  # type: ignore[attr-defined]
        return
    raw_dir = event_row.get('event_dir') or ''
    rel = _relative_event_path(raw_dir) or os.path.basename(
        raw_dir.rstrip('/').rstrip('\\'),
    )
    payload = json.dumps({
        "type": "live_event_uploaded",
        "event": rel,
        "event_timestamp": event_row.get('event_timestamp'),
        "event_reason": event_row.get('event_reason'),
        "files_uploaded": files_uploaded,
        "bytes_uploaded": bytes_uploaded,
        "uploaded_at": _now_iso(),
    }).encode('utf-8')
    try:
        req = urllib.request.Request(
            url, data=payload, method='POST',
            headers={'Content-Type': 'application/json'},
        )
        # Short timeout — the worker has already released the
        # task_coordinator before calling us, but we still don't want
        # a slow webhook to delay the next drain pass for minutes.
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read(1024)  # bounded drain; ignore body
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        logger.warning("LES webhook POST failed: %s", e)


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------


# How long to block on _wake.wait() between drain attempts when the
# queue has work but WiFi is down or task_coordinator is busy. Short
# enough to react to WiFi-connect callbacks promptly; long enough to
# keep idle CPU near zero.
_WAIT_WHEN_BUSY_SECONDS = 60.0
# How long to sleep between back-to-back uploads to let the gadget
# endpoint and SD card breathe.
_INTER_UPLOAD_SLEEP_SECONDS = 1.0


def _set_active(proc: Optional[subprocess.Popen]) -> None:
    global _active_proc
    with _active_proc_lock:
        _active_proc = proc


def _count_mp4s(files: List[str]) -> int:
    """Count how many entries in ``files`` are ``.mp4`` clips."""
    return sum(1 for f in files if f.lower().endswith('.mp4'))


# How long after enqueue we keep transient-deferring an event that has
# 0 MP4s yet (Tesla still writing). After this we either upload what
# we have (the event_folder mode might want metadata-only) or move the
# row to permanent failure, depending on mode.
_TRANSIENT_NO_MP4_MAX_AGE_SECONDS = 30 * 60   # 30 min
# How long to wait before re-checking when MP4s are still appearing.
_TRANSIENT_RETRY_SECONDS = 60

# Max age of event.json mtime to enqueue from the startup catch-up
# scan. Prevents re-enabling LES from triggering uploads of historical
# events the user didn't intend.
LIVE_EVENT_CATCHUP_MAX_AGE_DAYS = 7


def _enqueue_age_seconds(enqueued_at: Optional[str]) -> float:
    """Best-effort age-since-enqueue in seconds."""
    if not enqueued_at:
        return 0.0
    try:
        ts = enqueued_at.split('+')[0].rstrip('Z')
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except (ValueError, TypeError):
        return 0.0


def _process_one(conn: sqlite3.Connection, row: Dict,
                 cancel_event: threading.Event) -> Tuple[bool, str, int, bool, int]:
    """Upload one event.

    Returns ``(success, error_msg, bytes_uploaded, transient,
    files_uploaded)``. When ``transient`` is True the caller should
    requeue without consuming a retry attempt — used for the "MP4s
    aren't fully written yet" case so we never permanently fail an
    event because we raced Tesla. ``files_uploaded`` is the number of
    files actually transferred to the cloud (0 on failure / transient
    defer); the caller reuses it for the webhook payload to avoid
    re-scanning the event directory.
    """
    from services.cloud_archive_service import (
        load_provider_creds, write_rclone_conf, remove_rclone_conf,
        upload_path_via_rclone,
    )

    event_dir = row['event_dir']
    if not os.path.isdir(event_dir):
        return False, "Event directory missing", 0, False, 0

    # Refresh the RO mount once before file selection so we see Tesla's
    # latest writes (kernel caches the loop-mount view otherwise).
    try:
        from services.mapping_service import _refresh_ro_mount
        from services.video_service import get_teslacam_path
        tc = get_teslacam_path()
        if tc:
            _refresh_ro_mount(tc)
    except Exception:
        pass

    upload_scope = row.get('upload_scope') or LIVE_EVENT_UPLOAD_SCOPE
    files = select_event_files(
        event_dir, upload_scope, row.get('event_timestamp'),
    )
    mp4_count = _count_mp4s(files)
    age_seconds = _enqueue_age_seconds(row.get('enqueued_at'))

    # Critical correctness rule: never mark an event "uploaded" if we
    # only saw event.json. The MP4s ARE the event — uploading metadata
    # only is silent data loss. Defer transiently until MP4s appear.
    if mp4_count == 0:
        if age_seconds < _TRANSIENT_NO_MP4_MAX_AGE_SECONDS:
            return (False, "No MP4 clips visible yet — deferring",
                    0, True, 0)
        # Aged out — give up so the row doesn't sit forever.
        return (False,
                f"No MP4 clips after {int(age_seconds)}s — giving up",
                0, False, 0)

    creds = load_provider_creds()
    if not creds:
        # Treat as transient — credentials may come back. Don't consume
        # an attempt for a config issue the user is likely fixing.
        return False, "Cloud provider credentials unavailable", 0, True, 0

    # Use a UNIQUE conf path so cloud_archive's yield/re-acquire cycle
    # never collides with us on the shared tmpfs file. Include thread
    # ident so two concurrent rclone runs (shouldn't happen, but
    # belt-and-suspenders) don't trample each other either.
    conf_name = f"rclone-les-{os.getpid()}-{threading.get_ident()}.conf"
    conf_path = write_rclone_conf(
        CLOUD_ARCHIVE_PROVIDER, creds, conf_name=conf_name,
    )
    bytes_total = 0
    try:
        # Build the remote folder path from the event_dir's relative
        # location under the watched folders.
        # event_dir example: /mnt/gadget/part1-ro/TeslaCam/SentryClips/2024-05-07_14-32-10
        rel_event = _relative_event_path(event_dir)
        if rel_event is None:
            return False, "Could not compute remote path", 0, False, 0
        remote_dir = f"teslausb:{CLOUD_ARCHIVE_REMOTE_PATH}/{rel_event}"

        for local_path in files:
            if cancel_event.is_set():
                return False, "Cancelled", bytes_total, False, 0
            filename = os.path.basename(local_path)
            remote_dest = f"{remote_dir}/{filename}"

            # Use copyto (single file). The helper handles nice/ionice/bwlimit.
            returncode, stderr = upload_path_via_rclone(
                local_path, remote_dest, conf_path,
                CLOUD_ARCHIVE_MAX_UPLOAD_MBPS,
                timeout_seconds=600,
                proc_callback=_set_active,
            )
            if returncode != 0:
                return (False,
                        (stderr or f"rclone exit {returncode}").strip()[:500],
                        bytes_total, False, 0)
            try:
                bytes_total += os.path.getsize(local_path)
            except OSError:
                pass

        return True, "", bytes_total, False, len(files)
    finally:
        remove_rclone_conf(conf_path)


def _relative_event_path(event_dir: str) -> Optional[str]:
    """Reduce ``event_dir`` to ``<watch_folder>/<event_name>`` if possible."""
    parts = event_dir.replace("\\", "/").split("/")
    for folder in LIVE_EVENT_WATCH_FOLDERS:
        if folder in parts:
            i = parts.index(folder)
            tail = parts[i:i + 2]
            if len(tail) == 2:
                return "/".join(tail)
    return None


def _drain_once(cancel_event: threading.Event) -> bool:
    """Try to upload pending events until queue is empty or we yield.

    Returns True if at least one event was processed (success or fail);
    False if there was nothing to do or we couldn't get the lock.
    """
    from services.task_coordinator import acquire_task, release_task
    from services.cloud_archive_service import is_wifi_connected

    if not is_wifi_connected():
        return False

    conn = _open_db()
    try:
        _ensure_schema(conn)

        # Daily cap check up front
        if _data_cap_exceeded(conn):
            with _status_lock:
                _status["data_cap_reached"] = True
            return False
        else:
            with _status_lock:
                _status["data_cap_reached"] = False

        processed = False
        # Webhook payloads queued to fire AFTER releasing the global
        # task_coordinator lock — see issue #10. Bounded list (we never
        # process more than the queue depth in one drain pass).
        pending_webhooks: List[Dict] = []
        while not cancel_event.is_set():
            row = _claim_next_pending(conn)
            if row is None:
                break

            # Acquire the task lock BEFORE counting an attempt. If the
            # lock is busy, return the row to pending without burning
            # a retry — the worker will tick again later.
            if not acquire_task('live_event_sync'):
                _release_claim_to_pending(conn, row['id'])
                # Sleep briefly so we don't busy-spin if indexer is
                # parsing many small files in a row.
                time.sleep(1.0)
                return processed

            attempt_no = _record_attempt_start(conn, row['id'])
            try:
                if attempt_no == 0:
                    # I-3: row vanished between claim and attempt-start
                    # (operator manually deleted from live_event_queue).
                    # Skip it without recording a phantom failure; the
                    # finally block below still releases the task lock.
                    logger.warning(
                        "LES row %d disappeared between claim and "
                        "attempt-start; skipping", row['id'],
                    )
                    continue

                with _status_lock:
                    _status["running"] = True
                    _status["active_file"] = row.get('event_dir')

                logger.info(
                    "LES uploading: %s (reason=%s, attempt %d/%d)",
                    row['event_dir'], row.get('event_reason'),
                    attempt_no, LIVE_EVENT_RETRY_MAX_ATTEMPTS,
                )
                success, err, bytes_uploaded, transient, files_uploaded = (
                    _process_one(conn, row, cancel_event)
                )
                if success:
                    _mark_uploaded(conn, row['id'], bytes_uploaded)
                    with _status_lock:
                        _status["last_uploaded_at"] = _now_iso()
                        _status["last_uploaded_event"] = row.get('event_dir')
                        _status["last_error"] = None
                        _status["data_uploaded_today_bytes"] = (
                            _today_uploaded_bytes(conn)
                        )
                    logger.info(
                        "LES uploaded: %s (%.1f KB)",
                        row['event_dir'], bytes_uploaded / 1024,
                    )
                    # Defer webhook delivery until after we release the
                    # task_coordinator lock (slow webhooks must not
                    # block the indexer/cloud_archive). Reuse the file
                    # count returned by _process_one rather than
                    # re-running select_event_files() (I-1).
                    pending_webhooks.append({
                        "row": dict(row),
                        "files_uploaded": files_uploaded,
                        "bytes_uploaded": bytes_uploaded,
                    })
                else:
                    _mark_failed(conn, row['id'], attempt_no, err,
                                 transient=transient)
                    with _status_lock:
                        _status["last_error"] = err[:500]
                        _status["last_error_at"] = _now_iso()
                processed = True
            except Exception as e:
                logger.exception("LES worker exception processing row %d", row['id'])
                _mark_failed(conn, row['id'], attempt_no, str(e)[:500])
                with _status_lock:
                    _status["last_error"] = str(e)[:500]
                    _status["last_error_at"] = _now_iso()
                processed = True
            finally:
                with _status_lock:
                    _status["running"] = False
                    _status["active_file"] = None
                release_task('live_event_sync')

            # Pause between events so the gadget endpoint stays snappy.
            if not cancel_event.is_set() and _INTER_UPLOAD_SLEEP_SECONDS > 0:
                time.sleep(_INTER_UPLOAD_SLEEP_SECONDS)

            # Re-check daily cap before pulling the next row.
            if _data_cap_exceeded(conn):
                with _status_lock:
                    _status["data_cap_reached"] = True
                break

        # Fire webhooks AFTER releasing the lock (we've already
        # released for the last event in the finally above; this loop
        # holds nothing). A slow/dead webhook can't block the next LES
        # event or the indexer this way.
        for wh in pending_webhooks:
            try:
                _post_webhook(wh["row"], wh["files_uploaded"],
                              wh["bytes_uploaded"])
            except Exception:
                # _post_webhook already swallows network errors; this
                # catches any other surprise without taking the worker
                # down.
                logger.exception("LES webhook delivery failed")

        return processed
    finally:
        conn.close()


def _startup_catchup_scan() -> int:
    """Discover ``event.json`` files that exist on-disk but aren't queued.

    Covers two real-world cases the file-watcher can't catch:

    * gadget_web was restarting / down when Tesla wrote the event;
      inotify wasn't listening at the right moment.
    * LES was just enabled in config and there are pre-existing
      events on the SD card / USB image that the user wants uploaded.

    Bounded: only walks the configured ``LIVE_EVENT_WATCH_FOLDERS``
    under each TeslaCam root, one level deep. ``UNIQUE(event_dir)`` in
    the queue makes re-enqueues idempotent. Returns the number of new
    rows inserted.
    """
    if not LIVE_EVENT_SYNC_ENABLED:
        return 0
    cutoff_seconds = LIVE_EVENT_CATCHUP_MAX_AGE_DAYS * 86400
    now_ts = time.time()
    discovered: List[str] = []
    for root in _teslacam_roots():
        for folder in LIVE_EVENT_WATCH_FOLDERS:
            base = os.path.join(root, folder)
            if not os.path.isdir(base):
                continue
            try:
                for entry in os.scandir(base):
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    ej = os.path.join(entry.path, 'event.json')
                    if not os.path.isfile(ej):
                        continue
                    # I-4: bound the scan by event.json mtime so
                    # re-enabling LES doesn't enqueue years of
                    # historical events the user didn't intend to
                    # upload.
                    try:
                        mtime = os.stat(ej).st_mtime
                    except OSError:
                        # Conservative skip: if we can't stat we can't
                        # safely classify the age.
                        logger.debug(
                            "LES catchup scan stat failed for %s — skipping",
                            ej,
                        )
                        continue
                    age_seconds = now_ts - mtime
                    if age_seconds > cutoff_seconds:
                        logger.debug(
                            "LES catchup scan skipping %s (age %.1fd > %dd)",
                            ej, age_seconds / 86400,
                            LIVE_EVENT_CATCHUP_MAX_AGE_DAYS,
                        )
                        continue
                    discovered.append(ej)
            except OSError as e:
                logger.debug("LES catchup scan skipped %s: %s", base, e)
    if not discovered:
        return 0
    inserted = enqueue_event_json(discovered)
    if inserted:
        logger.info(
            "LES startup catch-up: discovered %d existing event.json file(s) "
            "(%d newly enqueued)", len(discovered), inserted,
        )
    return inserted


def _worker_loop() -> None:
    """Top-level worker loop. Idle on ``_wake``; never busy-loops."""
    logger.info("Live Event Sync worker started")

    # Startup recovery + prune (one-shot per process).
    try:
        conn = _open_db()
        try:
            _ensure_schema(conn)
            _startup_recovery(conn)
            _prune_old_uploaded(conn)
        finally:
            conn.close()
    except Exception as e:
        logger.error("LES startup recovery failed: %s", e)

    # Sweep orphaned rclone-les-*.conf files left behind by previous
    # worker processes that didn't run their finally blocks (SIGKILL,
    # OOM, power loss before sync, etc.). I-2.
    try:
        _sweep_orphaned_rclone_confs()
    except Exception as e:
        logger.error("LES startup rclone-conf sweep failed: %s", e)

    # One-shot catch-up scan: enqueue any event.json files already on
    # disk that haven't been queued yet (LES was disabled previously,
    # gadget_web was restarting when Tesla wrote them, etc.). The
    # UNIQUE(event_dir) constraint makes this safe to call on every
    # start.
    try:
        _startup_catchup_scan()
    except Exception as e:
        logger.error("LES startup catch-up scan failed: %s", e)

    # Wake immediately so an existing pending queue starts draining.
    _wake.set()

    while not _worker_stop.is_set():
        # Block until somebody wakes us (enqueue, WiFi-connect, or stop).
        # If the queue has work but we couldn't drain (WiFi down, task
        # coordinator busy, etc.), fall back to a slow timeout so we
        # don't busy-poll.
        _wake.wait(timeout=_WAIT_WHEN_BUSY_SECONDS)
        _wake.clear()
        if _worker_stop.is_set():
            break
        try:
            _drain_once(_worker_stop)
        except Exception as e:
            # Containment: never let the worker thread die.
            logger.exception("LES worker loop iteration failed: %s", e)
            with _status_lock:
                _status["last_error"] = str(e)[:500]
                _status["last_error_at"] = _now_iso()

    logger.info("Live Event Sync worker stopped")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start() -> bool:
    """Start the worker thread. Idempotent.

    Returns True if a new thread was started; False if disabled or
    already running.
    """
    global _worker_thread
    if not LIVE_EVENT_SYNC_ENABLED:
        logger.info("Live Event Sync disabled in config — not starting worker")
        return False
    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            return False
        _worker_stop.clear()
        _worker_thread = threading.Thread(
            target=_worker_loop, name="live-event-sync", daemon=True,
        )
        _worker_thread.start()
        return True


def stop(timeout: float = 5.0) -> bool:
    """Stop the worker thread (best-effort; daemon survives at process exit)."""
    global _worker_thread
    _worker_stop.set()
    _wake.set()
    with _active_proc_lock:
        proc = _active_proc
    if proc is not None:
        try:
            proc.terminate()
        except (OSError, ProcessLookupError):
            pass
    thread = _worker_thread
    if thread and thread.is_alive():
        thread.join(timeout=timeout)
        return not thread.is_alive()
    return True


def wake() -> None:
    """Wake the worker (called by the WiFi-connect dispatcher)."""
    _wake.set()


def get_status() -> Dict:
    """Return a status snapshot for the API blueprint."""
    snapshot: Dict = {}
    with _status_lock:
        snapshot.update(_status)

    # Add queue counts (cheap aggregate query). Phase 4.6 (#101)
    # reuses the same connection to compute the fresh data-cap
    # state so the cloud_archive page banner is accurate even when
    # the worker is idle (LES idles between events; the cached
    # ``data_cap_reached`` would otherwise lag until the next
    # upload cycle).
    try:
        conn = _open_db()
        try:
            _ensure_schema(conn)
            counts = {}
            for st in ('pending', 'uploading', 'uploaded', 'failed'):
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM live_event_queue "
                    "WHERE status = ?",
                    (st,),
                ).fetchone()
                counts[st] = int(row['n']) if row else 0
            snapshot['queue_counts'] = counts

            # Phase 4.6 — fresh today-uploaded count + cap state so
            # the cloud_archive banner is correct on every poll.
            today_bytes = _today_uploaded_bytes(conn)
            snapshot['data_uploaded_today_bytes'] = today_bytes
            cap_mb = int(LIVE_EVENT_DAILY_DATA_CAP_MB)
            snapshot['daily_data_cap_mb'] = cap_mb
            if cap_mb > 0:
                cap_bytes = cap_mb * 1024 * 1024
                snapshot['data_cap_reached'] = today_bytes >= cap_bytes
                snapshot['data_cap_pct'] = min(
                    100, int(round((today_bytes / cap_bytes) * 100))
                )
            else:
                # Unlimited — banner stays hidden, percent is null.
                snapshot['data_cap_reached'] = False
                snapshot['data_cap_pct'] = None
        finally:
            conn.close()
    except Exception as e:
        snapshot['queue_counts'] = {'error': str(e)[:200]}
        # Defensive: keep the cap fields present even on DB error so
        # the JS consumer doesn't crash on missing keys (mirrors the
        # Phase 4.4 ETA / Phase 4.5 pause_reason contract).
        snapshot.setdefault('data_uploaded_today_bytes', 0)
        snapshot.setdefault('daily_data_cap_mb', int(LIVE_EVENT_DAILY_DATA_CAP_MB))
        snapshot.setdefault('data_cap_reached', False)
        snapshot.setdefault('data_cap_pct', None)

    # Surface the cross-subsystem coordination signal so the
    # WiFi-connect dispatcher's drain wait can yield correctly when
    # the only remaining rows are in backoff / over the retry cap /
    # blocked by the daily data cap.
    try:
        snapshot['has_ready_work'] = has_ready_live_event_work()
    except Exception:
        snapshot['has_ready_work'] = False

    return snapshot


def list_queue(limit: int = 50) -> List[Dict]:
    """Return up to ``limit`` recent queue rows for the status panel."""
    try:
        conn = _open_db()
        try:
            _ensure_schema(conn)
            rows = conn.execute(
                "SELECT id, event_dir, event_timestamp, event_reason, "
                "upload_scope, status, enqueued_at, uploaded_at, "
                "attempts, last_error, previous_last_error, "
                "bytes_uploaded "
                "FROM live_event_queue "
                "ORDER BY enqueued_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        logger.error("LES list_queue failed: %s", e)
        return []


def count_failed() -> int:
    """Return the number of ``failed`` rows in the live-event queue.

    Cheap (single ``SELECT COUNT(*)`` over the ``status`` index) so the
    Failed Jobs counts endpoint and the future status-dot poller can
    call this every few seconds without touching the row data. Returns
    ``0`` on any DB error so a failed read can never break the
    aggregate counts page.
    """
    try:
        conn = _open_db()
        try:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM live_event_queue "
                "WHERE status = 'failed'"
            ).fetchone()
            return int(row['n']) if row else 0
        finally:
            conn.close()
    except Exception as e:
        logger.warning("LES count_failed failed: %s", e)
        return 0
