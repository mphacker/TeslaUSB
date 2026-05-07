"""Service layer for custom license-plate background management.

Tesla supports custom PNG license-plate background images on
Cybertruck (2024 Holiday Update), Model Y/3 (2025 firmware), and is
rolling out to S/X. The car reads PNG files from a ``LicensePlate/``
folder at the root of any attached USB drive.

Tesla requirements (community-verified at
https://github.com/teslamotors/custom-wraps/issues/13):

- Folder: ``LicensePlate`` at USB root (case-sensitive, no trailing s)
- Format: PNG only
- Dimensions (NA): 420 x 200 px
- Dimensions (EU/Italy): 420 x 100 px
- Max file size: 0.5 MB (512 KB)
- Filename: alphanumeric only, <= 32 characters (no spaces, dashes, underscores)
- Max files: 10

The service mirrors :mod:`services.wrap_service` but with the
plate-specific constants and a stricter dimension validator. It also
incorporates the three fixes called out in issue #58 from day one:

1. Count check reads from the RO mount in present mode (no silent
   bypass when ``part2_mount_path`` is ``None``).
2. No unconditional ``time.sleep`` after writes — the blueprint and
   service finish their work synchronously.
3. After a successful present-mode write or delete, the USB gadget is
   unbound/rebound via :func:`services.wrap_service.safe_rebind_usb_gadget`
   so Tesla picks up the new plate without a reboot.
"""

import os
import re
import shutil
import logging
import tempfile
import struct

logger = logging.getLogger(__name__)

# License-plate folder name (at the root of the USB drive)
LICENSE_PLATE_FOLDER = "LicensePlate"

# Tesla requirements
MAX_PLATE_SIZE = 512 * 1024  # 512 KB ("0.5 MB")
MAX_PLATE_COUNT = 10
MAX_FILENAME_LENGTH = 32

# Tesla-allowed dimensions for license-plate backgrounds.
PLATE_DIMENSIONS_NA = (420, 200)
PLATE_DIMENSIONS_EU = (420, 100)
ALLOWED_PLATE_DIMENSIONS = (PLATE_DIMENSIONS_NA, PLATE_DIMENSIONS_EU)

# Strict alphanumeric pattern — no underscores, dashes, or spaces.
# This is intentionally tighter than the wraps pattern; Tesla's plate
# parser rejects anything that isn't [A-Za-z0-9]+ before the .png.
VALID_FILENAME_PATTERN = re.compile(r'^[A-Za-z0-9]+$')


def get_png_dimensions(file_path):
    """Return ``(width, height)`` for a PNG on disk, or ``(None, None)``.

    Reads only the 8-byte signature and the IHDR chunk header, so it
    works on multi-megabyte files without loading the whole image.
    """
    try:
        with open(file_path, 'rb') as f:
            signature = f.read(8)
            if signature != b'\x89PNG\r\n\x1a\n':
                return None, None
            # IHDR chunk: 4 bytes length, 4 bytes type, then data
            f.read(4)  # length, ignored
            chunk_type = f.read(4)
            if chunk_type != b'IHDR':
                return None, None
            width = struct.unpack('>I', f.read(4))[0]
            height = struct.unpack('>I', f.read(4))[0]
            return width, height
    except Exception as e:
        logger.error(f"Error reading PNG dimensions: {e}")
        return None, None


def get_png_dimensions_from_bytes(file_bytes):
    """Return ``(width, height)`` from a PNG byte buffer, or ``(None, None)``."""
    try:
        if file_bytes[:8] != b'\x89PNG\r\n\x1a\n':
            return None, None
        chunk_type = file_bytes[12:16]
        if chunk_type != b'IHDR':
            return None, None
        width = struct.unpack('>I', file_bytes[16:20])[0]
        height = struct.unpack('>I', file_bytes[20:24])[0]
        return width, height
    except Exception as e:
        logger.error(f"Error reading PNG dimensions from bytes: {e}")
        return None, None


