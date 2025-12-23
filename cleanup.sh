#!/usr/bin/env bash
set -euo pipefail

# ============================================
# Tesla USB Gadget Cleanup Script
# ============================================
# This script safely removes all files and configurations
# created by setup-usb.sh while ensuring proper cleanup
# of system resources and services.

echo "Tesla USB Gadget Cleanup Script"
echo "==============================="

#!/bin/bash

# cleanup.sh - Remove all Tesla USB Gadget files and configuration
#
# This script safely removes all files and system configurations
# created by setup_usb.sh while ensuring proper cleanup
# of system resources like loop devices, mounts, and services.

set -euo pipefail

# Configuration - should match setup_usb.sh
GADGET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMG_NAME="usb_dual.img"
IMG_PATH="$GADGET_DIR/$IMG_NAME"
MNT_DIR="/mnt/gadget"
SMB_CONF="/etc/samba/smb.conf"

echo "Gadget directory: $GADGET_DIR"
echo "Image file: $IMG_PATH"

# Function to safely stop and disable systemd services
cleanup_service() {
  local service_name="$1"
  local service_file="$2"

  echo "Cleaning up service: $service_name"

  # Stop service if running
  if systemctl is-active --quiet "$service_name" 2>/dev/null; then
    echo "  Stopping $service_name..."
    systemctl stop "$service_name" || true
  fi

  # Disable service if enabled
  if systemctl is-enabled --quiet "$service_name" 2>/dev/null; then
    echo "  Disabling $service_name..."
    systemctl disable "$service_name" || true
  fi

  # Remove service file
  if [ -f "$service_file" ]; then
    echo "  Removing service file: $service_file"
    rm -f "$service_file" || true
  fi
}

# Function to cleanup USB gadget and loop devices
cleanup_usb_gadget() {
  echo "Cleaning up USB gadget and loop devices..."

  # Remove USB gadget module if loaded
  if lsmod | grep -q '^g_mass_storage'; then
    echo "  Removing g_mass_storage module..."
    rmmod g_mass_storage 2>/dev/null || true
    sleep 1
  fi

  # Unmount any mounted partitions (edit mode)
  echo "  Unmounting edit mode partitions..."
  for mp in "$MNT_DIR/part1" "$MNT_DIR/part2"; do
    if mountpoint -q "$mp" 2>/dev/null; then
      echo "    Unmounting $mp"
      umount "$mp" || true
    fi
  done

  # Unmount any read-only mounted partitions (present mode)
  echo "  Unmounting read-only partitions..."
  for mp in "$MNT_DIR/part1-ro" "$MNT_DIR/part2-ro"; do
    if mountpoint -q "$mp" 2>/dev/null; then
      echo "    Unmounting $mp"
      umount "$mp" || true
    fi
  done

  # Detach any loop devices associated with the image
  if [ -f "$IMG_PATH" ]; then
    echo "  Detaching loop devices for $IMG_PATH..."
    for loop in $(losetup -j "$IMG_PATH" 2>/dev/null | cut -d: -f1); do
      if [ -n "$loop" ]; then
        echo "    Detaching $loop"
        losetup -d "$loop" 2>/dev/null || true
      fi
    done
  fi
}

# Function to cleanup Samba configuration
cleanup_samba() {
  echo "Cleaning up Samba configuration..."

  if [ -f "$SMB_CONF" ]; then
    # Remove gadget_part1 and gadget_part2 shares from smb.conf
    echo "  Removing gadget shares from $SMB_CONF..."

    # Create backup before modification
    cp "$SMB_CONF" "${SMB_CONF}.cleanup_backup.$(date +%s)" 2>/dev/null || true

    # Remove gadget share sections using awk
    awk '
      BEGIN{skip=0}
      /^\[gadget_part1\]/{skip=1; next}
      /^\[gadget_part2\]/{skip=1; next}
      /^\[.*\]$/ {
        if(skip==1 && $0 !~ /^\[gadget_part1\]/ && $0 !~ /^\[gadget_part2\]/) {
          skip=0
        }
      }
      { if(skip==0) print }
    ' "$SMB_CONF" > "${SMB_CONF}.tmp" && mv "${SMB_CONF}.tmp" "$SMB_CONF" || true

    # Restart Samba to apply changes
    echo "  Restarting Samba services..."
    systemctl restart smbd nmbd 2>/dev/null || systemctl restart smbd 2>/dev/null || true
  fi
}

