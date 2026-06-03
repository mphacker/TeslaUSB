#!/usr/bin/env bash
#
# tesla_gadget_rebind.sh — force Tesla to re-read the USB gadget by
# simulating a USB re-plug (UDC unbind + rebind).
#
# Why: Tesla caches the custom lock chime (LockChime.wav at the root of
# the MEDIA partition — partition 2 of the single dashcam device) and
# only re-reads it on a fresh USB *enumeration*. A soft SCSI
# medium-change (tesla_cache_invalidate.sh, ~200 ms) is enough for
# Tesla to re-read directory listings (new music tracks, LightShow
# files), but it is NOT enough for the lock chime — the car keeps
# playing the OLD chime until the device is re-enumerated. v1 solved
# this the same way (rebind_usb_gadget): unbind the gadget UDC, pause,
# rebind. That re-plug is the only mechanism observed to make the car
# pick up a changed chime.
#
# What this does:
#   1. `sync` so the just-written LockChime.wav (and any clip the car
#      just finished) is flushed to the media backing store before the
#      daemon re-walks it.
#   2. SIGHUP the teslafat instance (teslafat@0) so it re-walks the
#      media partition'"'"'s backing tree and atomically swaps in a FRESH
#      synthesised exFAT view, then BLOCK (bounded) until the daemon
#      logs that the swap went live. Only the media partition reloads
#      on SIGHUP (the TeslaCam partition sets reload_on_sighup=false in
#      its DiskConfig), so the marker unambiguously means the chime
#      view is fresh. This MUST happen before the rebind: teslafat
#      snapshots its directory tree in memory, so without the re-walk
#      the rebind would just re-present the STALE chime and the car
#      would keep playing the old sound. The wait is gated on the
#      daemon'"'"'s RELOAD_LIVE_MARKER journal token (a contract with
#      teslafat'"'"'s main.rs). This step runs while the gadget is still
#      bound, so a failure here leaves TeslaCam fully attached.
#   3. Capture the current UDC + the LUN backing file path.
#   4. Unbind the gadget UDC (Tesla sees an eject of the device).
#   5. Wait briefly for the disconnect to settle.
#   6. Re-restore the LUN backing file if the unbind cleared it
#      (defensive — mirrors v1, which observed unbind clearing LUNs).
#   7. Rebind the UDC (Tesla re-enumerates, re-reads the fresh chime).
#   8. Wait (bounded) for the gadget to come back fully healthy — UDC
#      bound AND the LUN file re-backed — before returning success,
#      so the caller knows TeslaCam recording can resume.
#
# Safety: this briefly (~settle + rebind, ~2-4 s) detaches the single
# USB device from the car, including the TeslaCam partition. It is
# invoked ONLY for the rare, deliberate act of changing the active lock
# chime. The bounded health wait makes a failure to recover loud and
# fast (non-zero exit) rather than silently leaving TeslaCam detached.
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
#   6  media teslafat re-walk did not go live within --reload-timeout
#      (gadget left untouched — TeslaCam still attached)

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
# teslafat systemd instance to SIGHUP for the pre-rebind re-walk.
# The single teslafat@0 serves the whole partitioned disk; only its
# media partition (partition 2, holding LockChime.wav) opts into
# reload_on_sighup, so the re-walk refreshes the chime view while the
# continuously-recorded TeslaCam partition is left untouched.
TESLAFAT_MEDIA_UNIT="${TESLAFAT_MEDIA_UNIT:-teslafat@0}"
# How long to wait for teslafat@0 to log that its media re-walk swapped
# live. The media partition is read-mostly so the swap is near-instant;
# 15 s covers a brief overlapping host write (teslafat retries the
# gated swap for ~10 s) plus journal latency.
RELOAD_TIMEOUT_S="${RELOAD_TIMEOUT_S:-15}"
# Stable journal token teslafat logs the instant the re-walk goes live.
# CONTRACT: must equal RELOAD_LIVE_MARKER in
# rust/crates/teslafat/src/main.rs. Keep the two in lock-step.
RELOAD_LIVE_MARKER="${RELOAD_LIVE_MARKER:-teslafat-reload-live}"
DRY_RUN=0
# When 1, skip the pre-rebind media (chime) SIGHUP re-walk entirely and
# go straight to the UDC unbind/rebind. The recording-liveness watchdog
# uses this: recovering TeslaCam recording after a fault must NOT depend
# on — or be blocked by (exit 6) — the chime partition's re-walk, which
# is irrelevant to dashcam recording. Default 0 preserves the original
# chime-change behaviour for the web UI caller.
SKIP_MEDIA_RELOAD=0

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
                           [--settle N] [--timeout N]
                           [--media-unit UNIT] [--reload-timeout N]
                           [--dry-run] [--help]

