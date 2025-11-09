#!/usr/bin/env python3
"""
Boot Cleanup Script for TeslaUSB
Runs automatic cleanup on system boot for folders with 'run cleanup on boot' enabled
"""

import sys
import os
from pathlib import Path
import logging

# Add web directory to Python path to import modules
SCRIPT_DIR = Path(__file__).parent.resolve()
WEB_DIR = SCRIPT_DIR / 'web'
sys.path.insert(0, str(WEB_DIR))

# Import after adding to path
from config import GADGET_DIR, MNT_DIR
from services.cleanup_service import get_cleanup_service

# Configure logging
# Only log to stdout - the wrapper script uses 'tee' to write to file
# This avoids duplicate log entries (once from FileHandler, once from tee)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

def main():
    """
    Run automatic cleanup for folders with 'run cleanup on boot' enabled
    
    NOTE: This script runs during boot BEFORE state.txt is set, so we cannot use
    get_mount_path() which depends on current_mode(). The boot wrapper script
    guarantees partitions are mounted at /mnt/gadget/part1 and /mnt/gadget/part2
    """
    logger.info("=" * 60)
    logger.info("Starting automatic boot cleanup")
    logger.info("=" * 60)
    
    try:
        # Get cleanup service
        cleanup_service = get_cleanup_service(GADGET_DIR)
        
        # Use direct mount path - wrapper script mounts here during boot
        # (Cannot use get_mount_path() because state.txt may not be set yet)
        partition_path = Path(MNT_DIR) / 'part1'
        logger.info(f"Partition path: {partition_path}")
        
        if not partition_path.exists():
            logger.error(f"Partition not mounted: {partition_path}")
            return 1
        
        # Run automatic cleanup (only processes folders where enabled=True)
        result = cleanup_service.run_automatic_cleanup(partition_path, dry_run=False)
        
        # Log results
        if result['success']:
            logger.info(f"✓ Cleanup completed successfully")
            logger.info(f"  Deleted: {result['deleted_count']} files")
            logger.info(f"  Freed: {result['deleted_size_gb']} GB")
        else:
            logger.warning(f"⚠ Cleanup completed with errors")
            logger.warning(f"  Deleted: {result['deleted_count']} files")
            logger.warning(f"  Errors: {len(result['errors'])}")
            for error in result['errors']:
                logger.error(f"    {error}")
        
        # Log details by folder
        if result['deleted_count'] > 0:
            logger.info("Files deleted by folder:")
            folders = {}
            for file_info in result['deleted_files']:
                folder = file_info['folder']
                if folder not in folders:
                    folders[folder] = {'count': 0, 'size': 0}
                folders[folder]['count'] += 1
                folders[folder]['size'] += file_info['size']
            
            for folder, stats in folders.items():
                size_gb = round(stats['size'] / 1024**3, 2)
                logger.info(f"  {folder}: {stats['count']} files, {size_gb} GB")
        
        logger.info("=" * 60)
        logger.info("Boot cleanup completed")
        logger.info("=" * 60)
        
        return 0
        
    except Exception as e:
        logger.error(f"Failed to run boot cleanup: {e}", exc_info=True)
        return 1

if __name__ == '__main__':
    sys.exit(main())
