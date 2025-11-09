"""Service layer for light show management.

This module contains functions for managing Tesla light show files (.fseq, .mp3, .wav).
"""

import os
import shutil
import logging
import tempfile
import zipfile
from pathlib import Path

from config import LIGHT_SHOW_FOLDER

logger = logging.getLogger(__name__)


def upload_zip_file(uploaded_file, part2_mount_path=None):
    """
    Upload and extract a ZIP file containing light show files.
    
    Searches recursively through the ZIP to find all .fseq, .mp3, and .wav files
    regardless of their location within the ZIP structure.
    
    This is a mode-aware function that works in both Present and Edit modes:
    - In Edit mode: Uses normal file operations
    - In Present mode: Uses quick_edit_part2() to temporarily mount RW
    
    Args:
        uploaded_file: Flask file object from request.files
        part2_mount_path: Current mount path for part2 (RO or RW), can be None in present mode
    
    Returns:
        (success: bool, message: str, file_count: int)
    """
    from services.mode_service import current_mode
    from services.partition_mount_service import quick_edit_part2
    from config import MNT_DIR
    
    mode = current_mode()
    logger.info(f"Uploading ZIP file (mode: {mode})")
    
    # Create temporary directory for extraction
    temp_dir = tempfile.mkdtemp(prefix='lightshow_zip_')
    extracted_files = []
    
    try:
        # Save uploaded ZIP to temp location
        temp_zip_path = os.path.join(temp_dir, 'upload.zip')
        uploaded_file.save(temp_zip_path)
        
        # Extract and find all light show files
        extract_dir = os.path.join(temp_dir, 'extracted')
        os.makedirs(extract_dir, exist_ok=True)
        
        with zipfile.ZipFile(temp_zip_path, 'r') as zip_ref:
            # Extract all files
            zip_ref.extractall(extract_dir)
        
        # Recursively search for light show files
        for root, dirs, files in os.walk(extract_dir):
            for file in files:
                lower_file = file.lower()
                if lower_file.endswith(('.fseq', '.mp3', '.wav')):
                    source_path = os.path.join(root, file)
                    # Use just the filename (flatten structure)
                    extracted_files.append((source_path, file))
        
        if not extracted_files:
            shutil.rmtree(temp_dir)
            return False, "No light show files (.fseq, .mp3, .wav) found in ZIP", 0
        
        logger.info(f"Found {len(extracted_files)} light show files in ZIP")
        
        if mode == 'present':
            # Present mode - use quick_edit_part2() for temporary RW access
            def _do_quick_copy():
                """Quick file copy - should take < 1 second per file."""
                try:
                    rw_mount = os.path.join(MNT_DIR, 'part2')
                    lightshow_dir = os.path.join(rw_mount, LIGHT_SHOW_FOLDER)
                    
                    # Create LightShow directory if needed
                    if not os.path.isdir(lightshow_dir):
                        os.makedirs(lightshow_dir, exist_ok=True)
                    
                    # Copy all extracted files
                    copied_count = 0
                    for source_path, filename in extracted_files:
                        dest_path = os.path.join(lightshow_dir, filename)
                        shutil.copy2(source_path, dest_path)
                        copied_count += 1
                    
                    return True, f"Copied {copied_count} files successfully"
                except Exception as e:
                    logger.error(f"Error copying files: {e}", exc_info=True)
                    return False, f"Error copying files: {str(e)}"
            
            # Execute quick copy with timeout (5 seconds per file should be plenty)
            timeout = max(30, len(extracted_files) * 5)
            logger.info(f"Using quick edit part2 for ZIP upload (timeout: {timeout}s)")
            success, copy_msg = quick_edit_part2(_do_quick_copy, timeout=timeout)
            
            # Clean up temp directory
            shutil.rmtree(temp_dir)
            
            if success:
                return True, f"Successfully uploaded {len(extracted_files)} files from ZIP", len(extracted_files)
            else:
                return False, copy_msg, 0
                
        else:
            # Edit mode - normal operation
            try:
                if not part2_mount_path:
                    shutil.rmtree(temp_dir)
                    return False, "Part2 mount path required in edit mode", 0
                
                rw_mount = part2_mount_path
                lightshow_dir = os.path.join(rw_mount, LIGHT_SHOW_FOLDER)
                
                # Create LightShow directory if needed
                if not os.path.isdir(lightshow_dir):
                    os.makedirs(lightshow_dir, exist_ok=True)
                
                # Copy all extracted files
                copied_count = 0
                for source_path, filename in extracted_files:
                    dest_path = os.path.join(lightshow_dir, filename)
                    shutil.copy2(source_path, dest_path)
                    copied_count += 1
                
                # Clean up temp directory
                shutil.rmtree(temp_dir)
                
                return True, f"Successfully uploaded {copied_count} files from ZIP", copied_count
                    
            except Exception as e:
                shutil.rmtree(temp_dir, ignore_errors=True)
                logger.error(f"Error uploading ZIP: {e}", exc_info=True)
                return False, f"Error uploading ZIP: {str(e)}", 0
                
    except zipfile.BadZipFile:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return False, "Invalid ZIP file", 0
    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.error(f"Error processing ZIP: {e}", exc_info=True)
        return False, f"Error processing ZIP: {str(e)}", 0


