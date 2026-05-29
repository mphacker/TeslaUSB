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
#                   (fail 1 raises ${DEGRADED_FILE} — see below)
#   fail  2         pause uploads (touch ${PAUSE_FILE}) + nmcli down/up
#   fail  3         (in cool-down, no new action)
#   fail  4         brcmfmac SDIO unbind/bind (resets firmware
#                   without unloading the module — works even when
#                   NetworkManager still references it; falls back
#                   to rmmod/modprobe if SDIO node not found). ALSO
#                   arms an independent safe-reboot dead-man so a
#                   hard D-state wedge that freezes this script still
#                   recovers (see SAFE_REBOOT_* below).
#   fail  5         (in cool-down)
#   fail  6         ip link set wlan0 down; sleep 1; ip link set wlan0 up
#   fail  7 ..  9   (in cool-down)
#   fail 10         safe-reboot (quiesce USB gadget, then reboot);
#                   plain reboot if the script is absent ← LAST RESORT
#
# Pause file (${PAUSE_FILE}) is the contract with the Python upload
# worker: while the file exists, the worker sleeps and does not
# spawn rclone. Removed once WiFi has been healthy for 2 consecutive
# ticks (≈60 s).
#
# Degraded file (${DEGRADED_FILE}) is a softer, earlier contract with
# the same worker: while it exists, the worker keeps running but
# throttles (gentler bwlimit + longer inter-file cooldown) to shed
# SDIO load before the chip wedges. Raised at fail 1 or on elevated
# RTT; cleared when fully healthy. See DEGRADED_RTT_MS below.
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
readonly DEGRADED_FILE="${STATE_DIR}/wifi_degraded"
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

# Early-warning degradation. ${DEGRADED_FILE} is a SOFTER advisory
# than ${PAUSE_FILE}: it asks the upload worker to BACK OFF (gentler
# bwlimit + longer inter-file cooldown) rather than stop entirely, so
# we shed SDIO load BEFORE the chip wedges. Raised when either:
#   * fail_count >= 1 (a single missed gateway ping — the first sign
#     of trouble, one tier below the hard pause at fail=2), or
#   * the last healthy ping's average RTT exceeds ${DEGRADED_RTT_MS}
#     (the bus is congesting even though packets still get through).
# Cleared only when fail_count==0 AND RTT is back under threshold, so
# uploads ease back in gradually instead of slamming the chip again.
readonly DEGRADED_RTT_MS=400

# Safe-reboot dead-man. A HARD SDIO backplane wedge parks
# brcmf_sdio_dataworker in uninterruptible D-state holding the SDIO
# host mutex; the tier-4 unbind write then blocks in D-state too,
# which CANNOT be killed (timeout/SIGKILL are ignored until the
# kernel call returns) and keeps this script's flock — freezing the
# whole escalation ladder so tier-6/tier-10 never run. To guarantee
# recovery anyway, on entering tier 4 we arm an INDEPENDENT transient
# systemd timer (a separate process, no SDIO dependency) that runs
# the safe-reboot script after SAFE_REBOOT_DEADMAN_SECONDS. If the
# soft recovery works, a healthy tick cancels it; if we're wedged,
# it fires regardless of this script being stuck. The safe-reboot
# script quiesces the USB gadget first so TeslaCAM is not corrupted.
readonly SAFE_REBOOT_UNIT="wifi-safe-reboot"
readonly SAFE_REBOOT_SCRIPT="/usr/local/sbin/wifi-safe-reboot.sh"
readonly SAFE_REBOOT_DEADMAN_SECONDS=180

log() { logger -t "${LOG_TAG}" -- "$*"; printf '%s %s\n' "${LOG_TAG}" "$*" >&2; }

# _timeout_write <secs> <path> <data>
#   Write <data> to <path> under a wall-clock <secs> cap. Protects the
#   killable hang cases (slow bind, sysfs probe). A true D-state write
#   still won't honour the timeout — that's what the dead-man covers —
#   but this bounds everything else. Returns the write's status, or
#   124 if it timed out.
_timeout_write() {
  local secs="$1" path="$2" data="$3"
  # shellcheck disable=SC2016  # $1/$2 must expand in the child sh, not here.
  timeout "${secs}" sh -c 'printf "%s" "$2" > "$1"' _ "${path}" "${data}"
}

