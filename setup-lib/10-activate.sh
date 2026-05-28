#!/usr/bin/env bash
# setup-lib/10-activate.sh — Phase 6.10
#
# Final activation step. Reloads systemd, enables every unit installed
# by Phases 6.1/6.4/6.7, starts the stack in dependency order, then
# runs a post-start health check. This is the ONLY Phase 6 increment
# that flips units from "installed but inert" to "running".
#
# The 6.1–6.9 increments deliberately install files and leave services
# alone so the installer can lay down EVERY required piece (units,
# nginx site, NetworkManager profile, boot edits, watchdog config,
# memory tuning, service mask list) before a single dependent service
# is allowed to come up. 6.10 closes that loop.
#
# === Safety rails ===
#
#   * Refuses to run if 6.4 did not previously install
#     /etc/systemd/system/teslausb-web.service. Exits 4 (per setup.sh
#     "missing dependency or precondition") with a clear pointer at
#     the missing predecessor step.
#   * Refuses to proceed past an enable / start failure: prints the
#     failing unit's `systemctl status` head and the last minute of
#     `journalctl -u <unit>` then exits 5 so the operator can triage
#     without scrolling through unrelated installer chatter.
#   * Enables and starts ONLY units explicitly listed in
#     B1_ENABLE_UNITS / B1_START_ORDER. No globs, no fuzzy matching.
#   * Optional units (teslafat / cloud-archive / gadget-recovery) are
#     gated on `b1_unit_exists`; absence is logged and skipped, never
#     promoted to an error.
#   * Health check refuses on system state `maintenance` or `offline`
#     but accepts `degraded` — a v1-leftover unit in `failed` state
#     is the expected condition on the live Pi today and must not
#     block 6.10 completion.
#
# === Idempotency ===
#
# `b1_unit_enabled` / `b1_unit_active` are pure reads (run under
# dry-run too); a unit already in the target state short-circuits with
# an "already enabled" / "already active" log line and no mutation.
# Re-running `setup.sh --only 10` on a fully activated device is a
# no-op end-to-end (every enable + every start short-circuits, then
# the health check confirms the world is still up).
#
# === Dry-run ===
#
# Every mutation routes through `b1_run`. The health-check probes
# (`systemctl is-active`, `curl`, `systemctl is-system-running`) are
# dry-run-aware: under TESLAUSB_DRY_RUN=1 they print
# `DRY-RUN: would check <thing>` instead of executing — actually
# probing a live system mid-`--dry-run` would either confuse the
# operator (web app already up via dev gunicorn) or report failure
# for a stack we haven't actually started.

# Re-source common in case this file is invoked via --only.
# shellcheck source=00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/00-common.sh"

# --------------------------------------------------------------------
# Constants — exported so 6.11 (uninstall.sh) can reverse this step
# (disable + stop in reverse order).
# --------------------------------------------------------------------

# Units to `systemctl enable`. Optional units (teslafat / cloud-archive
# / gadget-recovery) are gated on b1_unit_exists at iteration time —
# their presence in this list does NOT imply they must exist on disk.
# NetworkManager is included for completeness; on Raspberry Pi OS
# Bookworm the package post-install already enables it, so this is a
# defensive re-enable (idempotent no-op via the b1_unit_enabled probe).
B1_ENABLE_UNITS=(
  teslausb-web.service
  teslausb-worker.service
  teslafat@0.service
  teslafat@1.service
  nbd-attach@0.service
  nbd-attach@1.service
  usb-gadget.service
  cloud-archive.service
  gadget-recovery.service
  nginx.service
  NetworkManager.service
  watchdog.service
  wifi-watchdog.timer
)
export B1_ENABLE_UNITS

