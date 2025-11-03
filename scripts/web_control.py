#!/usr/bin/env python3
"""
USB Gadget Web Control Interface

A simple Flask web application for controlling USB gadget modes.
Provides buttons to switch between "Present USB" and "Edit USB" modes.
Includes a file browser for TeslaCam videos.
"""

from flask import Flask, render_template_string, redirect, url_for, flash, request, send_file, jsonify
import subprocess
import os
import socket
import wave
import contextlib
import shutil
import threading
import time
import hashlib
from datetime import datetime

app = Flask(__name__)
# Configuration (will be updated by setup_usb.sh)
app.secret_key = "__SECRET_KEY__"
GADGET_DIR = "__GADGET_DIR__"
MNT_DIR = "__MNT_DIR__"
RO_MNT_DIR = "/mnt/gadget"  # Read-only mount directory for present mode
STATE_FILE = os.path.join(GADGET_DIR, "state.txt")
LOCK_CHIME_FILENAME = "LockChime.wav"
CHIMES_FOLDER = "Chimes"  # Folder on part2 where custom chimes are stored
MAX_LOCK_CHIME_SIZE = 1024 * 1024  # 1 MiB
USB_PARTITIONS = ("part1", "part2")
PART_LABEL_MAP = {"part1": "gadget_part1", "part2": "gadget_part2"}
THUMBNAIL_CACHE_DIR = os.path.join(GADGET_DIR, "thumbnails")

MODE_DISPLAY = {
    "present": ("USB Gadget Mode", "present"),
    "edit": ("Edit Mode", "edit"),
    "unknown": ("Unknown", "unknown"),
}


def close_samba_share(partition_key):
    """Ask Samba to close and reopen the relevant share so new files appear immediately."""
    share_name = PART_LABEL_MAP.get(partition_key, f"gadget_{partition_key}")
    commands = [
    ["sudo", "-n", "smbcontrol", "all", "close-share", share_name],
    ["sudo", "-n", "smbcontrol", "all", "reload-config"],
    ["sudo", "-n", "smbcontrol", "all", "close-share", share_name],
    ]
    for cmd in commands:
        try:
            subprocess.run(cmd, check=False, timeout=5, cwd=GADGET_DIR)
        except Exception:
            pass


def remove_other_lock_chimes(exempt_part):
    """Remove LockChime.wav from partitions other than the active one."""
    removed = []
    for usb_part in USB_PARTITIONS:
        if usb_part == exempt_part:
            continue
        other_path = os.path.join(MNT_DIR, usb_part, LOCK_CHIME_FILENAME)
        if os.path.isfile(other_path):
            close_samba_share(usb_part)
            try:
                os.remove(other_path)
                removed.append(PART_LABEL_MAP.get(usb_part, usb_part))
            except OSError:
                pass
    return removed


def restart_samba_services():
    """Force Samba to reload so new files are visible to clients."""
    for service in ("smbd", "nmbd"):
        try:
            subprocess.run(["sudo", "-n", "systemctl", "restart", service], check=False, timeout=10)
        except Exception:
            pass


def detect_mode():
    """Attempt to infer the current mode when the state file is missing."""
    try:
        result = subprocess.run(
            ["lsmod"], capture_output=True, text=True, check=False, timeout=5
        )
        if result.stdout and "g_mass_storage" in result.stdout:
            return "present"
    except Exception:
        pass

    try:
        for part in USB_PARTITIONS:
            mp = os.path.join(MNT_DIR, part)
            if os.path.ismount(mp):
                return "edit"
    except Exception:
        pass

    return "unknown"


def current_mode():
    """Read the current mode from the state file, falling back when needed."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as state_file:
            token = state_file.read().strip().lower()
            if token in MODE_DISPLAY:
                return token
    except FileNotFoundError:
        pass
    except OSError:
        pass

    return detect_mode()


def mode_display():
    """Return mode metadata and share paths when applicable."""
    token = current_mode()
    label, css_class = MODE_DISPLAY.get(token, MODE_DISPLAY["unknown"])
    share_paths = []

    if token == "edit":
        hostname = socket.gethostname()
        share_paths = [
            f"\\\\{hostname}\\gadget_part1",
            f"\\\\{hostname}\\gadget_part2",
        ]

    return token, label, css_class, share_paths


def lock_chime_ui_available(mode_token):
    """Determine if the lock chime UI should be active."""
    if mode_token == "edit":
        return True
    return any(True for _ in iter_mounted_partitions())


def iter_mounted_partitions():
    """Yield mounted USB partitions and their paths."""
    for part in USB_PARTITIONS:
        mount_path = os.path.join(MNT_DIR, part)
        if os.path.isdir(mount_path):
            yield part, mount_path


def iter_all_partitions():
    """Yield all accessible USB partitions based on current mode."""
    mode = current_mode()
    
    if mode == "present":
        # Use read-only mounts in present mode
        for part in USB_PARTITIONS:
            ro_path = os.path.join(RO_MNT_DIR, f"{part}-ro")
            if os.path.isdir(ro_path):
                yield part, ro_path
    else:
        # Use read-write mounts in edit mode
        for part in USB_PARTITIONS:
            rw_path = os.path.join(MNT_DIR, part)
            if os.path.isdir(rw_path):
                yield part, rw_path


def get_mount_path(partition):
    """Get the mount path for a specific partition based on current mode."""
    if partition not in USB_PARTITIONS:
        return None
    
    mode = current_mode()
    
    if mode == "present":
        # Use read-only mount in present mode
        ro_path = os.path.join(RO_MNT_DIR, f"{partition}-ro")
        if os.path.isdir(ro_path):
            return ro_path
    else:
        # Use read-write mount in edit mode
        rw_path = os.path.join(MNT_DIR, partition)
        if os.path.isdir(rw_path):
            return rw_path
    
    return None


def format_file_size(size_bytes):
    """Format file size in human-readable format."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"


def get_teslacam_path():
    """Get the TeslaCam path based on current mode."""
    mode = current_mode()
    
    if mode == "present":
        # Use read-only mount in present mode
        ro_path = os.path.join(RO_MNT_DIR, "part1-ro", "TeslaCam")
        if os.path.isdir(ro_path):
            return ro_path
    elif mode == "edit":
        # Use read-write mount in edit mode
        rw_path = os.path.join(MNT_DIR, "part1", "TeslaCam")
        if os.path.isdir(rw_path):
            return rw_path
    
    return None


def get_video_files(folder_path):
    """Get all video files from a folder with metadata."""
    video_extensions = ('.mp4', '.avi', '.mov', '.mkv')
    videos = []
    
    try:
        for entry in os.scandir(folder_path):
            if entry.is_file() and entry.name.lower().endswith(video_extensions):
                try:
                    stat_info = entry.stat()
                    session_info = parse_session_from_filename(entry.name)
                    videos.append({
                        'name': entry.name,
                        'path': entry.path,
                        'size': stat_info.st_size,
                        'size_mb': round(stat_info.st_size / (1024 * 1024), 2),
                        'modified': datetime.fromtimestamp(stat_info.st_mtime).strftime('%Y-%m-%d %I:%M:%S %p'),
                        'timestamp': stat_info.st_mtime,
                        'session': session_info['session'] if session_info else None,
                        'camera': session_info['camera'] if session_info else None
                    })
                except OSError:
                    continue
    except OSError:
        pass
    
    # Sort by modification time, newest first
    videos.sort(key=lambda x: x['timestamp'], reverse=True)
    return videos


def parse_session_from_filename(filename):
    """
    Parse Tesla video filename to extract session and camera info.
    Format: 2025-10-29_10-39-36-right_pillar.mp4
    Returns: {'session': '2025-10-29_10-39-36', 'camera': 'right_pillar'}
    """
    import re
    # Match pattern: YYYY-MM-DD_HH-MM-SS-camera.ext
    pattern = r'^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-(.+)\.\w+$'
    match = re.match(pattern, filename)
    if match:
        return {
            'session': match.group(1),
            'camera': match.group(2)
        }
    return None


def get_session_videos(folder_path, session_id):
    """Get all videos from a specific session."""
    all_videos = get_video_files(folder_path)
    session_videos = [v for v in all_videos if v['session'] == session_id]
    # Sort by camera name for consistent ordering
    session_videos.sort(key=lambda x: x['camera'] or '')
    return session_videos


def get_teslacam_folders():
    """Get available TeslaCam subfolders."""
    teslacam_path = get_teslacam_path()
    if not teslacam_path:
        return []
    
    folders = []
    try:
        for entry in os.scandir(teslacam_path):
            if entry.is_dir():
                folders.append({
                    'name': entry.name,
                    'path': entry.path
                })
    except OSError:
        pass
    
    folders.sort(key=lambda x: x['name'])
    return folders


def generate_thumbnail_hash(video_path):
    """Generate a unique hash for a video file based on path and modification time."""
    try:
        stat_info = os.stat(video_path)
        unique_string = f"{video_path}_{stat_info.st_mtime}_{stat_info.st_size}"
        return hashlib.md5(unique_string.encode()).hexdigest()
    except OSError:
        return None


def get_thumbnail_path(folder, filename):
    """Get the cached thumbnail path for a video file."""
    teslacam_path = get_teslacam_path()
    if not teslacam_path:
        return None
    
    video_path = os.path.join(teslacam_path, folder, filename)
    if not os.path.isfile(video_path):
        return None
    
    # Generate unique hash for this video
    video_hash = generate_thumbnail_hash(video_path)
    if not video_hash:
        return None
    
    # Create thumbnail filename
    thumbnail_filename = f"{video_hash}.jpg"
    thumbnail_path = os.path.join(THUMBNAIL_CACHE_DIR, thumbnail_filename)
    
    return thumbnail_path, video_path


def generate_thumbnail(video_path, thumbnail_path):
    """Generate a thumbnail from a video file using ffmpeg."""
    try:
        # Ensure cache directory exists
        os.makedirs(THUMBNAIL_CACHE_DIR, exist_ok=True)
        
        # Use ffmpeg to extract a frame at 1 second
        # -ss 1: seek to 1 second
        # -i: input file
        # -vframes 1: extract 1 frame
        # -vf scale=160:-1: resize to width 160px, keep aspect ratio
        # -y: overwrite output file
        result = subprocess.run(
            [
                "ffmpeg",
                "-ss", "1",
                "-i", video_path,
                "-vframes", "1",
                "-vf", "scale=160:-1",
                "-y",
                thumbnail_path
            ],
            capture_output=True,
            timeout=10,
            check=False
        )
        
        if result.returncode == 0 and os.path.isfile(thumbnail_path):
            return True
        
        return False
    except Exception:
        return False


def cleanup_orphaned_thumbnails():
    """Remove thumbnails for videos that no longer exist."""
    try:
        if not os.path.isdir(THUMBNAIL_CACHE_DIR):
            return
        
        teslacam_path = get_teslacam_path()
        if not teslacam_path:
            return
        
        # Build set of valid thumbnail hashes from existing videos
        valid_hashes = set()
        folders = get_teslacam_folders()
        
        for folder in folders:
            folder_path = os.path.join(teslacam_path, folder['name'])
            videos = get_video_files(folder_path)
            
            for video in videos:
                video_hash = generate_thumbnail_hash(video['path'])
                if video_hash:
                    valid_hashes.add(f"{video_hash}.jpg")
        
        # Remove thumbnails not in the valid set
        removed_count = 0
        for thumbnail_file in os.listdir(THUMBNAIL_CACHE_DIR):
            if thumbnail_file.endswith('.jpg') and thumbnail_file not in valid_hashes:
                try:
                    os.remove(os.path.join(THUMBNAIL_CACHE_DIR, thumbnail_file))
                    removed_count += 1
                except OSError:
                    pass
        
        return removed_count
    except Exception:
        return 0


