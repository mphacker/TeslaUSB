#!/usr/bin/env python3
"""Music upload and management helpers."""

import os
import uuid
import logging
from typing import Tuple

from werkzeug.utils import secure_filename

from config import MAX_UPLOAD_CHUNK_MB, MAX_UPLOAD_SIZE_MB
from services.partition_service import get_mount_path
from services.samba_service import close_samba_share
from services.mode_service import current_mode

logger = logging.getLogger(__name__)

# Allow common Tesla-friendly audio formats
ALLOWED_EXTS = {".mp3", ".flac", ".wav", ".aac", ".m4a"}
CHUNK_SIZE = MAX_UPLOAD_CHUNK_MB * 1024 * 1024
MAX_UPLOAD_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024


class UploadError(Exception):
    """Raised for user-facing upload errors."""


def _fs_free_bytes(path: str) -> int:
    """Return available bytes for the filesystem containing path."""
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize


def _fsync_path(path: str) -> None:
    """fsync a file path safely."""
    with open(path, "rb") as fh:
        os.fsync(fh.fileno())


def _fsync_dir(path: str) -> None:
    """fsync a directory to persist renames."""
    fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _stream_to_file(stream, dest_path: str) -> int:
    """Stream request data to dest_path without loading into memory."""
    total = 0
    with open(dest_path, "ab", buffering=0) as fh:
        while True:
            chunk = stream.read(CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            fh.write(chunk)
    return total


def _ensure_music_mount() -> Tuple[str, str]:
    """Return the music mount path or an error string."""
    mount_path = get_mount_path("part3")
    if not mount_path:
        return "", "Music drive not mounted. Switch to Edit mode and try again."
    if not os.path.isdir(mount_path):
        return "", "Music drive is unavailable."
    return mount_path, ""


def _validate_filename(name: str) -> str:
    safe = secure_filename(name)
    if not safe:
        raise UploadError("Invalid filename")
    ext = os.path.splitext(safe)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise UploadError("Unsupported file type. Allowed: mp3, flac, wav, aac, m4a")
    return safe


def list_music_files():
    mount_path, err = _ensure_music_mount()
    if err:
        return [], err, 0, 0

    music_files = []
    total_size = 0
    try:
        for entry in os.scandir(mount_path):
            if not entry.is_file():
                continue
            ext = os.path.splitext(entry.name)[1].lower()
            if ext not in ALLOWED_EXTS:
                continue
            stat = entry.stat()
            music_files.append({
                "name": entry.name,
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            })
            total_size += stat.st_size
    except OSError as e:
        logger.warning("Could not read music directory: %s", e)
        return [], "Unable to read music directory", 0, 0

    free_bytes = _fs_free_bytes(mount_path)
    music_files.sort(key=lambda x: x["name"].lower())
    return music_files, "", total_size, free_bytes


def _prepare_paths(filename: str, mount_path: str):
    music_dir = mount_path
    tmp_dir = os.path.join(music_dir, ".uploads")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_path = os.path.join(tmp_dir, f"{filename}.upload")
    final_path = os.path.join(music_dir, filename)
    return tmp_dir, tmp_path, final_path


def save_file(file_storage) -> Tuple[bool, str]:
    """Stream a Werkzeug FileStorage to the music partition with fsync + atomic rename."""
    mount_path, err = _ensure_music_mount()
    if err:
        return False, err

    filename = _validate_filename(file_storage.filename)
    tmp_dir, tmp_path, final_path = _prepare_paths(filename, mount_path)

    # Free space check
    file_storage.stream.seek(0, os.SEEK_END)
    incoming_size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if incoming_size > MAX_UPLOAD_BYTES:
        return False, f"File too large (>{MAX_UPLOAD_SIZE_MB} MiB limit)"

    free_bytes = _fs_free_bytes(mount_path)
    if free_bytes <= incoming_size + (4 * 1024 * 1024):
        return False, "Not enough free space on Music drive"

    # Stream to temp then atomically move
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    _stream_to_file(file_storage.stream, tmp_path)
    _fsync_path(tmp_path)
    _fsync_dir(tmp_dir)

    os.replace(tmp_path, final_path)
    _fsync_dir(mount_path)
    try:
        close_samba_share("part3")
    except Exception:
        pass
    return True, f"Uploaded {filename}"


def handle_chunk(upload_id: str, filename: str, chunk_index: int, total_chunks: int, total_size: int, stream) -> Tuple[bool, str, bool]:
    """
    Append a chunk to the staged upload file.

    Returns (success, message, is_finalized)
    """
    if total_size > MAX_UPLOAD_BYTES:
        raise UploadError(f"File too large (>{MAX_UPLOAD_SIZE_MB} MiB limit)")

    mount_path, err = _ensure_music_mount()
    if err:
        return False, err, False

    filename = _validate_filename(filename)
    tmp_dir = os.path.join(mount_path, ".uploads")
    os.makedirs(tmp_dir, exist_ok=True)
    staged_path = os.path.join(tmp_dir, f"{upload_id}.part")
    final_path = os.path.join(mount_path, filename)

    # On first chunk, ensure space and clear any stale parts
    if chunk_index == 0:
        if os.path.exists(staged_path):
            os.remove(staged_path)
        free_bytes = _fs_free_bytes(mount_path)
        if free_bytes <= total_size + (4 * 1024 * 1024):
            raise UploadError("Not enough free space on Music drive")

    written = _stream_to_file(stream, staged_path)
    logger.debug("Chunk %s/%s wrote %s bytes", chunk_index + 1, total_chunks, written)

    if chunk_index < total_chunks - 1:
        return True, "Chunk stored", False

    # Final chunk: validate size then atomically move
    actual_size = os.path.getsize(staged_path)
    if actual_size != total_size:
        raise UploadError(f"Size mismatch. Expected {total_size} bytes, got {actual_size}")

    _fsync_path(staged_path)
    _fsync_dir(tmp_dir)
    os.replace(staged_path, final_path)
    _fsync_dir(mount_path)

    try:
        close_samba_share("part3")
    except Exception:
        pass

    return True, f"Uploaded {filename}", True


def delete_music_file(filename: str) -> Tuple[bool, str]:
    mount_path, err = _ensure_music_mount()
    if err:
        return False, err

    filename = _validate_filename(filename)
    target = os.path.join(mount_path, filename)
    if not os.path.isfile(target):
        return False, "File not found"

    try:
        os.remove(target)
        _fsync_dir(mount_path)
        close_samba_share("part3")
    except Exception as exc:
        logger.error("Failed to delete %s: %s", filename, exc)
        return False, "Unable to delete file"
    return True, f"Deleted {filename}"


def require_edit_mode():
    if current_mode() != "edit":
        raise UploadError("Switch to Edit mode to upload music.")


def generate_upload_id() -> str:
    return uuid.uuid4().hex
