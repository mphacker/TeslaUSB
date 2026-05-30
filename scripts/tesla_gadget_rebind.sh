#!/usr/bin/env bash
#
# tesla_gadget_rebind.sh — force Tesla to re-read the USB gadget by
# simulating a USB re-plug (UDC unbind + rebind).
#
# Why: Tesla caches the custom lock chime (LockChime.wav at the root of
# the MEDIA LUN) and only re-reads it on a fresh USB *enumeration*. A
# soft SCSI medium-change (tesla_cache_invalidate.sh, ~200 ms) is enough
# for Tesla to re-read directory listings (new music tracks, LightShow
# files), but it is NOT enough for the lock chime — the car keeps
# playing the OLD chime until the device is re-enumerated. v1 solved
# this the same way (rebind_usb_gadget): unbind the gadget UDC, pause,
# rebind. That re-plug is the only mechanism observed to make the car
# pick up a changed chime.
#
# What this does:
#   1. `sync` so any clip the car just finished is flushed to the
#      backing store before we detach.
#   2. Capture the current UDC + both LUN backing file paths.
#   3. Unbind the gadget UDC (Tesla sees an eject of BOTH LUNs).
#   4. Wait briefly for the disconnect to settle.
#   5. Re-restore the LUN backing files if the unbind cleared them
#      (defensive — mirrors v1, which observed unbind clearing LUNs).
#   6. Rebind the UDC (Tesla re-enumerates, re-reads the chime).
#   7. Wait (bounded) for the gadget to come back fully healthy — UDC
#      bound AND both LUN files re-backed — before returning success,
#      so the caller knows TeslaCam recording can resume.
#
# Safety: this briefly (~settle + rebind, ~2-4 s) detaches BOTH LUNs
# from the car, including TeslaCam. It is invoked ONLY for the rare,
# deliberate act of changing the active lock chime. The bounded health
# wait makes a failure to recover loud and fast (non-zero exit) rather
# than silently leaving TeslaCam detached.
#
# Why reuse teslausb-hide-usb / teslausb-present-usb: those are the
# tested, idempotent UDC primitives installed by setup-lib/11-gadget.sh.
# This wrapper composes them and adds the `sync`, LUN-restore safety net,
# and bounded health verification that activation needs.
#
# Idempotency: safe to run repeatedly. Each run re-enumerates once and
# re-verifies health.
#
# Privilege: writing the configfs UDC + LUN files requires root. The web
# app invokes this as:
#     sudo -n /usr/local/bin/tesla_gadget_rebind.sh
# The NOPASSWD grant (!requiretty) lives in the B-1 sudoers allowlist
# (B1_SUDOERS_ALLOWLIST in setup-lib/02-users.sh, rendered into
# /etc/sudoers.d/teslausb-b1). The script is installed to /usr/local/bin
# by setup-lib/04-units.sh.
#
# Exit codes:
#   0  success — gadget re-enumerated and healthy
#   2  usage error (bad flag)
#   3  prerequisite missing (configfs gadget dir or helper scripts absent)
#   5  rebind failed, or the gadget did not return healthy in time

set -uo pipefail

# Defaults match the live B-1 USB gadget. See setup-lib/11-gadget.sh.
GADGET="${GADGET:-g1}"
FUNCTION="${FUNCTION:-mass_storage.usb0}"
# Seconds to wait between unbind and rebind so the host (Tesla) registers
# the disconnect. v1 used 1-2 s; 2 s is comfortably safe.
SETTLE_S="${SETTLE_S:-2}"
# How long to wait for the gadget to come back healthy after the rebind.
# Measured recovery is sub-second; 30 s is "the rebind wedged" rather
# than "this is slow".
RECOVER_TIMEOUT_S="${RECOVER_TIMEOUT_S:-30}"
DRY_RUN=0

CONFIGFS_ROOT="${CONFIGFS_ROOT:-/sys/kernel/config/usb_gadget}"
HIDE_USB="${HIDE_USB:-/usr/local/bin/teslausb-hide-usb}"
PRESENT_USB="${PRESENT_USB:-/usr/local/bin/teslausb-present-usb}"

usage() {
    cat <<'USAGE'
tesla_gadget_rebind.sh — unbind + rebind the USB gadget UDC so Tesla
                         re-enumerates and re-reads a changed
                         LockChime.wav.

USAGE:
    tesla_gadget_rebind.sh [--gadget NAME] [--function NAME]
                           [--settle N] [--timeout N] [--dry-run] [--help]

FLAGS:
    --gadget NAME   configfs gadget directory name (default: g1)
    --function NAME mass_storage function dir (default: mass_storage.usb0)
    --settle N      seconds between unbind and rebind (default: 2)
    --timeout N     seconds to wait for gadget recovery (default: 30)
    --dry-run       print what would happen; no UDC change
    --help          this message

EXIT CODES:
    0  success            2  usage error
    3  prerequisite missing
    5  rebind failed or gadget did not recover in time
USAGE
}

require_value() {
    if [[ -z "${2:-}" ]]; then
        echo "tesla_gadget_rebind.sh: $1 requires a value" >&2
        exit 2
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --gadget) require_value "$1" "${2:-}"; GADGET="$2"; shift 2 ;;
        --function) require_value "$1" "${2:-}"; FUNCTION="$2"; shift 2 ;;
        --settle) require_value "$1" "${2:-}"; SETTLE_S="$2"; shift 2 ;;
        --timeout) require_value "$1" "${2:-}"; RECOVER_TIMEOUT_S="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --help | -h) usage; exit 0 ;;
        *) echo "tesla_gadget_rebind.sh: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ ! "$SETTLE_S" =~ ^[0-9]+$ ]]; then
    echo "tesla_gadget_rebind.sh: --settle must be a non-negative integer (got: $SETTLE_S)" >&2
    exit 2
