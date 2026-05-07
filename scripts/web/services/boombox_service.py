#!/usr/bin/env python3
"""Boombox sound management for the Music drive (`/Boombox/` folder).

Tesla's Boombox feature plays user-supplied short audio clips through the
external pedestrian-warning speaker (PWS) when the vehicle is parked. Tesla
scans the `Boombox` folder at the root of any USB LUN. Per Tesla's spec:

* Folder name: ``Boombox`` (case-sensitive) at USB root.
* Formats: MP3 / WAV only.
* Tesla loads the first 5 files alphabetically; rest are ignored.
* Recommended ≤ 1 MB and ≤ 5 seconds per file.
* Hardware: vehicle must have a PWS (Model 3/Y/S/X built Sep 2019+, all
  Cybertruck).
* Per NHTSA recall 22V-068 (Feb 2022) custom Boombox sounds may only play
  while the vehicle is in Park.

This module is a *thin focused* wrapper around the existing music-drive
plumbing — it deliberately reuses ``music_service``'s atomic-write helpers
and ``partition_mount_service.quick_edit_part3`` rather than duplicating
them, and it calls ``wrap_service.safe_rebind_usb_gadget`` after
present-mode writes so Tesla picks up the new sound without a reboot.
"""

import logging
import os
import re
import shutil
import tempfile
from typing import List, Tuple

from config import MNT_DIR
from services.mode_service import current_mode
from services.partition_mount_service import quick_edit_part3
from services.samba_service import close_samba_share
# Helpers are imported (not duplicated) from music_service.
from services.music_service import _fsync_dir, _fsync_path, _sanitize_name
# Generic USB-rebind helper made public in PR #60. Boombox uses the same
# Tesla cache invalidation pattern as wraps.
from services.wrap_service import safe_rebind_usb_gadget

logger = logging.getLogger(__name__)


def _safe_fsync_path(path: str) -> None:
    """Best-effort ``_fsync_path`` that swallows OSError.

    fsync is a Linux-side crash-safety measure for the Pi. On platforms
    without a working file fsync (test runners on Windows, weird
    filesystems) we don't want a missing fsync to fail the upload —
    the file is already written by ``open()`` + ``write()``.
    """
    try:
        _fsync_path(path)
    except OSError:
        pass


def _safe_fsync_dir(path: str) -> None:
    """Best-effort ``_fsync_dir`` that swallows OSError / AttributeError.

    Directory fsync uses ``os.O_DIRECTORY`` which only exists on
    POSIX systems. On the Pi this still gives us crash-consistency on
    renames; on Windows test runners it's a no-op.
    """
    try:
        _fsync_dir(path)
    except (OSError, AttributeError):
        pass

# Tesla constants — do NOT change these without checking Tesla's spec.
BOOMBOX_FOLDER = "Boombox"
MAX_FILE_COUNT = 5
MAX_FILE_SIZE = 1 * 1024 * 1024  # 1 MiB
ALLOWED_EXTS = {".mp3", ".wav"}
MAX_FILENAME_LENGTH = 64

# Filesystem-safe filename pattern. Tesla doesn't enforce a regex but we
# do, so pre-existing filenames created via Samba that contain weird
# characters (slashes, colons, control chars) get refused.
VALID_FILENAME_PATTERN = re.compile(r'^[A-Za-z0-9 _\-.]+$')


class BoomboxServiceError(Exception):
    """Raised for user-facing boombox service errors."""


def _looks_like_wav(file_bytes: bytes) -> bool:
    """Return True if the bytes look like a RIFF/WAVE file."""
    return (
        len(file_bytes) >= 12
        and file_bytes[0:4] == b'RIFF'
        and file_bytes[8:12] == b'WAVE'
    )


def _looks_like_mp3(file_bytes: bytes) -> bool:
    """Return True if the bytes look like an MP3 (ID3 tag or frame sync).

    MP3 files start with either an ``ID3`` tag header or directly with an
    MPEG audio frame, which begins with a frame-sync byte (``0xFF``)
    followed by a byte whose top three bits are set (``0xE0`` mask).
    """
    if len(file_bytes) < 3:
        return False
    if file_bytes[:3] == b'ID3':
        return True
    return (
        len(file_bytes) >= 2
        and file_bytes[0] == 0xFF
        and (file_bytes[1] & 0xE0) == 0xE0
    )


