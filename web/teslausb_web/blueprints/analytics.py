"""Storage-analytics blueprint — replaces the Phase 5.4 scaffold.

Routes (mirror v1's ``scripts/web/blueprints/analytics.py``):

* ``GET /analytics/``                 → HTML dashboard.
* ``GET /analytics/api/data``         → full JSON snapshot.
* ``GET /analytics/api/partition-usage`` → partition usage only.
* ``GET /analytics/api/video-stats``  → video statistics only.
* ``GET /analytics/api/health``       → storage health only.

What changed from v1 (per docs/00-PLAN.md Phase 5 invariants):

* The v1 ``before_request`` ``IMG_CAM_PATH`` gate is **gone**. B-1 has
  no IMG/loopback layer; the page is always reachable.
* ``mode_control.*`` redirects → ``settings.index``.
* Per-partition fsck status is dropped (B-1 uses btrfs scrub via the
  system_health card; analytics is read-only).
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING

from flask import Blueprint, Response, current_app, jsonify, render_template

from teslausb_web.services.analytics_service import (
    AnalyticsDataError,
    AnalyticsError,
    AnalyticsService,
    CompleteAnalytics,
    complete_to_dict,
    health_to_dict,
    partition_to_dict,
    video_stats_to_dict,
)

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

logger = logging.getLogger(__name__)

analytics_bp = Blueprint("analytics", __name__, url_prefix="/analytics")


def _get_service() -> AnalyticsService:
    """Resolve the analytics service registered by the app factory."""
    service = current_app.extensions.get("analytics_service")
    if not isinstance(service, AnalyticsService):
        raise RuntimeError("analytics_service extension is not configured")
    return service


def _json_error(message: str, *, status: HTTPStatus) -> tuple[Response, int]:
    return jsonify({"success": False, "error": message}), int(status)


def _safe_compute() -> CompleteAnalytics | None:
    """Compute the full payload, logging + swallowing data errors.

    A torn DB or unreachable mount should not 500 the dashboard;
    the template handles ``analytics is None`` by rendering a
    friendly placeholder.
    """
    try:
        return _get_service().get_complete_analytics()
    except AnalyticsError as exc:
        logger.warning("analytics: complete payload unavailable: %s", exc)
        return None


@analytics_bp.route("/", endpoint="dashboard")
def dashboard() -> ResponseReturnValue:
    """Render the analytics dashboard.

    The view always returns 200 — partial data is preferred to a 500
    when one backing source is degraded (charter §"Fault-tolerant").
    """
    analytics = _safe_compute()
    return render_template(
        "analytics.html",
        page="analytics",
        analytics=analytics,
    )


@analytics_bp.route("/api/data")
def api_data() -> ResponseReturnValue:
    """Full JSON snapshot used by the dashboard's optional polling hook."""
    analytics = _safe_compute()
    if analytics is None:
        return _json_error(
            "Analytics data temporarily unavailable",
            status=HTTPStatus.SERVICE_UNAVAILABLE,
        )
    return jsonify(complete_to_dict(analytics))


@analytics_bp.route("/api/partition-usage")
def api_partition_usage() -> ResponseReturnValue:
    """Disk-usage probes only."""
    try:
        partitions = _get_service().get_partition_usage()
    except AnalyticsError as exc:
        logger.warning("analytics: partition usage failed: %s", exc)
        return _json_error(str(exc), status=HTTPStatus.SERVICE_UNAVAILABLE)
    return jsonify({"partitions": [partition_to_dict(p) for p in partitions]})


@analytics_bp.route("/api/video-stats")
def api_video_stats() -> ResponseReturnValue:
    """Mapping-DB-derived clip statistics."""
    try:
        stats = _get_service().get_video_statistics()
    except AnalyticsDataError as exc:
        return _json_error(str(exc), status=HTTPStatus.SERVICE_UNAVAILABLE)
    except AnalyticsError as exc:
        return _json_error(str(exc), status=HTTPStatus.INTERNAL_SERVER_ERROR)
    return jsonify(video_stats_to_dict(stats))


@analytics_bp.route("/api/health")
def api_health() -> ResponseReturnValue:
    """Composite storage-health verdict."""
    try:
        health = _get_service().get_storage_health()
    except AnalyticsError as exc:
        return _json_error(str(exc), status=HTTPStatus.SERVICE_UNAVAILABLE)
    return jsonify(health_to_dict(health))


__all__ = ("analytics_bp",)
