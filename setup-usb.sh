#!/usr/bin/env bash
set -euo pipefail

# ================= Configuration =================
GADGET_DIR_DEFAULT="/home/mhacker/TeslaUSB"
IMG_NAME="usb_dual.img"
PART1_SIZE="20G"  
PART2_SIZE="16G"
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
  TARGET_USER="mhacker"
fi

GADGET_DIR="$GADGET_DIR_DEFAULT"
IMG_PATH="$GADGET_DIR/$IMG_NAME"

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
TOTAL_MB=$((P1_MB + P2_MB + 2))

# Install prerequisites (only fetch/install if something is missing)
REQUIRED_PACKAGES=(
  parted
  dosfstools
  util-linux
  python3-flask
  samba
  samba-common-bin
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
if [ -f "$CONFIG_FILE" ]; then
  if ! grep -q '^dtoverlay=dwc2' "$CONFIG_FILE"; then
    if grep -q '^\[all\]' "$CONFIG_FILE"; then
      sed -i '/^\[all\]/a dtoverlay=dwc2' "$CONFIG_FILE"
      echo "Added dtoverlay=dwc2 under [all] in $CONFIG_FILE"
    else
      printf '\n[all]\ndtoverlay=dwc2\n' | tee -a "$CONFIG_FILE" >/dev/null
      echo "Appended [all] with dtoverlay=dwc2 to $CONFIG_FILE"
    fi
  else
    echo "dtoverlay=dwc2 already present in $CONFIG_FILE"
  fi
else
  echo "Warning: $CONFIG_FILE not found. Ensure your Pi uses /boot/firmware/config.txt"
fi

# Create gadget folder
mkdir -p "$GADGET_DIR"
chown "$TARGET_USER:$TARGET_USER" "$GADGET_DIR"

# Cleanup function for loop device
cleanup_loop_device() {
  if [ -n "${LOOP_DEV:-}" ]; then
    echo "Cleaning up loop device: $LOOP_DEV"
    losetup -d "$LOOP_DEV" 2>/dev/null || true
    LOOP_DEV=""
  fi
}

# Create image (if missing)
if [ -f "$IMG_PATH" ]; then
  echo "Image already exists at $IMG_PATH — skipping creation."
else
  # Set trap to cleanup on exit/error
  trap cleanup_loop_device EXIT INT TERM
  
  echo "Creating image $IMG_PATH (${TOTAL_MB}M)..."
  # Create sparse file (thin provisioned) - only allocates space as needed
  truncate -s "${TOTAL_MB}M" "$IMG_PATH" || {
    echo "Error: Failed to create image file"
    exit 1
  }
  
  LOOP_DEV=$(losetup --find --show "$IMG_PATH") || {
    echo "Error: Failed to create loop device"
    exit 1
  }
  
  # Validate loop device was created
  if [ -z "$LOOP_DEV" ] || [ ! -e "$LOOP_DEV" ]; then
    echo "Error: Loop device creation failed or device not accessible"
    exit 1
  fi
  
  echo "Using loop device: $LOOP_DEV"
  
  # Create partition table with error checking
  parted -s "$LOOP_DEV" mklabel msdos || {
    echo "Error: Failed to create partition table"
    exit 1
  }
  
  parted -s "$LOOP_DEV" mkpart primary fat32 1MiB $((1+P1_MB))MiB || {
    echo "Error: Failed to create first partition"
    exit 1
  }
  
  parted -s "$LOOP_DEV" mkpart primary fat32 $((1+P1_MB))MiB 100% || {
    echo "Error: Failed to create second partition"
    exit 1
  }
  
  partprobe "$LOOP_DEV" || true
  
  # Wait for partition nodes to appear (up to 10 seconds)
  echo "Waiting for partition nodes to appear..."
  for i in {1..10}; do
    if [ -e "${LOOP_DEV}p1" ] && [ -e "${LOOP_DEV}p2" ]; then
      echo "Partition nodes ready after ${i} seconds"
      break
    fi
    if [ $i -eq 10 ]; then
      echo "Error: Partition nodes ${LOOP_DEV}p1 and ${LOOP_DEV}p2 did not appear after 10 seconds"
      exit 1
    fi
    sleep 1
  done
  
  # Format partitions with error checking
  mkfs.vfat -F 32 -n "$LABEL1" "${LOOP_DEV}p1" || {
    echo "Error: Failed to format first partition"
    exit 1
  }
  
  mkfs.vfat -F 32 -n "$LABEL2" "${LOOP_DEV}p2" || {
    echo "Error: Failed to format second partition"
    exit 1
  }
  
  # Clean up loop device (will also be called by trap)
  cleanup_loop_device
  trap - EXIT INT TERM  # Remove trap since we're cleaning up normally
  
  echo "Image created and partitions formatted."
fi

# Create mount points
mkdir -p "$MNT_DIR/part1" "$MNT_DIR/part2"
chown "$TARGET_USER:$TARGET_USER" "$MNT_DIR/part1" "$MNT_DIR/part2"
chmod 775 "$MNT_DIR/part1" "$MNT_DIR/part2"

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

# Add authenticated shares
cat >> "$SMB_CONF" <<EOF

[gadget_part1]
   path = $MNT_DIR/part1
   browseable = yes
   writable = yes
   valid users = $TARGET_USER
   create mask = 0775
   directory mask = 0775

[gadget_part2]
   path = $MNT_DIR/part2
   browseable = yes
   writable = yes
   valid users = $TARGET_USER
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
      -e "s|__IMG_NAME__|$IMG_NAME|g" \
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

# ===== Configure passwordless sudo for gadget scripts =====
SUDOERS_D_DIR="/etc/sudoers.d"
SUDOERS_ENTRY="$SUDOERS_D_DIR/teslausb-gadget"
echo "Configuring passwordless sudo for gadget scripts..."
if [ ! -d "$SUDOERS_D_DIR" ]; then
  mkdir -p "$SUDOERS_D_DIR"
  chmod 755 "$SUDOERS_D_DIR"
fi

cat > "$SUDOERS_ENTRY" <<EOF
$TARGET_USER ALL=(ALL) NOPASSWD: \
  $GADGET_DIR/present_usb.sh, \
  $GADGET_DIR/edit_usb.sh, \
  /usr/bin/systemctl restart smbd, \
  /usr/bin/systemctl restart nmbd, \
  /usr/bin/smbcontrol
EOF
chmod 440 "$SUDOERS_ENTRY"

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

# Reload systemd and enable services
systemctl daemon-reload
systemctl enable --now gadget_web.service || systemctl restart gadget_web.service

systemctl daemon-reload
systemctl enable present_usb_on_boot.service || true

# Ensure the web service picks up the latest code changes
systemctl restart gadget_web.service || true

echo
echo "Installation complete."
echo " - present script: $GADGET_DIR/present_usb.sh"
echo " - edit script:    $GADGET_DIR/edit_usb.sh"
echo " - web UI:         http://<pi_ip>:$WEB_PORT/  (service: gadget_web.service)"
echo " - gadget auto-present on boot: present_usb_on_boot.service (enabled)"
echo "Samba shares: use user '$TARGET_USER' and the password set in SAMBA_PASS"
