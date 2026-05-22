"""Captive-portal and Wi-Fi management blueprint."""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, Final, cast

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
)

from teslausb_web.services.wifi_service import (
    ApMode,
    WifiCommandError,
    WifiConfigError,
    WifiConnectionRequest,
    WifiError,
    WifiService,
    WifiStatus,
)

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

logger = logging.getLogger(__name__)

captive_portal_bp = Blueprint("captive_portal", __name__)

_XHR_HEADER_VALUE: Final[str] = "XMLHttpRequest"
_CAPTIVE_ROUTE_LABELS: Final[dict[str, str]] = {
    "hotspot-detect.html": "Apple",
    "library/test/success.html": "Apple",
    "generate_204": "Android",
    "gen_204": "Android",
    "connecttest.txt": "Windows",
    "ncsi.txt": "Windows",
    "redirect": "Windows",
    "success.txt": "Generic",
    "canonical.html": "Generic",
}


def _get_service() -> WifiService:
    service = current_app.extensions.get("wifi_service")
    if not isinstance(service, WifiService):
        raise RuntimeError("wifi_service extension is not configured")
    return service


def _wants_json_response() -> bool:
    return request.headers.get("X-Requested-With") == _XHR_HEADER_VALUE or request.is_json


def _wifi_redirect() -> Response:
    return cast("Response", redirect("/settings/wifi"))


def _json_error(message: str) -> Response:
    return jsonify({"success": False, "error": message})


def _serialize_status() -> dict[str, object]:
    status = _get_service().get_status()
    return {
        "connected": status.connected,
        "current_ssid": status.current_ssid,
        "signal_strength": status.signal_strength,
        "ip_address": status.ip_address,
        "ap_mode": {
            "requested_enabled": status.ap_mode.requested_enabled,
            "active": status.ap_mode.active,
            "ssid": status.ap_mode.ssid,
            "passphrase_configured": status.ap_mode.passphrase_configured,
            "restore_deadline": (
                status.ap_mode.restore_deadline.isoformat()
                if status.ap_mode.restore_deadline is not None
                else None
            ),
        },
        "saved_networks": [
            {
                "ssid": network.ssid,
                "security": network.security,
                "has_passphrase": network.has_passphrase,
                "active": network.active,
            }
            for network in status.saved_networks
        ],
    }


def _fallback_status(service: WifiService) -> WifiStatus:
    return WifiStatus(
        connected=False,
        current_ssid=None,
        signal_strength=None,
        ip_address=None,
        ap_mode=ApMode(
            requested_enabled=False,
            active=False,
            ssid=service._config.ap_ssid,
            passphrase_configured=bool(service._config.ap_passphrase),
            restore_deadline=None,
            state_path=service._config.credentials_path.with_name(
                f"{service._config.credentials_path.stem}_ap_state.json"
            ),
        ),
        saved_networks=(),
    )


def _render_wifi_page(*, source: str | None = None) -> ResponseReturnValue:
    service = _get_service()
    scan_error: str | None = None
    try:
        networks = service.list_available_networks(rescan=request.args.get("rescan") == "1")
    except (RuntimeError, WifiError) as exc:
        logger.warning("Wi-Fi scan failed while rendering captive portal: %s", exc)
        networks = ()
        scan_error = str(exc)
    try:
        status = service.get_status()
    except (RuntimeError, WifiError) as exc:
        logger.warning("Wi-Fi status probe failed while rendering captive portal: %s", exc)
        status = _fallback_status(service)
        scan_error = scan_error or str(exc)
    context = {
        "page": "settings",
        "cloud_archive_available": False,
        "music_available": False,
        "shows_available": False,
        "wraps_available": False,
        "boombox_available": False,
        "chimes_available": False,
        "wifi_status": status,
        "available_networks": networks,
        "scan_error": scan_error,
        "portal_source": source,
    }
    return render_template("captive_portal.html", **context)


def _mutation_response(*, success: bool, message: str, status: HTTPStatus) -> ResponseReturnValue:
    if _wants_json_response():
        payload = _serialize_status()
        payload.update({"success": success, "message": message})
        return jsonify(payload), status
    flash(message, "success" if success else "error")
    return _wifi_redirect()


def _request_value(*names: str) -> str:
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        for name in names:
            value = payload.get(name)
            if value is not None:
                return str(value)
    for name in names:
        value = request.form.get(name)
        if value is not None:
            return value
    for name in names:
        value = request.args.get(name)
        if value is not None:
            return value
    return ""


