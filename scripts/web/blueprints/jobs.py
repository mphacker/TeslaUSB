"""Blueprint for the unified Failed Jobs page (Phase 4.1, #101).

Aggregates dead-letter / failed rows from every background subsystem
into one place so the user can see — and recover from — every failure
without clicking through five different pages.

Subsystems aggregated:

* **archive**       — ``archive_queue.archive_queue`` rows in
                      ``status='dead_letter'``
* **indexer**       — ``indexing_queue.indexing_queue`` rows whose
                      ``attempts >= _PARSE_ERROR_MAX_ATTEMPTS``
* **cloud_sync**    — ``cloud_archive.cloud_synced_files`` rows in
                      ``status='dead_letter'``
* **live_event_sync** — ``live_event_sync.live_event_queue`` rows in
                      ``status='failed'``

Routes:

* ``GET  /jobs``                                — HTML page (templates/failed_jobs.html)
* ``GET  /api/jobs/failed?subsystem=&limit=``   — JSON list (combined or per-subsystem)
* ``GET  /api/jobs/counts``                     — JSON ``{archive, indexer, cloud_sync,
                                                  live_event_sync, total}``
* ``POST /api/jobs/retry``                      — body ``{subsystem, id}`` (id is omitted
                                                  to retry every row); returns
                                                  ``{rows_reset}``

All routes are JSON-friendly. The HTML route renders the page shell;
the page polls the JSON endpoints client-side. No image-gating —
the page is informational and lists subsystems independently, so it
must work even when the cam image is missing (it will simply show
empty lists for the cam-dependent subsystems).

The counts endpoint goes through dedicated ``count_*`` helpers in
each subsystem (cheap ``SELECT COUNT(*)``) — never through the
listers. The listers fetch row payloads (``last_error`` strings can
be hundreds of bytes) and would amplify the request to ~7 000 rows /
~16 MB on a large dead-letter backlog, which would defeat the
status-dot polling use case (Phase 4.8).

The ``last_error`` strings returned by the listers are redacted via
:func:`_redact_last_error` to strip rclone bucket/host names and
absolute local paths before they leave the process. Originals stay
in the DB for journalctl / shell triage.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, render_template, request

from config import (
    CLOUD_ARCHIVE_ENABLED,
    LIVE_EVENT_SYNC_ENABLED,
    MAPPING_DB_PATH,
    MAPPING_ENABLED,
)

logger = logging.getLogger(__name__)

jobs_bp = Blueprint('jobs', __name__)


# ---------------------------------------------------------------------------
# Subsystem registry
# ---------------------------------------------------------------------------

# Order matters — this is the order rows appear when subsystem='all'.
# Most-actionable first (failed indexes are usually a stale archive
# pointer; failed archives are usually permission/disk; failed cloud
# uploads are usually auth/quota).
_SUBSYSTEMS = (
    'archive',
    'indexer',
    'cloud_sync',
    'live_event_sync',
)

# ---------------------------------------------------------------------------
# Error-message redaction
# ---------------------------------------------------------------------------

# Strip absolute local paths the user did not pick:
#   /mnt/gadget/...                 (USB RO mount; reveals device layout)
#   /home/<user>/...                (login name on the Pi)
#   /var/..., /run/..., /tmp/...    (system paths an LAN guest doesn't need)
# AND any rclone-style "remote:bucket/..." reference that could disclose the
# user's cloud provider, bucket name, or path on the cloud side. Anything
# that looks like a cloud-host hostname (s3.<region>.amazonaws.com, etc.)
# gets the host stripped to its TLD-1 component.
_REDACT_PATTERNS = (
    (re.compile(r'/(?:mnt|home|var|run|tmp)/[^\s\'"\)]+'), '<path>'),
    (re.compile(r'\b[A-Za-z][A-Za-z0-9_-]{0,30}:[A-Za-z0-9._/-]+'),
     '<remote>'),
    (re.compile(r'\b[A-Za-z0-9-]+\.s3[.-][^\s\'"\)]+\.amazonaws\.com\b'),
     '<s3-host>'),
)
_REDACT_MAX_LEN = 600


def _redact_last_error(msg: Any) -> str:
    """Sanitize a ``last_error`` string for HTTP response.

    Strips absolute local paths and cloud-remote identifiers that an
    LAN/AP guest viewing the Failed Jobs page does not need to see —
    bucket names, login user, USB mount paths. Originals stay in the
    DB for journalctl-side triage. Also caps length so a runaway
    rclone stack trace can't blow up the JSON payload.
    """
    if not msg:
        return ''
    s = str(msg)
    for pat, repl in _REDACT_PATTERNS:
        s = pat.sub(repl, s)
    if len(s) > _REDACT_MAX_LEN:
        s = s[:_REDACT_MAX_LEN].rstrip() + ' …'
    return s


def _safe(fn, default):
    """Call ``fn()``, returning ``default`` on any exception.

    The Failed Jobs page MUST render even when one subsystem's DB is
    missing or its config disabled — surfacing the other subsystems is
    more useful than a 500 page. Logs the exception so operators see
    the underlying issue in journalctl.
    """
    try:
        return fn()
    except Exception:  # noqa: BLE001
        logger.exception("/api/jobs sub-call crashed")
        return default


# ---------------------------------------------------------------------------
# Subsystem-specific list adapters
# ---------------------------------------------------------------------------

def _archive_rows(limit: int) -> List[Dict[str, Any]]:
    from services import archive_queue
    rows = archive_queue.list_dead_letters(limit=limit)
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            'subsystem': 'archive',
            'id': r.get('id'),
            'identifier': r.get('source_path') or r.get('archive_path') or '',
            'attempts': int(r.get('attempts') or 0),
            'last_error': _redact_last_error(r.get('last_error')),
            'enqueued_at': r.get('enqueued_at'),
            'extra': {
                'priority': r.get('priority'),
                'expected_size': r.get('expected_size'),
            },
        })
    return out


def _indexer_rows(limit: int) -> List[Dict[str, Any]]:
    if not MAPPING_ENABLED:
        return []
    from services import indexing_queue_service
    rows = indexing_queue_service.list_dead_letters(MAPPING_DB_PATH, limit=limit)
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            'subsystem': 'indexer',
            'id': r.get('canonical_key'),  # natural key in this table
            'identifier': r.get('file_path') or r.get('canonical_key') or '',
            'attempts': int(r.get('attempts') or 0),
            'last_error': _redact_last_error(r.get('last_error')),
            'enqueued_at': r.get('enqueued_at'),
            'extra': {
                'next_attempt_at': r.get('next_attempt_at'),
                'source': r.get('source'),
            },
        })
    return out


def _cloud_sync_rows(limit: int) -> List[Dict[str, Any]]:
    if not CLOUD_ARCHIVE_ENABLED:
        return []
    from services import cloud_archive_service
    rows = cloud_archive_service.list_dead_letters(limit=limit)
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            'subsystem': 'cloud_sync',
            'id': r.get('file_path'),  # natural key (UNIQUE)
            'identifier': r.get('file_path') or '',
            'attempts': int(r.get('retry_count') or 0),
            'last_error': _redact_last_error(r.get('last_error')),
            'enqueued_at': None,
            'extra': {
                'file_size': r.get('file_size'),
                'row_id': r.get('id'),
            },
        })
    return out


def _live_event_rows(limit: int) -> List[Dict[str, Any]]:
    if not LIVE_EVENT_SYNC_ENABLED:
        return []
    from services import live_event_sync_service
    # LES list_queue returns ALL recent rows; filter to failed only.
    raw = live_event_sync_service.list_queue(limit=max(limit * 4, 50))
    out: List[Dict[str, Any]] = []
    for r in raw:
        if r.get('status') != 'failed':
            continue
        if len(out) >= limit:
            break
        out.append({
            'subsystem': 'live_event_sync',
            'id': r.get('id'),
            'identifier': r.get('event_dir') or '',
            'attempts': int(r.get('attempts') or 0),
            'last_error': _redact_last_error(r.get('last_error')),
            'enqueued_at': r.get('enqueued_at'),
            'extra': {
                'event_timestamp': r.get('event_timestamp'),
                'event_reason': r.get('event_reason'),
                'upload_scope': r.get('upload_scope'),
                'bytes_uploaded': r.get('bytes_uploaded'),
            },
        })
    return out


_LISTERS = {
    'archive': _archive_rows,
    'indexer': _indexer_rows,
    'cloud_sync': _cloud_sync_rows,
    'live_event_sync': _live_event_rows,
}


# ---------------------------------------------------------------------------
# Subsystem-specific count adapters (cheap COUNT(*), used by /counts)
# ---------------------------------------------------------------------------

def _archive_count() -> int:
    from services import archive_queue
    return int(archive_queue.count_dead_letters())


def _indexer_count() -> int:
    if not MAPPING_ENABLED:
        return 0
    from services import indexing_queue_service
    return int(indexing_queue_service.count_dead_letters(MAPPING_DB_PATH))


def _cloud_sync_count() -> int:
    if not CLOUD_ARCHIVE_ENABLED:
        return 0
    from services import cloud_archive_service
    return int(cloud_archive_service.count_dead_letters())


def _live_event_count() -> int:
    if not LIVE_EVENT_SYNC_ENABLED:
        return 0
    from services import live_event_sync_service
    return int(live_event_sync_service.count_failed())


_COUNTERS = {
    'archive': _archive_count,
    'indexer': _indexer_count,
    'cloud_sync': _cloud_sync_count,
    'live_event_sync': _live_event_count,
}


# ---------------------------------------------------------------------------
# Subsystem-specific retry adapters
# ---------------------------------------------------------------------------

def _retry_archive(row_id: Optional[Any]) -> int:
    from services import archive_queue
    if row_id is None:
        return archive_queue.retry_dead_letter(row_id=None)
    try:
        rid = int(row_id)
    except (TypeError, ValueError):
        return 0
    return archive_queue.retry_dead_letter(row_id=rid)


def _retry_indexer(row_id: Optional[Any]) -> int:
    if not MAPPING_ENABLED:
        return 0
    from services import indexing_queue_service
    key = None if row_id is None else str(row_id)
    return indexing_queue_service.retry_dead_letter(MAPPING_DB_PATH,
                                                    canonical_key_value=key)


def _retry_cloud_sync(row_id: Optional[Any]) -> int:
    if not CLOUD_ARCHIVE_ENABLED:
        return 0
    from services import cloud_archive_service
    path = None if row_id is None else str(row_id)
    return cloud_archive_service.retry_dead_letter(file_path=path)


def _retry_live_event_sync(row_id: Optional[Any]) -> int:
    if not LIVE_EVENT_SYNC_ENABLED:
        return 0
    from services import live_event_sync_service
    if row_id is None:
        return live_event_sync_service.retry_failed(None)
    try:
        rid = int(row_id)
    except (TypeError, ValueError):
        return 0
    return live_event_sync_service.retry_failed(rid)


_RETRIERS = {
    'archive': _retry_archive,
    'indexer': _retry_indexer,
    'cloud_sync': _retry_cloud_sync,
    'live_event_sync': _retry_live_event_sync,
}


# ---------------------------------------------------------------------------
# HTML route
# ---------------------------------------------------------------------------

@jobs_bp.route('/jobs', methods=['GET'])
def failed_jobs_page():
    """Render the unified Failed Jobs page shell.

    The page polls ``/api/jobs/counts`` and ``/api/jobs/failed`` after
    load — no server-side data fetch in this handler so the page
    renders fast even when one of the subsystem DBs is slow or
    unavailable.
    """
    return render_template(
        'failed_jobs.html',
        page='settings',
        subsystems=list(_SUBSYSTEMS),
    )


# ---------------------------------------------------------------------------
# JSON routes
# ---------------------------------------------------------------------------

def _parse_limit(default: int = 100, hard_max: int = 1000) -> int:
    try:
        n = int(request.args.get('limit', default))
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, hard_max))


@jobs_bp.route('/api/jobs/counts', methods=['GET'])
def api_counts():
    """Return failed-job counts per subsystem plus a total.

    Cheap by design — every subsystem call goes through a dedicated
    ``count_*`` helper that runs a single ``SELECT COUNT(*)`` over the
    indexed status column, never a full row fetch. This MUST stay fast
    because Phase 4.8 will reuse this endpoint as the status-dot
    poller (every few seconds).
    """
    counts = {
        name: int(_safe(_COUNTERS[name], 0))
        for name in _SUBSYSTEMS
    }
    counts['total'] = sum(counts.values())
    return jsonify(counts)


@jobs_bp.route('/api/jobs/failed', methods=['GET'])
def api_failed():
    """Return failed/dead-letter rows for one subsystem, or all of them.

    Query params:
      * ``subsystem`` — one of ``archive``, ``indexer``, ``cloud_sync``,
        ``live_event_sync``, or omitted/``all`` for the union.
      * ``limit`` — per-subsystem cap (default 100, max 1000).
    """
    subsystem = (request.args.get('subsystem') or 'all').lower()
    limit = _parse_limit(default=100, hard_max=1000)

    if subsystem != 'all' and subsystem not in _LISTERS:
        return jsonify({
            'error': 'unknown subsystem',
            'allowed': list(_SUBSYSTEMS) + ['all'],
        }), 400

    if subsystem == 'all':
        rows: List[Dict[str, Any]] = []
        for name in _SUBSYSTEMS:
            rows.extend(_safe(lambda n=name: _LISTERS[n](limit), []))
    else:
        rows = _safe(lambda: _LISTERS[subsystem](limit), [])

    return jsonify({
        'subsystem': subsystem,
        'count': len(rows),
        'rows': rows,
    })


@jobs_bp.route('/api/jobs/retry', methods=['POST'])
def api_retry():
    """Reset failed/dead-letter rows so the worker picks them up again.

    Request body (JSON):
      * ``subsystem`` (required) — one of the four subsystem names.
      * ``id`` (optional) — omit / pass ``null`` to retry **every**
        failed row in that subsystem; otherwise the natural id for
        that subsystem (int row id for archive / live_event_sync,
        canonical_key string for indexer, file_path string for
        cloud_sync).

    Returns ``{subsystem, rows_reset}`` (HTTP 200) on success, or
    ``{error}`` (HTTP 400) on bad input.
    """
    payload = request.get_json(silent=True) or {}
    subsystem = (payload.get('subsystem') or '').lower()
    if subsystem not in _RETRIERS:
        return jsonify({
            'error': 'unknown or missing subsystem',
            'allowed': list(_SUBSYSTEMS),
        }), 400

    row_id = payload.get('id')
    try:
        n = _RETRIERS[subsystem](row_id)
    except Exception:  # noqa: BLE001
        logger.exception("/api/jobs/retry crashed (subsystem=%s, id=%r)",
                         subsystem, row_id)
        return jsonify({'error': 'retry failed', 'subsystem': subsystem}), 500

    return jsonify({'subsystem': subsystem, 'rows_reset': int(n)})
