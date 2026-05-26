#!/usr/bin/env bash
# uninstall-lib/01-packages.sh — reverses setup-lib/01-packages.sh.
#
# NOOP regardless of --purge: every package in B1_RUNTIME_PACKAGES
# is also used by stock Pi OS / v1, so removing them risks breaking
# the operator's other workflows.
#
# With --purge: nothing to purge for B-1 itself. The setup script
# previously installed btrfs-progs (used for an aborted btrfs
# subvolume design); that package is no longer requested, and the
# safe-purge list is empty.
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

# Conservative allow-list. If a future setup step ever installs a
# package that v1 / Pi OS Lite definitely did NOT ship, add it here
# along with the rationale in the header comment.
B1_SAFE_PURGE=()

b1_undo_01() {
  if (( ${B1_PURGE:-0} != 1 )); then
    b1_log "  packages kept (no --purge): ${B1_RUNTIME_PACKAGES[*]}"
    return 0
  fi

  if (( ${#B1_SAFE_PURGE[@]} == 0 )); then
    b1_log "  no B-1-specific packages to purge"
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
