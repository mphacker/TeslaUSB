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
    set_active_chime,
    upload_chime_file,
    delete_chime_file,
)
from services.chime_scheduler_service import get_scheduler, get_holidays_list, get_holidays_with_dates, format_schedule_display

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
    
    # Load schedules
    scheduler = get_scheduler()
    schedules = scheduler.list_schedules()
    
    # Get holidays list with dates for current year
    holidays = get_holidays_with_dates()
    
    return render_template(
        'lock_chimes.html',
        page='chimes',
        mode_label=label,
        mode_class=css_class,
        mode_token=token,
        active_chime=active_chime,
        chime_files=chime_files,
        schedules=schedules,
        holidays=holidays,
        format_schedule=format_schedule_display,
        auto_refresh=False,
        expandable=True,  # Allow page to expand beyond viewport for scheduler
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
    
    # Get part2 mount path (may be None in present mode, which is fine)
    part2_mount = get_mount_path("part2")
    
    # Use the service function (works in both modes)
    success, message = upload_chime_file(file, filename, part2_mount)
    
    if success:
        # Force Samba to see the new file (only in Edit mode)
        if current_mode() == "edit":
            try:
                close_samba_share("part2")
                restart_samba_services()
            except Exception:
                pass  # Not critical if Samba refresh fails
        
        # Small delay to let filesystem settle after quick_edit remount
        time.sleep(0.2)
        
        if is_ajax:
            return jsonify({"success": True, "message": message}), 200
        flash(message, "success")
    else:
        if is_ajax:
            return jsonify({"success": False, "error": message}), 400
        flash(message, "error")
    
    return redirect(url_for("lock_chimes.lock_chimes"))


@lock_chimes_bp.route("/set/<filename>", methods=["POST"])
def set_as_chime(filename):
    """Set a WAV file from Chimes folder as the active lock chime."""
    # Sanitize filename
    filename = os.path.basename(filename)
    
    # Get part2 mount path (may be None in present mode, which is fine)
    part2_mount = get_mount_path("part2")
    
    # Use the service function (works in both modes)
    success, message = set_active_chime(filename, part2_mount)
    
    if success:
        # Force Samba to see the change (only in Edit mode)
        if current_mode() == "edit":
            try:
                close_samba_share("part2")
                restart_samba_services()
            except Exception:
                pass  # Not critical if Samba refresh fails
        
        # Small delay to let filesystem settle after quick_edit remount
        time.sleep(0.2)
        
        flash(message, "success")
    else:
        flash(message, "error")
    
    # Add timestamp to force browser cache refresh
    return redirect(url_for("lock_chimes.lock_chimes", _=int(time.time())))


@lock_chimes_bp.route("/delete/<filename>", methods=["POST"])
def delete_lock_chime(filename):
    """Delete a lock chime file from Chimes folder."""
    # Sanitize filename
    filename = os.path.basename(filename)
    
    # Get part2 mount path (may be None in present mode, which is fine)
    part2_mount = get_mount_path("part2")
    
    # Use the service function (works in both modes)
    success, message = delete_chime_file(filename, part2_mount)
    
    if success:
        # Force Samba to see the change (only in Edit mode)
        if current_mode() == "edit":
            try:
                close_samba_share("part2")
                restart_samba_services()
            except Exception:
                pass  # Not critical if Samba refresh fails
        
        # Small delay to let filesystem settle after quick_edit remount
        time.sleep(0.2)
        
        flash(message, "success")
    else:
        flash(message, "error")
    
    return redirect(url_for("lock_chimes.lock_chimes"))


# ============================================================================
# Chime Scheduler Routes
# ============================================================================

@lock_chimes_bp.route("/schedule/add", methods=["POST"])
def add_schedule():
    """Add a new chime schedule."""
    try:
        # Get form data
        schedule_name = request.form.get('schedule_name', '').strip()
        chime_filename = request.form.get('chime_filename', '').strip()
        schedule_type = request.form.get('schedule_type', 'weekly').strip()
        
        # Get time - for holidays, default to 12:00 AM
        if schedule_type == 'holiday':
            hour_24 = 0
            minute = '00'
        else:
            hour_12 = int(request.form.get('hour', '12'))
            minute = request.form.get('minute', '00')
            am_pm = request.form.get('am_pm', 'AM').upper()
            
            # Convert to 24-hour format
            if am_pm == 'PM' and hour_12 != 12:
                hour_24 = hour_12 + 12
            elif am_pm == 'AM' and hour_12 == 12:
                hour_24 = 0
            else:
                hour_24 = hour_12
        
        time_str = f"{hour_24:02d}:{minute}"
        
        enabled = request.form.get('enabled') == 'true'
        
        # Validate inputs
        if not schedule_name:
            flash("Schedule name is required", "error")
            return redirect(url_for("lock_chimes.lock_chimes"))
        
        if not chime_filename:
            flash("Please select a chime", "error")
            return redirect(url_for("lock_chimes.lock_chimes"))
        
        # Type-specific validation and parameter gathering
        params = {
            'chime_filename': chime_filename,
            'time_str': time_str,
            'schedule_type': schedule_type,
            'name': schedule_name,
            'enabled': enabled
        }
        
        if schedule_type == 'weekly':
            days = request.form.getlist('days')
            if not days:
                flash("Please select at least one day", "error")
                return redirect(url_for("lock_chimes.lock_chimes"))
            params['days'] = days
        
        elif schedule_type == 'date':
            month = request.form.get('month')
            day = request.form.get('day')
            if not month or not day:
                flash("Please select a month and day", "error")
                return redirect(url_for("lock_chimes.lock_chimes"))
            params['month'] = int(month)
            params['day'] = int(day)
        
        elif schedule_type == 'holiday':
            holiday = request.form.get('holiday', '').strip()
            if not holiday:
                flash("Please select a holiday", "error")
                return redirect(url_for("lock_chimes.lock_chimes"))
            params['holiday'] = holiday
        
        else:
            flash(f"Invalid schedule type: {schedule_type}", "error")
            return redirect(url_for("lock_chimes.lock_chimes"))
        
        # Add schedule
        scheduler = get_scheduler()
        success, message, schedule_id = scheduler.add_schedule(**params)
        
        if success:
            flash(f"Schedule '{schedule_name}' created successfully", "success")
        else:
            flash(f"Failed to create schedule: {message}", "error")
    
    except Exception as e:
        flash(f"Error adding schedule: {str(e)}", "error")
    
    return redirect(url_for("lock_chimes.lock_chimes"))


@lock_chimes_bp.route("/schedule/<int:schedule_id>/toggle", methods=["POST"])
def toggle_schedule(schedule_id):
    """Enable or disable a schedule."""
    try:
        scheduler = get_scheduler()
        schedule = scheduler.get_schedule(schedule_id)
        
        if not schedule:
            flash("Schedule not found", "error")
            return redirect(url_for("lock_chimes.lock_chimes"))
        
        # Toggle enabled state
        new_enabled = not schedule.get('enabled', True)
        success, message = scheduler.update_schedule(schedule_id, enabled=new_enabled)
        
        if success:
            status = "enabled" if new_enabled else "disabled"
            flash(f"Schedule '{schedule['name']}' {status}", "success")
        else:
            flash(f"Failed to update schedule: {message}", "error")
    
    except Exception as e:
        flash(f"Error toggling schedule: {str(e)}", "error")
    
    return redirect(url_for("lock_chimes.lock_chimes"))


@lock_chimes_bp.route("/schedule/<int:schedule_id>/delete", methods=["POST"])
def delete_schedule(schedule_id):
    """Delete a schedule."""
    try:
        scheduler = get_scheduler()
        schedule = scheduler.get_schedule(schedule_id)
        
        if not schedule:
            flash("Schedule not found", "error")
            return redirect(url_for("lock_chimes.lock_chimes"))
        
        success, message = scheduler.delete_schedule(schedule_id)
        
        if success:
            flash(f"Schedule '{schedule['name']}' deleted", "success")
        else:
            flash(f"Failed to delete schedule: {message}", "error")
    
    except Exception as e:
        flash(f"Error deleting schedule: {str(e)}", "error")
    
    return redirect(url_for("lock_chimes.lock_chimes"))


@lock_chimes_bp.route("/schedule/<int:schedule_id>/edit", methods=["GET", "POST"])
def edit_schedule(schedule_id):
    """Edit an existing schedule (GET returns JSON, POST updates)."""
    scheduler = get_scheduler()
    schedule = scheduler.get_schedule(schedule_id)
    
    if not schedule:
        if request.method == "GET":
            return jsonify({"success": False, "error": "Schedule not found"}), 404
        else:
            flash("Schedule not found", "error")
            return redirect(url_for("lock_chimes.lock_chimes"))
    
    if request.method == "GET":
        # Convert 24-hour time to 12-hour format
        time_str = schedule.get('time', '00:00')
        try:
            time_parts = time_str.split(':')
            hour_24 = int(time_parts[0])
            minute = int(time_parts[1])
            
            am_pm = 'AM' if hour_24 < 12 else 'PM'
            hour_12 = hour_24 % 12
            if hour_12 == 0:
                hour_12 = 12
            
            schedule_data = {
                "id": schedule['id'],
                "name": schedule['name'],
                "chime_filename": schedule['chime_filename'],
                "time": time_str,
                "hour_12": hour_12,
                "minute": f"{minute:02d}",
                "am_pm": am_pm,
                "schedule_type": schedule.get('schedule_type', 'weekly'),
                "enabled": schedule.get('enabled', True)
            }
            
            # Add type-specific fields
            if schedule_data['schedule_type'] == 'weekly':
                schedule_data['days'] = schedule.get('days', [])
            elif schedule_data['schedule_type'] == 'date':
                schedule_data['month'] = schedule.get('month', 1)
                schedule_data['day'] = schedule.get('day', 1)
            elif schedule_data['schedule_type'] == 'holiday':
                schedule_data['holiday'] = schedule.get('holiday', '')
            
            return jsonify({
                "success": True,
                "schedule": schedule_data
            })
        except (ValueError, IndexError):
            return jsonify({"success": False, "error": "Invalid time format in schedule"}), 500
    
    # POST - Update schedule
    try:
        # Get form data
        schedule_name = request.form.get('schedule_name', '').strip()
        chime_filename = request.form.get('chime_filename', '').strip()
        schedule_type = request.form.get('schedule_type', 'weekly').strip()
        
        # Get time - for holidays, default to 12:00 AM
        if schedule_type == 'holiday':
            hour_24 = 0
            minute = '00'
        else:
            hour_12 = int(request.form.get('hour', '12'))
            minute = request.form.get('minute', '00')
            am_pm = request.form.get('am_pm', 'AM').upper()
            
            # Convert to 24-hour format
            if am_pm == 'PM' and hour_12 != 12:
                hour_24 = hour_12 + 12
            elif am_pm == 'AM' and hour_12 == 12:
                hour_24 = 0
            else:
                hour_24 = hour_12
        
        time_str = f"{hour_24:02d}:{minute}"
        
        enabled = request.form.get('enabled') == 'true'
        
        # Validate inputs
        if not schedule_name:
            flash("Schedule name is required", "error")
            return redirect(url_for("lock_chimes.lock_chimes"))
        
        if not chime_filename:
            flash("Please select a chime", "error")
            return redirect(url_for("lock_chimes.lock_chimes"))
        
        # Type-specific validation and parameter gathering
        params = {
            'chime_filename': chime_filename,
            'time': time_str,
            'schedule_type': schedule_type,
            'name': schedule_name,
            'enabled': enabled
        }
        
        if schedule_type == 'weekly':
            days = request.form.getlist('days')
            if not days:
                flash("Please select at least one day", "error")
                return redirect(url_for("lock_chimes.lock_chimes"))
            params['days'] = days
        
        elif schedule_type == 'date':
            month = request.form.get('month')
            day = request.form.get('day')
            if not month or not day:
                flash("Please select a month and day", "error")
                return redirect(url_for("lock_chimes.lock_chimes"))
            params['month'] = int(month)
            params['day'] = int(day)
        
        elif schedule_type == 'holiday':
            holiday = request.form.get('holiday', '').strip()
            if not holiday:
                flash("Please select a holiday", "error")
                return redirect(url_for("lock_chimes.lock_chimes"))
            params['holiday'] = holiday
        
        else:
            flash(f"Invalid schedule type: {schedule_type}", "error")
            return redirect(url_for("lock_chimes.lock_chimes"))
        
        # Update schedule
        success, message = scheduler.update_schedule(schedule_id=schedule_id, **params)
        
        if success:
            flash(f"Schedule '{schedule_name}' updated successfully", "success")
        else:
            flash(f"Failed to update schedule: {message}", "error")
    
    except Exception as e:
        flash(f"Error updating schedule: {str(e)}", "error")
    
    return redirect(url_for("lock_chimes.lock_chimes"))

