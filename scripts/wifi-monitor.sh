#!/bin/bash
set -uo pipefail

# WiFi Connection Monitor with offline AP fallback
# Note: set -e disabled for this long-running daemon (signals would cause exit)
# - Keeps STA connected when possible
# - Spins up a local AP (hostapd + dnsmasq) after sustained disconnects
# - Periodically retries STA while AP is running to avoid getting stuck

# ===== BOOT PERFORMANCE TIMING =====
WIFI_MONITOR_START_MS=$(date +%s%3N)
log_timing() {
    local checkpoint="$1"
    local now_ms=$(date +%s%3N)
    local elapsed=$((now_ms - WIFI_MONITOR_START_MS))
    echo "[WIFI-MONITOR TIMING] +${elapsed}ms: $checkpoint"
}
log_timing "WiFi monitor starting"
# ====================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log_timing "Script dir resolved"

CONFIG_FILE="$SCRIPT_DIR/config.sh"
[ -f "$CONFIG_FILE" ] && source "$CONFIG_FILE"
log_timing "Config loaded"

LOCK_FILE="/var/run/wifi-monitor.lock"
LOG_TAG="wifi-monitor"

WIFI_IF="${OFFLINE_AP_INTERFACE:-wlan0}"
PING_TARGET="${OFFLINE_AP_PING_TARGET:-8.8.8.8}"
PING_TIMEOUT=5  # Increased from 3 to 5 seconds for weak WiFi signals
MAX_FAILURES=3
CHECK_INTERVAL="${OFFLINE_AP_CHECK_INTERVAL:-60}"
DISCONNECT_GRACE="${OFFLINE_AP_DISCONNECT_GRACE:-45}"
MIN_RSSI="${OFFLINE_AP_MIN_RSSI:--70}"
AP_ENABLED="${OFFLINE_AP_ENABLED:-false}"
AP_VIRTUAL_IF="${OFFLINE_AP_VIRTUAL_IF:-uap0}"
AP_SSID="${OFFLINE_AP_SSID:-TeslaUSB}"
AP_PASSPHRASE="${OFFLINE_AP_PASSPHRASE:-teslausb1234}"
AP_CHANNEL="${OFFLINE_AP_CHANNEL:-6}"
AP_IPV4_CIDR="${OFFLINE_AP_IPV4_CIDR:-192.168.4.1/24}"
AP_DHCP_START="${OFFLINE_AP_DHCP_START:-192.168.4.10}"
AP_DHCP_END="${OFFLINE_AP_DHCP_END:-192.168.4.50}"

RUNTIME_DIR="/run/teslausb-ap"
HOSTAPD_CONF="$RUNTIME_DIR/hostapd.conf"
DNSMASQ_CONF="$RUNTIME_DIR/dnsmasq.conf"
AP_STATE_FILE="$RUNTIME_DIR/ap.state"
AP_FORCE_MODE_FILE="$RUNTIME_DIR/force.mode"
HOSTAPD_PID="$RUNTIME_DIR/hostapd.pid"
DNSMASQ_PID="$RUNTIME_DIR/dnsmasq.pid"

FAILURE_COUNT=0
LAST_GOOD_TS=$(date +%s)
WAKE_SIGNAL=0

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >&2
    logger -t "$LOG_TAG" "$1"
}

# Prevent multiple instances
if [ -f "$LOCK_FILE" ]; then
    log "Another instance is running, exiting"
    exit 0
fi
touch "$LOCK_FILE"

cleanup() {
    log "Cleaning up wifi-monitor..."
    rm -f "$LOCK_FILE"
    # Don't stop AP on exit - let it continue running
}

trap cleanup EXIT INT TERM
trap "WAKE_SIGNAL=1" USR1

sleep_interval() {
    WAKE_SIGNAL=0
    # Adaptive interval: faster when disconnected (searching), slower when connected
    local interval="$CHECK_INTERVAL"
    if ! link_up 2>/dev/null; then
        interval=$((CHECK_INTERVAL < 20 ? CHECK_INTERVAL : 20))
    fi
    sleep "$interval" 2>/dev/null || true
}

