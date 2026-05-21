"""Light-show blueprint.

Ports v1's light-show routes onto the B-1 service layer. The HTML
template lands in Phase 5.9c; until then the index route returns the
placeholder context payload the template will consume.
"""

from __future__ import annotations

import logging
import zipfile
from http import HTTPStatus
from io import BytesIO
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
    request,
    send_file,
    send_from_directory,
    url_for,
)

from teslausb_web.services.light_show_service import (
    LightShowError,
    LightShowFile,
    LightShowFileError,
    LightShowService,
    UploadResult,
)

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue
    from werkzeug.datastructures import FileStorage

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

light_shows_bp = Blueprint("light_shows", __name__, url_prefix="/light_shows")

_AUDIO_EXTENSIONS: Final[frozenset[str]] = frozenset({".mp3", ".wav"})
_BYTES_PER_KIB: Final[int] = 1024
_LIGHTSHOW_DIRNAME: Final[str] = "lightshow"
_LIBRARY_LABEL: Final[str] = "LightShow"
_LIBRARY_PARTITION_KEY: Final[str] = "library"
_XHR_HEADER_VALUE: Final[str] = "XMLHttpRequest"
_ZIP_SUFFIX: Final[str] = ".zip"


def _invalidate_caches(app: Flask) -> None:
    invalidator = app.extensions.get("cache_invalidator")
    if invalidator is not None:
        invalidator.schedule()


def _cfg() -> WebConfig:
    return cast("WebConfig", current_app.config["teslausb_config"])


def _light_show_folder() -> Path:
    cfg = _cfg()
    return cfg.paths.backing_root / _LIGHTSHOW_DIRNAME / cfg.light_shows.folder


def _get_service() -> LightShowService:
    service = current_app.extensions["light_show_service"]
    if not isinstance(service, LightShowService):
        raise RuntimeError("light_show_service extension is not configured")
    return service


def _wants_json_response() -> bool:
    return request.headers.get("X-Requested-With") == _XHR_HEADER_VALUE or request.is_json


def _json_error_payload(message: str) -> Response:
    return jsonify({"success": False, "error": message})


def _json_success_payload(**fields: object) -> Response:
    return jsonify({"success": True, **fields})


def _flash_or_json_error(message: str, status: HTTPStatus) -> ResponseReturnValue:
    logger.warning("light_shows request rejected: %s", message)
    if _wants_json_response():
        return _json_error_payload(message), status
    flash(message, "error")
    return redirect(url_for("light_shows.light_shows"))


def _flash_or_json_success(message: str, **fields: object) -> ResponseReturnValue:
    _invalidate_caches(current_app)
    if _wants_json_response():
        return _json_success_payload(message=message, **fields), HTTPStatus.OK
    flash(message, "success")
    return redirect(url_for("light_shows.light_shows"))


def _upload_error_status(message: str) -> HTTPStatus:
    if "limit is" in message.casefold():
        return HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    return HTTPStatus.BAD_REQUEST


def _safe_base_name(base_name: str) -> str:
    safe_name = _safe_plain_name(base_name)
    if Path(safe_name).suffix:
        raise LightShowError(f"Invalid light show name: {base_name!r}")
    return safe_name


def _safe_library_filename(filename: str) -> str:
    safe_name = _safe_plain_name(filename)
    if Path(safe_name).suffix.lower() not in _cfg().light_shows.allowed_extensions:
        raise LightShowError("Only fseq, mp3, and wav files are allowed")
    return safe_name


def _safe_zip_filename(filename: str) -> str:
    safe_name = _safe_plain_name(filename)
    if Path(safe_name).suffix.lower() != _ZIP_SUFFIX:
        raise LightShowError("Filename must end with .zip")
    return safe_name


def _safe_plain_name(name: str) -> str:
    candidate = name.strip()
    if not candidate:
        raise LightShowError("Filename is required")
    path_tokens = ("/", "\\", "..", "\x00")
    if Path(candidate).name != candidate or any(token in candidate for token in path_tokens):
        raise LightShowError(f"Invalid filename: {name!r}")
    if candidate in {".", ".."}:
        raise LightShowError(f"Invalid filename: {name!r}")
    return candidate


def _format_size_bytes(size_bytes: int) -> str:
    if size_bytes < _BYTES_PER_KIB:
        return f"{size_bytes} B"
    kib = size_bytes / _BYTES_PER_KIB
    if kib < _BYTES_PER_KIB:
        return f"{kib:.1f} KB"
    return f"{kib / _BYTES_PER_KIB:.2f} MB"


def _serialize_light_show_file(file_info: LightShowFile) -> dict[str, object]:
    return {
        "filename": file_info.filename,
        "size": file_info.size_bytes,
        "size_str": _format_size_bytes(file_info.size_bytes),
        "modified_at": file_info.modified_at.isoformat(),
    }


