#!/bin/bash
set -uo pipefail

# WiFi Connection Monitor with offline AP fallback
# Note: set -e disabled for this long-running daemon (signals would cause exit)
# - Keeps STA connected when possible
# - Spins up a local AP (hostapd + dnsmasq) after sustained disconnects
# - Periodically retries STA while AP is running to avoid getting stuck

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.sh"
[ -f "$CONFIG_FILE" ] && source "$CONFIG_FILE"

LOCK_FILE="/var/run/wifi-monitor.lock"
LOG_TAG="wifi-monitor"

WIFI_IF="${OFFLINE_AP_INTERFACE:-wlan0}"
PING_TARGET="${OFFLINE_AP_PING_TARGET:-8.8.8.8}"
PING_TIMEOUT=3
MAX_FAILURES=3
CHECK_INTERVAL="${OFFLINE_AP_CHECK_INTERVAL:-60}"
DISCONNECT_GRACE="${OFFLINE_AP_DISCONNECT_GRACE:-45}"
MIN_RSSI="${OFFLINE_AP_MIN_RSSI:--70}"
STABLE_SECONDS="${OFFLINE_AP_STABLE_SECONDS:-20}"
RETRY_SECONDS="${OFFLINE_AP_RETRY_SECONDS:-300}"
AP_ENABLED="${OFFLINE_AP_ENABLED:-false}"
AP_ALLOW_CONCURRENT="${OFFLINE_AP_ALLOW_CONCURRENT:-false}"
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
    sleep "$CHECK_INTERVAL" 2>/dev/null || true
}

ensure_runtime_dir() {
    mkdir -p "$RUNTIME_DIR"
}

ap_active() {
    [ -f "$AP_STATE_FILE" ]
}

ap_started_at() {
    if ap_active; then
        cat "$AP_STATE_FILE"
    else
        echo 0
    fi
}

record_ap_start() {
    date +%s >"$AP_STATE_FILE"
}

clear_ap_state() {
    rm -f "$AP_STATE_FILE"
}

ap_iface() {
    if [ "$AP_ALLOW_CONCURRENT" = "true" ]; then
        echo "$AP_VIRTUAL_IF"
    else
        echo "$WIFI_IF"
    fi
}

get_force_mode() {
    if [ -f "$AP_FORCE_MODE_FILE" ]; then
        local mode
        mode=$(cat "$AP_FORCE_MODE_FILE" 2>/dev/null || echo "auto")
        case "$mode" in
            force_on|force_off|auto) echo "$mode" ;;
            *) echo "auto" ;;
        esac
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

stop_sta_stack() {
    systemctl stop wpa_supplicant@"$WIFI_IF".service 2>/dev/null || true
    systemctl stop wpa_supplicant 2>/dev/null || true
    systemctl stop dhcpcd 2>/dev/null || true
}

start_sta_stack() {
    systemctl start dhcpcd 2>/dev/null || true
    systemctl start wpa_supplicant@"$WIFI_IF".service 2>/dev/null || true
    systemctl start wpa_supplicant 2>/dev/null || true
}

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
# Local DNS - resolve hostname to AP gateway
address=/$hostname/$gateway
address=/$hostname.local/$gateway
# Also provide upstream DNS
server=8.8.8.8
server=8.8.4.4
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
    
    # Clean up interface (non-blocking)
    ip addr flush dev "$iface" 2>/dev/null || true
    if [ "$AP_ALLOW_CONCURRENT" = "true" ] && [ "$iface" != "$WIFI_IF" ]; then
        iw dev "$iface" del 2>/dev/null || true
    fi
    clear_ap_state
    log "Stopped fallback AP"
}

