"""Blueprint for the archive observability + storage endpoints (issue #76).

Exposes the read-only JSON endpoints that power the Settings → Archive
panel, the Storage panel, and the persistent archive-health banner:

* ``GET /api/archive/status`` — combined health + queue + worker +
  disk + retention snapshot. The single source of truth for the UI.
* ``GET /api/archive/dead_letters`` — list dead-letter rows for the
  "View dead letters" modal.
* ``POST /api/archive/prune_now`` — synchronous retention prune.
* ``GET /api/archive_queue/status`` — Phase 2a observability stub kept
  as a thin alias so deploy-time scripts and external probes don't
  need a coordinated cutover.

Image-gated on ``IMG_CAM_PATH`` (the queued clips live on the cam
image / part1). Returns 503 JSON when the cam image is missing so
URL routing stays stable on installs without a TeslaCam drive.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from flask import Blueprint, jsonify, request

from config import ARCHIVE_QUEUE_ENABLED, IMG_CAM_PATH

logger = logging.getLogger(__name__)

archive_queue_bp = Blueprint(
    'archive_queue', __name__,
)


@archive_queue_bp.before_request
def _require_cam_image():
    if not os.path.isfile(IMG_CAM_PATH):
        return jsonify({"error": "Feature unavailable"}), 503


# ---------------------------------------------------------------------------
# Phase 2a alias (kept intentionally — small, no-cost back-compat)
# ---------------------------------------------------------------------------

@archive_queue_bp.route('/api/archive_queue/status', methods=['GET'])
def status():
    """Return queue counts plus producer state (Phase 2a alias)."""
    from services import archive_queue, archive_producer

    try:
        counts = archive_queue.get_queue_status()
    except Exception:
        logger.exception("archive_queue.get_queue_status crashed")
        counts = {}

    try:
        producer = archive_producer.get_producer_status()
    except Exception:
        logger.exception("archive_producer.get_producer_status crashed")
        producer = {}

    return jsonify({
        "enabled": bool(ARCHIVE_QUEUE_ENABLED),
        "counts": counts,
        "producer": producer,
    })


# ---------------------------------------------------------------------------
# Phase 2c — combined status endpoint (single source of truth for the UI)
# ---------------------------------------------------------------------------

def _safe_call(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        logger.exception("/api/archive/status sub-call crashed")
        return default


@archive_queue_bp.route('/api/archive/status', methods=['GET'])
def archive_status():
    """Return the full archive subsystem snapshot.

    Combines:
      * watchdog health (severity, message, staleness)
      * worker liveness (running, paused, active_file, last_outcome)
      * queue depths per priority + per status
      * SD-card disk usage
      * retention bookkeeping (next prune due, last prune summary)

    Designed so the Settings page can render every panel from a single
    poll. JSON only; no template rendering. ~1 KB payload.
    """
    from services import archive_queue, archive_watchdog, archive_worker

    health = _safe_call(archive_watchdog.get_status, {})
    worker_status = _safe_call(archive_worker.get_status, {})
    counts = _safe_call(archive_queue.get_queue_status, {})
    by_priority = _safe_call(
        archive_queue.get_pending_counts_by_priority, {1: 0, 2: 0, 3: 0},
    )

    response: Dict[str, Any] = {
        # Top-level health (severity, message, banner trigger).
        'severity': health.get('severity', 'ok'),
        'message': health.get('message', ''),
        'enabled': bool(ARCHIVE_QUEUE_ENABLED),
        'checked_at': health.get('checked_at'),
        'watchdog_running': health.get('watchdog_running', False),

        # Per-priority pending counts (RecentClips first).
        'queue_depth_p1': int(by_priority.get(1, 0)),
        'queue_depth_p2': int(by_priority.get(2, 0)),
        'queue_depth_p3': int(by_priority.get(3, 0)),

        # Per-status counts (matches Phase 2a alias for convenience).
        'pending_count': int(counts.get('pending', 0)),
        'claimed_count': int(counts.get('claimed', 0)),
        'copied_count': int(counts.get('copied', 0)),
        'source_gone_count': int(counts.get('source_gone', 0)),
        'dead_letter_count': int(counts.get('dead_letter', 0)),

        # Worker liveness.
        'worker_running': bool(worker_status.get('worker_running', False)),
        'paused': bool(worker_status.get('paused', False)),
        'active_file': worker_status.get('active_file'),
        'last_outcome': worker_status.get('last_outcome'),
        'last_error': worker_status.get('last_error'),
        'files_done_session': int(
            worker_status.get('files_done_session', 0),
        ),
        'disk_pause': worker_status.get(
            'disk_pause', {'is_paused_now': False, 'paused_until_epoch': 0.0},
        ),

        # Disk + retention.
        'disk_total_mb': int(health.get('disk_total_mb', 0)),
        'disk_used_mb': int(health.get('disk_used_mb', 0)),
        'disk_free_mb': int(health.get('disk_free_mb', 0)),
        'disk_warning_mb': int(health.get('disk_warning_mb', 500)),
        'disk_critical_mb': int(health.get('disk_critical_mb', 100)),
        'last_successful_copy_at': health.get('last_successful_copy_at'),
        'last_successful_copy_age_seconds': health.get(
            'last_successful_copy_age_seconds',
        ),
    }

    retention = (health.get('retention') or {})
    response['retention_days'] = int(retention.get('retention_days', 30))
    response['last_prune_at'] = retention.get('last_prune_at')
    response['last_prune_deleted'] = int(retention.get(
        'last_prune_deleted', 0,
    ))
    response['last_prune_freed_bytes'] = int(retention.get(
        'last_prune_freed_bytes', 0,
    ))
    response['last_prune_error'] = retention.get('last_prune_error')
    response['next_prune_due_at'] = retention.get('next_prune_due_at')

    return jsonify(response)


# ---------------------------------------------------------------------------
# Phase 2c — dead-letter inspection
# ---------------------------------------------------------------------------

@archive_queue_bp.route('/api/archive/dead_letters', methods=['GET'])
def archive_dead_letters():
    """Return up to ``limit`` dead-letter rows for forensic inspection.

    Used by the Settings → Archive panel "View dead letters" modal.
    Each row already includes ``last_error`` so the user can see why
    that clip moved out of pending.
    """
    from services import archive_queue
    try:
        limit = int(request.args.get('limit', 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 500))
    rows = _safe_call(
        lambda: archive_queue.list_queue(
            limit=limit, status='dead_letter',
        ),
        [],
    )
    return jsonify({'rows': rows, 'count': len(rows)})


# ---------------------------------------------------------------------------
# Phase 2c — manual retention prune trigger
# ---------------------------------------------------------------------------

@archive_queue_bp.route('/api/archive/prune_now', methods=['POST'])
def archive_prune_now():
    """Run a retention prune synchronously and return the summary.

    Designed to be called from the Settings → Storage "Prune now"
    button. For typical ArchivedClips sizes (a few hundred files) the
    prune completes in well under a second; the user's spinner does
    not become an actual UX problem.

    Returns 503 if the watchdog hasn't been started (which would mean
    the archive subsystem is disabled in config).
    """
    from services import archive_watchdog
    if not archive_watchdog.is_running():
        return jsonify({
            'started': False,
            'message': 'Archive watchdog is not running.',
        }), 503
    try:
        summary = archive_watchdog.force_prune_now()
    except Exception as e:  # noqa: BLE001
        logger.exception("archive_prune_now failed")
        return jsonify({'started': False, 'message': str(e)}), 500
    return jsonify({
        'started': True,
        'deleted_count': int(summary.get('deleted_count', 0)),
        'freed_bytes': int(summary.get('freed_bytes', 0)),
        'scanned': int(summary.get('scanned', 0)),
        'duration_seconds': float(summary.get('duration_seconds', 0.0)),
        'cutoff_iso': summary.get('cutoff_iso'),
        'retention_days': int(summary.get(
            'retention_days', 30,
        )),
    })

