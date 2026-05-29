#!/usr/bin/env bash
#
# tesla_cache_invalidate.sh — force Tesla to re-read a USB LUN
#
# Why: Tesla aggressively caches USB filesystem contents (directory
# listings, `LockChime.wav`, `LightShow.fseq`, etc.). After the web
# UI writes a new file into the backing tree, Tesla will keep playing
# the OLD cached version until something tells the SCSI layer the
# medium has changed. This script does that by clearing the LUN's
# `file` attribute in configfs (kernel sends MEDIUM NOT PRESENT to
# Tesla), waiting ~200 ms, then restoring the original value (kernel
# sends UNIT ATTENTION → NOT READY TO READY CHANGE). Tesla clears
# its cache and re-reads.
#
# Why not full gadget rebind: a UDC unbind+rebind drops USB enumeration
# entirely (~1–2 s) and Tesla treats it as a full re-plug. The LUN
# clear/set is ~200 ms and leaves enumeration intact — only the SCSI
# layer sees the change. Per docs/00-PLAN.md Phase 4c.
#
# Idempotency: invoking this script multiple times back-to-back is
# safe. Each call captures the current LUN file value, clears, sleeps,
# then restores that exact value. If interrupted (SIGINT/SIGTERM/ERR),
# a trap restores the captured value before exit.
#
# Privilege: writes to `/sys/kernel/config/usb_gadget/...` require
# root. The web app invokes this as `sudo
# /usr/local/bin/tesla_cache_invalidate.sh`; the NOPASSWD grant lives in
# the teslausb-b1 sudoers allowlist (setup-lib/02-users.sh). The script
# is installed to /usr/local/bin by setup-lib/04-units.sh.
#
# Exit codes:
#   0  success — medium-change cycle completed
#   2  usage error (bad flag)
#   3  configfs path missing (gadget not bound)
#   4  LUN file already empty (nothing to invalidate)
#   5  I/O error during clear or restore (LUN left in best-effort state)

set -uo pipefail

# Defaults match the live B-1 USB gadget: configfs gadget dir `g1`,
# mass_storage function instance `mass_storage.usb0`, LUN 1 == MEDIA
# drive (where LockChime.wav / LightShow.fseq live). See
# setup-lib/11-gadget.sh and `/sys/kernel/config/usb_gadget/g1/...`.
GADGET="${GADGET:-g1}"
FUNCTION="${FUNCTION:-mass_storage.usb0}"
LUN="${LUN:-1}"
EJECT_MS="${EJECT_MS:-200}"
DRY_RUN=0

CONFIGFS_ROOT="${CONFIGFS_ROOT:-/sys/kernel/config/usb_gadget}"

usage() {
    cat <<'USAGE'
tesla_cache_invalidate.sh — eject + re-insert a g_mass_storage LUN
                            so Tesla clears its USB cache.

USAGE:
    tesla_cache_invalidate.sh [--lun N] [--gadget NAME] [--function NAME]
                              [--eject-ms N] [--dry-run] [--help]

FLAGS:
    --lun N         LUN index to invalidate (default: 1 == media drive)
    --gadget NAME   configfs gadget directory name (default: g1)
    --function NAME mass_storage function dir (default: mass_storage.usb0)
    --eject-ms N    milliseconds between clear and restore (default: 200)
    --dry-run       print the cycle that would be performed; no writes
    --help          this message

ENVIRONMENT OVERRIDES:
    CONFIGFS_ROOT   default /sys/kernel/config/usb_gadget; tests override.
    GADGET, FUNCTION, LUN, EJECT_MS — same as the flags.

EXIT CODES:
    0  success           2  usage error
    3  gadget not bound  4  LUN already empty   5  I/O error
USAGE
}

