#!/bin/bash
set -euo pipefail

# present_usb.sh - Present USB gadget with dual-LUN configuration
# This script unmounts local mounts, presents the USB gadget with optimized read-only settings on LUN 1

# Performance tracking
SCRIPT_START=$(date +%s%3N)
log_timing() {
  local label="$1"
  local now=$(date +%s%3N)
  local elapsed=$((now - SCRIPT_START))
  echo "[TIMING] ${label}: ${elapsed}ms ($(date '+%H:%M:%S.%3N'))"
}

# Smart wait with verification - replaces fixed sleeps for safety with speed
# Usage: wait_until <check_command> <max_seconds> <description>
wait_until() {
  local check_cmd="$1"
  local max_wait="$2"
  local desc="${3:-operation}"
  local elapsed=0
  local interval=0.1

  while ! eval "$check_cmd" 2>/dev/null; do
    sleep $interval
    elapsed=$(echo "$elapsed + $interval" | bc)
    if (( $(echo "$elapsed >= $max_wait" | bc -l) )); then
      echo "  Warning: $desc did not complete within ${max_wait}s"
      return 1
    fi
  done
  return 0
}

# Create a fresh loop device for an image file
# After clearing any previous state, we create fresh loop devices.
# Usage: LOOP_DEV=$(create_loop "/path/to/image.img")
create_loop() {
  local img="$1"
  sudo losetup --show -f "$img"
}

# Load configuration
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
log_timing "Script start"
source "$SCRIPT_DIR/config.sh"
log_timing "Config loaded"

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

      echo "❌ ERROR: Cannot switch to present mode - file operation still in progress" >&2
      echo "Please wait for current upload/download/scheduler operation to complete" >&2
      exit 1
    fi

    sleep 1
  done

  echo "✓ File operation completed, proceeding with mode switch"
fi

log_timing "Lock check completed"

echo "Switching to USB gadget presentation mode..."

# Ask Samba to drop any open handles before shutting it down
echo "Closing Samba shares..."
sudo smbcontrol all close-share gadget_part1 2>/dev/null || true
sudo smbcontrol all close-share gadget_part2 2>/dev/null || true

# Stop Samba so nothing can reopen the image while we transition
echo "Stopping Samba services..."
sudo systemctl stop smbd || true
sudo systemctl stop nmbd || true

# Force all buffered data to disk before unmounting
echo "Flushing buffered writes to disk..."
sync
# Brief pause to ensure filesystem metadata is stable (reduced from 1s)
sleep 0.3

# Helper to unmount even if Samba clients are still attached
unmount_with_retry() {
  local target="$1"
  local attempt
  # Check if mounted in host namespace
  if ! sudo nsenter --mount=/proc/1/ns/mnt -- mountpoint -q "$target" 2>/dev/null && ! mountpoint -q "$target" 2>/dev/null; then
    return 0
  fi

  for attempt in 1 2 3; do
    # Unmount in host namespace to ensure it's visible system-wide
    if sudo nsenter --mount=/proc/1/ns/mnt -- umount "$target" 2>/dev/null || sudo umount "$target" 2>/dev/null; then
      echo "  Unmounted $target"
      return 0
    fi
    echo "  $target busy (attempt $attempt). Terminating remaining clients..."
    sudo fuser -km "$target" 2>/dev/null || true
    sleep 1
  done

  echo "  Unable to unmount $target cleanly; forcing lazy unmount..."
  sudo nsenter --mount=/proc/1/ns/mnt -- umount -lf "$target" 2>/dev/null || sudo umount -lf "$target" 2>/dev/null || true
  sleep 1

  # Check again in host namespace
  if sudo nsenter --mount=/proc/1/ns/mnt -- mountpoint -q "$target" 2>/dev/null || mountpoint -q "$target" 2>/dev/null; then
    echo "  Error: $target still mounted after forced unmount." >&2
    return 1
  fi

  echo "  Lazy unmount succeeded for $target"
  return 0
}

