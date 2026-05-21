#!/usr/bin/env bash
# uninstall-lib/05-network.sh — reverses setup-lib/05-network.sh.
#
# Remove the three files we wrote under /etc/NetworkManager/. Do NOT
# touch any other NetworkManager connection profile — the operator's
# primary STA WiFi (often the only path to the device) must survive.
# Do NOT restart NetworkManager: dropping the AP files on disk is
# sufficient; the operator picks up the change on next nmcli reload.
# Do NOT un-mask dhcpcd — leave the netstack handoff in place.

# shellcheck source=../setup-lib/00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/00-common.sh"
# shellcheck source=../setup-lib/05-network.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/05-network.sh"

b1_undo_05() {
  local target
  for target in "${B1_NETWORK_TARGETS[@]}"; do
    if [[ -e "${target}" ]]; then
      b1_log "  rm: ${target}"
      b1_run rm -f -- "${target}"
    else
      b1_log "  already absent: ${target}"
    fi
  done
  b1_log "  AP files removed; nmcli connection reload deferred to operator"
  return 0
}
