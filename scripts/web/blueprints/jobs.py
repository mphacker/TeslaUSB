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

Each row also carries ``previous_last_error`` (issue #132): the
prior failure reason from before the most recent retry. The four
worker subsystems all rotate ``last_error → previous_last_error``
each time they record a new failure, so the operator can see whether
the same error keeps recurring or whether retries are uncovering new
ones. Same redaction pipeline applies.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, render_template, request

from config import (
    CLOUD_ARCHIVE_ENABLED,
    LIVE_EVENT_SYNC_ENABLED,
    MAPPING_DB_PATH,
    MAPPING_ENABLED,
)
from utils import get_base_context

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
# Per-row guidance (issue #180)
# ---------------------------------------------------------------------------
#
# These helpers add two fields to each failed-job row that the UI uses
# to show the operator what they're looking at and what to do about
# it:
#
#   ``value`` — a deterministic "what is this clip / row" classifier
#               based on the identifier path. Tells the operator at a
#               glance whether the underlying data is unique-and-
#               valuable (an event clip Tesla recorded because
#               something happened) or commodity (60-min driving
#               footage that Tesla rotates out anyway, or a
#               re-derivable index row).
#
#   ``recommendation`` — a deterministic action recommendation based
#               on the redacted ``last_error`` string. The operator
#               always has the final say (both Retry and Delete
#               buttons are always rendered); this just steers them
#               toward the right one for the most common error
#               patterns and makes "I have no idea what to do" failure
#               modes much rarer.
#
# Both helpers are pure / deterministic — no DB access, no side
# effects — so they're cheap to call on every list row and trivial to
# unit-test. Update the heuristics below to add new categories; the
# UI consumes the resulting dicts opaquely.

# Clip-value tiers ordered most-irreplaceable first. The UI renders
# the badge color from the tier name (event=red, recent=amber,
# index/cloud/live_event=blue, archived=neutral).
_VALUE_TIERS: Dict[str, Tuple[str, str]] = {
    'event': (
        'Event clip',
        'Tesla recorded this because something happened to the car '
        '(impact, alarm, or manual save). Usually irreplaceable.',
    ),
    'recent': (
        'Rolling buffer',
        'RecentClips — Tesla writes these continuously while the car '
        'is powered (driving OR parked in Sentry standby) and rotates '
        'them out automatically. Losing one row is usually fine.',
    ),
    'archived': (
        'Already on SD card',
        'This clip is in ArchivedClips, so the source file is already '
        'preserved on the Pi even if the queue row is dropped.',
    ),
    'live_event': (
        'Live event upload',
        'Same value as an event clip — Tesla recorded this because '
        'something happened. The Live Event Sync subsystem only ever '
        'handles events.',
    ),
    'cloud': (
        'Cloud upload',
        'The file itself is still on the SD card; only the cloud '
        'upload failed. Re-uploading later is always safe.',
    ),
    'index': (
        'Map / trip data',
        'Just the trip-DB index row for this clip. The video file is '
        'untouched; deleting the row only loses the map waypoint, '
        'not the footage.',
    ),
    'unknown': (
        'Background task',
        'The subsystem did not provide enough information to classify '
        'the underlying data.',
    ),
}


def _classify_clip_value(subsystem: str, identifier: str) -> Dict[str, str]:
    """Return ``{tier, label, description}`` for a failed-job row.

    Pure / deterministic — looks only at the subsystem name and the
    identifier string. Suitable for the row-list response and for
    unit tests. Never raises: an unrecognised input falls through to
    the ``unknown`` tier so the UI still renders something.
    """
    ident = (identifier or '').lower()

    # LES is always an event upload — its queue rows are populated by
    # the inotify watcher firing on Tesla event.json arrivals only.
    if subsystem == 'live_event_sync':
        tier = 'live_event'
    # SentryClips / SavedClips identifiers are event clips regardless
    # of which subsystem holds the row (archive, indexer, cloud_sync).
    elif '/sentryclips/' in ident or '/savedclips/' in ident:
        tier = 'event'
    elif '/recentclips/' in ident:
        tier = 'recent'
    elif '/archivedclips/' in ident:
        tier = 'archived'
    elif subsystem == 'indexer':
        tier = 'index'
    elif subsystem == 'cloud_sync':
        tier = 'cloud'
    else:
        tier = 'unknown'

    label, description = _VALUE_TIERS[tier]
    return {'tier': tier, 'label': label, 'description': description}


# Recommendation actions:
#   'retry'   — the underlying file is fine; the failure is transient
#               or fixable (network, permission, lock contention).
#   'delete'  — the underlying file is gone or unparseable; further
#               retries will hit the same error. Safe to drop.
#   'either'  — the row could go either way; default fallback.
#
# Order of pattern checks matters: more specific patterns first.
# Each entry is (regex, action, reason). The first match wins.
_RECOMMENDATION_RULES: Tuple[Tuple[re.Pattern, str, str], ...] = (
    # Source file is gone — Tesla rotated the clip, the operator
    # deleted it, or a quick-edit pass removed it. Retrying will hit
    # the same FileNotFoundError every time.
    (re.compile(r'(?i)\b(?:no such file|file (?:not found|missing)|'
                r'enoent|source[_ ]?gone|does not exist)\b'),
     'delete',
     'The source file is gone. Retrying will fail the same way.'),

    # Permanent parse / format errors — the file is on disk but the
    # parser can't make sense of it. Retrying won't change the
    # bitstream.
    (re.compile(r'(?i)\b(?:moov atom|invalid (?:data|argument)|'
                r'corrupt|truncat\w*|parse[_ ]?error|unsupported '
                r'(?:codec|format)|not a valid )\b'),
     'delete',
     'The file is on disk but cannot be parsed. The clip itself is '
     'corrupt — retrying will hit the same error.'),

    # Transient I/O failures — bus contention, USB hotplug glitch,
    # SDIO timeout. The next archive cycle will probably succeed.
    (re.compile(r'(?i)\b(?:i/?o ?error|input/output error|stale file '
                r'handle|device or resource busy|read[_ ]?error|'
                r'write[_ ]?error)\b'),
     'retry',
     'Transient I/O glitch. Often fixed by waiting for the SDIO bus '
     'or USB gadget to settle, then retrying.'),

    # Network / cloud upload failures — wait for WiFi to recover and
    # retry. Almost never the right call to delete these.
    (re.compile(r'(?i)\b(?:connection (?:refused|reset|timed? ?out|'
                r'aborted)|network (?:is )?(?:unreachable|down)|'
                r'temporary (?:failure|name resolution)|'
                r'no route to host|enotconn|name or service|'
                r'tls handshake|ssl handshake|x509|getaddrinfo|'
                r'dial tcp)\b'),
     'retry',
     'Network failure. Retry once WiFi is healthy. The file itself '
     'is fine.'),

    # Cloud auth / quota / config — operator must fix the root cause
    # (rotate creds, free quota) before retrying. Don't delete.
    (re.compile(r'(?i)\b(?:401|403|access denied|forbidden|invalid '
                r'(?:credential|token|api[_ ]?key|signature)|quota '
                r'exceeded|out of space|over capacity|insufficient '
                r'storage|payment required|429|rate ?limit)\b'),
     'retry',
     'Cloud-side auth / quota / rate-limit error. Fix the root cause '
     '(rotate creds, free space, wait for quota window) then retry.'),

    # Permission denied on the local filesystem — usually a stale
    # mount or wrong owner; retry after fixing.
    (re.compile(r'(?i)\b(?:permission denied|operation not permitted|'
                r'eacces|read[-_ ]?only file ?system|erofs)\b'),
     'retry',
     'Permission / read-only-mount error. Check the disk image is '
     'in edit mode (or the file is unlocked) then retry.'),

    # Lock contention against the gadget / quick-edit / coordinator —
    # always transient.
    (re.compile(r'(?i)\b(?:lock (?:contention|timeout|busy)|could not '
                r'acquire|coordinator (?:busy|timeout))\b'),
     'retry',
     'Another subsystem held the lock during the previous attempt. '
     'Retrying once the lock is free almost always succeeds.'),
)


def _classify_recommendation(subsystem: str,
                             last_error: Optional[str],
                             attempts: int = 0,
                             identifier: Optional[str] = None
                             ) -> Dict[str, Any]:
    """Return ``{action, reason}`` advising Retry vs Delete.

    Pure / deterministic — looks only at the redacted ``last_error``
    string and (for the high-attempt fallback) the attempt count.
    The operator can always override; this just nudges the most
    common cases toward the obvious correct button.

    ``action`` is one of ``'retry'``, ``'delete'``, or ``'either'``.
    ``reason`` is a short human-readable sentence (≤ 120 chars).
    ``attempts`` is informational — used only for the
    "many-attempts-with-no-known-pattern" fallback so the operator
    isn't told to retry forever on a row that's already retried 5
    times against an unrecognised error.
    """
    err = (last_error or '').strip()
    if not err:
        return {
            'action': 'either',
            'reason': ('No error message recorded. Retry once to see '
                       'a fresh failure, or delete if the source is '
                       'no longer needed.'),
        }

    for pat, action, reason in _RECOMMENDATION_RULES:
        if pat.search(err):
            return {'action': action, 'reason': reason}

    # Unknown error pattern. If the row has retried many times without
    # hitting a known pattern, the underlying issue is probably stuck
    # — recommend delete so the operator doesn't hammer the worker.
    if attempts >= 5:
        return {
            'action': 'delete',
            'reason': ('This row has retried {0} times without '
                       'matching any known recoverable pattern. '
                       'Likely stuck — delete and let the watcher '
                       're-enqueue if the source is still valid.'
                       .format(attempts)),
        }

    return {
        'action': 'either',
        'reason': ('Error pattern not recognised. Retry once; if the '
                   'same error returns, delete is safe (the source '
                   'file stays on disk).'),
    }


# ---------------------------------------------------------------------------
# Subsystem-specific list adapters
# ---------------------------------------------------------------------------

def _enrich(row: Dict[str, Any]) -> Dict[str, Any]:
    """Add ``value`` and ``recommendation`` to a base row dict.

    Mutates and returns ``row`` for convenient inline use inside the
    list-comprehension-style adapters below.
    """
    row['value'] = _classify_clip_value(row.get('subsystem', ''),
                                        row.get('identifier') or '')
    row['recommendation'] = _classify_recommendation(
        row.get('subsystem', ''),
        row.get('last_error'),
        attempts=int(row.get('attempts') or 0),
        identifier=row.get('identifier') or '',
    )
    return row


def _archive_rows(limit: int) -> List[Dict[str, Any]]:
    from services import archive_queue
    rows = archive_queue.list_dead_letters(limit=limit)
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(_enrich({
            'subsystem': 'archive',
            'id': r.get('id'),
            'identifier': r.get('source_path') or r.get('archive_path') or '',
            'attempts': int(r.get('attempts') or 0),
            'last_error': _redact_last_error(r.get('last_error')),
            'previous_last_error': _redact_last_error(r.get('previous_last_error')),
            'enqueued_at': r.get('enqueued_at'),
            'extra': {
                'priority': r.get('priority'),
                'expected_size': r.get('expected_size'),
            },
        }))
    return out


def _indexer_rows(limit: int) -> List[Dict[str, Any]]:
    if not MAPPING_ENABLED:
        return []
    from services import indexing_queue_service
    rows = indexing_queue_service.list_dead_letters(MAPPING_DB_PATH, limit=limit)
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(_enrich({
            'subsystem': 'indexer',
            'id': r.get('canonical_key'),  # natural key in this table
            'identifier': r.get('file_path') or r.get('canonical_key') or '',
            'attempts': int(r.get('attempts') or 0),
            'last_error': _redact_last_error(r.get('last_error')),
            'previous_last_error': _redact_last_error(r.get('previous_last_error')),
            'enqueued_at': r.get('enqueued_at'),
            'extra': {
                'next_attempt_at': r.get('next_attempt_at'),
                'source': r.get('source'),
            },
        }))
    return out


def _cloud_sync_rows(limit: int) -> List[Dict[str, Any]]:
    if not CLOUD_ARCHIVE_ENABLED:
        return []
    from services import cloud_archive_service
    rows = cloud_archive_service.list_dead_letters(limit=limit)
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(_enrich({
            'subsystem': 'cloud_sync',
            'id': r.get('file_path'),  # natural key (UNIQUE)
            'identifier': r.get('file_path') or '',
            'attempts': int(r.get('retry_count') or 0),
            'last_error': _redact_last_error(r.get('last_error')),
            'previous_last_error': _redact_last_error(r.get('previous_last_error')),
            'enqueued_at': None,
            'extra': {
                'file_size': r.get('file_size'),
                'row_id': r.get('id'),
            },
        }))
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
        out.append(_enrich({
            'subsystem': 'live_event_sync',
            'id': r.get('id'),
            'identifier': r.get('event_dir') or '',
            'attempts': int(r.get('attempts') or 0),
            'last_error': _redact_last_error(r.get('last_error')),
            'previous_last_error': _redact_last_error(r.get('previous_last_error')),
            'enqueued_at': r.get('enqueued_at'),
            'extra': {
                'event_timestamp': r.get('event_timestamp'),
                'event_reason': r.get('event_reason'),
                'upload_scope': r.get('upload_scope'),
                'bytes_uploaded': r.get('bytes_uploaded'),
            },
        }))
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
# Subsystem-specific delete adapters (mirror the retry adapters above)
# ---------------------------------------------------------------------------
#
# Each adapter delegates to the matching service-layer
# ``delete_dead_letter`` / ``delete_failed`` function. Same id-typing
# rules as the retry adapters so the route-level handler is symmetric.

def _delete_archive(row_id: Optional[Any]) -> int:
    from services import archive_queue
    if row_id is None:
        return archive_queue.delete_dead_letter(row_id=None)
    try:
        rid = int(row_id)
    except (TypeError, ValueError):
        return 0
    return archive_queue.delete_dead_letter(row_id=rid)


def _delete_indexer(row_id: Optional[Any]) -> int:
    if not MAPPING_ENABLED:
        return 0
    from services import indexing_queue_service
    key = None if row_id is None else str(row_id)
    return indexing_queue_service.delete_dead_letter(MAPPING_DB_PATH,
                                                     canonical_key_value=key)


def _delete_cloud_sync(row_id: Optional[Any]) -> int:
    if not CLOUD_ARCHIVE_ENABLED:
        return 0
    from services import cloud_archive_service
    path = None if row_id is None else str(row_id)
    return cloud_archive_service.delete_dead_letter(file_path=path)


def _delete_live_event_sync(row_id: Optional[Any]) -> int:
    if not LIVE_EVENT_SYNC_ENABLED:
        return 0
    from services import live_event_sync_service
    if row_id is None:
        return live_event_sync_service.delete_failed(None)
    try:
        rid = int(row_id)
    except (TypeError, ValueError):
        return 0
    return live_event_sync_service.delete_failed(rid)


_DELETERS = {
    'archive': _delete_archive,
    'indexer': _delete_indexer,
    'cloud_sync': _delete_cloud_sync,
    'live_event_sync': _delete_live_event_sync,
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

    Issue #180 — merge ``get_base_context()`` so the left sidebar /
    mobile bottom-tab nav shows every available top-level page (Map,
    Analytics, Media, Cloud, Settings) instead of collapsing to just
    Settings. ``page='jobs'`` is intentional: it's not one of the
    canonical nav targets so no nav item gets the ``active`` class
    while the operator is on the Failed Jobs page.
    """
    ctx = get_base_context()
    ctx['page'] = 'jobs'
    ctx['subsystems'] = list(_SUBSYSTEMS)
    return render_template('failed_jobs.html', **ctx)


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
      * ``subsystem`` (required) — one of the four subsystem names, OR
        the literal string ``'all'`` (issue #180) to fan the retry-
        all out across every subsystem at once.
      * ``id`` (optional) — omit / pass ``null`` to retry **every**
        failed row in that subsystem; otherwise the natural id for
        that subsystem (int row id for archive / live_event_sync,
        canonical_key string for indexer, file_path string for
        cloud_sync). Ignored when ``subsystem='all'``.

    Returns ``{subsystem, rows_reset}`` (HTTP 200) on success, or
    ``{error}`` (HTTP 400) on bad input. When ``subsystem='all'`` the
    response also carries ``per_subsystem`` so the UI can show what
    happened in each.
    """
    payload = request.get_json(silent=True) or {}
    subsystem = (payload.get('subsystem') or '').lower()

    if subsystem == 'all':
        # Fan-out across every subsystem. Each adapter is wrapped in
        # ``_safe`` so one crashing subsystem can't deny the operator
        # the retry-all action across the others.
        per: Dict[str, int] = {}
        total = 0
        for name in _SUBSYSTEMS:
            n = int(_safe(lambda fn=_RETRIERS[name]: fn(None), 0))
            per[name] = n
            total += n
        return jsonify({
            'subsystem': 'all',
            'rows_reset': total,
            'per_subsystem': per,
        })

    if subsystem not in _RETRIERS:
        return jsonify({
            'error': 'unknown or missing subsystem',
            'allowed': list(_SUBSYSTEMS) + ['all'],
        }), 400

    row_id = payload.get('id')
    try:
        n = _RETRIERS[subsystem](row_id)
    except Exception:  # noqa: BLE001
        logger.exception("/api/jobs/retry crashed (subsystem=%s, id=%r)",
                         subsystem, row_id)
        return jsonify({'error': 'retry failed', 'subsystem': subsystem}), 500

    return jsonify({'subsystem': subsystem, 'rows_reset': int(n)})


@jobs_bp.route('/api/jobs/delete', methods=['POST'])
def api_delete():
    """Permanently delete failed/dead-letter rows.

    Request body (JSON):
      * ``subsystem`` (required) — one of the four subsystem names, OR
        the literal string ``'all'`` (issue #180) to fan the delete-
        all out across every subsystem at once.
      * ``id`` (optional) — omit / pass ``null`` to delete **every**
        failed row in that subsystem; otherwise the natural id for
        that subsystem (int row id for archive / live_event_sync,
        canonical_key string for indexer, file_path string for
        cloud_sync). Ignored when ``subsystem='all'``.

    Mirrors :func:`api_retry` exactly, but routes through ``_DELETERS``
    instead of ``_RETRIERS``. Returns ``{subsystem, rows_deleted}``
    (HTTP 200) on success, ``{error}`` (HTTP 400) on bad input,
    ``{error, subsystem}`` (HTTP 500) on adapter crash. When
    ``subsystem='all'`` the response also carries ``per_subsystem``.

    The delete only affects the named subsystem's queue table — it
    does NOT delete the underlying source file from disk, and it does
    NOT touch any other table (e.g. indexer delete preserves
    ``indexed_files`` / trips / waypoints / detected_events for the
    same source). Producers (inotify watcher, boot catch-up scan,
    archive worker) may legitimately re-enqueue the same source path
    later if it still exists; that's the producer doing its job and
    the new row starts fresh with ``attempts=0``.
    """
    payload = request.get_json(silent=True) or {}
    subsystem = (payload.get('subsystem') or '').lower()

    if subsystem == 'all':
        # Same fan-out semantics as retry (issue #180). Each
        # subsystem's deleter is wrapped in ``_safe`` so a single
        # crashing subsystem doesn't deny the operator the bulk
        # delete across the others.
        per: Dict[str, int] = {}
        total = 0
        for name in _SUBSYSTEMS:
            n = int(_safe(lambda fn=_DELETERS[name]: fn(None), 0))
            per[name] = n
            total += n
        return jsonify({
            'subsystem': 'all',
            'rows_deleted': total,
            'per_subsystem': per,
        })

    if subsystem not in _DELETERS:
        return jsonify({
            'error': 'unknown or missing subsystem',
            'allowed': list(_SUBSYSTEMS) + ['all'],
        }), 400

    row_id = payload.get('id')
    try:
        n = _DELETERS[subsystem](row_id)
    except Exception:  # noqa: BLE001
        logger.exception("/api/jobs/delete crashed (subsystem=%s, id=%r)",
                         subsystem, row_id)
        return jsonify({'error': 'delete failed', 'subsystem': subsystem}), 500

    return jsonify({'subsystem': subsystem, 'rows_deleted': int(n)})
