#!/usr/bin/env bash
# uninstall-lib/11-gadget.sh — reverses setup-lib/11-gadget.sh.
#
# Stops + disables the gadget pipeline units and removes the files
# that 6.11 installed. With or without --purge: the files installed
# by setup-lib/11-gadget.sh are all OURS (no operator data), so we
# remove them unconditionally.
#
# Order matters: we tear DOWN in reverse of build (gadget first so
# Tesla "ejects" cleanly, then nbd-attach, then teslafat). Each
# step is idempotent and tolerates missing units.

# shellcheck source=../setup-lib/00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/00-common.sh"
# shellcheck source=../setup-lib/11-gadget.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/11-gadget.sh"

_b1_stop_disable() {
  local unit="$1"
  if ! systemctl list-unit-files "${unit}" --no-pager 2>/dev/null | grep -q .; then
    return 0
  fi
  b1_run systemctl stop "${unit}" || true
  b1_run systemctl disable "${unit}" || true
}

_b1_rm_if_exists() {
  local path="$1"
  if [[ -e "${path}" ]]; then
    b1_log "  rm ${path}"
    b1_run rm -f -- "${path}"
  fi
}

b1_undo_11() {
  # 1. Stop + disable in reverse dependency order.
  _b1_stop_disable usb-gadget.service
  _b1_stop_disable nbd-attach@0.service
  _b1_stop_disable nbd-attach@1.service
  # teslafat@N stays under 6.4's ownership; we leave it.

  # 2. Best-effort teardown of any still-composed gadget (in case
  #    usb-gadget.service didn't run ExecStop cleanly).
  if [[ -x /usr/local/bin/teslausb-gadget-down ]]; then
    b1_run /usr/local/bin/teslausb-gadget-down || true
  fi

  # 3. Remove installed files.
  local f
  for f in "${B1_GADGET_TARGETS[@]}"; do
    _b1_rm_if_exists "${f}"
  done

  # 4. Reload systemd so removed units stop being listed.
  if [[ "${TESLAUSB_DRY_RUN:-0}" != "1" ]]; then
    b1_run systemctl daemon-reload
  else
    b1_log "DRY-RUN: systemctl daemon-reload"
  fi

  b1_log "  gadget pipeline uninstalled (teslafat@ left under 6.4 ownership)"
  return 0
}
