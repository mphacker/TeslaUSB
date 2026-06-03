#!/usr/bin/env bash
# /usr/local/sbin/recording-watchdog.sh
#
# Installed by setup-lib/13-recording-watchdog.sh and invoked by
# /etc/systemd/system/recording-watchdog.timer every 30 s.
#
# PURPOSE
# -------
# The #1 invariant of this device: the Tesla must ALWAYS be able to
# write dashcam video to the USB drive. The 2026-06-02 incident showed
# how that invariant breaks: an ungraceful Pi reset (a brownout, or the
# 15 s hardware watchdog firing on a BCM43436 SDIO stall) dropped the
# USB/NBD presentation while the car had the drive mounted. The kernel
# logged `nbd0: Other side returned error (5)`, `I/O error, dev nbd0`,
# and dwc2 `ep1 stalled`; the car FLAGGED the drive and stopped
# recording for ~23 h until a manual vehicle power-cycle re-enumerated
# the device.
#
# This watchdog detects the "the drive is faulted / the car dropped
# enumeration" state from UNAMBIGUOUS fault signatures and AUTOMATICALLY
# re-presents the USB gadget (tesla_gadget_rebind.sh --skip-media-reload)
# so the car re-mounts and resumes — WITHOUT ever rebooting.
#
# SCOPE (vs the boot re-present)
# ------------------------------
# This watchdog handles faults that occur DURING a drive (a live kernel
# NBD/USB I/O fault, or the car dropping enumeration). The SILENT
# post-reset case — our side comes back healthy but the car has already
# flagged the drive and will not touch it again until re-enumerated, with
# NO ongoing kernel error — is NOT detectable here; it is handled by a
# separate one-shot boot re-present (a forthcoming component) that
# automates what the manual vehicle power-cycle did. The two compose: the
# boot re-present recovers a reset; this watchdog recovers a live fault.
#
# LOCKED OPERATOR DECISION (2026-06-03)
# -------------------------------------
# Act ONLY on unambiguous fault signatures. NO write-stall / "car asleep
# vs flagged" heuristic — that is the false-positive-prone part and is
# dropped entirely. The watchdog acts only on signals that mean the drive
# is ALREADY faulted/lost from the car's view, where a re-present is
# strictly an improvement and cannot interrupt a healthy write. NEVER
# reboot to recover recording.
#
# FAULT CLASSES (reconciled with rubber-duck + GPT-5.5 review)
# ------------------------------------------------------------
#   HARD fault (act immediately; the write-in-flight guard does NOT
#   block it, because errored writes can still advance the block-layer
#   counter):
#     * a FRESH kernel NBD/block-I/O failure on nbd0 since the last
#       processed journal cursor — `nbd0: Other side returned error`,
#       `I/O error, dev nbd0`, `blk_update_request: I/O error ... nbd0`,
#       `nbd0: Receive control failed`, `nbd0: Connection timed out`,
#       `nbd0: shutting down sockets`.
#
#   SOFT fault (act only when it PERSISTS and no healthy write is in
#   flight — a single-tick wobble during host sleep/wake is ignored):
#     * UDC enumeration drop: state was `configured` on a prior tick and
#       is now NOT `configured` AND NOT `suspended` (suspend == the car
#       is asleep, which is healthy), persisting for ${UDC_PERSIST_TICKS}
#       consecutive ticks.
#
#   STRUCTURAL backend death (raise the DEGRADED marker + daemon.crit;
#   do NOT re-present — re-enumeration cannot fix a dead backend):
#     * /dev/nbd0 size is 0, or the LUN backing file is empty, or
#       teslafat@0 is not active.
#
# HARD SAFETY BOUNDS (so the watchdog can never become the interrupter)
# ---------------------------------------------------------------------
#   * Observe-only until ARMED: the first time we see our own side
#     healthy (UDC configured + LUN backed) after MIN_UPTIME_S, we seed
#     the baseline and arm. We never act on the recurring early-boot
#     enumeration noise (the sector-536890040 EIO reproduces every boot).
#   * Rate limit (uptime-based, so an NTP step can't skew it):
#       - at most ${RATE_MAX_IN_WINDOW} re-present per rolling
#         ${RATE_WINDOW_S}s,
#       - raise DEGRADED after ${WARN_AFTER_BOOT} re-presents this boot,
#       - HARD-STOP actuating after ${HARDSTOP_AFTER_BOOT} this boot
#         (keep observing + logging; never flap).
#   * Never re-present while a healthy write is in flight (SOFT path
#     only): if nbd0 write-sectors advanced this tick, skip.
#   * NEVER reboots. The only reboot path on the device remains the
#     wifi-watchdog last resort.
#   * The re-present primitive (tesla_gadget_rebind.sh) is itself
#     fail-safe: it traps EXIT/TERM and re-presents the gadget if killed
#     mid-rebind, so a reaped tick can never leave TeslaCam detached.
#
# IDEMPOTENCY / RE-ENTRANCY
# -------------------------
# Invoked by a oneshot service from a timer. State is read/written
# atomically (mv-over) under an flock so two ticks can't race the state
# machine. State lives on tmpfs (/run/teslausb) and resets every boot.

