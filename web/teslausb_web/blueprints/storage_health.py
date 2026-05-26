"""Storage Health blueprint — JSON snapshot for the Settings page card.

Exposes a single read-only endpoint, ``GET /api/storage/health``,
that returns the latest :class:`StorageHealthSnapshot` as JSON. The
front-end polls this every 60 s while the page is visible.

There is intentionally **no** mutating endpoint. The B-1 architecture
puts Tesla writes onto POSIX files in an ext4 filesystem on the SD
card; the correct response to corruption is "replace the SD card +
restore from cloud archive", not click a Repair button. Surfacing
the alarm is enough — the operator decides what to do.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING

from flask import Blueprint, current_app, g, jsonify

from teslausb_web.services.storage_health_service import (
    SEV_UNKNOWN,
    StorageHealthService,
    StorageHealthSnapshot,
)

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

logger = logging.getLogger(__name__)

storage_health_bp = Blueprint("storage_health", __name__)


def _get_service() -> StorageHealthService:
    service = current_app.extensions.get("storage_health_service")
    if not isinstance(service, StorageHealthService):
        raise RuntimeError("storage_health_service extension is not configured")
    return service


def current_snapshot() -> StorageHealthSnapshot:
    """Return the snapshot for this request, computing it at most once.

    Cached on ``flask.g`` so the settings template + the JSON endpoint
    don't both fork four subprocesses when one request renders both
    (e.g. when the API is polled inline by a server-side fragment).
    """
    cached = getattr(g, "_storage_health_snapshot", None)
    if isinstance(cached, StorageHealthSnapshot):
        return cached
    try:
        snapshot = _get_service().read_snapshot()
    except Exception as exc:  # noqa: BLE001 — never fail the request
        logger.exception("storage_health: unexpected error reading snapshot")
        snapshot = StorageHealthSnapshot(
            severity=SEV_UNKNOWN,
            messages=("Storage health probe crashed; see server log.",),
            probe_errors=(str(exc),),
        )
    g._storage_health_snapshot = snapshot
    return snapshot


@storage_health_bp.route("/api/storage/health", methods=["GET"])
def get_storage_health() -> ResponseReturnValue:
    snapshot = current_snapshot()
    return jsonify(snapshot.to_dict()), HTTPStatus.OK


__all__ = ("current_snapshot", "storage_health_bp")
