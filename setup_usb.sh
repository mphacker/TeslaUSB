#!/usr/bin/env bash
set -euo pipefail

# ================= Configuration =================
GADGET_DIR_DEFAULT="/home/pi/TeslaUSB"
IMG_CAM_NAME="usb_cam.img"        # TeslaCam partition (read-write)
IMG_LIGHTSHOW_NAME="usb_lightshow.img"  # Lightshow partition (read-only)
PART1_SIZE="427G"  
PART2_SIZE="20G"
LABEL1="TeslaCam"
LABEL2="Lightshow"
MNT_DIR="/mnt/gadget"
CONFIG_FILE="/boot/firmware/config.txt"
WEB_PORT=5000
SAMBA_PASS="tesla"   # <-- Configure the Samba password here
# =================================================

# Determine target user (prefer SUDO_USER)
if [ -n "${SUDO_USER-}" ]; then
  TARGET_USER="$SUDO_USER"
else
  TARGET_USER="pi"
fi

GADGET_DIR="$GADGET_DIR_DEFAULT"
IMG_CAM_PATH="$GADGET_DIR/$IMG_CAM_NAME"
IMG_LIGHTSHOW_PATH="$GADGET_DIR/$IMG_LIGHTSHOW_NAME"

# Validate user exists
if ! id "$TARGET_USER" >/dev/null 2>&1; then
  echo "User $TARGET_USER not found. Create it or run with a different sudo user."
  exit 1
fi
TARGET_UID=$(id -u "$TARGET_USER")
TARGET_GID=$(id -g "$TARGET_USER")
echo "Target user: $TARGET_USER (uid=$TARGET_UID gid=$TARGET_GID)"

# Helper: convert size to MiB
to_mib() {
  local s="$1"
  if [[ "$s" =~ ^([0-9]+)([Mm])$ ]]; then
    echo "${BASH_REMATCH[1]}"
  elif [[ "$s" =~ ^([0-9]+)([Gg])$ ]]; then
    echo $(( ${BASH_REMATCH[1]} * 1024 ))
  else
    echo "Invalid size format: $s (use 2048M or 4G)" >&2
    exit 2
  fi
}
P1_MB=$(to_mib "$PART1_SIZE")
P2_MB=$(to_mib "$PART2_SIZE")

# Note: We no longer need TOTAL_MB since we're creating separate images

# Install prerequisites (only fetch/install if something is missing)
REQUIRED_PACKAGES=(
  parted
  dosfstools
  exfatprogs
  util-linux
  psmisc
  python3-flask
  samba
  samba-common-bin
  ffmpeg
)

MISSING_PACKAGES=()
for pkg in "${REQUIRED_PACKAGES[@]}"; do
  if ! dpkg -s "$pkg" >/dev/null 2>&1; then
    MISSING_PACKAGES+=("$pkg")
  fi
done

