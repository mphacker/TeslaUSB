#!/usr/bin/env bash
# setup-lib/03-data-roots.sh — Phase 6.3 (ext4-only).
#
# Creates the per-LUN data roots that B-1 uses:
#
#   /srv/teslausb/teslacam   (sentry/dashcam clips backing tree)
#   /srv/teslausb/media      (music / lightshow / boombox / lock_chimes)
#
# Both are owned `teslausb:teslausb` mode 0775 so the web app
# (running as `pi`, added to group `teslausb` by 6.2) can write them.
#
# FILESYSTEM POLICY (revised 2026-05-31):
#   * Each root is a plain directory on whatever filesystem hosts
#     /srv (typically the SD card's ext4 root). teslafat and
#     teslausb-worker only require POSIX I/O; we never reformat the
#     operator's filesystem. The btrfs subvolume path was tried
#     during early B-1 design and dropped — Raspberry Pi OS images
#     ship ext4, no consumer SD card exposes wear telemetry, and
#     btrfs scrub has no value on a single-device pool. Storage
#     health is now surfaced by the Storage Health card in the web
#     UI (see web/teslausb_web/services/storage_health_service.py).
#
# Hard constraints (operator data is sacred):
#   * NO mkfs — we never reformat anyone's filesystem.
#   * NO mount — mount management is the operator's job.
#   * NO auto-delete — if a target path exists but is the wrong
#     kind (e.g. a leftover from a prior btrfs-era install), we
#     STOP with a clear error rather than silently rewriting.
#
# Idempotency: existing plain-directory paths are detected and
# skipped. Ownership and mode are corrected only when they don't
# already match.
#
# Dry-run: every mutation goes through b1_run. Probing (`stat`,
# `getent`) runs even under TESLAUSB_DRY_RUN so the dry-run report
# accurately predicts what WOULD change.

# shellcheck source=00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/00-common.sh"

# Public constants — uninstall-lib/03-data-roots.sh reuses them.
B1_DATA_ROOT="/srv/teslausb"
B1_DATA_DIRS=(teslacam media)
B1_DATA_OWNER="teslausb"
B1_DATA_GROUP="teslausb"
B1_DATA_MODE="775"

# teslausb-worker (Phase 4b) writes its SQLite index + cache here,
# and the Flask web app (currently runs as pi:pi until phase-6.2 user
# switch) also reads/writes its own mapping.db / cloud_sync.db
# alongside the worker's index. Both processes need write access:
#   * worker runs as B1_DATA_OWNER (teslausb) — file owner.
#   * web app runs as pi — only reachable via the teslausb GROUP.
# Mode 0770 — owner + group writable, world none. pi is added to the
# teslausb group in setup-lib/02-users.sh, so this grants both.
B1_STATE_DIR="/var/lib/teslausb"
B1_STATE_DIR_MODE="0770"

# Tesla-canonical subdirectories created inside each LUN backing root so
# the FAT32 volume teslafat synthesizes for the car already has the
# directory structure Tesla expects. Tesla creates these on first write
# anyway, but pre-creating means:
#   * the web app shows correct empty states from day one;
#   * the operator can confirm via the web UI that the gadget is
#     end-to-end alive *before* the car has written anything;
#   * users dropping LightShow / Boombox content onto the media drive
#     have a destination folder ready.
#
# TeslaCam LUN subdirs (Tesla writes here):
B1_TESLACAM_SUBDIRS=(
  "TeslaCam"
  "TeslaCam/RecentClips"
  "TeslaCam/SentryClips"
  "TeslaCam/SavedClips"
)
# Media LUN subdirs (Tesla reads here; user populates):
B1_MEDIA_SUBDIRS=(
  "LightShow"
  "Boombox"
)
export B1_DATA_ROOT B1_DATA_DIRS B1_DATA_OWNER B1_DATA_GROUP B1_DATA_MODE \
  B1_TESLACAM_SUBDIRS B1_MEDIA_SUBDIRS B1_STATE_DIR B1_STATE_DIR_MODE

# _b1_fs_type <path> — e.g. "ext2/ext3", "tmpfs", or "" on stat
# failure / missing path. Retained only for diagnostic logging.
_b1_fs_type() {
  stat -f -c %T "$1" 2>/dev/null || true
}

# _b1_ensure_root — make sure B1_DATA_ROOT exists.
_b1_ensure_root() {
  if [[ -d "${B1_DATA_ROOT}" ]]; then
    return 0
  fi
  local parent_fs
  parent_fs="$(_b1_fs_type "$(dirname "${B1_DATA_ROOT}")")"
  b1_log "creating ${B1_DATA_ROOT} on parent fs '${parent_fs:-unknown}'"
  b1_run mkdir -p "${B1_DATA_ROOT}"
}

# _b1_create_root <name> — idempotent plain-directory create.
_b1_create_root() {
  local name="$1"
  local path="${B1_DATA_ROOT}/${name}"

  if [[ -e "${path}" ]]; then
    if [[ -d "${path}" ]]; then
      b1_log "data root present: ${path}"
      return 0
    fi
    b1_err "${path} exists but is not a directory."
    b1_err "Move it aside or remove it manually, then re-run setup.sh."
    return 1
  fi

  b1_log "creating data root (plain dir): ${path}"
  b1_run mkdir -p "${path}"
}

