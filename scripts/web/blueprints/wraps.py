"""Blueprint for custom wrap management routes."""

import os
import time
import logging
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, jsonify

logger = logging.getLogger(__name__)

from config import USB_PARTITIONS, PART_LABEL_MAP
from utils import format_file_size, get_base_context
from services.mode_service import current_mode
from services.partition_service import get_mount_path, iter_all_partitions
from services.partition_mount_service import check_operation_in_progress
from services.wrap_service import (
    upload_wrap_file,
    delete_wrap_file,
    list_wrap_files,
    get_wrap_count,
    WRAPS_FOLDER,
    MAX_WRAP_COUNT,
    MAX_WRAP_SIZE,
    MIN_DIMENSION,
    MAX_DIMENSION,
    MAX_FILENAME_LENGTH
)
from services.samba_service import close_samba_share, restart_samba_services

wraps_bp = Blueprint('wraps', __name__, url_prefix='/wraps')


@wraps_bp.route("/")
def wraps():
    """Custom wraps management page."""
    ctx = get_base_context()

    # Check if file operation is in progress
    op_status = check_operation_in_progress()

    # If operation in progress, show limited page with operation banner
    if op_status['in_progress']:
        return render_template(
            'wraps.html',
            page='wraps',
            **ctx,
            wrap_files=[],
            wrap_count=0,
            max_wrap_count=MAX_WRAP_COUNT,
            auto_refresh=False,
            operation_in_progress=True,
            lock_age=op_status['lock_age'],
            estimated_completion=op_status['estimated_completion'],
            # Validation limits for client-side
            max_file_size=MAX_WRAP_SIZE,
            min_dimension=MIN_DIMENSION,
            max_dimension=MAX_DIMENSION,
            max_filename_length=MAX_FILENAME_LENGTH,
        )

    # Get all PNG files from Wraps folders
    wrap_files = []
    for part, mount_path in iter_all_partitions():
        files = list_wrap_files(mount_path)
        for file_info in files:
            file_info['partition_key'] = part
            file_info['partition'] = PART_LABEL_MAP.get(part, part)
            file_info['size_str'] = format_file_size(file_info['size'])
            if file_info['width'] and file_info['height']:
                file_info['dimensions'] = f"{file_info['width']}x{file_info['height']}"
            else:
                file_info['dimensions'] = "Unknown"
            wrap_files.append(file_info)

    # Sort by filename
    wrap_files.sort(key=lambda x: x['filename'].lower())

    return render_template(
        'wraps.html',
        page='wraps',
        **ctx,
        wrap_files=wrap_files,
        wrap_count=len(wrap_files),
        max_wrap_count=MAX_WRAP_COUNT,
        auto_refresh=False,
        operation_in_progress=False,
        # Validation limits for client-side
        max_file_size=MAX_WRAP_SIZE,
        min_dimension=MIN_DIMENSION,
        max_dimension=MAX_DIMENSION,
        max_filename_length=MAX_FILENAME_LENGTH,
    )


@wraps_bp.route("/thumbnail/<partition>/<filename>")
def wrap_thumbnail(partition, filename):
    """Serve a wrap PNG file as a thumbnail."""
    if partition not in USB_PARTITIONS:
        flash("Invalid partition", "error")
        return redirect(url_for("wraps.wraps"))

    mount_path = get_mount_path(partition)
    if not mount_path:
        flash("Partition not mounted", "error")
        return redirect(url_for("wraps.wraps"))

    wraps_dir = os.path.join(mount_path, WRAPS_FOLDER)
    file_path = os.path.join(wraps_dir, filename)

    if not os.path.isfile(file_path) or not filename.lower().endswith('.png'):
        flash("File not found", "error")
        return redirect(url_for("wraps.wraps"))

    return send_file(file_path, mimetype="image/png")


@wraps_bp.route("/download/<partition>/<filename>")
def download_wrap(partition, filename):
    """Download a wrap PNG file."""
    if partition not in USB_PARTITIONS:
        flash("Invalid partition", "error")
        return redirect(url_for("wraps.wraps"))

    mount_path = get_mount_path(partition)
    if not mount_path:
        flash("Partition not mounted", "error")
        return redirect(url_for("wraps.wraps"))

    wraps_dir = os.path.join(mount_path, WRAPS_FOLDER)
    file_path = os.path.join(wraps_dir, filename)

    if not os.path.isfile(file_path) or not filename.lower().endswith('.png'):
        flash("File not found", "error")
        return redirect(url_for("wraps.wraps"))

    return send_file(
        file_path,
        mimetype='image/png',
        as_attachment=True,
        download_name=filename
    )


