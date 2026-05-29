#!/usr/bin/env bash
# /usr/local/sbin/wifi-safe-reboot.sh
#
# Installed by setup-lib/12-wifi-stability.sh.
#
# PURPOSE
# -------
# Quiesce the USB mass-storage gadget, then reboot. This is the
# LAST-RESORT recovery for a HARD BCM43436 SDIO wedge — the failure
# class where brcmf_sdio_dataworker parks in uninterruptible D-state
# holding the SDIO host mutex. In that state the chip cannot be reset
# in software (unbind/bind blocks on the same mutex; WL_REG_ON is
# behind the VideoCore GPIO expander and is not Linux-toggleable;
# rfkill is not exposed). A SoC reboot is the only cure.
#
# It is invoked two ways, both from the WiFi watchdog stack:
#   1) `exec`'d directly when wifi-watchdog.sh reaches tier 10, and
#   2) by the INDEPENDENT systemd-run dead-man that wifi-watchdog.sh
#      arms on entering tier 4 — so even if the watchdog process is
#      itself frozen in D-state, this still runs and recovers us.
#
# WHY "SAFE"
# ----------
# Doing nothing leaves WiFi dead until the CAR eventually power-cuts
# the Pi — an uncontrolled power loss, the most dangerous outcome for
# the exFAT image and any in-flight clip. This script instead:
#   1) waits (briefly, bounded) for a TeslaCAM write-idle gap,
#   2) presents the car a CLEAN eject (unbind the dwc2 UDC — which is
#      independent of the wedged WiFi SDIO bus, so it works even mid-
#      wedge; the car finalizes its current clip on eject instead of
#      having it truncated by the shutdown teardown),
#   3) flushes the page cache / NBD writes to the backing store, then
#   4) reboots via the ordered systemd teardown, with hard fallbacks
#      so a stuck systemd can never strand the device offline.
#
# USB mass-storage to the car is independent of WiFi; the only data at
# risk is at most the single ~1-minute clip being written at the
# instant of eject, and steps 1–2 minimize even that.

set -u

readonly LOG_TAG="wifi-safe-reboot"
log() { logger -t "${LOG_TAG}" -- "$*"; printf '%s %s\n' "${LOG_TAG}" "$*" >&2; }

readonly HIDE_USB="/usr/local/bin/teslausb-hide-usb"

# Backing store the car writes into. Matches the teslausb-watch
# default (setup-lib/11-gadget.sh); the TeslaCam tree lives beneath
# it. Used only for best-effort write-idle detection.
readonly BACKING_ROOT="/srv/teslausb/teslacam"

# Best-effort write-idle gap. WiFi has already been dead for minutes
# by the time we get here, so the bound is deliberately short:
# recovering the device matters more than waiting for a perfect gap
# that may never come (e.g. active Sentry recording).
readonly WRITE_IDLE_DEADLINE_S=30
readonly WRITE_IDLE_WINDOW_S=4
readonly WRITE_IDLE_POLL_S=2

# writes_recent — true (0) if any file under BACKING_ROOT changed
# within the last WRITE_IDLE_WINDOW_S seconds. `-quit` stops at the
# first hit so this stays cheap on a large tree. Fails OPEN (treats
# errors as "no recent writes") so detection never blocks the reboot.
writes_recent() {
  local mins
  mins="$(awk -v s="${WRITE_IDLE_WINDOW_S}" 'BEGIN { printf "%f", s / 60 }')"
  [[ -n "$(find "${BACKING_ROOT}" -type f -mmin "-${mins}" -print -quit 2>/dev/null)" ]]
}

wait_for_write_idle() {
  if [[ ! -d "${BACKING_ROOT}" ]]; then
    log "backing root ${BACKING_ROOT} absent — skipping write-idle wait"
    return 0
  fi
  local waited=0
  while (( waited < WRITE_IDLE_DEADLINE_S )); do
    if ! writes_recent; then
      log "TeslaCAM write-idle (no writes in ${WRITE_IDLE_WINDOW_S}s) after ${waited}s"
      return 0
    fi
    sleep "${WRITE_IDLE_POLL_S}"
    waited=$(( waited + WRITE_IDLE_POLL_S ))
  done
  log "WARN: no write-idle gap within ${WRITE_IDLE_DEADLINE_S}s (car likely recording) — proceeding anyway"
  return 0
}

log "safe reboot requested — quiescing USB gadget before reboot"

# 1) Wait (bounded) for a write-idle gap.
wait_for_write_idle

# 2) Present the car a clean eject. The dwc2 UDC is unrelated to the
#    wedged WiFi SDIO controller (mmc1), so this succeeds even during
#    a hard WiFi wedge.
if [[ -x "${HIDE_USB}" ]]; then
  log "hiding USB (clean eject) via ${HIDE_USB}"
  timeout 15 "${HIDE_USB}" 2>&1 | logger -t "${LOG_TAG}" \
    || log "WARN: hide-usb failed/timed out"
else
  log "WARN: ${HIDE_USB} not found — skipping clean eject"
fi

# 3) Flush page cache / NBD writes through to the backing store.
log "sync"
timeout 30 sync || log "WARN: sync timed out"
sleep 1
timeout 30 sync || true

# 4) Reboot. `systemctl reboot` runs the ordered unit teardown
#    (usb-gadget ExecStop → nbd detach → teslafat SIGTERM clean flush)
#    as a second safety layer. Fall back to /sbin/reboot, then to the
#    kernel sysrq path, so a stuck systemd can never strand the device
#    offline — by this point the eject + sync above have already left
#    the image consistent.
log "rebooting now"
if ! timeout 30 systemctl reboot; then
  log "WARN: systemctl reboot did not take — trying /sbin/reboot"
  if ! timeout 15 /sbin/reboot; then
    log "WARN: /sbin/reboot failed — forcing reboot via sysrq"
    echo b > /proc/sysrq-trigger 2>/dev/null || true
  fi
fi