set -uo pipefail

readonly STATE_DIR="${RW_STATE_DIR:-/run/teslausb}"
readonly STATE_FILE="${STATE_DIR}/recording-watchdog.state"
readonly LEDGER_FILE="${STATE_DIR}/recording-watchdog.ledger"
readonly LOCK_FILE="${STATE_DIR}/recording-watchdog.lock"
readonly DEGRADED_FILE="${STATE_DIR}/recording_degraded"
readonly LOG_TAG="recording-watchdog"

# --- Tunables -------------------------------------------------------
# Do not act in the first MIN_UPTIME_S after boot — the gadget chain is
# still settling and early enumeration noise is high-false-positive.
readonly MIN_UPTIME_S="${RW_MIN_UPTIME_S:-90}"
# SOFT (UDC-drop) faults must persist this many consecutive ticks.
readonly UDC_PERSIST_TICKS="${RW_UDC_PERSIST_TICKS:-2}"
# Rolling rate-limit window + cap.
readonly RATE_WINDOW_S="${RW_RATE_WINDOW_S:-900}"
readonly RATE_MAX_IN_WINDOW="${RW_RATE_MAX_IN_WINDOW:-1}"
# Per-boot escalation. We raise DEGRADED after WARN_AFTER_BOOT
# re-presents and STOP actuating after HARDSTOP_AFTER_BOOT. Both reviews
# flagged a strict "2 per boot then stop" cap as too low for a long
# multi-hour drive that suffers several independent brownouts/stalls, so
# we warn at 2 but allow up to 4 before standing down.
readonly WARN_AFTER_BOOT="${RW_WARN_AFTER_BOOT:-2}"
readonly HARDSTOP_AFTER_BOOT="${RW_HARDSTOP_AFTER_BOOT:-4}"

# --- Overridable command/paths (for tests) --------------------------
JOURNALCTL="${RW_JOURNALCTL:-journalctl}"
SYSTEMCTL="${RW_SYSTEMCTL:-systemctl}"
LOGGER="${RW_LOGGER:-logger}"
REBIND_CMD="${RW_REBIND_CMD:-/usr/local/bin/tesla_gadget_rebind.sh}"
BOOT_ID_FILE="${RW_BOOT_ID_FILE:-/proc/sys/kernel/random/boot_id}"
UPTIME_FILE="${RW_UPTIME_FILE:-/proc/uptime}"
NBD_STAT_FILE="${RW_NBD_STAT_FILE:-/sys/block/nbd0/stat}"
NBD_SIZE_FILE="${RW_NBD_SIZE_FILE:-/sys/block/nbd0/size}"
TESLAFAT_UNIT="${RW_TESLAFAT_UNIT:-teslafat@0}"
# Skip the teslafat liveness probe (tests without systemd set this).
RW_SKIP_TESLAFAT_CHECK="${RW_SKIP_TESLAFAT_CHECK:-0}"

