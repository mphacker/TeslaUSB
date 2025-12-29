#!/usr/bin/env python3
"""
Mode service for TeslaUSB web control interface.

This module handles mode detection, state file management, and mode-related
UI logic. The system can be in one of three modes:
- present: USB gadget is active (Tesla can record)
- edit: Partitions mounted read-write with Samba access
- unknown: State cannot be determined
"""

import os
import socket
import subprocess
import glob
import logging

# Import configuration
from config import (
    STATE_FILE,
    MNT_DIR,
    USB_PARTITIONS,
    MODE_DISPLAY,
)

logger = logging.getLogger(__name__)


def _configfs_gadget_present():
    """Check if the configfs mass_storage gadget is active (present mode)."""
    try:
        # Look for any mass_storage.usb0 LUN0 file path and confirm it is set
        for lun_file in glob.glob('/sys/kernel/config/usb_gadget/*/functions/mass_storage.usb0/lun.0/file'):
            try:
                with open(lun_file, 'r', encoding='utf-8') as fh:
                    backing = fh.read().strip()
                if backing:
                    return True
            except OSError:
                continue
    except Exception:
        pass
    return False


def detect_mode():
    """Attempt to infer the current mode when the state file is missing."""
    # Prefer configfs gadget detection (current path) over legacy g_mass_storage
    if _configfs_gadget_present():
        logger.debug("Detected present mode via configfs gadget")
        return "present"

    try:
        result = subprocess.run(
            ["lsmod"], capture_output=True, text=True, check=False, timeout=5
        )
        if result.stdout and "g_mass_storage" in result.stdout:
            logger.debug("Detected present mode via g_mass_storage module")
            return "present"
    except Exception as e:
        logger.warning(f"Error checking lsmod for g_mass_storage: {e}")

    try:
        for part in USB_PARTITIONS:
            mp = os.path.join(MNT_DIR, part)
            if os.path.ismount(mp):
                logger.debug(f"Detected edit mode via mounted partition {mp}")
                return "edit"
    except Exception as e:
        logger.warning(f"Error checking mount points: {e}")

    logger.debug("Could not determine mode, returning unknown")
    return "unknown"


def current_mode():
    """Read the current mode from the state file, falling back when needed."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as state_file:
            token = state_file.read().strip().lower()
            if token in MODE_DISPLAY:
                return token
            logger.warning(f"Invalid mode token in state file: {token}")
    except FileNotFoundError:
        logger.debug(f"State file not found: {STATE_FILE}, falling back to detection")
    except OSError as e:
        logger.warning(f"Error reading state file: {e}")

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
    # Check if any partitions are mounted (even in present mode with RO mounts)
    for part in USB_PARTITIONS:
        mount_path = os.path.join(MNT_DIR, part)
        if os.path.isdir(mount_path):
            return True
    return False
