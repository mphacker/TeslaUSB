"""Blueprint for video browsing and management routes."""

import os
import socket
import logging
import tempfile
import zipfile
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, jsonify, Response, after_this_request

from config import THUMBNAIL_CACHE_DIR
from utils import generate_thumbnail_hash
from services.mode_service import mode_display, current_mode
from services.video_service import (
    get_teslacam_path,
    get_video_files,
    get_session_videos,
    get_teslacam_folders,
    get_events,
    get_event_details,
    group_videos_by_session,
    generate_video_thumbnail,
)

logger = logging.getLogger(__name__)

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

    # Pagination parameters
    try:
        page_num = int(request.args.get('page', 1))
    except ValueError:
        page_num = 1
    per_page = 12

    events = []
    total_events = 0
    folder_structure = 'events'  # Default to event-based structure

    if current_folder:
        folder_path = os.path.join(teslacam_path, current_folder)
        if os.path.isdir(folder_path):
            # Determine folder structure type
            folder_info = next((f for f in folders if f['name'] == current_folder), None)
            folder_structure = folder_info['structure'] if folder_info else 'events'

            # Get events/sessions based on folder structure
            if folder_structure == 'flat':
                # RecentClips: Group flat files by session
                events, total_events = group_videos_by_session(folder_path, page=page_num, per_page=per_page)
            else:
                # SavedClips/SentryClips: Get event subfolders
                events, total_events = get_events(folder_path, page=page_num, per_page=per_page)

    # Check if this is an AJAX request for infinite scroll
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({
            'events': events,
            'has_next': (page_num * per_page) < total_events,
            'next_page': page_num + 1,
            'folder_structure': folder_structure
        })

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
        folder_structure=folder_structure,
        hostname=socket.gethostname(),
        current_page=page_num,
        has_next=(page_num * per_page) < total_events
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

    # Check folder structure type
    folders = get_teslacam_folders()
    folder_info = next((f for f in folders if f['name'] == folder), None)
    folder_structure = folder_info['structure'] if folder_info else 'events'

    if folder_structure == 'flat':
        # For flat structure (RecentClips), build event-like object from session videos
        session_videos = get_session_videos(folder_path, event_name)

        if not session_videos:
            flash(f"Session not found: {event_name}", "error")
            return redirect(url_for("videos.file_browser", folder=folder))

        # Build event object matching event structure
        event = {
            'name': event_name,
            'datetime': session_videos[0]['modified'] if session_videos else event_name,
            'city': '',  # Flat structure doesn't have location metadata
            'reason': '',
            'camera_videos': {
                'front': None,
                'back': None,
                'left_repeater': None,
                'right_repeater': None,
                'left_pillar': None,
                'right_pillar': None,
            },
            'has_thumbnail': False,
        }

        # Map videos to camera angles
        for video in session_videos:
            camera = video.get('camera', '').lower()
            if camera in event['camera_videos']:
                event['camera_videos'][camera] = video['name']

        return render_template(
            'event_player.html',
            page='event',
            mode_label=label,
            mode_class=css_class,
            mode_token=token,
            folder=folder,
            event=event,
            folder_structure=folder_structure,  # Pass structure type to template
            hostname=socket.gethostname(),
        )

    # Get event details (for event-based structure)
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
        folder_structure=folder_structure,  # Pass structure type to template
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


