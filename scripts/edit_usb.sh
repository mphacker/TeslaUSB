#!/bin/bash
set -euo pipefail

# edit_usb.sh - Switch to edit mode with local mounts and Samba
# This script removes the USB gadget, mounts partitions locally, and starts Samba

# Load configuration
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/config.sh"

# Check for active file operations before proceeding
LOCK_FILE="$GADGET_DIR/.quick_edit_part2.lock"
LOCK_TIMEOUT=30
LOCK_CHECK_START=$(date +%s)

if [ -f "$LOCK_FILE" ]; then
  echo "⚠️  File operation in progress (lock file detected)"
  echo "Waiting up to ${LOCK_TIMEOUT}s for operation to complete..."
  
  while [ -f "$LOCK_FILE" ]; do
    LOCK_AGE=$(($(date +%s) - LOCK_CHECK_START))
    
    if [ $LOCK_AGE -ge $LOCK_TIMEOUT ]; then
      # Check if lock is stale (older than 2 minutes)
      if [ -f "$LOCK_FILE" ]; then
        LOCK_FILE_AGE=$(($(date +%s) - $(stat -c %Y "$LOCK_FILE" 2>/dev/null || echo 0)))
        if [ $LOCK_FILE_AGE -gt 120 ]; then
          echo "⚠️  Removing stale lock file (age: ${LOCK_FILE_AGE}s)"
          rm -f "$LOCK_FILE"
          break
        fi
      fi
      
      echo "❌ ERROR: Cannot switch to edit mode - file operation still in progress" >&2
      echo "Please wait for current upload/download/scheduler operation to complete" >&2
      exit 1
    fi
    
    sleep 1
  done
  
  echo "✓ File operation completed, proceeding with mode switch"
fi

echo "Switching to edit mode (local mount + Samba)..."

# Stop thumbnail generator to prevent file access during mode switch
echo "Stopping thumbnail generator..."
sudo systemctl stop thumbnail_generator.service 2>/dev/null || true

# Get user IDs for mounting
UID_VAL=$(id -u "$TARGET_USER")
GID_VAL=$(id -g "$TARGET_USER")

safe_unmount_dir() {
  local target="$1"
  local attempt
  
  # Check if actually mounted in the system mount namespace
  if ! sudo nsenter --mount=/proc/1/ns/mnt mountpoint -q "$target" 2>/dev/null; then
    return 0
  fi

  # Try normal unmount in the system mount namespace
  for attempt in 1 2 3; do
    echo "  Unmounting $target (attempt $attempt)..."
    
    if sudo nsenter --mount=/proc/1/ns/mnt umount "$target" 2>/dev/null; then
      sleep 1
      # Verify it's actually gone
      if ! sudo nsenter --mount=/proc/1/ns/mnt mountpoint -q "$target" 2>/dev/null; then
        echo "  Successfully unmounted $target"
        return 0
      else
        echo "  WARNING: umount succeeded but mount still exists (multiple mounts?)"
      fi
    fi
    
    # Still mounted, wait before retry
    if [ $attempt -lt 3 ]; then
      sleep 2
    fi
  done

  # If still mounted, this is an error - don't continue
  echo "  ERROR: Cannot unmount $target after 3 attempts" >&2
  echo "  This mount must be cleared before edit mode can work" >&2
  return 1
}

