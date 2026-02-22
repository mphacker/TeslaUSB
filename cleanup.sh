#!/usr/bin/env bash
set -euo pipefail

# ============================================
# Tesla USB Gadget Cleanup Script
# ============================================
# This script safely removes all files and configurations
# created by setup_usb.sh while ensuring proper cleanup
# of system resources and services.

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source the configuration file
if [ -f "$SCRIPT_DIR/scripts/config.sh" ]; then
  source "$SCRIPT_DIR/scripts/config.sh"
else
  echo "Error: Configuration file not found at $SCRIPT_DIR/scripts/config.sh"
  echo "Using default values..."
  # Fallback to defaults if config.sh doesn't exist
  GADGET_DIR="$SCRIPT_DIR"
  IMG_CAM_NAME="usb_cam.img"
  IMG_LIGHTSHOW_NAME="usb_lightshow.img"
  MNT_DIR="/mnt/gadget"
  SMB_CONF="/etc/samba/smb.conf"
  CONFIG_FILE="/boot/firmware/config.txt"
  TARGET_USER="${SUDO_USER:-$(whoami)}"
fi

# Compute image paths
IMG_CAM_PATH="$GADGET_DIR/$IMG_CAM_NAME"
IMG_LIGHTSHOW_PATH="$GADGET_DIR/$IMG_LIGHTSHOW_NAME"

echo "Tesla USB Gadget Cleanup Script"
echo "==============================="
echo "Gadget directory: $GADGET_DIR"
echo "TeslaCam image:   $IMG_CAM_PATH"
echo "Lightshow image:  $IMG_LIGHTSHOW_PATH"

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

  # Stop present_usb.sh script if running
  if [ -f "$GADGET_DIR/scripts/present_usb.sh" ]; then
    echo "  Stopping USB gadget (if active)..."
    "$GADGET_DIR/scripts/present_usb.sh" stop 2>/dev/null || true
  fi

  # Remove USB gadget configfs if exists
  if [ -d /sys/kernel/config/usb_gadget/pi_usb ]; then
    echo "  Removing USB gadget configuration..."
    # Unbind UDC if bound
    if [ -f /sys/kernel/config/usb_gadget/pi_usb/UDC ]; then
      echo "" > /sys/kernel/config/usb_gadget/pi_usb/UDC 2>/dev/null || true
    fi
    # Remove configuration
    rm -rf /sys/kernel/config/usb_gadget/pi_usb 2>/dev/null || true
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

  # Detach loop devices for both images
  echo "  Detaching loop devices..."
  for img in "$IMG_CAM_PATH" "$IMG_LIGHTSHOW_PATH"; do
    if [ -f "$img" ]; then
      for loop in $(losetup -j "$img" 2>/dev/null | cut -d: -f1); do
        if [ -n "$loop" ]; then
          echo "    Detaching $loop"
          losetup -d "$loop" 2>/dev/null || true
        fi
      done
    fi
  done
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

    # Remove Samba user
    echo "  Removing Samba user: $TARGET_USER"
    smbpasswd -x "$TARGET_USER" 2>/dev/null || true

    # Restart Samba to apply changes
    echo "  Restarting Samba services..."
    systemctl restart smbd nmbd 2>/dev/null || systemctl restart smbd 2>/dev/null || true
  fi
}

# Function to cleanup system configuration files
cleanup_system_configs() {
  echo "Cleaning up system configuration files..."

  # Remove NetworkManager uap0 ignore config
  if [ -f "/etc/NetworkManager/conf.d/unmanaged-uap0.conf" ]; then
    echo "  Removing NetworkManager uap0 config..."
    rm -f /etc/NetworkManager/conf.d/unmanaged-uap0.conf
    systemctl reload NetworkManager 2>/dev/null || true
  fi

  # Remove module loading config
  if [ -f "/etc/modules-load.d/dwc2.conf" ]; then
    echo "  Removing module loading config..."
    rm -f /etc/modules-load.d/dwc2.conf
  fi

  # Remove sudoers config
  if [ -f "/etc/sudoers.d/teslausb-gadget" ]; then
    echo "  Removing sudoers config..."
    rm -f /etc/sudoers.d/teslausb-gadget
  fi

  # Remove sysctl config
  if [ -f "/etc/sysctl.d/99-teslausb.conf" ]; then
    echo "  Removing sysctl config..."
    rm -f /etc/sysctl.d/99-teslausb.conf
    sysctl --system >/dev/null 2>&1 || true
  fi

  # Restore watchdog config (only if we modified it)
  if [ -f "/etc/watchdog.conf" ]; then
    if grep -q "TeslaUSB Hardware Watchdog Configuration" /etc/watchdog.conf 2>/dev/null; then
      echo "  Restoring default watchdog config..."
      # Just remove our custom config - watchdog package will use defaults
      rm -f /etc/watchdog.conf
    fi
  fi

  # Restore config.txt (remove dwc2 and watchdog entries from [all] section)
  if [ -f "$CONFIG_FILE" ]; then
    echo "  Restoring $CONFIG_FILE..."
    cp "$CONFIG_FILE" "${CONFIG_FILE}.cleanup_backup.$(date +%s)" 2>/dev/null || true

    # Remove dtoverlay=dwc2 and dtparam=watchdog=on lines
    sed -i '/^dtoverlay=dwc2$/d' "$CONFIG_FILE"
    sed -i '/^dtparam=watchdog=on$/d' "$CONFIG_FILE"

    echo "    Removed dwc2 overlay and watchdog parameter"
  fi
}

