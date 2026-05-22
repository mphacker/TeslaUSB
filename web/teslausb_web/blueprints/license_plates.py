"""Tracked license-plate management blueprint."""

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
    send_from_directory,
    url_for,
)

from teslausb_web.services.license_plate_service import (
    LicensePlate,
    LicensePlateError,
    LicensePlateService,
    PlateBulkOperationResult,
    PlateConfigError,
    PlateDuplicateError,
    PlateMatch,
    PlateNotFoundError,
    RedactionConfig,
)
from teslausb_web.services.photo_plate_service import (
    PhotoPlateError,
    PhotoPlateFileError,
    PhotoPlateService,
)

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

logger = logging.getLogger(__name__)

license_plates_bp = Blueprint("license_plates", __name__, url_prefix="/license_plates")

_XHR_HEADER_VALUE: Final[str] = "XMLHttpRequest"
_DEFAULT_PARTITION: Final[str] = "part2"


class LicensePlateBlueprintError(RuntimeError):
    """Base error raised by blueprint-only helpers."""


def _invalidate_caches(app: Flask) -> None:
    invalidator = app.extensions.get("cache_invalidator")
    if invalidator is not None:
        invalidator.schedule()


def _get_service() -> LicensePlateService:
    service = current_app.extensions["license_plate_service"]
    if not isinstance(service, LicensePlateService):
        raise RuntimeError("license_plate_service extension is not configured")
    return service


def _get_photo_service() -> PhotoPlateService:
    service = current_app.extensions.get("photo_plate_service")
    if not isinstance(service, PhotoPlateService):
        raise RuntimeError("photo_plate_service extension is not configured")
    return service


def _wants_json_response() -> bool:
    return request.headers.get("X-Requested-With") == _XHR_HEADER_VALUE or request.is_json


def _json_error_payload(message: str) -> Response:
    return jsonify({"success": False, "error": message})


def _json_message_payload(*, success: bool, message: str, **fields: object) -> Response:
    return jsonify({"success": success, "message": message, **fields})


def _redirect_to_license_plates(*, cache_bust: str | None = None) -> Response:
    if cache_bust is None:
        return cast("Response", redirect(url_for("license_plates.license_plates")))
    return cast(
        "Response",
        redirect(url_for("license_plates.license_plates", _=cache_bust)),
    )


def _license_plates_response(
    *,
    success: bool,
    message: str,
    status: HTTPStatus,
    **fields: object,
) -> ResponseReturnValue:
    if _wants_json_response():
        return _json_message_payload(success=success, message=message, **fields), status
    flash(message, "success" if success else "error")
    return _redirect_to_license_plates(cache_bust=request.args.get("_", "0"))


def _boundary_error(
    exc: PlateConfigError | PlateDuplicateError | PlateNotFoundError | LicensePlateError,
) -> ResponseReturnValue:
    if isinstance(exc, PlateDuplicateError):
        status = HTTPStatus.CONFLICT
    elif isinstance(exc, PlateNotFoundError):
        status = HTTPStatus.NOT_FOUND
    elif isinstance(exc, PlateConfigError):
        status = HTTPStatus.BAD_REQUEST
    else:
        status = HTTPStatus.INTERNAL_SERVER_ERROR
    return _license_plates_response(success=False, message=str(exc), status=status)


def _serialize_plate(plate: LicensePlate) -> dict[str, object]:
    return {
        "id": plate.id,
        "plate_text": plate.plate_text,
        "normalized_plate": plate.normalized_plate,
        "label": plate.label,
        "notes": plate.notes,
        "created_at": plate.created_at.isoformat(),
        "updated_at": plate.updated_at.isoformat(),
    }


def _serialize_redaction(config: RedactionConfig) -> dict[str, object]:
    return {
        "enabled": config.enabled,
        "updated_at": config.updated_at.isoformat(),
    }


