"""Blueprint for custom license plate image management routes."""

import os
import time
import logging
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, jsonify

logger = logging.getLogger(__name__)

from config import IMG_LIGHTSHOW_PATH
from utils import format_file_size, get_base_context
from services.mode_service import current_mode
from services.partition_service import get_mount_path
from services.partition_mount_service import check_operation_in_progress
from services.license_plate_service import (
    upload_plate_file,
    delete_plate_file,
    list_plate_files,
    get_plate_count,
    LICENSE_PLATE_FOLDER,
    MAX_PLATE_COUNT,
    MAX_PLATE_SIZE,
    EXPECTED_WIDTH,
    EXPECTED_HEIGHT,
    MAX_FILENAME_LENGTH,
)
from services.samba_service import close_samba_share, restart_samba_services

license_plates_bp = Blueprint('license_plates', __name__, url_prefix='/license_plates')


@license_plates_bp.before_request
def _require_lightshow_image():
    if not os.path.isfile(IMG_LIGHTSHOW_PATH):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"error": "Feature unavailable"}), 503
        flash("This feature is not available because the required disk image has not been created.")
        return redirect(url_for('mode_control.index'))


@license_plates_bp.route("/")
def license_plates():
    """License plate image management page."""
    ctx = get_base_context()

    op_status = check_operation_in_progress()

    if op_status['in_progress']:
        return render_template(
            'license_plates.html',
            page='media',
            media_tab='plates',
            **ctx,
            plate_files=[],
            plate_count=0,
            max_plate_count=MAX_PLATE_COUNT,
            auto_refresh=False,
            operation_in_progress=True,
            lock_age=op_status['lock_age'],
            estimated_completion=op_status['estimated_completion'],
            max_file_size=MAX_PLATE_SIZE,
            expected_width=EXPECTED_WIDTH,
            expected_height=EXPECTED_HEIGHT,
            max_filename_length=MAX_FILENAME_LENGTH,
        )

    mount_path = get_mount_path("part2")
    plate_files = []

    if mount_path:
        files = list_plate_files(mount_path)
        for file_info in files:
            file_info['partition_key'] = 'part2'
            file_info['size_str'] = format_file_size(file_info['size'])
            if file_info['width'] and file_info['height']:
                file_info['dimensions'] = f"{file_info['width']}x{file_info['height']}"
            else:
                file_info['dimensions'] = "Unknown"
            plate_files.append(file_info)

    return render_template(
        'license_plates.html',
        page='media',
        media_tab='plates',
        **ctx,
        plate_files=plate_files,
        plate_count=len(plate_files),
        max_plate_count=MAX_PLATE_COUNT,
        auto_refresh=False,
        operation_in_progress=False,
        max_file_size=MAX_PLATE_SIZE,
        expected_width=EXPECTED_WIDTH,
        expected_height=EXPECTED_HEIGHT,
        max_filename_length=MAX_FILENAME_LENGTH,
    )


@license_plates_bp.route("/download/<filename>")
def download_plate(filename):
    """Download a license plate PNG file."""
    mount_path = get_mount_path("part2")
    if not mount_path:
        flash("Partition not mounted", "error")
        return redirect(url_for("license_plates.license_plates"))

    plates_dir = os.path.join(mount_path, LICENSE_PLATE_FOLDER)
    file_path = os.path.join(plates_dir, filename)

    if not os.path.isfile(file_path) or not filename.lower().endswith('.png'):
        flash("File not found", "error")
        return redirect(url_for("license_plates.license_plates"))

    return send_file(
        file_path,
        mimetype='image/png',
        as_attachment=True,
        download_name=filename
    )


