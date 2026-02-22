"""
Cleanup Blueprint for TeslaUSB Web Interface
Handles cleanup configuration, preview, and execution
"""

import os
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash
from pathlib import Path
import sys

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import GADGET_DIR, IMG_CAM_PATH
from utils import get_base_context
from services.cleanup_service import get_cleanup_service
from services.analytics_service import get_partition_usage
from services.mode_service import current_mode
from services.partition_service import get_mount_path

cleanup_bp = Blueprint('cleanup', __name__, url_prefix='/cleanup')


@cleanup_bp.before_request
def _require_cam_image():
    if not os.path.isfile(IMG_CAM_PATH):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"error": "Feature unavailable"}), 503
        flash("This feature is not available because the required disk image has not been created.")
        return redirect(url_for('mode_control.index'))


@cleanup_bp.route('/')
def index():
    """Redirect to settings page"""
    return redirect(url_for('cleanup.settings'))


@cleanup_bp.route('/settings', methods=['GET'])
def settings():
    """
    Display cleanup configuration settings page
    """
    cleanup_service = get_cleanup_service(GADGET_DIR)

    # Get TeslaCam partition path and detect folders
    partition_path = Path(get_mount_path('part1'))
    policies = cleanup_service.get_policies_for_detected_folders(partition_path)

    ctx = get_base_context()
    mode = current_mode()

    return render_template(
        'cleanup_settings.html',
        policies=policies,
        mode=mode,
        **ctx,
        page='analytics'  # Show Analytics as active in nav
    )


@cleanup_bp.route('/settings', methods=['POST'])
def save_settings():
    """
    Save cleanup configuration settings
    """
    cleanup_service = get_cleanup_service(GADGET_DIR)

    # Get list of detected folders from form data
    partition_path = Path(get_mount_path('part1'))
    detected_folders = cleanup_service.detect_teslacam_folders(partition_path)

    # Build policies from form data for detected folders
    policies = {}

    for folder in detected_folders:
        policies[folder] = {
            'enabled': request.form.get(f'{folder}_enabled') == 'on',
            'age_based': {
                'enabled': request.form.get(f'{folder}_age_enabled') == 'on',
                'days': int(request.form.get(f'{folder}_age_days', 30))
            },
            'size_based': {
                'enabled': request.form.get(f'{folder}_size_enabled') == 'on',
                'max_gb': int(request.form.get(f'{folder}_size_gb', 50))
            },
            'count_based': {
                'enabled': request.form.get(f'{folder}_count_enabled') == 'on',
                'max_videos': int(request.form.get(f'{folder}_count_videos', 500))
            }
        }

    # Save policies
    if cleanup_service.save_policies(policies):
        flash('Cleanup settings saved successfully!', 'success')
    else:
        flash('Error saving cleanup settings', 'error')

    return redirect(url_for('cleanup.settings'))


@cleanup_bp.route('/preview')
def preview():
    """
    Preview cleanup plan - show what will be deleted
    """
    mode = current_mode()

    # Can only preview in Edit mode (need read-write access)
    if mode != 'edit':
        flash('Please switch to Edit mode to preview cleanup', 'warning')
        return redirect(url_for('cleanup.settings'))

    cleanup_service = get_cleanup_service(GADGET_DIR)

    # Get TeslaCam partition path
    partition_path = Path(get_mount_path('part1'))

    # DEBUG: Print loaded policies
    print(f"DEBUG: Loaded policies: {cleanup_service.get_policies()}")
    print(f"DEBUG: Partition path: {partition_path}")

    # Calculate cleanup plan
    cleanup_plan = cleanup_service.calculate_cleanup_plan(partition_path)

    # DEBUG: Print cleanup plan
    print(f"DEBUG: Cleanup plan: total_count={cleanup_plan.get('total_count', 0)}, total_size={cleanup_plan.get('total_size', 0)}")
    print(f"DEBUG: Breakdown: {cleanup_plan.get('breakdown_by_folder', {})}")

    # Get current partition usage for impact preview
    partition_usage = get_partition_usage()
    teslacam_usage = partition_usage.get('part1', {})

    # Calculate impact
    impact = cleanup_service.preview_cleanup_impact(cleanup_plan, teslacam_usage)

    ctx = get_base_context()

    return render_template(
        'cleanup_preview.html',
        cleanup_plan=cleanup_plan,
        impact=impact,
        mode=mode,
        **ctx,
        page='analytics'
    )


@cleanup_bp.route('/execute', methods=['POST'])
def execute():
    """
    Execute cleanup plan - actually delete files
    """
    mode = current_mode()

    # Can only execute in Edit mode (need read-write access)
    if mode != 'edit':
        return jsonify({
            'success': False,
            'error': 'Must be in Edit mode to delete files'
        }), 400

    # Get dry_run parameter
    dry_run = request.form.get('dry_run') == 'true'

    cleanup_service = get_cleanup_service(GADGET_DIR)

    # Get TeslaCam partition path
    partition_path = Path(get_mount_path('part1'))

    # Calculate cleanup plan
    cleanup_plan = cleanup_service.calculate_cleanup_plan(partition_path)

    # Execute cleanup
    result = cleanup_service.execute_cleanup(cleanup_plan, dry_run=dry_run)

    # Store result in session for report page
    # For now, just redirect to report with result in URL params
    if result['success']:
        flash(f"Cleanup complete! Deleted {result['deleted_count']} files ({result['deleted_size_gb']} GB)", 'success')
    else:
        flash(f"Cleanup completed with errors. Deleted {result['deleted_count']} files.", 'warning')

    return redirect(url_for('cleanup.report',
                           deleted_count=result['deleted_count'],
                           deleted_size_gb=result['deleted_size_gb'],
                           dry_run=dry_run))


@cleanup_bp.route('/report')
def report():
    """
    Show cleanup execution report
    """
    deleted_count = request.args.get('deleted_count', 0, type=int)
    deleted_size_gb = request.args.get('deleted_size_gb', 0.0, type=float)
    dry_run = request.args.get('dry_run', 'false') == 'true'

    # Get updated partition usage
    partition_usage = get_partition_usage()
    teslacam_usage = partition_usage.get('part1', {})

    ctx = get_base_context()
    mode = current_mode()

    return render_template(
        'cleanup_report.html',
        deleted_count=deleted_count,
        deleted_size_gb=deleted_size_gb,
        dry_run=dry_run,
        partition_usage=teslacam_usage,
        mode=mode,
        **ctx,
        page='analytics'
    )


@cleanup_bp.route('/api/calculate', methods=['POST'])
def api_calculate():
    """
    API endpoint to calculate cleanup plan with custom policies
    Returns JSON with cleanup plan
    """
    mode = current_mode()

    if mode != 'edit':
        return jsonify({
            'success': False,
            'error': 'Must be in Edit mode'
        }), 400

    try:
        cleanup_service = get_cleanup_service(GADGET_DIR)
        partition_path = Path(get_mount_path('part1'))
        cleanup_plan = cleanup_service.calculate_cleanup_plan(partition_path)

        return jsonify({
            'success': True,
            'total_count': cleanup_plan['total_count'],
            'total_size_gb': cleanup_plan['total_size_gb'],
            'breakdown': {
                folder: {
                    'count': data['count'],
                    'size_gb': round(data['size'] / 1024**3, 2)
                }
                for folder, data in cleanup_plan['breakdown_by_folder'].items()
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