def _build_show_groups(
    files: tuple[LightShowFile, ...],
    *,
    active_show: str | None,
) -> list[dict[str, object]]:
    grouped: dict[str, dict[str, object]] = {}
    for file_info in files:
        base_name = Path(file_info.filename).stem
        group = grouped.setdefault(
            base_name,
            {
                "base_name": base_name,
                "fseq_file": None,
                "audio_file": None,
                "partition_key": _LIBRARY_PARTITION_KEY,
                "partition": _LIBRARY_LABEL,
                "is_active": False,
            },
        )
        payload = {
            "filename": file_info.filename,
            "size": file_info.size_bytes,
            "size_str": _format_size_bytes(file_info.size_bytes),
        }
        if Path(file_info.filename).suffix.lower() == ".fseq":
            group["fseq_file"] = payload
        else:
            group["audio_file"] = payload
        if active_show is not None and file_info.filename == active_show:
            group["is_active"] = True
    return [grouped[name] for name in sorted(grouped, key=str.lower)]


def _index_context() -> dict[str, object]:
    service = _get_service()
    files = service.list_files()
    active_show = service.get_active_show()
    return {
        "page": "media",
        "media_tab": "shows",
        "shows_available": True,
        "auto_refresh": False,
        "show_groups": _build_show_groups(files, active_show=active_show),
        "active_show": active_show,
    }


def _single_upload_result(uploaded_file: FileStorage) -> UploadResult:
    service = _get_service()
    filename = uploaded_file.filename
    if filename is None or not filename:
        return UploadResult(success=False, message="No file selected", file_count=0)
    if Path(filename).suffix.lower() == _ZIP_SUFFIX:
        _safe_zip_filename(filename)
        return service.upload_zip(uploaded_file)
    return service.upload_files((uploaded_file,))


def _iter_request_uploads(*field_names: str) -> list[FileStorage]:
    return [
        file
        for field_name in field_names
        for file in request.files.getlist(field_name)
        if file.filename is not None
    ]


def _request_list(*names: str) -> list[str]:
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        for name in names:
            value = payload.get(name)
            if isinstance(value, list):
                return [str(item) for item in value]
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


def _filenames_for_base_names(base_names: list[str]) -> list[str]:
    if not base_names:
        return []
    normalized = {_safe_base_name(base_name) for base_name in base_names}
    filenames = [
        file_info.filename
        for file_info in _get_service().list_files()
        if Path(file_info.filename).stem in normalized
    ]
    return list(dict.fromkeys(filenames))


def _matching_files_for_base_name(base_name: str) -> tuple[LightShowFile, ...]:
    safe_name = _safe_base_name(base_name)
    matches = tuple(
        file_info
        for file_info in _get_service().list_files()
        if Path(file_info.filename).stem == safe_name
    )
    if not matches:
        raise LightShowError(f"File not found: {safe_name}")
    return matches


def _mimetype_for_filename(filename: str) -> str:
    if Path(filename).suffix.lower() == ".wav":
        return "audio/wav"
    return "audio/mpeg"


@light_shows_bp.route("/")
def light_shows() -> ResponseReturnValue:
    """Return the placeholder context payload until the 5.9c HTML template lands."""
    try:
        return jsonify(_index_context()), HTTPStatus.OK
    except LightShowError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.BAD_REQUEST
    except LightShowFileError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.INTERNAL_SERVER_ERROR
    except ValueError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.BAD_REQUEST
    except Exception:
        logger.exception("Unhandled error while preparing light_shows index")
        return _json_error_payload("Internal server error"), HTTPStatus.INTERNAL_SERVER_ERROR


@light_shows_bp.route("/list", methods=["GET"])
def list_light_shows() -> ResponseReturnValue:
    """Return the light-show library grouped by base filename."""
    try:
        service = _get_service()
        files = service.list_files()
        active_show = service.get_active_show()
        return (
            _json_success_payload(
                files=[_serialize_light_show_file(file_info) for file_info in files],
                show_groups=_build_show_groups(files, active_show=active_show),
                active_show=active_show,
            ),
            HTTPStatus.OK,
        )
    except LightShowError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.BAD_REQUEST
    except LightShowFileError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.INTERNAL_SERVER_ERROR
    except ValueError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.BAD_REQUEST
    except Exception:
        logger.exception("Unhandled error while listing light shows")
        return _json_error_payload("Internal server error"), HTTPStatus.INTERNAL_SERVER_ERROR


