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
import av
from datetime import datetime
from PIL import Image
import logging
from threading import Semaphore

# Import configuration
from config import (
    MNT_DIR,
    RO_MNT_DIR,
    VIDEO_EXTENSIONS,
    THUMBNAIL_CACHE_DIR,
)

logger = logging.getLogger(__name__)

# Semaphore to limit concurrent thumbnail generation
# Pi Zero 2 W has 4 cores but limited RAM/thermal headroom
# Limit to 1 concurrent generation to prevent CPU starvation for other requests
thumbnail_semaphore = Semaphore(1)

# Import other services
from services.mode_service import current_mode

# Import utility functions
from utils import parse_session_from_filename


# MP4 magic bytes: ftyp box signature
MP4_FTYP_SIGNATURE = b'ftyp'


def is_valid_mp4(filepath):
    """
    Check if a file has valid MP4 headers (not encrypted by Tesla).

    Tesla encrypts some camera angles in RecentClips until they're saved.
    This function checks for the 'ftyp' box which is required for valid MP4.

    Args:
        filepath: Path to the video file

    Returns:
        bool: True if file has valid MP4 headers, False if encrypted/corrupt
    """
    try:
        with open(filepath, 'rb') as f:
            # Read first 12 bytes - ftyp box is typically at offset 4-7
            header = f.read(12)
            if len(header) < 12:
                return False
            # Check for 'ftyp' signature (typically at bytes 4-7)
            return MP4_FTYP_SIGNATURE in header
    except (OSError, IOError):
        return False


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


def get_events(folder_path, page=1, per_page=12):
    """
    Get all Tesla events (event-based folder structure) from a TeslaCam folder.

    Args:
        folder_path: Path to a TeslaCam folder (e.g., SavedClips, SentryClips)
        page: Page number (1-based)
        per_page: Number of items per page

    Returns:
        tuple: (events_list, total_count)
        - events_list: List of event dictionaries for the requested page
        - total_count: Total number of events available
    """
    events = []

    try:
        for entry in os.scandir(folder_path):
            if entry.is_dir():
                # Quick check if it looks like an event folder before full parsing
                # This speeds up listing significantly
                events.append({
                    'name': entry.name,
                    'path': entry.path,
                    'timestamp': entry.stat().st_mtime
                })
    except OSError:
        pass

    # Sort by timestamp, newest first
    events.sort(key=lambda x: x['timestamp'], reverse=True)

    total_count = len(events)

    # Calculate pagination slice
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page

    # Slice the raw list first to avoid parsing everything
    paged_raw_events = events[start_idx:end_idx]

    # Fully parse only the requested page
    parsed_events = []
    for raw_event in paged_raw_events:
        event_data = _parse_event_folder(raw_event['path'], raw_event['name'])
        if event_data:
            parsed_events.append(event_data)

    return parsed_events, total_count


