"""
Partition mount service for temporary read-write access.

Provides safe temporary read-write mounting of partition 2 (lightshow)
while in Present mode without disrupting Tesla recording on partition 1.
"""

import os
import subprocess
import time
import logging
import threading
from pathlib import Path
from contextlib import contextmanager

from config import GADGET_DIR, MNT_DIR

logger = logging.getLogger(__name__)

# Lock file to prevent concurrent quick edit operations
QUICK_EDIT_LOCK = os.path.join(GADGET_DIR, '.quick_edit_part2.lock')
# Maximum age for a lock file before it's considered stale (in seconds)
LOCK_MAX_AGE = 120  # 2 minutes


def get_or_create_loop(img_path: str) -> str:
    """
    Get an existing loop device for the image or create a new one.

    Uses the --nooverlap (-L) flag which:
    1. Checks if a loop device already exists for this file
    2. If yes, returns that device (reuse)
    3. If no, creates a new one

    This prevents accumulation of duplicate loop devices.

    Args:
        img_path: Path to the image file

    Returns:
        Loop device path (e.g., /dev/loop0) or empty string on failure
    """
    try:
        # Use -L (--nooverlap) to reuse existing devices
        result = subprocess.run(
            ['sudo', '/usr/sbin/losetup', '--show', '-f', '-L', img_path],
            capture_output=True,
            text=True,
            check=False,
            timeout=10
        )

        if result.returncode == 0 and result.stdout.strip():
            loop_dev = result.stdout.strip()
            logger.debug(f"Got loop device {loop_dev} for {img_path} (via --nooverlap)")
            return loop_dev

        # Fallback without --nooverlap (older losetup versions)
        result = subprocess.run(
            ['sudo', '/usr/sbin/losetup', '--show', '-f', img_path],
            capture_output=True,
            text=True,
            check=True,
            timeout=10
        )
        loop_dev = result.stdout.strip()
        logger.debug(f"Created new loop device {loop_dev} for {img_path}")
        return loop_dev

    except Exception as e:
        logger.error(f"Failed to get/create loop device for {img_path}: {e}")
        return ''


class OperationTimeout(Exception):
    """Exception raised when operation exceeds maximum time limit."""
    pass


def run_with_timeout(func, timeout_seconds, *args, **kwargs):
    """
    Run a function with a timeout. If it exceeds the timeout, raise OperationTimeout.

    This works in threaded environments (like Flask) unlike signal-based approaches.

    Args:
        func: Function to run
        timeout_seconds: Maximum seconds to allow
        *args, **kwargs: Arguments to pass to func

    Returns:
        The function's return value

    Raises:
        OperationTimeout: If function takes longer than timeout_seconds
    """
    result = [None]
    exception = [None]

    def target():
        try:
            result[0] = func(*args, **kwargs)
        except Exception as e:
            exception[0] = e

    thread = threading.Thread(target=target)
    thread.daemon = True
    thread.start()
    thread.join(timeout_seconds)

    if thread.is_alive():
        # Thread is still running - timeout occurred
        logger.error(f"Operation timed out after {timeout_seconds} seconds")
        raise OperationTimeout(f"Operation exceeded {timeout_seconds} second timeout")

    if exception[0]:
        raise exception[0]

    return result[0]


@contextmanager
def _acquire_lock(timeout=10):
    """Acquire lock file to prevent concurrent operations."""
    start_time = time.time()

    while os.path.exists(QUICK_EDIT_LOCK):
        # Check if lock file is stale (older than LOCK_MAX_AGE)
        try:
            lock_age = time.time() - os.path.getmtime(QUICK_EDIT_LOCK)
            if lock_age > LOCK_MAX_AGE:
                logger.warning(f"Removing stale lock file (age: {lock_age:.1f}s)")
                os.remove(QUICK_EDIT_LOCK)
                break  # Lock removed, proceed to acquire
        except OSError:
            pass  # Lock file disappeared, that's fine

        if time.time() - start_time > timeout:
            # Before giving up, check one more time if it's stale
            try:
                lock_age = time.time() - os.path.getmtime(QUICK_EDIT_LOCK)
                if lock_age > LOCK_MAX_AGE:
                    logger.warning(f"Removing stale lock file on timeout (age: {lock_age:.1f}s)")
                    os.remove(QUICK_EDIT_LOCK)
                else:
                    raise TimeoutError("Could not acquire lock for quick edit operation")
            except OSError:
                pass  # Lock file disappeared
            break
        time.sleep(0.1)

    try:
        # Create lock file
        Path(QUICK_EDIT_LOCK).touch()
        yield
    finally:
        # Remove lock file
        try:
            os.remove(QUICK_EDIT_LOCK)
        except OSError:
            pass


