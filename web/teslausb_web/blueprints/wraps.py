"""Wrap-management blueprint."""

from __future__ import annotations

import logging
import re
import time
from http import HTTPStatus
from pathlib import Path
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

from teslausb_web.services.wrap_service import WrapError, WrapFileError, WrapInfo, WrapService

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

wraps_bp = Blueprint("wraps", __name__, url_prefix="/wraps")

_BYTES_PER_KIB: Final[int] = 1024
_LIGHTSHOW_DIRNAME: Final[str] = "lightshow"
_LIBRARY_LABEL: Final[str] = "LightShow"
_LIBRARY_PARTITION_KEY: Final[str] = "library"
_PATH_TRAVERSAL_TOKENS: Final[frozenset[str]] = frozenset({"/", "\\", "..", "\x00"})
_VALID_FILENAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_\- ]+$")
_XHR_HEADER_VALUE: Final[str] = "XMLHttpRequest"


def _invalidate_caches(app: Flask) -> None:
    invalidator = app.extensions.get("cache_invalidator")
    if invalidator is not None:
        invalidator.schedule()


def _cfg() -> WebConfig:
    return cast("WebConfig", current_app.config["teslausb_config"])


def _wraps_folder() -> Path:
    cfg = _cfg()
    return cfg.paths.backing_root / _LIGHTSHOW_DIRNAME / cfg.wraps.folder


def _get_service() -> WrapService:
    service = current_app.extensions["wrap_service"]
    if not isinstance(service, WrapService):
        raise RuntimeError("wrap_service extension is not configured")
    return service


def _wants_json_response() -> bool:
    return request.headers.get("X-Requested-With") == _XHR_HEADER_VALUE or request.is_json


def _json_error_payload(message: str) -> Response:
    return jsonify({"success": False, "error": message})


def _json_success_payload(**fields: object) -> Response:
    return jsonify({"success": True, **fields})


def _redirect_to_wraps(*, cache_bust: bool = False) -> Response:
    if cache_bust:
        return cast("Response", redirect(url_for("wraps.wraps", _=int(time.time()))))
    return cast("Response", redirect(url_for("wraps.wraps")))


def _flash_or_json_error(message: str, status: HTTPStatus) -> ResponseReturnValue:
    logger.warning("wraps request rejected: %s", message)
    if _wants_json_response():
        return _json_error_payload(message), status
    flash(message, "error")
    return _redirect_to_wraps()


def _mutation_success(
    message: str,
    *,
    cache_bust: bool = False,
    **fields: object,
) -> ResponseReturnValue:
    _invalidate_caches(current_app)
    if _wants_json_response():
        return _json_success_payload(message=message, **fields), HTTPStatus.OK
    flash(message, "success")
    return _redirect_to_wraps(cache_bust=cache_bust)


def _upload_error_status(message: str) -> HTTPStatus:
    if "1 mb or less" in message.casefold():
        return HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    return HTTPStatus.BAD_REQUEST


def _format_size_bytes(size_bytes: int) -> str:
    if size_bytes < _BYTES_PER_KIB:
        return f"{size_bytes} B"
    kib = size_bytes / _BYTES_PER_KIB
    if kib < _BYTES_PER_KIB:
        return f"{kib:.1f} KB"
    return f"{kib / _BYTES_PER_KIB:.2f} MB"


def _safe_wrap_filename(filename: str) -> str:
    cfg = _cfg()
    candidate = filename.strip()
    if not candidate:
        raise WrapError("Filename is required")
    if candidate in {".", ".."}:
        raise WrapError(f"Invalid filename: {filename!r}")
    if Path(candidate).name != candidate or any(
        token in candidate for token in _PATH_TRAVERSAL_TOKENS
    ):
        raise WrapError(f"Invalid filename: {filename!r}")
    if Path(candidate).suffix.lower() not in cfg.wraps.allowed_extensions:
        raise WrapError("Only PNG files are allowed")
    stem = Path(candidate).stem
    if len(stem) > cfg.wraps.max_filename_length:
        raise WrapError(
            "Filename must be "
            f"{cfg.wraps.max_filename_length} characters or less (currently {len(stem)})"
        )
    if not stem:
        raise WrapError("Filename cannot be empty")
    if _VALID_FILENAME_PATTERN.fullmatch(stem) is None:
        raise WrapError(
            "Filename can only contain letters, numbers, underscores, dashes, and spaces"
        )
    return candidate


