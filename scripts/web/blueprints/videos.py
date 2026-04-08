"""Blueprint for video browsing and management routes."""

import os
import logging
import tempfile
import zipfile
from flask import Blueprint, request, redirect, url_for, flash, send_file, jsonify, Response, after_this_request

from config import IMG_CAM_PATH
from utils import get_base_context
from services.mode_service import current_mode
from services.video_service import (
    get_teslacam_path,
    get_session_videos,
    get_teslacam_folders,
    get_events,
    get_event_details,
    group_videos_by_session,
    is_valid_mp4,
)

logger = logging.getLogger(__name__)

videos_bp = Blueprint('videos', __name__, url_prefix='/videos')


def _check_archive_fallback(filename: str, folder_hint: str = None):
    """Check if a video file exists in ArchivedClips on the SD card.

    Returns the archive path if found, None otherwise.
    If folder_hint is 'ArchivedClips', goes directly to the archive dir.
    """
    try:
        from config import ARCHIVE_DIR, ARCHIVE_ENABLED
        if ARCHIVE_ENABLED and filename:
            archive_path = os.path.join(ARCHIVE_DIR, os.path.basename(filename))
            if os.path.isfile(archive_path):
                return archive_path
    except ImportError:
        pass
    return None


@videos_bp.before_request
def _require_cam_image():
    if not os.path.isfile(IMG_CAM_PATH):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"error": "Feature unavailable"}), 503
        flash("This feature is not available because the required disk image has not been created.")
        return redirect(url_for('mode_control.index'))


@videos_bp.route("/")
def file_browser():
    """Video listing API for the map video panel.

    AJAX requests get JSON (used by map's loadVideoList).
    Browser requests redirect to the map page (videos.html no longer exists).
    """
    # Browser requests → redirect to map
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return redirect(url_for('mapping.map_view'))

    ctx = get_base_context()
    teslacam_path = get_teslacam_path()

    if not teslacam_path:
        return jsonify({'events': [], 'has_next': False, 'folder_structure': 'events'})

    folders = get_teslacam_folders()
    current_folder = request.args.get('folder', folders[0]['name'] if folders else None)

    try:
        page_num = int(request.args.get('page', 1))
    except ValueError:
        page_num = 1
    per_page = 12

    events = []
    total_events = 0
    folder_structure = 'events'

    if current_folder:
        # Handle ArchivedClips (SD card, not on USB image)
        if current_folder == 'ArchivedClips':
            try:
                from config import ARCHIVE_DIR, ARCHIVE_ENABLED
                if ARCHIVE_ENABLED:
                    folder_path = ARCHIVE_DIR
                else:
                    return jsonify({'events': [], 'has_next': False, 'folder_structure': 'flat'})
            except ImportError:
                return jsonify({'events': [], 'has_next': False, 'folder_structure': 'flat'})
        else:
            folder_path = os.path.join(teslacam_path, current_folder)
        if os.path.isdir(folder_path):
            folder_info = next((f for f in folders if f['name'] == current_folder), None)
            if current_folder == 'ArchivedClips':
                folder_structure = 'flat'
            else:
                folder_structure = folder_info['structure'] if folder_info else 'events'

            if folder_structure == 'flat':
                events, total_events = group_videos_by_session(folder_path, page=page_num, per_page=per_page)
            else:
                events, total_events = get_events(folder_path, page=page_num, per_page=per_page)

    compact_events = []
    for event in events:
        compact_event = {
            'name': event['name'],
            'datetime': event['datetime'],
            'size_mb': event['size_mb'],
            'camera_videos': {k: v for k, v in event.get('camera_videos', {}).items() if v},
        }
        if event.get('city'):
            compact_event['city'] = event['city']
        if event.get('reason'):
            compact_event['reason'] = event['reason']
        encrypted = {k: v for k, v in event.get('encrypted_videos', {}).items() if v}
        if encrypted:
            compact_event['encrypted_videos'] = encrypted
        compact_events.append(compact_event)

    # Count total video files across all events in this folder
    total_video_files = 0
    if current_folder and folder_path and os.path.isdir(folder_path):
        try:
            for entry in os.scandir(folder_path):
                if entry.is_dir(follow_symlinks=False):
                    for sub in os.scandir(entry.path):
                        if sub.name.lower().endswith('.mp4'):
                            total_video_files += 1
                elif entry.name.lower().endswith('.mp4'):
                    total_video_files += 1
        except (PermissionError, OSError):
            pass

    return jsonify({
        'events': compact_events,
        'has_next': (page_num * per_page) < total_events,
        'next_page': page_num + 1,
        'total_count': total_events,
        'total_video_count': total_video_files,
        'folder_structure': folder_structure
    })



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

    # Direct archive path: if first segment is ArchivedClips, serve from SD card
    if sanitized_parts and sanitized_parts[0] == 'ArchivedClips':
        video_path = _check_archive_fallback(sanitized_parts[-1])
        if not video_path:
            return "Video not found", 404
    else:
        video_path = os.path.join(teslacam_path, *sanitized_parts)
        if not os.path.isfile(video_path):
            video_path = _check_archive_fallback(sanitized_parts[-1]) if sanitized_parts else None
            if not video_path:
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

    if sanitized_parts and sanitized_parts[0] == 'ArchivedClips':
        video_path = _check_archive_fallback(sanitized_parts[-1])
        if not video_path:
            return "Video not found", 404
    else:
        video_path = os.path.join(teslacam_path, *sanitized_parts)
        if not os.path.isfile(video_path):
            video_path = _check_archive_fallback(sanitized_parts[-1]) if sanitized_parts else None
            if not video_path:
                return "Video not found", 404
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
    filename = sanitized_parts[-1]

    if sanitized_parts and sanitized_parts[0] == 'ArchivedClips':
        video_path = _check_archive_fallback(filename)
        if not video_path:
            return "Video not found", 404
    else:
        video_path = os.path.join(teslacam_path, *sanitized_parts)
        if not os.path.isfile(video_path):
            video_path = _check_archive_fallback(filename) if filename else None
            if not video_path:
                return "Video not found", 404
        if not video_path:
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
    # Use GADGET_DIR for temp storage to avoid filling tmpfs
    from config import GADGET_DIR as _gadget_dir
    temp_dir = os.path.join(_gadget_dir, '.cache', 'zip_temp')
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


