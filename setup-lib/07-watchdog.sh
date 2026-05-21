#!/usr/bin/env bash
# setup-lib/07-watchdog.sh — Phase 6.7
#
# Lays down two systemd drop-ins that mirror v1's last-resort safety
# rails plus the matching /etc/watchdog.conf body:
#
#   1) /etc/systemd/system/watchdog.service.d/10-teslausb-priority.conf
#      — boosts the watchdog daemon's CPU + I/O scheduling so a
#        misbehaving worker can never starve it long enough to miss
#        a /dev/watchdog ping window.
#
#   2) /etc/watchdog.conf — B-1's minimal known-good watchdogd config
#      (15 s hardware timeout, 5 s interval, realtime priority 1).
#      Edited line-by-line (ensure-or-replace) so any operator-added
#      keys we don't manage survive untouched.
#
#   3) /etc/systemd/system/ssh.service.d/10-teslausb-protect.conf
#      (or sshd.service.d/… on distros where the unit is sshd)
#      — pins OOMScoreAdjust=-1000 + Restart=always so the device's
#        only remote recovery path can't be killed by the OOM killer
#        or a transient sshd crash.
#
# IMPORTANT: this step deliberately does NOT enable, start, restart,
# or reload watchdog/ssh. Activation is owned by Phase 6.10. Re-running
# 6.7 on a live device only rewrites the drop-ins and triggers ONE
# `systemctl daemon-reload` if anything changed — it never bounces a
# running service (the operator may be browsing the web UI over SSH).
#
# Idempotency: sha256(rendered) vs sha256(on-disk) for the .conf
# overrides; line-presence ensure-or-replace for /etc/watchdog.conf
# (a missing key is appended; a key with a different value is replaced
# in place; matching keys are no-ops — duplicate keys are never
# produced). The first overwrite per file spawns one `b1_backup`
# sibling; subsequent runs do not pile up backups.
#
# Dry-run: every mutation routes through `b1_run` / `b1_run_quiet`.
# Reads (sha256, grep, b1_unit_exists) always execute so --dry-run
# can accurately report exactly what WOULD be touched.
#
# Charter / ADR notes:
#   * No `systemctl enable` / `start` / `restart` (Phase 6.10).
#   * No `apt-get` here (watchdog pkg is in Phase 6.1).
#   * Override bodies live as constants at file scope so reviewers
#     and the 6.11 uninstaller can read them without executing this
#     script.

# Re-source common in case this file is invoked via --only.
# shellcheck source=00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/00-common.sh"

# Repo root (used only for staging temp files inside the repo — NEVER /tmp).
B1_REPO_ROOT_07="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --------------------------------------------------------------------
# Constants — exported so 6.11 (uninstall.sh) can reverse this step.
# --------------------------------------------------------------------

# watchdog.service drop-in path (priority boost).
B1_WATCHDOG_OVERRIDE="/etc/systemd/system/watchdog.service.d/10-teslausb-priority.conf"
export B1_WATCHDOG_OVERRIDE

# watchdogd configuration file.
B1_WATCHDOG_CONF="/etc/watchdog.conf"
export B1_WATCHDOG_CONF

# Keys this step manages inside /etc/watchdog.conf — exported so the
# 6.11 uninstaller knows exactly which keys to revert to the
# `.b1-backup-<ISO>` sibling (any operator-added keys outside this
# list are preserved verbatim).
B1_WATCHDOG_CONF_KEYS=(
  watchdog-device
  watchdog-timeout
  interval
  realtime
  priority
)
export B1_WATCHDOG_CONF_KEYS

# sshd drop-in path — populated by b1_step_07 once the live unit name
# (ssh.service on Raspberry Pi OS, sshd.service on some others) is
# detected. Initialised to the Pi OS default so 6.11 can still find it
# even if uninstall is invoked on a fresh checkout.
B1_SSH_OVERRIDE_PATH="/etc/systemd/system/ssh.service.d/10-teslausb-protect.conf"
export B1_SSH_OVERRIDE_PATH

# --------------------------------------------------------------------
# watchdog.service drop-in body (constant)
# --------------------------------------------------------------------
read -r -d '' B1_WATCHDOG_OVERRIDE_BODY <<'WD_OVERRIDE' || true
# Phase 6.7: priority boost so the watchdog ping never loses CPU to a
# misbehaving worker thread. Mirrors v1's safeguard.
[Service]
Nice=-10
IOSchedulingClass=realtime
IOSchedulingPriority=4
WD_OVERRIDE