def validate_boombox_filename(filename: str) -> Tuple[bool, str]:
    """Validate the filename against Tesla and filesystem rules.

    Returns ``(is_valid, error_message)``. The error string is empty when
    valid (suitable for direct use in flash messages).
    """
    safe = _sanitize_name(filename or "")
    if not safe:
        return False, "Filename cannot be empty"

    # Length check applies to the whole filename including extension —
    # the cap is generous enough (64 chars) that cutting off the
    # extension would be confusing.
    if len(safe) > MAX_FILENAME_LENGTH:
        return False, (
            f"Filename must be {MAX_FILENAME_LENGTH} characters or less "
            f"(currently {len(safe)})"
        )

    ext = os.path.splitext(safe)[1].lower()
    if ext not in ALLOWED_EXTS:
        return False, "Only MP3 and WAV files are allowed"

    # Reject path-traversal-ish or non-printable characters. _sanitize_name
    # already strips the worst offenders, so this catches things like
    # bracketed/escaped names that survived sanitization.
    if not VALID_FILENAME_PATTERN.match(safe):
        return False, (
            "Filename can only contain letters, numbers, spaces, "
            "underscores, dashes, and dots"
        )

    return True, ""


def validate_boombox_file(file_bytes: bytes, filename: str) -> Tuple[bool, str]:
    """Validate filename, size, and magic-bytes for a candidate file.

    Returns ``(is_valid, error_message)``. Used by both the route layer
    (to reject early before touching the filesystem) and any future
    "scan existing files for compliance" feature.
    """
    ok, err = validate_boombox_filename(filename)
    if not ok:
        return False, err

    if len(file_bytes) > MAX_FILE_SIZE:
        size_mb = len(file_bytes) / (1024 * 1024)
        return False, (
            f"File size must be 1 MB or less (got {size_mb:.2f} MB)"
        )

    if len(file_bytes) == 0:
        return False, "File is empty"

    ext = os.path.splitext(filename)[1].lower()
    if ext == ".wav":
        if not _looks_like_wav(file_bytes):
            return False, "File does not appear to be a valid WAV"
    elif ext == ".mp3":
        if not _looks_like_mp3(file_bytes):
            return False, "File does not appear to be a valid MP3"

    return True, ""


def _boombox_dir_for(mount_path: str) -> str:
    """Compose the boombox directory path under the given mount."""
    return os.path.join(mount_path, BOOMBOX_FOLDER)


def get_boombox_count(mount_path: str) -> int:
    """Count files in the boombox folder at ``mount_path``.

    Returns 0 if the mount is missing or the folder doesn't exist yet —
    those are normal "no sounds" states, not errors.
    """
    if not mount_path:
        return 0
    boombox_dir = _boombox_dir_for(mount_path)
    if not os.path.isdir(boombox_dir):
        return 0
    try:
        count = 0
        for entry in os.listdir(boombox_dir):
            full = os.path.join(boombox_dir, entry)
            if not os.path.isfile(full):
                continue
            ext = os.path.splitext(entry)[1].lower()
            if ext in ALLOWED_EXTS:
                count += 1
        return count
    except OSError as exc:
        logger.warning("Could not list boombox folder %s: %s",
                       boombox_dir, exc)
        return 0


def get_boombox_count_any_mode() -> int:
    """Return the boombox file count from whichever mount is accessible.

    Counting and writing are different concerns: writing in present mode
    requires a temporary RW remount via ``quick_edit_part3``, but counting
    only needs to read directory entries — the RO mount permanently held
    in present mode is sufficient and avoids a needless RW cycle. In edit
    mode the RW mount is the only mount.

    This mirrors ``wrap_service.get_wrap_count_any_mode`` and exists for
    the same reason: passing ``None`` (or the upload destination, which is
    ``None`` in present mode) to the basic counter would silently return
    0 and bypass ``MAX_FILE_COUNT`` enforcement entirely.
    """
    mode = current_mode()
    if mode == 'present':
        mount_path = os.path.join(MNT_DIR, 'part3-ro')
    else:
        mount_path = os.path.join(MNT_DIR, 'part3')
    return get_boombox_count(mount_path)


