"""
Filesystem check service for TeslaUSB partitions.

Provides background filesystem checking and repair capabilities
with proper unmount/remount handling in edit mode.
"""

import os
import re
import subprocess
import time
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple, List

from config import GADGET_DIR, MNT_DIR
from .partition_service import iter_all_partitions
from .mode_service import current_mode

logger = logging.getLogger(__name__)

# Status file to track fsck operations
FSCK_STATUS_FILE = os.path.join(GADGET_DIR, 'fsck_status.json')
FSCK_HISTORY_FILE = os.path.join(GADGET_DIR, 'fsck_history.json')

# Background check thread and process
_fsck_thread = None
_fsck_process = None
_fsck_lock = threading.Lock()
_cancel_requested = False


class FsckError(Exception):
    """Exception raised when fsck operations fail."""
    pass


# Patterns for transient errors (from active Tesla writes)
# These are expected when Tesla is actively recording and not real corruption
TRANSIENT_ERROR_PATTERNS = [
    r'cluster [0-9a-fx]+ is marked as free',  # Bitmap not synced yet
    r'file size.*does not match.*allocated',   # Size mismatch during write
]

# Patterns for real corruption that needs repair
REAL_CORRUPTION_PATTERNS = [
    r'cross.?linked',                          # Two files claim same cluster
    r'orphan',                                 # Lost clusters
    r'directory.*corrupt',                     # Directory structure damage
    r'invalid.*cluster',                       # Bad cluster chain
    r'loop.*detected',                         # Circular reference
    r'boot.*sector',                           # Boot sector issues
    r'fat.*table',                             # FAT corruption
]


def _classify_fsck_errors(log_path: str) -> Tuple[List[str], List[str], bool]:
    """
    Classify errors from fsck log into transient vs real corruption.

    Args:
        log_path: Path to fsck log file

    Returns:
        Tuple of (transient_errors, real_errors, only_transient)
    """
    transient_errors = []
    real_errors = []

    try:
        if not os.path.exists(log_path):
            return [], [], False

        with open(log_path, 'r') as f:
            content = f.read()

        # Find all ERROR lines
        for line in content.split('\n'):
            if 'ERROR:' not in line and 'error:' not in line.lower():
                continue

            line_lower = line.lower()

            # Check for real corruption patterns first (higher priority)
            is_real = False
            for pattern in REAL_CORRUPTION_PATTERNS:
                if re.search(pattern, line_lower):
                    real_errors.append(line.strip())
                    is_real = True
                    break

            if is_real:
                continue

            # Check for transient patterns
            is_transient = False
            for pattern in TRANSIENT_ERROR_PATTERNS:
                if re.search(pattern, line_lower):
                    transient_errors.append(line.strip())
                    is_transient = True
                    break

            # Unknown error type - treat as real corruption to be safe
            if not is_transient:
                real_errors.append(line.strip())

    except Exception as e:
        logger.warning(f"Failed to classify fsck errors: {e}")
        return [], [], False

    only_transient = len(transient_errors) > 0 and len(real_errors) == 0
    return transient_errors, real_errors, only_transient


def _is_actively_recording(partition_num: int) -> bool:
    """
    Check if Tesla is actively recording to this partition.

    Looks for recently modified files in RecentClips (< 2 minutes old).
    Only applies to TeslaCam partition (part1).

    Args:
        partition_num: Partition number (1 or 2)

    Returns:
        True if Tesla appears to be actively recording
    """
    # Only TeslaCam (part1) has RecentClips
    if partition_num != 1:
        return False

    # Check if in present mode (USB connected to Tesla)
    if current_mode() != 'present':
        return False

    try:
        # Get mount path
        mount_path = None
        for part, path in iter_all_partitions():
            if part == f'part{partition_num}':
                mount_path = path
                break

        if not mount_path:
            return False

        recent_clips = os.path.join(mount_path, 'TeslaCam', 'RecentClips')

        # Use nsenter to check in PID 1 namespace
        result = subprocess.run(
            ['sudo', 'nsenter', '--mount=/proc/1/ns/mnt', '--',
             'find', recent_clips, '-maxdepth', '1', '-type', 'f',
             '-mmin', '-2', '-name', '*.mp4'],
            capture_output=True,
            text=True,
            timeout=5
        )

        # If any files found, Tesla is recording
        recent_files = [f for f in result.stdout.strip().split('\n') if f]
        if recent_files:
            logger.info(f"Active recording detected: {len(recent_files)} recent files")
            return True

    except Exception as e:
        logger.debug(f"Could not check for active recording: {e}")

    return False


