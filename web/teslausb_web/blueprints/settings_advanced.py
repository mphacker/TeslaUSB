"""Advanced settings blueprint."""

from __future__ import annotations

import json
import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, Final, cast

from flask import (
    Blueprint,
    Flask,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from teslausb_web.services.system_settings_service import (
    SystemSettings,
    SystemSettingsConfigError,
    SystemSettingsService,
    SystemSettingsStateError,
)
from teslausb_web.services.teslafat_client import (
    IpcDaemonError,
    IpcProtocolError,
    TeslaFatClient,
)

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

logger = logging.getLogger(__name__)

settings_bp = Blueprint("settings_advanced", __name__)

_XHR_HEADER_VALUE: Final[str] = "XMLHttpRequest"
_MESSAGE_MAX_CHARS: Final[int] = 120


def _invalidate_caches(app: Flask) -> None:
    invalidator = app.extensions.get("cache_invalidator")
    if invalidator is not None:
        invalidator.schedule()


def _get_service() -> SystemSettingsService:
    service = current_app.extensions["system_settings_service"]
    if not isinstance(service, SystemSettingsService):
        raise RuntimeError("system_settings_service extension is not configured")
    return service


def _get_teslafat_client() -> TeslaFatClient:
    client = current_app.extensions["teslafat_client"]
    if not isinstance(client, TeslaFatClient):
        raise RuntimeError("teslafat_client extension is not configured")
    return client


def _wants_json_response() -> bool:
    return (
        request.headers.get("X-Requested-With") == _XHR_HEADER_VALUE
        or request.is_json
        or request.path.startswith("/api/")
    )


def _json_error_payload(message: str) -> Response:
    return jsonify({"success": False, "error": message})


def _json_message_payload(*, success: bool, message: str, **fields: object) -> Response:
    return jsonify({"success": success, "message": message, **fields})


def _truncate(message: str) -> str:
    if len(message) <= _MESSAGE_MAX_CHARS:
        return message
    return message[: _MESSAGE_MAX_CHARS - 1] + "…"


def _redirect_to_settings(*, cache_bust: str | None = None) -> Response:
    if cache_bust is None:
        return cast("Response", redirect(url_for("settings_advanced.advanced")))
    return cast("Response", redirect(url_for("settings_advanced.advanced", _=cache_bust)))


def _settings_response(
    *,
    success: bool,
    message: str,
    status: HTTPStatus,
    **fields: object,
) -> ResponseReturnValue:
    if _wants_json_response():
        return _json_message_payload(success=success, message=message, **fields), status
    flash(message, "success" if success else "error")
    return _redirect_to_settings(cache_bust=request.args.get("_", "0"))


def _boundary_error(
    exc: SystemSettingsConfigError | SystemSettingsStateError,
) -> ResponseReturnValue:
    status = (
        HTTPStatus.BAD_REQUEST
        if isinstance(exc, SystemSettingsConfigError)
        else HTTPStatus.INTERNAL_SERVER_ERROR
    )
    return _settings_response(success=False, message=str(exc), status=status)


def _ipc_error(message: str, *, status: HTTPStatus) -> ResponseReturnValue:
    return _settings_response(success=False, message=message, status=status)


def _serialize_settings(settings: SystemSettings) -> dict[str, object]:
    return _get_service().serialize_settings(settings)


def _request_bool(name: str) -> bool:
    payload = request.get_json(silent=True)
    if isinstance(payload, dict) and name in payload:
        value = payload[name]
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "on", "yes"}
        if isinstance(value, int):
            return value != 0
        raise SystemSettingsConfigError(f"{name} must be a boolean")
    value = request.form.get(name)
    return value is not None and value.strip().lower() in {"1", "true", "on", "yes"}


def _request_text(name: str) -> str:
    payload = request.get_json(silent=True)
    if isinstance(payload, dict) and name in payload:
        value = payload[name]
        if isinstance(value, str):
            return value
        if value is None:
            return ""
        return str(value)
    value = request.form.get(name)
    return "" if value is None else value


def _request_settings_update() -> dict[str, object]:
    _ = request.form.get("partition")
    _ = request.form.get("<partition>")
    return {
        "samba_enabled": _request_bool("samba_enabled"),
        "log_level": _request_text("log_level"),
    }


def _index_context(settings: SystemSettings) -> dict[str, object]:
    service = _get_service()
    snapshot = service.config_snapshot(settings)
    return {
        "page": "settings",
        "auto_refresh": False,
        "operation_in_progress": False,
        "system_settings": settings,
        "log_levels": service.log_levels(),
        "raw_config_json": json.dumps(snapshot, indent=2, sort_keys=True),
        "settings_payload": _serialize_settings(settings),
        "samba_stub_note": (
            "This switch stores the desired network-sharing flag only. "
            "The actual service wiring lands in Phase 5.17."
        ),
    }