def _boundary_error(exc: RuntimeError | ValueError) -> ResponseReturnValue:
    if isinstance(exc, WifiCommandError):
        status = HTTPStatus.BAD_GATEWAY
    elif isinstance(exc, (WifiConfigError, WifiError, ValueError)):
        status = HTTPStatus.BAD_REQUEST
    else:
        status = HTTPStatus.INTERNAL_SERVER_ERROR
        logger.exception("Unhandled captive portal error")
        exc = RuntimeError("Internal server error")
    if _wants_json_response() or request.path in {
        "/settings/wifi/status",
        "/settings/wifi/networks",
    }:
        return _json_error(str(exc)), status
    flash(str(exc), "error")
    return _wifi_redirect()


@captive_portal_bp.errorhandler(WifiCommandError)
@captive_portal_bp.errorhandler(WifiConfigError)
@captive_portal_bp.errorhandler(WifiError)
@captive_portal_bp.errorhandler(ValueError)
@captive_portal_bp.errorhandler(RuntimeError)
def _handle_captive_portal_error(exc: RuntimeError | ValueError) -> ResponseReturnValue:
    return _boundary_error(exc)


@captive_portal_bp.route("/settings/wifi")
@captive_portal_bp.route("/captive-portal")
def wifi_setup() -> ResponseReturnValue:
    return _render_wifi_page()


@captive_portal_bp.route("/settings/wifi/status")
def wifi_status() -> ResponseReturnValue:
    return jsonify(_serialize_status())


@captive_portal_bp.route("/settings/wifi/networks")
def wifi_networks() -> ResponseReturnValue:
    networks = _get_service().list_available_networks(rescan=request.args.get("rescan") == "1")
    return (
        jsonify(
            {
                "success": True,
                "networks": [
                    {
                        "ssid": network.ssid,
                        "signal_strength": network.signal_strength,
                        "secured": network.secured,
                        "security": network.security,
                        "active": network.active,
                        "saved": network.saved,
                    }
                    for network in networks
                ],
            }
        ),
        HTTPStatus.OK,
    )


@captive_portal_bp.route("/settings/wifi/connect", methods=["POST"])
def connect_wifi() -> ResponseReturnValue:
    ssid = _request_value("ssid")
    passphrase = _request_value("passphrase", "password")
    _get_service().connect(WifiConnectionRequest(ssid=ssid, passphrase=passphrase))
    return _mutation_response(
        success=True,
        message=f"Connected to {ssid}",
        status=HTTPStatus.OK,
    )


@captive_portal_bp.route("/settings/wifi/disconnect", methods=["POST"])
def disconnect_wifi() -> ResponseReturnValue:
    _get_service().disconnect()
    return _mutation_response(
        success=True,
        message="Disconnected from Wi-Fi and restored setup access point",
        status=HTTPStatus.OK,
    )


@captive_portal_bp.route("/settings/wifi/forget", methods=["POST"])
def forget_wifi() -> ResponseReturnValue:
    ssid = _request_value("ssid")
    _get_service().forget_network(ssid)
    return _mutation_response(
        success=True,
        message=f"Forgot network {ssid}",
        status=HTTPStatus.OK,
    )


@captive_portal_bp.route("/settings/wifi/access-point", methods=["POST"])
def toggle_ap_mode() -> ResponseReturnValue:
    enabled = _request_value("enabled", "ap_enabled").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    _get_service().set_ap_mode(enabled=enabled)
    message = "Setup access point enabled" if enabled else "Setup access point disabled"
    return _mutation_response(success=True, message=message, status=HTTPStatus.OK)


@captive_portal_bp.route("/hotspot-detect.html")
@captive_portal_bp.route("/library/test/success.html")
@captive_portal_bp.route("/generate_204")
@captive_portal_bp.route("/gen_204")
@captive_portal_bp.route("/connecttest.txt")
@captive_portal_bp.route("/ncsi.txt")
@captive_portal_bp.route("/redirect")
@captive_portal_bp.route("/success.txt")
@captive_portal_bp.route("/canonical.html")
def captive_detection() -> ResponseReturnValue:
    route_label = _CAPTIVE_ROUTE_LABELS.get(request.path.lstrip("/"), "Unknown")
    logger.info("%s captive portal probe from %s", route_label, request.remote_addr)
    return _render_wifi_page(source=route_label)


@captive_portal_bp.route("/settings/wifi/configure-ap", methods=["POST"])
def configure_ap() -> ResponseReturnValue:
    """Update AP SSID/passphrase. Full implementation pending Phase 5.21."""
    # TODO(#228): implement AP credential update via WifiService
    ssid = _request_value("ssid")
    passphrase = _request_value("passphrase")
    _ = passphrase
    if ssid:
        logger.info("configure_ap stub called with ssid=%r (not persisted yet)", ssid)
    flash("AP configuration update is not yet supported in B-1", "info")
    return _wifi_redirect()


