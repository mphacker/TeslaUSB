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
    """Atomically update config.yaml — delegates to shared helper."""
    from helpers.config_updater import update_config_yaml
    update_config_yaml(updates)
    # Invalidate cached config
    _cloud_config_cache.clear()


# Cache for config.yaml reads (avoids disk I/O on every page load)
_cloud_config_cache: dict = {}


def _get_cloud_config_cached() -> dict:
    """Return cloud_archive section from config.yaml, cached for 30s."""
    import time
    now = time.time()
    if _cloud_config_cache.get('ts', 0) + 30 > now:
        return _cloud_config_cache.get('data', {})
    import yaml
    try:
        with open(CONFIG_YAML, 'r') as f:
            cfg = yaml.safe_load(f) or {}
        data = cfg.get('cloud_archive', {})
        _cloud_config_cache['data'] = data
        _cloud_config_cache['ts'] = now
        # Piggyback the live_event_sync section on the same disk read so
        # the cloud archive page can render LES settings without a
        # second yaml.load() per pageview.
        _cloud_config_cache['les_data'] = cfg.get('live_event_sync', {}) or {}
        return data
    except Exception:
        return _cloud_config_cache.get('data', {})


def _get_les_config_cached() -> dict:
    """Return live_event_sync section from config.yaml, cached for 30s.

    Always cache-coherent with ``_get_cloud_config_cached()`` because
    they share the same dict; calling either one refreshes both.
    """
    if 'les_data' not in _cloud_config_cache:
        # Force a populate.
        _get_cloud_config_cached()
    return _cloud_config_cache.get('les_data', {})


def _resolve_keep_clips_until_synced(cloud_cfg: dict) -> bool:
    """Translate ``cloud_archive.delete_unsynced`` into the UI toggle state.

    The web UI exposes a positive-framed toggle ("Keep clips until
    backed up to cloud") which is the inverse of the backend
    ``delete_unsynced`` boolean. When the YAML key is unset
    (``None``), fall back to the same auto-default the watchdog uses:
    ``True`` when a cloud provider is configured, ``False`` otherwise.
    """
    raw = cloud_cfg.get('delete_unsynced', None) if cloud_cfg else None
    if raw is None:
        provider_set = bool(CLOUD_ARCHIVE_PROVIDER) and os.path.isfile(
            CLOUD_PROVIDER_CREDS_PATH
        )
        return provider_set
    return not bool(raw)


