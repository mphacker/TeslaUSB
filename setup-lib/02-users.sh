#!/usr/bin/env bash
# setup-lib/02-users.sh — Phase 6.2
#
# Creates the `teslausb` system user + group, adds `pi` to that
# group, and installs a sudoers fragment at
# `/etc/sudoers.d/teslausb-b1` that grants exactly the
# narrowly-scoped privileged commands the B-1 web app + helpers
# need (per `docs/00-PLAN.md` row 6.2).
#
# WHY a sudoers fragment and not "just give pi root"? Charter
# Pillar 5 (least-privilege): the gunicorn process must not run
# as root, and the captive_portal blueprint legitimately needs a
# handful of privileged commands (nmcli, iw scan, smbd toggle,
# tesla-cache-invalidate). Listing them explicitly here is the
# audit trail.
#
# WHY visudo -cf - before writing? A broken sudoers file breaks
# `sudo` system-wide, including for the operator. We render the
# fragment to a string, validate it through `visudo -cf -` (a
# pure read), and ONLY THEN call b1_run to persist it. This
# means a typo here cannot wedge the device.
#
# Idempotency:
#   * `getent group/passwd` short-circuits user/group creation.
#   * `id -nG pi` short-circuits the group-add.
#   * `cmp -s` short-circuits the sudoers write when the target
#     already matches the rendered content AND the mode is 0440.
#   * Second run of `setup.sh --only 02` produces zero mutations.
#
# Dry-run: every mutation goes through `b1_run`. The visudo
# validation runs in dry-run too — it's a pure read with no
# side effects, and skipping it would defeat the safety check.
#
# Reused by 6.11 (uninstall.sh): B1_SUDOERS_ALLOWLIST and the
# B1_TESLAUSB_* constants below are the source of truth so the
# inverse step can remove exactly what we added.

# Re-source common in case this file is invoked via --only.
# shellcheck source=00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/00-common.sh"

# --------------------------------------------------------------------
# Constants (exported so 6.11 / uninstall.sh can re-use verbatim)
# --------------------------------------------------------------------

B1_TESLAUSB_USER="teslausb"
B1_TESLAUSB_GROUP="teslausb"
B1_TESLAUSB_HOME="/var/lib/teslausb"
B1_TESLAUSB_SHELL="/usr/sbin/nologin"
B1_SUDOERS_PATH="/etc/sudoers.d/teslausb-b1"
B1_VISUDO_BIN="/usr/sbin/visudo"

# Narrowly-scoped privileged commands the web app + helpers need.
# Order matters only for human readability — the rendered Cmnd_Alias
# below is what sudoers actually evaluates.
#
# IMPORTANT: keep entries as *exact* command lines (with arg patterns
# where applicable). Adding a bare `/usr/bin/systemctl` would defeat
# the whole point — that would let the web app restart sshd, mask
# units, etc. If you need a new privileged action, add a specific
# verb here, not a wildcard.
B1_SUDOERS_ALLOWLIST=(
  "/usr/sbin/iw dev wlan0 *"             # captive_portal scan
  "/usr/bin/systemctl restart smbd"      # web UI samba toggle
  "/usr/bin/systemctl start smbd"
  "/usr/bin/systemctl stop smbd"
  "/usr/bin/systemctl is-active smbd"
  "/usr/local/bin/tesla_cache_invalidate.sh" # Phase 4c.2 invalidation
  "/usr/local/bin/tesla_gadget_rebind.sh" # lock-chime full UDC re-enumeration
  "/usr/local/bin/teslausb_delete_clip.sh *" # privileged clip delete (self-validating path)
  "/usr/bin/nmcli"                       # NetworkManager from captive_portal
)

export B1_TESLAUSB_USER B1_TESLAUSB_GROUP B1_TESLAUSB_HOME \
       B1_TESLAUSB_SHELL B1_SUDOERS_PATH B1_VISUDO_BIN \
       B1_SUDOERS_ALLOWLIST

# --------------------------------------------------------------------
# Internal: render the sudoers fragment to stdout.
# --------------------------------------------------------------------
#
# Format:
#   * One Cmnd_Alias (B1_CMDS) bundling every entry of
#     B1_SUDOERS_ALLOWLIST — keeps the per-user lines short and
#     makes the Defaults!B1_CMDS override apply to all of them.
#   * NOPASSWD grant for both `teslausb` and `pi`. `pi` is the
#     user gunicorn currently runs under (Phase 5); `teslausb`
#     is the future-state owner once 6.4 moves gunicorn there.
#   * `Defaults!B1_CMDS !requiretty` so systemd-spawned shells
#     (no controlling tty) can invoke these without `sudo: a
#     terminal is required` errors.
_b1_render_sudoers() {
  local i n="${#B1_SUDOERS_ALLOWLIST[@]}"
  printf '# Managed by TeslaUSB B-1 setup.sh (Phase 6.2). Do not edit by hand.\n'
  printf '# Source of truth: setup-lib/02-users.sh / B1_SUDOERS_ALLOWLIST.\n'
  printf '# Removed by uninstall.sh (Phase 6.11).\n'
  printf '\n'
  printf 'Cmnd_Alias B1_CMDS = '
  for (( i = 0; i < n; i++ )); do
    if (( i == n - 1 )); then
      printf '%s\n' "${B1_SUDOERS_ALLOWLIST[i]}"
    else
      printf '%s, \\\n                     ' "${B1_SUDOERS_ALLOWLIST[i]}"
    fi
  done
  printf '\n'
  printf '%s ALL=(root) NOPASSWD: B1_CMDS\n' "${B1_TESLAUSB_USER}"
  printf 'pi       ALL=(root) NOPASSWD: B1_CMDS\n'
  printf '\n'
  printf '# Allow invocation from systemd-spawned shells (no controlling tty).\n'
  printf 'Defaults!B1_CMDS !requiretty\n'
}

