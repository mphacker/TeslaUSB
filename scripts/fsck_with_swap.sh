#!/usr/bin/env bash
# Helper script to run fsck with swap support
# Usage: fsck_with_swap.sh <device> <filesystem_type> [mode]
# Modes: quick (read-only check), repair (auto-repair)

set -euo pipefail

DEVICE="$1"
FS_TYPE="$2"
MODE="${3:-quick}"
SWAP_FILE="/var/swap/fsck.swap"
LOG_FILE="/var/log/teslausb/fsck_$(basename "$DEVICE").log"

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_FILE")"

echo "Running fsck on $DEVICE (type: $FS_TYPE, mode: $MODE)"

# Enable swap if it exists and isn't already enabled
SWAP_ENABLED=0
if [ -f "$SWAP_FILE" ]; then
  if ! swapon --show | grep -q "$SWAP_FILE"; then
    echo "Enabling swap for fsck operation..."
    swapon "$SWAP_FILE" 2>/dev/null || {
      echo "Warning: Could not enable swap (may already be active)"
    }
    SWAP_ENABLED=1
  fi
fi

# Cleanup function
cleanup() {
  if [ $SWAP_ENABLED -eq 1 ]; then
    echo "Disabling swap..."
    swapoff "$SWAP_FILE" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# Run appropriate fsck based on filesystem type and mode
FSCK_STATUS=0
# Larger images (427G) need longer than 60s; allow 300s for quick, 600s for repair
QUICK_TIMEOUT=300
REPAIR_TIMEOUT=600

case "$FS_TYPE" in
  vfat)
    if [ "$MODE" = "repair" ]; then
      timeout "$REPAIR_TIMEOUT" fsck.vfat -a "$DEVICE" >"$LOG_FILE" 2>&1 || FSCK_STATUS=$?
    else
      timeout "$QUICK_TIMEOUT" fsck.vfat -n "$DEVICE" >"$LOG_FILE" 2>&1 || FSCK_STATUS=$?
    fi
    ;;
  exfat)
    if [ "$MODE" = "repair" ]; then
      timeout "$REPAIR_TIMEOUT" fsck.exfat -p "$DEVICE" >"$LOG_FILE" 2>&1 || FSCK_STATUS=$?
    else
      # Quick read-only check
      timeout "$QUICK_TIMEOUT" fsck.exfat -n "$DEVICE" >"$LOG_FILE" 2>&1 || FSCK_STATUS=$?
    fi
    ;;
  *)
    echo "Error: Unsupported filesystem type: $FS_TYPE" >&2
    exit 1
    ;;
esac

# Interpret fsck exit codes
case $FSCK_STATUS in
  0)
    echo "✓ Filesystem check passed - no errors found"
    rm -f "$LOG_FILE"
    exit 0
    ;;
  1)
    echo "✓ Filesystem errors corrected successfully"
    echo "   Details: $LOG_FILE"
    exit 0
    ;;
  2)
    echo "⚠ Filesystem corrected - system should be rebooted"
    echo "   Details: $LOG_FILE"
    exit 0
    ;;
  4)
    echo "✗ Filesystem errors left uncorrected"
    echo "   Details: $LOG_FILE"
    exit 4
    ;;
  8)
    echo "✗ Operational error during fsck"
    echo "   Details: $LOG_FILE"
    exit 8
    ;;
  124)
    echo "⚠ Filesystem check timed out"
    echo "   Details: $LOG_FILE"
    exit 124
    ;;
  *)
    echo "✗ Unknown fsck exit code: $FSCK_STATUS"
    echo "   Details: $LOG_FILE"
    exit $FSCK_STATUS
    ;;
esac
