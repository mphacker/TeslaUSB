#!/bin/bash
set -euo pipefail

# edit_usb.sh - Switch to edit mode with local mounts and Samba
# This script removes the USB gadget, mounts partitions locally, and starts Samba

# Configuration (will be updated by setup-usb.sh)
IMG_CAM="__GADGET_DIR__/__IMG_CAM_NAME__"
IMG_LIGHTSHOW="__GADGET_DIR__/__IMG_LIGHTSHOW_NAME__"
MNT_DIR="__MNT_DIR__"
TARGET_USER="__TARGET_USER__"
STATE_FILE="__GADGET_DIR__/state.txt"

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
  if ! mountpoint -q "$target" 2>/dev/null; then
    return 0
  fi

  for attempt in 1 2 3; do
    if sudo umount "$target" 2>/dev/null; then
      echo "  Unmounted $target"
      return 0
    fi
    echo "  $target busy (attempt $attempt). Terminating remaining clients..."
    sudo fuser -km "$target" 2>/dev/null || true
    sleep 1
  done

  echo "  Unable to unmount $target cleanly; forcing lazy unmount..."
  sudo umount -lf "$target" 2>/dev/null || true
  sleep 1

  if mountpoint -q "$target" 2>/dev/null; then
    echo "Error: $target still mounted after forced unmount." >&2
    return 1
  fi

  echo "  Lazy unmount succeeded for $target"
  return 0
}

# Remove gadget if active (with force to prevent hanging)
# First check for configfs gadget
CONFIGFS_GADGET="/sys/kernel/config/usb_gadget/teslausb"
if [ -d "$CONFIGFS_GADGET" ]; then
  echo "Removing configfs USB gadget..."
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
  
  # Unbind UDC first
  if [ -f "$CONFIGFS_GADGET/UDC" ]; then
    echo "  Unbinding UDC..."
    echo "" | sudo tee "$CONFIGFS_GADGET/UDC" > /dev/null 2>&1 || true
    sleep 1
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
  sleep 1
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

# Clean up stale loop devices tied to the images
echo "Cleaning up stale loop devices..."
for existing in $(losetup -j "$IMG_CAM" 2>/dev/null | cut -d: -f1); do
  if [ -n "$existing" ]; then
    echo "  Detaching stale loop device (TeslaCam): $existing"
    sudo losetup -d "$existing" 2>/dev/null || true
  fi
done
for existing in $(losetup -j "$IMG_LIGHTSHOW" 2>/dev/null | cut -d: -f1); do
  if [ -n "$existing" ]; then
    echo "  Detaching stale loop device (Lightshow): $existing"
    sudo losetup -d "$existing" 2>/dev/null || true
  fi
done

# Ensure all pending operations complete
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

# Mount TeslaCam partition (part1)
MP="$MNT_DIR/part1"
FS_TYPE=$(sudo blkid -o value -s TYPE "$LOOP_CAM" 2>/dev/null || echo "unknown")
echo "  Mounting $LOOP_CAM at $MP..."

if [ "$FS_TYPE" = "exfat" ]; then
  sudo mount -t exfat -o rw,uid=$UID_VAL,gid=$GID_VAL,umask=000 "$LOOP_CAM" "$MP"
elif [ "$FS_TYPE" = "vfat" ]; then
  sudo mount -t vfat -o rw,uid=$UID_VAL,gid=$GID_VAL,umask=000 "$LOOP_CAM" "$MP"
else
  echo "  Warning: Unknown filesystem type '$FS_TYPE', attempting generic mount"
  sudo mount -o rw "$LOOP_CAM" "$MP"
fi

if ! mountpoint -q "$MP"; then
  echo "Error: Failed to mount $LOOP_CAM at $MP" >&2
  exit 1
fi
echo "  Mounted $LOOP_CAM at $MP (filesystem: $FS_TYPE)"

# Mount Lightshow partition (part2)
MP="$MNT_DIR/part2"
FS_TYPE=$(sudo blkid -o value -s TYPE "$LOOP_LIGHTSHOW" 2>/dev/null || echo "unknown")
echo "  Mounting $LOOP_LIGHTSHOW at $MP..."

if [ "$FS_TYPE" = "exfat" ]; then
  sudo mount -t exfat -o rw,uid=$UID_VAL,gid=$GID_VAL,umask=000 "$LOOP_LIGHTSHOW" "$MP"
elif [ "$FS_TYPE" = "vfat" ]; then
  sudo mount -t vfat -o rw,uid=$UID_VAL,gid=$GID_VAL,umask=000 "$LOOP_LIGHTSHOW" "$MP"
else
  echo "  Warning: Unknown filesystem type '$FS_TYPE', attempting generic mount"
  sudo mount -o rw "$LOOP_LIGHTSHOW" "$MP"
fi

if ! mountpoint -q "$MP"; then
  echo "Error: Failed to mount $LOOP_LIGHTSHOW at $MP" >&2
  exit 1
fi
echo "  Mounted $LOOP_LIGHTSHOW at $MP (filesystem: $FS_TYPE)"

# Refresh Samba so shares expose the freshly mounted partitions
echo "Refreshing Samba shares..."
sudo smbcontrol all close-share gadget_part1 2>/dev/null || true
sudo smbcontrol all close-share gadget_part2 2>/dev/null || true
sudo systemctl restart smbd || true
sudo systemctl restart nmbd || true

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