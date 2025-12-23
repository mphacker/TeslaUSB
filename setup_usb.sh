#!/usr/bin/env bash
set -euo pipefail

# ================= Configuration =================
GADGET_DIR_DEFAULT="/home/pi/TeslaUSB"
IMG_CAM_NAME="usb_cam.img"        # TeslaCam partition (read-write)
IMG_LIGHTSHOW_NAME="usb_lightshow.img"  # Lightshow partition (read-only)
PART1_SIZE=""
PART2_SIZE=""
RESERVE_SIZE=""   # headroom to leave free on the Pi filesystem (default suggested: 5G)
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

# ===== Friendly image sizing (safe defaults; avoid filling rootfs) =====

mib_to_gib_str() {
  local mib="$1"
  local gib=$(( mib / 1024 ))
  if [ "$gib" -lt 1 ]; then
    echo "${mib}M"
  else
    echo "${gib}G"
  fi
}

round_down_gib_mib() {
  local mib="$1"
  local rounded=$(( (mib / 1024) * 1024 ))
  if [ "$rounded" -lt 512 ]; then
    rounded=512
  fi
  echo "$rounded"
}

fs_avail_bytes_for_path() {
  local path="$1"
  df -B1 --output=avail "$path" | tail -n 1 | tr -d ' '
}

size_to_bytes() {
  local s="$1"
  if [[ "$s" =~ ^([0-9]+)([Mm])$ ]]; then
    echo $(( ${BASH_REMATCH[1]} * 1024 * 1024 ))
  elif [[ "$s" =~ ^([0-9]+)([Gg])$ ]]; then
    echo $(( ${BASH_REMATCH[1]} * 1024 * 1024 * 1024 ))
  else
    echo "Invalid size format: $s (use 512M or 5G)" >&2
    exit 2
  fi
}

# If sizes are not configured, suggest safe defaults based on free space
# on the filesystem that will store the image files (GADGET_DIR_DEFAULT).
NEED_SIZE_VALIDATION=0
USABLE_MIB=0

if [ -z "${PART1_SIZE}" ] || [ -z "${PART2_SIZE}" ]; then
  # Ensure parent directory exists for df check
  mkdir -p "$GADGET_DIR_DEFAULT" 2>/dev/null || true
  FS_AVAIL_BYTES="$(fs_avail_bytes_for_path "$GADGET_DIR_DEFAULT")"

  # Headroom: default 5G, user-adjustable
  DEFAULT_RESERVE_STR="5G"

  if [ -z "${RESERVE_SIZE}" ]; then
    read -r -p "Filesystem headroom to leave free (default ${DEFAULT_RESERVE_STR}): " RESERVE_INPUT
    RESERVE_SIZE="${RESERVE_INPUT:-$DEFAULT_RESERVE_STR}"
  fi

  RESERVE_BYTES="$(size_to_bytes "$RESERVE_SIZE")"

  if [ "$FS_AVAIL_BYTES" -le "$RESERVE_BYTES" ]; then
    echo "ERROR: Not enough free space to safely create image files under $GADGET_DIR_DEFAULT."
    echo "Free:    $((FS_AVAIL_BYTES / 1024 / 1024)) MiB"
    echo "Safety Reserve: $RESERVE_SIZE ($((RESERVE_BYTES / 1024 / 1024)) MiB)"
    echo "Free up space or move GADGET_DIR to a larger filesystem."
    exit 1
  fi

  USABLE_BYTES=$(( FS_AVAIL_BYTES - RESERVE_BYTES ))
  USABLE_MIB=$(( USABLE_BYTES / 1024 / 1024 ))

  # Default Lightshow to 10G
  DEFAULT_P2_MIB=10240
  DEFAULT_P2_STR="10G"

  if [ "$USABLE_MIB" -le "$DEFAULT_P2_MIB" ]; then
    echo "ERROR: Not enough usable space for Lightshow default (${DEFAULT_P2_STR}) after safety reserve."
    echo "Usable: ${USABLE_MIB} MiB, Lightshow: ${DEFAULT_P2_MIB} MiB"
    echo "Free up space or reduce Lightshow size."
    exit 1
  fi

  SUG_P2_STR="$DEFAULT_P2_STR"
  SUG_P1_MIB="$(round_down_gib_mib $(( USABLE_MIB - DEFAULT_P2_MIB )))"
  SUG_P1_STR="$(mib_to_gib_str "$SUG_P1_MIB")"

  echo ""
  echo "============================================"
  echo "TeslaUSB image sizing"
  echo "============================================"
  echo "Images will be created under: $GADGET_DIR_DEFAULT"
  echo "Filesystem free space: $((FS_AVAIL_BYTES / 1024 / 1024)) MiB"
  echo "Safety reserve:        $((RESERVE_BYTES / 1024 / 1024)) MiB"
  echo "Usable for images:     ${USABLE_MIB} MiB"
  echo ""
  echo "Recommended sizes (safe, leaves headroom for Raspberry Pi OS):"
  echo "  Lightshow (PART2_SIZE): $SUG_P2_STR (default)"
  echo "  TeslaCam  (PART1_SIZE): $SUG_P1_STR (uses remaining usable space)"
  echo ""

  if [ -z "${PART2_SIZE}" ]; then
    read -r -p "Enter Lightshow size (default ${SUG_P2_STR}): " PART2_SIZE_INPUT
    PART2_SIZE="${PART2_SIZE_INPUT:-$SUG_P2_STR}"
    # Validate format immediately
    if ! size_to_bytes "$PART2_SIZE" >/dev/null 2>&1; then
      echo "ERROR: Invalid size format for Lightshow: $PART2_SIZE"
      echo "Use format like 512M or 5G (whole numbers only)"
      exit 2
    fi
  fi

  if [ -z "${PART1_SIZE}" ]; then
    read -r -p "Enter TeslaCam size (default ${SUG_P1_STR}): " PART1_SIZE_INPUT
    PART1_SIZE="${PART1_SIZE_INPUT:-$SUG_P1_STR}"
    # Validate format immediately
    if ! size_to_bytes "$PART1_SIZE" >/dev/null 2>&1; then
      echo "ERROR: Invalid size format for TeslaCam: $PART1_SIZE"
      echo "Use format like 512M or 5G (whole numbers only)"
      exit 2
    fi
  fi

  echo ""
  echo "Selected sizes:"
  echo "  PART1_SIZE=$PART1_SIZE"
  echo "  PART2_SIZE=$PART2_SIZE"
  echo ""

  NEED_SIZE_VALIDATION=1
