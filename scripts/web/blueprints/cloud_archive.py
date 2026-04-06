"""Blueprint for cloud archive management routes."""

import os
import logging

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
    CLOUD_ARCHIVE_DB_PATH,
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

    # Re-read dynamic settings from config.yaml (may have been updated at runtime)
    import yaml
    _provider = CLOUD_ARCHIVE_PROVIDER
    _sync_folders = CLOUD_ARCHIVE_SYNC_FOLDERS
    _priority_order = CLOUD_ARCHIVE_PRIORITY_ORDER
    _max_upload_mbps = CLOUD_ARCHIVE_MAX_UPLOAD_MBPS
    _remote_path = CLOUD_ARCHIVE_REMOTE_PATH
    _keep_local = CLOUD_ARCHIVE_KEEP_LOCAL
    try:
        with open(CONFIG_YAML, 'r') as f:
            _cfg = yaml.safe_load(f) or {}
        _cloud = _cfg.get('cloud_archive', {})
        _provider = _cloud.get('provider', '') or _provider
        _sync_folders = _cloud.get('sync_folders', _sync_folders)
        _priority_order = _cloud.get('priority_order', _priority_order)
        _max_upload_mbps = int(_cloud.get('max_upload_mbps', _max_upload_mbps))
        _remote_path = _cloud.get('remote_path', _remote_path)
        _keep_local = bool(_cloud.get('keep_local_after_upload', _keep_local))
        _sync_enabled = bool(_cloud.get('sync_enabled', True))
    except Exception:
        _sync_enabled = True
        pass
    provider_connected = bool(_provider) and os.path.isfile(CLOUD_PROVIDER_CREDS_PATH)

    # Get token expiry for connected providers
    _token_expiry = None
    if provider_connected:
        try:
            from services.cloud_rclone_service import get_connection_status
            _conn = get_connection_status()
            _token_expiry = _conn.get("token_expiry")
        except Exception:
            pass

    ctx = get_base_context()
    return render_template(
        'cloud_archive.html',
        page='cloud',
        sync_status=sync_status,
        sync_stats=sync_stats,
        sync_history=sync_history,
        provider=_provider,
        provider_connected=provider_connected,
        token_expiry=_token_expiry,
        sync_enabled=_sync_enabled,
        sync_folders=_sync_folders,
        priority_order=_priority_order,
        max_upload_mbps=_max_upload_mbps,
        remote_path=_remote_path,
        keep_local=_keep_local,
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
    """Stop a running cloud sync.

    Accepts JSON: { "graceful": true } (default) or { "graceful": false }.
    Graceful=true finishes the current file; false kills immediately.
    """
    from services.cloud_archive_service import stop_sync

    data = request.get_json(silent=True) or {}
    graceful = data.get('graceful', True)

    try:
        ok, msg = stop_sync(graceful=graceful)
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



@cloud_archive_bp.route('/api/provider', methods=['POST'])
def api_save_provider():
    """Save cloud provider selection to config.yaml."""
    data = request.get_json(silent=True)
    if not data or 'provider' not in data:
        return jsonify({"success": False, "message": "Missing provider."}), 400

    provider = data['provider']
    try:
        _update_config_yaml({'cloud_archive.provider': provider})
        logger.info("Cloud provider set to %s", provider)
        return jsonify({"success": True})
    except Exception as exc:
        logger.exception("Failed to save provider selection")
        return jsonify({"success": False, "message": str(exc)}), 500


# ---------------------------------------------------------------------------
# rclone authorize token paste endpoints
# ---------------------------------------------------------------------------

@cloud_archive_bp.route('/api/connect', methods=['POST'])
def api_connect_provider():
    """Save rclone authorize token for a cloud provider.

    Expects JSON: { "provider": "onedrive", "token": "<pasted blob>" }
    """
    from services.cloud_rclone_service import (
        parse_rclone_token, save_credentials, PROVIDERS,
    )

    data = request.get_json(silent=True) or {}
    provider = data.get('provider', '')
    token_raw = data.get('token', '')

    if not provider or not token_raw:
        return jsonify({"success": False,
                        "message": "Missing provider or token."}), 400

    if provider not in PROVIDERS:
        return jsonify({"success": False,
                        "message": f"Unknown provider: {provider}"}), 400

    try:
        token = parse_rclone_token(token_raw)
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 400

    try:
        save_credentials(provider, token)
        _update_config_yaml({'cloud_archive.provider': provider})
        return jsonify({"success": True, "message": "Connected successfully."})
    except Exception as exc:
        logger.exception("Failed to save cloud credentials for %s", provider)
        return jsonify({"success": False, "message": str(exc)}), 500


@cloud_archive_bp.route('/api/disconnect', methods=['POST'])
def api_disconnect_provider():
    """Remove stored cloud credentials."""
    from services.cloud_rclone_service import remove_credentials

    try:
        remove_credentials()
        _update_config_yaml({'cloud_archive.provider': ''})
        return jsonify({"success": True})
    except Exception as exc:
        logger.exception("Failed to disconnect cloud provider")
        return jsonify({"success": False, "message": str(exc)}), 500


@cloud_archive_bp.route('/api/test_connection', methods=['POST'])
def api_test_connection():
    """Test connectivity to the configured cloud provider."""
    from services.cloud_rclone_service import test_connection

    try:
        ok, msg = test_connection()
        auth_error = msg.startswith("AUTH_ERROR:") if not ok else False
        display_msg = msg.replace("AUTH_ERROR: ", "") if auth_error else msg
        if ok:
            logger.info("Cloud connection test succeeded")
            return jsonify({"success": True, "message": display_msg})
        logger.warning("Cloud connection test failed: %s", msg)
        return jsonify({"success": False, "message": display_msg,
                        "auth_error": auth_error}), 400
    except Exception as exc:
        logger.exception("Cloud connection test error")
        return jsonify({"success": False, "message": str(exc)}), 500


@cloud_archive_bp.route('/api/connection_status')
def api_connection_status():
    """Return current provider connection status."""
    from services.cloud_rclone_service import get_connection_status

    try:
        return jsonify(get_connection_status())
    except Exception as exc:
        logger.exception("Failed to get connection status")
        return jsonify({"connected": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Folder browsing & creation
# ---------------------------------------------------------------------------

@cloud_archive_bp.route('/api/browse')
def api_browse_folders():
    """List folders at a given path on the connected cloud provider."""
    from services.cloud_rclone_service import list_folders

    path = request.args.get('path', '')

    try:
        ok, data = list_folders(path)
        if ok:
            return jsonify({"success": True, "folders": data, "path": path})
        auth_error = isinstance(data, str) and data.startswith("AUTH_ERROR:")
        display_msg = data.replace("AUTH_ERROR: ", "") if auth_error else data
        return jsonify({"success": False, "message": display_msg,
                        "auth_error": auth_error}), 400
    except Exception as exc:
        logger.exception("Folder browse error")
        return jsonify({"success": False, "message": str(exc)}), 500


@cloud_archive_bp.route('/api/mkdir', methods=['POST'])
def api_create_folder():
    """Create a new folder on the connected cloud provider."""
    from services.cloud_rclone_service import create_folder

    data = request.get_json(silent=True) or {}
    path = data.get('path', '')
    if not path:
        return jsonify({"success": False, "message": "Folder path required."}), 400

    try:
        ok, msg = create_folder(path)
        if ok:
            return jsonify({"success": True, "message": msg})
        return jsonify({"success": False, "message": msg}), 400
    except Exception as exc:
        logger.exception("Folder creation error")
        return jsonify({"success": False, "message": str(exc)}), 500


@cloud_archive_bp.route('/api/set_remote_path', methods=['POST'])
def api_set_remote_path():
    """Set the cloud sync destination folder path."""
    data = request.get_json(silent=True) or {}
    path = data.get('path', '')

    try:
        _update_config_yaml({'cloud_archive.remote_path': path or 'TeslaUSB'})
        logger.info("Cloud remote path set to: %s", path or 'TeslaUSB')
        return jsonify({"success": True, "path": path or 'TeslaUSB'})
    except Exception as exc:
        logger.exception("Failed to set remote path")
        return jsonify({"success": False, "message": str(exc)}), 500


@cloud_archive_bp.route('/api/toggle_sync', methods=['POST'])
def api_toggle_sync():
    """Enable or disable automatic cloud sync."""
    data = request.get_json(silent=True) or {}
    enabled = data.get('enabled', True)

    try:
        _update_config_yaml({'cloud_archive.sync_enabled': bool(enabled)})
        logger.info("Cloud sync %s", "enabled" if enabled else "disabled")
        return jsonify({"success": True, "sync_enabled": bool(enabled)})
    except Exception as exc:
        logger.exception("Failed to toggle sync")
        return jsonify({"success": False, "message": str(exc)}), 500


# ---------------------------------------------------------------------------
# Single-file archive (from video panel)
# ---------------------------------------------------------------------------

@cloud_archive_bp.route('/api/archive_file', methods=['POST'])
def api_archive_file():
    """Archive a single video file/event to the cloud."""
    from services.cloud_rclone_service import archive_file
    from services.video_service import get_teslacam_path

    data = request.get_json(silent=True) or {}
    folder = data.get('folder', '')  # e.g. "SentryClips"
    event_name = data.get('event', '')  # e.g. "2025-01-15_14-30-45"
    filename = data.get('filename', '')  # specific file, or empty for whole event

    if not folder or not event_name:
        return jsonify({"success": False, "message": "Missing folder or event."}), 400

    teslacam = get_teslacam_path()
    if not teslacam:
        return jsonify({"success": False, "message": "TeslaCam path not available."}), 400

    if filename:
        local_path = os.path.join(teslacam, folder, event_name, filename)
    else:
        # Archive the whole event folder — pick the front camera as primary
        event_dir = os.path.join(teslacam, folder, event_name)
        if not os.path.isdir(event_dir):
            # Flat structure
            local_path = os.path.join(teslacam, folder, event_name)
        else:
            local_path = event_dir

    # For directories, we need a different approach — archive each file
    if os.path.isdir(local_path):
        # Find all video files in the event
        files = [f for f in os.listdir(local_path)
                 if f.lower().endswith(('.mp4', '.ts'))]
        if not files:
            return jsonify({"success": False, "message": "No video files found."}), 400
        # Archive the first file (front camera typically), user can archive more
        local_path = os.path.join(local_path, files[0])

    if not os.path.isfile(local_path):
        return jsonify({"success": False, "message": "File not found."}), 404

    try:
        ok, msg = archive_file(local_path, teslacam)
        if ok:
            return jsonify({"success": True, "message": msg})
        return jsonify({"success": False, "message": msg}), 400
    except Exception as exc:
        logger.exception("Failed to start archive")
        return jsonify({"success": False, "message": str(exc)}), 500


@cloud_archive_bp.route('/api/archive_status')
def api_archive_status():
    """Return current single-file archive status."""
    from services.cloud_rclone_service import get_archive_status

    try:
        return jsonify(get_archive_status())
    except Exception as exc:
        logger.exception("Failed to get archive status")
        return jsonify({"running": False, "error": str(exc)}), 500


@cloud_archive_bp.route('/api/archive_cancel', methods=['POST'])
def api_archive_cancel():
    """Cancel an in-progress single-file archive."""
    from services.cloud_rclone_service import cancel_archive

    try:
        ok, msg = cancel_archive()
        return jsonify({"success": ok, "message": msg})
    except Exception as exc:
        logger.exception("Failed to cancel archive")
        return jsonify({"success": False, "message": str(exc)}), 500