@light_shows_bp.route("/active", methods=["GET"])
def get_active_light_show() -> ResponseReturnValue:
    """Return the active light-show filename, if one is selected."""
    try:
        return _json_success_payload(filename=_get_service().get_active_show()), HTTPStatus.OK
    except LightShowError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.BAD_REQUEST
    except LightShowFileError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.INTERNAL_SERVER_ERROR
    except ValueError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.BAD_REQUEST
    except Exception:
        logger.exception("Unhandled error while reading active light show")
        return _json_error_payload("Internal server error"), HTTPStatus.INTERNAL_SERVER_ERROR


@light_shows_bp.route("/play/<partition>/<filename>")
def play_light_show_audio(partition: str, filename: str) -> ResponseReturnValue:
    """Stream one light-show audio file from the library."""
    del partition
    try:
        safe_name = _safe_library_filename(filename)
        if Path(safe_name).suffix.lower() not in _AUDIO_EXTENSIONS:
            raise LightShowError("Only mp3 and wav files can be played")
        exists = any(file_info.filename == safe_name for file_info in _get_service().list_files())
        if not exists:
            return "File not found", HTTPStatus.NOT_FOUND
        return send_from_directory(
            _light_show_folder(),
            safe_name,
            mimetype=_mimetype_for_filename(safe_name),
        )
    except LightShowError as exc:
        return str(exc), HTTPStatus.BAD_REQUEST
    except LightShowFileError as exc:
        return str(exc), HTTPStatus.INTERNAL_SERVER_ERROR
    except ValueError as exc:
        return str(exc), HTTPStatus.BAD_REQUEST
    except Exception:
        logger.exception("Unhandled error while streaming light show audio %s", filename)
        return "Internal server error", HTTPStatus.INTERNAL_SERVER_ERROR


@light_shows_bp.route("/download/<partition>/<base_name>")
def download_light_show(partition: str, base_name: str) -> ResponseReturnValue:
    """Download one grouped light show as a ZIP archive."""
    del partition
    try:
        matches = _matching_files_for_base_name(base_name)
        buffer = BytesIO()
        folder = _light_show_folder()
        with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file_info in matches:
                archive.writestr(file_info.filename, (folder / file_info.filename).read_bytes())
        buffer.seek(0)
        return send_file(
            buffer,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"{_safe_base_name(base_name)}.zip",
        )
    except LightShowError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except LightShowFileError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
    except ValueError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except Exception:
        logger.exception("Unhandled error while downloading light show %s", base_name)
        return _flash_or_json_error("Internal server error", HTTPStatus.INTERNAL_SERVER_ERROR)


@light_shows_bp.route("/upload_multiple", methods=["POST"])
def upload_multiple_light_shows() -> ResponseReturnValue:  # noqa: PLR0911, PLR0912
    """Upload multiple light-show files, preserving v1's aggregate JSON result shape."""
    uploads = _iter_request_uploads("show_files")
    if not uploads:
        return _flash_or_json_error("No files selected", HTTPStatus.BAD_REQUEST)
    try:
        results: list[dict[str, object]] = []
        total_uploaded = 0
        for uploaded_file in uploads:
            if uploaded_file.filename is None or uploaded_file.filename == "":
                continue
            result = _single_upload_result(uploaded_file)
            results.append(
                {
                    "filename": uploaded_file.filename,
                    "success": result.success,
                    "message": result.message,
                    "file_count": result.file_count,
                }
            )
            if result.success:
                total_uploaded += result.file_count
        if not results:
            return _flash_or_json_error("No files selected", HTTPStatus.BAD_REQUEST)
        success_count = sum(1 for result in results if result["success"] is True)
        summary = (
            f"Successfully uploaded {total_uploaded} file(s) "
            f"from {success_count}/{len(results)} submission(s)"
        )
        if total_uploaded > 0:
            _invalidate_caches(current_app)
        if _wants_json_response():
            payload = {
                "success": success_count > 0,
                "results": results,
                "total_uploaded": total_uploaded,
                "summary": summary,
            }
            return jsonify(payload), HTTPStatus.OK
        if total_uploaded > 0:
            flash(f"Successfully uploaded {total_uploaded} file(s)", "success")
        else:
            flash("Failed to upload files", "error")
        return redirect(url_for("light_shows.light_shows"))
    except LightShowError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except LightShowFileError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
    except ValueError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except Exception:
        logger.exception("Unhandled error while uploading multiple light shows")
        return _flash_or_json_error("Internal server error", HTTPStatus.INTERNAL_SERVER_ERROR)


