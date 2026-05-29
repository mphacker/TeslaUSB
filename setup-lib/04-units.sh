#!/usr/bin/env bash
# setup-lib/04-units.sh — Phase 6.4
#
# Installs the B-1 systemd unit files (and the nginx site drop-in)
# into the system locations, then runs `systemctl daemon-reload`
# EXACTLY ONCE if anything changed.
#
# IMPORTANT: this step deliberately does NOT `systemctl enable` or
# `systemctl start` any unit. Activation is owned by Phase 6.10
# (final enable + post-start health check) so that the installer
# can lay down every required file across 6.4 – 6.9 before a single
# service is allowed to run. This separation also means re-running
# 6.4 on a live device only rewrites unit files and triggers a
# daemon-reload — it never bounces a running service.
#
# Idempotency: every install compares sha256(source) vs sha256(target)
# and is a no-op if they match. A `b1_backup` sibling is created the
# FIRST time a target is overwritten (subsequent overwrites do not
# accumulate backups — `b1_backup` is itself idempotent).
#
# Dry-run: every mutation flows through `b1_run`; in TESLAUSB_DRY_RUN=1
# mode the step still reads sources + targets so the operator can see
# exactly what WOULD be touched.
#
# Charter / ADR notes:
#   * No `systemctl enable` / `start` / `stop` here (Phase 6.10).
#   * No `apt-get` here (Phase 6.1).
#   * The `teslausb-web.service` unit body lives as a constant heredoc
#     in this file so reviewers + the 6.11 uninstaller can read it
#     without executing the script.

# Re-source common in case this file is invoked via --only.
# shellcheck source=00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/00-common.sh"

# Repo root is one level above setup-lib/. Used to locate the source
# unit files that earlier phases shipped under rust/crates/<crate>/units/.
B1_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --------------------------------------------------------------------
# Constants — exported so 6.11 (uninstall.sh) can reuse them verbatim.
# --------------------------------------------------------------------

# Unit file install targets (system-wide systemd dir).
B1_UNIT_TARGETS=(
  /etc/systemd/system/teslafat@.service
  /etc/systemd/system/teslausb-worker.service
  /etc/systemd/system/teslausb-web.service
)
export B1_UNIT_TARGETS

# Nginx site drop-in (available + enabled symlink).
B1_NGINX_SITE_AVAIL=/etc/nginx/sites-available/teslausb
B1_NGINX_SITE_ENABLED=/etc/nginx/sites-enabled/teslausb
export B1_NGINX_SITE_AVAIL B1_NGINX_SITE_ENABLED

# Source paths shipped by earlier phases (Phase 1.6 + Phase 4b).
B1_TESLAFAT_UNIT_SRC="${B1_REPO_ROOT}/rust/crates/teslafat/units/teslafat@.service"
B1_WORKER_UNIT_SRC="${B1_REPO_ROOT}/rust/crates/teslausb-worker/units/teslausb-worker.service"

# Nginx site source (Phase 5.19 deployment config).
B1_NGINX_SITE_SRC="${B1_REPO_ROOT}/config/nginx-teslausb.conf"

# Cache-invalidation helper (Phase 4c) — forces Tesla to re-read a USB
# LUN after the web UI rewrites LockChime.wav / LightShow.fseq. The web
# app invokes it as `sudo /usr/local/bin/tesla_cache_invalidate.sh`.
B1_CACHE_INVALIDATE_SRC="${B1_REPO_ROOT}/scripts/tesla_cache_invalidate.sh"
B1_CACHE_INVALIDATE_DST="/usr/local/bin/tesla_cache_invalidate.sh"