HTML_TEMPLATE = """
<!doctype html>
<html>
<head>
    <meta charset='utf-8'>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    {% if auto_refresh %}
    <meta http-equiv="refresh" content="15">
    {% endif %}
    <title>Tesla USB Gadget Control</title>
    <style>
        * {
            box-sizing: border-box;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0;
            padding: 0;
            background-color: #f5f5f5;
        }
        .navbar {
            background-color: #2c3e50;
            color: white;
            padding: 15px 20px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.1);
        }
        .navbar-content {
            max-width: 1200px;
            margin: 0 auto;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
        }
        .navbar h1 {
            margin: 0;
            font-size: 20px;
            color: white;
        }
        /* Hamburger menu - hidden on desktop */
        .hamburger {
            display: none;
            flex-direction: column;
            cursor: pointer;
            padding: 5px;
        }
        .hamburger span {
            width: 25px;
            height: 3px;
            background-color: white;
            margin: 3px 0;
            transition: 0.3s;
            border-radius: 3px;
        }
        /* Mobile overlay menu - hidden by default */
        .mobile-menu-overlay {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(0, 0, 0, 0.5);
            z-index: 999;
        }
        .mobile-menu {
            display: none;
            position: fixed;
            top: 0;
            left: -100%;
            width: 250px;
            height: 100%;
            background-color: #2c3e50;
            z-index: 1000;
            padding: 20px;
            transition: left 0.3s ease;
            overflow-y: auto;
        }
        .mobile-menu.active {
            left: 0;
        }
        .mobile-menu-close {
            color: white;
            font-size: 30px;
            cursor: pointer;
            text-align: left;
            margin-bottom: 20px;
        }
        .mobile-menu a {
            display: block;
            color: white;
            text-decoration: none;
            padding: 15px 10px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
            font-size: 16px;
        }
        .mobile-menu a:hover,
        .mobile-menu a.active {
            background-color: rgba(255, 255, 255, 0.1);
        }
        .navbar h1 a {
            color: white;
            text-decoration: none;
            cursor: pointer;
        }
        .nav-links {
            display: flex;
            gap: 20px;
        }
        .nav-links a {
            color: white;
            text-decoration: none;
            padding: 8px 16px;
            border-radius: 4px;
            transition: background-color 0.2s;
        }
        .nav-links a:hover {
            background-color: rgba(255,255,255,0.1);
        }
        .nav-links a.active {
            background-color: rgba(255,255,255,0.2);
        }
        .main-content {
            max-width: 1200px;
            margin: 20px auto;
            padding: 0 20px;
            display: flex;
            flex-direction: column;
            height: calc(100vh - 100px);
        }
        .container {
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            margin-bottom: 20px;
            display: flex;
            flex-direction: column;
            flex: 1;
            overflow: hidden;
        }
        h1, h2 {
            color: #333;
            margin-top: 0;
        }
        button {
            padding: 12px 25px;
            margin: 10px 5px;
            border: none;
            border-radius: 5px;
            font-size: 16px;
            cursor: pointer;
            transition: opacity 0.2s;
        }
        .present-btn {
            background-color: #007bff;
            color: white;
        }
        .edit-btn {
            background-color: #28a745;
            color: white;
        }
        .set-chime-btn {
            background-color: #6f42c1;
            color: white;
        }
        button:hover {
            opacity: 0.9;
        }
        button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        .loading-overlay {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(0, 0, 0, 0.7);
            z-index: 9999;
            justify-content: center;
            align-items: center;
            flex-direction: column;
        }
        .spinner {
            border: 8px solid #f3f3f3;
            border-top: 8px solid #007bff;
            border-radius: 50%;
            width: 60px;
            height: 60px;
            animation: spin 1s linear infinite;
        }
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        .loading-text {
            color: white;
            font-size: 18px;
            margin-top: 20px;
            font-weight: 500;
        }
        .messages {
            margin: 20px 0;
        }
        .messages .success {
            background-color: #d4edda;
            color: #155724;
            padding: 12px;
            border-radius: 5px;
            margin: 5px 0;
        }
        .messages .info {
            background-color: #d1ecf1;
            color: #0c5460;
            padding: 12px;
            border-radius: 5px;
            margin: 5px 0;
        }
        .messages .error {
            background-color: #f8d7da;
            color: #721c24;
            padding: 12px;
            border-radius: 5px;
            margin: 5px 0;
        }
        .info-box {
            background-color: #e2e3e5;
            color: #383d41;
            padding: 15px;
            border-radius: 5px;
            margin: 20px 0;
            font-size: 14px;
        }
        .status-label {
            text-align: center;
            font-weight: 600;
            margin-bottom: 20px;
            padding: 12px;
            border-radius: 6px;
            font-size: 16px;
        }
        .status-label.present {
            background-color: #d4edda;
            color: #155724;
        }
        .status-label.edit {
            background-color: #d1ecf1;
            color: #0c5460;
        }
        .status-label.unknown {
            background-color: #fff3cd;
            color: #856404;
        }
        .shares {
            background-color: #f8f9fa;
            border: 1px solid #d6d8db;
            border-radius: 6px;
            padding: 12px;
            margin-top: 20px;
            font-size: 14px;
        }
        .shares ul {
            margin: 8px 0 0;
            padding-left: 18px;
        }
        .shares code {
            background: #eef0f3;
            padding: 2px 4px;
            border-radius: 4px;
        }
        .lock-chime {
            margin-top: 30px;
            padding: 20px;
            border: 1px solid #d6d8db;
            border-radius: 6px;
            background-color: #f8f9fa;
        }
        .lock-chime h2 {
            margin-top: 0;
            font-size: 20px;
        }
        .lock-chime select {
            width: 100%;
            padding: 10px;
            margin: 10px 0 15px;
            border-radius: 4px;
            border: 1px solid #ced4da;
            font-size: 15px;
        }
        .folder-selector {
            margin: 20px 0;
        }
        .folder-selector select {
            width: 100%;
            padding: 12px;
            border-radius: 4px;
            border: 1px solid #ced4da;
            font-size: 15px;
        }
        .video-table-container {
            flex: 1;
            overflow-y: auto;
            overflow-x: hidden;
            border: 1px solid #dee2e6;
            border-radius: 4px;
            margin: 20px 0 0 0;
            min-height: 0;
        }
        .video-table {
            width: 100%;
            border-collapse: collapse;
        }
        .video-table th {
            background-color: #f8f9fa;
            color: #495057;
            font-weight: 600;
            padding: 12px;
            text-align: left;
            position: sticky;
            top: 0;
            z-index: 10;
            border-bottom: 2px solid #dee2e6;
        }
        .video-table td {
            padding: 12px;
            border-bottom: 1px solid #dee2e6;
        }
        .video-table tbody tr:hover {
            background-color: #f8f9fa;
        }
        .video-name {
            color: #007bff;
            cursor: pointer;
            text-decoration: none;
        }
        .video-name:hover {
            text-decoration: underline;
        }
        .btn-download {
            background-color: #17a2b8;
            color: white;
            padding: 6px 12px;
            border-radius: 4px;
            text-decoration: none;
            font-size: 14px;
            display: inline-block;
            margin-right: 5px;
        }
        .btn-download:hover {
            background-color: #138496;
        }
        .btn-session {
            background-color: #6f42c1;
            color: white;
            padding: 6px 12px;
            border-radius: 4px;
            text-decoration: none;
            font-size: 14px;
            display: inline-block;
            margin-right: 5px;
        }
        .btn-session:hover {
            background-color: #5a32a3;
        }
        .btn-delete {
            background-color: #dc3545;
            color: white;
            padding: 6px 12px;
            border-radius: 4px;
            text-decoration: none;
            font-size: 14px;
            display: inline-block;
            border: none;
            cursor: pointer;
        }
        .btn-delete:hover {
            background-color: #c82333;
        }
        .btn-delete-all {
            background-color: #dc3545;
            color: white;
            padding: 10px 20px;
            border-radius: 4px;
            font-size: 14px;
            border: none;
            cursor: pointer;
            margin-bottom: 10px;
        }
        .btn-delete-all:hover {
            background-color: #c82333;
        }
        .folder-controls {
            margin-bottom: 20px;
        }
        .folder-selector {
            margin-bottom: 10px;
        }
        .delete-all-container {
            text-align: right;
            margin-bottom: 10px;
        }
        .video-thumbnail {
            width: 80px;
            height: 45px;
            object-fit: cover;
            border-radius: 4px;
            display: block;
        }
        .thumbnail-cell {
            text-align: center;
            width: 90px;
        }
        #videoPlayer {
            width: 100%;
            max-width: 100%;
            margin: 20px 0;
            display: none;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        .no-videos {
            text-align: center;
            padding: 40px;
            color: #6c757d;
        }
        .session-grid {
            display: grid;
            gap: 15px;
            margin: 20px 0;
            max-width: 1400px;
            margin-left: auto;
            margin-right: auto;
        }
        .session-grid.grid-1 {
            grid-template-columns: 1fr;
        }
        .session-grid.grid-2 {
            grid-template-columns: repeat(2, 1fr);
        }
        .session-grid.grid-3 {
            grid-template-columns: repeat(2, 1fr);
        }
        .session-grid.grid-4 {
            grid-template-columns: repeat(2, 1fr);
        }
        /* Tesla camera layout: 2 rows x 3 columns */
        .session-grid.tesla-layout {
            grid-template-columns: repeat(3, 1fr);
            grid-template-rows: repeat(2, auto);
            gap: 10px;
        }
        /* Specific positioning for Tesla cameras */
        .tesla-left_pillar { grid-row: 1; grid-column: 1; }
        .tesla-front { grid-row: 1; grid-column: 2; }
        .tesla-right_pillar { grid-row: 1; grid-column: 3; }
        .tesla-left_repeater { grid-row: 2; grid-column: 1; }
        .tesla-back { grid-row: 2; grid-column: 2; }
        .tesla-right_repeater { grid-row: 2; grid-column: 3; }
        /* Fallback for any other camera names */
        .tesla-unknown {
            grid-column: span 1;
        }
        .session-video-container {
            background: #f8f9fa;
            border-radius: 8px;
            padding: 10px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .session-video-container video {
            width: 100%;
            max-height: 280px;
            border-radius: 4px;
            background: #000;
        }
        .session-video-label {
            text-align: center;
            font-weight: 600;
            margin-top: 8px;
            color: #495057;
            font-size: 14px;
            cursor: pointer;
        }
        .session-video-label:hover {
            color: #007bff;
        }
        .session-controls {
            text-align: center;
            margin: 20px 0;
            padding: 15px;
            background: #f8f9fa;
            border-radius: 8px;
        }
        .session-controls button {
            margin: 5px;
            padding: 10px 20px;
            font-size: 16px;
        }
        /* Hide mobile cards on desktop by default */
        .mobile-card-container {
            display: none;
        }
        @media (max-width: 768px) {
            /* Show hamburger menu on mobile */
            .hamburger {
                display: flex;
            }
            /* Hide desktop navigation on mobile */
            .nav-links {
                display: none;
            }
            /* Show mobile menu when active */
            .mobile-menu-overlay.active {
                display: block;
            }
            .mobile-menu {
                display: block;
            }
            /* Navbar adjustments */
            .navbar-content {
                flex-direction: row;
                justify-content: flex-start;
                gap: 15px;
            }
            .navbar h1 {
                font-size: 16px;
            }
            /* Reset main-content and container for mobile */
            .main-content {
                height: auto;
                margin: 10px auto;
                padding: 0 10px;
            }
            .container {
                padding: 15px;
                margin-bottom: 10px;
                flex: initial;
                overflow: visible;
            }
            /* Session grid adjustments */
            .session-grid.grid-2,
            .session-grid.grid-3,
            .session-grid.grid-4 {
                grid-template-columns: 1fr;
            }
            /* Keep Tesla layout on mobile but make it narrower */
            .session-grid.tesla-layout {
                grid-template-columns: repeat(3, 1fr);
                gap: 5px;
            }
            .session-video-container {
                padding: 5px;
            }
            .session-video-container video {
                max-height: 150px;
            }
            .session-video-label {
                font-size: 11px;
                margin-top: 4px;
            }
            /* Hide desktop tables on mobile */
            .video-table {
                display: none;
            }
            .video-table-container {
                display: none;
            }
            
            /* Mobile card layout */
            .mobile-card-container {
                display: block;
                max-width: 100%;
                overflow-x: hidden;
            }
            .mobile-card {
                background: white;
                border: 1px solid #ddd;
                border-radius: 8px;
                padding: 15px;
                margin-bottom: 15px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                overflow-wrap: break-word;
                word-wrap: break-word;
                word-break: break-word;
            }
            .mobile-card-header {
                display: flex;
                align-items: flex-start;
                margin-bottom: 12px;
                gap: 12px;
            }
            .mobile-card-thumbnail {
                flex-shrink: 0;
                width: 80px;
                height: 60px;
                object-fit: cover;
                border-radius: 4px;
                background: #f0f0f0;
            }
            .mobile-card-title {
                flex: 1;
                min-width: 0;
            }
            .mobile-card-title a {
                color: #007bff;
                text-decoration: none;
                font-weight: 500;
                word-break: break-word;
            }
            .mobile-card-meta {
                font-size: 12px;
                color: #6c757d;
                margin-top: 4px;
            }
            .mobile-card-info {
                display: grid;
                gap: 8px;
                margin-bottom: 12px;
                font-size: 14px;
            }
            .mobile-card-info-row {
                display: flex;
                justify-content: space-between;
                padding: 6px 0;
                border-bottom: 1px solid #f0f0f0;
            }
            .mobile-card-info-label {
                font-weight: 600;
                color: #495057;
            }
            .mobile-card-info-value {
                color: #6c757d;
                text-align: right;
                overflow-wrap: break-word;
                word-wrap: break-word;
                word-break: break-word;
                max-width: 100%;
            }
            .mobile-card-audio {
                margin: 12px 0;
            }
            .mobile-card-audio audio {
                width: 100%;
                height: 35px;
            }
            .mobile-card-actions {
                display: flex;
                flex-wrap: wrap;
                gap: 8px;
                margin-top: 12px;
                align-items: center;
            }
            .mobile-card-actions .btn-session,
            .mobile-card-actions .btn-download,
            .mobile-card-actions .present-btn,
            .mobile-card-actions .set-chime-btn,
            .mobile-card-actions .btn-delete {
                flex: 1;
                min-width: 100px;
                text-align: center;
                padding: 10px 8px !important;
                font-size: 13px !important;
                border-radius: 4px !important;
                text-decoration: none !important;
                display: flex !important;
                align-items: center !important;
                justify-content: center !important;
                line-height: 1.3 !important;
                box-sizing: border-box !important;
                height: 44px !important;
                min-height: 44px !important;
                max-height: 44px !important;
            }
            .mobile-card-actions .present-btn {
                background-color: #007bff;
                color: white;
                border: none;
            }
            .mobile-card-actions .set-chime-btn {
                background-color: #6f42c1;
                color: white;
                border: none;
            }
            .mobile-card-actions form {
                flex: 1;
                min-width: 100px;
                display: flex;
            }
            .mobile-card-actions form button {
                width: 100% !important;
                padding: 10px 8px !important;
                font-size: 13px !important;
                line-height: 1.3 !important;
                box-sizing: border-box !important;
                height: 44px !important;
                min-height: 44px !important;
                max-height: 44px !important;
                display: flex !important;
                align-items: center !important;
                justify-content: center !important;
            }
            .mobile-card-status {
                display: inline-block;
                padding: 4px 8px;
                border-radius: 4px;
                font-size: 12px;
                font-weight: 500;
            }
            .mobile-card-status.valid {
                background-color: #d4edda;
                color: #155724;
            }
            .mobile-card-status.invalid {
                background-color: #f8d7da;
                color: #721c24;
            }
        }
    </style>
</head>
<body>
    <div class="navbar">
        <div class="navbar-content">
            <div class="hamburger" onclick="toggleMobileMenu()">
                <span></span>
                <span></span>
                <span></span>
            </div>
            <h1><a href="{{ url_for('index') }}">🚗 Tesla USB Gadget Control - {{ hostname }}</a></h1>
            <div class="nav-links">
                <a href="{{ url_for('index') }}" {% if page == 'control' %}class="active"{% endif %}>Control</a>
                <a href="{{ url_for('file_browser') }}" {% if page == 'browser' %}class="active"{% endif %}>Videos</a>
                <a href="{{ url_for('lock_chimes') }}" {% if page == 'chimes' %}class="active"{% endif %}>Lock Chimes</a>
                <a href="{{ url_for('light_shows') }}" {% if page == 'shows' %}class="active"{% endif %}>Light Shows</a>
            </div>
        </div>
    </div>
    
    <!-- Mobile Menu Overlay -->
    <div class="mobile-menu-overlay" id="mobileMenuOverlay" onclick="toggleMobileMenu()"></div>
    
    <!-- Mobile Menu -->
    <div class="mobile-menu" id="mobileMenu">
        <div class="mobile-menu-close" onclick="toggleMobileMenu()">&times;</div>
        <a href="{{ url_for('index') }}" {% if page == 'control' %}class="active"{% endif %}>Control</a>
        <a href="{{ url_for('file_browser') }}" {% if page == 'browser' %}class="active"{% endif %}>Videos</a>
        <a href="{{ url_for('lock_chimes') }}" {% if page == 'chimes' %}class="active"{% endif %}>Lock Chimes</a>
        <a href="{{ url_for('light_shows') }}" {% if page == 'shows' %}class="active"{% endif %}>Light Shows</a>
    </div>
    
    <div class="main-content">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                <div class="messages">
                    {% for cat, msg in messages %}
                        <div class="{{cat}}">{{msg}}</div>
                    {% endfor %}
                </div>
            {% endif %}
        {% endwith %}
        
        {% block content %}{% endblock %}
    </div>
    
    <script>
    // Mobile menu toggle
    function toggleMobileMenu() {
        const mobileMenu = document.getElementById('mobileMenu');
        const overlay = document.getElementById('mobileMenuOverlay');
        
        if (mobileMenu && overlay) {
            mobileMenu.classList.toggle('active');
            overlay.classList.toggle('active');
        }
    }
    
    // Global audio player management: pause all other audio/video when one starts playing
    // (Excluded from multi-camera session view where multiple videos play simultaneously)
    document.addEventListener('DOMContentLoaded', function() {
        // Check if we're on the multi-camera session view page
        const isSessionView = document.querySelector('.session-grid') !== null;
        
        // Skip auto-pause behavior on session view (has its own sync controls)
        if (isSessionView) {
            return;
        }
        
        // Get all audio and video elements on the page
        const allMediaElements = document.querySelectorAll('audio, video');
        
        allMediaElements.forEach(function(media) {
            media.addEventListener('play', function() {
                // When this media starts playing, pause all others
                allMediaElements.forEach(function(otherMedia) {
                    if (otherMedia !== media && !otherMedia.paused) {
                        otherMedia.pause();
                    }
                });
            });
        });
    });
    </script>
</body>
</html>
"""

