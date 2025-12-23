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
import hashlib
import logging
import time
from pathlib import Path

# ===== PERFORMANCE TIMING =====
SCRIPT_START = time.time()
def log_timing(checkpoint):
    """Log timing checkpoint with millisecond precision."""
    elapsed_ms = int((time.time() - SCRIPT_START) * 1000)
    print(f"[RANDOM_CHIME TIMING] +{elapsed_ms}ms: {checkpoint}", flush=True)
# ===============================

log_timing("Script started")

# Add web directory to Python path
SCRIPT_DIR = Path(__file__).parent.resolve()
log_timing("Script dir resolved")

WEB_DIR = SCRIPT_DIR / 'web'
sys.path.insert(0, str(WEB_DIR))
log_timing("Path setup complete")

# Import after adding to path
from config import GADGET_DIR, LOCK_CHIME_FILENAME, CHIMES_FOLDER
log_timing("Config module imported")

from services.chime_group_service import get_group_manager
log_timing("Chime group service imported")

from services.lock_chime_service import set_active_chime
log_timing("Lock chime service imported")

from services.partition_service import get_mount_path
log_timing("Partition service imported")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()  # Log to stdout for systemd
    ]
)

logger = logging.getLogger(__name__)
log_timing("Logging configured")


def identify_active_chime(part2_mount):
    """
    Identify which library chime is currently active by comparing MD5 hashes.

    Args:
        part2_mount: Mount path for part2

    Returns:
        Filename of the currently active chime, or None if not found
    """
    identify_start = time.time()
    log_timing("Starting active chime identification")

    active_chime_path = os.path.join(part2_mount, LOCK_CHIME_FILENAME)
    if not os.path.isfile(active_chime_path):
        log_timing("No active chime file found")
        return None

    # Calculate MD5 of active chime
    try:
        log_timing("Calculating active chime MD5")
        active_md5 = hashlib.md5()
        with open(active_chime_path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                active_md5.update(chunk)
        active_hash = active_md5.hexdigest()
        log_timing("Active chime MD5 calculated")
    except Exception as e:
        logger.warning(f"Could not read active chime: {e}")
        return None

    # Compare with all library chimes
    chimes_dir = os.path.join(part2_mount, CHIMES_FOLDER)
    if not os.path.isdir(chimes_dir):
        log_timing("Chimes directory not found")
        return None

    log_timing("Starting library chime comparison")
    try:
        chime_count = 0
        for entry in os.listdir(chimes_dir):
            if not entry.lower().endswith('.wav'):
                continue

            chime_count += 1
            entry_path = os.path.join(chimes_dir, entry)
            if not os.path.isfile(entry_path):
                continue

            try:
                # Calculate MD5 of library chime
                lib_md5 = hashlib.md5()
                with open(entry_path, 'rb') as f:
                    for chunk in iter(lambda: f.read(65536), b''):
                        lib_md5.update(chunk)
                lib_hash = lib_md5.hexdigest()

                # Match found
                if lib_hash == active_hash:
                    elapsed = int((time.time() - identify_start) * 1000)
                    log_timing(f"Active chime identified as '{entry}' after checking {chime_count} files ({elapsed}ms)")
                    logger.info(f"Active chime identified as: {entry}")
                    return entry
            except Exception as e:
                logger.debug(f"Could not read library chime {entry}: {e}")
                continue

        elapsed = int((time.time() - identify_start) * 1000)
        log_timing(f"No matching chime found after checking {chime_count} files ({elapsed}ms)")
    except Exception as e:
        logger.warning(f"Error scanning chimes directory: {e}")

    return None


def main():
    """Select and set random chime on boot if random mode is enabled."""
    log_timing("Main function started")
    logger.info("=" * 60)
    logger.info("Random Chime Boot Selector")
    logger.info("=" * 60)

    try:
        # Load group manager
        log_timing("Loading group manager")
        manager = get_group_manager()
        log_timing("Group manager loaded")

        # Check if random mode is enabled
        log_timing("Checking random mode config")
        random_config = manager.get_random_config()
        log_timing("Random config retrieved")

        if not random_config.get('enabled'):
            log_timing("Random mode disabled - exiting")
            logger.info("Random mode is not enabled - skipping")
            return 0

        group_id = random_config.get('group_id')
        log_timing(f"Random mode enabled for group: {group_id}")
        logger.info(f"Random mode enabled for group: {group_id}")

        # Get the group
        log_timing("Fetching group data")
        group = manager.get_group(group_id)
        log_timing("Group data retrieved")

        if not group:
            logger.error(f"Group '{group_id}' not found")
            return 1

        if group['chime_count'] == 0:
            logger.error(f"Group '{group['name']}' has no chimes")
            return 1

        logger.info(f"Group '{group['name']}' has {group['chime_count']} chime(s)")

        # Get currently active chime to avoid selecting it again
        log_timing("Getting part2 mount path")

        # BOOT OPTIMIZATION: During boot, part2 is already mounted RW at /mnt/gadget/part2
        # Check for this mount first to avoid unnecessary quick_edit_part2 operations
        from config import MNT_DIR
        boot_mount_rw = os.path.join(MNT_DIR, 'part2')
        if os.path.ismount(boot_mount_rw):
            part2_mount = boot_mount_rw
            log_timing(f"Using boot RW mount: {part2_mount}")
            logger.info(f"Using boot-time RW mount: {part2_mount}")
        else:
            # Fall back to auto-detection (for manual runs in present mode)
            part2_mount = get_mount_path('part2')
            log_timing(f"Using auto-detected mount: {part2_mount}")
            logger.info(f"Using auto-detected mount: {part2_mount}")

        current_chime = None

        if part2_mount:
            active_chime_path = os.path.join(part2_mount, LOCK_CHIME_FILENAME)
            if os.path.isfile(active_chime_path):
                # Identify which library chime is currently active by comparing MD5 hashes
                current_chime = identify_active_chime(part2_mount)
                if current_chime:
                    logger.info(f"Avoiding currently active chime: {current_chime}")

        # Select random chime (with high-resolution time seed for better randomness)
        log_timing("Selecting random chime")
        selected_chime = manager.select_random_chime(
            avoid_chime=current_chime,
            use_seed=True
        )
        log_timing(f"Random chime selected: {selected_chime}")

        if not selected_chime:
            logger.error("Failed to select random chime")
            return 1

        logger.info(f"Selected random chime: {selected_chime}")

        # Set as active chime
        # Note: At boot, we're typically in a temporary RW state before presenting USB
        # So we can directly write to part2_mount
        log_timing("Setting active chime")

        # BOOT OPTIMIZATION: If we're using the boot RW mount, skip quick_edit_part2
        use_boot_mount = os.path.ismount(boot_mount_rw) and part2_mount == boot_mount_rw
        success, message = set_active_chime(selected_chime, part2_mount, skip_quick_edit=use_boot_mount)
        log_timing(f"Set active chime result: {success}")

        if success:
            logger.info(f"✓ Successfully set random chime: {message}")
            total_ms = int((time.time() - SCRIPT_START) * 1000)
            log_timing(f"Script completed successfully (total: {total_ms}ms)")
            return 0
        else:
            logger.error(f"✗ Failed to set random chime: {message}")
            return 1

    except Exception as e:
        logger.error(f"Error selecting random chime: {e}", exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