# --------------------------------------------------------------------
# /etc/watchdog.conf desired key=value pairs (constant)
# --------------------------------------------------------------------
# Order matters only for first-time creation (file written verbatim
# below); on existing files each key is enforced independently via
# ensure-or-replace, so operator-added unrelated keys survive.
B1_WATCHDOG_CONF_HEADER='# Phase 6.7 watchdog config (managed by TeslaUSB setup.sh)'
B1_WATCHDOG_CONF_VALUES=(
  "watchdog-device = /dev/watchdog"
  "watchdog-timeout = 15"
  "interval = 5"
  "realtime = yes"
  "priority = 1"
)

# --------------------------------------------------------------------
# ssh.service drop-in body (constant)
# --------------------------------------------------------------------
read -r -d '' B1_SSH_OVERRIDE_BODY <<'SSH_OVERRIDE' || true
# Phase 6.7: ensure sshd is never OOM-killed and survives any
# misbehaving worker. SSH is our only remote recovery path.
[Service]
OOMScoreAdjust=-1000
Nice=-5
Restart=always
RestartSec=5
SSH_OVERRIDE

# --------------------------------------------------------------------
# Helpers (private to this step)
# --------------------------------------------------------------------

_b1_07_sha256() {
  local path="$1"
  if [[ ! -e "${path}" ]]; then
    printf ''
    return 0
  fi
  sha256sum -- "${path}" 2>/dev/null | awk '{print $1}'
}

# _b1_07_install_inline <content> <dst> <mode> [<state-var>]
#   * Idempotent install of an in-memory string at <dst>.
#   * Compares sha256(staged) vs sha256(dst); no-op on match.
#   * Backs up any pre-existing target via b1_backup before overwrite.
#   * Stages under the repo's setup-lib/ (NEVER /tmp — runtime policy).
_b1_07_install_inline() {
  local content="$1"
  local dst="$2"
  local mode="$3"
  local state_var="${4:-}"

  local stage
  stage="${B1_REPO_ROOT_07}/setup-lib/.b1-stage-07-$$-${RANDOM}"
  printf '%s' "${content}" > "${stage}"
  # shellcheck disable=SC2064
  trap "rm -f -- '${stage}'" RETURN

  local src_sum dst_sum
  src_sum="$(_b1_07_sha256 "${stage}")"
  dst_sum="$(_b1_07_sha256 "${dst}")"

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

  b1_run install -m "${mode}" -- "${stage}" "${dst}"

  if [[ -n "${state_var}" ]]; then
    printf -v "${state_var}" '%s' 1
  fi
}

# _b1_07_ensure_conf_line <file> <key> <full-line> [<state-var>]
#   * If <file> has no `^<key>` line → append <full-line>.
#   * If <file> has a `^<key>` line with a different value → replace
#     it in place with <full-line> (preserves position; never produces
#     a duplicate key).
#   * If a matching line already exists → no-op.
#   * Sets <state-var> to "1" iff the file was actually modified.
#
# The regex anchors on `^<key>` followed by either whitespace or `=`
# so we don't accidentally match `watchdog-timeout` when looking for
# `watchdog` (defence-in-depth — current keys don't collide).
_b1_07_ensure_conf_line() {
  local file="$1"
  local key="$2"
  local line="$3"
  local state_var="${4:-}"

  # File must exist (caller creates it first if needed).
  if [[ ! -e "${file}" ]]; then
    b1_err "ensure-conf-line: ${file} missing (caller bug)"
    return 1
  fi

  # Pattern matches start-of-line <key> followed by space, tab, or `=`.
  # `grep -E` keeps the regex portable; the key itself is treated as a
  # literal (no metachars expected in our keyset).
  local pat="^${key}[[:space:]=]"

  if grep -Eq -- "${pat}" "${file}"; then
    # A line for this key exists — check whether it matches the
    # desired value verbatim. If yes, no-op; otherwise replace.
    if grep -Fxq -- "${line}" "${file}"; then
      b1_log "  conf-key ok: ${key}"
      return 0
    fi
    b1_log "  conf-key differs: ${key} — replacing in place"
    if [[ "${TESLAUSB_DRY_RUN:-0}" == "1" ]]; then
      b1_log "DRY-RUN: sed -i replace ${key} in ${file}"
    else
      # Escape `/` and `&` in the replacement so sed treats it as a
      # literal. The replacement line has no newlines.
      local esc
      esc="$(printf '%s' "${line}" | sed -e 's/[\/&]/\\&/g')"
      sed -i -E "s/${pat}.*/${esc}/" "${file}"
    fi
    if [[ -n "${state_var}" ]]; then
      printf -v "${state_var}" '%s' 1
    fi
    return 0
  fi

  b1_log "  conf-key missing: ${key} — appending"
  if [[ "${TESLAUSB_DRY_RUN:-0}" == "1" ]]; then
    b1_log "DRY-RUN: append '${line}' to ${file}"
  else
    printf '%s\n' "${line}" >> "${file}"
  fi
  if [[ -n "${state_var}" ]]; then
    printf -v "${state_var}" '%s' 1
  fi
}