# --------------------------------------------------------------------
# Step
# --------------------------------------------------------------------

b1_step_02() {
  # ----- group -----
  if getent group "${B1_TESLAUSB_GROUP}" >/dev/null 2>&1; then
    b1_log "group present: ${B1_TESLAUSB_GROUP}"
  else
    b1_log "creating system group: ${B1_TESLAUSB_GROUP}"
    b1_run groupadd --system "${B1_TESLAUSB_GROUP}"
  fi

  # ----- user -----
  if getent passwd "${B1_TESLAUSB_USER}" >/dev/null 2>&1; then
    b1_log "user present: ${B1_TESLAUSB_USER}"
  else
    b1_log "creating system user: ${B1_TESLAUSB_USER} (home=${B1_TESLAUSB_HOME})"
    b1_run useradd \
      --system \
      --gid "${B1_TESLAUSB_GROUP}" \
      --home-dir "${B1_TESLAUSB_HOME}" \
      --create-home \
      --shell "${B1_TESLAUSB_SHELL}" \
      "${B1_TESLAUSB_USER}"
  fi

  # ----- pi → teslausb membership -----
  # `pi` may not exist on a freshly-flashed Pi OS Lite that the
  # operator renamed at first boot. Treat absence as a no-op (the
  # web app will run as `teslausb` post-6.4 anyway).
  if ! getent passwd pi >/dev/null 2>&1; then
    b1_log "user pi not present — skipping group add"
  elif id -nG pi 2>/dev/null | tr ' ' '\n' | grep -qx "${B1_TESLAUSB_GROUP}"; then
    b1_log "pi already in group ${B1_TESLAUSB_GROUP}"
  else
    b1_log "adding pi to group ${B1_TESLAUSB_GROUP}"
    b1_run usermod -aG "${B1_TESLAUSB_GROUP}" pi
  fi

  # ----- www-data → pi membership -----
  # nginx runs as `www-data` and proxies to the gunicorn Unix
  # socket at /run/teslausb/gunicorn.sock owned `pi:pi` mode 0660
  # (see config/gunicorn.conf.py `umask = 0o007`). Adding www-data
  # to the `pi` group lets nginx connect without changing the
  # web service's User/Group (which would race with teslafat over
  # /run/teslausb ownership). Phase 6.2 will revisit when the web
  # service moves under the `teslausb` user.
  if getent passwd www-data >/dev/null 2>&1 && getent passwd pi >/dev/null 2>&1; then
    if id -nG www-data 2>/dev/null | tr ' ' '\n' | grep -qx pi; then
      b1_log "www-data already in group pi"
    else
      b1_log "adding www-data to group pi (nginx → gunicorn.sock access)"
      b1_run usermod -aG pi www-data
    fi
  fi

  # ----- sudoers fragment -----
  local content
  content="$(_b1_render_sudoers)"

  # Validate FIRST — always, including dry-run. visudo -cf - is a
  # pure read; a broken render here would silently overwrite a
  # working /etc/sudoers.d/teslausb-b1 on a re-run, so we never
  # skip this check.
  # Absolute path: visudo lives in /usr/sbin which is not in a
  # normal user's PATH, and dry-run is allowed to run non-root.
  if [[ ! -x "${B1_VISUDO_BIN}" ]]; then
    b1_err "${B1_VISUDO_BIN} not found — install the 'sudo' package (6.1) first"
    return 1
  fi
  local visudo_out
  if ! visudo_out="$(printf '%s' "${content}" | "${B1_VISUDO_BIN}" -cf - 2>&1)"; then
    b1_err "visudo rejected rendered sudoers fragment — refusing to write"
    b1_err "visudo said: ${visudo_out}"
    b1_err "rendered content was:"
    printf '%s\n' "${content}" | while IFS= read -r line; do
      b1_err "  ${line}"
    done
    return 1
  fi
  b1_log "sudoers fragment validated by visudo -cf -"

  # Idempotency: skip the write if the target file already matches
  # byte-for-byte AND has mode 0440 + root:root ownership.
  local need_write=1
  if [[ -f "${B1_SUDOERS_PATH}" ]]; then
    local cur_mode cur_owner
    cur_mode="$(stat -c '%a' "${B1_SUDOERS_PATH}" 2>/dev/null || echo '')"
    cur_owner="$(stat -c '%U:%G' "${B1_SUDOERS_PATH}" 2>/dev/null || echo '')"
    if printf '%s' "${content}" | cmp -s - "${B1_SUDOERS_PATH}" \
       && [[ "${cur_mode}" == "440" && "${cur_owner}" == "root:root" ]]; then
      need_write=0
    fi
  fi

  if (( need_write == 0 )); then
    b1_log "sudoers fragment already up-to-date: ${B1_SUDOERS_PATH}"
    return 0
  fi

  # Pre-existing file gets a one-time backup sibling before we
  # overwrite (b1_backup is idempotent — no piling up on re-run).
  if [[ -f "${B1_SUDOERS_PATH}" ]]; then
    b1_backup "${B1_SUDOERS_PATH}"
  fi

  b1_log "writing sudoers fragment: ${B1_SUDOERS_PATH}"
  # `install` sets owner+group+mode atomically; the source `/dev/stdin`
  # works because b1_run preserves the parent shell's stdin (here-string
  # below). In dry-run b1_run logs and returns 0 without consuming the
  # here-string — that's fine, the content was already validated above.
  b1_run install -o root -g root -m 0440 /dev/stdin "${B1_SUDOERS_PATH}" <<< "${content}"
}