ensure_runtime_dir() {
    mkdir -p "$RUNTIME_DIR"
}

ap_active() {
    [ -f "$AP_STATE_FILE" ]
}

# Removed: ap_started_at() - no longer used after removing retry logic

record_ap_start() {
    date +%s >"$AP_STATE_FILE"
}

clear_ap_state() {
    rm -f "$AP_STATE_FILE"
}

ap_iface() {
    # Use wlan0 directly for AP mode (not a virtual uap0).
    # On Pi Zero 2W (single-radio brcmfmac), creating a virtual __ap interface
    # still leaves wpa_supplicant holding wlan0 in STA mode, causing passive
    # channel scans that break AP TCP connections. Using wlan0 directly lets
    # hostapd take full control of the radio with no STA activity at all.
    echo "$WIFI_IF"
}

get_force_mode() {
    # First check runtime file (takes precedence)
    if [ -f "$AP_FORCE_MODE_FILE" ]; then
        local mode
        mode=$(cat "$AP_FORCE_MODE_FILE" 2>/dev/null || echo "auto")
        case "$mode" in
            force_on|force_off|auto) echo "$mode" ;;
            *) echo "auto" ;;
        esac
    # Fall back to persistent config
    elif [ -n "${OFFLINE_AP_FORCE_MODE:-}" ]; then
        echo "${OFFLINE_AP_FORCE_MODE}"
    else
        echo "auto"
    fi
}

current_rssi() {
    local sig
    sig=$(iw dev "$WIFI_IF" link 2>/dev/null | awk '/signal:/ {print $2}') || true
    echo "${sig:--100}"
}

link_up() {
    iw dev "$WIFI_IF" link 2>/dev/null | grep -q "Connected to"
}

ip_ready() {
    ip addr show "$WIFI_IF" 2>/dev/null | grep -q "inet "
}

ping_ok() {
    ping -c 1 -W "$PING_TIMEOUT" "$PING_TARGET" >/dev/null 2>&1
}

check_wifi() {
    if ! link_up; then
        log "$WIFI_IF not associated"
        return 1
    fi
    if ! ip_ready; then
        log "$WIFI_IF has no IP address"
        return 1
    fi
    if ping_ok; then
        return 0
    fi
    log "Ping to $PING_TARGET failed"
    return 1
}

restart_wifi_interface() {
    log "Restarting WiFi interface $WIFI_IF"
    if ip link set "$WIFI_IF" down 2>/dev/null; then
        sleep 2
        if ip link set "$WIFI_IF" up 2>/dev/null; then
            sleep 5
            return 0
        fi
    fi
    return 1
}

restart_networking() {
    log "Restarting networking stack"
    if systemctl is-active --quiet NetworkManager; then
        if systemctl restart NetworkManager 2>/dev/null; then
            sleep 10
            return 0
        fi
    fi
    if systemctl is-active --quiet dhcpcd; then
        if systemctl restart dhcpcd 2>/dev/null; then
            sleep 10
            return 0
        fi
    fi
    if systemctl is-active --quiet wpa_supplicant; then
        if systemctl restart wpa_supplicant 2>/dev/null; then
            sleep 10
            return 0
        fi
    fi
    return 1
}

# Removed: stop_sta_stack() and start_sta_stack() - no longer needed with concurrent mode
# WiFi client (STA) runs continuously alongside the AP on separate interfaces

write_hostapd_conf() {
    local iface="$1"
    cat >"$HOSTAPD_CONF" <<EOF
interface=$iface
driver=nl80211
ssid=$AP_SSID
hw_mode=g
channel=$AP_CHANNEL
wmm_enabled=0
auth_algs=1
wpa=2
wpa_passphrase=$AP_PASSPHRASE
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
EOF
}

