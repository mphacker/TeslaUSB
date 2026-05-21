#!/usr/bin/env bash
# uninstall-lib/02-users.sh — reverses setup-lib/02-users.sh.
#
# Always: remove the sudoers fragment. Validate FIRST that the
# REMAINING sudoers set still parses (visudo -cf on /etc/sudoers and
# every other /etc/sudoers.d/* file) — a broken sudoers wedges sudo
# system-wide for the operator. Only after validation do we rm.
#
# Without --purge: leave the teslausb user/group + pi's membership
# alone (they're harmless and removing pi's supplementary group can
# strand sessions). With --purge: userdel -r teslausb iff (a) it
# exists, (b) no running processes own it. NEVER touches `pi`.
#
# Wipes /var/lib/teslausb-b1 only under --purge.

# shellcheck source=../setup-lib/00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/00-common.sh"
# shellcheck source=../setup-lib/02-users.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/02-users.sh"

_b1_02_validate_remaining_sudoers() {
  # Concatenate /etc/sudoers + every /etc/sudoers.d/* EXCEPT the file
  # we're about to delete, then pipe through visudo -cf -. A broken
  # remainder aborts uninstall — better to keep the fragment than to
  # wedge sudo.
  local target="$1"
  if [[ ! -x "${B1_VISUDO_BIN}" ]]; then
    b1_warn "  ${B1_VISUDO_BIN} not present — skipping pre-rm validation"
    return 0
  fi
  local tmp tmpdir
  tmpdir="$(dirname "${BASH_SOURCE[0]}")"
  tmp="${tmpdir}/.b1-undo-02-sudoers-$$.tmp"
  # shellcheck disable=SC2064
  trap "rm -f -- '${tmp}'" RETURN
  : > "${tmp}"
  [[ -r /etc/sudoers ]] && cat /etc/sudoers >> "${tmp}"
  local f
  shopt -s nullglob
  for f in /etc/sudoers.d/*; do
    [[ "${f}" == "${target}" ]] && continue
    [[ -r "${f}" ]] && cat "${f}" >> "${tmp}"
  done
  shopt -u nullglob
  if ! "${B1_VISUDO_BIN}" -cf "${tmp}" >/dev/null 2>&1; then
    b1_err "  remaining sudoers set fails visudo -cf — refusing to rm ${target}"
    return 1
  fi
  b1_log "  remaining sudoers validated by visudo -cf"
  return 0
}

b1_undo_02() {
  # 1) sudoers fragment.
  if [[ -e "${B1_SUDOERS_PATH}" ]]; then
    _b1_02_validate_remaining_sudoers "${B1_SUDOERS_PATH}" || return 1
    b1_log "  rm: ${B1_SUDOERS_PATH}"
    b1_run rm -f -- "${B1_SUDOERS_PATH}"
  else
    b1_log "  sudoers fragment already absent: ${B1_SUDOERS_PATH}"
  fi

  if (( ${B1_PURGE:-0} != 1 )); then
    b1_log "  user/group kept (no --purge): ${B1_TESLAUSB_USER}/${B1_TESLAUSB_GROUP}"
    return 0
  fi

  # 2) --purge path: delete teslausb user (and its home), then group.
  # Defence-in-depth guard: NEVER touch pi.
  if [[ "${B1_TESLAUSB_USER}" == "pi" || "${B1_TESLAUSB_GROUP}" == "pi" ]]; then
    b1_err "  refusing to userdel: B1_TESLAUSB_USER/GROUP resolved to 'pi'"
    return 1
  fi

  if getent passwd "${B1_TESLAUSB_USER}" >/dev/null 2>&1; then
    # Refuse if any process is owned by the user. `pgrep -c` prints 0
    # AND returns non-zero when there are no matches — strip the
    # extra `echo 0` newline that `|| echo 0` would otherwise tack on.
    local running
    running="$(pgrep -u "${B1_TESLAUSB_USER}" -c 2>/dev/null | head -n1)"
    [[ -z "${running}" ]] && running="0"
    if [[ "${running}" != "0" ]]; then
      b1_err "  refusing to userdel ${B1_TESLAUSB_USER}: ${running} running process(es)"
      return 1
    fi
    b1_log "  userdel -r ${B1_TESLAUSB_USER}"
    b1_run userdel -r "${B1_TESLAUSB_USER}" || b1_warn "  userdel returned non-zero"
  else
    b1_log "  user already absent: ${B1_TESLAUSB_USER}"
  fi

  if getent group "${B1_TESLAUSB_GROUP}" >/dev/null 2>&1; then
    b1_log "  groupdel ${B1_TESLAUSB_GROUP}"
    b1_run groupdel "${B1_TESLAUSB_GROUP}" || b1_warn "  groupdel returned non-zero"
  fi

  # 3) Wipe /var/lib/teslausb-b1 (purely ours).
  local b1_state="/var/lib/teslausb-b1"
  if [[ -d "${b1_state}" ]]; then
    b1_log "  rm -rf: ${b1_state}"
    b1_run rm -rf -- "${b1_state}"
  fi
  return 0
}
