"""Unified pipeline queue helpers — issue #184 Wave 4 — Phase I.1.

This module is the single point of access for the new ``pipeline_queue``
table in ``geodata.db``. The table itself is created by
``mapping_migrations._SCHEMA_SQL`` at v16; this module owns the
producer / consumer / backfill API.

**Phase I.1 only adds the dual-write side.** Legacy producers
(``archive_queue.enqueue_for_archive``, ``indexing_queue_service.
enqueue_for_indexing``, and the cloud-synced-files insertion path)
call into this module's :func:`dual_write_enqueue` after they write
to their own legacy table. Reads remain on the legacy tables — no
behaviour change yet.

Design rules:

* **Best-effort dual-write.** A failure to write to ``pipeline_queue``
  must never fail the legacy enqueue. The legacy queue is the source
  of truth in Phase I.1; pipeline_queue is shadow data being validated.
  All errors are logged at WARNING and swallowed.
* **Idempotent.** The composite unique constraint
  ``(source_path, stage, legacy_table)`` plus ``INSERT OR IGNORE``
  makes repeated dual-writes harmless. Producers that re-enqueue
  (e.g. inotify firing on the same path twice) write at most one
  pipeline_queue row.
* **Cross-DB writes are short-lived connections.** The
  ``cloud_synced_files`` dual-write is the only cross-DB case (cloud
  state is in ``cloud_sync.db``; ``pipeline_queue`` is in
  ``geodata.db``). Each dual-write opens a fresh ``geodata.db``
  connection, writes one row, and closes. No long-lived second
  connection is held alongside the legacy DB connection — that
  would double the connection count and complicate task_coordinator
  semantics.

Public API:

* :data:`STAGE_*` constants — canonical stage names.
* :data:`LEGACY_TABLE_*` constants — canonical legacy table names.
* :data:`PRIORITY_*` constants — canonical priorities.
* :func:`dual_write_enqueue` — producer hook.
* :func:`dual_write_enqueue_many` — batched producer hook.
* :func:`update_pipeline_row` — state-mirror by ``(stage, source_path)``.
* :func:`update_pipeline_row_by_legacy_id` — state-mirror by
  ``(legacy_table, legacy_id)``.
* :func:`claim_next_for_stage` — Wave 4 PR-C reader API; atomic
  pick-and-claim of the next pending row in a stage. Production
  workers wire to this in PR-D.
* :func:`peek_next_for_stage` — Wave 4 PR-C; non-mutating "what's
  next" lookup (parity tests + Settings page).
* :func:`peek_top_n_paths_for_stage` — Wave 4 PR-E; non-mutating
  list of the next N candidate ``source_path`` values in pick order.
  Used by the archive worker's shadow-mode comparison to tolerate
  the documented secondary-sort divergence (``enqueued_at`` vs
  legacy ``expected_mtime``) — a legacy pick is treated as "agreed"
  if it appears anywhere in the pipeline_queue's top-N candidates.
* :func:`ready_count_for_stage` — Wave 4 PR-C; cheap COUNT(*) of
  eligible rows for a stage.
* :func:`recover_stale_claims_pipeline` — Wave 4 PR-D; release
  ``in_progress`` rows whose claimer crashed (claimed_at older
  than threshold). Called once at worker startup.
* :func:`get_recovery_telemetry` — Wave 4 PR-E (#195); read the
  in-memory stale-recovery counters (total recovered, last call
  timestamp, last call count, total call count). Reset on
  ``gadget_web`` restart.
* :func:`pipeline_status` — debug / verification view (counts grouped
  by legacy_table + stage + status), augmented with the
  ``get_recovery_telemetry`` counters.
* :func:`backfill_legacy_queues` — one-time migration helper.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical constants — every dual-write site MUST use these strings.
# ---------------------------------------------------------------------------

# Stage values. The unified worker (Phase I.2) selects on
# ``WHERE stage = ? AND status = 'pending'``; producer hooks set the
# initial stage when enqueuing.
#
# Note: there is no ``STAGE_LIVE_EVENT_*`` family. Issue #184 PR-F4
# folded LES into the unified cloud worker; live-event uploads are
# now ``stage='cloud_pending'`` rows distinguished only by
# ``priority=PRIORITY_LIVE_EVENT`` (0) vs. bulk catch-up at
# ``priority=PRIORITY_CLOUD_BULK`` (4).
STAGE_ARCHIVE_PENDING = 'archive_pending'
STAGE_ARCHIVE_DONE = 'archive_done'
STAGE_INDEX_PENDING = 'index_pending'
STAGE_INDEX_DONE = 'index_done'
STAGE_CLOUD_PENDING = 'cloud_pending'
STAGE_CLOUD_DONE = 'cloud_done'

# Legacy table names — used by the dual-write hooks to tag which
# legacy producer created each pipeline_queue row.
#
# Note: there is no ``LEGACY_TABLE_LIVE_EVENT``. The
# ``live_event_queue`` table was dropped in cloud_sync.db v4
# (issue #202) after Wave 4 PR-F4 deleted the LES subsystem.
LEGACY_TABLE_ARCHIVE = 'archive_queue'
LEGACY_TABLE_INDEXING = 'indexing_queue'
LEGACY_TABLE_CLOUD_SYNCED = 'cloud_synced_files'

# Priority mapping — lower is more urgent.
PRIORITY_LIVE_EVENT = 0          # live-event upload (Sentry/Saved event.json)
PRIORITY_ARCHIVE_EVENT = 1       # Sentry / Saved clips
PRIORITY_ARCHIVE_RECENT = 2      # RecentClips (age-bound)
PRIORITY_ARCHIVE_OTHER = 3       # ArchivedClips back-fill / other
PRIORITY_CLOUD_BULK = 4          # cloud_synced_files bulk catch-up
PRIORITY_INDEXING = 5            # default indexing


# Stale-claim threshold for ``recover_stale_claims_pipeline`` — same
# default as ``indexing_queue_service._STALE_CLAIM_SECONDS`` (30 min).
# A claim older than this is presumed orphaned by a crashed worker
# and recycled back to ``status='pending'`` on the next worker
# startup. Tuned to be longer than the longest legitimate
# single-row processing time (an extreme archive copy of a 60s
# multi-camera Sentry event may take ~5 minutes under load + the
# task_coordinator may pause on high loadavg) but short enough that
# a real crash is detected within the same boot cycle.
_PIPELINE_STALE_CLAIM_SECONDS = 1800.0


# Wave 4 PR-E (issue #195): stale-recovery telemetry counters.
# Process-local (no DB write), reset on gadget_web restart. Surfaced
# via :func:`pipeline_status` so operators can spot a flapping crash
# loop that releases the same rows over and over (which would
# otherwise only show up as repeated WARNING lines in the journal).
# A monotonic counter, the timestamp of the last non-zero recovery
# call, and the count from that call are all the operator needs to
# answer "is recovery healthy or pathological?".
_recover_stale_claims_lock = threading.Lock()
_recover_stale_claims_total = 0
_recover_stale_claims_last_at: Optional[float] = None
_recover_stale_claims_last_count = 0
_recover_stale_claims_call_count = 0  # incl. zero-result calls


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def resolve_pipeline_db() -> Optional[str]:
    """Public helper: return the geodata.db path, or None if config can't be loaded.

    Lazy import of ``config`` so unit tests that don't bootstrap the
    Flask app can still import this module without side effects.
    Logs at DEBUG when the import fails so a broken bootstrap is at
    least detectable in ``journalctl -u gadget_web`` (the dual-write
    helpers swallow the resulting ``False`` return at WARNING via
    their own paths, but a silent ``None`` here would otherwise look
    indistinguishable from a deliberate test injection).
    """
    try:
        from config import MAPPING_DB_PATH  # type: ignore
        return MAPPING_DB_PATH
    except Exception as e:  # noqa: BLE001
        logger.debug("pipeline_queue config not loaded: %s", e)
        return None


# Backward-compat private alias. Existing in-tree callers and tests
# referenced ``_resolve_pipeline_db`` before it was promoted; keep the
# name working so we don't have to touch every call site at once.
_resolve_pipeline_db = resolve_pipeline_db


def _open_pipeline_conn(db_path: str) -> sqlite3.Connection:
    """Open the pipeline DB with the same conservative pragmas as the
    rest of the geodata.db consumers. Caller must close.

    ``synchronous=NORMAL`` + ``journal_mode=WAL`` are set here
    defensively even though both are technically DB-wide and already
    set by other geodata.db openers (`archive_queue._open_archive_conn`,
    `indexing_queue_service._open_queue_conn`). ``journal_mode`` is
    DB-wide and persistent; ``synchronous`` is per-connection and
    defaults to ``FULL`` (~2× fsync latency) — without this line the
    pipeline_queue path would be slower than the legacy queues that
    sit beside it.
    """
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA mmap_size=0")
    conn.execute("PRAGMA cache_size=-256")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _now_epoch() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def dual_write_enqueue(*,
                       source_path: str,
                       stage: str,
                       legacy_table: str,
                       legacy_id: Optional[int] = None,
                       priority: int = PRIORITY_INDEXING,
                       dest_path: Optional[str] = None,
                       payload: Optional[Dict[str, Any]] = None,
                       status: str = 'pending',
                       db_path: Optional[str] = None) -> bool:
    """Insert a row into ``pipeline_queue`` mirroring a legacy enqueue.

    Returns True if a new row was inserted, False if the row already
    exists (idempotent), and False if any error occurs (logged at
    WARNING). NEVER raises.

    Args:
        source_path: The resource being processed. For archive /
            indexing this is the file path; for LES it's the
            ``event.json`` path; for cloud_synced_files it's the
            file path.
        stage: One of the ``STAGE_*`` constants.
        legacy_table: One of the ``LEGACY_TABLE_*`` constants — which
            old queue this row mirrors.
        legacy_id: Back-pointer to the legacy row's primary key, if
            available. Used by the migration helper to verify that
            every legacy row has a corresponding pipeline_queue row.
        priority: Lower = more urgent. Default ``PRIORITY_INDEXING``.
        dest_path: Final destination on SD card (archive only); None
            for queues that don't have a destination.
        payload: Queue-specific extras that don't fit the flat schema.
            Stored as JSON in the ``payload_json`` column. Examples:
            ``{'expected_size': 1234, 'expected_mtime': 1.0}`` for
            archive; ``{'event_reason': 'sentry', 'upload_scope':
            'event_minute'}`` for LES.
        status: Initial within-stage status. Defaults to ``'pending'``
            for live producer hooks. Backfill paths pass the
            translated legacy status (``'in_progress'`` / ``'done'`` /
            ``'failed'``) so an already-completed legacy row lands as
            ``stage='X_done', status='done'`` rather than
            ``status='pending'`` (which would re-process it).
        db_path: Override the geodata.db path (test injection).
    """
    if not source_path or not stage or not legacy_table:
        return False
    if db_path is None:
        db_path = _resolve_pipeline_db()
    if not db_path:
        return False
    # Don't auto-create the DB on a misconfigured deploy; signal
    # missing-DB consistently with ``pipeline_status``.
    if not os.path.isfile(db_path):
        logger.debug(
            "pipeline_queue dual-write skipped (DB %s missing)", db_path,
        )
        return False
    payload_text = json.dumps(payload, separators=(',', ':')) if payload else None
    conn = None
    try:
        conn = _open_pipeline_conn(db_path)
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO pipeline_queue
                (source_path, dest_path, stage, status, priority,
                 enqueued_at, payload_json, legacy_id, legacy_table)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source_path, dest_path, stage, status, int(priority),
             _now_epoch(), payload_text, legacy_id, legacy_table),
        )
        conn.commit()
        return bool(cur.rowcount)
    except sqlite3.Error as e:
        # Best-effort: a failed dual-write must NOT fail the legacy
        # enqueue. Log at WARNING so the operator can see drift if
        # this ever fires repeatedly.
        logger.warning(
            "pipeline_queue dual-write failed for %s/%s: %s",
            legacy_table, source_path, e,
        )
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def dual_write_enqueue_many(rows: Iterable[Dict[str, Any]],
                            db_path: Optional[str] = None) -> int:
    """Batched dual-write — same semantics as ``dual_write_enqueue``
    but for a list of rows.

    ``rows`` is an iterable of dicts with keys matching the named
    arguments of :func:`dual_write_enqueue` (including the optional
    ``status`` key). Returns the count of newly-inserted rows.
    Errors on individual rows are NOT raised; the batch continues.
    SQLite errors at the executemany level return 0 and log a
    warning.

    The returned count uses ``conn.total_changes`` deltas around the
    ``executemany`` because SQLite's ``cur.rowcount`` after
    ``executemany`` is the LAST statement's row count, not the sum,
    and is unreliable when ``INSERT OR IGNORE`` skips some rows.
    """
    if db_path is None:
        db_path = _resolve_pipeline_db()
    if not db_path:
        return 0
    if not os.path.isfile(db_path):
        logger.debug(
            "pipeline_queue batched dual-write skipped (DB %s missing)",
            db_path,
        )
        return 0
    rows = list(rows)
    if not rows:
        return 0
    now = _now_epoch()
    tuples = []
    for r in rows:
        src = r.get('source_path')
        stage = r.get('stage')
        legacy_table = r.get('legacy_table')
        if not src or not stage or not legacy_table:
            continue
        payload = r.get('payload')
        payload_text = json.dumps(payload, separators=(',', ':')) if payload else None
        tuples.append((
            src, r.get('dest_path'), stage,
            r.get('status', 'pending'),
            int(r.get('priority', PRIORITY_INDEXING)),
            now, payload_text,
            r.get('legacy_id'), legacy_table,
        ))
    if not tuples:
        return 0
    conn = None
    try:
        conn = _open_pipeline_conn(db_path)
        before = conn.total_changes
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            """
            INSERT OR IGNORE INTO pipeline_queue
                (source_path, dest_path, stage, status, priority,
                 enqueued_at, payload_json, legacy_id, legacy_table)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuples,
        )
        conn.commit()
        return max(0, conn.total_changes - before)
    except sqlite3.Error as e:
        logger.warning(
            "pipeline_queue dual-write batch failed (%d rows): %s",
            len(tuples), e,
        )
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