if [ ${#MISSING_PACKAGES[@]} -gt 0 ]; then
  echo "Installing missing packages: ${MISSING_PACKAGES[*]}"
  apt-get update
  apt-get install -y "${MISSING_PACKAGES[@]}"
else
  echo "All required packages already installed; skipping apt install."
fi

# Ensure config.txt contains dtoverlay=dwc2 under [all]
# Note: We use dtoverlay=dwc2 WITHOUT dr_mode parameter to allow auto-detection
if [ -f "$CONFIG_FILE" ]; then
  # Check if dtoverlay=dwc2 exists in [all] section (not in platform-specific sections)
  if grep -q '^\[all\]' "$CONFIG_FILE"; then
    # [all] section exists - check if dwc2 is already there
    if ! awk '/^\[all\]/,/^\[/ {if (/^dtoverlay=dwc2$/) exit 0} END {exit 1}' "$CONFIG_FILE"; then
      # Add dtoverlay=dwc2 right after [all] line
      sed -i '/^\[all\]/a dtoverlay=dwc2' "$CONFIG_FILE"
      echo "Added dtoverlay=dwc2 under [all] section in $CONFIG_FILE"
    else
      echo "dtoverlay=dwc2 already present under [all] in $CONFIG_FILE"
    fi
  else
    # No [all] section - append it with dwc2
    printf '\n[all]\ndtoverlay=dwc2\n' | tee -a "$CONFIG_FILE" >/dev/null
    echo "Appended [all] section with dtoverlay=dwc2 to $CONFIG_FILE"
  fi
else
  echo "Warning: $CONFIG_FILE not found. Ensure your Pi uses /boot/firmware/config.txt"
fi

# Configure modules to load at boot via systemd
MODULES_LOAD_CONF="/etc/modules-load.d/dwc2.conf"
if [ ! -f "$MODULES_LOAD_CONF" ]; then
  echo "Configuring modules to load at boot..."
  cat > "$MODULES_LOAD_CONF" <<EOF
# USB gadget modules for Tesla USB storage
dwc2
libcomposite
EOF
  echo "Created $MODULES_LOAD_CONF"
else
  echo "Module loading configuration already exists at $MODULES_LOAD_CONF"
fi

# Create gadget folder
mkdir -p "$GADGET_DIR"
chown "$TARGET_USER:$TARGET_USER" "$GADGET_DIR"

# Cleanup function for loop devices
cleanup_loop_devices() {
  if [ -n "${LOOP_CAM:-}" ]; then
    echo "Cleaning up loop device: $LOOP_CAM"
    losetup -d "$LOOP_CAM" 2>/dev/null || true
    LOOP_CAM=""
  fi
  if [ -n "${LOOP_LIGHTSHOW:-}" ]; then
    echo "Cleaning up loop device: $LOOP_LIGHTSHOW"
    losetup -d "$LOOP_LIGHTSHOW" 2>/dev/null || true
    LOOP_LIGHTSHOW=""
  fi
}

# Create TeslaCam image (if missing)
if [ -f "$IMG_CAM_PATH" ]; then
  echo "TeslaCam image already exists at $IMG_CAM_PATH — skipping creation."
else
  # Set trap to cleanup on exit/error
  trap cleanup_loop_devices EXIT INT TERM
  
  echo "Creating TeslaCam image $IMG_CAM_PATH (${P1_MB}M)..."
  # Create sparse file (thin provisioned) - only allocates space as needed
  truncate -s "${P1_MB}M" "$IMG_CAM_PATH" || {
    echo "Error: Failed to create TeslaCam image file"
    exit 1
  }
  
  LOOP_CAM=$(losetup --find --show "$IMG_CAM_PATH") || {
    echo "Error: Failed to create loop device for TeslaCam"
    exit 1
  }
  
  # Validate loop device was created
  if [ -z "$LOOP_CAM" ] || [ ! -e "$LOOP_CAM" ]; then
    echo "Error: Loop device creation failed or device not accessible"
    exit 1
  fi
  
  echo "Using loop device: $LOOP_CAM"
  
  # Format as single filesystem - use exFAT for large partitions (>32GB), FAT32 for smaller
  echo "Formatting TeslaCam partition (${LABEL1})..."
  if [ "$P1_MB" -gt 32768 ]; then
    echo "  Using exFAT (partition size: ${P1_MB}MB > 32GB)"
    mkfs.exfat -n "$LABEL1" "$LOOP_CAM" || {
      echo "Error: Failed to format TeslaCam partition with exFAT"
      exit 1
    }
  else
    echo "  Using FAT32 (partition size: ${P1_MB}MB <= 32GB)"
    mkfs.vfat -F 32 -n "$LABEL1" "$LOOP_CAM" || {
      echo "Error: Failed to format TeslaCam partition with FAT32"
      exit 1
    }
  fi
  
  # Clean up loop device
  losetup -d "$LOOP_CAM" 2>/dev/null || true
  LOOP_CAM=""
  
  echo "TeslaCam image created and formatted."
fi

# Create Lightshow image (if missing)
if [ -f "$IMG_LIGHTSHOW_PATH" ]; then
  echo "Lightshow image already exists at $IMG_LIGHTSHOW_PATH — skipping creation."
else
  # Set trap to cleanup on exit/error (if not already set)
  trap cleanup_loop_devices EXIT INT TERM
  
  echo "Creating Lightshow image $IMG_LIGHTSHOW_PATH (${P2_MB}M)..."
  truncate -s "${P2_MB}M" "$IMG_LIGHTSHOW_PATH" || {
    echo "Error: Failed to create Lightshow image file"
    exit 1
  }
  
  LOOP_LIGHTSHOW=$(losetup --find --show "$IMG_LIGHTSHOW_PATH") || {
    echo "Error: Failed to create loop device for Lightshow"
    exit 1
  }
  
  if [ -z "$LOOP_LIGHTSHOW" ] || [ ! -e "$LOOP_LIGHTSHOW" ]; then
    echo "Error: Loop device creation failed or device not accessible"
    exit 1
  fi
  
  echo "Using loop device: $LOOP_LIGHTSHOW"
  
  # Format Lightshow partition
  echo "Formatting Lightshow partition (${LABEL2})..."
  if [ "$P2_MB" -gt 32768 ]; then
    echo "  Using exFAT (partition size: ${P2_MB}MB > 32GB)"
    mkfs.exfat -n "$LABEL2" "$LOOP_LIGHTSHOW" || {
      echo "Error: Failed to format Lightshow partition with exFAT"
      exit 1
    }
  else
    echo "  Using FAT32 (partition size: ${P2_MB}MB <= 32GB)"
    mkfs.vfat -F 32 -n "$LABEL2" "$LOOP_LIGHTSHOW" || {
      echo "Error: Failed to format Lightshow partition with FAT32"
      exit 1
    }
  fi
  
  # Clean up loop device
  losetup -d "$LOOP_LIGHTSHOW" 2>/dev/null || true
  LOOP_LIGHTSHOW=""
  
  echo "Lightshow image created and formatted."
fi

# Clean up any remaining loop devices
cleanup_loop_devices
trap - EXIT INT TERM  # Remove trap since we're done with image creation

# Create mount points
mkdir -p "$MNT_DIR/part1" "$MNT_DIR/part2"
chown "$TARGET_USER:$TARGET_USER" "$MNT_DIR/part1" "$MNT_DIR/part2"
chmod 775 "$MNT_DIR/part1" "$MNT_DIR/part2"

# Create thumbnail cache directory in persistent location
THUMBNAIL_CACHE_DIR="$GADGET_DIR/thumbnails"
mkdir -p "$THUMBNAIL_CACHE_DIR"
chown "$TARGET_USER:$TARGET_USER" "$THUMBNAIL_CACHE_DIR"
chmod 775 "$THUMBNAIL_CACHE_DIR"
echo "Thumbnail cache directory at: $THUMBNAIL_CACHE_DIR"

# ===== Configure Samba for authenticated user =====
# Add user to Samba with configured password
(echo "$SAMBA_PASS"; echo "$SAMBA_PASS") | sudo smbpasswd -s -a "$TARGET_USER" || true

# Backup smb.conf
SMB_CONF="/etc/samba/smb.conf"
cp "$SMB_CONF" "${SMB_CONF}.bak.$(date +%s)"

# Remove existing gadget_part1 / gadget_part2 blocks
awk '
  BEGIN{skip=0}
  /^\[gadget_part1\]/{skip=1}
  /^\[gadget_part2\]/{skip=1}
  /^\[.*\]$/ { if(skip==1 && $0 !~ /^\[gadget_part1\]/ && $0 !~ /^\[gadget_part2\]/) { skip=0 } }
  { if(skip==0) print }
' "$SMB_CONF" > "${SMB_CONF}.tmp" || cp "$SMB_CONF" "${SMB_CONF}.tmp"
mv "${SMB_CONF}.tmp" "$SMB_CONF"

# Configure global security settings to prevent guest access issues with Windows
# Remove or update problematic guest-related settings in [global] section
sed -i 's/^[[:space:]]*map to guest.*$/# map to guest = Bad User (disabled for Windows compatibility)/' "$SMB_CONF"
sed -i 's/^[[:space:]]*usershare allow guests.*$/# usershare allow guests = no (disabled for Windows compatibility)/' "$SMB_CONF"

# Ensure proper authentication settings are in [global] section
if ! grep -q "^[[:space:]]*security = user" "$SMB_CONF"; then
  sed -i '/^\[global\]/a \   security = user' "$SMB_CONF"
fi

# Add min protocol to ensure Windows 10/11 compatibility
if ! grep -q "server min protocol" "$SMB_CONF"; then
  sed -i '/^\[global\]/a \   server min protocol = SMB2' "$SMB_CONF"
fi

# Add NTLM authentication for Windows compatibility
if ! grep -q "ntlm auth" "$SMB_CONF"; then
  sed -i '/^\[global\]/a \   ntlm auth = ntlmv2-only' "$SMB_CONF"
fi

# Add client protocol settings
if ! grep -q "client min protocol" "$SMB_CONF"; then
  sed -i '/^\[global\]/a \   client min protocol = SMB2' "$SMB_CONF"
fi
if ! grep -q "client max protocol" "$SMB_CONF"; then
  sed -i '/^\[global\]/a \   client max protocol = SMB3' "$SMB_CONF"
fi

# Add authenticated shares
cat >> "$SMB_CONF" <<EOF

[gadget_part1]
   path = $MNT_DIR/part1
   browseable = yes
   writable = yes
   valid users = $TARGET_USER
   guest ok = no
   create mask = 0775
   directory mask = 0775

[gadget_part2]
   path = $MNT_DIR/part2
   browseable = yes
   writable = yes
   valid users = $TARGET_USER
   guest ok = no
   create mask = 0775
   directory mask = 0775
EOF

# Restart Samba
systemctl restart smbd nmbd 2>/dev/null || systemctl restart smbd || true

# ===== Install and configure scripts from templates =====
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES_DIR="$SCRIPT_DIR/templates"
SCRIPTS_DIR="$SCRIPT_DIR/scripts"

# Function to configure template files
configure_template() {
  local template_file="$1"
  local output_file="$2"
  
  if [ ! -f "$template_file" ]; then
    echo "Error: Template file not found: $template_file"
    exit 1
  fi
  
  echo "Configuring: $(basename "$output_file")"
  
  # Generate a random secret key for the web interface
  SECRET_KEY=$(openssl rand -hex 32 2>/dev/null || date +%s | sha256sum | head -c 32)
  
  # Replace placeholders in template with actual values
  sed -e "s|__GADGET_DIR__|$GADGET_DIR|g" \
      -e "s|__IMG_CAM_NAME__|$IMG_CAM_NAME|g" \
      -e "s|__IMG_LIGHTSHOW_NAME__|$IMG_LIGHTSHOW_NAME|g" \
      -e "s|__MNT_DIR__|$MNT_DIR|g" \
      -e "s|__TARGET_USER__|$TARGET_USER|g" \
      -e "s|__WEB_PORT__|$WEB_PORT|g" \
      -e "s|__SECRET_KEY__|$SECRET_KEY|g" \
      "$template_file" > "$output_file"
  
  chmod +x "$output_file"
  chown "$TARGET_USER:$TARGET_USER" "$output_file"
}

# Install script files from templates
echo "Installing script files from templates..."
configure_template "$SCRIPTS_DIR/present_usb.sh" "$GADGET_DIR/present_usb.sh"
configure_template "$SCRIPTS_DIR/edit_usb.sh" "$GADGET_DIR/edit_usb.sh"
configure_template "$SCRIPTS_DIR/web_control.py" "$GADGET_DIR/web_control.py"
configure_template "$SCRIPTS_DIR/generate_thumbnails.py" "$GADGET_DIR/generate_thumbnails.py"

# ===== Configure passwordless sudo for gadget scripts =====
SUDOERS_D_DIR="/etc/sudoers.d"
SUDOERS_ENTRY="$SUDOERS_D_DIR/teslausb-gadget"
echo "Configuring passwordless sudo for gadget scripts..."
if [ ! -d "$SUDOERS_D_DIR" ]; then
  mkdir -p "$SUDOERS_D_DIR"
  chmod 755 "$SUDOERS_D_DIR"
fi

# Create comprehensive sudoers file for all commands used by the scripts
cat > "$SUDOERS_ENTRY" <<EOF
# Allow $TARGET_USER to run gadget control scripts and all required system commands
# without password for web interface automation

# First, allow the main scripts to run with full sudo privileges
$TARGET_USER ALL=(ALL) NOPASSWD: $GADGET_DIR/present_usb.sh
$TARGET_USER ALL=(ALL) NOPASSWD: $GADGET_DIR/edit_usb.sh

# Allow all system commands used within the scripts
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/sbin/smbcontrol
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/sbin/rmmod
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/sbin/modprobe
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/sbin/losetup
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/bin/mount
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/bin/umount
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/bin/fuser
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/bin/mkdir
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/bin/chown
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/bin/rm
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/sbin/fsck.vfat
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/sbin/fsck.exfat
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/sbin/blkid
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/bin/tee
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/bin/lsof
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/bin/kill
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/bin/sync
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/bin/timeout
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/bin/nsenter

# Allow cache dropping for exFAT filesystem sync (required for web lock chime updates)
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/bin/sh -c echo 3 > /proc/sys/vm/drop_caches
$TARGET_USER ALL=(ALL) NOPASSWD: /bin/sh -c echo 3 > /proc/sys/vm/drop_caches
EOF
chmod 440 "$SUDOERS_ENTRY"

# Validate sudoers file syntax
if ! visudo -c -f "$SUDOERS_ENTRY" >/dev/null 2>&1; then
  echo "ERROR: Generated sudoers file has syntax errors. Rolling back..."
  rm -f "$SUDOERS_ENTRY"
  exit 1
fi

echo "Sudoers configuration completed successfully."

STATE_FILE="$GADGET_DIR/state.txt"
if [ ! -f "$STATE_FILE" ]; then
  echo "Initializing mode state file..."
  echo "unknown" > "$STATE_FILE"
  chown "$TARGET_USER:$TARGET_USER" "$STATE_FILE"
fi

# ===== Systemd services from templates =====
echo "Installing systemd services from templates..."

# Web UI service
SERVICE_FILE="/etc/systemd/system/gadget_web.service"
configure_template "$TEMPLATES_DIR/gadget_web.service" "$SERVICE_FILE"

# Auto-present service  
AUTO_SERVICE="/etc/systemd/system/present_usb_on_boot.service"
configure_template "$TEMPLATES_DIR/present_usb_on_boot.service" "$AUTO_SERVICE"

# Thumbnail generator service
THUMBNAIL_SERVICE="/etc/systemd/system/thumbnail_generator.service"
configure_template "$TEMPLATES_DIR/thumbnail_generator.service" "$THUMBNAIL_SERVICE"

# Thumbnail generator timer
THUMBNAIL_TIMER="/etc/systemd/system/thumbnail_generator.timer"
configure_template "$TEMPLATES_DIR/thumbnail_generator.timer" "$THUMBNAIL_TIMER"

# Reload systemd and enable services
systemctl daemon-reload
systemctl enable --now gadget_web.service || systemctl restart gadget_web.service

systemctl daemon-reload
systemctl enable present_usb_on_boot.service || true

# Enable and start thumbnail generator timer
systemctl enable --now thumbnail_generator.timer || systemctl restart thumbnail_generator.timer

# Ensure the web service picks up the latest code changes
systemctl restart gadget_web.service || true

# ===== Create Chimes folder on Lightshow partition =====
echo
echo "Setting up Chimes folder on Lightshow partition..."
TEMP_MOUNT="/tmp/lightshow_setup_$$"
mkdir -p "$TEMP_MOUNT"

# Mount lightshow partition temporarily
LOOP_SETUP=$(losetup -f)
losetup "$LOOP_SETUP" "$IMG_LIGHTSHOW_PATH"
mount "$LOOP_SETUP" "$TEMP_MOUNT"

# Create Chimes directory
mkdir -p "$TEMP_MOUNT/Chimes"
mkdir -p "$TEMP_MOUNT/LightShow"  # Also ensure LightShow folder exists

# Migrate any existing WAV files (except LockChime.wav) to Chimes folder
echo "Migrating existing WAV files to Chimes folder..."
MIGRATED_COUNT=0
for wavfile in "$TEMP_MOUNT"/*.wav "$TEMP_MOUNT"/*.WAV; do
  if [ -f "$wavfile" ]; then
    filename=$(basename "$wavfile")
    # Skip LockChime.wav (case-insensitive)
    if [[ "${filename,,}" != "lockchime.wav" ]]; then
      echo "  Moving $filename to Chimes/"
      mv "$wavfile" "$TEMP_MOUNT/Chimes/"
      MIGRATED_COUNT=$((MIGRATED_COUNT + 1))
    fi
  fi
done

if [ $MIGRATED_COUNT -gt 0 ]; then
  echo "  Migrated $MIGRATED_COUNT WAV file(s) to Chimes folder"
else
  echo "  No WAV files found to migrate"
fi

# Sync and unmount
sync
umount "$TEMP_MOUNT"
losetup -d "$LOOP_SETUP"
rmdir "$TEMP_MOUNT"
echo "Chimes folder setup complete."

echo
echo "Installation complete."
echo " - present script: $GADGET_DIR/present_usb.sh"
echo " - edit script:    $GADGET_DIR/edit_usb.sh"
echo " - web UI:         http://<pi_ip>:$WEB_PORT/  (service: gadget_web.service)"
echo " - gadget auto-present on boot: present_usb_on_boot.service (enabled)"
echo "Samba shares: use user '$TARGET_USER' and the password set in SAMBA_PASS"
echo
echo "Switching to present mode..."
"$GADGET_DIR/present_usb.sh"
echo
echo "Setup complete! The Pi is now in present mode."
