#!/bin/bash
set -euo pipefail

# WiFi Connection Monitor
# Monitors WiFi connection and attempts reconnection if connection is lost
# Designed to run as a systemd service

LOCK_FILE="/var/run/wifi-monitor.lock"
LOG_TAG="wifi-monitor"
PING_TARGET="8.8.8.8"  # Google DNS - reliable target
PING_TIMEOUT=3
MAX_FAILURES=3
CHECK_INTERVAL=60  # Check every 60 seconds
FAILURE_COUNT=0

# Prevent multiple instances
if [ -f "$LOCK_FILE" ]; then
    logger -t "$LOG_TAG" "Another instance is running, exiting"
    exit 0
fi
touch "$LOCK_FILE"
trap "rm -f $LOCK_FILE" EXIT INT TERM

# Function to check WiFi connection
check_wifi() {
    # First check: Does wlan0 have an IP address?
    if ! ip addr show wlan0 2>/dev/null | grep -q "inet "; then
        logger -t "$LOG_TAG" "wlan0 has no IP address"
        return 1
    fi
    
    # Second check: Can we ping a reliable external host?
    if ping -c 1 -W "$PING_TIMEOUT" "$PING_TARGET" >/dev/null 2>&1; then
        return 0
    else
        logger -t "$LOG_TAG" "Ping to $PING_TARGET failed"
        return 1
    fi
}

# Function to restart WiFi interface
restart_wifi_interface() {
    logger -t "$LOG_TAG" "Attempting to restart WiFi interface..."
    
    # Bring interface down
    if ip link set wlan0 down 2>/dev/null; then
        sleep 2
        
        # Bring interface up
        if ip link set wlan0 up 2>/dev/null; then
            sleep 5
            logger -t "$LOG_TAG" "WiFi interface restarted"
            return 0
        fi
    fi
    
    logger -t "$LOG_TAG" "Failed to restart WiFi interface"
    return 1
}

# Function to restart networking service
restart_networking() {
    logger -t "$LOG_TAG" "Attempting to restart networking service..."
    
    # Try NetworkManager first (most common on Raspberry Pi OS Desktop)
    if systemctl is-active --quiet NetworkManager; then
        if systemctl restart NetworkManager 2>/dev/null; then
            sleep 10
            logger -t "$LOG_TAG" "NetworkManager restarted"
            return 0
        fi
    fi
    
    # Fallback to dhcpcd (used on Raspberry Pi OS Lite)
    if systemctl is-active --quiet dhcpcd; then
        if systemctl restart dhcpcd 2>/dev/null; then
            sleep 10
            logger -t "$LOG_TAG" "dhcpcd restarted"
            return 0
        fi
    fi
    
    # Fallback to wpa_supplicant
    if systemctl is-active --quiet wpa_supplicant; then
        if systemctl restart wpa_supplicant 2>/dev/null; then
            sleep 10
            logger -t "$LOG_TAG" "wpa_supplicant restarted"
            return 0
        fi
    fi
    
    logger -t "$LOG_TAG" "Failed to restart networking service"
    return 1
}

# Log startup
logger -t "$LOG_TAG" "WiFi monitor started (checking every ${CHECK_INTERVAL}s, ping target: $PING_TARGET)"

# Main monitoring loop
while true; do
    if check_wifi; then
        # Connection is good
        if [ $FAILURE_COUNT -gt 0 ]; then
            logger -t "$LOG_TAG" "WiFi connection restored after $FAILURE_COUNT failures"
        fi
        FAILURE_COUNT=0
    else
        # Connection failed
        FAILURE_COUNT=$((FAILURE_COUNT + 1))
        logger -t "$LOG_TAG" "WiFi check failed (attempt $FAILURE_COUNT/$MAX_FAILURES)"
        
        # After MAX_FAILURES, attempt recovery
        if [ $FAILURE_COUNT -ge $MAX_FAILURES ]; then
            logger -t "$LOG_TAG" "Max failures reached, attempting recovery..."
            
            # Step 1: Try restarting interface
            if restart_wifi_interface && check_wifi; then
                logger -t "$LOG_TAG" "Recovery successful after interface restart"
                FAILURE_COUNT=0
            else
                # Step 2: Try restarting networking service
                logger -t "$LOG_TAG" "Interface restart failed, trying networking service..."
                if restart_networking && check_wifi; then
                    logger -t "$LOG_TAG" "Recovery successful after networking service restart"
                    FAILURE_COUNT=0
                else
                    logger -t "$LOG_TAG" "All recovery attempts failed, will retry on next check"
                    # Don't reset failure count - will keep trying
                fi
            fi
        fi
    fi
    
    # Wait before next check
    sleep "$CHECK_INTERVAL"
done
