#!/usr/bin/env bash
# uninstall-lib/12-wifi-stability.sh — reverses setup-lib/12-wifi-stability.sh.
#
# Removes the BCM43436 SDIO WiFi stability stack. The seven files
# laid down by 6.12 are all OURS (no operator data), so we remove
# them unconditionally with or without --purge.
#
# Order: stop the timer + service first so the watchdog can't fire
# during teardown, then remove the unit files + script + modprobe /
# NM drop-ins, then daemon-reload.
#
# We deliberately do NOT:
#   - Reload brcmfmac (would yank wlan0 out from under the operator).
#   - Restart NetworkManager (same hazard; the powersave drop-in
#     only affects NEW connections so removing it is a passive op).
#   - Touch any /run/teslausb/* state files — those are tmpfs and
#     vanish on reboot.

# shellcheck source=../setup-lib/00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/00-common.sh"
# shellcheck source=../setup-lib/12-wifi-stability.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/12-wifi-stability.sh"

_b1_stop_disable_12() {
  local unit="$1"
  if ! systemctl list-unit-files "${unit}" --no-pager 2>/dev/null | grep -q .; then
    return 0
  fi
  b1_run systemctl stop "${unit}" || true
  b1_run systemctl disable "${unit}" || true
}

_b1_rm_if_exists_12() {
  local path="$1"
  if [[ -e "${path}" ]]; then
    b1_log "  rm ${path}"
    b1_run rm -f -- "${path}"
  fi
}

b1_undo_12() {
  b1_log "stopping wifi-watchdog timer + service"
  _b1_stop_disable_12 wifi-watchdog.timer
  _b1_stop_disable_12 wifi-watchdog.service

  b1_log "removing wifi-stability artifacts"
  _b1_rm_if_exists_12 "${B1_WIFI_WATCHDOG_TIMER}"
  _b1_rm_if_exists_12 "${B1_WIFI_WATCHDOG_UNIT}"
  _b1_rm_if_exists_12 "${B1_WIFI_WATCHDOG_SCRIPT}"
  _b1_rm_if_exists_12 "${B1_WIFI_SAFE_REBOOT_SCRIPT}"
  _b1_rm_if_exists_12 "${B1_BRCMFMAC_CONF}"
  _b1_rm_if_exists_12 "${B1_NM_POWERSAVE_CONF}"
  _b1_rm_if_exists_12 "${B1_NM_WIFI_CHURN_CONF}"

  if [[ "${TESLAUSB_DRY_RUN:-0}" != "1" ]]; then
    b1_run systemctl daemon-reload
  else
    b1_log "DRY-RUN: systemctl daemon-reload"
  fi

  b1_log "note: brcmfmac module options remain active until next reboot;"
  b1_log "      NM powersave drop-in removal affects NEW connections only."
}