@wraps_bp.route("/upload_multiple", methods=["POST"])
def upload_multiple_wraps():
    """Upload multiple wrap files at once."""
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    mode = current_mode()

    # Get all uploaded files
    files = request.files.getlist('wrap_files')

    if not files or len(files) == 0:
        if is_ajax:
            return jsonify({"success": False, "error": "No files selected"}), 400
        flash("No files selected", "error")
        return redirect(url_for("wraps.wraps"))

    # Get part2 mount path (only needed in edit mode, None is fine for present mode)
    part2_mount_path = get_mount_path("part2") if mode == "edit" else None

    # Check current wrap count
    current_count = get_wrap_count(part2_mount_path) if part2_mount_path else 0

    results = []
    total_uploaded = 0

    for file in files:
        if file.filename == "":
            continue

        # Check if we'd exceed the max count
        if current_count + total_uploaded >= MAX_WRAP_COUNT:
            results.append({
                'filename': file.filename,
                'success': False,
                'message': f"Maximum of {MAX_WRAP_COUNT} wraps allowed"
            })
            continue

        filename = file.filename

        # Handle individual file upload
        success, message, dimensions = upload_wrap_file(file, filename, part2_mount_path)
        results.append({
            'filename': filename,
            'success': success,
            'message': message,
            'dimensions': f"{dimensions[0]}x{dimensions[1]}" if dimensions else None
        })
        if success:
            total_uploaded += 1

    # Refresh Samba shares only if in edit mode
    if mode == "edit" and total_uploaded > 0:
        try:
            close_samba_share('gadget_part2')
            restart_samba_services()
        except Exception as e:
            logger.error(f"Samba refresh failed: {e}")

    # Delay for filesystem settling
    if total_uploaded > 0:
        time.sleep(1.0)

    if is_ajax:
        success_count = sum(1 for r in results if r['success'])
        return jsonify({
            'success': success_count > 0,
            'results': results,
            'total_uploaded': total_uploaded,
            'summary': f"Successfully uploaded {total_uploaded} wrap(s) from {success_count}/{len(results)} file(s)"
        }), 200

    # Non-AJAX fallback
    success_count = sum(1 for r in results if r['success'])
    if success_count > 0:
        flash(f"Successfully uploaded {total_uploaded} wrap(s)", "success")
    else:
        flash("Failed to upload wraps", "error")

    return redirect(url_for("wraps.wraps", _=int(time.time())))


@wraps_bp.route("/upload", methods=["POST"])
def upload_wrap():
    """Upload a new wrap PNG file."""
    mode = current_mode()

    if "wrap_file" not in request.files:
        flash("No file selected", "error")
        return redirect(url_for("wraps.wraps"))

    file = request.files["wrap_file"]
    if file.filename == "":
        flash("No file selected", "error")
        return redirect(url_for("wraps.wraps"))

    # Get part2 mount path (only needed in edit mode, None is fine for present mode)
    part2_mount_path = get_mount_path("part2") if mode == "edit" else None

    # Check current wrap count
    current_count = get_wrap_count(part2_mount_path) if part2_mount_path else 0
    if current_count >= MAX_WRAP_COUNT:
        flash(f"Maximum of {MAX_WRAP_COUNT} wraps allowed. Delete some wraps first.", "error")
        return redirect(url_for("wraps.wraps"))

    # Handle file upload
    success, message, dimensions = upload_wrap_file(file, file.filename, part2_mount_path)

    if success:
        flash(message, "success")

        # Refresh Samba shares only if in edit mode
        if mode == "edit":
            try:
                close_samba_share('gadget_part2')
                restart_samba_services()
            except Exception as e:
                flash(f"File uploaded but Samba refresh failed: {str(e)}", "warning")

        # Longer delay for filesystem settling after quick_edit remount
        time.sleep(1.0)
    else:
        flash(message, "error")

    # Add timestamp to force browser cache refresh
    return redirect(url_for("wraps.wraps", _=int(time.time())))


@wraps_bp.route("/delete/<partition>/<filename>", methods=["POST"])
def delete_wrap(partition, filename):
    """Delete a wrap PNG file."""
    mode = current_mode()

    if partition not in USB_PARTITIONS:
        flash("Invalid partition", "error")
        return redirect(url_for("wraps.wraps"))

    # Get part2 mount path (only needed in edit mode, None is fine for present mode)
    part2_mount_path = get_mount_path(partition) if mode == "edit" else None

    # Delete the file using the service (mode-aware)
    success, message = delete_wrap_file(filename, part2_mount_path)

    if success:
        flash(message, "success")

        # Refresh Samba shares only if in edit mode
        if mode == "edit":
            try:
                close_samba_share('gadget_part2')
                restart_samba_services()
            except Exception as e:
                flash(f"File deleted but Samba refresh failed: {str(e)}", "warning")

        # Small delay for filesystem settling
        time.sleep(0.2)
    else:
        flash(message, "error")

    return redirect(url_for("wraps.wraps"))
