"""Blueprint for lock chime management routes."""

import os
import socket
import subprocess
import time
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, jsonify

from config import GADGET_DIR, LOCK_CHIME_FILENAME, CHIMES_FOLDER, MAX_LOCK_CHIME_SIZE
from utils import format_file_size
from services.mode_service import mode_display, current_mode
from services.partition_service import get_mount_path
from services.samba_service import close_samba_share, restart_samba_services
from services.lock_chime_service import (
    validate_tesla_wav,
    reencode_wav_for_tesla,
    replace_lock_chime,
)

lock_chimes_bp = Blueprint('lock_chimes', __name__, url_prefix='/lock_chimes')


@lock_chimes_bp.route("/")
def lock_chimes():
    """Lock chimes management page."""
    token, label, css_class, share_paths = mode_display()
    
    # Get current active chime from part2 root
    active_chime = None
    part2_mount = get_mount_path("part2")
    
    if part2_mount:
        active_chime_path = os.path.join(part2_mount, LOCK_CHIME_FILENAME)
        if os.path.isfile(active_chime_path):
            size = os.path.getsize(active_chime_path)
            mtime = int(os.path.getmtime(active_chime_path))
            active_chime = {
                "filename": LOCK_CHIME_FILENAME,
                "size": size,
                "size_str": format_file_size(size),
                "mtime": mtime,
            }
    
    # Get all WAV files from Chimes folder on part2
    chime_files = []
    if part2_mount:
        chimes_dir = os.path.join(part2_mount, CHIMES_FOLDER)
        if os.path.isdir(chimes_dir):
            try:
                entries = os.listdir(chimes_dir)
                for entry in entries:
                    if not entry.lower().endswith(".wav"):
                        continue
                    
                    full_path = os.path.join(chimes_dir, entry)
                    if os.path.isfile(full_path):
                        size = os.path.getsize(full_path)
                        mtime = int(os.path.getmtime(full_path))
                        
                        # Validate the file
                        is_valid, msg = validate_tesla_wav(full_path)
                        
                        chime_files.append({
                            "filename": entry,
                            "size": size,
                            "size_str": format_file_size(size),
                            "mtime": mtime,
                            "is_valid": is_valid,
                            "validation_msg": msg,
                        })
            except OSError:
                pass
    
    # Sort alphabetically
    chime_files.sort(key=lambda x: x["filename"].lower())
    
    return render_template(
        'lock_chimes.html',
        page='chimes',
        mode_label=label,
        mode_class=css_class,
        mode_token=token,
        active_chime=active_chime,
        chime_files=chime_files,
        auto_refresh=False,
        hostname=socket.gethostname(),
    )


@lock_chimes_bp.route("/play/active")
def play_active_chime():
    """Stream the active LockChime.wav file from part2 root."""
    part2_mount = get_mount_path("part2")
    if not part2_mount:
        flash("Partition not mounted", "error")
        return redirect(url_for("lock_chimes.lock_chimes"))
    
    file_path = os.path.join(part2_mount, LOCK_CHIME_FILENAME)
    if not os.path.isfile(file_path):
        flash("Active lock chime not found", "error")
        return redirect(url_for("lock_chimes.lock_chimes"))
    
    return send_file(file_path, mimetype="audio/wav")


@lock_chimes_bp.route("/play/<filename>")
def play_lock_chime(filename):
    """Stream a lock chime WAV file from the Chimes folder."""
    part2_mount = get_mount_path("part2")
    if not part2_mount:
        flash("Partition not mounted", "error")
        return redirect(url_for("lock_chimes.lock_chimes"))
    
    # Sanitize filename
    filename = os.path.basename(filename)
    
    chimes_dir = os.path.join(part2_mount, CHIMES_FOLDER)
    file_path = os.path.join(chimes_dir, filename)
    
    if not os.path.isfile(file_path) or not filename.lower().endswith(".wav"):
        flash("File not found", "error")
        return redirect(url_for("lock_chimes.lock_chimes"))
    
    return send_file(file_path, mimetype="audio/wav")


