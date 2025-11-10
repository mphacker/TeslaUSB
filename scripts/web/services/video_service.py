#!/usr/bin/env python3
"""
Video service for TeslaUSB web control interface.

This module handles TeslaCam video file discovery, metadata extraction,
and session grouping. It provides mode-aware path resolution for accessing
video files in both present (read-only) and edit (read-write) modes.
"""

import os
from datetime import datetime

# Import configuration
from config import (
    MNT_DIR,
    RO_MNT_DIR,
    VIDEO_EXTENSIONS,
)

# Import other services
from services.mode_service import current_mode

# Import utility functions
from utils import parse_session_from_filename


def get_teslacam_path():
    """
    Get the TeslaCam path based on current mode.
    
    Returns:
        str: Path to TeslaCam directory, or None if not accessible
        
    In present mode, returns the read-only mount path.
    In edit mode, returns the read-write mount path.
    """
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
    """
    Get all video files from a folder with metadata.
    
    Args:
        folder_path: Path to the folder containing videos
        
    Returns:
        list: List of video file dictionaries with metadata (name, size, timestamp, session, camera)
        
    The returned videos are sorted by modification time (newest first).
    Each video includes parsed session and camera information from the filename.
    """
    videos = []
    
    try:
        for entry in os.scandir(folder_path):
            if entry.is_file() and entry.name.lower().endswith(VIDEO_EXTENSIONS):
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


def get_session_videos(folder_path, session_id):
    """
    Get all videos from a specific session.
    
    Args:
        folder_path: Path to the folder containing videos
        session_id: Session identifier (e.g., "2025-11-08_08-15-44")
        
    Returns:
        list: List of video file dictionaries from the specified session,
              sorted by camera name for consistent ordering
    """
    all_videos = get_video_files(folder_path)
    session_videos = [v for v in all_videos if v['session'] == session_id]
    # Sort by camera name for consistent ordering
    session_videos.sort(key=lambda x: x['camera'] or '')
    return session_videos


def get_teslacam_folders():
    """
    Get available TeslaCam subfolders.
    
    Returns:
        list: List of folder dictionaries with name and path,
              sorted alphabetically by name
              
    Common folders include:
    - RecentClips: Last hour of recordings
    - SavedClips: Manually saved clips (honk)
    - SentryClips: Sentry mode recordings
    """
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