_UPDATE_COLUMNS: Tuple[str, ...] = (
    "new_stage", "status", "attempts", "last_error",
    "completed_at", "next_retry_at", "payload_json",
)
"""Names of the optional kwargs accepted by both
:func:`update_pipeline_row` and
:func:`update_pipeline_row_by_legacy_id`.

Used by the "no kwargs passed" gate AND the SET-clause builder so a
future column addition needs to be made in exactly one place. The
DB column name happens to differ from the kwarg name only for
``new_stage`` (column is ``stage``); :data:`_KWARG_TO_COLUMN`
translates.
"""

_KWARG_TO_COLUMN: Dict[str, str] = {
    "new_stage": "stage",
    "status": "status",
    "attempts": "attempts",
    "last_error": "last_error",
    "completed_at": "completed_at",
    "next_retry_at": "next_retry_at",
    "payload_json": "payload_json",
}


def _build_update_sql_and_params(
    where_clause: str,
    where_params: List[Any],
    **kwargs,
) -> Optional[Tuple[str, List[Any]]]:
    """Build the UPDATE SQL + bind params for the ``update_pipeline_row*``
    helpers.

    Returns ``None`` when no settable kwargs were passed (so the
    caller can short-circuit with the documented "silent no-op"
    semantics). Otherwise returns ``(sql, params)`` where ``params``
    is in positional order: SET values first, then the ``where_params``
    appended.
    """
    set_clauses: List[str] = []
    set_params: List[Any] = []
    for kwarg_name in _UPDATE_COLUMNS:
        value = kwargs.get(kwarg_name)
        if value is None:
            continue
        column = _KWARG_TO_COLUMN[kwarg_name]
        set_clauses.append(f"{column} = ?")
        set_params.append(value)
    if not set_clauses:
        return None
    sql = (
        "UPDATE pipeline_queue SET "
        + ", ".join(set_clauses)
        + " WHERE " + where_clause
    )
    return sql, set_params + list(where_params)


