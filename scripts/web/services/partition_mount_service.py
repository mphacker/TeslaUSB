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


@contextmanager
def _acquire_lock(timeout=10):
    """Acquire lock file to prevent concurrent operations."""
    start_time = time.time()
    
    while os.path.exists(QUICK_EDIT_LOCK):
        if time.time() - start_time > timeout:
            raise TimeoutError("Could not acquire lock for quick edit operation")
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
            
            # Step 1: Unbind USB gadget to release the loop device
            logger.info("Unbinding USB gadget UDC")
            subprocess.run(
                ['sudo', 'sh', '-c', 'echo "" > /sys/kernel/config/usb_gadget/*/UDC'],
                capture_output=True,
                timeout=5
            )
            
            # Step 1b: Clear the file backing for LUN 1 (lightshow)
            logger.info("Clearing file backing for LUN 1")
            subprocess.run(
                ['sudo', 'sh', '-c', 'echo "" > /sys/kernel/config/usb_gadget/*/functions/mass_storage.usb0/lun.1/file'],
                capture_output=True,
                timeout=5
            )
            # Ignore errors - may already be cleared
            
            # Step 2: Unmount ALL mounts of the loop device
            logger.info("Unmounting all mounts of loop device")
            # First find the loop device
            result = subprocess.run(
                ['sudo', '/usr/sbin/losetup', '-j', img_path],
                capture_output=True,
                timeout=5,
                text=True
            )
            
            if result.returncode == 0 and result.stdout.strip():
                loop_dev = result.stdout.split(':')[0].strip()
                logger.info(f"Found loop device: {loop_dev}")
                
                # Find all mount points for this loop device
                result = subprocess.run(
                    ['mount'],
                    capture_output=True,
                    timeout=5,
                    text=True
                )
                
                for line in result.stdout.splitlines():
                    if loop_dev in line:
                        # Extract mount point (third field)
                        parts = line.split()
                        if len(parts) >= 3:
                            mount_point = parts[2]
                            logger.info(f"Unmounting {mount_point}")
                            subprocess.run(
                                ['sudo', 'nsenter', '--mount=/proc/1/ns/mnt', 'umount', mount_point],
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
                
                # Step 6: Sync filesystem
                logger.info("Syncing filesystem")
                subprocess.run(['sync'], timeout=5, check=True)
                time.sleep(0.5)  # Give sync time to complete
                
                return True, message
                
            finally:
                # Step 7: Always cleanup - unmount RW and remount RO
                logger.info("Cleaning up mounts")
                
                # Unmount read-write
                subprocess.run(
                    ['sudo', 'nsenter', '--mount=/proc/1/ns/mnt', 'umount', mount_rw],
                    capture_output=True,
                    timeout=5
                )
                
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
                
                # Restore file backing for LUN 1
                logger.info("Restoring file backing for LUN 1")
                subprocess.run(
                    ['sudo', 'sh', '-c', f'echo "{img_path}" > /sys/kernel/config/usb_gadget/*/functions/mass_storage.usb0/lun.1/file'],
                    capture_output=True,
                    timeout=5
                )
                
                # Rebind USB gadget
                logger.info("Rebinding USB gadget UDC")
                # Get UDC device name
                result = subprocess.run(
                    ['sh', '-c', 'ls /sys/class/udc | head -n1'],
                    capture_output=True,
                    timeout=5,
                    text=True
                )
                if result.returncode == 0 and result.stdout.strip():
                    udc_device = result.stdout.strip()
                    subprocess.run(
                        ['sudo', 'sh', '-c', f'echo "{udc_device}" > /sys/kernel/config/usb_gadget/*/UDC'],
                        capture_output=True,
                        timeout=5
                    )
                    logger.info(f"Rebound UDC: {udc_device}")
                
                logger.info("Quick edit part2 operation completed, read-only mount restored")
    
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
