#!/usr/bin/env bash
# uninstall-lib/08-memory.sh — reverses setup-lib/08-memory.sh.
#
# Default (no --purge):
#   * rm /etc/sysctl.d/90-teslausb-b1.conf
#   * sysctl --system (reload remaining drop-ins)
#   * leave the swap file ALONE (operator may be relying on its 1 GiB
#     of pressure relief while other steps are still uninstalling).
#
# With --purge:
#   * swapoff /var/swap/b1.swap (only if active — never our v1 sibling)
#   * remove the exact /etc/fstab line we appended
#   * rm /var/swap/b1.swap
# NEVER touches /var/swap/fsck.swap (v1's swap).

# shellcheck source=../setup-lib/00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/00-common.sh"
# shellcheck source=../setup-lib/08-memory.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/08-memory.sh"

b1_undo_08() {
  # 1) sysctl drop-in (always removed — it's purely ours).
  if [[ -e "${B1_SYSCTL_CONF}" ]]; then
    b1_log "  rm: ${B1_SYSCTL_CONF}"
    b1_run rm -f -- "${B1_SYSCTL_CONF}"
    b1_run sysctl --system >/dev/null 2>&1 || b1_warn "  sysctl --system returned non-zero"
  else
    b1_log "  sysctl drop-in already absent: ${B1_SYSCTL_CONF}"
  fi

  # 2) Swap — only with --purge.
  if (( ${B1_PURGE:-0} != 1 )); then
    b1_log "  swap kept (no --purge): ${B1_SWAP_FILE}"
    return 0
  fi

  # Refuse to touch v1's swap, ever.
  if [[ "${B1_SWAP_FILE}" != "/var/swap/b1.swap" ]]; then
    b1_err "  refusing to swap-purge non-b1 path: ${B1_SWAP_FILE}"
    return 1
  fi

  # swapoff if active.
  if command -v swapon >/dev/null 2>&1 \
       && swapon --show=NAME --noheadings 2>/dev/null \
            | awk -v p="${B1_SWAP_FILE}" '$1==p{f=1} END{exit f?0:1}'; then
    b1_log "  swapoff: ${B1_SWAP_FILE}"
    b1_run swapoff -- "${B1_SWAP_FILE}" || b1_warn "  swapoff returned non-zero"
  fi

  # Remove our fstab line (precise match — never sed-on-pattern).
  if grep -Fxq -- "${B1_FSTAB_LINE}" "${B1_FSTAB}" 2>/dev/null; then
    b1_backup "${B1_FSTAB}"
    b1_log "  removing fstab line: ${B1_FSTAB_LINE}"
    if [[ "${TESLAUSB_DRY_RUN:-0}" == "1" ]]; then
      b1_log "DRY-RUN: remove ${B1_FSTAB_LINE} from ${B1_FSTAB}"
    else
      grep -Fxv -- "${B1_FSTAB_LINE}" "${B1_FSTAB}" \
        | install -o root -g root -m 0644 /dev/stdin "${B1_FSTAB}"
    fi
  else
    b1_log "  fstab line already absent: ${B1_FSTAB_LINE}"
  fi

  # rm the swap file itself.
  if [[ -e "${B1_SWAP_FILE}" ]]; then
    b1_log "  rm: ${B1_SWAP_FILE}"
    b1_run rm -f -- "${B1_SWAP_FILE}"
  fi
  return 0
}