def _restore_lun_backing(img_path, max_retries=3, lun_number=1):
    """
    Restore the LUN backing file. This is CRITICAL and must succeed.

    This function will retry multiple times with increasing delays to ensure
    the USB gadget is never left in an unusable state.

    Args:
        img_path: Path to the image file to set as LUN backing
        max_retries: Maximum number of retry attempts
        lun_number: LUN index (1 for lightshow, 2 for music)

    Returns:
        bool: True if successful, False otherwise
    """
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                logger.warning(f"Retrying LUN{lun_number} backing restoration (attempt {attempt + 1}/{max_retries})")
                time.sleep(0.5 * attempt)  # Exponential backoff

            # Find the gadget LUN file path
            result = subprocess.run(
                ['sh', '-c', f'ls -d /sys/kernel/config/usb_gadget/*/functions/mass_storage.usb0/lun.{lun_number}/file 2>/dev/null | head -n1'],
                capture_output=True,
                text=True,
                check=False,
                timeout=5
            )

            if result.returncode != 0 or not result.stdout.strip():
                logger.error("Could not find LUN file path in sysfs")
                continue

            lun_file_path = result.stdout.strip()
            logger.info(f"Restoring LUN backing: {lun_file_path} = {img_path}")

            # Set the backing file
            result = subprocess.run(
                ['sudo', 'sh', '-c', f'echo "{img_path}" > {lun_file_path}'],
                capture_output=True,
                text=True,
                check=False,
                timeout=5
            )

            if result.returncode != 0:
                stderr = result.stderr if result.stderr else "No error output"
                logger.error(f"Failed to set LUN backing file: {stderr}")
                continue

            # Verify it was set correctly
            result = subprocess.run(
                ['cat', lun_file_path],
                capture_output=True,
                text=True,
                check=False,
                timeout=5
            )

            if result.returncode == 0:
                current_backing = result.stdout.strip()
                if current_backing == img_path:
                    logger.info(f"✓ LUN backing file successfully restored: {current_backing}")
                    return True
                else:
                    logger.error(f"LUN backing verification failed: expected '{img_path}', got '{current_backing}'")

        except Exception as e:
            logger.error(f"Exception while restoring LUN backing: {e}", exc_info=True)

    logger.error(f"CRITICAL: Failed to restore LUN backing after {max_retries} attempts")
    return False


def check_and_recover_gadget_state():
    """
    Check the current gadget state and recover if needed.

    This function detects and fixes common bad states:
    - LUN1 backing file empty or missing
    - Inconsistent mount states
    - Orphaned loop devices

    Returns:
        dict: {
            'healthy': bool,
            'issues_found': list of strings,
            'fixes_applied': list of strings,
            'errors': list of strings
        }
    """
    logger.info("Checking gadget state...")
    result = {
        'healthy': True,
        'issues_found': [],
        'fixes_applied': [],
        'errors': []
    }

    img_path = os.path.join(GADGET_DIR, 'usb_lightshow.img')

    # Get current mode to determine which checks are appropriate
    from services.mode_service import current_mode
    mode = current_mode()

    # Check 1: Verify image file exists
    if not os.path.exists(img_path):
        result['healthy'] = False
        result['issues_found'].append(f"Image file missing: {img_path}")
        result['errors'].append("Cannot proceed without image file")
        return result

    # Check 2: Verify LUN1 backing file state (only in present mode)
    # In edit mode, the gadget is not active so LUN backing file won't exist
    if mode == 'present':
        try:
            proc = subprocess.run(
                ['sh', '-c', 'cat /sys/kernel/config/usb_gadget/*/functions/mass_storage.usb0/lun.1/file 2>/dev/null'],
                capture_output=True,
                text=True,
                check=False,
                timeout=5
            )

            if proc.returncode == 0:
                current_backing = proc.stdout.strip()
                # Normalize paths for comparison (resolve symlinks, relative paths)
                expected_path = os.path.realpath(img_path)
                current_path = os.path.realpath(current_backing) if current_backing else ""

                if not current_backing:
                    result['healthy'] = False
                    result['issues_found'].append("LUN1 backing file is empty")

                    # Attempt to restore
                    logger.info("Attempting to restore LUN1 backing file...")
                    if _restore_lun_backing(img_path):
                        result['fixes_applied'].append("Restored LUN1 backing file")
                        result['healthy'] = True  # Mark as healthy after successful fix
                    else:
                        result['errors'].append("Failed to restore LUN1 backing file")

                elif current_path != expected_path:
                    result['healthy'] = False
                    result['issues_found'].append(f"LUN1 backing incorrect: {current_backing} (expected: {img_path})")

                    # Attempt to correct
                    logger.info(f"Correcting LUN1 backing file from '{current_backing}' to '{img_path}'...")
                    if _restore_lun_backing(img_path):
                        result['fixes_applied'].append("Corrected LUN1 backing file")
                        result['healthy'] = True  # Mark as healthy after successful fix
                    else:
                        result['errors'].append("Failed to correct LUN1 backing file")
            else:
                result['issues_found'].append("Could not read LUN1 backing file")

        except Exception as e:
            result['errors'].append(f"Error checking LUN backing: {e}")

    # Check 2b: Verify LUN2 (music) backing file state (only in present mode)
    if mode == 'present':
        try:
            from config import MUSIC_ENABLED, IMG_MUSIC_NAME
            if MUSIC_ENABLED:
                music_img_path = os.path.join(GADGET_DIR, IMG_MUSIC_NAME)
                if os.path.isfile(music_img_path):
                    proc = subprocess.run(
                        ['sh', '-c', 'cat /sys/kernel/config/usb_gadget/*/functions/mass_storage.usb0/lun.2/file 2>/dev/null'],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=5
                    )
                    if proc.returncode == 0:
                        current_backing = proc.stdout.strip()
                        if not current_backing:
                            result['issues_found'].append("LUN2 (music) backing file is empty")
                            logger.info("Attempting to restore LUN2 backing file...")
                            if _restore_lun_backing(music_img_path, lun_number=2):
                                result['fixes_applied'].append("Restored LUN2 backing file")
                            else:
                                result['errors'].append("Failed to restore LUN2 backing file")
        except ImportError:
            pass  # Music config not available

    # Check 3: Look for orphaned RW mounts that should be RO (only in present mode)
    # In edit mode, RW mounts are expected and normal
    if mode == 'present':
        try:
            mount_rw = os.path.join(MNT_DIR, 'part2')
            proc = subprocess.run(
                ['mount'],
                capture_output=True,
                text=True,
                check=False,
                timeout=5
            )

            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    if mount_rw in line and 'rw' in line and 'loop' in line:
                        result['healthy'] = False
                        result['issues_found'].append(f"Unexpected RW mount found: {mount_rw}")
                        # Don't auto-fix this as it could be a legitimate operation in progress

        except Exception as e:
            logger.warning(f"Error checking mount state: {e}")

    if result['healthy'] and not result['issues_found']:
        logger.info("✓ Gadget state is healthy")
    else:
        logger.warning(f"Gadget state check: {len(result['issues_found'])} issues found, {len(result['fixes_applied'])} fixes applied")

    return result