def _get_last_prune_kept_unsynced_count() -> int:
    """Return the count of clips held back at the most recent prune.

    Cheap (in-memory state lookup). Returns 0 when the watchdog has
    not yet run a prune or its module is unavailable.
    """
    try:
        from services import archive_watchdog
        status = archive_watchdog.get_status()
        return int(
            status.get('retention', {}).get('last_prune_kept_unsynced', 0)
        )
    except Exception:  # noqa: BLE001
        return 0


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

    # Re-read dynamic settings from config.yaml (cached for 30s to reduce I/O)
    import yaml
    _provider = CLOUD_ARCHIVE_PROVIDER
    _sync_folders = CLOUD_ARCHIVE_SYNC_FOLDERS
    _priority_order = CLOUD_ARCHIVE_PRIORITY_ORDER
    _max_upload_mbps = CLOUD_ARCHIVE_MAX_UPLOAD_MBPS
    _remote_path = CLOUD_ARCHIVE_REMOTE_PATH
    _sync_enabled = True
    try:
        _cloud = _get_cloud_config_cached()
        _provider = _cloud.get('provider', '') or _provider
        _sync_folders = _cloud.get('sync_folders', _sync_folders)
        _priority_order = _cloud.get('priority_order', _priority_order)
        _max_upload_mbps = int(_cloud.get('max_upload_mbps', _max_upload_mbps))
        _remote_path = _cloud.get('remote_path', _remote_path)
        _sync_enabled = bool(_cloud.get('sync_enabled', True))
    except Exception:
        pass
    provider_connected = bool(_provider) and os.path.isfile(CLOUD_PROVIDER_CREDS_PATH)

    # Get token expiry for connected providers — but SKIP when sync is
    # running because get_connection_status() spawns rclone which competes
    # for RAM on the Pi Zero (464MB) and can trigger the watchdog.
    _token_expiry = None
    if provider_connected and not sync_status.get("running"):
        try:
            from services.cloud_rclone_service import get_connection_status
            _conn = get_connection_status()
            _token_expiry = _conn.get("token_expiry")
        except Exception:
            pass

    ctx = get_base_context()

    # Load Live Event Sync (LES) config from config.yaml so the LES card
    # in the cloud sync page can render with current values. Reads come
    # from the same 30s cache that backs cloud_archive settings.
    _les_cfg = _get_les_config_cached()
    les_enabled = bool(_les_cfg.get('enabled', False))
    les_watch_folders = list(_les_cfg.get('watch_folders', ['SentryClips', 'SavedClips']))
    les_upload_scope = str(_les_cfg.get('upload_scope', 'event_minute'))
    les_retry_max_attempts = int(_les_cfg.get('retry_max_attempts', 5))
    les_retry_backoff_seconds = list(
        _les_cfg.get('retry_backoff_seconds', [30, 120, 300, 900, 3600])
    )
    les_daily_data_cap_mb = int(_les_cfg.get('daily_data_cap_mb', 0))
    les_notify_webhook_url = str(_les_cfg.get('notify_webhook_url', '') or '')

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
        cloud_reserve_gb=_cloud.get('cloud_reserve_gb', 1),
        sync_non_event_videos=bool(_cloud.get('sync_non_event_videos', False)),
        cloud_auto_cleanup=bool(_cloud.get('cloud_auto_cleanup', False)),
        cloud_min_retention_days=int(_cloud.get('cloud_min_retention_days', 30)),
        # Phase 2.6 — bulk cloud sync retry cap. Default 5, range 1-20.
        # Settings save writes to ``cloud_archive.retry_max_attempts``;
        # the worker re-reads the value on every failure so a Settings
        # change takes effect on the next iteration without restart.
        cloud_retry_max_attempts=int(_cloud.get('retry_max_attempts', 5)),
        # Phase 1 item 1.3 — retention-respects-cloud toggle + counter.
        # ``keep_clips_until_synced`` is the UI-friendly inversion of the
        # backend ``cloud_archive.delete_unsynced`` config key.
        keep_clips_until_synced=_resolve_keep_clips_until_synced(_cloud),
        kept_unsynced_count=_get_last_prune_kept_unsynced_count(),
        les_enabled=les_enabled,
        les_watch_folders=les_watch_folders,
        les_upload_scope=les_upload_scope,
        les_retry_max_attempts=les_retry_max_attempts,
        les_retry_backoff_seconds=les_retry_backoff_seconds,
        les_daily_data_cap_mb=les_daily_data_cap_mb,
        les_notify_webhook_url=les_notify_webhook_url,
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
        cloud_reserve_gb = max(0, float(request.form.get('cloud_reserve_gb', 1)))
        sync_non_event = 'sync_non_event_videos' in request.form
        auto_cleanup = 'cloud_auto_cleanup' in request.form
        min_retention = max(1, int(request.form.get('cloud_min_retention_days', 30)))
        # Phase 2.6 — clamp to 1-20 to match the UI input min/max and the
        # service's _RETRY_MAX_ATTEMPTS_MIN/MAX. A value outside this
        # range from a hand-crafted form submission falls back to the
        # default (5) rather than disabling the cap entirely.
        try:
            _raw_retry = int(request.form.get('cloud_retry_max_attempts', 5))
        except (TypeError, ValueError):
            _raw_retry = 5
        cloud_retry_max_attempts = max(1, min(20, _raw_retry))

        config_updates = {
            'cloud_archive.sync_folders': sync_folders,
            'cloud_archive.priority_order': priority_order,
            'cloud_archive.max_upload_mbps': max_upload_mbps,
            'cloud_archive.cloud_reserve_gb': cloud_reserve_gb,
            'cloud_archive.sync_non_event_videos': sync_non_event,
            'cloud_archive.cloud_auto_cleanup': auto_cleanup,
            'cloud_archive.cloud_min_retention_days': min_retention,
            'cloud_archive.retry_max_attempts': cloud_retry_max_attempts,
        }

        # Phase 1 item 1.3 — UI toggle is positive-framed
        # ("keep_clips_until_synced"), backend key is its inverse
        # ("delete_unsynced"). Only persist when a cloud provider is
        # connected: when no provider is configured the template renders
        # the toggle disabled (browsers do not submit disabled
        # checkboxes), so the form won't carry the user's intended
        # state. Writing ``delete_unsynced=true`` in that case would
        # silently override the documented null/auto-default and break
        # the auto-protection promised when the user later connects a
        # provider. PR #96 review fix.
        #
        # Provider-connected check mirrors the GET handler at L160:
        # provider name from cloud_archive.provider in YAML AND a
        # creds file actually present on disk.
        try:
            _cloud_cfg = _get_cloud_config_cached()
            _provider = (_cloud_cfg.get('provider', '') or '').strip()
        except Exception:  # noqa: BLE001
            _provider = ''
        provider_connected = bool(_provider) and os.path.isfile(
            CLOUD_PROVIDER_CREDS_PATH
        )

        delete_unsynced = None  # for log line
        if provider_connected:
            keep_until_synced = 'keep_clips_until_synced' in request.form
            delete_unsynced = not keep_until_synced
            config_updates['cloud_archive.delete_unsynced'] = delete_unsynced

        _update_config_yaml(config_updates)

        flash("Cloud sync settings saved.", "success")
        logger.info(
            "Cloud sync settings updated: folders=%s, priority=%s, "
            "bw=%d Mbps, delete_unsynced=%s (provider_connected=%s)",
            sync_folders, priority_order, max_upload_mbps,
            delete_unsynced, provider_connected,
        )
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
    """Return current sync status and stats for UI polling.

    Stats are cached for 10s to avoid hammering SQLite every 3s poll
    while rclone is doing heavy I/O on the same SD card.
    """
    from services.cloud_archive_service import get_sync_status, get_sync_stats
    import time as _time

    try:
        status = get_sync_status()

        now = _time.time()
        if not hasattr(api_status, '_cache') or now - api_status._cache.get('ts', 0) > 10:
            stats = get_sync_stats(CLOUD_ARCHIVE_DB_PATH)
            api_status._cache = {'data': stats, 'ts': now}
        else:
            stats = api_status._cache['data']

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