@captive_portal_bp.route("/settings/wifi/force-ap", methods=["POST"])
def force_ap() -> ResponseReturnValue:
    """Force AP on/off. Maps to toggle_ap_mode."""
    mode = _request_value("mode")
    enabled = mode in {"on", "1", "true", "yes", "force_on"}
    _get_service().set_ap_mode(enabled=enabled)
    message = "Access Point enabled" if enabled else "Access Point disabled"
    return _mutation_response(success=True, message=message, status=HTTPStatus.OK)


@captive_portal_bp.route("/api/wifi/saved")
def api_wifi_saved() -> ResponseReturnValue:
    """Return saved Wi-Fi networks in v1 array format.

    Each saved network is annotated with `in_range` / `signal` by
    cross-referencing the most recent scan against the profile's
    actual 802-11 SSID — NetworkManager's connection profile id
    (which we list as `name`/`ssid` in the payload) is user-editable
    and may differ from the on-air SSID (e.g. profile "WiFi-Trez"
    advertising SSID "Trez"). Without that map every non-active
    saved network looked out-of-range, which is what Issue #228
    surfaces. The currently-connected network is always considered
    in-range with the live signal from the connection state.

    We request a fresh `rescan=True` here because NetworkManager's
    cached wifi list collapses to only the associated AP within a
    minute or so of association. This page is not high-frequency,
    so the ~3 s scan latency is acceptable.
    """
    service = _get_service()
    status = service.get_status()
    try:
        available = service.list_available_networks(rescan=True)
    except WifiError as exc:
        # Don't poison the saved-networks panel just because a scan
        # failed — fall back to "in_range unknown" (only the active
        # network keeps its in-range flag below).
        logger.warning("api_wifi_saved: scan unavailable: %s", exc)
        available = ()
    try:
        profile_ssids = service.saved_wifi_profile_ssids()
    except WifiError as exc:
        logger.warning("api_wifi_saved: profile SSID lookup failed: %s", exc)
        profile_ssids = {}
    # Keep the strongest signal per on-air SSID (some APs broadcast
    # on multiple bands and appear twice in the scan).
    scan_by_ssid: dict[str, int] = {}
    for network in available:
        signal = network.signal_strength or 0
        if network.ssid not in scan_by_ssid or signal > scan_by_ssid[network.ssid]:
            scan_by_ssid[network.ssid] = signal
    active_name = status.current_ssid if status.connected else None
    active_signal = status.signal_strength or 0 if status.connected else 0
    networks = []
    for network in status.saved_networks:
        # `network.ssid` is the NM profile name (e.g. "WiFi-Trez").
        # Resolve to the on-air SSID for the scan cross-reference;
        # fall back to the profile name if we can't read the SSID.
        air_ssid = profile_ssids.get(network.ssid, network.ssid)
        scan_signal = scan_by_ssid.get(air_ssid)
        if scan_signal is None and air_ssid != network.ssid:
            # Last-ditch: some legacy profiles do have matching name.
            scan_signal = scan_by_ssid.get(network.ssid)
        is_active = network.active or network.ssid == active_name
        if is_active:
            in_range = True
            signal = scan_signal if scan_signal is not None else active_signal
        elif scan_signal is not None:
            in_range = True
            signal = scan_signal
        else:
            in_range = False
            signal = 0
        networks.append(
            {
                "name": network.ssid,
                "ssid": network.ssid,
                "active": network.active,
                "in_range": in_range,
                "signal": signal,
            }
        )
    return jsonify(networks)


@captive_portal_bp.route("/api/wifi/reorder", methods=["POST"])
def api_wifi_reorder() -> ResponseReturnValue:
    """Reorder saved Wi-Fi networks. Stub pending Phase 5.21."""
    # TODO(#228): implement network reordering via WifiService
    logger.info("api_wifi_reorder stub called (not yet implemented)")
    return jsonify({"success": True, "message": "Network order saved (stub)"})


@captive_portal_bp.route("/settings/wifi/dismiss-status", methods=["POST"])
def dismiss_wifi_status() -> ResponseReturnValue:
    """Dismiss the WiFi status alert. B-1 uses flash messages, so this is a no-op."""
    return jsonify({"success": True})


@captive_portal_bp.route("/favicon.ico")
def favicon() -> ResponseReturnValue:
    return "", HTTPStatus.NO_CONTENT
