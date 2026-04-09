"""Blueprint for mode control routes (present/edit mode switching)."""

import os
import subprocess
import time
import logging
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify

from config import GADGET_DIR
from utils import get_base_context
from services.mode_service import mode_display
from services.ap_service import ap_status, ap_force, get_ap_config, update_ap_config
from services.wifi_service import get_current_wifi_connection, update_wifi_credentials, get_available_networks, get_wifi_status, clear_wifi_status, get_saved_networks, forget_network, reorder_networks

mode_control_bp = Blueprint('mode_control', __name__, url_prefix='/settings')

logger = logging.getLogger(__name__)


def _trigger_auto_index_after_mode_switch():
    """Trigger background video indexing after a mode switch if enabled."""
    try:
        from config import (
            MAPPING_ENABLED, MAPPING_INDEX_ON_MODE_SWITCH, MAPPING_DB_PATH,
            MAPPING_SAMPLE_RATE, MAPPING_EVENT_THRESHOLDS, MAPPING_TRIP_GAP_MINUTES,
        )
        if not MAPPING_ENABLED or not MAPPING_INDEX_ON_MODE_SWITCH:
            return
        from services.video_service import get_teslacam_path
        from services.mapping_service import trigger_auto_index
        teslacam = get_teslacam_path()
        if teslacam:
            trigger_auto_index(
                db_path=MAPPING_DB_PATH,
                teslacam_path=teslacam,
                sample_rate=MAPPING_SAMPLE_RATE,
                thresholds=MAPPING_EVENT_THRESHOLDS,
                trip_gap_minutes=MAPPING_TRIP_GAP_MINUTES,
            )
    except Exception as e:
        logger.warning("Auto-index after mode switch failed: %s", e)


def _trigger_cloud_sync_after_mode_switch():
    """Trigger cloud archive sync after a mode switch if enabled."""
    try:
        from config import CLOUD_ARCHIVE_ENABLED, CLOUD_ARCHIVE_DB_PATH
        if not CLOUD_ARCHIVE_ENABLED:
            return
        from services.video_service import get_teslacam_path
        from services.cloud_archive_service import trigger_auto_sync
        teslacam = get_teslacam_path()
        if teslacam:
            trigger_auto_sync(teslacam, CLOUD_ARCHIVE_DB_PATH)
    except Exception as e:
        logger.warning("Cloud sync after mode switch failed: %s", e)


def _get_system_info():
    """Gather device information for the System settings section."""
    import socket
    import platform

    info = {
        'hostname': socket.gethostname(),
        'platform': platform.machine(),
        'python': platform.python_version(),
    }

    # IP addresses
    try:
        result = subprocess.run(
            ['hostname', '-I'], capture_output=True, text=True, timeout=3
        )
        info['ip_addresses'] = result.stdout.strip().split() if result.returncode == 0 else []
    except Exception:
        info['ip_addresses'] = []

    # Uptime
    try:
        with open('/proc/uptime', 'r') as f:
            secs = float(f.read().split()[0])
        days, rem = divmod(int(secs), 86400)
        hours, rem = divmod(rem, 3600)
        mins = rem // 60
        parts = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0:
            parts.append(f"{hours}h")
        parts.append(f"{mins}m")
        info['uptime'] = ' '.join(parts)
    except Exception:
        info['uptime'] = 'unknown'

    # Disk image sizes
    from config import IMG_CAM_PATH, IMG_LIGHTSHOW_PATH, IMG_MUSIC_PATH, MUSIC_ENABLED
    images = [
        ('TeslaCam', IMG_CAM_PATH),
        ('LightShow', IMG_LIGHTSHOW_PATH),
    ]
    if MUSIC_ENABLED:
        images.append(('Music', IMG_MUSIC_PATH))
    info['disk_images'] = []
    for label, path in images:
        try:
            size = os.path.getsize(path)
            info['disk_images'].append({
                'label': label,
                'size_gb': round(size / (1024 ** 3), 1),
            })
        except OSError:
            pass

    # Git version
    try:
        result = subprocess.run(
            ['git', '-c', f'safe.directory={GADGET_DIR}',
             '--no-pager', 'log', '--oneline', '-1'],
            capture_output=True, text=True, timeout=3,
            cwd=GADGET_DIR,
        )
        if result.returncode == 0 and result.stdout.strip():
            info['version'] = result.stdout.strip()
        else:
            info['version'] = 'unknown'
    except Exception:
        info['version'] = 'unknown'

    # Memory
    try:
        with open('/proc/meminfo', 'r') as f:
            meminfo = {}
            for line in f:
                k, _, v = line.partition(':')
                if v:
                    meminfo[k.strip()] = v.strip()
            total_kb = int(meminfo.get('MemTotal', '0').split()[0])
            avail_kb = int(meminfo.get('MemAvailable', '0').split()[0])
            info['mem_total_mb'] = round(total_kb / 1024)
            info['mem_avail_mb'] = round(avail_kb / 1024)
    except Exception:
        info['mem_total_mb'] = 0
        info['mem_avail_mb'] = 0

    return info


