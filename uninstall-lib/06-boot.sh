#!/usr/bin/env bash
# uninstall-lib/06-boot.sh — reverses setup-lib/06-boot.sh.
#
# Restore /boot/firmware/cmdline.txt and /boot/firmware/config.txt
# (or /boot/* on Buster) from their `.b1-backup-<ISO>` siblings if
# present. If no backup exists for a file we leave it alone (the
# operator may have edited it post-install; we never blindly delete
# boot-firmware content).
#
# Always prints a reboot-required notice if anything was restored.

# shellcheck source=../setup-lib/00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/00-common.sh"
# shellcheck source=../setup-lib/06-boot.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/06-boot.sh"

_b1_06_restore() {
  local target="$1"
  local backup
  backup="$(printf '%s\n' "${target}.b1-backup-"* 2>/dev/null \
            | LC_ALL=C sort | tail -n1)"
  if [[ -z "${backup}" || ! -e "${backup}" ]]; then
    b1_log "  no backup for ${target} — leaving file unchanged"
    return 1
  fi
  b1_log "  restoring ${target} <- ${backup}"
  b1_run cp -a -- "${backup}" "${target}"
  return 0
}

b1_undo_06() {
  # Resolve actual boot paths first (helper from setup-lib/06-boot.sh).
  if ! _b1_resolve_boot_paths; then
    b1_log "  no boot-firmware files found — skipping (not a Pi)"
    return 0
  fi

  local restored=0
  if _b1_06_restore "${B1_BOOT_CMDLINE}"; then restored=1; fi
  if _b1_06_restore "${B1_BOOT_CONFIG}";  then restored=1; fi

  if (( restored == 1 )); then
    b1_warn "REBOOT REQUIRED to fully revert boot changes"
  fi
  return 0
}