# --------------------------------------------------------------------
# teslausb-web.service — inline body (constant)
# --------------------------------------------------------------------
#
# Reviewers: this is the source of truth for the web-app unit.
# Operator + 6.11 uninstaller read it from here, not from a runtime
# heredoc. Any change MUST be paired with a `systemd-analyze verify`
# pass (this step runs that automatically when the binary is present).
#
# Choices:
#   * User=pi for now — Phase 6.2 introduces the `teslausb` system
#     user; this unit will be re-pointed at it once 6.2 is verified.
#     TODO(phase-6.2): User=teslausb / Group=teslausb / SupplementaryGroups=www-data
#   * RuntimeDirectory=teslausb so /run/teslausb/ is created at start
#     with mode 0755 and owned by the unit's User=/Group=. Gunicorn
#     binds /run/teslausb/gunicorn.sock and chmods it via its own
#     umask=0o007 (see config/gunicorn.conf.py).
#   * ExecStart points at /opt/teslausb/web/.venv/bin/gunicorn — the
#     venv is laid down by the Phase 5 web-app deploy (not part of
#     6.4). The unit installs cleanly even if the binary is missing;
#     6.10 fails loudly if the venv was not provisioned.
#   * After=/Wants=network-online.target because nginx (the only
#     upstream) listens on port 80 and gunicorn binds a Unix socket
#     under /run, which is a tmpfs available very early — but the
#     web app's IPC client connects to teslausb-worker which may
#     itself want network-online for log shipping.
#   * KillSignal=SIGTERM + TimeoutStopSec aligned with the
#     `graceful_timeout=30` setting in config/gunicorn.conf.py.

read -r -d '' B1_WEB_UNIT_BODY <<'WEB_UNIT' || true
# TeslaUSB B-1 web app (gunicorn → Flask, fronted by nginx).
#
# Installed by setup-lib/04-units.sh (Phase 6.4). DO NOT edit this
# file in-place — re-run `setup.sh --only 04` after editing the
# heredoc in setup-lib/04-units.sh. setup.sh backs up any local
# divergence as `.b1-backup-<timestamp>` before overwriting.

[Unit]
Description=TeslaUSB B-1 web app (gunicorn)
Documentation=https://github.com/mphacker/TeslaUSB
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
# TODO(phase-6.2): switch User/Group to `teslausb` once that account
# exists and the web venv ownership has been migrated. Until then
# the process runs as `pi:pi`; the gunicorn-created Unix socket is
# owned `pi:pi` (mode 0660 via the `umask = 0o007` line in
# `config/gunicorn.conf.py`). Phase 6.2 adds `www-data` to the
# `pi` group so nginx can connect to the socket without a
# recursive `chown` after start. (Avoided changing `Group=` here
# because teslafat@.service also declares `RuntimeDirectory=teslausb`
# under User/Group=teslausb; competing owners on the shared dir
# would race at startup.)
User=pi
Group=pi

# Create /run/teslausb-web (mode 0755) at service start; systemd cleans
# it up on stop. Gunicorn binds the socket inside this directory.
#
# CRITICAL — must NOT be the same name as teslafat@.service's
# `RuntimeDirectory=teslausb`. When two units share a RuntimeDirectory,
# every restart of one unit makes systemd recreate the dir with that
# unit's User/Group and wipe files it doesn't track — silently
# deleting the OTHER unit's sockets. We hit this live on
# cybertruckusb.local: a teslausb-web restart erased
# `/run/teslausb/teslafat-{0,1}.sock`, which then broke
# nbd-attach@{0,1} (CONNECT failed) and cascaded to usb-gadget
# (dependency failed) — the Pi was no longer presenting USB to the
# Tesla until teslafat was restarted.
RuntimeDirectory=teslausb-web
RuntimeDirectoryMode=0755

Environment=TESLAUSB_WEB_CONFIG=/etc/teslausb/teslausb-web.toml
Environment=PYTHONUNBUFFERED=1

ExecStart=/opt/teslausb/web/.venv/bin/gunicorn -c /etc/teslausb/gunicorn.conf.py teslausb_web.wsgi:app

# Match config/gunicorn.conf.py's graceful_timeout=30 with headroom
# for the master-process shutdown after workers exit.
KillSignal=SIGTERM
TimeoutStopSec=45s

Restart=on-failure
RestartSec=5s

# --- Resource caps (Pi Zero 2 W, 512 MiB RAM) -----------------------
# The web process hosts the cloud-archive uploader as a background
# thread, so it can spike memory when rclone is running. MemoryHigh
# applies soft pressure (throttling allocation, swapping) BEFORE
# MemoryMax triggers the kernel OOM killer. We deliberately keep
# OOMPolicy=stop (not `continue`) so a hard OOM still tears the unit
# down — combined with Restart=on-failure that produces a clean
# restart cycle, and combined with wifi-watchdog.service that
# produces a reboot path if even the restart fails.
#
#   Operator: User input: "any critical OOM does reboot the device.
#   It is critical that the device never fully loses wifi or SSH
#   capabilities."
MemoryHigh=300M
MemoryMax=400M
OOMPolicy=stop
TasksMax=128