# Units to `systemctl start`, in dependency order. NetworkManager must
# be up before nginx binds port 80; teslafat (block backend) must be
# up before nbd-attach (which connects to its socket); nbd-attach
# must be up before usb-gadget (which uses /dev/nbd{0,1} as LUN
# backings); usb-gadget LAST in the storage chain — once UDC is bound
# Tesla sees drives, so we want every layer below it healthy first.
# teslausb-web depends on the worker which depends on teslafat being
# attachable. watchdog goes last so its priority drop-in from 6.7
# doesn't starve anything still racing to start.
B1_START_ORDER=(
  NetworkManager.service
  nginx.service
  teslafat@0.service
  teslafat@1.service
  nbd-attach@0.service
  nbd-attach@1.service
  usb-gadget.service
  teslausb-worker.service
  teslausb-web.service
  cloud-archive.service
  gadget-recovery.service
  watchdog.service
  wifi-watchdog.timer
)
export B1_START_ORDER

# Post-start HTTP probe. nginx (Phase 6.4 site drop-in) terminates the
# operator-facing port 80 and reverse-proxies to gunicorn on
# /run/teslausb/gunicorn.sock; the `/` route returns 200 (or 302
# depending on auth/setup state) so either is acceptable evidence the
# stack is end-to-end alive.
B1_HEALTHCHECK_URL="http://127.0.0.1/"
export B1_HEALTHCHECK_URL

# Units the health check insists are `active`. teslafat / cloud-archive
# / gadget-recovery are intentionally NOT in this list because they
# are optional and may legitimately be absent on a given build.
B1_REQUIRED_ACTIVE=(
  teslausb-web.service
  nginx.service
  watchdog.service
)
export B1_REQUIRED_ACTIVE

# Predecessor file: if 6.4 didn't run, this won't exist and we abort.
B1_PRECONDITION_FILE="/etc/systemd/system/teslausb-web.service"

# --------------------------------------------------------------------
# Helpers (private to this step)
# --------------------------------------------------------------------

# _b1_dump_failure <unit>  — on enable/start failure, dump the unit's
# status head + the last minute of its journal so the operator has
# triage data inline with the installer log. Best-effort (commands
# that themselves fail are tolerated — we're already failing).
_b1_dump_failure() {
  local unit="$1"
  b1_err "=== systemctl status ${unit} (head) ==="
  systemctl status --no-pager --lines=0 "${unit}" 2>&1 | head -n 12 \
    | while IFS= read -r line; do b1_err "  ${line}"; done || true
  b1_err "=== journalctl -u ${unit} --since '1 min ago' (tail) ==="
  journalctl -u "${unit}" --since "1 min ago" --no-pager 2>&1 | tail -n 30 \
    | while IFS= read -r line; do b1_err "  ${line}"; done || true
}

# _b1_enable_one <unit>  — enable if (a) the unit exists and (b) it
# isn't already enabled. Optional units that don't exist are logged
# and skipped. Hard-fails (return 1) only on `systemctl enable` error.
_b1_enable_one() {
  local unit="$1"
  if ! b1_unit_exists "${unit}"; then
    b1_log "unit absent: skip enable ${unit}"
    return 0
  fi
  if b1_unit_enabled "${unit}"; then
    b1_log "already enabled: ${unit}"
    return 0
  fi
  b1_log "enable: ${unit}"
  if ! b1_run systemctl enable "${unit}"; then
    _b1_dump_failure "${unit}"
    return 1
  fi
}

# _b1_start_one <unit>  — start if (a) the unit exists and (b) it
# isn't already active. Same skip/fail contract as _b1_enable_one.
_b1_start_one() {
  local unit="$1"
  if ! b1_unit_exists "${unit}"; then
    b1_log "unit absent: skip start ${unit}"
    return 0
  fi
  if b1_unit_active "${unit}"; then
    b1_log "already active: ${unit}"
    return 0
  fi
  b1_log "start: ${unit}"
  if ! b1_run systemctl start "${unit}"; then
    _b1_dump_failure "${unit}"
    return 1
  fi
}

# _b1_check_active <unit...>  — assert every listed unit is `active`.
# Dry-run-aware: under TESLAUSB_DRY_RUN=1, logs the intended check and
# returns 0 without probing (probing a live device mid-dry-run would
# misreport, since we haven't actually started anything).
_b1_check_active() {
  if [[ "${TESLAUSB_DRY_RUN:-0}" == "1" ]]; then
    b1_log "DRY-RUN: would check active: $*"
    return 0
  fi
  local unit state
  for unit in "$@"; do
    state="$(systemctl is-active "${unit}" 2>/dev/null || true)"
    if [[ "${state}" != "active" ]]; then
      b1_err "health check FAILED: ${unit} state=${state} (want active)"
      _b1_dump_failure "${unit}"
      return 1
    fi
    b1_log "health: ${unit} active"
  done
}