def _serialize_wrap_info(file_info: WrapInfo) -> dict[str, object]:
    dimensions = "Unknown"
    if file_info.width is not None and file_info.height is not None:
        dimensions = f"{file_info.width}x{file_info.height}"
    return {
        "filename": file_info.filename,
        "size": file_info.size_bytes,
        "size_str": _format_size_bytes(file_info.size_bytes),
        "width": file_info.width,
        "height": file_info.height,
        "dimensions": dimensions,
        "modified_at": file_info.modified_at.isoformat(),
        "partition_key": _LIBRARY_PARTITION_KEY,
        "partition": _LIBRARY_LABEL,
    }


def _index_context() -> dict[str, object]:
    service = _get_service()
    wraps = service.list_wraps()
    cfg = _cfg()
    wrap_files = [_serialize_wrap_info(file_info) for file_info in wraps]
    return {
        "page": "media",
        "media_tab": "wraps",
        "wraps_available": True,
        "auto_refresh": False,
        "operation_in_progress": False,
        "wrap_files": wrap_files,
        "wrap_count": len(wrap_files),
        "max_wrap_count": cfg.wraps.max_upload_count,
        "max_file_size": cfg.wraps.max_size,
        "min_dimension": cfg.wraps.min_dimension,
        "max_dimension": cfg.wraps.max_dimension,
        "max_filename_length": cfg.wraps.max_filename_length,
    }


def _request_list(*names: str) -> list[str]:
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        for name in names:
            value = payload.get(name)
            if isinstance(value, list):
                return [str(item) for item in value if str(item)]
            if value is not None:
                return [str(value)]
    for name in names:
        values = request.form.getlist(name)
        if values:
            return [value for value in values if value]
        single = request.form.get(name)
        if single:
            return [single]
    return []


def _wrap_dimensions(filename: str) -> str | None:
    safe_name = _safe_wrap_filename(filename)
    for wrap in _get_service().list_wraps():
        if wrap.filename != safe_name:
            continue
        if wrap.width is None or wrap.height is None:
            return None
        return f"{wrap.width}x{wrap.height}"
    return None


@wraps_bp.route("/")
def wraps() -> ResponseReturnValue:
    try:
        context = _index_context()
        return render_template("wraps.html", **context)
    except WrapError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.BAD_REQUEST
    except WrapFileError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.INTERNAL_SERVER_ERROR
    except ValueError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.BAD_REQUEST
    except Exception:
        logger.exception("Unhandled error while preparing wraps index")
        return _json_error_payload("Internal server error"), HTTPStatus.INTERNAL_SERVER_ERROR


@wraps_bp.route("/download/<partition>/<filename>")
def download_wrap(partition: str, filename: str) -> ResponseReturnValue:
    del partition
    try:
        safe_name = _safe_wrap_filename(filename)
        exists = any(file_info.filename == safe_name for file_info in _get_service().list_wraps())
        if not exists:
            return _flash_or_json_error("File not found", HTTPStatus.NOT_FOUND)
        return send_from_directory(
            _wraps_folder(),
            safe_name,
            mimetype="image/png",
            as_attachment=True,
            download_name=safe_name,
        )
    except WrapError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except WrapFileError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
    except ValueError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except Exception:
        logger.exception("Unhandled error while downloading wrap %s", filename)
        return _flash_or_json_error("Internal server error", HTTPStatus.INTERNAL_SERVER_ERROR)