# Unmount drives if mounted
log_timing "Starting unmount sequence"
echo "Unmounting drives..."
UNMOUNT_TARGETS=("$MNT_DIR/part1" "$MNT_DIR/part2")
if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
  UNMOUNT_TARGETS+=("$MNT_DIR/part3")
fi
for mp in "${UNMOUNT_TARGETS[@]}"; do
  # Sync each partition before unmounting
  if mountpoint -q "$mp" 2>/dev/null; then
    echo "  Syncing $mp..."
    sudo sync -f "$mp" 2>/dev/null || sync
  fi
  if ! unmount_with_retry "$mp"; then
    echo "  Aborting gadget presentation to avoid corruption." >&2
    exit 1
  fi
done

log_timing "Drives unmounted"

# Also unmount any existing read-only mounts from previous present mode
echo "Unmounting any existing read-only mounts..."
RO_MNT_DIR="/mnt/gadget"
RO_UNMOUNT_TARGETS=("$RO_MNT_DIR/part1-ro" "$RO_MNT_DIR/part2-ro")
if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
  RO_UNMOUNT_TARGETS+=("$RO_MNT_DIR/part3-ro")
fi
for mp in "${RO_UNMOUNT_TARGETS[@]}"; do
  if mountpoint -q "$mp" 2>/dev/null || sudo nsenter --mount=/proc/1/ns/mnt -- mountpoint -q "$mp" 2>/dev/null; then
    echo "  Unmounting $mp..."
    unmount_with_retry "$mp" || true
  fi
done

# One final sync after all unmounts
sync
log_timing "Final sync completed"

# Clean up existing loop devices for our images
# After unmounting, detach any lingering loop devices to avoid accumulation
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
sleep 0.2
log_timing "Loop devices cleaned up"

# ============================================================================
# Boot-time filesystem check and repair (optional, ~1 second total)
# ============================================================================
# These variables will hold loop devices for reuse later in the script
LOOP_CAM=""
LOOP_LIGHTSHOW=""
LOOP_MUSIC=""

if [ "${BOOT_FSCK_ENABLED:-false}" = "true" ]; then
  echo "Running boot-time filesystem check and repair..."

  # Create loop devices for fsck (will be reused for local mounts too)
  LOOP_CAM=$(create_loop "$IMG_CAM")
  LOOP_LIGHTSHOW=$(create_loop "$IMG_LIGHTSHOW")
  if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
    LOOP_MUSIC=$(create_loop "$IMG_MUSIC")
  fi

  # Detect filesystem types
  FS_TYPE_CAM=$(sudo blkid -o value -s TYPE "$LOOP_CAM" 2>/dev/null || echo "exfat")
  FS_TYPE_LIGHTSHOW=$(sudo blkid -o value -s TYPE "$LOOP_LIGHTSHOW" 2>/dev/null || echo "vfat")
  if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
    FS_TYPE_MUSIC=$(sudo blkid -o value -s TYPE "$LOOP_MUSIC" 2>/dev/null || echo "vfat")
  fi

  # Run fsck on TeslaCam (part1)
  echo "  Checking TeslaCam ($FS_TYPE_CAM)..."
  if [ "$FS_TYPE_CAM" = "exfat" ]; then
    if sudo fsck.exfat -p "$LOOP_CAM" 2>&1; then
      echo "    ✓ TeslaCam: clean"
    else
      echo "    ⚠ TeslaCam: repaired or has issues"
    fi
  else
    if sudo fsck.vfat -p "$LOOP_CAM" 2>&1; then
      echo "    ✓ TeslaCam: clean"
    else
      echo "    ⚠ TeslaCam: repaired or has issues"
    fi
  fi

  # Run fsck on LightShow (part2)
  echo "  Checking LightShow ($FS_TYPE_LIGHTSHOW)..."
  if [ "$FS_TYPE_LIGHTSHOW" = "exfat" ]; then
    if sudo fsck.exfat -p "$LOOP_LIGHTSHOW" 2>&1; then
      echo "    ✓ LightShow: clean"
    else
      echo "    ⚠ LightShow: repaired or has issues"
    fi
  else
    if sudo fsck.vfat -p "$LOOP_LIGHTSHOW" 2>&1; then
      echo "    ✓ LightShow: clean"
    else
      echo "    ⚠ LightShow: repaired or has issues"
    fi
  fi

  if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
    echo "  Checking Music ($FS_TYPE_MUSIC)..."
    if [ "$FS_TYPE_MUSIC" = "exfat" ]; then
      if sudo fsck.exfat -p "$LOOP_MUSIC" 2>&1; then
        echo "    ✓ Music: clean"
      else
        echo "    ⚠ Music: repaired or has issues"
      fi
    else
      if sudo fsck.vfat -p "$LOOP_MUSIC" 2>&1; then
        echo "    ✓ Music: clean"
      else
        echo "    ⚠ Music: repaired or has issues"
      fi
    fi
  fi

  # Note: Loop devices (LOOP_CAM, LOOP_LIGHTSHOW, LOOP_MUSIC) preserved for later reuse in local mounts
  echo "  Loop devices preserved for local mount reuse"

  log_timing "Boot fsck completed"