# Make the web process a polite I/O citizen RELATIVE to other
# cgroups, without slowing the gunicorn workers serving HTTP. The
# heavy rclone subprocess is renice'd separately inside the Python
# uploader (see cloud_rclone_service._build_transfer_command) so the
# HTTP-serving threads stay at default priority.
CPUWeight=80
IOWeight=80

[Install]
WantedBy=multi-user.target
WEB_UNIT

# --------------------------------------------------------------------
# Helpers (private to this step)
# --------------------------------------------------------------------

# _b1_sha256 <path>  — print sha256 hex of <path> (empty string if
# the file is missing). Uses `sha256sum` which ships with coreutils.
_b1_sha256() {
  local path="$1"
  if [[ ! -e "${path}" ]]; then
    printf ''
    return 0
  fi
  sha256sum -- "${path}" 2>/dev/null | awk '{print $1}'
}

# _b1_install_file <src> <dst> <mode> [<state-var>]
#   * Compares sha256(src) vs sha256(dst).
#   * If identical → no-op, returns 0.
#   * Otherwise → b1_backup the existing target, then b1_run cp / chmod.
#   * Sets the named state var (if provided) to "1" when a write happened.
_b1_install_file() {
  local src="$1"
  local dst="$2"
  local mode="$3"
  local state_var="${4:-}"

  if [[ ! -r "${src}" ]]; then
    b1_err "source missing or unreadable: ${src}"
    return 1
  fi

  local src_sum dst_sum
  src_sum="$(_b1_sha256 "${src}")"
  dst_sum="$(_b1_sha256 "${dst}")"

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

# _b1_install_inline <content> <dst> <mode> [<state-var>]
#   * Same idempotency contract as _b1_install_file but the source
#     is a string in memory (used for the teslausb-web.service heredoc).
#   * Writes the content to a temp file under the repo's setup-lib/
#     directory (NEVER /tmp — runtime policy) then delegates to
#     `install`.
_b1_install_inline() {
  local content="$1"
  local dst="$2"
  local mode="$3"
  local state_var="${4:-}"

  # Stage in a unique repo-local file; cleaned up at function exit.
  local stage
  stage="${B1_REPO_ROOT}/setup-lib/.b1-stage-$$-${RANDOM}"
  printf '%s' "${content}" > "${stage}"
  # shellcheck disable=SC2064
  trap "rm -f -- '${stage}'" RETURN

  local src_sum dst_sum
  src_sum="$(_b1_sha256 "${stage}")"
  dst_sum="$(_b1_sha256 "${dst}")"

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

# _b1_ensure_symlink <link> <target>
#   * Idempotent: if <link> already points at <target>, no-op.
#   * Otherwise: backup any existing non-matching link/file, then
#     create the symlink via `ln -sfn`.
_b1_ensure_symlink() {
  local link="$1"
  local target="$2"

  if [[ -L "${link}" ]]; then
    local current
    current="$(readlink -- "${link}")"
    if [[ "${current}" == "${target}" ]]; then
      b1_log "symlink ok: ${link} → ${target}"
      return 0
    fi
    b1_log "symlink differs: ${link} → ${current} (want ${target}) — backing up"
    b1_backup "${link}"
  elif [[ -e "${link}" ]]; then
    b1_log "symlink path occupied by non-link: ${link} — backing up"
    b1_backup "${link}"
  else
    b1_log "new symlink: ${link} → ${target}"
  fi

  b1_run ln -sfn -- "${target}" "${link}"
}

# _b1_verify_unit <path>
#   * Runs `systemd-analyze verify` against the installed unit if
#     the tool is available AND the file actually exists on disk
#     (i.e. not skipped by --dry-run). Non-fatal: a verify failure
#     logs a warning so the operator can investigate without aborting
#     the whole installer.
_b1_verify_unit() {
  local path="$1"
  if ! command -v systemd-analyze >/dev/null 2>&1; then
    return 0
  fi
  if [[ ! -e "${path}" ]]; then
    return 0
  fi
  if ! systemd-analyze verify "${path}" 2>&1 | while IFS= read -r line; do
       b1_log "  verify: ${line}"
     done; then
    b1_warn "systemd-analyze verify reported issues for ${path}"
  fi
}

# --------------------------------------------------------------------
# Step entry point
# --------------------------------------------------------------------

b1_step_04() {
  local changed=""
  local web_changed=""
  local nginx_changed=""

  # ------------------------------------------------------------------
  # 1) teslafat@.service (Phase 1.6, instances @0 + @1 enabled later
  #    by 6.10 against teslafat-0.toml + teslafat-1.toml).
  # ------------------------------------------------------------------
  if [[ ! -r "${B1_TESLAFAT_UNIT_SRC}" ]]; then
    b1_err "missing required unit source: ${B1_TESLAFAT_UNIT_SRC}"
    b1_err "  (Phase 1.6 ships this file — has the worktree been pruned?)"
    return 1
  fi
  _b1_install_file \
    "${B1_TESLAFAT_UNIT_SRC}" \
    /etc/systemd/system/teslafat@.service \
    0644 \
    changed
  _b1_verify_unit /etc/systemd/system/teslafat@.service

  # ------------------------------------------------------------------
  # 2) teslausb-worker.service (Phase 4b).
  # ------------------------------------------------------------------
  if [[ ! -r "${B1_WORKER_UNIT_SRC}" ]]; then
    b1_err "missing required unit source: ${B1_WORKER_UNIT_SRC}"
    b1_err "  (Phase 4b ships this file — has the worktree been pruned?)"
    return 1
  fi
  _b1_install_file \
    "${B1_WORKER_UNIT_SRC}" \
    /etc/systemd/system/teslausb-worker.service \
    0644 \
    changed
  _b1_verify_unit /etc/systemd/system/teslausb-worker.service

  # ------------------------------------------------------------------
  # 3) teslausb-web.service (inline — Phase 5.19 deployment is being
  #    formalised here; future phases may move the body into a real
  #    source file under config/).
  # ------------------------------------------------------------------
  _b1_install_inline \
    "${B1_WEB_UNIT_BODY}" \
    /etc/systemd/system/teslausb-web.service \
    0644 \
    web_changed
  if [[ -n "${web_changed}" ]]; then
    changed=1
  fi
  _b1_verify_unit /etc/systemd/system/teslausb-web.service

  # ------------------------------------------------------------------
  # 4) nginx site drop-in (sites-available + enabled symlink).
  #    Phase 6.10 owns disabling /etc/nginx/sites-enabled/default;
  #    we deliberately leave it alone here.
  # ------------------------------------------------------------------
  if [[ ! -r "${B1_NGINX_SITE_SRC}" ]]; then
    b1_err "missing required nginx site source: ${B1_NGINX_SITE_SRC}"
    return 1
  fi
  _b1_install_file \
    "${B1_NGINX_SITE_SRC}" \
    "${B1_NGINX_SITE_AVAIL}" \
    0644 \
    nginx_changed
  _b1_ensure_symlink \
    "${B1_NGINX_SITE_ENABLED}" \
    ../sites-available/teslausb
  if [[ -n "${nginx_changed}" ]]; then
    b1_log "nginx site changed — operator should run \`nginx -t && systemctl reload nginx\` in Phase 6.10"
  fi

  # ------------------------------------------------------------------
  # 5) cache-invalidation helper script (Phase 4c). Installed to
  #    /usr/local/bin so the web app's `sudo tesla_cache_invalidate.sh`
  #    call resolves. Not a systemd unit — no daemon-reload needed.
  # ------------------------------------------------------------------
  if [[ ! -r "${B1_CACHE_INVALIDATE_SRC}" ]]; then
    b1_err "missing required script source: ${B1_CACHE_INVALIDATE_SRC}"
    b1_err "  (Phase 4c ships this file — has the worktree been pruned?)"
    return 1
  fi
  _b1_install_file \
    "${B1_CACHE_INVALIDATE_SRC}" \
    "${B1_CACHE_INVALIDATE_DST}" \
    0755

  # ------------------------------------------------------------------
  # 6) daemon-reload (ONCE, only if any unit file changed). We do NOT
  #    daemon-reload for nginx-only changes — nginx is reloaded by
  #    Phase 6.10 once every dependent file is in place.
  # ------------------------------------------------------------------
  if [[ -n "${changed}" ]]; then
    b1_log "unit files changed — running daemon-reload"
    b1_run systemctl daemon-reload
  else
    b1_log "no unit files changed — skipping daemon-reload"
  fi

  return 0
}
