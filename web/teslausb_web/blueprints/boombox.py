"""Boombox-management blueprint."""

from __future__ import annotations

import logging
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
    send_file,
    url_for,
)

from teslausb_web.services.boombox_service import (
    BoomboxError,
    BoomboxFile,
    BoomboxFileError,
    BoomboxListing,
    BoomboxService,
)

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

boombox_bp = Blueprint("boombox", __name__, url_prefix="/boombox")

_BYTES_PER_KIB: Final[int] = 1024
_PATH_TRAVERSAL_TOKENS: Final[frozenset[str]] = frozenset({"/", "\\", "..", "\x00"})
_XHR_HEADER_VALUE: Final[str] = "XMLHttpRequest"
_MIME_TYPES: Final[dict[str, str]] = {".mp3": "audio/mpeg", ".wav": "audio/wav"}
_TESLA_GUIDANCE_MAX_SECONDS: Final[int] = 5


def _invalidate_caches(app: Flask) -> None:
    invalidator = app.extensions.get("cache_invalidator")
    if invalidator is not None:
        invalidator.schedule()


def _cfg() -> WebConfig:
    return cast("WebConfig", current_app.config["teslausb_config"])


def _boombox_folder() -> Path:
    return _cfg().paths.media_root / _cfg().music.folder / _cfg().boombox.base_dir


def _get_service() -> BoomboxService:
    service = current_app.extensions["boombox_service"]
    if not isinstance(service, BoomboxService):
        raise RuntimeError("boombox_service extension is not configured")
    return service


def _wants_json_response() -> bool:
    return request.headers.get("X-Requested-With") == _XHR_HEADER_VALUE or request.is_json


def _json_error_payload(message: str) -> Response:
    return jsonify({"error": message})


def _redirect_to_boombox(*, cache_bust: str | None = None) -> Response:
    if cache_bust is None:
        return cast("Response", redirect(url_for("boombox.boombox_home")))
    return cast("Response", redirect(url_for("boombox.boombox_home", _=cache_bust)))


def _boombox_response(
    *,
    success: bool,
    message: str,
    status: HTTPStatus,
    **fields: object,
) -> ResponseReturnValue:
    if _wants_json_response():
        return jsonify({"success": success, "message": message, **fields}), status
    flash(message, "success" if success else "error")
    return _redirect_to_boombox(cache_bust=request.args.get("_", "0"))


def _boundary_error(
    exc: BoomboxError | BoomboxFileError | ValueError, *, json_only: bool = False
) -> ResponseReturnValue:
    status = (
        HTTPStatus.INTERNAL_SERVER_ERROR
        if isinstance(exc, BoomboxFileError)
        else HTTPStatus.BAD_REQUEST
    )
    if json_only:
        return _json_error_payload(str(exc)), status
    return _boombox_response(success=False, message=str(exc), status=status)


def _format_size_bytes(size_bytes: int) -> str:
    if size_bytes < _BYTES_PER_KIB:
        return f"{size_bytes} B"
    kib = size_bytes / _BYTES_PER_KIB
    if kib < _BYTES_PER_KIB:
        return f"{kib:.1f} KB"
    return f"{kib / _BYTES_PER_KIB:.2f} MB"


def _safe_boombox_filename(filename: str) -> str:
    candidate = filename.strip()
    if not candidate:
        raise BoomboxError("Filename is required")
    if candidate in {".", ".."}:
        raise BoomboxError(f"Invalid filename: {filename!r}")
    if Path(candidate).name != candidate or any(
        token in candidate for token in _PATH_TRAVERSAL_TOKENS
    ):
        raise BoomboxError(f"Invalid filename: {filename!r}")
    allowed_extensions = tuple(extension.lower() for extension in _cfg().boombox.allowed_extensions)
    if Path(candidate).suffix.lower() not in allowed_extensions:
        raise BoomboxError("Only MP3 and WAV files are allowed")
    return candidate


def _mime_type_for_path(filepath: str) -> str:
    return _MIME_TYPES.get(Path(filepath.lower()).suffix, "application/octet-stream")


def _serialize_boombox_file(file_info: BoomboxFile) -> dict[str, object]:
    return {
        "filename": file_info.filename,
        "size": file_info.size_bytes,
        "size_bytes": file_info.size_bytes,
        "size_str": _format_size_bytes(file_info.size_bytes),
        "modified_at": file_info.modified_at.isoformat(),
    }


def _index_payload(listing: BoomboxListing) -> dict[str, object]:
    return {
        "files": [_serialize_boombox_file(file_info) for file_info in listing.files],
        "file_count": len(listing.files),
        "max_files": listing.max_files,
        "max_file_count": listing.max_files,
        "max_file_bytes": _cfg().boombox.max_file_bytes,
    }


