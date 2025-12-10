#!/bin/bash
set -euo pipefail

# AP control helper for web UI / CLI
# Allows forcing fallback AP on/off/auto and reporting status.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.sh"
[ -f "$CONFIG_FILE" ] && source "$CONFIG_FILE"

RUNTIME_DIR="/run/teslausb-ap"
AP_STATE_FILE="$RUNTIME_DIR/ap.state"
FORCE_MODE_FILE="$RUNTIME_DIR/force.mode"
HOSTAPD_PID="$RUNTIME_DIR/hostapd.pid"
DNSMASQ_PID="$RUNTIME_DIR/dnsmasq.pid"
AP_VIRTUAL_IF="${OFFLINE_AP_VIRTUAL_IF:-uap0}"
WIFI_IF="${OFFLINE_AP_INTERFACE:-wlan0}"
AP_SSID="${OFFLINE_AP_SSID:-TeslaUSB}"
AP_IPV4_CIDR="${OFFLINE_AP_IPV4_CIDR:-192.168.4.1/24}"
AP_DHCP_START="${OFFLINE_AP_DHCP_START:-192.168.4.10}"
AP_DHCP_END="${OFFLINE_AP_DHCP_END:-192.168.4.50}"
RETRY_SECONDS="${OFFLINE_AP_RETRY_SECONDS:-300}"

ensure_runtime_dir() {
  mkdir -p "$RUNTIME_DIR"
}

ap_iface() {
  echo "$AP_VIRTUAL_IF"
}

get_force_mode() {
  # First check runtime file (takes precedence)
  if [ -f "$FORCE_MODE_FILE" ]; then
    local mode
    mode=$(cat "$FORCE_MODE_FILE" 2>/dev/null || echo "auto")
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

set_force_mode() {
  local mode="$1"
  ensure_runtime_dir
  
  # Write to runtime file (for immediate effect)
  echo "$mode" >"$FORCE_MODE_FILE"
  
  # Persist to config.sh (survives reboot)
  if [ -f "$CONFIG_FILE" ]; then
    # Use sed to update the config file
    sed -i "s|^OFFLINE_AP_FORCE_MODE=.*|OFFLINE_AP_FORCE_MODE=\"$mode\"|" "$CONFIG_FILE"
  fi
  
  # Nudge monitor to wake sooner (best-effort)
  systemctl kill -s SIGUSR1 wifi-monitor.service 2>/dev/null || true
}

status_json() {
  local active force iface gateway hostapd_running dnsmasq_running
  force=$(get_force_mode)
  iface=$(ap_iface)
  gateway="${AP_IPV4_CIDR%%/*}"
  
  # Check if processes are actually running (more reliable than state file)
  if [ -f "$HOSTAPD_PID" ] && ps -p "$(cat "$HOSTAPD_PID" 2>/dev/null)" >/dev/null 2>&1; then
    hostapd_running=true
  else
    hostapd_running=false
  fi
  
  if [ -f "$DNSMASQ_PID" ] && ps -p "$(cat "$DNSMASQ_PID" 2>/dev/null)" >/dev/null 2>&1; then
    dnsmasq_running=true
  else
    dnsmasq_running=false
  fi
  
  # AP is active if both processes are running
  if [ "$hostapd_running" = "true" ] && [ "$dnsmasq_running" = "true" ]; then
    active=true
  else
    active=false
  fi
  
  cat <<EOF
{
  "ap_active": $active,
  "force_mode": "$force",
  "allow_concurrent": true,
  "ap_interface": "$iface",
  "static_ip": "$gateway",
  "dhcp_range_start": "$AP_DHCP_START",
  "dhcp_range_end": "$AP_DHCP_END",
  "ssid": "$AP_SSID",
  "retry_seconds": "$RETRY_SECONDS",
  "hostapd_pid": "$( [ -f "$HOSTAPD_PID" ] && cat "$HOSTAPD_PID" )",
  "dnsmasq_pid": "$( [ -f "$DNSMASQ_PID" ] && cat "$DNSMASQ_PID" )"
}
EOF
}

usage() {
  cat <<EOF
Usage: $0 [status|force-on|force-off|force-auto|reload]
  status      Print JSON status
  force-on    Force AP on until changed
  force-off   Force AP off (blocks auto start)
  force-auto  Return to automatic behavior
  reload      Reload config and restart AP if currently active
EOF
}

reload_ap() {
  # Use force mode to cleanly stop AP, avoiding race conditions
  set_force_mode "force_off"
  sleep 2
  
  # Clean up state and config files so fresh ones are generated with new config
  rm -f /run/teslausb-ap/hostapd.conf /run/teslausb-ap/dnsmasq.conf
  
  # Return to auto mode - wifi-monitor will restart AP if needed
  set_force_mode "auto"
}

case "${1-}" in
  status)
    status_json
    ;;
  force-on)
    set_force_mode "force_on"
    ;;
  force-off)
    set_force_mode "force_off"
    ;;
  force-auto)
    set_force_mode "auto"
    ;;
  reload)
    reload_ap
    ;;
  *)
    usage
    exit 1
    ;;
esac