def quick_edit_part2(operation_callback, timeout=10):
    """
    Temporarily mount part2 (lightshow) read-write to execute an operation.

    This is safe to call while in Present mode because:
    - The USB gadget serves the image FILE directly, not mount points
    - Tesla's LUN 1 (lightshow) is read-only from Tesla's perspective
    - Part1 (TeslaCam) remains untouched and recording continues

    Enhanced with robust error handling to ensure the device is NEVER left in a bad state:
    - Pre-flight state check and automatic recovery
    - Operation timeout enforcement
    - Priority-based cleanup with retries
    - Post-operation state validation

    Process:
    1. Check and recover any existing bad state
    2. Acquire exclusive lock
    3. Clear LUN1 backing (temporary)
    4. Unmount part2-ro (read-only mount)
    5. Setup RW loop device and mount
    6. Execute operation_callback (with timeout)
    7. Priority cleanup:
       - P1: Restore LUN1 backing file (CRITICAL - with retries)
       - P2: Restore RO mount
       - P3: Cleanup temp mounts and loops
    8. Validate final state

    Args:
        operation_callback: Function to execute while part2 is writable.
                          Should return (success, message)
        timeout: Maximum seconds to wait for operation (default: 10)
                Note: This is for lock acquisition. Operation gets 60s max.

    Returns:
        (success: bool, message: str)
    """
    logger.info("Starting quick edit part2 operation")

    # PRE-FLIGHT: Check and fix any existing bad state before we start
    try:
        state_check = check_and_recover_gadget_state()
        if state_check['errors']:
            logger.error(f"Pre-flight check failed with errors: {state_check['errors']}")
            return False, f"System in bad state: {'; '.join(state_check['errors'])}"
        if state_check['fixes_applied']:
            logger.info(f"Pre-flight fixes applied: {state_check['fixes_applied']}")
    except Exception as e:
        logger.error(f"Pre-flight check failed: {e}", exc_info=True)
        # Continue anyway - we'll try to recover

    img_path = os.path.join(GADGET_DIR, 'usb_lightshow.img')
    mount_ro = os.path.join(MNT_DIR, 'part2-ro')
    mount_rw = os.path.join(MNT_DIR, 'part2')

    # Track what we've done for cleanup
    cleanup_state = {
        'lun_cleared': False,
        'ro_unmounted': False,
        'rw_mounted': False,
        'loop_dev': None,
        'operation_success': False
    }

    try:
        with _acquire_lock(timeout=timeout):

            # Step 1: Clear the file backing for LUN 1 (lightshow) WITHOUT removing LUN structure
            logger.info("Clearing file backing for LUN 1")
            try:
                subprocess.run(
                    ['sudo', 'sh', '-c', 'echo "" > /sys/kernel/config/usb_gadget/*/functions/mass_storage.usb0/lun.1/file'],
                    capture_output=True,
                    check=False,
                    timeout=5
                )
                cleanup_state['lun_cleared'] = True
            except Exception as e:
                logger.warning(f"Could not clear LUN backing (non-fatal): {e}")

            # Step 2: Unmount ALL mounts of the loop device and detach all loop devices
            logger.info("Unmounting all mounts of loop device")
            try:
                result = subprocess.run(
                    ['sudo', '/usr/sbin/losetup', '-j', img_path],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10
                )

                # Unmount any existing mounts for loop devices (but don't detach - we'll reuse)
                if result.returncode == 0 and result.stdout.strip():
                    for line in result.stdout.strip().splitlines():
                        old_loop_dev = line.split(':')[0].strip()
                        logger.info(f"Found existing loop device: {old_loop_dev}")

                        # Find and unmount any mounts for this loop device
                        mount_result = subprocess.run(
                            ['mount'],
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=5
                        )

                        for mount_line in mount_result.stdout.splitlines():
                            parts = mount_line.split()
                            if len(parts) >= 3 and parts[0] == old_loop_dev:
                                mount_point = parts[2]
                                logger.info(f"Unmounting {mount_point} (from {old_loop_dev})")
                                subprocess.run(
                                    ['sudo', 'nsenter', '--mount=/proc/1/ns/mnt', 'umount', mount_point],
                                    capture_output=True,
                                    check=False,
                                    timeout=10
                                )
                                if mount_point == mount_ro:
                                    cleanup_state['ro_unmounted'] = True

                        # NOTE: We do NOT detach the loop device here - it will be reused
                        # in get_or_create_loop() below. This prevents loop device accumulation.
            except Exception as e:
                logger.warning(f"Error during unmount (non-fatal): {e}")

            # Step 3: Get or create RW loop device (reuse existing if possible)
            logger.info("Getting/creating read-write loop device")
            try:
                loop_dev = get_or_create_loop(img_path)
                if not loop_dev:
                    raise ValueError("Could not get/create loop device")
                cleanup_state['loop_dev'] = loop_dev
                logger.info(f"Using loop device: {loop_dev}")
            except Exception as e:
                logger.error(f"Failed to get/create loop device: {e}")
                raise ValueError(f"Could not get/create loop device: {e}")

            # Step 4: Detect filesystem type and mount read-write
            try:
                result = subprocess.run(
                    ['sudo', '/usr/sbin/blkid', '-o', 'value', '-s', 'TYPE', loop_dev],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10
                )
                fs_type = result.stdout.strip() if result.returncode == 0 else 'vfat'
                logger.info(f"Filesystem type: {fs_type}")

                # Create mount directory
                subprocess.run(
                    ['sudo', 'mkdir', '-p', mount_rw],
                    capture_output=True,
                    check=True
                )

                # Mount RW
                logger.info(f"Mounting {loop_dev} read-write at {mount_rw}")
                mount_cmd = [
                    'sudo', 'nsenter', '--mount=/proc/1/ns/mnt',
                    'mount', '-t', fs_type,
                    '-o', 'rw,uid=1000,gid=1000,umask=000',
                    loop_dev, mount_rw
                ]

                subprocess.run(
                    mount_cmd,
                    capture_output=True,
                    check=True
                )
                cleanup_state['rw_mounted'] = True
                logger.info("✓ RW mount successful")

            except Exception as e:
                logger.error(f"Failed to mount RW: {e}")
                raise ValueError(f"Could not mount filesystem: {e}")

            # Step 5: Execute the operation with overall timeout protection
            logger.info("Executing operation callback")
            operation_start = time.time()

            try:
                # Wrap operation in timeout - max 60 seconds for the entire operation
                success, message = run_with_timeout(operation_callback, 60)
                operation_time = time.time() - operation_start
                logger.info(f"Operation completed in {operation_time:.2f}s: {message}")
                cleanup_state['operation_success'] = success

                if not success:
                    # Operation failed but we still need to cleanup properly
                    logger.warning(f"Operation reported failure: {message}")

            except OperationTimeout as e:
                logger.error(f"Operation timed out: {e}")
                success = False
                message = "Operation timed out after 60 seconds"
            except Exception as e:
                logger.error(f"Operation callback raised exception: {e}", exc_info=True)
                success = False
                message = f"Operation error: {str(e)}"

            # Step 6: Sync filesystem - critical for ensuring changes are written
            logger.info("Syncing filesystem")
            try:
                subprocess.run(['sync'], check=False, timeout=5)
                # Reduced from 1s - sync is synchronous on completion
                time.sleep(0.3)
            except Exception as e:
                logger.warning(f"Sync failed (non-fatal): {e}")

            # CRITICAL SECTION: Cleanup with priority levels
            # Priority 1: RESTORE LUN BACKING (MUST SUCCEED)
            logger.info("PRIORITY 1: Restoring LUN backing file")
            lun_restored = _restore_lun_backing(img_path, max_retries=3)
            if not lun_restored:
                logger.error("CRITICAL: LUN backing restoration failed!")
                # This is bad but we continue cleanup

            # Priority 2: Restore RO mount for normal operations
            logger.info("PRIORITY 2: Restoring RO mount")
            try:
                # Unmount RW
                if cleanup_state['rw_mounted']:
                    subprocess.run(
                        ['sudo', 'nsenter', '--mount=/proc/1/ns/mnt', 'umount', mount_rw],
                        capture_output=True,
                        check=False
                    )
                    logger.info("✓ Unmounted RW mount")

                # The loop device created earlier is RW - we can mount it RO without detaching
                # Using mount -o ro on a RW loop device works fine
                ro_loop_dev = cleanup_state['loop_dev']

                if ro_loop_dev:
                    logger.info(f"Reusing existing loop device for RO mount: {ro_loop_dev}")

                    # Remount RO using the same loop device
                    subprocess.run(
                        ['sudo', 'mkdir', '-p', mount_ro],
                        capture_output=True,
                        check=False
                    )

                    mount_ro_cmd = [
                        'sudo', 'nsenter', '--mount=/proc/1/ns/mnt',
                        'mount', '-t', fs_type,
                        '-o', 'ro,uid=1000,gid=1000,umask=022',
                        ro_loop_dev, mount_ro
                    ]

                    ro_mount_result = subprocess.run(
                        mount_ro_cmd,
                        capture_output=True,
                        check=False
                    )

                    if ro_mount_result.returncode == 0:
                        logger.info("✓ RO mount restored")
                    else:
                        logger.warning("RO mount failed (non-critical)")

                    # Flush buffers
                    subprocess.run(
                        ['sudo', '/usr/sbin/blockdev', '--flushbufs', ro_loop_dev],
                        capture_output=True,
                        check=False
                    )

            except Exception as e:
                logger.error(f"Error during RO mount restoration: {e}", exc_info=True)

            # Priority 3: Drop caches (nice to have)
            try:
                subprocess.run(
                    ['sudo', 'sh', '-c', 'echo 3 > /proc/sys/vm/drop_caches'],
                    capture_output=True,
                    check=False
                )
                logger.info("✓ Dropped caches")
            except Exception:
                pass  # Not critical

            logger.info("Quick edit part2 operation completed")

            # Final state validation
            final_state = check_and_recover_gadget_state()
            if not final_state['healthy']:
                logger.warning(f"Post-operation state check found issues: {final_state['issues_found']}")
                if final_state['fixes_applied']:
                    logger.info(f"Auto-applied fixes: {final_state['fixes_applied']}")

            # Return the operation result
            return success, message

    except TimeoutError as e:
        logger.error(f"Timeout during quick edit: {e}")
        # Try emergency LUN restore
        _restore_lun_backing(img_path, max_retries=3)
        return False, f"Operation timed out: {e}"

    except subprocess.TimeoutExpired:
        logger.error("Command timeout during quick edit")
        # Try emergency LUN restore
        _restore_lun_backing(img_path, max_retries=3)
        return False, "Operation timed out"

    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed during quick edit: {e}")
        stderr = e.stderr.decode('utf-8', errors='ignore') if e.stderr else ''
        # Try emergency LUN restore
        _restore_lun_backing(img_path, max_retries=3)
        return False, f"Mount operation failed: {stderr[:200]}"

    except Exception as e:
        logger.error(f"Unexpected error during quick edit: {e}", exc_info=True)
        # Try emergency LUN restore
        _restore_lun_backing(img_path, max_retries=3)
        return False, f"Unexpected error: {str(e)}"