fi
if [[ ! "$RECOVER_TIMEOUT_S" =~ ^[0-9]+$ ]]; then
    echo "tesla_gadget_rebind.sh: --timeout must be a non-negative integer (got: $RECOVER_TIMEOUT_S)" >&2
    exit 2
fi
# $GADGET / $FUNCTION are interpolated into configfs paths below. Restrict
# them to a safe charset so a caller cannot use path-traversal (e.g.
# `--gadget ../../etc`) to point the configfs writes outside the gadget
# tree. The web app always invokes with zero args, but the sudoers grant
# permits arbitrary args, so validate here as defense-in-depth.
if [[ ! "$GADGET" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "tesla_gadget_rebind.sh: --gadget must match [A-Za-z0-9._-]+ (got: $GADGET)" >&2
    exit 2
fi
if [[ ! "$FUNCTION" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "tesla_gadget_rebind.sh: --function must match [A-Za-z0-9._-]+ (got: $FUNCTION)" >&2
    exit 2
fi

GADGET_DIR="${CONFIGFS_ROOT}/${GADGET}"
UDC_FILE="${GADGET_DIR}/UDC"
LUN0_FILE="${GADGET_DIR}/functions/${FUNCTION}/lun.0/file"
LUN1_FILE="${GADGET_DIR}/functions/${FUNCTION}/lun.1/file"

if [[ ! -d "$GADGET_DIR" ]]; then
    echo "tesla_gadget_rebind.sh: configfs gadget dir not found: $GADGET_DIR" >&2
    echo "  (gadget not composed, or wrong --gadget)" >&2
    exit 3
fi
for helper in "$HIDE_USB" "$PRESENT_USB"; do
    if [[ ! -x "$helper" ]]; then
        echo "tesla_gadget_rebind.sh: required helper missing or not executable: $helper" >&2
        exit 3
    fi
done

# Capture the current backing so we can re-restore if the unbind clears
# the LUN file attributes (observed in v1's rebind path).
lun0_was="$(cat "$LUN0_FILE" 2> /dev/null || true)"
lun1_was="$(cat "$LUN1_FILE" 2> /dev/null || true)"

if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "DRY-RUN: would 'sync', '${HIDE_USB}', sleep ${SETTLE_S}s," \
         "restore LUN files if cleared, '${PRESENT_USB}', then wait up to" \
         "${RECOVER_TIMEOUT_S}s for UDC + both LUN files to re-back"
    exit 0
fi

# 1. Flush dirty backing writes before detaching.
sync

# 2. Unbind the UDC (Tesla "ejects" both drives).
echo "tesla_gadget_rebind.sh: unbinding USB gadget (Tesla sees eject)..."
if ! "$HIDE_USB"; then
    echo "tesla_gadget_rebind.sh: ${HIDE_USB} failed" >&2
    exit 5
fi

# 3. Let the disconnect settle on the host side.
sleep "$SETTLE_S"

# 4. Re-restore LUN backing files if the unbind cleared them, so the
#    gadget comes back with BOTH drives intact (TeslaCam must not vanish).
restore_lun() {
    local lun_file="$1" want="$2"
    [[ -z "$want" ]] && return 0
    local now
    now="$(cat "$lun_file" 2> /dev/null || true)"
    if [[ -z "$now" ]]; then
        echo "tesla_gadget_rebind.sh: restoring backing for $(basename "$(dirname "$lun_file")")=${want}"
        echo "$want" > "$lun_file" 2> /dev/null || true
    fi
}
restore_lun "$LUN0_FILE" "$lun0_was"
restore_lun "$LUN1_FILE" "$lun1_was"

# 5. Rebind the UDC (Tesla re-enumerates, re-reads LockChime.wav).
echo "tesla_gadget_rebind.sh: rebinding USB gadget (Tesla re-enumerates)..."
if ! "$PRESENT_USB"; then
    echo "tesla_gadget_rebind.sh: ${PRESENT_USB} failed" >&2
    exit 5
fi

# 6. Wait (bounded) for full health: UDC bound AND both LUN files backed.
deadline=$(( $(date +%s) + RECOVER_TIMEOUT_S ))
while true; do
    udc="$(cat "$UDC_FILE" 2> /dev/null || true)"
    lun0="$(cat "$LUN0_FILE" 2> /dev/null || true)"
    lun1="$(cat "$LUN1_FILE" 2> /dev/null || true)"
    if [[ -n "$udc" && -n "$lun0" && -n "$lun1" ]]; then
        echo "tesla_gadget_rebind.sh: gadget healthy (UDC=${udc}, lun0=${lun0}, lun1=${lun1})"
        exit 0
    fi
    if [[ "$(date +%s)" -ge "$deadline" ]]; then
        echo "tesla_gadget_rebind.sh: gadget did NOT recover within ${RECOVER_TIMEOUT_S}s" \
             "(UDC='${udc}' lun0='${lun0}' lun1='${lun1}')" >&2
        echo "  TeslaCam may be detached — operator should inspect" \
             "'systemctl status usb-gadget.service teslafat@0 teslafat@1'" >&2
        exit 5
    fi
    sleep 1
done