@settings_bp.route("/settings/advanced")
@settings_bp.route("/settings/advanced/")
def advanced() -> ResponseReturnValue:
    try:
        return render_template(
            "settings_advanced.html",
            **_index_context(_get_service().get_settings()),
        )
    except (SystemSettingsConfigError, SystemSettingsStateError) as exc:
        return _boundary_error(exc)
    except Exception:
        logger.exception("Unhandled error while preparing advanced settings index")
        return _json_error_payload("Internal server error"), HTTPStatus.INTERNAL_SERVER_ERROR


@settings_bp.route("/settings/advanced/save", methods=["POST"])
@settings_bp.route("/api/settings/advanced", methods=["POST"])
def save_settings() -> ResponseReturnValue:
    try:
        settings = _get_service().update_settings(_request_settings_update())
    except (SystemSettingsConfigError, SystemSettingsStateError) as exc:
        return _boundary_error(exc)
    _invalidate_caches(current_app)
    return _settings_response(
        success=True,
        message="Saved advanced settings",
        status=HTTPStatus.OK,
        settings=_serialize_settings(settings),
    )


@settings_bp.route("/api/settings/advanced", methods=["GET"])
def current_settings() -> ResponseReturnValue:
    try:
        settings = _get_service().get_settings()
        return jsonify({"success": True, "settings": _serialize_settings(settings)}), HTTPStatus.OK
    except (SystemSettingsConfigError, SystemSettingsStateError) as exc:
        return _boundary_error(exc)


@settings_bp.route("/api/settings/advanced/samba", methods=["GET"])
def samba_toggle_state() -> ResponseReturnValue:
    try:
        settings = _get_service().get_settings()
        return jsonify(
            {
                "success": True,
                "samba_enabled": settings.samba_enabled,
                "state_path": str(_get_service().config.state_path),
            }
        ), HTTPStatus.OK
    except (SystemSettingsConfigError, SystemSettingsStateError) as exc:
        return _boundary_error(exc)


@settings_bp.route("/api/settings/advanced/samba", methods=["POST"])
def update_samba_toggle() -> ResponseReturnValue:
    try:
        settings = _get_service().update_settings({"samba_enabled": _request_bool("samba_enabled")})
    except (SystemSettingsConfigError, SystemSettingsStateError) as exc:
        return _boundary_error(exc)
    _invalidate_caches(current_app)
    return _settings_response(
        success=True,
        message="Saved network-sharing stub flag",
        status=HTTPStatus.OK,
        settings=_serialize_settings(settings),
    )


@settings_bp.route("/settings/advanced/ipc/status", methods=["POST"])
@settings_bp.route("/api/settings/advanced/ipc/status", methods=["POST"])
def ipc_status() -> ResponseReturnValue:
    try:
        body = _get_teslafat_client().status()
    except FileNotFoundError:
        return _ipc_error("Daemon socket missing", status=HTTPStatus.SERVICE_UNAVAILABLE)
    except (ConnectionError, TimeoutError, BlockingIOError) as exc:
        return _ipc_error(
            _truncate(f"Daemon unreachable: {exc}"),
            status=HTTPStatus.SERVICE_UNAVAILABLE,
        )
    except IpcDaemonError as exc:
        return _ipc_error(
            _truncate(f"Daemon error: {exc.body.code}"),
            status=HTTPStatus.BAD_GATEWAY,
        )
    except IpcProtocolError as exc:
        return _ipc_error(
            _truncate(f"Protocol error: {exc}"),
            status=HTTPStatus.BAD_GATEWAY,
        )
    _invalidate_caches(current_app)
    return _settings_response(
        success=True,
        message=f"Daemon replied {body.state} on LUN {body.lun_id}",
        status=HTTPStatus.OK,
        ipc={
            "lun_id": body.lun_id,
            "state": body.state,
            "uptime_seconds": body.uptime_seconds,
            "volume_label": body.volume_label,
        },
    )


@settings_bp.route("/settings/advanced/ipc/cache-invalidate", methods=["POST"])
@settings_bp.route("/api/settings/advanced/ipc/cache-invalidate", methods=["POST"])
def ipc_invalidate_cache() -> ResponseReturnValue:
    try:
        _get_teslafat_client().invalidate_cache()
    except FileNotFoundError:
        return _ipc_error("Daemon socket missing", status=HTTPStatus.SERVICE_UNAVAILABLE)
    except (ConnectionError, TimeoutError, BlockingIOError) as exc:
        return _ipc_error(
            _truncate(f"Daemon unreachable: {exc}"),
            status=HTTPStatus.SERVICE_UNAVAILABLE,
        )
    except IpcDaemonError as exc:
        return _ipc_error(
            _truncate(f"Daemon error: {exc.body.code}"),
            status=HTTPStatus.BAD_GATEWAY,
        )
    except IpcProtocolError as exc:
        return _ipc_error(
            _truncate(f"Protocol error: {exc}"),
            status=HTTPStatus.BAD_GATEWAY,
        )
    _invalidate_caches(current_app)
    return _settings_response(
        success=True,
        message="Sent cache invalidation request to daemon",
        status=HTTPStatus.OK,
    )


__all__ = ("settings_bp",)