def get_all_boombox_files() -> List[dict]:
    """List boombox files with size and a per-file compliance flag.

    Reads from whichever mount is accessible in the current mode (RO in
    present, RW in edit). Each entry has ``filename``, ``size``,
    ``size_str``, ``valid``, and ``warning`` (empty when ``valid`` is
    ``True``). Files that exist on disk but violate Boombox rules are
    listed with a warning rather than hidden — users created them via
    Samba and need to know why Tesla isn't loading them.
    """
    mode = current_mode()
    if mode == 'present':
        mount_path = os.path.join(MNT_DIR, 'part3-ro')
    else:
        mount_path = os.path.join(MNT_DIR, 'part3')

    boombox_dir = _boombox_dir_for(mount_path)
    if not os.path.isdir(boombox_dir):
        return []

    results = []
    try:
        entries = sorted(os.listdir(boombox_dir), key=str.lower)
    except OSError as exc:
        logger.warning("Could not list boombox folder %s: %s",
                       boombox_dir, exc)
        return []

    for name in entries:
        full = os.path.join(boombox_dir, name)
        if not os.path.isfile(full):
            continue
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        ext = os.path.splitext(name)[1].lower()

        warning = ""
        # Filename and extension checks first — magic-byte sniff is too
        # expensive to run on every file in the listing path, so we only
        # do header checks at upload time. Filename / size / extension
        # cover the most common pre-existing problems.
        ok, err = validate_boombox_filename(name)
        if not ok:
            warning = err
        elif ext not in ALLOWED_EXTS:
            warning = "Only MP3 and WAV files are allowed"
        elif size > MAX_FILE_SIZE:
            size_mb = size / (1024 * 1024)
            warning = (f"File size must be 1 MB or less (got {size_mb:.2f} MB)")

        results.append({
            'filename': name,
            'size': size,
            'valid': not warning,
            'warning': warning,
        })

    return results


def resolve_boombox_file_path(filename: str) -> str:
    """Return the absolute filesystem path for a boombox file.

    Raises ``BoomboxServiceError`` if the drive is not mounted, the
    filename is invalid, or the file doesn't exist. The returned path is
    always inside the mode-appropriate boombox folder — the basename is
    re-derived after sanitization to defeat path traversal regardless of
    what the route layer passed in.
    """
    safe = _sanitize_name(filename or "")
    if not safe or os.sep in safe or '/' in safe or '\\' in safe:
        raise BoomboxServiceError("Invalid filename")

    ext = os.path.splitext(safe)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise BoomboxServiceError("Unsupported file type")

    mode = current_mode()
    if mode == 'present':
        mount_path = os.path.join(MNT_DIR, 'part3-ro')
    else:
        mount_path = os.path.join(MNT_DIR, 'part3')

    boombox_dir = _boombox_dir_for(mount_path)
    file_path = os.path.join(boombox_dir, safe)
    # Belt-and-suspenders: confirm the resolved path is still inside the
    # boombox folder after any symlink resolution.
    if os.path.commonpath([os.path.realpath(boombox_dir),
                           os.path.realpath(file_path)]) != \
            os.path.realpath(boombox_dir):
        raise BoomboxServiceError("Invalid path")

    if not os.path.isfile(file_path):
        raise BoomboxServiceError("File not found")
    return file_path


