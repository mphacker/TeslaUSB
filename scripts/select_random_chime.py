#!/usr/bin/env python3
"""
Random Chime Selector on Boot

This script runs on device boot to select a random chime from the configured group
if random mode is enabled. It integrates with the boot sequence to set an active
chime before the USB gadget is presented to the vehicle.

Run this BEFORE presenting the USB gadget to ensure the chime is set.
"""

import sys
import os
import logging
from pathlib import Path

# Add web directory to Python path
SCRIPT_DIR = Path(__file__).parent.resolve()
WEB_DIR = SCRIPT_DIR / 'web'
sys.path.insert(0, str(WEB_DIR))

# Import after adding to path
from config import GADGET_DIR, LOCK_CHIME_FILENAME
from services.chime_group_service import get_group_manager
from services.lock_chime_service import set_active_chime
from services.partition_service import get_mount_path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()  # Log to stdout for systemd
    ]
)

logger = logging.getLogger(__name__)


def main():
    """Select and set random chime on boot if random mode is enabled."""
    logger.info("=" * 60)
    logger.info("Random Chime Boot Selector")
    logger.info("=" * 60)
    
    try:
        # Load group manager
        manager = get_group_manager()
        
        # Check if random mode is enabled
        random_config = manager.get_random_config()
        
        if not random_config.get('enabled'):
            logger.info("Random mode is not enabled - skipping")
            return 0
        
        group_id = random_config.get('group_id')
        logger.info(f"Random mode enabled for group: {group_id}")
        
        # Get the group
        group = manager.get_group(group_id)
        if not group:
            logger.error(f"Group '{group_id}' not found")
            return 1
        
        if group['chime_count'] == 0:
            logger.error(f"Group '{group['name']}' has no chimes")
            return 1
        
        logger.info(f"Group '{group['name']}' has {group['chime_count']} chime(s)")
        
        # Get currently active chime to avoid selecting it again
        part2_mount = get_mount_path('part2')
        current_chime = None
        
        if part2_mount:
            active_chime_path = os.path.join(part2_mount, LOCK_CHIME_FILENAME)
            if os.path.isfile(active_chime_path):
                # We can't easily identify which library chime this is without comparing
                # Just get the last selected from config
                current_chime = random_config.get('last_selected')
                if current_chime:
                    logger.info(f"Avoiding previously selected chime: {current_chime}")
        
        # Select random chime (with high-resolution time seed for better randomness)
        selected_chime = manager.select_random_chime(
            avoid_chime=current_chime,
            use_seed=True
        )
        
        if not selected_chime:
            logger.error("Failed to select random chime")
            return 1
        
        logger.info(f"Selected random chime: {selected_chime}")
        
        # Set as active chime
        # Note: At boot, we're typically in a temporary RW state before presenting USB
        # So we can directly write to part2_mount
        success, message = set_active_chime(selected_chime, part2_mount)
        
        if success:
            logger.info(f"✓ Successfully set random chime: {message}")
            return 0
        else:
            logger.error(f"✗ Failed to set random chime: {message}")
            return 1
    
    except Exception as e:
        logger.error(f"Error selecting random chime: {e}", exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