def _serialize_match(match: PlateMatch) -> dict[str, object]:
    matched_plate = None
    if match.matched_plate is not None:
        matched_plate = _serialize_plate(match.matched_plate)
    return {
        "candidate": match.candidate,
        "normalized_candidate": match.normalized_candidate,
        "matched_plate": matched_plate,
        "is_match": match.is_match,
    }


def _index_context(
    plates: tuple[LicensePlate, ...],
    redaction_config: RedactionConfig,
) -> dict[str, object]:
    service_config = _get_service().config
    photo_service = _get_photo_service()
    try:
        plate_files = photo_service.list_plates()
    except PhotoPlateFileError:
        logger.exception("Failed to list photo plates")
        plate_files = ()
    return {
        "page": "media",
        "media_tab": "plates",
        "license_plates_available": True,
        "auto_refresh": False,
        "operation_in_progress": False,
        "plates": plates,
        "plate_count": len(plates),
        "redaction_config": redaction_config,
        "max_plate_length": service_config.max_plate_length,
        "max_label_length": service_config.max_label_length,
        "max_notes_length": service_config.max_notes_length,
        "default_partition": _DEFAULT_PARTITION,
        # Photo plate (Tesla custom-background PNG) section.
        "plate_files": plate_files,
        "max_file_size": 512 * 1024,  # Tesla spec: 512 KB max per PNG
        "max_filename_length": 12,  # Tesla spec: 12 alphanumeric chars
        "max_plate_count": 5,  # Tesla spec: 5 plates max
        "plate_width_na": 420,  # Tesla NA spec: 420 x 75 px
        "plate_height_na": 75,
        "plate_width_eu": 492,  # Tesla EU/Italy spec: 492 x 75 px
        "plate_height_eu": 75,
    }


def _request_text(name: str) -> str:
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        value = payload.get(name)
        if isinstance(value, str):
            return value
        if value is None:
            return ""
        return str(value)
    value = request.form.get(name)
    return "" if value is None else value


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
        raise PlateConfigError(f"{name} must be a boolean")
    value = request.form.get(name)
    return value is not None and value.strip().lower() in {"1", "true", "on", "yes"}


def _request_int_list(*names: str) -> list[int]:
    return [_coerce_request_int(value) for value in _request_raw_values(*names)]


def _request_raw_values(*names: str) -> list[object]:
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        for name in names:
            if name not in payload:
                continue
            value = payload[name]
            return value if isinstance(value, list) else [value]
    for name in names:
        values = request.form.getlist(name)
        if values:
            return [value for value in values if value.strip()]
    return []


def _coerce_request_int(raw_value: object) -> int:
    if isinstance(raw_value, bool):
        raise PlateConfigError("plate_ids must contain integers")
    if isinstance(raw_value, int):
        return raw_value
    if isinstance(raw_value, str):
        candidate = raw_value.strip()
        if not candidate:
            raise PlateConfigError("plate_ids must contain integers")
        try:
            return int(candidate)
        except ValueError as exc:
            raise PlateConfigError("plate_ids must contain integers") from exc
    raise PlateConfigError("plate_ids must contain integers")


@license_plates_bp.route("/")
def license_plates() -> ResponseReturnValue:
    try:
        service = _get_service()
        plates = service.list_license_plates()
        redaction_config = service.get_redaction_config()
        return render_template(
            "license_plates.html",
            **_index_context(plates, redaction_config),
        )
    except (PlateConfigError, PlateDuplicateError, PlateNotFoundError, LicensePlateError) as exc:
        return _boundary_error(exc)
    except Exception:
        logger.exception("Unhandled error while preparing license plate index")
        return _json_error_payload("Internal server error"), HTTPStatus.INTERNAL_SERVER_ERROR


