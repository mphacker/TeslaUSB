#!/usr/bin/env bash
# setup-lib/09-mask-services.sh — Phase 6.9
#
# Masks the desktop / printing / modem services that ship enabled on a
# stock Raspberry Pi OS image but are dead weight on a headless B-1
# install. Reclaims ~30–50 MB RAM on a Pi Zero 2 W (mirrors v1's
# desktop-services-disable block in `setup_usb.sh`).
#
# === avahi-daemon — INTENTIONALLY NOT MASKED ===
#
# The PLAN row 6.9 left avahi "decide at impl time". The decision is:
# **avahi-daemon STAYS ENABLED.** The Pi is reached on the operator's
# LAN via `cybertruckusb.local` (mDNS). Masking avahi-daemon would
# break:
#   * `ssh pi@cybertruckusb.local` (every operator workflow + the
#     hardware-test skill's safety wrapper)
#   * the web UI URL `http://cybertruckusb.local/` printed in the
#     captive-portal handoff (Phase 5)
#   * mDNS discovery from phones / laptops on the same Wi-Fi
# avahi-daemon is therefore explicitly absent from B1_UNITS_TO_MASK
# AND explicitly present in B1_MASK_DENYLIST (defence in depth).
#
# === Two-stage reversibility ===
#
# Per PLAN we DISABLE first then MASK. That ordering matters: a
# masked-but-still-enabled unit confuses operators reading
# `systemctl list-unit-files`; disabling first leaves a clean
# audit trail. To re-enable a unit later:
#     systemctl unmask <unit> && systemctl enable <unit>
# We deliberately DO NOT `systemctl stop` running units in this
# step — masking + reboot is the canonical reversal path, and
# stopping mid-installer risks racing other 6.x steps (e.g. 6.5
# NetworkManager). Operational stop/start of any non-B-1 service
# is out of scope for setup.sh.
#
# === Safety rails ===
#
#   * Allow-list only. We mask exactly what is in B1_UNITS_TO_MASK.
#     No globs, no fuzzy matching, no "while-read-from-systemctl".
#   * Deny-list cross-check. Every candidate is asserted against
#     B1_MASK_DENYLIST before any state change — defence in depth
#     in case someone edits B1_UNITS_TO_MASK in a hurry.
#   * Conditional on existence. Pi Zero 2 W Lite images typically
#     ship without lightdm / pipewire / cups; "absent: skip" is the
#     expected path on a fresh device and is fully idempotent.
#   * Re-mask-safe. If `systemctl is-enabled` already returns
#     `masked`, the unit is left alone.
#
# Idempotency: every probe (`b1_unit_exists`, `systemctl is-enabled`)
# is a pure read and runs even under dry-run, so a `--dry-run --only 09`
# accurately reports what WOULD change. Every mutation routes through
# `b1_run`.

# Re-source common in case this file is invoked via --only.
# shellcheck source=00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/00-common.sh"

# --------------------------------------------------------------------
# Constants — exported so 6.11 (uninstall.sh) can reuse them verbatim
# when reversing the mask (unmask + enable, in mirror order).
# --------------------------------------------------------------------

# Units to mask. Each entry will be:
#   - checked for existence (b1_unit_exists)
#   - if exists and not already masked: disabled then masked
#   - if already masked: no-op
# avahi-daemon is intentionally NOT in this list — see file header.
B1_UNITS_TO_MASK=(
  lightdm.service
  pipewire.service
  pipewire.socket
  wireplumber.service
  colord.service
  cups.service
  cups.socket
  cups-browsed.service
  triggerhappy.service
  triggerhappy.socket
  ModemManager.service
)
export B1_UNITS_TO_MASK

# Absolute don't-mask names. Any future edit to B1_UNITS_TO_MASK that
# accidentally introduces one of these will abort the step loudly
# instead of bricking SSH / NetworkManager / mDNS discovery.
# (Also catches typos like `ssh.service` vs `sshd.service`.)
B1_MASK_DENYLIST=(
  ssh.service
  sshd.service
  NetworkManager.service
  avahi-daemon.service
)
export B1_MASK_DENYLIST

# --------------------------------------------------------------------
# Helpers (private to this step)
# --------------------------------------------------------------------

# _b1_denied <unit>  — true if <unit> is in B1_MASK_DENYLIST OR matches
# a structural prefix we never mask (systemd-*, teslausb-*, teslafat*).
# Prefix rules are encoded here rather than in B1_MASK_DENYLIST because
# they cover *families* of units, not specific names.
_b1_denied() {
  local unit="$1"
  local d
  for d in "${B1_MASK_DENYLIST[@]}"; do
    if [[ "${unit}" == "${d}" ]]; then
      return 0
    fi
  done
  case "${unit}" in
    systemd-*|teslausb-*|teslafat*)
      return 0
      ;;
  esac
  return 1
}

# _b1_is_masked <unit>  — true if `systemctl is-enabled` returns
# exactly `masked`. Pure read; runs in dry-run too.
_b1_is_masked() {
  [[ "$(systemctl is-enabled "$1" 2>/dev/null || echo unknown)" == "masked" ]]
}

# _b1_mask_one <unit>  — full per-unit pipeline: deny-list check,
# existence check, already-masked short-circuit, disable + mask.
_b1_mask_one() {
  local unit="$1"

  # Defence in depth: even though B1_UNITS_TO_MASK is hardcoded above,
  # re-check every candidate against the deny-list at the point of use.
  if _b1_denied "${unit}"; then
    b1_err "refusing to mask deny-listed unit: ${unit}"
    return 1
  fi

  if ! b1_unit_exists "${unit}"; then
    b1_log "unit absent: skip ${unit}"
    return 0
  fi

  if _b1_is_masked "${unit}"; then
    b1_log "already masked: ${unit}"
    return 0
  fi

  # Some units (notably *.socket entries activated by .service deps)
  # have no [Install] section, so `disable` returns non-zero with a
  # harmless "not enabled" message. We tolerate that — `mask` is the
  # state we actually care about.
  b1_log "disable + mask: ${unit}"
  b1_run systemctl disable "${unit}" 2>/dev/null || true
  b1_run systemctl mask "${unit}"
}

# --------------------------------------------------------------------
# Step entry point
# --------------------------------------------------------------------

b1_step_09() {
  # Pre-flight: sanity-check the allow-list against the deny-list
  # ONCE before touching anything. A bad edit to B1_UNITS_TO_MASK
  # should abort before we mutate even one unit.
  local unit bad=()
  for unit in "${B1_UNITS_TO_MASK[@]}"; do
    if _b1_denied "${unit}"; then
      bad+=("${unit}")
    fi
  done
  if (( ${#bad[@]} > 0 )); then
    b1_err "B1_UNITS_TO_MASK contains deny-listed entries: ${bad[*]}"
    b1_err "  (B1_MASK_DENYLIST=${B1_MASK_DENYLIST[*]})"
    return 1
  fi

  for unit in "${B1_UNITS_TO_MASK[@]}"; do
    _b1_mask_one "${unit}" || return 1
  done

  return 0
}