# Remove gadget if active (with force to prevent hanging)
# First check for configfs gadget
CONFIGFS_GADGET="/sys/kernel/config/usb_gadget/teslausb"
if [ -d "$CONFIGFS_GADGET" ]; then
  echo "Removing configfs USB gadget..."
  # Sync all pending writes first
  sync
  sleep 1
  
  # Unbind UDC FIRST - this disconnects the gadget from USB before touching mounts
  if [ -f "$CONFIGFS_GADGET/UDC" ]; then
    echo "  Unbinding UDC..."
    echo "" | sudo tee "$CONFIGFS_GADGET/UDC" > /dev/null 2>&1 || true
    sleep 2
  fi
  
  # Remove function links
  echo "  Removing function links..."
  sudo rm -f "$CONFIGFS_GADGET"/configs/*/mass_storage.* 2>/dev/null || true
  
  # Remove configurations
  sudo rmdir "$CONFIGFS_GADGET"/configs/*/strings/* 2>/dev/null || true
  sudo rmdir "$CONFIGFS_GADGET"/configs/* 2>/dev/null || true
  
  # Remove LUNs from functions
  sudo rmdir "$CONFIGFS_GADGET"/functions/mass_storage.usb0/lun.* 2>/dev/null || true
  
  # Remove functions
  sudo rmdir "$CONFIGFS_GADGET"/functions/* 2>/dev/null || true
  
  # Remove strings
  sudo rmdir "$CONFIGFS_GADGET"/strings/* 2>/dev/null || true
  
  # Remove gadget
  sudo rmdir "$CONFIGFS_GADGET" 2>/dev/null || true
  
  echo "  Configfs gadget removed successfully"
  sleep 2
  
  # NOW unmount read-only mounts after gadget is fully disconnected
  echo "Unmounting read-only mounts from present mode..."
  RO_MNT_DIR="/mnt/gadget"
  for mp in "$RO_MNT_DIR/part1-ro" "$RO_MNT_DIR/part2-ro"; do
    if mountpoint -q "$mp" 2>/dev/null; then
      echo "  Unmounting $mp..."
      if ! safe_unmount_dir "$mp"; then
        echo "  ERROR: Could not unmount $mp even after disconnecting gadget"
        exit 1
      fi
    fi
  done
  
# Check for legacy g_mass_storage module
elif lsmod | grep -q '^g_mass_storage'; then
  echo "Removing legacy g_mass_storage module..."
  # Sync all pending writes first
  sync
  sleep 1
  
  # Unmount any read-only mounts from present mode first
  echo "Unmounting read-only mounts from present mode..."
  RO_MNT_DIR="/mnt/gadget"
  for mp in "$RO_MNT_DIR/part1-ro" "$RO_MNT_DIR/part2-ro"; do
    if mountpoint -q "$mp" 2>/dev/null; then
      echo "  Unmounting $mp..."
      if ! safe_unmount_dir "$mp"; then
        echo "  Warning: Could not cleanly unmount $mp"
      fi
    fi
  done
  
  # Try to unbind the UDC (USB Device Controller) first to cleanly disconnect
  UDC_DIR="/sys/class/udc"
  if [ -d "$UDC_DIR" ]; then
    for udc in "$UDC_DIR"/*; do
      if [ -e "$udc" ]; then
        UDC_NAME=$(basename "$udc")
        echo "  Unbinding UDC: $UDC_NAME"
        echo "" | sudo tee /sys/kernel/config/usb_gadget/*/UDC 2>/dev/null || true
      fi
    done
    sleep 1
  fi
  
  # Now try to remove the module
  echo "  Removing g_mass_storage module..."
  if sudo timeout 5 rmmod g_mass_storage 2>/dev/null; then
    echo "  USB gadget module removed successfully"
  else
    echo "  WARNING: Module removal timed out or failed. Forcing..."
    # Kill any processes holding the module
    sudo lsof 2>/dev/null | grep g_mass_storage | awk '{print $2}' | xargs -r sudo kill -9 2>/dev/null || true
    # Try one more time
    sudo rmmod -f g_mass_storage 2>/dev/null || true
  fi
  sleep 1
fi

# Ensure read-only mounts from present mode are unmounted (critical for mode switching)
# This runs regardless of which gadget type was active, as a safety measure
echo "Ensuring read-only mounts are cleared..."
RO_MNT_DIR="/mnt/gadget"
for mp in "$RO_MNT_DIR/part1-ro" "$RO_MNT_DIR/part2-ro"; do
  if mountpoint -q "$mp" 2>/dev/null; then
    echo "  Unmounting $mp..."
    if ! safe_unmount_dir "$mp"; then
      echo "  WARNING: Could not cleanly unmount $mp, but continuing anyway..." >&2
    else
      echo "  Successfully unmounted $mp"
    fi
  fi
