#!/usr/bin/env bash
# uninstall-lib/10-activate.sh — reverses setup-lib/10-*.sh (Phase 6.10).
#
# Phase 6.10 is the "final enable + start" step. Inverse: stop+disable
# every unit it enabled. We source the 10-*.sh setup file if present
# to pick up `B1_ENABLE_UNITS`; if it isn't there yet (sibling has not
# landed 6.10), we fall back to the canonical list derived from the
# 6.4 unit-install constants + 6.4's nginx site enablement.
#
# Idempotent: skips units that aren't active / aren't enabled. Never
# touches a unit it didn't enable. Honours TESLAUSB_DRY_RUN via b1_run.

# shellcheck source=../setup-lib/00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/00-common.sh"

b1_undo_10() {
  # Pick up B1_ENABLE_UNITS from setup-lib/10-*.sh if it shipped.
  local setup10
  setup10="$(printf '%s\n' "$(dirname "${BASH_SOURCE[0]}")/../setup-lib"/10-*.sh 2>/dev/null | head -n1)"
  if [[ -n "${setup10}" && -r "${setup10}" ]]; then
    # shellcheck source=/dev/null
    source "${setup10}"
  fi

  local units=()
  if [[ -n "${B1_ENABLE_UNITS+x}" ]] && (( ${#B1_ENABLE_UNITS[@]} > 0 )); then
    units=("${B1_ENABLE_UNITS[@]}")
  else
    # Canonical fallback: every unit 6.4 lays down + nginx (6.4 also
    # installs the nginx site). teslafat is templated — Phase 6.10
    # enables only @0; mirror that here.
    units=(
      nginx.service
      teslausb-web.service
      teslausb-worker.service
      'teslafat@0.service'
    )
  fi
  b1_log "undo 10: stop+disable ${#units[@]} unit(s): ${units[*]}"

  local u
  for u in "${units[@]}"; do
    if ! b1_unit_exists "${u}"; then
      b1_log "  absent: ${u}"
      continue
    fi
    if b1_unit_active "${u}"; then
      b1_log "  stop: ${u}"
      b1_run systemctl stop "${u}" || b1_warn "    stop returned non-zero for ${u}"
    else
      b1_log "  not active: ${u}"
    fi
    if b1_unit_enabled "${u}"; then
      b1_log "  disable: ${u}"
      b1_run systemctl disable "${u}" || b1_warn "    disable returned non-zero for ${u}"
    else
      b1_log "  not enabled: ${u}"
    fi
  done
  return 0
}