@mode_control_bp.route("/")
def index():
    """Main page with control buttons."""
    start_time = time.time()
    timings = {}

    # Measure get_base_context (includes mode_display)
    t0 = time.time()
    ctx = get_base_context()
    timings['mode_display'] = time.time() - t0

    # Measure ap_status
    t0 = time.time()
    ap = ap_status()
    timings['ap_status'] = time.time() - t0

    # Measure get_ap_config
    t0 = time.time()
    ap_config = get_ap_config()
    timings['get_ap_config'] = time.time() - t0

    # Measure get_current_wifi_connection
    t0 = time.time()
    wifi_status = get_current_wifi_connection()
    timings['wifi_status'] = time.time() - t0

    # Get any pending WiFi change status (for displaying alerts)
    wifi_change_status = get_wifi_status()

    total_time = time.time() - start_time
    timings['total'] = total_time

    # Log performance metrics
    logger.info(f"Settings page load times: mode={timings['mode_display']:.3f}s, "
                f"ap_status={timings['ap_status']:.3f}s, "
                f"ap_config={timings['get_ap_config']:.3f}s, "
                f"wifi={timings['wifi_status']:.3f}s, "
                f"total={total_time:.3f}s")

    return render_template(
        'index.html',
        page='settings',
        **ctx,
        ap_status=ap,
        ap_config=ap_config,
        wifi_status=wifi_status,
        wifi_change_status=wifi_change_status,
        system_info=_get_system_info(),
        auto_refresh=False,
        # Config settings for editable sections
        cfg_archive=_get_archive_config(),
        cfg_mapping=_get_mapping_config(),
        cfg_network=_get_network_config(),
    )