done

# Wait for lazy unmounts to complete
echo "Waiting for mounts to fully release..."
sleep 3
sync

# Clean up any stale loop devices that might still be attached to the read-only mounts
# This is important because loop devices from present mode might still be active
echo "Cleaning up loop devices from present mode..."
for existing in $(sudo losetup -j "$IMG_CAM" 2>/dev/null | cut -d: -f1); do
  if [ -n "$existing" ]; then
    echo "  Detaching loop device (TeslaCam): $existing"
    sudo losetup -d "$existing" 2>/dev/null || true
  fi
done
for existing in $(sudo losetup -j "$IMG_LIGHTSHOW" 2>/dev/null | cut -d: -f1); do
  if [ -n "$existing" ]; then
    echo "  Detaching loop device (Lightshow): $existing"
    sudo losetup -d "$existing" 2>/dev/null || true
  fi
done

sync
sleep 2

# Prepare mount points
echo "Preparing mount points..."
sudo mkdir -p "$MNT_DIR/part1" "$MNT_DIR/part2"
sudo chown "$TARGET_USER:$TARGET_USER" "$MNT_DIR/part1" "$MNT_DIR/part2"

# Ensure previous mounts are cleared before setting up new loop devices
# This prevents remounting while partitions are still in use
for PART_NUM in 1 2; do
  MP="$MNT_DIR/part${PART_NUM}"
  if mountpoint -q "$MP" 2>/dev/null; then
    echo "Unmounting existing mount at $MP"
    if ! safe_unmount_dir "$MP"; then
      echo "Error: could not clear existing mount at $MP" >&2
      exit 1
    fi
  fi
done

# Ensure all pending operations complete before setting up new loop devices
sync
sleep 1

# Setup loop device for TeslaCam image (part1)
echo "Setting up loop device for TeslaCam..."
LOOP_CAM=$(sudo losetup --show -f "$IMG_CAM")
if [ -z "$LOOP_CAM" ]; then
  echo "ERROR: Failed to create loop device for $IMG_CAM"
  exit 1
fi
echo "Using loop device for TeslaCam: $LOOP_CAM"

# Verify the loop device is actually attached to our image
VERIFY=$(sudo losetup -l | grep "$LOOP_CAM" | grep "$IMG_CAM" || true)
if [ -z "$VERIFY" ]; then
  echo "ERROR: Loop device $LOOP_CAM is not attached to $IMG_CAM"
  sudo losetup -d "$LOOP_CAM" 2>/dev/null || true
  exit 1
fi
echo "Verified: $LOOP_CAM is attached to $IMG_CAM"

# Setup loop device for Lightshow image (part2)
echo "Setting up loop device for Lightshow..."
LOOP_LIGHTSHOW=$(sudo losetup --show -f "$IMG_LIGHTSHOW")
if [ -z "$LOOP_LIGHTSHOW" ]; then
  echo "ERROR: Failed to create loop device for $IMG_LIGHTSHOW"
  sudo losetup -d "$LOOP_CAM" 2>/dev/null || true
  exit 1
fi
echo "Using loop device for Lightshow: $LOOP_LIGHTSHOW"

# Verify the loop device is actually attached to our image
VERIFY=$(sudo losetup -l | grep "$LOOP_LIGHTSHOW" | grep "$IMG_LIGHTSHOW" || true)
if [ -z "$VERIFY" ]; then
  echo "ERROR: Loop device $LOOP_LIGHTSHOW is not attached to $IMG_LIGHTSHOW"
  sudo losetup -d "$LOOP_CAM" 2>/dev/null || true
  sudo losetup -d "$LOOP_LIGHTSHOW" 2>/dev/null || true
  exit 1
