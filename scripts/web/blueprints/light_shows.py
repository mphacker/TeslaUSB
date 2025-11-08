"""Blueprint for light show management routes."""

import os
import socket
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file

from config import USB_PARTITIONS, PART_LABEL_MAP
from utils import format_file_size
from services.mode_service import mode_display, current_mode
from services.partition_service import get_mount_path, iter_all_partitions

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


@light_shows_bp.route("/upload", methods=["POST"])
def upload_light_show():
    """Upload a new light show file."""
    if current_mode() != "edit":
        flash("Files can only be uploaded in Edit Mode", "error")
        return redirect(url_for("light_shows.light_shows"))
    
    if "show_file" not in request.files:
        flash("No file selected", "error")
        return redirect(url_for("light_shows.light_shows"))
    
    file = request.files["show_file"]
    if file.filename == "":
        flash("No file selected", "error")
        return redirect(url_for("light_shows.light_shows"))
    
    lower_filename = file.filename.lower()
    if not (lower_filename.endswith(".fseq") or lower_filename.endswith(".mp3") or lower_filename.endswith(".wav")):
        flash("Only fseq, mp3, and wav files are allowed", "error")
        return redirect(url_for("light_shows.light_shows"))
    
    # Save to part2 LightShow folder
    mount_path = get_mount_path("part2")
    if not mount_path:
        flash("part2 not mounted", "error")
        return redirect(url_for("light_shows.light_shows"))
    
    lightshow_dir = os.path.join(mount_path, "LightShow")
    os.makedirs(lightshow_dir, exist_ok=True)
    
    filename = os.path.basename(file.filename)
    dest_path = os.path.join(lightshow_dir, filename)
    
    try:
        file.save(dest_path)
        flash(f"Uploaded {filename} successfully", "success")
    except Exception as e:
        flash(f"Failed to upload file: {str(e)}", "error")
    
    return redirect(url_for("light_shows.light_shows"))


@light_shows_bp.route("/delete/<partition>/<base_name>", methods=["POST"])
def delete_light_show(partition, base_name):
    """Delete both fseq and mp3 files for a light show."""
    if current_mode() != "edit":
        flash("Files can only be deleted in Edit Mode", "error")
        return redirect(url_for("light_shows.light_shows"))
    
    if partition not in USB_PARTITIONS:
        flash("Invalid partition", "error")
        return redirect(url_for("light_shows.light_shows"))
    
    mount_path = get_mount_path(partition)
    if not mount_path:
        flash("Partition not mounted", "error")
        return redirect(url_for("light_shows.light_shows"))
    
    lightshow_dir = os.path.join(mount_path, "LightShow")
    
    # Try to delete fseq, mp3, and wav files
    deleted_files = []
    errors = []
    
    for ext in [".fseq", ".mp3", ".wav"]:
        filename = base_name + ext
        file_path = os.path.join(lightshow_dir, filename)
        
        if os.path.isfile(file_path):
            try:
                os.remove(file_path)
                deleted_files.append(filename)
            except Exception as e:
                errors.append(f"{filename}: {str(e)}")
    
    if deleted_files:
        flash(f"Deleted {', '.join(deleted_files)}", "success")
    
    if errors:
        flash(f"Errors: {'; '.join(errors)}", "error")
    
    if not deleted_files and not errors:
        flash("No files found to delete", "error")
    
    return redirect(url_for("light_shows.light_shows"))
