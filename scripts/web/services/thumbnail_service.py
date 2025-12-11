#!/usr/bin/env python3
"""
Thumbnail service for TeslaUSB web control interface.

This module handles thumbnail generation and caching for video files.
Thumbnails are extracted from videos using PyAV (Python bindings for FFmpeg libraries)
for minimal CPU/memory overhead on Raspberry Pi Zero 2 W.

On-demand generation: 1-3 seconds per thumbnail
Cached retrieval: 0.1-0.2 seconds
"""

import os
import sys
import threading
import queue
from pathlib import Path

# Import configuration
from config import (
    THUMBNAIL_CACHE_DIR,
)

# Import other services
from services.video_service import get_teslacam_path

# Import utility functions
from utils import generate_thumbnail_hash

# Import video processing library (PyAV - required)
try:
    import av  # PyAV - direct ffmpeg library access for video frame extraction
    from PIL import Image  # PIL for image resizing and JPEG encoding
    VIDEO_BACKEND = 'av'
except ImportError:
    VIDEO_BACKEND = None
    print("ERROR: python3-av not available. Install with: sudo apt install python3-av python3-pil", file=sys.stderr)

# Global queue for background thumbnail generation
_thumbnail_queue = queue.Queue(maxsize=100)
_queue_worker_started = False
_queue_lock = threading.Lock()


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


def generate_thumbnail_av(video_path, thumbnail_path):
    """
    Generate thumbnail using PyAV (direct library access).
    Extracts the first I-frame without full video decoding.
    """
    from PIL import Image
    import av
    
    container = av.open(video_path)
    stream = container.streams.video[0]
    
    # Enable multi-threading for faster decoding
    stream.thread_type = 'AUTO'
    
    for frame in container.decode(video=0):
        # Convert frame to PIL Image
        img = frame.to_image()
        
        # Resize to thumbnail (80px width for low memory usage)
        width = 80
        aspect = img.height / img.width
        height = int(width * aspect)
        img = img.resize((width, height), Image.Resampling.LANCZOS)
        
        # Save as JPEG with moderate quality
        img.save(thumbnail_path, 'JPEG', quality=75, optimize=True)
        
        container.close()
        return True
    
    container.close()
    return False


def generate_thumbnail(video_path, thumbnail_path, fast=True):
    """
    Generate a thumbnail from a video file using efficient Python libraries.
    
    Args:
        video_path: Full path to the video file
        thumbnail_path: Full path where thumbnail should be saved
        fast: Use fast thumbnail extraction (default True)
        
    Returns:
        bool: True if thumbnail was generated successfully, False otherwise
        
    Uses PyAV for direct frame extraction.
    Much faster and more memory-efficient than spawning ffmpeg processes.
    Target: 1-3 seconds on Raspberry Pi Zero 2 W.
    """
    import logging
    
    try:
        # Ensure cache directory exists
        os.makedirs(THUMBNAIL_CACHE_DIR, exist_ok=True)
        
        if VIDEO_BACKEND == 'av':
            # PyAV: Direct library access (no process spawn)
            return generate_thumbnail_av(video_path, thumbnail_path)
        else:
            logging.error("PyAV not available (install python3-av python3-pil)")
            return False
            
    except Exception as e:
        logging.error(f"Thumbnail generation failed for {os.path.basename(video_path)}: {e}")
        return False


def generate_thumbnail_sync(folder, filename):
    """
    Synchronously generate a thumbnail for immediate use (on-demand).
    
    Args:
        folder: TeslaCam subfolder name
        filename: Video filename
        
    Returns:
        str: Path to generated thumbnail, or None if failed
    """
    result = get_thumbnail_path(folder, filename)
    if not result:
        return None
    
    thumbnail_path, video_path = result
    
    # If thumbnail already exists, return it
    if os.path.isfile(thumbnail_path):
        return thumbnail_path
    
    # Generate thumbnail now
    if generate_thumbnail(video_path, thumbnail_path, fast=True):
        return thumbnail_path
    
    return None


