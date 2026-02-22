#!/bin/bash
set -euo pipefail

# edit_usb.sh - Switch to edit mode with local mounts and Samba
# This script removes the USB gadget, mounts drives locally, and starts Samba

# Performance tracking
SCRIPT_START=$(date +%s%3N)
log_timing() {
  local label="$1"
  local now=$(date +%s%3N)
  local elapsed=$((now - SCRIPT_START))
  echo "[TIMING] ${label}: ${elapsed}ms ($(date '+%H:%M:%S.%3N'))"
}

# Smart wait: polls until condition is true or timeout (much faster than fixed sleep)
# Usage: wait_until "test -condition" 5 "description"
wait_until() {
  local check_cmd="$1"
  local max_wait="$2"
  local desc="${3:-operation}"
  local start=$(date +%s%3N)
  local deadline=$((start + max_wait * 1000))

  while ! eval "$check_cmd" 2>/dev/null; do
    local now=$(date +%s%3N)
    if [ $now -ge $deadline ]; then
      return 1
    fi
    sleep 0.05
  done
  return 0
}

# Create a fresh loop device for an image file
# After clearing gadget LUN files and unmounting, existing loop devices will have
# AUTOCLEAR set and will be automatically destroyed. We create fresh ones.
# Usage: LOOP_DEV=$(create_loop "/path/to/image.img")
create_loop() {
  local img="$1"
  sudo losetup --show -f "$img"
}

log_timing "Script start"

# Load configuration
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/config.sh"

