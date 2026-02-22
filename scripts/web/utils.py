#!/usr/bin/env python3
"""
Utility functions for TeslaUSB web control interface.

This module contains pure helper functions that don't depend on Flask
or global application state. These functions are used throughout the
web_control.py application.
"""

import os
import hashlib
import re
import socket


def get_base_context():
    """
    Return common template context variables for all pages.

    Returns:
        dict: Context with mode_token, mode_label, mode_class, share_paths, hostname
    """
    # Import here to avoid circular imports
    from services.mode_service import mode_display
    from services.partition_service import get_feature_availability

    token, label, css_class, share_paths = mode_display()
    return {
        'mode_token': token,
        'mode_label': label,
        'mode_class': css_class,
        'share_paths': share_paths,
        'hostname': socket.gethostname(),
        **get_feature_availability(),
    }


def format_file_size(size_bytes):
    """Format file size in human-readable format."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"


def parse_session_from_filename(filename):
    """
    Parse Tesla video filename to extract session and camera info.
    Format: 2025-10-29_10-39-36-right_pillar.mp4
    Returns: {'session': '2025-10-29_10-39-36', 'camera': 'right_pillar'}
    """
    # Match pattern: YYYY-MM-DD_HH-MM-SS-camera.ext
    pattern = r'^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-(.+)\.\w+$'
    match = re.match(pattern, filename)
    if match:
        return {
            'session': match.group(1),
            'camera': match.group(2)
        }
    return None


def generate_thumbnail_hash(video_path):
    """Generate a unique hash for a video file based on path and modification time."""
    try:
        stat_info = os.stat(video_path)
        unique_string = f"{video_path}_{stat_info.st_mtime}_{stat_info.st_size}"
        return hashlib.md5(unique_string.encode()).hexdigest()
    except OSError:
        return None