write_dnsmasq_conf() {
    local iface="$1"
    local gateway
    gateway="${AP_IPV4_CIDR%%/*}"
    local hostname
    hostname=$(hostname)
    cat >"$DNSMASQ_CONF" <<EOF
interface=$iface
bind-interfaces
dhcp-range=$AP_DHCP_START,$AP_DHCP_END,12h
dhcp-option=3,$gateway
dhcp-option=6,$gateway
# Captive Portal - redirect all DNS queries to our gateway
# This forces devices to see our web portal regardless of what domain they try to access
address=/#/$gateway
# Local DNS - resolve hostname to AP gateway
address=/$hostname/$gateway
address=/$hostname.local/$gateway
log-queries
log-dhcp
EOF
}

stop_ap() {
    local iface
    iface=$(ap_iface)

    # Kill hostapd (non-blocking)
    if [ -f "$HOSTAPD_PID" ]; then
        kill "$(cat "$HOSTAPD_PID")" 2>/dev/null || true
        sleep 0.5
        rm -f "$HOSTAPD_PID"
    fi
    pkill -9 hostapd 2>/dev/null || true

    # Kill dnsmasq (non-blocking)
    if [ -f "$DNSMASQ_PID" ]; then
        kill "$(cat "$DNSMASQ_PID")" 2>/dev/null || true
        sleep 0.5
        rm -f "$DNSMASQ_PID"
    fi
    pkill -9 dnsmasq 2>/dev/null || true

    # Remove AP IP and restore wlan0 to managed mode for WiFi client
    ip addr flush dev "$iface" 2>/dev/null || true
    nmcli device set "$iface" managed yes 2>/dev/null || true

    clear_ap_state
    log "Stopped fallback AP — restored $iface to managed mode"
}

start_ap() {
    ensure_runtime_dir
    local iface
    iface=$(ap_iface)

    stop_ap

    # Verify physical interface exists
    if ! iw dev "$iface" info >/dev/null 2>&1; then
        log "Interface $iface not found, cannot start AP"
        return 1
    fi

    # Release wlan0 from NetworkManager and wpa_supplicant.
    # nmcli unmanage calls wpa_supplicant RemoveInterface over D-Bus, which
    # makes wpa_supplicant completely release the nl80211 socket for wlan0.
    # hostapd can then take the interface and switch it to __ap mode with no
    # STA firmware module running alongside — eliminating channel scan interference.
    nmcli device disconnect "$iface" 2>/dev/null || true
    nmcli device set "$iface" managed no 2>/dev/null || true
    sleep 1  # let wpa_supplicant finish RemoveInterface before hostapd takes over
    log "Released $iface from NetworkManager — radio available for AP mode"

    write_hostapd_conf "$iface"
    write_dnsmasq_conf "$iface"

    ip link set "$iface" up 2>/dev/null || true

    systemctl stop dnsmasq 2>/dev/null || true
    systemctl stop hostapd 2>/dev/null || true

    # Start hostapd first — it switches wlan0 from managed to __ap mode via nl80211
    local hostapd_out
    hostapd_out=$(hostapd -B -P "$HOSTAPD_PID" "$HOSTAPD_CONF" 2>&1)
    if [ $? -ne 0 ]; then
        log "Failed to start hostapd: $hostapd_out"
        nmcli device set "$iface" managed yes 2>/dev/null || true
        return 1
    fi

    # Assign IP after hostapd has configured the interface in AP mode
    ip addr flush dev "$iface" 2>/dev/null || true
    ip addr add "$AP_IPV4_CIDR" dev "$iface" || {
        log "Failed to assign IP $AP_IPV4_CIDR to $iface"
        kill "$(cat "$HOSTAPD_PID" 2>/dev/null)" 2>/dev/null || true
        rm -f "$HOSTAPD_PID"
        nmcli device set "$iface" managed yes 2>/dev/null || true
        return 1
    }

    # Start dnsmasq after IP is assigned
    if ! dnsmasq --conf-file="$DNSMASQ_CONF" --pid-file="$DNSMASQ_PID"; then
        log "Failed to start dnsmasq for fallback AP"
        kill "$(cat "$HOSTAPD_PID" 2>/dev/null)" 2>/dev/null || true
        rm -f "$HOSTAPD_PID"
        ip addr flush dev "$iface" 2>/dev/null || true
        nmcli device set "$iface" managed yes 2>/dev/null || true
        return 1
    fi

    record_ap_start
    log "Fallback AP started on $iface (SSID: $AP_SSID)"
}

