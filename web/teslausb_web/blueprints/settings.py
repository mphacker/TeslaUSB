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
    url_for,
)

from teslausb_web.services.system_settings_service import (
    SystemSettingsConfigError,
    SystemSettingsService,
    SystemSettingsStateError,
)
from teslausb_web.services.wifi_service import WifiError, WifiService

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


def _redirect_to_index() -> Response:
    return cast("Response", redirect(url_for("settings.index")))


def _stub_save(message: str) -> ResponseReturnValue:
    flash(message, "info")
    return _redirect_to_index()


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
    try:
        status = _get_wifi_service().get_status()
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
            "ssid": status.ap_mode.ssid,
            "passphrase": "",
        }
        return wifi_status, ap_status, ap_config
    except (RuntimeError, WifiError) as exc:
        logger.warning("settings dashboard Wi-Fi status unavailable: %s", exc)
        fallback_ssid = cfg.wifi.ap_ssid
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
        ap_config = {"ssid": fallback_ssid, "passphrase": ""}
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


def _index_context() -> dict[str, object]:
    wifi_status, ap_status, ap_config = _wifi_context()
    return {
        "page": "settings",
        "auto_refresh": False,
        "operation_in_progress": False,
        "wifi_change_status": None,
        "wifi_status": wifi_status,
        "ap_status": ap_status,
        "ap_config": ap_config,
        "videos_available": True,
        "cfg_archive": {"enabled": False, "retention_days": 30, "min_free_space_gb": 5},
        "cfg_mapping": {"enabled": False, "trip_gap_minutes": 15, "speed_limit_mph": 80},
        "cfg_network": {"samba_password": ""},
        "system_info": _system_info(),
        "samba_on": _samba_on(),
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


@settings_dashboard_bp.route("/settings/save/archive", methods=["POST"])
def save_archive_settings() -> ResponseReturnValue:
    return _stub_save("Archive settings are not yet supported in B-1")


@settings_dashboard_bp.route("/settings/save/mapping", methods=["POST"])
def save_mapping_settings() -> ResponseReturnValue:
    return _stub_save("Mapping settings are not yet supported in B-1")


@settings_dashboard_bp.route("/settings/save/network", methods=["POST"])
def save_network_settings() -> ResponseReturnValue:
    return _stub_save("Network settings are not yet supported in B-1")


@settings_dashboard_bp.route("/api/settings/wifi/dismiss-status", methods=["POST"])
def dismiss_wifi_status() -> ResponseReturnValue:
    return jsonify({"success": True}), HTTPStatus.OK


__all__ = ("settings_dashboard_bp",)
