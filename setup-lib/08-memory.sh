#!/usr/bin/env bash
# setup-lib/08-memory.sh — Phase 6.8
#
# Memory & VM tuning. Mirrors v1's `optimize_memory_for_setup` minus
# the lightdm-disable (which lives in Phase 6.9). Two concerns:
#
#   1) A 1 GiB persistent swap file at /var/swap/b1.swap with a matching
#      /etc/fstab entry so it survives reboot. The Pi Zero 2 W ships
#      with 512 MB RAM and B-1's gunicorn + nbd-client + teslafat
#      working set occasionally peaks well above that during setup +
#      first archive run. The 1 GiB size is a deliberate decision
#      captured in the B1_SWAP_SIZE_BYTES constant — do not shrink
#      without operator approval.
#
#   2) A sysctl drop-in at /etc/sysctl.d/90-teslausb-b1.conf that pins
#      vm.swappiness=10 (prefer RAM, only swap under real pressure),
#      vm.min_free_kbytes=8192 (keep 8 MiB free so the OOM killer
#      doesn't fire while NetworkManager is mid-AP-bringup), and
#      kernel.panic=10 (auto-reboot 10 s after a kernel panic — the
#      headless USB target has no console operator).
#
# Idempotency contract: re-running 6.8 on an already-tuned device is
# a no-op. Every probe runs even under TESLAUSB_DRY_RUN=1 so the
# operator sees exactly what WOULD change.
#
# Dry-run contract: every mutation flows through `b1_run`; no
# fallocate / mkswap / swapon / fstab edit happens when dry-run is
# set. `sysctl --system` likewise is logged-only in dry-run.
#
# Safety rails (charter-mandated, see docs/03-CODE-QUALITY-CHARTER.md):
#   * NEVER edit /etc/sysctl.conf directly — drop-ins only.
#   * `b1_backup /etc/fstab` BEFORE any fstab append. Idempotent.
#   * If swap creation fails, abort BEFORE writing the fstab entry —
#     a fstab line pointing at a missing swapfile sends the next boot
#     into emergency mode.
#   * No `sudo` (setup.sh is already root). No `reboot`. No `swapoff`
#     of pre-existing swaps the operator may rely on.

# Re-source common in case this file is invoked via --only.
# shellcheck source=00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/00-common.sh"

# --------------------------------------------------------------------
# Constants — exported so 6.11 (uninstall.sh) can reuse them verbatim.
# --------------------------------------------------------------------

B1_SWAP_FILE="/var/swap/b1.swap"
B1_SWAP_SIZE_BYTES=1073741824   # 1 GiB — see header for sizing rationale.
B1_SYSCTL_CONF="/etc/sysctl.d/90-teslausb-b1.conf"
B1_FSTAB="/etc/fstab"
export B1_SWAP_FILE B1_SWAP_SIZE_BYTES B1_SYSCTL_CONF B1_FSTAB

# fstab line we own. Matched verbatim (anchored) so we never duplicate.
B1_FSTAB_LINE="${B1_SWAP_FILE} none swap sw 0 0"

# --------------------------------------------------------------------
# sysctl drop-in body (constant at file scope so reviewers + the 6.11
# uninstaller can read it without executing the script).
# --------------------------------------------------------------------

read -r -d '' B1_SYSCTL_BODY <<'SYSCTL' || true
# Phase 6.8 — TeslaUSB B-1 VM and memory tuning
# Managed by setup-lib/08-memory.sh; see docs/00-PLAN.md row 6.8.
vm.swappiness = 10
vm.min_free_kbytes = 8192
kernel.panic = 10
SYSCTL

# --------------------------------------------------------------------
# Helpers (private to this step)
# --------------------------------------------------------------------

# _b1_sha256 <path> — sha256 hex of <path>, empty if missing.
_b1_sha256() {
  local path="$1"
  if [[ ! -e "${path}" ]]; then
    printf ''
    return 0
  fi
  sha256sum -- "${path}" 2>/dev/null | awk '{print $1}'
}

# _b1_sha256_stdin — sha256 hex of stdin.
_b1_sha256_stdin() {
  sha256sum | awk '{print $1}'
}