# Removed: maybe_retry_sta_from_ap() - no longer needed with mandatory concurrent mode
# In concurrent mode, STA and AP run simultaneously without interference

# Cleanup any stale virtual interface from previous crash/unclean shutdown
log_timing "Cleaning up stale interfaces"
iw dev "$AP_VIRTUAL_IF" del 2>/dev/null || true

# Initialize runtime force mode from persistent config if not already set
log_timing "Initializing runtime directory"
ensure_runtime_dir
if [ ! -f "$AP_FORCE_MODE_FILE" ] && [ -n "${OFFLINE_AP_FORCE_MODE:-}" ]; then
    log "Initializing force mode from config: ${OFFLINE_AP_FORCE_MODE}"
    echo "${OFFLINE_AP_FORCE_MODE}" >"$AP_FORCE_MODE_FILE"
fi
log_timing "Force mode initialized"

# Verify physical WiFi interface exists
if ! iw dev "$WIFI_IF" info >/dev/null 2>&1; then
    log "WARNING: Physical WiFi interface $WIFI_IF not found - AP feature will not work"
fi
log_timing "WiFi interface verified"

# ── Boot-time STA connect attempt ──
# Try to connect to a saved network BEFORE entering the main loop.
# The Pi Zero 2 W has a single-radio chip — running the AP (hostapd on uap0)
# locks the radio to one channel and prevents STA association on other channels.
# So we attempt STA first while the radio is free.
log_timing "Attempting initial WiFi connection"
BOOT_CONNECTED=0
if [ "$(get_force_mode)" != "force_on" ]; then
    # Give NetworkManager up to 30 seconds to auto-connect
    for attempt in 1 2 3 4 5 6; do
        if check_wifi; then
            log "WiFi connected at boot (attempt $attempt)"
            BOOT_CONNECTED=1
            break
        fi
        sleep 5
    done

    if [ $BOOT_CONNECTED -eq 0 ]; then
        # NetworkManager didn't auto-connect — try highest-priority saved connection
        log "Auto-connect failed after 30s, trying explicit connect..."
        BEST_CONN=$(nmcli -t -f NAME,TYPE,AUTOCONNECT-PRIORITY connection show 2>/dev/null \
            | grep ':.*wireless' \
            | sort -t: -k3 -rn \
            | head -1 \
            | cut -d: -f1)
        if [ -n "$BEST_CONN" ]; then
            log "  Trying: $BEST_CONN"
            nmcli --wait 20 connection up "$BEST_CONN" 2>/dev/null
            sleep 5
            if check_wifi; then
                log "Connected to $BEST_CONN at boot"
                BOOT_CONNECTED=1
            else
                log "Explicit connect to $BEST_CONN failed"
            fi
        fi
    fi
fi
log_timing "Initial WiFi attempt complete (connected=$BOOT_CONNECTED)"

log_timing "WiFi monitor initialization complete (total: $(($(date +%s%3N) - WIFI_MONITOR_START_MS))ms)"
log "WiFi monitor started (interval ${CHECK_INTERVAL}s, AP fallback ${AP_ENABLED})"