fi
echo "Verified: $LOOP_LIGHTSHOW is attached to $IMG_LIGHTSHOW"

sleep 0.5

# Trap to clean up loop devices only on script failure (not on successful exit)
cleanup_loops_on_failure() {
  local exit_code=$?
  if [ $exit_code -ne 0 ]; then
    echo "Script failed with exit code $exit_code, cleaning up loop devices..."
    [ -n "${LOOP_CAM:-}" ] && sudo losetup -d "$LOOP_CAM" 2>/dev/null || true
    [ -n "${LOOP_LIGHTSHOW:-}" ] && sudo losetup -d "$LOOP_LIGHTSHOW" 2>/dev/null || true
  fi
}
trap cleanup_loops_on_failure EXIT

# Run filesystem checks before mounting to auto-repair filesystem issues
echo "Running filesystem checks..."

# Check TeslaCam partition
echo "  Checking $LOOP_CAM (TeslaCam)..."
FS_TYPE=$(sudo blkid -o value -s TYPE "$LOOP_CAM" 2>/dev/null || echo "unknown")
echo "  Filesystem type: $FS_TYPE"

LOG_FILE="/tmp/fsck_gadget_part1.log"
set +e
if [ "$FS_TYPE" = "vfat" ]; then
  sudo fsck.vfat -a "$LOOP_CAM" >"$LOG_FILE" 2>&1
  FSCK_STATUS=$?
elif [ "$FS_TYPE" = "exfat" ]; then
  sudo fsck.exfat -a "$LOOP_CAM" >"$LOG_FILE" 2>&1
  FSCK_STATUS=$?
else
  echo "  Warning: Unknown filesystem type '$FS_TYPE', skipping fsck"
  FSCK_STATUS=0
fi
set -e

if [ $FSCK_STATUS -ge 4 ]; then
  echo "  Critical filesystem errors detected on ${LOOP_CAM}. See $LOG_FILE" >&2
  exit 1
fi

if [ $FSCK_STATUS -eq 0 ]; then
  rm -f "$LOG_FILE"
else
  echo "  Filesystem repairs applied on ${LOOP_CAM}. Details saved to $LOG_FILE"
fi

# Check Lightshow partition
echo "  Checking $LOOP_LIGHTSHOW (Lightshow)..."
FS_TYPE=$(sudo blkid -o value -s TYPE "$LOOP_LIGHTSHOW" 2>/dev/null || echo "unknown")
echo "  Filesystem type: $FS_TYPE"

LOG_FILE="/tmp/fsck_gadget_part2.log"
set +e
if [ "$FS_TYPE" = "vfat" ]; then
  sudo fsck.vfat -a "$LOOP_LIGHTSHOW" >"$LOG_FILE" 2>&1
  FSCK_STATUS=$?
elif [ "$FS_TYPE" = "exfat" ]; then
  sudo fsck.exfat -a "$LOOP_LIGHTSHOW" >"$LOG_FILE" 2>&1
  FSCK_STATUS=$?
else
  echo "  Warning: Unknown filesystem type '$FS_TYPE', skipping fsck"
  FSCK_STATUS=0
fi
set -e

if [ $FSCK_STATUS -ge 4 ]; then
  echo "  Critical filesystem errors detected on ${LOOP_LIGHTSHOW}. See $LOG_FILE" >&2
  exit 1
fi

if [ $FSCK_STATUS -eq 0 ]; then
  rm -f "$LOG_FILE"
else
  echo "  Filesystem repairs applied on ${LOOP_LIGHTSHOW}. Details saved to $LOG_FILE"
fi

# Mount partitions
echo "Mounting partitions..."

# Mount TeslaCam partition (part1) in system mount namespace
MP="$MNT_DIR/part1"
FS_TYPE=$(sudo blkid -o value -s TYPE "$LOOP_CAM" 2>/dev/null || echo "unknown")
echo "  Mounting $LOOP_CAM at $MP..."

