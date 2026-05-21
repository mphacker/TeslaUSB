"""Music-library blueprint."""

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

from teslausb_web.services.music_service import MusicError, MusicFileError, MusicService

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

    from teslausb_web.services.music_service import MusicFile, MusicListing

logger = logging.getLogger(__name__)

music_bp = Blueprint("music", __name__, url_prefix="/music")

_BYTES_PER_KIB: Final[int] = 1024
_XHR_HEADER_VALUE: Final[str] = "XMLHttpRequest"
_MIME_TYPES: Final[dict[str, str]] = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
    ".m4a": "audio/mp4",
}


def _invalidate_caches(app: Flask) -> None:
    invalidator = app.extensions.get("cache_invalidator")
    if invalidator is not None:
        invalidator.schedule()


def _get_service() -> MusicService:
    service = current_app.extensions["music_service"]
    if not isinstance(service, MusicService):
        raise RuntimeError("music_service extension is not configured")
    return service


def _wants_json_response() -> bool:
    return request.headers.get("X-Requested-With") == _XHR_HEADER_VALUE or request.is_json


def _json_error_payload(message: str) -> Response:
    return jsonify({"success": False, "error": message})


def _json_message_payload(*, success: bool, message: str, **fields: object) -> Response:
    return jsonify({"success": success, "message": message, **fields})


def _redirect_to_music(*, path: str = "", cache_bust: str | None = None) -> Response:
    if cache_bust is None and not path:
        return cast("Response", redirect(url_for("music.music_home")))
    if cache_bust is None:
        return cast("Response", redirect(url_for("music.music_home", path=path)))
    if not path:
        return cast("Response", redirect(url_for("music.music_home", _=cache_bust)))
    return cast("Response", redirect(url_for("music.music_home", path=path, _=cache_bust)))


def _format_size_bytes(size_bytes: int) -> str:
    if size_bytes < _BYTES_PER_KIB:
        return f"{size_bytes} B"
    kib = size_bytes / _BYTES_PER_KIB
    if kib < _BYTES_PER_KIB:
        return f"{kib:.1f} KB"
    return f"{kib / _BYTES_PER_KIB:.2f} MB"


def _serialize_music_file(file_info: MusicFile) -> dict[str, object]:
    return {
        "name": file_info.name,
        "path": file_info.path,
        "size": file_info.size_bytes,
        "size_bytes": file_info.size_bytes,
        "modified_at": file_info.modified_at.isoformat(),
    }


def _index_context(listing: MusicListing) -> dict[str, object]:
    return {
        "page": "media",
        "media_tab": "music",
        "music_available": True,
        "auto_refresh": False,
        "operation_in_progress": False,
        "dirs": listing.directories,
        "files": [_serialize_music_file(file_info) for file_info in listing.files],
        "error": None,
        "used_bytes": listing.used_bytes,
        "free_bytes": listing.free_bytes,
        "total_bytes": listing.total_bytes,
        "current_path": listing.relative_path,
        "format_file_size": _format_size_bytes,
        "max_upload_size_mb": current_app.config.get("MAX_CONTENT_LENGTH", 0) // (1024 * 1024),
        "max_upload_chunk_mb": current_app.config.get("MAX_FORM_MEMORY_SIZE", 0) // (1024 * 1024),
    }


def _request_value(*names: str) -> str:
    for name in names:
        value = request.args.get(name)
        if value is not None:
            return value
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
    return ""


def _music_response(
    *,
    success: bool,
    message: str,
    status: HTTPStatus,
    path: str = "",
) -> ResponseReturnValue:
    if _wants_json_response():
        return _json_message_payload(success=success, message=message), status
    flash(message, "success" if success else "error")
    return _redirect_to_music(path=path, cache_bust=request.args.get("_", "0"))


def _mime_type_for_path(filepath: str) -> str:
    return _MIME_TYPES.get(Path(filepath.lower()).suffix, "application/octet-stream")


@music_bp.route("/")
def music_home() -> ResponseReturnValue:
    try:
        listing = _get_service().list_files(request.args.get("path", ""))
        context = _index_context(listing)
        return render_template("music.html", **context)
    except MusicError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.BAD_REQUEST
    except MusicFileError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.INTERNAL_SERVER_ERROR
    except ValueError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.BAD_REQUEST
    except Exception:
        logger.exception("Unhandled error while preparing music index")
        return _json_error_payload("Internal server error"), HTTPStatus.INTERNAL_SERVER_ERROR


