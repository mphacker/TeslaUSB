#!/usr/bin/env bash
# setup-lib/13-recording-watchdog.sh — Phase 6.13
#
# Lays down the recording-liveness watchdog: a 30 s systemd timer +
# oneshot service that detects unambiguous USB/NBD fault signatures and
# AUTOMATICALLY re-presents the USB gadget so the Tesla re-mounts and
# resumes recording — WITHOUT ever rebooting. This is the software
# guard for the #1 invariant (the car must ALWAYS be able to write
# TeslaCam), motivated by the 2026-06-02 23 h recording gap.
#
# Files installed:
#   1) /usr/local/sbin/recording-watchdog.sh
#      The fault-detection + bounded re-present escalation script.
#   2) /etc/systemd/system/recording-watchdog.service
#   3) /etc/systemd/system/recording-watchdog.timer
#      systemd timer (30 s tick) + oneshot service wrapping (1).
#
# The re-present primitive itself (scripts/tesla_gadget_rebind.sh, with
# the --skip-media-reload flag) is installed by setup-lib/04-units.sh —
# this step depends on it being present at /usr/local/bin but does not
# re-install it.
#
# Like the rest of Phase 6, the file-install portion of this step is
# idempotent. Activation, however, is done HERE (not deferred to Phase
# 6.10): the activate step runs earlier in the numeric step order, so on
# a fresh single-pass install it would silently skip a timer that does
# not yet exist. The recording-watchdog timer has no network side effects
# (unlike the wifi-stability timer), so enabling+starting it inline is
# safe and ensures the guard is live after one setup.sh run. The watchdog
# runs as root (no User= in the unit) so it can invoke the
# configfs-writing re-present primitive directly; no sudoers grant is
# required.
#
# Source artifacts live under `deploy/recording-stability/` next to the
# repo root so reviewers (and the 6.13 uninstaller) can read them
# without executing this script.
#
# Idempotency: sha256(source) vs sha256(on-disk) per file. No changes →
# no daemon-reload. First overwrite of any pre-existing target spawns
# one `b1_backup` sibling; subsequent runs do not pile up backups.

# shellcheck source=00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/00-common.sh"

# Repo root + asset directory.
B1_REPO_ROOT_13="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
B1_REC_STAB_SRC="${B1_REPO_ROOT_13}/deploy/recording-stability"

# --------------------------------------------------------------------
# Install targets — exported so 6.13 (uninstall.sh) can reverse this.
# --------------------------------------------------------------------

B1_REC_WATCHDOG_SCRIPT="/usr/local/sbin/recording-watchdog.sh"
export B1_REC_WATCHDOG_SCRIPT

B1_REC_WATCHDOG_UNIT="/etc/systemd/system/recording-watchdog.service"
export B1_REC_WATCHDOG_UNIT

B1_REC_WATCHDOG_TIMER="/etc/systemd/system/recording-watchdog.timer"
export B1_REC_WATCHDOG_TIMER

# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------

_b1_13_sha256() {
  local path="$1"
  [[ -e "${path}" ]] || { printf ''; return 0; }
  sha256sum -- "${path}" 2>/dev/null | awk '{print $1}'
}

# _b1_13_install_file <src> <dst> <mode> [<state-var>]
_b1_13_install_file() {
  local src="$1"
  local dst="$2"
  local mode="$3"
  local state_var="${4:-}"

  if [[ ! -f "${src}" ]]; then
    b1_err "missing source artifact: ${src}"
    return 1
  fi

  local src_sum dst_sum
  src_sum="$(_b1_13_sha256 "${src}")"
  dst_sum="$(_b1_13_sha256 "${dst}")"

  if [[ -n "${dst_sum}" && "${src_sum}" == "${dst_sum}" ]]; then
    b1_log "unchanged: ${dst} (sha256=${dst_sum:0:12}…)"
    return 0
  fi

  if [[ -e "${dst}" ]]; then
    b1_log "differs: ${dst} (target=${dst_sum:0:12}…, source=${src_sum:0:12}…) — backing up"
    b1_backup "${dst}"
  else
    b1_log "new: ${dst} (sha256=${src_sum:0:12}…)"
  fi

  b1_run install -m "${mode}" -- "${src}" "${dst}"

  if [[ -n "${state_var}" ]]; then
    printf -v "${state_var}" '%s' 1
  fi
}

# --------------------------------------------------------------------
# Step entry point
# --------------------------------------------------------------------

b1_step_13() {
  if [[ ! -d "${B1_REC_STAB_SRC}" ]]; then
    b1_err "asset directory missing: ${B1_REC_STAB_SRC}"
    return 1
  fi

  local changed=""

  # 1) recording-watchdog script (executable).
  b1_run mkdir -p -- "$(dirname "${B1_REC_WATCHDOG_SCRIPT}")"
  _b1_13_install_file \
    "${B1_REC_STAB_SRC}/recording-watchdog.sh" \
    "${B1_REC_WATCHDOG_SCRIPT}" \
    0755 \
    changed

  # 2) recording-watchdog.service.
  _b1_13_install_file \
    "${B1_REC_STAB_SRC}/recording-watchdog.service" \
    "${B1_REC_WATCHDOG_UNIT}" \
    0644 \
    changed

  # 3) recording-watchdog.timer.
  _b1_13_install_file \
    "${B1_REC_STAB_SRC}/recording-watchdog.timer" \
    "${B1_REC_WATCHDOG_TIMER}" \
    0644 \
    changed

  # daemon-reload only on actual change. The recording-watchdog timer is
  # ACTIVATED here (not deferred to 6.10): the activate step runs BEFORE
  # this one in the numeric step order, so on a fresh single-pass
  # setup.sh it cannot enable a timer that does not yet exist. Unlike the
  # wifi-stability timer — which 6.10 must own because starting it churns
  # wlan0 and can drop the operator's SSH session — this timer has NO
  # network impact, so it is safe to enable+start inline and guarantees
  # the #1-invariant guard is live after one run. Idempotent.
  if [[ -n "${changed}" ]]; then
    b1_log "recording-watchdog artifacts changed — running daemon-reload"
    b1_run systemctl daemon-reload
  else
    b1_log "no recording-watchdog artifacts changed — skipping daemon-reload"
  fi

  if b1_unit_exists recording-watchdog.timer; then
    if b1_unit_enabled recording-watchdog.timer; then
      b1_log "already enabled: recording-watchdog.timer"
    else
      b1_log "enable: recording-watchdog.timer"
      b1_run systemctl enable recording-watchdog.timer
    fi
    if b1_unit_active recording-watchdog.timer; then
      b1_log "already active: recording-watchdog.timer"
    else
      b1_log "start: recording-watchdog.timer"
      b1_run systemctl start recording-watchdog.timer
    fi
  else
    b1_err "recording-watchdog.timer not found after install — cannot activate"
    return 1
  fi

  return 0
}