# arm_safe_reboot_deadman
#   Idempotently arm the independent safe-reboot timer. No-op if it is
#   already armed (so re-entering tier 4 does not reset the clock).
#   Falls back to a plain reboot if the safe-reboot script is absent,
#   so the last-resort guarantee holds even before Phase 2 ships.
arm_safe_reboot_deadman() {
  if systemctl is-active --quiet "${SAFE_REBOOT_UNIT}.timer" 2>/dev/null; then
    log "  safe-reboot dead-man already armed"
    return 0
  fi
  systemctl reset-failed "${SAFE_REBOOT_UNIT}.timer" "${SAFE_REBOOT_UNIT}.service" \
    >/dev/null 2>&1 || true
  local target="${SAFE_REBOOT_SCRIPT}"
  if [[ ! -x "${target}" ]]; then
    target="/sbin/reboot"
    log "  WARN: ${SAFE_REBOOT_SCRIPT} missing — arming plain reboot dead-man"
  fi
  if systemd-run --on-active="${SAFE_REBOOT_DEADMAN_SECONDS}" \
       --unit="${SAFE_REBOOT_UNIT}" \
       --description="TeslaUSB WiFi safe-reboot dead-man" \
       "${target}" >/dev/null 2>&1; then
    log "  armed safe-reboot dead-man (${SAFE_REBOOT_DEADMAN_SECONDS}s → ${target})"
  else
    log "  WARN: failed to arm safe-reboot dead-man"
  fi
}

# cancel_safe_reboot_deadman
#   Stand down the safe-reboot timer once WiFi is solidly back.
#   Idempotent: a no-op when nothing is armed.
cancel_safe_reboot_deadman() {
  if systemctl is-active --quiet "${SAFE_REBOOT_UNIT}.timer" 2>/dev/null; then
    systemctl stop "${SAFE_REBOOT_UNIT}.timer" >/dev/null 2>&1 || true
    systemctl reset-failed "${SAFE_REBOOT_UNIT}.timer" "${SAFE_REBOOT_UNIT}.service" \
      >/dev/null 2>&1 || true
    log "  cancelled safe-reboot dead-man (wifi healthy)"
  fi
}

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
rtt_avg=""
if [[ -z "${gateway}" ]]; then
  # No default route at all → treat as a fail tick.
  log "no default route — fail tick (count was ${fail_count})"
  fail_count=$((fail_count + 1))
  healthy_ticks=0
else
  if ping_out="$(ping -n -q -c "${PING_COUNT}" -W "${PING_TIMEOUT_S}" "${gateway}" 2>/dev/null)"; then
    if (( fail_count > 0 )); then
      fail_count=$((fail_count - 1))
    fi
    healthy_ticks=$((healthy_ticks + 1))
    # Parse average RTT from the "rtt min/avg/max/mdev = a/b/c/d ms"
    # summary line. Field 5 (split on '/') is the avg value.
    rtt_avg="$(printf '%s\n' "${ping_out}" \
                | awk -F'/' '/min\/avg\/max/ {print $5; exit}')"
    log "healthy (gw=${gateway} fail=${fail_count} healthy_ticks=${healthy_ticks} rtt=${rtt_avg:-?}ms)"
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

# Early-warning degraded advisory (softer than the pause above). Raise
# it the moment we see a single miss (fail>=1) OR the bus is congesting
# (elevated RTT) so the uploader throttles BEFORE the chip wedges.
# Clear it only when fully healthy (fail==0 and RTT under threshold).
rtt_high=false
if [[ -n "${rtt_avg}" ]] \
   && awk -v r="${rtt_avg}" -v t="${DEGRADED_RTT_MS}" 'BEGIN { exit !(r > t) }'; then
  rtt_high=true
fi
if (( fail_count >= 1 )) || ${rtt_high}; then
  if [[ ! -e "${DEGRADED_FILE}" ]]; then
    log "wifi degraded — advising uploader to throttle (fail=${fail_count} rtt=${rtt_avg:-?}ms)"
  fi
  : > "${DEGRADED_FILE}"
elif [[ -e "${DEGRADED_FILE}" ]]; then
  rm -f "${DEGRADED_FILE}"
  log "wifi recovered — clearing degraded advisory"
fi