def _thumbnail_queue_worker():
    """
    Background worker that processes thumbnail generation requests.
    Runs in a separate thread to avoid blocking web requests.
    """
    while True:
        try:
            video_path, thumbnail_path = _thumbnail_queue.get(timeout=1)
            
            # Skip if thumbnail already exists
            if os.path.isfile(thumbnail_path):
                _thumbnail_queue.task_done()
                continue
            
            # Generate thumbnail
            generate_thumbnail(video_path, thumbnail_path, fast=True)
            _thumbnail_queue.task_done()
            
        except queue.Empty:
            continue
        except Exception:
            pass


def queue_thumbnail_generation(folder, filename):
    """
    Queue a thumbnail for background generation.
    
    Args:
        folder: TeslaCam subfolder name
        filename: Video filename
        
    Returns:
        bool: True if queued successfully, False otherwise
    """
    global _queue_worker_started
    
    result = get_thumbnail_path(folder, filename)
    if not result:
        return False
    
    thumbnail_path, video_path = result
    
    # If thumbnail already exists, no need to queue
    if os.path.isfile(thumbnail_path):
        return True
    
    # Start background worker if not already running
    with _queue_lock:
        if not _queue_worker_started:
            worker = threading.Thread(target=_thumbnail_queue_worker, daemon=True)
            worker.start()
            _queue_worker_started = True
    
    # Add to queue (non-blocking)
    try:
        _thumbnail_queue.put_nowait((video_path, thumbnail_path))
        return True
    except queue.Full:
        return False


def batch_generate_thumbnails(video_list, max_count=10):
    """
    Generate thumbnails for multiple videos (batch processing).
    
    Args:
        video_list: List of (folder, filename) tuples
        max_count: Maximum number of thumbnails to generate in this batch
        
    Returns:
        int: Number of thumbnails generated
    """
    generated = 0
    
    for folder, filename in video_list[:max_count]:
        result = get_thumbnail_path(folder, filename)
        if not result:
            continue
        
        thumbnail_path, video_path = result
        
        # Skip if already exists
        if os.path.isfile(thumbnail_path):
            continue
        
        # Generate thumbnail
        if generate_thumbnail(video_path, thumbnail_path, fast=True):
            generated += 1
    
    return generated


def cleanup_orphaned_thumbnails():
    """
    Remove thumbnails for videos that no longer exist.
    
    Returns:
        int: Number of thumbnails removed
    """
    import re
    from config import VIDEO_EXTENSIONS
    
    try:
        if not os.path.isdir(THUMBNAIL_CACHE_DIR):
            return 0
        
        teslacam_path = get_teslacam_path()
        if not teslacam_path:
            return 0
        
        # Build set of valid thumbnail hashes from existing videos
        valid_hashes = set()
        
        # Scan all TeslaCam folders
        for folder_entry in os.scandir(teslacam_path):
            if not folder_entry.is_dir():
                continue
            
            # Scan all videos in folder
            for video_entry in os.scandir(folder_entry.path):
                if not video_entry.is_file():
                    continue
                
                if not video_entry.name.lower().endswith(VIDEO_EXTENSIONS):
                    continue
                
                # Import here to avoid circular dependency
                from utils import generate_thumbnail_hash
                video_hash = generate_thumbnail_hash(video_entry.path)
                if video_hash:
                    valid_hashes.add(f"{video_hash}.jpg")
        
        # Remove orphaned thumbnails
        removed_count = 0
        for thumbnail_file in os.listdir(THUMBNAIL_CACHE_DIR):
            if not thumbnail_file.endswith('.jpg'):
                continue
            
            if thumbnail_file not in valid_hashes:
                try:
                    thumbnail_path = os.path.join(THUMBNAIL_CACHE_DIR, thumbnail_file)
                    os.remove(thumbnail_path)
                    removed_count += 1
                except OSError:
                    pass
        
        return removed_count
    
    except Exception:
        return 0