@videos_bp.route("/sei/<path:filepath>")
def fetch_video_for_sei(filepath):
    """Fetch complete video file for SEI parsing (no range requests).

    This endpoint serves the entire video file at once for client-side SEI extraction.
    Unlike /stream/, this does not support HTTP Range requests.

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

    if not os.path.isfile(video_path):
        return "Video not found", 404

    # Send complete file with proper headers for in-browser processing
    response = send_file(
        video_path,
        mimetype='video/mp4',
        as_attachment=False,
        conditional=False  # Disable conditional requests
    )
    # Allow caching since videos don't change
    response.headers['Cache-Control'] = 'public, max-age=3600'
    return response


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


@videos_bp.route("/download_event/<folder>/<event_name>")
def download_event(folder, event_name):
    """Download all camera videos for an event as a zip file.

    Works with both event-based (SavedClips/SentryClips) and flat (RecentClips) structures.
    """
    teslacam_path = get_teslacam_path()
    if not teslacam_path:
        return "TeslaCam not accessible", 404

    # Sanitize inputs
    folder = os.path.basename(folder)
    folder_path = os.path.join(teslacam_path, folder)

    if not os.path.isdir(folder_path):
        return "Folder not found", 404

    # Determine folder structure
    folders = get_teslacam_folders()
    folder_info = next((f for f in folders if f['name'] == folder), None)
    folder_structure = folder_info['structure'] if folder_info else 'events'

    # Collect video files
    video_files = []

    if folder_structure == 'flat':
        # RecentClips: Get session videos
        session_videos = get_session_videos(folder_path, event_name)
        for video in session_videos:
            video_path = os.path.join(folder_path, video['name'])
            if os.path.isfile(video_path):
                video_files.append((video_path, video['name']))
    else:
        # SavedClips/SentryClips: Get event folder videos
        event_path = os.path.join(folder_path, os.path.basename(event_name))
        if os.path.isdir(event_path):
            event = get_event_details(folder_path, event_name)
            if event:
                for camera_key, filename in event['camera_videos'].items():
                    if filename:
                        video_path = os.path.join(event_path, filename)
                        if os.path.isfile(video_path):
                            video_files.append((video_path, filename))

    if not video_files:
        return "No videos found for this event", 404

    # Create zip file on disk (not in /tmp which is RAM-based and too small)
    # Use /home/pi for temp storage to avoid filling tmpfs
    temp_dir = '/home/pi/.cache/teslausb/zip_temp'
    os.makedirs(temp_dir, exist_ok=True)

    temp_fd, temp_path = tempfile.mkstemp(suffix='.zip', dir=temp_dir)
    os.close(temp_fd)

    with zipfile.ZipFile(temp_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for video_path, filename in video_files:
            zipf.write(video_path, filename)

    # Register cleanup callback to delete temp file after response is sent
    @after_this_request
    def cleanup(response):
        try:
            os.unlink(temp_path)
        except Exception as e:
            logger.error(f"Failed to cleanup temp zip: {e}")
        return response

    # Send the zip file
    return send_file(
        temp_path,
        as_attachment=True,
        download_name=f"{event_name}.zip",
        mimetype='application/zip'
    )


@videos_bp.route("/event_thumbnail/<folder>/<event_name>")
def get_event_thumbnail(folder, event_name):
    """Get the Tesla-generated thumbnail for an event (SavedClips/SentryClips)."""
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

    # Return with 7-day cache header for better performance
    return send_file(thumb_path, mimetype='image/png',
                    max_age=604800, conditional=True)


@videos_bp.route("/session_thumbnail/<folder>/<session_name>")
def get_session_thumbnail(folder, session_name):
    """Generate/retrieve thumbnail for a session (RecentClips) from front camera video."""
    teslacam_path = get_teslacam_path()
    if not teslacam_path:
        return "TeslaCam not accessible", 404

    # Sanitize inputs
    folder = os.path.basename(folder)
    session_name = os.path.basename(session_name)

    # Find front camera video for this session
    folder_path = os.path.join(teslacam_path, folder)
    front_video = None

    try:
        for entry in os.scandir(folder_path):
            if (entry.is_file() and
                entry.name.startswith(session_name) and
                'front' in entry.name.lower() and
                entry.name.lower().endswith(('.mp4', '.avi', '.mov'))):
                front_video = entry.path
                break
    except OSError:
        return "Video not found", 404

    if not front_video:
        return "Front camera video not found", 404

    # Generate cache key based on video path and modification time
    cache_key = generate_thumbnail_hash(front_video)
    if not cache_key:
        return "Failed to generate cache key", 500

    # Check cache
    cache_path = os.path.join(THUMBNAIL_CACHE_DIR, f"{cache_key}.png")

    if os.path.isfile(cache_path):
        # Return cached thumbnail with 7-day cache header
        return send_file(cache_path, mimetype='image/png',
                        max_age=604800, conditional=True)

    # Generate thumbnail (1-3 seconds on Pi Zero 2 W)
    if generate_video_thumbnail(front_video, cache_path):
        return send_file(cache_path, mimetype='image/png',
                        max_age=604800, conditional=True)
    else:
        return "Failed to generate thumbnail", 500


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