FLAGS:
    --gadget NAME      configfs gadget directory name (default: g1)
    --function NAME    mass_storage function dir (default: mass_storage.usb0)
    --settle N         seconds between unbind and rebind (default: 2)
    --timeout N        seconds to wait for gadget recovery (default: 30)
    --media-unit UNIT  teslafat systemd instance whose media partition
                       to SIGHUP before rebind (default: teslafat@0)
    --reload-timeout N seconds to wait for the media re-walk to go live
                       (default: 15)
    --skip-media-reload  skip the pre-rebind chime re-walk entirely and
                       go straight to the UDC unbind/rebind (used by the
                       recording-liveness watchdog; recording recovery
                       must not depend on the chime partition)
    --dry-run          print what would happen; no UDC change
    --help             this message

EXIT CODES:
    0  success            2  usage error
    3  prerequisite missing
    5  rebind failed or gadget did not recover in time
    6  media teslafat re-walk did not go live in time (gadget untouched)
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
        --media-unit) require_value "$1" "${2:-}"; TESLAFAT_MEDIA_UNIT="$2"; shift 2 ;;
        --reload-timeout) require_value "$1" "${2:-}"; RELOAD_TIMEOUT_S="$2"; shift 2 ;;
        --skip-media-reload) SKIP_MEDIA_RELOAD=1; shift ;;
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
if [[ ! "$RELOAD_TIMEOUT_S" =~ ^[0-9]+$ ]]; then
    echo "tesla_gadget_rebind.sh: --reload-timeout must be a non-negative integer (got: $RELOAD_TIMEOUT_S)" >&2
    exit 2
fi
# $TESLAFAT_MEDIA_UNIT is passed to systemctl/journalctl. Restrict it to
# a safe systemd-unit charset (template instances use '@') so a caller
# cannot inject shell metacharacters or extra arguments.
if [[ ! "$TESLAFAT_MEDIA_UNIT" =~ ^[A-Za-z0-9._@-]+$ ]]; then
    echo "tesla_gadget_rebind.sh: --media-unit must match [A-Za-z0-9._@-]+ (got: $TESLAFAT_MEDIA_UNIT)" >&2
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

# Re-restore the LUN backing file if the unbind cleared it, so the gadget
# comes back with the device intact (TeslaCam must not vanish). Defined
# early so the abnormal-exit safety net below can call it.
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

# Abnormal-exit safety net. The window between "unbind the UDC" and
# "rebind the UDC" is the ONE interval where the car sees no device. If
# this script is KILLED in that window — e.g. the recording-liveness
# watchdog's systemd TimeoutStartSec reaps a slow rebind, or an operator
# Ctrl-C's it — a naive script would leave TeslaCam detached and the
# watchdog would have become the very interrupter it exists to prevent.
# The trap re-presents the gadget (restoring the LUN backing first) on
# ANY exit taken while the device is unbound, so a fatal interruption
# still leaves the car with a presented drive. It is a strict no-op on
# the normal success path (the flag is cleared once PRESENT_USB runs)
# and on every early exit before the unbind.
_REBIND_USB_HIDDEN=0
_rebind_safety_net() {
    [[ "$_REBIND_USB_HIDDEN" -eq 1 ]] || return 0
    _REBIND_USB_HIDDEN=0
    echo "tesla_gadget_rebind.sh: interrupted while gadget unbound — re-presenting to keep TeslaCam attached" >&2
    restore_lun "$LUN0_FILE" "$lun0_was"
    "$PRESENT_USB" >/dev/null 2>&1 || \
        echo "tesla_gadget_rebind.sh: safety-net re-present FAILED — operator must inspect usb-gadget" >&2
}
# EXIT always runs the net (it is a no-op once presented). INT/TERM run
# the net and then EXIT explicitly with the conventional signal code, so
# the script cannot RESUME the rebind sequence after an interrupt and
# double-present (the EXIT trap then sees the flag already cleared and
# no-ops).
trap _rebind_safety_net EXIT
trap '_rebind_safety_net; exit 130' INT
trap '_rebind_safety_net; exit 143' TERM

