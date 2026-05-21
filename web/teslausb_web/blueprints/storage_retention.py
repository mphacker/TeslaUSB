"""Storage-retention settings blueprint."""

from __future__ import annotations

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

from teslausb_web.services.storage_retention_service import (
    RetentionConfigError,
    RetentionPolicy,
    RetentionStateError,
    StorageRetentionService,
)

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

logger = logging.getLogger(__name__)

storage_retention_bp = Blueprint("storage_retention", __name__)

_XHR_HEADER_VALUE: Final[str] = "XMLHttpRequest"


def _invalidate_caches(app: Flask) -> None:
    invalidator = app.extensions.get("cache_invalidator")
    if invalidator is not None:
        invalidator.schedule()


def _get_service() -> StorageRetentionService:
    service = current_app.extensions["storage_retention_service"]
    if not isinstance(service, StorageRetentionService):
        raise RuntimeError("storage_retention_service extension is not configured")
    return service


def _wants_json_response() -> bool:
    return request.headers.get("X-Requested-With") == _XHR_HEADER_VALUE or request.is_json


def _json_error_payload(message: str) -> Response:
    return jsonify({"success": False, "error": message})


def _json_message_payload(*, success: bool, message: str, **fields: object) -> Response:
    return jsonify({"success": success, "message": message, **fields})


def _redirect_to_settings(*, cache_bust: str | None = None) -> Response:
    if cache_bust is None:
        return cast("Response", redirect(url_for("storage_retention.settings")))
    return cast(
        "Response",
        redirect(url_for("storage_retention.settings", _=cache_bust)),
    )


def _storage_retention_response(
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


def _boundary_error(exc: RetentionConfigError | RetentionStateError) -> ResponseReturnValue:
    status = (
        HTTPStatus.BAD_REQUEST
        if isinstance(exc, RetentionConfigError)
        else HTTPStatus.INTERNAL_SERVER_ERROR
    )
    if _wants_json_response():
        return _json_error_payload(str(exc)), status
    flash(str(exc), "error")
    return _redirect_to_settings(cache_bust=request.args.get("_", "0"))


def _serialize_policy(policy: RetentionPolicy) -> dict[str, object]:
    return _get_service().serialize_policy(policy)


def _policy_payload(policy: RetentionPolicy) -> dict[str, object]:
    service = _get_service()
    return {
        "policy": _serialize_policy(policy),
        "rows": [
            {
                "key": row.key,
                "label": row.label,
                "keep_field": row.keep_field,
                "days_field": row.days_field,
                "keep": row.keep,
                "retention_days": row.retention_days,
                "guidance": row.guidance,
                "caution": row.caution,
            }
            for row in service.policy_rows(policy)
        ],
        "ranges": service.ranges(),
        "preview_available": False,
    }


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
        raise RetentionConfigError(f"{name} must be a boolean")
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


def _request_policy_update() -> dict[str, object]:
    _ = request.form.get("partition")
    _ = request.form.get("<partition>")
    return {
        "max_age_days": _request_text("max_age_days"),
        "target_free_pct": _request_text("target_free_pct"),
        "max_archive_size_gb": _request_text("max_archive_size_gb"),
        "short_retention_warning_days": _request_text("short_retention_warning_days"),
        "keep_recent_clips": _request_bool("keep_recent_clips"),
        "keep_saved_clips": _request_bool("keep_saved_clips"),
        "keep_event_clips": _request_bool("keep_event_clips"),
        "keep_encrypted_clips": _request_bool("keep_encrypted_clips"),
        "keep_archived_clips": _request_bool("keep_archived_clips"),
        "dry_run": _request_bool("dry_run"),
        "recent_clips_days": _request_text("recent_clips_days"),
        "saved_clips_days": _request_text("saved_clips_days"),
        "event_clips_days": _request_text("event_clips_days"),
        "encrypted_clips_days": _request_text("encrypted_clips_days"),
        "archived_clips_days": _request_text("archived_clips_days"),
    }


def _index_context(policy: RetentionPolicy) -> dict[str, object]:
    service = _get_service()
    return {
        "page": "settings",
        "auto_refresh": False,
        "operation_in_progress": False,
        "storage_retention_available": True,
        "policy": policy,
        "policy_rows": service.policy_rows(policy),
        "ranges": service.ranges(),
        "preview_available": False,
    }


@storage_retention_bp.route("/cleanup")
@storage_retention_bp.route("/cleanup/")
def index() -> ResponseReturnValue:
    try:
        policy = _get_service().get_policy()
        return render_template("cleanup_settings.html", **_index_context(policy))
    except (RetentionConfigError, RetentionStateError) as exc:
        return _boundary_error(exc)
    except Exception:
        logger.exception("Unhandled error while preparing storage-retention index")
        return _json_error_payload("Internal server error"), HTTPStatus.INTERNAL_SERVER_ERROR


@storage_retention_bp.route("/cleanup/settings")
def settings() -> ResponseReturnValue:
    return index()


@storage_retention_bp.route("/cleanup/settings", methods=["POST"])
@storage_retention_bp.route("/api/cleanup/policy", methods=["POST"])
def save_settings() -> ResponseReturnValue:
    try:
        policy = _get_service().update_policy(_request_policy_update())
    except (RetentionConfigError, RetentionStateError) as exc:
        return _boundary_error(exc)
    _invalidate_caches(current_app)
    return _storage_retention_response(
        success=True,
        message="Saved storage-retention settings",
        status=HTTPStatus.OK,
        **_policy_payload(policy),
    )


@storage_retention_bp.route("/api/cleanup/policy", methods=["GET"])
@storage_retention_bp.route("/api/cleanup/status", methods=["GET"])
def current_policy() -> ResponseReturnValue:
    try:
        payload = {"success": True, **_policy_payload(_get_service().get_policy())}
        return jsonify(payload), HTTPStatus.OK
    except (RetentionConfigError, RetentionStateError) as exc:
        return _boundary_error(exc)


@storage_retention_bp.route("/cleanup/preview")
@storage_retention_bp.route("/api/cleanup/preview")
def preview() -> ResponseReturnValue:
    try:
        preview_summary = _get_service().preview_summary()
        return jsonify(
            {
                "success": True,
                "preview_available": preview_summary.preview_available,
                "deferred_reason": preview_summary.deferred_reason,
            }
        )
    except (RetentionConfigError, RetentionStateError) as exc:
        if _wants_json_response() or request.path.startswith("/api/cleanup/"):
            return _json_error_payload(str(exc)), HTTPStatus.SERVICE_UNAVAILABLE
        flash(str(exc), "error")
        return _redirect_to_settings(cache_bust=request.args.get("_", "0"))