# _b1_fix_ownership <path> — chown teslausb:teslausb + chmod 0775
# only if the current state doesn't already match.
_b1_fix_ownership() {
  local path="$1"
  if [[ ! -e "${path}" ]]; then
    b1_log "skip chown (path absent under dry-run): ${path}"
    return 0
  fi
  local current desired
  current="$(stat -c '%U:%G:%a' "${path}" 2>/dev/null || echo ':')"
  desired="${B1_DATA_OWNER}:${B1_DATA_GROUP}:${B1_DATA_MODE}"
  if [[ "${current}" == "${desired}" ]]; then
    b1_log "ownership ok: ${path} (${current})"
    return 0
  fi
  b1_log "fixing ownership: ${path} ${current} → ${desired}"
  b1_run chown "${B1_DATA_OWNER}:${B1_DATA_GROUP}" "${path}"
  b1_run chmod "0${B1_DATA_MODE}" "${path}"
}

# _b1_ensure_tesla_subdir <relative-path> <lun-root>
# Idempotently create + chown a Tesla-canonical subdirectory inside
# a LUN backing root.
_b1_ensure_tesla_subdir() {
  local rel="$1" lun_root="$2"
  local path="${lun_root}/${rel}"
  if [[ ! -d "${path}" ]]; then
    b1_log "creating Tesla subdir: ${path}"
    b1_run mkdir -p "${path}"
  else
    b1_log "Tesla subdir present: ${path}"
  fi
  _b1_fix_ownership "${path}"
}

b1_step_03() {
  _b1_ensure_root || return 1

  b1_log "filesystem at ${B1_DATA_ROOT}: $(_b1_fs_type "${B1_DATA_ROOT}") — using plain directories"

  local have_group=1
  if ! getent group "${B1_DATA_GROUP}" >/dev/null 2>&1; then
    have_group=0
    b1_warn "group ${B1_DATA_GROUP} not present yet (step 02 owns it) — skipping chown."
  fi

  local name
  # Fix the top-level B1_DATA_ROOT itself first. Without this the
  # web app (running as `pi` with secondary group `teslausb`) cannot
  # touch-probe `backing_root` (mode 0755 blocks group writes), which
  # makes the System Health "storage_writable" probe report ERROR
  # even when both `teslacam/` and `media/` subdirs are writable.
  if (( have_group )); then
    _b1_fix_ownership "${B1_DATA_ROOT}" || return 1
  fi
  for name in "${B1_DATA_DIRS[@]}"; do
    _b1_create_root "${name}" || return 1
    if (( have_group )); then
      _b1_fix_ownership "${B1_DATA_ROOT}/${name}" || return 1
    fi
  done

  # Tesla-canonical subdirectories inside each LUN backing root.
  # Created only when the group exists so ownership lands correctly.
  if (( have_group )); then
    local sub
    b1_log "creating Tesla folder structure under ${B1_DATA_ROOT}/teslacam"
    for sub in "${B1_TESLACAM_SUBDIRS[@]}"; do
      _b1_ensure_tesla_subdir "${sub}" "${B1_DATA_ROOT}/teslacam" || return 1
    done
    b1_log "creating Tesla folder structure under ${B1_DATA_ROOT}/media"
    for sub in "${B1_MEDIA_SUBDIRS[@]}"; do
      _b1_ensure_tesla_subdir "${sub}" "${B1_DATA_ROOT}/media" || return 1
    done
  fi

  # Worker state directory (SQLite index + caches). Step 10 starts
  # teslausb-worker which fails with SQLITE_READONLY_DIRECTORY if
  # this dir does not exist owned by the worker's User=.
  if (( have_group )); then
    if [[ ! -d "${B1_STATE_DIR}" ]]; then
      b1_log "creating worker state dir ${B1_STATE_DIR} (mode ${B1_STATE_DIR_MODE})"
      b1_run mkdir -p "${B1_STATE_DIR}"
      b1_run chmod "${B1_STATE_DIR_MODE}" "${B1_STATE_DIR}"
    fi
    _b1_fix_ownership "${B1_STATE_DIR}" || return 1

    # Worker-owned SQLite files. If the index DB was created by an
    # earlier bootstrap run (e.g. by pi during manual testing or an
    # ad-hoc sqlite3 invocation), the worker — which runs as
    # teslausb — will see SQLite return "attempt to write a readonly
    # database" even though the bytes are readable. The fix is to
    # match ownership to the worker user. See LEARNINGS Phase 6.
    local _wf
    for _wf in \
        "${B1_STATE_DIR}/index.sqlite3" \
        "${B1_STATE_DIR}/index.sqlite3-wal" \
        "${B1_STATE_DIR}/index.sqlite3-shm"; do
      if [[ -f "${_wf}" ]]; then
        local _curr
        _curr="$(stat -c '%U:%G' "${_wf}" 2>/dev/null || echo ':')"
        if [[ "${_curr}" != "${B1_DATA_OWNER}:${B1_DATA_GROUP}" ]]; then
          b1_log "fixing worker DB ownership: ${_wf} ${_curr} → ${B1_DATA_OWNER}:${B1_DATA_GROUP}"
          b1_run chown "${B1_DATA_OWNER}:${B1_DATA_GROUP}" "${_wf}"
          b1_run chmod 0664 "${_wf}"
        fi
      fi
    done
  fi
}