else
  echo "Boot-time fsck disabled (set disk_images.boot_fsck_enabled: true to enable)"
fi

# Remove mount directories to avoid accidental access when unmounted
echo "Removing mount directories..."
REMOVE_TARGETS=("$MNT_DIR/part1" "$MNT_DIR/part2")
if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
  REMOVE_TARGETS+=("$MNT_DIR/part3")
fi
for mp in "${REMOVE_TARGETS[@]}"; do
  # Check if mounted in host namespace
  if sudo nsenter --mount=/proc/1/ns/mnt -- mountpoint -q "$mp" 2>/dev/null || mountpoint -q "$mp" 2>/dev/null; then
    echo "  Skipping removal of $mp (still mounted)" >&2
    continue
  fi
  if [ -d "$mp" ]; then
    sudo rm -rf "$mp" || true
  fi
done

# Flush any pending writes to the image files
echo "Flushing pending filesystem buffers..."
sync

# Note: We don't need to detach existing loop devices for the gadget to work.
# The USB gadget uses the image files directly, not through loop devices.
# Loop devices are only needed for local mounting. If they exist from a previous
# session, that's fine - the gadget can still access the files.

# Stop conflicting rpi-usb-gadget service if running (Pi OS Bookworm+ default)
# This service claims the UDC for a USB Ethernet gadget, blocking our mass-storage gadget
for svc in rpi-usb-gadget.service usb-gadget.service; do
  if systemctl is-active --quiet "$svc" 2>/dev/null; then
    echo "Stopping conflicting $svc..."
    sudo systemctl stop "$svc" 2>/dev/null || true
    sleep 0.3
  fi
done

# Remove legacy gadget module if present
if lsmod | grep -q '^g_mass_storage'; then
  echo "Removing existing USB gadget module..."
  sudo rmmod g_mass_storage || true
  sleep 1
fi

# Remove existing gadget configuration if present
CONFIGFS_GADGET="/sys/kernel/config/usb_gadget/teslausb"
if [ -d "$CONFIGFS_GADGET" ]; then
  echo "Removing existing gadget configuration..."

  # Unbind UDC first
  if [ -f "$CONFIGFS_GADGET/UDC" ]; then
    echo "" | sudo tee "$CONFIGFS_GADGET/UDC" > /dev/null 2>&1 || true
    # Brief settle time (reduced from 1s - unbind is synchronous)
    sleep 0.3
  fi

  # Clear LUN backing files to release kernel file references
  for lun in "$CONFIGFS_GADGET"/functions/mass_storage.usb0/lun.*; do
    if [ -f "$lun/file" ]; then
      echo "" | sudo tee "$lun/file" > /dev/null 2>&1 || true
    fi
  done
  sleep 0.1

  # Remove function links
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
fi