def validate_plate_filename(filename):
    """Validate a license-plate filename.

    Returns ``(is_valid, error_message)``.
    """
    if not filename.lower().endswith('.png'):
        return False, "Only PNG files are allowed"

    base_name = os.path.splitext(filename)[0]

    if len(base_name) == 0:
        return False, "Filename cannot be empty"

    if len(base_name) > MAX_FILENAME_LENGTH:
        return False, (
            f"Filename must be {MAX_FILENAME_LENGTH} characters or less "
            f"(currently {len(base_name)})"
        )

    if not VALID_FILENAME_PATTERN.match(base_name):
        return False, (
            "Filename can only contain letters and numbers "
            "(no spaces, dashes, or underscores)"
        )

    return True, None


def validate_plate_dimensions(width, height):
    """Validate license-plate dimensions.

    Tesla accepts only two exact sizes: 420x200 (NA) or 420x100 (EU).
    The smart-resize cropper in the UI is responsible for producing
    one of these — by the time bytes hit the server they must match.

    Returns ``(is_valid, error_message)``.
    """
    if width is None or height is None:
        return False, "Could not read image dimensions - file may be corrupted"

    if (width, height) not in ALLOWED_PLATE_DIMENSIONS:
        return False, (
            f"License plate must be exactly 420x200 (NA) or 420x100 (EU); "
            f"got {width}x{height}. Use the in-browser cropper to produce "
            f"a compliant size."
        )

    return True, None


def validate_plate_file(file_bytes, filename):
    """Validate a license-plate file end-to-end.

    Returns ``(is_valid, error_message, dimensions_or_None)``.
    """
    is_valid, error = validate_plate_filename(filename)
    if not is_valid:
        return False, error, None

    if len(file_bytes) > MAX_PLATE_SIZE:
        size_kb = len(file_bytes) / 1024
        return False, (
            f"File size must be 512 KB or less (got {size_kb:.1f} KB). "
            f"Try a simpler image or reduce the color depth."
        ), None

    width, height = get_png_dimensions_from_bytes(file_bytes)
    is_valid, error = validate_plate_dimensions(width, height)
    if not is_valid:
        return False, error, None

    return True, None, (width, height)


def _safe_rebind_usb_gadget():
    """Rebind the USB gadget so Tesla notices the new plate.

    Delegates to :func:`services.wrap_service.safe_rebind_usb_gadget`
    so we share one helper. The function swallows failures (rebind
    issues are logged but never block the user-facing operation —
    the file is already on disk by the time we get here).
    """
    from services.wrap_service import safe_rebind_usb_gadget
    safe_rebind_usb_gadget()


# Re-exported for callers that want to call the helper directly
# (the blueprint uses this name in a batch-rebind path).
safe_rebind_usb_gadget = _safe_rebind_usb_gadget


