#!/usr/bin/env python3
"""
Partition service for TeslaUSB web control interface.

This module handles USB partition mount path resolution and iteration.
Partitions can be accessed in different ways depending on the current mode:
- present mode: Read-only mounts at /mnt/gadget/partN-ro
- edit mode: Read-write mounts at /mnt/gadget/partN
"""

import os
import logging

logger = logging.getLogger(__name__)

# Import configuration
from config import (
    MNT_DIR,
    RO_MNT_DIR,
    USB_PARTITIONS,
    IMG_CAM_PATH,
    IMG_LIGHTSHOW_PATH,
    IMG_MUSIC_PATH,
    MUSIC_ENABLED,
)

# Import mode service
from services.mode_service import current_mode


def get_feature_availability():
    """Return a dict of boolean flags indicating which features are available.

    Checks os.path.isfile() on each image path at call time so the result
    adapts dynamically when images are created or deleted — no service
    restart needed.
    """
    cam_exists = os.path.isfile(IMG_CAM_PATH)
    lightshow_exists = os.path.isfile(IMG_LIGHTSHOW_PATH)
    music_exists = MUSIC_ENABLED and os.path.isfile(IMG_MUSIC_PATH)

    return {
        'analytics_available': cam_exists,
        'videos_available': cam_exists,
        'chimes_available': lightshow_exists,
        'shows_available': lightshow_exists,
        'wraps_available': lightshow_exists,
        'music_available': music_exists,
    }


def iter_mounted_partitions():
    """Yield mounted USB partitions and their paths."""
    for part in USB_PARTITIONS:
        mount_path = os.path.join(MNT_DIR, part)
        if os.path.isdir(mount_path):
            yield part, mount_path


def iter_all_partitions():
    """Yield all accessible USB partitions based on current mode."""
    mode = current_mode()

    if mode == "present":
        # Use read-only mounts in present mode
        for part in USB_PARTITIONS:
            ro_path = os.path.join(RO_MNT_DIR, f"{part}-ro")
            if os.path.isdir(ro_path):
                yield part, ro_path
    else:
        # Use read-write mounts in edit mode
        for part in USB_PARTITIONS:
            rw_path = os.path.join(MNT_DIR, part)
            if os.path.isdir(rw_path):
                yield part, rw_path


def get_mount_path(partition):
    """Get the mount path for a specific partition based on current mode."""
    if partition not in USB_PARTITIONS:
        return None

    mode = current_mode()

    if mode == "present":
        # Use read-only mount in present mode
        ro_path = os.path.join(RO_MNT_DIR, f"{partition}-ro")
        if os.path.isdir(ro_path):
            return ro_path
    else:
        # Use read-write mount in edit mode
        rw_path = os.path.join(MNT_DIR, partition)
        if os.path.isdir(rw_path):
            return rw_path

    return None
