"""Settings dashboard blueprint — v1 consolidated Settings/Dashboard page.

Owns the ``settings`` Blueprint name and ``/settings/`` URL so that
``base.html``'s ``url_for("settings.index")`` resolves to the real dashboard
instead of the Phase 5.4 scaffold. ``/`` is owned by ``mapping.map_view``
(the v1 home page) — operators visiting ``http://device/`` land on the
Map, matching v1.

Removed from v1 parity (per docs/00-PLAN.md invariants):
* Mode-toggle button and all mode_control.* switch endpoints.
* Filesystem Health Check section (fsck / IMG / loopback — B-1 uses btrfs).
"""

from __future__ import annotations

import logging
import platform
import socket
import sys
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, cast

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from teslausb_web.services.gadget_state import gadget_mode_token
from teslausb_web.services.mapping_settings_service import (
    MappingSettingsError,
    MappingSettingsService,
)
from teslausb_web.services.samba_password_service import (
    SambaPasswordCommandError,
    SambaPasswordError,
    SambaPasswordNotInstalledError,
    SambaPasswordService,
    SambaPasswordValidationError,
)
from teslausb_web.services.system_settings_service import (
    SystemSettingsConfigError,
    SystemSettingsService,
    SystemSettingsStateError,
)
from teslausb_web.services.wifi_service import WifiConfigError, WifiError, WifiService

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

settings_dashboard_bp = Blueprint("settings", __name__)

_MEMINFO_FIELD_VALUE_INDEX = 1


def _get_wifi_service() -> WifiService:
    service = current_app.extensions.get("wifi_service")
    if not isinstance(service, WifiService):
        raise RuntimeError("wifi_service extension is not configured")
    return service


def _get_system_settings_service() -> SystemSettingsService:
    service = current_app.extensions.get("system_settings_service")
    if not isinstance(service, SystemSettingsService):
        raise RuntimeError("system_settings_service extension is not configured")
    return service


def _get_mapping_settings_service() -> MappingSettingsService:
    service = current_app.extensions.get("mapping_settings_service")
    if not isinstance(service, MappingSettingsService):
        raise RuntimeError("mapping_settings_service extension is not configured")
    return service


def _get_samba_password_service() -> SambaPasswordService:
    service = current_app.extensions.get("samba_password_service")
    if not isinstance(service, SambaPasswordService):
        raise RuntimeError("samba_password_service extension is not configured")
    return service


def _coerce_int_form_field(field_name: str, raw: str | None) -> int:
    if raw is None or not raw.strip():
        raise MappingSettingsError(f"{field_name} is required")
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise MappingSettingsError(f"{field_name} must be an integer") from exc


def _redirect_to_index() -> Response:
    return cast("Response", redirect(url_for("settings.index")))


def _config() -> WebConfig:
    return cast("WebConfig", current_app.config["teslausb_config"])


def _read_meminfo_mb(field_name: str) -> int:
    meminfo = Path("/proc/meminfo")
    if not meminfo.is_file():
        return 0
    try:
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith(field_name):
                parts = line.split()
                if len(parts) > _MEMINFO_FIELD_VALUE_INDEX:
                    return int(parts[_MEMINFO_FIELD_VALUE_INDEX]) // 1024
    except (OSError, ValueError):
        logger.warning("Could not read %s from /proc/meminfo", field_name)
    return 0


def _get_mem_avail_mb() -> int:
    value = _read_meminfo_mb("MemAvailable:")
    return value if value > 0 else _read_meminfo_mb("MemFree:")


def _get_mem_total_mb() -> int:
    return _read_meminfo_mb("MemTotal:")


