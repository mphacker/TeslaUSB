#!/usr/bin/env bash
# setup-lib/01-packages.sh — Phase 6.1
#
# Installs the apt packages that B-1 needs at runtime. Per
# `docs/00-PLAN.md` row 6.1 the list is:
#
#   nbd-client nginx python3-venv network-manager
#   watchdog dnsmasq-base hostapd
#
# FORBIDDEN by ADR-0008: rustup, cargo, gcc, build-essential.
# Building Rust on a Pi Zero 2 W is forbidden — the device runs
# cross-compiled binaries only. Two prior H1 attempts to `cargo
# build` on-device wedged the Pi.
#
# Idempotency: b1_pkg_install (see 00-common.sh) skips packages
# already in state `ii`. apt-get update fires at most once per hour
# AND only if at least one install is actually needed.
#
# Dry-run: every mutation goes through b1_run / b1_run_quiet, so
# nothing is touched when TESLAUSB_DRY_RUN=1.

# Re-source common in case this file is invoked via --only.
# shellcheck source=00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/00-common.sh"

# Apt names — kept in a constant so 6.11 (uninstall.sh) can reuse it
# verbatim when --purge is requested. Add a package here, and the
# inverse step picks it up automatically.
B1_RUNTIME_PACKAGES=(
  nbd-client       # client-side NBD (Rust teslafat daemon ships server-side)
  nginx            # reverse proxy in front of gunicorn (6.10)
  python3-venv     # Phase 5 web app venv
  network-manager  # NetworkManager + nmcli used by captive_portal
  watchdog         # hardware watchdog daemon (6.7)
  dnsmasq-base     # AP-mode DHCP/DNS (6.5)
  hostapd          # AP-mode WiFi (6.5)
)
export B1_RUNTIME_PACKAGES

b1_step_01() {
  # Forbidden-by-ADR-0008 sanity guard. If anyone ever appends a
  # build toolchain to B1_RUNTIME_PACKAGES we want a loud failure,
  # not a silent on-device cargo build attempt.
  local forbidden=(rustup cargo gcc build-essential clang llvm)
  local pkg bad=()
  for pkg in "${B1_RUNTIME_PACKAGES[@]}"; do
    for f in "${forbidden[@]}"; do
      if [[ "${pkg}" == "${f}" ]]; then bad+=("${pkg}"); fi
    done
  done
  if (( ${#bad[@]} > 0 )); then
    b1_err "ADR-0008 violation: refusing to install build toolchain on device: ${bad[*]}"
    return 1
  fi

  # apt + dpkg presence (skipped under dry-run so we can still preview
  # on a dev machine without apt).
  if [[ "${TESLAUSB_DRY_RUN:-0}" != "1" ]]; then
    if ! command -v apt-get >/dev/null 2>&1 || ! command -v dpkg-query >/dev/null 2>&1; then
      b1_err "apt-get / dpkg-query not found — this script targets Debian/Raspberry Pi OS"
      return 1
    fi
  fi

  b1_pkg_install "${B1_RUNTIME_PACKAGES[@]}"
}