@light_shows_bp.route("/upload", methods=["POST"])
def upload_light_show() -> ResponseReturnValue:  # noqa: PLR0911
    """Upload one file or one ZIP archive, with optional multi-file fallback."""
    uploads = _iter_request_uploads("show_file", "show_files")
    if not uploads:
        return _flash_or_json_error("No file selected", HTTPStatus.BAD_REQUEST)
    try:
        service = _get_service()
        if len(uploads) > 1 and all(
            file.filename is not None and Path(file.filename).suffix.lower() != _ZIP_SUFFIX
            for file in uploads
        ):
            result = service.upload_files(uploads)
        else:
            result = _single_upload_result(uploads[0])
        if not result.success:
            return _flash_or_json_error(result.message, _upload_error_status(result.message))
        return _flash_or_json_success(result.message, file_count=result.file_count)
    except LightShowError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except LightShowFileError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
    except ValueError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except Exception:
        logger.exception("Unhandled error while uploading light show")
        return _flash_or_json_error("Internal server error", HTTPStatus.INTERNAL_SERVER_ERROR)


@light_shows_bp.route("/upload_zip", methods=["POST"])
def upload_light_show_zip() -> ResponseReturnValue:  # noqa: PLR0911
    """Upload one ZIP archive of light-show files."""
    if "show_file" not in request.files:
        return _flash_or_json_error("No file selected", HTTPStatus.BAD_REQUEST)
    uploaded_file = request.files["show_file"]
    if uploaded_file.filename is None or not uploaded_file.filename:
        return _flash_or_json_error("No file selected", HTTPStatus.BAD_REQUEST)
    try:
        _safe_zip_filename(uploaded_file.filename)
        result = _get_service().upload_zip(uploaded_file)
        if not result.success:
            return _flash_or_json_error(result.message, _upload_error_status(result.message))
        return _flash_or_json_success(result.message, file_count=result.file_count)
    except LightShowError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except LightShowFileError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
    except ValueError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except Exception:
        logger.exception("Unhandled error while uploading light-show ZIP")
        return _flash_or_json_error("Internal server error", HTTPStatus.INTERNAL_SERVER_ERROR)


@light_shows_bp.route("/delete/<partition>/<base_name>", methods=["POST", "DELETE"])
def delete_light_show(partition: str, base_name: str) -> ResponseReturnValue:
    """Delete all files that share one v1 base name."""
    del partition
    try:
        filenames = _filenames_for_base_names([base_name])
        result = _get_service().bulk_delete(filenames)
        if not result.success:
            return _flash_or_json_error(result.message, HTTPStatus.BAD_REQUEST)
        return _flash_or_json_success(result.message)
    except LightShowError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except LightShowFileError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
    except ValueError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except Exception:
        logger.exception("Unhandled error while deleting light show %s", base_name)
        return _flash_or_json_error("Internal server error", HTTPStatus.INTERNAL_SERVER_ERROR)


@light_shows_bp.route("/delete/<filename>", methods=["POST", "DELETE"])
def delete_light_show_file(filename: str) -> ResponseReturnValue:
    """Delete one light-show library file by filename."""
    try:
        result = _get_service().delete_file(_safe_library_filename(filename))
        if not result.success:
            return _flash_or_json_error(result.message, HTTPStatus.BAD_REQUEST)
        return _flash_or_json_success(result.message)
    except LightShowError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except LightShowFileError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
    except ValueError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except Exception:
        logger.exception("Unhandled error while deleting light-show file %s", filename)
        return _flash_or_json_error("Internal server error", HTTPStatus.INTERNAL_SERVER_ERROR)


@light_shows_bp.route("/bulk_delete", methods=["POST"])
def bulk_delete_light_shows() -> ResponseReturnValue:
    """Delete multiple files, accepting either filenames or v1 base names."""
    try:
        filenames = _request_list("filenames", "files")
        if not filenames:
            filenames = _filenames_for_base_names(_request_list("base_names"))
        result = _get_service().bulk_delete(filenames)
        if not result.success:
            return _flash_or_json_error(result.message, HTTPStatus.BAD_REQUEST)
        return _flash_or_json_success(result.message)
    except LightShowError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except LightShowFileError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
    except ValueError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except Exception:
        logger.exception("Unhandled error while bulk deleting light shows")
        return _flash_or_json_error("Internal server error", HTTPStatus.INTERNAL_SERVER_ERROR)


@light_shows_bp.route("/set_active/<filename>", methods=["POST"])
def set_active_light_show(filename: str) -> ResponseReturnValue:
    """Persist the active light-show filename."""
    try:
        safe_name = _safe_library_filename(filename)
        _get_service().set_active_show(safe_name)
        return _flash_or_json_success(f"Set active light show to {safe_name}")
    except LightShowError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except LightShowFileError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
    except ValueError as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    except Exception:
        logger.exception("Unhandled error while setting active light show %s", filename)
        return _flash_or_json_error("Internal server error", HTTPStatus.INTERNAL_SERVER_ERROR)


__all__ = ("light_shows_bp",)
