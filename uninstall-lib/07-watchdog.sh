#!/usr/bin/env bash
# uninstall-lib/07-watchdog.sh — reverses setup-lib/07-watchdog.sh.
#
# Remove the two systemd drop-ins (watchdog priority boost + ssh
# protect), daemon-reload, and restore /etc/watchdog.conf from its
# `.b1-backup-<ISO>` sibling iff one exists (i.e. v1 had its own
# config; we should not strand the operator with our minimal one).

# shellcheck source=../setup-lib/00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/00-common.sh"
# shellcheck source=../setup-lib/07-watchdog.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/07-watchdog.sh"

_b1_07_restore_from_backup() {
  local target="$1"
  # Pick the newest .b1-backup-* sibling (lex sort works because the
  # stamp is ISO-8601 fixed-width).
  local backup
  backup="$(printf '%s\n' "${target}.b1-backup-"* 2>/dev/null \
            | LC_ALL=C sort | tail -n1)"
  if [[ -z "${backup}" || ! -e "${backup}" ]]; then
    return 1
  fi
  b1_log "  restoring ${target} <- ${backup}"
  b1_run cp -a -- "${backup}" "${target}"
  return 0
}

b1_undo_07() {
  local changed=0
  local override
  for override in "${B1_WATCHDOG_OVERRIDE}" "${B1_SSH_OVERRIDE_PATH}"; do
    if [[ -e "${override}" ]]; then
      b1_log "  rm: ${override}"
      b1_run rm -f -- "${override}"
      # Try to rmdir the .d/ shell if it's now empty (purely cosmetic;
      # ignore failure — operator might have other drop-ins in there).
      b1_run rmdir --ignore-fail-on-non-empty -- "$(dirname "${override}")" 2>/dev/null || true
      changed=1
    else
      b1_log "  already absent: ${override}"
    fi
  done

  # Restore /etc/watchdog.conf from backup if present; otherwise leave
  # the file alone — the operator may have edited it post-install.
  if [[ -e "${B1_WATCHDOG_CONF}" ]]; then
    if _b1_07_restore_from_backup "${B1_WATCHDOG_CONF}"; then
      changed=1
    else
      b1_log "  no backup for ${B1_WATCHDOG_CONF} — leaving B-1's config in place"
    fi
  fi

  if (( changed == 1 )); then
    b1_run systemctl daemon-reload
  fi
  return 0
}