log_timing "Gadget removed/cleared"

# Mount configfs if not already mounted
if ! mountpoint -q /sys/kernel/config 2>/dev/null; then
  sudo mount -t configfs none /sys/kernel/config || true
fi

# Present dual-LUN gadget using configfs
echo "Presenting USB gadget with dual LUNs (TeslaCam RW + Lightshow RO)..."

# Create gadget directory
sudo mkdir -p "$CONFIGFS_GADGET"
cd "$CONFIGFS_GADGET"

# Device descriptors (Tesla-compatible)
echo 0x1d6b | sudo tee idVendor > /dev/null  # Linux Foundation
echo 0x0104 | sudo tee idProduct > /dev/null # Multifunction Composite Gadget
echo 0x0100 | sudo tee bcdDevice > /dev/null # Device version 1.0
echo 0x0200 | sudo tee bcdUSB > /dev/null    # USB 2.0

# String descriptors
sudo mkdir -p strings/0x409
echo "$(cat /proc/sys/kernel/random/uuid | cut -c1-15)" | sudo tee strings/0x409/serialnumber > /dev/null
echo "TeslaUSB" | sudo tee strings/0x409/manufacturer > /dev/null
echo "Tesla Storage" | sudo tee strings/0x409/product > /dev/null

# Create configuration
sudo mkdir -p configs/c.1
sudo mkdir -p configs/c.1/strings/0x409
echo "TeslaCam + Lightshow" | sudo tee configs/c.1/strings/0x409/configuration > /dev/null
echo 500 | sudo tee configs/c.1/MaxPower > /dev/null  # 500mA

# Create mass storage function
sudo mkdir -p functions/mass_storage.usb0

# Configure LUN 0: TeslaCam (READ-WRITE)
echo 1 | sudo tee functions/mass_storage.usb0/stall > /dev/null
echo 1 | sudo tee functions/mass_storage.usb0/lun.0/removable > /dev/null
echo 0 | sudo tee functions/mass_storage.usb0/lun.0/ro > /dev/null  # Read-write for Tesla to record
echo 0 | sudo tee functions/mass_storage.usb0/lun.0/cdrom > /dev/null
echo "$IMG_CAM" | sudo tee functions/mass_storage.usb0/lun.0/file > /dev/null

# Configure LUN 1: Lightshow (READ-ONLY)
# Create LUN 1 directory explicitly
sudo mkdir -p functions/mass_storage.usb0/lun.1
echo 1 | sudo tee functions/mass_storage.usb0/lun.1/removable > /dev/null
echo 1 | sudo tee functions/mass_storage.usb0/lun.1/ro > /dev/null  # Read-only for performance!
echo 0 | sudo tee functions/mass_storage.usb0/lun.1/cdrom > /dev/null
echo "$IMG_LIGHTSHOW" | sudo tee functions/mass_storage.usb0/lun.1/file > /dev/null

# Configure LUN 2: Music (READ-ONLY to Tesla)
if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
  if [ ! -f "$IMG_MUSIC" ]; then
    echo "WARNING: Music image not found at $IMG_MUSIC — skipping LUN 2" >&2
    MUSIC_ENABLED_BOOL=0
  else
    sudo mkdir -p functions/mass_storage.usb0/lun.2
    echo 1 | sudo tee functions/mass_storage.usb0/lun.2/removable > /dev/null
    echo 1 | sudo tee functions/mass_storage.usb0/lun.2/ro > /dev/null
    echo 0 | sudo tee functions/mass_storage.usb0/lun.2/cdrom > /dev/null
    echo "$IMG_MUSIC" | sudo tee functions/mass_storage.usb0/lun.2/file > /dev/null
  fi
