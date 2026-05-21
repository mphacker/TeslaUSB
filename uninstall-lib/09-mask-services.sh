#!/usr/bin/env bash
# uninstall-lib/09-mask-services.sh — reverses setup-lib/09-mask-services.sh.
#
# `systemctl unmask` every unit in B1_UNITS_TO_MASK. We deliberately do
# NOT `systemctl enable` them: they were masked because we don't want
# them running; leaving them in the disabled-but-unmasked state restores
# the choice to the operator (and matches stock Pi OS Lite behaviour
# for units that ship absent).

# shellcheck source=../setup-lib/00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/00-common.sh"
# shellcheck source=../setup-lib/09-mask-services.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/09-mask-services.sh"

b1_undo_09() {
  local u state
  for u in "${B1_UNITS_TO_MASK[@]}"; do
    if ! b1_unit_exists "${u}"; then
      b1_log "  absent: ${u}"
      continue
    fi
    # is-enabled returns non-zero AND prints "masked" for masked units;
    # the `|| echo unknown` would then APPEND a second line. Take only
    # the first line so the comparison below is exact.
    state="$(systemctl is-enabled "${u}" 2>/dev/null | head -n1)"
    [[ -z "${state}" ]] && state="unknown"
    if [[ "${state}" != "masked" ]]; then
      b1_log "  not masked (${state}): ${u} — leaving alone"
      continue
    fi
    b1_log "  unmask: ${u}"
    b1_run systemctl unmask "${u}"
  done
  return 0
}
