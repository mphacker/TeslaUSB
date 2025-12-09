#!/bin/bash
#
# TeslaUSB Shell Configuration
#
# EDIT THIS FILE to match your installation.
# This file is sourced by present_usb.sh, edit_usb.sh, and other shell scripts.
#

# Installation directory where TeslaUSB is installed
GADGET_DIR="/home/pi/TeslaUSB"

# Linux user running the TeslaUSB services
TARGET_USER="pi"

# Disk image filenames
IMG_CAM_NAME="usb_cam.img"
IMG_LIGHTSHOW_NAME="usb_lightshow.img"

# Mount directory for USB partitions
MNT_DIR="/mnt/gadget"

# Offline fallback access point (for in-car/mobile access when STA WiFi is down)
OFFLINE_AP_ENABLED="true"             # Set to "false" to disable fallback AP
OFFLINE_AP_INTERFACE="wlan0"          # WiFi interface used for AP/STA
OFFLINE_AP_SSID="TeslaUSB"            # SSID broadcast when AP is active
OFFLINE_AP_PASSPHRASE="teslausb1234"  # WPA2 passphrase 8-63 chars
OFFLINE_AP_CHANNEL="6"                # 2.4GHz channel
OFFLINE_AP_IPV4_CIDR="192.168.4.1/24" # Static IP for AP interface
OFFLINE_AP_DHCP_START="192.168.4.10"  # DHCP range start
OFFLINE_AP_DHCP_END="192.168.4.50"    # DHCP range end
OFFLINE_AP_CHECK_INTERVAL="20"       # Seconds between health checks
OFFLINE_AP_DISCONNECT_GRACE="30"     # Seconds offline before starting AP
OFFLINE_AP_MIN_RSSI="-70"            # Minimum RSSI (dBm) to tear down AP
OFFLINE_AP_STABLE_SECONDS="20"       # Seconds of good link before stopping AP
OFFLINE_AP_PING_TARGET="8.8.8.8"     # Ping target to confirm WAN reachability
OFFLINE_AP_RETRY_SECONDS="300"       # While AP is active, retry STA join every N seconds
OFFLINE_AP_VIRTUAL_IF="uap0"         # Virtual AP interface name (concurrent mode always enabled)

# ============================================================================
# Computed paths (don't modify these)
# ============================================================================
IMG_CAM="$GADGET_DIR/$IMG_CAM_NAME"
IMG_LIGHTSHOW="$GADGET_DIR/$IMG_LIGHTSHOW_NAME"
STATE_FILE="$GADGET_DIR/state.txt"
