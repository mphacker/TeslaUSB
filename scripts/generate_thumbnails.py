#!/usr/bin/env python3
"""
Thumbnail Generator Background Process

Scans TeslaCam videos and generates thumbnails in the background.
Removes orphaned thumbnails for deleted videos.
"""

import os
import sys
import subprocess
import hashlib
import time
import signal
from pathlib import Path

# Configuration
THUMBNAIL_CACHE_DIR = "__GADGET_DIR__/thumbnails"
MNT_DIR = "__MNT_DIR__"
RO_MNT_DIR = "/mnt/gadget"
STATE_FILE = "__GADGET_DIR__/state.txt"
VIDEO_EXTENSIONS = ('.mp4', '.avi', '.mov', '.mkv')

# Global flag for graceful shutdown
shutdown_requested = False


def signal_handler(signum, frame):
    """Handle SIGTERM signal for graceful shutdown."""
    global shutdown_requested
    print(f"Received signal {signum}, will exit after current thumbnail completes...")
    shutdown_requested = True


def get_current_mode():
    """Read the current mode from the state file."""
    try:
        with open(STATE_FILE, 'r') as f:
            return f.read().strip().lower()
    except:
        return 'unknown'


def get_teslacam_path():
    """Get the TeslaCam path based on current mode."""
    mode = get_current_mode()
    
    if mode == "present":
        ro_path = os.path.join(RO_MNT_DIR, "part1-ro", "TeslaCam")
        if os.path.isdir(ro_path):
            return ro_path
    elif mode == "edit":
        rw_path = os.path.join(MNT_DIR, "part1", "TeslaCam")
        if os.path.isdir(rw_path):
            return rw_path
    
    return None


def generate_video_hash(video_path):
    """Generate a unique hash for a video file."""
    try:
        stat_info = os.stat(video_path)
        unique_string = f"{video_path}_{stat_info.st_mtime}_{stat_info.st_size}"
        return hashlib.md5(unique_string.encode()).hexdigest()
    except OSError:
        return None


def generate_thumbnail(video_path, thumbnail_path):
    """Generate a thumbnail from a video file using ffmpeg."""
    try:
        # Ensure cache directory exists
        os.makedirs(THUMBNAIL_CACHE_DIR, exist_ok=True)
        
        # Use ffmpeg to extract a frame at 1 second
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
        else:
            # Try at 0 seconds if 1 second fails (video might be shorter)
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-ss", "0",
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
            return result.returncode == 0 and os.path.isfile(thumbnail_path)
        
    except Exception as e:
        print(f"Error generating thumbnail for {video_path}: {e}", file=sys.stderr)
        return False


def scan_and_generate_thumbnails():
    """Scan all videos and generate missing thumbnails."""
    teslacam_path = get_teslacam_path()
    if not teslacam_path:
        print("TeslaCam path not accessible")
        return
    
    print(f"Scanning TeslaCam path: {teslacam_path}")
    
    # Track valid thumbnail hashes
    valid_hashes = set()
    generated_count = 0
    skipped_count = 0
    
    # Scan all folders
    try:
        for folder_entry in os.scandir(teslacam_path):
            # Check for shutdown request
            if shutdown_requested:
                print("Shutdown requested - stopping scan")
                break
            
            if not folder_entry.is_dir():
                continue
            
            print(f"Scanning folder: {folder_entry.name}")
            
            # Scan all videos in folder
            for video_entry in os.scandir(folder_entry.path):
                # Check for shutdown request
                if shutdown_requested:
                    print("Shutdown requested - stopping scan")
                    break
                
                if not video_entry.is_file():
                    continue
                
                if not video_entry.name.lower().endswith(VIDEO_EXTENSIONS):
                    continue
                
                video_path = video_entry.path
                video_hash = generate_video_hash(video_path)
                
                if not video_hash:
                    continue
                
                thumbnail_filename = f"{video_hash}.jpg"
                valid_hashes.add(thumbnail_filename)
                thumbnail_path = os.path.join(THUMBNAIL_CACHE_DIR, thumbnail_filename)
                
                # Check if thumbnail already exists
                if os.path.isfile(thumbnail_path):
                    skipped_count += 1
                    continue
                
                # Generate thumbnail
                print(f"Generating thumbnail for: {video_entry.name}")
                if generate_thumbnail(video_path, thumbnail_path):
                    generated_count += 1
                    print(f"  ✓ Generated: {thumbnail_filename}")
                    
                    # Add small delay between thumbnails to reduce memory pressure
                    time.sleep(0.5)
                else:
                    print(f"  ✗ Failed: {thumbnail_filename}")
    
    except Exception as e:
        print(f"Error scanning videos: {e}", file=sys.stderr)
    
    print(f"Generated {generated_count} new thumbnails, skipped {skipped_count} existing")
    
    # Cleanup orphaned thumbnails
    cleanup_orphaned_thumbnails(valid_hashes)


def cleanup_orphaned_thumbnails(valid_hashes):
    """Remove thumbnails for videos that no longer exist."""
    try:
        if not os.path.isdir(THUMBNAIL_CACHE_DIR):
            return
        
        removed_count = 0
        for thumbnail_file in os.listdir(THUMBNAIL_CACHE_DIR):
            if not thumbnail_file.endswith('.jpg'):
                continue
            
            if thumbnail_file not in valid_hashes:
                try:
                    os.remove(os.path.join(THUMBNAIL_CACHE_DIR, thumbnail_file))
                    removed_count += 1
                    print(f"Removed orphaned thumbnail: {thumbnail_file}")
                except OSError:
                    pass
        
        print(f"Removed {removed_count} orphaned thumbnails")
    
    except Exception as e:
        print(f"Error cleaning up thumbnails: {e}", file=sys.stderr)


if __name__ == "__main__":
    # Register signal handler for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    print("=== TeslaCam Thumbnail Generator ===")
    print(f"Started at: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    scan_and_generate_thumbnails()
    
    if shutdown_requested:
        print("Shutdown requested - exiting gracefully")
    
    print(f"Finished at: {time.strftime('%Y-%m-%d %H:%M:%S')}")
