"""Blueprint for video browsing and management routes."""

import os
import socket
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, jsonify

from services.mode_service import mode_display, current_mode
from services.video_service import (
    get_teslacam_path,
    get_video_files,
    get_session_videos,
    get_teslacam_folders,
    get_events,
    get_event_details,
)

videos_bp = Blueprint('videos', __name__, url_prefix='/videos')


@videos_bp.route("/")
def file_browser():
    """Event list page for TeslaCam videos - shows list of events by folder."""
    token, label, css_class, share_paths = mode_display()
    teslacam_path = get_teslacam_path()

    if not teslacam_path:
        return render_template(
            'videos.html',
            page='browser',
            mode_label=label,
            mode_class=css_class,
            mode_token=token,
            teslacam_available=False,
            folders=[],
            events=[],
            current_folder=None,
            hostname=socket.gethostname(),
        )

    folders = get_teslacam_folders()
    current_folder = request.args.get('folder', folders[0]['name'] if folders else None)
    events = []

    if current_folder:
        folder_path = os.path.join(teslacam_path, current_folder)
        if os.path.isdir(folder_path):
            events = get_events(folder_path)

    return render_template(
        'videos.html',
        page='browser',
        mode_label=label,
        mode_class=css_class,
        mode_token=token,
        teslacam_available=True,
        folders=folders,
        events=events,
        current_folder=current_folder,
        hostname=socket.gethostname(),
    )


@videos_bp.route("/event/<folder>/<event_name>")
def view_event(folder, event_name):
    """View a Tesla event in Tesla-style multi-camera player."""
    token, label, css_class, share_paths = mode_display()
    teslacam_path = get_teslacam_path()

    if not teslacam_path:
        flash("TeslaCam path is not accessible", "error")
        return redirect(url_for("videos.file_browser"))

    # Sanitize inputs
    folder = os.path.basename(folder)
    folder_path = os.path.join(teslacam_path, folder)

    if not os.path.isdir(folder_path):
        flash(f"Folder not found: {folder}", "error")
        return redirect(url_for("videos.file_browser"))

    # Get event details
    event = get_event_details(folder_path, event_name)

    if not event:
        flash(f"Event not found: {event_name}", "error")
        return redirect(url_for("videos.file_browser", folder=folder))

    return render_template(
        'event_player.html',
        page='event',
        mode_label=label,
        mode_class=css_class,
        mode_token=token,
        folder=folder,
        event=event,
        hostname=socket.gethostname(),
    )


@videos_bp.route("/session/<folder>/<session>")
def view_session(folder, session):
    """View all videos from a recording session in synchronized multi-camera view."""
    token, label, css_class, share_paths = mode_display()
    teslacam_path = get_teslacam_path()

    if not teslacam_path:
        flash("TeslaCam path is not accessible", "error")
        return redirect(url_for("videos.file_browser"))

    # Sanitize inputs
    folder = os.path.basename(folder)
    folder_path = os.path.join(teslacam_path, folder)

    if not os.path.isdir(folder_path):
        flash(f"Folder not found: {folder}", "error")
        return redirect(url_for("videos.file_browser"))

    # Get all videos for this session
    session_videos = get_session_videos(folder_path, session)

    if not session_videos:
        flash(f"No videos found for session: {session}", "error")
        return redirect(url_for("videos.file_browser", folder=folder))

    return render_template(
        'session.html',
        page='session',
        mode_label=label,
        mode_class=css_class,
        mode_token=token,
        folder=folder,
        session_id=session,
        videos=session_videos,
        hostname=socket.gethostname(),
    )


def _iter_file_range(path, start, end, chunk_size=256 * 1024):
    """Yield chunks for the requested byte range (inclusive)."""
    with open(path, 'rb') as f:
        f.seek(start)
        bytes_left = end - start + 1
        while bytes_left > 0:
            chunk = f.read(min(chunk_size, bytes_left))
            if not chunk:
                break
            bytes_left -= len(chunk)
            yield chunk


