"""Blueprint for Live Event Sync (LES) status and retry endpoints.

LES is the real-time per-event uploader (separate from the bulk
``cloud_archive`` sync). This blueprint exposes a thin API so the web
UI and on-device tooling can:

* Read queue + last-upload status (``GET /api/live_events/status``)
* List recent queue rows (``GET /api/live_events/queue``)
* Retry a failed row (``POST /api/live_events/retry/<id>``)
* Wake the worker (``POST /api/live_events/wake``) — called by the
  NetworkManager WiFi-connect dispatcher path so LES drains BEFORE
  cloud_archive sync runs.

All endpoints are JSON-only (no HTML). The blueprint is image-gated on
``IMG_CAM_PATH`` (Sentry/Saved events live on part1 — TeslaCam). When
the image isn't present the routes return 503 JSON; when LES is
disabled in config they return ``{"enabled": false}`` so the UI can
gracefully render a disabled state instead of a hard error.
"""

import logging
import os

from flask import Blueprint, jsonify, request

from config import IMG_CAM_PATH, LIVE_EVENT_SYNC_ENABLED

logger = logging.getLogger(__name__)

live_events_bp = Blueprint('live_events', __name__,
                           url_prefix='/api/live_events')


@live_events_bp.before_request
def _require_cam_image():
    """Block all routes when usb_cam.img is missing (no Sentry/Saved data)."""
    if not os.path.isfile(IMG_CAM_PATH):
        return jsonify({"error": "Feature unavailable"}), 503


@live_events_bp.route('/status', methods=['GET'])
def status():
    """Return a JSON snapshot of LES state plus queue counts."""
    if not LIVE_EVENT_SYNC_ENABLED:
        return jsonify({"enabled": False})
    from services.live_event_sync_service import get_status
    return jsonify({"enabled": True, **get_status()})


@live_events_bp.route('/queue', methods=['GET'])
def queue():
    """Return up to ``limit`` recent queue rows (default 50)."""
    if not LIVE_EVENT_SYNC_ENABLED:
        return jsonify({"enabled": False, "rows": []})
    from services.live_event_sync_service import list_queue
    try:
        limit = max(1, min(200, int(request.args.get('limit', 50))))
    except (ValueError, TypeError):
        limit = 50
    return jsonify({"enabled": True, "rows": list_queue(limit)})


@live_events_bp.route('/retry/<int:row_id>', methods=['POST'])
def retry(row_id: int):
    """Reset a single failed row to pending and wake the worker."""
    if not LIVE_EVENT_SYNC_ENABLED:
        return jsonify({"enabled": False, "error": "LES disabled"}), 400
    from services.live_event_sync_service import retry_failed
    n = retry_failed(row_id)
    return jsonify({"enabled": True, "rows_reset": n})


@live_events_bp.route('/retry_all', methods=['POST'])
def retry_all():
    """Reset every failed row to pending."""
    if not LIVE_EVENT_SYNC_ENABLED:
        return jsonify({"enabled": False, "error": "LES disabled"}), 400
    from services.live_event_sync_service import retry_failed
    n = retry_failed(None)
    return jsonify({"enabled": True, "rows_reset": n})


@live_events_bp.route('/wake', methods=['POST'])
def wake():
    """Poke the worker thread (used by the WiFi-connect dispatcher)."""
    if not LIVE_EVENT_SYNC_ENABLED:
        return jsonify({"enabled": False})
    from services.live_event_sync_service import wake as _wake
    _wake()
    return jsonify({"enabled": True, "woken": True})
