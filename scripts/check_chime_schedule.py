#!/usr/bin/env python3
"""
Chime Schedule Checker - Runs periodically to apply scheduled chime changes.

This script:
1. Loads the chime scheduler configuration
2. Determines which chime should be active at the current time
3. Checks if the current active chime matches
4. If different, changes the chime (using quick edit if in present mode)

Designed to run every minute via systemd timer.
"""

import sys
import os
import hashlib
from pathlib import Path
import logging

# Add web directory to Python path to import modules
SCRIPT_DIR = Path(__file__).parent.resolve()
WEB_DIR = SCRIPT_DIR / 'web'
sys.path.insert(0, str(WEB_DIR))

# Import after adding to path
from config import GADGET_DIR, LOCK_CHIME_FILENAME, CHIMES_FOLDER
from services.chime_scheduler_service import get_scheduler
from services.lock_chime_service import set_active_chime
from services.partition_service import get_mount_path
from services.mode_service import current_mode

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()  # Log to stdout, systemd will capture
    ]
)

logger = logging.getLogger(__name__)


def get_file_md5(filepath):
    """
    Calculate MD5 hash of a file.
    
    Args:
        filepath: Path to the file
    
    Returns:
        MD5 hash as hexadecimal string
    """
    md5 = hashlib.md5()
    try:
        with open(filepath, 'rb') as f:
            # Read in chunks to handle large files efficiently
            for chunk in iter(lambda: f.read(8192), b''):
                md5.update(chunk)
        return md5.hexdigest()
    except Exception as e:
        logger.error(f"Error calculating MD5 for {filepath}: {e}")
        return None


def get_current_active_chime():
    """
    Get the filename of the currently active lock chime.
    
    Returns:
        Chime filename or None if no active chime
    """
    # Check part2 mount for LockChime.wav
    part2_mount = get_mount_path('part2')
    
    if not part2_mount:
        logger.warning("Part2 not mounted, cannot check current chime")
        return None
    
    active_chime_path = os.path.join(part2_mount, LOCK_CHIME_FILENAME)
    
    if not os.path.isfile(active_chime_path):
        logger.info("No active lock chime currently set")
        return None
    
    # We can't easily determine which library chime this is without comparing content
    # For now, we'll just indicate there IS an active chime
    return "ACTIVE_CHIME_PRESENT"


def main():
    """Check schedule and apply chime if needed."""
    logger.info("=" * 60)
    logger.info("Checking chime schedule")
    logger.info("=" * 60)
    
    try:
        # Load scheduler
        scheduler = get_scheduler()
        
        # Get which chime should be active now
        target_chime = scheduler.get_active_chime()
        
        if not target_chime:
            logger.info("No schedule applies at this time, keeping current chime")
            return 0
        
        logger.info(f"Schedule says chime should be: {target_chime}")
        
        # Get current mode
        mode = current_mode()
        logger.info(f"Current mode: {mode}")
        
        # In present mode, we don't need to validate file existence
        # because quick_edit_part2() will handle mounting part2 RW temporarily
        # In edit mode, validate the chime file exists before proceeding
        if mode == 'edit':
            part2_mount = get_mount_path('part2')
            
            if not part2_mount:
                logger.error("Part2 not mounted, cannot apply schedule")
                return 1
            
            # Verify target chime exists in library
            chimes_dir = os.path.join(part2_mount, CHIMES_FOLDER)
            target_chime_path = os.path.join(chimes_dir, target_chime)
            
            if not os.path.isfile(target_chime_path):
                logger.error(f"Scheduled chime not found in library: {target_chime}")
                return 1
            
            # Check if current active chime is already the target
            # Use MD5 hash comparison to definitively check if files are identical
            active_chime_path = os.path.join(part2_mount, LOCK_CHIME_FILENAME)
            
            if os.path.isfile(active_chime_path):
                active_md5 = get_file_md5(active_chime_path)
                target_md5 = get_file_md5(target_chime_path)
                
                if active_md5 and target_md5 and active_md5 == target_md5:
                    # Files are identical - no need to replace
                    logger.info(f"Active chime is already {target_chime} (MD5 match), skipping replacement")
                    return 0
                
                logger.info(f"Active chime differs from {target_chime} (MD5 mismatch), will replace")
        else:
            # In present mode, we can't easily check MD5 without mounting
            # Let set_active_chime handle it via quick_edit_part2
            logger.info("Present mode: delegating file operations to set_active_chime")
            part2_mount = None  # Will be handled by quick_edit_part2
        
        # Apply the schedule - set the target chime as active
        logger.info(f"Applying schedule: setting {target_chime} as active chime")
        
        # set_active_chime is mode-aware and will use quick_edit_part2() in present mode
        success, message = set_active_chime(target_chime, part2_mount)
        
        if success:
            logger.info(f"✓ Schedule applied successfully: {message}")
            return 0
        else:
            logger.error(f"✗ Failed to apply schedule: {message}")
            return 1
    
    except Exception as e:
        logger.error(f"Error checking chime schedule: {e}", exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