def _get_status() -> Dict:
    """Load current fsck status from file."""
    try:
        if os.path.exists(FSCK_STATUS_FILE):
            with open(FSCK_STATUS_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load fsck status: {e}")

    return {
        'running': False,
        'partition': None,
        'mode': None,
        'progress': None,
        'start_time': None
    }


def _save_status(status: Dict):
    """Save current fsck status to file."""
    try:
        with open(FSCK_STATUS_FILE, 'w') as f:
            json.dump(status, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save fsck status: {e}")


def _get_history() -> list:
    """Load fsck history from file."""
    try:
        if os.path.exists(FSCK_HISTORY_FILE):
            with open(FSCK_HISTORY_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load fsck history: {e}")

    return []


def _save_history(history: list):
    """Save fsck history to file (keep last 20 entries)."""
    try:
        # Keep only last 20 entries
        history = history[-20:]
        with open(FSCK_HISTORY_FILE, 'w') as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save fsck history: {e}")


def _add_history_entry(partition: str, mode: str, result: str, details: str, duration: float):
    """Add entry to fsck history."""
    history = _get_history()
    entry = {
        'timestamp': datetime.now().isoformat(),
        'partition': partition,
        'mode': mode,
        'result': result,
        'details': details,
        'duration_seconds': round(duration, 1)
    }
    history.append(entry)
    _save_history(history)


def _run_fsck_background(partition_num: int, mode: str):
    """
    Background function to run fsck on a partition.

    Args:
        partition_num: Partition number (1 or 2)
        mode: 'quick' for read-only check or 'repair' for auto-repair
    """
    start_time = time.time()
    partition_name = f"part{partition_num}"
    current_system_mode = current_mode()
    need_unmount = False  # Initialize early to avoid UnboundLocalError

    try:
        # Update status to running
        status = {
            'running': True,
            'partition': partition_name,
            'mode': mode,
            'progress': 'Starting filesystem check...',
            'start_time': datetime.now().isoformat()
        }
        _save_status(status)

        # Construct partition info
        # Image files: part1 = usb_cam.img, part2 = usb_lightshow.img
        img_name = 'usb_cam.img' if partition_num == 1 else 'usb_lightshow.img'
        img_path = os.path.join(GADGET_DIR, img_name)

        # Verify image exists
        if not os.path.exists(img_path):
            raise FsckError(f"Image file not found: {img_path}")

        # Get mount path from partition service
        mount_path = None
        for part, path in iter_all_partitions():
            if part == partition_name:
                mount_path = path
                break

        if not mount_path:
            raise FsckError(f"Mount path not found for {partition_name}")

        logger.info(f"Starting fsck {mode} on {partition_name}: {img_path} (system mode: {current_system_mode})")

        # For repair mode: MUST unmount (edit mode only)
        # For quick mode in edit: unmount for consistency
        # For quick mode in present: use existing loop device (read-only check safe)
        need_unmount = (mode == 'repair') or (current_system_mode == 'edit')

        if need_unmount:
            status['progress'] = 'Unmounting partition...'
            _save_status(status)

            # Check if mounted in PID 1 namespace
            check_cmd = ['sudo', 'nsenter', '--mount=/proc/1/ns/mnt', '--', 'mountpoint', '-q', mount_path]
            is_mounted = subprocess.run(check_cmd, capture_output=True).returncode == 0

            if is_mounted:
                logger.info(f"Unmounting {mount_path}...")
                umount_cmd = ['sudo', 'nsenter', '--mount=/proc/1/ns/mnt', '--', 'umount', mount_path]
                subprocess.run(umount_cmd, check=True, capture_output=True, text=True)
                time.sleep(0.5)
        else:
            # Quick check in present mode - use existing mount
            logger.info(f"Quick check in present mode - using existing loop device")

        # Get or create loop device
        status['progress'] = 'Attaching loop device...'
        _save_status(status)

        # Check for existing loop device
        losetup_check = subprocess.run(
            ['losetup', '-j', img_path],
            capture_output=True,
            text=True
        )

        if losetup_check.stdout.strip():
            # Extract loop device name
            loop_dev = losetup_check.stdout.split(':')[0].strip()
            logger.info(f"Using existing loop device: {loop_dev}")
        else:
            # Create new loop device
            loop_result = subprocess.run(
                ['sudo', 'losetup', '--show', '-f', img_path],
                capture_output=True,
                text=True,
                check=True
            )
            loop_dev = loop_result.stdout.strip()
            logger.info(f"Created loop device: {loop_dev}")

        # Detect filesystem type
        fs_type_result = subprocess.run(
            ['sudo', 'blkid', '-o', 'value', '-s', 'TYPE', loop_dev],
            capture_output=True,
            text=True
        )
        fs_type = fs_type_result.stdout.strip() or 'unknown'

        if fs_type not in ['vfat', 'exfat']:
            raise FsckError(f"Unsupported filesystem type: {fs_type}")

        # Run fsck
        status['progress'] = f'Running filesystem check ({mode})...'
        _save_status(status)

        fsck_script = os.path.join(GADGET_DIR, 'scripts', 'fsck_with_swap.sh')
        fsck_cmd = ['sudo', fsck_script, loop_dev, fs_type, mode]

        # Set environment variable to enable background mode with extended timeouts
        fsck_env = os.environ.copy()
        fsck_env['FSCK_BACKGROUND'] = '1'

        logger.info(f"Running: {' '.join(fsck_cmd)} (background mode with extended timeouts)")

        # Use Popen to allow cancellation
        global _fsck_process, _cancel_requested
        _cancel_requested = False

        _fsck_process = subprocess.Popen(
            fsck_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=fsck_env
        )

        # Wait for process to complete or be cancelled
        try:
            stdout, stderr = _fsck_process.communicate()
            fsck_status = _fsck_process.returncode
        except Exception as e:
            logger.error(f"Error during fsck execution: {e}")
            _fsck_process.kill()
            _fsck_process.wait()
            raise
        finally:
            _fsck_process = None

        # Check if cancelled
        if _cancel_requested:
            logger.info(f"Fsck cancelled by user for {partition_name}")
            result = 'cancelled'
            details = 'Filesystem check cancelled by user'
        elif fsck_status == 0:
            result = 'healthy'
            details = 'No errors found'
        elif fsck_status == 1:
            result = 'repaired'
            details = 'Errors corrected successfully'
        elif fsck_status == 2:
            result = 'repaired'
            details = 'Errors corrected - consider rebooting'
        elif fsck_status == 4:
            # Errors found - check if transient (from active Tesla writes) or real corruption
            log_path = f"/var/log/teslausb/fsck_{os.path.basename(loop_dev)}.log"
            transient, real, only_transient = _classify_fsck_errors(log_path)

            if only_transient and _is_actively_recording(partition_num):
                # Only transient errors and Tesla is recording - this is expected
                result = 'recording'
                details = f'Tesla recording in progress ({len(transient)} transient inconsistencies)'
                logger.info(f"Classified as active recording: {len(transient)} transient errors")
            elif only_transient and mode == 'quick':
                # Only transient errors but not actively recording - might have just stopped
                result = 'recording'
                details = f'Recent writes detected ({len(transient)} pending sync)'
                logger.info(f"Transient errors only (no active recording): {len(transient)}")
            else:
                # Real corruption found
                result = 'errors'
                if real:
                    details = f'Real corruption detected ({len(real)} issues)'
                    logger.warning(f"Real corruption found: {real[:3]}...")  # Log first 3
                else:
                    details = 'Errors left uncorrected'
        elif fsck_status == 8:
            result = 'failed'
            details = 'Operational error during fsck'
        elif fsck_status == 124:
            result = 'timeout'
            details = 'Filesystem check timed out (partition may be very large)'
        else:
            result = 'unknown'
            details = f'Unknown exit code: {fsck_status}'

        logger.info(f"Fsck completed: {result} - {details}")

        # Remount partition if we unmounted it
        if need_unmount:
            status['progress'] = 'Remounting partition...'
            _save_status(status)

            # Determine mount options based on filesystem type
            if fs_type == 'exfat':
                mount_opts = 'rw,uid=1000,gid=1000,umask=000'
                mount_type = 'exfat'
            else:
                mount_opts = 'rw,uid=1000,gid=1000,umask=000'
                mount_type = 'vfat'

            mount_cmd = [
                'sudo', 'nsenter', '--mount=/proc/1/ns/mnt', '--',
                'mount', '-t', mount_type, '-o', mount_opts,
                loop_dev, mount_path
            ]
            subprocess.run(mount_cmd, check=True, capture_output=True, text=True)
            logger.info(f"Remounted {mount_path}")
        else:
            logger.info(f"Skipping remount (quick check in present mode)")

        # Calculate duration
        duration = time.time() - start_time

        # Add to history
        _add_history_entry(partition_name, mode, result, details, duration)

        # Update status to complete
        status = {
            'running': False,
            'partition': partition_name,
            'mode': mode,
            'progress': 'Complete',
            'start_time': None,
            'result': result,
            'details': details,
            'duration': round(duration, 1)
        }
        _save_status(status)

    except Exception as e:
        logger.exception(f"Fsck failed for {partition_name}")
        duration = time.time() - start_time

        # Try to remount on error (only if we unmounted it)
        if need_unmount:
            try:
                # Re-check for loop device
                losetup_check = subprocess.run(
                    ['losetup', '-j', img_path],
                    capture_output=True,
                    text=True
                )
                if losetup_check.stdout.strip():
                    loop_dev = losetup_check.stdout.split(':')[0].strip()

                    # Detect filesystem
                    fs_type_result = subprocess.run(
                        ['sudo', 'blkid', '-o', 'value', '-s', 'TYPE', loop_dev],
                        capture_output=True,
                        text=True
                    )
                    fs_type = fs_type_result.stdout.strip() or 'vfat'

                    if fs_type == 'exfat':
                        mount_opts = 'rw,uid=1000,gid=1000,umask=000'
                        mount_type = 'exfat'
                    else:
                        mount_opts = 'rw,uid=1000,gid=1000,umask=000'
                        mount_type = 'vfat'

                    mount_cmd = [
                        'sudo', 'nsenter', '--mount=/proc/1/ns/mnt', '--',
                        'mount', '-t', mount_type, '-o', mount_opts,
                        loop_dev, mount_path
                    ]
                    subprocess.run(mount_cmd, check=False, capture_output=True)
                    logger.info(f"Remounted {mount_path} after error")
            except Exception as remount_error:
                logger.error(f"Failed to remount after error: {remount_error}")

        _add_history_entry(partition_name, mode, 'failed', str(e), duration)

        status = {
            'running': False,
            'partition': partition_name,
            'mode': mode,
            'progress': 'Failed',
            'start_time': None,
            'result': 'failed',
            'details': str(e),
            'duration': round(duration, 1)
        }
        _save_status(status)


def start_fsck(partition_num: int, mode: str = 'quick') -> Tuple[bool, str]:
    """
    Start filesystem check on a partition in background.

    Args:
        partition_num: Partition number (1 or 2)
        mode: 'quick' for read-only check or 'repair' for auto-repair

    Returns:
        Tuple of (success: bool, message: str)
    """
    global _fsck_thread

    # Validate mode
    if mode not in ['quick', 'repair']:
        return False, "Invalid mode. Use 'quick' or 'repair'"

    # Repair mode requires edit mode (needs unmounted filesystem)
    # Quick mode can run in either mode (read-only check is safe)
    system_mode = current_mode()
    if mode == 'repair' and system_mode != 'edit':
        return False, "Repair mode requires edit mode. Switch to edit mode first or use quick check."

    with _fsck_lock:
        # Check if already running
        status = _get_status()
        if status.get('running'):
            return False, f"Filesystem check already running on {status.get('partition')}"

        # Start background thread
        _fsck_thread = threading.Thread(
            target=_run_fsck_background,
            args=(partition_num, mode),
            daemon=True
        )
        _fsck_thread.start()

        msg = f"Filesystem check started on part{partition_num}"
        if mode == 'quick' and system_mode == 'present':
            msg += " (read-only check while USB active)"

        return True, msg


def cancel_fsck() -> Tuple[bool, str]:
    """
    Cancel a running filesystem check.

    Returns:
        Tuple of (success: bool, message: str)
    """
    global _fsck_process, _cancel_requested

    with _fsck_lock:
        status = _get_status()
        if not status.get('running'):
            return False, "No filesystem check is currently running"

        if _fsck_process is None:
            return False, "Unable to cancel - process not found"

        try:
            # Set cancel flag
            _cancel_requested = True

            # Terminate the fsck process
            _fsck_process.terminate()

            # Give it a moment to terminate gracefully
            try:
                _fsck_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Force kill if it doesn't terminate
                _fsck_process.kill()
                _fsck_process.wait()

            logger.info(f"Cancelled fsck on {status.get('partition')}")
            return True, "Filesystem check cancelled"
        except Exception as e:
            logger.error(f"Error cancelling fsck: {e}")
            return False, f"Error cancelling check: {str(e)}"


def get_status() -> Dict:
    """Get current filesystem check status."""
    return _get_status()


def get_history() -> list:
    """Get filesystem check history."""
    return _get_history()


def get_last_check(partition_num: int) -> Optional[Dict]:
    """
    Get the last fsck result for a partition.

    Args:
        partition_num: Partition number (1 or 2)

    Returns:
        Dict with timestamp and result, or None if never checked
    """
    history = _get_history()
    partition_name = f"part{partition_num}"

    # Find most recent check for this partition (any result)
    for entry in reversed(history):
        if entry['partition'] == partition_name:
            return {
                'timestamp': entry['timestamp'],
                'result': entry['result'],
                'details': entry['details'],
                'age_hours': (datetime.now() - datetime.fromisoformat(entry['timestamp'])).total_seconds() / 3600
            }

    return None


def _cleanup_orphaned_status():
    """
    Clean up orphaned status file from service restarts.

    Called on module initialization to detect if status shows running
    but no actual thread exists (e.g., after service restart).
    """
    global _fsck_thread

    try:
        status = _get_status()
        print(f"[FSCK] Startup status check: running={status.get('running')}, thread={_fsck_thread}")
        logger.info(f"Startup status check: running={status.get('running')}, thread={_fsck_thread}")

        # If status shows running but no thread exists, mark as failed
        if status.get('running') and (_fsck_thread is None or not _fsck_thread.is_alive()):
            print("[FSCK] Detected orphaned fsck status from service restart - clearing")
            logger.warning("Detected orphaned fsck status from service restart - clearing")

            # Record failure in history
            try:
                _add_history_entry(
                    partition=status.get('partition', 'unknown'),
                    mode=status.get('mode', 'unknown'),
                    result='failed',
                    details='Service restarted during filesystem check',
                    duration=0
                )
                print("[FSCK] Added failure record to history")
                logger.info("Added failure record to history")
            except Exception as e:
                print(f"[FSCK] Failed to add to history: {e}")
                logger.error(f"Failed to add to history: {e}", exc_info=True)

            # Clear status
            try:
                new_status = {
                    'running': False,
                    'partition': None,
                    'mode': None,
                    'progress': '',
                    'start_time': None,
                    'error': 'Service restarted during filesystem check'
                }
                print(f"[FSCK] Saving new status: {new_status}")
                logger.info(f"Saving new status: {new_status}")
                _save_status(new_status)
                print("[FSCK] Status file saved successfully")
                logger.info("Status file saved successfully")

                # Verify it was saved
                saved_status = _get_status()
                print(f"[FSCK] Verified status after save: running={saved_status.get('running')}")
                logger.info(f"Verified status after save: running={saved_status.get('running')}")
            except Exception as e:
                print(f"[FSCK] Failed to save status: {e}")
                logger.error(f"Failed to save status: {e}", exc_info=True)

            print("[FSCK] Orphaned fsck status cleanup complete")
            logger.info("Orphaned fsck status cleanup complete")

    except Exception as e:
        print(f"[FSCK] Failed to cleanup orphaned status: {e}")
        logger.error(f"Failed to cleanup orphaned status: {e}", exc_info=True)


# Initialize on module load - call after all functions are defined
_cleanup_orphaned_status()