def quick_edit_part3(operation_callback, timeout=10):
    """
    Temporarily mount part3 (music) read-write to execute an operation.

    This is safe to call while in Present mode because:
    - The USB gadget serves the image FILE directly, not mount points
    - Tesla's LUN 2 (music) is read-only from Tesla's perspective
    - Part1 (TeslaCam) and Part2 (lightshow) remain untouched

    Process mirrors quick_edit_part2 but targets LUN 2 / part3:
    1. Check and recover any existing bad state
    2. Acquire exclusive lock
    3. Clear LUN2 backing (temporary)
    4. Unmount part3-ro (read-only mount)
    5. Setup RW loop device and mount
    6. Execute operation_callback (with timeout)
    7. Priority cleanup:
       - P1: Restore LUN2 backing file (CRITICAL - with retries)
       - P2: Restore RO mount
       - P3: Cleanup temp mounts and loops
    8. Validate final state

    Args:
        operation_callback: Function to execute while part3 is writable.
                          Should return (success, message)
        timeout: Maximum seconds to wait for lock acquisition (default: 10).
                Note: Operation gets 60s max.

    Returns:
        (success: bool, message: str)
    """
    from config import IMG_MUSIC_NAME

    logger.info("Starting quick edit part3 (music) operation")

    img_path = os.path.join(GADGET_DIR, IMG_MUSIC_NAME)

    if not os.path.isfile(img_path):
        return False, f"Music image not found: {img_path}"

    # PRE-FLIGHT: Check and fix any existing bad state before we start
    try:
        state_check = check_and_recover_gadget_state()
        if state_check['errors']:
            logger.error(f"Pre-flight check failed with errors: {state_check['errors']}")
            return False, f"System in bad state: {'; '.join(state_check['errors'])}"
        if state_check['fixes_applied']:
            logger.info(f"Pre-flight fixes applied: {state_check['fixes_applied']}")
    except Exception as e:
        logger.error(f"Pre-flight check failed: {e}", exc_info=True)
        # Continue anyway - we'll try to recover

    mount_ro = os.path.join(MNT_DIR, 'part3-ro')
    mount_rw = os.path.join(MNT_DIR, 'part3')

    # Track what we've done for cleanup
    cleanup_state = {
        'lun_cleared': False,
        'ro_unmounted': False,
        'rw_mounted': False,
        'loop_dev': None,
        'operation_success': False
    }

    fs_type = 'vfat'  # default, detected below

    try:
        with _acquire_lock(timeout=timeout):

            # Step 1: Clear the file backing for LUN 2 (music) WITHOUT removing LUN structure
            logger.info("Clearing file backing for LUN 2 (music)")
            try:
                subprocess.run(
                    ['sudo', 'sh', '-c', 'echo "" > /sys/kernel/config/usb_gadget/*/functions/mass_storage.usb0/lun.2/file'],
                    capture_output=True,
                    check=False,
                    timeout=5
                )
                cleanup_state['lun_cleared'] = True
            except Exception as e:
                logger.warning(f"Could not clear LUN2 backing (non-fatal): {e}")

            # Step 2: Unmount ALL mounts of the loop device and detach all loop devices
            logger.info("Unmounting all mounts of music loop device")
            try:
                result = subprocess.run(
                    ['sudo', '/usr/sbin/losetup', '-j', img_path],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10
                )

                if result.returncode == 0 and result.stdout.strip():
                    for line in result.stdout.strip().splitlines():
                        old_loop_dev = line.split(':')[0].strip()
                        logger.info(f"Found existing loop device: {old_loop_dev}")

                        mount_result = subprocess.run(
                            ['mount'],
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=5
                        )

                        for mount_line in mount_result.stdout.splitlines():
                            parts = mount_line.split()
                            if len(parts) >= 3 and parts[0] == old_loop_dev:
                                mount_point = parts[2]
                                logger.info(f"Unmounting {mount_point} (from {old_loop_dev})")
                                subprocess.run(
                                    ['sudo', 'nsenter', '--mount=/proc/1/ns/mnt', 'umount', mount_point],
                                    capture_output=True,
                                    check=False,
                                    timeout=10
                                )
                                if mount_point == mount_ro:
                                    cleanup_state['ro_unmounted'] = True
            except Exception as e:
                logger.warning(f"Error during unmount (non-fatal): {e}")

            # Step 3: Get or create RW loop device (reuse existing if possible)
            logger.info("Getting/creating read-write loop device for music")
            try:
                loop_dev = get_or_create_loop(img_path)
                if not loop_dev:
                    raise ValueError("Could not get/create loop device")
                cleanup_state['loop_dev'] = loop_dev
                logger.info(f"Using loop device: {loop_dev}")
            except Exception as e:
                logger.error(f"Failed to get/create loop device: {e}")
                raise ValueError(f"Could not get/create loop device: {e}")

            # Step 4: Detect filesystem type and mount read-write
            try:
                result = subprocess.run(
                    ['sudo', '/usr/sbin/blkid', '-o', 'value', '-s', 'TYPE', loop_dev],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10
                )
                fs_type = result.stdout.strip() if result.returncode == 0 else 'vfat'
                logger.info(f"Music filesystem type: {fs_type}")

                subprocess.run(
                    ['sudo', 'mkdir', '-p', mount_rw],
                    capture_output=True,
                    check=True
                )

                logger.info(f"Mounting {loop_dev} read-write at {mount_rw}")
                mount_cmd = [
                    'sudo', 'nsenter', '--mount=/proc/1/ns/mnt',
                    'mount', '-t', fs_type,
                    '-o', 'rw,uid=1000,gid=1000,umask=000',
                    loop_dev, mount_rw
                ]

                subprocess.run(
                    mount_cmd,
                    capture_output=True,
                    check=True
                )
                cleanup_state['rw_mounted'] = True
                logger.info("✓ Music RW mount successful")

            except Exception as e:
                logger.error(f"Failed to mount music RW: {e}")
                raise ValueError(f"Could not mount music filesystem: {e}")

            # Step 5: Execute the operation with overall timeout protection
            logger.info("Executing music operation callback")
            operation_start = time.time()

            try:
                success, message = run_with_timeout(operation_callback, 60)
                operation_time = time.time() - operation_start
                logger.info(f"Music operation completed in {operation_time:.2f}s: {message}")
                cleanup_state['operation_success'] = success

                if not success:
                    logger.warning(f"Music operation reported failure: {message}")

            except OperationTimeout as e:
                logger.error(f"Music operation timed out: {e}")
                success = False
                message = "Operation timed out after 60 seconds"
            except Exception as e:
                logger.error(f"Music operation callback raised exception: {e}", exc_info=True)
                success = False
                message = f"Operation error: {str(e)}"

            # Step 6: Sync filesystem
            logger.info("Syncing filesystem")
            try:
                subprocess.run(['sync'], check=False, timeout=5)
                time.sleep(0.3)
            except Exception as e:
                logger.warning(f"Sync failed (non-fatal): {e}")

            # CRITICAL SECTION: Cleanup with priority levels
            # Priority 1: RESTORE LUN2 BACKING (MUST SUCCEED)
            logger.info("PRIORITY 1: Restoring LUN2 (music) backing file")
            lun_restored = _restore_lun_backing(img_path, max_retries=3, lun_number=2)
            if not lun_restored:
                logger.error("CRITICAL: LUN2 backing restoration failed!")

            # Priority 2: Restore RO mount for normal operations
            logger.info("PRIORITY 2: Restoring music RO mount")
            try:
                if cleanup_state['rw_mounted']:
                    subprocess.run(
                        ['sudo', 'nsenter', '--mount=/proc/1/ns/mnt', 'umount', mount_rw],
                        capture_output=True,
                        check=False
                    )
                    logger.info("✓ Unmounted music RW mount")

                ro_loop_dev = cleanup_state['loop_dev']

                if ro_loop_dev:
                    logger.info(f"Reusing existing loop device for music RO mount: {ro_loop_dev}")

                    subprocess.run(
                        ['sudo', 'mkdir', '-p', mount_ro],
                        capture_output=True,
                        check=False
                    )

                    mount_ro_cmd = [
                        'sudo', 'nsenter', '--mount=/proc/1/ns/mnt',
                        'mount', '-t', fs_type,
                        '-o', 'ro,uid=1000,gid=1000,umask=022',
                        ro_loop_dev, mount_ro
                    ]

                    ro_mount_result = subprocess.run(
                        mount_ro_cmd,
                        capture_output=True,
                        check=False
                    )

                    if ro_mount_result.returncode == 0:
                        logger.info("✓ Music RO mount restored")
                    else:
                        logger.warning("Music RO mount failed (non-critical)")

                    subprocess.run(
                        ['sudo', '/usr/sbin/blockdev', '--flushbufs', ro_loop_dev],
                        capture_output=True,
                        check=False
                    )

            except Exception as e:
                logger.error(f"Error during music RO mount restoration: {e}", exc_info=True)

            # Priority 3: Drop caches (nice to have)
            try:
                subprocess.run(
                    ['sudo', 'sh', '-c', 'echo 3 > /proc/sys/vm/drop_caches'],
                    capture_output=True,
                    check=False
                )
                logger.info("✓ Dropped caches")
            except Exception:
                pass

            logger.info("Quick edit part3 (music) operation completed")

            # Final state validation
            final_state = check_and_recover_gadget_state()
            if not final_state['healthy']:
                logger.warning(f"Post-operation state check found issues: {final_state['issues_found']}")
                if final_state['fixes_applied']:
                    logger.info(f"Auto-applied fixes: {final_state['fixes_applied']}")

            return success, message

    except TimeoutError as e:
        logger.error(f"Timeout during quick edit part3: {e}")
        _restore_lun_backing(img_path, max_retries=3, lun_number=2)
        return False, f"Operation timed out: {e}"

    except subprocess.TimeoutExpired:
        logger.error("Command timeout during quick edit part3")
        _restore_lun_backing(img_path, max_retries=3, lun_number=2)
        return False, "Operation timed out"

    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed during quick edit part3: {e}")
        stderr = e.stderr.decode('utf-8', errors='ignore') if e.stderr else ''
        _restore_lun_backing(img_path, max_retries=3, lun_number=2)
        return False, f"Mount operation failed: {stderr[:200]}"

    except Exception as e:
        logger.error(f"Unexpected error during quick edit part3: {e}", exc_info=True)
        _restore_lun_backing(img_path, max_retries=3, lun_number=2)
        return False, f"Unexpected error: {str(e)}"


