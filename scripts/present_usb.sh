#!/bin/bash
set -euo pipefail

#!/bin/bash
set -euo pipefail

# present_usb.sh - Present USB gadget with dual-LUN configuration
# This script unmounts local mounts, presents the USB gadget with optimized read-only settings on LUN 1

# Configuration (will be updated by setup_usb.sh)
IMG_CAM="__GADGET_DIR__/__IMG_CAM_NAME__"
IMG_LIGHTSHOW="__GADGET_DIR__/__IMG_LIGHTSHOW_NAME__"
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

# Stop thumbnail generator to prevent file access during mode switch
echo "Stopping thumbnail generator..."
sudo systemctl stop thumbnail_generator.service 2>/dev/null || true

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
sleep 1

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

# Unmount partitions if mounted
echo "Unmounting partitions..."
for mp in "$MNT_DIR/part1" "$MNT_DIR/part2"; do
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

# Also unmount any existing read-only mounts from previous present mode
echo "Unmounting any existing read-only mounts..."
RO_MNT_DIR="/mnt/gadget"
for mp in "$RO_MNT_DIR/part1-ro" "$RO_MNT_DIR/part2-ro"; do
  if mountpoint -q "$mp" 2>/dev/null || sudo nsenter --mount=/proc/1/ns/mnt -- mountpoint -q "$mp" 2>/dev/null; then
    echo "  Unmounting $mp..."
    unmount_with_retry "$mp" || true
  fi
done

# One final sync after all unmounts
sync

# Run filesystem checks to ensure clean FAT volumes before presenting to the host
echo "Running filesystem checks..."

# Check TeslaCam image (part1)
if [ -f "$IMG_CAM" ]; then
  LOOP_DEV=$(losetup -j "$IMG_CAM" 2>/dev/null | head -n1 | cut -d: -f1)
  if [ -z "$LOOP_DEV" ]; then
    LOOP_DEV=$(sudo losetup --show -f "$IMG_CAM")
    EPHEMERAL_LOOP=1
    LOOP_DEV_FSCK="$LOOP_DEV"
  else
    LOOP_DEV_FSCK="$LOOP_DEV"
  fi

  if [ -n "$LOOP_DEV" ] && [ -e "$LOOP_DEV" ]; then
    LOG_FILE="/tmp/fsck_gadget_part1.log"
    
    # Detect filesystem type
    FS_TYPE=$(sudo blkid -o value -s TYPE "$LOOP_DEV" 2>/dev/null || echo "unknown")
    
    # Skip fsck for large exFAT partitions to avoid OOM issues
    if [ "$FS_TYPE" = "exfat" ]; then
      echo "  Skipping fsck for ${LOOP_DEV} (exFAT - would cause OOM on large partitions)"
    elif [ "$FS_TYPE" = "vfat" ]; then
      echo "  Checking ${LOOP_DEV} (TeslaCam)..."
      echo "    Filesystem type: $FS_TYPE"
      
      set +e
      sudo fsck.vfat -a "$LOOP_DEV" >"$LOG_FILE" 2>&1
      FSCK_STATUS=$?
      set -e

      if [ $FSCK_STATUS -ge 4 ]; then
        echo "  Critical filesystem errors detected on ${LOOP_DEV}. See $LOG_FILE" >&2
        exit 1
      fi

      if [ $FSCK_STATUS -eq 0 ]; then
        rm -f "$LOG_FILE"
      else
        echo "  Filesystem repairs applied on ${LOOP_DEV}. Details saved to $LOG_FILE"
      fi
    else
      echo "    Warning: Unknown filesystem type '$FS_TYPE' for TeslaCam, skipping fsck"
    fi
    
    # Detach ephemeral loop if we created it
    if [ "${EPHEMERAL_LOOP:-0}" -eq 1 ]; then
      sudo losetup -d "$LOOP_DEV" 2>/dev/null || true
      EPHEMERAL_LOOP=0
    fi
  fi