def _index_context(listing: BoomboxListing) -> dict[str, object]:
    context = _index_payload(listing)
    max_bytes = _cfg().boombox.max_file_bytes
    context.update(
        {
            "page": "media",
            "media_tab": "boombox",
            "boombox_available": True,
            "auto_refresh": False,
            "operation_in_progress": False,
            "boombox_folder": _cfg().boombox.base_dir,
            "max_file_size": max_bytes,
            "max_file_size_mb": max(1, max_bytes // (1024 * 1024)),
            "max_file_size_str": _format_size_bytes(max_bytes),
            "tesla_guidance_max_seconds": _TESLA_GUIDANCE_MAX_SECONDS,
        }
    )
    return context


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


def _resolve_boombox_path(filename: str) -> Path:
    safe_name = _safe_boombox_filename(filename)
    candidate = _boombox_folder() / safe_name
    try:
        is_symlink = candidate.is_symlink()
        is_file = candidate.is_file()
    except OSError as exc:
        raise BoomboxFileError(f"Failed to read boombox file {safe_name}: {exc}") from exc
    if is_symlink or not is_file:
        raise FileNotFoundError(safe_name)
    return candidate


def _delete_boombox_files(filenames: list[str]) -> int:
    unique_names = list(dict.fromkeys(_safe_boombox_filename(filename) for filename in filenames))
    if not unique_names:
        raise BoomboxError("No files selected")
    folder = _boombox_folder()
    candidates: list[Path] = []
    for safe_name in unique_names:
        candidate = folder / safe_name
        try:
            if candidate.is_symlink() or not candidate.is_file():
                raise BoomboxError(f"{safe_name}: File not found")
        except OSError as exc:
            raise BoomboxFileError(f"Failed to inspect boombox file {safe_name}: {exc}") from exc
        candidates.append(candidate)
    for candidate in candidates:
        try:
            candidate.unlink()
        except OSError as exc:
            raise BoomboxFileError(
                f"Failed to delete boombox file {candidate.name}: {exc}"
            ) from exc
    return len(candidates)


@boombox_bp.route("/")
def boombox_home() -> ResponseReturnValue:
    try:
        return render_template("boombox.html", **_index_context(_get_service().list_files()))
    except (BoomboxError, BoomboxFileError, ValueError) as exc:
        return _boundary_error(exc, json_only=True)
    except Exception:
        logger.exception("Unhandled error while preparing boombox index")
        return _json_error_payload("Internal server error"), HTTPStatus.INTERNAL_SERVER_ERROR


@boombox_bp.route("/upload", methods=["POST"])
def upload_boombox() -> ResponseReturnValue:  # noqa: PLR0912
    uploads = [file for file in request.files.getlist("boombox_files") if file.filename is not None]
    if not uploads:
        single = request.files.get("boombox_file")
        if single is not None and single.filename:
            uploads = [single]
    if not uploads or all(not uploaded_file.filename for uploaded_file in uploads):
        return _boombox_response(
            success=False,
            message="No files selected",
            status=HTTPStatus.BAD_REQUEST,
        )
    try:
        listing = _get_service().list_files()
        accepted = len(listing.files)
        results: list[dict[str, object]] = []
        successes = 0
        service = _get_service()
        for uploaded_file in uploads:
            raw_name = uploaded_file.filename
            if raw_name is None or raw_name == "":
                continue
            if accepted >= listing.max_files:
                results.append(
                    {
                        "filename": raw_name,
                        "success": False,
                        "message": f"Maximum of {listing.max_files} Boombox sounds allowed",
                    }
                )
                continue
            result = service.upload_file(uploaded_file)
            results.append(
                {
                    "filename": raw_name,
                    "success": result.success,
                    "message": result.message,
                }
            )
            if result.success:
                successes += result.file_count
                accepted += result.file_count
        if _wants_json_response():
            status = HTTPStatus.OK if successes > 0 else HTTPStatus.BAD_REQUEST
            return (
                jsonify(
                    {
                        "success": successes > 0,
                        "uploaded": successes,
                        "results": results,
                    }
                ),
                status,
            )
        if successes > 0:
            flash(f"Uploaded {successes} file(s)", "success")
        for result_entry in results:
            if result_entry["success"] is True:
                continue
            flash(f"{result_entry['filename']}: {result_entry['message']}", "error")
        return _redirect_to_boombox(cache_bust=request.args.get("_", "0"))
    except (BoomboxError, BoomboxFileError, ValueError) as exc:
        return _boundary_error(exc)
    except Exception:
        logger.exception("Unhandled error while uploading boombox files")
        return _boombox_response(
            success=False,
            message="Internal server error",
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )


@boombox_bp.route("/delete/<path:filename>", methods=["POST"])
def delete_boombox(filename: str) -> ResponseReturnValue:
    try:
        result = _get_service().delete_file(filename)
        status = HTTPStatus.OK if result.success else HTTPStatus.BAD_REQUEST
        return _boombox_response(success=result.success, message=result.message, status=status)
    except (BoomboxError, BoomboxFileError, ValueError) as exc:
        return _boundary_error(exc)
    except Exception:
        logger.exception("Unhandled error while deleting boombox file %s", filename)
        return _boombox_response(
            success=False,
            message="Internal server error",
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )


@boombox_bp.route("/bulk_delete", methods=["POST"])
def bulk_delete_boombox() -> ResponseReturnValue:
    try:
        deleted_count = _delete_boombox_files(_request_list("filenames", "files"))
        _invalidate_caches(current_app)
        return _boombox_response(
            success=True,
            message=f"Deleted {deleted_count} boombox file(s)",
            status=HTTPStatus.OK,
            deleted_count=deleted_count,
        )
    except (BoomboxError, BoomboxFileError, ValueError) as exc:
        return _boundary_error(exc)
    except Exception:
        logger.exception("Unhandled error while bulk deleting boombox files")
        return _boombox_response(
            success=False,
            message="Internal server error",
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )


@boombox_bp.route("/play/<path:filename>")
def play_boombox(filename: str) -> ResponseReturnValue:
    try:
        return send_file(_resolve_boombox_path(filename), mimetype=_mime_type_for_path(filename))
    except FileNotFoundError:
        return _json_error_payload("File not found"), HTTPStatus.NOT_FOUND
    except (BoomboxError, BoomboxFileError, ValueError) as exc:
        return _boundary_error(exc, json_only=True)
    except Exception:
        logger.exception("Unhandled error while streaming boombox file %s", filename)
        return _json_error_payload("Internal server error"), HTTPStatus.INTERNAL_SERVER_ERROR


__all__ = ("boombox_bp",)