def upload_light_show_file(uploaded_file, filename, part2_mount_path=None):
    """
    Upload a light show file to the LightShow/ folder.
    
    This is a mode-aware function that works in both Present and Edit modes:
    - In Edit mode: Uses normal file operations
    - In Present mode: Uses quick_edit_part2() to temporarily mount RW
    
    Args:
        uploaded_file: Flask file object from request.files
        filename: Name of the file to save
        part2_mount_path: Current mount path for part2 (RO or RW), can be None in present mode
    
    Returns:
        (success: bool, message: str)
    """
    from services.mode_service import current_mode
    from services.partition_mount_service import quick_edit_part2
    from config import MNT_DIR
    
    mode = current_mode()
    logger.info(f"Uploading light show file {filename} (mode: {mode})")
    
    # Validate file extension
    lower_filename = filename.lower()
    if not (lower_filename.endswith(".fseq") or lower_filename.endswith(".mp3") or lower_filename.endswith(".wav")):
        return False, "Only fseq, mp3, and wav files are allowed"
    
    # Sanitize filename
    filename = os.path.basename(filename)
    
    if mode == 'present':
        # Present mode - use quick_edit_part2() for temporary RW access
        # Save file to temp location first to avoid timeout during upload
        import tempfile
        temp_dir = tempfile.mkdtemp(prefix='lightshow_upload_')
        
        try:
            temp_file_path = os.path.join(temp_dir, filename)
            uploaded_file.save(temp_file_path)
            
            def _do_quick_copy():
                """Quick file copy - should take < 1 second per file."""
                try:
                    rw_mount = os.path.join(MNT_DIR, 'part2')
                    lightshow_dir = os.path.join(rw_mount, LIGHT_SHOW_FOLDER)
                    
                    # Create LightShow directory if needed
                    if not os.path.isdir(lightshow_dir):
                        os.makedirs(lightshow_dir, exist_ok=True)
                    
                    dest_path = os.path.join(lightshow_dir, filename)
                    
                    # Copy the file
                    shutil.copy2(temp_file_path, dest_path)
                    
                    return True, "File copied successfully"
                except Exception as e:
                    logger.error(f"Error copying file: {e}", exc_info=True)
                    return False, f"Error copying file: {str(e)}"
            
            # Execute quick copy with short timeout
            logger.info("Using quick edit part2 for file upload")
            success, copy_msg = quick_edit_part2(_do_quick_copy, timeout=30)
            
            # Clean up temp directory
            shutil.rmtree(temp_dir)
            
            if success:
                return True, f"Successfully uploaded {filename}"
            else:
                return False, copy_msg
                
        except Exception as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.error(f"Error uploading light show: {e}", exc_info=True)
            return False, f"Error uploading file: {str(e)}"
    
    else:
        # Edit mode - normal operation
        def _do_upload():
            """Internal function to perform the actual upload."""
            try:
                if not part2_mount_path:
                    return False, "Part2 mount path required in edit mode"
                
                rw_mount = part2_mount_path
                lightshow_dir = os.path.join(rw_mount, LIGHT_SHOW_FOLDER)
                
                # Create LightShow directory if needed
                if not os.path.isdir(lightshow_dir):
                    os.makedirs(lightshow_dir, exist_ok=True)
                
                dest_path = os.path.join(lightshow_dir, filename)
                
                # Save file
                uploaded_file.seek(0)  # Reset file pointer
                uploaded_file.save(dest_path)
                
                return True, f"Successfully uploaded {filename}"
                    
            except Exception as e:
                logger.error(f"Error uploading light show: {e}", exc_info=True)
                return False, f"Error uploading file: {str(e)}"
        
        return _do_upload()