@license_plates_bp.route("/add", methods=["POST"])
def add_license_plate() -> ResponseReturnValue:
    try:
        plate = _get_service().add_license_plate(
            _request_text("plate_text"),
            label=_request_text("label"),
            notes=_request_text("notes"),
        )
    except (PlateConfigError, PlateDuplicateError, PlateNotFoundError, LicensePlateError) as exc:
        return _boundary_error(exc)
    _invalidate_caches(current_app)
    return _license_plates_response(
        success=True,
        message=f"Saved tracked plate {plate.plate_text}",
        status=HTTPStatus.CREATED,
        plate=_serialize_plate(plate),
    )


@license_plates_bp.route("/update/<int:plate_id>", methods=["POST"])
def update_license_plate(plate_id: int) -> ResponseReturnValue:
    try:
        plate = _get_service().update_license_plate(
            plate_id,
            plate_text=_request_text("plate_text"),
            label=_request_text("label"),
            notes=_request_text("notes"),
        )
    except (PlateConfigError, PlateDuplicateError, PlateNotFoundError, LicensePlateError) as exc:
        return _boundary_error(exc)
    _invalidate_caches(current_app)
    return _license_plates_response(
        success=True,
        message=f"Updated tracked plate {plate.plate_text}",
        status=HTTPStatus.OK,
        plate=_serialize_plate(plate),
    )


@license_plates_bp.route("/delete/<partition>/<int:plate_id>", methods=["POST"])
def delete_license_plate(partition: str, plate_id: int) -> ResponseReturnValue:
    del partition
    try:
        _get_service().delete_license_plate(plate_id)
    except (PlateConfigError, PlateDuplicateError, PlateNotFoundError, LicensePlateError) as exc:
        return _boundary_error(exc)
    _invalidate_caches(current_app)
    return _license_plates_response(
        success=True,
        message="Deleted tracked plate",
        status=HTTPStatus.OK,
    )


@license_plates_bp.route("/bulk_delete", methods=["POST"])
def bulk_delete_license_plates() -> ResponseReturnValue:
    try:
        result = _get_service().bulk_delete(_request_int_list("plate_ids", "ids"))
    except (PlateConfigError, PlateDuplicateError, PlateNotFoundError, LicensePlateError) as exc:
        return _boundary_error(exc)
    status = HTTPStatus.OK if result.deleted_count > 0 else HTTPStatus.NOT_FOUND
    if result.deleted_count > 0:
        _invalidate_caches(current_app)
    return _license_plates_response(
        success=result.deleted_count > 0,
        message=result.message,
        status=status,
        bulk_result=_serialize_bulk_result(result),
    )


@license_plates_bp.route("/redaction", methods=["POST"])
def update_redaction() -> ResponseReturnValue:
    try:
        config = _get_service().update_redaction_config(enabled=_request_bool("enabled"))
    except (PlateConfigError, PlateDuplicateError, PlateNotFoundError, LicensePlateError) as exc:
        return _boundary_error(exc)
    _invalidate_caches(current_app)
    message = "Updated default redaction setting"
    return _license_plates_response(
        success=True,
        message=message,
        status=HTTPStatus.OK,
        redaction_config=_serialize_redaction(config),
    )


@license_plates_bp.route("/match", methods=["POST"])
def match_license_plate() -> ResponseReturnValue:
    try:
        match = _get_service().match_plate(_request_text("candidate"))
    except (PlateConfigError, PlateDuplicateError, PlateNotFoundError, LicensePlateError) as exc:
        return _boundary_error(exc)
    message = (
        f"{match.normalized_candidate} is tracked"
        if match.is_match
        else f"{match.normalized_candidate} is not tracked"
    )
    return _json_message_payload(
        success=True,
        message=message,
        match=_serialize_match(match),
    ), HTTPStatus.OK


def _serialize_bulk_result(result: PlateBulkOperationResult) -> dict[str, object]:
    return {
        "requested_count": result.requested_count,
        "deleted_count": result.deleted_count,
        "missing_ids": list(result.missing_ids),
        "message": result.message,
        "success": result.success,
    }