if [[ "$DRY_RUN" -eq 1 ]]; then
    if [[ "$SKIP_MEDIA_RELOAD" -eq 1 ]]; then
        echo "DRY-RUN: would 'sync' (SKIPPING media re-walk), then '${HIDE_USB}'," \
             "sleep ${SETTLE_S}s, restore the LUN file if cleared, '${PRESENT_USB}'," \
             "then wait up to ${RECOVER_TIMEOUT_S}s for UDC + the LUN file to re-back"
    else
        echo "DRY-RUN: would 'sync', SIGHUP ${TESLAFAT_MEDIA_UNIT} and wait up to" \
             "${RELOAD_TIMEOUT_S}s for '${RELOAD_LIVE_MARKER}', then '${HIDE_USB}'," \
             "sleep ${SETTLE_S}s, restore the LUN file if cleared, '${PRESENT_USB}'," \
             "then wait up to ${RECOVER_TIMEOUT_S}s for UDC + the LUN file to re-back"
    fi
    exit 0
fi

# Send the media teslafat instance a SIGHUP and block until it logs that
# the re-walk swapped live, so the subsequent rebind re-presents the
# FRESH chime rather than the stale in-memory snapshot. Runs while the
# gadget is still bound, so any failure here is safe (TeslaCam stays
# attached) and returns exit 6 without ever touching the UDC.
reload_media_lun() {
    # Mark the journal cursor *before* the SIGHUP so we only match a
    # swap caused by THIS request, not a stale one from an earlier run.
    local since
    since="@$(date +%s)"
    echo "tesla_gadget_rebind.sh: re-walking media LUN (SIGHUP ${TESLAFAT_MEDIA_UNIT})..."
    if ! systemctl kill -s HUP "$TESLAFAT_MEDIA_UNIT" 2> /dev/null; then
        echo "tesla_gadget_rebind.sh: could not SIGHUP ${TESLAFAT_MEDIA_UNIT}" \
             "(not running?) — refusing to rebind a stale view" >&2
        return 1
    fi
    local deadline
    deadline=$(( $(date +%s) + RELOAD_TIMEOUT_S ))
    while true; do
        if journalctl -u "$TESLAFAT_MEDIA_UNIT" --since "$since" --no-pager -o cat 2> /dev/null \
                | grep -q -- "$RELOAD_LIVE_MARKER"; then
            echo "tesla_gadget_rebind.sh: media re-walk is live"
            return 0
        fi
        if [[ "$(date +%s)" -ge "$deadline" ]]; then
            echo "tesla_gadget_rebind.sh: media re-walk did NOT go live within" \
                 "${RELOAD_TIMEOUT_S}s — refusing to rebind a stale view" >&2
            return 1
        fi
        sleep 1
    done
}

# 1. Flush dirty backing writes (incl. the just-written LockChime.wav)
#    so the daemon's re-walk sees the new bytes. Bounded: a faulted NBD
#    backing can wedge a bare `sync` indefinitely, which would freeze
#    the recording-liveness watchdog tick — cap it so a stuck flush can
#    never block recovery. A timed-out sync is non-fatal (we proceed to
#    re-present regardless; durability of the last bytes is best-effort).
timeout 15 sync \
    || echo "tesla_gadget_rebind.sh: WARNING: sync did not complete in 15s — proceeding" >&2

