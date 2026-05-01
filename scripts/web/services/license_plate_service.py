"""Service layer for custom license plate image management.

This module contains functions for managing Tesla custom license plate PNG images.
Tesla requirements:
- Resolution: 420x100 pixels (recommended)
- File Size: Max 1 MB
- File Name: Alphanumeric, underscores, dashes, spaces only (max 30 chars)
- File Format: PNG only
- File Count: Up to 10 images at a time
- Folder: 'LicensePlate' at root level of part2 (LightShow drive)
"""

import os
import re
import shutil
import logging
import tempfile
import struct

logger = logging.getLogger(__name__)

# License plate folder name (at root of USB drive, part2)
LICENSE_PLATE_FOLDER = "LicensePlate"

# Tesla requirements
MAX_PLATE_SIZE = 1 * 1024 * 1024  # 1 MB
EXPECTED_WIDTH = 420
EXPECTED_HEIGHT = 100
MAX_FILENAME_LENGTH = 30
MAX_PLATE_COUNT = 10

# Valid filename pattern (alphanumeric, underscores, dashes, spaces)
VALID_FILENAME_PATTERN = re.compile(r'^[a-zA-Z0-9_\- ]+$')


def get_png_dimensions(file_path):
    """
    Get dimensions of a PNG file by reading the header.

    Args:
        file_path: Path to the PNG file

    Returns:
        (width, height) tuple or (None, None) if not a valid PNG
    """
    try:
        with open(file_path, 'rb') as f:
            signature = f.read(8)
            if signature != b'\x89PNG\r\n\x1a\n':
                return None, None

            struct.unpack('>I', f.read(4))[0]  # chunk length
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
    """
    Get dimensions of a PNG file from bytes.

    Args:
        file_bytes: PNG file as bytes

    Returns:
        (width, height) tuple or (None, None) if not a valid PNG
    """
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
    """
    Validate license plate filename.

    Args:
        filename: The filename to validate (without path)

    Returns:
        (is_valid: bool, error_message: str or None)
    """
    if not filename.lower().endswith('.png'):
        return False, "Only PNG files are allowed"

    base_name = os.path.splitext(filename)[0]

    if len(base_name) > MAX_FILENAME_LENGTH:
        return False, f"Filename must be {MAX_FILENAME_LENGTH} characters or less (currently {len(base_name)})"

    if len(base_name) == 0:
        return False, "Filename cannot be empty"

    if not VALID_FILENAME_PATTERN.match(base_name):
        return False, "Filename can only contain letters, numbers, underscores, dashes, and spaces"

    return True, None


def validate_plate_file(file_bytes, filename):
    """
    Validate a license plate file.

    Dimensions are not hard-enforced — Tesla recommends 420x100 but any
    PNG within the size limit is accepted. A warning is returned if the
    dimensions differ from the recommended size.

    Args:
        file_bytes: The file content as bytes
        filename: The filename

    Returns:
        (is_valid: bool, error_message: str or None, dimensions: tuple or None, warning: str or None)
    """
    is_valid, error = validate_plate_filename(filename)
    if not is_valid:
        return False, error, None, None

    if len(file_bytes) > MAX_PLATE_SIZE:
        size_mb = len(file_bytes) / (1024 * 1024)
        return False, f"File size must be 1 MB or less (got {size_mb:.2f} MB)", None, None

    width, height = get_png_dimensions_from_bytes(file_bytes)
    if width is None or height is None:
        return False, "Could not read image dimensions - file may be corrupted or not a valid PNG", None, None

    warning = None
    if width != EXPECTED_WIDTH or height != EXPECTED_HEIGHT:
        warning = (
            f"Image is {width}x{height}px. "
            f"Tesla recommends {EXPECTED_WIDTH}x{EXPECTED_HEIGHT}px for best results."
        )

    return True, None, (width, height), warning