def upload_plate_file(uploaded_file, filename, part2_mount_path=None,
                      defer_rebind=False):
    """Upload a license-plate PNG to the LicensePlate/ folder.

    Mode-aware:
        - In edit mode: writes directly to ``part2_mount_path``.
        - In present mode: uses ``quick_edit_part2()`` to temporarily
          mount RW. After a successful write the USB gadget is
          rebound (unless ``defer_rebind=True``) so Tesla invalidates
          its cache and shows the plate without a reboot.

    Args:
        uploaded_file: Flask ``FileStorage`` (provides ``read`` + ``seek``).
        filename: Destination filename (basename only — sanitized here).
        part2_mount_path: RW mount path for part2 in edit mode. Ignored
            in present mode (we use ``MNT_DIR/part2`` after quick_edit
            remounts it RW).
        defer_rebind: When ``True``, skip the post-write USB rebind so
            a bulk caller can issue one rebind for the whole batch.

    Returns:
        ``(success, message, dimensions_or_None)``
    """
    from services.mode_service import current_mode
    from services.partition_mount_service import quick_edit_part2
    from config import MNT_DIR

    mode = current_mode()
    logger.info(f"Uploading license plate {filename} (mode: {mode})")

    file_bytes = uploaded_file.read()
    uploaded_file.seek(0)

    is_valid, error, dimensions = validate_plate_file(file_bytes, filename)
    if not is_valid:
        return False, error, None

    # Strip any path components from a hostile filename before joining
    # it with the destination directory.
    filename = os.path.basename(filename)

    if mode == 'present':
        # Stage the bytes in a temp file so the quick-edit window stays
        # short (the RW remount is what's expensive — the copy itself
        # is sub-second).
        temp_dir = tempfile.mkdtemp(prefix='plate_upload_')
        try:
            temp_file_path = os.path.join(temp_dir, filename)
            with open(temp_file_path, 'wb') as f:
                f.write(file_bytes)

            def _do_quick_copy():
                try:
                    rw_mount = os.path.join(MNT_DIR, 'part2')
                    plates_dir = os.path.join(rw_mount, LICENSE_PLATE_FOLDER)
                    if not os.path.isdir(plates_dir):
                        os.makedirs(plates_dir, exist_ok=True)
                    dest_path = os.path.join(plates_dir, filename)
                    shutil.copy2(temp_file_path, dest_path)
                    return True, "File copied successfully"
                except Exception as e:
                    logger.error(f"Error copying plate: {e}", exc_info=True)
                    return False, f"Error copying file: {str(e)}"

            logger.info("Using quick edit part2 for license-plate upload")
            success, copy_msg = quick_edit_part2(_do_quick_copy, timeout=30)

            shutil.rmtree(temp_dir, ignore_errors=True)

            if success:
                if not defer_rebind:
                    _safe_rebind_usb_gadget()
                return True, (
                    f"Successfully uploaded {filename} "
                    f"({dimensions[0]}x{dimensions[1]})"
                ), dimensions
            return False, copy_msg, None

        except Exception as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.error(f"Error uploading plate: {e}", exc_info=True)
            return False, f"Error uploading file: {str(e)}", None

    # Edit mode — write directly. The gadget is unbound, so no rebind.
    def _do_upload():
        try:
            if not part2_mount_path:
                return False, "Part2 mount path required in edit mode", None
            plates_dir = os.path.join(part2_mount_path, LICENSE_PLATE_FOLDER)
            if not os.path.isdir(plates_dir):
                os.makedirs(plates_dir, exist_ok=True)
            dest_path = os.path.join(plates_dir, filename)
            with open(dest_path, 'wb') as f:
                f.write(file_bytes)
            return True, (
                f"Successfully uploaded {filename} "
                f"({dimensions[0]}x{dimensions[1]})"
            ), dimensions
        except Exception as e:
            logger.error(f"Error uploading plate: {e}", exc_info=True)
            return False, f"Error uploading file: {str(e)}", None

    return _do_upload()


def delete_plate_file(filename, part2_mount_path=None, defer_rebind=False):
    """Delete a license-plate PNG.

    Mode-aware (mirrors :func:`upload_plate_file`). Present-mode
    deletes go through ``quick_edit_part2`` and then rebind the USB
    gadget so Tesla drops the file from its cache.

    Returns ``(success, message)``.
    """
    from services.mode_service import current_mode
    from services.partition_mount_service import quick_edit_part2
    from config import MNT_DIR

    mode = current_mode()
    logger.info(f"Deleting license plate {filename} (mode: {mode})")

    # Sanitize — never let path separators leak into the join.
    filename = os.path.basename(filename)

    def _do_delete():
        try:
            if mode == 'present':
                rw_mount = os.path.join(MNT_DIR, 'part2')
            else:
                if not part2_mount_path:
                    return False, "Part2 mount path required in edit mode"
                rw_mount = part2_mount_path

            plates_dir = os.path.join(rw_mount, LICENSE_PLATE_FOLDER)
            file_path = os.path.join(plates_dir, filename)

            if os.path.isfile(file_path):
                os.remove(file_path)
                logger.info(f"Deleted plate {filename}")
                return True, f"Deleted {filename}"
            return False, "File not found"
        except Exception as e:
            logger.error(f"Error deleting plate: {e}", exc_info=True)
            return False, f"Error deleting file: {str(e)}"

    if mode == 'present':
        logger.info("Using quick edit part2 for license-plate deletion")
        success, msg = quick_edit_part2(_do_delete)
        if success and not defer_rebind:
            _safe_rebind_usb_gadget()
        return success, msg

    return _do_delete()


