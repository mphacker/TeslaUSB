#!/usr/bin/env bash
# uninstall-lib/03-data-roots.sh — reverses setup-lib/03-data-roots.sh.
#
# NOOP without --purge (operator data is sacred — the whole point of
# Phase 6.3 is that we never auto-delete the per-LUN data trees).
#
# With --purge:
#   * For each B1_DATA_DIRS entry under B1_DATA_ROOT:
#       - If the path is a btrfs subvolume:
#           - Refuse if `btrfs subvolume list -s <path>` reports any
#             snapshots (chained backup chains are sacred).
#           - Otherwise `btrfs subvolume delete` it.
#       - If the path is a plain directory:
#           - `rm -rf` it. The operator asked for --purge; we honour it.
#   * The parent B1_DATA_ROOT directory itself is left alone — it's
#     usually a mount point owned by the operator.

# shellcheck source=../setup-lib/00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/00-common.sh"
# shellcheck source=../setup-lib/03-data-roots.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/03-data-roots.sh"

b1_undo_03() {
  if (( ${B1_PURGE:-0} != 1 )); then
    b1_log "  data roots kept (no --purge): ${B1_DATA_DIRS[*]} under ${B1_DATA_ROOT}"
    return 0
  fi

  local name path
  for name in "${B1_DATA_DIRS[@]}"; do
    path="${B1_DATA_ROOT}/${name}"
    if [[ ! -e "${path}" ]]; then
      b1_log "  absent: ${path}"
      continue
    fi

    # btrfs subvolume branch
    if command -v btrfs >/dev/null 2>&1 && btrfs subvolume show "${path}" >/dev/null 2>&1; then
      local snap_count
      snap_count="$(btrfs subvolume list -s "${path}" 2>/dev/null | wc -l | tr -d ' ')"
      if [[ "${snap_count}" != "0" ]]; then
        b1_err "  refusing to delete ${path}: ${snap_count} snapshot(s) reference it"
        return 1
      fi
      b1_log "  btrfs subvolume delete: ${path}"
      b1_run btrfs subvolume delete -- "${path}"
      continue
    fi

    # Plain directory branch
    if [[ -d "${path}" ]]; then
      b1_log "  rm -rf data root (plain dir): ${path}"
      b1_run rm -rf -- "${path}"
      continue
    fi

    b1_warn "  ${path} is neither a btrfs subvolume nor a plain directory — skipping"
  done
  return 0
}
