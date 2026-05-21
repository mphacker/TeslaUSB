#!/usr/bin/env bash
# uninstall-lib/01-packages.sh — reverses setup-lib/01-packages.sh.
#
# NOOP without --purge (every package in B1_RUNTIME_PACKAGES is also
# used by stock Pi OS / v1, with one exception: btrfs-progs).
#
# With --purge: apt-get purge ONLY packages on the safe-purge list.
# Everything else is on the deny-list because v1 (or stock Pi OS Lite)
# installs it and the operator may still depend on it after uninstall.
#
# SAFE PURGE LIST (apt-get purge with --purge):
#   * btrfs-progs   — only B-1 ever needed this on a stock Pi OS image
#
# DENY LIST (never apt-purged, even with --purge):
#   * nbd-client       — v1 used this for its loopback gadget path
#   * nginx            — operator-visible web stack; v1 had it too
#   * python3-venv     — base Python install on Pi OS
#   * network-manager  — Bookworm Pi OS uses NM by default; removing
#                        it would brick wlan0 mid-uninstall
#   * watchdog         — v1's last-resort safety rail
#   * dnsmasq-base     — used by NetworkManager's shared mode
#   * hostapd          — operator may use this outside B-1's AP

# shellcheck source=../setup-lib/00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/00-common.sh"
# shellcheck source=../setup-lib/01-packages.sh
source "$(dirname "${BASH_SOURCE[0]}")/../setup-lib/01-packages.sh"

# Conservative allow-list. If you add a package to B1_RUNTIME_PACKAGES
# that v1 / Pi OS Lite definitely did NOT ship, add it here and update
# the header comment with the rationale.
B1_SAFE_PURGE=(btrfs-progs)

b1_undo_01() {
  if (( ${B1_PURGE:-0} != 1 )); then
    b1_log "  packages kept (no --purge): ${B1_RUNTIME_PACKAGES[*]}"
    return 0
  fi

  if ! command -v apt-get >/dev/null 2>&1; then
    b1_warn "  apt-get not present — skipping package purge"
    return 0
  fi

  local pkg to_purge=()
  for pkg in "${B1_SAFE_PURGE[@]}"; do
    if b1_pkg_installed "${pkg}"; then
      to_purge+=("${pkg}")
    else
      b1_log "  already absent: ${pkg}"
    fi
  done
  if (( ${#to_purge[@]} == 0 )); then
    b1_log "  nothing to purge"
    return 0
  fi
  b1_log "  apt-get purge -y ${to_purge[*]}"
  b1_run_quiet env DEBIAN_FRONTEND=noninteractive apt-get purge -y "${to_purge[@]}"
  b1_run_quiet env DEBIAN_FRONTEND=noninteractive apt-get autoremove -y --purge
  return 0
}
