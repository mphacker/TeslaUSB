#!/usr/bin/env python3
"""Boombox custom honk-sound management helpers.

Manages MP3 files in the /Media/ folder on the music partition (LUN 2).
Tesla reads this folder for custom Boombox horn sounds.

Constraints:
- Folder: /Media/ at the root of usb_music.img (part3)
- Format: MP3 only
- Max file size: 1 MiB
- Max file count: 20
"""

import os
import re
import logging
from typing import List, Tuple

from config import MNT_DIR
from services.partition_service import get_mount_path
from services.samba_service import close_samba_share
from services.mode_service import current_mode
from services.partition_mount_service import quick_edit_part3

logger = logging.getLogger(__name__)

BOOMBOX_FOLDER = "Media"
ALLOWED_EXT = ".mp3"
MAX_FILE_SIZE = 1 * 1024 * 1024  # 1 MiB
MAX_FILE_COUNT = 20


class BoomboxServiceError(Exception):
    """Raised for user-facing Boombox service errors."""


def _sanitize_filename(name: str) -> str:
    """Strip directory components and disallowed characters from a filename."""
    base = os.path.basename(name or "")
    base = re.sub(r'[\x00-\x1f\x7f/\\:]', '', base).strip()
    base = re.sub(r'\s+', ' ', base)
    return base


def _ensure_mount() -> Tuple[str, str]:
    """Return the music partition mount path, or an empty string and error message."""
    mount_path = get_mount_path("part3")
    if not mount_path:
        return "", "Music drive not mounted. Switch to Edit mode and try again."
    if not os.path.ismount(mount_path):
        return "", "Music drive is unavailable."
    return mount_path, ""


def _media_dir(mount_path: str) -> str:
    """Return the /Media/ directory path within the given mount, creating it if needed."""
    media_dir = os.path.join(mount_path, BOOMBOX_FOLDER)
    os.makedirs(media_dir, exist_ok=True)
    return media_dir


def _validate_filename(name: str) -> str:
    """Sanitize and validate an uploaded filename; raise BoomboxServiceError on failure."""
    safe = _sanitize_filename(name)
    if not safe:
        raise BoomboxServiceError("Invalid filename.")
    ext = os.path.splitext(safe)[1].lower()
    if ext != ALLOWED_EXT:
        raise BoomboxServiceError("Only MP3 files are accepted.")
    return safe


def _safe_file_path(media_dir: str, filename: str) -> str:
    """Return an absolute path within media_dir for filename, rejecting traversal attempts."""
    target = os.path.normpath(os.path.join(media_dir, filename))
    if os.path.commonpath([media_dir, target]) != os.path.abspath(media_dir):
        raise BoomboxServiceError("Invalid file path.")
    return target


def list_boombox_files(mount_path: str = "") -> Tuple[List[dict], str, int, int]:
    """Return (files, error, used_bytes, free_bytes) for the /Media/ folder.

    Each file entry: {name, size, size_str}.
    If mount_path is empty the current mode mount is resolved automatically.
    """
    if not mount_path:
        mount_path, err = _ensure_mount()
        if err:
            return [], err, 0, 0

    media_dir = _media_dir(mount_path)

    try:
        stat = os.statvfs(mount_path)
        free_bytes = stat.f_bavail * stat.f_frsize
        total_bytes = stat.f_blocks * stat.f_frsize
        used_bytes = total_bytes - free_bytes
    except OSError:
        free_bytes = 0
        used_bytes = 0

    files = []
    try:
        for entry in os.scandir(media_dir):
            if not entry.is_file():
                continue
            if os.path.splitext(entry.name)[1].lower() != ALLOWED_EXT:
                continue
            size = entry.stat().st_size
            files.append({"name": entry.name, "size": size})
    except OSError as exc:
        logger.warning("Could not read Media directory: %s", exc)
        return [], "Unable to read Media directory.", used_bytes, free_bytes

    files.sort(key=lambda f: f["name"].lower())
    return files, "", used_bytes, free_bytes