def get_plate_count(mount_path):
    """Count PNG files in the LicensePlate folder under ``mount_path``."""
    if not mount_path:
        return 0
    plates_dir = os.path.join(mount_path, LICENSE_PLATE_FOLDER)
    if not os.path.isdir(plates_dir):
        return 0
    try:
        return sum(
            1 for entry in os.listdir(plates_dir)
            if entry.lower().endswith('.png')
        )
    except OSError:
        return 0


def get_plate_count_any_mode():
    """Return the plate count from whichever mount is accessible.

    The original wraps blueprint passed ``None`` in present mode,
    which silently bypassed the count limit. This helper picks the
    right mount per current mode (RO in present, RW in edit) so the
    limit is enforced before any expensive ``quick_edit_part2`` call.

    Returns:
        int: Number of PNG plate files on the LightShow drive.
    """
    from services.mode_service import current_mode
    from config import MNT_DIR

    if current_mode() == 'present':
        mount_path = os.path.join(MNT_DIR, 'part2-ro')
    else:
        mount_path = os.path.join(MNT_DIR, 'part2')
    return get_plate_count(mount_path)


def list_plate_files(mount_path):
    """List every PNG in the LicensePlate folder with compliance metadata.

    Unlike the wraps listing, this surfaces non-compliant files (wrong
    dimensions, oversize, bad filename) with an ``issues`` array so
    users who dropped junk in via Samba can see it and clean up. Files
    are never silently filtered or hidden.

    Returns a list of dicts with keys: ``filename``, ``size``,
    ``width``, ``height``, ``path``, ``compliant`` (bool), ``issues``
    (list of human-readable strings).
    """
    if not mount_path:
        return []

    plates_dir = os.path.join(mount_path, LICENSE_PLATE_FOLDER)
    if not os.path.isdir(plates_dir):
        return []

    files = []
    try:
        for entry in os.listdir(plates_dir):
            if not entry.lower().endswith('.png'):
                continue

            full_path = os.path.join(plates_dir, entry)
            if not os.path.isfile(full_path):
                continue

            try:
                size = os.path.getsize(full_path)
                width, height = get_png_dimensions(full_path)
            except OSError as e:
                logger.warning(f"Could not read plate file {entry}: {e}")
                continue

            issues = []
            name_ok, name_err = validate_plate_filename(entry)
            if not name_ok:
                issues.append(name_err)
            if size > MAX_PLATE_SIZE:
                issues.append(
                    f"File is {size / 1024:.1f} KB (limit 512 KB)"
                )
            if width is None or height is None:
                issues.append("Could not read PNG dimensions")
            elif (width, height) not in ALLOWED_PLATE_DIMENSIONS:
                issues.append(
                    f"Dimensions {width}x{height} are not 420x200 (NA) "
                    f"or 420x100 (EU)"
                )

            files.append({
                'filename': entry,
                'size': size,
                'width': width,
                'height': height,
                'path': full_path,
                'compliant': len(issues) == 0,
                'issues': issues,
            })

        files.sort(key=lambda x: x['filename'].lower())
    except OSError as e:
        logger.error(f"Error listing license-plate files: {e}")

    return files