else
  echo "  Warning: TeslaCam image not found at $IMG_CAM" >&2
fi

# Check Lightshow image (part2)
if [ -f "$IMG_LIGHTSHOW" ]; then
  LOOP_DEV=$(losetup -j "$IMG_LIGHTSHOW" 2>/dev/null | head -n1 | cut -d: -f1)
  if [ -z "$LOOP_DEV" ]; then
    LOOP_DEV=$(sudo losetup --show -f "$IMG_LIGHTSHOW")
    EPHEMERAL_LOOP=1
    LOOP_DEV_FSCK="$LOOP_DEV"
  else
    LOOP_DEV_FSCK="$LOOP_DEV"
  fi

  if [ -n "$LOOP_DEV" ] && [ -e "$LOOP_DEV" ]; then
    LOG_FILE="/tmp/fsck_gadget_part2.log"
    
    # Detect filesystem type
    FS_TYPE=$(sudo blkid -o value -s TYPE "$LOOP_DEV" 2>/dev/null || echo "unknown")
    
    if [ "$FS_TYPE" = "exfat" ]; then
      echo "  Skipping fsck for ${LOOP_DEV} (exFAT - would cause OOM on large partitions)"
    elif [ "$FS_TYPE" = "vfat" ]; then
      echo "  Checking ${LOOP_DEV} (Lightshow)..."
      echo "    Filesystem type: $FS_TYPE"
      
      set +e
      sudo fsck.vfat -a "$LOOP_DEV" >"$LOG_FILE" 2>&1
      FSCK_STATUS=$?
      set -e

      if [ $FSCK_STATUS -ge 4 ]; then
        echo "  Critical filesystem errors detected on ${LOOP_DEV}. See $LOG_FILE" >&2
        exit 1
      fi

      if [ $FSCK_STATUS -eq 0 ]; then
        rm -f "$LOG_FILE"
      else
        echo "  Filesystem repairs applied on ${LOOP_DEV}. Details saved to $LOG_FILE"
      fi
    else
      echo "    Warning: Unknown filesystem type '$FS_TYPE' for Lightshow, skipping fsck"
    fi
    
    # Detach ephemeral loop if we created it
    if [ "${EPHEMERAL_LOOP:-0}" -eq 1 ]; then
      sudo losetup -d "$LOOP_DEV" 2>/dev/null || true
      EPHEMERAL_LOOP=0
    fi
  fi
else
  echo "  Warning: Lightshow image not found at $IMG_LIGHTSHOW" >&2
fi

# Remove mount directories to avoid accidental access when unmounted
echo "Removing mount directories..."
for mp in "$MNT_DIR/part1" "$MNT_DIR/part2"; do
  # Check if mounted in host namespace
  if sudo nsenter --mount=/proc/1/ns/mnt -- mountpoint -q "$mp" 2>/dev/null || mountpoint -q "$mp" 2>/dev/null; then
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

# Detach loop devices for the images so the gadget gets an exclusive handle
echo "Detaching loop devices..."
for loop in $(losetup -j "$IMG_CAM" 2>/dev/null | cut -d: -f1); do
  if [ -n "$loop" ]; then
    echo "  Detaching $loop"
    sudo losetup -d "$loop" || true
  fi
done
for loop in $(losetup -j "$IMG_LIGHTSHOW" 2>/dev/null | cut -d: -f1); do
  if [ -n "$loop" ]; then
    echo "  Detaching $loop"
    sudo losetup -d "$loop" || true
  fi
done
EPHEMERAL_LOOP=0

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
    sleep 1
  fi
  
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
sudo mkdir -p "$RO_MNT_DIR/part1-ro" "$RO_MNT_DIR/part2-ro"

# Get user IDs for mounting
UID_VAL=$(id -u "$TARGET_USER")
GID_VAL=$(id -g "$TARGET_USER")

