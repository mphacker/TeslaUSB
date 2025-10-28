#!/bin/bash
set -euo pipefail

# present_usb.sh - Switch to USB gadget presentation mode
# This script stops Samba, unmounts partitions, and presents the USB gadget

# Configuration (will be updated by setup-usb.sh)
IMG="__GADGET_DIR__/__IMG_NAME__"
MNT_DIR="__MNT_DIR__"
TARGET_USER="__TARGET_USER__"
STATE_FILE="__GADGET_DIR__/state.txt"

EPHEMERAL_LOOP=0
LOOP_DEV_FSCK=""

cleanup_ephemeral_loop() {
  if [ "${EPHEMERAL_LOOP:-0}" -eq 1 ] && [ -n "$LOOP_DEV_FSCK" ]; then
    sudo losetup -d "$LOOP_DEV_FSCK" 2>/dev/null || true
  fi
}
trap cleanup_ephemeral_loop EXIT

echo "Switching to USB gadget presentation mode..."

# Ask Samba to drop any open handles before shutting it down
echo "Closing Samba shares..."
sudo smbcontrol all close-share gadget_part1 2>/dev/null || true
sudo smbcontrol all close-share gadget_part2 2>/dev/null || true

# Stop Samba so nothing can reopen the image while we transition
echo "Stopping Samba services..."
sudo systemctl stop smbd || true
sudo systemctl stop nmbd || true

# Helper to unmount even if Samba clients are still attached
unmount_with_retry() {
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
    echo "  Error: $target still mounted after forced unmount." >&2
    return 1
  fi

  echo "  Lazy unmount succeeded for $target"
  return 0
}

# Unmount partitions if mounted
echo "Unmounting partitions..."
for mp in "$MNT_DIR/part1" "$MNT_DIR/part2"; do
  if ! unmount_with_retry "$mp"; then
    echo "  Aborting gadget presentation to avoid corruption." >&2
    exit 1
  fi
done

# Run filesystem checks to ensure clean FAT volumes before presenting to the host
echo "Running filesystem checks..."
LOOP_DEV=$(losetup -j "$IMG" 2>/dev/null | head -n1 | cut -d: -f1)
if [ -z "$LOOP_DEV" ]; then
  LOOP_DEV=$(sudo losetup --show -fP "$IMG")
  EPHEMERAL_LOOP=1
  LOOP_DEV_FSCK="$LOOP_DEV"
else
  LOOP_DEV_FSCK="$LOOP_DEV"
fi

if [ -n "$LOOP_DEV" ]; then
  for PART_NUM in 1 2; do
    LOOP_PART="${LOOP_DEV}p${PART_NUM}"
    if [ -e "$LOOP_PART" ]; then
      LOG_FILE="/tmp/fsck_gadget_part${PART_NUM}.log"
      echo "  Checking ${LOOP_PART}..."
      set +e
      sudo fsck.vfat -a "$LOOP_PART" >"$LOG_FILE" 2>&1
      FSCK_STATUS=$?
      set -e

      if [ $FSCK_STATUS -ge 4 ]; then
        echo "  Critical filesystem errors detected on ${LOOP_PART}. See $LOG_FILE" >&2
        exit 1
      fi

      if [ $FSCK_STATUS -eq 0 ]; then
        rm -f "$LOG_FILE"
      else
        echo "  Filesystem repairs applied on ${LOOP_PART}. Details saved to $LOG_FILE"
      fi
    fi
  done
else
  echo "  Warning: unable to attach loop device for filesystem checks." >&2
fi

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

# Detach loop devices for the image so the gadget gets an exclusive handle
echo "Detaching loop devices..."
for loop in $(losetup -j "$IMG" 2>/dev/null | cut -d: -f1); do
  if [ -n "$loop" ]; then
    echo "  Detaching $loop"
    sudo losetup -d "$loop" || true
  fi
done
EPHEMERAL_LOOP=0

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