# _b1_07_detect_ssh_unit
#   Echoes the live ssh unit name (ssh.service or sshd.service). On
#   Raspberry Pi OS this is ssh.service. Falls back to ssh.service if
#   neither is detected (e.g. running outside the target host under
#   --dry-run) so the dry-run still reports the canonical path.
_b1_07_detect_ssh_unit() {
  if b1_unit_exists ssh.service; then
    echo "ssh.service"
    return 0
  fi
  if b1_unit_exists sshd.service; then
    echo "sshd.service"
    return 0
  fi
  b1_warn "neither ssh.service nor sshd.service found — defaulting to ssh.service (Raspberry Pi OS)"
  echo "ssh.service"
}

# --------------------------------------------------------------------
# Step entry point
# --------------------------------------------------------------------

b1_step_07() {
  local changed=""

  # ------------------------------------------------------------------
  # 1) watchdog.service drop-in
  # ------------------------------------------------------------------
  local wd_dir
  wd_dir="$(dirname "${B1_WATCHDOG_OVERRIDE}")"
  b1_run mkdir -p -- "${wd_dir}"
  _b1_07_install_inline \
    "${B1_WATCHDOG_OVERRIDE_BODY}" \
    "${B1_WATCHDOG_OVERRIDE}" \
    0644 \
    changed

  # ------------------------------------------------------------------
  # 2) /etc/watchdog.conf — ensure-or-replace per key
  # ------------------------------------------------------------------
  # If the file does not exist yet, create it from scratch with the
  # header + all desired keys in one go (b1_backup is still called so
  # the missing-file path is recorded explicitly in the log). On an
  # existing file, back it up FIRST then walk each key.
  if [[ ! -e "${B1_WATCHDOG_CONF}" ]]; then
    b1_log "new: ${B1_WATCHDOG_CONF}"
    b1_backup "${B1_WATCHDOG_CONF}"   # no-op (missing)
    local initial
    initial="${B1_WATCHDOG_CONF_HEADER}"$'\n'
    local v
    for v in "${B1_WATCHDOG_CONF_VALUES[@]}"; do
      initial+="${v}"$'\n'
    done
    if [[ "${TESLAUSB_DRY_RUN:-0}" == "1" ]]; then
      b1_log "DRY-RUN: write ${B1_WATCHDOG_CONF} with header + ${#B1_WATCHDOG_CONF_VALUES[@]} keys"
    else
      printf '%s' "${initial}" > "${B1_WATCHDOG_CONF}"
      chmod 0644 -- "${B1_WATCHDOG_CONF}"
      chown root:root -- "${B1_WATCHDOG_CONF}"
    fi
    changed=1
  else
    # Existing file — back up once, then ensure each managed key.
    b1_backup "${B1_WATCHDOG_CONF}"
    local idx key line
    for idx in "${!B1_WATCHDOG_CONF_KEYS[@]}"; do
      key="${B1_WATCHDOG_CONF_KEYS[${idx}]}"
      line="${B1_WATCHDOG_CONF_VALUES[${idx}]}"
      _b1_07_ensure_conf_line "${B1_WATCHDOG_CONF}" "${key}" "${line}" changed
    done
  fi

  # ------------------------------------------------------------------
  # 3) ssh.service (or sshd.service) drop-in
  # ------------------------------------------------------------------
  local ssh_unit
  ssh_unit="$(_b1_07_detect_ssh_unit)"
  B1_SSH_OVERRIDE_PATH="/etc/systemd/system/${ssh_unit}.d/10-teslausb-protect.conf"
  export B1_SSH_OVERRIDE_PATH
  b1_log "ssh unit detected: ${ssh_unit} → ${B1_SSH_OVERRIDE_PATH}"

  local ssh_dir
  ssh_dir="$(dirname "${B1_SSH_OVERRIDE_PATH}")"
  b1_run mkdir -p -- "${ssh_dir}"
  _b1_07_install_inline \
    "${B1_SSH_OVERRIDE_BODY}" \
    "${B1_SSH_OVERRIDE_PATH}" \
    0644 \
    changed

  # ------------------------------------------------------------------
  # 4) daemon-reload (ONCE, only if anything changed). We do NOT
  #    restart watchdog or ssh here — Phase 6.10 owns activation
  #    and the operator may currently be browsing over the very ssh
  #    session this drop-in modifies.
  # ------------------------------------------------------------------
  if [[ -n "${changed}" ]]; then
    b1_log "watchdog/ssh drop-ins changed — running daemon-reload"
    b1_run systemctl daemon-reload
  else
    b1_log "no drop-ins changed — skipping daemon-reload"
  fi

  return 0
}
