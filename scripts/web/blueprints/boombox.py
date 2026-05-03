"""Blueprint for Boombox custom honk-sound management.

Manages MP3 files in the /Media/ folder on the music partition (LUN 2).
Tesla reads /Media/ for custom Boombox horn sounds. Files other than MP3
are rejected at upload time; the filename becomes the selectable sound name
in the Tesla UI.
"""

import os
import logging
from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    send_file,
)

from config import IMG_MUSIC_PATH, MUSIC_ENABLED
from utils import get_base_context, format_file_size
from services.boombox_service import (
    list_boombox_files,
    upload_boombox_file,
    delete_boombox_file,
    resolve_boombox_file_path,
    BoomboxServiceError,
    MAX_FILE_SIZE,
    MAX_FILE_COUNT,
)

logger = logging.getLogger(__name__)

boombox_bp = Blueprint("boombox", __name__, url_prefix="/boombox")


@boombox_bp.before_request
def _require_music_image():
    if not MUSIC_ENABLED or not os.path.isfile(IMG_MUSIC_PATH):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"error": "Feature unavailable"}), 503
        flash("This feature is not available because the Music disk image has not been created.")
        return redirect(url_for("mode_control.index"))


@boombox_bp.route("/")
def boombox_home():
    ctx = get_base_context()
    files, error, used_bytes, free_bytes = list_boombox_files()
    return render_template(
        "boombox.html",
        page="media",
        media_tab="boombox",
        **ctx,
        files=files,
        error=error,
        used_bytes=used_bytes,
        free_bytes=free_bytes,
        max_file_size=MAX_FILE_SIZE,
        max_file_count=MAX_FILE_COUNT,
        format_file_size=format_file_size,
    )


@boombox_bp.route("/upload", methods=["POST"])
def upload_boombox():
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    file = request.files.get("boombox_file")

    if not file or not file.filename:
        if is_ajax:
            return jsonify({"success": False, "error": "No file selected."}), 400
        flash("No file selected.", "error")
        return redirect(url_for("boombox.boombox_home"))

    try:
        ok, msg = upload_boombox_file(file)
    except BoomboxServiceError as exc:
        ok, msg = False, str(exc)

    if is_ajax:
        return jsonify({"success": ok, "message": msg}), (200 if ok else 400)

    flash(msg, "success" if ok else "error")
    return redirect(url_for("boombox.boombox_home"))


@boombox_bp.route("/delete/<path:filename>", methods=["POST"])
def delete_boombox(filename):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    try:
        ok, msg = delete_boombox_file(filename)
    except BoomboxServiceError as exc:
        ok, msg = False, str(exc)

    if is_ajax:
        return jsonify({"success": ok, "message": msg}), (200 if ok else 400)

    flash(msg, "success" if ok else "error")
    return redirect(url_for("boombox.boombox_home"))


@boombox_bp.route("/play/<path:filename>")
def play_boombox(filename):
    """Stream a Boombox MP3 for in-browser preview."""
    try:
        file_path = resolve_boombox_file_path(filename)
    except BoomboxServiceError as exc:
        flash(str(exc), "error")
        return redirect(url_for("boombox.boombox_home"))

    return send_file(file_path, mimetype="audio/mpeg")
