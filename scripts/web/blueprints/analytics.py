"""Blueprint for storage analytics and monitoring."""

import os
from flask import Blueprint, render_template, jsonify, request, redirect, url_for, flash

from config import IMG_CAM_PATH
from utils import get_base_context
from services.partition_mount_service import check_operation_in_progress
from services.analytics_service import (
    get_complete_analytics,
    get_partition_usage,
    get_video_statistics,
    get_storage_health
)

analytics_bp = Blueprint('analytics', __name__, url_prefix='/analytics')


@analytics_bp.before_request
def _require_cam_image():
    if not os.path.isfile(IMG_CAM_PATH):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"error": "Feature unavailable"}), 503
        flash("This feature is not available because the required disk image has not been created.")
        return redirect(url_for('mode_control.index'))


@analytics_bp.route("/")
def dashboard():
    """Storage analytics dashboard page."""
    ctx = get_base_context()

    # Check if operation in progress (though analytics reads from part1, not affected by quick_edit_part2)
    op_status = check_operation_in_progress()

    analytics = get_complete_analytics()

    return render_template(
        'analytics.html',
        page='analytics',
        **ctx,
        analytics=analytics,
        operation_in_progress=op_status['in_progress'],
        lock_age=op_status.get('lock_age', 0),
        estimated_completion=op_status.get('estimated_completion', 0),
    )


@analytics_bp.route("/api/data")
def api_data():
    """API endpoint for analytics data (for AJAX updates)."""
    analytics = get_complete_analytics()
    return jsonify(analytics)


@analytics_bp.route("/api/partition-usage")
def api_partition_usage():
    """API endpoint for partition usage only."""
    return jsonify(get_partition_usage())


@analytics_bp.route("/api/video-stats")
def api_video_stats():
    """API endpoint for video statistics only."""
    return jsonify(get_video_statistics())


@analytics_bp.route("/api/health")
def api_health():
    """API endpoint for storage health check."""
    return jsonify(get_storage_health())
