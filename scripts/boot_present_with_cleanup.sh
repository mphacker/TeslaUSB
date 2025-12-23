#!/bin/bash
set -euo pipefail

# boot_present_with_cleanup.sh - Boot-time wrapper that runs cleanup before presenting USB
# This script is called by present_usb_on_boot.service
# It checks if cleanup is needed, runs it if enabled, then calls present_usb.sh

# ===== BOOT PERFORMANCE TIMING =====
BOOT_START_MS=$(date +%s%3N)
log_timing() {
    local checkpoint="$1"
    local now_ms=$(date +%s%3N)
    local elapsed=$((now_ms - BOOT_START_MS))
    echo "[BOOT TIMING] +${elapsed}ms: $checkpoint"
}
# ====================================

log_timing "Script started"

# Load configuration
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
log_timing "Script dir resolved"

source "$SCRIPT_DIR/config.sh"
log_timing "Config loaded"

CLEANUP_CONFIG="$GADGET_DIR/cleanup_config.json"
CLEANUP_SCRIPT="$GADGET_DIR/scripts/run_boot_cleanup.py"
LOG_FILE="$GADGET_DIR/boot_cleanup.log"

echo "===== Boot-time USB presentation with optional cleanup ====="
echo "$(date)"
log_timing "Variables initialized"

# Function to check if any folder has cleanup enabled
needs_cleanup() {
    log_timing "Checking cleanup config"
    if [ ! -f "$CLEANUP_CONFIG" ]; then
        echo "No cleanup config found, skipping cleanup"
        log_timing "No cleanup config found"
        return 1
    fi

    # Check if any folder has "enabled": true
    if grep -q '"enabled": true' "$CLEANUP_CONFIG" 2>/dev/null; then
        echo "Cleanup enabled for at least one folder"
        log_timing "Cleanup enabled detected"
        return 0
    else
        echo "No folders have cleanup enabled, skipping cleanup"
        log_timing "Cleanup not enabled"
        return 1
    fi
}

# Function to run cleanup with minimal filesystem setup
run_cleanup() {
    log_timing "Starting cleanup process"
    echo "Running automatic cleanup before presenting USB..."

    # Mount partitions read-write for cleanup
    log_timing "Setting up loop devices for cleanup"
    echo "Mounting partitions read-write for cleanup..."

    # Note: Images are single-partition filesystems, not partitioned disks
    # So we mount the loop device directly, not loop devicep1
    LOOP1=$(sudo losetup --find --show "$IMG_CAM")
    LOOP2=$(sudo losetup --find --show "$IMG_LIGHTSHOW")

    # Define mount points
    MNT_PART1="$MNT_DIR/part1"
    MNT_PART2="$MNT_DIR/part2"

    # Create mount points if needed
    sudo mkdir -p "$MNT_PART1" "$MNT_PART2"

    # Get filesystem types
    FS_TYPE1=$(sudo blkid -o value -s TYPE "$LOOP1" 2>/dev/null || echo "unknown")
    FS_TYPE2=$(sudo blkid -o value -s TYPE "$LOOP2" 2>/dev/null || echo "unknown")

    # Mount partition 1 (TeslaCam)
    if [ "$FS_TYPE1" = "exfat" ]; then
        sudo nsenter --mount=/proc/1/ns/mnt mount -t exfat -o rw,uid=1000,gid=1000,umask=000 "$LOOP1" "$MNT_PART1"
    else
        sudo nsenter --mount=/proc/1/ns/mnt mount -t vfat -o rw,uid=1000,gid=1000,umask=000 "$LOOP1" "$MNT_PART1"
    fi

    # Mount partition 2 (Lightshows/Chimes)
    if [ "$FS_TYPE2" = "exfat" ]; then
        sudo nsenter --mount=/proc/1/ns/mnt mount -t exfat -o rw,uid=1000,gid=1000,umask=000 "$LOOP2" "$MNT_PART2"
    else
        sudo nsenter --mount=/proc/1/ns/mnt mount -t vfat -o rw,uid=1000,gid=1000,umask=000 "$LOOP2" "$MNT_PART2"
    fi

    log_timing "Partitions mounted, starting cleanup script"
    echo "Partitions mounted, running cleanup script..."

    # Run cleanup script
    /usr/bin/python3 "$CLEANUP_SCRIPT" 2>&1 | tee -a "$LOG_FILE"
    CLEANUP_RESULT=${PIPESTATUS[0]}

    # Flush writes
    sync
    sleep 1

    # Unmount partitions
    log_timing "Cleanup script finished, unmounting"
    echo "Cleanup complete, unmounting partitions..."
    sudo nsenter --mount=/proc/1/ns/mnt umount "$MNT_PART1" || true
    sudo nsenter --mount=/proc/1/ns/mnt umount "$MNT_PART2" || true

    # Detach loops
    sudo losetup -d "$LOOP1" || true
    sudo losetup -d "$LOOP2" || true

    log_timing "Cleanup unmount complete (code=$CLEANUP_RESULT)"
    if [ $CLEANUP_RESULT -eq 0 ]; then
        echo "Cleanup completed successfully"
    else
        echo "Warning: Cleanup script returned error code $CLEANUP_RESULT"
    fi

    return $CLEANUP_RESULT
}

