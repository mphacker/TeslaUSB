"""
File safety utilities for TeslaUSB.

Provides guards to prevent accidental deletion or overwriting of critical
files — most importantly the USB disk images (*.img) that Tesla records to.

Every code path that deletes files MUST call is_protected_file() first.
"""

import logging
import os

logger = logging.getLogger(__name__)

# Lazy-load GADGET_DIR to avoid circular imports at module level
_gadget_dir = None


def _get_gadget_dir():
    global _gadget_dir
    if _gadget_dir is None:
        from config import GADGET_DIR
        _gadget_dir = os.path.realpath(GADGET_DIR)
    return _gadget_dir


def is_protected_file(path: str) -> bool:
    """Check whether a file path is protected from deletion.

    Protected files:
    - Any ``*.img`` file inside GADGET_DIR (the USB disk images)

    Args:
        path: Absolute or relative path to check.

    Returns:
        True if the file MUST NOT be deleted/overwritten.
    """
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return False

    # Protect *.img files in the gadget directory
    if real.lower().endswith(".img"):
        gadget = _get_gadget_dir()
        if real.startswith(gadget + os.sep) or real == gadget:
            logger.warning(
                "BLOCKED: attempt to delete/overwrite protected IMG file: %s",
                real,
            )
            return True

    return False


def safe_remove(path: str) -> bool:
    """Remove a file only if it is not protected.

    Args:
        path: File to remove.

    Returns:
        True if the file was removed, False if it was protected or missing.

    Raises:
        OSError: If removal fails for a reason other than file-not-found.
    """
    if is_protected_file(path):
        logger.error(
            "REFUSED to delete protected file: %s — IMG files must never be deleted",
            path,
        )
        return False
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


def safe_rmtree(path: str) -> bool:
    """Remove a directory tree only if it contains no protected files.

    Scans the tree first; if ANY protected file is found the entire
    operation is refused.

    Args:
        path: Directory to remove.

    Returns:
        True if removed, False if refused or missing.
    """
    import shutil

    if not os.path.isdir(path):
        return False

    # Scan for protected files before removing anything
    for dirpath, _dirnames, filenames in os.walk(path):
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            if is_protected_file(full):
                logger.error(
                    "REFUSED to rmtree %s — contains protected file: %s",
                    path,
                    full,
                )
                return False

    shutil.rmtree(path)
    return True