require_value() {
    # $1 = flag name, $2 = value (may be empty if missing).
    # Bash `set -u` would normally turn a missing `$2` into a noisy
    # "unbound variable" exit 1; explicit check produces a clean
    # exit 2 (usage error) per the documented exit-code contract.
    if [[ -z "${2:-}" ]]; then
        echo "tesla_cache_invalidate.sh: $1 requires a value" >&2
        exit 2
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --lun) require_value "$1" "${2:-}"; LUN="$2"; shift 2 ;;
        --gadget) require_value "$1" "${2:-}"; GADGET="$2"; shift 2 ;;
        --function) require_value "$1" "${2:-}"; FUNCTION="$2"; shift 2 ;;
        --eject-ms) require_value "$1" "${2:-}"; EJECT_MS="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "tesla_cache_invalidate.sh: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

# Validate numeric args before path construction so a bogus --lun
# can't produce a misleading "configfs path missing" diagnostic.
if [[ ! "$LUN" =~ ^[0-9]+$ ]]; then
    echo "tesla_cache_invalidate.sh: --lun must be a non-negative integer (got: $LUN)" >&2
    exit 2
fi
if [[ ! "$EJECT_MS" =~ ^[0-9]+$ ]]; then
    echo "tesla_cache_invalidate.sh: --eject-ms must be a non-negative integer (got: $EJECT_MS)" >&2
    exit 2
fi

LUN_FILE="${CONFIGFS_ROOT}/${GADGET}/functions/${FUNCTION}/lun.${LUN}/file"

if [[ ! -f "$LUN_FILE" ]]; then
    echo "tesla_cache_invalidate.sh: LUN file not found: $LUN_FILE" >&2
    echo "  (gadget not bound, or wrong --gadget / --function / --lun)" >&2
    exit 3
fi

ORIGINAL="$(cat "$LUN_FILE")"
if [[ -z "$ORIGINAL" ]]; then
    echo "tesla_cache_invalidate.sh: LUN $LUN already empty; nothing to invalidate" >&2
    exit 4
fi

restore_lun() {
    # Best-effort restore on any unexpected exit. Errors here cannot
    # change the exit code (trap fires after the main path) but are
    # logged so an operator can diagnose.
    if [[ "$(cat "$LUN_FILE" 2> /dev/null || true)" != "$ORIGINAL" ]]; then
        printf '%s' "$ORIGINAL" > "$LUN_FILE" 2> /dev/null \
            || echo "tesla_cache_invalidate.sh: WARN: failed to restore LUN $LUN to '$ORIGINAL'" >&2
    fi
}
trap restore_lun EXIT INT TERM

if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "DRY-RUN: would clear $LUN_FILE (was '$ORIGINAL'), sleep ${EJECT_MS}ms, restore"
    trap - EXIT INT TERM
    exit 0
fi

# Eject: write empty string. The kernel sends MEDIUM NOT PRESENT.
if ! printf '' > "$LUN_FILE"; then
    echo "tesla_cache_invalidate.sh: failed to clear $LUN_FILE" >&2
    exit 5
fi

# `sleep` accepts fractional seconds on GNU coreutils; convert
# milliseconds to seconds via awk for portability (no bc dependency).
SLEEP_S="$(awk -v ms="$EJECT_MS" 'BEGIN { printf "%.3f", ms/1000 }')"
sleep "$SLEEP_S"

# Re-insert: restore the original backing path. The kernel sends
# UNIT ATTENTION → MEDIUM MAY HAVE CHANGED. Tesla clears its cache.
if ! printf '%s' "$ORIGINAL" > "$LUN_FILE"; then
    echo "tesla_cache_invalidate.sh: failed to restore $LUN_FILE to '$ORIGINAL'" >&2
    exit 5
fi

# Successful completion — clear the trap so a clean exit doesn't
# trigger a redundant restore.
trap - EXIT INT TERM
echo "tesla_cache_invalidate.sh: cycled LUN $LUN (gadget=$GADGET, function=$FUNCTION, eject=${EJECT_MS}ms)"
exit 0