# Mount TeslaCam image (part1) - separate loop device for local read-only access
LOOP_CAM=$(losetup -j "$IMG_CAM" 2>/dev/null | head -n1 | cut -d: -f1)
if [ -z "$LOOP_CAM" ]; then
  LOOP_CAM=$(sudo losetup --show -f "$IMG_CAM")
fi

if [ -n "$LOOP_CAM" ] && [ -e "$LOOP_CAM" ]; then
  # Detect filesystem type
  FS_TYPE=$(sudo blkid -o value -s TYPE "$LOOP_CAM" 2>/dev/null || echo "vfat")
  
  echo "  Mounting ${LOOP_CAM} (TeslaCam) at $RO_MNT_DIR/part1-ro (read-only)..."
  
  if [ "$FS_TYPE" = "vfat" ]; then
    sudo mount -t vfat -o ro,uid=$UID_VAL,gid=$GID_VAL,umask=022 "$LOOP_CAM" "$RO_MNT_DIR/part1-ro"
  elif [ "$FS_TYPE" = "exfat" ]; then
    sudo mount -t exfat -o ro,uid=$UID_VAL,gid=$GID_VAL,umask=022 "$LOOP_CAM" "$RO_MNT_DIR/part1-ro"
  else
    sudo mount -o ro "$LOOP_CAM" "$RO_MNT_DIR/part1-ro"
  fi
  
  echo "  Mounted successfully at $RO_MNT_DIR/part1-ro"
else
  echo "  Warning: Unable to attach loop device for TeslaCam read-only mounting"
fi

# Mount Lightshow image (part2) - separate loop device for local read-only access
LOOP_LIGHTSHOW=$(losetup -j "$IMG_LIGHTSHOW" 2>/dev/null | head -n1 | cut -d: -f1)
if [ -z "$LOOP_LIGHTSHOW" ]; then
  LOOP_LIGHTSHOW=$(sudo losetup --show -f "$IMG_LIGHTSHOW")
fi

if [ -n "$LOOP_LIGHTSHOW" ] && [ -e "$LOOP_LIGHTSHOW" ]; then
  # Detect filesystem type
  FS_TYPE=$(sudo blkid -o value -s TYPE "$LOOP_LIGHTSHOW" 2>/dev/null || echo "vfat")
  
  echo "  Mounting ${LOOP_LIGHTSHOW} (Lightshow) at $RO_MNT_DIR/part2-ro (read-only)..."
  
  if [ "$FS_TYPE" = "vfat" ]; then
    sudo mount -t vfat -o ro,uid=$UID_VAL,gid=$GID_VAL,umask=022 "$LOOP_LIGHTSHOW" "$RO_MNT_DIR/part2-ro"
  elif [ "$FS_TYPE" = "exfat" ]; then
    sudo mount -t exfat -o ro,uid=$UID_VAL,gid=$GID_VAL,umask=022 "$LOOP_LIGHTSHOW" "$RO_MNT_DIR/part2-ro"
  else
    sudo mount -o ro "$LOOP_LIGHTSHOW" "$RO_MNT_DIR/part2-ro"
  fi
  
  echo "  Mounted successfully at $RO_MNT_DIR/part2-ro"
else
  echo "  Warning: Unable to attach loop device for Lightshow read-only mounting"
fi

# Restart thumbnail generator timer now that we're in present mode with read-only mounts
echo "Restarting thumbnail generator timer..."
sudo systemctl start thumbnail_generator.timer 2>/dev/null || true

echo "USB gadget presented successfully!"
echo "The Pi should now appear as TWO USB storage devices when connected:"
echo "  - LUN 0: TeslaCam (Read-Write) - Tesla can record dashcam footage"
echo "  - LUN 1: Lightshow (Read-Only) - Optimized read performance for Tesla"
echo "Read-only mounts available at: $RO_MNT_DIR/part1-ro and $RO_MNT_DIR/part2-ro"