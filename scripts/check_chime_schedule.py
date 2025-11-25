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
import time
import hashlib
from pathlib import Path
import logging

# Add web directory to Python path to import modules
SCRIPT_DIR = Path(__file__).parent.resolve()
WEB_DIR = SCRIPT_DIR / 'web'
sys.path.insert(0, str(WEB_DIR))

# Import after adding to path
from config import GADGET_DIR, LOCK_CHIME_FILENAME, CHIMES_FOLDER
from services.chime_scheduler_service import get_scheduler, cleanup_expired_date_schedules
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
    
    # CRITICAL: Check for active file operations before attempting any work
    # If quick_edit_part2() is already running (manual upload/delete), skip this run
    lock_file = os.path.join(GADGET_DIR, '.quick_edit_part2.lock')
    
    if os.path.exists(lock_file):
        # Check if lock is stale (older than 2 minutes)
        try:
            lock_age = time.time() - os.path.getmtime(lock_file)
            if lock_age > 120:  # 2 minutes
                logger.warning(f"Removing stale lock file (age: {lock_age:.1f}s)")
                try:
                    os.remove(lock_file)
                except OSError:
                    pass  # Already removed
            else:
                logger.info(f"File operation in progress (lock age: {lock_age:.1f}s), skipping this run")
                logger.info("Will try again on next scheduled run")
                return 0
        except OSError:
            pass  # Lock file disappeared
    
    try:
        # Load scheduler
        scheduler = get_scheduler()
        
        # Clean up expired date schedules that have already run
        cleanup_expired_date_schedules(scheduler)
        
        # Get all enabled schedules
        enabled_schedules = scheduler.list_schedules(enabled_only=True)
        
        if not enabled_schedules:
            logger.info("No enabled schedules found")
            return 0
        
        logger.info(f"Checking {len(enabled_schedules)} enabled schedule(s)")
        
        # Check each schedule to see if it should execute
        # We want to find the MOST RECENT schedule that should have run but hasn't
        eligible_schedules = []
        
        for schedule in enabled_schedules:
            schedule_id = schedule['id']
            should_run, chime_filename, reason = scheduler.should_execute_schedule(schedule_id)
            
            logger.info(f"Schedule {schedule_id} ({schedule.get('name', 'Unnamed')}): {reason}")
            
            if should_run:
                eligible_schedules.append({
                    'schedule': schedule,
                    'chime_filename': chime_filename,
                    'scheduled_time': schedule['time']
                })
        
        if not eligible_schedules:
            logger.info("No schedules need to be executed at this time")
            return 0
        
        # If multiple schedules are eligible, pick the most recent one
        # Example: Device offline until 3:15pm, schedules at 8am, 10am, 3pm should pick 3pm
        # Sort by scheduled time (descending) to get the latest time first
        eligible_schedules.sort(key=lambda x: x['scheduled_time'], reverse=True)
        
        # If there are ties (same time), apply precedence: Holiday > Date > Weekly
        schedule_to_execute = None
        chime_to_use = None
        latest_time = eligible_schedules[0]['scheduled_time']
        
        # Get all schedules at the latest time
        schedules_at_latest_time = [s for s in eligible_schedules if s['scheduled_time'] == latest_time]
        
        if len(schedules_at_latest_time) == 1:
            schedule_to_execute = schedules_at_latest_time[0]['schedule']
            chime_to_use = schedules_at_latest_time[0]['chime_filename']
        else:
            # Multiple schedules at same time - use precedence
            type_priority = {'holiday': 3, 'date': 2, 'weekly': 1}
            schedules_at_latest_time.sort(
                key=lambda x: type_priority.get(x['schedule'].get('schedule_type', 'weekly'), 0),
                reverse=True
            )
            schedule_to_execute = schedules_at_latest_time[0]['schedule']
            chime_to_use = schedules_at_latest_time[0]['chime_filename']
        
        if len(eligible_schedules) > 1:
            logger.info(f"Found {len(eligible_schedules)} eligible schedules, selected most recent at {latest_time}")
        
        logger.info(f"Schedule {schedule_to_execute['id']} ({schedule_to_execute.get('name', 'Unnamed')}) at {latest_time} should execute with chime: {chime_to_use}")
        
        # Handle random chime selection
        if chime_to_use == 'RANDOM':
            actual_chime = scheduler._select_random_chime()
            if not actual_chime:
                logger.error("Random chime requested but no valid chimes found")
                return 1
            logger.info(f"Random chime selected: {actual_chime}")
            chime_to_use = actual_chime
        
        # Get current mode
        mode = current_mode()
        logger.info(f"Current mode: {mode}")
        
        # Get part2 mount path (will be None in present mode, handled by set_active_chime)
        part2_mount = get_mount_path('part2') if mode == 'edit' else None
        
        # Apply the schedule - set the chime as active
        logger.info(f"Applying schedule: setting {chime_to_use} as active chime")
        
        # set_active_chime is mode-aware and will use quick_edit_part2() in present mode
        success, message = set_active_chime(chime_to_use, part2_mount)
        
        if success:
            # CRITICAL FIX: Mark ALL eligible schedules as executed, not just the one we ran
            # This prevents earlier schedules from running on subsequent timer ticks
            # and overwriting the correct (most recent) chime
            #
            # Example scenario: Device offline 7am-4pm, schedules at 8am and 3pm
            # - At 4:01pm both are eligible, we execute 3pm (correct)
            # - Without this fix, 8am is still marked as "not run today"
            # - At 4:02pm the 8am schedule would execute and overwrite the 3pm chime (BUG!)
            # - With this fix, both are marked as executed, preventing the overwrite
            for elig_schedule in eligible_schedules:
                scheduler.record_execution(elig_schedule['schedule']['id'])
                if elig_schedule['schedule']['id'] != schedule_to_execute['id']:
                    logger.info(f"  Marked schedule {elig_schedule['schedule']['id']} ({elig_schedule['schedule'].get('name', 'Unnamed')}) as executed (skipped, superseded by later schedule)")
            
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