fi

# Link function to configuration
sudo ln -s functions/mass_storage.usb0 configs/c.1/

# Find and enable UDC
UDC_DEVICE=$(ls /sys/class/udc | head -n1)
if [ -z "$UDC_DEVICE" ]; then
  echo "Error: No UDC device found. Is dwc2 module loaded?" >&2
  exit 1
fi

echo "Binding to UDC: $UDC_DEVICE"
echo "$UDC_DEVICE" | sudo tee UDC > /dev/null

echo "Updating mode state..."
echo "present" > "$STATE_FILE"
chown "$TARGET_USER:$TARGET_USER" "$STATE_FILE" 2>/dev/null || true

# Mount partitions locally in read-only mode for browsing
# NOTE: These mounts allow you to browse/read files while the gadget is presented.
# This is generally safe for read-only access, but be aware:
# - If the host (Tesla) is actively writing to TeslaCam, you may see stale cached data
# - Best used when Tesla is not actively recording (e.g., after driving)
echo "Mounting partitions locally in read-only mode..."
RO_MNT_DIR="/mnt/gadget"
if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
  sudo mkdir -p "$RO_MNT_DIR/part1-ro" "$RO_MNT_DIR/part2-ro" "$RO_MNT_DIR/part3-ro"
else
  sudo mkdir -p "$RO_MNT_DIR/part1-ro" "$RO_MNT_DIR/part2-ro"
fi

# Get user IDs for mounting
UID_VAL=$(id -u "$TARGET_USER")
GID_VAL=$(id -g "$TARGET_USER")

# Mount TeslaCam image (part1) - reuse fsck loop device if available, otherwise create
if [ -z "$LOOP_CAM" ] || [ ! -e "$LOOP_CAM" ]; then
  LOOP_CAM=$(create_loop "$IMG_CAM")
fi

if [ -n "$LOOP_CAM" ] && [ -e "$LOOP_CAM" ]; then
  # Detect filesystem type
  FS_TYPE=$(sudo blkid -o value -s TYPE "$LOOP_CAM" 2>/dev/null || echo "vfat")

  echo "  Mounting ${LOOP_CAM} (TeslaCam) at $RO_MNT_DIR/part1-ro (read-only)..."

  if [ "$FS_TYPE" = "vfat" ]; then
    sudo nsenter --mount=/proc/1/ns/mnt mount -t vfat -o ro,uid=$UID_VAL,gid=$GID_VAL,umask=022 "$LOOP_CAM" "$RO_MNT_DIR/part1-ro"
  elif [ "$FS_TYPE" = "exfat" ]; then
    sudo nsenter --mount=/proc/1/ns/mnt mount -t exfat -o ro,uid=$UID_VAL,gid=$GID_VAL,umask=022 "$LOOP_CAM" "$RO_MNT_DIR/part1-ro"
  else
    sudo nsenter --mount=/proc/1/ns/mnt mount -o ro "$LOOP_CAM" "$RO_MNT_DIR/part1-ro"
  fi

  echo "  Mounted successfully at $RO_MNT_DIR/part1-ro"
else
  echo "  Warning: Unable to attach loop device for TeslaCam read-only mounting"
fi

# Mount Lightshow image (part2) - reuse fsck loop device if available, otherwise create
if [ -z "$LOOP_LIGHTSHOW" ] || [ ! -e "$LOOP_LIGHTSHOW" ]; then
  LOOP_LIGHTSHOW=$(create_loop "$IMG_LIGHTSHOW")
fi

