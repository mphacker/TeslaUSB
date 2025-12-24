#!/bin/bash
# Network Performance Test Script for TeslaUSB
# Run this to measure WiFi/network performance metrics

# Don't use set -e so we can handle errors gracefully
set -uo pipefail

echo "========================================"
echo "TeslaUSB Network Performance Test"
echo "========================================"
echo "Date: $(date)"
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test results file
RESULTS_FILE="/tmp/network_perf_$(date +%Y%m%d_%H%M%S).txt"

log_result() {
    echo "$1" | tee -a "$RESULTS_FILE"
}

# Helper function for safe division (avoids bc dependency)
safe_divide() {
    local num=$1
    local denom=$2
    if [ -n "$num" ] && [ -n "$denom" ] && [ "$denom" != "0" ]; then
        awk "BEGIN {printf \"%.2f\", $num / $denom}"
    else
        echo "N/A"
    fi
}

log_result "========================================"
log_result "NETWORK PERFORMANCE TEST RESULTS"
log_result "========================================"
log_result "Timestamp: $(date)"
log_result ""

# 1. System Info
log_result "--- SYSTEM INFO ---"
log_result "Hostname: $(hostname)"
log_result "Kernel: $(uname -r)"
log_result "CPU Governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo 'N/A')"
log_result "CPU Freq: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null || echo 'N/A') kHz"
log_result "Memory Free: $(free -m | awk '/Mem:/ {print $4}') MB"
log_result "Swap Used: $(free -m | awk '/Swap:/ {print $3}') MB"
log_result ""

# 2. WiFi Signal Info
log_result "--- WIFI SIGNAL INFO ---"
WIFI_INFO=$(cat /proc/net/wireless 2>/dev/null | tail -1)
if [ -n "$WIFI_INFO" ]; then
    LINK_QUALITY=$(echo "$WIFI_INFO" | awk '{print $3}' | tr -d '.')
    SIGNAL_LEVEL=$(echo "$WIFI_INFO" | awk '{print $4}' | tr -d '.')
    NOISE_LEVEL=$(echo "$WIFI_INFO" | awk '{print $5}' | tr -d '.')
    RETRIES=$(echo "$WIFI_INFO" | awk '{print $9}')
    log_result "Link Quality: $LINK_QUALITY"
    log_result "Signal Level: $SIGNAL_LEVEL dBm"
    log_result "Noise Level: $NOISE_LEVEL dBm"
    log_result "TX Retries: $RETRIES"
fi

# Get bit rate
BIT_RATE=$(/sbin/iwconfig wlan0 2>/dev/null | grep "Bit Rate" | awk -F'=' '{print $2}' | awk '{print $1, $2}')
log_result "Bit Rate: $BIT_RATE"

# TX Power
TX_POWER=$(/sbin/iwconfig wlan0 2>/dev/null | grep "Tx-Power" | awk -F'Tx-Power=' '{print $2}' | awk '{print $1, $2}')
log_result "TX Power: $TX_POWER"

# Power Management
POWER_MGMT=$(/sbin/iwconfig wlan0 2>/dev/null | grep "Power Management" | awk -F':' '{print $2}')
log_result "Power Management: $POWER_MGMT"
log_result ""

# 3. Network Configuration
log_result "--- NETWORK CONFIG ---"
log_result "TX Queue Length: $(cat /sys/class/net/wlan0/tx_queue_len 2>/dev/null || echo 'N/A')"
log_result "MTU: $(cat /sys/class/net/wlan0/mtu 2>/dev/null || echo 'N/A')"
log_result "TCP Congestion: $(cat /proc/sys/net/ipv4/tcp_congestion_control 2>/dev/null || echo 'N/A')"
log_result "Read-ahead KB: $(cat /sys/block/mmcblk0/queue/read_ahead_kb 2>/dev/null || echo 'N/A')"
log_result ""

# 4. Latency Tests
log_result "--- LATENCY TESTS ---"

# Ping to gateway
GATEWAY=$(ip route | grep default | awk '{print $3}' | head -1)
if [ -n "$GATEWAY" ]; then
    log_result "Gateway: $GATEWAY"
    PING_RESULT=$(ping -c 10 -q "$GATEWAY" 2>/dev/null | tail -2)
    log_result "Gateway Ping Stats:"
    log_result "$PING_RESULT"
fi
log_result ""

# 5. Throughput Tests
log_result "--- THROUGHPUT TESTS ---"

# Test 1: Small file (simulates API responses)
log_result "Test 1: Small download (100KB)..."
SMALL_SPEED=$(curl -o /dev/null -w '%{speed_download}' --max-time 30 -s "http://speedtest.tele2.net/100KB.zip" 2>/dev/null || echo "0")
SMALL_SPEED_KB=$(safe_divide "$SMALL_SPEED" 1024)
log_result "  100KB download: ${SMALL_SPEED_KB} KB/s"

# Test 2: Medium file (simulates thumbnail batch)
log_result "Test 2: Medium download (1MB)..."
MED_SPEED=$(curl -o /dev/null -w '%{speed_download}' --max-time 60 -s "http://speedtest.tele2.net/1MB.zip" 2>/dev/null || echo "0")
MED_SPEED_KB=$(safe_divide "$MED_SPEED" 1024)
log_result "  1MB download: ${MED_SPEED_KB} KB/s"

