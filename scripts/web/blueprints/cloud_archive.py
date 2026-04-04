"""Blueprint for cloud archive management routes."""

import os
import logging
import subprocess

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify

from config import (
    CONFIG_YAML,
    GADGET_DIR,
    CLOUD_ARCHIVE_ENABLED,
    CLOUD_ARCHIVE_PROVIDER,
    CLOUD_ARCHIVE_REMOTE_PATH,
    CLOUD_ARCHIVE_SYNC_FOLDERS,
    CLOUD_ARCHIVE_PRIORITY_ORDER,
    CLOUD_ARCHIVE_MAX_UPLOAD_MBPS,
    CLOUD_ARCHIVE_KEEP_LOCAL,
    CLOUD_PROVIDER_CREDS_PATH,
)
from utils import get_base_context

cloud_archive_bp = Blueprint('cloud_archive', __name__, url_prefix='/cloud')
logger = logging.getLogger(__name__)


@cloud_archive_bp.before_request
def _require_cloud_archive():
    if not CLOUD_ARCHIVE_ENABLED:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"error": "Cloud archive not enabled"}), 503
        flash("Cloud archive is not enabled. Enable it in config.yaml.", "warning")
        return redirect(url_for('mode_control.index'))


# ---------------------------------------------------------------------------
# Helper: atomic config.yaml update
# ---------------------------------------------------------------------------

def _update_config_yaml(updates: dict):
    """Atomically update config.yaml with new values.

    Args:
        updates: Dict of dotted-key paths to new values,
                 e.g. ``{'cloud_archive.max_upload_mbps': 10}``.
    """
    import yaml

    with open(CONFIG_YAML, 'r') as f:
        cfg = yaml.safe_load(f)

    for key, value in updates.items():
        keys = key.split('.')
        d = cfg
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = value

    tmp_path = CONFIG_YAML + '.tmp'
    with open(tmp_path, 'w') as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, CONFIG_YAML)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@cloud_archive_bp.route('/')
def index():
    """Main cloud archive dashboard."""
    from services.cloud_archive_service import (
        get_sync_status,
        get_sync_stats,
        get_sync_history,
    )

    try:
        sync_status = get_sync_status()
        sync_stats = get_sync_stats(CLOUD_ARCHIVE_DB_PATH)
        sync_history = get_sync_history(CLOUD_ARCHIVE_DB_PATH)
    except Exception:
        logger.exception("Failed to load cloud archive data")
        sync_status = {}
        sync_stats = {}
        sync_history = []

    ctx = get_base_context()
    return render_template(
        'cloud_archive.html',
        page='cloud',
        sync_status=sync_status,
        sync_stats=sync_stats,
        sync_history=sync_history,
        provider=CLOUD_ARCHIVE_PROVIDER,
        sync_folders=CLOUD_ARCHIVE_SYNC_FOLDERS,
        priority_order=CLOUD_ARCHIVE_PRIORITY_ORDER,
        max_upload_mbps=CLOUD_ARCHIVE_MAX_UPLOAD_MBPS,
        remote_path=CLOUD_ARCHIVE_REMOTE_PATH,
        keep_local=CLOUD_ARCHIVE_KEEP_LOCAL,
        **ctx,
    )


# ---------------------------------------------------------------------------
# Form endpoints
# ---------------------------------------------------------------------------

@cloud_archive_bp.route('/settings', methods=['POST'])
def save_settings():
    """Save cloud sync settings from form submission."""
    try:
        sync_folders = request.form.getlist('sync_folders')
        priority_raw = request.form.get('priority_order', '')
        priority_order = [p.strip() for p in priority_raw.split(',') if p.strip()]
        max_upload_mbps = int(request.form.get('max_upload_mbps', 5))

        _update_config_yaml({
            'cloud_archive.sync_folders': sync_folders,
            'cloud_archive.priority_order': priority_order,
            'cloud_archive.max_upload_mbps': max_upload_mbps,
        })

        flash("Cloud sync settings saved.", "success")
        logger.info("Cloud sync settings updated: folders=%s, priority=%s, bw=%d Mbps",
                     sync_folders, priority_order, max_upload_mbps)
    except Exception:
        logger.exception("Failed to save cloud sync settings")
        flash("Error saving cloud sync settings.", "danger")

    return redirect(url_for('cloud_archive.index'))


