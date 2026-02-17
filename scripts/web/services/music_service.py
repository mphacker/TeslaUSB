#!/usr/bin/env python3
"""Music upload and management helpers."""

import os
import uuid
import shutil
import logging
from typing import Tuple, List

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


def _normalize_rel_path(rel_path: str) -> str:
    rel = (rel_path or "").strip("/")
    if not rel:
        return ""
    cleaned: List[str] = []
    for segment in rel.split("/"):
        segment = segment.strip()
        if segment in {"", ".", ".."}:
            continue
        safe_seg = secure_filename(segment)
        if not safe_seg:
            raise UploadError("Invalid folder name")
        cleaned.append(safe_seg)
    return "/".join(cleaned)


def _resolve_subpath(mount_path: str, rel_path: str) -> str:
    """Return an absolute path within the music mount for the given relative path."""
    rel = _normalize_rel_path(rel_path)
    if not rel:
        return mount_path
    target = os.path.join(mount_path, rel)
    common = os.path.commonpath([mount_path, target])
    if common != os.path.abspath(mount_path):
        raise UploadError("Invalid path")
    return target


def _ensure_music_mount() -> Tuple[str, str]:
    """Return the music mount path or an error string."""
    mount_path = get_mount_path("part3")
    if not mount_path:
        return "", "Music drive not mounted. Switch to Edit mode and try again."
    if not os.path.isdir(mount_path):
        return "", "Music drive is unavailable."
    return mount_path, ""


def _get_music_root(mount_path: str) -> Tuple[str, str]:
    """Ensure the Tesla-required Music folder exists and return its path."""
    music_root = os.path.join(mount_path, "Music")
    try:
        os.makedirs(music_root, exist_ok=True)
    except OSError as exc:
        logger.error("Unable to create Music folder: %s", exc)
        return "", "Unable to access Music folder"
    return music_root, ""


def _validate_filename(name: str) -> str:
    safe = secure_filename(name)
    if not safe:
        raise UploadError("Invalid filename")
    ext = os.path.splitext(safe)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise UploadError("Unsupported file type. Allowed: mp3, flac, wav, aac, m4a")
    return safe


def list_music_files(rel_path: str = ""):
    mount_path, err = _ensure_music_mount()
    if err:
        return [], [], err, 0, 0, "", 0

    music_root, err = _get_music_root(mount_path)
    if err:
        return [], [], err, 0, 0, "", 0

    try:
        current_rel = _normalize_rel_path(rel_path)
    except UploadError as exc:
        return [], [], str(exc), 0, 0, "", 0

    try:
        stat = os.statvfs(mount_path)
        total_bytes = stat.f_blocks * stat.f_frsize
        free_bytes = stat.f_bavail * stat.f_frsize
        used_bytes = max(0, total_bytes - (stat.f_bfree * stat.f_frsize))
    except OSError as exc:
        logger.warning("Could not stat music mount: %s", exc)
        return [], [], "Music drive unavailable", 0, 0, current_rel, 0

    target_dir = _resolve_subpath(music_root, current_rel)
    if not os.path.isdir(target_dir):
        return [], [], "Folder not found", used_bytes, free_bytes, current_rel, total_bytes

    dirs = []
    music_files = []
    total_size = 0
    try:
        for entry in os.scandir(target_dir):
            if entry.name.startswith('.'):
                # Skip temp/upload internals and hidden items
                continue
            if entry.is_dir():
                dirs.append({
                    "name": entry.name,
                    "path": f"{current_rel + '/' if current_rel else ''}{entry.name}",
                })
                continue
            if not entry.is_file():
                continue
            ext = os.path.splitext(entry.name)[1].lower()
            if ext not in ALLOWED_EXTS:
                continue
            stat = entry.stat()
            rel_file = f"{current_rel + '/' if current_rel else ''}{entry.name}"
            music_files.append({
                "name": entry.name,
                "path": rel_file,
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            })
            total_size += stat.st_size
    except OSError as e:
        logger.warning("Could not read music directory: %s", e)
        return [], [], "Unable to read music directory", used_bytes, free_bytes, current_rel, total_bytes

    dirs.sort(key=lambda x: x["name"].lower())
    music_files.sort(key=lambda x: x["name"].lower())
    return dirs, music_files, "", used_bytes, free_bytes, current_rel, total_bytes


def _prepare_paths(filename: str, music_dir: str):
    tmp_dir = os.path.join(music_dir, ".uploads")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_path = os.path.join(tmp_dir, f"{filename}.upload")
    final_path = os.path.join(music_dir, filename)
    return tmp_dir, tmp_path, final_path


