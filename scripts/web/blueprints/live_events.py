"""Blueprint for Live Event Sync (LES) status and retry endpoints.

LES is the real-time per-event uploader (separate from the bulk
``cloud_archive`` sync). This blueprint exposes a thin API so the web
UI and on-device tooling can:

* Read queue + last-upload status (``GET /api/live_events/status``)
* List recent queue rows (``GET /api/live_events/queue``)
* Retry a failed row (``POST /api/live_events/retry/<id>``)
* Wake the worker (``POST /api/live_events/wake``) — called by the
  NetworkManager WiFi-connect dispatcher path so LES drains BEFORE
  cloud_archive sync runs.
* Read current config from ``config.yaml`` (``GET /api/live_events/config``)
* Save config + schedule restart (``POST /api/live_events/settings``)
* Trigger an explicit restart (``POST /api/live_events/restart``)

All endpoints are JSON-only (no HTML). Runtime endpoints are image-gated
on ``IMG_CAM_PATH`` (Sentry/Saved events live on part1 — TeslaCam), so
they return 503 when the cam image is missing. **Configuration**
endpoints (``/config``, ``/settings``, ``/restart``) intentionally
bypass that gate so the user can pre-configure LES before the cam
image is provisioned. When LES is disabled in config the runtime
routes return ``{"enabled": false}`` so the UI can render a disabled
state instead of a hard error.
"""

import logging
import os
import subprocess
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request

from config import CONFIG_YAML, IMG_CAM_PATH, LIVE_EVENT_SYNC_ENABLED

logger = logging.getLogger(__name__)

live_events_bp = Blueprint('live_events', __name__,
                           url_prefix='/api/live_events')


# Endpoints that are allowed even when the cam image is missing — these
# are configuration / lifecycle endpoints that don't touch event data.
_CONFIG_ENDPOINTS = frozenset({
    'live_events.get_config',
    'live_events.save_settings',
    'live_events.restart_service',
})


@live_events_bp.before_request
def _require_cam_image():
    """Block runtime routes when usb_cam.img is missing.

    Configuration endpoints stay available so the user can prepare LES
    settings before the cam image exists.
    """
    if request.endpoint in _CONFIG_ENDPOINTS:
        return None
    if not os.path.isfile(IMG_CAM_PATH):
        return jsonify({"error": "Feature unavailable"}), 503


@live_events_bp.route('/status', methods=['GET'])
def status():
    """Return a JSON snapshot of LES state plus queue counts."""
    if not LIVE_EVENT_SYNC_ENABLED:
        return jsonify({"enabled": False})
    from services.live_event_sync_service import get_status
    return jsonify({"enabled": True, **get_status()})


@live_events_bp.route('/queue', methods=['GET'])
def queue():
    """Return up to ``limit`` recent queue rows (default 50)."""
    if not LIVE_EVENT_SYNC_ENABLED:
        return jsonify({"enabled": False, "rows": []})
    from services.live_event_sync_service import list_queue
    try:
        limit = max(1, min(200, int(request.args.get('limit', 50))))
    except (ValueError, TypeError):
        limit = 50
    return jsonify({"enabled": True, "rows": list_queue(limit)})


@live_events_bp.route('/retry/<int:row_id>', methods=['POST'])
def retry(row_id: int):
    """Reset a single failed row to pending and wake the worker."""
    if not LIVE_EVENT_SYNC_ENABLED:
        return jsonify({"enabled": False, "error": "LES disabled"}), 400
    from services.live_event_sync_service import retry_failed
    n = retry_failed(row_id)
    return jsonify({"enabled": True, "rows_reset": n})


@live_events_bp.route('/retry_all', methods=['POST'])
def retry_all():
    """Reset every failed row to pending."""
    if not LIVE_EVENT_SYNC_ENABLED:
        return jsonify({"enabled": False, "error": "LES disabled"}), 400
    from services.live_event_sync_service import retry_failed
    n = retry_failed(None)
    return jsonify({"enabled": True, "rows_reset": n})


@live_events_bp.route('/wake', methods=['POST'])
def wake():
    """Poke the worker thread (used by the WiFi-connect dispatcher)."""
    if not LIVE_EVENT_SYNC_ENABLED:
        return jsonify({"enabled": False})
    from services.live_event_sync_service import wake as _wake
    _wake()
    return jsonify({"enabled": True, "woken": True})