@license_plates_bp.route("/upload_multiple", methods=["POST"])
def upload_multiple_plates():
    """Upload multiple license plate PNG files at once."""
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    mode = current_mode()

    files = request.files.getlist('plate_files')

    if not files or len(files) == 0:
        if is_ajax:
            return jsonify({"success": False, "error": "No files selected"}), 400
        flash("No files selected", "error")
        return redirect(url_for("license_plates.license_plates"))

    part2_mount_path = get_mount_path("part2") if mode == "edit" else None
    current_count = get_plate_count(part2_mount_path) if part2_mount_path else 0

    results = []
    total_uploaded = 0

    for file in files:
        if file.filename == "":
            continue

        if current_count + total_uploaded >= MAX_PLATE_COUNT:
            results.append({
                'filename': file.filename,
                'success': False,
                'message': f"Maximum of {MAX_PLATE_COUNT} license plates allowed"
            })
            continue

        filename = file.filename

        success, message, dimensions, warning = upload_plate_file(file, filename, part2_mount_path)
        result = {
            'filename': filename,
            'success': success,
            'message': message,
            'dimensions': f"{dimensions[0]}x{dimensions[1]}" if dimensions else None,
        }
        if warning:
            result['warning'] = warning
        results.append(result)
        if success:
            total_uploaded += 1

    if mode == "edit" and total_uploaded > 0:
        try:
            close_samba_share('gadget_part2')
            restart_samba_services()
        except Exception as e:
            logger.error(f"Samba refresh failed: {e}")

    if total_uploaded > 0:
        time.sleep(1.0)

    if is_ajax:
        success_count = sum(1 for r in results if r['success'])
        return jsonify({
            'success': success_count > 0,
            'results': results,
            'total_uploaded': total_uploaded,
            'summary': f"Successfully uploaded {total_uploaded} plate image(s)"
        }), 200

    success_count = sum(1 for r in results if r['success'])
    if success_count > 0:
        flash(f"Successfully uploaded {total_uploaded} plate image(s)", "success")
    else:
        flash("Failed to upload plate images", "error")

    return redirect(url_for("license_plates.license_plates", _=int(time.time())))


@license_plates_bp.route("/upload", methods=["POST"])
def upload_plate():
    """Upload a single license plate PNG file."""
    mode = current_mode()

    if "plate_file" not in request.files:
        flash("No file selected", "error")
        return redirect(url_for("license_plates.license_plates"))

    file = request.files["plate_file"]
    if file.filename == "":
        flash("No file selected", "error")
        return redirect(url_for("license_plates.license_plates"))

    part2_mount_path = get_mount_path("part2") if mode == "edit" else None
    current_count = get_plate_count(part2_mount_path) if part2_mount_path else 0
    if current_count >= MAX_PLATE_COUNT:
        flash(f"Maximum of {MAX_PLATE_COUNT} license plates allowed. Delete some first.", "error")
        return redirect(url_for("license_plates.license_plates"))

    success, message, dimensions, warning = upload_plate_file(file, file.filename, part2_mount_path)

    if success:
        flash(message, "success")
        if warning:
            flash(warning, "warning")

        if mode == "edit":
            try:
                close_samba_share('gadget_part2')
                restart_samba_services()
            except Exception as e:
                flash(f"File uploaded but Samba refresh failed: {str(e)}", "warning")

        time.sleep(1.0)
    else:
        flash(message, "error")

    return redirect(url_for("license_plates.license_plates", _=int(time.time())))


@license_plates_bp.route("/delete/<filename>", methods=["POST"])
def delete_plate(filename):
    """Delete a license plate PNG file."""
    mode = current_mode()

    part2_mount_path = get_mount_path("part2") if mode == "edit" else None

    success, message = delete_plate_file(filename, part2_mount_path)

    if success:
        flash(message, "success")

        if mode == "edit":
            try:
                close_samba_share('gadget_part2')
                restart_samba_services()
            except Exception as e:
                flash(f"File deleted but Samba refresh failed: {str(e)}", "warning")

        time.sleep(0.2)
    else:
        flash(message, "error")

    return redirect(url_for("license_plates.license_plates"))