@videos_bp.route("/delete_event/<folder>/<event_name>", methods=["POST"])
def delete_event(folder, event_name):
    """Delete all videos for an event/session.

    For SavedClips/SentryClips (event structure): Deletes the entire event folder.
    For RecentClips (flat structure): Deletes all camera views for the session.
    """
    # Only allow deletion in edit mode
    if current_mode() != "edit":
        return jsonify({
            'success': False,
            'error': 'Videos can only be deleted in Edit Mode.'
        }), 403

    teslacam_path = get_teslacam_path()
    if not teslacam_path:
        return jsonify({
            'success': False,
            'error': 'TeslaCam not accessible.'
        }), 404

    # Sanitize inputs
    folder = os.path.basename(folder)
    event_name = os.path.basename(event_name)
    folder_path = os.path.join(teslacam_path, folder)

    if not os.path.isdir(folder_path):
        return jsonify({
            'success': False,
            'error': f'Folder not found: {folder}'
        }), 404

    # Determine folder structure
    folders = get_teslacam_folders()
    folder_info = next((f for f in folders if f['name'] == folder), None)
    folder_structure = folder_info['structure'] if folder_info else 'events'

    deleted_count = 0
    error_count = 0
    deleted_files = []

    try:
        if folder_structure == 'flat':
            # RecentClips: Delete all videos matching the session timestamp
            from services.file_safety import is_protected_file
            session_videos = get_session_videos(folder_path, event_name)
            for video in session_videos:
                try:
                    if is_protected_file(video['path']):
                        logger.error("BLOCKED deletion of protected file: %s", video['path'])
                        error_count += 1
                        continue
                    os.remove(video['path'])
                    deleted_count += 1
                    deleted_files.append(video['name'])
                except OSError as e:
                    logger.error(f"Failed to delete {video['path']}: {e}")
                    error_count += 1
        else:
            # SavedClips/SentryClips: Delete the entire event folder
            import shutil
            event_path = os.path.join(folder_path, event_name)

            if not os.path.isdir(event_path):
                return jsonify({
                    'success': False,
                    'error': f'Event not found: {event_name}'
                }), 404

            # Count files before deletion
            with os.scandir(event_path) as entries:
                for entry in entries:
                    if entry.is_file():
                        deleted_count += 1
                        deleted_files.append(entry.name)

            # Delete the entire folder (with IMG protection)
            from services.file_safety import safe_rmtree
            if not safe_rmtree(event_path):
                return jsonify({
                    'success': False,
                    'error': 'Refused: folder contains protected files'
                }), 403

    except Exception as e:
        logger.error(f"Error deleting event {event_name}: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

    # Clean up geodata.db entries for deleted front-camera videos
    if deleted_files:
        try:
            from config import MAPPING_ENABLED, MAPPING_DB_PATH
            if MAPPING_ENABLED:
                from services.mapping_service import purge_deleted_videos
                full_paths = [
                    os.path.join(folder_path, event_name, f) if folder_structure != 'flat'
                    else os.path.join(folder_path, f)
                    for f in deleted_files
                ]
                purge_deleted_videos(MAPPING_DB_PATH, deleted_paths=full_paths)
        except Exception as e:
            logger.warning("Failed to purge geodata for deleted videos: %s", e)

    return jsonify({
        'success': True,
        'deleted_count': deleted_count,
        'deleted_files': deleted_files,
        'error_count': error_count
    })
