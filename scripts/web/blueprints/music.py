"""Blueprint for music library management."""

import logging
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app

from utils import get_base_context, format_file_size
from services.music_service import (
    list_music_files,
    save_file,
    handle_chunk,
    delete_music_file,
    UploadError,
    generate_upload_id,
    require_edit_mode,
)

music_bp = Blueprint("music", __name__, url_prefix="/music")
logger = logging.getLogger(__name__)


@music_bp.route("/")
def music_home():
    ctx = get_base_context()
    files, error, total_size, free_bytes = list_music_files()
    return render_template(
        "music.html",
        page="music",
        **ctx,
        files=files,
        error=error,
        total_size=total_size,
        free_bytes=free_bytes,
        format_file_size=format_file_size,
        max_upload_size_mb=current_app.config.get("MAX_CONTENT_LENGTH", 0) // (1024 * 1024),
        max_upload_chunk_mb=current_app.config.get("MAX_FORM_MEMORY_SIZE", 0) // (1024 * 1024),
    )


@music_bp.route("/upload", methods=["POST"])
def upload_music():
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    try:
        require_edit_mode()
    except UploadError as exc:
        if is_ajax:
            return jsonify({"success": False, "error": str(exc)}), 400
        flash(str(exc), "error")
        return redirect(url_for("music.music_home"))

    files = request.files.getlist("music_files")
    if not files:
        if is_ajax:
            return jsonify({"success": False, "error": "No files selected"}), 400
        flash("No files selected", "error")
        return redirect(url_for("music.music_home"))

    successes = 0
    messages = []
    for file in files:
        if not file or not file.filename:
            continue
        ok, msg = save_file(file)
        if ok:
            successes += 1
        messages.append(msg)

    if is_ajax:
        status = 200 if successes else 400
        return jsonify({
            "success": successes > 0,
            "messages": messages,
            "uploaded": successes,
        }), status

    if successes:
        flash(f"Uploaded {successes} file(s)", "success")
    else:
        flash("Failed to upload files", "error")
    return redirect(url_for("music.music_home", _=request.args.get('_', 0)))


@music_bp.route("/upload_chunk", methods=["POST"])
def upload_chunk():
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    try:
        require_edit_mode()
    except UploadError as exc:
        if is_ajax:
            return jsonify({"success": False, "error": str(exc)}), 400
        flash(str(exc), "error")
        return redirect(url_for("music.music_home"))

    try:
        upload_id = request.args.get("upload_id") or request.form.get("upload_id") or generate_upload_id()
        filename = request.args.get("filename") or request.form.get("filename")
        chunk_index = int(request.args.get("chunk_index") or request.form.get("chunk_index") or 0)
        total_chunks = int(request.args.get("total_chunks") or request.form.get("total_chunks") or 1)
        total_size = int(request.args.get("total_size") or request.form.get("total_size") or request.headers.get("X-File-Size") or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid chunk metadata"}), 400

    if not filename:
        return jsonify({"success": False, "error": "Missing filename"}), 400
    if total_size <= 0:
        return jsonify({"success": False, "error": "Missing file size"}), 400

    try:
        success, message, finalized = handle_chunk(
            upload_id=upload_id,
            filename=filename,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
            total_size=total_size,
            stream=request.stream,
        )
    except UploadError as exc:
        logger.warning("Chunk upload rejected: %s", exc)
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Chunk upload failed: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": "Server error"}), 500

    return jsonify({"success": success, "message": message, "finalized": finalized})


@music_bp.route("/delete/<path:filename>", methods=["POST"])
def delete_music(filename):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    try:
        require_edit_mode()
    except UploadError as exc:
        if is_ajax:
            return jsonify({"success": False, "error": str(exc)}), 400
        flash(str(exc), "error")
        return redirect(url_for("music.music_home"))

    try:
        ok, msg = delete_music_file(filename)
    except UploadError as exc:
        ok, msg = False, str(exc)

    if is_ajax:
        status = 200 if ok else 400
        return jsonify({"success": ok, "message": msg}), status

    flash(msg, "success" if ok else "error")
    return redirect(url_for("music.music_home", _=request.args.get('_', 0)))