while true; do
    force_mode=$(get_force_mode)

    if [ "$force_mode" = "force_on" ]; then
        # Force-on mode: Start AP immediately (runs concurrently with WiFi client)
        if ! ap_active; then
            log "Force-on requested; starting fallback AP"
            start_ap || log "Force-on start failed"
        fi
        sleep_interval
        continue
    fi

    if [ "$force_mode" = "force_off" ]; then
        if ap_active; then
            log "Force-off requested; stopping fallback AP"
            stop_ap
        fi
        sleep_interval
        continue
    fi

    if ap_active; then
        # AP is running. Check if WiFi STA came back (NM may auto-reconnect).
        if check_wifi; then
            rssi=$(current_rssi)
            if [ -n "$rssi" ] && [ "$rssi" -ge "$MIN_RSSI" ]; then
                log "Auto mode: WiFi healthy (RSSI ${rssi}dBm); stopping AP"
                stop_ap
                sleep_interval
                continue
            fi
        fi

        # WiFi still down while AP is active.
        # Periodically stop AP and attempt STA reconnect (~every 5 min).
        # Pi Zero 2 W single-radio can't scan/associate while AP holds the channel.
        STA_RETRY_COUNTER=${STA_RETRY_COUNTER:-0}
        STA_RETRY_COUNTER=$((STA_RETRY_COUNTER + 1))
        if [ $STA_RETRY_COUNTER -ge 15 ]; then
            STA_RETRY_COUNTER=0
            log "Periodic STA retry: stopping AP to attempt home WiFi"
            stop_ap
            sleep 8

            # Let NetworkManager try to auto-connect (radio is now free)
            # Longer wait needed because wlan0 was frozen (unmanaged+down)
            nmcli device wifi rescan 2>/dev/null
            sleep 20

            if check_wifi; then
                log "STA reconnected — AP stays off"
                FAILURE_COUNT=0
                LAST_GOOD_TS=$(date +%s)
            else
                log "STA retry failed — restarting AP"
                start_ap || log "AP restart failed"
            fi
        fi

        sleep_interval
        continue
    fi

    if check_wifi; then
        if [ $FAILURE_COUNT -gt 0 ]; then
            log "WiFi restored after $FAILURE_COUNT failures"
        fi
        FAILURE_COUNT=0
        LAST_GOOD_TS=$(date +%s)
    else
        FAILURE_COUNT=$((FAILURE_COUNT + 1))
        log "WiFi check failed (attempt $FAILURE_COUNT/$MAX_FAILURES)"

        if [ $FAILURE_COUNT -ge $MAX_FAILURES ]; then
            log "Max failures reached; attempting STA recovery"
            if restart_wifi_interface && check_wifi; then
                log "Recovery successful after interface restart"
                FAILURE_COUNT=0
                LAST_GOOD_TS=$(date +%s)
            elif restart_networking && check_wifi; then
                log "Recovery successful after networking restart"
                FAILURE_COUNT=0
                LAST_GOOD_TS=$(date +%s)
            else
                # Last resort: try explicit nmcli reconnect to configured connection
                log "Standard recovery failed; trying explicit nmcli reconnect"
                local active_conn
                active_conn=$(nmcli -t -f NAME,TYPE connection show 2>/dev/null | grep ':.*wireless' | head -1 | cut -d: -f1)
                if [ -n "$active_conn" ]; then
                    nmcli connection up "$active_conn" 2>/dev/null && sleep 5
                    if check_wifi; then
                        log "Recovery successful after explicit nmcli reconnect"
                        FAILURE_COUNT=0
                        LAST_GOOD_TS=$(date +%s)
                    else
                        log "All recovery attempts failed"
                    fi
                else
                    log "No wireless connection profile found for reconnect"
                fi
            fi
        fi

        if [ "$AP_ENABLED" = "true" ] && [ "$force_mode" != "force_off" ]; then
            now=$(date +%s)
            if [ $(( now - LAST_GOOD_TS )) -ge "$DISCONNECT_GRACE" ]; then
                log "Offline for ${DISCONNECT_GRACE}s; starting fallback AP"
                if start_ap; then
                    # AP started successfully
                    FAILURE_COUNT=0
                    LAST_GOOD_TS=$now
                else
                    log "Failed to start fallback AP"
                    # Don't reset LAST_GOOD_TS on failure - allow faster retry
                    # Reset failure count to start grace period over
                    FAILURE_COUNT=0
                fi
            fi
        fi
    fi

    sleep_interval
done