# Function to select random chime if random mode is enabled
select_random_chime() {
    log_timing "Checking random chime mode"
    echo "Checking if random chime mode is enabled..."

    RANDOM_CHIME_SCRIPT="$GADGET_DIR/scripts/select_random_chime.py"

    if [ ! -f "$RANDOM_CHIME_SCRIPT" ]; then
        echo "Random chime script not found, skipping"
        return 0
    fi

    # Mount part2 read-write so we can set the chime
    echo "Mounting part2 for random chime selection..."
    LOOP2=$(sudo losetup --find --show "$IMG_LIGHTSHOW")
    MNT_PART2="$MNT_DIR/part2"
    sudo mkdir -p "$MNT_PART2"

    # Get filesystem type
    FS_TYPE2=$(sudo blkid -o value -s TYPE "$LOOP2" 2>/dev/null || echo "unknown")

    # Mount partition 2 (Lightshows/Chimes)
    if [ "$FS_TYPE2" = "exfat" ]; then
        sudo nsenter --mount=/proc/1/ns/mnt mount -t exfat -o rw,uid=1000,gid=1000,umask=000 "$LOOP2" "$MNT_PART2"
    else
        sudo nsenter --mount=/proc/1/ns/mnt mount -t vfat -o rw,uid=1000,gid=1000,umask=000 "$LOOP2" "$MNT_PART2"
    fi

    # Run random chime selector
    /usr/bin/python3 "$RANDOM_CHIME_SCRIPT"
    RESULT=$?

    if [ $RESULT -eq 0 ]; then
        echo "Random chime selection completed successfully"
    else
        echo "Random chime selection skipped or failed (code $RESULT)"
    fi

    # Flush writes and unmount
    sync
    sleep 1
    sudo nsenter --mount=/proc/1/ns/mnt umount "$MNT_PART2" || true
    sudo losetup -d "$LOOP2" || true

    log_timing "Random chime selection complete"
    return 0  # Don't fail boot if random chime has issues
}

# Main logic
if needs_cleanup; then
    run_cleanup || echo "Cleanup encountered errors but continuing with USB presentation..."
else
    log_timing "Skipping cleanup (not enabled)"
    echo "Skipping cleanup (not enabled)"
fi

# Select random chime if random mode is enabled
echo ""
select_random_chime

# Now run the normal present script
echo ""
log_timing "Boot wrapper complete, executing present_usb.sh"
echo "Proceeding with USB gadget presentation..."
echo "[BOOT TIMING] Total boot wrapper time: $(($(date +%s%3N) - BOOT_START_MS))ms"
exec "$SCRIPT_DIR/present_usb.sh"