HTML_CONTROL_PAGE = """
{% extends HTML_TEMPLATE %}
{% block content %}
<div class="container">
    <div class="status-label {{ mode_class }}">Current Mode: {{ mode_label }}</div>
    
    <div id="loadingOverlay" class="loading-overlay">
        <div class="spinner"></div>
        <div class="loading-text">Switching modes, please wait...</div>
    </div>
    
    <form method="post" action="{{url_for('present_usb')}}" id="presentForm" style="display: inline;">
        <button type="submit" class="present-btn" id="presentBtn">📱 Present USB Gadget</button>
    </form>
    
    <form method="post" action="{{url_for('edit_usb')}}" id="editForm" style="display: inline;">
        <button type="submit" class="edit-btn" id="editBtn">📁 Edit USB (mount + Samba)</button>
    </form>
    
    <div class="info-box">
        <strong>Present USB Mode:</strong> Pi appears as USB storage to Tesla. Files are accessible in read-only mode locally.<br>
        <strong>Edit USB Mode:</strong> Partitions mounted locally with Samba access for full read-write access.
    </div>

    {% if share_paths %}
    <div class="shares">
        <strong>Network Shares:</strong>
        <ul>
            {% for path in share_paths %}
            <li><code>{{ path }}</code></li>
            {% endfor %}
        </ul>
    </div>
    {% endif %}
</div>

<script>
// Prevent multiple mode switch submissions
const presentForm = document.getElementById('presentForm');
const editForm = document.getElementById('editForm');
const presentBtn = document.getElementById('presentBtn');
const editBtn = document.getElementById('editBtn');
const loadingOverlay = document.getElementById('loadingOverlay');

function disableButtons() {
    presentBtn.disabled = true;
    editBtn.disabled = true;
    presentBtn.style.opacity = '0.5';
    editBtn.style.opacity = '0.5';
    presentBtn.style.cursor = 'not-allowed';
    editBtn.style.cursor = 'not-allowed';
    loadingOverlay.style.display = 'flex';
}

presentForm.addEventListener('submit', function(e) {
    if (presentBtn.disabled) {
        e.preventDefault();
        return false;
    }
    disableButtons();
});

editForm.addEventListener('submit', function(e) {
    if (editBtn.disabled) {
        e.preventDefault();
        return false;
    }
    disableButtons();
});
</script>
{% endblock %}
"""

HTML_BROWSER_PAGE = """
{% extends HTML_TEMPLATE %}
{% block content %}
<div class="container">
    <h2>📹 TeslaCam Video Browser</h2>
    <div class="status-label {{ mode_class }}">Current Mode: {{ mode_label }}</div>
    
    {% if not teslacam_available %}
    <div class="no-videos">
        <p><strong>TeslaCam folder is not accessible.</strong></p>
        <p>Make sure the system is in Present or Edit mode and the TeslaCam folder exists.</p>
    </div>
    {% elif folders %}
    <div class="folder-controls">
        <div class="folder-selector">
            <label for="folderSelect"><strong>Select Folder:</strong></label>
            <select id="folderSelect" onchange="loadFolder(this.value)">
                {% for folder in folders %}
                <option value="{{ folder.name }}" {% if folder.name == current_folder %}selected{% endif %}>
                    {{ folder.name }}
                </option>
                {% endfor %}
            </select>
        </div>
        {% if mode_token == 'edit' and (videos or remaining_videos) %}
        <div class="delete-all-container">
            <form method="post" action="{{ url_for('delete_all_videos', folder=current_folder) }}" 
                  onsubmit="return confirm('Are you sure you want to delete ALL {{ total_video_count }} videos in {{ current_folder }}? This cannot be undone!');" 
                  style="display: inline;">
                <button type="submit" class="btn-delete-all">🗑️ Delete All Videos</button>
            </form>
        </div>
        {% endif %}
    </div>
    
    <video id="videoPlayer" controls></video>
    
    {% if videos %}
    <div class="video-table-container">
        <table class="video-table">
            <thead>
                <tr>
                    <th class="thumbnail-cell">Preview</th>
                    <th>Filename</th>
                    <th>Size</th>
                    <th>Modified</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                {% for video in videos %}
                <tr>
                    <td class="thumbnail-cell">
                        <img src="{{ url_for('get_thumbnail', folder=current_folder, filename=video.name) }}" 
                             alt="Thumbnail" 
                             class="video-thumbnail"
                             loading="lazy"
                             onerror="this.style.display='none'">
                    </td>
                    <td>
                        <a href="#" class="video-name" onclick="playVideo('{{ video.name }}'); return false;">
                            {{ video.name }}
                        </a>
                        {% if video.session %}
                        <br><small style="color: #6c757d;">Session: {{ video.session }} | Camera: {{ video.camera }}</small>
                        {% endif %}
                    </td>
                    <td>{{ video.size_mb }} MB</td>
                    <td>{{ video.modified }}</td>
                    <td>
                        {% if video.session %}
                        <a href="{{ url_for('view_session', folder=current_folder, session=video.session) }}" 
                           class="btn-session" 
                           title="View all cameras for this session">
                            📹 Session
                        </a>
                        {% endif %}
                        <a href="{{ url_for('download_video', folder=current_folder, filename=video.name) }}" 
                           class="btn-download" download>
                            ⬇️ Download
                        </a>
                        {% if mode_token == 'edit' %}
                        <form method="post" action="{{ url_for('delete_video', folder=current_folder, filename=video.name) }}" 
                              onsubmit="return confirm('Are you sure you want to delete {{ video.name }}?');" 
                              style="display: inline;">
                            <button type="submit" class="btn-delete">🗑️ Delete</button>
                        </form>
                        {% endif %}
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    
    <!-- Mobile Card Layout -->
    <div class="mobile-card-container">
        {% for video in videos %}
        <div class="mobile-card">
            <div class="mobile-card-header">
                <img src="{{ url_for('get_thumbnail', folder=current_folder, filename=video.name) }}" 
                     alt="Thumbnail" 
                     class="mobile-card-thumbnail"
                     loading="lazy"
                     onerror="this.style.display='none'">
                <div class="mobile-card-title">
                    <a href="#" onclick="playVideo('{{ video.name }}'); return false;">
                        {{ video.name }}
                    </a>
                    {% if video.session %}
                    <div class="mobile-card-meta">Session: {{ video.session }} | Camera: {{ video.camera }}</div>
                    {% endif %}
                </div>
            </div>
            <div class="mobile-card-info">
                <div class="mobile-card-info-row">
                    <span class="mobile-card-info-label">Size:</span>
                    <span class="mobile-card-info-value">{{ video.size_mb }} MB</span>
                </div>
                <div class="mobile-card-info-row">
                    <span class="mobile-card-info-label">Modified:</span>
                    <span class="mobile-card-info-value">{{ video.modified }}</span>
                </div>
            </div>
            <div class="mobile-card-actions">
                {% if video.session %}
                <a href="{{ url_for('view_session', folder=current_folder, session=video.session) }}" 
                   class="btn-session" 
                   title="View all cameras for this session">
                    📹 Session
                </a>
                {% endif %}
                <a href="{{ url_for('download_video', folder=current_folder, filename=video.name) }}" 
                   class="btn-download" download>
                    ⬇️ Download
                </a>
                {% if mode_token == 'edit' %}
                <form method="post" action="{{ url_for('delete_video', folder=current_folder, filename=video.name) }}" 
                      onsubmit="return confirm('Are you sure you want to delete {{ video.name }}?');" 
                      style="display: inline;">
                    <button type="submit" class="btn-delete">🗑️ Delete</button>
                </form>
                {% endif %}
            </div>
        </div>
        {% endfor %}
    </div>
    {% else %}
    <div class="no-videos">
        <p>No videos found in this folder.</p>
    </div>
    {% endif %}
    {% else %}
    <div class="no-videos">
        <p>No TeslaCam folders found.</p>
    </div>
    {% endif %}
</div>

<!-- Loading indicator for infinite scroll -->
<div id="loadingIndicator" style="display: none; text-align: center; padding: 30px; margin: 20px 0;">
    <div style="display: inline-block;">
        <div style="border: 4px solid #f3f3f3; border-top: 4px solid #007bff; border-radius: 50%; width: 40px; height: 40px; animation: spin 1s linear infinite; margin: 0 auto;"></div>
        <p style="margin-top: 15px; color: #666; font-size: 14px;">Loading more videos...</p>
    </div>
</div>

<style>
@keyframes spin {
    0% { transform: rotate(0deg); }
    100% { transform: rotate(360deg); }
}
</style>

<script>
// Remaining videos data for lazy loading
window.remainingVideos = {{ remaining_videos|tojson|safe }};
window.currentFolder = {{ current_folder|tojson|safe }};
window.modeToken = {{ mode_token|tojson|safe }};
window.videosLoaded = false;
window.isLoading = false;
window.currentBatchIndex = 0;
const BATCH_SIZE = 15;

function loadFolder(folderName) {
    window.location.href = "{{ url_for('file_browser') }}?folder=" + encodeURIComponent(folderName);
}

function playVideo(filename) {
    const videoPlayer = document.getElementById('videoPlayer');
    const folder = document.getElementById('folderSelect').value;
    videoPlayer.src = "{{ url_for('stream_video', folder='FOLDER_PLACEHOLDER', filename='FILE_PLACEHOLDER') }}"
        .replace('FOLDER_PLACEHOLDER', encodeURIComponent(folder))
        .replace('FILE_PLACEHOLDER', encodeURIComponent(filename));
    videoPlayer.style.display = 'block';
    videoPlayer.play();
    videoPlayer.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// Infinite scroll: Load more videos as user scrolls
function loadMoreVideos() {
    if (window.isLoading || !window.remainingVideos || window.remainingVideos.length === 0) {
        return;
    }
    
    const startIndex = window.currentBatchIndex;
    const endIndex = Math.min(startIndex + BATCH_SIZE, window.remainingVideos.length);
    const batch = window.remainingVideos.slice(startIndex, endIndex);
    
    if (batch.length === 0) {
        return;
    }
    
    window.isLoading = true;
    document.getElementById('loadingIndicator').style.display = 'block';
    
    // Small delay to show loading indicator (smoother UX)
    setTimeout(function() {
        // Render desktop table rows
        const tableBody = document.querySelector('.video-table-container tbody');
        if (tableBody) {
            batch.forEach(function(video) {
                const row = createTableRow(video);
                tableBody.appendChild(row);
            });
        }
        
        // Render mobile cards
        const mobileContainer = document.querySelector('.mobile-card-container');
        if (mobileContainer) {
            batch.forEach(function(video) {
                const card = createMobileCard(video);
                mobileContainer.appendChild(card);
            });
        }
        
        window.currentBatchIndex = endIndex;
        window.isLoading = false;
        document.getElementById('loadingIndicator').style.display = 'none';
        
        // If all videos loaded, mark as complete
        if (window.currentBatchIndex >= window.remainingVideos.length) {
            window.videosLoaded = true;
        }
    }, 100);
}

// Create table row for desktop view
function createTableRow(video) {
    const tr = document.createElement('tr');
    
    const sessionInfo = video.session ? 
        `<br><small style="color: #6c757d;">Session: ${escapeHtml(video.session)} | Camera: ${escapeHtml(video.camera)}</small>` : '';
    
    const sessionBtn = video.session ? 
        `<a href="/videos/session/${encodeURIComponent(window.currentFolder)}/${encodeURIComponent(video.session)}" 
            class="btn-session" title="View all cameras for this session">📹 Session</a>` : '';
    
    const deleteBtn = window.modeToken === 'edit' ? 
        `<form method="post" action="/videos/delete/${encodeURIComponent(window.currentFolder)}/${encodeURIComponent(video.name)}" 
              onsubmit="return confirm('Are you sure you want to delete ${escapeHtml(video.name)}?');" 
              style="display: inline;">
            <button type="submit" class="btn-delete">🗑️ Delete</button>
        </form>` : '';
    
    tr.innerHTML = `
        <td class="thumbnail-cell">
            <img src="/videos/thumbnail/${encodeURIComponent(window.currentFolder)}/${encodeURIComponent(video.name)}" 
                 alt="Thumbnail" 
                 class="video-thumbnail"
                 loading="lazy"
                 onerror="this.style.display='none'">
        </td>
        <td>
            <a href="#" class="video-name" onclick="playVideo('${escapeHtml(video.name)}'); return false;">
                ${escapeHtml(video.name)}
            </a>
            ${sessionInfo}
        </td>
        <td>${video.size_mb} MB</td>
        <td>${escapeHtml(video.modified)}</td>
        <td>
            ${sessionBtn}
            <a href="/videos/download/${encodeURIComponent(window.currentFolder)}/${encodeURIComponent(video.name)}" 
               class="btn-download" download>⬇️ Download</a>
            ${deleteBtn}
        </td>
    `;
    
    return tr;
}

// Create mobile card
function createMobileCard(video) {
    const div = document.createElement('div');
    div.className = 'mobile-card';
    
    const sessionInfo = video.session ? 
        `<div class="mobile-card-meta">Session: ${escapeHtml(video.session)} | Camera: ${escapeHtml(video.camera)}</div>` : '';
    
    const sessionBtn = video.session ? 
        `<a href="/videos/session/${encodeURIComponent(window.currentFolder)}/${encodeURIComponent(video.session)}" 
            class="btn-session" title="View all cameras for this session">📹 Session</a>` : '';
    
    const deleteBtn = window.modeToken === 'edit' ? 
        `<form method="post" action="/videos/delete/${encodeURIComponent(window.currentFolder)}/${encodeURIComponent(video.name)}" 
              onsubmit="return confirm('Are you sure you want to delete ${escapeHtml(video.name)}?');" 
              style="display: inline;">
            <button type="submit" class="btn-delete">🗑️ Delete</button>
        </form>` : '';
    
    div.innerHTML = `
        <div class="mobile-card-header">
            <img src="/videos/thumbnail/${encodeURIComponent(window.currentFolder)}/${encodeURIComponent(video.name)}" 
                 alt="Thumbnail" 
                 class="mobile-card-thumbnail"
                 loading="lazy"
                 onerror="this.style.display='none'">
            <div class="mobile-card-title">
                <a href="#" onclick="playVideo('${escapeHtml(video.name)}'); return false;">
                    ${escapeHtml(video.name)}
                </a>
                ${sessionInfo}
            </div>
        </div>
        <div class="mobile-card-info">
            <div class="mobile-card-info-row">
                <span class="mobile-card-info-label">Size:</span>
                <span class="mobile-card-info-value">${video.size_mb} MB</span>
            </div>
            <div class="mobile-card-info-row">
                <span class="mobile-card-info-label">Modified:</span>
                <span class="mobile-card-info-value">${escapeHtml(video.modified)}</span>
            </div>
        </div>
        <div class="mobile-card-actions">
            ${sessionBtn}
            <a href="/videos/download/${encodeURIComponent(window.currentFolder)}/${encodeURIComponent(video.name)}" 
               class="btn-download" download>⬇️ Download</a>
            ${deleteBtn}
        </div>
    `;
    
    return div;
}

// HTML escape helper
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Scroll event handler with 85% trigger (works for both desktop table and mobile)
function handleScroll(element) {
    if (window.videosLoaded) {
        return;
    }
    
    let scrollPosition, pageHeight, scrollPercentage;
    
    // Check if scrolling within desktop table container or window (mobile)
    if (element && element.classList && element.classList.contains('video-table-container')) {
        // Desktop: table container scroll
        scrollPosition = element.scrollTop + element.clientHeight;
        pageHeight = element.scrollHeight;
        scrollPercentage = (scrollPosition / pageHeight) * 100;
    } else {
        // Mobile: window scroll
        scrollPosition = window.scrollY + window.innerHeight;
        pageHeight = document.documentElement.scrollHeight;
        scrollPercentage = (scrollPosition / pageHeight) * 100;
    }
    
    // Trigger at 85% scroll
    if (scrollPercentage >= 85) {
        loadMoreVideos();
    }
}

// Debounce scroll events
let scrollTimeout;

// Auto-play video if 'play' parameter is in URL
window.addEventListener('DOMContentLoaded', function() {
    const urlParams = new URLSearchParams(window.location.search);
    const videoToPlay = urlParams.get('play');
    if (videoToPlay) {
        // Small delay to ensure page is fully loaded
        setTimeout(function() {
            playVideo(videoToPlay);
        }, 300);
    }
    
    // Only enable infinite scroll if we have remaining videos (Videos page only)
    if (!window.remainingVideos || window.remainingVideos.length === 0) {
        return;
    }
    
    // Check if we're on mobile (using card view) or desktop (using table view)
    const isMobile = function() {
        return window.innerWidth <= 768;
    };
    
    // Add scroll listener to desktop table container (desktop only)
    const tableContainer = document.querySelector('.video-table-container');
    if (tableContainer) {
        tableContainer.addEventListener('scroll', function() {
            // Only handle table scroll on desktop
            if (!isMobile()) {
                if (scrollTimeout) {
                    clearTimeout(scrollTimeout);
                }
                scrollTimeout = setTimeout(function() {
                    handleScroll(tableContainer);
                }, 50);
            }
        });
        
        // Initial check for desktop table
        if (!isMobile()) {
            setTimeout(function() {
                handleScroll(tableContainer);
            }, 500);
        }
    }
    
    // Add scroll listener to window (mobile only)
    window.addEventListener('scroll', function() {
        // Only handle window scroll on mobile
        if (isMobile()) {
            if (scrollTimeout) {
                clearTimeout(scrollTimeout);
            }
            scrollTimeout = setTimeout(function() {
                handleScroll(null);
            }, 50);
        }
    });
    
    // Initial check for mobile
    if (isMobile()) {
        setTimeout(function() {
            handleScroll(null);
        }, 500);
    }
});
</script>
{% endblock %}
"""

