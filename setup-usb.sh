#!/usr/bin/env bash
set -euo pipefail

# ================= Configuration =================
GADGET_DIR_DEFAULT="/home/mhacker/gadget"
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

# Install prerequisites
apt update
apt install -y parted dosfstools util-linux python3-flask samba samba-common-bin

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

# ===== present_usb.sh =====
cat > "$GADGET_DIR/present_usb.sh" <<SH
#!/bin/bash
set -euo pipefail
IMG="$GADGET_DIR/$IMG_NAME"
MNT_DIR="$MNT_DIR"
# Stop Samba
sudo systemctl stop smbd || true
# Unmount partitions if mounted
for mp in "\$MNT_DIR/part1" "\$MNT_DIR/part2"; do
  if mountpoint -q "\$mp"; then sudo umount "\$mp"; fi
done
# Detach loop devices for the image
for loop in \$(losetup -j "\$IMG" | cut -d: -f1); do
  sudo losetup -d "\$loop" || true
done
# Remove gadget module if present
if lsmod | grep -q '^g_mass_storage'; then sudo rmmod g_mass_storage || true; sleep 1; fi
# Present gadget
sudo modprobe g_mass_storage file="\$IMG" stall=0 removable=1 ro=0
echo "Presented USB gadget."
SH

# ===== edit_usb.sh =====
cat > "$GADGET_DIR/edit_usb.sh" <<SH
#!/bin/bash
set -euo pipefail
GADGET_DIR="$GADGET_DIR"
IMG="\$GADGET_DIR/$IMG_NAME"
MNT_DIR="$MNT_DIR"
TARGET_USER="$TARGET_USER"
UID_VAL=\$(id -u "$TARGET_USER")
GID_VAL=\$(id -g "$TARGET_USER")
# Remove gadget if active
if lsmod | grep -q '^g_mass_storage'; then
  sudo rmmod g_mass_storage || true
  sleep 1
fi
# Prepare mount points
sudo mkdir -p "\$MNT_DIR/part1" "\$MNT_DIR/part2"
sudo chown "$TARGET_USER:$TARGET_USER" "\$MNT_DIR/part1" "\$MNT_DIR/part2"
# Setup loop device
LOOP=\$(sudo losetup --show -fP "\$IMG")
sleep 0.5
# Mount partitions
for PART_NUM in 1 2; do
  LOOP_PART="\${LOOP}p\${PART_NUM}"
  MP="\$MNT_DIR/part\${PART_NUM}"
  if mountpoint -q "\$MP"; then sudo umount "\$MP"; fi
  sudo mount -o uid=\$UID_VAL,gid=\$GID_VAL,umask=002 "\$LOOP_PART" "\$MP"
done
# Start Samba
sudo systemctl restart smbd || true
echo "Partitions mounted and Samba started."
SH

chmod +x "$GADGET_DIR/present_usb.sh" "$GADGET_DIR/edit_usb.sh"
chown -R "$TARGET_USER:$TARGET_USER" "$GADGET_DIR"

# ===== Web UI =====
WEB_FILE="$GADGET_DIR/web_control.py"
cat > "$WEB_FILE" <<PY
#!/usr/bin/env python3
from flask import Flask, render_template_string, redirect, url_for, flash
import subprocess
app = Flask(__name__)
app.secret_key = "localdevsecret"
GADGET_DIR="$GADGET_DIR"
HTML_TEMPLATE = """
<!doctype html><html><head><meta charset='utf-8'><title>USB Gadget</title>
<style>body{font-family:sans-serif;padding:20px}button{padding:12px 20px;margin:10px}</style>
</head><body>
<h1>USB Gadget Control</h1>
{% with messages = get_flashed_messages(with_categories=true) %}
  {% if messages %}
    <ul>{% for cat,msg in messages %}<li class="{{cat}}">{{msg}}</li>{% endfor %}</ul>
  {% endif %}
{% endwith %}
<form method="post" action="{{url_for('present_usb')}}"><button type="submit">Present USB Gadget</button></form>
<form method="post" action="{{url_for('edit_usb')}}"><button type="submit">Edit USB (mount + Samba)</button></form>
</body></html>
"""
def run_script(name):
    path = f"{GADGET_DIR}/{name}"
    try:
        subprocess.run(["sudo", path], check=True)
        return True, f"{name} executed"
    except subprocess.CalledProcessError as e:
        return False, str(e)
@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)
@app.route("/present_usb", methods=["POST"])
def present_usb():
    ok,msg = run_script("present_usb.sh")
    flash(msg, "success" if ok else "error")
    return redirect(url_for("index"))
@app.route("/edit_usb", methods=["POST"])
def edit_usb():
    ok,msg = run_script("edit_usb.sh")
    flash(msg, "success" if ok else "error")
    return redirect(url_for("index"))
if __name__=="__main__":
    app.run(host="0.0.0.0", port=$WEB_PORT)
PY

chown "$TARGET_USER:$TARGET_USER" "$WEB_FILE"
chmod +x "$WEB_FILE"

# ===== Systemd service for web UI =====
SERVICE_FILE="/etc/systemd/system/gadget_web.service"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=USB Gadget Web Control
After=network.target

[Service]
Type=simple
User=$TARGET_USER
ExecStart=/usr/bin/python3 $WEB_FILE
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now gadget_web.service || systemctl restart gadget_web.service

# ===== Systemd service to present gadget at boot =====
AUTO_SERVICE="/etc/systemd/system/present_usb_on_boot.service"
cat > "$AUTO_SERVICE" <<EOF
[Unit]
Description=Present USB gadget at boot
After=multi-user.target
Wants=multi-user.target

[Service]
Type=oneshot
ExecStart=$GADGET_DIR/present_usb.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable present_usb_on_boot.service || true

echo
echo "Installation complete."
echo " - present script: $GADGET_DIR/present_usb.sh"
echo " - edit script:    $GADGET_DIR/edit_usb.sh"
echo " - web UI:         http://<pi_ip>:$WEB_PORT/  (service: gadget_web.service)"
echo " - gadget auto-present on boot: present_usb_on_boot.service (enabled)"
echo "Samba shares: use user '$TARGET_USER' and the password set in SAMBA_PASS"
