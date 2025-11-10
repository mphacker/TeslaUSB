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

# ============================================================================
# Computed paths (don't modify these)
# ============================================================================
IMG_CAM="$GADGET_DIR/$IMG_CAM_NAME"
IMG_LIGHTSHOW="$GADGET_DIR/$IMG_LIGHTSHOW_NAME"
STATE_FILE="$GADGET_DIR/state.txt"