@cloud_archive_bp.route('/api/storage_usage')
def api_storage_usage():
    """Return cloud storage quota and usage."""
    from services.cloud_rclone_service import get_storage_usage

    try:
        return jsonify(get_storage_usage())
    except Exception as exc:
        logger.exception("Failed to get storage usage")
        return jsonify({}), 500


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
# Sync queue & batch status endpoints
# ---------------------------------------------------------------------------

@cloud_archive_bp.route('/api/sync_status_batch', methods=['POST'])
def api_sync_status_batch():
    """Check cloud sync status for multiple events."""
    from services.cloud_archive_service import get_sync_status_for_events
    data = request.get_json(silent=True) or {}
    events = data.get('events', [])
    try:
        statuses = get_sync_status_for_events(events)
        return jsonify({"statuses": statuses})
    except Exception as exc:
        logger.exception("Failed to fetch batch sync statuses")
        return jsonify({"statuses": {}})


@cloud_archive_bp.route('/api/queue_event', methods=['POST'])
def api_queue_event():
    """Add an event to the cloud sync queue with optional priority."""
    data = request.get_json(silent=True) or {}
    folder = data.get('folder', '')
    event_name = data.get('event', '')
    priority = data.get('priority', False)

    if not folder or not event_name:
        return jsonify({"success": False, "message": "Missing folder or event"}), 400

    from services.cloud_archive_service import queue_event_for_sync
    try:
        ok, msg = queue_event_for_sync(folder, event_name, priority=priority)
        return jsonify({"success": ok, "message": msg})
    except Exception as exc:
        logger.exception("Failed to queue event for sync")
        return jsonify({"success": False, "message": str(exc)}), 500


@cloud_archive_bp.route('/api/queue')
def api_queue():
    """Return current sync queue."""
    from services.cloud_archive_service import get_sync_queue
    try:
        return jsonify(get_sync_queue())
    except Exception as exc:
        logger.exception("Failed to fetch sync queue")
        return jsonify({"queue": [], "error": str(exc)})


