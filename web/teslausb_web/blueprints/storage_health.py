"""Storage Health blueprint — JSON snapshot for the Settings page card.

Endpoints:

* ``GET /api/storage/health`` — returns the latest
  :class:`StorageHealthSnapshot` as JSON. The front-end polls this
  every 60 s while the page is visible.
* ``POST /api/storage/health/fsck-on-next-boot`` — touches
  ``/forcefsck`` so the kernel's systemd-fsck@.service runs
  ``e2fsck -fy`` on every fstab filesystem at the next boot. The
  operator must then reboot to take effect — this endpoint
  intentionally does NOT reboot for them.
* ``DELETE /api/storage/health/fsck-on-next-boot`` — removes the
  sentinel so the scheduled fsck is cancelled.

The endpoints always return the freshly-recomputed snapshot so the
client can render the new state with no second round-trip.
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


def _refresh_snapshot() -> StorageHealthSnapshot:
    """Force a re-probe after a mutating action."""
    g.pop("_storage_health_snapshot", None)
    return current_snapshot()


@storage_health_bp.route("/api/storage/health", methods=["GET"])
def get_storage_health() -> ResponseReturnValue:
    snapshot = current_snapshot()
    return jsonify(snapshot.to_dict()), HTTPStatus.OK


@storage_health_bp.route(
    "/api/storage/health/fsck-on-next-boot", methods=["POST"]
)
def schedule_fsck() -> ResponseReturnValue:
    try:
        _get_service().schedule_fsck_at_next_boot()
    except Exception as exc:  # noqa: BLE001 — surface to user as JSON
        logger.exception("storage_health: failed to schedule fsck")
        return (
            jsonify({"error": str(exc), "snapshot": current_snapshot().to_dict()}),
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )
    return jsonify(_refresh_snapshot().to_dict()), HTTPStatus.OK


@storage_health_bp.route(
    "/api/storage/health/fsck-on-next-boot", methods=["DELETE"]
)
def cancel_fsck() -> ResponseReturnValue:
    try:
        _get_service().cancel_scheduled_fsck()
    except Exception as exc:  # noqa: BLE001 — surface to user as JSON
        logger.exception("storage_health: failed to cancel scheduled fsck")
        return (
            jsonify({"error": str(exc), "snapshot": current_snapshot().to_dict()}),
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )
    return jsonify(_refresh_snapshot().to_dict()), HTTPStatus.OK


__all__ = ("current_snapshot", "storage_health_bp")