def upload_boombox_file(uploaded_file, filename: str) -> Tuple[bool, str]:
    """Validate and persist a boombox file. Mode-aware.

    In present mode uses ``quick_edit_part3`` for a short RW window plus a
    USB rebind so Tesla notices. In edit mode writes directly to the RW
    mount. Atomic write (temp-in-same-dir + fsync + ``os.replace``) is
    used in both modes so a crash mid-write can never leave a partial
    file that Tesla then tries to play.

    Enforces ``MAX_FILE_COUNT`` in **both** modes by reading the count
    from the RO mount in present mode — the same defense ``wrap_service``
    uses to avoid the silent-bypass bug fixed in PR #60.
    """
    safe_filename = _sanitize_name(filename or "")
    if not safe_filename:
        return False, "Invalid filename"

    # Read the whole file into memory once. The 1 MiB cap (MAX_FILE_SIZE)
    # is small enough that this is fine even on Pi Zero 2 W; it lets us
    # validate magic bytes before we touch the filesystem.
    file_bytes = uploaded_file.read()
    try:
        uploaded_file.seek(0)
    except Exception:
        # Some FileStorage-likes don't support seek; not fatal here
        # because we already have the bytes.
        pass

    ok, err = validate_boombox_file(file_bytes, safe_filename)
    if not ok:
        return False, err

    # Count check — refuse before doing the expensive RW cycle. Uses the
    # mode-aware helper so present mode reads the RO mount instead of
    # silently returning 0 for a None destination path.
    current_count = get_boombox_count_any_mode()
    if current_count >= MAX_FILE_COUNT:
        return False, (
            f"Maximum of {MAX_FILE_COUNT} Boombox sounds allowed. "
            "Delete one first."
        )

    mode = current_mode()
    logger.info("Uploading boombox file %s (mode: %s)", safe_filename, mode)

    def _do_save(rw_mount: str) -> Tuple[bool, str]:
        boombox_dir = _boombox_dir_for(rw_mount)
        try:
            os.makedirs(boombox_dir, exist_ok=True)
        except OSError as exc:
            logger.error("Cannot create boombox dir %s: %s",
                         boombox_dir, exc)
            return False, "Unable to access Boombox folder"

        final_path = os.path.join(boombox_dir, safe_filename)
        # Atomic write: tmp file in the same directory, fsync, replace,
        # fsync the parent directory so the rename survives a crash.
        tmp_path = os.path.join(boombox_dir, f".{safe_filename}.upload")
        try:
            with open(tmp_path, "wb") as fh:
                fh.write(file_bytes)
            _safe_fsync_path(tmp_path)
            _safe_fsync_dir(boombox_dir)
            os.replace(tmp_path, final_path)
            _safe_fsync_dir(boombox_dir)
        except OSError as exc:
            logger.error("Failed to write %s: %s", final_path, exc)
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            return False, "Failed to write file"
        try:
            close_samba_share("part3")
        except Exception:
            pass
        return True, f"Uploaded {safe_filename}"

    if mode == 'present':
        # Stage to /var/tmp first to keep the RW window tiny — opening
        # the destination file inside quick_edit_part3 is fine for a
        # 1 MiB write but mirrors the pattern used by wrap_service for
        # consistency. (Boombox files are smaller than wraps so we
        # could write directly, but the indirection is cheap.)
        temp_dir = tempfile.mkdtemp(prefix='boombox_upload_')
        try:
            staged = os.path.join(temp_dir, safe_filename)
            with open(staged, "wb") as fh:
                fh.write(file_bytes)

            def _quick_callback():
                rw_mount = os.path.join(MNT_DIR, 'part3')
                return _do_save(rw_mount)

            success, msg = quick_edit_part3(_quick_callback, timeout=30)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        if success:
            # Tesla caches the USB filesystem; without this the new
            # sound doesn't appear in the in-car Boombox menu until
            # the next natural rebind. Failure of the rebind itself
            # is non-fatal — the file is on disk and the safe_rebind
            # helper logs a warning for operators.
            safe_rebind_usb_gadget()
        return success, msg

    # Edit mode: gadget is unbound, no rebind needed.
    rw_mount = os.path.join(MNT_DIR, 'part3')
    return _do_save(rw_mount)


def delete_boombox_file(filename: str) -> Tuple[bool, str]:
    """Delete a boombox file. Mode-aware (mirrors upload)."""
    safe_filename = _sanitize_name(filename or "")
    if not safe_filename:
        return False, "Invalid filename"
    # Reject anything that resembles a path; the boombox folder is flat.
    if os.sep in safe_filename or '/' in safe_filename or '\\' in safe_filename:
        return False, "Invalid filename"

    mode = current_mode()
    logger.info("Deleting boombox file %s (mode: %s)", safe_filename, mode)

    def _do_delete(rw_mount: str) -> Tuple[bool, str]:
        boombox_dir = _boombox_dir_for(rw_mount)
        target = os.path.join(boombox_dir, safe_filename)
        if not os.path.isfile(target):
            return False, "File not found"
        try:
            os.remove(target)
            _safe_fsync_dir(boombox_dir)
        except OSError as exc:
            logger.error("Failed to delete %s: %s", target, exc)
            return False, "Failed to delete file"
        try:
            close_samba_share("part3")
        except Exception:
            pass
        return True, f"Deleted {safe_filename}"

    if mode == 'present':
        def _quick_callback():
            rw_mount = os.path.join(MNT_DIR, 'part3')
            return _do_delete(rw_mount)

        success, msg = quick_edit_part3(_quick_callback, timeout=30)
        if success:
            safe_rebind_usb_gadget()
        return success, msg

    rw_mount = os.path.join(MNT_DIR, 'part3')
    return _do_delete(rw_mount)
