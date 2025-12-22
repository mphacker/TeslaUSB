"""Blueprint for filesystem check operations."""

from flask import Blueprint, jsonify, request

from services.mode_service import current_mode
from services import fsck_service

fsck_bp = Blueprint('fsck', __name__, url_prefix='/fsck')


@fsck_bp.route("/api/start", methods=['POST'])
def start_check():
    """
    Start filesystem check on a partition.

    POST body:
    {
        "partition": 1 or 2,
        "mode": "quick" or "repair"
    }
    """
    try:
        data = request.get_json()
        partition = data.get('partition')
        mode = data.get('mode', 'quick')

        # Validate partition
        if partition not in [1, 2]:
            return jsonify({
                'success': False,
                'message': 'Invalid partition. Must be 1 or 2'
            }), 400

        # Start fsck
        success, message = fsck_service.start_fsck(partition, mode)

        if success:
            return jsonify({
                'success': True,
                'message': message
            })
        else:
            return jsonify({
                'success': False,
                'message': message
            }), 400

    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error starting filesystem check: {str(e)}'
        }), 500


@fsck_bp.route("/api/status")
def get_status():
    """Get current filesystem check status."""
    try:
        status = fsck_service.get_status()
        return jsonify(status)
    except Exception as e:
        return jsonify({
            'error': str(e)
        }), 500


@fsck_bp.route("/api/cancel", methods=['POST'])
def cancel_check():
    """Cancel a running filesystem check."""
    try:
        success, message = fsck_service.cancel_fsck()

        if success:
            return jsonify({
                'success': True,
                'message': message
            })
        else:
            return jsonify({
                'success': False,
                'message': message
            }), 400

    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error cancelling filesystem check: {str(e)}'
        }), 500


@fsck_bp.route("/api/history")
def get_history():
    """Get filesystem check history."""
    try:
        history = fsck_service.get_history()
        return jsonify(history)
    except Exception as e:
        return jsonify({
            'error': str(e)
        }), 500


@fsck_bp.route("/api/last-check/<int:partition>")
def get_last_check(partition):
    """Get last successful check for a partition."""
    try:
        if partition not in [1, 2]:
            return jsonify({
                'error': 'Invalid partition'
            }), 400

        last_check = fsck_service.get_last_check(partition)

        if last_check:
            return jsonify(last_check)
        else:
            return jsonify({
                'timestamp': None,
                'result': 'never_checked'
            })

    except Exception as e:
        return jsonify({
            'error': str(e)
        }), 500
