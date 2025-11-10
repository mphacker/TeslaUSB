"""Blueprint for API endpoints."""

from flask import Blueprint, jsonify

from services.partition_mount_service import check_operation_in_progress

api_bp = Blueprint('api', __name__, url_prefix='/api')


@api_bp.route("/operation_status")
def operation_status():
    """
    Check if a file operation is currently in progress.
    
    Used by JavaScript to poll operation status and trigger auto-refresh
    when operations complete.
    
    Returns:
        JSON with operation status details
    """
    status = check_operation_in_progress()
    return jsonify(status)