def upload_boombox_file(file_storage) -> Tuple[bool, str]:
    """Upload an MP3 file to /Media/ on the music partition.

    Mode-aware: uses quick_edit_part3 in present mode.
    """
    filename = _validate_filename(file_storage.filename)

    file_storage.stream.seek(0, os.SEEK_END)
    incoming_size = file_storage.stream.tell()
    file_storage.stream.seek(0)

    if incoming_size > MAX_FILE_SIZE:
        return False, f"File too large (max {MAX_FILE_SIZE // (1024 * 1024)} MiB)."

    file_bytes = file_storage.read()

    def _do_upload(mount_path: str) -> Tuple[bool, str]:
        media_dir = _media_dir(mount_path)

        existing = [
            e.name for e in os.scandir(media_dir)
            if e.is_file() and os.path.splitext(e.name)[1].lower() == ALLOWED_EXT
        ]
        if len(existing) >= MAX_FILE_COUNT:
            return False, f"Maximum of {MAX_FILE_COUNT} files reached. Delete one first."

        free_bytes = os.statvfs(mount_path).f_bavail * os.statvfs(mount_path).f_frsize
        if free_bytes <= incoming_size + (2 * 1024 * 1024):
            return False, "Not enough free space on Music drive."

        final_path = _safe_file_path(media_dir, filename)
        tmp_path = final_path + ".tmp"

        try:
            with open(tmp_path, "wb") as fh:
                fh.write(file_bytes)
                fh.flush()
                os.fsync(fh.fileno())

            os.replace(tmp_path, final_path)

            fd = os.open(media_dir, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            logger.error("Failed to write Boombox file: %s", exc)
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return False, "Failed to save file."

        try:
            close_samba_share("part3")
        except Exception:
            pass

        return True, f"Uploaded {filename}."

    mode = current_mode()
    if mode == "present":
        def _quick_callback():
            rw_mount = os.path.join(MNT_DIR, "part3")
            return _do_upload(rw_mount)

        return quick_edit_part3(_quick_callback, timeout=30)

    mount_path, err = _ensure_mount()
    if err:
        return False, err
    return _do_upload(mount_path)


def delete_boombox_file(filename: str) -> Tuple[bool, str]:
    """Delete a single MP3 from /Media/ on the music partition.

    Mode-aware: uses quick_edit_part3 in present mode.
    """
    safe_name = _validate_filename(filename)

    def _do_delete(mount_path: str) -> Tuple[bool, str]:
        media_dir = _media_dir(mount_path)
        file_path = _safe_file_path(media_dir, safe_name)

        if not os.path.isfile(file_path):
            return False, f"{safe_name} not found."

        try:
            os.remove(file_path)
            fd = os.open(media_dir, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            logger.error("Failed to delete Boombox file: %s", exc)
            return False, "Failed to delete file."

        try:
            close_samba_share("part3")
        except Exception:
            pass

        return True, f"Deleted {safe_name}."

    mode = current_mode()
    if mode == "present":
        def _quick_callback():
            rw_mount = os.path.join(MNT_DIR, "part3")
            return _do_delete(rw_mount)

        return quick_edit_part3(_quick_callback, timeout=30)

    mount_path, err = _ensure_mount()
    if err:
        return False, err
    return _do_delete(mount_path)


def resolve_boombox_file_path(filename: str) -> str:
    """Return the absolute filesystem path for a Boombox MP3 file.

    Raises BoomboxServiceError if the drive is not mounted or the file is absent.
    """
    safe_name = _validate_filename(filename)
    mount_path, err = _ensure_mount()
    if err:
        raise BoomboxServiceError(err)
    media_dir = _media_dir(mount_path)
    file_path = _safe_file_path(media_dir, safe_name)
    if not os.path.isfile(file_path):
        raise BoomboxServiceError("File not found.")
    return file_path
