"""Blueprint for light show management routes."""

import os
import socket
import time
import logging
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file

logger = logging.getLogger(__name__)

from config import USB_PARTITIONS, PART_LABEL_MAP
from utils import format_file_size
from services.mode_service import mode_display, current_mode
from services.partition_service import get_mount_path, iter_all_partitions
from services.light_show_service import upload_light_show_file, upload_zip_file, delete_light_show_files, create_light_show_zip
from services.samba_service import close_samba_share, restart_samba_services

light_shows_bp = Blueprint('light_shows', __name__, url_prefix='/light_shows')


@light_shows_bp.route("/")
def light_shows():
    """Light shows management page."""
    token, label, css_class, share_paths = mode_display()
    
    # Get all fseq, mp3, and wav files from LightShow folders
    files_dict = {}  # Group files by base name
    for part, mount_path in iter_all_partitions():
        lightshow_dir = os.path.join(mount_path, "LightShow")
        if not os.path.isdir(lightshow_dir):
            continue
        
        try:
            entries = os.listdir(lightshow_dir)
        except OSError:
            continue
        
        for entry in entries:
            lower_entry = entry.lower()
            if not (lower_entry.endswith(".fseq") or lower_entry.endswith(".mp3") or lower_entry.endswith(".wav")):
                continue
            
            full_path = os.path.join(lightshow_dir, entry)
            if os.path.isfile(full_path):
                # Get base name without extension
                base_name = os.path.splitext(entry)[0]
                
                if base_name not in files_dict:
                    files_dict[base_name] = {
                        "base_name": base_name,
                        "fseq_file": None,
                        "audio_file": None,
                        "partition_key": part,
                        "partition": PART_LABEL_MAP.get(part, part),
                    }
                
                size = os.path.getsize(full_path)
                if lower_entry.endswith(".fseq"):
                    files_dict[base_name]["fseq_file"] = {
                        "filename": entry,
                        "size": size,
                        "size_str": format_file_size(size),
                    }
                elif lower_entry.endswith(".mp3") or lower_entry.endswith(".wav"):
                    files_dict[base_name]["audio_file"] = {
                        "filename": entry,
                        "size": size,
                        "size_str": format_file_size(size),
                    }
    
    # Convert to list and sort by base name
    show_groups = list(files_dict.values())
    show_groups.sort(key=lambda x: x["base_name"].lower())
    
    return render_template(
        'light_shows.html',
        page='shows',
        mode_label=label,
        mode_class=css_class,
        mode_token=token,
        show_groups=show_groups,
        auto_refresh=False,
        hostname=socket.gethostname(),
    )


@light_shows_bp.route("/play/<partition>/<filename>")
def play_light_show_audio(partition, filename):
    """Stream a light show audio file."""
    if partition not in USB_PARTITIONS:
        flash("Invalid partition", "error")
        return redirect(url_for("light_shows.light_shows"))
    
    mount_path = get_mount_path(partition)
    if not mount_path:
        flash("Partition not mounted", "error")
        return redirect(url_for("light_shows.light_shows"))
    
    lightshow_dir = os.path.join(mount_path, "LightShow")
    file_path = os.path.join(lightshow_dir, filename)
    
    lower_filename = filename.lower()
    if not os.path.isfile(file_path) or not (lower_filename.endswith(".mp3") or lower_filename.endswith(".wav")):
        flash("File not found", "error")
        return redirect(url_for("light_shows.light_shows"))
    
    # Determine MIME type based on file extension
    if lower_filename.endswith(".wav"):
        mimetype = "audio/wav"
    else:
        mimetype = "audio/mpeg"
    
    return send_file(file_path, mimetype=mimetype)


@light_shows_bp.route("/download/<partition>/<base_name>")
def download_light_show(partition, base_name):
    """Download a light show as a ZIP file containing all related files."""
    if partition not in USB_PARTITIONS:
        flash("Invalid partition", "error")
        return redirect(url_for("light_shows.light_shows"))
    
    mount_path = get_mount_path(partition)
    if not mount_path:
        flash("Partition not mounted", "error")
        return redirect(url_for("light_shows.light_shows"))
    
    # Create the ZIP file
    zip_path, error = create_light_show_zip(base_name, mount_path)
    
    if error:
        flash(error, "error")
        return redirect(url_for("light_shows.light_shows"))
    
    # Send the ZIP file and clean it up after sending
    try:
        return send_file(
            zip_path,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f"{base_name}.zip"
        )
    finally:
        # Clean up the temporary ZIP file after sending
        try:
            if os.path.exists(zip_path):
                os.remove(zip_path)
        except Exception as e:
            logger.error(f"Failed to clean up temporary ZIP file: {e}")


@light_shows_bp.route("/upload", methods=["POST"])
def upload_light_show():
    """Upload a new light show file or ZIP containing light show files."""
    mode = current_mode()
    
    if "show_file" not in request.files:
        flash("No file selected", "error")
        return redirect(url_for("light_shows.light_shows"))
    
    file = request.files["show_file"]
    if file.filename == "":
        flash("No file selected", "error")
        return redirect(url_for("light_shows.light_shows"))
    
    # Get part2 mount path (only needed in edit mode, None is fine for present mode)
    part2_mount_path = get_mount_path("part2") if mode == "edit" else None
    
    # Check if this is a ZIP file
    if file.filename.lower().endswith('.zip'):
        # Handle ZIP file upload
        success, message, file_count = upload_zip_file(file, part2_mount_path)
        
        if success:
            flash(message, "success")
            
            # Refresh Samba shares only if in edit mode
            if mode == "edit":
                try:
                    close_samba_share('gadget_part2')
                    restart_samba_services()
                except Exception as e:
                    flash(f"Files uploaded but Samba refresh failed: {str(e)}", "warning")
            
            # Longer delay for filesystem settling after quick_edit remount
            time.sleep(1.0)
        else:
            flash(message, "error")
    else:
        # Handle individual file upload
        success, message = upload_light_show_file(file, file.filename, part2_mount_path)
        
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
    return redirect(url_for("light_shows.light_shows", _=int(time.time())))


@light_shows_bp.route("/delete/<partition>/<base_name>", methods=["POST"])
def delete_light_show(partition, base_name):
    """Delete both fseq and mp3 files for a light show."""
    mode = current_mode()
    
    if partition not in USB_PARTITIONS:
        flash("Invalid partition", "error")
        return redirect(url_for("light_shows.light_shows"))
    
    # Get part2 mount path (only needed in edit mode, None is fine for present mode)
    part2_mount_path = get_mount_path(partition) if mode == "edit" else None
    
    # Delete the files using the service (mode-aware)
    success, message = delete_light_show_files(base_name, part2_mount_path)
    
    if success:
        flash(message, "success")
        
        # Refresh Samba shares only if in edit mode
        if mode == "edit":
            try:
                close_samba_share('gadget_part2')
                restart_samba_services()
            except Exception as e:
                flash(f"Files deleted but Samba refresh failed: {str(e)}", "warning")
        
        # Small delay for filesystem settling
        time.sleep(0.2)
    else:
        flash(message, "error")
    
    return redirect(url_for("light_shows.light_shows"))
