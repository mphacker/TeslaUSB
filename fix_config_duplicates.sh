#!/bin/bash
# Fix duplicate entries in /boot/firmware/config.txt
# This script removes duplicate dtoverlay=dwc2 and dtparam=watchdog=on lines

set -euo pipefail

CONFIG_FILE="/boot/firmware/config.txt"
BACKUP_FILE="${CONFIG_FILE}.backup.$(date +%Y%m%d_%H%M%S)"

echo "Fixing duplicate entries in $CONFIG_FILE"

# Create backup
echo "Creating backup: $BACKUP_FILE"
sudo cp "$CONFIG_FILE" "$BACKUP_FILE"

# Create temporary file
TEMP_FILE=$(mktemp)

# Process config.txt: keep only first occurrence of each line in [all] section
sudo awk '
BEGIN { in_all = 0 }
/^\[all\]/ { 
  in_all = 1
  print
  next
}
/^\[/ { 
  in_all = 0
  print
  next
}
{
  if (in_all) {
    # In [all] section - track what we have seen
    if ($0 == "dtoverlay=dwc2") {
      if (!seen_dwc2) {
        print
        seen_dwc2 = 1
      }
    } else if ($0 == "dtparam=watchdog=on") {
      if (!seen_watchdog) {
        print
        seen_watchdog = 1
      }
    } else {
      # Other lines in [all] section - pass through
      print
    }
  } else {
    # Not in [all] section - pass through
    print
  }
}
' "$CONFIG_FILE" > "$TEMP_FILE"

# Replace original file
sudo mv "$TEMP_FILE" "$CONFIG_FILE"

echo "Duplicates removed. Backup saved at $BACKUP_FILE"
echo ""
echo "Current [all] section:"
grep -A10 '^\[all\]' "$CONFIG_FILE" | head -11
