#!/usr/bin/env bash
# setup-lib/00-common.sh — shared helpers for every Phase 6 step.
#
# Sourced by setup.sh and re-sourced by each step file. Every public
# helper is prefixed `b1_` so it cannot collide with apt/systemctl
# function names.
#
# Idempotency contract: every helper that mutates state checks the
# observable post-state first and is a no-op if it already matches.
#
# Dry-run contract: TESLAUSB_DRY_RUN=1 makes `b1_run` log-and-skip
# instead of executing. Idempotency checks themselves (pure reads)
# always run so the dry-run can accurately report what WOULD happen.

# Guard against double-source (steps source this themselves so they
# work when run via --only).
if [[ -n "${B1_COMMON_LOADED:-}" ]]; then
  return 0
fi
B1_COMMON_LOADED=1

# --------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------

b1_log() {
  # ISO-8601 timestamp + message → stderr (so stdout stays clean for
  # any caller that wants to capture command output).
  printf '%s setup.sh: %s\n' "$(date -Is)" "$*" >&2
}

b1_warn() { b1_log "WARN: $*"; }
b1_err()  { b1_log "ERROR: $*"; }

# --------------------------------------------------------------------
# Dry-run-aware command runner
# --------------------------------------------------------------------

# b1_run <cmd...>  — executes the command unless TESLAUSB_DRY_RUN=1.
# In dry-run mode logs the command verbatim and returns 0.
b1_run() {
  if [[ "${TESLAUSB_DRY_RUN:-0}" == "1" ]]; then
    b1_log "DRY-RUN: $*"
    return 0
  fi
  "$@"
}

# b1_run_quiet — same but stdout/stderr piped through b1_log.
b1_run_quiet() {
  if [[ "${TESLAUSB_DRY_RUN:-0}" == "1" ]]; then
    b1_log "DRY-RUN: $*"
    return 0
  fi
  "$@" 2>&1 | while IFS= read -r line; do b1_log "  $line"; done
  # PIPESTATUS[0] holds the exit code of the leftmost piped command.
  return "${PIPESTATUS[0]}"
}

# --------------------------------------------------------------------
# Backups
# --------------------------------------------------------------------

# b1_backup <path>  — copy <path> to <path>.b1-backup-<ISO> the FIRST
# time it is called for that path; subsequent calls are no-ops so
# rerunning setup.sh doesn't pile up backups.
b1_backup() {
  local target="$1"
  if [[ ! -e "${target}" ]]; then
    b1_log "backup skipped (missing): ${target}"
    return 0
  fi
  # Look for an existing .b1-backup-* sibling.
  if compgen -G "${target}.b1-backup-*" > /dev/null; then
    b1_log "backup exists for ${target} — keeping original"
    return 0
  fi
  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  b1_run cp -a "${target}" "${target}.b1-backup-${stamp}"
}

# --------------------------------------------------------------------
# Package management (apt-get)
# --------------------------------------------------------------------

# b1_pkg_installed <name>  — true if dpkg believes the package is
# installed AND configured (status starts with "ii").
b1_pkg_installed() {
  local pkg="$1"
  local status
  status="$(dpkg-query -W -f='${db:Status-Abbrev}' "${pkg}" 2>/dev/null || true)"
  [[ "${status}" == "ii" || "${status}" == "ii " ]]
}

# b1_pkg_install <pkg...>  — install each listed package, skipping any
# that are already installed. Updates the apt cache only if at least
# one install is needed AND the cache is older than 1 hour (avoids
# slamming archive.raspberrypi.com on every setup re-run).
b1_pkg_install() {
  local needed=()
  for pkg in "$@"; do
    if b1_pkg_installed "${pkg}"; then
      b1_log "package present: ${pkg}"
    else
      needed+=("${pkg}")
    fi
  done
  if (( ${#needed[@]} == 0 )); then
    b1_log "all packages already installed"
    return 0
  fi
  b1_log "packages to install: ${needed[*]}"

  # Refresh apt cache if older than 1h.
  local cache="/var/cache/apt/pkgcache.bin"
  local stale=1
  if [[ -f "${cache}" ]]; then
    local age now mtime
    now="$(date +%s)"
    mtime="$(stat -c %Y "${cache}" 2>/dev/null || echo 0)"
    age=$(( now - mtime ))
    if (( age < 3600 )); then
      stale=0
    fi
  fi
  if (( stale )); then
    b1_log "apt cache stale → apt-get update"
    b1_run_quiet apt-get update -q
  else
    b1_log "apt cache fresh, skipping update"
  fi

  # DEBIAN_FRONTEND=noninteractive prevents tzdata-style prompts
  # during unattended installs.
  b1_run_quiet env DEBIAN_FRONTEND=noninteractive \
    apt-get install -y --no-install-recommends "${needed[@]}"
}

# --------------------------------------------------------------------
# systemd helpers (used by 6.4/6.7/6.9/6.10)
# --------------------------------------------------------------------

b1_unit_exists() {
  systemctl list-unit-files "$1" --no-legend 2>/dev/null | grep -q "$1"
}

b1_unit_enabled() {
  [[ "$(systemctl is-enabled "$1" 2>/dev/null || echo unknown)" == "enabled" ]]
}

b1_unit_active() {
  systemctl is-active --quiet "$1"
}

# --------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------

# b1_is_pi  — true on a Raspberry Pi (per device-tree model string).
b1_is_pi() {
  [[ -r /proc/device-tree/model ]] && grep -qi 'raspberry pi' /proc/device-tree/model
}

# Export public surface for child shells (some apt hooks fork).
export -f b1_log b1_warn b1_err b1_run b1_run_quiet b1_backup \
          b1_pkg_installed b1_pkg_install b1_unit_exists \
          b1_unit_enabled b1_unit_active b1_is_pi 2>/dev/null || true