# UDC/configfs geometry (mirrors tesla_gadget_rebind.sh defaults).
CONFIGFS_ROOT="${RW_CONFIGFS_ROOT:-/sys/kernel/config/usb_gadget}"
GADGET="${RW_GADGET:-g1}"
FUNCTION="${RW_FUNCTION:-mass_storage.usb0}"
# Explicit override for the UDC state file; otherwise the first entry of
# /sys/class/udc/*/state is used.
UDC_STATE_FILE="${RW_UDC_STATE_FILE:-}"
UDC_CLASS_DIR="${RW_UDC_CLASS_DIR:-/sys/class/udc}"

readonly UDC_FILE="${CONFIGFS_ROOT}/${GADGET}/UDC"
readonly LUN0_FILE="${CONFIGFS_ROOT}/${GADGET}/functions/${FUNCTION}/lun.0/file"

# Kernel-log fault signatures (single ERE). All are unambiguous nbd0
# block-I/O / NBD-transport failures. We deliberately match ONLY nbd0
# (the TeslaCam data LUN). The media/chime LUN is nbd1; a fault there
# must NEVER trigger a full gadget re-present that would interrupt a
# healthy nbd0 recording in progress.
readonly HARD_FAULT_RE='nbd0: Other side returned error|I/O error, dev nbd0|blk_update_request: I/O error.*nbd0|nbd0: Receive control failed|nbd0: Connection timed out|nbd0: shutting down sockets'

log() { "${LOGGER}" -t "${LOG_TAG}" -- "$*" 2>/dev/null || true; printf '%s %s\n' "${LOG_TAG}" "$*" >&2; }

# raise_degraded <reason>
#   Mark the device recording-degraded (advisory marker for diagnostics)
#   and log loudly at daemon.crit. Idempotent: only logs the marker
#   transition once, but always emits the crit line so the journal shows
#   every occurrence.
raise_degraded() {
  local reason="$1"
  "${LOGGER}" -t "${LOG_TAG}" -p daemon.crit -- "DEGRADED: ${reason}" 2>/dev/null || true
  printf '%s DEGRADED: %s\n' "${LOG_TAG}" "${reason}" >&2
  if [[ ! -e "${DEGRADED_FILE}" ]]; then
    printf '%s\n' "${reason}" > "${DEGRADED_FILE}" 2>/dev/null || true
  fi
}

clear_degraded() {
  if [[ -e "${DEGRADED_FILE}" ]]; then
    rm -f "${DEGRADED_FILE}" 2>/dev/null || true
    log "recording healthy again — clearing DEGRADED marker"
  fi
}

# kernel_tail_cursor
#   Current head cursor of the kernel journal (so we can seed the
#   baseline to "now" and ignore everything older — including
#   previous-boot logs and the recurring early-boot EIO).
kernel_tail_cursor() {
  timeout 10 "${JOURNALCTL}" -k -n0 --show-cursor --no-pager 2>/dev/null \
    | sed -n 's/^-- cursor: //p' | tail -n1
}

