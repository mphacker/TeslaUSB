#!/usr/bin/env bash
# Network and WiFi Performance Optimization Script
# Run this to apply network tuning without full setup

set -euo pipefail

echo "Applying network performance optimizations..."

# Apply sysctl network settings
if [ -f /etc/sysctl.d/99-teslausb.conf ]; then
  echo "  Applying sysctl network tuning..."
  sudo sysctl -p /etc/sysctl.d/99-teslausb.conf >/dev/null 2>&1 || true
fi

# Set CPU governor to performance mode
echo "  Setting CPU governor to performance..."
for cpu in /sys/devices/system/cpu/cpu[0-9]*; do
  if [ -f "$cpu/cpufreq/scaling_governor" ]; then
    echo performance | sudo tee "$cpu/cpufreq/scaling_governor" >/dev/null 2>&1 || true
  fi
done

# WiFi interface optimizations
if [ -d /sys/class/net/wlan0 ]; then
  echo "  Optimizing WiFi interface (wlan0)..."

  # Increase TX queue length
  sudo ip link set wlan0 txqueuelen 2000 2>/dev/null || true

  # Set WiFi fragmentation threshold (reduces overhead for large packets)
  sudo iwconfig wlan0 frag 2346 2>/dev/null || true

  # Disable RTS/CTS (reduces overhead on good signal)
  sudo iwconfig wlan0 rts off 2>/dev/null || true

  echo "  WiFi optimization complete"
else
  echo "  WiFi interface not found, skipping WiFi-specific optimizations"
fi

# Display current network stats
echo ""
echo "Current WiFi status:"
cat /proc/net/wireless 2>/dev/null || echo "  WiFi stats not available"

echo ""
echo "Network optimization complete!"
echo "Recommendations:"
echo "  - Move Pi closer to WiFi router if signal is weak (< -65 dBm)"

