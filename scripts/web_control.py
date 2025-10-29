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
# Configuration (will be updated by setup-usb.sh)
app.secret_key = "__SECRET_KEY__"
GADGET_DIR = "__GADGET_DIR__"
MNT_DIR = "__MNT_DIR__"
RO_MNT_DIR = "/mnt/gadget"  # Read-only mount directory for present mode
STATE_FILE = os.path.join(GADGET_DIR, "state.txt")
LOCK_CHIME_FILENAME = "LockChime.wav"
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
                        'modified': datetime.fromtimestamp(stat_info.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
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
        }
        .container {
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            margin-bottom: 20px;
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
            max-height: 600px;
            overflow-y: auto;
            border: 1px solid #dee2e6;
            border-radius: 4px;
            margin: 20px 0;
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
        @media (max-width: 768px) {
            .navbar-content {
                flex-direction: column;
                align-items: flex-start;
            }
            .nav-links {
                margin-top: 10px;
                flex-wrap: wrap;
            }
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
            .video-table th:nth-child(1),
            .video-table td:nth-child(1),
            .video-table th:nth-child(4),
            .video-table td:nth-child(4) {
                display: none;
            }
        }
    </style>
</head>
<body>
    <div class="navbar">
        <div class="navbar-content">
            <h1><a href="{{ url_for('index') }}">🚗 Tesla USB Gadget Control</a></h1>
            <div class="nav-links">
                <a href="{{ url_for('index') }}" {% if page == 'control' %}class="active"{% endif %}>Control</a>
                <a href="{{ url_for('file_browser') }}" {% if page == 'browser' %}class="active"{% endif %}>Videos</a>
            </div>
        </div>
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

    {% if show_lock_chime %}
    <div class="lock-chime">
        <h2>Custom Lock Chime</h2>
        {% if not lock_chime_ready %}
        <p>Switch to Edit Mode to manage the custom lock chime.</p>
        {% elif wav_options %}
        <form method="post" action="{{ url_for('set_chime') }}">
            <label for="selected_wav">Choose a WAV file to use as LockChime:</label>
            <select name="selected_wav" id="selected_wav" required>
                {% for option in wav_options %}
                <option value="{{ option.value }}">{{ option.label }}</option>
                {% endfor %}
            </select>
            <button type="submit" class="set-chime-btn">🔔 Set Chime</button>
        </form>
        {% else %}
        <p>No additional WAV files found in the root of gadget_part1 or gadget_part2.</p>
        {% endif %}
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
        {% if mode_token == 'edit' and videos %}
        <div class="delete-all-container">
            <form method="post" action="{{ url_for('delete_all_videos', folder=current_folder) }}" 
                  onsubmit="return confirm('Are you sure you want to delete ALL {{ videos|length }} videos in {{ current_folder }}? This cannot be undone!');" 
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

<script>
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
    </div>
    
    <div class="session-grid tesla-layout">
        {% for video in videos %}
        <div class="session-video-container tesla-{{ video.camera|replace('_', '_')|lower }}">
            <video id="video-{{ loop.index0 }}" controls preload="metadata"
                   src="{{ url_for('stream_video', folder=folder, filename=video.name) }}">
                Your browser does not support the video tag.
            </video>
            <div class="session-video-label" onclick="openFullVideo('{{ video.name }}')">
                📹 {{ video.camera|replace('_', ' ')|title }}
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

// Play all videos
function playAll() {
    syncAll(); // Sync before playing
    videos.forEach(video => {
        video.play().catch(e => console.log('Play failed:', e));
    });
}

// Pause all videos
function pauseAll() {
    videos.forEach(video => video.pause());
}

// Seek all videos to a specific time
function seekAll(time) {
    videos.forEach(video => {
        video.currentTime = time;
    });
}

// Automatically sync videos when the first one plays
if (videos.length > 0) {
    videos[0].addEventListener('play', () => {
        const masterTime = videos[0].currentTime;
        videos.forEach((video, index) => {
            if (index !== 0 && video.paused) {
                video.currentTime = masterTime;
                video.play().catch(e => console.log('Auto-play failed:', e));
            }
        });
    });
    
    videos[0].addEventListener('pause', () => {
        videos.forEach((video, index) => {
            if (index !== 0) {
                video.pause();
            }
        });
    });
    
    videos[0].addEventListener('seeked', () => {
        const masterTime = videos[0].currentTime;
        videos.forEach((video, index) => {
            if (index !== 0) {
                video.currentTime = masterTime;
            }
        });
    });
}

// Periodically re-sync videos during playback to handle drift
let syncInterval;
videos.forEach((video, index) => {
    video.addEventListener('playing', () => {
        if (index === 0 && !syncInterval) {
            syncInterval = setInterval(() => {
                const masterTime = videos[0].currentTime;
                videos.forEach((v, i) => {
                    if (i !== 0 && !v.paused) {
                        const diff = Math.abs(v.currentTime - masterTime);
                        // Re-sync if drift is more than 0.3 seconds
                        if (diff > 0.3) {
                            v.currentTime = masterTime;
                        }
                    }
                });
            }, 1000); // Check every second
        }
    });
    
    video.addEventListener('pause', () => {
        if (index === 0 && syncInterval) {
            clearInterval(syncInterval);
            syncInterval = null;
        }
    });
});
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


def replace_lock_chime(source_path, destination_path):
    """Swap in the selected WAV using temporary file to invalidate all caches."""
    src_size = os.path.getsize(source_path)

    if src_size == 0:
        raise ValueError("Selected WAV file is empty.")

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
        
        # Sync the deletion and wait
        subprocess.run(["sync"], check=False, timeout=5)
        time.sleep(0.3)

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
        time.sleep(0.2)
        
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
        
        # Final full sync
        subprocess.run(["sync"], check=False, timeout=10)
        
        # Drop ALL caches again
        try:
            subprocess.run(
                ["sudo", "-n", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
                check=False,
                timeout=5
            )
        except Exception:
            pass
        
        # Extra time for exFAT to settle
        time.sleep(0.2)
            
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
    lock_chime_ready = lock_chime_ui_available(token)
    show_lock_chime = token != "present"
    wav_options = list_available_wavs() if lock_chime_ready else []
    
    # Render using template inheritance
    combined_template = HTML_TEMPLATE.replace("{% block content %}{% endblock %}", HTML_CONTROL_PAGE.replace("{% extends HTML_TEMPLATE %}", "").replace("{% block content %}", "").replace("{% endblock %}", ""))
    
    return render_template_string(
        combined_template,
        page='control',
        mode_label=label,
        mode_class=css_class,
        share_paths=share_paths,
        mode_token=token,
        lock_chime_ready=lock_chime_ready,
        show_lock_chime=show_lock_chime,
        wav_options=wav_options,
        auto_refresh=False,
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
            current_folder=None
        )
    
    folders = get_teslacam_folders()
    current_folder = request.args.get('folder', folders[0]['name'] if folders else None)
    videos = []
    
    if current_folder:
        folder_path = os.path.join(teslacam_path, current_folder)
        if os.path.isdir(folder_path):
            videos = get_video_files(folder_path)
    
    combined_template = HTML_TEMPLATE.replace("{% block content %}{% endblock %}", HTML_BROWSER_PAGE.replace("{% extends HTML_TEMPLATE %}", "").replace("{% block content %}", "").replace("{% endblock %}", ""))
    
    return render_template_string(
        combined_template,
        page='browser',
        mode_label=label,
        mode_class=css_class,
        mode_token=token,
        teslacam_available=True,
        folders=folders,
        videos=videos,
        current_folder=current_folder
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
        videos=session_videos
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
                timeout=30,
            )
            
        if result.returncode == 0:
            flash("Successfully switched to Present Mode", "success")
        else:
            flash(f"Present mode switch completed with warnings. Check {log_path} for details.", "info")
            
    except subprocess.TimeoutExpired:
        flash("Error: Script timed out after 30 seconds", "error")
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
                timeout=30,
            )
            
        if result.returncode == 0:
            flash("Successfully switched to Edit Mode", "success")
        else:
            flash(f"Edit mode switch completed with warnings. Check {log_path} for details.", "info")
            
    except subprocess.TimeoutExpired:
        flash("Error: Script timed out after 30 seconds", "error")
    except Exception as e:
        flash(f"Error: {str(e)}", "error")
    
    return redirect(url_for("index"))


@app.route("/set_chime", methods=["POST"])
def set_chime():
    """Replace LockChime.wav with a selected WAV file while in edit mode."""
    if current_mode() != "edit":
        flash("Custom lock chime can only be updated while in Edit Mode.", "error")
        return redirect(url_for("index"))

    selection = request.form.get("selected_wav", "").strip()

    if not selection:
        flash("Select a WAV file to set as the lock chime.", "error")
        return redirect(url_for("index"))

    if ":" not in selection:
        flash("Invalid selection for lock chime.", "error")
        return redirect(url_for("index"))

    part, filename = selection.split(":", 1)
    part = part.strip()
    filename = os.path.basename(filename.strip())

    if part not in USB_PARTITIONS or not filename:
        flash("Invalid partition or filename for lock chime selection.", "error")
        return redirect(url_for("index"))

    if not filename.lower().endswith(".wav"):
        flash("Selected file must use the .wav extension.", "error")
        return redirect(url_for("index"))

    source_dir = os.path.join(MNT_DIR, part)
    source_path = os.path.join(source_dir, filename)

    if not os.path.isfile(source_path):
        flash("Selected WAV file is no longer available.", "error")
        return redirect(url_for("index"))

    # Close Samba share BEFORE making changes
    close_samba_share(part)

    target_part = part
    target_dir = source_dir
    target_path = os.path.join(target_dir, LOCK_CHIME_FILENAME)

    # Also try to break any oplocks on the specific file
    try:
        subprocess.run(
            ["sudo", "-n", "smbcontrol", "all", "close-denied", target_path],
            check=False,
            timeout=5
        )
    except Exception:
        pass

    try:
        replace_lock_chime(source_path, target_path)
    except Exception as exc:
        flash(f"Unable to set custom lock chime: {exc}", "error")
        return redirect(url_for("index"))

    removed_duplicates = remove_other_lock_chimes(target_part)

    # Multiple sync strategies for exFAT
    try:
        subprocess.run(["sync"], check=True, timeout=10)
        # Force filesystem-specific sync
        subprocess.run(["sync", "-f", target_dir], check=False, timeout=10)
    except Exception:
        pass

    # Give filesystem time to settle after all operations
    time.sleep(1)

    # Restart Samba to clear ALL caches and oplocks
    restart_samba_services()
    
    # Give Samba more time to fully restart and clear state
    time.sleep(2)
    
    # Close share again after restart to force reconnection
    close_samba_share(target_part)

    if removed_duplicates:
        duplicate_list = ", ".join(removed_duplicates)
        flash(
            f"Removed stale LockChime.wav copies from: {duplicate_list} to maintain a single active chime.",
            "info",
        )

    # Verify the file was actually updated by checking its size
    try:
        final_size = os.path.getsize(target_path)
        expected_size = os.path.getsize(source_path)
        if final_size != expected_size:
            flash(
                f"Warning: File sizes don't match after copy (expected {expected_size}, got {final_size}). "
                "The file may not have been properly written to the exFAT filesystem.",
                "error"
            )
    except Exception:
        pass

    flash(
        f"Custom lock chime updated successfully using '{filename}' on {PART_LABEL_MAP.get(target_part, target_part)}. "
        "If your Windows SMB connection still shows old data, disconnect and reconnect to the share.",
        "success",
    )

    for issue in validate_lock_chime():
        flash(issue, "error")

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


if __name__ == "__main__":
    print(f"Starting Tesla USB Gadget Web Control")
    print(f"Gadget directory: {GADGET_DIR}")
    print(f"Access the interface at: http://0.0.0.0:__WEB_PORT__/")
    app.run(host="0.0.0.0", port=__WEB_PORT__, debug=False, threaded=True)
