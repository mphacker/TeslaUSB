#!/usr/bin/env python3
"""
Video service for TeslaUSB web control interface.

This module handles TeslaCam video file discovery, metadata extraction,
and event grouping. It provides mode-aware path resolution for accessing
video files in both present (read-only) and edit (read-write) modes.

Supports Tesla's event-based folder structure where each event has its own
subfolder containing multiple camera angle videos, event.json, thumb.png, etc.
"""

import os
import json
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


def get_events(folder_path):
    """
    Get all Tesla events (event-based folder structure) from a TeslaCam folder.

    Args:
        folder_path: Path to a TeslaCam folder (e.g., SavedClips, SentryClips)

    Returns:
        list: List of event dictionaries with metadata, sorted by timestamp (newest first)

    Each event is a subfolder containing:
    - Multiple camera angle videos (front, back, left_repeater, right_repeater, left_pillar, right_pillar)
    - event.json with metadata (timestamp, city, reason, camera, etc.)
    - thumb.png thumbnail
    - event.mp4 grid view video (optional)
    """
    events = []

    try:
        for entry in os.scandir(folder_path):
            if entry.is_dir():
                event_data = _parse_event_folder(entry.path, entry.name)
                if event_data:
                    events.append(event_data)
    except OSError:
        pass

    # Sort by timestamp, newest first
    events.sort(key=lambda x: x['timestamp'], reverse=True)
    return events


def _parse_event_folder(event_path, event_name):
    """
    Parse a Tesla event folder and extract metadata.

    Args:
        event_path: Full path to the event folder
        event_name: Name of the event folder (e.g., "2025-11-27_20-42-09")

    Returns:
        dict: Event metadata or None if not a valid event folder
    """
    try:
        # Check for event.json
        event_json_path = os.path.join(event_path, 'event.json')
        event_metadata = {}

        if os.path.exists(event_json_path):
            try:
                with open(event_json_path, 'r') as f:
                    event_metadata = json.load(f)
            except (OSError, json.JSONDecodeError):
                pass

        # Check for thumb.png
        thumb_path = os.path.join(event_path, 'thumb.png')
        has_thumbnail = os.path.exists(thumb_path)

        # Scan for video files and categorize by camera angle
        camera_videos = {
            'front': None,
            'back': None,
            'left_repeater': None,
            'right_repeater': None,
            'left_pillar': None,
            'right_pillar': None,
            'event': None  # Grid view video
        }

        total_size = 0
        latest_timestamp = 0

        for entry in os.scandir(event_path):
            if entry.is_file() and entry.name.lower().endswith(VIDEO_EXTENSIONS):
                try:
                    stat_info = entry.stat()
                    total_size += stat_info.st_size
                    latest_timestamp = max(latest_timestamp, stat_info.st_mtime)

                    # Categorize video by camera angle
                    name_lower = entry.name.lower()
                    if 'front' in name_lower:
                        camera_videos['front'] = entry.name
                    elif 'back' in name_lower:
                        camera_videos['back'] = entry.name
                    elif 'left_repeater' in name_lower:
                        camera_videos['left_repeater'] = entry.name
                    elif 'right_repeater' in name_lower:
                        camera_videos['right_repeater'] = entry.name
                    elif 'left_pillar' in name_lower:
                        camera_videos['left_pillar'] = entry.name
                    elif 'right_pillar' in name_lower:
                        camera_videos['right_pillar'] = entry.name
                    elif name_lower == 'event.mp4':
                        camera_videos['event'] = entry.name
                except OSError:
                    continue

        # If no videos found, not a valid event
        if not any(camera_videos.values()):
            return None

        # Parse timestamp from event name (format: YYYY-MM-DD_HH-MM-SS)
        event_timestamp = None
        try:
            dt = datetime.strptime(event_name, '%Y-%m-%d_%H-%M-%S')
            event_timestamp = dt.timestamp()
        except ValueError:
            # Fall back to latest file timestamp
            event_timestamp = latest_timestamp if latest_timestamp > 0 else 0

        return {
            'name': event_name,
            'path': event_path,
            'timestamp': event_timestamp,
            'datetime': datetime.fromtimestamp(event_timestamp).strftime('%Y-%m-%d %I:%M:%S %p'),
            'size': total_size,
            'size_mb': round(total_size / (1024 * 1024), 2),
            'has_thumbnail': has_thumbnail,
            'camera_videos': camera_videos,
            'metadata': event_metadata,
            'city': event_metadata.get('city', ''),
            'reason': event_metadata.get('reason', ''),
        }
    except OSError:
        return None


def get_event_details(folder_path, event_name):
    """
    Get detailed information about a specific event.

    Args:
        folder_path: Path to the TeslaCam folder
        event_name: Name of the event folder

    Returns:
        dict: Event details or None if not found
    """
    event_path = os.path.join(folder_path, event_name)
    if not os.path.isdir(event_path):
        return None

    return _parse_event_folder(event_path, event_name)


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