@music_bp.route("/upload", methods=["POST"])
def upload_music() -> ResponseReturnValue:  # noqa: PLR0911, PLR0912
    current_path = request.args.get("path") or request.form.get("path") or ""
    uploads = [file for file in request.files.getlist("music_files") if file.filename is not None]
    if not uploads:
        if _wants_json_response():
            return _json_error_payload("No files selected"), HTTPStatus.BAD_REQUEST
        flash("No files selected", "error")
        return _redirect_to_music()
    try:
        service = _get_service()
        successes = 0
        messages: list[str] = []
        for uploaded_file in uploads:
            if not uploaded_file.filename:
                continue
            result = service.save_file(uploaded_file, current_path)
            if result.success:
                successes += 1
            messages.append(result.message)
        if successes > 0:
            _invalidate_caches(current_app)
        if _wants_json_response():
            status = HTTPStatus.OK if successes > 0 else HTTPStatus.BAD_REQUEST
            return (
                jsonify(
                    {
                        "success": successes > 0,
                        "messages": messages,
                        "uploaded": successes,
                    }
                ),
                status,
            )
        if successes > 0:
            flash(f"Uploaded {successes} file(s)", "success")
        else:
            flash("Failed to upload files", "error")
        return _redirect_to_music(path=current_path, cache_bust=request.args.get("_", "0"))
    except MusicError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.BAD_REQUEST
    except MusicFileError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.INTERNAL_SERVER_ERROR
    except ValueError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.BAD_REQUEST
    except Exception:
        logger.exception("Unhandled error while uploading music")
        return _json_error_payload("Internal server error"), HTTPStatus.INTERNAL_SERVER_ERROR


@music_bp.route("/upload_chunk", methods=["POST"])
def upload_chunk() -> ResponseReturnValue:  # noqa: PLR0911
    current_path = request.args.get("path") or request.form.get("path") or ""
    try:
        service = _get_service()
        upload_id = _request_value("upload_id") or service.generate_upload_id()
        filename = _request_value("filename")
        chunk_index = int(_request_value("chunk_index") or 0)
        total_chunks = int(_request_value("total_chunks") or 1)
        total_size = int(_request_value("total_size") or request.headers.get("X-File-Size") or "0")
    except (TypeError, ValueError):
        return _json_error_payload("Invalid chunk metadata"), HTTPStatus.BAD_REQUEST
    if not filename:
        return _json_error_payload("Missing filename"), HTTPStatus.BAD_REQUEST
    if total_size <= 0:
        return _json_error_payload("Missing file size"), HTTPStatus.BAD_REQUEST
    try:
        result = service.handle_chunk(
            upload_id=upload_id,
            filename=filename,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
            total_size=total_size,
            stream=request.stream,
            rel_path=current_path,
        )
        if result.success and result.is_finalized:
            _invalidate_caches(current_app)
        return (
            jsonify(
                {
                    "success": result.success,
                    "message": result.message,
                    "finalized": result.is_finalized,
                }
            ),
            HTTPStatus.OK,
        )
    except MusicError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.BAD_REQUEST
    except MusicFileError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.INTERNAL_SERVER_ERROR
    except ValueError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.BAD_REQUEST
    except Exception:
        logger.exception("Unhandled error while uploading music chunk %s", filename)
        return _json_error_payload("Server error"), HTTPStatus.INTERNAL_SERVER_ERROR


@music_bp.route("/delete/<path:filename>", methods=["POST"])
def delete_music(filename: str) -> ResponseReturnValue:
    current_path = request.args.get("path", "")
    try:
        result = _get_service().delete_file(filename)
        if result.success:
            _invalidate_caches(current_app)
            return _music_response(
                success=True,
                message=result.message,
                status=HTTPStatus.OK,
                path=current_path,
            )
        return _music_response(
            success=False,
            message=result.message,
            status=HTTPStatus.BAD_REQUEST,
            path=current_path,
        )
    except MusicError as exc:
        return _music_response(
            success=False,
            message=str(exc),
            status=HTTPStatus.BAD_REQUEST,
            path=current_path,
        )
    except MusicFileError as exc:
        return _music_response(
            success=False,
            message=str(exc),
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            path=current_path,
        )
    except ValueError as exc:
        return _music_response(
            success=False,
            message=str(exc),
            status=HTTPStatus.BAD_REQUEST,
            path=current_path,
        )
    except Exception:
        logger.exception("Unhandled error while deleting music file %s", filename)
        return _music_response(
            success=False,
            message="Internal server error",
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            path=current_path,
        )


