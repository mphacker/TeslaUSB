"""Blueprint for Tesla Fleet API account management, vehicle controls, and bonus features."""

import base64
import logging
import os
import subprocess
from urllib.parse import urlencode

from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from config import (
    CONFIG_YAML,
    TESLA_API_CLIENT_ID,
    TESLA_API_CLIENT_SECRET,
    TESLA_API_DB_PATH,
    TESLA_API_KEEP_AWAKE_METHOD,
    TESLA_API_LOW_BATTERY_THRESHOLD,
    TESLA_API_MAX_AWAKE_MINUTES,
    TESLA_API_MONTHLY_BUDGET,
    TESLA_API_WAKE_INTERVAL,
    TESLA_TOKENS_PATH,
)
from utils import get_base_context

tesla_api_bp = Blueprint('tesla_api', __name__, url_prefix='/tesla')
logger = logging.getLogger(__name__)

# Tesla OAuth endpoints (must match tesla_api_service constants)
_TESLA_AUTH_URL = "https://auth.tesla.com/oauth2/v3/authorize"
_TESLA_SCOPES = "openid offline_access vehicle_device_data vehicle_location vehicle_cmds"


# ---------------------------------------------------------------------------
# Feature gating
# ---------------------------------------------------------------------------

@tesla_api_bp.before_request
def _require_tesla_configured():
    """Allow index and save_credentials unconditionally; gate everything else."""
    # Always allow the setup/credentials pages
    if request.endpoint in ('tesla_api.index', 'tesla_api.save_credentials'):
        return None
    if not TESLA_API_CLIENT_ID:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"error": "Tesla API not configured. Set Client ID first."}), 503
        flash("Configure Tesla API credentials first.", "warning")
        return redirect(url_for('tesla_api.index'))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_wifi_available():
    """Check if WiFi (not AP) is connected on wlan0."""
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'DEVICE,TYPE,STATE', 'device'],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split('\n'):
            parts = line.split(':')
            if (
                len(parts) >= 3
                and parts[0] == 'wlan0'
                and parts[1] == 'wifi'
                and parts[2] == 'connected'
            ):
                return True
    except Exception:
        pass
    return False


def _get_selected_vin():
    """Return the VIN stored in the Flask session, or None."""
    return session.get('tesla_vin')


def _require_vin_or_error():
    """Return (vin, error_response).  error_response is None when VIN is available."""
    vin = _get_selected_vin()
    if vin:
        return vin, None

    # Try to auto-select first vehicle
    try:
        from services.tesla_api_service import get_vehicles
        vehicles = get_vehicles(TESLA_API_DB_PATH)
        if vehicles:
            vin = vehicles[0].get('vin')
            if vin:
                session['tesla_vin'] = vin
                return vin, None
    except Exception as exc:
        logger.warning("Failed to auto-select VIN: %s", exc)

    return None, jsonify({"success": False, "error": "No vehicle selected"}), 400


def _update_config_yaml(updates: dict):
    """Atomically update config.yaml with *updates* (dotted-key → value)."""
    import yaml

    with open(CONFIG_YAML, 'r') as fh:
        cfg = yaml.safe_load(fh)

    for key, value in updates.items():
        keys = key.split('.')
        d = cfg
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = value

    tmp_path = CONFIG_YAML + '.tmp'
    with open(tmp_path, 'w') as fh:
        yaml.safe_dump(cfg, fh, default_flow_style=False, sort_keys=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, CONFIG_YAML)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@tesla_api_bp.route('/')