@lock_chimes_bp.route("/download/<filename>")
def download_lock_chime(filename):
    """Download a lock chime WAV file from the Chimes folder."""
    part2_mount = get_mount_path("part2")
    if not part2_mount:
        flash("Partition not mounted", "error")
        return redirect(url_for("lock_chimes.lock_chimes"))
    
    # Sanitize filename
    filename = os.path.basename(filename)
    
    chimes_dir = os.path.join(part2_mount, CHIMES_FOLDER)
    file_path = os.path.join(chimes_dir, filename)
    
    if not os.path.isfile(file_path) or not filename.lower().endswith(".wav"):
        flash("File not found", "error")
        return redirect(url_for("lock_chimes.lock_chimes"))
    
    return send_file(file_path, mimetype="audio/wav", as_attachment=True, download_name=filename)


@lock_chimes_bp.route("/upload", methods=["POST"])
def upload_lock_chime():
    """Upload a new lock chime WAV or MP3 file."""
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    
    if current_mode() != "edit":
        if is_ajax:
            return jsonify({"success": False, "error": "Files can only be uploaded in Edit Mode"}), 400
        flash("Files can only be uploaded in Edit Mode", "error")
        return redirect(url_for("lock_chimes.lock_chimes"))
    
    if "chime_file" not in request.files:
        if is_ajax:
            return jsonify({"success": False, "error": "No file selected"}), 400
        flash("No file selected", "error")
        return redirect(url_for("lock_chimes.lock_chimes"))
    
    file = request.files["chime_file"]
    if file.filename == "":
        if is_ajax:
            return jsonify({"success": False, "error": "No file selected"}), 400
        flash("No file selected", "error")
        return redirect(url_for("lock_chimes.lock_chimes"))
    
    # Check file extension - allow WAV and MP3
    file_ext = os.path.splitext(file.filename.lower())[1]
    if file_ext not in [".wav", ".mp3"]:
        if is_ajax:
            return jsonify({"success": False, "error": "Only WAV and MP3 files are allowed"}), 400
        flash("Only WAV and MP3 files are allowed", "error")
        return redirect(url_for("lock_chimes.lock_chimes"))
    
    # Final filename will always be .wav
    filename = os.path.splitext(os.path.basename(file.filename))[0] + ".wav"
    
    # Prevent uploading a file named LockChime.wav
    if filename.lower() == LOCK_CHIME_FILENAME.lower():
        if is_ajax:
            return jsonify({"success": False, "error": "Cannot upload a file named LockChime.wav. Please rename your file."}), 400
        flash("Cannot upload a file named LockChime.wav. Please rename your file.", "error")
        return redirect(url_for("lock_chimes.lock_chimes"))
    
    # Get part2 mount path
    part2_mount = get_mount_path("part2")
    if not part2_mount:
        if is_ajax:
            return jsonify({"success": False, "error": "part2 not mounted"}), 500
        flash("part2 not mounted", "error")
        return redirect(url_for("lock_chimes.lock_chimes"))
    
    # Verify mount is writable by checking if it's the read-only path
    if part2_mount.endswith("-ro"):
        if is_ajax:
            return jsonify({"success": False, "error": "System is in Present mode with read-only access. Switch to Edit mode to upload files."}), 400
        flash("System is in Present mode with read-only access. Switch to Edit mode to upload files.", "error")
        return redirect(url_for("lock_chimes.lock_chimes"))
    
    # Save to Chimes folder
    chimes_dir = os.path.join(part2_mount, CHIMES_FOLDER)
    if not os.path.isdir(chimes_dir):
        try:
            os.makedirs(chimes_dir, exist_ok=True)
        except OSError as e:
            if is_ajax:
                return jsonify({"success": False, "error": f"Cannot create Chimes directory (filesystem may be read-only): {str(e)}"}), 500
            flash(f"Cannot create Chimes directory (filesystem may be read-only): {str(e)}", "error")
            return redirect(url_for("lock_chimes.lock_chimes"))
    
    dest_path = os.path.join(chimes_dir, filename)
    
    try:
        # Save to temporary location first (use underscore to avoid multiple dots issue on FAT/exFAT)
        temp_path = dest_path.replace('.wav', '_upload.wav')
        file.save(temp_path)
        
        # For MP3 files, we need to convert to WAV first before validation
        if file_ext == ".mp3":
            # Convert MP3 to temporary WAV for processing
            mp3_temp_path = temp_path
            temp_path = dest_path.replace('.wav', '_converted.wav')
            
            # Use FFmpeg to convert MP3 to WAV
            try:
                cmd = [
                    "ffmpeg", "-i", mp3_temp_path,
                    "-acodec", "pcm_s16le",  # 16-bit PCM
                    "-ar", "44100",          # 44.1 kHz
                    "-ac", "1",              # Mono
                    "-y",                    # Overwrite output
                    temp_path
                ]
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=30
                )
                
                # Remove the MP3 temp file
                os.remove(mp3_temp_path)
                
                if result.returncode != 0:
                    stderr_output = result.stderr.decode('utf-8', errors='ignore')
                    raise RuntimeError(f"FFmpeg MP3 conversion failed: {stderr_output}")
                    
            except subprocess.TimeoutExpired:
                if os.path.exists(mp3_temp_path):
                    os.remove(mp3_temp_path)
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                if is_ajax:
                    return jsonify({"success": False, "error": "MP3 conversion timed out (file too long?)"}), 500
                flash("MP3 conversion timed out (file too long?)", "error")
                return redirect(url_for("lock_chimes.lock_chimes"))
            except Exception as e:
                if os.path.exists(mp3_temp_path):
                    os.remove(mp3_temp_path)
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                if is_ajax:
                    return jsonify({"success": False, "error": f"MP3 conversion failed: {str(e)}"}), 500
                flash(f"MP3 conversion failed: {str(e)}", "error")
                return redirect(url_for("lock_chimes.lock_chimes"))
        
        # Validate the uploaded/converted file
        is_valid, validation_msg = validate_tesla_wav(temp_path)
        
        if not is_valid:
            # Try to re-encode the file to meet Tesla's requirements (use simple temp name with .wav extension)
            reencoded_path = dest_path.replace('.wav', '_reenc.wav')
            
            # Track progress messages (for AJAX, we return these in the response)
            progress_messages = []
            def progress_callback(msg):
                progress_messages.append(msg)
            
            success, reencode_msg, details = reencode_wav_for_tesla(temp_path, reencoded_path, progress_callback)
            
            if success:
                # Re-encoding successful, validate the re-encoded file
                is_valid_reencoded, validation_msg_reencoded = validate_tesla_wav(reencoded_path)
                
                if is_valid_reencoded:
                    # Use the re-encoded file
                    os.remove(temp_path)
                    temp_path = reencoded_path
                    
                    # Build user-friendly message
                    strategy_desc = details.get('strategy', 'Tesla-compatible format')
                    size_info = details.get('size_mb', 'under 1 MB')
                    
                    if is_ajax:
                        return jsonify({
                            "success": True, 
                            "message": f"Uploaded {filename} successfully!\n\nFile was automatically re-encoded to {strategy_desc} with final size of {size_info} MB.",
                            "reencoded": True,
                            "details": details,
                            "progress": progress_messages
                        }), 200
                else:
                    # Re-encoded file still doesn't meet requirements
                    os.remove(temp_path)
                    if os.path.exists(reencoded_path):
                        os.remove(reencoded_path)
                    if is_ajax:
                        return jsonify({"success": False, "error": f"Re-encoded file failed validation: {validation_msg_reencoded}"}), 400
                    flash(f"Re-encoded file failed validation: {validation_msg_reencoded}", "error")
                    return redirect(url_for("lock_chimes.lock_chimes"))
            else:
                # Re-encoding failed
                os.remove(temp_path)
                if os.path.exists(reencoded_path):
                    os.remove(reencoded_path)
                if is_ajax:
                    return jsonify({
                        "success": False, 
                        "error": f"Invalid WAV file: {validation_msg}.\n\nRe-encoding failed: {reencode_msg}",
                        "progress": progress_messages
                    }), 400
                flash(f"Invalid WAV file: {validation_msg}. Re-encoding failed: {reencode_msg}", "error")
                return redirect(url_for("lock_chimes.lock_chimes"))
        
        # Move to final location
        if os.path.exists(dest_path):
            os.remove(dest_path)
        os.rename(temp_path, dest_path)
        
        # Sync to ensure file is written
        subprocess.run(["sync"], check=False, timeout=5)
        
        # Force Samba to see the new file
        close_samba_share("part2")
        restart_samba_services()
        
        if is_ajax:
            return jsonify({"success": True, "message": f"Uploaded {filename} successfully"}), 200
        flash(f"Uploaded {filename} successfully", "success")
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        reencoded_path = dest_path + ".reencoded.tmp"
        if os.path.exists(reencoded_path):
            os.remove(reencoded_path)
        if is_ajax:
            return jsonify({"success": False, "error": f"Failed to upload file: {str(e)}"}), 500
        flash(f"Failed to upload file: {str(e)}", "error")
    
    return redirect(url_for("lock_chimes.lock_chimes"))