def _parse_clips_from_event(event_path):
    """
    Parse all video clips from a SavedClips event folder and group by timestamp.

    SavedClips events contain multiple 1-minute clips over time, each with all camera angles.
    For example: 2025-12-23_18-15-46-front.mp4, 2025-12-23_18-15-46-back.mp4, etc.

    Args:
        event_path: Full path to the event folder

    Returns:
        list: Sorted list of clip dictionaries (oldest to newest), each containing:
              - timestamp_str: Clip timestamp (e.g., "2025-12-23_18-15-46")
              - timestamp: Unix timestamp
              - camera_videos: Dict mapping camera angles to filenames
              - encrypted_videos: Dict mapping camera angles to True if encrypted
              Empty list if no clips found.
    """
    clips_by_timestamp = {}

    try:
        for entry in os.scandir(event_path):
            if entry.is_file() and entry.name.lower().endswith(VIDEO_EXTENSIONS):
                # Skip event.mp4 grid view
                if entry.name.lower() == 'event.mp4':
                    continue

                # Parse filename: YYYY-MM-DD_HH-MM-SS-camera.mp4
                parts = entry.name.rsplit('-', 1)  # Split from right to get camera
                if len(parts) != 2:
                    continue

                timestamp_str = parts[0]  # "2025-12-23_18-15-46"
                camera_with_ext = parts[1]  # "front.mp4"
                camera = camera_with_ext.rsplit('.', 1)[0]  # "front"

                # Create clip entry if not exists
                if timestamp_str not in clips_by_timestamp:
                    # Parse timestamp to datetime for sorting
                    try:
                        dt = datetime.strptime(timestamp_str, '%Y-%m-%d_%H-%M-%S')
                        clips_by_timestamp[timestamp_str] = {
                            'timestamp_str': timestamp_str,
                            'timestamp': dt.timestamp(),
                            'camera_videos': {
                                'front': None,
                                'back': None,
                                'left_repeater': None,
                                'right_repeater': None,
                                'left_pillar': None,
                                'right_pillar': None,
                            },
                            'encrypted_videos': {
                                'front': False,
                                'back': False,
                                'left_repeater': False,
                                'right_repeater': False,
                                'left_pillar': False,
                                'right_pillar': False,
                            }
                        }
                    except ValueError:
                        continue

                # Add camera video to clip and check if encrypted
                if camera in clips_by_timestamp[timestamp_str]['camera_videos']:
                    clips_by_timestamp[timestamp_str]['camera_videos'][camera] = entry.name
                    # Check if video has valid MP4 headers
                    if not is_valid_mp4(entry.path):
                        clips_by_timestamp[timestamp_str]['encrypted_videos'][camera] = True

    except OSError:
        return []

    # Convert to sorted list (oldest to newest)
    clips_list = sorted(clips_by_timestamp.values(), key=lambda x: x['timestamp'])
    return clips_list


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

        # Parse clips from event folder (for SavedClips with multiple timestamps)
        clips = _parse_clips_from_event(event_path)

        # Scan for video files and categorize by camera angle
        # For backward compatibility, also get first/default clip videos
        camera_videos = {
            'front': None,
            'back': None,
            'left_repeater': None,
            'right_repeater': None,
            'left_pillar': None,
            'right_pillar': None,
            'event': None  # Grid view video
        }

        # Track encrypted/invalid videos (Tesla encrypts some camera angles)
        encrypted_videos = {
            'front': False,
            'back': False,
            'left_repeater': False,
            'right_repeater': False,
            'left_pillar': False,
            'right_pillar': False,
        }

        total_size = 0
        latest_timestamp = 0

        for entry in os.scandir(event_path):
            if entry.is_file() and entry.name.lower().endswith(VIDEO_EXTENSIONS):
                try:
                    stat_info = entry.stat()
                    total_size += stat_info.st_size
                    latest_timestamp = max(latest_timestamp, stat_info.st_mtime)

                    # Categorize video by camera angle (use first found for compatibility)
                    name_lower = entry.name.lower()
                    camera_key = None
                    if name_lower == 'event.mp4':
                        camera_videos['event'] = entry.name
                    elif 'front' in name_lower and camera_videos['front'] is None:
                        camera_videos['front'] = entry.name
                        camera_key = 'front'
                    elif 'back' in name_lower and camera_videos['back'] is None:
                        camera_videos['back'] = entry.name
                        camera_key = 'back'
                    elif 'left_repeater' in name_lower and camera_videos['left_repeater'] is None:
                        camera_videos['left_repeater'] = entry.name
                        camera_key = 'left_repeater'
                    elif 'right_repeater' in name_lower and camera_videos['right_repeater'] is None:
                        camera_videos['right_repeater'] = entry.name
                        camera_key = 'right_repeater'
                    elif 'left_pillar' in name_lower and camera_videos['left_pillar'] is None:
                        camera_videos['left_pillar'] = entry.name
                        camera_key = 'left_pillar'
                    elif 'right_pillar' in name_lower and camera_videos['right_pillar'] is None:
                        camera_videos['right_pillar'] = entry.name
                        camera_key = 'right_pillar'

                    # Check if video has valid MP4 headers
                    if camera_key and not is_valid_mp4(entry.path):
                        encrypted_videos[camera_key] = True
                except OSError:
                    continue

        # If no videos found, not a valid event
        if not any(camera_videos.values()) and not clips:
            return None

        # Parse timestamp from event name (format: YYYY-MM-DD_HH-MM-SS)
        event_timestamp = None
        try:
            dt = datetime.strptime(event_name, '%Y-%m-%d_%H-%M-%S')
            event_timestamp = dt.timestamp()
        except ValueError:
            # Fall back to latest file timestamp
            event_timestamp = latest_timestamp if latest_timestamp > 0 else 0

        # Determine starting clip index (closest clip before or at event timestamp)
        starting_clip_index = 0
        if clips:
            for i, clip in enumerate(clips):
                if clip['timestamp'] <= event_timestamp:
                    starting_clip_index = i
                else:
                    break

        return {
            'name': event_name,
            'path': event_path,
            'timestamp': event_timestamp,
            'datetime': datetime.fromtimestamp(event_timestamp).strftime('%Y-%m-%d %I:%M:%S %p'),
            'size': total_size,
            'size_mb': round(total_size / (1024 * 1024), 2),
            'has_thumbnail': has_thumbnail,
            'camera_videos': camera_videos,
            'encrypted_videos': encrypted_videos,  # Track which videos are encrypted
            'metadata': event_metadata,
            'city': event_metadata.get('city', ''),
            'reason': event_metadata.get('reason', ''),
            'clips': clips,  # List of all clips in chronological order
            'starting_clip_index': starting_clip_index,  # Which clip to start playback at
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


def group_videos_by_session(folder_path, page=1, per_page=12):
    """
    Group flat video files by recording session (for RecentClips folder).

    Args:
        folder_path: Path to folder with flat video files
        page: Page number (1-based)
        per_page: Number of items per page

    Returns:
        tuple: (session_list, total_count)
        - session_list: List of session dictionaries for the requested page
        - total_count: Total number of sessions available
    """
    all_videos = get_video_files(folder_path)

    # Group by session
    sessions = {}
    for video in all_videos:
        session_id = video.get('session')
        if not session_id:
            continue

        if session_id not in sessions:
            sessions[session_id] = {
                'name': session_id,
                'timestamp': video['timestamp'],
                'size': 0,
                'camera_videos': {
                    'front': None,
                    'back': None,
                    'left_repeater': None,
                    'right_repeater': None,
                    'left_pillar': None,
                    'right_pillar': None,
                },
                'encrypted_videos': {
                    'front': False,
                    'back': False,
                    'left_repeater': False,
                    'right_repeater': False,
                    'left_pillar': False,
                    'right_pillar': False,
                },
                'has_thumbnail': True,  # Generated on-demand
                'metadata': {},
                'city': '',
                'reason': '',
            }

        # Add video to session
        sessions[session_id]['size'] += video['size']

        # Map to camera angle and check encryption
        camera = video.get('camera', '').lower()
        if camera in sessions[session_id]['camera_videos']:
            sessions[session_id]['camera_videos'][camera] = video['name']
            # Check if video has valid MP4 headers
            if not is_valid_mp4(video['path']):
                sessions[session_id]['encrypted_videos'][camera] = True

    # Convert to list
    session_list = list(sessions.values())

    # Sort by timestamp, newest first
    session_list.sort(key=lambda x: x['timestamp'], reverse=True)

    total_count = len(session_list)

    # Calculate pagination slice
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page

    # Slice the list
    paged_sessions = session_list[start_idx:end_idx]

    # Format only the paged items
    for session_data in paged_sessions:
        session_data['size_mb'] = round(session_data['size'] / (1024 * 1024), 2)
        session_data['datetime'] = datetime.fromtimestamp(session_data['timestamp']).strftime('%Y-%m-%d %I:%M:%S %p')

    return paged_sessions, total_count


def generate_video_thumbnail(video_path, output_path, size=(80, 45)):
    """
    Generate thumbnail from first frame of video using PyAV.

    Args:
        video_path: Path to source video file
        output_path: Path to save thumbnail PNG
        size: Tuple of (width, height) for thumbnail, default 80x45px

    Returns:
        bool: True if successful, False otherwise

    Optimized for Pi Zero 2 W memory constraints.
    Uses a semaphore to prevent concurrent CPU saturation.
    """
    # Acquire semaphore to limit concurrency
    if not thumbnail_semaphore.acquire(blocking=True, timeout=10):
        logger.warning(f"Timeout waiting for thumbnail semaphore: {video_path}")
        return False

    try:
        # Open video container
        container = av.open(video_path)

        # Get first video frame
        for frame in container.decode(video=0):
            # Convert to PIL Image
            img = frame.to_image()

            # Resize to thumbnail size
            img.thumbnail(size, Image.Resampling.LANCZOS)

            # Ensure output directory exists
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            # Save as PNG
            img.save(output_path, 'PNG', optimize=True)

            # Close container and return after first frame
            container.close()
            return True

    except Exception as e:
        logger.error(f"Failed to generate thumbnail for {video_path}: {e}")
        return False
    finally:
        # Always release the semaphore
        thumbnail_semaphore.release()

    return False


def get_teslacam_folders():
    """
    Get available TeslaCam subfolders.

    Returns:
        list: List of folder dictionaries with name, path, and structure type,
              sorted alphabetically by name

    Common folders include:
    - RecentClips: Last hour of recordings (flat structure)
    - SavedClips: Manually saved clips (event subfolder structure)
    - SentryClips: Sentry mode recordings (event subfolder structure)
    """
    teslacam_path = get_teslacam_path()
    if not teslacam_path:
        return []

    folders = []
    try:
        for entry in os.scandir(teslacam_path):
            if entry.is_dir():
                # Determine structure type
                # RecentClips stores files directly, others use event subfolders
                structure_type = 'flat' if entry.name == 'RecentClips' else 'events'

                folders.append({
                    'name': entry.name,
                    'path': entry.path,
                    'structure': structure_type
                })
    except OSError:
        pass

    folders.sort(key=lambda x: x['name'])
    return folders