# Test 3: Large file (simulates video download)
log_result "Test 3: Large download (10MB)..."
START_TIME=$(date +%s)
LARGE_SPEED=$(curl -o /dev/null -w '%{speed_download}' --max-time 120 -s "http://speedtest.tele2.net/10MB.zip" 2>/dev/null || echo "0")
END_TIME=$(date +%s)
LARGE_SPEED_KB=$(safe_divide "$LARGE_SPEED" 1024)
DURATION=$((END_TIME - START_TIME))
log_result "  10MB download: ${LARGE_SPEED_KB} KB/s (${DURATION}s)"
log_result ""

# 6. Local Video Streaming Test (if web server is running)
log_result "--- LOCAL VIDEO STREAMING TEST ---"
if pgrep -f "web_control.py" > /dev/null 2>&1; then
    # Find a video file to test with
    VIDEO_PATH=$(find /mnt/gadget -name "*.mp4" -type f 2>/dev/null | head -1)
    if [ -n "$VIDEO_PATH" ] && [ -f "$VIDEO_PATH" ]; then
        VIDEO_SIZE=$(stat -c%s "$VIDEO_PATH" 2>/dev/null || echo "0")
        VIDEO_SIZE_MB=$(safe_divide "$VIDEO_SIZE" 1048576)
        log_result "Test video: $VIDEO_PATH (${VIDEO_SIZE_MB} MB)"

        # Test local streaming throughput
        log_result "Testing local file read speed..."
        READ_START=$(date +%s)
        dd if="$VIDEO_PATH" of=/dev/null bs=256K count=40 2>&1 | tail -1 || true
        READ_END=$(date +%s)
        READ_DURATION=$((READ_END - READ_START))
        if [ "$READ_DURATION" -gt 0 ]; then
            READ_SPEED=$(awk "BEGIN {printf \"%.2f\", (40 * 256) / $READ_DURATION / 1024}")
            log_result "  Local read speed: ~${READ_SPEED} MB/s (10MB in ${READ_DURATION}s)"
        else
            log_result "  Local read speed: Very fast (<1s)"
        fi
    else
        log_result "No video files found for streaming test"
    fi
else
    log_result "Web server not running, skipping local streaming test"
fi
log_result ""

# 7. Disk I/O Performance
log_result "--- DISK I/O PERFORMANCE ---"
log_result "Testing sequential read from SD card..."
DISK_READ=$(dd if=/dev/mmcblk0 of=/dev/null bs=4M count=25 2>&1 | grep -oE '[0-9.]+ [MG]B/s' || echo "N/A")
log_result "  Sequential read: $DISK_READ"
log_result ""

# 8. TCP Buffer Settings
log_result "--- TCP BUFFER SETTINGS ---"
log_result "tcp_rmem: $(cat /proc/sys/net/ipv4/tcp_rmem 2>/dev/null)"
log_result "tcp_wmem: $(cat /proc/sys/net/ipv4/tcp_wmem 2>/dev/null)"
log_result "rmem_max: $(cat /proc/sys/net/core/rmem_max 2>/dev/null)"
log_result "wmem_max: $(cat /proc/sys/net/core/wmem_max 2>/dev/null)"
log_result ""

# 9. WiFi Retry Stats (capture delta)
log_result "--- WIFI RETRY DELTA TEST ---"
RETRIES_START=$(cat /proc/net/wireless 2>/dev/null | tail -1 | awk '{print $9}')
log_result "Retries at start: $RETRIES_START"
# Do a quick transfer during a 10 second window
curl -o /dev/null -s --max-time 8 "http://speedtest.tele2.net/1MB.zip" 2>/dev/null || true
sleep 2
RETRIES_END=$(cat /proc/net/wireless 2>/dev/null | tail -1 | awk '{print $9}')
log_result "Retries after 10s activity: $RETRIES_END"
if [ -n "$RETRIES_START" ] && [ -n "$RETRIES_END" ]; then
    RETRY_DELTA=$((RETRIES_END - RETRIES_START))
    log_result "Retry delta: $RETRY_DELTA (lower is better)"
else
    RETRY_DELTA="N/A"
    log_result "Retry delta: N/A"
fi
log_result ""

# Summary
log_result "========================================"
log_result "PERFORMANCE SUMMARY"
log_result "========================================"
log_result "WiFi Signal: $SIGNAL_LEVEL dBm"
log_result "Bit Rate: $BIT_RATE"
log_result "Small File: ${SMALL_SPEED_KB} KB/s"
log_result "Medium File: ${MED_SPEED_KB} KB/s"
log_result "Large File: ${LARGE_SPEED_KB} KB/s"
log_result "TX Retries (10s): $RETRY_DELTA"
log_result "CPU Governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)"
log_result "========================================"
log_result ""
log_result "Results saved to: $RESULTS_FILE"

echo ""
echo -e "${GREEN}Test complete! Results saved to: $RESULTS_FILE${NC}"
