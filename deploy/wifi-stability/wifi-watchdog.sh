#!/usr/bin/env bash
# /usr/local/sbin/wifi-watchdog.sh
#
# Installed by setup-lib/12-wifi-stability.sh and invoked by
# /etc/systemd/system/wifi-watchdog.timer every 30 s.
#
# PURPOSE
# -------
# The BCM43436 SDIO WiFi chip on the Pi Zero 2 W can wedge under
# sustained TX load (rclone uploads, big SMB writes). The kernel
# keeps running — the hardware watchdog never trips — but WiFi
# stays dead until brcmfmac is reloaded or the box is rebooted.
# This script ladders escalating recovery actions so the device
# ALWAYS comes back without losing more than a few minutes, and
# WITHOUT the v1 sledgehammer of "reboot after 3 minutes".
#
# ESCALATION LADDER (state persisted to ${STATE_FILE})
# ----------------------------------------------------
#   fail  0 ..  1   ping fail count <2          : do nothing
#   fail  2         pause uploads (touch ${PAUSE_FILE}) + nmcli down/up
#   fail  3         (in cool-down, no new action)
#   fail  4         rmmod brcmfmac; sleep 2; modprobe brcmfmac
#   fail  5         (in cool-down)
#   fail  6         ip link set wlan0 down; sleep 1; ip link set wlan0 up
#   fail  7 ..  9   (in cool-down)
#   fail 10         systemctl reboot              ← LAST RESORT
#
# Pause file (${PAUSE_FILE}) is the contract with the Python upload
# worker: while the file exists, the worker sleeps and does not
# spawn rclone. Removed once WiFi has been healthy for 2 consecutive
# ticks (≈60 s).
#
# SUCCESS CRITERIA
# ----------------
# A "healthy" tick is gateway-ping reply within 2 s. When healthy,
# fail count is decremented (not reset) so a chip that's flapping
# still escalates. After ${PAUSE_RELEASE_HEALTHY_TICKS} consecutive
# healthy ticks, the pause file is removed and uploads resume.
#
# IDEMPOTENCY / RE-ENTRANCY
# -------------------------
# This script is invoked by a oneshot service from a timer. Each
# invocation reads/writes ${STATE_FILE} atomically (mv-over). flock
# on ${STATE_FILE}.lock prevents two concurrent invocations from
# racing the state machine (should never happen with a 30 s timer,
# but is cheap insurance).
#
# SAFETY
# ------
# The reboot tier is intentionally retained: if the chip is so dead
# that none of the soft recoveries work, only a reboot will fix it
# and losing the device entirely is worse than 60 s of downtime.
#
#   Operator: User input: "any critical OOM does reboot the device.
#   It is critical that the device never fully loses wifi or SSH
#   capabilities. So if something happens bad we do need a reboot
#   to get it working"

set -u

readonly STATE_DIR="/run/teslausb"
readonly STATE_FILE="${STATE_DIR}/wifi-watchdog.state"
readonly LOCK_FILE="${STATE_DIR}/wifi-watchdog.lock"
readonly PAUSE_FILE="${STATE_DIR}/uploads_paused"
readonly LOG_TAG="wifi-watchdog"

# Tier thresholds (failed-ping counts). Must be strictly ascending.
readonly TIER_PAUSE=2
readonly TIER_BRCMFMAC_RELOAD=4
readonly TIER_LINK_BOUNCE=6
readonly TIER_REBOOT=10

# Pause file is released after this many consecutive healthy ticks.
readonly PAUSE_RELEASE_HEALTHY_TICKS=2

# Pings before declaring this tick a fail.
readonly PING_COUNT=3
readonly PING_TIMEOUT_S=2

log() { logger -t "${LOG_TAG}" -- "$*"; printf '%s %s\n' "${LOG_TAG}" "$*" >&2; }

mkdir -p "${STATE_DIR}"

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  log "another instance is running; skipping tick"
  exit 0
fi

# Read state: two integers, "<fail_count> <healthy_ticks>"
fail_count=0
healthy_ticks=0
if [[ -r "${STATE_FILE}" ]]; then
  read -r fail_count healthy_ticks < "${STATE_FILE}" || true
  fail_count="${fail_count:-0}"
  healthy_ticks="${healthy_ticks:-0}"
fi

write_state() {
  local tmp="${STATE_FILE}.tmp"
  printf '%d %d\n' "$1" "$2" > "${tmp}"
  mv -f "${tmp}" "${STATE_FILE}"
}

gateway="$(ip route show default 2>/dev/null | awk '/default/ {print $3; exit}')"
if [[ -z "${gateway}" ]]; then
  # No default route at all → treat as a fail tick.
  log "no default route — fail tick (count was ${fail_count})"
  fail_count=$((fail_count + 1))
  healthy_ticks=0
else
  if ping -n -q -c "${PING_COUNT}" -W "${PING_TIMEOUT_S}" "${gateway}" >/dev/null 2>&1; then
    if (( fail_count > 0 )); then
      fail_count=$((fail_count - 1))
    fi
    healthy_ticks=$((healthy_ticks + 1))
    log "healthy (gw=${gateway} fail=${fail_count} healthy_ticks=${healthy_ticks})"
  else
    fail_count=$((fail_count + 1))
    healthy_ticks=0
    log "ping failed (gw=${gateway} fail=${fail_count})"
  fi
fi

# Release the pause once we've been healthy long enough.
if [[ -e "${PAUSE_FILE}" ]] && (( healthy_ticks >= PAUSE_RELEASE_HEALTHY_TICKS )); then
  rm -f "${PAUSE_FILE}"
  log "wifi healthy ${healthy_ticks} ticks — releasing upload pause"
fi

# Tier escalation. Each tier acts only on the EXACT count to avoid
# repeating heavy actions while the chip is still recovering.
case "${fail_count}" in
  "${TIER_PAUSE}")
    log "TIER ${fail_count}: pause uploads + nmcli down/up wlan0"
    : > "${PAUSE_FILE}"
    nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null \
      | awk -F: '$2=="wlan0" {print $1}' \
      | while read -r conn; do
          [[ -n "${conn}" ]] || continue
          log "  nmcli connection down '${conn}'"
          nmcli connection down "${conn}" >/dev/null 2>&1 || true
          log "  nmcli connection up   '${conn}'"
          nmcli connection up   "${conn}" >/dev/null 2>&1 || true
        done
    ;;
  "${TIER_BRCMFMAC_RELOAD}")
    log "TIER ${fail_count}: reload brcmfmac module"
    : > "${PAUSE_FILE}"
    modprobe -r brcmfmac 2>&1 | logger -t "${LOG_TAG}" || true
    sleep 2
    modprobe   brcmfmac 2>&1 | logger -t "${LOG_TAG}" || true
    ;;
  "${TIER_LINK_BOUNCE}")
    log "TIER ${fail_count}: ip link bounce wlan0"
    : > "${PAUSE_FILE}"
    ip link set wlan0 down 2>&1 | logger -t "${LOG_TAG}" || true
    sleep 1
    ip link set wlan0 up   2>&1 | logger -t "${LOG_TAG}" || true
    ;;
  *)
    if (( fail_count >= TIER_REBOOT )); then
      log "TIER ${fail_count}: REBOOT (all soft recoveries exhausted)"
      # Best-effort: persist intent so the reboot reason is clear
      # in journal across the boot.
      logger -t "${LOG_TAG}" -p daemon.crit \
        "rebooting after ${fail_count} consecutive failed ticks"
      systemctl reboot
    fi
    ;;
esac

write_state "${fail_count}" "${healthy_ticks}"
exit 0