# 2. Re-walk the media LUN so teslafat serves the fresh chime, and wait
#    for the swap to go live BEFORE we re-enumerate. Failure here leaves
#    the gadget bound (TeslaCam safe) and exits 6.
#
#    --skip-media-reload bypasses this entirely: the recording-liveness
#    watchdog recovers TeslaCam by re-presenting the gadget, and that
#    recovery must never depend on (or be blocked by) the chime
#    partition's re-walk. The `sync` above still flushed pending writes.
if [[ "$SKIP_MEDIA_RELOAD" -eq 1 ]]; then
    echo "tesla_gadget_rebind.sh: --skip-media-reload set; skipping chime re-walk"
    # B2 sanity: the LUN should already be backed by a block device
    # (normally /dev/nbd0). A missing/empty backing means re-presenting
    # alone cannot restore recording — log it loudly but still proceed,
    # because restore_lun below re-applies the captured backing.
    if [[ -z "$lun0_was" ]]; then
        echo "tesla_gadget_rebind.sh: WARNING: LUN backing is empty before rebind" \
             "(no captured device to restore) — gadget may not recover" >&2
    fi
elif ! reload_media_lun; then
    exit 6
fi

# 3. Unbind the UDC (Tesla "ejects" the device). From here until the
#    rebind below the car sees no drive; the EXIT/INT/TERM safety net
#    re-presents the gadget if this script is killed in that window.
#    Arm the safety net BEFORE the unbind: if a signal lands in the tiny
#    gap after HIDE_USB unbinds the UDC but before the flag is set, the
#    trap would otherwise see a "not hidden" flag and leave TeslaCam
#    detached. PRESENT_USB is idempotent, so arming early is safe even if
#    HIDE_USB fails (the trap simply re-presents an already-bound gadget).
echo "tesla_gadget_rebind.sh: unbinding USB gadget (Tesla sees eject)..."
_REBIND_USB_HIDDEN=1
if ! "$HIDE_USB"; then
    echo "tesla_gadget_rebind.sh: ${HIDE_USB} failed" >&2
    exit 5
fi

# 4. Let the disconnect settle on the host side.
sleep "$SETTLE_S"

# 5. Re-restore the LUN backing file if the unbind cleared it, so the
#    gadget comes back with the device intact (TeslaCam must not vanish).
restore_lun "$LUN0_FILE" "$lun0_was"

# 6. Rebind the UDC (Tesla re-enumerates, re-reads LockChime.wav).
echo "tesla_gadget_rebind.sh: rebinding USB gadget (Tesla re-enumerates)..."
if ! "$PRESENT_USB"; then
    echo "tesla_gadget_rebind.sh: ${PRESENT_USB} failed" >&2
    exit 5
fi
# Gadget is presented again — stand down the safety net so a later
# normal exit (or even a kill during the health wait) does not double
# re-present.
_REBIND_USB_HIDDEN=0

# 7. Wait (bounded) for full health: UDC bound AND the LUN file backed.
deadline=$(( $(date +%s) + RECOVER_TIMEOUT_S ))
while true; do
    udc="$(cat "$UDC_FILE" 2> /dev/null || true)"
    lun0="$(cat "$LUN0_FILE" 2> /dev/null || true)"
    if [[ -n "$udc" && -n "$lun0" ]]; then
        echo "tesla_gadget_rebind.sh: gadget healthy (UDC=${udc}, lun0=${lun0})"
        exit 0
    fi
    if [[ "$(date +%s)" -ge "$deadline" ]]; then
        echo "tesla_gadget_rebind.sh: gadget did NOT recover within ${RECOVER_TIMEOUT_S}s" \
             "(UDC='${udc}' lun0='${lun0}')" >&2
        echo "  TeslaCam may be detached — operator should inspect" \
             "'systemctl status usb-gadget.service teslafat@0'" >&2
        exit 5
    fi
    sleep 1
done
