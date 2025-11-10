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

# Import configuration
from config import (
    STATE_FILE,
    MNT_DIR,
    USB_PARTITIONS,
    MODE_DISPLAY,
)


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
    # Check if any partitions are mounted (even in present mode with RO mounts)
    for part in USB_PARTITIONS:
        mount_path = os.path.join(MNT_DIR, part)
        if os.path.isdir(mount_path):
            return True
    return False
