"""Blueprint for the Phase 2a archive_queue observability stub (issue #76).

Exposes a single read-only JSON endpoint:

* ``GET /api/archive_queue/status`` — counts per status plus producer
  thread state.

This is the minimum surface needed to verify Phase 2a is enqueueing
correctly during the deployment window. The full per-priority counts,
dead-letter inspection, and retry endpoints land in Phase 2c.

Image-gated on ``IMG_CAM_PATH`` (the queued clips live on the cam
image / part1). Returns 503 JSON when the cam image is missing so
URL routing stays stable on installs without a TeslaCam drive.
"""

from __future__ import annotations

import logging
import os

from flask import Blueprint, jsonify

from config import ARCHIVE_QUEUE_ENABLED, IMG_CAM_PATH

logger = logging.getLogger(__name__)

archive_queue_bp = Blueprint(
    'archive_queue', __name__, url_prefix='/api/archive_queue',
)


@archive_queue_bp.before_request
def _require_cam_image():
    if not os.path.isfile(IMG_CAM_PATH):
        return jsonify({"error": "Feature unavailable"}), 503


@archive_queue_bp.route('/status', methods=['GET'])
def status():
    """Return queue counts plus producer state.

    When ``archive_queue.enabled`` is False in ``config.yaml`` the
    queue counts are still returned (rows from a previous enabled
    install are still visible), but ``producer.running`` will be False
    because :mod:`web_control` skipped the producer thread startup.
    """
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
