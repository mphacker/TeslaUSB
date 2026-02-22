"""Blueprint for music library management."""

import os
import logging
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app, send_file

from config import MUSIC_ENABLED, IMG_MUSIC_PATH
from utils import get_base_context, format_file_size
from services.music_service import (
    list_music_files,
    save_file,
    handle_chunk,
    delete_music_file,
    delete_directory,
    create_directory,
    move_music_file,
    resolve_music_file_path,
    MusicServiceError,
    generate_upload_id,
)

music_bp = Blueprint("music", __name__, url_prefix="/music")
logger = logging.getLogger(__name__)


@music_bp.before_request
def _require_music_image():
    if not MUSIC_ENABLED or not os.path.isfile(IMG_MUSIC_PATH):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"error": "Feature unavailable"}), 503
        flash("This feature is not available because the required disk image has not been created.")
        return redirect(url_for('mode_control.index'))


@music_bp.route("/")
def music_home():
    ctx = get_base_context()
    current_path = request.args.get("path", "")
    dirs, files, error, used_bytes, free_bytes, current_path, total_bytes = list_music_files(current_path)
    return render_template(
        "music.html",
        page="music",
        **ctx,
        dirs=dirs,
        files=files,
        error=error,
        used_bytes=used_bytes,
        free_bytes=free_bytes,
        total_bytes=total_bytes,
        current_path=current_path,
        format_file_size=format_file_size,
        max_upload_size_mb=current_app.config.get("MAX_CONTENT_LENGTH", 0) // (1024 * 1024),
        max_upload_chunk_mb=current_app.config.get("MAX_FORM_MEMORY_SIZE", 0) // (1024 * 1024),
    )


@music_bp.route("/upload", methods=["POST"])
def upload_music():
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    current_path = request.args.get("path") or request.form.get("path") or ""

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
        ok, msg = save_file(file, current_path)
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
    return redirect(url_for("music.music_home", path=current_path, _=request.args.get('_', 0)))


@music_bp.route("/upload_chunk", methods=["POST"])
def upload_chunk():
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    current_path = request.args.get("path") or request.form.get("path") or ""

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
            rel_path=current_path,
        )
    except MusicServiceError as exc:
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
        ok, msg = delete_music_file(filename)
    except MusicServiceError as exc:
        ok, msg = False, str(exc)

    if is_ajax:
        status = 200 if ok else 400
        return jsonify({"success": ok, "message": msg}), status

    flash(msg, "success" if ok else "error")
    return redirect(url_for("music.music_home", path=request.args.get("path", ""), _=request.args.get('_', 0)))


@music_bp.route("/delete_dir/<path:dirname>", methods=["POST"])
def delete_music_dir(dirname):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    try:
        ok, msg = delete_directory(dirname)
    except MusicServiceError as exc:
        ok, msg = False, str(exc)

    if is_ajax:
        status = 200 if ok else 400
        return jsonify({"success": ok, "message": msg}), status

    flash(msg, "success" if ok else "error")
    parent = dirname.rsplit("/", 1)[0] if "/" in dirname else ""
    return redirect(url_for("music.music_home", path=parent, _=request.args.get('_', 0)))


@music_bp.route("/move", methods=["POST"])
def move_music():
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    data = request.get_json(silent=True) or request.form
    source = data.get("source", "") if data else ""
    dest_path = data.get("dest_path", "") if data else ""
    new_name = data.get("new_name", "") if data else ""

    ok, msg = move_music_file(source, dest_path, new_name)
    if is_ajax:
        status = 200 if ok else 400
        return jsonify({"success": ok, "message": msg}), status

    flash(msg, "success" if ok else "error")
    return redirect(url_for("music.music_home", path=dest_path))


@music_bp.route("/mkdir", methods=["POST"])
def create_music_folder():
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    data = request.get_json(silent=True) or request.form
    current_path = data.get("path", "") if data else ""
    name = data.get("name", "") if data else ""

    ok, msg = create_directory(current_path, name)
    if is_ajax:
        status = 200 if ok else 400
        return jsonify({"success": ok, "message": msg}), status

    flash(msg, "success" if ok else "error")
    return redirect(url_for("music.music_home", path=current_path))


@music_bp.route("/play/<path:filepath>")
def play_music(filepath):
    """Stream a music file for in-browser playback."""
    try:
        file_path = resolve_music_file_path(filepath)
    except MusicServiceError as exc:
        flash(str(exc), "error")
        return redirect(url_for("music.music_home"))

    lower = filepath.lower()
    mime_map = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".aac": "audio/aac",
        ".m4a": "audio/mp4",
    }
    ext = os.path.splitext(lower)[1]
    mimetype = mime_map.get(ext, "application/octet-stream")
    return send_file(file_path, mimetype=mimetype)
