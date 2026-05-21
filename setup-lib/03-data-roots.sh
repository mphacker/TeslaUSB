#!/usr/bin/env bash
# setup-lib/03-data-roots.sh — Phase 6.3 (filesystem-agnostic).
#
# Creates the per-LUN data roots that B-1 uses:
#
#   /srv/teslausb/teslacam   (sentry/dashcam clips backing tree)
#   /srv/teslausb/media      (music / lightshow / boombox / lock_chimes)
#
# Both are owned `teslausb:teslausb` mode 0775 so the web app
# (running as `pi`, added to group `teslausb` by 6.2) can write them.
#
# FILESYSTEM POLICY (revised 2026-05-21):
#   * If /srv is on btrfs: each root is created as a btrfs subvolume
#     (so `btrfs scrub`/quotas/etc. can operate on it).
#   * If /srv is on ext4 / any other FS: each root is created as a
#     plain directory. teslafat and teslausb-worker only require
#     POSIX I/O; the underlying FS is otherwise opaque.
#
# Hard constraints (operator data is sacred):
#   * NO mkfs — we never reformat anyone's filesystem.
#   * NO mount — mount management is the operator's job.
#   * NO auto-delete — if a target path exists but is the wrong
#     kind (e.g. a btrfs subvolume on a btrfs-aware host became a
#     plain dir on a re-image), we STOP with a clear error rather
#     than silently rewriting.
#
# Idempotency: existing paths in the correct shape (subvolume on
# btrfs, plain dir elsewhere) are detected and skipped. Ownership
# and mode are corrected only when they don't already match.
#
# Dry-run: every mutation goes through b1_run. Probing (`stat -f`,
# `btrfs subvolume show`, `getent`) runs even under TESLAUSB_DRY_RUN
# so the dry-run report accurately predicts what WOULD change.

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

# Backwards-compat aliases (older docs / commits reference B1_BTRFS_*).
# shellcheck disable=SC2034  # exported for downstream consumers
B1_BTRFS_ROOT="${B1_DATA_ROOT}"
# shellcheck disable=SC2034
B1_BTRFS_SUBVOLS=("${B1_DATA_DIRS[@]}")
# shellcheck disable=SC2034
B1_BTRFS_OWNER="${B1_DATA_OWNER}"
# shellcheck disable=SC2034
B1_BTRFS_GROUP="${B1_DATA_GROUP}"
# shellcheck disable=SC2034
B1_BTRFS_MODE="${B1_DATA_MODE}"
export B1_BTRFS_ROOT B1_BTRFS_SUBVOLS B1_BTRFS_OWNER B1_BTRFS_GROUP B1_BTRFS_MODE

# _b1_fs_type <path> — e.g. "btrfs", "ext2/ext3", "tmpfs", or "" on
# stat failure / missing path.
_b1_fs_type() {
  stat -f -c %T "$1" 2>/dev/null || true
}

# _b1_have_btrfs — true iff btrfs userspace + at least mountpoint is
# on btrfs. Used to decide subvolume vs plain-dir creation.
_b1_root_is_btrfs() {
  [[ "$(_b1_fs_type "${B1_DATA_ROOT}")" == "btrfs" ]]
}

# _b1_is_subvolume <path> — quiet probe.
_b1_is_subvolume() {
  command -v btrfs >/dev/null 2>&1 && btrfs subvolume show "$1" >/dev/null 2>&1
}

# _b1_ensure_root — make sure B1_DATA_ROOT exists. Tolerant of any
# filesystem (no longer requires btrfs).
_b1_ensure_root() {
  if [[ -d "${B1_DATA_ROOT}" ]]; then
    return 0
  fi
  local parent_fs
  parent_fs="$(_b1_fs_type "$(dirname "${B1_DATA_ROOT}")")"
  b1_log "creating ${B1_DATA_ROOT} on parent fs '${parent_fs:-unknown}'"
  b1_run mkdir -p "${B1_DATA_ROOT}"
}

# _b1_create_root <name> — idempotent. Decides subvolume vs dir
# based on the filesystem hosting B1_DATA_ROOT.
_b1_create_root() {
  local name="$1"
  local path="${B1_DATA_ROOT}/${name}"
  local want_subvol=0
  _b1_root_is_btrfs && want_subvol=1

  if [[ -e "${path}" ]]; then
    if (( want_subvol )); then
      if _b1_is_subvolume "${path}"; then
        b1_log "subvolume present: ${path}"
        return 0
      fi
      b1_err "${path} exists but is not a btrfs subvolume (parent is btrfs)."
      b1_err "Move it aside or remove it manually, then re-run setup.sh."
      return 1
    else
      if [[ -d "${path}" ]]; then
        b1_log "data root present: ${path}"
        return 0
      fi
      b1_err "${path} exists but is not a directory."
      b1_err "Move it aside or remove it manually, then re-run setup.sh."
      return 1
    fi
  fi

  if (( want_subvol )); then
    b1_log "creating btrfs subvolume: ${path}"
    b1_run btrfs subvolume create "${path}"
  else
    b1_log "creating data root (plain dir): ${path}"
    b1_run mkdir -p "${path}"
  fi
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

  if _b1_root_is_btrfs; then
    if ! command -v btrfs >/dev/null 2>&1; then
      b1_warn "/srv is btrfs but 'btrfs' command not found — install btrfs-progs (step 01) first; skipping 03."
      return 0
    fi
    b1_log "filesystem at ${B1_DATA_ROOT}: btrfs — using subvolumes"
  else
    b1_log "filesystem at ${B1_DATA_ROOT}: $(_b1_fs_type "${B1_DATA_ROOT}") — using plain directories"
  fi

  local have_group=1
  if ! getent group "${B1_DATA_GROUP}" >/dev/null 2>&1; then
    have_group=0
    b1_warn "group ${B1_DATA_GROUP} not present yet (step 02 owns it) — skipping chown."
  fi

  local name
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