HTML_SESSION_PAGE = """
{% extends HTML_TEMPLATE %}
{% block content %}
<div class="container">
    <h2>📹 Multi-Camera Session View</h2>
    <div class="status-label {{ mode_class }}">Current Mode: {{ mode_label }}</div>
    
    <div class="info-box">
        <strong>Session:</strong> {{ session_id }}<br>
        <strong>Folder:</strong> {{ folder }}<br>
        <strong>Cameras:</strong> {{ videos|length }} view(s)
    </div>
    
    <div class="session-controls">
        <button onclick="playAll()" class="present-btn">▶️ Play All</button>
        <button onclick="pauseAll()" class="edit-btn">⏸️ Pause All</button>
        <button onclick="seekAll(0)" class="set-chime-btn">⏮️ Restart</button>
        <button onclick="syncAll()" class="present-btn">🔄 Sync Playback</button>
        <button onclick="toggleAutoSync()" id="autoSyncBtn" class="edit-btn">🔄 Auto-Sync: ON</button>
        <button onclick="toggleLowBandwidth()" id="lowBandwidthBtn" class="edit-btn">📶 Low Bandwidth: ON</button>
    </div>
    
    <div class="info-box" style="background-color: #e3f2fd; border-left: 4px solid #2196f3; margin: 10px 0;">
        <p style="margin: 0; font-size: 13px;">
            <strong>💡 Performance Tips:</strong><br>
            • <strong>Low Bandwidth Mode</strong>: Enabled by default - streams videos on-demand (recommended for Pi Zero 2 W)<br>
            • <strong>Auto-Sync</strong>: Keeps cameras synchronized during playback<br>
            • Videos use HTTP streaming - browser buffers only what's needed
        </p>
    </div>
    
    <div class="session-grid tesla-layout" id="sessionGrid">
        {% for video in videos %}
        <div class="session-video-container tesla-{{ video.camera|replace('_', '_')|lower }}" data-camera-index="{{ loop.index0 }}">
            <video id="video-{{ loop.index0 }}" controls preload="none"
                   src="{{ url_for('stream_video', folder=folder, filename=video.name) }}">
                Your browser does not support the video tag.
            </video>
            <div class="session-video-label" onclick="openFullVideo('{{ video.name }}')">
                📹 {{ video.camera|replace('_', ' ')|title }}
                <span id="status-{{ loop.index0 }}" style="font-size: 11px; color: #666;"></span>
            </div>
        </div>
        {% endfor %}
    </div>
    
    <div style="text-align: center; margin-top: 20px;">
        <a href="{{ url_for('file_browser', folder=folder) }}" class="btn-download">
            ← Back to Video List
        </a>
    </div>
</div>

<script>
// Get all video elements
const videos = document.querySelectorAll('.session-video-container video');
let autoSyncEnabled = true;
let lowBandwidthMode = true; // Start with low bandwidth mode enabled by default (optimal for Pi Zero 2 W)
let syncInterval = null;
const SYNC_THRESHOLD = 0.2; // Sync if drift is more than 0.2 seconds
const SYNC_CHECK_INTERVAL = 500; // Check every 500ms for tighter sync

// Toggle low bandwidth mode
function toggleLowBandwidth() {
    lowBandwidthMode = !lowBandwidthMode;
    const btn = document.getElementById('lowBandwidthBtn');
    
    if (lowBandwidthMode) {
        btn.textContent = '📶 Low Bandwidth: ON';
        btn.className = 'edit-btn';
        // Change all videos to preload=none (streaming only)
        videos.forEach((video, index) => {
            video.preload = 'none';
            const statusElem = document.getElementById(`status-${index}`);
            if (statusElem) {
                statusElem.textContent = ' (Stream mode)';
                statusElem.style.color = '#2196f3';
            }
        });
        console.log('Low bandwidth mode: Videos will stream on-demand only');
    } else {
        btn.textContent = '📶 Low Bandwidth: OFF';
        btn.className = 'present-btn';
        // Change to preload=metadata (light buffering)
        videos.forEach((video, index) => {
            video.preload = 'metadata';
            const statusElem = document.getElementById(`status-${index}`);
            if (statusElem) {
                statusElem.textContent = ' (Buffer mode)';
                statusElem.style.color = '#4caf50';
            }
        });
        console.log('Normal mode: Videos will buffer metadata');
    }
}

// Toggle auto-sync on/off
function toggleAutoSync() {
    autoSyncEnabled = !autoSyncEnabled;
    const btn = document.getElementById('autoSyncBtn');
    btn.textContent = autoSyncEnabled ? '🔄 Auto-Sync: ON' : '🔄 Auto-Sync: OFF';
    btn.className = autoSyncEnabled ? 'edit-btn' : 'btn-delete';
    
    if (!autoSyncEnabled && syncInterval) {
        clearInterval(syncInterval);
        syncInterval = null;
    } else if (autoSyncEnabled && !videos[0].paused) {
        startAutoSync();
    }
}

// Show loading/buffering status for each video
videos.forEach((video, index) => {
    // Buffer progress monitoring
    video.addEventListener('progress', () => {
        if (lowBandwidthMode) return; // Don't show buffer in low bandwidth mode
        
        if (video.buffered.length > 0) {
            const bufferedEnd = video.buffered.end(video.buffered.length - 1);
            const duration = video.duration;
            if (duration > 0) {
                const bufferedSeconds = Math.round(bufferedEnd);
                const statusElem = document.getElementById(`status-${index}`);
                if (statusElem && !video.paused) {
                    statusElem.textContent = ` (${bufferedSeconds}s buffered)`;
                    statusElem.style.color = '#ff9800';
                }
            }
        }
    });
    
    // Video is ready to play
    video.addEventListener('canplay', () => {
        const statusElem = document.getElementById(`status-${index}`);
        if (statusElem && !lowBandwidthMode) {
            statusElem.textContent = ' (Ready)';
            statusElem.style.color = '#4caf50';
        }
    });
    
    // Video is playing
    video.addEventListener('playing', () => {
        const statusElem = document.getElementById(`status-${index}`);
        if (statusElem) {
            statusElem.textContent = lowBandwidthMode ? ' (Streaming...)' : ' (Playing)';
            statusElem.style.color = '#4caf50';
        }
    });
    
    // Video is waiting for data
    video.addEventListener('waiting', () => {
        const statusElem = document.getElementById(`status-${index}`);
        if (statusElem) {
            statusElem.textContent = ' (Buffering...)';
            statusElem.style.color = '#ff9800';
        }
    });
    
    // Video stalled (network issue)
    video.addEventListener('stalled', () => {
        const statusElem = document.getElementById(`status-${index}`);
        if (statusElem) {
            statusElem.textContent = ' (Stalled)';
            statusElem.style.color = '#f44336';
        }
    });
});

// Open individual video in full view
function openFullVideo(filename) {
    window.location.href = "{{ url_for('file_browser', folder=folder) }}&play=" + encodeURIComponent(filename);
}

// Sync all videos to the first video's time
function syncAll() {
    if (videos.length === 0) return;
    const masterTime = videos[0].currentTime;
    videos.forEach((video, index) => {
        if (index !== 0) {
            video.currentTime = masterTime;
        }
    });
}

// Start automatic sync monitoring
function startAutoSync() {
    if (!autoSyncEnabled || syncInterval) return;
    
    syncInterval = setInterval(() => {
        if (!autoSyncEnabled || videos[0].paused) {
            clearInterval(syncInterval);
            syncInterval = null;
            return;
        }
        
        const masterTime = videos[0].currentTime;
        videos.forEach((video, index) => {
            if (index !== 0 && !video.paused) {
                const diff = Math.abs(video.currentTime - masterTime);
                // Re-sync if drift exceeds threshold
                if (diff > SYNC_THRESHOLD) {
                    console.log(`Syncing video ${index}: drift ${diff.toFixed(2)}s`);
                    video.currentTime = masterTime;
                }
            }
        });
    }, SYNC_CHECK_INTERVAL);
}

// Play all videos with sync
function playAll() {
    syncAll(); // Sync before playing
    
    // Start playing all videos
    const playPromises = Array.from(videos).map(video => 
        video.play().catch(e => {
            console.log('Play failed:', e);
            return null;
        })
    );
    
    // Once all start playing, enable auto-sync
    Promise.all(playPromises).then(() => {
        if (autoSyncEnabled) {
            startAutoSync();
        }
    });
}

// Pause all videos
function pauseAll() {
    videos.forEach(video => video.pause());
    if (syncInterval) {
        clearInterval(syncInterval);
        syncInterval = null;
    }
}

// Seek all videos to a specific time
function seekAll(time) {
    videos.forEach(video => {
        video.currentTime = time;
    });
}

// Master video (first one) controls all others
if (videos.length > 0) {
    // When master plays, play all others
    videos[0].addEventListener('play', () => {
        const masterTime = videos[0].currentTime;
        videos.forEach((video, index) => {
            if (index !== 0 && video.paused) {
                video.currentTime = masterTime;
                video.play().catch(e => console.log('Auto-play failed:', e));
            }
        });
        
        if (autoSyncEnabled) {
            startAutoSync();
        }
    });
    
    // When master pauses, pause all others
    videos[0].addEventListener('pause', () => {
        videos.forEach((video, index) => {
            if (index !== 0) {
                video.pause();
            }
        });
        
        if (syncInterval) {
            clearInterval(syncInterval);
            syncInterval = null;
        }
    });
    
    // When master seeks, seek all others
    videos[0].addEventListener('seeked', () => {
        const masterTime = videos[0].currentTime;
        videos.forEach((video, index) => {
            if (index !== 0) {
                video.currentTime = masterTime;
            }
        });
    });
}

// Initialize status indicators
window.addEventListener('load', () => {
    console.log('Multi-camera session view loaded. Using HTTP range requests for streaming.');
    console.log(`Videos: ${videos.length}, Low Bandwidth: ${lowBandwidthMode ? 'ON' : 'OFF'}`);
    
    videos.forEach((video, index) => {
        const statusElem = document.getElementById(`status-${index}`);
        if (statusElem) {
            statusElem.textContent = lowBandwidthMode ? ' (Stream mode)' : ' (Loading...)';
            statusElem.style.color = lowBandwidthMode ? '#2196f3' : '#ff9800';
        }
    });
});
</script>
{% endblock %}
"""