def delete_light_show_files(base_name, part2_mount_path=None):
    """
    Delete all files for a light show (fseq, mp3, wav).
    
    This is a mode-aware function that works in both Present and Edit modes:
    - In Edit mode: Uses normal file operations
    - In Present mode: Uses quick_edit_part2() to temporarily mount RW
    
    Args:
        base_name: Base name of the light show (without extension)
        part2_mount_path: Current mount path for part2 (RO or RW), can be None in present mode
    
    Returns:
        (success: bool, message: str)
    """
    from services.mode_service import current_mode
    from services.partition_mount_service import quick_edit_part2
    from config import MNT_DIR
    
    mode = current_mode()
    logger.info(f"Deleting light show files for {base_name} (mode: {mode})")
    
    # Sanitize base_name
    base_name = os.path.basename(base_name)
    
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
            
            lightshow_dir = os.path.join(rw_mount, LIGHT_SHOW_FOLDER)
            
            deleted_files = []
            errors = []
            
            # Try to delete fseq, mp3, and wav files
            for ext in [".fseq", ".mp3", ".wav"]:
                filename = base_name + ext
                file_path = os.path.join(lightshow_dir, filename)
                
                if os.path.isfile(file_path):
                    try:
                        os.remove(file_path)
                        deleted_files.append(filename)
                        logger.info(f"Deleted {filename}")
                    except Exception as e:
                        errors.append(f"{filename}: {str(e)}")
                        logger.error(f"Failed to delete {filename}: {e}")
            
            if deleted_files:
                message = f"Deleted {', '.join(deleted_files)}"
                if errors:
                    message += f" (Errors: {'; '.join(errors)})"
                return True, message
            elif errors:
                return False, f"Errors: {'; '.join(errors)}"
            else:
                return False, "No files found to delete"
            
        except Exception as e:
            logger.error(f"Error deleting light show: {e}", exc_info=True)
            return False, f"Error deleting files: {str(e)}"
    
    # Execute based on current mode
    if mode == 'present':
        # Use quick edit to temporarily mount RW
        logger.info("Using quick edit part2 for light show deletion")
        return quick_edit_part2(_do_delete)
    else:
        # Normal edit mode operation
        return _do_delete()


def create_light_show_zip(base_name, part2_mount_path):
    """
    Create a ZIP file containing all files for a light show.
    
    Args:
        base_name: Base name of the light show (without extension)
        part2_mount_path: Current mount path for part2
    
    Returns:
        (zip_path: str or None, error_message: str or None)
    """
    logger.info(f"Creating ZIP for light show {base_name}")
    
    # Sanitize base_name
    base_name = os.path.basename(base_name)
    
    try:
        if not part2_mount_path:
            return None, "Part2 mount path required"
        
        lightshow_dir = os.path.join(part2_mount_path, LIGHT_SHOW_FOLDER)
        
        # Find all files for this light show
        files_to_zip = []
        for ext in [".fseq", ".mp3", ".wav"]:
            filename = base_name + ext
            file_path = os.path.join(lightshow_dir, filename)
            
            if os.path.isfile(file_path):
                files_to_zip.append((filename, file_path))
        
        if not files_to_zip:
            return None, "No files found for this light show"
        
        # Create a temporary ZIP file
        temp_dir = tempfile.gettempdir()
        zip_filename = f"{base_name}.zip"
        zip_path = os.path.join(temp_dir, zip_filename)
        
        # Create the ZIP
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for filename, file_path in files_to_zip:
                zipf.write(file_path, arcname=filename)
                logger.info(f"Added {filename} to ZIP")
        
        logger.info(f"Created ZIP at {zip_path} with {len(files_to_zip)} file(s)")
        return zip_path, None
        
    except Exception as e:
        logger.error(f"Error creating ZIP: {e}", exc_info=True)
        return None, f"Error creating ZIP: {str(e)}"