def _format_duration(seconds: int) -> str:
    days, remainder = divmod(max(seconds, 0), 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, _ = divmod(remainder, 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _format_uptime() -> str:
    uptime_path = Path("/proc/uptime")
    if not uptime_path.is_file():
        return "Unknown"
    try:
        value = uptime_path.read_text(encoding="utf-8").split()[0]
        return _format_duration(int(float(value)))
    except (OSError, ValueError, IndexError):
        logger.warning("Could not parse /proc/uptime")
        return "Unknown"


def _get_ip_addresses() -> tuple[str, ...]:
    host = socket.gethostname()
    seen: set[str] = set()
    addresses: list[str] = []
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return ()
    for info in infos:
        addr = info[4][0]
        if not isinstance(addr, str):
            continue
        if addr.startswith("127.") or addr == "::1":
            continue
        if addr in seen:
            continue
        seen.add(addr)
        addresses.append(addr)
    return tuple(addresses)


def _wifi_context() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    cfg = _config()
    service = _get_wifi_service()
    form_ssid, form_passphrase = service.ap_credentials_for_form()
    try:
        status = service.get_status()
        wifi_status: dict[str, object] = {
            "connected": status.connected,
            "current_ssid": status.current_ssid,
            "signal_strength": status.signal_strength,
            "ip_address": status.ip_address,
        }
        ap_status: dict[str, object] = {
            "error": None,
            "ap_active": status.ap_mode.active,
            "force_mode": "force_on" if status.ap_mode.requested_enabled else "auto",
            "ssid": status.ap_mode.ssid,
            "static_ip": "10.0.0.1",
            "dhcp_range_start": "10.0.0.10",
            "dhcp_range_end": "10.0.0.50",
            "allow_concurrent": True,
        }
        ap_config: dict[str, object] = {
            "ssid": form_ssid,
            "passphrase": form_passphrase,
        }
        return wifi_status, ap_status, ap_config
    except (RuntimeError, WifiError) as exc:
        logger.warning("settings dashboard Wi-Fi status unavailable: %s", exc)
        fallback_ssid = form_ssid or cfg.wifi.ap_ssid
        wifi_status = {
            "connected": False,
            "current_ssid": None,
            "signal_strength": None,
            "ip_address": None,
        }
        ap_status = {
            "error": str(exc),
            "ap_active": False,
            "force_mode": "auto",
            "ssid": fallback_ssid,
            "static_ip": "10.0.0.1",
            "dhcp_range_start": "10.0.0.10",
            "dhcp_range_end": "10.0.0.50",
            "allow_concurrent": True,
        }
        ap_config = {"ssid": fallback_ssid, "passphrase": form_passphrase}
        return wifi_status, ap_status, ap_config


def _samba_on() -> bool:
    try:
        return _get_system_settings_service().get_settings().samba_enabled
    except (RuntimeError, SystemSettingsConfigError, SystemSettingsStateError) as exc:
        logger.warning("settings dashboard samba status unavailable: %s", exc)
        return False


def _system_info() -> dict[str, object]:
    return {
        "hostname": socket.gethostname(),
        "ip_addresses": list(_get_ip_addresses()),
        "uptime": _format_uptime(),
        "platform": platform.platform(terse=True),
        "python": sys.version.split()[0],
        "mem_avail_mb": _get_mem_avail_mb(),
        "mem_total_mb": _get_mem_total_mb(),
        "version": "B-1",
        "disk_images": [],
    }


def _samba_password_set() -> bool:
    """One pdbedit probe per request — cached on flask.g to avoid double work."""
    from flask import g

    cached = getattr(g, "_samba_password_set", None)
    if isinstance(cached, bool):
        return cached
    try:
        result = _get_samba_password_service().user_exists()
    except (RuntimeError, SambaPasswordError) as exc:
        logger.warning("settings dashboard samba password probe failed: %s", exc)
        result = False
    g._samba_password_set = result
    return result


def _share_paths_for_display() -> list[dict[str, str]]:
    """UNC paths to show on the Network File Sharing card."""
    samba_service = current_app.extensions.get("samba_service")
    shares = getattr(getattr(samba_service, "config", None), "shares", ()) or ()
    host = socket.gethostname() or "teslausb"
    return [
        {"name": share.name, "unc": f"\\\\{host}\\{share.name}"}
        for share in shares
    ]


def _index_context() -> dict[str, object]:
    wifi_status, ap_status, ap_config = _wifi_context()
    mapping_service = _get_mapping_settings_service()
    mapping_snapshot = mapping_service.get_settings()
    samba_on = _samba_on()
    share_paths = _share_paths_for_display()
    return {
        "page": "settings",
        "auto_refresh": False,
        "operation_in_progress": False,
        "wifi_change_status": None,
        "wifi_status": wifi_status,
        "ap_status": ap_status,
        "ap_config": ap_config,
        "cfg_mapping": mapping_service.serialize_for_template(mapping_snapshot),
        "cfg_network": {
            "samba_password": "",
            "samba_enabled": samba_on,
            "samba_password_set": _samba_password_set(),
            "share_paths": share_paths,
        },
        "system_info": _system_info(),
        "samba_on": samba_on,
        # Probe the live USB-gadget state (configfs UDC + both LUN
        # backing files). Returns 'present' only when the gadget is
        # actually bound to a UDC and both LUNs have backing block
        # devices — the green "Connected to Tesla" card promises Tesla
        # can use the drives, so it must reflect kernel state, not a
        # static value. Any failure in the teslafat → nbd-attach →
        # usb-gadget chain drops to 'unknown' and shows the orange
        # "Status Unknown" card.
        "mode_token": gadget_mode_token(),
        "share_paths": share_paths,
    }


def _render_dashboard() -> ResponseReturnValue:
    try:
        return render_template("index.html", **_index_context())
    except Exception:
        logger.exception("Unhandled error while preparing settings dashboard")
        return (
            jsonify({"success": False, "error": "Internal server error"}),
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


@settings_dashboard_bp.route("/settings/")
def index() -> ResponseReturnValue:
    return _render_dashboard()


@settings_dashboard_bp.route("/settings/configure_ap", methods=["POST"])
def configure_ap() -> ResponseReturnValue:
    """Persist the operator's AP SSID/passphrase override and apply it.

    The form ships `ssid` (1-32 chars) and `passphrase` (8-63 chars or
    empty). The WifiService validates and saves these to an override
    JSON file so they survive restart, and bounces the AP profile if
    it's currently broadcasting.
    """
    ssid = (request.form.get("ssid") or "").strip()
    passphrase = request.form.get("passphrase") or ""
    if not ssid:
        flash("AP SSID is required.", "error")
        return _redirect_to_index()
    try:
        _get_wifi_service().update_ap_credentials(ssid=ssid, passphrase=passphrase)
    except WifiConfigError as exc:
        flash(f"Invalid AP credentials: {exc}", "error")
        return _redirect_to_index()
    except WifiError as exc:
        logger.warning("Failed to apply AP credentials: %s", exc)
        flash(f"Saved AP credentials but failed to apply to live profile: {exc}", "warning")
        return _redirect_to_index()
    flash(f"AP credentials updated (SSID: {ssid}).", "success")
    return _redirect_to_index()


@settings_dashboard_bp.route("/settings/force_ap", methods=["POST"])
def force_ap() -> ResponseReturnValue:
    """Start or stop the AP profile on demand.

    The form ships `mode=on` (Start AP Now) or `mode=off` (Stop AP).
    Any other value is treated as an error; we deliberately do not
    invent a third state.
    """
    mode = (request.form.get("mode") or "").strip().lower()
    if mode not in {"on", "off"}:
        flash(f"Unsupported AP mode {mode!r}; expected 'on' or 'off'.", "error")
        return _redirect_to_index()
    try:
        _get_wifi_service().set_ap_mode(enabled=(mode == "on"))
    except WifiConfigError as exc:
        flash(f"Invalid AP configuration: {exc}", "error")
        return _redirect_to_index()
    except WifiError as exc:
        logger.warning("Failed to %s AP: %s", "start" if mode == "on" else "stop", exc)
        flash(f"Failed to {'start' if mode == 'on' else 'stop'} AP: {exc}", "error")
        return _redirect_to_index()
    flash(
        "Access point started." if mode == "on" else "Access point stopped.",
        "success",
    )
    return _redirect_to_index()


@settings_dashboard_bp.route("/settings/save/mapping", methods=["POST"])
def save_mapping_settings() -> ResponseReturnValue:
    """Persist mapping thresholds. Worker picks them up on next tick."""
    service = _get_mapping_settings_service()
    try:
        trip_gap = _coerce_int_form_field(
            "Trip gap (minutes)",
            request.form.get("trip_gap_minutes"),
        )
        speed_mph = _coerce_int_form_field(
            "Speed alert (mph)",
            request.form.get("speed_limit_mph"),
        )
        snapshot = service.save_settings(
            trip_gap_minutes=trip_gap,
            speed_limit_mph=speed_mph,
        )
    except MappingSettingsError as exc:
        flash(f"Invalid mapping settings: {exc}", "error")
        return _redirect_to_index()
    flash(
        f"Mapping settings saved (trip gap {snapshot.trip_gap_minutes} min, "
        f"speed alert {snapshot.speed_limit_mph} mph"
        f"{' — disabled' if not snapshot.speed_limit_enabled else ''}).",
        "success",
    )
    return _redirect_to_index()


@settings_dashboard_bp.route("/settings/save/network", methods=["POST"])
def save_network_settings() -> ResponseReturnValue:
    """Toggle SMB sharing and (optionally) update the Samba password.

    The toggle persists via SystemSettingsService — the app-factory
    callback then starts/stops smbd. Password changes go through
    SambaPasswordService and update only the Samba TDB, never our
    JSON state.
    """
    samba_enabled = request.form.get("samba_enabled") == "on"
    raw_password = request.form.get("samba_password", "")
    password_provided = bool(raw_password)

    if password_provided:
        try:
            _get_samba_password_service().set_password(raw_password)
        except SambaPasswordValidationError as exc:
            flash(f"Password rejected: {exc}", "error")
            return _redirect_to_index()
        except SambaPasswordNotInstalledError:
            flash("Samba is not installed on this device.", "error")
            return _redirect_to_index()
        except (SambaPasswordCommandError, RuntimeError) as exc:
            logger.exception("Failed to set Samba password")
            flash(f"Failed to update Samba password: {exc}", "error")
            return _redirect_to_index()

    try:
        settings_service = _get_system_settings_service()
        current = settings_service.get_settings()
        settings_service.update_settings(
            {
                "samba_enabled": samba_enabled,
                "log_level": current.log_level,
            }
        )
    except (RuntimeError, SystemSettingsConfigError, SystemSettingsStateError) as exc:
        logger.exception("Failed to persist samba_enabled toggle")
        flash(f"Failed to save Network File Sharing settings: {exc}", "error")
        return _redirect_to_index()

    share_paths = _share_paths_for_display()
    share_summary = ", ".join(entry["unc"] for entry in share_paths) or "(no shares)"
    state_msg = "enabled" if samba_enabled else "disabled"
    pw_msg = " Samba password updated." if password_provided else ""
    flash(
        f"Network File Sharing {state_msg}. Shares: {share_summary}.{pw_msg}",
        "success",
    )
    return _redirect_to_index()


@settings_dashboard_bp.route("/api/settings/wifi/dismiss-status", methods=["POST"])
def dismiss_wifi_status() -> ResponseReturnValue:
    return jsonify({"success": True}), HTTPStatus.OK


__all__ = ("settings_dashboard_bp",)