@wraps_bp.route("/upload_multiple", methods=["POST"])
def upload_multiple_wraps() -> ResponseReturnValue:  # noqa: PLR0911, PLR0912
    uploads = [file for file in request.files.getlist("wrap_files") if file.filename is not None]
    if not uploads:
        return _flash_or_json_error("No files selected", HTTPStatus.BAD_REQUEST)
    try:
        service = _get_service()
        max_wrap_count = _cfg().wraps.max_upload_count
        current_count = service.get_wrap_count()
        results: list[dict[str, object]] = []
        total_uploaded = 0
        for uploaded_file in uploads:
            raw_name = uploaded_file.filename
            if raw_name is None or raw_name == "":
                continue
            if current_count + total_uploaded >= max_wrap_count:
                results.append(
                    {
                        "filename": raw_name,
                        "success": False,
                        "message": f"Maximum of {max_wrap_count} wraps allowed",
                        "dimensions": None,
                    }
                )
                continue
            result = service.upload_files((uploaded_file,))
            results.append(
                {
                    "filename": raw_name,
                    "success": result.success,
                    "message": result.message,
                    "dimensions": _wrap_dimensions(raw_name) if result.success else None,
                }
            )
            if result.success:
                total_uploaded += result.file_count
        if not results:
            return _flash_or_json_error("No files selected", HTTPStatus.BAD_REQUEST)
        success_count = sum(1 for result in results if result["success"] is True)
        if total_uploaded > 0:
            _invalidate_caches(current_app)
        if _wants_json_response():
            return (
                jsonify(
                    {
                        "success": success_count > 0,
                        "results": results,
                        "total_uploaded": total_uploaded,
                        "summary": (
                            f"Successfully uploaded {total_uploaded} wrap(s) "
                            f"from {success_count}/{len(results)} file(s)"
                        ),
                    }
                ),
                HTTPStatus.OK,
            )
        if success_count > 0:
            flash(f"Successfully uploaded {total_uploaded} wrap(s)", "success")
        else:
            flash("Failed to upload wraps", "error")
        return _redirect_to_wraps(cache_bust=True)
    except WrapError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except WrapFileError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
    except ValueError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except Exception:
        logger.exception("Unhandled error while uploading multiple wraps")
        return _flash_or_json_error("Internal server error", HTTPStatus.INTERNAL_SERVER_ERROR)


@wraps_bp.route("/upload", methods=["POST"])
def upload_wrap() -> ResponseReturnValue:  # noqa: PLR0911
    if "wrap_file" not in request.files:
        return _flash_or_json_error("No file selected", HTTPStatus.BAD_REQUEST)
    uploaded_file = request.files["wrap_file"]
    if uploaded_file.filename is None or uploaded_file.filename == "":
        return _flash_or_json_error("No file selected", HTTPStatus.BAD_REQUEST)
    try:
        service = _get_service()
        max_wrap_count = _cfg().wraps.max_upload_count
        if service.get_wrap_count() >= max_wrap_count:
            return _flash_or_json_error(
                f"Maximum of {max_wrap_count} wraps allowed. Delete some wraps first.",
                HTTPStatus.BAD_REQUEST,
            )
        result = service.upload_files((uploaded_file,))
        if not result.success:
            return _flash_or_json_error(result.message, _upload_error_status(result.message))
        return _mutation_success(result.message, cache_bust=True, file_count=result.file_count)
    except WrapError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except WrapFileError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
    except ValueError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except Exception:
        logger.exception("Unhandled error while uploading wrap")
        return _flash_or_json_error("Internal server error", HTTPStatus.INTERNAL_SERVER_ERROR)


@wraps_bp.route("/delete/<partition>/<filename>", methods=["POST"])
def delete_wrap(partition: str, filename: str) -> ResponseReturnValue:
    del partition
    try:
        result = _get_service().delete_wrap(_safe_wrap_filename(filename))
        if not result.success:
            return _flash_or_json_error(result.message, HTTPStatus.BAD_REQUEST)
        return _mutation_success(result.message, deleted_count=result.deleted_count)
    except WrapError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except WrapFileError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
    except ValueError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except Exception:
        logger.exception("Unhandled error while deleting wrap %s", filename)
        return _flash_or_json_error("Internal server error", HTTPStatus.INTERNAL_SERVER_ERROR)


@wraps_bp.route("/bulk_delete", methods=["POST"])
def bulk_delete_wraps() -> ResponseReturnValue:
    try:
        filenames = _request_list("filenames", "files")
        result = _get_service().bulk_delete(filenames)
        if not result.success:
            return _flash_or_json_error(result.message, HTTPStatus.BAD_REQUEST)
        return _mutation_success(result.message, deleted_count=result.deleted_count)
    except WrapError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except WrapFileError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
    except ValueError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except Exception:
        logger.exception("Unhandled error while bulk deleting wraps")
        return _flash_or_json_error("Internal server error", HTTPStatus.INTERNAL_SERVER_ERROR)


__all__ = ("wraps_bp",)
