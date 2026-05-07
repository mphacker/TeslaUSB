"""Blueprint for custom license-plate management routes.

License plates live on the LightShow drive (part2) alongside wraps
and light shows. The routes mirror the wraps blueprint pattern but
use the stricter license-plate validators and the count helper that
doesn't bypass the limit in present mode.
"""

import os
import time
import logging

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, send_file, jsonify,
)

logger = logging.getLogger(__name__)

from config import USB_PARTITIONS, PART_LABEL_MAP, IMG_LIGHTSHOW_PATH
from utils import format_file_size, get_base_context
from services.mode_service import current_mode
from services.partition_service import get_mount_path, iter_all_partitions
from services.partition_mount_service import check_operation_in_progress
from services.license_plate_service import (
    upload_plate_file,
    delete_plate_file,
    list_plate_files,
    get_plate_count_any_mode,
    safe_rebind_usb_gadget,
    LICENSE_PLATE_FOLDER,
    MAX_PLATE_COUNT,
    MAX_PLATE_SIZE,
    MAX_FILENAME_LENGTH,
    PLATE_DIMENSIONS_NA,
    PLATE_DIMENSIONS_EU,
)
from services.samba_service import close_samba_share, restart_samba_services

license_plates_bp = Blueprint(
    'license_plates', __name__, url_prefix='/license_plates',
)


@license_plates_bp.before_request
def _require_lightshow_image():
    """Block all routes when the LightShow disk image is missing.

    Mirrors the wraps gating pattern — AJAX gets a 503 JSON, browser
    requests get a flash + redirect to the settings page.
    """
    if not os.path.isfile(IMG_LIGHTSHOW_PATH):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"error": "Feature unavailable"}), 503
        flash(
            "This feature is not available because the required disk "
            "image has not been created."
        )
        return redirect(url_for('mode_control.index'))


def _render_plate_page(plate_files, plate_count, op_status=None):
    """Common renderer for the license-plate page.

    Centralized so the operation-in-progress branch and the normal
    branch share the same template kwargs and we can't accidentally
    diverge them.
    """
    ctx = get_base_context()
    operation_in_progress = bool(op_status and op_status['in_progress'])
    return render_template(
        'license_plates.html',
        page='media',
        media_tab='plates',
        **ctx,
        plate_files=plate_files,
        plate_count=plate_count,
        max_plate_count=MAX_PLATE_COUNT,
        auto_refresh=False,
        operation_in_progress=operation_in_progress,
        lock_age=(op_status or {}).get('lock_age', 0),
        estimated_completion=(op_status or {}).get('estimated_completion', 0),
        # Tesla spec values for client-side validation + UI display.
        max_file_size=MAX_PLATE_SIZE,
        max_filename_length=MAX_FILENAME_LENGTH,
        plate_width_na=PLATE_DIMENSIONS_NA[0],
        plate_height_na=PLATE_DIMENSIONS_NA[1],
        plate_width_eu=PLATE_DIMENSIONS_EU[0],
        plate_height_eu=PLATE_DIMENSIONS_EU[1],
    )


@license_plates_bp.route("/")
def license_plates():
    """License-plate management page."""
    op_status = check_operation_in_progress()

    if op_status['in_progress']:
        # While quick_edit_part2 is held by another caller, render
        # the operation banner and an empty list.
        return _render_plate_page([], 0, op_status=op_status)

    plate_files = []
    for part, mount_path in iter_all_partitions():
        for file_info in list_plate_files(mount_path):
            file_info['partition_key'] = part
            file_info['partition'] = PART_LABEL_MAP.get(part, part)
            file_info['size_str'] = format_file_size(file_info['size'])
            if file_info['width'] and file_info['height']:
                file_info['dimensions'] = (
                    f"{file_info['width']}x{file_info['height']}"
                )
            else:
                file_info['dimensions'] = "Unknown"
            plate_files.append(file_info)

    plate_files.sort(key=lambda x: x['filename'].lower())
    return _render_plate_page(plate_files, len(plate_files))


@license_plates_bp.route("/download/<partition>/<filename>")
def download_plate(partition, filename):
    """Download a license-plate PNG file."""
    if partition not in USB_PARTITIONS:
        flash("Invalid partition", "error")
        return redirect(url_for("license_plates.license_plates"))

    mount_path = get_mount_path(partition)
    if not mount_path:
        flash("Partition not mounted", "error")
        return redirect(url_for("license_plates.license_plates"))

    # Strip any path components from a hostile filename and re-check
    # the extension before joining with the destination directory.
    safe_name = os.path.basename(filename)
    if not safe_name.lower().endswith('.png'):
        flash("File not found", "error")
        return redirect(url_for("license_plates.license_plates"))

    file_path = os.path.join(mount_path, LICENSE_PLATE_FOLDER, safe_name)

    # Defense-in-depth: verify the resolved path lives under the
    # LicensePlate folder before serving it. basename() defangs `..`
    # traversal in the filename itself, but a symlink under
    # LicensePlate/ pointing outside the folder would still be served
    # without this check. realpath() follows symlinks; commonpath()
    # confirms containment under the expected root.
    expected_dir = os.path.realpath(os.path.join(mount_path, LICENSE_PLATE_FOLDER))
    try:
        resolved = os.path.realpath(file_path)
        if os.path.commonpath([expected_dir, resolved]) != expected_dir:
            flash("File not found", "error")
            return redirect(url_for("license_plates.license_plates"))
    except ValueError:
        # commonpath raises ValueError on mixed drives (Windows) or
        # when paths cannot be compared. Treat as not-found.
        flash("File not found", "error")
        return redirect(url_for("license_plates.license_plates"))

    if not os.path.isfile(file_path):
        flash("File not found", "error")
        return redirect(url_for("license_plates.license_plates"))

    return send_file(
        file_path,
        mimetype='image/png',
        as_attachment=True,
        download_name=safe_name,
    )