if [ -n "$LOOP_LIGHTSHOW" ] && [ -e "$LOOP_LIGHTSHOW" ]; then
  # Detect filesystem type
  FS_TYPE=$(sudo blkid -o value -s TYPE "$LOOP_LIGHTSHOW" 2>/dev/null || echo "vfat")

  echo "  Mounting ${LOOP_LIGHTSHOW} (Lightshow) at $RO_MNT_DIR/part2-ro (read-only)..."

  if [ "$FS_TYPE" = "vfat" ]; then
    sudo nsenter --mount=/proc/1/ns/mnt mount -t vfat -o ro,uid=$UID_VAL,gid=$GID_VAL,umask=022 "$LOOP_LIGHTSHOW" "$RO_MNT_DIR/part2-ro"
  elif [ "$FS_TYPE" = "exfat" ]; then
    sudo nsenter --mount=/proc/1/ns/mnt mount -t exfat -o ro,uid=$UID_VAL,gid=$GID_VAL,umask=022 "$LOOP_LIGHTSHOW" "$RO_MNT_DIR/part2-ro"
  else
    sudo nsenter --mount=/proc/1/ns/mnt mount -o ro "$LOOP_LIGHTSHOW" "$RO_MNT_DIR/part2-ro"
  fi

  echo "  Mounted successfully at $RO_MNT_DIR/part2-ro"
else
  echo "  Warning: Unable to attach loop device for Lightshow read-only mounting"
fi

# Mount Music image (part3) when enabled - reuse fsck loop device if available, otherwise create
if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
  if [ -z "$LOOP_MUSIC" ] || [ ! -e "$LOOP_MUSIC" ]; then
    LOOP_MUSIC=$(create_loop "$IMG_MUSIC")
  fi

  if [ -n "$LOOP_MUSIC" ] && [ -e "$LOOP_MUSIC" ]; then
    FS_TYPE=$(sudo blkid -o value -s TYPE "$LOOP_MUSIC" 2>/dev/null || echo "vfat")

    echo "  Mounting ${LOOP_MUSIC} (Music) at $RO_MNT_DIR/part3-ro (read-only)..."

    if [ "$FS_TYPE" = "vfat" ]; then
      sudo nsenter --mount=/proc/1/ns/mnt mount -t vfat -o ro,uid=$UID_VAL,gid=$GID_VAL,umask=022 "$LOOP_MUSIC" "$RO_MNT_DIR/part3-ro"
    elif [ "$FS_TYPE" = "exfat" ]; then
      sudo nsenter --mount=/proc/1/ns/mnt mount -t exfat -o ro,uid=$UID_VAL,gid=$GID_VAL,umask=022 "$LOOP_MUSIC" "$RO_MNT_DIR/part3-ro"
    else
      sudo nsenter --mount=/proc/1/ns/mnt mount -o ro "$LOOP_MUSIC" "$RO_MNT_DIR/part3-ro"
    fi

    echo "  Mounted successfully at $RO_MNT_DIR/part3-ro"
  else
    echo "  Warning: Unable to attach loop device for Music read-only mounting"
  fi
fi
log_timing "USB gadget fully configured and mounted"

echo "USB gadget presented successfully!"
if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
  echo "The Pi should now appear as THREE USB storage devices when connected:"
else
  echo "The Pi should now appear as TWO USB storage devices when connected:"
fi
echo "  - LUN 0: TeslaCam (Read-Write) - Tesla can record dashcam footage"
echo "  - LUN 1: Lightshow (Read-Only) - Optimized read performance for Tesla"
if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
  echo "  - LUN 2: Music (Read-Only) - Media files for Tesla audio"
fi
if [ $MUSIC_ENABLED_BOOL -eq 1 ]; then
  echo "Read-only mounts available at: $RO_MNT_DIR/part1-ro, $RO_MNT_DIR/part2-ro, $RO_MNT_DIR/part3-ro"
else
  echo "Read-only mounts available at: $RO_MNT_DIR/part1-ro and $RO_MNT_DIR/part2-ro"
fi

log_timing "Script completed successfully"
echo "[PERFORMANCE] Total execution time: $(($(date +%s%3N) - SCRIPT_START))ms"
