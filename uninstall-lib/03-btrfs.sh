#!/usr/bin/env bash
# uninstall-lib/03-btrfs.sh — reverses setup-lib/03-btrfs.sh.
#
# NOOP without --purge (operator data is sacred — the whole point of
# Phase 6.3 is that we never auto-delete subvolumes).
#
# With --purge:
#   * For each B1_BTRFS_SUBVOLS entry under B1_BTRFS_ROOT:
#       - Refuse if `btrfs subvolume list -s <path>` reports any
#         snapshots (sacred data + chained backup chains).
#       - Otherwise `btrfs subvolume delete` it.
#   * The parent B1_BTRFS_ROOT directory itself is left alone — it's
#     usually a mount point owned by the operator.

# shellcheck source=../setup-lib/00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/00-common.sh"
# shellcheck source=../setup-lib/03-btrfs.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/03-btrfs.sh"

b1_undo_03() {
  if (( ${B1_PURGE:-0} != 1 )); then
    b1_log "  subvolumes kept (no --purge): ${B1_BTRFS_SUBVOLS[*]} under ${B1_BTRFS_ROOT}"
    return 0
  fi

  if ! command -v btrfs >/dev/null 2>&1; then
    b1_warn "  btrfs cmd not available — skipping subvolume purge"
    return 0
  fi

  local name path
  for name in "${B1_BTRFS_SUBVOLS[@]}"; do
    path="${B1_BTRFS_ROOT}/${name}"
    if [[ ! -e "${path}" ]]; then
      b1_log "  absent: ${path}"
      continue
    fi
    if ! btrfs subvolume show "${path}" >/dev/null 2>&1; then
      b1_warn "  ${path} is not a btrfs subvolume — refusing to delete"
      continue
    fi
    # Refuse if any snapshot references this subvolume.
    local snap_count
    snap_count="$(btrfs subvolume list -s "${path}" 2>/dev/null | wc -l | tr -d ' ')"
    if [[ "${snap_count}" != "0" ]]; then
      b1_err "  refusing to delete ${path}: ${snap_count} snapshot(s) reference it"
      return 1
    fi
    b1_log "  btrfs subvolume delete: ${path}"
    b1_run btrfs subvolume delete -- "${path}"
  done
  return 0
}