@license_plates_bp.route("/upload_multiple", methods=["POST"])
def upload_multiple_plates():
    """Upload multiple license-plate files in one batch."""
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    mode = current_mode()

    files = request.files.getlist('plate_files')
    if not files:
        if is_ajax:
            return jsonify({"success": False, "error": "No files selected"}), 400
        flash("No files selected", "error")
        return redirect(url_for("license_plates.license_plates"))

    # Edit mode needs the explicit RW path; present mode infers it via
    # quick_edit_part2 inside the service.
    part2_mount_path = get_mount_path("part2") if mode == "edit" else None

    # Mode-aware count check — uses the RO mount in present mode so
    # MAX_PLATE_COUNT is enforced before quick_edit fires.
    current_count = get_plate_count_any_mode()

    results = []
    total_uploaded = 0

    for file in files:
        if not file.filename:
            continue

        if current_count + total_uploaded >= MAX_PLATE_COUNT:
            results.append({
                'filename': file.filename,
                'success': False,
                'message': f"Maximum of {MAX_PLATE_COUNT} license plates allowed",
            })
            continue

        # defer_rebind=True per file — one batch rebind below.
        success, message, dimensions = upload_plate_file(
            file, file.filename, part2_mount_path, defer_rebind=True,
        )
        results.append({
            'filename': file.filename,
            'success': success,
            'message': message,
            'dimensions': (
                f"{dimensions[0]}x{dimensions[1]}" if dimensions else None
            ),
        })
        if success:
            total_uploaded += 1

    if mode == "edit" and total_uploaded > 0:
        try:
            close_samba_share('gadget_part2')
            restart_samba_services()
        except Exception as e:
            logger.error(f"Samba refresh failed: {e}")

    # One USB rebind for the whole batch (present mode only).
    if mode == "present" and total_uploaded > 0:
        safe_rebind_usb_gadget()

    if is_ajax:
        success_count = sum(1 for r in results if r['success'])
        return jsonify({
            'success': success_count > 0,
            'results': results,
            'total_uploaded': total_uploaded,
            'summary': (
                f"Successfully uploaded {total_uploaded} license "
                f"plate(s) from {success_count}/{len(results)} file(s)"
            ),
        }), 200

    success_count = sum(1 for r in results if r['success'])
    if success_count > 0:
        flash(
            f"Successfully uploaded {total_uploaded} license plate(s)",
            "success",
        )
    else:
        flash("Failed to upload license plates", "error")

    return redirect(
        url_for("license_plates.license_plates", _=int(time.time()))
    )


@license_plates_bp.route("/upload", methods=["POST"])
def upload_plate():
    """Upload a single license-plate PNG."""
    mode = current_mode()

    if "plate_file" not in request.files:
        flash("No file selected", "error")
        return redirect(url_for("license_plates.license_plates"))

    file = request.files["plate_file"]
    if file.filename == "":
        flash("No file selected", "error")
        return redirect(url_for("license_plates.license_plates"))

    part2_mount_path = get_mount_path("part2") if mode == "edit" else None

    current_count = get_plate_count_any_mode()
    if current_count >= MAX_PLATE_COUNT:
        flash(
            f"Maximum of {MAX_PLATE_COUNT} license plates allowed. "
            f"Delete some plates first.",
            "error",
        )
        return redirect(url_for("license_plates.license_plates"))

    # The service handles the post-upload USB rebind in present mode.
    success, message, dimensions = upload_plate_file(
        file, file.filename, part2_mount_path,
    )

    if success:
        flash(message, "success")
        if mode == "edit":
            try:
                close_samba_share('gadget_part2')
                restart_samba_services()
            except Exception as e:
                flash(
                    f"File uploaded but Samba refresh failed: {str(e)}",
                    "warning",
                )
    else:
        flash(message, "error")

    # Cache-bust the redirect so the freshly-uploaded plate appears.
    return redirect(
        url_for("license_plates.license_plates", _=int(time.time()))
    )


@license_plates_bp.route("/delete/<partition>/<filename>", methods=["POST"])
def delete_plate(partition, filename):
    """Delete a license-plate PNG."""
    mode = current_mode()

    if partition not in USB_PARTITIONS:
        flash("Invalid partition", "error")
        return redirect(url_for("license_plates.license_plates"))

    part2_mount_path = get_mount_path(partition) if mode == "edit" else None

    # The service rebinds the USB gadget after a successful present-mode
    # delete so Tesla drops the plate from its cache without a reboot.
    success, message = delete_plate_file(filename, part2_mount_path)

    if success:
        flash(message, "success")
        if mode == "edit":
            try:
                close_samba_share('gadget_part2')
                restart_samba_services()
            except Exception as e:
                flash(
                    f"File deleted but Samba refresh failed: {str(e)}",
                    "warning",
                )
    else:
        flash(message, "error")

    return redirect(url_for("license_plates.license_plates"))