HTML_LOCK_CHIMES_PAGE = """
{% extends HTML_TEMPLATE %}
{% block content %}
<div class="container">
    <h2>🔔 Lock Chimes</h2>
    <div class="status-label {{ mode_class }}">Current Mode: {{ mode_label }}</div>
    
    {% if mode_token == 'edit' %}
    <!-- iOS Safari Warning -->
    <div id="iosWarning" style="display: none; background-color: #fff3cd; border-left: 4px solid #ffc107; padding: 15px; border-radius: 4px; margin-bottom: 20px;">
        <p style="margin: 0; font-size: 14px;"><strong>⚠️ iOS Browser Limitation:</strong> File uploading is only available through Safari when running on iOS. Please open this page in Safari to upload files.</p>
        <p style="margin: 8px 0 0 0; font-size: 13px; color: #666;">Note: Desktop browsers (Windows/Mac/Linux) work normally regardless of browser choice.</p>
    </div>
    
    <div class="folder-controls" id="chimeUploadControls">
        <form method="post" action="{{ url_for('upload_lock_chime') }}" enctype="multipart/form-data" style="margin-bottom: 20px;" id="chimeUploadForm">
            <label for="chime_file" style="display: block; margin-bottom: 8px; font-weight: 600;">Upload New Chime to Library:</label>
            <input type="file" name="chime_file" id="chime_file" accept=".wav" required 
                   style="display: block; margin-bottom: 10px; padding: 10px; border: 2px solid #ddd; border-radius: 4px; background: white; width: 100%; max-width: 400px; font-size: 14px;">
            <button type="submit" class="edit-btn" id="chimeUploadBtn">📤 Upload</button>
            <p style="margin: 5px 0 0 0; font-size: 12px; color: #666;">
                Optimal: 16-bit PCM, 44.1 or 48 kHz, under 1MB<br>
                Files not meeting requirements will be automatically re-encoded
            </p>
        </form>
        <!-- Upload Progress Bar -->
        <div id="chimeUploadProgress" style="display: none; margin-bottom: 20px;">
            <div style="background: #f0f0f0; border-radius: 8px; padding: 15px; border: 2px solid #007bff;">
                <h4 style="margin: 0 0 10px 0; color: #007bff;">📤 Uploading Chime...</h4>
                <div style="background: #e0e0e0; border-radius: 4px; height: 30px; overflow: hidden; margin-bottom: 10px;">
                    <div id="chimeProgressBar" style="background: linear-gradient(90deg, #007bff, #0056b3); height: 100%; width: 0%; transition: width 0.3s; display: flex; align-items: center; justify-content: center; color: white; font-weight: bold; font-size: 14px;">
                        0%
                    </div>
                </div>
                <p id="chimeUploadStatus" style="margin: 0; font-size: 13px; color: #666;">Preparing upload...</p>
            </div>
        </div>
    </div>
    <div class="info-box" style="background-color: #fff3cd; border-left: 4px solid #ffc107; margin-bottom: 20px;">
        <p style="margin: 0; font-size: 14px;"><strong>⚠️ Tesla Cache Note:</strong> Tesla may take 5-30 minutes to recognize a new lock chime due to aggressive caching. If the old chime still plays after setting a new one:</p>
        <ul style="margin: 8px 0 0 20px; font-size: 13px;">
            <li>Put car to sleep (walk away for 5+ minutes), then wake it</li>
            <li>Switch to Present mode, wait 10 seconds, then back to Edit mode (forces USB re-enumeration)</li>
            <li>Physically unplug/replug the Pi from Tesla's USB port</li>
        </ul>
    </div>
    {% endif %}
    
    <!-- Active Lock Chime -->
    {% if active_chime %}
    <div style="background: #e8f5e9; padding: 15px; border-radius: 8px; margin-bottom: 20px; border-left: 4px solid #4caf50;">
        <h3 style="margin: 0 0 10px 0; color: #2e7d32;">🔊 Active Lock Chime</h3>
        <div style="display: flex; align-items: center; gap: 15px;">
            <div>
                <strong>{{ active_chime.filename }}</strong><br>
                <span style="color: #666; font-size: 13px;">{{ active_chime.size_str }}</span>
            </div>
            <audio controls preload="none" style="flex: 1; max-width: 300px; height: 30px;">
                <source src="{{ url_for('play_active_chime') }}?v={{ active_chime.mtime }}" type="audio/wav">
            </audio>
        </div>
    </div>
    {% else %}
    <div class="info-box" style="background-color: #ffebee; border-left: 4px solid #f44336; margin-bottom: 20px;">
        <p style="margin: 0;">⚠️ No active lock chime set. Select a chime from the library below.</p>
    </div>
    {% endif %}
    
    <!-- Chime Library -->
    <h3 style="margin: 20px 0 10px 0;">📚 Chime Library</h3>
    {% if chime_files %}
    <div class="video-table-container">
        <table class="video-table">
            <thead>
                <tr>
                    <th>Filename</th>
                    <th>Size</th>
                    <th>Status</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                {% for chime in chime_files %}
                <tr style="{% if not chime.is_valid %}background-color: #ffebee;{% endif %}">
                    <td>{{ chime.filename }}</td>
                    <td>{{ chime.size_str }}</td>
                    <td>
                        {% if chime.is_valid %}
                        <span style="color: #4caf50;">✓ Valid</span>
                        {% else %}
                        <span style="color: #f44336;" title="{{ chime.validation_msg }}">✗ {{ chime.validation_msg[:30] }}...</span>
                        {% endif %}
                    </td>
                    <td>
                        <div style="display: flex; flex-direction: column; gap: 8px;">
                            <audio controls preload="none" style="height: 30px; width: 100%; max-width: 300px;">
                                <source src="{{ url_for('play_lock_chime', filename=chime.filename) }}?v={{ chime.mtime }}" type="audio/wav">
                            </audio>
                            <div style="display: flex; gap: 5px; flex-wrap: wrap; align-items: center;">
                                <a href="{{ url_for('download_lock_chime', filename=chime.filename) }}" class="present-btn" style="text-decoration: none; padding: 6px 12px; display: inline-flex; align-items: center; justify-content: center; border-radius: 4px; line-height: 1.5; height: 34px; box-sizing: border-box;">⬇️ Download</a>
                                {% if mode_token == 'edit' %}
                                    {% if chime.is_valid %}
                                    <form method="post" action="{{ url_for('set_as_chime', filename=chime.filename) }}" style="display: inline;" onsubmit="return handleSetChime(this);">
                                        <button type="submit" class="set-chime-btn" style="padding: 6px 12px; margin: 0; font-size: 14px; height: 34px; display: inline-flex; align-items: center; justify-content: center; box-sizing: border-box;">🔔 Set as Active</button>
                                    </form>
                                    {% endif %}
                                    <form method="post" action="{{ url_for('delete_lock_chime', filename=chime.filename) }}" style="display: inline;" 
                                          onsubmit="return confirm('Are you sure you want to delete {{ chime.filename }}?');">
                                        <button type="submit" class="btn-delete" style="padding: 6px 12px; margin: 0; font-size: 14px; height: 34px; display: inline-flex; align-items: center; justify-content: center; box-sizing: border-box;">🗑️ Delete</button>
                                    </form>
                                {% endif %}
                            </div>
                        </div>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    
    <!-- Mobile Card Layout for Lock Chimes -->
    <div class="mobile-card-container">
        {% for chime in chime_files %}
        <div class="mobile-card" style="{% if not chime.is_valid %}border-left: 4px solid #f44336;{% endif %}">
            <div class="mobile-card-title">
                <strong>{{ chime.filename }}</strong>
            </div>
            <div class="mobile-card-info">
                <div class="mobile-card-info-row">
                    <span class="mobile-card-info-label">Size:</span>
                    <span class="mobile-card-info-value">{{ chime.size_str }}</span>
                </div>
                <div class="mobile-card-info-row">
                    <span class="mobile-card-info-label">Status:</span>
                    <span class="mobile-card-info-value">
                        {% if chime.is_valid %}
                        <span class="mobile-card-status valid">✓ Valid</span>
                        {% else %}
                        <span class="mobile-card-status invalid" title="{{ chime.validation_msg }}">✗ Invalid</span>
                        {% endif %}
                    </span>
                </div>
            </div>
            <div class="mobile-card-audio">
                <audio controls preload="none">
                    <source src="{{ url_for('play_lock_chime', filename=chime.filename) }}?v={{ chime.mtime }}" type="audio/wav">
                </audio>
            </div>
            <div class="mobile-card-actions">
                <a href="{{ url_for('download_lock_chime', filename=chime.filename) }}" class="present-btn">⬇️ Download</a>
                {% if mode_token == 'edit' %}
                    {% if chime.is_valid %}
                    <form method="post" action="{{ url_for('set_as_chime', filename=chime.filename) }}" onsubmit="return handleSetChime(this);">
                        <button type="submit" class="set-chime-btn">🔔 Set as Active</button>
                    </form>
                    {% endif %}
                    <form method="post" action="{{ url_for('delete_lock_chime', filename=chime.filename) }}" 
                          onsubmit="return confirm('Are you sure you want to delete {{ chime.filename }}?');">
                        <button type="submit" class="btn-delete">🗑️ Delete</button>
                    </form>
                {% endif %}
            </div>
        </div>
        {% endfor %}
    </div>
    {% else %}
    <div class="info-box">
        <p>No chimes found in the Chimes library.</p>
        {% if mode_token == 'edit' %}
        <p>Upload WAV files above to add them to your library.</p>
        {% else %}
        <p>Switch to Edit Mode to upload files.</p>
        {% endif %}
    </div>
    {% endif %}
</div>

<!-- Loading overlay for Set as Chime operation -->
<div id="chimeLoadingOverlay" style="display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); z-index: 9999; justify-content: center; align-items: center;">
    <div style="text-align: center; color: white;">
        <div class="spinner" style="border: 8px solid #f3f3f3; border-top: 8px solid #007bff; border-radius: 50%; width: 60px; height: 60px; animation: spin 1s linear infinite; margin: 0 auto 20px;"></div>
        <h3>Setting lock chime...</h3>
        <p>Please wait, this may take a few seconds</p>
    </div>
</div>

<style>
@keyframes spin {
    0% { transform: rotate(0deg); }
    100% { transform: rotate(360deg); }
}
</style>

<script>
function handleSetChime(form) {
    // Disable all Set as Active buttons
    const allChimeButtons = document.querySelectorAll('.set-chime-btn');
    allChimeButtons.forEach(btn => btn.disabled = true);
    
    // Show loading overlay
    document.getElementById('chimeLoadingOverlay').style.display = 'flex';
    
    return true;  // Allow form submission
}

// Handle chime upload with progress bar
document.addEventListener('DOMContentLoaded', function() {
    const form = document.getElementById('chimeUploadForm');
    if (!form) return;
    
    form.addEventListener('submit', function(e) {
        e.preventDefault();
        
        const fileInput = document.getElementById('chime_file');
        const uploadBtn = document.getElementById('chimeUploadBtn');
        const progressDiv = document.getElementById('chimeUploadProgress');
        const progressBar = document.getElementById('chimeProgressBar');
        const statusText = document.getElementById('chimeUploadStatus');
        
        if (!fileInput.files || fileInput.files.length === 0) {
            alert('Please select a file to upload');
            return;
        }
        
        const file = fileInput.files[0];
        const formData = new FormData(form);
        
        // Disable upload button and show progress
        uploadBtn.disabled = true;
        uploadBtn.textContent = '⏳ Uploading...';
        progressDiv.style.display = 'block';
        
        // Create AJAX request with progress tracking
        const xhr = new XMLHttpRequest();
        
        // Track upload progress
        xhr.upload.addEventListener('progress', function(e) {
            if (e.lengthComputable) {
                const percentComplete = Math.round((e.loaded / e.total) * 100);
                progressBar.style.width = percentComplete + '%';
                progressBar.textContent = percentComplete + '%';
                statusText.textContent = `Uploading ${file.name}... (${(e.loaded / 1024).toFixed(1)} KB / ${(e.total / 1024).toFixed(1)} KB)`;
            }
        });
        
        // When upload finishes, show processing status
        xhr.upload.addEventListener('loadend', function() {
            if (xhr.readyState !== 4) {  // If request not complete yet
                progressBar.style.width = '100%';
                progressBar.textContent = '🔄 Processing...';
                progressBar.style.background = '#ffc107';
                statusText.textContent = 'Upload complete! Processing and validating file...';
                statusText.style.color = '#856404';
            }
        });
        
        // Handle completion
        xhr.addEventListener('load', function() {
            let response;
            try {
                response = JSON.parse(xhr.responseText);
            } catch (e) {
                response = null;
            }
            
            if (xhr.status === 200 && response && response.success) {
                progressBar.style.width = '100%';
                progressBar.textContent = '✓ Complete';
                progressBar.style.background = '#28a745';
                
                // Show re-encoding info if present
                let successMsg = 'Upload complete!';
                if (response.reencoded && response.details) {
                    const details = response.details;
                    successMsg = `Upload successful! File was automatically re-encoded to ${details.strategy || 'Tesla format'} (${details.size_mb || '<1'} MB)`;
                }
                
                statusText.textContent = successMsg;
                statusText.style.color = '#28a745';
                
                // Redirect after short delay
                setTimeout(function() {
                    window.location.reload();
                }, 1000);
            } else {
                progressBar.style.background = '#dc3545';
                const errorMsg = response && response.error ? response.error : xhr.statusText || 'Upload failed';
                statusText.textContent = 'Upload failed: ' + errorMsg;
                statusText.style.color = '#dc3545';
                uploadBtn.disabled = false;
                uploadBtn.textContent = '📤 Upload';
            }
        });
        
        // Handle errors
        xhr.addEventListener('error', function() {
            progressBar.style.background = '#dc3545';
            statusText.textContent = 'Upload failed: Network error';
            statusText.style.color = '#dc3545';
            uploadBtn.disabled = false;
            uploadBtn.textContent = '📤 Upload';
        });
        
        // Send the request
        xhr.open('POST', form.action);
        xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');  // Mark as AJAX request
        xhr.send(formData);
    });
});

// iOS Browser Detection and Warning Display
(function() {
    // Detect iOS device
    function isIOS() {
        return /(iPad|iPhone|iPod)/i.test(navigator.userAgent) && !window.MSStream;
    }
    
    // Detect Safari (true Safari, not other browsers using WebKit on iOS)
    function isSafari() {
        var ua = navigator.userAgent;
        // Safari should have "Safari" but NOT Chrome, CriOS, EdgiOS, FxiOS, OPiOS
        return /Safari/i.test(ua) && !/Chrome|CriOS|EdgiOS|FxiOS|OPiOS/i.test(ua);
    }
    
    // Show warning and hide controls if iOS + not Safari
    if (isIOS() && !isSafari()) {
        var warning = document.getElementById('iosWarning');
        var controls = document.getElementById('chimeUploadControls');
        if (warning) warning.style.display = 'block';
        if (controls) controls.style.display = 'none';
    }
})();
</script>
{% endblock %}
"""

