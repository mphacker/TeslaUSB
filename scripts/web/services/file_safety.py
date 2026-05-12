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


def safe_delete_archive_video(path: str) -> int:
    """The single doorway for deleting an archived video file.

    Every code path in TeslaUSB that deletes a clip from the local archive
    (retention prune, size trim, free-space trim, corrupt-file purge,
    non-driving prune, watchdog retention, manual cleanup) MUST go through
    this function. Calling ``os.remove`` / ``os.unlink`` directly on
    archive files is a contract violation — past data-loss incidents were
    caused by a delete path that bypassed the protected-file check.

    The helper:

    * Refuses to delete any file flagged by :func:`is_protected_file`
      (currently: ``*.img`` files inside ``GADGET_DIR``).
    * Reads the file size BEFORE removing so the caller can update its
      bytes-freed accounting.
    * Swallows ``OSError`` (including FileNotFoundError) and returns 0
      so loops over many candidate files don't blow up on transient races.

    Geodata reconciliation (``mapping_service.purge_deleted_videos``) is
    intentionally NOT done here — it would create a circular-import risk
    and the May 7 contract requires the caller to control which rows get
    NULL'd. Callers that hold a list of successfully-deleted paths should
    call ``purge_deleted_videos`` themselves after the loop finishes.

    Args:
        path: Absolute path to the archived video to delete.

    Returns:
        Byte count of the deleted file, or 0 if the file was protected,
        missing, or removal failed for any other reason. A ``> 0`` return
        means the file was definitively removed from disk.
    """
    if is_protected_file(path):
        return 0
    try:
        size = os.path.getsize(path)
    except OSError:
        # Missing or stat failure — nothing to delete.
        return 0
    try:
        os.remove(path)
    except FileNotFoundError:
        return 0
    except OSError as e:
        logger.warning(
            "safe_delete_archive_video: failed to remove %s: %s", path, e,
        )
        return 0
    return int(size)


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
