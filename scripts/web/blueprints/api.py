"""Blueprint for API endpoints."""

import os
from flask import Blueprint, jsonify

from services.partition_mount_service import check_operation_in_progress
from services.partition_service import get_mount_path
from config import CHIMES_FOLDER

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


@api_bp.route("/chime_filenames")
def chime_filenames():
    """
    Get list of existing chime filenames.
    
    Used by JavaScript to avoid filename collisions when uploading.
    
    Returns:
        JSON array of filenames
    """
    filenames = []
    part2_mount = get_mount_path("part2")
    
    if part2_mount:
        chimes_dir = os.path.join(part2_mount, CHIMES_FOLDER)
        if os.path.isdir(chimes_dir):
            try:
                entries = os.listdir(chimes_dir)
                for entry in entries:
                    if entry.lower().endswith(('.wav', '.mp3')):
                        full_path = os.path.join(chimes_dir, entry)
                        if os.path.isfile(full_path):
                            filenames.append(entry)
            except Exception:
                pass
    
    return jsonify({"filenames": filenames})


@api_bp.route("/rename_chime/<old_filename>/<new_filename>", methods=['POST'])
def rename_chime(old_filename, new_filename):
    """
    Rename a lock chime file without re-encoding.
    
    Used when user only changes filename in trim editor without modifying audio.
    Uses mode-aware operations for safe renaming.
    
    Args:
        old_filename: Current filename
        new_filename: Desired new filename
    
    Returns:
        JSON with success status
    """
    from services.lock_chime_service import rename_chime_file
    
    try:
        result = rename_chime_file(old_filename, new_filename)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@api_bp.route("/gadget_state", methods=['GET'])
def gadget_state():
    """
    Check the current USB gadget state.
    
    Returns:
        JSON with state information including:
        - healthy: bool indicating if system is in good state
        - issues_found: list of detected issues
        - fixes_applied: list of automatic fixes applied
        - errors: list of errors encountered
    """
    from services.partition_mount_service import check_and_recover_gadget_state
    
    try:
        state = check_and_recover_gadget_state()
        return jsonify(state)
    except Exception as e:
        return jsonify({
            "healthy": False,
            "issues_found": ["Failed to check state"],
            "fixes_applied": [],
            "errors": [str(e)]
        }), 500


@api_bp.route("/recover_gadget", methods=['POST'])
def recover_gadget():
    """
    Manually trigger gadget state recovery.
    
    This endpoint attempts to fix common issues:
    - Empty or incorrect LUN backing file
    - Inconsistent mount states
    - Orphaned loop devices
    
    Returns:
        JSON with recovery results
    """
    from services.partition_mount_service import check_and_recover_gadget_state
    import logging
    
    logger = logging.getLogger(__name__)
    logger.info("Manual gadget recovery triggered via API")
    
    try:
        state = check_and_recover_gadget_state()
        
        if state['errors']:
            return jsonify({
                "success": False,
                "message": "Recovery encountered errors",
                "details": state
            }), 500
        
        if state['fixes_applied']:
            return jsonify({
                "success": True,
                "message": f"Applied {len(state['fixes_applied'])} fixes",
                "details": state
            })
        
        if state['healthy']:
            return jsonify({
                "success": True,
                "message": "System is healthy, no fixes needed",
                "details": state
            })
        
        return jsonify({
            "success": False,
            "message": "Issues found but no fixes could be applied",
            "details": state
        }), 500
        
    except Exception as e:
        logger.error(f"Error during manual recovery: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "message": f"Recovery failed: {str(e)}",
            "details": {"errors": [str(e)]}
        }), 500