# _b1_check_http  — curl the local health-check URL. 200 and 302 are
# both accepted (root may redirect to /setup or /login depending on
# state). Dry-run-aware.
_b1_check_http() {
  if [[ "${TESLAUSB_DRY_RUN:-0}" == "1" ]]; then
    b1_log "DRY-RUN: would check HTTP GET ${B1_HEALTHCHECK_URL}"
    return 0
  fi
  local code
  code="$(curl -fsS -o /dev/null -w '%{http_code}' \
    --max-time 5 "${B1_HEALTHCHECK_URL}" 2>/dev/null || echo 000)"
  case "${code}" in
    200|302)
      b1_log "health: HTTP ${code} on ${B1_HEALTHCHECK_URL}"
      return 0
      ;;
    *)
      b1_err "health check FAILED: HTTP ${code} on ${B1_HEALTHCHECK_URL} (want 200 or 302)"
      return 1
      ;;
  esac
}

# _b1_check_system  — systemctl is-system-running. `running` and
# `degraded` accepted (degraded is expected on the Pi during the
# B-1 transition while v1 leftovers may still report failed). Hard
# reject on `maintenance` / `offline`. Dry-run-aware. Echoes the
# observed state for the summary line.
_b1_check_system() {
  if [[ "${TESLAUSB_DRY_RUN:-0}" == "1" ]]; then
    b1_log "DRY-RUN: would check systemctl is-system-running"
    printf 'dry-run'
    return 0
  fi
  local state
  state="$(systemctl is-system-running 2>/dev/null || true)"
  case "${state}" in
    running|degraded)
      b1_log "health: system state=${state}"
      printf '%s' "${state}"
      return 0
      ;;
    maintenance|offline|"")
      b1_err "health check FAILED: system state=${state:-unknown} (want running or degraded)"
      return 1
      ;;
    *)
      # initializing / starting / stopping — transient; tolerate but warn.
      b1_warn "system state=${state} (transient; accepting)"
      printf '%s' "${state}"
      return 0
      ;;
  esac
}

# --------------------------------------------------------------------
# Step entry point
# --------------------------------------------------------------------

b1_step_10() {
  # Precondition: 6.4 must have installed the web-app unit. If not,
  # there is nothing meaningful to enable / start and the rest of
  # this step would either no-op or fail confusingly.
  if [[ ! -e "${B1_PRECONDITION_FILE}" ]]; then
    b1_err "precondition missing: ${B1_PRECONDITION_FILE}"
    b1_err "  (Phase 6.4 installs this unit — run \`setup.sh --only 04\` first.)"
    return 4
  fi

  # daemon-reload — required because 6.4 / 6.7 may have installed or
  # edited unit files since the last reload. Idempotent and cheap.
  b1_log "systemd daemon-reload"
  b1_run systemctl daemon-reload

  # Enable phase.
  local unit
  for unit in "${B1_ENABLE_UNITS[@]}"; do
    if ! _b1_enable_one "${unit}"; then
      b1_err "enable failed for ${unit} — aborting 6.10"
      return 5
    fi
  done

  # Start phase (dependency order).
  for unit in "${B1_START_ORDER[@]}"; do
    if ! _b1_start_one "${unit}"; then
      b1_err "start failed for ${unit} — aborting 6.10"
      return 5
    fi
  done

  # Post-start health check.
  if ! _b1_check_active "${B1_REQUIRED_ACTIVE[@]}"; then
    return 5
  fi
  if ! _b1_check_http; then
    return 5
  fi
  local sys_state
  if ! sys_state="$(_b1_check_system)"; then
    return 5
  fi

  b1_log "B-1 stack active: web/nginx/watchdog up; HTTP 200 on /; system ${sys_state}"
  return 0
}
