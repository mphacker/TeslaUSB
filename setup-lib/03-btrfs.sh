#!/usr/bin/env bash
# setup-lib/03-btrfs.sh — Phase 6.3
#
# Creates the btrfs subvolumes that B-1 uses as its data roots:
#
#   /srv/teslausb/teslacam   (sentry/dashcam clips backing tree)
#   /srv/teslausb/media      (music / lightshow / boombox / lock_chimes)
#
# Both subvolumes are owned `teslausb:teslausb` mode 0775 so the web
# app (running as `pi`, added to group `teslausb` by 6.2) can write
# them.
#
# Hard constraints (operator data is sacred):
#   * NO mkfs — we never reformat anyone's filesystem.
#   * NO mount — mount management is the operator's job.
#   * NO auto-delete — if `/srv/teslausb/<name>` exists as a plain
#     directory (not a subvolume), we STOP with a clear error.
#   * If `/srv/teslausb` is not on a btrfs filesystem, we STOP and
#     ask the operator to mount one there first.
#
# Idempotency: existing subvolumes are detected via
# `btrfs subvolume show` and skipped. Ownership/mode are only
# corrected when they don't already match. Re-running this step on a
# fully-configured device performs zero mutations.
#
# Dry-run: every mutation goes through b1_run. Probing
# (`stat -f`, `btrfs subvolume show`, `getent`) runs even under
# TESLAUSB_DRY_RUN so the dry-run report accurately predicts what
# WOULD change.

# Re-source common in case this file is invoked via --only.
# shellcheck source=00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/00-common.sh"

# Public constants — 6.11 (uninstall.sh) reuses them verbatim so the
# inverse step always targets the same paths this step created.
B1_BTRFS_ROOT="/srv/teslausb"
B1_BTRFS_SUBVOLS=(teslacam media)
B1_BTRFS_OWNER="teslausb"
B1_BTRFS_GROUP="teslausb"
B1_BTRFS_MODE="775"
export B1_BTRFS_ROOT B1_BTRFS_SUBVOLS B1_BTRFS_OWNER B1_BTRFS_GROUP B1_BTRFS_MODE

# _b1_fs_type <path>  — `stat -f -c %T <path>` (e.g. "btrfs",
# "ext2/ext3", "tmpfs"). Empty string if the path doesn't exist or
# stat fails.
_b1_fs_type() {
  stat -f -c %T "$1" 2>/dev/null || true
}

# _b1_is_subvolume <path>  — true iff `btrfs subvolume show` succeeds.
# Quiet on both stdout and stderr; we don't want noise in the log
# when the answer is simply "no".
_b1_is_subvolume() {
  btrfs subvolume show "$1" >/dev/null 2>&1
}

# _b1_ensure_root  — make sure B1_BTRFS_ROOT exists AND is on btrfs.
# Returns 0 on success, 1 with a clear b1_err on any failure.
_b1_ensure_root() {
  if [[ -d "${B1_BTRFS_ROOT}" ]]; then
    local fstype
    fstype="$(_b1_fs_type "${B1_BTRFS_ROOT}")"
    if [[ "${fstype}" != "btrfs" ]]; then
      b1_err "${B1_BTRFS_ROOT} exists but is on '${fstype:-unknown}', not btrfs."
      b1_err "Mount a btrfs volume at ${B1_BTRFS_ROOT} first, then re-run setup.sh."
      return 1
    fi
    return 0
  fi

  # Root missing — only create it if its parent is already btrfs.
  # We refuse to create the dir on a non-btrfs parent because the
  # subvolume create that follows would fail anyway and we'd leave
  # an empty directory behind for the operator to clean up.
  local parent
  parent="$(dirname "${B1_BTRFS_ROOT}")"
  local parent_fs
  parent_fs="$(_b1_fs_type "${parent}")"
  if [[ "${parent_fs}" != "btrfs" ]]; then
    b1_err "${B1_BTRFS_ROOT} does not exist and parent ${parent} is on '${parent_fs:-unknown}', not btrfs."
    b1_err "Mount a btrfs volume at ${B1_BTRFS_ROOT} first, then re-run setup.sh."
    return 1
  fi

  b1_log "creating ${B1_BTRFS_ROOT} on btrfs parent ${parent}"
  b1_run mkdir -p "${B1_BTRFS_ROOT}"
}

# _b1_create_subvol <name>  — idempotently ensure a single subvolume
# under B1_BTRFS_ROOT exists. Returns non-zero on a refusal (path
# exists as a regular directory).
_b1_create_subvol() {
  local name="$1"
  local path="${B1_BTRFS_ROOT}/${name}"

  if [[ -e "${path}" ]]; then
    if _b1_is_subvolume "${path}"; then
      b1_log "subvolume present: ${path}"
      return 0
    fi
    b1_err "${path} exists but is not a btrfs subvolume — refusing to delete operator data."
    b1_err "Move it aside (or remove it manually) and re-run setup.sh."
    return 1
  fi

  b1_log "creating btrfs subvolume: ${path}"
  b1_run btrfs subvolume create "${path}"
}

# _b1_fix_ownership <path>  — chown teslausb:teslausb + chmod 0775
# only if the current state doesn't already match. Pure no-op on
# already-correct paths.
_b1_fix_ownership() {
  local path="$1"

  # If the path doesn't exist yet (dry-run on a non-btrfs box where
  # the create above was a logged no-op), skip silently — there's
  # nothing to stat.
  if [[ ! -e "${path}" ]]; then
    b1_log "skip chown (path absent under dry-run): ${path}"
    return 0
  fi

  local current
  current="$(stat -c '%U:%G:%a' "${path}" 2>/dev/null || echo ':')"
  local desired="${B1_BTRFS_OWNER}:${B1_BTRFS_GROUP}:${B1_BTRFS_MODE}"

  if [[ "${current}" == "${desired}" ]]; then
    b1_log "ownership ok: ${path} (${current})"
    return 0
  fi

  b1_log "fixing ownership: ${path} ${current} → ${desired}"
  b1_run chown "${B1_BTRFS_OWNER}:${B1_BTRFS_GROUP}" "${path}"
  b1_run chmod "0${B1_BTRFS_MODE}" "${path}"
}

b1_step_03() {
  # 6.1 installs btrfs-progs, but --only 03 may be invoked before
  # --only 01 ever runs. Tolerate gracefully so the dry-run path
  # stays useful on a stock dev machine.
  if ! command -v btrfs >/dev/null 2>&1; then
    b1_warn "btrfs command not found — install btrfs-progs (step 01) first; skipping 03."
    return 0
  fi

  _b1_ensure_root || return 1

  # The teslausb user/group is provisioned by 6.2. If we run before
  # 6.2 (e.g. --only 03 on a fresh box), warn but continue: the
  # subvolumes still get created, only the chown is deferred.
  local have_group=1
  if ! getent group "${B1_BTRFS_GROUP}" >/dev/null 2>&1; then
    have_group=0
    b1_warn "group ${B1_BTRFS_GROUP} not present yet (step 02 owns it) — skipping chown."
  fi

  local name
  for name in "${B1_BTRFS_SUBVOLS[@]}"; do
    _b1_create_subvol "${name}" || return 1
    if (( have_group )); then
      _b1_fix_ownership "${B1_BTRFS_ROOT}/${name}" || return 1
    fi
  done
}