HTML_LIGHT_SHOWS_PAGE = """
{% extends HTML_TEMPLATE %}
{% block content %}
<div class="container">
    <h2>💡 Light Shows</h2>
    <div class="status-label {{ mode_class }}">Current Mode: {{ mode_label }}</div>
    
    {% if mode_token == 'edit' %}
    <!-- iOS Safari Warning -->
    <div id="iosWarning" style="display: none; background-color: #fff3cd; border-left: 4px solid #ffc107; padding: 15px; border-radius: 4px; margin-bottom: 20px;">
        <p style="margin: 0; font-size: 14px;"><strong>⚠️ iOS Browser Limitation:</strong> File uploading is only available through Safari when running on iOS. Please open this page in Safari to upload files.</p>
        <p style="margin: 8px 0 0 0; font-size: 13px; color: #666;">Note: Desktop browsers (Windows/Mac/Linux) work normally regardless of browser choice.</p>
    </div>
    
    <div class="folder-controls" id="showUploadControls">
        <form method="post" action="{{ url_for('upload_light_show') }}" enctype="multipart/form-data" style="margin-bottom: 20px;" id="showUploadForm">
            <label for="show_file" style="display: block; margin-bottom: 8px; font-weight: 600;">Upload Light Show File (fseq, mp3, or wav):</label>
            <input type="file" name="show_file" id="show_file" accept=".fseq,.mp3,.wav" required 
                   style="display: block; margin-bottom: 10px; padding: 10px; border: 2px solid #ddd; border-radius: 4px; background: white; width: 100%; max-width: 400px; font-size: 14px;">
            <button type="submit" class="edit-btn" id="showUploadBtn">📤 Upload</button>
        </form>
        <!-- Upload Progress Bar -->
        <div id="showUploadProgress" style="display: none; margin-bottom: 20px;">
            <div style="background: #f0f0f0; border-radius: 8px; padding: 15px; border: 2px solid #6f42c1;">
                <h4 style="margin: 0 0 10px 0; color: #6f42c1;">📤 Uploading Light Show...</h4>
                <div style="background: #e0e0e0; border-radius: 4px; height: 30px; overflow: hidden; margin-bottom: 10px;">
                    <div id="showProgressBar" style="background: linear-gradient(90deg, #6f42c1, #5a32a3); height: 100%; width: 0%; transition: width 0.3s; display: flex; align-items: center; justify-content: center; color: white; font-weight: bold; font-size: 14px;">
                        0%
                    </div>
                </div>
                <p id="showUploadStatus" style="margin: 0; font-size: 13px; color: #666;">Preparing upload...</p>
            </div>
        </div>
    </div>
    {% endif %}
    
    {% if show_groups %}
    <div class="video-table-container">
        <table class="video-table" style="table-layout: fixed;">
            <thead>
                <tr>
                    <th style="width: 25%;">Show Name</th>
                    <th style="width: 45%;">Files</th>
                    <th style="width: 30%;">Actions</th>
                </tr>
            </thead>
            <tbody>
                {% for group in show_groups %}
                <tr>
                    <td style="word-wrap: break-word; overflow-wrap: break-word;">{{ group.base_name }}</td>
                    <td style="word-wrap: break-word; overflow-wrap: break-word; font-size: 0.9em;">
                        {% if group.fseq_file %}
                        <div style="margin-bottom: 5px;">
                            <strong>FSEQ:</strong> {{ group.fseq_file.filename }} ({{ group.fseq_file.size_str }})
                        </div>
                        {% endif %}
                        {% if group.audio_file %}
                        <div>
                            <strong>Audio:</strong> {{ group.audio_file.filename }} ({{ group.audio_file.size_str }})
                        </div>
                        {% endif %}
                    </td>
                    <td>
                        {% if group.audio_file %}
                        <div style="margin-bottom: 8px;">
                            <audio controls preload="none" style="width: 100%; max-width: 200px; height: 30px;">
                                {% if group.audio_file.filename.lower().endswith('.mp3') %}
                                <source src="{{ url_for('play_light_show_audio', partition=group.partition_key, filename=group.audio_file.filename) }}" type="audio/mpeg">
                                {% else %}
                                <source src="{{ url_for('play_light_show_audio', partition=group.partition_key, filename=group.audio_file.filename) }}" type="audio/wav">
                                {% endif %}
                            </audio>
                        </div>
                        {% endif %}
                        {% if mode_token == 'edit' %}
                        <form method="post" action="{{ url_for('delete_light_show', partition=group.partition_key, base_name=group.base_name) }}" style="display: block;" 
                              onsubmit="return confirm('Are you sure you want to delete all files for {{ group.base_name }}?');">
                            <button type="submit" class="btn-delete">🗑️ Delete</button>
                        </form>
                        {% endif %}
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    
    <!-- Mobile Card Layout for Light Shows -->
    <div class="mobile-card-container">
        {% for group in show_groups %}
        <div class="mobile-card">
            <div class="mobile-card-title">
                <strong>{{ group.base_name }}</strong>
            </div>
            <div class="mobile-card-info">
                {% if group.fseq_file %}
                <div class="mobile-card-info-row">
                    <span class="mobile-card-info-label">FSEQ:</span>
                    <span class="mobile-card-info-value" style="font-size: 12px; word-break: break-word;">
                        {{ group.fseq_file.filename }}<br>
                        ({{ group.fseq_file.size_str }})
                    </span>
                </div>
                {% endif %}
                {% if group.audio_file %}
                <div class="mobile-card-info-row">
                    <span class="mobile-card-info-label">Audio:</span>
                    <span class="mobile-card-info-value" style="font-size: 12px; word-break: break-word;">
                        {{ group.audio_file.filename }}<br>
                        ({{ group.audio_file.size_str }})
                    </span>
                </div>
                {% endif %}
            </div>
            {% if group.audio_file %}
            <div class="mobile-card-audio">
                <audio controls preload="none">
                    {% if group.audio_file.filename.lower().endswith('.mp3') %}
                    <source src="{{ url_for('play_light_show_audio', partition=group.partition_key, filename=group.audio_file.filename) }}" type="audio/mpeg">
                    {% else %}
                    <source src="{{ url_for('play_light_show_audio', partition=group.partition_key, filename=group.audio_file.filename) }}" type="audio/wav">
                    {% endif %}
                </audio>
            </div>
            {% endif %}
            {% if mode_token == 'edit' %}
            <div class="mobile-card-actions">
                <form method="post" action="{{ url_for('delete_light_show', partition=group.partition_key, base_name=group.base_name) }}" 
                      onsubmit="return confirm('Are you sure you want to delete all files for {{ group.base_name }}?');">
                    <button type="submit" class="btn-delete">🗑️ Delete</button>
                </form>
            </div>
            {% endif %}
        </div>
        {% endfor %}
    </div>
    {% else %}
    <div class="info-box">
        <p>No light show files found in the LightShow folders.</p>
        {% if mode_token != 'edit' %}
        <p>Switch to Edit Mode to upload files.</p>
        {% endif %}
    </div>
    {% endif %}
</div>

<script>
// Handle light show upload with progress bar
document.addEventListener('DOMContentLoaded', function() {
    const form = document.getElementById('showUploadForm');
    if (!form) return;
    
    form.addEventListener('submit', function(e) {
        e.preventDefault();
        
        const fileInput = document.getElementById('show_file');
        const uploadBtn = document.getElementById('showUploadBtn');
        const progressDiv = document.getElementById('showUploadProgress');
        const progressBar = document.getElementById('showProgressBar');
        const statusText = document.getElementById('showUploadStatus');
        
        if (!fileInput.files || fileInput.files.length === 0) {
            alert('Please select a file to upload');
            return;
        }
        
        const file = fileInput.files[0];
        const formData = new FormData(form);
        
        // Disable upload button and show progress
        uploadBtn.disabled = true;
        uploadBtn.textContent = '⏳ Uploading...';
        progressDiv.style.display = 'block';
        
        // Create AJAX request with progress tracking
        const xhr = new XMLHttpRequest();
        
        // Track upload progress
        xhr.upload.addEventListener('progress', function(e) {
            if (e.lengthComputable) {
                const percentComplete = Math.round((e.loaded / e.total) * 100);
                progressBar.style.width = percentComplete + '%';
                progressBar.textContent = percentComplete + '%';
                
                // Format file size display
                const loadedKB = (e.loaded / 1024).toFixed(1);
                const totalKB = (e.total / 1024).toFixed(1);
                const loadedMB = (e.loaded / 1024 / 1024).toFixed(2);
                const totalMB = (e.total / 1024 / 1024).toFixed(2);
                
                if (e.total > 1024 * 1024) {
                    statusText.textContent = `Uploading ${file.name}... (${loadedMB} MB / ${totalMB} MB)`;
                } else {
                    statusText.textContent = `Uploading ${file.name}... (${loadedKB} KB / ${totalKB} KB)`;
                }
            }
        });
        
        // Handle completion
        xhr.addEventListener('load', function() {
            if (xhr.status === 200 || xhr.status === 302) {
                progressBar.style.width = '100%';
                progressBar.textContent = '100%';
                progressBar.style.background = '#28a745';
                statusText.textContent = 'Upload complete! Redirecting...';
                statusText.style.color = '#28a745';
                
                // Redirect after short delay
                setTimeout(function() {
                    window.location.reload();
                }, 1000);
            } else {
                progressBar.style.background = '#dc3545';
                statusText.textContent = 'Upload failed: ' + xhr.statusText;
                statusText.style.color = '#dc3545';
                uploadBtn.disabled = false;
                uploadBtn.textContent = '📤 Upload';
            }
        });
        
        // Handle errors
        xhr.addEventListener('error', function() {
            progressBar.style.background = '#dc3545';
            statusText.textContent = 'Upload failed: Network error';
            statusText.style.color = '#dc3545';
            uploadBtn.disabled = false;
            uploadBtn.textContent = '📤 Upload';
        });
        
        // Send the request
        xhr.open('POST', form.action);
        xhr.send(formData);
    });
});

// iOS Browser Detection and Warning Display
(function() {
    // Detect iOS device
    function isIOS() {
        return /(iPad|iPhone|iPod)/i.test(navigator.userAgent) && !window.MSStream;
    }
    
    // Detect Safari (true Safari, not other browsers using WebKit on iOS)
    function isSafari() {
        var ua = navigator.userAgent;
        // Safari should have "Safari" but NOT Chrome, CriOS, EdgiOS, FxiOS, OPiOS
        return /Safari/i.test(ua) && !/Chrome|CriOS|EdgiOS|FxiOS|OPiOS/i.test(ua);
    }
    
    // Show warning and hide controls if iOS + not Safari
    if (isIOS() && !isSafari()) {
        var warning = document.getElementById('iosWarning');
        var controls = document.getElementById('showUploadControls');
        if (warning) warning.style.display = 'block';
        if (controls) controls.style.display = 'none';
    }
})();
</script>
{% endblock %}
"""


def run_script(script_name, background=False):
    """Execute a script and return success status and message."""
    script_path = os.path.join(GADGET_DIR, script_name)
    
    if not os.path.exists(script_path):
        return False, f"Script not found: {script_name}"
    
    cmd = ["sudo", "-n", script_path]
    env = os.environ.copy()
    env["PATH"] = env.get("PATH", "/usr/bin:/bin")

    if background:
        # Run in background and redirect output to a log file
        log_file = os.path.join(GADGET_DIR, f"{script_name}.log")
        try:
            # Start process detached from this process
            with open(log_file, "w") as log:
                subprocess.Popen(
                    cmd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    cwd=GADGET_DIR,
                    env=env,
                    start_new_session=True,
                )
            return True, f"{script_name} started in background. Check {log_file} for details. Please wait 5-10 seconds, then refresh."
        except Exception as e:
            return False, f"Failed to start {script_name}: {str(e)}"

    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=GADGET_DIR,
            env=env,
        )
        output = (result.stdout or result.stderr or "").strip()
        message = output if output else f"{script_name} executed successfully"
        return True, message
    except subprocess.CalledProcessError as e:
        parts = [getattr(e, "stderr", ""), getattr(e, "stdout", "")]
        combined = "\n".join(part for part in parts if part)
        error_msg = combined.strip() if combined else str(e)

        if "a password is required" in error_msg.lower():
            hint = (
                "Passwordless sudo is required for the web UI. Add an entry such as "
                f"'__TARGET_USER__ ALL=(ALL) NOPASSWD: {script_path}' to sudoers."
            )
            error_msg = f"sudo password required for {script_name}. {hint}"
        return False, f"Error executing {script_name}: {error_msg}"
    except subprocess.TimeoutExpired:
        return False, f"Timeout executing {script_name}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"


def list_available_wavs():
    """Return selectable WAV files in USB roots excluding LockChime."""
    options = []

    for part, mount_path in iter_mounted_partitions():
        try:
            entries = os.listdir(mount_path)
        except OSError:
            continue

        for entry in entries:
            if not entry.lower().endswith(".wav"):
                continue

            if entry.lower() == LOCK_CHIME_FILENAME.lower():
                continue

            full_path = os.path.join(mount_path, entry)

            if os.path.isfile(full_path):
                relative_root = PART_LABEL_MAP.get(part, part)
                label = f"{entry} ({relative_root}: {mount_path})"
                value = f"{part}:{entry}"
                options.append({"label": label, "value": value})

    return sorted(options, key=lambda item: item["label"].lower())


def validate_lock_chime():
    """Validate the custom lock chime file against Tesla requirements."""
    issues = []
    chime_files = []

    for part, mount_path in iter_mounted_partitions():

        try:
            entries = os.listdir(mount_path)
        except OSError as exc:
            issues.append(f"Unable to read contents of {mount_path}: {exc}")
            continue

        matches = [entry for entry in entries if entry.lower() == LOCK_CHIME_FILENAME.lower()]

        for entry in matches:
            full_path = os.path.join(mount_path, entry)
            display_part = PART_LABEL_MAP.get(part, part)

            if not os.path.isfile(full_path):
                issues.append(f"{entry} on {display_part} must be a file, not a directory.")
                continue

            chime_files.append((full_path, entry, display_part))

            if entry != LOCK_CHIME_FILENAME:
                issues.append(
                    f"{entry} on {display_part} must be renamed exactly {LOCK_CHIME_FILENAME}."
                )

    if not chime_files:
        return issues

    if len(chime_files) > 1:
        partitions = ", ".join(part for _, _, part in chime_files)
        issues.append(
            f"Multiple {LOCK_CHIME_FILENAME} files detected on: {partitions}. Only one lock chime may exist across both USB drives."
        )

    for full_path, entry, part in chime_files:
        try:
            size_bytes = os.path.getsize(full_path)
        except OSError as exc:
            issues.append(f"Unable to read size of {entry} on {part}: {exc}")
            continue

        if size_bytes > MAX_LOCK_CHIME_SIZE:
            size_mb = size_bytes / (1024 * 1024)
            issues.append(
                f"{entry} on {part} is {size_mb:.2f} MiB. Tesla requires the file to be 1 MiB or smaller."
            )

        try:
            with contextlib.closing(wave.open(full_path, "rb")) as wav_file:
                wav_file.getparams()
        except (wave.Error, EOFError):
            issues.append(f"{entry} on {part} is not a valid WAV file.")
        except OSError as exc:
            issues.append(f"Unable to read {entry} on {part}: {exc}")

    return issues


def reencode_wav_for_tesla(input_path, output_path, progress_callback=None):
    """
    Re-encode a WAV file to meet Tesla's requirements using FFmpeg with multi-pass attempts:
    - Pass 1: 16-bit PCM, 48 kHz, mono (standard quality)
    - Pass 2: 16-bit PCM, 44.1 kHz, mono (reduced sample rate)
    - Pass 3: 8-bit PCM, 44.1 kHz, mono (reduced bit depth - last resort)
    
    Returns: (success, message, details_dict)
    """
    # Define encoding strategies in order of quality (best to worst)
    strategies = [
        {
            "name": "High quality (16-bit, 48kHz, mono)",
            "args": ["-acodec", "pcm_s16le", "-ar", "48000", "-ac", "1"]
        },
        {
            "name": "Medium quality (16-bit, 44.1kHz, mono)",
            "args": ["-acodec", "pcm_s16le", "-ar", "44100", "-ac", "1"]
        },
        {
            "name": "Reduced quality (8-bit, 44.1kHz, mono)",
            "args": ["-acodec", "pcm_u8", "-ar", "44100", "-ac", "1"]
        }
    ]
    
    last_error = None
    
    for attempt, strategy in enumerate(strategies, 1):
        try:
            if progress_callback:
                progress_callback(f"Attempt {attempt}/{len(strategies)}: {strategy['name']}")
            
            # Build FFmpeg command
            cmd = ["ffmpeg", "-i", input_path] + strategy["args"] + ["-y", output_path]
            
            # Use FFmpeg to re-encode
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=30,
                check=False
            )
            
            if result.returncode != 0:
                # Extract the actual error from FFmpeg output
                stderr_output = result.stderr.decode('utf-8', errors='ignore')
                
                # Check for read-only filesystem errors
                if 'read-only file system' in stderr_output.lower() or 'invalid argument' in stderr_output.lower():
                    return False, "Cannot write to filesystem (may be mounted read-only). Please ensure system is in Edit mode.", {}
                
                # FFmpeg errors typically appear after "Error" keyword or in last few lines
                error_lines = []
                for line in stderr_output.split('\n'):
                    line = line.strip()
                    if line and any(keyword in line.lower() for keyword in ['error', 'invalid', 'could not', 'failed', 'unable']):
                        error_lines.append(line)
                
                # If we found error lines, use the last few
                if error_lines:
                    error_msg = '. '.join(error_lines[-3:])[:300]
                else:
                    # Fall back to last non-empty lines
                    lines = [l.strip() for l in stderr_output.split('\n') if l.strip()]
                    error_msg = '. '.join(lines[-3:])[:300] if lines else "Unknown FFmpeg error"
                
                last_error = f"FFmpeg conversion failed: {error_msg}"
                continue  # Try next strategy
            
            # Check if output file was created and is not empty
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                last_error = "Re-encoding produced an empty file"
                continue
            
            # Check if re-encoded file is under size limit
            size_bytes = os.path.getsize(output_path)
            if size_bytes > MAX_LOCK_CHIME_SIZE:
                size_mb = size_bytes / (1024 * 1024)
                last_error = f"File still too large: {size_mb:.2f} MB (need < 1 MB)"
                
                # If not the last strategy, try next one
                if attempt < len(strategies):
                    continue
                else:
                    return False, f"Unable to compress file below 1 MB even with lowest quality settings. Final size: {size_mb:.2f} MB.", {}
            
            # Success! Return with details
            size_mb = size_bytes / (1024 * 1024)
            details = {
                "strategy": strategy["name"],
                "attempt": attempt,
                "size_mb": f"{size_mb:.2f}"
            }
            return True, f"Successfully re-encoded using {strategy['name']} (size: {size_mb:.2f} MB)", details
            
        except FileNotFoundError:
            return False, "FFmpeg is not installed on the system", {}
        except subprocess.TimeoutExpired:
            last_error = "Re-encoding timed out (file too large or complex)"
            continue
        except Exception as e:
            last_error = f"Re-encoding error: {str(e)}"
            continue
    
    # All strategies failed
    return False, f"All re-encoding attempts failed. Last error: {last_error}", {}


def validate_tesla_wav(file_path):
    """
    Validate WAV file meets Tesla's requirements:
    - Under 1MB in size
    - 16-bit PCM
    - 44.1 kHz or 48 kHz sample rate
    
    Returns: (is_valid, error_message)
    """
    try:
        # Check file size
        size_bytes = os.path.getsize(file_path)
        if size_bytes > MAX_LOCK_CHIME_SIZE:
            size_mb = size_bytes / (1024 * 1024)
            return False, f"File is {size_mb:.2f} MB. Tesla requires lock chimes to be under 1 MB."
        
        if size_bytes == 0:
            return False, "File is empty."
        
        # Check WAV format
        with contextlib.closing(wave.open(file_path, "rb")) as wav_file:
            params = wav_file.getparams()
            
            # Check sample width (16-bit = 2 bytes)
            if params.sampwidth != 2:
                bit_depth = params.sampwidth * 8
                return False, f"File is {bit_depth}-bit. Tesla requires 16-bit PCM."
            
            # Check sample rate (44100 Hz or 48000 Hz)
            if params.framerate not in (44100, 48000):
                rate_khz = params.framerate / 1000
                return False, f"Sample rate is {rate_khz:.1f} kHz. Tesla requires 44.1 kHz or 48 kHz."
            
            # Check if it's PCM (compression type should be 'NONE')
            if params.comptype != 'NONE':
                return False, f"File uses {params.comptype} compression. Tesla requires uncompressed PCM."
        
        return True, "Valid"
        
    except (wave.Error, EOFError):
        return False, "Not a valid WAV file."
    except OSError as exc:
        return False, f"Unable to read file: {exc}"