def index():
    """Tesla settings page — main dashboard for account, vehicles, and controls."""
    from services.tesla_api_service import (
        get_monthly_spend,
        get_vehicles,
        is_connected,
        needs_pin_unlock,
    )

    connected = is_connected()
    pin_needed = needs_pin_unlock()
    vehicles = []
    budget = {}
    audit_log = []
    wifi = _is_wifi_available()

    if connected and not pin_needed:
        try:
            vehicles = get_vehicles(TESLA_API_DB_PATH) or []
        except Exception as exc:
            logger.warning("Failed to list vehicles: %s", exc)

        try:
            budget = get_monthly_spend(TESLA_API_DB_PATH)
        except Exception as exc:
            logger.warning("Failed to load budget: %s", exc)

        try:
            from services.tesla_api_service import _init_tesla_tables
            conn = _init_tesla_tables(TESLA_API_DB_PATH)
            rows = conn.execute(
                "SELECT timestamp, endpoint, method, success, source, error_msg "
                "FROM tesla_api_audit ORDER BY timestamp DESC LIMIT 50"
            ).fetchall()
            conn.close()
            audit_log = [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("Failed to load audit log: %s", exc)

        # Auto-select first VIN if none in session
        if vehicles and not _get_selected_vin():
            first_vin = vehicles[0].get('vin')
            if first_vin:
                session['tesla_vin'] = first_vin

    ctx = get_base_context()
    # Override stale module-level value with fresh config read
    ctx.pop('tesla_api_configured', None)

    # Re-read client credentials from config.yaml (may have just been saved)
    import yaml
    _client_id = TESLA_API_CLIENT_ID
    _client_secret = TESLA_API_CLIENT_SECRET
    try:
        with open(CONFIG_YAML, 'r') as f:
            _cfg = yaml.safe_load(f) or {}
        _tesla_cfg = _cfg.get('tesla_api', {})
        _client_id = _tesla_cfg.get('client_id', '') or _client_id
        _client_secret = _tesla_cfg.get('client_secret', '') or _client_secret
    except Exception:
        pass

    # Mask client secret for display (show first 4 chars + dots)
    secret_masked = (_client_secret[:4] + '•' * 12) if _client_secret and len(_client_secret) > 4 else ''

    return render_template(
        'tesla_settings.html',
        page='tesla',
        connected=connected,
        pin_needed=pin_needed,
        vehicles=vehicles,
        selected_vin=_get_selected_vin(),
        budget=budget,
        audit_log=audit_log,
        wifi_available=wifi,
        client_id=_client_id,
        client_secret_masked=secret_masked,
        tesla_api_configured=bool(_client_id),
        keep_awake_method=TESLA_API_KEEP_AWAKE_METHOD,
        max_awake_minutes=TESLA_API_MAX_AWAKE_MINUTES,
        low_battery_threshold=TESLA_API_LOW_BATTERY_THRESHOLD,
        wake_interval_seconds=TESLA_API_WAKE_INTERVAL,
        monthly_budget=TESLA_API_MONTHLY_BUDGET,
        **ctx,
    )


# ---------------------------------------------------------------------------
# Form endpoints
# ---------------------------------------------------------------------------

@tesla_api_bp.route('/settings', methods=['POST'])
def save_settings():
    """Save Tesla API settings from form submission."""
    try:
        keep_awake_method = request.form.get('keep_awake_method', 'wake_only')
        max_awake_minutes = int(request.form.get('max_awake_minutes', 60))
        low_battery_threshold = int(request.form.get('low_battery_threshold', 20))
        wake_interval = int(request.form.get('wake_interval_seconds', 90))
        monthly_budget = float(request.form.get('monthly_budget_limit', 10.0))

        _update_config_yaml({
            'tesla_api.keep_awake_method': keep_awake_method,
            'tesla_api.max_awake_minutes': max_awake_minutes,
            'tesla_api.low_battery_threshold': low_battery_threshold,
            'tesla_api.wake_interval_seconds': wake_interval,
            'tesla_api.monthly_budget_limit': monthly_budget,
        })

        flash("Tesla API settings saved.", "success")
        logger.info(
            "Tesla settings updated: method=%s, max_awake=%dm, battery=%d%%, interval=%ds, budget=$%.2f",
            keep_awake_method, max_awake_minutes, low_battery_threshold, wake_interval, monthly_budget,
        )
    except (ValueError, TypeError) as exc:
        logger.warning("Invalid Tesla settings form data: %s", exc)
        flash("Invalid settings values.", "danger")
    except Exception:
        logger.exception("Failed to save Tesla API settings")
        flash("Error saving Tesla API settings.", "danger")

    return redirect(url_for('tesla_api.index'))


@tesla_api_bp.route('/credentials', methods=['POST'])
def save_credentials():
    """Save Tesla API Client ID and Client Secret from the web UI."""
    try:
        client_id = request.form.get('client_id', '').strip()
        client_secret = request.form.get('client_secret', '').strip()

        if not client_id:
            flash("Client ID is required.", "warning")
            return redirect(url_for('tesla_api.index'))

        updates = {
            'tesla_api.client_id': client_id,
        }
        # Only update secret if a new one was provided (not the masked display value)
        if client_secret and '•' not in client_secret:
            updates['tesla_api.client_secret'] = client_secret

        _update_config_yaml(updates)

        # Update the in-memory config values so the page reflects changes immediately
        import config as _cfg
        _cfg.TESLA_API_CLIENT_ID = client_id
        if client_secret and not client_secret.startswith('•'):
            _cfg.TESLA_API_CLIENT_SECRET = client_secret

        flash("Tesla API credentials saved. You can now connect your Tesla account.", "success")
        logger.info("Tesla API credentials updated (client_id=%s…)", client_id[:8] if len(client_id) > 8 else client_id)
    except Exception:
        logger.exception("Failed to save Tesla API credentials")
        flash("Error saving credentials.", "danger")

    return redirect(url_for('tesla_api.index'))


@tesla_api_bp.route('/select_vehicle', methods=['POST'])
def select_vehicle():
    """Store the user-selected VIN in the Flask session."""
    vin = request.form.get('vin', '').strip()
    if vin:
        session['tesla_vin'] = vin
        flash(f"Vehicle {vin[-6:]} selected.", "success")
        logger.info("Selected vehicle VIN …%s", vin[-6:])
    else:
        flash("No VIN provided.", "warning")
    return redirect(url_for('tesla_api.index'))


# ---------------------------------------------------------------------------
# OAuth flow (URL paste — Tesla only allows http://localhost redirect)
# ---------------------------------------------------------------------------

# Must match what's registered in the Tesla developer portal
_TESLA_REDIRECT_URI = "https://mphacker.github.io/TeslaUSB/auth/callback/"


@tesla_api_bp.route('/auth')
def auth():
    """Generate Tesla OAuth URL for the user to open in their browser."""
    state = base64.urlsafe_b64encode(os.urandom(16)).decode()
    session['tesla_oauth_state'] = state

    params = {
        'client_id': TESLA_API_CLIENT_ID,
        'redirect_uri': _TESLA_REDIRECT_URI,
        'response_type': 'code',
        'scope': _TESLA_SCOPES,
        'state': state,
    }
    auth_url = f"{_TESLA_AUTH_URL}?{urlencode(params)}"

    logger.info("Generated Tesla OAuth URL")
    session['tesla_auth_url'] = auth_url
    flash("Open the link below in a browser, sign in to Tesla, then paste the redirect URL back here.", "info")
    return redirect(url_for('tesla_api.index'))


@tesla_api_bp.route('/auth/complete', methods=['POST'])
def auth_complete():
    """Accept the pasted authorization code and exchange it for tokens.

    The user copies the code from the GitHub Pages callback page and
    pastes it here. Also accepts a full redirect URL as fallback.
    """
    from urllib.parse import urlparse, parse_qs

    pasted = request.form.get('redirect_url', '').strip()
    if not pasted:
        flash("Please paste the authorization code.", "warning")
        return redirect(url_for('tesla_api.index'))

    # Detect if it's a full URL or a bare code
    if pasted.startswith('http://') or pasted.startswith('https://'):
        try:
            parsed = urlparse(pasted)
            qs = parse_qs(parsed.query)
        except Exception:
            flash("Could not parse the URL.", "danger")
            return redirect(url_for('tesla_api.index'))

        error = qs.get('error', [None])[0]
        if error:
            desc = qs.get('error_description', [error])[0]
            flash(f"Tesla login failed: {desc}", "danger")
            return redirect(url_for('tesla_api.index'))

        code = qs.get('code', [None])[0]
        state = qs.get('state', [None])[0]
    else:
        # Bare authorization code
        code = pasted
        state = None

    expected_state = session.pop('tesla_oauth_state', None)
    if expected_state and state and state != expected_state:
        logger.warning("Tesla OAuth state mismatch")
        flash("Authentication failed — state mismatch. Please try again.", "danger")
        return redirect(url_for('tesla_api.index'))

    if not code:
        flash("No authorization code found in the URL. Make sure you copied the full URL.", "danger")
        return redirect(url_for('tesla_api.index'))

    pin = session.get('tesla_pin', '')

    try:
        from services.tesla_api_service import exchange_code
        result = exchange_code(code, _TESLA_REDIRECT_URI, pin)
    except Exception as exc:
        logger.exception("Tesla OAuth code exchange failed")
        flash(f"Failed to connect Tesla account: {exc}", "danger")
        return redirect(url_for('tesla_api.index'))

    if result:
        flash("Tesla account connected successfully!", "success")
        logger.info("Tesla OAuth completed — tokens saved")
        session.pop('tesla_auth_url', None)
    else:
        flash("Failed to exchange authorization code. Please try again.", "danger")
        logger.error("Tesla OAuth code exchange returned None")

    return redirect(url_for('tesla_api.index'))


@tesla_api_bp.route('/callback')
def callback():
    """Legacy callback route — redirects to index with instructions."""
    flash("Copy the full URL from your browser's address bar and paste it into the Tesla settings page.", "info")
    return redirect(url_for('tesla_api.index'))


@tesla_api_bp.route('/disconnect', methods=['POST'])
def disconnect():
    """Revoke tokens and disconnect Tesla account."""
    try:
        from services.tesla_api_service import clear_tokens, stop_keep_awake
        stop_keep_awake()
        clear_tokens()
    except Exception as exc:
        logger.exception("Error during Tesla disconnect")
        flash(f"Error disconnecting: {exc}", "danger")
        return redirect(url_for('tesla_api.index'))

    session.pop('tesla_vin', None)
    session.pop('tesla_oauth_state', None)
    flash("Tesla account disconnected.", "success")
    logger.info("Tesla account disconnected by user")
    return redirect(url_for('tesla_api.index'))


# ---------------------------------------------------------------------------
# AJAX API endpoints
# ---------------------------------------------------------------------------

@tesla_api_bp.route('/api/status')
def api_status():
    """Return vehicle status JSON for the dashboard widget."""
    from services.tesla_api_service import can_spend, is_connected

    if not is_connected():
        return jsonify({"success": False, "error": "Not connected"}), 401

    if not _is_wifi_available():
        return jsonify({"success": False, "error": "WiFi offline", "offline": True}), 503

    if not can_spend(TESLA_API_DB_PATH, 'data'):
        return jsonify({"success": False, "error": "Monthly API budget exceeded"}), 429

    vin, err = _require_vin_or_error()
    if err:
        return err

    try:
        from services.tesla_api_service import get_vehicle_status
        status = get_vehicle_status(vin, TESLA_API_DB_PATH)
        if status is None:
            return jsonify({"success": False, "error": "Could not retrieve vehicle status"}), 502
        return jsonify({"success": True, "data": status})
    except Exception as exc:
        logger.error("Vehicle status request failed: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


@tesla_api_bp.route('/api/sentry', methods=['POST'])
def api_sentry():
    """Toggle Sentry Mode on/off."""
    from services.tesla_api_service import can_spend, is_connected

    if not is_connected():
        return jsonify({"success": False, "error": "Not connected"}), 401

    if not _is_wifi_available():
        return jsonify({"success": False, "error": "WiFi offline", "offline": True}), 503

    if not can_spend(TESLA_API_DB_PATH, 'command'):
        return jsonify({"success": False, "error": "Monthly API budget exceeded"}), 429

    data = request.get_json(silent=True)
    if data is None or 'on' not in data:
        return jsonify({"success": False, "error": "Missing 'on' parameter"}), 400

    vin, err = _require_vin_or_error()
    if err:
        return err

    try:
        from services.tesla_api_service import set_sentry_mode
        result = set_sentry_mode(vin, bool(data['on']), TESLA_API_DB_PATH)
        if result is None:
            return jsonify({"success": False, "error": "Command failed"}), 502
        logger.info("Sentry mode %s for VIN …%s", "enabled" if data['on'] else "disabled", vin[-6:])
        return jsonify({"success": True, "data": result})
    except Exception as exc:
        logger.error("Sentry mode toggle failed: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


@tesla_api_bp.route('/api/climate', methods=['POST'])
def api_climate():
    """Start or stop climate control."""
    from services.tesla_api_service import can_spend, is_connected

    if not is_connected():
        return jsonify({"success": False, "error": "Not connected"}), 401

    if not _is_wifi_available():
        return jsonify({"success": False, "error": "WiFi offline", "offline": True}), 503

    if not can_spend(TESLA_API_DB_PATH, 'command'):
        return jsonify({"success": False, "error": "Monthly API budget exceeded"}), 429

    data = request.get_json(silent=True)
    if data is None or 'on' not in data:
        return jsonify({"success": False, "error": "Missing 'on' parameter"}), 400

    vin, err = _require_vin_or_error()
    if err:
        return err

    try:
        if data['on']:
            from services.tesla_api_service import start_climate
            result = start_climate(vin, TESLA_API_DB_PATH)
            action = "started"
        else:
            from services.tesla_api_service import stop_climate
            result = stop_climate(vin, TESLA_API_DB_PATH)
            action = "stopped"

        if result is None:
            return jsonify({"success": False, "error": "Command failed"}), 502
        logger.info("Climate %s for VIN …%s", action, vin[-6:])
        return jsonify({"success": True, "data": result})
    except Exception as exc:
        logger.error("Climate control failed: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


@tesla_api_bp.route('/api/budget')
def api_budget():
    """Return monthly API budget breakdown."""
    try:
        from services.tesla_api_service import get_monthly_spend
        spend = get_monthly_spend(TESLA_API_DB_PATH)
        return jsonify({"success": True, "data": spend})
    except Exception as exc:
        logger.error("Budget query failed: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


@tesla_api_bp.route('/api/audit')
def api_audit():
    """Return recent API audit log entries (last 50)."""
    try:
        from services.tesla_api_service import _init_tesla_tables
        conn = _init_tesla_tables(TESLA_API_DB_PATH)
        rows = conn.execute(
            "SELECT timestamp, endpoint, method, success, source, error_msg "
            "FROM tesla_api_audit ORDER BY timestamp DESC LIMIT 50"
        ).fetchall()
        conn.close()
        return jsonify({"success": True, "data": [dict(r) for r in rows]})
    except Exception as exc:
        logger.error("Audit log query failed: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500
