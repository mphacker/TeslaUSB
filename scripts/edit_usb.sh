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

# Remove gadget if active
if lsmod | grep -q '^g_mass_storage'; then
  echo "Removing USB gadget module..."
  sudo rmmod g_mass_storage || true
  sleep 1
fi

# Prepare mount points
echo "Preparing mount points..."
sudo mkdir -p "$MNT_DIR/part1" "$MNT_DIR/part2"
sudo chown "$TARGET_USER:$TARGET_USER" "$MNT_DIR/part1" "$MNT_DIR/part2"

# Setup loop device
echo "Setting up loop device..."
LOOP=$(sudo losetup --show -fP "$IMG")
echo "Using loop device: $LOOP"
sleep 0.5

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

# Mount partitions
echo "Mounting partitions..."
for PART_NUM in 1 2; do
  LOOP_PART="${LOOP}p${PART_NUM}"
  MP="$MNT_DIR/part${PART_NUM}"
  
  # Unmount if already mounted
  if mountpoint -q "$MP" 2>/dev/null; then
    echo "  Unmounting existing mount at $MP"
    sudo umount "$MP" || true
  fi
  
  echo "  Mounting $LOOP_PART to $MP"
  sudo mount -o uid=$UID_VAL,gid=$GID_VAL,umask=002,flush "$LOOP_PART" "$MP"
done

# Start Samba
echo "Starting Samba services..."
sudo systemctl restart smbd || true

echo "Updating mode state..."
echo "edit" > "$STATE_FILE"
chown "$TARGET_USER:$TARGET_USER" "$STATE_FILE" 2>/dev/null || true

echo "Edit mode activated successfully!"
echo "Partitions are now mounted locally and accessible via Samba shares:"
echo "  - Part 1: $MNT_DIR/part1"
echo "  - Part 2: $MNT_DIR/part2"
echo "  - Samba shares: gadget_part1, gadget_part2"