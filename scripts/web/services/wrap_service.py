"""Service layer for custom wrap management.

This module contains functions for managing Tesla custom wrap PNG images.
Tesla requirements:
- Resolution: 512x512 to 1024x1024 pixels
- File Size: Max 1 MB
- File Name: Alphanumeric, underscores, dashes, spaces only (max 30 chars)
- File Format: PNG only
- File Count: Up to 10 images at a time
- Folder: 'Wraps' at root level
"""

import os
import re
import shutil
import logging
import tempfile
import struct
import zlib

logger = logging.getLogger(__name__)

# Wrap folder name (at root of USB drive)
WRAPS_FOLDER = "Wraps"

# Tesla requirements
MAX_WRAP_SIZE = 1 * 1024 * 1024  # 1 MB
MIN_DIMENSION = 512
MAX_DIMENSION = 1024
MAX_FILENAME_LENGTH = 30
MAX_WRAP_COUNT = 10

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
            # Read PNG signature (8 bytes)
            signature = f.read(8)
            if signature != b'\x89PNG\r\n\x1a\n':
                return None, None

            # Read IHDR chunk length (4 bytes) and type (4 bytes)
            chunk_length = struct.unpack('>I', f.read(4))[0]
            chunk_type = f.read(4)

            if chunk_type != b'IHDR':
                return None, None

            # Read width and height (4 bytes each)
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
        # Check PNG signature
        if file_bytes[:8] != b'\x89PNG\r\n\x1a\n':
            return None, None

        # Read IHDR chunk
        chunk_length = struct.unpack('>I', file_bytes[8:12])[0]
        chunk_type = file_bytes[12:16]

        if chunk_type != b'IHDR':
            return None, None

        # Read width and height
        width = struct.unpack('>I', file_bytes[16:20])[0]
        height = struct.unpack('>I', file_bytes[20:24])[0]

        return width, height
    except Exception as e:
        logger.error(f"Error reading PNG dimensions from bytes: {e}")
        return None, None


def validate_wrap_filename(filename):
    """
    Validate wrap filename according to Tesla requirements.

    Args:
        filename: The filename to validate (without path)

    Returns:
        (is_valid: bool, error_message: str or None)
    """
    # Check extension
    if not filename.lower().endswith('.png'):
        return False, "Only PNG files are allowed"

    # Get base name without extension
    base_name = os.path.splitext(filename)[0]

    # Check length
    if len(base_name) > MAX_FILENAME_LENGTH:
        return False, f"Filename must be {MAX_FILENAME_LENGTH} characters or less (currently {len(base_name)})"

    if len(base_name) == 0:
        return False, "Filename cannot be empty"

    # Check characters
    if not VALID_FILENAME_PATTERN.match(base_name):
        return False, "Filename can only contain letters, numbers, underscores, dashes, and spaces"

    return True, None


def validate_wrap_dimensions(width, height):
    """
    Validate wrap dimensions according to Tesla requirements.

    Args:
        width: Image width in pixels
        height: Image height in pixels

    Returns:
        (is_valid: bool, error_message: str or None)
    """
    if width is None or height is None:
        return False, "Could not read image dimensions - file may be corrupted"

    # Check if dimensions are within valid range
    if width < MIN_DIMENSION or height < MIN_DIMENSION:
        return False, f"Image dimensions must be at least {MIN_DIMENSION}x{MIN_DIMENSION} (got {width}x{height})"

    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        return False, f"Image dimensions must not exceed {MAX_DIMENSION}x{MAX_DIMENSION} (got {width}x{height})"

    return True, None


def validate_wrap_file(file_bytes, filename):
    """
    Validate a wrap file according to all Tesla requirements.

    Args:
        file_bytes: The file content as bytes
        filename: The filename

    Returns:
        (is_valid: bool, error_message: str or None, dimensions: tuple or None)
    """
    # Validate filename
    is_valid, error = validate_wrap_filename(filename)
    if not is_valid:
        return False, error, None

    # Check file size
    if len(file_bytes) > MAX_WRAP_SIZE:
        size_mb = len(file_bytes) / (1024 * 1024)
        return False, f"File size must be 1 MB or less (got {size_mb:.2f} MB)", None

    # Get and validate dimensions
    width, height = get_png_dimensions_from_bytes(file_bytes)
    is_valid, error = validate_wrap_dimensions(width, height)
    if not is_valid:
        return False, error, None

    return True, None, (width, height)


