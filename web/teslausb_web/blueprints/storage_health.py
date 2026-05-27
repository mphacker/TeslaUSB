"""Storage Health blueprint — JSON snapshot for the Settings page card.

Endpoints:

* ``GET /api/storage/health`` — returns the latest
  :class:`StorageHealthSnapshot` as JSON. The front-end polls this
  every 60 s while the page is visible.
* ``POST /api/storage/health/fsck-on-next-boot`` — arms a one-shot
  filesystem check at the next boot. Three things are armed: the
  ``/forcefsck`` sentinel (for non-root fstab entries), the
  ``fsck.mode=force`` token in the kernel cmdline (the only thing
  that forces a root-fs check on a Pi with no initramfs), and a
  boot-id marker that lets the app strip the cmdline flag again
  after the operator reboots. The operator must reboot to take
  effect — this endpoint intentionally does NOT reboot for them.
* ``DELETE /api/storage/health/fsck-on-next-boot`` — removes the
  sentinel so the scheduled fsck is cancelled.
* ``POST /api/storage/health/reboot-now`` — invokes
  ``systemctl reboot``. Requires a JSON body ``{"confirm": true}`` so
  an accidental request can't take the device down. Pairs with the
  scheduler so the operator can fsck-and-reboot in one click.

The endpoints always return the freshly-recomputed snapshot so the
client can render the new state with no second round-trip.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING

from flask import Blueprint, current_app, g, jsonify, request

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


@storage_health_bp.route(
    "/api/storage/health/reboot-now", methods=["POST"]
)
def reboot_now() -> ResponseReturnValue:
    """Reboot the device immediately so a scheduled fsck can run.

    Requires ``{"confirm": true}`` in the JSON body so a stray POST
    (e.g. a browser pre-fetch) can never take the device down.
    Returns ``202 Accepted`` because the work happens after we've
    already shipped the response — the client should expect the
    connection to drop and reconnect.
    """
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return (
            jsonify(
                {
                    "error": "Reboot requires explicit confirmation.",
                    "snapshot": current_snapshot().to_dict(),
                }
            ),
            HTTPStatus.BAD_REQUEST,
        )
    try:
        _get_service().reboot_now()
    except Exception as exc:  # noqa: BLE001 — surface to user as JSON
        logger.exception("storage_health: failed to reboot")
        return (
            jsonify({"error": str(exc), "snapshot": current_snapshot().to_dict()}),
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )
    return (
        jsonify(
            {
                "rebooting": True,
                "snapshot": current_snapshot().to_dict(),
            }
        ),
        HTTPStatus.ACCEPTED,
    )


@storage_health_bp.route(
    "/api/storage/health/online-check", methods=["POST"]
)
def trigger_online_check() -> ResponseReturnValue:
    """Trigger an on-demand read-only ``e2fsck -nf`` of the root device.

    Runs in a daemon thread so the request returns immediately
    (``202 Accepted``). The client polls ``GET /api/storage/health``
    to see when ``online_check_running`` flips back to ``false`` and
    a fresh ``online_check_iso``/``status``/``message`` appears.

    Safe to invoke any time: the check runs with ``-n`` (no writes)
    and the service collapses overlapping requests so two concurrent
    POSTs cannot start two concurrent ``e2fsck`` processes.
    """
    try:
        started = _get_service().maybe_start_background_online_check(force=True)
    except Exception as exc:  # noqa: BLE001 — surface to user as JSON
        logger.exception("storage_health: failed to start online check")
        return (
            jsonify({"error": str(exc), "snapshot": current_snapshot().to_dict()}),
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )
    return (
        jsonify(
            {
                "started": started,
                "snapshot": _refresh_snapshot().to_dict(),
            }
        ),
        HTTPStatus.ACCEPTED,
    )


__all__ = ("current_snapshot", "storage_health_bp")
