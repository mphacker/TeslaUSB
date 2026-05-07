"""Blueprint for Boombox sound management.

Boombox sounds live in the ``/Boombox/`` folder at the root of the Music
drive (``usb_music.img`` / part3). Tesla scans that folder for short
MP3/WAV clips that play through the external pedestrian-warning speaker
when the vehicle is parked.

This blueprint is a focused UX wrapper around an already-possible
operation (the existing Music browser can create the same folder). It
adds strict per-Tesla-spec validation, a 5-file cap, and the NHTSA
parked-only safety warning that the feature requires.
"""

import logging
import os

from flask import (
    Blueprint, flash, jsonify, redirect, render_template, request, send_file,
    url_for,
)

from config import IMG_MUSIC_PATH, MUSIC_ENABLED
from utils import format_file_size, get_base_context
from services.boombox_service import (
    BOOMBOX_FOLDER,
    BoomboxServiceError,
    MAX_FILE_COUNT,
    MAX_FILE_SIZE,
    MAX_FILENAME_LENGTH,
    delete_boombox_file,
    get_all_boombox_files,
    get_boombox_count_any_mode,
    resolve_boombox_file_path,
    upload_boombox_file,
)

boombox_bp = Blueprint('boombox', __name__, url_prefix='/boombox')
logger = logging.getLogger(__name__)


@boombox_bp.before_request
def _require_music_image():
    # Same gating condition as the music blueprint — the Boombox folder
    # lives on the music drive, so without it there is nowhere to write.
    if not MUSIC_ENABLED or not os.path.isfile(IMG_MUSIC_PATH):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"error": "Feature unavailable"}), 503
        flash("This feature is not available because the required disk "
              "image has not been created.")
        return redirect(url_for('mode_control.index'))


@boombox_bp.route("/")
def boombox_home():
    ctx = get_base_context()
    files = get_all_boombox_files()
    # Tag display strings on the server so the template stays simple.
    for entry in files:
        entry['size_str'] = format_file_size(entry['size'])
    return render_template(
        "boombox.html",
        page='media',
        media_tab='boombox',
        **ctx,
        files=files,
        file_count=len(files),
        max_file_count=MAX_FILE_COUNT,
        max_file_size=MAX_FILE_SIZE,
        max_file_size_mb=MAX_FILE_SIZE // (1024 * 1024),
        max_filename_length=MAX_FILENAME_LENGTH,
        boombox_folder=BOOMBOX_FOLDER,
    )


@boombox_bp.route("/upload", methods=["POST"])
def upload():
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    files = request.files.getlist('boombox_files')
    if not files:
        # Fall back to the single-file field name for the simple form.
        single = request.files.get('boombox_file')
        if single and single.filename:
            files = [single]

    if not files or all(not f or not f.filename for f in files):
        if is_ajax:
            return jsonify({
                "success": False, "error": "No files selected"}), 400
        flash("No files selected", "error")
        return redirect(url_for('boombox.boombox_home'))

    # Always re-check the count per file in the loop — the count check
    # in upload_boombox_file is the authoritative guard, but echoing it
    # here lets us return per-file results for AJAX callers without
    # round-tripping through the service for files we already know
    # would be rejected.
    starting_count = get_boombox_count_any_mode()
    accepted = starting_count
    results = []
    successes = 0

    for f in files:
        if not f or not f.filename:
            continue
        if accepted >= MAX_FILE_COUNT:
            results.append({
                'filename': f.filename,
                'success': False,
                'message': (f"Maximum of {MAX_FILE_COUNT} Boombox sounds "
                            "allowed"),
            })
            continue
        try:
            ok, msg = upload_boombox_file(f, f.filename)
        except BoomboxServiceError as exc:
            ok, msg = False, str(exc)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Boombox upload failed: %s", exc, exc_info=True)
            ok, msg = False, "Server error"

        results.append({'filename': f.filename, 'success': ok,
                        'message': msg})
        if ok:
            successes += 1
            accepted += 1

    if is_ajax:
        status = 200 if successes else 400
        return jsonify({
            'success': successes > 0,
            'uploaded': successes,
            'results': results,
        }), status

    if successes:
        flash(f"Uploaded {successes} file(s)", "success")
    failures = [r for r in results if not r['success']]
    for r in failures:
        flash(f"{r['filename']}: {r['message']}", "error")
    return redirect(url_for('boombox.boombox_home'))


@boombox_bp.route("/delete/<path:filename>", methods=["POST"])
def delete(filename):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    try:
        ok, msg = delete_boombox_file(filename)
    except BoomboxServiceError as exc:
        ok, msg = False, str(exc)
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Boombox delete failed: %s", exc, exc_info=True)
        ok, msg = False, "Server error"

    if is_ajax:
        status = 200 if ok else 400
        return jsonify({"success": ok, "message": msg}), status

    flash(msg, "success" if ok else "error")
    return redirect(url_for('boombox.boombox_home'))


@boombox_bp.route("/play/<path:filename>")
def play(filename):
    """Stream a boombox file for in-browser HTML5 audio preview."""
    try:
        file_path = resolve_boombox_file_path(filename)
    except BoomboxServiceError as exc:
        flash(str(exc), "error")
        return redirect(url_for('boombox.boombox_home'))

    ext = os.path.splitext(file_path)[1].lower()
    mimetype = 'audio/mpeg' if ext == '.mp3' else 'audio/wav'
    return send_file(file_path, mimetype=mimetype)