# read_udc_state
#   Emit the gadget's UDC controller state (configured / suspended /
#   not attached / addressed / ...). Empty if unreadable.
read_udc_state() {
  local f="${UDC_STATE_FILE}"
  if [[ -z "${f}" ]]; then
    # /sys/class/udc/<controller> entries are SYMLINKS into /sys/devices,
    # so `find` (which does not descend symlinks without -L) finds
    # nothing. A glob resolves the symlink during pathname expansion, so
    # /sys/class/udc/*/state is the only reliable discovery here.
    local cand
    for cand in "${UDC_CLASS_DIR}"/*/state; do
      [[ -r "${cand}" ]] || continue
      f="${cand}"
      break
    done
  fi
  [[ -n "${f}" && -r "${f}" ]] || { printf ''; return 0; }
  tr -d '[:space:]' < "${f}" 2>/dev/null || true
}

# nbd_write_sectors
#   Field 7 of /sys/block/nbd0/stat == sectors written. -1 if absent.
nbd_write_sectors() {
  if [[ -r "${NBD_STAT_FILE}" ]]; then
    awk '{print $7}' "${NBD_STAT_FILE}" 2>/dev/null || printf -- '-1'
  else
    printf -- '-1'
  fi
}

# nbd_size
#   /sys/block/nbd0/size (in 512-byte sectors). -1 if absent.
nbd_size() {
  if [[ -r "${NBD_SIZE_FILE}" ]]; then
    tr -d '[:space:]' < "${NBD_SIZE_FILE}" 2>/dev/null || printf -- '-1'
  else
    printf -- '-1'
  fi
}

teslafat_active() {
  [[ "${RW_SKIP_TESLAFAT_CHECK}" == "1" ]] && return 0
  "${SYSTEMCTL}" is-active --quiet "${TESLAFAT_UNIT}" 2>/dev/null
}

mkdir -p "${STATE_DIR}" 2>/dev/null || true

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  log "another instance is running; skipping tick"
  exit 0
fi

# --- Load persisted state -------------------------------------------
# Layout: "<boot_id> <armed> <prev_udc> <prev_write> <udc_bad_ticks> <cursor>"
# cursor is last (it contains no whitespace) so a plain read works.
st_boot_id=""
st_armed=0
st_prev_udc=""
st_prev_write=-1
st_udc_bad=0
st_cursor=""
if [[ -r "${STATE_FILE}" ]]; then
  read -r st_boot_id st_armed st_prev_udc st_prev_write st_udc_bad st_cursor < "${STATE_FILE}" || true
  st_armed="${st_armed:-0}"
  st_prev_write="${st_prev_write:--1}"
  st_udc_bad="${st_udc_bad:-0}"
fi

write_state() {
  local tmp="${STATE_FILE}.tmp"
  printf '%s %s %s %s %s %s\n' "$1" "$2" "${3:-_}" "$4" "$5" "${6:-}" > "${tmp}"
  mv -f "${tmp}" "${STATE_FILE}"
}

# Current uptime (integer seconds).
uptime_s=0
if [[ -r "${UPTIME_FILE}" ]]; then
  read -r uptime_s _ < "${UPTIME_FILE}" || true
  uptime_s="${uptime_s%%.*}"
  [[ "${uptime_s}" =~ ^[0-9]+$ ]] || uptime_s=0
fi

cur_boot_id="$(tr -d '[:space:]' < "${BOOT_ID_FILE}" 2>/dev/null || true)"

# --- Reboot detection: reset everything on a new boot ----------------
if [[ "${cur_boot_id}" != "${st_boot_id}" ]]; then
  log "new boot (${cur_boot_id:-?}) — resetting watchdog state, observe-only baseline"
  st_boot_id="${cur_boot_id}"
  st_armed=0
  st_prev_udc=""
  st_prev_write=-1
  st_udc_bad=0
  st_cursor="$(kernel_tail_cursor)"
  : > "${LEDGER_FILE}" 2>/dev/null || true
fi

# A persisted "_" sentinel means "no previous udc value".
[[ "${st_prev_udc}" == "_" ]] && st_prev_udc=""

# --- Read current signals -------------------------------------------
udc_state="$(read_udc_state)"
write_sectors="$(nbd_write_sectors)"
size_sectors="$(nbd_size)"
lun_backing="$(cat "${LUN0_FILE}" 2>/dev/null || true)"
udc_bound="$(cat "${UDC_FILE}" 2>/dev/null || true)"

# --- Observe-only baseline: seed + arm, never act -------------------
if [[ "${st_armed}" != "1" ]]; then
  if (( uptime_s >= MIN_UPTIME_S )) \
       && [[ -n "${udc_bound}" && -n "${lun_backing}" && "${udc_state}" == "configured" ]]; then
    st_armed=1
    log "armed (udc=configured, lun backed, uptime=${uptime_s}s)"
  else
    log "observe-only (uptime=${uptime_s}s udc='${udc_state}' bound=$([[ -n ${udc_bound} ]] && echo y || echo n) lun=$([[ -n ${lun_backing} ]] && echo y || echo n))"
  fi
  # Always advance the cursor + baseline so we never act on pre-arm logs.
  st_cursor="$(kernel_tail_cursor)"
  write_state "${st_boot_id}" "${st_armed}" "${udc_state}" "${write_sectors}" 0 "${st_cursor}"
  exit 0
fi

# ====================================================================
# ARMED — fault detection
# ====================================================================

# --- Kernel fault scan (HARD) ---------------------------------------
fault_hard=""
if [[ -n "${st_cursor}" ]]; then
  scan_out="$(timeout 10 "${JOURNALCTL}" -k --after-cursor "${st_cursor}" -o cat --show-cursor --no-pager 2>/dev/null)"
  scan_rc=$?
  new_cursor="$(printf '%s\n' "${scan_out}" | sed -n 's/^-- cursor: //p' | tail -n1)"
  msgs="$(printf '%s\n' "${scan_out}" | grep -v '^-- cursor: ' || true)"
  if (( scan_rc != 0 )) || [[ -z "${new_cursor}" ]]; then
    # journalctl failed or the cursor was vacuumed away — re-seed to the
    # tail and do NOT act on the kernel signal this tick (we can't trust
    # what we read). UDC + structural checks below still apply.
    log "kernel scan unavailable / invalid cursor — re-seeding (rc=${scan_rc})"
    st_cursor="$(kernel_tail_cursor)"
    msgs=""
  else
    st_cursor="${new_cursor}"
  fi
  if [[ -n "${msgs}" ]] && printf '%s\n' "${msgs}" | grep -E -q -- "${HARD_FAULT_RE}"; then
    fault_hard="kernel-nbd-fault"
    log "HARD fault signature in kernel log: $(printf '%s\n' "${msgs}" | grep -E -- "${HARD_FAULT_RE}" | head -n1)"
  fi
else
  st_cursor="$(kernel_tail_cursor)"
fi

# --- UDC drop (SOFT, persistent) ------------------------------------
# Count CONSECUTIVE ticks in a "bad" controller state — not the
# configured->bad edge, which only ever lasts one tick once we record
# the bad value. A bad state is anything that is neither "configured"
# (healthy) nor "suspended" (the car is asleep, which is healthy). We
# also require our gadget to still be BOUND (UDC configfs file
# non-empty): if the gadget was deliberately torn down (our own
# rebind, maintenance hide), that is not a fault to recover.
fault_soft=""
if [[ -n "${udc_state}" && "${udc_state}" != "configured" && "${udc_state}" != "suspended" \
        && -n "${udc_bound}" ]]; then
  st_udc_bad=$((st_udc_bad + 1))
else
  st_udc_bad=0
fi
if (( st_udc_bad >= UDC_PERSIST_TICKS )); then
  fault_soft="udc-drop(${st_prev_udc:-?}->${udc_state} x${st_udc_bad})"
fi

# --- Structural backend death (DEGRADED, never re-present) ----------
# A re-present cannot fix a dead/absent backend, so these raise DEGRADED
# instead of actuating. nbd0 size is "dead" if it is not a positive
# integer: 0 (kernel reports a zero-sized device), -1 (our sentinel for
# an absent /sys/block/nbd0/size), or any non-numeric/empty read.
backend_dead=""
[[ "${size_sectors}" =~ ^[1-9][0-9]*$ ]] || backend_dead+="nbd0-size-bad(${size_sectors}) "
[[ -z "${lun_backing}" ]] && backend_dead+="lun-empty "
if ! teslafat_active; then backend_dead+="teslafat-inactive "; fi

# --- Write-in-flight guard (SOFT path only) -------------------------
write_advanced=false
if [[ "${st_prev_write}" =~ ^[0-9]+$ && "${write_sectors}" =~ ^[0-9]+$ ]] \
     && (( write_sectors > st_prev_write )); then
  write_advanced=true
fi

# --- Decide ---------------------------------------------------------
act_reason=""
if [[ -n "${fault_hard}" ]]; then
  if [[ -n "${backend_dead}" ]]; then
    raise_degraded "hard fault (${fault_hard}) but backend dead (${backend_dead% }) — re-present cannot fix"
  else
    act_reason="HARD:${fault_hard}"
  fi
elif [[ -n "${fault_soft}" ]]; then
  if [[ -n "${backend_dead}" ]]; then
    raise_degraded "soft fault (${fault_soft}) but backend dead (${backend_dead% }) — re-present cannot fix"
  elif ${write_advanced}; then
    log "soft fault ${fault_soft} but writes advancing (${st_prev_write}->${write_sectors}) — not acting"
  else
    act_reason="SOFT:${fault_soft}"
  fi
elif [[ -n "${backend_dead}" ]]; then
  raise_degraded "backend dead (${backend_dead% })"
fi

# --- Actuate (under hard bounds) ------------------------------------
if [[ -n "${act_reason}" ]]; then
  # Count this-boot actuations: total (boot_count) and within the
  # rolling window (window_count). Ledger holds one uptime-second stamp
  # per actuation this boot; it is wiped on reboot.
  boot_count=0
  window_count=0
  if [[ -r "${LEDGER_FILE}" ]]; then
    while read -r ts; do
      [[ "${ts}" =~ ^[0-9]+$ ]] || continue
      boot_count=$((boot_count + 1))
      if (( uptime_s - ts <= RATE_WINDOW_S )); then
        window_count=$((window_count + 1))
      fi
    done < "${LEDGER_FILE}"
  fi

  if (( boot_count >= HARDSTOP_AFTER_BOOT )); then
    raise_degraded "re-present HARD-STOP: ${boot_count} actuations this boot (trigger ${act_reason}) — standing down to avoid flapping"
  elif (( window_count >= RATE_MAX_IN_WINDOW )); then
    log "rate-limited: ${window_count} re-present(s) in last ${RATE_WINDOW_S}s — skipping (trigger ${act_reason})"
  else
    log "ACTUATING re-present — trigger=${act_reason} evidence: udc='${udc_state}' write=${st_prev_write}->${write_sectors} nbd_size=${size_sectors} lun='${lun_backing}'"
    printf '%s\n' "${uptime_s}" >> "${LEDGER_FILE}" 2>/dev/null || true
    if (( boot_count + 1 >= WARN_AFTER_BOOT )); then
      raise_degraded "re-present #$((boot_count + 1)) this boot (trigger ${act_reason}) — recurring fault, watching"
    fi
    if "${REBIND_CMD}" --skip-media-reload; then
      log "re-present completed OK (trigger ${act_reason})"
    else
      rc=$?
      raise_degraded "re-present FAILED rc=${rc} (trigger ${act_reason}) — TeslaCam may be detached"
    fi
    st_udc_bad=0
  fi
else
  # Fully healthy tick — stand down any stale DEGRADED advisory.
  if [[ -z "${backend_dead}" && "${udc_state}" == "configured" ]]; then
    clear_degraded
  fi
fi

write_state "${st_boot_id}" "${st_armed}" "${udc_state}" "${write_sectors}" "${st_udc_bad}" "${st_cursor}"
exit 0