start_ap() {
    ensure_runtime_dir
    local iface
    iface=$(ap_iface)

    stop_ap

    if [ "$AP_ALLOW_CONCURRENT" = "true" ] && [ "$iface" != "$WIFI_IF" ]; then
        iw dev "$iface" del 2>/dev/null || true
        if ! iw dev "$WIFI_IF" interface add "$iface" type __ap; then
            log "Failed to create virtual AP interface $iface from $WIFI_IF"
            return 1
        fi
        log "Created virtual AP interface $iface"
        
        # Tell NetworkManager to ignore this interface
        nmcli device set "$iface" managed no 2>/dev/null || true
    else
        stop_sta_stack
    fi

    write_hostapd_conf "$iface"
    write_dnsmasq_conf "$iface"

    # Configure interface (don't set down/up for virtual AP interface)
    ip addr flush dev "$iface" 2>/dev/null || true
    ip addr add "$AP_IPV4_CIDR" dev "$iface" || {
        log "Failed to assign IP $AP_IPV4_CIDR to $iface"
        return 1
    }

    systemctl stop dnsmasq 2>/dev/null || true
    systemctl stop hostapd 2>/dev/null || true

    # Start dnsmasq
    if ! dnsmasq --conf-file="$DNSMASQ_CONF" --pid-file="$DNSMASQ_PID"; then
        log "Failed to start dnsmasq for fallback AP"
        return 1
    fi
    
    # Start hostapd (capture errors)
    local hostapd_out
    hostapd_out=$(hostapd -B -P "$HOSTAPD_PID" "$HOSTAPD_CONF" 2>&1)
    if [ $? -ne 0 ]; then
        log "Failed to start hostapd: $hostapd_out"
        kill "$(cat "$DNSMASQ_PID" 2>/dev/null)" 2>/dev/null || true
        rm -f "$DNSMASQ_PID"
        return 1
    fi

    record_ap_start
    log "Fallback AP started on $iface (SSID: $AP_SSID, concurrent=$AP_ALLOW_CONCURRENT)"
}

maybe_retry_sta_from_ap() {
    if ! ap_active; then
        return
    fi
    if [ "$AP_ALLOW_CONCURRENT" = "true" ]; then
        # In concurrent mode, STA stays up alongside AP; no teardown retries needed
        return
    fi
    local now
    now=$(date +%s)
    if [ $(( now - $(ap_started_at) )) -lt "$RETRY_SECONDS" ]; then
        return
    fi

    log "Retrying STA join while AP is active"
    stop_ap
    start_sta_stack
    sleep 10

    local rssi
    if link_up && ip_ready; then
        rssi=$(current_rssi)
        if [ "$rssi" -ge "$MIN_RSSI" ]; then
            log "STA link restored (RSSI ${rssi}dBm); keeping AP down"
            FAILURE_COUNT=0
            LAST_GOOD_TS=$(date +%s)
            return
        fi
    fi

    log "STA retry failed or weak; re-enabling fallback AP"
    start_ap || log "Retry to start AP failed"
}

log "WiFi monitor started (interval ${CHECK_INTERVAL}s, AP fallback ${AP_ENABLED})"

while true; do
    force_mode=$(get_force_mode)

    if [ "$force_mode" = "force_on" ]; then
        # Force-on mode: Start AP immediately (concurrent with STA if enabled)
        if ! ap_active; then
            log "Force-on requested; starting fallback AP (concurrent mode)"
            start_ap || log "Force-on start failed"
        fi
        sleep_interval
        continue
    fi

    if [ "$force_mode" = "force_off" ] && ap_active; then
        log "Force-off requested; stopping fallback AP"
        stop_ap
        start_sta_stack
    fi

    if ap_active; then
        # In auto mode with concurrent AP, check if WiFi is healthy and stop AP
        if [ "$force_mode" = "auto" ] && [ "$AP_ALLOW_CONCURRENT" = "true" ]; then
            if check_wifi; then
                rssi=$(current_rssi)
                if [ -n "$rssi" ] && [ "$rssi" -ge "$MIN_RSSI" ]; then
                    log "Auto mode: WiFi healthy (RSSI ${rssi}dBm); stopping concurrent AP"
                    stop_ap
                    sleep_interval
                    continue
                fi
            fi
        fi
        
        # If not concurrent or WiFi not healthy, try STA recovery
        maybe_retry_sta_from_ap
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
                log "Recovery attempts failed"
            fi
        fi

        if [ "$AP_ENABLED" = "true" ] && [ "$force_mode" != "force_off" ]; then
            now=$(date +%s)
            if [ $(( now - LAST_GOOD_TS )) -ge "$DISCONNECT_GRACE" ]; then
                log "Offline for ${DISCONNECT_GRACE}s; starting fallback AP"
                start_ap || log "Failed to start fallback AP"
                FAILURE_COUNT=0
                LAST_GOOD_TS=$now
            fi
        fi
    fi

    sleep_interval
done