@videos_bp.route("/stream/<path:filepath>")
def stream_video(filepath):
    """Stream a video file with HTTP Range/206 support.

    filepath can be:
    - folder/filename (legacy)
    - folder/event_name/filename (new event structure)
    """
    from flask import Response

    teslacam_path = get_teslacam_path()
    if not teslacam_path:
        return "TeslaCam not accessible", 404

    # Sanitize and build path
    parts = filepath.split('/')
    sanitized_parts = [os.path.basename(p) for p in parts]
    video_path = os.path.join(teslacam_path, *sanitized_parts)

    if not os.path.isfile(video_path):
        return "Video not found", 404

    file_size = os.path.getsize(video_path)
    range_header = request.headers.get('Range')
    if not range_header:
        # No range; fall back to full file
        response = send_file(video_path, mimetype='video/mp4')
        response.headers['Accept-Ranges'] = 'bytes'
        return response

    # Parse simple single-range headers: bytes=start-end
    try:
        units, rng = range_header.strip().split('=')
        if units != 'bytes':
            raise ValueError
        start_str, end_str = rng.split('-')
        if start_str == '':
            # suffix range
            suffix = int(end_str)
            if suffix <= 0:
                raise ValueError
            start = max(file_size - suffix, 0)
            end = file_size - 1
        else:
            start = int(start_str)
            end = int(end_str) if end_str else file_size - 1
        if start < 0 or end < start or end >= file_size:
            raise ValueError
    except (ValueError, IndexError):
        return Response(status=416)

    length = end - start + 1
    resp = Response(
        _iter_file_range(video_path, start, end),
        status=206,
        mimetype='video/mp4',
        direct_passthrough=True,
    )
    resp.headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
    resp.headers['Accept-Ranges'] = 'bytes'
    resp.headers['Content-Length'] = str(length)

    # HEAD requests should not stream body
    if request.method == 'HEAD':
        resp.response = []
        resp.headers['Content-Length'] = str(length)

    return resp


@videos_bp.route("/download/<path:filepath>")
def download_video(filepath):
    """Download a video file.

    filepath can be:
    - folder/filename (legacy)
    - folder/event_name/filename (new event structure)
    """
    teslacam_path = get_teslacam_path()
    if not teslacam_path:
        return "TeslaCam not accessible", 404

    # Sanitize and build path
    parts = filepath.split('/')
    sanitized_parts = [os.path.basename(p) for p in parts]
    video_path = os.path.join(teslacam_path, *sanitized_parts)
    filename = sanitized_parts[-1]

    if not os.path.isfile(video_path):
        return "Video not found", 404

    return send_file(video_path, as_attachment=True, download_name=filename)


@videos_bp.route("/event_thumbnail/<folder>/<event_name>")
def get_event_thumbnail(folder, event_name):
    """Get the Tesla-generated thumbnail for an event."""
    teslacam_path = get_teslacam_path()
    if not teslacam_path:
        return "TeslaCam not accessible", 404

    # Sanitize inputs
    folder = os.path.basename(folder)
    event_name = os.path.basename(event_name)

    thumb_path = os.path.join(teslacam_path, folder, event_name, 'thumb.png')

    if not os.path.isfile(thumb_path):
        # Return a placeholder or 404
        return "Thumbnail not found", 404

    return send_file(thumb_path, mimetype='image/png')


@videos_bp.route("/delete/<folder>/<filename>", methods=["POST"])
def delete_video(folder, filename):
    """Delete a single video file."""
    # Only allow deletion in edit mode
    if current_mode() != "edit":
        flash("Videos can only be deleted in Edit Mode.", "error")
        return redirect(url_for("videos.file_browser", folder=folder))

    teslacam_path = get_teslacam_path()
    if not teslacam_path:
        flash("TeslaCam not accessible.", "error")
        return redirect(url_for("videos.file_browser"))

    # Sanitize inputs
    folder = os.path.basename(folder)
    filename = os.path.basename(filename)

    video_path = os.path.join(teslacam_path, folder, filename)

    if not os.path.isfile(video_path):
        flash("Video not found.", "error")
        return redirect(url_for("videos.file_browser", folder=folder))

    try:
        # Delete the video file
        os.remove(video_path)
        flash(f"Successfully deleted {filename}", "success")
    except OSError as e:
        flash(f"Error deleting {filename}: {str(e)}", "error")

    return redirect(url_for("videos.file_browser", folder=folder))


@videos_bp.route("/delete_all/<folder>", methods=["POST"])
def delete_all_videos(folder):
    """Delete all videos in a folder."""
    # Only allow deletion in edit mode
    if current_mode() != "edit":
        flash("Videos can only be deleted in Edit Mode.", "error")
        return redirect(url_for("videos.file_browser", folder=folder))

    teslacam_path = get_teslacam_path()
    if not teslacam_path:
        flash("TeslaCam not accessible.", "error")
        return redirect(url_for("videos.file_browser"))

    # Sanitize input
    folder = os.path.basename(folder)
    folder_path = os.path.join(teslacam_path, folder)

    if not os.path.isdir(folder_path):
        flash("Folder not found.", "error")
        return redirect(url_for("videos.file_browser"))

    # Get all videos in the folder
    videos = get_video_files(folder_path)
    deleted_count = 0
    error_count = 0

    for video in videos:
        try:
            # Delete the video file
            os.remove(video['path'])
            deleted_count += 1
        except OSError:
            error_count += 1

    if deleted_count > 0:
        flash(f"Successfully deleted {deleted_count} video(s) from {folder}", "success")
    if error_count > 0:
        flash(f"Failed to delete {error_count} video(s)", "error")

    return redirect(url_for("videos.file_browser", folder=folder))