@mode_control_bp.route("/present_usb", methods=["POST"])
def present_usb():
    """Switch to USB gadget presentation mode."""
    script_path = os.path.join(GADGET_DIR, "scripts", "present_usb.sh")
    log_path = os.path.join(GADGET_DIR, "present_usb_web.log")

    try:
        with open(log_path, "w") as log:
            result = subprocess.run(
                ["sudo", "-n", "bash", script_path],
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
            # Auto-index videos after switching to present mode
            _trigger_auto_index_after_mode_switch()
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
        with open(log_path, "w") as log:
            result = subprocess.run(
                ["sudo", "-n", "bash", script_path],
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
            # Auto-index videos after switching to edit mode
            _trigger_auto_index_after_mode_switch()
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
    ctx = get_base_context()
    ap = ap_status()
    return {
        "status": "running",
        "gadget_dir": GADGET_DIR,
        "mode": ctx['mode_token'],
        "mode_label": ctx['mode_label'],
        "mode_class": ctx['mode_class'],
        "share_paths": ctx['share_paths'],
        "ap": ap,
    }


@mode_control_bp.route("/ap/force", methods=["POST"])
def force_ap():
    """Force the fallback AP on/off/auto via web UI.

    - Start AP Now: Sets force-on mode (persists across reboot)
    - Stop AP: Returns to auto mode (persists, AP only starts if WiFi fails)
    """
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
            flash("AP forced on - will remain on even after reboot", "success")
        elif action == "off":
            flash("AP stopped and auto mode restored - AP will only start if WiFi becomes unavailable", "info")
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


@mode_control_bp.route("/wifi/scan", methods=["GET"])
def scan_wifi_networks():
    """Scan for available WiFi networks and return as JSON."""
    try:
        networks = get_available_networks()
        return {
            "success": True,
            "networks": networks,
        }
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Error scanning WiFi networks: {exc}")
        return {
            "success": False,
            "error": str(exc),
            "networks": [],
        }


@mode_control_bp.route("/wifi/dismiss-status", methods=["POST"])
def dismiss_wifi_status():
    """Dismiss the WiFi change status alert."""
    clear_wifi_status()
    return {"success": True}


@mode_control_bp.route("/api/wifi/saved")
def api_wifi_saved():
    """List saved WiFi networks with signal and priority."""
    try:
        return jsonify(get_saved_networks())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@mode_control_bp.route("/api/wifi/reorder", methods=["POST"])
def api_wifi_reorder():
    """Reorder WiFi network priorities."""
    data = request.get_json(silent=True) or {}
    networks = data.get("networks", [])
    if not networks:
        return jsonify({"success": False, "message": "No networks provided"}), 400
    try:
        result = reorder_networks(networks)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@mode_control_bp.route("/api/wifi/forget", methods=["POST"])
def api_wifi_forget():
    """Forget a saved WiFi network."""
    data = request.get_json(silent=True) or {}
    name = data.get("name", "")
    if not name:
        return jsonify({"success": False, "message": "No network name provided"}), 400
    try:
        result = forget_network(name)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@mode_control_bp.route("/api/wifi/scan")
def api_wifi_scan():
    """Scan for available WiFi networks (JSON API)."""
    try:
        return jsonify(get_available_networks(rescan=True))
    except Exception:
        return jsonify([])


# ---------------------------------------------------------------------------
# Settings Config Helpers
# ---------------------------------------------------------------------------

def _get_archive_config() -> dict:
    """Read current archive settings for the settings page."""
    from config import (
        ARCHIVE_ENABLED, ARCHIVE_ONLY_DRIVING,
        ARCHIVE_RETENTION_DAYS, ARCHIVE_MIN_FREE_SPACE_GB,
    )
    return {
        'enabled': ARCHIVE_ENABLED,
        'only_driving': ARCHIVE_ONLY_DRIVING,
        'retention_days': ARCHIVE_RETENTION_DAYS,
        'min_free_space_gb': ARCHIVE_MIN_FREE_SPACE_GB,
    }


def _get_mapping_config() -> dict:
    """Read current mapping settings for the settings page."""
    from config import (
        MAPPING_ENABLED, MAPPING_ARCHIVE_INDEXING,
        MAPPING_TRIP_GAP_MINUTES, MAPPING_EVENT_THRESHOLDS,
    )
    return {
        'enabled': MAPPING_ENABLED,
        'archive_indexing': MAPPING_ARCHIVE_INDEXING,
        'trip_gap_minutes': MAPPING_TRIP_GAP_MINUTES,
        'speed_limit_mph': round(MAPPING_EVENT_THRESHOLDS.get('speed_limit_mps', 35.76) * 2.237, 0),
    }


def _get_network_config() -> dict:
    """Read current network settings for the settings page."""
    from config import config
    return {
        'samba_password': config.get('network', {}).get('samba_password', ''),
    }


# ---------------------------------------------------------------------------
# Settings Update Routes
# ---------------------------------------------------------------------------

@mode_control_bp.route("/save/archive", methods=["POST"])
def save_archive_settings():
    """Save archive settings from the settings page."""
    from helpers.config_updater import update_config_yaml

    try:
        updates = {
            'archive.enabled': 'enabled' in request.form,
            'archive.only_driving': 'only_driving' in request.form,
            'archive.retention_days': max(1, int(request.form.get('retention_days', 30))),
            'archive.min_free_space_gb': max(1, int(request.form.get('min_free_space_gb', 10))),
        }
        update_config_yaml(updates)
        flash("Archive settings saved. Restart service to apply.", "success")
    except (ValueError, TypeError) as e:
        flash(f"Invalid value: {e}", "danger")
    except Exception as e:
        flash(f"Failed to save: {e}", "danger")

    return redirect(url_for('mode_control.index'))


@mode_control_bp.route("/save/mapping", methods=["POST"])
def save_mapping_settings():
    """Save mapping/indexing settings from the settings page."""
    from helpers.config_updater import update_config_yaml

    try:
        speed_mph = float(request.form.get('speed_limit_mph', 80))
        speed_mps = round(speed_mph / 2.237, 2)

        updates = {
            'mapping.enabled': 'enabled' in request.form,
            'mapping.archive_indexing': 'archive_indexing' in request.form,
            'mapping.trip_gap_minutes': max(1, int(request.form.get('trip_gap_minutes', 5))),
            'mapping.event_detection.speed_limit_mps': speed_mps,
        }
        update_config_yaml(updates)
        flash("Mapping settings saved. Restart service to apply.", "success")
    except (ValueError, TypeError) as e:
        flash(f"Invalid value: {e}", "danger")
    except Exception as e:
        flash(f"Failed to save: {e}", "danger")

    return redirect(url_for('mode_control.index'))


@mode_control_bp.route("/save/network", methods=["POST"])
def save_network_settings():
    """Save network settings (Samba password) from the settings page."""
    from helpers.config_updater import update_config_yaml

    password = request.form.get('samba_password', '').strip()
    if not password:
        flash("Samba password cannot be empty.", "danger")
        return redirect(url_for('mode_control.index'))

    try:
        update_config_yaml({'network.samba_password': password})
        # Update Samba user password live
        result = subprocess.run(
            ["sudo", "-n", "bash", "-c",
             f"echo -e '{password}\\n{password}' | smbpasswd -s -a pi"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            flash("Samba password updated.", "success")
        else:
            flash("Password saved to config but Samba update failed. Run setup_usb.sh to apply.", "warning")
    except Exception as e:
        flash(f"Failed to save: {e}", "danger")

    return redirect(url_for('mode_control.index'))
