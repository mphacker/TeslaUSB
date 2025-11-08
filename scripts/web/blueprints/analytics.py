"""Blueprint for storage analytics and monitoring."""

import socket
from flask import Blueprint, render_template, jsonify

from services.mode_service import mode_display
from services.analytics_service import (
    get_complete_analytics,
    get_partition_usage,
    get_video_statistics,
    get_storage_health
)

analytics_bp = Blueprint('analytics', __name__, url_prefix='/analytics')


@analytics_bp.route("/")
def dashboard():
    """Storage analytics dashboard page."""
    token, label, css_class, share_paths = mode_display()
    analytics = get_complete_analytics()
    
    return render_template(
        'analytics.html',
        page='analytics',
        mode_label=label,
        mode_class=css_class,
        mode_token=token,
        analytics=analytics,
        hostname=socket.gethostname()
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
