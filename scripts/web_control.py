def close_samba_share(partition_key):
    """Ask Samba to close the relevant share so new files appear immediately."""
    share_name = PART_LABEL_MAP.get(partition_key, f"gadget_{partition_key}")
    commands = [
        ["sudo", "-n", "smbcontrol", "all", "close-share", share_name],
        ["sudo", "-n", "smbcontrol", "all", "reload-config"],
    ]
    for cmd in commands:
        try:
            subprocess.run(cmd, check=False, timeout=5, cwd=GADGET_DIR)
        except Exception:
            pass
#!/usr/bin/env python3
"""
USB Gadget Web Control Interface

A simple Flask web application for controlling USB gadget modes.
Provides buttons to switch between "Present USB" and "Edit USB" modes.
"""

from flask import Flask, render_template_string, redirect, url_for, flash, request
import subprocess
import os
import socket
import wave
import contextlib
import shutil

app = Flask(__name__)
# Configuration (will be updated by setup-usb.sh)
app.secret_key = "__SECRET_KEY__"
GADGET_DIR = "__GADGET_DIR__"
MNT_DIR = "__MNT_DIR__"
STATE_FILE = os.path.join(GADGET_DIR, "state.txt")
LOCK_CHIME_FILENAME = "LockChime.wav"
MAX_LOCK_CHIME_SIZE = 1024 * 1024  # 1 MiB
USB_PARTITIONS = ("part1", "part2")
PART_LABEL_MAP = {"part1": "gadget_part1", "part2": "gadget_part2"}

MODE_DISPLAY = {
    "present": ("USB Gadget Mode", "present"),
    "edit": ("Edit Mode", "edit"),
    "unknown": ("Unknown", "unknown"),
}


def detect_mode():
    """Attempt to infer the current mode when the state file is missing."""
    try:
        result = subprocess.run(
            ["lsmod"], capture_output=True, text=True, check=False, timeout=5
        )
        if result.stdout and "g_mass_storage" in result.stdout:
            return "present"
    except Exception:
        pass

    try:
        for part in USB_PARTITIONS:
            mp = os.path.join(MNT_DIR, part)
            if os.path.ismount(mp):
                return "edit"
    except Exception:
        pass

    return "unknown"