def replace_lock_chime(source_path, destination_path):
    """Swap in the selected WAV using temporary file to invalidate all caches."""
    src_size = os.path.getsize(source_path)

    if src_size == 0:
        raise ValueError("Selected WAV file is empty.")

    # Calculate MD5 hash of source file
    source_md5 = hashlib.md5()
    with open(source_path, "rb") as src_f:
        for chunk in iter(lambda: src_f.read(8192), b""):
            source_md5.update(chunk)
    source_hash = source_md5.hexdigest()

    dest_dir = os.path.dirname(destination_path)
    backup_path = os.path.join(dest_dir, "oldLockChime.wav")
    temp_path = os.path.join(dest_dir, ".LockChime.wav.tmp")

    # Drop any cached data BEFORE we start
    try:
        subprocess.run(
            ["sudo", "-n", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
            check=False,
            timeout=5
        )
    except Exception:
        pass

    # Backup existing file if present
    if os.path.isfile(destination_path):
        if os.path.isfile(backup_path):
            os.remove(backup_path)
        shutil.copyfile(destination_path, backup_path)
        
        # DELETE the old LockChime.wav completely
        os.remove(destination_path)
        
        # Sync the deletion multiple times to ensure it propagates
        subprocess.run(["sync"], check=False, timeout=5)
        time.sleep(0.5)
        subprocess.run(["sync"], check=False, timeout=5)
        time.sleep(0.5)

    try:
        # Write to a temporary file first with a different name
        # This ensures Windows never associates it with the old file
        shutil.copyfile(source_path, temp_path)
        temp_size = os.path.getsize(temp_path)
        if temp_size != src_size:
            raise IOError(
                f"Temp file size mismatch (expected {src_size} bytes, got {temp_size} bytes)."
            )
        
        # Sync the temp file completely
        with open(temp_path, "r+b") as temp_file:
            temp_file.flush()
            os.fsync(temp_file.fileno())
        
        subprocess.run(["sync"], check=False, timeout=10)
        time.sleep(0.5)
        
        # Now rename temp to final name - this creates a NEW directory entry
        # while the temp file data is already fully written
        os.rename(temp_path, destination_path)
        
        # Sync the directory metadata (the rename operation)
        try:
            dir_fd = os.open(dest_dir, os.O_RDONLY)
            os.fsync(dir_fd)
            os.close(dir_fd)
        except Exception:
            pass
        
        # Force sync of the destination file itself
        with open(destination_path, "r+b") as dest_file:
            dest_file.flush()
            os.fsync(dest_file.fileno())
        
        # Final full sync - critical for exFAT
        subprocess.run(["sync"], check=False, timeout=10)
        time.sleep(1.0)
        
        # Drop ALL caches again
        try:
            subprocess.run(
                ["sudo", "-n", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
                check=False,
                timeout=5
            )
        except Exception:
            pass
        
        # Update file access/modification times to force inode metadata change
        # This helps Tesla detect the file has changed even if size is the same
        try:
            current_time = time.time()
            os.utime(destination_path, (current_time, current_time))
            # Sync the metadata change
            subprocess.run(["sync"], check=False, timeout=5)
        except Exception:
            pass
        
        # Extra time for exFAT to settle and ensure all buffers are flushed
        time.sleep(0.5)
        
        # Verify the file contents match by comparing MD5 hashes
        dest_md5 = hashlib.md5()
        with open(destination_path, "rb") as dst_f:
            for chunk in iter(lambda: dst_f.read(8192), b""):
                dest_md5.update(chunk)
        dest_hash = dest_md5.hexdigest()
        
        if source_hash != dest_hash:
            raise IOError(
                f"File verification failed - MD5 mismatch after sync\n"
                f"Source: {source_hash}\n"
                f"Dest:   {dest_hash}"
            )
            
    except Exception:
        # Clean up temp file if it exists
        if os.path.isfile(temp_path):
            os.remove(temp_path)
        
        # Restore backup on failure
        if os.path.isfile(backup_path) and not os.path.isfile(destination_path):
            shutil.copyfile(backup_path, destination_path)
        raise

    # Clean up backup on success
    if os.path.isfile(backup_path):
        os.remove(backup_path)


@app.route("/")
def index():
    """Main page with control buttons."""
    token, label, css_class, share_paths = mode_display()
    
    # Render using template inheritance
    combined_template = HTML_TEMPLATE.replace("{% block content %}{% endblock %}", HTML_CONTROL_PAGE.replace("{% extends HTML_TEMPLATE %}", "").replace("{% block content %}", "").replace("{% endblock %}", ""))
    
    return render_template_string(
        combined_template,
        page='control',
        mode_label=label,
        mode_class=css_class,
        share_paths=share_paths,
        mode_token=token,
        auto_refresh=False,
        hostname=socket.gethostname(),
    )

@app.route("/videos")
def file_browser():
    """File browser page for TeslaCam videos."""
    token, label, css_class, share_paths = mode_display()
    teslacam_path = get_teslacam_path()
    
    if not teslacam_path:
        combined_template = HTML_TEMPLATE.replace("{% block content %}{% endblock %}", HTML_BROWSER_PAGE.replace("{% extends HTML_TEMPLATE %}", "").replace("{% block content %}", "").replace("{% endblock %}", ""))
        return render_template_string(
            combined_template,
            page='browser',
            mode_label=label,
            mode_class=css_class,
            teslacam_available=False,
            folders=[],
            videos=[],
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
    
    combined_template = HTML_TEMPLATE.replace("{% block content %}{% endblock %}", HTML_BROWSER_PAGE.replace("{% extends HTML_TEMPLATE %}", "").replace("{% block content %}", "").replace("{% endblock %}", ""))
    
    return render_template_string(
        combined_template,
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


@app.route("/videos/session/<folder>/<session>")
def view_session(folder, session):
    """View all videos from a recording session in synchronized multi-camera view."""
    token, label, css_class, share_paths = mode_display()
    teslacam_path = get_teslacam_path()
    
    if not teslacam_path:
        flash("TeslaCam path is not accessible", "error")
        return redirect(url_for("file_browser"))
    
    # Sanitize inputs
    folder = os.path.basename(folder)
    folder_path = os.path.join(teslacam_path, folder)
    
    if not os.path.isdir(folder_path):
        flash(f"Folder not found: {folder}", "error")
        return redirect(url_for("file_browser"))
    
    # Get all videos for this session
    session_videos = get_session_videos(folder_path, session)
    
    if not session_videos:
        flash(f"No videos found for session: {session}", "error")
        return redirect(url_for("file_browser", folder=folder))
    
    # Render using template inheritance
    combined_template = HTML_TEMPLATE.replace("{% block content %}{% endblock %}", 
        HTML_SESSION_PAGE.replace("{% extends HTML_TEMPLATE %}", "")
        .replace("{% block content %}", "").replace("{% endblock %}", ""))
    
    return render_template_string(
        combined_template,
        page='session',
        mode_label=label,
        mode_class=css_class,
        mode_token=token,
        folder=folder,
        session_id=session,
        videos=session_videos,
        hostname=socket.gethostname(),
    )


@app.route("/videos/stream/<folder>/<filename>")
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


@app.route("/videos/download/<folder>/<filename>")
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


@app.route("/videos/thumbnail/<folder>/<filename>")
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


@app.route("/videos/cleanup_thumbnails", methods=["POST"])
def cleanup_thumbnails():
    """Cleanup orphaned thumbnails."""
    removed = cleanup_orphaned_thumbnails()
    return jsonify({"success": True, "removed": removed})


@app.route("/videos/delete/<folder>/<filename>", methods=["POST"])
def delete_video(folder, filename):
    """Delete a single video file."""
    # Only allow deletion in edit mode
    if current_mode() != "edit":
        flash("Videos can only be deleted in Edit Mode.", "error")
        return redirect(url_for("file_browser", folder=folder))
    
    teslacam_path = get_teslacam_path()
    if not teslacam_path:
        flash("TeslaCam not accessible.", "error")
        return redirect(url_for("file_browser"))
    
    # Sanitize inputs
    folder = os.path.basename(folder)
    filename = os.path.basename(filename)
    
    video_path = os.path.join(teslacam_path, folder, filename)
    
    if not os.path.isfile(video_path):
        flash("Video not found.", "error")
        return redirect(url_for("file_browser", folder=folder))
    
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
    
    return redirect(url_for("file_browser", folder=folder))


@app.route("/videos/delete_all/<folder>", methods=["POST"])
def delete_all_videos(folder):
    """Delete all videos in a folder."""
    # Only allow deletion in edit mode
    if current_mode() != "edit":
        flash("Videos can only be deleted in Edit Mode.", "error")
        return redirect(url_for("file_browser", folder=folder))
    
    teslacam_path = get_teslacam_path()
    if not teslacam_path:
        flash("TeslaCam not accessible.", "error")
        return redirect(url_for("file_browser"))
    
    # Sanitize input
    folder = os.path.basename(folder)
    folder_path = os.path.join(teslacam_path, folder)
    
    if not os.path.isdir(folder_path):
        flash("Folder not found.", "error")
        return redirect(url_for("file_browser"))
    
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
    
    return redirect(url_for("file_browser", folder=folder))


@app.route("/present_usb", methods=["POST"])
def present_usb():
    """Switch to USB gadget presentation mode."""
    script_path = os.path.join(GADGET_DIR, "present_usb.sh")
    log_path = os.path.join(GADGET_DIR, "present_usb_web.log")
    
    try:
        # Run the script directly with sudo (script has #!/bin/bash shebang)
        with open(log_path, "w") as log:
            result = subprocess.run(
                ["sudo", "-n", script_path],
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=GADGET_DIR,
                timeout=120,  # Increased to 120s - large drives can take time for fsck and mounting
            )
            
        if result.returncode == 0:
            flash("Successfully switched to Present Mode", "success")
        else:
            flash(f"Present mode switch completed with warnings. Check {log_path} for details.", "info")
            
    except subprocess.TimeoutExpired:
        flash("Error: Script timed out after 120 seconds", "error")
    except Exception as e:
        flash(f"Error: {str(e)}", "error")
    
    return redirect(url_for("index"))


@app.route("/edit_usb", methods=["POST"])
def edit_usb():
    """Switch to edit mode with local mounts and Samba."""
    script_path = os.path.join(GADGET_DIR, "edit_usb.sh")
    log_path = os.path.join(GADGET_DIR, "edit_usb_web.log")
    
    try:
        # Run the script directly with sudo (script has #!/bin/bash shebang)
        with open(log_path, "w") as log:
            result = subprocess.run(
                ["sudo", "-n", script_path],
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=GADGET_DIR,
                timeout=120,  # Increased to 120s - unmount retries and gadget removal can take time
            )
            
        if result.returncode == 0:
            flash("Successfully switched to Edit Mode", "success")
        else:
            flash(f"Edit mode switch completed with warnings. Check {log_path} for details.", "info")
            
    except subprocess.TimeoutExpired:
        flash("Error: Script timed out after 120 seconds", "error")
    except Exception as e:
        flash(f"Error: {str(e)}", "error")
    
    return redirect(url_for("index"))


@app.route("/status")
def status():
    """Simple status endpoint for health checks."""
    token, label, css_class, share_paths = mode_display()
    return {
        "status": "running",
        "gadget_dir": GADGET_DIR,
        "mode": token,
        "mode_label": label,
        "mode_class": css_class,
        "share_paths": share_paths,
    }


@app.route("/lock_chimes")
def lock_chimes():
    """Lock chimes management page."""
    token, label, css_class, share_paths = mode_display()
    
    # Get current active chime from part2 root
    active_chime = None
    part2_mount = get_mount_path("part2")
    
    if part2_mount:
        active_chime_path = os.path.join(part2_mount, LOCK_CHIME_FILENAME)
        if os.path.isfile(active_chime_path):
            size = os.path.getsize(active_chime_path)
            mtime = int(os.path.getmtime(active_chime_path))
            active_chime = {
                "filename": LOCK_CHIME_FILENAME,
                "size": size,
                "size_str": format_file_size(size),
                "mtime": mtime,
            }
    
    # Get all WAV files from Chimes folder on part2
    chime_files = []
    if part2_mount:
        chimes_dir = os.path.join(part2_mount, CHIMES_FOLDER)
        if os.path.isdir(chimes_dir):
            try:
                entries = os.listdir(chimes_dir)
                for entry in entries:
                    if not entry.lower().endswith(".wav"):
                        continue
                    
                    full_path = os.path.join(chimes_dir, entry)
                    if os.path.isfile(full_path):
                        size = os.path.getsize(full_path)
                        mtime = int(os.path.getmtime(full_path))
                        
                        # Validate the file
                        is_valid, msg = validate_tesla_wav(full_path)
                        
                        chime_files.append({
                            "filename": entry,
                            "size": size,
                            "size_str": format_file_size(size),
                            "mtime": mtime,
                            "is_valid": is_valid,
                            "validation_msg": msg,
                        })
            except OSError:
                pass
    
    # Sort alphabetically
    chime_files.sort(key=lambda x: x["filename"].lower())
    
    combined_template = HTML_TEMPLATE.replace("{% block content %}{% endblock %}", HTML_LOCK_CHIMES_PAGE.replace("{% extends HTML_TEMPLATE %}", "").replace("{% block content %}", "").replace("{% endblock %}", ""))
    
    return render_template_string(
        combined_template,
        page='chimes',
        mode_label=label,
        mode_class=css_class,
        mode_token=token,
        active_chime=active_chime,
        chime_files=chime_files,
        auto_refresh=False,
        hostname=socket.gethostname(),
    )


@app.route("/lock_chimes/play/active")
def play_active_chime():
    """Stream the active LockChime.wav file from part2 root."""
    part2_mount = get_mount_path("part2")
    if not part2_mount:
        flash("Partition not mounted", "error")
        return redirect(url_for("lock_chimes"))
    
    file_path = os.path.join(part2_mount, LOCK_CHIME_FILENAME)
    if not os.path.isfile(file_path):
        flash("Active lock chime not found", "error")
        return redirect(url_for("lock_chimes"))
    
    return send_file(file_path, mimetype="audio/wav")


@app.route("/lock_chimes/play/<filename>")
def play_lock_chime(filename):
    """Stream a lock chime WAV file from the Chimes folder."""
    part2_mount = get_mount_path("part2")
    if not part2_mount:
        flash("Partition not mounted", "error")
        return redirect(url_for("lock_chimes"))
    
    # Sanitize filename
    filename = os.path.basename(filename)
    
    chimes_dir = os.path.join(part2_mount, CHIMES_FOLDER)
    file_path = os.path.join(chimes_dir, filename)
    
    if not os.path.isfile(file_path) or not filename.lower().endswith(".wav"):
        flash("File not found", "error")
        return redirect(url_for("lock_chimes"))
    
    return send_file(file_path, mimetype="audio/wav")


@app.route("/lock_chimes/download/<filename>")
def download_lock_chime(filename):
    """Download a lock chime WAV file from the Chimes folder."""
    part2_mount = get_mount_path("part2")
    if not part2_mount:
        flash("Partition not mounted", "error")
        return redirect(url_for("lock_chimes"))
    
    # Sanitize filename
    filename = os.path.basename(filename)
    
    chimes_dir = os.path.join(part2_mount, CHIMES_FOLDER)
    file_path = os.path.join(chimes_dir, filename)
    
    if not os.path.isfile(file_path) or not filename.lower().endswith(".wav"):
        flash("File not found", "error")
        return redirect(url_for("lock_chimes"))
    
    return send_file(file_path, mimetype="audio/wav", as_attachment=True, download_name=filename)


@app.route("/lock_chimes/upload", methods=["POST"])
def upload_lock_chime():
    """Upload a new lock chime WAV file."""
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    
    if current_mode() != "edit":
        if is_ajax:
            return jsonify({"success": False, "error": "Files can only be uploaded in Edit Mode"}), 400
        flash("Files can only be uploaded in Edit Mode", "error")
        return redirect(url_for("lock_chimes"))
    
    if "chime_file" not in request.files:
        if is_ajax:
            return jsonify({"success": False, "error": "No file selected"}), 400
        flash("No file selected", "error")
        return redirect(url_for("lock_chimes"))
    
    file = request.files["chime_file"]
    if file.filename == "":
        if is_ajax:
            return jsonify({"success": False, "error": "No file selected"}), 400
        flash("No file selected", "error")
        return redirect(url_for("lock_chimes"))
    
    if not file.filename.lower().endswith(".wav"):
        if is_ajax:
            return jsonify({"success": False, "error": "Only WAV files are allowed"}), 400
        flash("Only WAV files are allowed", "error")
        return redirect(url_for("lock_chimes"))
    
    filename = os.path.basename(file.filename)
    
    # Prevent uploading a file named LockChime.wav
    if filename.lower() == LOCK_CHIME_FILENAME.lower():
        if is_ajax:
            return jsonify({"success": False, "error": "Cannot upload a file named LockChime.wav. Please rename your file."}), 400
        flash("Cannot upload a file named LockChime.wav. Please rename your file.", "error")
        return redirect(url_for("lock_chimes"))
    
    # Get part2 mount path
    part2_mount = get_mount_path("part2")
    if not part2_mount:
        if is_ajax:
            return jsonify({"success": False, "error": "part2 not mounted"}), 500
        flash("part2 not mounted", "error")
        return redirect(url_for("lock_chimes"))
    
    # Verify mount is writable by checking if it's the read-only path
    if part2_mount.endswith("-ro"):
        if is_ajax:
            return jsonify({"success": False, "error": "System is in Present mode with read-only access. Switch to Edit mode to upload files."}), 400
        flash("System is in Present mode with read-only access. Switch to Edit mode to upload files.", "error")
        return redirect(url_for("lock_chimes"))
    
    # Save to Chimes folder
    chimes_dir = os.path.join(part2_mount, CHIMES_FOLDER)
    if not os.path.isdir(chimes_dir):
        try:
            os.makedirs(chimes_dir, exist_ok=True)
        except OSError as e:
            if is_ajax:
                return jsonify({"success": False, "error": f"Cannot create Chimes directory (filesystem may be read-only): {str(e)}"}), 500
            flash(f"Cannot create Chimes directory (filesystem may be read-only): {str(e)}", "error")
            return redirect(url_for("lock_chimes"))
    
    dest_path = os.path.join(chimes_dir, filename)
    
    try:
        # Save to temporary location first (use underscore to avoid multiple dots issue on FAT/exFAT)
        temp_path = dest_path.replace('.wav', '_upload.wav')
        file.save(temp_path)
        
        # Validate the uploaded file
        is_valid, validation_msg = validate_tesla_wav(temp_path)
        
        if not is_valid:
            # Try to re-encode the file to meet Tesla's requirements (use simple temp name with .wav extension)
            reencoded_path = dest_path.replace('.wav', '_reenc.wav')
            
            # Track progress messages (for AJAX, we return these in the response)
            progress_messages = []
            def progress_callback(msg):
                progress_messages.append(msg)
            
            success, reencode_msg, details = reencode_wav_for_tesla(temp_path, reencoded_path, progress_callback)
            
            if success:
                # Re-encoding successful, validate the re-encoded file
                is_valid_reencoded, validation_msg_reencoded = validate_tesla_wav(reencoded_path)
                
                if is_valid_reencoded:
                    # Use the re-encoded file
                    os.remove(temp_path)
                    temp_path = reencoded_path
                    
                    # Build user-friendly message
                    strategy_desc = details.get('strategy', 'Tesla-compatible format')
                    size_info = details.get('size_mb', 'under 1 MB')
                    
                    if is_ajax:
                        return jsonify({
                            "success": True, 
                            "message": f"Uploaded {filename} successfully!\n\nFile was automatically re-encoded to {strategy_desc} with final size of {size_info} MB.",
                            "reencoded": True,
                            "details": details,
                            "progress": progress_messages
                        }), 200
                else:
                    # Re-encoded file still doesn't meet requirements
                    os.remove(temp_path)
                    if os.path.exists(reencoded_path):
                        os.remove(reencoded_path)
                    if is_ajax:
                        return jsonify({"success": False, "error": f"Re-encoded file failed validation: {validation_msg_reencoded}"}), 400
                    flash(f"Re-encoded file failed validation: {validation_msg_reencoded}", "error")
                    return redirect(url_for("lock_chimes"))
            else:
                # Re-encoding failed
                os.remove(temp_path)
                if os.path.exists(reencoded_path):
                    os.remove(reencoded_path)
                if is_ajax:
                    return jsonify({
                        "success": False, 
                        "error": f"Invalid WAV file: {validation_msg}.\n\nRe-encoding failed: {reencode_msg}",
                        "progress": progress_messages
                    }), 400
                flash(f"Invalid WAV file: {validation_msg}. Re-encoding failed: {reencode_msg}", "error")
                return redirect(url_for("lock_chimes"))
        
        # Move to final location
        if os.path.exists(dest_path):
            os.remove(dest_path)
        os.rename(temp_path, dest_path)
        
        # Sync to ensure file is written
        subprocess.run(["sync"], check=False, timeout=5)
        
        # Force Samba to see the new file
        close_samba_share("part2")
        restart_samba_services()
        
        if is_ajax:
            return jsonify({"success": True, "message": f"Uploaded {filename} successfully"}), 200
        flash(f"Uploaded {filename} successfully", "success")
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        reencoded_path = dest_path + ".reencoded.tmp"
        if os.path.exists(reencoded_path):
            os.remove(reencoded_path)
        if is_ajax:
            return jsonify({"success": False, "error": f"Failed to upload file: {str(e)}"}), 500
        flash(f"Failed to upload file: {str(e)}", "error")
    
    return redirect(url_for("lock_chimes"))


@app.route("/lock_chimes/set/<filename>", methods=["POST"])
def set_as_chime(filename):
    """Set a WAV file from Chimes folder as the active lock chime."""
    if current_mode() != "edit":
        flash("Lock chime can only be updated in Edit Mode", "error")
        return redirect(url_for("lock_chimes"))
    
    # Sanitize filename
    filename = os.path.basename(filename)
    
    part2_mount = get_mount_path("part2")
    if not part2_mount:
        flash("Partition not mounted", "error")
        return redirect(url_for("lock_chimes"))
    
    chimes_dir = os.path.join(part2_mount, CHIMES_FOLDER)
    source_path = os.path.join(chimes_dir, filename)
    
    if not os.path.isfile(source_path):
        flash("Source file not found in Chimes folder", "error")
        return redirect(url_for("lock_chimes"))
    
    # Validate before setting
    is_valid, validation_msg = validate_tesla_wav(source_path)
    if not is_valid:
        flash(f"Cannot set as chime: {validation_msg}", "error")
        return redirect(url_for("lock_chimes"))
    
    target_path = os.path.join(part2_mount, LOCK_CHIME_FILENAME)
    
    close_samba_share("part2")
    
    try:
        replace_lock_chime(source_path, target_path)
        
        # Additional sync after all operations
        subprocess.run(["sync"], check=False, timeout=10)
        time.sleep(2)
        
        # Drop caches one more time to ensure Samba/web sees fresh data
        try:
            subprocess.run(
                ["sudo", "-n", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
                check=False,
                timeout=5
            )
        except Exception:
            pass
        
        restart_samba_services()
        time.sleep(3)
        close_samba_share("part2")
        
        flash(f"Set {filename} as active lock chime", "success")
    except Exception as e:
        flash(f"Failed to set lock chime: {str(e)}", "error")
    
    # Add timestamp to force browser cache refresh
    return redirect(url_for("lock_chimes", _=int(time.time())))


@app.route("/lock_chimes/delete/<filename>", methods=["POST"])
def delete_lock_chime(filename):
    """Delete a lock chime file from Chimes folder."""
    if current_mode() != "edit":
        flash("Files can only be deleted in Edit Mode", "error")
        return redirect(url_for("lock_chimes"))
    
    # Sanitize filename
    filename = os.path.basename(filename)
    
    part2_mount = get_mount_path("part2")
    if not part2_mount:
        flash("Partition not mounted", "error")
        return redirect(url_for("lock_chimes"))
    
    chimes_dir = os.path.join(part2_mount, CHIMES_FOLDER)
    file_path = os.path.join(chimes_dir, filename)
    
    if not os.path.isfile(file_path):
        flash("File not found", "error")
        return redirect(url_for("lock_chimes"))
    
    try:
        os.remove(file_path)
        flash(f"Deleted {filename}", "success")
    except Exception as e:
        flash(f"Failed to delete file: {str(e)}", "error")
    
    return redirect(url_for("lock_chimes"))


@app.route("/light_shows")
def light_shows():
    """Light shows management page."""
    token, label, css_class, share_paths = mode_display()
    
    # Get all fseq, mp3, and wav files from LightShow folders
    files_dict = {}  # Group files by base name
    for part, mount_path in iter_all_partitions():
        lightshow_dir = os.path.join(mount_path, "LightShow")
        if not os.path.isdir(lightshow_dir):
            continue
        
        try:
            entries = os.listdir(lightshow_dir)
        except OSError:
            continue
        
        for entry in entries:
            lower_entry = entry.lower()
            if not (lower_entry.endswith(".fseq") or lower_entry.endswith(".mp3") or lower_entry.endswith(".wav")):
                continue
            
            full_path = os.path.join(lightshow_dir, entry)
            if os.path.isfile(full_path):
                # Get base name without extension
                base_name = os.path.splitext(entry)[0]
                
                if base_name not in files_dict:
                    files_dict[base_name] = {
                        "base_name": base_name,
                        "fseq_file": None,
                        "audio_file": None,
                        "partition_key": part,
                        "partition": PART_LABEL_MAP.get(part, part),
                    }
                
                size = os.path.getsize(full_path)
                if lower_entry.endswith(".fseq"):
                    files_dict[base_name]["fseq_file"] = {
                        "filename": entry,
                        "size": size,
                        "size_str": format_file_size(size),
                    }
                elif lower_entry.endswith(".mp3") or lower_entry.endswith(".wav"):
                    files_dict[base_name]["audio_file"] = {
                        "filename": entry,
                        "size": size,
                        "size_str": format_file_size(size),
                    }
    
    # Convert to list and sort by base name
    show_groups = list(files_dict.values())
    show_groups.sort(key=lambda x: x["base_name"].lower())
    
    combined_template = HTML_TEMPLATE.replace("{% block content %}{% endblock %}", HTML_LIGHT_SHOWS_PAGE.replace("{% extends HTML_TEMPLATE %}", "").replace("{% block content %}", "").replace("{% endblock %}", ""))
    
    return render_template_string(
        combined_template,
        page='shows',
        mode_label=label,
        mode_class=css_class,
        mode_token=token,
        show_groups=show_groups,
        auto_refresh=False,
        hostname=socket.gethostname(),
    )


@app.route("/light_shows/play/<partition>/<filename>")
def play_light_show_audio(partition, filename):
    """Stream a light show audio file."""
    if partition not in USB_PARTITIONS:
        flash("Invalid partition", "error")
        return redirect(url_for("light_shows"))
    
    mount_path = get_mount_path(partition)
    if not mount_path:
        flash("Partition not mounted", "error")
        return redirect(url_for("light_shows"))
    
    lightshow_dir = os.path.join(mount_path, "LightShow")
    file_path = os.path.join(lightshow_dir, filename)
    
    lower_filename = filename.lower()
    if not os.path.isfile(file_path) or not (lower_filename.endswith(".mp3") or lower_filename.endswith(".wav")):
        flash("File not found", "error")
        return redirect(url_for("light_shows"))
    
    # Determine MIME type based on file extension
    if lower_filename.endswith(".wav"):
        mimetype = "audio/wav"
    else:
        mimetype = "audio/mpeg"
    
    return send_file(file_path, mimetype=mimetype)


@app.route("/light_shows/upload", methods=["POST"])
def upload_light_show():
    """Upload a new light show file."""
    if current_mode() != "edit":
        flash("Files can only be uploaded in Edit Mode", "error")
        return redirect(url_for("light_shows"))
    
    if "show_file" not in request.files:
        flash("No file selected", "error")
        return redirect(url_for("light_shows"))
    
    file = request.files["show_file"]
    if file.filename == "":
        flash("No file selected", "error")
        return redirect(url_for("light_shows"))
    
    lower_filename = file.filename.lower()
    if not (lower_filename.endswith(".fseq") or lower_filename.endswith(".mp3") or lower_filename.endswith(".wav")):
        flash("Only fseq, mp3, and wav files are allowed", "error")
        return redirect(url_for("light_shows"))
    
    # Save to part2 LightShow folder
    mount_path = get_mount_path("part2")
    if not mount_path:
        flash("part2 not mounted", "error")
        return redirect(url_for("light_shows"))
    
    lightshow_dir = os.path.join(mount_path, "LightShow")
    os.makedirs(lightshow_dir, exist_ok=True)
    
    filename = os.path.basename(file.filename)
    dest_path = os.path.join(lightshow_dir, filename)
    
    try:
        file.save(dest_path)
        flash(f"Uploaded {filename} successfully", "success")
    except Exception as e:
        flash(f"Failed to upload file: {str(e)}", "error")
    
    return redirect(url_for("light_shows"))


@app.route("/light_shows/delete/<partition>/<base_name>", methods=["POST"])
def delete_light_show(partition, base_name):
    """Delete both fseq and mp3 files for a light show."""
    if current_mode() != "edit":
        flash("Files can only be deleted in Edit Mode", "error")
        return redirect(url_for("light_shows"))
    
    if partition not in USB_PARTITIONS:
        flash("Invalid partition", "error")
        return redirect(url_for("light_shows"))
    
    mount_path = get_mount_path(partition)
    if not mount_path:
        flash("Partition not mounted", "error")
        return redirect(url_for("light_shows"))
    
    lightshow_dir = os.path.join(mount_path, "LightShow")
    
    # Try to delete fseq, mp3, and wav files
    deleted_files = []
    errors = []
    
    for ext in [".fseq", ".mp3", ".wav"]:
        filename = base_name + ext
        file_path = os.path.join(lightshow_dir, filename)
        
        if os.path.isfile(file_path):
            try:
                os.remove(file_path)
                deleted_files.append(filename)
            except Exception as e:
                errors.append(f"{filename}: {str(e)}")
    
    if deleted_files:
        flash(f"Deleted {', '.join(deleted_files)}", "success")
    
    if errors:
        flash(f"Errors: {'; '.join(errors)}", "error")
    
    if not deleted_files and not errors:
        flash("No files found to delete", "error")
    
    return redirect(url_for("light_shows"))


if __name__ == "__main__":
    print(f"Starting Tesla USB Gadget Web Control")
    print(f"Gadget directory: {GADGET_DIR}")
    print(f"Access the interface at: http://0.0.0.0:__WEB_PORT__/")
    app.run(host="0.0.0.0", port=__WEB_PORT__, debug=False, threaded=True)
