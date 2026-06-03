#!/usr/bin/env bash
# uninstall-lib/13-recording-watchdog.sh — reverses setup-lib/13-recording-watchdog.sh.
#
# Removes the recording-liveness watchdog stack. The three files laid
# down by 6.13 are all OURS (no operator data), so we remove them
# unconditionally with or without --purge.
#
# Order: stop the timer + service first so the watchdog can't fire
# (and re-present the gadget) during teardown, then remove the unit
# files + script, then daemon-reload.
#
# We deliberately do NOT touch /run/teslausb/* state files — those are
# tmpfs and vanish on reboot.

# shellcheck source=../setup-lib/00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/00-common.sh"
# shellcheck source=../setup-lib/13-recording-watchdog.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/13-recording-watchdog.sh"

_b1_stop_disable_13() {
  local unit="$1"
  if ! systemctl list-unit-files "${unit}" --no-pager 2>/dev/null | grep -q .; then
    return 0
  fi
  b1_run systemctl stop "${unit}" || true
  b1_run systemctl disable "${unit}" || true
}

_b1_rm_if_exists_13() {
  local path="$1"
  if [[ -e "${path}" ]]; then
    b1_log "  rm ${path}"
    b1_run rm -f -- "${path}"
  fi
}

b1_undo_13() {
  b1_log "stopping recording-watchdog timer + service"
  _b1_stop_disable_13 recording-watchdog.timer
  _b1_stop_disable_13 recording-watchdog.service

  b1_log "removing recording-watchdog artifacts"
  _b1_rm_if_exists_13 "${B1_REC_WATCHDOG_TIMER}"
  _b1_rm_if_exists_13 "${B1_REC_WATCHDOG_UNIT}"
  _b1_rm_if_exists_13 "${B1_REC_WATCHDOG_SCRIPT}"

  if [[ "${TESLAUSB_DRY_RUN:-0}" != "1" ]]; then
    b1_run systemctl daemon-reload
  else
    b1_log "DRY-RUN: systemctl daemon-reload"
  fi
}