def upload_wrap_file(uploaded_file, filename, part2_mount_path=None):
    """
    Upload a wrap PNG file to the Wraps/ folder.

    This is a mode-aware function that works in both Present and Edit modes:
    - In Edit mode: Uses normal file operations
    - In Present mode: Uses quick_edit_part2() to temporarily mount RW

    Args:
        uploaded_file: Flask file object from request.files
        filename: Name of the file to save
        part2_mount_path: Current mount path for part2 (RO or RW), can be None in present mode

    Returns:
        (success: bool, message: str, dimensions: tuple or None)
    """
    from services.mode_service import current_mode
    from services.partition_mount_service import quick_edit_part2
    from config import MNT_DIR

    mode = current_mode()
    logger.info(f"Uploading wrap file {filename} (mode: {mode})")

    # Read file content for validation
    file_bytes = uploaded_file.read()
    uploaded_file.seek(0)  # Reset for saving later

    # Validate the file
    is_valid, error, dimensions = validate_wrap_file(file_bytes, filename)
    if not is_valid:
        return False, error, None

    # Sanitize filename
    filename = os.path.basename(filename)

    if mode == 'present':
        # Present mode - use quick_edit_part2() for temporary RW access
        # Save file to temp location first to avoid timeout during upload
        temp_dir = tempfile.mkdtemp(prefix='wrap_upload_')

        try:
            temp_file_path = os.path.join(temp_dir, filename)
            with open(temp_file_path, 'wb') as f:
                f.write(file_bytes)

            def _do_quick_copy():
                """Quick file copy - should take < 1 second per file."""
                try:
                    rw_mount = os.path.join(MNT_DIR, 'part2')
                    wraps_dir = os.path.join(rw_mount, WRAPS_FOLDER)

                    # Create Wraps directory if needed
                    if not os.path.isdir(wraps_dir):
                        os.makedirs(wraps_dir, exist_ok=True)

                    dest_path = os.path.join(wraps_dir, filename)

                    # Copy the file
                    shutil.copy2(temp_file_path, dest_path)

                    return True, "File copied successfully"
                except Exception as e:
                    logger.error(f"Error copying file: {e}", exc_info=True)
                    return False, f"Error copying file: {str(e)}"

            # Execute quick copy with short timeout
            logger.info("Using quick edit part2 for wrap file upload")
            success, copy_msg = quick_edit_part2(_do_quick_copy, timeout=30)

            # Clean up temp directory
            shutil.rmtree(temp_dir)

            if success:
                return True, f"Successfully uploaded {filename} ({dimensions[0]}x{dimensions[1]})", dimensions
            else:
                return False, copy_msg, None

        except Exception as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.error(f"Error uploading wrap: {e}", exc_info=True)
            return False, f"Error uploading file: {str(e)}", None

    else:
        # Edit mode - normal operation
        def _do_upload():
            """Internal function to perform the actual upload."""
            try:
                if not part2_mount_path:
                    return False, "Part2 mount path required in edit mode", None

                rw_mount = part2_mount_path
                wraps_dir = os.path.join(rw_mount, WRAPS_FOLDER)

                # Create Wraps directory if needed
                if not os.path.isdir(wraps_dir):
                    os.makedirs(wraps_dir, exist_ok=True)

                dest_path = os.path.join(wraps_dir, filename)

                # Save file
                with open(dest_path, 'wb') as f:
                    f.write(file_bytes)

                return True, f"Successfully uploaded {filename} ({dimensions[0]}x{dimensions[1]})", dimensions

            except Exception as e:
                logger.error(f"Error uploading wrap: {e}", exc_info=True)
                return False, f"Error uploading file: {str(e)}", None

        return _do_upload()


def delete_wrap_file(filename, part2_mount_path=None):
    """
    Delete a wrap PNG file.

    This is a mode-aware function that works in both Present and Edit modes:
    - In Edit mode: Uses normal file operations
    - In Present mode: Uses quick_edit_part2() to temporarily mount RW

    Args:
        filename: Name of the wrap file to delete
        part2_mount_path: Current mount path for part2 (RO or RW), can be None in present mode

    Returns:
        (success: bool, message: str)
    """
    from services.mode_service import current_mode
    from services.partition_mount_service import quick_edit_part2
    from config import MNT_DIR

    mode = current_mode()
    logger.info(f"Deleting wrap file {filename} (mode: {mode})")

    # Sanitize filename
    filename = os.path.basename(filename)

    def _do_delete():
        """Internal function to perform the actual deletion."""
        try:
            # In quick edit mode, use /mnt/gadget/part2 (RW mount)
            # Otherwise use the provided mount path
            if mode == 'present':
                rw_mount = os.path.join(MNT_DIR, 'part2')
            else:
                if not part2_mount_path:
                    return False, "Part2 mount path required in edit mode"
                rw_mount = part2_mount_path

            wraps_dir = os.path.join(rw_mount, WRAPS_FOLDER)
            file_path = os.path.join(wraps_dir, filename)

            if os.path.isfile(file_path):
                os.remove(file_path)
                logger.info(f"Deleted {filename}")
                return True, f"Deleted {filename}"
            else:
                return False, "File not found"

        except Exception as e:
            logger.error(f"Error deleting wrap: {e}", exc_info=True)
            return False, f"Error deleting file: {str(e)}"

    # Execute based on current mode
    if mode == 'present':
        # Use quick edit to temporarily mount RW
        logger.info("Using quick edit part2 for wrap deletion")
        return quick_edit_part2(_do_delete)
    else:
        # Normal edit mode operation
        return _do_delete()


def get_wrap_count(mount_path):
    """
    Get the current count of wrap files.

    Args:
        mount_path: Mount path for the partition

    Returns:
        int: Number of wrap files
    """
    if not mount_path:
        return 0

    wraps_dir = os.path.join(mount_path, WRAPS_FOLDER)
    if not os.path.isdir(wraps_dir):
        return 0

    try:
        count = 0
        for entry in os.listdir(wraps_dir):
            if entry.lower().endswith('.png'):
                count += 1
        return count
    except OSError:
        return 0


def list_wrap_files(mount_path):
    """
    List all wrap files in the Wraps folder.

    Args:
        mount_path: Mount path for the partition

    Returns:
        List of wrap file info dictionaries
    """
    if not mount_path:
        return []

    wraps_dir = os.path.join(mount_path, WRAPS_FOLDER)
    if not os.path.isdir(wraps_dir):
        return []

    files = []
    try:
        for entry in os.listdir(wraps_dir):
            if not entry.lower().endswith('.png'):
                continue

            full_path = os.path.join(wraps_dir, entry)
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
                logger.warning(f"Could not read wrap file {entry}: {e}")
                continue

        # Sort by filename
        files.sort(key=lambda x: x['filename'].lower())

    except OSError as e:
        logger.error(f"Error listing wrap files: {e}")

    return files