@lock_chimes_bp.route("/set/<filename>", methods=["POST"])
def set_as_chime(filename):
    """Set a WAV file from Chimes folder as the active lock chime."""
    if current_mode() != "edit":
        flash("Lock chime can only be updated in Edit Mode", "error")
        return redirect(url_for("lock_chimes.lock_chimes"))
    
    # Sanitize filename
    filename = os.path.basename(filename)
    
    part2_mount = get_mount_path("part2")
    if not part2_mount:
        flash("Partition not mounted", "error")
        return redirect(url_for("lock_chimes.lock_chimes"))
    
    chimes_dir = os.path.join(part2_mount, CHIMES_FOLDER)
    source_path = os.path.join(chimes_dir, filename)
    
    if not os.path.isfile(source_path):
        flash("Source file not found in Chimes folder", "error")
        return redirect(url_for("lock_chimes.lock_chimes"))
    
    # Validate before setting
    is_valid, validation_msg = validate_tesla_wav(source_path)
    if not is_valid:
        flash(f"Cannot set as chime: {validation_msg}", "error")
        return redirect(url_for("lock_chimes.lock_chimes"))
    
    target_path = os.path.join(part2_mount, LOCK_CHIME_FILENAME)
    
    close_samba_share("part2")
    
    try:
        replace_lock_chime(source_path, target_path)
        
        # Additional sync after all operations
        subprocess.run(["sync"], check=False, timeout=10)
        time.sleep(2)
        
        # Drop caches one more time to ensure Samba/web sees fresh data
        try:
            subprocess.run(
                ["sudo", "-n", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
                check=False,
                timeout=5
            )
        except Exception:
            pass
        
        restart_samba_services()
        time.sleep(3)
        close_samba_share("part2")
        
        flash(f"Set {filename} as active lock chime", "success")
    except Exception as e:
        flash(f"Failed to set lock chime: {str(e)}", "error")
    
    # Add timestamp to force browser cache refresh
    return redirect(url_for("lock_chimes.lock_chimes", _=int(time.time())))


@lock_chimes_bp.route("/delete/<filename>", methods=["POST"])
def delete_lock_chime(filename):
    """Delete a lock chime file from Chimes folder."""
    if current_mode() != "edit":
        flash("Files can only be deleted in Edit Mode", "error")
        return redirect(url_for("lock_chimes.lock_chimes"))
    
    # Sanitize filename
    filename = os.path.basename(filename)
    
    part2_mount = get_mount_path("part2")
    if not part2_mount:
        flash("Partition not mounted", "error")
        return redirect(url_for("lock_chimes.lock_chimes"))
    
    chimes_dir = os.path.join(part2_mount, CHIMES_FOLDER)
    file_path = os.path.join(chimes_dir, filename)
    
    if not os.path.isfile(file_path):
        flash("File not found", "error")
        return redirect(url_for("lock_chimes.lock_chimes"))
    
    try:
        os.remove(file_path)
        flash(f"Deleted {filename}", "success")
    except Exception as e:
        flash(f"Failed to delete file: {str(e)}", "error")
    
    return redirect(url_for("lock_chimes.lock_chimes"))
