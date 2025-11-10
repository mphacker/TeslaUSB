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
)
from services.thumbnail_service import get_thumbnail_path

videos_bp = Blueprint('videos', __name__, url_prefix='/videos')


@videos_bp.route("/")
def file_browser():
    """File browser page for TeslaCam videos."""
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
            videos=[],
            remaining_videos=[],
            total_video_count=0,
            current_folder=None,
            hostname=socket.gethostname(),
        )
    
    folders = get_teslacam_folders()
    current_folder = request.args.get('folder', folders[0]['name'] if folders else None)
    all_videos = []
    initial_videos = []
    remaining_videos = []
    total_video_count = 0
    
    if current_folder:
        folder_path = os.path.join(teslacam_path, current_folder)
        if os.path.isdir(folder_path):
            all_videos = get_video_files(folder_path)
            total_video_count = len(all_videos)
            # Split into initial load (15) and remaining (for lazy loading)
            initial_videos = all_videos[:15]
            remaining_videos = all_videos[15:]
    
    return render_template(
        'videos.html',
        page='browser',
        mode_label=label,
        mode_class=css_class,
        mode_token=token,
        teslacam_available=True,
        folders=folders,
        videos=initial_videos,
        remaining_videos=remaining_videos,
        total_video_count=total_video_count,
        current_folder=current_folder,
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


@videos_bp.route("/stream/<folder>/<filename>")
def stream_video(folder, filename):
    """Stream a video file."""
    teslacam_path = get_teslacam_path()
    if not teslacam_path:
        return "TeslaCam not accessible", 404
    
    # Sanitize inputs
    folder = os.path.basename(folder)
    filename = os.path.basename(filename)
    
    video_path = os.path.join(teslacam_path, folder, filename)
    
    if not os.path.isfile(video_path):
        return "Video not found", 404
    
    return send_file(video_path, mimetype='video/mp4')


@videos_bp.route("/download/<folder>/<filename>")
def download_video(folder, filename):
    """Download a video file."""
    teslacam_path = get_teslacam_path()
    if not teslacam_path:
        return "TeslaCam not accessible", 404
    
    # Sanitize inputs
    folder = os.path.basename(folder)
    filename = os.path.basename(filename)
    
    video_path = os.path.join(teslacam_path, folder, filename)
    
    if not os.path.isfile(video_path):
        return "Video not found", 404
    
    return send_file(video_path, as_attachment=True, download_name=filename)


@videos_bp.route("/thumbnail/<folder>/<filename>")
def get_thumbnail(folder, filename):
    """Get or generate a thumbnail for a video file."""
    # Sanitize inputs
    folder = os.path.basename(folder)
    filename = os.path.basename(filename)
    
    result = get_thumbnail_path(folder, filename)
    if not result:
        return "Video not found", 404
    
    thumbnail_path, video_path = result
    
    # Check if thumbnail exists
    if os.path.isfile(thumbnail_path):
        response = send_file(thumbnail_path, mimetype='image/jpeg')
        # Add aggressive caching headers (cache for 7 days)
        response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
        response.headers['Expires'] = '604800'
        return response
    
    # Thumbnail doesn't exist - return 404 so browser doesn't keep trying
    # The background process will generate it eventually
    return "Thumbnail not yet generated", 404




# TODO: Re-enable this route after moving cleanup_orphaned_thumbnails to thumbnail_service
# @videos_bp.route("/cleanup_thumbnails", methods=["POST"])
# def cleanup_thumbnails():
#     """Cleanup orphaned thumbnails."""
#     removed = cleanup_orphaned_thumbnails()
#     return jsonify({"success": True, "removed": removed})


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
        
        # Delete the thumbnail if it exists
        result = get_thumbnail_path(folder, filename)
        if result:
            thumbnail_path, _ = result
            if os.path.isfile(thumbnail_path):
                try:
                    os.remove(thumbnail_path)
                except OSError:
                    pass
        
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
            
            # Delete the thumbnail if it exists
            result = get_thumbnail_path(folder, video['name'])
            if result:
                thumbnail_path, _ = result
                if os.path.isfile(thumbnail_path):
                    try:
                        os.remove(thumbnail_path)
                    except OSError:
                        pass
        except OSError:
            error_count += 1
    
    if deleted_count > 0:
        flash(f"Successfully deleted {deleted_count} video(s) from {folder}", "success")
    if error_count > 0:
        flash(f"Failed to delete {error_count} video(s)", "error")
    
    return redirect(url_for("videos.file_browser", folder=folder))