def update_pipeline_row(
    *,
    stage: str,
    source_path: str,
    new_stage: Optional[str] = None,
    status: Optional[str] = None,
    attempts: Optional[int] = None,
    last_error: Optional[str] = None,
    completed_at: Optional[float] = None,
    next_retry_at: Optional[float] = None,
    payload_json: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    """Update an existing ``pipeline_queue`` row identified by
    ``(stage, source_path)`` to reflect a state transition on the
    legacy queue side (claim / complete / release / fail).

    Used by the legacy queue mutation functions in ``archive_queue``,
    ``indexing_queue_service``, ``live_event_sync_service`` and
    ``cloud_archive_service`` to keep ``pipeline_queue`` in sync with
    every state change — without that, the table stale-drifts to
    ``status='pending'`` after PR-A's enqueue dual-write fires once
    and is never updated again.

    **Idempotent / silent no-op semantics.** Returns ``False`` (not an
    error) when:
      * the DB doesn't exist (test or pre-migration boot),
      * the matching row doesn't exist (PR-A only mirrored enqueues
        that happened AFTER the dual-write hooks went live; older
        legacy rows backfilled at PR-A time also exist, but very old
        rows from before any tracking may legitimately not be there),
      * sqlite hits an OperationalError (lock contention etc.).

    All ``None`` parameters are LEFT UNCHANGED in the row. Pass only
    what changed. ``new_stage`` is the optional terminal-stage
    transition (e.g. ``archive_pending`` → ``archive_done``); when
    omitted, the existing stage is preserved.

    Caller MUST hold the legacy queue write transaction or wrap this
    in their own try/finally — this helper deliberately does not
    raise so a pipeline_queue glitch can never abort a legacy
    mutation.
    """
    if db_path is None:
        db_path = _resolve_pipeline_db()
    if not db_path or not os.path.isfile(db_path):
        return False
    built = _build_update_sql_and_params(
        "stage = ? AND source_path = ?",
        [stage, source_path],
        new_stage=new_stage, status=status, attempts=attempts,
        last_error=last_error, completed_at=completed_at,
        next_retry_at=next_retry_at, payload_json=payload_json,
    )
    if built is None:
        # Nothing to update — silently no-op so callers don't have to
        # track which optional kwargs they passed.
        return False
    sql, params = built
    conn = None
    try:
        conn = _open_pipeline_conn(db_path)
        cur = conn.execute(sql, params)
        conn.commit()
        # ``rowcount`` here is the count of ROWS matched/updated
        # for a non-executemany UPDATE — accurate.
        return cur.rowcount > 0
    except sqlite3.Error as e:
        logger.debug(
            "update_pipeline_row(stage=%s, src=%s) failed: %s",
            stage, source_path, e,
        )
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def update_pipeline_row_by_legacy_id(
    *,
    legacy_table: str,
    legacy_id: int,
    new_stage: Optional[str] = None,
    status: Optional[str] = None,
    attempts: Optional[int] = None,
    last_error: Optional[str] = None,
    completed_at: Optional[float] = None,
    next_retry_at: Optional[float] = None,
    payload_json: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    """Same as :func:`update_pipeline_row` but matches the row by
    ``(legacy_table, legacy_id)`` — the back-pointer columns
    populated at enqueue time. Lets ``archive_queue`` /
    ``cloud_archive_service`` update by the same integer ``row_id``
    they already know without a second SELECT for ``source_path``.

    Same silent no-op semantics as the source_path variant.
    """
    if db_path is None:
        db_path = _resolve_pipeline_db()
    if not db_path or not os.path.isfile(db_path):
        return False
    if not legacy_table or legacy_id is None:
        return False
    built = _build_update_sql_and_params(
        "legacy_table = ? AND legacy_id = ?",
        [legacy_table, int(legacy_id)],
        new_stage=new_stage, status=status, attempts=attempts,
        last_error=last_error, completed_at=completed_at,
        next_retry_at=next_retry_at, payload_json=payload_json,
    )
    if built is None:
        return False
    sql, params = built
    conn = None
    try:
        conn = _open_pipeline_conn(db_path)
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount > 0
    except sqlite3.Error as e:
        logger.debug(
            "update_pipeline_row_by_legacy_id(table=%s, id=%s) failed: %s",
            legacy_table, legacy_id, e,
        )
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def release_pipeline_claim(
    *,
    legacy_table: str,
    legacy_id: int,
    last_error: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    """Release a claimed pipeline_queue row back to ``status='pending'``,
    nulling ``claimed_by`` and ``claimed_at``.

    Wave 4 PR-F1 (issue #184): used by ``archive_worker`` when a
    pipeline_queue row was claimed but the work didn't actually
    start (e.g. the legacy mirror failed or the dispatch path bailed
    before doing real work). Differs from
    :func:`update_pipeline_row_by_legacy_id` with ``status='pending'``
    in that it ALSO clears the claim metadata — leaving stale
    ``claimed_by`` / ``claimed_at`` on a ``pending`` row would
    confuse forensics (looks like a stuck active claim) and would
    NOT be picked up by :func:`recover_stale_claims_pipeline`
    (which filters on ``status='in_progress'`` only).

    The ``_UPDATE_COLUMNS`` whitelist used by
    :func:`update_pipeline_row*` deliberately excludes ``claimed_by``
    / ``claimed_at`` so callers can't accidentally clear claim state
    via the generic update API; this helper is the public, named
    entry point for "release a claim cleanly."

    Args:
        legacy_table: One of the ``LEGACY_TABLE_*`` constants — same
            semantics as :func:`update_pipeline_row_by_legacy_id`.
        legacy_id: ``archive_queue.id`` (or equivalent legacy PK).
        last_error: Optional human-readable string stamped into
            ``last_error`` for forensic context. ``None`` (default)
            leaves the existing ``last_error`` untouched.
        db_path: Override the geodata.db path (test injection).

    Returns:
        ``True`` on a successful UPDATE (rowcount > 0). ``False`` on
        missing DB, missing args, sqlite error, or no matching row
        — same silent no-op semantics as the rest of this module.
        Returns ``False`` (not raising) so a pipeline_queue glitch
        cannot abort the caller's mutation flow.
    """
    if db_path is None:
        db_path = _resolve_pipeline_db()
    if not db_path or not os.path.isfile(db_path):
        return False
    if not legacy_table or legacy_id is None:
        return False
    if last_error is not None:
        sql = (
            "UPDATE pipeline_queue "
            "   SET status = 'pending', "
            "       claimed_by = NULL, "
            "       claimed_at = NULL, "
            "       last_error = ? "
            " WHERE legacy_table = ? AND legacy_id = ?"
        )
        params = (last_error, legacy_table, int(legacy_id))
    else:
        sql = (
            "UPDATE pipeline_queue "
            "   SET status = 'pending', "
            "       claimed_by = NULL, "
            "       claimed_at = NULL "
            " WHERE legacy_table = ? AND legacy_id = ?"
        )
        params = (legacy_table, int(legacy_id))
    conn = None
    try:
        conn = _open_pipeline_conn(db_path)
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount > 0
    except sqlite3.Error as e:
        logger.debug(
            "release_pipeline_claim(table=%s, id=%s) failed: %s",
            legacy_table, legacy_id, e,
        )
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def dead_letter_pipeline_row_by_id(
    *,
    row_id: int,
    last_error: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    """Move a single ``pipeline_queue`` row to ``status='dead_letter'``
    keyed by its primary-key ``id``.

    Wave 4 PR-F3 review fix (issue #184): used by
    ``cloud_archive_service._claim_via_pipeline_reader_cloud`` when a
    claimed row has an unrecoverable data-shape gap (e.g. empty
    ``source_path``) — the legacy_id-keyed and source_path-keyed
    helpers can't address such a row by content. Without an id-keyed
    dead-letter the only options are (a) leave it in ``in_progress``
    so :func:`recover_stale_claims_pipeline` keeps recycling it back
    to ``pending`` only for the same gap to re-fire (a recycle loop),
    or (b) write an inline UPDATE in every caller. This helper closes
    the gap cleanly.

    Behaviour:
      * Atomically sets ``status='dead_letter'``, NULLs ``claimed_by``
        / ``claimed_at`` (so the row presents as a clean dead-letter
        entry to operators and to :func:`recover_stale_claims_pipeline`,
        which only picks up ``in_progress``).
      * Optional ``last_error`` stamped for forensics; ``None`` leaves
        the existing error untouched.
      * Returns ``True`` on rowcount > 0, ``False`` otherwise.
        Never raises — silent no-op on missing DB / sqlite errors,
        same defensive contract as the rest of this module.
    """
    if db_path is None:
        db_path = _resolve_pipeline_db()
    if not db_path or not os.path.isfile(db_path):
        return False
    if row_id is None:
        return False
    try:
        row_id_int = int(row_id)
    except (TypeError, ValueError):
        return False
    if last_error is not None:
        sql = (
            "UPDATE pipeline_queue "
            "   SET status = 'dead_letter', "
            "       claimed_by = NULL, "
            "       claimed_at = NULL, "
            "       last_error = ? "
            " WHERE id = ?"
        )
        params: Tuple[Any, ...] = (last_error, row_id_int)
    else:
        sql = (
            "UPDATE pipeline_queue "
            "   SET status = 'dead_letter', "
            "       claimed_by = NULL, "
            "       claimed_at = NULL "
            " WHERE id = ?"
        )
        params = (row_id_int,)
    conn = None
    try:
        conn = _open_pipeline_conn(db_path)
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount > 0
    except sqlite3.Error as e:
        logger.debug(
            "dead_letter_pipeline_row_by_id(id=%s) failed: %s",
            row_id_int, e,
        )
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def release_pipeline_claim_by_source_path(
    *,
    stage: str,
    source_path: str,
    last_error: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    """Release a claimed row keyed by ``(stage, source_path)``.

    Wave 4 PR-F3 (issue #184): the cloud_archive variant of
    :func:`release_pipeline_claim`. Cloud rows are mirrored from
    ``cloud_synced_files`` lazily — the producer in
    ``cloud_archive_service._discover_events`` enqueues with
    ``source_path = <relative event.json path>`` but does NOT set
    ``legacy_id`` because the corresponding ``cloud_synced_files``
    row is not created until upload starts. The legacy_id-keyed
    :func:`release_pipeline_claim` therefore cannot find these rows.

    The lookup uses the ``(stage, source_path)`` UNIQUE index — the
    same index PR-A added for dual-write enqueue idempotency — so
    this is O(1) and never matches more than one row.

    Behaviour parity with :func:`release_pipeline_claim`:
      * Resets ``status='pending'``, NULLs ``claimed_by`` /
        ``claimed_at`` (so :func:`recover_stale_claims_pipeline`
        won't pick the row up — it filters on ``in_progress``).
      * Optional ``last_error`` stamped for forensics; ``None``
        leaves the existing error untouched.
      * Returns ``True`` on rowcount > 0, ``False`` otherwise.
        Never raises — silent no-op on missing DB / sqlite errors,
        same defensive contract as the rest of this module.
    """
    if db_path is None:
        db_path = _resolve_pipeline_db()
    if not db_path or not os.path.isfile(db_path):
        return False
    if not stage or not source_path:
        return False
    if last_error is not None:
        sql = (
            "UPDATE pipeline_queue "
            "   SET status = 'pending', "
            "       claimed_by = NULL, "
            "       claimed_at = NULL, "
            "       last_error = ? "
            " WHERE stage = ? AND source_path = ?"
        )
        params = (last_error, stage, source_path)
    else:
        sql = (
            "UPDATE pipeline_queue "
            "   SET status = 'pending', "
            "       claimed_by = NULL, "
            "       claimed_at = NULL "
            " WHERE stage = ? AND source_path = ?"
        )
        params = (stage, source_path)
    conn = None
    try:
        conn = _open_pipeline_conn(db_path)
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount > 0
    except sqlite3.Error as e:
        logger.debug(
            "release_pipeline_claim_by_source_path(stage=%s, "
            "source_path=%r) failed: %s",
            stage, source_path, e,
        )
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


# ---------------------------------------------------------------------------
# Reader API — Wave 4 PR-C (issue #184)
# ---------------------------------------------------------------------------
# These functions let a worker treat ``pipeline_queue`` as the source of
# truth for the *next item to process*, instead of querying the legacy
# table directly. PR-C only ADDS the API; production wiring (switching
# the archive worker over) ships in PR-D so the pipeline_queue view of
# the world can be observed for one release before any worker depends
# on it.

def claim_next_for_stage(
    *,
    stage: str,
    claimed_by: str,
    db_path: Optional[str] = None,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Atomically claim the next pending row in ``stage``.

    The intended consumer is the unified worker (Wave 4 PR-D). This
    function is purely a read+update on ``pipeline_queue`` — it does
    NOT touch any legacy table. PR-D will compose this with the
    existing legacy ``mark_*`` helpers (which already dual-write back
    via PR-B) so a row's lifecycle stays consistent across both
    tables during the transition.

    Pick order:
      ``WHERE stage = ? AND status = 'pending'
            AND (next_retry_at IS NULL OR next_retry_at <= ?)
       ORDER BY priority ASC, enqueued_at ASC, id ASC LIMIT 1``

    The legacy archive_queue uses ``priority ASC, expected_mtime ASC
    NULLS LAST, id ASC`` — we use ``enqueued_at`` instead because
    ``expected_mtime`` lives in ``payload_json`` and ``json_extract``
    cannot use the ``idx_pipeline_ready`` partial index. In production
    ``enqueued_at`` is a usable proxy: inotify enqueues files in
    arrival order (≈ mtime order from Tesla's POV), and boot catch-up
    enqueues in directory-walk order which is also roughly mtime
    order. Within a single batched enqueue the ordering may differ
    from a strict mtime sort — this is acceptable for PR-C (parity
    with the no-reads-yet baseline). PR-D will add an
    ``expected_mtime REAL`` column or json-extract index if backlog
    catch-up shows the proxy is insufficient.

    Atomicity: the SELECT-then-UPDATE pair runs inside a single
    ``BEGIN IMMEDIATE`` transaction so a concurrent claim from a
    second worker cannot double-pick the same row. The UPDATE adds a
    defensive ``WHERE status = 'pending'`` guard so even if BEGIN
    IMMEDIATE were defeated (unrelated busy-timeout race), the second
    worker's UPDATE returns rowcount=0 and we report "no work" rather
    than handing out a duplicated claim.

    Args:
        stage: One of the ``STAGE_*_PENDING`` constants. Filtering
            on stage is required — this function refuses to operate
            without one (returns ``None``) so a caller bug can't
            sweep the entire queue indiscriminately.
        claimed_by: Operator-readable string for diagnostics.
            Persisted to the row's ``claimed_by`` column AND echoed
            back as the synthesised ``_claimed_by`` key in the
            returned dict (the latter is preserved for backward
            compat with PR-C callers — it equals the persisted
            value). The persistence (added in PR-D / schema v17)
            lets :func:`recover_stale_claims_pipeline` detect rows
            whose claimer crashed and recycle them back to
            ``status='pending'``.
        db_path: Override the geodata.db path (test injection).
        now: Override the "current time" used for ``next_retry_at``
            comparisons AND for the ``claimed_at`` timestamp
            written into the row. **Production code MUST pass
            ``None`` (or omit the argument) so the persisted
            ``claimed_at`` reflects the wall-clock time used by
            :func:`recover_stale_claims_pipeline` to detect stale
            claims.** Hard-coded values are for test injection only.

    Returns:
        A dict snapshot of the claimed row, augmented with two
        synthesised keys:
          * ``payload``: the deserialised ``payload_json`` (or
            ``{}`` when JSON is absent/malformed) so callers don't
            have to ``json.loads`` themselves.
          * ``_claimed_by``: echoed back verbatim from the input
            argument; useful for log/trace correlation.
        ``status`` and ``attempts`` reflect the post-UPDATE values
        (``'in_progress'`` and ``previous + 1`` respectively) so the
        caller sees the same view the DB has after commit.

        Returns ``None`` if the queue is empty for the stage, the
        DB doesn't exist, ``stage`` is empty, or any sqlite error
        fires.
    """
    if not stage:
        return None
    if db_path is None:
        db_path = _resolve_pipeline_db()
    if not db_path or not os.path.isfile(db_path):
        return None
    if now is None:
        now = _now_epoch()
    conn = None
    try:
        conn = _open_pipeline_conn(db_path)
        conn.execute("BEGIN IMMEDIATE")
        sel = conn.execute(
            """SELECT * FROM pipeline_queue
                WHERE stage = ?
                  AND status = 'pending'
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY priority ASC, enqueued_at ASC, id ASC
                LIMIT 1""",
            (stage, now),
        ).fetchone()
        if sel is None:
            conn.rollback()
            return None
        cur = conn.execute(
            """UPDATE pipeline_queue
                  SET status = 'in_progress',
                      attempts = attempts + 1,
                      claimed_by = ?,
                      claimed_at = ?
                WHERE id = ? AND status = 'pending'""",
            (claimed_by, now, sel['id']),
        )
        if cur.rowcount != 1:
            # Another worker raced us between the SELECT and the
            # UPDATE despite BEGIN IMMEDIATE (shouldn't happen, but
            # the defensive WHERE catches it). Roll back so we don't
            # half-commit a stale view.
            conn.rollback()
            return None
        conn.commit()
        row = dict(sel)
        row['status'] = 'in_progress'
        row['attempts'] = (sel['attempts'] or 0) + 1
        # v17: persist the claim so ``recover_stale_claims_pipeline``
        # can detect crashed workers. The legacy ``_claimed_by``
        # synthesised key is preserved for callers that already
        # consume it (it's identical to the new persisted column).
        row['claimed_by'] = claimed_by
        row['claimed_at'] = now
        if row.get('payload_json'):
            try:
                parsed = json.loads(row['payload_json'])
            except (json.JSONDecodeError, TypeError):
                parsed = {}
            # Defensive: producers declare ``payload: Dict[str, Any]``
            # but a hand-crafted (or future) row could store a JSON
            # list / number / string. Callers expect ``.get(...)`` to
            # work, so coerce non-dict to empty dict — matches the
            # docstring's "(or `{}` when JSON is absent/malformed)"
            # contract.
            row['payload'] = parsed if isinstance(parsed, dict) else {}
        else:
            row['payload'] = {}
        row['_claimed_by'] = claimed_by
        return row
    except sqlite3.Error as e:
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        logger.warning(
            "claim_next_for_stage(stage=%s) failed: %s", stage, e,
        )
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def peek_next_for_stage(
    *,
    stage: str,
    db_path: Optional[str] = None,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Return the next claimable row for ``stage`` WITHOUT claiming it.

    Same ordering and filters as :func:`claim_next_for_stage`. Used
    by parity tests and by ``/api/pipeline_queue/status`` to surface
    "what would the worker pick next?" without actually picking it.

    Returns the row as a plain dict (no payload-parsing convenience —
    callers that need the payload can ``json.loads`` themselves), or
    ``None`` if there's nothing ready.
    """
    if not stage:
        return None
    if db_path is None:
        db_path = _resolve_pipeline_db()
    if not db_path or not os.path.isfile(db_path):
        return None
    if now is None:
        now = _now_epoch()
    conn = None
    try:
        conn = _open_pipeline_conn(db_path)
        sel = conn.execute(
            """SELECT * FROM pipeline_queue
                WHERE stage = ?
                  AND status = 'pending'
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY priority ASC, enqueued_at ASC, id ASC
                LIMIT 1""",
            (stage, now),
        ).fetchone()
        return dict(sel) if sel is not None else None
    except sqlite3.Error as e:
        logger.debug(
            "peek_next_for_stage(stage=%s) failed: %s", stage, e,
        )
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def ready_count_for_stage(
    *,
    stage: str,
    db_path: Optional[str] = None,
    now: Optional[float] = None,
) -> int:
    """Count rows in ``stage`` that are eligible for the next claim.

    Cheap O(idx) scan against ``idx_pipeline_ready`` — safe to call
    from the Settings page. Returns 0 on any error or missing DB.
    """
    if not stage:
        return 0
    if db_path is None:
        db_path = _resolve_pipeline_db()
    if not db_path or not os.path.isfile(db_path):
        return 0
    if now is None:
        now = _now_epoch()
    conn = None
    try:
        conn = _open_pipeline_conn(db_path)
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM pipeline_queue
                WHERE stage = ?
                  AND status = 'pending'
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)""",
            (stage, now),
        ).fetchone()
        return int(row['n']) if row is not None else 0
    except sqlite3.Error as e:
        logger.debug(
            "ready_count_for_stage(stage=%s) failed: %s", stage, e,
        )
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def peek_top_n_paths_for_stage(
    *,
    stage: str,
    limit: int,
    db_path: Optional[str] = None,
    now: Optional[float] = None,
) -> Tuple[str, ...]:
    """Return up to ``limit`` ``source_path`` candidates in pick order.

    Same WHERE / ORDER BY as :func:`peek_next_for_stage` and
    :func:`claim_next_for_stage`. Used by the archive worker's
    shadow-mode comparison to tolerate the documented secondary-sort
    divergence (``enqueued_at`` here vs ``expected_mtime`` in the
    legacy ``archive_queue`` reader). A legacy pick is treated as
    "agreed" by the shadow path if it appears anywhere in this
    top-N — only when the legacy row is **absent** from the
    pipeline_queue's top-N do we have a real dual-write gap worth
    a WARNING.

    Cheap by design: returns just the ``source_path`` strings (no
    payload parsing, no row dicts) so the worker hot-path adds at
    most one indexed-SELECT per iteration. ``Tuple`` (not ``list``)
    so callers can use it in ``in`` checks without worrying about
    accidental mutation.

    Args:
        stage: One of the ``STAGE_*_PENDING`` constants. Refused
            (returns empty tuple) when empty / falsy.
        limit: Maximum number of candidates to return. Clamped to
            ``[1, 50]`` — the shadow path doesn't benefit from
            larger windows and we don't want a buggy caller to
            sweep the whole queue.
        db_path: Override the geodata.db path (test injection).
        now: Override "current time" for ``next_retry_at``
            comparisons (test injection only).

    Returns:
        Tuple of ``source_path`` strings in ``(priority,
        enqueued_at, id)`` order. Empty tuple if the queue is
        empty for the stage, the DB doesn't exist, ``stage`` is
        empty, ``limit`` is non-positive, or any sqlite error
        fires.
    """
    if not stage:
        return ()
    try:
        clamped_limit = max(1, min(50, int(limit)))
    except (TypeError, ValueError):
        return ()
    if db_path is None:
        db_path = _resolve_pipeline_db()
    if not db_path or not os.path.isfile(db_path):
        return ()
    if now is None:
        now = _now_epoch()
    conn = None
    try:
        conn = _open_pipeline_conn(db_path)
        rows = conn.execute(
            """SELECT source_path FROM pipeline_queue
                WHERE stage = ?
                  AND status = 'pending'
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY priority ASC, enqueued_at ASC, id ASC
                LIMIT ?""",
            (stage, now, clamped_limit),
        ).fetchall()
        return tuple(
            r['source_path'] for r in rows if r['source_path']
        )
    except sqlite3.Error as e:
        logger.debug(
            "peek_top_n_paths_for_stage(stage=%s) failed: %s",
            stage, e,
        )
        return ()
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def recover_stale_claims_pipeline(
    *,
    db_path: Optional[str] = None,
    max_age_seconds: float = _PIPELINE_STALE_CLAIM_SECONDS,
    now: Optional[float] = None,
) -> int:
    """Release ``in_progress`` rows whose ``claimed_at`` is older
    than ``max_age_seconds``, recycling them back to ``status='pending'``.

    Mirror of :func:`indexing_queue_service.recover_stale_claims` for
    ``pipeline_queue``. Called once at worker startup so a previous
    crash can't permanently lock a row in ``in_progress``. Without
    this, an ``in_progress`` row whose claimer crashed mid-work
    would be orphaned forever (no ``claimed_at`` timeout, no
    recovery mechanism — exactly the gap PR-D closes per issue #193).

    **API note:** unlike ``indexing_queue_service.recover_stale_claims``
    (positional ``db_path, max_age_seconds``), this function is
    **keyword-only** to match the ``claim_next_for_stage`` /
    ``peek_next_for_stage`` / ``ready_count_for_stage`` style of the
    rest of this module. The reader-API consistency is the priority;
    callers wiring both helpers into a unified worker should pass
    arguments by name.

    The reset:
      * Flips ``status`` from ``'in_progress'`` to ``'pending'``.
      * Clears ``claimed_by`` and ``claimed_at`` (returning the row
        to its pre-claim state).
      * **Preserves** every other field — ``attempts``,
        ``next_retry_at``, ``last_error``, ``payload_json``,
        ``priority``, ``enqueued_at``, ``source_path``, ``stage``,
        ``legacy_id``, ``legacy_table``. ``attempts`` is preserved
        so a row that has already been attempted N times isn't given
        a free retry — the next ``claim_next_for_stage`` will
        increment to N+1. ``next_retry_at`` is preserved so a
        failure-driven backoff (set by the failed-claim path) still
        fires correctly. ``last_error`` is preserved as a forensic
        breadcrumb so operators can see why the claim was retried.

    Args:
        db_path: Override the geodata.db path.
        max_age_seconds: Claims older than this are released.
            Default ``_PIPELINE_STALE_CLAIM_SECONDS`` (1800 s = 30 min).
        now: Override "current time" (test injection only).

    Returns:
        The count of rows released. Returns 0 on missing DB / sqlite
        error (logged at WARNING). NEVER raises.
    """
    # Wave 4 PR-E (issue #195): bump call_count at function entry
    # so an operator querying telemetry sees "recovery is being
    # called" even when the early-return paths fire (missing DB,
    # sqlite error). Without this, a misconfigured DB path would
    # leave call_count=0 and an operator would conclude "recovery
    # never fired" when the truth is "recovery fired but couldn't
    # open the DB". Total / last_at / last_count remain bounded to
    # the actual successful release.
    global _recover_stale_claims_call_count
    with _recover_stale_claims_lock:
        _recover_stale_claims_call_count += 1
    if db_path is None:
        db_path = _resolve_pipeline_db()
    if not db_path or not os.path.isfile(db_path):
        return 0
    if now is None:
        now = _now_epoch()
    cutoff = now - max_age_seconds
    conn = None
    try:
        conn = _open_pipeline_conn(db_path)
        # NOTE: Single-statement UPDATE — relies on sqlite3's implicit
        # ``BEGIN`` for atomicity, which is functionally equivalent to
        # ``BEGIN IMMEDIATE`` for a one-shot UPDATE because the write
        # lock is acquired before any rows are scanned. If this is
        # ever extended to a SELECT-then-UPDATE pattern (e.g. logging
        # which row IDs were released), the transaction MUST be
        # promoted to ``BEGIN IMMEDIATE`` to defend against the same
        # race that ``claim_next_for_stage`` already handles.
        cur = conn.execute(
            """UPDATE pipeline_queue
                  SET status = 'pending',
                      claimed_by = NULL,
                      claimed_at = NULL
                WHERE status = 'in_progress'
                  AND claimed_at IS NOT NULL
                  AND claimed_at < ?""",
            (cutoff,),
        )
        released = cur.rowcount or 0
        conn.commit()
        if released:
            logger.warning(
                "Released %d stale pipeline_queue claim(s) (>%ds old)",
                released, int(max_age_seconds),
            )
        # Wave 4 PR-E (issue #195): bump telemetry counters under
        # lock so concurrent recovery calls (only ever one, but
        # defensive for future workers) don't race on the read-modify-
        # write of the running totals. ``_recover_stale_claims_call_count``
        # is bumped at function entry so it covers the early-return
        # paths too; here we only update the totals/last_at/last_count
        # for the successful-release accounting.
        global _recover_stale_claims_total
        global _recover_stale_claims_last_at
        global _recover_stale_claims_last_count
        with _recover_stale_claims_lock:
            if released > 0:
                _recover_stale_claims_total += released
                _recover_stale_claims_last_at = now
                _recover_stale_claims_last_count = released
        return released
    except sqlite3.Error as e:
        logger.warning("recover_stale_claims_pipeline failed: %s", e)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def get_recovery_telemetry() -> Dict[str, Any]:
    """Return the in-memory stale-recovery counters as a snapshot dict.

    Process-local counters reset on ``gadget_web`` restart. Used by
    :func:`pipeline_status` so the Settings page / status endpoint
    can show "recovery is healthy" vs. "recovery is repeatedly
    releasing rows" — the latter is a strong signal that a worker is
    crash-looping. Per follow-up issue #195.

    Keys:
      * ``stale_recoveries_total``: monotonic count of rows recovered
        since process start.
      * ``stale_recoveries_last_at``: epoch seconds of the last
        non-zero recovery call (``None`` if never).
      * ``stale_recoveries_last_count``: count from the last
        non-zero recovery call (0 if never).
      * ``stale_recoveries_call_count``: total recovery calls made,
        including zero-result calls AND early-return paths
        (missing DB / sqlite error). High call_count + zero total
        = "called but found nothing", which is the healthy steady
        state. High call_count + zero total + persistent
        ``stale_recoveries_last_at = None`` after a known
        misconfigured DB = "called but couldn't open DB" — check
        ``geodata.db`` path / permissions.
    """
    with _recover_stale_claims_lock:
        return {
            'stale_recoveries_total': _recover_stale_claims_total,
            'stale_recoveries_last_at': _recover_stale_claims_last_at,
            'stale_recoveries_last_count': _recover_stale_claims_last_count,
            'stale_recoveries_call_count': _recover_stale_claims_call_count,
        }


def pipeline_status(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Return a small dict summarising the pipeline_queue state.

    Useful for debugging / the Settings page / dual-write parity
    checks. Counts grouped by ``(legacy_table, stage, status)``.

    On any sqlite error the queue-state portion is omitted but the
    in-memory ``get_recovery_telemetry()`` snapshot is still
    returned — telemetry is process-local and arguably MORE useful
    when the DB read is failing (it tells operators "recovery has
    fired N times" even when ``pipeline_queue`` itself can't be
    read). Fully missing DB returns just the telemetry snapshot
    too, for the same reason.
    """
    telemetry = get_recovery_telemetry()
    if db_path is None:
        db_path = _resolve_pipeline_db()
    if not db_path or not os.path.isfile(db_path):
        return dict(telemetry)
    conn = None
    try:
        conn = _open_pipeline_conn(db_path)
        rows = conn.execute(
            """SELECT legacy_table, stage, status, COUNT(*) AS n
               FROM pipeline_queue
               GROUP BY legacy_table, stage, status
               ORDER BY legacy_table, stage, status"""
        ).fetchall()
        result: Dict[str, Any] = {
            'total': sum(r['n'] for r in rows),
            'by_legacy_stage_status': [
                {
                    'legacy_table': r['legacy_table'],
                    'stage': r['stage'],
                    'status': r['status'],
                    'count': r['n'],
                }
                for r in rows
            ],
        }
        # Wave 4 PR-E (issue #195): merge in the in-memory
        # stale-recovery counters so a single endpoint payload
        # answers "is the queue OK and is the recovery healthy?"
        result.update(telemetry)
        return result
    except sqlite3.Error as e:
        logger.warning("pipeline_status failed: %s", e)
        # Surface telemetry even on DB read failure — see docstring.
        return dict(telemetry)
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


# ---------------------------------------------------------------------------
# Backfill from legacy queues — one-time migration helper
# ---------------------------------------------------------------------------

def backfill_legacy_queues(*,
                           pipeline_db_path: Optional[str] = None,
                           cloud_db_path: Optional[str] = None,
                           force: bool = False) -> Dict[str, int]:
    """Backfill ``pipeline_queue`` from the four legacy queue tables.

    Idempotent — re-running is safe (the unique constraint catches
    duplicates). Returns a per-source count of rows inserted.

    This is a one-time migration to handle pending rows that existed
    BEFORE the dual-write hooks were installed (i.e., the backlog at
    upgrade time). After upgrade, dual-write keeps both tables in
    sync; this backfill only covers the upgrade gap.

    **One-shot guard.** After the first successful run we record the
    completion timestamp in ``kv_meta`` (key:
    ``pipeline_backfill_completed_at``). Subsequent calls SKIP the
    work and return zeroes — without this guard the backfill would
    re-scan all four legacy tables on every boot and (for the
    cross-DB tables) open one geodata.db connection per legacy row,
    which on a Pi Zero 2 W stacks tens of seconds of useless SDIO
    fsync work onto the most fragile boot phase. Set ``force=True``
    to bypass the guard (tests and recovery use this).

    Two source DBs:
      * ``pipeline_db_path`` (geodata.db): archive_queue + indexing_queue
      * ``cloud_db_path`` (cloud_sync.db): cloud_synced_files

    Both default to the configured paths via lazy ``config`` import.
    """
    if pipeline_db_path is None:
        pipeline_db_path = _resolve_pipeline_db()
    if cloud_db_path is None:
        try:
            from config import CLOUD_ARCHIVE_DB_PATH  # type: ignore
            cloud_db_path = CLOUD_ARCHIVE_DB_PATH
        except Exception:  # noqa: BLE001
            cloud_db_path = None

    counts = {
        LEGACY_TABLE_ARCHIVE: 0,
        LEGACY_TABLE_INDEXING: 0,
        LEGACY_TABLE_CLOUD_SYNCED: 0,
    }

    # One-shot guard — skip if a prior run completed successfully.
    if not force and pipeline_db_path and os.path.isfile(pipeline_db_path):
        prior = _kv_meta_get(pipeline_db_path,
                             'pipeline_backfill_completed_at')
        if prior:
            logger.debug(
                "pipeline_queue backfill already completed at %s — skipping",
                prior,
            )
            return counts

    if pipeline_db_path and os.path.isfile(pipeline_db_path):
        counts[LEGACY_TABLE_ARCHIVE] = _backfill_archive_queue(pipeline_db_path)
        counts[LEGACY_TABLE_INDEXING] = _backfill_indexing_queue(pipeline_db_path)
    if cloud_db_path and os.path.isfile(cloud_db_path):
        counts[LEGACY_TABLE_CLOUD_SYNCED] = _backfill_cloud_synced_files(
            cloud_db_path, pipeline_db_path,
        )
    total = sum(counts.values())
    if total:
        logger.info("pipeline_queue backfill: %s (total %d)", counts, total)

    # Mark complete on success — even if total==0 (no backlog rows).
    # The guarantee is "we have run the scan"; whether the legacy
    # tables had rows is irrelevant.
    if pipeline_db_path and os.path.isfile(pipeline_db_path):
        _kv_meta_set(pipeline_db_path,
                     'pipeline_backfill_completed_at',
                     datetime.now(timezone.utc).isoformat())
    return counts


def _kv_meta_get(db_path: str, key: str) -> Optional[str]:
    """Read ``kv_meta`` value or return None."""
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        row = conn.execute(
            "SELECT value FROM kv_meta WHERE key = ?", (key,),
        ).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def _kv_meta_set(db_path: str, key: str, value: str) -> bool:
    """Upsert ``kv_meta`` value. Returns True on success."""
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        conn.execute(
            "INSERT INTO kv_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
        return True
    except sqlite3.Error as e:
        logger.warning("kv_meta set %s failed: %s", key, e)
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def _backfill_archive_queue(pipeline_db: str) -> int:
    """Backfill from ``archive_queue`` (same DB as pipeline_queue).

    Single-DB backfill — uses one ``INSERT INTO ... SELECT`` for atomicity.
    """
    conn = None
    try:
        conn = _open_pipeline_conn(pipeline_db)
        # Existence check — archive_queue may not be present on
        # very old DBs.
        if not _table_exists(conn, 'archive_queue'):
            return 0
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO pipeline_queue
                (source_path, dest_path, stage, status, priority,
                 attempts, last_error, enqueued_at, payload_json,
                 legacy_id, legacy_table)
            SELECT
                source_path,
                dest_path,
                CASE
                    WHEN status IN ('copied') THEN 'archive_done'
                    ELSE 'archive_pending'
                END,
                CASE
                    WHEN status = 'pending'  THEN 'pending'
                    WHEN status = 'claimed'  THEN 'in_progress'
                    WHEN status = 'copied'   THEN 'done'
                    ELSE 'failed'
                END,
                COALESCE(priority, 3),
                COALESCE(attempts, 0),
                last_error,
                COALESCE(strftime('%s', enqueued_at) + 0, ?),
                json_object('expected_size', expected_size,
                            'expected_mtime', expected_mtime),
                id,
                'archive_queue'
            FROM archive_queue
            """,
            (_now_epoch(),),
        )
        conn.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    except sqlite3.Error as e:
        logger.warning("backfill archive_queue failed: %s", e)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def _backfill_indexing_queue(pipeline_db: str) -> int:
    """Backfill from ``indexing_queue`` (same DB as pipeline_queue).

    Wave 4 PR-B: ``source_path`` is set to ``canonical_key`` (not
    ``file_path``) so state-mutation lookups by canonical_key work
    against pipeline_queue rows. ``file_path`` is preserved in payload.
    """
    conn = None
    try:
        conn = _open_pipeline_conn(pipeline_db)
        if not _table_exists(conn, 'indexing_queue'):
            return 0
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO pipeline_queue
                (source_path, stage, status, priority,
                 attempts, last_error, enqueued_at, next_retry_at,
                 payload_json, legacy_id, legacy_table)
            SELECT
                canonical_key,
                'index_pending',
                CASE
                    WHEN claimed_by IS NOT NULL THEN 'in_progress'
                    ELSE 'pending'
                END,
                COALESCE(priority, 50),
                COALESCE(attempts, 0),
                last_error,
                COALESCE(enqueued_at, ?),
                next_attempt_at,
                json_object('file_path', file_path,
                            'source', source),
                NULL,
                'indexing_queue'
            FROM indexing_queue
            """,
            (_now_epoch(),),
        )
        conn.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    except sqlite3.Error as e:
        logger.warning("backfill indexing_queue failed: %s", e)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def _backfill_cloud_synced_files(cloud_db: str, pipeline_db: Optional[str]) -> int:
    """Backfill from ``cloud_synced_files`` (cloud_sync.db) into
    ``pipeline_queue`` (geodata.db). CROSS-DB.
    """
    if not pipeline_db or not os.path.isfile(pipeline_db):
        return 0
    src_conn = None
    try:
        src_conn = sqlite3.connect(cloud_db, timeout=10.0)
        src_conn.row_factory = sqlite3.Row
        if not _table_exists(src_conn, 'cloud_synced_files'):
            return 0
        rows = src_conn.execute(
            "SELECT id, file_path, remote_path, file_size, file_mtime, "
            "status, retry_count, last_error, synced_at "
            "FROM cloud_synced_files"
        ).fetchall()
    except sqlite3.Error as e:
        logger.warning("backfill cloud_synced_files read failed: %s", e)
        return 0
    finally:
        if src_conn is not None:
            try:
                src_conn.close()
            except sqlite3.Error:
                pass

    if not rows:
        return 0

    inserted = 0
    for r in rows:
        stage = (
            STAGE_CLOUD_DONE if r['status'] == 'synced'
            else STAGE_CLOUD_PENDING
        )
        # Translate the legacy status to the unified within-stage
        # status. Without this an already-synced row would land
        # ``stage='cloud_done', status='pending'`` and look like
        # work that still needs to be done.
        status = {
            'pending': 'pending',
            'queued': 'pending',
            'uploading': 'in_progress',
            'syncing': 'in_progress',
            'synced': 'done',
            'failed': 'failed',
        }.get(r['status'], 'failed')
        if dual_write_enqueue(
            source_path=r['file_path'],
            stage=stage,
            legacy_table=LEGACY_TABLE_CLOUD_SYNCED,
            legacy_id=r['id'],
            priority=PRIORITY_CLOUD_BULK,
            dest_path=r['remote_path'],
            payload={
                'file_size': r['file_size'],
                'file_mtime': r['file_mtime'],
                'last_error': r['last_error'],
            },
            status=status,
            db_path=pipeline_db,
        ):
            inserted += 1
    return inserted


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
