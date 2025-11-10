#!/usr/bin/env python3
"""
Thumbnail service for TeslaUSB web control interface.

This module handles thumbnail generation and caching for video files.
Thumbnails are extracted from videos using ffmpeg and cached in a persistent
directory to avoid regenerating them on every page load.
"""

import os
import subprocess

# Import configuration
from config import (
    THUMBNAIL_CACHE_DIR,
)

# Import other services
from services.video_service import get_teslacam_path

# Import utility functions
from utils import generate_thumbnail_hash


def get_thumbnail_path(folder, filename):
    """
    Get the cached thumbnail path for a video file.
    
    Args:
        folder: TeslaCam subfolder name (e.g., "RecentClips")
        filename: Video filename (e.g., "2025-11-08_08-15-44-front.mp4")
        
    Returns:
        tuple: (thumbnail_path, video_path) if video exists, None otherwise
        
    The thumbnail path is based on an MD5 hash of the video's path, mtime, and size.
    This ensures thumbnails are regenerated if the video file changes.
    """
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
    """
    Generate a thumbnail from a video file using ffmpeg.
    
    Args:
        video_path: Full path to the video file
        thumbnail_path: Full path where thumbnail should be saved
        
    Returns:
        bool: True if thumbnail was generated successfully, False otherwise
        
    This function extracts a frame at 1 second into the video and resizes it
    to 160px width while maintaining aspect ratio. The thumbnail is saved as JPEG.
    """
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