def save_file(file_storage, rel_path: str = "") -> Tuple[bool, str]:
    """Stream a Werkzeug FileStorage to the music partition with fsync + atomic rename."""
    mount_path, err = _ensure_music_mount()
    if err:
        return False, err

    music_root, err = _get_music_root(mount_path)
    if err:
        return False, err

    target_dir = _resolve_subpath(music_root, rel_path)
    if not os.path.isdir(target_dir):
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError:
            return False, "Target folder unavailable"

    filename = _validate_filename(file_storage.filename)
    tmp_dir, tmp_path, final_path = _prepare_paths(filename, target_dir)

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
    _fsync_dir(target_dir)
    try:
        close_samba_share("part3")
    except Exception:
        pass
    return True, f"Uploaded {filename}"


def handle_chunk(upload_id: str, filename: str, chunk_index: int, total_chunks: int, total_size: int, stream, rel_path: str = "") -> Tuple[bool, str, bool]:
    """
    Append a chunk to the staged upload file.

    Returns (success, message, is_finalized)
    """
    if total_size > MAX_UPLOAD_BYTES:
        raise UploadError(f"File too large (>{MAX_UPLOAD_SIZE_MB} MiB limit)")

    mount_path, err = _ensure_music_mount()
    if err:
        return False, err, False

    music_root, err = _get_music_root(mount_path)
    if err:
        return False, err, False

    target_dir = _resolve_subpath(music_root, rel_path)
    os.makedirs(target_dir, exist_ok=True)

    filename = _validate_filename(filename)
    tmp_dir = os.path.join(target_dir, ".uploads")
    os.makedirs(tmp_dir, exist_ok=True)
    staged_path = os.path.join(tmp_dir, f"{upload_id}.part")
    final_path = os.path.join(target_dir, filename)

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
    _fsync_dir(target_dir)

    try:
        close_samba_share("part3")
    except Exception:
        pass

    return True, f"Uploaded {filename}", True


def delete_music_file(rel_path: str) -> Tuple[bool, str]:
    mount_path, err = _ensure_music_mount()
    if err:
        return False, err

    music_root, err = _get_music_root(mount_path)
    if err:
        return False, err

    target_path = _resolve_subpath(music_root, rel_path)
    filename = os.path.basename(target_path)
    if not os.path.isfile(target_path):
        return False, "File not found"

    try:
        os.remove(target_path)
        _fsync_dir(os.path.dirname(target_path))
        close_samba_share("part3")
    except Exception as exc:
        logger.error("Failed to delete %s: %s", filename, exc)
        return False, "Unable to delete file"
    return True, f"Deleted {filename}"


def create_directory(rel_path: str, name: str) -> Tuple[bool, str]:
    mount_path, err = _ensure_music_mount()
    if err:
        return False, err

    music_root, err = _get_music_root(mount_path)
    if err:
        return False, err

    base_dir = _resolve_subpath(music_root, rel_path)
    safe_name = secure_filename(name or "")
    if not safe_name:
        return False, "Invalid folder name"

    target_dir = os.path.join(base_dir, safe_name)

    try:
        os.makedirs(target_dir, exist_ok=False)
        _fsync_dir(base_dir)
    except FileExistsError:
        return False, "Folder already exists"
    except Exception:
        return False, "Could not create folder"
    return True, f"Created folder {safe_name}"


def delete_directory(rel_path: str) -> Tuple[bool, str]:
    mount_path, err = _ensure_music_mount()
    if err:
        return False, err

    music_root, err = _get_music_root(mount_path)
    if err:
        return False, err

    if not rel_path:
        return False, "Invalid folder path"

    target_dir = _resolve_subpath(music_root, rel_path)
    if os.path.abspath(target_dir) == os.path.abspath(music_root):
        return False, "Cannot delete root folder"
    if not os.path.isdir(target_dir):
        return False, "Folder not found"

    try:
        shutil.rmtree(target_dir)
        _fsync_dir(os.path.dirname(target_dir))
        close_samba_share("part3")
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to delete folder %s: %s", rel_path, exc)
        return False, "Unable to delete folder"
    return True, "Deleted folder"


def move_music_file(source_rel: str, dest_rel: str, new_name: str = "") -> Tuple[bool, str]:
    mount_path, err = _ensure_music_mount()
    if err:
        return False, err

    music_root, err = _get_music_root(mount_path)
    if err:
        return False, err

    src_path = _resolve_subpath(music_root, source_rel)
    if not os.path.isfile(src_path):
        return False, "Source file not found"

    dest_dir = _resolve_subpath(music_root, dest_rel)
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError:
        return False, "Destination unavailable"

    dest_name = _validate_filename(new_name) if new_name else os.path.basename(src_path)
    dest_path = os.path.join(dest_dir, dest_name)

    try:
        os.replace(src_path, dest_path)
        _fsync_dir(os.path.dirname(src_path))
        _fsync_dir(dest_dir)
        close_samba_share("part3")
    except Exception as exc:
        logger.error("Failed to move %s -> %s: %s", src_path, dest_path, exc)
        return False, "Unable to move file"
    return True, f"Moved to {dest_name}"


def require_edit_mode():
    if current_mode() != "edit":
        raise UploadError("Switch to Edit mode to upload music.")


def generate_upload_id() -> str:
    return uuid.uuid4().hex