@cloud_archive_bp.route('/api/queue/remove', methods=['POST'])
def api_queue_remove():
    """Remove an item from the sync queue."""
    data = request.get_json(silent=True) or {}
    file_path = data.get('file_path', '')
    if not file_path:
        return jsonify({"success": False, "message": "No file path"})
    from services.cloud_archive_service import remove_from_queue
    try:
        ok, msg = remove_from_queue(file_path)
        return jsonify({"success": ok, "message": msg})
    except Exception as exc:
        logger.exception("Failed to remove item from queue")
        return jsonify({"success": False, "message": str(exc)})


@cloud_archive_bp.route('/api/queue/clear', methods=['POST'])
def api_queue_clear():
    """Clear all queued items."""
    from services.cloud_archive_service import clear_queue
    try:
        ok, msg = clear_queue()
        return jsonify({"success": ok, "message": msg})
    except Exception as exc:
        logger.exception("Failed to clear sync queue")
        return jsonify({"success": False, "message": str(exc)})


@cloud_archive_bp.route('/api/archive_cleanup', methods=['POST'])
def api_archive_cleanup():
    """Manually trigger smart archive cleanup."""
    from services.video_archive_service import trigger_archive_cleanup
    try:
        result = trigger_archive_cleanup()
        return jsonify({"success": True, "result": result})
    except Exception as exc:
        logger.exception("Failed to run archive cleanup")
        return jsonify({"success": False, "message": str(exc)}), 500


# ---------------------------------------------------------------------------
# Single-file archive (from video panel)
# ---------------------------------------------------------------------------

@cloud_archive_bp.route('/api/archive_file', methods=['POST'])
def api_archive_file():
    """Archive all camera views for a video event to the cloud.

    Preserves the USB folder structure on the remote:
    remote_path/SentryClips/2026-01-01_12-00-00/...
    """
    from services.cloud_rclone_service import archive_event
    from services.video_service import get_teslacam_path

    data = request.get_json(silent=True) or {}
    folder = data.get('folder', '')  # e.g. "SentryClips"
    event_name = data.get('event', '')  # e.g. "2026-01-15_14-30-45"

    if not folder or not event_name:
        return jsonify({"success": False, "message": "Missing folder or event."}), 400

    teslacam = get_teslacam_path()
    if not teslacam:
        return jsonify({"success": False, "message": "TeslaCam path not available."}), 400

    try:
        ok, msg = archive_event(folder, event_name, teslacam)
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


# ---------------------------------------------------------------------------
# Bandwidth Test API
# ---------------------------------------------------------------------------

@cloud_archive_bp.route('/api/bandwidth_test', methods=['POST'])
def api_bandwidth_test():
    """Start a bandwidth test to find optimal upload speed."""
    from services.bandwidth_test_service import start_bandwidth_test
    from services.cloud_archive_service import get_sync_status

    # Don't run during active sync
    if get_sync_status().get("running"):
        return jsonify({"success": False, "message": "Stop the active sync first"}), 400

    # Need rclone conf — write a temporary one
    from services.cloud_archive_service import _write_rclone_conf, _load_provider_creds
    creds = _load_provider_creds()
    if not creds:
        return jsonify({"success": False, "message": "No cloud provider credentials"}), 400

    try:
        conf_path = _write_rclone_conf(CLOUD_ARCHIVE_PROVIDER, creds)
        ok, msg = start_bandwidth_test(conf_path, CLOUD_ARCHIVE_REMOTE_PATH)
        return jsonify({"success": ok, "message": msg})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@cloud_archive_bp.route('/api/bandwidth_test/status')
def api_bandwidth_test_status():
    """Return bandwidth test progress and results."""
    from services.bandwidth_test_service import get_bandwidth_test_status
    return jsonify(get_bandwidth_test_status())


@cloud_archive_bp.route('/api/bandwidth_test/apply', methods=['POST'])
def api_bandwidth_test_apply():
    """Apply the recommended bandwidth from the test."""
    from services.bandwidth_test_service import get_bandwidth_test_status

    status = get_bandwidth_test_status()
    recommended = status.get("recommended_mbps")
    if not recommended:
        return jsonify({"success": False, "message": "No test results available"}), 400

    try:
        _update_config_yaml({'cloud_archive.max_upload_mbps': recommended})
        return jsonify({"success": True, "message": f"Bandwidth set to {recommended} Mbps"})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500