# ---------------------------------------------------------------------------
# Configuration endpoints (always available, even when cam image is missing)
# ---------------------------------------------------------------------------

_ALLOWED_WATCH_FOLDERS = ('SentryClips', 'SavedClips')
_ALLOWED_UPLOAD_SCOPES = ('event_minute', 'event_folder')
_MAX_WEBHOOK_URL_LEN = 2048
_MAX_DAILY_CAP_MB = 1024 * 1024  # 1 TiB ceiling — far above any sane setting
_MIN_RETRY_ATTEMPTS = 1
_MAX_RETRY_ATTEMPTS = 20


def _read_les_section() -> dict:
    """Read the live_event_sync block fresh from config.yaml on disk."""
    import yaml
    with open(CONFIG_YAML, 'r') as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get('live_event_sync', {}) or {}


def _validate_les_payload(data: dict):
    """Validate and normalize a LES settings payload.

    Returns ``(ok: bool, error: str, normalized: dict)``. When ``ok`` is
    True the normalized dict is safe to persist verbatim.
    """
    try:
        enabled = bool(data.get('enabled', False))

        wf_raw = data.get('watch_folders', [])
        if not isinstance(wf_raw, list):
            return False, "watch_folders must be a list", {}
        watch_folders = []
        for f in wf_raw:
            f_str = str(f)
            if f_str not in _ALLOWED_WATCH_FOLDERS:
                return False, f"watch_folders contains unsupported value '{f_str}'", {}
            if f_str not in watch_folders:
                watch_folders.append(f_str)
        if enabled and not watch_folders:
            return False, "Enable Live Event Sync requires at least one watch folder", {}

        upload_scope = str(data.get('upload_scope', 'event_minute'))
        if upload_scope not in _ALLOWED_UPLOAD_SCOPES:
            return False, (
                "upload_scope must be one of " + ", ".join(_ALLOWED_UPLOAD_SCOPES)
            ), {}

        try:
            retry_max_attempts = int(data.get('retry_max_attempts', 5))
        except (TypeError, ValueError):
            return False, "retry_max_attempts must be an integer", {}
        if not (_MIN_RETRY_ATTEMPTS <= retry_max_attempts <= _MAX_RETRY_ATTEMPTS):
            return False, (
                f"retry_max_attempts must be between {_MIN_RETRY_ATTEMPTS} "
                f"and {_MAX_RETRY_ATTEMPTS}"
            ), {}

        try:
            daily_data_cap_mb = int(data.get('daily_data_cap_mb', 0))
        except (TypeError, ValueError):
            return False, "daily_data_cap_mb must be an integer", {}
        if not (0 <= daily_data_cap_mb <= _MAX_DAILY_CAP_MB):
            return False, (
                f"daily_data_cap_mb must be between 0 and {_MAX_DAILY_CAP_MB}"
            ), {}

        webhook = str(data.get('notify_webhook_url', '') or '').strip()
        if webhook:
            if len(webhook) > _MAX_WEBHOOK_URL_LEN:
                return False, "notify_webhook_url too long", {}
            if any(ord(c) < 32 or ord(c) == 127 for c in webhook):
                return False, "notify_webhook_url contains control characters", {}
            parsed = urlparse(webhook)
            if parsed.scheme not in ('http', 'https') or not parsed.netloc:
                return False, "notify_webhook_url must start with http:// or https://", {}

        return True, "", {
            'enabled': enabled,
            'watch_folders': watch_folders,
            'upload_scope': upload_scope,
            'retry_max_attempts': retry_max_attempts,
            'daily_data_cap_mb': daily_data_cap_mb,
            'notify_webhook_url': webhook,
        }
    except Exception as exc:
        logger.exception("LES validation crashed")
        return False, f"validation error: {exc}", {}