# _b1_swap_file_size <path> — print byte size of file, or empty if
# the file is missing. Pure read; safe under dry-run.
_b1_swap_file_size() {
  local path="$1"
  if [[ ! -e "${path}" ]]; then
    printf ''
    return 0
  fi
  stat -c %s -- "${path}" 2>/dev/null || printf ''
}

# _b1_swap_active <path> — true iff `swapon --show` lists <path>.
_b1_swap_active() {
  local path="$1"
  if ! command -v swapon >/dev/null 2>&1; then
    return 1
  fi
  swapon --show=NAME --noheadings 2>/dev/null \
    | awk -v p="${path}" '$1 == p { found=1 } END { exit found ? 0 : 1 }'
}

# _b1_fstab_has_swap — true iff /etc/fstab already references our swap.
_b1_fstab_has_swap() {
  [[ -r "${B1_FSTAB}" ]] && grep -q "^${B1_SWAP_FILE}[[:space:]]" "${B1_FSTAB}"
}

# --------------------------------------------------------------------
# Sub-step: 1 GiB persistent swap file.
# --------------------------------------------------------------------

_b1_ensure_swap() {
  local size_now active fstab_ok=0
  size_now="$(_b1_swap_file_size "${B1_SWAP_FILE}")"
  if _b1_swap_active "${B1_SWAP_FILE}"; then active=1; else active=0; fi
  if _b1_fstab_has_swap; then fstab_ok=1; fi

  # Fully-converged short-circuit.
  if [[ "${size_now}" == "${B1_SWAP_SIZE_BYTES}" ]] \
     && (( active == 1 )) && (( fstab_ok == 1 )); then
    b1_log "swap ok: ${B1_SWAP_FILE} size=${size_now} active=yes fstab=yes"
    return 0
  fi

  b1_log "swap status: file_size=${size_now:-<missing>} want=${B1_SWAP_SIZE_BYTES} active=${active} fstab=${fstab_ok}"

  # 1) Ensure parent dir.
  if [[ ! -d "$(dirname "${B1_SWAP_FILE}")" ]]; then
    b1_run mkdir -p -- "$(dirname "${B1_SWAP_FILE}")"
  fi

  # 2) (Re)create the swap file iff size is wrong / missing.
  if [[ "${size_now}" != "${B1_SWAP_SIZE_BYTES}" ]]; then
    # If a wrong-sized swap file pre-exists AND it is currently active,
    # refuse to silently swapoff — the operator may be relying on it.
    # Log loudly and abort BEFORE touching fstab or the file.
    if [[ -n "${size_now}" ]] && _b1_swap_active "${B1_SWAP_FILE}"; then
      b1_err "${B1_SWAP_FILE} is active at the wrong size (${size_now} bytes, want ${B1_SWAP_SIZE_BYTES})."
      b1_err "  Refusing to swapoff a live swap. Operator must \`swapoff ${B1_SWAP_FILE}\` and re-run."
      return 1
    fi
    if [[ -e "${B1_SWAP_FILE}" ]]; then
      b1_log "removing wrong-sized swap file: ${B1_SWAP_FILE} (${size_now} bytes)"
      b1_run rm -f -- "${B1_SWAP_FILE}"
    fi

    if command -v fallocate >/dev/null 2>&1; then
      b1_log "creating ${B1_SWAP_FILE} via fallocate -l 1G"
      if ! b1_run fallocate -l 1G -- "${B1_SWAP_FILE}"; then
        b1_err "fallocate failed — out of disk? Aborting BEFORE fstab edit."
        return 1
      fi
    else
      # fallocate ships with coreutils on Pi OS; this is a defensive fallback.
      b1_warn "fallocate not found — falling back to dd (slow)"
      if ! b1_run dd if=/dev/zero of="${B1_SWAP_FILE}" bs=1M count=1024 status=none; then
        b1_err "dd failed — out of disk? Aborting BEFORE fstab edit."
        # Clean up partial file so a retry starts fresh.
        b1_run rm -f -- "${B1_SWAP_FILE}"
        return 1
      fi
    fi

    b1_run chmod 0600 -- "${B1_SWAP_FILE}"
    if ! b1_run mkswap -- "${B1_SWAP_FILE}"; then
      b1_err "mkswap failed on ${B1_SWAP_FILE}. Aborting BEFORE fstab edit."
      return 1
    fi
  else
    # File size is correct — make sure mode is 0600 (mkswap refuses
    # readable-by-world files; this also matches what we'd write fresh).
    local mode_now
    mode_now="$(stat -c %a -- "${B1_SWAP_FILE}" 2>/dev/null || echo '')"
    if [[ "${mode_now}" != "600" ]]; then
      b1_log "swap file mode is ${mode_now:-<unknown>}, fixing to 600"
      b1_run chmod 0600 -- "${B1_SWAP_FILE}"
    fi
  fi

  # 3) Activate (only if not already active). We never swapoff.
  if ! _b1_swap_active "${B1_SWAP_FILE}"; then
    if ! b1_run swapon -- "${B1_SWAP_FILE}"; then
      b1_err "swapon failed on ${B1_SWAP_FILE}. Aborting BEFORE fstab edit."
      return 1
    fi
  else
    b1_log "swap already active: ${B1_SWAP_FILE}"
  fi

  # 4) fstab entry (idempotent). Backup BEFORE first edit.
  if _b1_fstab_has_swap; then
    b1_log "fstab already has ${B1_SWAP_FILE} entry — leaving alone"
  else
    b1_log "appending fstab entry: ${B1_FSTAB_LINE}"
    b1_backup "${B1_FSTAB}"
    if [[ "${TESLAUSB_DRY_RUN:-0}" == "1" ]]; then
      b1_log "DRY-RUN: append to ${B1_FSTAB}: ${B1_FSTAB_LINE}"
    else
      printf '%s\n' "${B1_FSTAB_LINE}" >> "${B1_FSTAB}"
    fi
  fi
}