MUSIC_ENABLED_LC="$(printf '%s' "${MUSIC_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')"
MUSIC_ENABLED_BOOL=0
[ "$MUSIC_ENABLED_LC" = "true" ] && MUSIC_ENABLED_BOOL=1

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
    if sudo nsenter --mount=/proc/1/ns/mnt umount "$target" 2>/dev/null; then
      # Quick poll to verify unmount (much faster than fixed sleep)
      if wait_until "! sudo nsenter --mount=/proc/1/ns/mnt mountpoint -q '$target'" 1 "verify unmount"; then
        return 0
      fi
      echo "  WARNING: umount succeeded but mount still exists (multiple mounts?)"
    fi

    # Still mounted, brief pause before retry
    [ $attempt -lt 3 ] && sleep 0.2
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
  # Brief pause for filesystem stability (reduced from 1s)
  sleep 0.2

  # Unbind UDC FIRST - this disconnects the gadget from USB before touching mounts
  if [ -f "$CONFIGFS_GADGET/UDC" ]; then
    echo "  Unbinding UDC..."
    echo "" | sudo tee "$CONFIGFS_GADGET/UDC" > /dev/null 2>&1 || true
    # Brief settle time (reduced from 2s - unbind is synchronous)
    sleep 0.5
  fi

  # Clear LUN backing files BEFORE removing functions
  # This releases the kernel's file references to the image files
  echo "  Clearing LUN backing files..."
  for lun in "$CONFIGFS_GADGET"/functions/mass_storage.usb0/lun.*; do
    if [ -f "$lun/file" ]; then
      echo "" | sudo tee "$lun/file" > /dev/null 2>&1 || true
    fi
  done
  sleep 0.2

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
  # Brief settle time (reduced from 2s)
  sleep 0.5

  # NOW unmount read-only mounts after gadget is fully disconnected
  echo "Unmounting read-only mounts from present mode..."
  RO_MNT_DIR="/mnt/gadget"
  RO_UNMOUNT_TARGETS=("$RO_MNT_DIR/part1-ro" "$RO_MNT_DIR/part2-ro")
  if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
    RO_UNMOUNT_TARGETS+=("$RO_MNT_DIR/part3-ro")
  fi
  for mp in "${RO_UNMOUNT_TARGETS[@]}"; do
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
  LEGACY_RO_TARGETS=("$RO_MNT_DIR/part1-ro" "$RO_MNT_DIR/part2-ro")
  if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
    LEGACY_RO_TARGETS+=("$RO_MNT_DIR/part3-ro")
  fi
  for mp in "${LEGACY_RO_TARGETS[@]}"; do
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

# Verify all mounts are released (quick check - already unmounted above)
RO_MNT_DIR="/mnt/gadget"
VERIFY_RO_TARGETS=("$RO_MNT_DIR/part1-ro" "$RO_MNT_DIR/part2-ro")
if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
  VERIFY_RO_TARGETS+=("$RO_MNT_DIR/part3-ro")
fi
for mp in "${VERIFY_RO_TARGETS[@]}"; do
  if sudo nsenter --mount=/proc/1/ns/mnt mountpoint -q "$mp" 2>/dev/null; then
    echo "  Clearing remaining mount: $mp"
    safe_unmount_dir "$mp" || true
  fi
done
log_timing "Mounts released"

# Detach all existing loop devices for our images
# After clearing LUN files and unmounting, loop devices may still exist
# We must detach them before creating fresh ones to avoid accumulation
echo "Cleaning up existing loop devices..."
LOOP_IMAGES=("$IMG_CAM" "$IMG_LIGHTSHOW")
if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
  LOOP_IMAGES+=("$IMG_MUSIC")
fi
for img in "${LOOP_IMAGES[@]}"; do
  for loop in $(losetup -j "$img" 2>/dev/null | cut -d: -f1); do
    if [ -n "$loop" ]; then
      echo "  Detaching $loop..."
      sudo losetup -d "$loop" 2>/dev/null || true
    fi
  done
done
# Brief pause for loop device cleanup to complete
sleep 0.3
log_timing "Loop devices cleaned up"

# Prepare mount points
echo "Preparing mount points..."
sudo mkdir -p "$MNT_DIR/part1" "$MNT_DIR/part2"
sudo chown "$TARGET_USER:$TARGET_USER" "$MNT_DIR/part1" "$MNT_DIR/part2"

# Ensure previous mounts are cleared before setting up new loop devices
# This prevents remounting while drives are still in use
PART_RANGE=(1 2)
if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
  PART_RANGE+=(3)
fi

for PART_NUM in "${PART_RANGE[@]}"; do
  MP="$MNT_DIR/part${PART_NUM}"
  if mountpoint -q "$MP" 2>/dev/null; then
    echo "Unmounting existing mount at $MP"
    if ! safe_unmount_dir "$MP"; then
      echo "Error: could not clear existing mount at $MP" >&2
      exit 1
    fi
  fi
done

# Ensure all pending operations complete before setting up loop devices
sync
# Brief pause for stability (reduced from 1s)
sleep 0.2

# Setup loop device for TeslaCam image (part1)
echo "Setting up loop device for TeslaCam..."
LOOP_CAM=$(create_loop "$IMG_CAM")
if [ -z "$LOOP_CAM" ]; then
  echo "ERROR: Failed to get/create loop device for $IMG_CAM"
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
LOOP_LIGHTSHOW=$(create_loop "$IMG_LIGHTSHOW")
if [ -z "$LOOP_LIGHTSHOW" ]; then
  echo "ERROR: Failed to get/create loop device for $IMG_LIGHTSHOW"
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

if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
  echo "Setting up loop device for Music..."
  LOOP_MUSIC=$(create_loop "$IMG_MUSIC")
  if [ -z "$LOOP_MUSIC" ]; then
    echo "ERROR: Failed to get/create loop device for $IMG_MUSIC"
    sudo losetup -d "$LOOP_CAM" 2>/dev/null || true
    sudo losetup -d "$LOOP_LIGHTSHOW" 2>/dev/null || true
    exit 1
  fi
  echo "Using loop device for Music: $LOOP_MUSIC"

  VERIFY=$(sudo losetup -l | grep "$LOOP_MUSIC" | grep "$IMG_MUSIC" || true)
  if [ -z "$VERIFY" ]; then
    echo "ERROR: Loop device $LOOP_MUSIC is not attached to $IMG_MUSIC"
    sudo losetup -d "$LOOP_CAM" 2>/dev/null || true
    sudo losetup -d "$LOOP_LIGHTSHOW" 2>/dev/null || true
    sudo losetup -d "$LOOP_MUSIC" 2>/dev/null || true
    exit 1
  fi
  echo "Verified: $LOOP_MUSIC is attached to $IMG_MUSIC"
fi

sleep 0.5

# Trap to log on failure but NOT detach loop devices (they may be reused/shared)
log_failure_on_exit() {
  local exit_code=$?
  if [ $exit_code -ne 0 ]; then
    echo "Script failed with exit code $exit_code"
    echo "Loop devices preserved for debugging:"
    sudo losetup -l | head -5
  fi
}
trap log_failure_on_exit EXIT

# Filesystem checks removed from mode switching for faster operation
# Use the web interface Analytics page to run manual filesystem checks

# Mount drives
echo "Mounting drives..."

# Ensure mount points exist (present mode may remove them)
if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
  sudo mkdir -p "$MNT_DIR/part1" "$MNT_DIR/part2" "$MNT_DIR/part3"
else
  sudo mkdir -p "$MNT_DIR/part1" "$MNT_DIR/part2"
fi
sudo chown "$TARGET_USER:$TARGET_USER" "$MNT_DIR/part1" "$MNT_DIR/part2" 2>/dev/null || true
if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
  sudo chown "$TARGET_USER:$TARGET_USER" "$MNT_DIR/part3" 2>/dev/null || true
fi

# Mount TeslaCam drive (part1) in system mount namespace
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

# Mount Lightshow drive (part2) in system mount namespace
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

if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
  echo "Mounting Music drive (part3) in system mount namespace"
  MP="$MNT_DIR/part3"
  FS_TYPE=$(sudo blkid -o value -s TYPE "$LOOP_MUSIC" 2>/dev/null || echo "unknown")
  echo "  Mounting $LOOP_MUSIC at $MP..."

  if [ "$FS_TYPE" = "exfat" ]; then
    sudo nsenter --mount=/proc/1/ns/mnt mount -t exfat -o rw,uid=$UID_VAL,gid=$GID_VAL,umask=000 "$LOOP_MUSIC" "$MP"
  elif [ "$FS_TYPE" = "vfat" ]; then
    sudo nsenter --mount=/proc/1/ns/mnt mount -t vfat -o rw,uid=$UID_VAL,gid=$GID_VAL,umask=000 "$LOOP_MUSIC" "$MP"
  else
    echo "  Warning: Unknown filesystem type '$FS_TYPE', attempting generic mount"
    sudo nsenter --mount=/proc/1/ns/mnt mount -o rw "$LOOP_MUSIC" "$MP"
  fi

  if ! sudo nsenter --mount=/proc/1/ns/mnt mountpoint -q "$MP"; then
    echo "Error: Failed to mount $LOOP_MUSIC at $MP" >&2
    exit 1
  fi
  echo "  Mounted $LOOP_MUSIC at $MP (filesystem: $FS_TYPE)"
fi

# Refresh Samba so shares expose the freshly mounted drives
echo "Refreshing Samba shares..."
# Close any cached shares and reload config (faster than full restart)
sudo smbcontrol all close-share gadget_part1 2>/dev/null || true
sudo smbcontrol all close-share gadget_part2 2>/dev/null || true
if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
  sudo smbcontrol all close-share gadget_part3 2>/dev/null || true
fi
# If Samba is running, reload config is sufficient; otherwise start it
if systemctl is-active --quiet smbd; then
  sudo smbcontrol all reload-config 2>/dev/null || true
else
  sudo systemctl start smbd nmbd 2>/dev/null || true
  wait_until "systemctl is-active --quiet smbd" 2 "Samba startup" || true
fi
log_timing "Samba refreshed"
# Verify mounts are accessible
if [ -d "$MNT_DIR/part1" ]; then
  echo "  Part1 files: $(ls -A "$MNT_DIR/part1" 2>/dev/null | wc -l) items"
fi
if [ -d "$MNT_DIR/part2" ]; then
  echo "  Part2 files: $(ls -A "$MNT_DIR/part2" 2>/dev/null | wc -l) items"
fi
if [ $MUSIC_ENABLED_BOOL -eq 1 ] && [ -d "$MNT_DIR/part3" ]; then
  echo "  Part3 files: $(ls -A "$MNT_DIR/part3" 2>/dev/null | wc -l) items"
fi

echo "Updating mode state..."
echo "edit" > "$STATE_FILE"
chown "$TARGET_USER:$TARGET_USER" "$STATE_FILE" 2>/dev/null || true

echo "Ensuring buffered writes are flushed..."
sync

echo "Edit mode activated successfully!"
echo "Drives are now mounted locally and accessible via Samba shares:"
echo "  - Part 1: $MNT_DIR/part1"
echo "  - Part 2: $MNT_DIR/part2"
echo "  - Samba shares: gadget_part1, gadget_part2"
if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
  echo "  - Part 3: $MNT_DIR/part3"
  echo "  - Samba shares: gadget_part3 (music)"
fi

log_timing "Script completed successfully"
echo "[PERFORMANCE] Total execution time: $(($(date +%s%3N) - SCRIPT_START))ms"
