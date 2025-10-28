#!/bin/bash
set -euo pipefail

# present_usb.sh - Switch to USB gadget presentation mode
# This script stops Samba, unmounts partitions, and presents the USB gadget

# Configuration (will be updated by setup-usb.sh)
IMG="__GADGET_DIR__/__IMG_NAME__"
MNT_DIR="__MNT_DIR__"
TARGET_USER="__TARGET_USER__"
STATE_FILE="__GADGET_DIR__/state.txt"

echo "Switching to USB gadget presentation mode..."

# Stop Samba
echo "Stopping Samba services..."
sudo systemctl stop smbd || true
sudo systemctl stop nmbd || true

# Unmount partitions if mounted
echo "Unmounting partitions..."
for mp in "$MNT_DIR/part1" "$MNT_DIR/part2"; do
  if mountpoint -q "$mp" 2>/dev/null; then
    echo "  Unmounting $mp"
    if ! sudo umount "$mp"; then
      echo "  Warning: failed to unmount $mp" >&2
      echo "  Attempting lazy unmount of $mp"
      sudo umount -l "$mp" 2>/dev/null || true
    fi
  fi
done

# Remove mount directories to avoid accidental access when unmounted
echo "Removing mount directories..."
for mp in "$MNT_DIR/part1" "$MNT_DIR/part2"; do
  if mountpoint -q "$mp" 2>/dev/null; then
    echo "  Skipping removal of $mp (still mounted)" >&2
    continue
  fi
  if [ -d "$mp" ]; then
    sudo rm -rf "$mp" || true
  fi
done

# Flush any pending writes to the image before detaching loops
echo "Flushing pending filesystem buffers..."
sync

# Detach loop devices for the image
echo "Detaching loop devices..."
for loop in $(losetup -j "$IMG" 2>/dev/null | cut -d: -f1); do
  if [ -n "$loop" ]; then
    echo "  Detaching $loop"
    sudo losetup -d "$loop" || true
  fi
done

# Remove gadget module if present
if lsmod | grep -q '^g_mass_storage'; then
  echo "Removing existing USB gadget module..."
  sudo rmmod g_mass_storage || true
  sleep 1
fi

# Present gadget
echo "Presenting USB gadget..."
sudo modprobe g_mass_storage file="$IMG" stall=0 removable=1 ro=0

echo "Updating mode state..."
echo "present" > "$STATE_FILE"
chown "$TARGET_USER:$TARGET_USER" "$STATE_FILE" 2>/dev/null || true

echo "USB gadget presented successfully!"
echo "The Pi should now appear as a USB storage device when connected."