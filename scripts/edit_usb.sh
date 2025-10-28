#!/bin/bash
set -euo pipefail

# edit_usb.sh - Switch to edit mode with local mounts and Samba
# This script removes the USB gadget, mounts partitions locally, and starts Samba

# Configuration (will be updated by setup-usb.sh)
IMG="__GADGET_DIR__/__IMG_NAME__"
MNT_DIR="__MNT_DIR__"
TARGET_USER="__TARGET_USER__"
STATE_FILE="__GADGET_DIR__/state.txt"

echo "Switching to edit mode (local mount + Samba)..."

# Get user IDs for mounting
UID_VAL=$(id -u "$TARGET_USER")
GID_VAL=$(id -g "$TARGET_USER")

# Remove gadget if active (with force to prevent hanging)
if lsmod | grep -q '^g_mass_storage'; then
  echo "Removing USB gadget module..."
  # Sync all pending writes first
  sync
  sleep 1
  
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

# Remove gadget if active
if lsmod | grep -q '^g_mass_storage'; then
  echo "Removing USB gadget module..."
  # Ensure no active I/O before removing module
  sync
  sleep 2
  sudo rmmod g_mass_storage || true
  sleep 1
fi

# Prepare mount points
echo "Preparing mount points..."
sudo mkdir -p "$MNT_DIR/part1" "$MNT_DIR/part2"
sudo chown "$TARGET_USER:$TARGET_USER" "$MNT_DIR/part1" "$MNT_DIR/part2"

# Clean up any stale loop devices tied to the image before assigning a new one
for existing in $(losetup -j "$IMG" 2>/dev/null | cut -d: -f1); do
  if [ -n "$existing" ]; then
    echo "Detaching stale loop device: $existing"
    sudo losetup -d "$existing" 2>/dev/null || true
  fi
done

# Setup loop device
echo "Setting up loop device..."
LOOP=$(sudo losetup --show -fP "$IMG")
echo "Using loop device: $LOOP"
sleep 0.5

cleanup_loop_on_failure() {
  if [ -n "${LOOP:-}" ]; then
    if ! mountpoint -q "$MNT_DIR/part1" 2>/dev/null && ! mountpoint -q "$MNT_DIR/part2" 2>/dev/null; then
      sudo losetup -d "$LOOP" 2>/dev/null || true
    fi
  fi
}
trap cleanup_loop_on_failure EXIT

# Ensure the partition device nodes exist before proceeding
for p in 1 2; do
  if [ ! -e "${LOOP}p${p}" ]; then
    echo "  Warning: ${LOOP}p${p} missing; waiting for partition nodes..."
    for wait in 1 2 3 4 5; do
      sleep 0.5
      if [ -e "${LOOP}p${p}" ]; then
        echo "  ${LOOP}p${p} detected after ${wait}/5 checks"
        break
      fi
      if [ $wait -eq 5 ]; then
        echo "Error: partition node ${LOOP}p${p} did not appear" >&2
        exit 1
      fi
    done
  fi
done

# Run filesystem checks before mounting to auto-repair FAT issues
echo "Running filesystem checks..."
for PART_NUM in 1 2; do
  LOOP_PART="${LOOP}p${PART_NUM}"
  LOG_FILE="/tmp/fsck_gadget_part${PART_NUM}.log"
  echo "  Checking ${LOOP_PART}..."
  set +e
  sudo fsck.vfat -a "$LOOP_PART" >"$LOG_FILE" 2>&1
  FSCK_STATUS=$?
  set -e

  if [ $FSCK_STATUS -ge 4 ]; then
    echo "  Critical filesystem errors detected on ${LOOP_PART}. See $LOG_FILE" >&2
    sudo losetup -d "$LOOP" 2>/dev/null || true
    exit 1
  fi

  if [ $FSCK_STATUS -eq 0 ]; then
    rm -f "$LOG_FILE"
  else
    echo "  Filesystem repairs applied on ${LOOP_PART}. Details saved to $LOG_FILE"
  fi
done

# Mount partitions
echo "Mounting partitions..."
for PART_NUM in 1 2; do
  LOOP_PART="${LOOP}p${PART_NUM}"
  MP="$MNT_DIR/part${PART_NUM}"
  
  # Unmount if already mounted
  if mountpoint -q "$MP" 2>/dev/null; then
    echo "  Unmounting existing mount at $MP"
    if ! safe_unmount_dir "$MP"; then
      echo "Error: could not clear existing mount at $MP" >&2
      exit 1
    fi
  fi
  
  echo "  Mounting $LOOP_PART to $MP"
  sudo mount -o uid=$UID_VAL,gid=$GID_VAL,umask=002,flush "$LOOP_PART" "$MP"
done

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