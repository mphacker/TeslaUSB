"""
Partition mount service for temporary read-write access.

Provides safe temporary read-write mounting of partition 2 (lightshow)
while in Present mode without disrupting Tesla recording on partition 1.
"""

import os
import subprocess
import time
import logging
from pathlib import Path
from contextlib import contextmanager

from config import GADGET_DIR, MNT_DIR

logger = logging.getLogger(__name__)

# Lock file to prevent concurrent quick edit operations
QUICK_EDIT_LOCK = os.path.join(GADGET_DIR, '.quick_edit_part2.lock')
# Maximum age for a lock file before it's considered stale (in seconds)
LOCK_MAX_AGE = 120  # 2 minutes


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


def quick_edit_part2(operation_callback, timeout=10):
    """
    Temporarily mount part2 (lightshow) read-write to execute an operation.
    
    This is safe to call while in Present mode because:
    - The USB gadget serves the image FILE directly, not mount points
    - Tesla's LUN 1 (lightshow) is read-only from Tesla's perspective
    - Part1 (TeslaCam) remains untouched and recording continues
    
    Process:
    1. Acquire exclusive lock
    2. Unmount part2-ro (read-only mount)
    3. Mount part2 read-write at /mnt/gadget/part2
    4. Execute operation_callback
    5. Sync filesystem
    6. Unmount part2
    7. Remount part2-ro (read-only)
    8. Release lock
    
    Args:
        operation_callback: Function to execute while part2 is writable.
                          Should return (success, message)
        timeout: Maximum seconds to wait for operation (default: 10)
    
    Returns:
        (success: bool, message: str)
    """
    logger.info("Starting quick edit part2 operation")
    
    try:
        with _acquire_lock(timeout=timeout):
            img_path = os.path.join(GADGET_DIR, 'usb_lightshow.img')
            mount_ro = os.path.join(MNT_DIR, 'part2-ro')
            mount_rw = os.path.join(MNT_DIR, 'part2')
            
            # Step 1: Clear the file backing for LUN 1 (lightshow) WITHOUT removing LUN structure
            # This keeps the gadget stable while we work on the filesystem
            logger.info("Clearing file backing for LUN 1")
            subprocess.run(
                ['sudo', 'sh', '-c', 'echo "" > /sys/kernel/config/usb_gadget/*/functions/mass_storage.usb0/lun.1/file'],
                capture_output=True,
                timeout=5
            )
            
            # Step 2: Unmount ALL mounts of the loop device
            logger.info("Unmounting all mounts of loop device")
            # First find ALL loop devices for this image
            result = subprocess.run(
                ['sudo', '/usr/sbin/losetup', '-j', img_path],
                capture_output=True,
                timeout=5,
                text=True
            )
            
            # Unmount and detach ALL existing loop devices for this image
            if result.returncode == 0 and result.stdout.strip():
                for line in result.stdout.strip().splitlines():
                    old_loop_dev = line.split(':')[0].strip()
                    logger.info(f"Found existing loop device: {old_loop_dev}")
                    
                    # Find and unmount any mounts for this loop device
                    # Use exact match on the device field (first field in mount output)
                    mount_result = subprocess.run(
                        ['mount'],
                        capture_output=True,
                        timeout=5,
                        text=True
                    )
                    
                    for mount_line in mount_result.stdout.splitlines():
                        parts = mount_line.split()
                        if len(parts) >= 3 and parts[0] == old_loop_dev:
                            # parts[0] is the device, parts[2] is the mount point
                            mount_point = parts[2]
                            logger.info(f"Unmounting {mount_point} (from {old_loop_dev})")
                            subprocess.run(
                                ['sudo', 'nsenter', '--mount=/proc/1/ns/mnt', 'umount', mount_point],
                                capture_output=True,
                                timeout=15  # Increased timeout for busy filesystems
                            )
                    
                    # Detach the loop device
                    logger.info(f"Detaching old loop device: {old_loop_dev}")
                    subprocess.run(
                        ['sudo', '/usr/sbin/losetup', '-d', old_loop_dev],
                        capture_output=True,
                        timeout=5
                    )
            
            #  Step 3: Find or create loop device for the image (reattach if needed)
            logger.info("Setting up loop device")
            result = subprocess.run(
                ['sudo', '/usr/sbin/losetup', '-j', img_path],
                capture_output=True,
                timeout=5,
                text=True
            )
            
            if result.returncode == 0 and result.stdout.strip():
                # Loop device already exists
                loop_dev = result.stdout.split(':')[0].strip()
                logger.info(f"Using existing loop device: {loop_dev}")
                
                # Make it read-write
                logger.info(f"Setting {loop_dev} to read-write mode")
                subprocess.run(
                    ['sudo', '/usr/sbin/losetup', '-r', '-d', loop_dev],
                    capture_output=True,
                    timeout=5
                )
                # Recreate as read-write
                result = subprocess.run(
                    ['sudo', '/usr/sbin/losetup', '--show', '-f', img_path],
                    capture_output=True,
                    timeout=5,
                    text=True,
                    check=True
                )
                loop_dev = result.stdout.strip()
                logger.info(f"Recreated loop device as RW: {loop_dev}")
            else:
                # Create new loop device (defaults to RW)
                result = subprocess.run(
                    ['sudo', '/usr/sbin/losetup', '--show', '-f', img_path],
                    capture_output=True,
                    timeout=5,
                    text=True,
                    check=True
                )
                loop_dev = result.stdout.strip()
                logger.info(f"Created new loop device: {loop_dev}")
            
            # Step 3: Detect filesystem type
            result = subprocess.run(
                ['sudo', '/usr/sbin/blkid', '-o', 'value', '-s', 'TYPE', loop_dev],
                capture_output=True,
                timeout=5,
                text=True
            )
            fs_type = result.stdout.strip() if result.returncode == 0 else 'vfat'
            logger.info(f"Filesystem type: {fs_type}")
            
            # Step 4: Create mount directory and mount read-write
            logger.info(f"Mounting {loop_dev} read-write at {mount_rw}")
            
            # Create mount directory with sudo
            subprocess.run(
                ['sudo', 'mkdir', '-p', mount_rw],
                capture_output=True,
                timeout=5,
                check=True
            )
            
            mount_cmd = [
                'sudo', 'nsenter', '--mount=/proc/1/ns/mnt',
                'mount', '-t', fs_type,
                '-o', 'rw,uid=1000,gid=1000,umask=000',
                loop_dev, mount_rw
            ]
            
            result = subprocess.run(
                mount_cmd,
                capture_output=True,
                timeout=5,
                check=True
            )
            
            try:
                # Step 5: Execute the operation
                logger.info("Executing operation callback")
                operation_start = time.time()
                success, message = operation_callback()
                operation_time = time.time() - operation_start
                
                logger.info(f"Operation completed in {operation_time:.2f}s: {message}")
                
                if not success:
                    return False, message
                
                # Step 6: Sync filesystem - critical for ensuring changes are written to image file
                logger.info("Syncing filesystem")
                subprocess.run(['sync'], timeout=5, check=True)
                time.sleep(1)  # Give sync time to complete
                
                # Drop caches to ensure fresh reads
                try:
                    subprocess.run(
                        ['sudo', 'sh', '-c', 'echo 3 > /proc/sys/vm/drop_caches'],
                        capture_output=True,
                        timeout=5
                    )
                    logger.info("Dropped caches")
                except Exception:
                    pass  # Not critical if this fails
                
                return True, message
                
            finally:
                # Step 7: Always cleanup - unmount RW and remount RO
                logger.info("Cleaning up mounts")
                
                # Final sync before unmounting
                subprocess.run(['sync'], capture_output=True, timeout=5)
                time.sleep(0.5)
                
                # Unmount read-write
                subprocess.run(
                    ['sudo', 'nsenter', '--mount=/proc/1/ns/mnt', 'umount', mount_rw],
                    capture_output=True,
                    timeout=5
                )
                
                # CRITICAL: Detach and reattach loop device to force kernel to re-read image file
                # This ensures USB gadget serves the updated content
                logger.info(f"Detaching loop device {loop_dev} to flush changes")
                subprocess.run(
                    ['sudo', '/usr/sbin/losetup', '-d', loop_dev],
                    capture_output=True,
                    timeout=5
                )
                
                # Recreate loop device (read-only for present mode)
                logger.info("Recreating loop device as read-only")
                result = subprocess.run(
                    ['sudo', '/usr/sbin/losetup', '--show', '-f', '-r', img_path],
                    capture_output=True,
                    timeout=5,
                    text=True
                )
                if result.returncode == 0:
                    loop_dev = result.stdout.strip()
                    logger.info(f"Recreated loop device: {loop_dev}")
                
                # Remount read-only
                subprocess.run(
                    ['sudo', 'mkdir', '-p', mount_ro],
                    capture_output=True,
                    timeout=5
                )
                mount_ro_cmd = [
                    'sudo', 'nsenter', '--mount=/proc/1/ns/mnt',
                    'mount', '-t', fs_type,
                    '-o', 'ro,uid=1000,gid=1000,umask=022',
                    loop_dev, mount_ro
                ]
                
                subprocess.run(
                    mount_ro_cmd,
                    capture_output=True,
                    timeout=5
                )
                
                # Flush all buffers for the loop device to ensure clean state
                logger.info(f"Flushing buffers for {loop_dev}")
                subprocess.run(
                    ['sudo', '/usr/sbin/blockdev', '--flushbufs', loop_dev],
                    capture_output=True,
                    timeout=5
                )
                
                # Restore file backing for LUN 1 with the IMAGE FILE (not loop device)
                # This matches how present_usb.sh works and ensures host sees fresh data
                logger.info(f"Restoring file backing for LUN 1 with image file: {img_path}")
                
                # Find the actual gadget path
                result = subprocess.run(
                    ['sh', '-c', 'ls -d /sys/kernel/config/usb_gadget/*/functions/mass_storage.usb0/lun.1/file | head -n1'],
                    capture_output=True,
                    timeout=5,
                    text=True
                )
                
                if result.returncode == 0 and result.stdout.strip():
                    lun_file_path = result.stdout.strip()
                    logger.info(f"Setting LUN file: {lun_file_path} = {img_path}")
                    result = subprocess.run(
                        ['sudo', 'sh', '-c', f'echo "{img_path}" > {lun_file_path}'],
                        capture_output=True,
                        timeout=5,
                        text=True
                    )
                    if result.returncode != 0:
                        stderr = result.stderr if result.stderr else "No error output"
                        logger.error(f"Failed to set LUN file backing: {stderr}")
                    else:
                        # Verify it was set
                        verify_result = subprocess.run(
                            ['cat', lun_file_path],
                            capture_output=True,
                            timeout=5,
                            text=True
                        )
                        logger.info(f"LUN file now contains: {verify_result.stdout.strip()}")
                
                logger.info("Quick edit part2 operation completed, read-only mount restored")
                logger.info("Note: Windows may cache the drive contents. Eject and re-insert to see changes.")
    
    except TimeoutError as e:
        logger.error(f"Timeout during quick edit: {e}")
        return False, f"Operation timed out: {e}"
    
    except subprocess.TimeoutExpired:
        logger.error("Command timeout during quick edit")
        return False, "Operation timed out"
    
    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed during quick edit: {e}")
        stderr = e.stderr.decode('utf-8', errors='ignore') if e.stderr else ''
        return False, f"Mount operation failed: {stderr[:200]}"
    
    except Exception as e:
        logger.error(f"Unexpected error during quick edit: {e}", exc_info=True)
        return False, f"Unexpected error: {str(e)}"


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
