#!/usr/bin/env python3
"""
TeslaUSB Configuration File

EDIT THIS FILE to match your installation.
All paths and settings can be customized here.
"""

import os
import secrets

# ============================================================================
# USER EDITABLE SETTINGS - Modify these values for your installation
# ============================================================================

# Installation directory where TeslaUSB is installed
GADGET_DIR = '/home/pi/TeslaUSB'

# Linux user running the TeslaUSB services
TARGET_USER = 'pi'

# Flask web server configuration
WEB_PORT = 5000

# Auto-generate secret key if not set (or set to default)
_DEFAULT_SECRET = 'CHANGE-THIS-TO-A-RANDOM-SECRET-KEY-ON-FIRST-INSTALL'
SECRET_KEY = _DEFAULT_SECRET

# Auto-generate a secret key on first run if still using default
if SECRET_KEY == _DEFAULT_SECRET:
    SECRET_KEY = secrets.token_hex(32)
    print(f"Generated new SECRET_KEY. Consider saving it to config.py for persistence.")

# Mount directory for USB partitions
MNT_DIR = '/mnt/gadget'

# ============================================================================
# ADVANCED SETTINGS - Usually don't need to change these
# ============================================================================

# Read-only mount directory for present mode (computed from MNT_DIR)
RO_MNT_DIR = MNT_DIR  # Same as MNT_DIR since we use -ro and -rw suffixes

# State management
STATE_FILE = os.path.join(GADGET_DIR, "state.txt")

# Lock chime configuration
LOCK_CHIME_FILENAME = "LockChime.wav"
CHIMES_FOLDER = "Chimes"  # Folder on part2 where custom chimes are stored
MAX_LOCK_CHIME_SIZE = 1024 * 1024  # 1 MiB

# Light show configuration
LIGHT_SHOW_FOLDER = "LightShow"  # Folder on part2 where light shows are stored

# USB partition configuration
USB_PARTITIONS = ("part1", "part2")
PART_LABEL_MAP = {"part1": "gadget_part1", "part2": "gadget_part2"}

# Thumbnail configuration
THUMBNAIL_CACHE_DIR = os.path.join(GADGET_DIR, "thumbnails")

# Mode display configuration
MODE_DISPLAY = {
    "present": ("USB Gadget Mode", "present"),
    "edit": ("Edit Mode", "edit"),
    "unknown": ("Unknown", "unknown"),
}

# File type configurations
VIDEO_EXTENSIONS = ('.mp4', '.avi', '.mov', '.mkv')
LIGHT_SHOW_EXTENSIONS = ('.fseq', '.mp3', '.wav')

# Script paths (scripts are in GADGET_DIR/scripts/)
def get_script_path(script_name):
    """Get the full path to a script in the GADGET_DIR/scripts/ directory."""
    return os.path.join(GADGET_DIR, "scripts", script_name)