if [ "$FS_TYPE" = "exfat" ]; then
  sudo nsenter --mount=/proc/1/ns/mnt mount -t exfat -o rw,uid=$UID_VAL,gid=$GID_VAL,umask=000 "$LOOP_CAM" "$MP"
elif [ "$FS_TYPE" = "vfat" ]; then
  sudo nsenter --mount=/proc/1/ns/mnt mount -t vfat -o rw,uid=$UID_VAL,gid=$GID_VAL,umask=000 "$LOOP_CAM" "$MP"
else
  echo "  Warning: Unknown filesystem type '$FS_TYPE', attempting generic mount"
  sudo nsenter --mount=/proc/1/ns/mnt mount -o rw "$LOOP_CAM" "$MP"
fi

if ! sudo nsenter --mount=/proc/1/ns/mnt mountpoint -q "$MP"; then
  echo "Error: Failed to mount $LOOP_CAM at $MP" >&2
  exit 1
fi
echo "  Mounted $LOOP_CAM at $MP (filesystem: $FS_TYPE)"

# Mount Lightshow partition (part2) in system mount namespace
MP="$MNT_DIR/part2"
FS_TYPE=$(sudo blkid -o value -s TYPE "$LOOP_LIGHTSHOW" 2>/dev/null || echo "unknown")
echo "  Mounting $LOOP_LIGHTSHOW at $MP..."

if [ "$FS_TYPE" = "exfat" ]; then
  sudo nsenter --mount=/proc/1/ns/mnt mount -t exfat -o rw,uid=$UID_VAL,gid=$GID_VAL,umask=000 "$LOOP_LIGHTSHOW" "$MP"
elif [ "$FS_TYPE" = "vfat" ]; then
  sudo nsenter --mount=/proc/1/ns/mnt mount -t vfat -o rw,uid=$UID_VAL,gid=$GID_VAL,umask=000 "$LOOP_LIGHTSHOW" "$MP"
else
  echo "  Warning: Unknown filesystem type '$FS_TYPE', attempting generic mount"
  sudo nsenter --mount=/proc/1/ns/mnt mount -o rw "$LOOP_LIGHTSHOW" "$MP"
fi

if ! sudo nsenter --mount=/proc/1/ns/mnt mountpoint -q "$MP"; then
  echo "Error: Failed to mount $LOOP_LIGHTSHOW at $MP" >&2
  exit 1
fi
echo "  Mounted $LOOP_LIGHTSHOW at $MP (filesystem: $FS_TYPE)"

# Refresh Samba so shares expose the freshly mounted partitions
echo "Refreshing Samba shares..."
# Force close any cached shares
sudo smbcontrol all close-share gadget_part1 2>/dev/null || true
sudo smbcontrol all close-share gadget_part2 2>/dev/null || true
# Reload Samba configuration
sudo smbcontrol all reload-config 2>/dev/null || true
# Restart Samba services to ensure they see the new mounts
sudo systemctl restart smbd nmbd 2>/dev/null || true
# Give Samba a moment to initialize
sleep 2
# Verify mounts are accessible
if [ -d "$MNT_DIR/part1" ]; then
  echo "  Part1 files: $(ls -A "$MNT_DIR/part1" 2>/dev/null | wc -l) items"
fi
if [ -d "$MNT_DIR/part2" ]; then
  echo "  Part2 files: $(ls -A "$MNT_DIR/part2" 2>/dev/null | wc -l) items"
fi

echo "Updating mode state..."
echo "edit" > "$STATE_FILE"
chown "$TARGET_USER:$TARGET_USER" "$STATE_FILE" 2>/dev/null || true

echo "Ensuring buffered writes are flushed..."
sync

echo "Edit mode activated successfully!"
echo "Partitions are now mounted locally and accessible via Samba shares:"
echo "  - Part 1: $MNT_DIR/part1"
echo "  - Part 2: $MNT_DIR/part2"
echo "  - Samba shares: gadget_part1, gadget_part2"