def upload_plate_file(uploaded_file, filename, part2_mount_path=None):
    """
    Upload a license plate PNG file to the LicensePlate/ folder.

    Mode-aware: uses quick_edit_part2 in Present mode, direct write in Edit mode.

    Args:
        uploaded_file: Flask file object from request.files
        filename: Name of the file to save
        part2_mount_path: Current mount path for part2 (RO or RW), can be None in present mode

    Returns:
        (success: bool, message: str, dimensions: tuple or None, warning: str or None)
    """
    from services.mode_service import current_mode
    from services.partition_mount_service import quick_edit_part2
    from config import MNT_DIR

    mode = current_mode()
    logger.info(f"Uploading license plate file {filename} (mode: {mode})")

    file_bytes = uploaded_file.read()
    uploaded_file.seek(0)

    is_valid, error, dimensions, warning = validate_plate_file(file_bytes, filename)
    if not is_valid:
        return False, error, None, None

    filename = os.path.basename(filename)

    if mode == 'present':
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
                    logger.error(f"Error copying plate file: {e}", exc_info=True)
                    return False, f"Error copying file: {str(e)}"

            logger.info("Using quick edit part2 for license plate upload")
            success, copy_msg = quick_edit_part2(_do_quick_copy, timeout=30)

            shutil.rmtree(temp_dir)

            if success:
                dim_str = f"{dimensions[0]}x{dimensions[1]}"
                return True, f"Successfully uploaded {filename} ({dim_str})", dimensions, warning
            else:
                return False, copy_msg, None, None

        except Exception as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.error(f"Error uploading license plate: {e}", exc_info=True)
            return False, f"Error uploading file: {str(e)}", None, None

    else:
        try:
            if not part2_mount_path:
                return False, "Part2 mount path required in edit mode", None, None

            plates_dir = os.path.join(part2_mount_path, LICENSE_PLATE_FOLDER)

            if not os.path.isdir(plates_dir):
                os.makedirs(plates_dir, exist_ok=True)

            dest_path = os.path.join(plates_dir, filename)
            with open(dest_path, 'wb') as f:
                f.write(file_bytes)

            dim_str = f"{dimensions[0]}x{dimensions[1]}"
            return True, f"Successfully uploaded {filename} ({dim_str})", dimensions, warning

        except Exception as e:
            logger.error(f"Error uploading license plate: {e}", exc_info=True)
            return False, f"Error uploading file: {str(e)}", None, None


def delete_plate_file(filename, part2_mount_path=None):
    """
    Delete a license plate PNG file.

    Mode-aware: uses quick_edit_part2 in Present mode, direct delete in Edit mode.

    Args:
        filename: Name of the license plate file to delete
        part2_mount_path: Current mount path for part2 (RO or RW), can be None in present mode

    Returns:
        (success: bool, message: str)
    """
    from services.mode_service import current_mode
    from services.partition_mount_service import quick_edit_part2
    from config import MNT_DIR

    mode = current_mode()
    logger.info(f"Deleting license plate file {filename} (mode: {mode})")

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
                logger.info(f"Deleted {filename}")
                return True, f"Deleted {filename}"
            else:
                return False, "File not found"

        except Exception as e:
            logger.error(f"Error deleting license plate: {e}", exc_info=True)
            return False, f"Error deleting file: {str(e)}"

    if mode == 'present':
        logger.info("Using quick edit part2 for license plate deletion")
        return quick_edit_part2(_do_delete)
    else:
        return _do_delete()


def get_plate_count(mount_path):
    """
    Get the current count of license plate files.

    Args:
        mount_path: Mount path for part2

    Returns:
        int: Number of license plate PNG files
    """
    if not mount_path:
        return 0

    plates_dir = os.path.join(mount_path, LICENSE_PLATE_FOLDER)
    if not os.path.isdir(plates_dir):
        return 0

    try:
        return sum(1 for entry in os.listdir(plates_dir) if entry.lower().endswith('.png'))
    except OSError:
        return 0


def list_plate_files(mount_path):
    """
    List all license plate files in the LicensePlate folder.

    Args:
        mount_path: Mount path for part2

    Returns:
        List of plate file info dictionaries
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

                files.append({
                    'filename': entry,
                    'size': size,
                    'width': width,
                    'height': height,
                    'path': full_path,
                })
            except OSError as e:
                logger.warning(f"Could not read license plate file {entry}: {e}")
                continue

        files.sort(key=lambda x: x['filename'].lower())

    except OSError as e:
        logger.error(f"Error listing license plate files: {e}")

    return files