def current_mode():
    """Read the current mode from the state file, falling back when needed."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as state_file:
            token = state_file.read().strip().lower()
            if token in MODE_DISPLAY:
                return token
    except FileNotFoundError:
        pass
    except OSError:
        pass

    return detect_mode()


def mode_display():
    """Return mode metadata and share paths when applicable."""
    token = current_mode()
    label, css_class = MODE_DISPLAY.get(token, MODE_DISPLAY["unknown"])
    share_paths = []

    if token == "edit":
        hostname = socket.gethostname()
        share_paths = [
            f"\\\\{hostname}\\gadget_part1",
            f"\\\\{hostname}\\gadget_part2",
        ]

    return token, label, css_class, share_paths


def lock_chime_ui_available(mode_token):
    """Determine if the lock chime UI should be active."""
    if mode_token == "edit":
        return True
    return any(True for _ in iter_mounted_partitions())


def iter_mounted_partitions():
    """Yield mounted USB partitions and their paths."""
    for part in USB_PARTITIONS:
        mount_path = os.path.join(MNT_DIR, part)
        if os.path.isdir(mount_path):
            yield part, mount_path

HTML_TEMPLATE = """
<!doctype html>
<html>
<head>
    <meta charset='utf-8'>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Tesla USB Gadget Control</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            padding: 20px;
            max-width: 600px;
            margin: 0 auto;
            background-color: #f5f5f5;
        }
        .container {
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        h1 {
            color: #333;
            text-align: center;
            margin-bottom: 30px;
        }
        button {
            padding: 15px 25px;
            margin: 10px;
            border: none;
            border-radius: 5px;
            font-size: 16px;
            cursor: pointer;
            width: 100%;
            max-width: 250px;
            display: block;
            margin: 10px auto;
        }
        .present-btn {
            background-color: #007bff;
            color: white;
        }
        .edit-btn {
            background-color: #28a745;
            color: white;
        }
        button:hover {
            opacity: 0.9;
        }
        .messages {
            margin: 20px 0;
        }
        .success {
            background-color: #d4edda;
            color: #155724;
            padding: 10px;
            border-radius: 5px;
            margin: 5px 0;
        }
        .error {
            background-color: #f8d7da;
            color: #721c24;
            padding: 10px;
            border-radius: 5px;
            margin: 5px 0;
        }
        .info {
            background-color: #e2e3e5;
            color: #383d41;
            padding: 15px;
            border-radius: 5px;
            margin: 20px 0;
            font-size: 14px;
        }
        .status-label {
            text-align: center;
            font-weight: 600;
            margin-bottom: 20px;
            padding: 12px;
            border-radius: 6px;
        }
        .status-label.present {
            background-color: #d4edda;
            color: #155724;
        }
        .status-label.edit {
            background-color: #d1ecf1;
            color: #0c5460;
        }
        .status-label.unknown {
            background-color: #fff3cd;
            color: #856404;
        }
        .shares {
            background-color: #f8f9fa;
            border: 1px solid #d6d8db;
            border-radius: 6px;
            padding: 12px;
            margin-top: 20px;
            font-size: 14px;
        }
        .shares ul {
            margin: 8px 0 0;
            padding-left: 18px;
        }
        .shares code {
            background: #eef0f3;
            padding: 2px 4px;
            border-radius: 4px;
        }
        .lock-chime {
            margin-top: 30px;
            padding: 20px;
            border: 1px solid #d6d8db;
            border-radius: 6px;
            background-color: #f8f9fa;
        }
        .lock-chime h2 {
            margin-top: 0;
            font-size: 20px;
        }
        .lock-chime select {
            width: 100%;
            padding: 10px;
            margin: 10px 0 15px;
            border-radius: 4px;
            border: 1px solid #ced4da;
            font-size: 15px;
        }
        .set-chime-btn {
            background-color: #6f42c1;
            color: white;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🚗 Tesla USB Gadget Control</h1>
        <div class="status-label {{ mode_class }}">Current Mode: {{ mode_label }}</div>
        
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                <div class="messages">
                    {% for cat, msg in messages %}
                        <div class="{{cat}}">{{msg}}</div>
                    {% endfor %}
                </div>
            {% endif %}
        {% endwith %}
        
        <form method="post" action="{{url_for('present_usb')}}">
            <button type="submit" class="present-btn">📱 Present USB Gadget</button>
        </form>
        
        <form method="post" action="{{url_for('edit_usb')}}">
            <button type="submit" class="edit-btn">📁 Edit USB (mount + Samba)</button>
        </form>
        
        <div class="info">
            <strong>Present USB Mode:</strong> Pi appears as USB storage to Tesla<br>
            <strong>Edit USB Mode:</strong> Partitions mounted locally with Samba access
        </div>

        {% if share_paths %}
        <div class="shares">
            <strong>Network Shares:</strong>
            <ul>
                {% for path in share_paths %}
                <li><code>{{ path }}</code></li>
                {% endfor %}
            </ul>
        </div>
        {% endif %}

        {% if show_lock_chime %}
        <div class="lock-chime">
            <h2>Custom Lock Chime</h2>
            {% if not lock_chime_ready %}
            <p>Switch to Edit Mode to manage the custom lock chime.</p>
            {% elif wav_options %}
            <form method="post" action="{{ url_for('set_chime') }}">
                <label for="selected_wav">Choose a WAV file to use as LockChime:</label>
                <select name="selected_wav" id="selected_wav" required>
                    {% for option in wav_options %}
                    <option value="{{ option.value }}">{{ option.label }}</option>
                    {% endfor %}
                </select>
                <button type="submit" class="set-chime-btn">🔔 Set Chime</button>
            </form>
            {% else %}
            <p>No additional WAV files found in the root of gadget_part1 or gadget_part2.</p>
            {% endif %}
        </div>
        {% endif %}
    </div>
</body>
</html>
"""

def run_script(script_name):
    """Execute a script and return success status and message."""
    script_path = os.path.join(GADGET_DIR, script_name)
    
    if not os.path.exists(script_path):
        return False, f"Script not found: {script_name}"
    
    cmd = ["sudo", "-n", script_path]
    env = os.environ.copy()
    env["PATH"] = env.get("PATH", "/usr/bin:/bin")

    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=GADGET_DIR,
            env=env,
        )
        output = (result.stdout or result.stderr or "").strip()
        message = output if output else f"{script_name} executed successfully"
        return True, message
    except subprocess.CalledProcessError as e:
        parts = [getattr(e, "stderr", ""), getattr(e, "stdout", "")]
        combined = "\n".join(part for part in parts if part)
        error_msg = combined.strip() if combined else str(e)

        if "a password is required" in error_msg.lower():
            hint = (
                "Passwordless sudo is required for the web UI. Add an entry such as "
                f"'__TARGET_USER__ ALL=(ALL) NOPASSWD: {script_path}' to sudoers."
            )
            error_msg = f"sudo password required for {script_name}. {hint}"
        return False, f"Error executing {script_name}: {error_msg}"
    except subprocess.TimeoutExpired:
        return False, f"Timeout executing {script_name}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"


def list_available_wavs():
    """Return selectable WAV files in USB roots excluding LockChime."""
    options = []

    for part, mount_path in iter_mounted_partitions():
        try:
            entries = os.listdir(mount_path)
        except OSError:
            continue

        for entry in entries:
            if not entry.lower().endswith(".wav"):
                continue

            if entry.lower() == LOCK_CHIME_FILENAME.lower():
                continue

            full_path = os.path.join(mount_path, entry)

            if os.path.isfile(full_path):
                relative_root = PART_LABEL_MAP.get(part, part)
                label = f"{entry} ({relative_root}: {mount_path})"
                value = f"{part}:{entry}"
                options.append({"label": label, "value": value})

    return sorted(options, key=lambda item: item["label"].lower())


def validate_lock_chime():
    """Validate the custom lock chime file against Tesla requirements."""
    issues = []
    chime_files = []

    for part, mount_path in iter_mounted_partitions():

        try:
            entries = os.listdir(mount_path)
        except OSError as exc:
            issues.append(f"Unable to read contents of {mount_path}: {exc}")
            continue

        matches = [entry for entry in entries if entry.lower() == LOCK_CHIME_FILENAME.lower()]

        for entry in matches:
            full_path = os.path.join(mount_path, entry)
            display_part = PART_LABEL_MAP.get(part, part)

            if not os.path.isfile(full_path):
                issues.append(f"{entry} on {display_part} must be a file, not a directory.")
                continue

            chime_files.append((full_path, entry, display_part))

            if entry != LOCK_CHIME_FILENAME:
                issues.append(
                    f"{entry} on {display_part} must be renamed exactly {LOCK_CHIME_FILENAME}."
                )

    if not chime_files:
        return issues

    if len(chime_files) > 1:
        partitions = ", ".join(part for _, _, part in chime_files)
        issues.append(
            f"Multiple {LOCK_CHIME_FILENAME} files detected on: {partitions}. Only one lock chime may exist across both USB drives."
        )

    for full_path, entry, part in chime_files:
        try:
            size_bytes = os.path.getsize(full_path)
        except OSError as exc:
            issues.append(f"Unable to read size of {entry} on {part}: {exc}")
            continue

        if size_bytes > MAX_LOCK_CHIME_SIZE:
            size_mb = size_bytes / (1024 * 1024)
            issues.append(
                f"{entry} on {part} is {size_mb:.2f} MiB. Tesla requires the file to be 1 MiB or smaller."
            )

        try:
            with contextlib.closing(wave.open(full_path, "rb")) as wav_file:
                wav_file.getparams()
        except (wave.Error, EOFError):
            issues.append(f"{entry} on {part} is not a valid WAV file.")
        except OSError as exc:
            issues.append(f"Unable to read {entry} on {part}: {exc}")

    return issues


def replace_lock_chime(source_path, destination_path):
    """Swap in the selected WAV using same-directory backup semantics."""
    src_size = os.path.getsize(source_path)

    if src_size == 0:
        raise ValueError("Selected WAV file is empty.")

    dest_dir = os.path.dirname(destination_path)
    backup_path = os.path.join(dest_dir, "oldLockChime.wav")

    if os.path.isfile(destination_path):
        if os.path.isfile(backup_path):
            os.remove(backup_path)
        os.rename(destination_path, backup_path)

    try:
        shutil.copyfile(source_path, destination_path)
        dest_size = os.path.getsize(destination_path)
        if dest_size != src_size:
            raise IOError(
                f"Copied file size mismatch (expected {src_size} bytes, got {dest_size} bytes)."
            )
    except Exception:
        if os.path.isfile(backup_path) and not os.path.isfile(destination_path):
            os.rename(backup_path, destination_path)
        raise

    if os.path.isfile(backup_path):
        os.remove(backup_path)

@app.route("/")
def index():
    """Main page with control buttons."""
    token, label, css_class, share_paths = mode_display()
    lock_chime_ready = lock_chime_ui_available(token)
    show_lock_chime = token != "present"
    wav_options = list_available_wavs() if lock_chime_ready else []
    return render_template_string(
        HTML_TEMPLATE,
        mode_label=label,
        mode_class=css_class,
        share_paths=share_paths,
        mode_token=token,
        lock_chime_ready=lock_chime_ready,
        show_lock_chime=show_lock_chime,
        wav_options=wav_options,
    )

@app.route("/present_usb", methods=["POST"])
def present_usb():
    """Switch to USB gadget presentation mode."""
    success, message = run_script("present_usb.sh")
    flash(message, "success" if success else "error")
    return redirect(url_for("index"))

@app.route("/edit_usb", methods=["POST"])
def edit_usb():
    """Switch to edit mode with local mounts and Samba."""
    success, message = run_script("edit_usb.sh")
    flash(message, "success" if success else "error")

    if success:
        lock_chime_issues = validate_lock_chime()
        for issue in lock_chime_issues:
            flash(issue, "error")
    return redirect(url_for("index"))


@app.route("/set_chime", methods=["POST"])
def set_chime():
    """Replace LockChime.wav with a selected WAV file while in edit mode."""
    if current_mode() != "edit":
        flash("Custom lock chime can only be updated while in Edit Mode.", "error")
        return redirect(url_for("index"))

    selection = request.form.get("selected_wav", "").strip()

    if not selection:
        flash("Select a WAV file to set as the lock chime.", "error")
        return redirect(url_for("index"))

    if ":" not in selection:
        flash("Invalid selection for lock chime.", "error")
        return redirect(url_for("index"))

    part, filename = selection.split(":", 1)
    part = part.strip()
    filename = os.path.basename(filename.strip())

    if part not in USB_PARTITIONS or not filename:
        flash("Invalid partition or filename for lock chime selection.", "error")
        return redirect(url_for("index"))

    if not filename.lower().endswith(".wav"):
        flash("Selected file must use the .wav extension.", "error")
        return redirect(url_for("index"))

    source_dir = os.path.join(MNT_DIR, part)
    source_path = os.path.join(source_dir, filename)

    if not os.path.isfile(source_path):
        flash("Selected WAV file is no longer available.", "error")
        return redirect(url_for("index"))

    target_part = part
    target_dir = source_dir

    existing_lock_paths = []
    for usb_part in USB_PARTITIONS:
        candidate = os.path.join(MNT_DIR, usb_part, LOCK_CHIME_FILENAME)
        if os.path.isfile(candidate):
            existing_lock_paths.append((usb_part, candidate))

    if len(existing_lock_paths) > 1:
        flash(
            "Multiple LockChime.wav files detected. Resolve duplicates before updating the custom chime.",
            "error",
        )
        return redirect(url_for("index"))

    backup_info = None

    if existing_lock_paths:
        target_part, existing_path = existing_lock_paths[0]
        target_dir = os.path.dirname(existing_path)
        backup_path = os.path.join(target_dir, "OldLockChime.wav")

        try:
            if os.path.isfile(backup_path):
                os.remove(backup_path)
            shutil.copyfile(existing_path, backup_path)
            backup_info = (existing_path, backup_path)
        except OSError as exc:
            flash(
                "Unable to prepare existing lock chime for replacement. Ensure the USB drive is writable.",
                "error",
            )
            flash(str(exc), "error")
            return redirect(url_for("index"))

    target_path = os.path.join(target_dir, LOCK_CHIME_FILENAME)

    try:
        replace_lock_chime(source_path, target_path)
    except Exception as exc:
        if backup_info and os.path.isfile(backup_info[1]):
            try:
                os.rename(backup_info[1], backup_info[0])
            except OSError as revert_exc:
                flash(
                    "Failed to restore original LockChime after an error. Manual fix required.",
                    "error",
                )
                flash(f"Restore error: {revert_exc}", "error")
            flash(f"Unable to set custom lock chime: {exc}", "error")
            return redirect(url_for("index"))

        flash(f"Unable to set custom lock chime: {exc}", "error")
        return redirect(url_for("index"))

    if backup_info and os.path.isfile(backup_info[1]):
        try:
            os.remove(backup_info[1])
        except OSError as exc:
            flash(
                "Lock chime updated, but unable to delete OldLockChime.wav automatically. Remove it manually.",
                "error",
            )
            flash(str(exc), "error")
            return redirect(url_for("index"))

    close_samba_share(part)

    try:
        subprocess.run(["sync"], check=True, timeout=10)
    except Exception:
        pass

    close_samba_share(part)

    if target_part != part:
        close_samba_share(target_part)
        try:
            subprocess.run(["sync"], check=True, timeout=10)
        except Exception:
            pass
        close_samba_share(target_part)

    flash("Custom lock chime updated successfully.", "success")

    for issue in validate_lock_chime():
        flash(issue, "error")

    return redirect(url_for("index"))

@app.route("/status")
def status():
    """Simple status endpoint for health checks."""
    token, label, css_class, share_paths = mode_display()
    return {
        "status": "running",
        "gadget_dir": GADGET_DIR,
        "mode": token,
        "mode_label": label,
        "mode_class": css_class,
        "share_paths": share_paths,
    }

if __name__ == "__main__":
    print(f"Starting Tesla USB Gadget Web Control")
    print(f"Gadget directory: {GADGET_DIR}")
    print(f"Access the interface at: http://0.0.0.0:__WEB_PORT__/")
    app.run(host="0.0.0.0", port=__WEB_PORT__, debug=False)