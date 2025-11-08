#!/usr/bin/env python3
"""
Samba service for TeslaUSB web control interface.

This module handles Samba (SMB) share management operations.
Samba shares allow Windows/network access to the USB partitions.
These functions ensure that file changes are immediately visible to Samba clients.
"""

import subprocess

# Import configuration
from config import (
    GADGET_DIR,
    PART_LABEL_MAP,
)


def close_samba_share(partition_key):
    """
    Ask Samba to close and reopen the relevant share so new files appear immediately.
    
    Args:
        partition_key: The partition identifier (e.g., "part1", "part2")
        
    This function sends commands to Samba to force it to release and reload the share,
    which makes newly uploaded or deleted files visible to network clients without
    requiring them to disconnect and reconnect.
    """
    share_name = PART_LABEL_MAP.get(partition_key, f"gadget_{partition_key}")
    commands = [
        ["sudo", "-n", "smbcontrol", "all", "close-share", share_name],
        ["sudo", "-n", "smbcontrol", "all", "reload-config"],
        ["sudo", "-n", "smbcontrol", "all", "close-share", share_name],
    ]
    for cmd in commands:
        try:
            subprocess.run(cmd, check=False, timeout=5, cwd=GADGET_DIR)
        except Exception:
            pass


def restart_samba_services():
    """
    Force Samba to reload so new files are visible to clients.
    
    This restarts both the smbd (SMB daemon) and nmbd (NetBIOS name server)
    services, which is a more aggressive approach than close_samba_share().
    Use this after major changes like mode switches.
    """
    for service in ("smbd", "nmbd"):
        try:
            subprocess.run(["sudo", "-n", "systemctl", "restart", service], check=False, timeout=10)
        except Exception:
            pass
