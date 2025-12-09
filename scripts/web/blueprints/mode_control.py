"""Blueprint for mode control routes (present/edit mode switching)."""

import os
import socket
import subprocess
from flask import Blueprint, render_template, request, redirect, url_for, flash

from config import GADGET_DIR
from services.mode_service import mode_display
from services.ap_service import ap_status, ap_force, get_ap_config, update_ap_config
from services.wifi_service import get_current_wifi_connection, update_wifi_credentials

mode_control_bp = Blueprint('mode_control', __name__)


@mode_control_bp.route("/")
def index():
    """Main page with control buttons."""
    token, label, css_class, share_paths = mode_display()
    ap = ap_status()
    ap_config = get_ap_config()
    wifi_status = get_current_wifi_connection()
    
    return render_template(
        'index.html',
        page='control',
        mode_label=label,
        mode_class=css_class,
        share_paths=share_paths,
        mode_token=token,
        ap_status=ap,
        ap_config=ap_config,
        wifi_status=wifi_status,
        auto_refresh=False,
        hostname=socket.gethostname(),
    )


@mode_control_bp.route("/present_usb", methods=["POST"])
def present_usb():
    """Switch to USB gadget presentation mode."""
    script_path = os.path.join(GADGET_DIR, "scripts", "present_usb.sh")
    log_path = os.path.join(GADGET_DIR, "present_usb_web.log")
    
    try:
        # Run the script directly with sudo (script has #!/bin/bash shebang)
        with open(log_path, "w") as log:
            result = subprocess.run(
                ["sudo", "-n", script_path],
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=GADGET_DIR,
                timeout=120,  # Increased to 120s - large drives can take time for fsck and mounting
            )
        
        # Check for lock-related errors in the log
        try:
            with open(log_path, "r") as log:
                log_content = log.read()
                if "file operation still in progress" in log_content.lower():
                    flash("Cannot switch modes - file operation in progress. Please wait for uploads/downloads to complete.", "warning")
                    return redirect(url_for("mode_control.index"))
        except Exception:
            pass  # If we can't read the log, continue with normal error handling
            
        if result.returncode == 0:
            flash("Successfully switched to Present Mode", "success")
        else:
            flash(f"Present mode switch completed with warnings. Check {log_path} for details.", "info")
            
    except subprocess.TimeoutExpired:
        flash("Error: Script timed out after 120 seconds", "error")
    except Exception as e:
        flash(f"Error: {str(e)}", "error")
    
    return redirect(url_for("mode_control.index"))


@mode_control_bp.route("/edit_usb", methods=["POST"])
def edit_usb():
    """Switch to edit mode with local mounts and Samba."""
    script_path = os.path.join(GADGET_DIR, "scripts", "edit_usb.sh")
    log_path = os.path.join(GADGET_DIR, "edit_usb_web.log")
    
    try:
        # Run the script directly with sudo (script has #!/bin/bash shebang)
        with open(log_path, "w") as log:
            result = subprocess.run(
                ["sudo", "-n", script_path],
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=GADGET_DIR,
                timeout=120,  # Increased to 120s - unmount retries and gadget removal can take time
            )
        
        # Check for lock-related errors in the log
        try:
            with open(log_path, "r") as log:
                log_content = log.read()
                if "file operation still in progress" in log_content.lower():
                    flash("Cannot switch modes - file operation in progress. Please wait for uploads/downloads to complete.", "warning")
                    return redirect(url_for("mode_control.index"))
        except Exception:
            pass  # If we can't read the log, continue with normal error handling
            
        if result.returncode == 0:
            flash("Successfully switched to Edit Mode", "success")
        else:
            flash(f"Edit mode switch completed with warnings. Check {log_path} for details.", "info")
            
    except subprocess.TimeoutExpired:
        flash("Error: Script timed out after 120 seconds", "error")
    except Exception as e:
        flash(f"Error: {str(e)}", "error")
    
    return redirect(url_for("mode_control.index"))


@mode_control_bp.route("/status")
def status():
    """Simple status endpoint for health checks."""
    token, label, css_class, share_paths = mode_display()
    ap = ap_status()
    return {
        "status": "running",
        "gadget_dir": GADGET_DIR,
        "mode": token,
        "mode_label": label,
        "mode_class": css_class,
        "share_paths": share_paths,
        "ap": ap,
    }


@mode_control_bp.route("/ap/force", methods=["POST"])
def force_ap():
    """Force the fallback AP on/off/auto via web UI."""
    action = request.form.get("mode", "auto")
    allowed = {
        "on": "force-on",
        "off": "force-auto",  # Stop AP and return to auto mode
    }
    if action not in allowed:
        flash("Invalid AP action", "error")
        return redirect(url_for("mode_control.index"))

    try:
        ap_force(allowed[action])
        if action == "on":
            flash("Fallback AP forced on", "success")
        elif action == "off":
            flash("AP stopped. Auto mode enabled - AP will restart if WiFi is unavailable.", "info")
    except Exception as exc:  # noqa: BLE001
        flash(f"Failed to update AP state: {exc}", "error")

    return redirect(url_for("mode_control.index"))


@mode_control_bp.route("/ap/configure", methods=["POST"])
def configure_ap():
    """Update AP SSID and password."""
    ssid = request.form.get("ssid", "").strip()
    passphrase = request.form.get("passphrase", "").strip()
    
    if not ssid:
        flash("SSID cannot be empty", "error")
        return redirect(url_for("mode_control.index"))
    
    try:
        update_ap_config(ssid, passphrase)
        flash(f"AP credentials updated. New SSID: {ssid}. Please reconnect if currently connected to the AP.", "success")
    except ValueError as exc:
        flash(f"Validation error: {exc}", "error")
    except Exception as exc:  # noqa: BLE001
        flash(f"Failed to update AP credentials: {exc}", "error")
    
    return redirect(url_for("mode_control.index"))


@mode_control_bp.route("/wifi/configure", methods=["POST"])
def configure_wifi():
    """Update WiFi client credentials."""
    ssid = request.form.get("wifi_ssid", "").strip()
    password = request.form.get("wifi_password", "").strip()
    
    if not ssid:
        flash("WiFi SSID cannot be empty", "error")
        return redirect(url_for("mode_control.index"))
    
    try:
        result = update_wifi_credentials(ssid, password)
        
        if result.get("success"):
            flash(f"✓ {result.get('message', 'WiFi updated successfully')}", "success")
        else:
            flash(f"⚠ {result.get('message', 'Failed to connect to WiFi network')}", "warning")
            
    except ValueError as exc:
        flash(f"Validation error: {exc}", "error")
    except Exception as exc:  # noqa: BLE001
        flash(f"Error updating WiFi: {exc}", "error")
    
    return redirect(url_for("mode_control.index"))