# Function to remove mount directories
cleanup_mount_dirs() {
  echo "Cleaning up mount directories..."

  for dir in "$MNT_DIR/part1" "$MNT_DIR/part2" "$MNT_DIR/part1-ro" "$MNT_DIR/part2-ro" "$MNT_DIR"; do
    if [ -d "$dir" ]; then
      echo "  Removing directory: $dir"
      rmdir "$dir" 2>/dev/null || true
    fi
  done
}

# Function to remove gadget directory files (except this script)
cleanup_gadget_files() {
  echo "Cleaning up gadget directory files..."

  # List of files created by setup_usb.sh (excluding this script and templates)
  local files_to_remove=(
    "present_usb.sh"
    "edit_usb.sh"
    "web_control.py"
    "state.txt"
  )

  for file in "${files_to_remove[@]}"; do
    local file_path="$GADGET_DIR/$file"
    if [ -f "$file_path" ]; then
      echo "  Removing file: $file"
      rm -f "$file_path" || true
    fi
  done

  # Remove any backup files or logs that might have been created
  echo "  Removing any backup or temporary files..."
  rm -f "$GADGET_DIR"/*.bak "$GADGET_DIR"/*.tmp "$GADGET_DIR"/*.log 2>/dev/null || true

  # Note: We intentionally leave the scripts/ and templates/ directories
  # as they are part of the source repository, not generated files
}

# Main cleanup sequence
main() {
  echo
  echo "Starting cleanup process..."
  echo

  # Ensure we're running as root for system cleanup
  if [ "$EUID" -ne 0 ]; then
    echo "Error: This script must be run as root (use sudo)"
    echo "Usage: sudo $0"
    exit 1
  fi

  # Step 1: Stop and disable systemd services
  echo "Step 1: Cleaning up systemd services"
  cleanup_service "gadget_web.service" "/etc/systemd/system/gadget_web.service"
  cleanup_service "present_usb_on_boot.service" "/etc/systemd/system/present_usb_on_boot.service"

  # Reload systemd after removing service files
  echo "  Reloading systemd daemon..."
  systemctl daemon-reload || true

  echo

  # Step 2: Clean up USB gadget and loop devices
  echo "Step 2: Cleaning up USB gadget and loop devices"
  cleanup_usb_gadget
  echo

  # Step 3: Clean up Samba configuration
  echo "Step 3: Cleaning up Samba configuration"
  cleanup_samba
  echo

  # Step 4: Remove mount directories
  echo "Step 4: Cleaning up mount directories"
  cleanup_mount_dirs
  echo

  # Step 5: Remove gadget files (this should be last, before the image)
  echo "Step 5: Cleaning up gadget directory files"
  cleanup_gadget_files
  echo

  echo "Cleanup completed successfully!"
  echo
  echo "Summary of actions performed:"
  echo "  - Stopped and removed systemd services (gadget_web, present_usb_on_boot)"
  echo "  - Removed USB gadget module and detached loop devices"
  echo "  - Cleaned up Samba share configuration"
  echo "  - Removed mount directories ($MNT_DIR)"
  echo "  - Removed gadget files (scripts, web UI, state file)"
  echo "  - Disk image ($IMG_PATH) preserved for manual review"
  echo
  echo "Note: This cleanup script itself remains in $GADGET_DIR"
  echo "      You may delete it manually if no longer needed."
  echo
  echo "To remove packages that were installed (optional):"
  echo "  sudo apt remove --autoremove python3-flask samba samba-common-bin"
  echo
}

# Confirmation prompt
echo "This will remove all USB gadget configuration and files."
echo "The following will be cleaned up:"
echo "  - Systemd services (gadget_web, present_usb_on_boot)"
echo "  - USB gadget module and loop devices"
echo "  - Samba share configuration"
echo "  - Mount directories ($MNT_DIR)"
echo "  - Generated files in $GADGET_DIR (scripts, web UI, state file)"
echo "  - Disk image will be preserved at $IMG_PATH"
echo
read -p "Are you sure you want to proceed? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
  main
else
  echo "Cleanup cancelled."
  exit 0
fi