def _schedule_service_restart() -> bool:
    """Schedule a delayed restart of gadget_web.service via systemd-run.

    Uses ``systemd-run --on-active=2`` so the restart job runs as a
    transient timer **outside** this service's cgroup. That way the
    restart still fires even when our own process is killed during the
    restart sequence. Returns True when scheduling succeeded.

    No sudo: gadget_web.service runs as root (port 80 binding requirement,
    see copilot-instructions.md "Web App Patterns"), so ``systemd-run``
    is callable directly. Any future privilege drop would have to either
    (a) add a sudoers rule for ``systemd-run --on-active=...
    systemctl restart gadget_web.service`` or (b) replace this with a
    PolicyKit / dbus call. The settings UI surfaces ``restart_scheduled``
    in its toast so a scheduling failure is at least visible.
    """
    try:
        subprocess.Popen(
            [
                'systemd-run',
                '--on-active=2',
                '/bin/systemctl',
                'restart',
                'gadget_web.service',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        logger.info("Scheduled gadget_web.service restart via systemd-run")
        return True
    except Exception:
        logger.exception("Failed to schedule gadget_web.service restart")
        return False


@live_events_bp.route('/config', methods=['GET'])
def get_config():
    """Return current LES settings from ``config.yaml``.

    Always reachable — does not require ``usb_cam.img`` because the
    settings UI must be able to load config even on a fresh device.
    """
    try:
        les = _read_les_section()
        return jsonify({
            "enabled": bool(les.get('enabled', False)),
            "watch_folders": list(les.get('watch_folders', list(_ALLOWED_WATCH_FOLDERS))),
            "upload_scope": str(les.get('upload_scope', 'event_minute')),
            "retry_max_attempts": int(les.get('retry_max_attempts', 5)),
            "retry_backoff_seconds": list(
                les.get('retry_backoff_seconds', [30, 120, 300, 900, 3600])
            ),
            "daily_data_cap_mb": int(les.get('daily_data_cap_mb', 0)),
            "notify_webhook_url": str(les.get('notify_webhook_url', '') or ''),
            "image_present": os.path.isfile(IMG_CAM_PATH),
            "running_enabled": bool(LIVE_EVENT_SYNC_ENABLED),
        })
    except Exception as exc:
        logger.exception("Failed to read LES config")
        return jsonify({"error": str(exc)}), 500


@live_events_bp.route('/settings', methods=['POST'])
def save_settings():
    """Persist LES settings to ``config.yaml`` and schedule a restart.

    All current LES settings are captured at module import time as
    constants in ``config.py``. Saving therefore always requires a
    service restart for the changes to take effect — that's what the
    scheduler call is for.
    """
    from helpers.config_updater import update_config_yaml

    payload = request.get_json(silent=True) or {}
    ok, err, norm = _validate_les_payload(payload)
    if not ok:
        return jsonify({"success": False, "error": err}), 400

    try:
        update_config_yaml({
            'live_event_sync.enabled': norm['enabled'],
            'live_event_sync.watch_folders': norm['watch_folders'],
            'live_event_sync.upload_scope': norm['upload_scope'],
            'live_event_sync.retry_max_attempts': norm['retry_max_attempts'],
            'live_event_sync.daily_data_cap_mb': norm['daily_data_cap_mb'],
            'live_event_sync.notify_webhook_url': norm['notify_webhook_url'],
        })
    except Exception as exc:
        logger.exception("Failed to persist LES settings")
        return jsonify({"success": False, "error": str(exc)}), 500

    # Don't log the webhook URL value — it can carry secrets/tokens.
    logger.info(
        "LES settings saved: enabled=%s watch_folders=%s scope=%s "
        "retries=%d cap_mb=%d webhook=%s",
        norm['enabled'], norm['watch_folders'], norm['upload_scope'],
        norm['retry_max_attempts'], norm['daily_data_cap_mb'],
        'set' if norm['notify_webhook_url'] else 'none',
    )

    restart_scheduled = _schedule_service_restart()

    response = {
        "success": True,
        "restart_scheduled": restart_scheduled,
        "config": {
            **norm,
            'notify_webhook_url_set': bool(norm['notify_webhook_url']),
        },
    }
    # Echo back the URL only for convenience of the form repopulation;
    # the caller already had it.
    response['config']['notify_webhook_url'] = norm['notify_webhook_url']
    return jsonify(response)


@live_events_bp.route('/restart', methods=['POST'])
def restart_service():
    """Manually schedule a restart of ``gadget_web.service``."""
    if _schedule_service_restart():
        return jsonify({"success": True, "restart_scheduled": True})
    return jsonify({"success": False, "error": "scheduling failed"}), 500