@music_bp.route("/delete_dir/<path:dirname>", methods=["POST"])
def delete_music_dir(dirname: str) -> ResponseReturnValue:
    parent = dirname.rsplit("/", 1)[0] if "/" in dirname else ""
    try:
        result = _get_service().delete_directory(dirname)
        if result.success:
            _invalidate_caches(current_app)
            return _music_response(
                success=True,
                message=result.message,
                status=HTTPStatus.OK,
                path=parent,
            )
        return _music_response(
            success=False,
            message=result.message,
            status=HTTPStatus.BAD_REQUEST,
            path=parent,
        )
    except MusicError as exc:
        return _music_response(
            success=False,
            message=str(exc),
            status=HTTPStatus.BAD_REQUEST,
            path=parent,
        )
    except MusicFileError as exc:
        return _music_response(
            success=False,
            message=str(exc),
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            path=parent,
        )
    except ValueError as exc:
        return _music_response(
            success=False,
            message=str(exc),
            status=HTTPStatus.BAD_REQUEST,
            path=parent,
        )
    except Exception:
        logger.exception("Unhandled error while deleting music directory %s", dirname)
        return _music_response(
            success=False,
            message="Internal server error",
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            path=parent,
        )


@music_bp.route("/move", methods=["POST"])
def move_music() -> ResponseReturnValue:
    dest_path = _request_value("dest_path")
    try:
        result = _get_service().move_file(
            _request_value("source"),
            dest_path,
            _request_value("new_name"),
        )
        if result.success:
            _invalidate_caches(current_app)
            return _music_response(
                success=True,
                message=result.message,
                status=HTTPStatus.OK,
                path=dest_path,
            )
        return _music_response(
            success=False,
            message=result.message,
            status=HTTPStatus.BAD_REQUEST,
            path=dest_path,
        )
    except MusicError as exc:
        return _music_response(
            success=False,
            message=str(exc),
            status=HTTPStatus.BAD_REQUEST,
            path=dest_path,
        )
    except MusicFileError as exc:
        return _music_response(
            success=False,
            message=str(exc),
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            path=dest_path,
        )
    except ValueError as exc:
        return _music_response(
            success=False,
            message=str(exc),
            status=HTTPStatus.BAD_REQUEST,
            path=dest_path,
        )
    except Exception:
        logger.exception("Unhandled error while moving music file")
        return _music_response(
            success=False,
            message="Internal server error",
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            path=dest_path,
        )


@music_bp.route("/mkdir", methods=["POST"])
def create_music_folder() -> ResponseReturnValue:
    current_path = _request_value("path")
    try:
        result = _get_service().create_directory(current_path, _request_value("name"))
        if result.success:
            _invalidate_caches(current_app)
            return _music_response(
                success=True,
                message=result.message,
                status=HTTPStatus.OK,
                path=current_path,
            )
        return _music_response(
            success=False,
            message=result.message,
            status=HTTPStatus.BAD_REQUEST,
            path=current_path,
        )
    except MusicError as exc:
        return _music_response(
            success=False,
            message=str(exc),
            status=HTTPStatus.BAD_REQUEST,
            path=current_path,
        )
    except MusicFileError as exc:
        return _music_response(
            success=False,
            message=str(exc),
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            path=current_path,
        )
    except ValueError as exc:
        return _music_response(
            success=False,
            message=str(exc),
            status=HTTPStatus.BAD_REQUEST,
            path=current_path,
        )
    except Exception:
        logger.exception("Unhandled error while creating music directory")
        return _music_response(
            success=False,
            message="Internal server error",
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            path=current_path,
        )


@music_bp.route("/play/<path:filepath>")
def play_music(filepath: str) -> ResponseReturnValue:
    try:
        file_path = _get_service().resolve_file_path(filepath)
        return send_file(file_path, mimetype=_mime_type_for_path(filepath))
    except MusicError as exc:
        return str(exc), HTTPStatus.BAD_REQUEST
    except MusicFileError as exc:
        return str(exc), HTTPStatus.INTERNAL_SERVER_ERROR
    except ValueError as exc:
        return str(exc), HTTPStatus.BAD_REQUEST
    except Exception:
        logger.exception("Unhandled error while streaming music file %s", filepath)
        return "Internal server error", HTTPStatus.INTERNAL_SERVER_ERROR


__all__ = ("music_bp",)