# Function to cleanup swap files
cleanup_swap() {
  echo "Cleaning up swap files..."

  # Disable and remove persistent swap file
  if [ -f "/var/swap/fsck.swap" ]; then
    echo "  Removing persistent swap file..."
    swapoff /var/swap/fsck.swap 2>/dev/null || true
    rm -f /var/swap/fsck.swap

    # Remove from /etc/fstab if present
    if grep -q "/var/swap/fsck.swap" /etc/fstab 2>/dev/null; then
      echo "  Removing swap entry from /etc/fstab..."
      sed -i '\|/var/swap/fsck.swap|d' /etc/fstab
    fi
  fi

  # Remove swap directory if empty
  if [ -d "/var/swap" ]; then
    rmdir /var/swap 2>/dev/null || true
  fi
}

# Function to restore desktop services
restore_desktop_services() {
  echo "Restoring desktop services..."

  # Unmask desktop services that were masked
  local desktop_services=("pipewire" "wireplumber" "pipewire-pulse" "colord")
  for service in "${desktop_services[@]}"; do
    if systemctl is-masked "${service}.service" >/dev/null 2>&1; then
      echo "  Unmasking ${service}.service..."
      systemctl unmask "${service}.service" 2>/dev/null || true
    fi
  done

  # Unmask rpi-usb-gadget if we masked it during setup
  for svc in rpi-usb-gadget.service usb-gadget.service; do
    if systemctl is-masked "$svc" >/dev/null 2>&1; then
      echo "  Unmasking $svc (restoring Raspberry Pi OS default)..."
      systemctl unmask "$svc" 2>/dev/null || true
      systemctl enable "$svc" 2>/dev/null || true
    fi
  done

  # Check if we changed default target to multi-user
  if systemctl get-default 2>/dev/null | grep -q "multi-user.target"; then
    echo "  Restoring graphical.target as default..."
    systemctl set-default graphical.target 2>/dev/null || true

    # Re-enable lightdm if it exists
    if systemctl list-unit-files | grep -q "lightdm.service"; then
      echo "  Re-enabling lightdm..."
      systemctl enable lightdm 2>/dev/null || true
    fi
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

# Function to remove gadget directory files
cleanup_gadget_files() {
  echo "Cleaning up gadget directory files..."

  # Remove state file
  if [ -f "$GADGET_DIR/state.txt" ]; then
    echo "  Removing state.txt"
    rm -f "$GADGET_DIR/state.txt"
  fi

  # Remove thumbnail cache directory
  if [ -d "$GADGET_DIR/thumbnails" ]; then
    echo "  Removing thumbnail cache directory"
    rm -rf "$GADGET_DIR/thumbnails"
  fi

  # Remove any backup files or logs
  echo "  Removing backup and temporary files..."
  rm -f "$GADGET_DIR"/*.bak "$GADGET_DIR"/*.tmp "$GADGET_DIR"/*.log 2>/dev/null || true
}

# Function to remove image files (optional)
cleanup_images() {
  local remove_images="$1"

  if [ "$remove_images" = "yes" ]; then
    echo "Removing disk image files..."

    if [ -f "$IMG_CAM_PATH" ]; then
      echo "  Removing TeslaCam image: $IMG_CAM_PATH"
      rm -f "$IMG_CAM_PATH"
    fi

    if [ -f "$IMG_LIGHTSHOW_PATH" ]; then
      echo "  Removing Lightshow image: $IMG_LIGHTSHOW_PATH"
      rm -f "$IMG_LIGHTSHOW_PATH"
    fi
  else
    echo "Preserving disk image files:"
    [ -f "$IMG_CAM_PATH" ] && echo "  - $IMG_CAM_PATH"
    [ -f "$IMG_LIGHTSHOW_PATH" ] && echo "  - $IMG_LIGHTSHOW_PATH"
  fi
}

# Main cleanup sequence
main() {
  local remove_images="$1"

  echo
  echo "Starting cleanup process..."
  echo

  # Ensure we're running as root for system cleanup
  if [ "$EUID" -ne 0 ]; then
    echo "Error: This script must be run as root (use sudo)"
    echo "Usage: sudo $0"
    exit 1
  fi

  # Step 1: Stop and disable all systemd services
  echo "Step 1: Cleaning up systemd services"
  cleanup_service "gadget_web.service" "/etc/systemd/system/gadget_web.service"
  cleanup_service "present_usb_on_boot.service" "/etc/systemd/system/present_usb_on_boot.service"
  cleanup_service "chime_scheduler.service" "/etc/systemd/system/chime_scheduler.service"
  cleanup_service "chime_scheduler.timer" "/etc/systemd/system/chime_scheduler.timer"
  cleanup_service "wifi-powersave-off.service" "/etc/systemd/system/wifi-powersave-off.service"
  cleanup_service "wifi-monitor.service" "/etc/systemd/system/wifi-monitor.service"

  # Stop hostapd and dnsmasq if running
  echo "  Stopping hostapd and dnsmasq..."
  systemctl stop hostapd 2>/dev/null || true
  systemctl stop dnsmasq 2>/dev/null || true

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

  # Step 4: Clean up system configuration files
  echo "Step 4: Cleaning up system configuration files"
  cleanup_system_configs
  echo

  # Step 5: Clean up swap files
  echo "Step 5: Cleaning up swap files"
  cleanup_swap
  echo

  # Step 6: Restore desktop services
  echo "Step 6: Restoring desktop services"
  restore_desktop_services
  echo

  # Step 7: Remove mount directories
  echo "Step 7: Cleaning up mount directories"
  cleanup_mount_dirs
  echo

  # Step 8: Remove gadget files
  echo "Step 8: Cleaning up gadget directory files"
  cleanup_gadget_files
  echo

  # Step 9: Remove image files (optional)
  echo "Step 9: Disk image cleanup"
  cleanup_images "$remove_images"
  echo

  echo "============================================"
  echo "Cleanup completed successfully!"
  echo "============================================"
  echo
  echo "Summary of actions performed:"
  echo "  ✓ Stopped and removed systemd services"
  echo "  ✓ Removed USB gadget configuration"
  echo "  ✓ Cleaned up Samba shares and user"
  echo "  ✓ Removed system configuration files"
  echo "  ✓ Removed persistent swap file"
  echo "  ✓ Restored desktop services (if applicable)"
  echo "  ✓ Removed mount directories"
  echo "  ✓ Removed gadget files"
  if [ "$remove_images" = "yes" ]; then
    echo "  ✓ Removed disk image files"
  else
    echo "  ✓ Preserved disk image files"
  fi
  echo
  echo "To complete the cleanup:"
  echo "  1. Reboot the Pi to restore original boot configuration"
  echo "  2. Optionally remove installed packages:"
  echo "     sudo apt remove --autoremove python3-flask python3-waitress \\"
  echo "          python3-av python3-pil samba samba-common-bin ffmpeg \\"
  echo "          watchdog hostapd dnsmasq"
  echo
  echo "Note: This cleanup script remains in $GADGET_DIR"
  echo "      The scripts/ and templates/ directories also remain"
  echo "      You may delete them manually if no longer needed."
  echo
}

# Confirmation prompt
echo
echo "This will remove all USB gadget configuration and restore your Pi."
echo
echo "The following will be cleaned up:"
echo "  - All systemd services (gadget_web, chime_scheduler, wifi-monitor, etc.)"
echo "  - USB gadget configuration and loop devices"
echo "  - Samba share configuration and user"
echo "  - System configuration files (NetworkManager, modules, sudoers, sysctl)"
echo "  - Persistent swap file"
echo "  - /boot/firmware/config.txt modifications (dwc2, watchdog)"
echo "  - Mount directories ($MNT_DIR)"
echo "  - Generated files in $GADGET_DIR"
echo

# Ask about disk images
REMOVE_IMAGES="no"
if [ -f "$IMG_CAM_PATH" ] || [ -f "$IMG_LIGHTSHOW_PATH" ]; then
  echo "Disk image files found:"
  [ -f "$IMG_CAM_PATH" ] && echo "  - $IMG_CAM_PATH ($(du -h "$IMG_CAM_PATH" 2>/dev/null | cut -f1))"
  [ -f "$IMG_LIGHTSHOW_PATH" ] && echo "  - $IMG_LIGHTSHOW_PATH ($(du -h "$IMG_LIGHTSHOW_PATH" 2>/dev/null | cut -f1))"
  echo
  read -p "Do you want to DELETE these image files? (y/N): " -n 1 -r
  echo
  if [[ $REPLY =~ ^[Yy]$ ]]; then
    REMOVE_IMAGES="yes"
    echo "Image files will be deleted."
  else
    echo "Image files will be preserved."
  fi
  echo
fi

read -p "Are you sure you want to proceed with cleanup? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
  main "$REMOVE_IMAGES"
else
  echo "Cleanup cancelled."
  exit 0
fi