fi

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

# Validate selected sizes against usable space (if computed)
if [ "${NEED_SIZE_VALIDATION:-0}" = "1" ]; then
  TOTAL_MIB=$(( P1_MB + P2_MB ))
  if [ "$TOTAL_MIB" -gt "$USABLE_MIB" ]; then
    echo "ERROR: Selected sizes exceed safe usable space under $GADGET_DIR_DEFAULT."
    echo "Usable:  ${USABLE_MIB} MiB (after safety reserve)"
    echo "Chosen:  ${TOTAL_MIB} MiB (PART1=${P1_MB} MiB, PART2=${P2_MB} MiB)"
    echo "Reduce TeslaCam and/or Lightshow sizes."
    exit 1
  fi
fi

echo "============================================"
echo "Preview"
echo "============================================"
echo "This will create (or keep, if already present) the following image files:"
echo "  1) TeslaCam  : $IMG_CAM_PATH  size=$PART1_SIZE  label=$LABEL1  (read-write)"
echo "  2) Lightshow : $IMG_LIGHTSHOW_PATH  size=$PART2_SIZE  label=$LABEL2  (read-only)"
echo ""
echo "Images are stored under: $GADGET_DIR_DEFAULT"
echo "If these sizes are too large, the Pi can run out of disk and behave badly."
echo ""
read -r -p "Proceed with these sizes? [y/N]: " PROCEED
PROCEED_LC="$(printf '%s' "$PROCEED" | tr '[:upper:]' '[:lower:]')"
case "$PROCEED_LC" in
  y|yes) echo "Proceeding..." ;;
  *) echo "Aborted by user."; exit 0 ;;
esac
echo ""

# Install prerequisites (only fetch/install if something is missing)

REQUIRED_PACKAGES=(
  parted
  dosfstools
  exfatprogs
  util-linux
  psmisc
  python3-flask
  python3-waitress
  python3-av
  python3-pil
  samba
  samba-common-bin
  ffmpeg
  watchdog
  wireless-tools
  iw
  hostapd
  dnsmasq
)

# Note on packages:
# - python3-waitress: Production WSGI server (10-20x faster than Flask dev server)
# - python3-av: PyAV for instant thumbnail generation
# - python3-pil: PIL/Pillow for image resizing
# - ffmpeg: Used by lock chime service for audio validation and re-encoding

