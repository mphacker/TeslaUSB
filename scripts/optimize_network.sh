#!/usr/bin/env bash
# Network and WiFi Performance Optimization Script
# Run this to apply network tuning without full setup
# Also called by network-optimizations.service at boot for persistence

set -euo pipefail

# Check if running as root
if [ "$EUID" -ne 0 ]; then
  echo "This script must be run as root (sudo)"
  exit 1
fi

echo "Applying network performance optimizations..."

# Apply sysctl network settings
if [ -f /etc/sysctl.d/99-teslausb.conf ]; then
  echo "  Applying sysctl network tuning..."
  sysctl -p /etc/sysctl.d/99-teslausb.conf >/dev/null 2>&1 || true
fi

# Set CPU governor to performance mode (max freq for faster I/O processing)
echo "  Setting CPU governor to performance..."
for cpu in /sys/devices/system/cpu/cpu[0-9]*; do
  if [ -f "$cpu/cpufreq/scaling_governor" ]; then
    echo performance > "$cpu/cpufreq/scaling_governor" 2>/dev/null || true
  fi
done

# Wait for wlan0 to be ready (important at boot)
for i in $(seq 1 30); do
  if [ -d /sys/class/net/wlan0 ]; then
    break
  fi
  sleep 1
done

# WiFi interface optimizations
if [ -d /sys/class/net/wlan0 ]; then
  echo "  Optimizing WiFi interface (wlan0)..."

  # Disable WiFi power management (prevents sleep-related disconnects)
  iwconfig wlan0 power off 2>/dev/null || true

  # Increase TX queue length (reduces packet drops under load)
  ip link set wlan0 txqueuelen 2000 2>/dev/null || true

  # Enable RTS threshold for weak signal environments
  # RTS/CTS handshake reduces collisions but adds overhead
  # 500 bytes is a good balance - small packets skip RTS, large use it
  iwconfig wlan0 rts 500 2>/dev/null || true

  echo "  WiFi optimization complete"
else
  echo "  WiFi interface not found, skipping WiFi-specific optimizations"
fi

# Increase disk read-ahead for better video streaming performance
if [ -f /sys/block/mmcblk0/queue/read_ahead_kb ]; then
  echo "  Setting disk read-ahead to 2048KB..."
  echo 2048 > /sys/block/mmcblk0/queue/read_ahead_kb 2>/dev/null || true
fi

# Additional TCP optimizations (supplement sysctl.d settings)
echo "  Applying additional TCP settings..."
sysctl -w net.ipv4.tcp_sack=1 > /dev/null 2>&1 || true
sysctl -w net.ipv4.tcp_timestamps=1 > /dev/null 2>&1 || true
sysctl -w net.ipv4.tcp_no_metrics_save=1 > /dev/null 2>&1 || true
sysctl -w net.core.netdev_budget=600 > /dev/null 2>&1 || true
sysctl -w net.core.netdev_budget_usecs=8000 > /dev/null 2>&1 || true

# Set WiFi regulatory domain for maximum TX power (US = 30dBm)
echo "  Setting WiFi regulatory domain..."
if [ ! -f /etc/default/crda ]; then
  echo 'REGDOMAIN=US' > /etc/default/crda
fi
iw reg set US 2>/dev/null || true

# Log completion for systemd service
if [ -n "${INVOCATION_ID:-}" ]; then
  # Running under systemd
  logger -t network-opt "Network optimizations applied: CPU=performance, TX=2000, readahead=2048KB, RTS=500"
fi

echo ""
echo "Network optimization complete!"
echo "  CPU Governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo 'N/A')"
echo "  CPU Freq: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null || echo 'N/A') kHz"
echo "  TX Queue: $(cat /sys/class/net/wlan0/tx_queue_len 2>/dev/null || echo 'N/A')"
echo "  Read-ahead: $(cat /sys/block/mmcblk0/queue/read_ahead_kb 2>/dev/null || echo 'N/A') KB"