# ---------------------------------------------------------------------------
# AJAX API endpoints
# ---------------------------------------------------------------------------

@cloud_archive_bp.route('/api/sync_now', methods=['POST'])
def api_sync_now():
    """Trigger a manual cloud sync."""
    from services.cloud_archive_service import start_sync
    from services.video_service import get_teslacam_path

    try:
        teslacam = get_teslacam_path()
        if not teslacam:
            return jsonify({"success": False, "message": "TeslaCam path not available"}), 400
        ok, msg = start_sync(teslacam, CLOUD_ARCHIVE_DB_PATH, trigger='manual')
        return jsonify({"success": ok, "message": msg})
    except Exception as exc:
        logger.exception("Failed to start cloud sync")
        return jsonify({"success": False, "message": str(exc)}), 500


@cloud_archive_bp.route('/api/sync_stop', methods=['POST'])
def api_sync_stop():
    """Stop a running cloud sync."""
    from services.cloud_archive_service import stop_sync

    try:
        ok, msg = stop_sync()
        return jsonify({"success": ok, "message": msg})
    except Exception as exc:
        logger.exception("Failed to stop cloud sync")
        return jsonify({"success": False, "message": str(exc)}), 500


@cloud_archive_bp.route('/api/status')
def api_status():
    """Return current sync status and stats for UI polling."""
    from services.cloud_archive_service import get_sync_status, get_sync_stats

    try:
        status = get_sync_status()
        stats = get_sync_stats(CLOUD_ARCHIVE_DB_PATH)
        return jsonify({"status": status, "stats": stats})
    except Exception as exc:
        logger.exception("Failed to fetch sync status")
        return jsonify({"error": str(exc)}), 500


@cloud_archive_bp.route('/api/history')
def api_history():
    """Return sync session history."""
    from services.cloud_archive_service import get_sync_history

    try:
        history = get_sync_history(CLOUD_ARCHIVE_DB_PATH)
        return jsonify({"history": history})
    except Exception as exc:
        logger.exception("Failed to fetch sync history")
        return jsonify({"error": str(exc)}), 500


@cloud_archive_bp.route('/api/test_connection', methods=['POST'])
def api_test_connection():
    """Test connectivity to the configured rclone remote."""
    try:
        rclone_conf = os.path.join(GADGET_DIR, 'rclone.conf')
        result = subprocess.run(
            ['rclone', 'lsd', '--config', rclone_conf, 'teslausb:'],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info("Cloud connection test succeeded")
            return jsonify({"success": True, "message": "Connection successful."})
        logger.warning("Cloud connection test failed: %s", result.stderr.strip())
        return jsonify({"success": False, "message": result.stderr.strip()}), 400
    except subprocess.TimeoutExpired:
        logger.warning("Cloud connection test timed out")
        return jsonify({"success": False, "message": "Connection timed out."}), 504
    except Exception as exc:
        logger.exception("Cloud connection test error")
        return jsonify({"success": False, "message": str(exc)}), 500


@cloud_archive_bp.route('/api/provider', methods=['POST'])
def api_save_provider():
    """Save cloud provider credentials (encrypted)."""
    import json

    data = request.get_json(silent=True)
    if not data or 'provider' not in data or 'credentials' not in data:
        return jsonify({"success": False, "message": "Missing provider or credentials."}), 400

    provider = data['provider']
    credentials = data['credentials']

    try:
        # Encrypt credentials using the hardware-bound key (same as Tesla tokens)
        from services.tesla_api_service import derive_encryption_key
        from cryptography.fernet import Fernet
        import json as _json

        key = derive_encryption_key()
        fernet = Fernet(key)
        encrypted = fernet.encrypt(_json.dumps(credentials).encode())

        os.makedirs(os.path.dirname(CLOUD_PROVIDER_CREDS_PATH) or '.', exist_ok=True)
        tmp = CLOUD_PROVIDER_CREDS_PATH + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(encrypted)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CLOUD_PROVIDER_CREDS_PATH)

        _update_config_yaml({'cloud_archive.provider': provider})

        logger.info("Cloud provider updated to %s", provider)
        return jsonify({"success": True})
    except Exception as exc:
        logger.exception("Failed to save cloud provider credentials")
        return jsonify({"success": False, "message": str(exc)}), 500