# Lightweight apt helpers (reduce OOM risk on Pi Zero/2W)
apt_update_safe() {
  local attempt=1
  local max_attempts=3
  while [ $attempt -le $max_attempts ]; do
    echo "Running apt-get update (attempt $attempt/$max_attempts)..."
    if apt-get update \
      -o Acquire::Retries=3 \
      -o Acquire::http::No-Cache=true \
      -o Acquire::Languages=none \
      -o APT::Update::Reduce-Download-Size=true \
      -o Acquire::PDiffs=true \
      -o Acquire::http::Pipeline-Depth=0; then
      return 0
    fi
    echo "apt-get update failed (attempt $attempt). Cleaning lists and retrying..."
    rm -rf /var/lib/apt/lists/*
    attempt=$((attempt + 1))
    sleep 2
  done
  echo "apt-get update failed after $max_attempts attempts" >&2
  return 1
}

install_pkg_safe() {
  local pkg="$1"
  echo "Installing $pkg (no-recommends)..."
  if apt-get install -y --no-install-recommends "$pkg"; then
    return 0
  fi
  echo "Retrying $pkg with default recommends..."
  apt-get install -y "$pkg"
}

enable_install_swap() {
  INSTALL_SWAP="/var/swap/teslausb_pkg.swap"
  if swapon --show | grep -q "$INSTALL_SWAP" 2>/dev/null; then
    echo "Temporary swap already active"
    return
  fi
  echo "Enabling temporary swap for package installs (1GB)..."
  # Use existing swap if available, otherwise create temporary
  if [ -f "/var/swap/fsck.swap" ] && ! swapon --show | grep -q "fsck.swap" 2>/dev/null; then
    echo "  Using existing fsck swap file"
    swapon /var/swap/fsck.swap 2>/dev/null && return
  fi
  # Create temporary 1GB swap
  mkdir -p /var/swap
  if fallocate -l 1G "$INSTALL_SWAP" 2>/dev/null || dd if=/dev/zero of="$INSTALL_SWAP" bs=1M count=1024 status=none; then
    chmod 600 "$INSTALL_SWAP"
    mkswap "$INSTALL_SWAP" >/dev/null 2>&1 || { echo "mkswap failed"; return 1; }
    swapon "$INSTALL_SWAP" 2>/dev/null || { echo "swapon failed"; return 1; }
    echo "  Swap enabled: $(swapon --show | grep -E 'teslausb|fsck' || echo 'NONE - FAILED')"
  else
    echo "ERROR: could not create temporary swap"
    return 1
  fi
}

disable_install_swap() {
  if [ -n "${INSTALL_SWAP-}" ] && [ -f "$INSTALL_SWAP" ]; then
    swapoff "$INSTALL_SWAP" 2>/dev/null || true
    rm -f "$INSTALL_SWAP"
  fi
}

stop_nonessential_services() {
  # Stop heavy memory users during package install (keep WiFi up)
  echo "Stopping memory-intensive services..."
  systemctl is-active gadget_web.service >/dev/null 2>&1 && systemctl stop gadget_web.service 2>/dev/null || true
  systemctl is-active chime_scheduler.service >/dev/null 2>&1 && systemctl stop chime_scheduler.service 2>/dev/null || true
  systemctl is-active chime_scheduler.timer >/dev/null 2>&1 && systemctl stop chime_scheduler.timer 2>/dev/null || true
  systemctl is-active smbd >/dev/null 2>&1 && systemctl stop smbd 2>/dev/null || true
  systemctl is-active nmbd >/dev/null 2>&1 && systemctl stop nmbd 2>/dev/null || true
  systemctl is-active cups.service >/dev/null 2>&1 && systemctl stop cups.service 2>/dev/null || true
  systemctl is-active cups-browsed.service >/dev/null 2>&1 && systemctl stop cups-browsed.service 2>/dev/null || true
  systemctl is-active ModemManager.service >/dev/null 2>&1 && systemctl stop ModemManager.service 2>/dev/null || true
  systemctl is-active packagekit.service >/dev/null 2>&1 && systemctl stop packagekit.service 2>/dev/null || true
  systemctl is-active lightdm.service >/dev/null 2>&1 && systemctl stop lightdm.service 2>/dev/null || true
  echo "  Stopped active services to free memory"
}

start_nonessential_services() {
  echo "Restarting services..."
  systemctl is-enabled smbd >/dev/null 2>&1 && systemctl start smbd 2>/dev/null || true
  systemctl is-enabled nmbd >/dev/null 2>&1 && systemctl start nmbd 2>/dev/null || true
  systemctl is-enabled chime_scheduler.timer >/dev/null 2>&1 && systemctl start chime_scheduler.timer 2>/dev/null || true
  systemctl is-enabled gadget_web.service >/dev/null 2>&1 && systemctl start gadget_web.service 2>/dev/null || true
  # Only restart if enabled (don't re-enable lightdm if we just disabled it)
  systemctl is-enabled lightdm.service >/dev/null 2>&1 && systemctl start lightdm.service 2>/dev/null || true
  systemctl is-enabled cups.service >/dev/null 2>&1 && systemctl start cups.service 2>/dev/null || true
  echo "  Services restarted"
}

# ===== Clean up old/unused services from previous installations =====
cleanup_old_services() {
  echo "Checking for old/unused services from previous installations..."

  # Stop and disable old thumbnail generator service (replaced by on-demand generation)
  if systemctl list-unit-files | grep -q 'thumbnail_generator'; then
    echo "  Removing old thumbnail_generator service..."
    systemctl stop thumbnail_generator.service 2>/dev/null || true
    systemctl stop thumbnail_generator.timer 2>/dev/null || true
    systemctl disable thumbnail_generator.service 2>/dev/null || true
    systemctl disable thumbnail_generator.timer 2>/dev/null || true
    systemctl unmask thumbnail_generator.service 2>/dev/null || true
    systemctl unmask thumbnail_generator.timer 2>/dev/null || true
    rm -f /etc/systemd/system/thumbnail_generator.service
    rm -f /etc/systemd/system/thumbnail_generator.timer
    systemctl daemon-reload
    echo "    ✓ Removed thumbnail_generator service and timer"
  fi

  # Remove old template files if they exist
  if [ -f "$GADGET_DIR/templates/thumbnail_generator.service" ] || [ -f "$GADGET_DIR/templates/thumbnail_generator.timer" ]; then
    echo "  Removing old thumbnail generator templates..."
    rm -f "$GADGET_DIR/templates/thumbnail_generator.service"
    rm -f "$GADGET_DIR/templates/thumbnail_generator.timer"
    echo "    ✓ Removed old template files"
  fi

  # Remove old background thumbnail generation script
  if [ -f "$GADGET_DIR/scripts/generate_thumbnails.py" ]; then
    echo "  Removing old background thumbnail generator script..."
    rm -f "$GADGET_DIR/scripts/generate_thumbnails.py"
    echo "    ✓ Removed generate_thumbnails.py"
  fi

  echo "Old service cleanup complete."
}

# ===== Optimize memory for setup (disable unnecessary services) =====
optimize_memory_for_setup() {
  echo "Optimizing memory for setup..."

  # Disable graphical desktop services if present (saves 50-60MB on Pi Zero 2W)
  if systemctl is-enabled lightdm.service >/dev/null 2>&1; then
    echo "  Disabling graphical desktop (lightdm)..."
    systemctl stop lightdm graphical.target 2>/dev/null || true
    systemctl disable lightdm 2>/dev/null || true
    systemctl set-default multi-user.target 2>/dev/null || true
    echo "    ✓ Graphical desktop disabled (saves ~50-60MB RAM)"
  else
    echo "  Graphical desktop not installed or already disabled"
  fi

  # Ensure swap is available early (critical for low-memory systems)
  if ! swapon --show 2>/dev/null | grep -q '/'; then
    echo "  No active swap detected, enabling swap for setup..."

    # Try to use existing fsck swap if available
    if [ -f "/var/swap/fsck.swap" ]; then
      echo "    Using existing fsck.swap file"
      swapon /var/swap/fsck.swap 2>/dev/null && echo "    ✓ Swap enabled (fsck.swap)" && return
    fi

    # Try to use any existing swapfile
    if [ -f "/swapfile" ]; then
      echo "    Using existing /swapfile"
      swapon /swapfile 2>/dev/null && echo "    ✓ Swap enabled (/swapfile)" && return
    fi

    # Create temporary swap for setup
    echo "    Creating temporary 512MB swap..."
    if dd if=/dev/zero of=/swapfile bs=1M count=512 status=none 2>/dev/null; then
      chmod 600 /swapfile
      mkswap /swapfile >/dev/null 2>&1
      swapon /swapfile 2>/dev/null && echo "    ✓ Temporary swap created and enabled (512MB)"
    else
      echo "    Warning: Could not create swap (may cause OOM on low-memory systems)"
    fi
  else
    echo "  Swap already active: $(swapon --show 2>/dev/null | tail -n +2 | awk '{print $1, $3}')"
  fi

  echo "Memory optimization complete."
  echo ""
}

# Run cleanup before package installation
cleanup_old_services

# Optimize memory before package installation (critical for Pi Zero/2W)
optimize_memory_for_setup

MISSING_PACKAGES=()
for pkg in "${REQUIRED_PACKAGES[@]}"; do
  if ! dpkg -s "$pkg" >/dev/null 2>&1; then
    MISSING_PACKAGES+=("$pkg")
  fi
done

if [ ${#MISSING_PACKAGES[@]} -gt 0 ]; then
  echo "Installing missing packages: ${MISSING_PACKAGES[*]}"

  # Prepare for low-memory install
  stop_nonessential_services
  enable_install_swap || { echo "ERROR: Failed to enable swap. Cannot proceed."; exit 1; }

  # Run apt-get update
  apt_update_safe

  # Install packages one at a time to avoid OOM on low-memory systems
  for pkg in "${MISSING_PACKAGES[@]}"; do
    install_pkg_safe "$pkg" || echo "Warning: install of $pkg reported an error"
  done

  # Cleanup
  disable_install_swap
  start_nonessential_services

  # Remove orphaned packages to save disk space
  echo "Removing orphaned packages..."
  apt-get autoremove -y >/dev/null 2>&1 || true
  echo "  ✓ Orphaned packages removed"
else
  echo "All required packages already installed; skipping apt install."
fi

# Ensure hostapd/dnsmasq don't auto-start outside our controller
systemctl disable hostapd 2>/dev/null || true
systemctl stop hostapd 2>/dev/null || true
systemctl disable dnsmasq 2>/dev/null || true
systemctl stop dnsmasq 2>/dev/null || true

# Configure NetworkManager to ignore virtual AP interface (uap0)
NM_CONF_DIR="/etc/NetworkManager/conf.d"
NM_UNMANAGED_CONF="$NM_CONF_DIR/unmanaged-uap0.conf"
if [ ! -f "$NM_UNMANAGED_CONF" ]; then
  mkdir -p "$NM_CONF_DIR"
  cat > "$NM_UNMANAGED_CONF" <<EOF
[keyfile]
unmanaged-devices=interface-name:uap0
EOF
  echo "Created NetworkManager config to ignore uap0 interface"
  if systemctl is-active --quiet NetworkManager; then
    systemctl reload NetworkManager 2>/dev/null || true
  fi
else
  echo "NetworkManager already configured to ignore uap0"
fi

# Ensure config.txt contains dtoverlay=dwc2 and dtparam=watchdog=on under [all]
# Note: We use dtoverlay=dwc2 WITHOUT dr_mode parameter to allow auto-detection
CONFIG_CHANGED=0
if [ -f "$CONFIG_FILE" ]; then
  # Check if [all] section exists
  if grep -q '^\[all\]' "$CONFIG_FILE"; then
    # [all] section exists - check and add entries if needed

    # Check and add dtoverlay=dwc2 (only if not already present)
    if ! grep -q '^dtoverlay=dwc2$' "$CONFIG_FILE"; then
      # Add dtoverlay=dwc2 right after [all] line
      sed -i '/^\[all\]/a dtoverlay=dwc2' "$CONFIG_FILE"
      echo "Added dtoverlay=dwc2 under [all] section in $CONFIG_FILE"
      CONFIG_CHANGED=1
    else
      echo "dtoverlay=dwc2 already present in $CONFIG_FILE"
    fi

    # Check and add dtparam=watchdog=on (only if not already present)
    if ! grep -q '^dtparam=watchdog=on$' "$CONFIG_FILE"; then
      # Add dtparam=watchdog=on right after [all] line
      sed -i '/^\[all\]/a dtparam=watchdog=on' "$CONFIG_FILE"
      echo "Added dtparam=watchdog=on under [all] section in $CONFIG_FILE"
      CONFIG_CHANGED=1
    else
      echo "dtparam=watchdog=on already present in $CONFIG_FILE"
    fi
  else
    # No [all] section - append it with both entries
    printf '\n[all]\ndtoverlay=dwc2\ndtparam=watchdog=on\n' >> "$CONFIG_FILE"
    echo "Appended [all] section with dtoverlay=dwc2 and dtparam=watchdog=on to $CONFIG_FILE"
    CONFIG_CHANGED=1
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

# ===== Configure scripts (no copying - run in place) =====
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES_DIR="$SCRIPT_DIR/templates"
SCRIPTS_DIR="$SCRIPT_DIR/scripts"

echo "Verifying scripts directory structure..."
if [ ! -d "$SCRIPTS_DIR/web" ]; then
  echo "ERROR: scripts/web directory not found at $SCRIPTS_DIR/web"
  exit 1
fi

# Ensure GADGET_DIR and SCRIPTS_DIR are the same (run-in-place)
if [ "$GADGET_DIR" != "$SCRIPT_DIR" ]; then
  echo "WARNING: GADGET_DIR ($GADGET_DIR) differs from SCRIPT_DIR ($SCRIPT_DIR)"
  echo "This setup expects to run in-place at $GADGET_DIR"
  echo "Please ensure this script is run from $GADGET_DIR"
fi

# Create runtime directories
mkdir -p "$GADGET_DIR/thumbnails"
chown -R "$TARGET_USER:$TARGET_USER" "$GADGET_DIR/thumbnails"

# Set permissions on scripts
chmod +x "$SCRIPTS_DIR"/*.sh "$SCRIPTS_DIR"/*.py 2>/dev/null || true
chown -R "$TARGET_USER:$TARGET_USER" "$SCRIPTS_DIR"

echo ""
echo "============================================"
echo "Scripts are running in-place from:"
echo "  $SCRIPTS_DIR"
echo ""
echo "Edit configuration files:"
echo "  - $SCRIPTS_DIR/config.sh (shell scripts)"
echo "  - $SCRIPTS_DIR/web/config.py (web app)"
echo "============================================"
echo ""

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
$TARGET_USER ALL=(ALL) NOPASSWD: $GADGET_DIR/scripts/present_usb.sh
$TARGET_USER ALL=(ALL) NOPASSWD: $GADGET_DIR/scripts/edit_usb.sh
$TARGET_USER ALL=(ALL) NOPASSWD: $GADGET_DIR/scripts/ap_control.sh

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
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/bin/sed
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/bin/pkill
$TARGET_USER ALL=(ALL) NOPASSWD: /usr/bin/nmcli

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

# ===== Clean up deprecated thumbnail system =====
echo "Cleaning up deprecated thumbnail generation system..."

# Stop and disable old thumbnail services
if systemctl is-enabled thumbnail_generator.service 2>/dev/null; then
  systemctl stop thumbnail_generator.service 2>/dev/null || true
  systemctl disable thumbnail_generator.service 2>/dev/null || true
fi

if systemctl is-enabled thumbnail_generator.timer 2>/dev/null; then
  systemctl stop thumbnail_generator.timer 2>/dev/null || true
  systemctl disable thumbnail_generator.timer 2>/dev/null || true
fi

# Remove systemd service files
rm -f /etc/systemd/system/thumbnail_generator.service 2>/dev/null || true
rm -f /etc/systemd/system/thumbnail_generator.timer 2>/dev/null || true

# Remove thumbnail service Python file
rm -f "$GADGET_DIR/scripts/web/services/thumbnail_service.py" 2>/dev/null || true

# Remove thumbnail cache directory
if [ -d "$GADGET_DIR/thumbnails" ]; then
  echo "  Removing thumbnail cache directory..."
  rm -rf "$GADGET_DIR/thumbnails" 2>/dev/null || true
fi

echo "Deprecated thumbnail system cleanup complete."

# ===== Systemd services =====
echo "Installing systemd services..."

# Helper function to process systemd service templates
configure_service() {
  local template_file="$1"
  local output_file="$2"

  sed -e "s|__GADGET_DIR__|$GADGET_DIR|g" \
      -e "s|__MNT_DIR__|$MNT_DIR|g" \
      -e "s|__TARGET_USER__|$TARGET_USER|g" \
      "$template_file" > "$output_file"
}

# Web UI service
SERVICE_FILE="/etc/systemd/system/gadget_web.service"
configure_service "$TEMPLATES_DIR/gadget_web.service" "$SERVICE_FILE"

# Auto-present service
AUTO_SERVICE="/etc/systemd/system/present_usb_on_boot.service"
configure_service "$TEMPLATES_DIR/present_usb_on_boot.service" "$AUTO_SERVICE"

# Chime scheduler service
CHIME_SCHEDULER_SERVICE="/etc/systemd/system/chime_scheduler.service"
configure_service "$TEMPLATES_DIR/chime_scheduler.service" "$CHIME_SCHEDULER_SERVICE"

# Chime scheduler timer
CHIME_SCHEDULER_TIMER="/etc/systemd/system/chime_scheduler.timer"
configure_service "$TEMPLATES_DIR/chime_scheduler.timer" "$CHIME_SCHEDULER_TIMER"

# WiFi power management disable service
WIFI_POWERSAVE_SERVICE="/etc/systemd/system/wifi-powersave-off.service"
configure_service "$TEMPLATES_DIR/wifi-powersave-off.service" "$WIFI_POWERSAVE_SERVICE"

# WiFi monitor service
WIFI_MONITOR_SERVICE="/etc/systemd/system/wifi-monitor.service"
configure_service "$TEMPLATES_DIR/wifi-monitor.service" "$WIFI_MONITOR_SERVICE"

# Ensure wifi-monitor.sh and optimize_network.sh are executable
chmod +x "$SCRIPT_DIR/scripts/wifi-monitor.sh"
chmod +x "$SCRIPT_DIR/scripts/optimize_network.sh" 2>/dev/null || true

# Apply network optimizations on first boot
if [ -f "$SCRIPT_DIR/scripts/optimize_network.sh" ]; then
  echo "Applying network optimizations..."
  "$SCRIPT_DIR/scripts/optimize_network.sh" 2>/dev/null || echo "  Note: Some optimizations require reboot to take effect"
fi

# Reload systemd and enable services
systemctl daemon-reload
systemctl enable --now gadget_web.service || systemctl restart gadget_web.service

systemctl daemon-reload
systemctl enable present_usb_on_boot.service || true

# Enable and start chime scheduler timer
systemctl enable --now chime_scheduler.timer || systemctl restart chime_scheduler.timer

# Enable and start WiFi monitoring services
systemctl enable --now wifi-powersave-off.service || systemctl restart wifi-powersave-off.service
systemctl enable --now wifi-monitor.service || systemctl restart wifi-monitor.service

# Ensure the web service picks up the latest code changes
systemctl restart gadget_web.service || true

# ===== Configure System Reliability Features =====
echo
echo "Configuring system reliability features..."

# Configure sysctl for kernel panic auto-reboot and network performance
SYSCTL_CONF="/etc/sysctl.d/99-teslausb.conf"
if [ ! -f "$SYSCTL_CONF" ] || ! grep -q "kernel.panic" "$SYSCTL_CONF" 2>/dev/null; then
  echo "Creating sysctl configuration for system reliability and network performance..."
  cat > "$SYSCTL_CONF" <<'EOF'
# TeslaUSB System Reliability Configuration

# Reboot 10 seconds after kernel panic
kernel.panic = 10

# Treat kernel oops as panic (triggers auto-reboot)
kernel.panic_on_oops = 1

# Don't panic on OOM - let OOM killer work instead
vm.panic_on_oom = 0

# Swappiness (how aggressively to use swap) - low value for SD card longevity
vm.swappiness = 10

# Network Performance Tuning (WiFi optimization)
# Increase network buffer sizes for better throughput
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.core.rmem_default = 1048576
net.core.wmem_default = 1048576

# TCP buffer auto-tuning (min, default, max in bytes)
net.ipv4.tcp_rmem = 4096 1048576 16777216
net.ipv4.tcp_wmem = 4096 1048576 16777216

# Enable TCP window scaling for high-latency networks
net.ipv4.tcp_window_scaling = 1

# Use BBR congestion control (better for WiFi/wireless)
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr

# Reduce TIME_WAIT socket timeout to free resources faster
net.ipv4.tcp_fin_timeout = 15

# Allow reuse of TIME_WAIT sockets
net.ipv4.tcp_tw_reuse = 1

# Increase max queued packets
net.core.netdev_max_backlog = 5000

# Enable TCP fast open
net.ipv4.tcp_fastopen = 3

# Disable IPv6 if not needed (reduces overhead)
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
EOF
  chmod 644 "$SYSCTL_CONF"
  echo "  Created $SYSCTL_CONF"

  # Apply sysctl settings immediately
  sysctl -p "$SYSCTL_CONF" >/dev/null 2>&1 || true
  echo "  Applied sysctl settings"
else
  echo "Sysctl configuration already exists at $SYSCTL_CONF"
fi

# Configure hardware watchdog
WATCHDOG_CONF="/etc/watchdog.conf"
if [ ! -f "$WATCHDOG_CONF" ] || ! grep -q "watchdog-device" "$WATCHDOG_CONF" 2>/dev/null; then
  echo "Configuring hardware watchdog..."
  cat > "$WATCHDOG_CONF" <<'EOF'
# TeslaUSB Hardware Watchdog Configuration
# Tuned for Raspberry Pi Zero 2W (512MB RAM, 4 cores)

# Watchdog device
watchdog-device = /dev/watchdog

# Watchdog timeout (hardware reset after 15 seconds of no response)
watchdog-timeout = 15

# Test /dev/watchdog every 10 seconds
interval = 10

# Reboot if 1-minute load average exceeds 24 (6x the 4 cores)
max-load-1 = 24

# Reboot if free memory drops below 50MB (about 10% of 512MB)
min-memory = 50000

# Realtime priority for watchdog daemon
realtime = yes
priority = 1

# Log to syslog
log-dir = /var/log/watchdog

# Repair binary (try to fix issues before forcing reboot)
repair-binary = /usr/lib/watchdog/repair
repair-timeout = 60

# Test network connectivity (optional - can be enabled if desired)
# ping = 8.8.8.8
# ping-count = 3

# Verbose logging
verbose = yes
EOF
  chmod 644 "$WATCHDOG_CONF"
  echo "  Created $WATCHDOG_CONF"
else
  echo "Watchdog configuration already exists at $WATCHDOG_CONF"
fi

# Enable and start watchdog service
echo "Enabling watchdog service..."
systemctl enable watchdog.service || true
systemctl restart watchdog.service 2>/dev/null || echo "  Note: Watchdog will start on next reboot (requires dtparam=watchdog=on)"

echo "System reliability features configured."

# ===== Create Persistent Swapfile for FSCK Operations =====
echo
echo "Creating persistent swapfile for filesystem checks..."
SWAP_DIR="/var/swap"
SWAP_FILE="$SWAP_DIR/fsck.swap"
SWAP_SIZE_MB=1024  # 1GB swap

# Handle legacy /var/swap file (move it aside if it exists as a file)
if [ -f "/var/swap" ] && [ ! -d "/var/swap" ]; then
  echo "  Moving legacy /var/swap file to /var/swap.old..."
  mv /var/swap /var/swap.old
fi

if [ ! -f "$SWAP_FILE" ]; then
  # Create swap directory if it doesn't exist
  if [ ! -d "$SWAP_DIR" ]; then
    mkdir -p "$SWAP_DIR"
  fi

  # Create swapfile using fallocate (faster than dd)
  echo "  Creating 1GB swapfile at $SWAP_FILE..."
  fallocate -l ${SWAP_SIZE_MB}M "$SWAP_FILE" || {
    # Fallback to dd if fallocate fails
    echo "  fallocate failed, using dd instead..."
    dd if=/dev/zero of="$SWAP_FILE" bs=1M count=$SWAP_SIZE_MB status=progress
  }

  # Secure permissions and format as swap
  chmod 600 "$SWAP_FILE"
  mkswap "$SWAP_FILE"

  echo "  ✓ Swapfile created successfully"

  # Add to /etc/fstab for automatic mounting on boot
  if ! grep -q "$SWAP_FILE" /etc/fstab 2>/dev/null; then
    echo "  Adding swap to /etc/fstab for persistent mounting..."
    echo "$SWAP_FILE none swap sw 0 0" >> /etc/fstab
    systemctl daemon-reload
    echo "  ✓ Swap will be enabled automatically on boot"
  fi

  # Enable swap now
  swapon "$SWAP_FILE" 2>/dev/null || echo "  Note: Swap enabled, will activate on reboot"

  # Clean up temporary swapfile from optimize_memory_for_setup if it exists
  if [ -f "/swapfile" ] && [ "$SWAP_FILE" != "/swapfile" ]; then
    echo "  Cleaning up temporary /swapfile..."
    swapoff /swapfile 2>/dev/null || true
    rm -f /swapfile
    echo "  ✓ Temporary swapfile removed"
  fi

else
  echo "  Swapfile already exists at $SWAP_FILE"

  # Ensure it's in fstab even if file exists
  if ! grep -q "$SWAP_FILE" /etc/fstab 2>/dev/null; then
    echo "  Adding existing swap to /etc/fstab..."
    echo "$SWAP_FILE none swap sw 0 0" >> /etc/fstab
    systemctl daemon-reload
    echo "  ✓ Swap will be enabled automatically on boot"
  fi

  # Clean up temporary swapfile from optimize_memory_for_setup if it exists
  if [ -f "/swapfile" ] && [ "$SWAP_FILE" != "/swapfile" ]; then
    echo "  Cleaning up temporary /swapfile..."
    swapoff /swapfile 2>/dev/null || true
    rm -f /swapfile
    echo "  ✓ Temporary swapfile removed"
  fi

  # Enable swap if not already active
  if ! swapon --show 2>/dev/null | grep -q "$SWAP_FILE"; then
    echo "  Enabling swap..."
    swapon "$SWAP_FILE" 2>/dev/null || true
  fi
fi

# ===== Disable Unnecessary Desktop Services (Save ~30MB RAM) =====
echo
echo "Disabling unnecessary desktop services to save memory..."

# Stop and mask audio/color management services (not needed for headless USB gadget)
DESKTOP_SERVICES=("pipewire" "wireplumber" "pipewire-pulse" "colord")
for service in "${DESKTOP_SERVICES[@]}"; do
  if systemctl is-active "$service" >/dev/null 2>&1 || systemctl is-enabled "$service" >/dev/null 2>&1; then
    echo "  Stopping and masking $service..."
    systemctl stop "$service" 2>/dev/null || true
    systemctl mask "$service" 2>/dev/null || true
  fi
done

echo "  ✓ Desktop services disabled (saves ~30MB RAM)"

# ===== Create TeslaCam folder on TeslaCam partition =====
echo
echo "Setting up TeslaCam folder on TeslaCam partition..."
TEMP_MOUNT="/tmp/teslacam_setup_$$"
mkdir -p "$TEMP_MOUNT"

# Mount TeslaCam partition temporarily
LOOP_SETUP=$(losetup -f)
losetup "$LOOP_SETUP" "$IMG_CAM_PATH"

# Detect filesystem type
FS_TYPE=$(blkid -o value -s TYPE "$LOOP_SETUP" 2>/dev/null || echo "vfat")
if [ "$FS_TYPE" = "exfat" ]; then
  mount -t exfat "$LOOP_SETUP" "$TEMP_MOUNT"
else
  mount -t vfat "$LOOP_SETUP" "$TEMP_MOUNT"
fi

# Create TeslaCam directory if it doesn't exist
if [ ! -d "$TEMP_MOUNT/TeslaCam" ]; then
  echo "  Creating TeslaCam folder..."
  mkdir -p "$TEMP_MOUNT/TeslaCam"
else
  echo "  TeslaCam folder already exists"
fi

# Sync and unmount
sync
umount "$TEMP_MOUNT"
losetup -d "$LOOP_SETUP"
rmdir "$TEMP_MOUNT"
echo "TeslaCam folder setup complete."

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
echo " - present script: $GADGET_DIR/scripts/present_usb.sh"
echo " - edit script:    $GADGET_DIR/scripts/edit_usb.sh"
echo " - web UI:         http://<pi_ip>:$WEB_PORT/  (service: gadget_web.service)"
echo " - gadget auto-present on boot: present_usb_on_boot.service (with optional cleanup)"
echo "Samba shares: use user '$TARGET_USER' and the password set in SAMBA_PASS"
echo
echo "System Reliability Features Enabled:"
echo " - Hardware watchdog: Auto-reboot on system hang (watchdog.service)"
echo " - Service auto-restart: All services restart on failure"
echo " - Memory limits: Services limited to prevent OOM crashes"
echo " - Kernel panic auto-reboot: 10 second timeout"
echo " - WiFi auto-reconnect: Active monitoring (wifi-monitor.service)"
echo " - WiFi power-save disabled: Prevents sleep-related disconnects"
echo

# Load required kernel modules before presenting USB gadget
echo "Loading USB gadget kernel modules..."
modprobe configfs 2>/dev/null || true
modprobe libcomposite 2>/dev/null || true

# Try to load dwc2 - this might fail on first install if config.txt was just updated
if ! modprobe dwc2 2>/dev/null; then
    echo "Warning: dwc2 module not available yet"
fi

# Ensure configfs is mounted
if ! mountpoint -q /sys/kernel/config 2>/dev/null; then
    echo "Mounting configfs..."
    mount -t configfs none /sys/kernel/config 2>/dev/null || true
fi

# Check if UDC is available (indicates dwc2 is working)
if [ ! -d /sys/class/udc ] || [ -z "$(ls -A /sys/class/udc 2>/dev/null)" ]; then
    echo ""
    echo "============================================"
    echo "⚠️  REBOOT REQUIRED"
    echo "============================================"
    echo "The USB gadget hardware (dwc2) is not available yet."
    echo ""
    if [ "$CONFIG_CHANGED" = "1" ]; then
        echo "Reason: config.txt was just modified with USB gadget settings."
        echo ""
    fi
    echo "Next steps:"
    echo "  1. Reboot the Raspberry Pi:  sudo reboot"
    echo "  2. After reboot, the USB gadget will be automatically enabled"
    echo "  3. Hardware watchdog will activate for system protection"
    echo "  4. Access the web interface at: http://$(hostname -I | awk '{print $1}'):$WEB_PORT/"
    echo ""
    echo "The system is configured and ready, but requires a reboot to activate"
    echo "the USB gadget hardware support and hardware watchdog."
    echo "============================================"
    exit 0
fi

echo "USB gadget hardware detected. Switching to present mode..."
"$GADGET_DIR/scripts/present_usb.sh"
echo
echo "Setup complete! The Pi is now in present mode."