def rebind_usb_gadget(delay_seconds=1):
    """
    Unbind and rebind the USB gadget to force Tesla to re-enumerate the device.

    This simulates unplugging/replugging the USB drive, which forces Tesla to:
    - Clear its file cache
    - Re-scan the directory structure
    - Notice file changes (like updated LockChime.wav)

    Critical for lock chime changes to be recognized by the vehicle.

    Args:
        delay_seconds: Seconds to wait between unbind and rebind (default 1, reduced from 2)

    Returns:
        (success: bool, message: str)
    """
    logger.info("Rebinding USB gadget to force Tesla re-enumeration...")

    try:
        # Get the image path for LUN restoration
        img_path = os.path.join(GADGET_DIR, 'usb_lightshow.img')

        # Check if music LUN is enabled
        try:
            from config import MUSIC_ENABLED, IMG_MUSIC_NAME
            music_img_path = os.path.join(GADGET_DIR, IMG_MUSIC_NAME) if MUSIC_ENABLED else None
        except ImportError:
            music_img_path = None

        # Find the UDC device
        result = subprocess.run(
            ['sh', '-c', 'ls /sys/class/udc 2>/dev/null | head -n1'],
            capture_output=True,
            text=True,
            check=False
        )

        if result.returncode != 0 or not result.stdout.strip():
            return False, "Could not find UDC device"

        udc_device = result.stdout.strip()
        logger.info(f"Found UDC device: {udc_device}")

        # Find gadget UDC file path
        result = subprocess.run(
            ['sh', '-c', 'ls /sys/kernel/config/usb_gadget/*/UDC 2>/dev/null | head -n1'],
            capture_output=True,
            text=True,
            check=False
        )

        if result.returncode != 0 or not result.stdout.strip():
            return False, "Could not find gadget UDC file"

        udc_file = result.stdout.strip()
        logger.info(f"Found gadget UDC file: {udc_file}")

        # Step 1: Unbind UDC (disconnect from Tesla)
        logger.info("Unbinding UDC...")
        result = subprocess.run(
            ['sudo', 'sh', '-c', f'echo "" > {udc_file}'],
            capture_output=True,
            text=True,
            check=False,
            timeout=5
        )

        if result.returncode != 0:
            logger.warning(f"Unbind returned non-zero: {result.stderr}")
            # Continue anyway - may already be unbound

        # Step 2: Wait for disconnect to settle
        logger.info(f"Waiting {delay_seconds}s for disconnect to settle...")
        time.sleep(delay_seconds)

        # Step 3: Ensure LUN1 backing file is set before rebinding
        # This is critical - unbinding may have cleared it
        logger.info("Ensuring LUN1 backing file is set before rebind...")
        if not _restore_lun_backing(img_path, max_retries=3):
            logger.error("Failed to restore LUN backing before rebind")
            # Try to rebind anyway, but log the issue

        # Step 3b: Ensure LUN2 (music) backing file if enabled
        if music_img_path and os.path.isfile(music_img_path):
            logger.info("Ensuring LUN2 (music) backing file is set before rebind...")
            if not _restore_lun_backing(music_img_path, max_retries=3, lun_number=2):
                logger.warning("Failed to restore music LUN backing before rebind")

        # Step 4: Rebind UDC (reconnect to Tesla)
        logger.info(f"Rebinding UDC: {udc_device}")
        result = subprocess.run(
            ['sudo', 'sh', '-c', f'echo "{udc_device}" > {udc_file}'],
            capture_output=True,
            text=True,
            check=False,
            timeout=5
        )

        if result.returncode != 0:
            stderr = result.stderr if result.stderr else "No error output"
            logger.error(f"Failed to rebind UDC: {stderr}")
            # Ensure LUN is restored even if rebind failed
            _restore_lun_backing(img_path, max_retries=3)
            if music_img_path and os.path.isfile(music_img_path):
                _restore_lun_backing(music_img_path, max_retries=3, lun_number=2)
            return False, f"Failed to rebind UDC: {stderr}"

        # Step 5: Verify rebind was successful
        result = subprocess.run(
            ['cat', udc_file],
            capture_output=True,
            text=True,
            check=False
        )

        if result.returncode == 0:
            current_udc = result.stdout.strip()
            if current_udc == udc_device:
                logger.info(f"✓ USB gadget successfully rebound: {current_udc}")

                # Step 6: Final verification that LUN backing is still correct
                logger.info("Verifying LUN1 backing file after rebind...")
                if not _restore_lun_backing(img_path, max_retries=3):
                    logger.warning("LUN backing verification/restoration failed after rebind")
                    return True, "USB gadget rebound (LUN may need attention)"

                # Step 6b: Verify LUN2 (music) backing if enabled
                if music_img_path and os.path.isfile(music_img_path):
                    logger.info("Verifying LUN2 (music) backing file after rebind...")
                    if not _restore_lun_backing(music_img_path, max_retries=3, lun_number=2):
                        logger.warning("Music LUN backing verification failed after rebind")

                return True, "USB gadget rebound successfully"
            else:
                logger.error(f"UDC verification failed: expected '{udc_device}', got '{current_udc}'")
                # Attempt to restore LUN even on verification failure
                _restore_lun_backing(img_path, max_retries=3)
                if music_img_path and os.path.isfile(music_img_path):
                    _restore_lun_backing(music_img_path, max_retries=3, lun_number=2)
                return False, f"UDC verification failed"

        # Attempt to restore LUN on any other failure path
        _restore_lun_backing(img_path, max_retries=3)
        if music_img_path and os.path.isfile(music_img_path):
            _restore_lun_backing(music_img_path, max_retries=3, lun_number=2)
        return False, "Could not verify UDC rebind"

    except subprocess.TimeoutExpired:
        logger.error("Timeout during USB gadget rebind")
        # Ensure LUN is restored even on timeout
        img_path = os.path.join(GADGET_DIR, 'usb_lightshow.img')
        _restore_lun_backing(img_path, max_retries=3)
        if music_img_path and os.path.isfile(music_img_path):
            _restore_lun_backing(music_img_path, max_retries=3, lun_number=2)
        return False, "Operation timed out"
    except Exception as e:
        logger.error(f"Exception during USB gadget rebind: {e}", exc_info=True)
        # Ensure LUN is restored even on exception
        img_path = os.path.join(GADGET_DIR, 'usb_lightshow.img')
        _restore_lun_backing(img_path, max_retries=3)
        try:
            from config import MUSIC_ENABLED, IMG_MUSIC_NAME
            if MUSIC_ENABLED:
                m_path = os.path.join(GADGET_DIR, IMG_MUSIC_NAME)
                if os.path.isfile(m_path):
                    _restore_lun_backing(m_path, max_retries=3, lun_number=2)
        except ImportError:
            pass
        return False, f"Error rebinding gadget: {str(e)}"


def check_operation_in_progress():
    """
    Check if a file operation is currently in progress.

    Returns dict with:
        - in_progress (bool): True if operation is active
        - lock_age (float): Age of lock file in seconds (if exists)
        - estimated_completion (int): Estimated seconds until completion
        - operation_type (str): 'quick_edit' or 'unknown'
    """
    import time

    if not os.path.exists(QUICK_EDIT_LOCK):
        return {
            'in_progress': False,
            'lock_age': 0,
            'estimated_completion': 0,
            'operation_type': None
        }

    try:
        lock_age = time.time() - os.path.getmtime(QUICK_EDIT_LOCK)

        # Most quick_edit operations complete in 3-10 seconds
        # Estimate completion time, with max of 10 seconds
        estimated_completion = max(0, 10 - int(lock_age))

        return {
            'in_progress': True,
            'lock_age': lock_age,
            'estimated_completion': estimated_completion,
            'operation_type': 'quick_edit'
        }
    except OSError:
        # Lock file disappeared between check and stat
        return {
            'in_progress': False,
            'lock_age': 0,
            'estimated_completion': 0,
            'operation_type': None
        }