# ---------------------------------------------------------------------------
# Photo plate routes — manage the Tesla custom-background PNGs that live in
# {backing_root}/lightshow/LicensePlate/ on the lightshow USB partition.
# Tesla firmware spec: PNG only, 420x75 (NA) or 492x75 (EU), <=512 KB,
# base name <=12 alphanumeric characters, <=5 plates.
# ---------------------------------------------------------------------------


def _photo_upload_response(
    *, success: bool, message: str, file_count: int = 0
) -> ResponseReturnValue:
    if _wants_json_response():
        status = HTTPStatus.OK if success else HTTPStatus.BAD_REQUEST
        return (
            _json_message_payload(success=success, message=message, file_count=file_count),
            status,
        )
    flash(message, "info" if success else "error")
    return _redirect_to_license_plates()


@license_plates_bp.route("/upload", methods=["POST"])
def upload_plate() -> ResponseReturnValue:
    """Upload a single PNG plate via the `plate_file` form field."""
    uploaded = request.files.get("plate_file")
    if uploaded is None or not (uploaded.filename or "").strip():
        return _photo_upload_response(success=False, message="No file selected")
    try:
        service = _get_photo_service()
        result = service.upload_files([uploaded])
    except PhotoPlateFileError as exc:
        logger.exception("Photo plate upload failed")
        return _photo_upload_response(success=False, message=str(exc))
    if result.success:
        _invalidate_caches(current_app)
    return _photo_upload_response(
        success=result.success, message=result.message, file_count=result.file_count
    )


@license_plates_bp.route("/upload_multiple", methods=["POST"])
def upload_multiple_plates() -> ResponseReturnValue:
    """Upload one or more PNG plates via the `plate_files` form field."""
    uploads = request.files.getlist("plate_files")
    if not uploads:
        return _photo_upload_response(success=False, message="No files selected")
    try:
        service = _get_photo_service()
        result = service.upload_files(uploads)
    except PhotoPlateFileError as exc:
        logger.exception("Photo plate upload failed")
        return _photo_upload_response(success=False, message=str(exc))
    if result.success:
        _invalidate_caches(current_app)
    return _photo_upload_response(
        success=result.success, message=result.message, file_count=result.file_count
    )


@license_plates_bp.route("/download/<partition>/<path:filename>")
def download_plate(partition: str, filename: str) -> ResponseReturnValue:
    """Serve a stored plate PNG so the browser can render the thumbnail."""
    del partition  # Single-partition (LightShow) layout; kept for URL symmetry.
    try:
        service = _get_photo_service()
        file_path = service.resolve_plate(filename)
    except PhotoPlateError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.NOT_FOUND
    return send_from_directory(
        directory=service.plates_folder,
        path=file_path.name,
        mimetype="image/png",
        as_attachment=False,
    )


@license_plates_bp.route("/delete_image/<partition>/<path:filename>", methods=["POST"])
def delete_plate(partition: str, filename: str) -> ResponseReturnValue:
    """Delete a stored plate PNG and schedule a UI cache invalidation."""
    del partition
    try:
        service = _get_photo_service()
        result = service.delete_plate(filename)
    except PhotoPlateError as exc:
        if _wants_json_response():
            return _json_error_payload(str(exc)), HTTPStatus.BAD_REQUEST
        flash(str(exc), "error")
        return _redirect_to_license_plates()
    except PhotoPlateFileError as exc:
        logger.exception("Photo plate delete failed")
        if _wants_json_response():
            return _json_error_payload(str(exc)), HTTPStatus.INTERNAL_SERVER_ERROR
        flash(str(exc), "error")
        return _redirect_to_license_plates()
    if result.success:
        _invalidate_caches(current_app)
    if _wants_json_response():
        return _json_message_payload(success=result.success, message=result.message)
    flash(result.message, "info" if result.success else "error")
    return _redirect_to_license_plates()


__all__ = ("license_plates_bp",)