# Stand down the safe-reboot dead-man only once WiFi is SOLIDLY back
# (no outstanding failures + healthy for the release window). Gating
# on a clean fail_count avoids a flapping chip repeatedly cancelling
# the dead-man and thereby dodging the last-resort reboot forever.
if (( fail_count == 0 )) && (( healthy_ticks >= PAUSE_RELEASE_HEALTHY_TICKS )); then
  cancel_safe_reboot_deadman
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
    log "TIER ${fail_count}: reset brcmfmac firmware (SDIO unbind/bind)"
    : > "${PAUSE_FILE}"

    # Arm the independent safe-reboot dead-man BEFORE touching the
    # SDIO bus. If the unbind below wedges this script in D-state,
    # the dead-man (a separate process) still fires and recovers us.
    arm_safe_reboot_deadman

    # Bring NM's wlan0 connection(s) down so the driver/device is
    # idle before we yank it. `modprobe -r brcmfmac` fails with
    # "Module is in use" when NM still has a handle on the device,
    # which is exactly the failure mode that triggered the
    # 2026-05-28 14:50 tier-10 reboot. SDIO unbind/bind avoids that
    # problem, but bringing the link down first is still polite.
    nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null \
      | awk -F: '$2=="wlan0" {print $1}' \
      | while read -r conn; do
          [[ -n "${conn}" ]] || continue
          log "  nmcli connection down '${conn}'"
          nmcli connection down "${conn}" >/dev/null 2>&1 || true
        done
    ip link set wlan0 down 2>/dev/null || true

    # Preferred path: SDIO unbind/bind. The brcmfmac driver registers
    # itself under /sys/bus/sdio/drivers/brcmfmac as a directory whose
    # entries look like "mmc1:0001:1". Writing that name to ./unbind
    # detaches the firmware regardless of refcount, then writing it to
    # ./bind re-probes and reloads firmware — same effect as a module
    # reload but it does NOT require the module to be idle.
    sdio_dev=""
    if [[ -d /sys/bus/sdio/drivers/brcmfmac ]]; then
      sdio_dev="$(find /sys/bus/sdio/drivers/brcmfmac -mindepth 1 -maxdepth 1 \
                   -name 'mmc*' -printf '%f\n' 2>/dev/null | head -n1)"
    fi

    reset_ok=false
    if [[ -n "${sdio_dev}" ]]; then
      log "  SDIO unbind ${sdio_dev}"
      if _timeout_write 20 /sys/bus/sdio/drivers/brcmfmac/unbind "${sdio_dev}" 2>/dev/null; then
        sleep 2
        log "  SDIO bind   ${sdio_dev}"
        if _timeout_write 20 /sys/bus/sdio/drivers/brcmfmac/bind "${sdio_dev}" 2>/dev/null; then
          reset_ok=true
        else
          log "  SDIO bind failed/timed out for ${sdio_dev}"
        fi
      else
        log "  SDIO unbind failed/timed out for ${sdio_dev} — will try modprobe"
      fi
    else
      log "  no SDIO brcmfmac device found — will try modprobe"
    fi

    # Fallback: full module reload. Will only succeed if the link-down
    # above was enough to release the module; otherwise it's a no-op
    # and we'll escalate to tier 6 / 10 on the next ticks. Both calls
    # are time-capped so a stuck modprobe can't stall the tick.
    if ! ${reset_ok}; then
      timeout 30 modprobe -r brcmfmac 2>&1 | logger -t "${LOG_TAG}" || true
      sleep 2
      timeout 30 modprobe   brcmfmac 2>&1 | logger -t "${LOG_TAG}" || true
    fi

    # Bring NM's connection back so it can re-associate. NM will
    # often auto-reconnect when wlan0 reappears, but an explicit
    # `connection up` cuts the recovery window noticeably.
    sleep 2
    nmcli -t -f NAME,DEVICE connection show 2>/dev/null \
      | awk -F: '$2=="wlan0" {print $1}' \
      | while read -r conn; do
          [[ -n "${conn}" ]] || continue
          log "  nmcli connection up   '${conn}'"
          nmcli connection up "${conn}" >/dev/null 2>&1 || true
        done
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
      # Prefer the safe-reboot path (quiesces the USB gadget so the
      # car's TeslaCAM write is not corrupted). Fall back to a plain
      # reboot if the script is missing. `exec` so we don't return.
      if [[ -x "${SAFE_REBOOT_SCRIPT}" ]]; then
        exec "${SAFE_REBOOT_SCRIPT}"
      fi
      systemctl reboot
    fi
    ;;
esac

write_state "${fail_count}" "${healthy_ticks}"
exit 0