# --------------------------------------------------------------------
# Sub-step: sysctl drop-in.
# --------------------------------------------------------------------

_b1_ensure_sysctl() {
  local want_sum have_sum
  want_sum="$(printf '%s' "${B1_SYSCTL_BODY}" | _b1_sha256_stdin)"
  have_sum="$(_b1_sha256 "${B1_SYSCTL_CONF}")"

  if [[ -n "${have_sum}" && "${want_sum}" == "${have_sum}" ]]; then
    b1_log "sysctl unchanged: ${B1_SYSCTL_CONF} (sha256=${have_sum:0:12}…)"
    # Make sure mode/owner are right even when contents match (cheap).
    local mode_now owner_now
    mode_now="$(stat -c %a -- "${B1_SYSCTL_CONF}" 2>/dev/null || echo '')"
    owner_now="$(stat -c %U:%G -- "${B1_SYSCTL_CONF}" 2>/dev/null || echo '')"
    if [[ "${mode_now}" != "644" || "${owner_now}" != "root:root" ]]; then
      b1_log "fixing sysctl perms: mode=${mode_now} owner=${owner_now} → 644 root:root"
      b1_run chmod 0644 -- "${B1_SYSCTL_CONF}"
      b1_run chown root:root -- "${B1_SYSCTL_CONF}"
    fi
    return 0
  fi

  if [[ -e "${B1_SYSCTL_CONF}" ]]; then
    b1_log "sysctl differs: ${B1_SYSCTL_CONF} (target=${have_sum:0:12}…, want=${want_sum:0:12}…) — backing up"
    b1_backup "${B1_SYSCTL_CONF}"
  else
    b1_log "sysctl new: ${B1_SYSCTL_CONF} (sha256=${want_sum:0:12}…)"
  fi

  if [[ "${TESLAUSB_DRY_RUN:-0}" == "1" ]]; then
    b1_log "DRY-RUN: write ${B1_SYSCTL_CONF} (mode 0644 root:root, $(printf '%s' "${B1_SYSCTL_BODY}" | wc -c) bytes)"
    b1_log "DRY-RUN: sysctl --system"
    return 0
  fi

  # install -m 0644 -o root -g root from stdin (avoids ever staging
  # under /tmp; charter forbids /tmp writes).
  printf '%s' "${B1_SYSCTL_BODY}" \
    | install -o root -g root -m 0644 /dev/stdin "${B1_SYSCTL_CONF}"

  b1_run sysctl --system >/dev/null
}

# --------------------------------------------------------------------
# Step entry point
# --------------------------------------------------------------------

b1_step_08() {
  _b1_ensure_swap || return 1
  _b1_ensure_sysctl || return 1
  return 0
}
