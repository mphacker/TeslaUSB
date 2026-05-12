"""
File safety utilities for TeslaUSB.

Provides guards to prevent accidental deletion or overwriting of critical
files — most importantly the USB disk images (*.img) that Tesla records to.

Every code path that deletes files MUST call is_protected_file() first.
"""

import logging
import os
from enum import Enum
from typing import NamedTuple

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


class DeleteOutcome(Enum):
    """Three-valued result for :func:`safe_delete_archive_video`.

    Distinguishes the three semantically-different "did not delete"
    cases so callers can react appropriately without re-probing the
    filesystem (which would also double-log the BLOCKED warning).
    """

    DELETED = "deleted"      # File was removed from disk.
    PROTECTED = "protected"  # is_protected_file refused (e.g. *.img).
    MISSING = "missing"      # File didn't exist (FileNotFoundError).
    ERROR = "error"          # Other OSError (permissions, I/O, etc.).


class DeleteResult(NamedTuple):
    """Outcome + bytes freed for :func:`safe_delete_archive_video`.

    ``outcome`` carries the ternary state; ``bytes_freed`` is the size
    of the deleted file (only meaningful when outcome is DELETED, but
    will be 0 for an actually-deleted 0-byte file too — callers should
    use ``outcome is DeleteOutcome.DELETED`` for the boolean test, not
    ``bytes_freed > 0``).
    """

    outcome: "DeleteOutcome"
    bytes_freed: int


def safe_delete_archive_video(path: str) -> DeleteResult:
    """The single doorway for deleting an archived video file.

    Every code path in TeslaUSB that deletes a clip from the local archive
    (retention prune, size trim, free-space trim, corrupt-file purge,
    non-driving prune, watchdog retention, manual cleanup, video-panel
    delete) MUST go through this function. Calling ``os.remove`` /
    ``os.unlink`` directly on archive files is a contract violation —
    past data-loss incidents were caused by a delete path that bypassed
    the protected-file check.

    The helper:

    * Refuses to delete any file flagged by :func:`is_protected_file`
      (currently: ``*.img`` files inside ``GADGET_DIR``) — returns
      ``DeleteOutcome.PROTECTED``.
    * Reads the file size BEFORE removing so the caller can update its
      bytes-freed accounting.
    * Returns ``DeleteOutcome.MISSING`` for ``FileNotFoundError`` so
      loops over many candidate files don't blow up on transient races.
    * Returns ``DeleteOutcome.ERROR`` (with a WARNING log) for other
      ``OSError`` so callers can surface "skipped (unwritable)" without
      re-probing the filesystem.

    Geodata reconciliation (``mapping_service.purge_deleted_videos``)
    is intentionally NOT done here — it would create a circular-import
    risk and the May 7 contract requires the caller to control which
    rows get NULL'd. Callers that hold a list of
    successfully-deleted paths should call ``purge_deleted_videos``
    themselves after the loop finishes.

    Args:
        path: Absolute path to the archived video to delete.

    Returns:
        :class:`DeleteResult` with ``outcome`` and ``bytes_freed``.
        ``bytes_freed`` is the file size at the moment of deletion;
        ``0`` for any non-DELETED outcome (and also for a real 0-byte
        delete, so use ``outcome is DeleteOutcome.DELETED`` for the
        boolean test).
    """
    if is_protected_file(path):
        return DeleteResult(DeleteOutcome.PROTECTED, 0)
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        return DeleteResult(DeleteOutcome.MISSING, 0)
    except OSError as e:
        logger.warning(
            "safe_delete_archive_video: stat failed for %s: %s", path, e,
        )
        return DeleteResult(DeleteOutcome.ERROR, 0)
    try:
        os.remove(path)
    except FileNotFoundError:
        return DeleteResult(DeleteOutcome.MISSING, 0)
    except OSError as e:
        logger.warning(
            "safe_delete_archive_video: failed to remove %s: %s", path, e,
        )
        return DeleteResult(DeleteOutcome.ERROR, 0)
    return DeleteResult(DeleteOutcome.DELETED, int(size))


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
