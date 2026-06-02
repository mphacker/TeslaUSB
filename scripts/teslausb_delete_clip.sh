#!/usr/bin/env bash
#
# teslausb_delete_clip.sh — privileged, path-validated TeslaCam clip deleter.
#
# Why this exists: the Flask web app runs as `pi`, but Tesla writes (and the
# Rust materializer recreates) per-event clip directories as
# `teslausb:teslausb` mode 0755. `pi` is a member of the `teslausb` group,
# but 0755 grants the group only r-x, so `pi` cannot unlink the files inside
# an event directory — the in-UI Delete button fails with EACCES. Rather than
# migrate the whole web service to the `teslausb` account (which would also
# require handing it the broad sudo `pi` currently holds for samba /
# storage-health / reboot), we grant `pi` NOPASSWD for THIS one narrow helper.
#
# Security model: sudoers passes the path argument UNTRUSTED (the grant uses a
# `*` wildcard), so this script re-validates containment independently of the
# Python caller. It will only delete a path that, after canonicalisation:
#   * lives under ALLOWED_BASE (the web backing_root parent), and
#   * sits inside a `TeslaCam/<RecentClips|SavedClips|SentryClips>/` event
#     subtree (never the TeslaCam dir or a bare category dir), and
#   * is not reached through a symlinked final component.
# Anything else exits non-zero WITHOUT deleting. sudo runs with env_reset, so
# the ALLOWED_BASE override below only takes effect when the script is invoked
# directly (i.e. the integration test) — production always uses the default.
#
# Invoked by the web as:
#   sudo -n /usr/local/bin/teslausb_delete_clip.sh <absolute-clip-path>
# NOPASSWD grant: B1_SUDOERS_ALLOWLIST in setup-lib/02-users.sh.
# Installed to /usr/local/bin by setup-lib/04-units.sh.
#
# Exit codes:
#   0  success — target deleted (or already absent: idempotent)
#   2  usage error (wrong argument count)
#   3  path is not absolute
#   4  path failed canonicalisation
#   5  path is outside the allowed TeslaCam clip subtree
#   6  refusing to follow a symlinked target
#   7  rm failed

set -uo pipefail

# The web app's backing_root always lives under this base on the target
# device (config default `/srv/teslausb`; the live deploy uses
# `/srv/teslausb/teslacam`). Both place the clip tree at
# `<...>/TeslaCam/<Category>/<event>`, so anchoring here is a safe superset
# that still excludes the media partition, the OS, and home directories.
readonly ALLOWED_BASE="${TESLAUSB_DELETE_ALLOWED_BASE:-/srv/teslausb}"

# The only category directories Tesla creates under TeslaCam/. A deletable
# path must name one of these immediately after the `TeslaCam/` segment and
# then descend at least one level (the event dir or a file within it).
readonly TESLACAM_CLIP_RE='/TeslaCam/(RecentClips|SavedClips|SentryClips)/[^/]+'

if [[ $# -ne 1 ]]; then
    echo "usage: teslausb_delete_clip.sh <absolute-clip-path>" >&2
    exit 2
fi
target="$1"

if [[ "$target" != /* ]]; then
    echo "teslausb_delete_clip.sh: path must be absolute: $target" >&2
    exit 3
fi

# Reject a symlinked final component up front: deleting through a symlink
# could let a crafted link escape the validated tree.
if [[ -L "$target" ]]; then
    echo "teslausb_delete_clip.sh: refusing symlinked target: $target" >&2
    exit 6
fi

# Canonicalise lexically AND physically. `-m` tolerates a missing final
# component so an already-deleted target still validates (idempotent no-op).
if ! real_target="$(realpath -m -- "$target" 2> /dev/null)"; then
    echo "teslausb_delete_clip.sh: cannot canonicalise: $target" >&2
    exit 4
fi
real_base="$(realpath -m -- "$ALLOWED_BASE" 2> /dev/null || printf '%s' "$ALLOWED_BASE")"

# Containment check 1: strictly under ALLOWED_BASE.
case "$real_target" in
    "$real_base"/*) : ;;
    *)
        echo "teslausb_delete_clip.sh: outside allowed base: $real_target" >&2
        exit 5
        ;;
esac

# Containment check 2: inside a TeslaCam/<Category>/<event> subtree.
if [[ ! "$real_target" =~ $TESLACAM_CLIP_RE ]]; then
    echo "teslausb_delete_clip.sh: not a TeslaCam clip path: $real_target" >&2
    exit 5
fi

if [[ ! -e "$real_target" ]]; then
    # Post-condition (target absent) already holds — succeed so the web's
    # delete is idempotent across retries / partial prior deletions.
    exit 0
fi

if ! rm -rf -- "$real_target"; then
    echo "teslausb_delete_clip.sh: rm failed: $real_target" >&2
    exit 7
fi
exit 0
