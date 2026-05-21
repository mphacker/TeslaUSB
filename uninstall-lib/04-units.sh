#!/usr/bin/env bash
# uninstall-lib/04-units.sh — reverses setup-lib/04-units.sh.
#
# Stop + disable + remove every unit file from B1_UNIT_TARGETS, plus
# the nginx site drop-in (sites-enabled symlink + sites-available
# file). daemon-reload once at end if anything changed. nginx -s reload
# if nginx is still running. Does NOT apt-purge nginx — that's owned
# by undo-01 under --purge.

# shellcheck source=../setup-lib/00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/00-common.sh"
# shellcheck source=../setup-lib/04-units.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/04-units.sh"

b1_undo_04() {
  local changed=0
  local target unit
  # 1) units: stop, disable, rm.
  for target in "${B1_UNIT_TARGETS[@]}"; do
    unit="$(basename "${target}")"
    # teslafat@.service is templated — stop any active instances too.
    if [[ "${unit}" == *@.service ]]; then
      local instance
      while read -r instance; do
        [[ -z "${instance}" ]] && continue
        b1_log "  stop+disable instance: ${instance}"
        b1_run systemctl stop "${instance}" 2>/dev/null || true
        b1_run systemctl disable "${instance}" 2>/dev/null || true
      done < <(systemctl list-units --type=service --no-legend --plain 2>/dev/null \
                 | awk -v u="${unit%@.service}@" '$1 ~ "^"u { print $1 }')
    elif b1_unit_exists "${unit}"; then
      if b1_unit_active "${unit}"; then
        b1_log "  stop: ${unit}"
        b1_run systemctl stop "${unit}" || b1_warn "    stop returned non-zero"
      fi
      if b1_unit_enabled "${unit}"; then
        b1_log "  disable: ${unit}"
        b1_run systemctl disable "${unit}" || b1_warn "    disable returned non-zero"
      fi
    fi
    if [[ -e "${target}" ]]; then
      b1_log "  rm unit file: ${target}"
      b1_run rm -f -- "${target}"
      changed=1
    fi
  done

  # 2) nginx site drop-in (enabled symlink + available file).
  if [[ -L "${B1_NGINX_SITE_ENABLED}" || -e "${B1_NGINX_SITE_ENABLED}" ]]; then
    b1_log "  rm nginx site enabled: ${B1_NGINX_SITE_ENABLED}"
    b1_run rm -f -- "${B1_NGINX_SITE_ENABLED}"
    changed=1
  fi
  if [[ -e "${B1_NGINX_SITE_AVAIL}" ]]; then
    b1_log "  rm nginx site avail: ${B1_NGINX_SITE_AVAIL}"
    b1_run rm -f -- "${B1_NGINX_SITE_AVAIL}"
    changed=1
  fi

  # 3) daemon-reload + nginx reload (only if nginx is still up).
  if (( changed == 1 )); then
    b1_run systemctl daemon-reload
    if b1_unit_active nginx; then
      b1_log "  nginx still running — nginx -s reload"
      b1_run nginx -s reload 2>/dev/null || b1_warn "  nginx -s reload returned non-zero"
    fi
  fi
  return 0
}
