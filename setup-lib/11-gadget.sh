#!/usr/bin/env bash
# setup-lib/11-gadget.sh — Phase 6.11 (USB gadget pipeline)
#
# Wires up the customer-facing USB gadget so Tesla sees one mass-
# storage LUN backed by a single teslafat-served, MBR-partitioned
# disk. This step is what makes B-1 actually USEFUL — without it
# teslafat is just a daemon talking to an empty socket and nothing
# reaches the car.
#
# Pipeline (in startup order):
#
#   teslafat@0.service                                 (Phase 6.4)
#         │ AF_UNIX sock (/run/teslausb/teslafat-0.sock)
#         ▼
#   nbd-attach@0.service                               (this step)
#   nbd-client -unix … sock /dev/nbd0
#         │ whole synthesized disk: MBR + two exFAT partitions
#         ▼
#   ┌────────────────────────────────────────────────┐
#   │ usb-gadget.service  — configfs g1               │
#   │   functions/mass_storage.usb0/                  │
#   │     lun.0/file = /dev/nbd0                      │
#   │   UDC = <dwc2 UDC name from /sys/class/udc/>    │
#   └────────────────────────────────────────────────┘
#         │
#         ▼
#       Tesla
#
# === What this step installs ===
#
#   * /etc/modprobe.d/teslausb-nbd.conf — `options nbd nbds_max=1 max_part=0`
#   * /etc/modules-load.d/teslausb.conf — load `nbd` at boot
#   * /etc/teslausb/teslafat-0.toml — DiskConfig: TeslaCam + media exFAT partitions
#   * /etc/systemd/system/nbd-attach@.service — templated attach unit
#   * /etc/systemd/system/usb-gadget.service — configfs gadget oneshot
#   * /usr/local/bin/teslausb-gadget-up — configfs g1 builder script
#   * /usr/local/bin/teslausb-gadget-down — configfs g1 teardown script
#   * /usr/local/bin/teslausb-present-usb — UDC bind (Tesla "sees" the disk)
#   * /usr/local/bin/teslausb-hide-usb — UDC unbind (Tesla "ejects" the disk)
#   * /usr/local/bin/teslausb-watch — live inotify view of TeslaCam/media writes
#
# === Safety rails ===
#
#   * Refuses to install if /etc/systemd/system/teslafat@.service is
#     absent (Phase 6.4 predecessor). Exits 4.
#   * NEVER tries to bring the gadget UP itself — that is Phase 6.10's
#     job (which enables nbd-attach@0.service + usb-gadget.service).
#   * Every file install is idempotent: sha256(src) vs sha256(dst);
#     no-op on match; one-shot `.b1-backup-<ISO>` sidecar on first
#     divergence; mode/owner re-applied if drifted.
#
# === Idempotency ===
#
# Re-running `setup.sh --only 11` on a fully-installed device is a
# no-op end-to-end.
#
# === Dry-run ===
#
# Every mutation routes through `b1_run`. File installs print sha256
# diff, would-be mode/owner, and the destination path without writing.

# shellcheck source=00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/00-common.sh"

# ---- Public constants (consumed by uninstall-lib/11-gadget.sh) -----

B1_GADGET_NAME="g1"
B1_GADGET_VENDOR_ID="0x1d6b"    # Linux Foundation
B1_GADGET_PRODUCT_ID="0x0104"   # Multifunction Composite Gadget
B1_GADGET_SERIAL="teslausb-b1"
B1_GADGET_MANUFACTURER="TeslaUSB"
B1_GADGET_PRODUCT="TeslaUSB B-1"

B1_NBD_MODPROBE_CONF="/etc/modprobe.d/teslausb-nbd.conf"
B1_NBD_MODULES_LOAD="/etc/modules-load.d/teslausb.conf"
B1_TESLAFAT_CONF_DIR="/etc/teslausb"
B1_TESLAFAT_CONF_0="${B1_TESLAFAT_CONF_DIR}/teslafat-0.toml"
# Unified storage + cleanup config (AC.1). Web UI + Rust worker read
# this file; teslafat-0.toml is kept in sync from it on resize.
B1_TESLAUSB_CONF="${B1_TESLAFAT_CONF_DIR}/teslausb.toml"

B1_NBD_ATTACH_UNIT="/etc/systemd/system/nbd-attach@.service"
B1_USB_GADGET_UNIT="/etc/systemd/system/usb-gadget.service"

B1_GADGET_UP_BIN="/usr/local/bin/teslausb-gadget-up"
B1_GADGET_DOWN_BIN="/usr/local/bin/teslausb-gadget-down"
B1_PRESENT_USB_BIN="/usr/local/bin/teslausb-present-usb"
B1_HIDE_USB_BIN="/usr/local/bin/teslausb-hide-usb"
B1_WATCH_BIN="/usr/local/bin/teslausb-watch"
# AC.3 — operator-driven partition resize helper + narrow sudoers.
B1_RESIZE_LUN_BIN="/usr/local/bin/teslausb-resize-lun"
B1_RESIZE_SUDOERS="/etc/sudoers.d/teslausb-resize"

B1_GADGET_TARGETS=(
  "${B1_NBD_MODPROBE_CONF}"
  "${B1_NBD_MODULES_LOAD}"
  "${B1_TESLAFAT_CONF_0}"
  "${B1_TESLAUSB_CONF}"
  "${B1_NBD_ATTACH_UNIT}"
  "${B1_USB_GADGET_UNIT}"
  "${B1_GADGET_UP_BIN}"
  "${B1_GADGET_DOWN_BIN}"
  "${B1_PRESENT_USB_BIN}"
  "${B1_HIDE_USB_BIN}"
  "${B1_WATCH_BIN}"
  "${B1_RESIZE_LUN_BIN}"
  "${B1_RESIZE_SUDOERS}"
)

export B1_GADGET_NAME B1_GADGET_VENDOR_ID B1_GADGET_PRODUCT_ID \
  B1_GADGET_SERIAL B1_GADGET_MANUFACTURER B1_GADGET_PRODUCT \
  B1_NBD_MODPROBE_CONF B1_NBD_MODULES_LOAD B1_TESLAFAT_CONF_DIR \
  B1_TESLAFAT_CONF_0 B1_TESLAUSB_CONF B1_NBD_ATTACH_UNIT \
  B1_USB_GADGET_UNIT B1_GADGET_UP_BIN B1_GADGET_DOWN_BIN \
  B1_PRESENT_USB_BIN B1_HIDE_USB_BIN B1_WATCH_BIN \
  B1_RESIZE_LUN_BIN B1_RESIZE_SUDOERS B1_GADGET_TARGETS

# ---- Inline file body constants -----------------------------------

B1_NBD_MODPROBE_BODY='# Managed by teslausb-b1 setup.sh (Phase 6.11). Do not edit.
# nbds_max=1 because only /dev/nbd0 backs the single USB LUN.
# max_part=0 is intentional: the Pi must not partition-scan the
# synthesized MBR disk; it only serves the whole block device while
# Tesla reads the partition table over USB.
options nbd nbds_max=1 max_part=0
'

B1_NBD_MODULES_BODY='# Managed by teslausb-b1 setup.sh (Phase 6.11). Do not edit.
nbd
'

B1_TESLAFAT_CONF_0_TEMPLATE='# Managed by teslausb-b1 setup.sh (Phase 6.11). Do not edit.
# exFAT is required for both partitions, and both partitions live on
# one USB device so Tesla reads LockChime.wav, LightShow, and Boombox
# from partition 2 instead of ignoring a separate media device.
disk_signature = 0x54455355

[nbd]
socket_path = "/run/teslausb/teslafat-0.sock"
handshake_timeout_seconds = 30

[[partition]]
backing_root = "/srv/teslausb/teslacam"
volume_size_gb = __TESLACAM_SIZE_GB__
volume_label = "TESLACAM"
fs_type = "exfat"
# The dashcam volume is written continuously by the car. Never
# live-swap its synth layout on SIGHUP (chime activation): a swap of
# an actively-recorded volume is out of scope, and excluding it keeps
# the media partition the only one whose reload logs the
# teslafat-reload-live marker the rebind script waits on.
reload_on_sighup = false
# Disk-backed pending-data spill (ADR-0021). Holds pre-dir-entry
# cluster writes safely on disk so we never lose Tesla video bytes
# under burst load.
spill_dir = "/var/lib/teslafat/spill/0"
[partition.retention]
# Hide RecentClips entries older than 1 hour from Tesla. Matches
# Tesla'"'"'s own RecentClips rotation window; the worker still keeps
# the underlying files until the cleanup policy reaps them.
recentclips_hide_after_seconds = 3600

[[partition]]
backing_root = "/srv/teslausb/media"
volume_size_gb = __MEDIA_SIZE_GB__
volume_label = "MEDIA"
fs_type = "exfat"
# Read-mostly media volume holding LockChime.wav, LightShow/, and
# Boombox/. Live-reload on SIGHUP so a chime/lightshow change is
# re-walked and swapped in before the gadget re-enumerates (the
# rebind script gates on this partition'"'"'s reload marker).
reload_on_sighup = true
# Disk-backed pending-data spill (ADR-0021); partition-scoped so
# media writes never share pending data with dashcam writes.
spill_dir = "/var/lib/teslafat/spill/1"
[partition.retention]
# Media files are user-managed; never hide based on age.
recentclips_hide_after_seconds = 0
'

# Unified storage + cleanup config (AC.1). Source of truth for partition
# sizes and auto-cleanup knobs. `safety_buffer_gb` is documented so
# operators can edit by hand; minimum 5 GB (see docs/06-OPERATIONS.md).
# `target_free_pct = 0` means "auto-tune from indexer median clip
# size"; the Rust worker computes 2x the bytes-per-recording-minute
# at runtime.
B1_TESLAUSB_CONF_TEMPLATE='# Managed by teslausb-b1 setup.sh (Phase 6.11) and the web UI.
# Documented in docs/06-OPERATIONS.md "Editing teslausb.toml".

[storage]
# Cushion held back on top of the MEASURED OS/non-partition SD usage so a
# resize can never physically overfill the card. Web UI enforces the same
# minimum (5 GB). Default 5 GB.
safety_buffer_gb = __SAFETY_BUFFER_GB__
teslacam_gb = __TESLACAM_GB__
media_gb = __MEDIA_GB__

[cleanup]
# 0 = auto-tune to 2x the median 6-camera-1-minute recording size.
target_free_pct = 0
# 0 = unlimited (sentry only auto-deleted as last resort).
sentry_max_age_days = 0
# RecentClips with GPS/SEI data are preserved over plain clips.
preserve_with_gps = true
'

B1_NBD_ATTACH_UNIT_BODY='# Managed by teslausb-b1 setup.sh (Phase 6.11). Do not edit.
# nbd-attach@N.service — attaches /dev/nbdN to teslafat@N'"'"'s
# AF_UNIX socket. Phase 6.10 enables only instance 0 for the single disk.
[Unit]
Description=Attach /dev/nbd%i to teslafat@%i (Phase 6.11)
Documentation=https://github.com/mphacker/TeslaUSB
Requires=teslafat@%i.service
After=teslafat@%i.service
PartOf=teslafat@%i.service
# Cap automatic restart loops (paired with `Restart=on-failure`
# below). Belongs in [Unit] per systemd.unit(5).
StartLimitBurst=10
StartLimitIntervalSec=120

[Service]
Type=oneshot
RemainAfterExit=yes
# teslafat@%i.service is `Type=notify` (Phase H/boot-race fix), so
# systemd already holds back this unit until teslafat has bound its
# AF_UNIX socket and finished opening the SynthBackend. The poll
# below is kept as a belt-and-suspenders safety net in case a
# future change reverts teslafat to `Type=simple`, or systemd
# evaluates the Requires= ordering on a kernel/systemd combo that
# delivers the READY=1 datagram with surprising latency.
ExecStartPre=/bin/sh -c '"'"'for i in $(seq 1 30); do [ -S /run/teslausb/teslafat-%i.sock ] && exit 0; sleep 0.5; done; echo "teslafat-%i.sock did not appear within 15s" >&2; exit 1'"'"'
# nbd-client uses a unix socket: -unix <path> instead of -h/-p.
# -persist keeps the kernel side alive across brief daemon restarts.
# -block-size 512 MUST match the synthesized disk sector size. If
# these disagree, hosts reject the block device before Tesla can
# enumerate it as a usable USB drive; Phase 6 hardware bring-up
# proved the NBD and filesystem sector sizes must agree.
ExecStart=/usr/sbin/nbd-client -unix /run/teslausb/teslafat-%i.sock /dev/nbd%i -persist -block-size 512 -nofork
ExecStop=/usr/sbin/nbd-client -d /dev/nbd%i

# Self-heal a transient attach failure (e.g. teslafat restarted out
# from under nbd-client during a config update). systemd will retry
# up to StartLimitBurst times within StartLimitIntervalSec; if we
# blow past that we genuinely need an operator. RestartSec is short
# (2s) because the failure mode is usually "teslafat needs another
# moment to be ready" rather than a permanent error.
Restart=on-failure
RestartSec=2s

[Install]
WantedBy=multi-user.target
'

B1_USB_GADGET_UNIT_BODY='# Managed by teslausb-b1 setup.sh (Phase 6.11). Do not edit.
# usb-gadget.service — composes the configfs g1 gadget with one
# mass_storage LUN backed by /dev/nbd0: an MBR-partitioned disk
# with TeslaCam and media exFAT partitions.
[Unit]
Description=TeslaUSB B-1 USB gadget (configfs, one mass_storage LUN; MBR disk, two exFAT partitions)
Documentation=https://github.com/mphacker/TeslaUSB
Requires=nbd-attach@0.service
After=nbd-attach@0.service sys-kernel-config.mount
# Cap automatic restart loops (paired with `Restart=on-failure`
# below). Belongs in [Unit] per systemd.unit(5).
StartLimitBurst=10
StartLimitIntervalSec=180

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/teslausb-gadget-up
ExecStop=/usr/local/bin/teslausb-gadget-down

# Defence-in-depth: if the gadget composer trips for a transient
# reason (e.g. dwc2 UDC not enumerated yet on a cold boot) we want
# systemd to retry rather than leaving the Tesla recording-blind
# until the operator intervenes. The teslausb-gadget-up script is
# idempotent (it no-ops when g1 is already bound), so retries are
# safe. StartLimit caps a true loop.
Restart=on-failure
RestartSec=3s

[Install]
WantedBy=multi-user.target
'

B1_GADGET_UP_BODY='#!/bin/sh
# teslausb-gadget-up — compose configfs g1 + bind UDC.
# Idempotent: if g1 already exists with a non-empty UDC, exit 0.
set -eu

GADGET_NAME="g1"
GADGET_DIR="/sys/kernel/config/usb_gadget/${GADGET_NAME}"
LUN0="/dev/nbd0"

VENDOR_ID="0x1d6b"
PRODUCT_ID="0x0104"
SERIAL="teslausb-b1"
MANUFACTURER="TeslaUSB"
PRODUCT="TeslaUSB B-1"

require_block() {
  if [ ! -b "$1" ]; then
    echo "teslausb-gadget-up: backing device $1 not present" >&2
    exit 2
  fi
}

ensure_modules() {
  modprobe libcomposite || true
  modprobe usb_f_mass_storage || true
}

mount_configfs() {
  if [ ! -d /sys/kernel/config/usb_gadget ]; then
    if ! mountpoint -q /sys/kernel/config; then
      mount -t configfs none /sys/kernel/config
    fi
  fi
}

teardown_existing_g1() {
  # If g1 already exists, idempotency: if bound + LUN matches, exit 0.
  # Otherwise tear it down before rebuilding.
  if [ -d "${GADGET_DIR}" ]; then
    current_udc=$(cat "${GADGET_DIR}/UDC" 2>/dev/null || true)
    current_lun0=$(cat "${GADGET_DIR}/functions/mass_storage.usb0/lun.0/file" 2>/dev/null || true)
    if [ -n "${current_udc}" ] && [ "${current_lun0}" = "${LUN0}" ]; then
      echo "teslausb-gadget-up: g1 already up on ${current_udc} with correct LUN — idempotent no-op"
      exit 0
    fi
    # Tear down for rebuild.
    /usr/local/bin/teslausb-gadget-down || true
  fi
}

build_g1() {
  mkdir -p "${GADGET_DIR}"
  echo "${VENDOR_ID}"  > "${GADGET_DIR}/idVendor"
  echo "${PRODUCT_ID}" > "${GADGET_DIR}/idProduct"
  echo "0x0100"        > "${GADGET_DIR}/bcdDevice"
  echo "0x0200"        > "${GADGET_DIR}/bcdUSB"

  mkdir -p "${GADGET_DIR}/strings/0x409"
  echo "${SERIAL}"       > "${GADGET_DIR}/strings/0x409/serialnumber"
  echo "${MANUFACTURER}" > "${GADGET_DIR}/strings/0x409/manufacturer"
  echo "${PRODUCT}"      > "${GADGET_DIR}/strings/0x409/product"

  # One mass_storage LUN; the backing disk contains the MBR partitions.
  mkdir -p "${GADGET_DIR}/functions/mass_storage.usb0"
  echo 1 > "${GADGET_DIR}/functions/mass_storage.usb0/stall"

  # LUN 0 — partitioned TeslaUSB disk
  echo 1 > "${GADGET_DIR}/functions/mass_storage.usb0/lun.0/removable"
  echo 0 > "${GADGET_DIR}/functions/mass_storage.usb0/lun.0/cdrom"
  echo 0 > "${GADGET_DIR}/functions/mass_storage.usb0/lun.0/ro"
  echo "${LUN0}" > "${GADGET_DIR}/functions/mass_storage.usb0/lun.0/file"

  # Configuration c.1
  mkdir -p "${GADGET_DIR}/configs/c.1/strings/0x409"
  echo "TeslaUSB Config 1" > "${GADGET_DIR}/configs/c.1/strings/0x409/configuration"
  echo 250 > "${GADGET_DIR}/configs/c.1/MaxPower"
  ln -sf "${GADGET_DIR}/functions/mass_storage.usb0" "${GADGET_DIR}/configs/c.1/"
}

bind_udc() {
  # Bind to the first UDC the kernel exposes (dwc2 on Pi Zero 2 W).
  UDC=$(ls /sys/class/udc 2>/dev/null | head -n1)
  if [ -z "${UDC}" ]; then
    echo "teslausb-gadget-up: no UDC found in /sys/class/udc — is dwc2 loaded?" >&2
    exit 3
  fi
  echo "${UDC}" > "${GADGET_DIR}/UDC"
  echo "teslausb-gadget-up: gadget bound on UDC ${UDC}"
}

ensure_modules
mount_configfs
require_block "${LUN0}"
teardown_existing_g1
build_g1
bind_udc
'

B1_GADGET_DOWN_BODY='#!/bin/sh
# teslausb-gadget-down — unbind UDC + tear down configfs g1.
# Idempotent: missing g1 is OK.
set -eu

GADGET_DIR="/sys/kernel/config/usb_gadget/g1"

if [ ! -d "${GADGET_DIR}" ]; then
  echo "teslausb-gadget-down: ${GADGET_DIR} absent — nothing to do"
  exit 0
fi

# Unbind UDC first (safe even if already empty).
if [ -e "${GADGET_DIR}/UDC" ]; then
  echo "" > "${GADGET_DIR}/UDC" || true
fi

# Remove function-from-config symlinks.
for link in "${GADGET_DIR}/configs/c.1/"mass_storage.*; do
  [ -L "${link}" ] && rm -f "${link}"
done

# Strings dirs.
rmdir "${GADGET_DIR}/configs/c.1/strings/0x409" 2>/dev/null || true
rmdir "${GADGET_DIR}/configs/c.1" 2>/dev/null || true

# Functions.
rmdir "${GADGET_DIR}/functions/mass_storage.usb0" 2>/dev/null || true

# Top-level strings + gadget dir.
rmdir "${GADGET_DIR}/strings/0x409" 2>/dev/null || true
rmdir "${GADGET_DIR}" 2>/dev/null || true

echo "teslausb-gadget-down: g1 torn down"
'

B1_PRESENT_USB_BODY='#!/bin/sh
# teslausb-present-usb — bind g1 to first available UDC (Tesla sees drives).
# Idempotent: if already bound, no-op.
set -eu

GADGET_DIR="/sys/kernel/config/usb_gadget/g1"

if [ ! -d "${GADGET_DIR}" ]; then
  echo "teslausb-present-usb: g1 not composed yet — run teslausb-gadget-up first" >&2
  exit 2
fi

current=$(cat "${GADGET_DIR}/UDC" 2>/dev/null || true)
if [ -n "${current}" ]; then
  echo "teslausb-present-usb: already bound to ${current}"
  exit 0
fi

UDC=$(ls /sys/class/udc 2>/dev/null | head -n1)
if [ -z "${UDC}" ]; then
  echo "teslausb-present-usb: no UDC available — is dwc2 loaded?" >&2
  exit 3
fi
echo "${UDC}" > "${GADGET_DIR}/UDC"
echo "teslausb-present-usb: bound to ${UDC}"
'

B1_HIDE_USB_BODY='#!/bin/sh
# teslausb-hide-usb — unbind g1 from UDC (Tesla "ejects" drives).
# Idempotent: missing g1 or empty UDC is OK.
set -eu

GADGET_DIR="/sys/kernel/config/usb_gadget/g1"

if [ ! -d "${GADGET_DIR}" ]; then
  echo "teslausb-hide-usb: g1 absent — nothing to unbind"
  exit 0
fi

current=$(cat "${GADGET_DIR}/UDC" 2>/dev/null || true)
if [ -z "${current}" ]; then
  echo "teslausb-hide-usb: already unbound"
  exit 0
fi
echo "" > "${GADGET_DIR}/UDC"
echo "teslausb-hide-usb: unbound from ${current}"
'

B1_WATCH_BODY='#!/bin/sh
# teslausb-watch — live view of Tesla writes to the TeslaCam backing
# store. Streams inotify events (create/modify/close_write/move/delete)
# with timestamps, and prints periodic size + file-count snapshots so
# you can confirm Tesla is actually writing the SD card.
#
# Usage: teslausb-watch [path]   (default: /srv/teslausb/teslacam)
# Optional: teslausb-watch media (alias for /srv/teslausb/media)
set -eu

case "${1:-teslacam}" in
  teslacam) ROOT=/srv/teslausb/teslacam ;;
  media)    ROOT=/srv/teslausb/media ;;
  /*)       ROOT="$1" ;;
  *)        echo "usage: teslausb-watch [teslacam|media|/abs/path]" >&2; exit 2 ;;
esac

if [ ! -d "$ROOT" ]; then
  echo "teslausb-watch: $ROOT does not exist" >&2
  exit 3
fi

if ! command -v inotifywait >/dev/null 2>&1; then
  echo "teslausb-watch: install inotify-tools (apt install inotify-tools)" >&2
  exit 4
fi

snapshot() {
  files=$(find "$ROOT" -type f 2>/dev/null | wc -l)
  size=$(du -sh "$ROOT" 2>/dev/null | awk "{print \$1}")
  recent=$(find "$ROOT" -type f -mmin -1 2>/dev/null | wc -l)
  printf "%s  SNAPSHOT  files=%s  size=%s  modified-in-last-60s=%s\n" \
    "$(date +%H:%M:%S)" "$files" "$size" "$recent"
}

echo "teslausb-watch: streaming $ROOT (Ctrl-C to stop)"
snapshot

# Run snapshot every 30s in background; foreground = inotify stream.
( while true; do sleep 30; snapshot; done ) &
SNAP_PID=$!
trap "kill $SNAP_PID 2>/dev/null; exit 0" INT TERM EXIT

exec inotifywait -m -r --quiet \
  --format "%T  %e  %w%f" \
  --timefmt "%H:%M:%S" \
  -e create -e close_write -e moved_to -e delete -e moved_from \
  "$ROOT"
'

# AC.3 — teslausb-resize-lun: operator-driven partition size change.
#
# Usage: teslausb-resize-lun --lun {teslacam|media} --size-gb N
#
# Reads /etc/teslausb/teslausb.toml for the safety buffer, MEASURES the
# OS/non-partition SD usage at apply time, and enforces the no-overcommit
# cap so the card can never physically overfill:
#
#   cap_teslacam + cap_media + measured_os_usage + safety_buffer <= sd_total
#
# (all computed in bytes; measured_os_usage = sd_total - sd_avail -
# du(teslacam) - du(media)). It refuses to shrink a partition below its
# currently-used bytes, atomically rewrites the target partition in the
# single DiskConfig AND the unified teslausb.toml, then bounces usb-gadget +
# teslafat@0 so Tesla re-enumerates with the new advertised partition size.
#
# Exit codes:
#   0 success
#   2 usage error (missing/invalid args)
#   3 validation failure (cap exceeded, shrink-with-used,
#     unknown LUN)
#   4 I/O error (config write / sed / systemctl)
B1_RESIZE_LUN_BODY='#!/bin/bash
# Managed by teslausb-b1 setup.sh. Do not edit by hand.
set -euo pipefail

CONF_DIR="/etc/teslausb"
TESLAUSB_TOML="${CONF_DIR}/teslausb.toml"
DISK_TOML="${CONF_DIR}/teslafat-0.toml"

usage() {
  echo "usage: teslausb-resize-lun --lun {teslacam|media} --size-gb N" >&2
  exit 2
}

LUN=""
SIZE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --lun)     LUN="${2:-}";  shift 2 ;;
    --size-gb) SIZE="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $1" >&2; usage ;;
  esac
done

[[ -n "${LUN}" && -n "${SIZE}" ]] || usage
[[ "${SIZE}" =~ ^[0-9]+$ ]]       || { echo "size-gb must be integer" >&2; exit 2; }

if (( SIZE < 4 || SIZE > 2048 )); then
  echo "size-gb must be in [4, 2048]" >&2
  exit 3
fi

case "${LUN}" in
  teslacam) PARTITION_IDX=0; LUN_BACKING="/srv/teslausb/teslacam"; KEY="teslacam_gb" ;;
  media)    PARTITION_IDX=1; LUN_BACKING="/srv/teslausb/media";    KEY="media_gb"    ;;
  *)        echo "unknown LUN: ${LUN}" >&2; exit 3 ;;
esac

# Pure-bash TOML int reader.
read_toml_int() {
  local key="$1" file="$2" default="$3"
  if [[ ! -e "${file}" ]]; then echo "${default}"; return; fi
  local line val=""
  while IFS= read -r line; do
    if [[ "${line}" =~ ^[[:space:]]*${key}[[:space:]]*=[[:space:]]*([0-9]+) ]]; then
      val="${BASH_REMATCH[1]}"; break
    fi
  done < "${file}"
  if [[ -z "${val}" ]]; then echo "${default}"; else echo "${val}"; fi
}

read_partition_volume_size_gb() {
  local target="$1" file="$2" default="$3"
  if [[ ! -e "${file}" ]]; then echo "${default}"; return; fi
  local line partition=-1
  while IFS= read -r line; do
    if [[ "${line}" =~ ^[[:space:]]*\[\[partition\]\] ]]; then
      partition=$(( partition + 1 ))
      continue
    fi
    if (( partition == target )) && [[ "${line}" =~ ^[[:space:]]*volume_size_gb[[:space:]]*=[[:space:]]*([0-9]+) ]]; then
      echo "${BASH_REMATCH[1]}"
      return
    fi
  done < "${file}"
  echo "${default}"
}

rewrite_partition_volume_size_gb() {
  local target="$1" size="$2" src="$3" dst="$4"
  local line partition=-1 changed=0
  : > "${dst}" || return 1
  while IFS= read -r line; do
    if [[ "${line}" =~ ^[[:space:]]*\[\[partition\]\] ]]; then
      partition=$(( partition + 1 ))
    fi
    if (( partition == target )) && [[ "${line}" =~ ^[[:space:]]*volume_size_gb[[:space:]]*= ]]; then
      printf "%s\n" "volume_size_gb = ${size}" >> "${dst}" || return 1
      changed=1
    else
      printf "%s\n" "${line}" >> "${dst}" || return 1
    fi
  done < "${src}"
  (( changed == 1 ))
}

OS_RESERVE_DEFAULT=5
# Read the safety buffer; accept the legacy os_reserve_gb key on a
# pre-rework teslausb.toml (a larger held-back value is always safe).
SAFETY_BUFFER_GB=$(read_toml_int safety_buffer_gb "${TESLAUSB_TOML}" 0)
if (( SAFETY_BUFFER_GB == 0 )); then
  SAFETY_BUFFER_GB=$(read_toml_int os_reserve_gb "${TESLAUSB_TOML}" "${OS_RESERVE_DEFAULT}")
fi
CUR_TESLACAM_GB=$(read_toml_int teslacam_gb "${TESLAUSB_TOML}" 64)
CUR_MEDIA_GB=$(read_toml_int    media_gb    "${TESLAUSB_TOML}" 32)

# Defensive: enforce the safety-buffer floor (charter AC.1). Both Python
# (storage_config.py) and Rust (storage_config.rs) already enforce >= 5 GB
# on save, but a hand-edited TOML could slip a smaller value through;
# refuse so the resize never erases the OS-crash cushion.
if (( SAFETY_BUFFER_GB < 5 )); then
  echo "refusing: safety_buffer_gb=${SAFETY_BUFFER_GB} GB is below the 5 GB floor" >&2
  exit 3
fi

# Defensive: re-validate KEY before it is interpolated into sed
# below. The case statement above already constrains LUN, but
# any future change that adds a new LUN must also extend KEY to
# match this allowlist or the resize aborts safely.
[[ "${KEY}" =~ ^(teslacam_gb|media_gb)$ ]] || {
  echo "internal error: invalid KEY ${KEY}" >&2
  exit 4
}

# --- No-overcommit cap (byte-precise; measured OS usage) ---------------
# Sample the SD filesystem and the two partition backing trees NOW (at
# apply time) so the cap reflects live usage with no stale TOCTOU window.
GIB=$(( 1024 * 1024 * 1024 ))
read_df_bytes() { df -B1 --output="$1" /srv/teslausb 2>/dev/null | tail -n1 | tr -d " "; }
SD_TOTAL_B=$(read_df_bytes size)
SD_AVAIL_B=$(read_df_bytes avail)

# Fail CLOSED: if the SD geometry cannot be read we must NOT resize. The
# no-overcommit cap below is the last guard against physically overfilling
# the card (which would crash the rootfs and stop TeslaCam writes), so an
# unreadable df must abort, never silently skip the check.
if ! [[ "${SD_TOTAL_B}" =~ ^[0-9]+$ ]] || (( SD_TOTAL_B <= 0 )); then
  echo "refusing: could not read SD total size from df (got: ${SD_TOTAL_B:-empty})" >&2
  exit 3
fi
if ! [[ "${SD_AVAIL_B}" =~ ^[0-9]+$ ]]; then
  echo "refusing: could not read SD available size from df (got: ${SD_AVAIL_B:-empty})" >&2
  exit 3
fi

du_bytes() { du -sb "$1" 2>/dev/null | cut -f1; }
USED_TC_B=$(du_bytes /srv/teslausb/teslacam)
USED_MD_B=$(du_bytes /srv/teslausb/media)
# Fail CLOSED on a du failure too: a 0 here would both under-count OS usage
# AND defeat the shrink guard below, so an unreadable backing tree aborts.
if ! [[ "${USED_TC_B}" =~ ^[0-9]+$ ]] || ! [[ "${USED_MD_B}" =~ ^[0-9]+$ ]]; then
  echo "refusing: could not measure backing-tree usage via du" >&2
  exit 3
fi

# measured_os_usage = everything physically on the card that is NOT the
# two partition backing trees (OS, journal, swap, index, tmp). Clamp >= 0.
OS_USED_B=$(( SD_TOTAL_B - SD_AVAIL_B - USED_TC_B - USED_MD_B ))
(( OS_USED_B < 0 )) && OS_USED_B=0

if [[ "${LUN}" == "teslacam" ]]; then
  CAP_TC_B=$(( SIZE * GIB ));         CAP_MD_B=$(( CUR_MEDIA_GB * GIB ))
else
  CAP_TC_B=$(( CUR_TESLACAM_GB * GIB )); CAP_MD_B=$(( SIZE * GIB ))
fi
BUFFER_B=$(( SAFETY_BUFFER_GB * GIB ))
NEED_B=$(( CAP_TC_B + CAP_MD_B + OS_USED_B + BUFFER_B ))

if (( NEED_B > SD_TOTAL_B )); then
  os_used_gb=$(( (OS_USED_B + GIB - 1) / GIB ))
  sd_total_gb=$(( SD_TOTAL_B / GIB ))
  echo "refusing: teslacam(${CAP_TC_B}) + media(${CAP_MD_B}) + os_usage(~${os_used_gb} GB)" \
       "+ buffer(${SAFETY_BUFFER_GB} GB) exceeds SD capacity ~${sd_total_gb} GB" >&2
  exit 3
fi

# Shrink guard: refuse if currently-used data > new size. Reuse the
# bytes already sampled above; ceil to GB so the floor is conservative.
if [[ "${LUN}" == "teslacam" ]]; then
  USED_BYTES=${USED_TC_B}
else
  USED_BYTES=${USED_MD_B}
fi
USED_GB=$(( (USED_BYTES + GIB - 1) / GIB ))
if (( USED_GB > SIZE )); then
  echo "refusing: ${LUN_BACKING} uses ${USED_GB} GB which exceeds requested ${SIZE} GB" >&2
  exit 3
fi

OLD_SIZE=$(read_partition_volume_size_gb "${PARTITION_IDX}" "${DISK_TOML}" 0)
echo "resize-lun: ${LUN} ${OLD_SIZE} -> ${SIZE} GB"

# Atomically rewrite the selected partition in the single DiskConfig.
TMP="${DISK_TOML}.tmp.$$"
if ! rewrite_partition_volume_size_gb "${PARTITION_IDX}" "${SIZE}" "${DISK_TOML}" "${TMP}"; then
  rm -f "${TMP}"
  echo "resize-lun: failed to update partition ${PARTITION_IDX} in ${DISK_TOML}" >&2
  exit 4
fi
if [[ "$(read_partition_volume_size_gb "${PARTITION_IDX}" "${TMP}" 0)" != "${SIZE}" ]]; then
  rm -f "${TMP}"
  echo "resize-lun: verification failed for partition ${PARTITION_IDX} in ${DISK_TOML}" >&2
  exit 4
fi
chmod 0644 "${TMP}" || { rm -f "${TMP}"; exit 4; }
mv -f "${TMP}" "${DISK_TOML}" || { rm -f "${TMP}"; exit 4; }

# Atomically rewrite the [storage] section of teslausb.toml.
if [[ -e "${TESLAUSB_TOML}" ]]; then
  TMP2="${TESLAUSB_TOML}.tmp.$$"
  sed -E "s/^([[:space:]]*${KEY}[[:space:]]*=[[:space:]]*).*/\1${SIZE}/"     "${TESLAUSB_TOML}" > "${TMP2}"
  chmod 0644 "${TMP2}"
  mv -f "${TMP2}" "${TESLAUSB_TOML}"
fi

# Bounce: unbind UDC (Tesla sees eject), restart teslafat, re-bind. A trap
# guarantees the gadget is re-presented even if a step below fails or the
# helper is interrupted (SIGINT/SIGTERM) mid-bounce, so we never leave the
# Tesla without a USB drive. Presenting is idempotent.
present_usb() { /usr/local/bin/teslausb-present-usb >/dev/null 2>&1; }
trap present_usb EXIT INT TERM
/usr/local/bin/teslausb-hide-usb >/dev/null 2>&1 || true
systemctl restart teslafat@0 >/dev/null 2>&1 || true
sleep 2
if ! present_usb; then
  sleep 2
  if ! present_usb; then
    echo "resize-lun: ERROR could not re-present USB to Tesla after resize" >&2
    exit 5
  fi
fi
trap - EXIT INT TERM

echo "resize-lun: complete; ${LUN} now ${SIZE} GB"
exit 0
'

# AC.3 — sudoers fragment so the Flask user (gadget_web) can call
# the resize helper without a password. The fragment is intentionally
# narrow: only the resize-lun binary, no shell escapes, no env.
B1_RESIZE_SUDOERS_BODY='# Managed by teslausb-b1 setup.sh (AC.3). Do not edit.
# Allow the Flask web user to drive LUN size changes without
# unlocking sudo more broadly.
gadget_web ALL=(root) NOPASSWD: /usr/local/bin/teslausb-resize-lun
'

# _b1_data_root_free_gb  — best-effort: total bytes of the filesystem
# hosting /srv/teslausb, expressed in whole GB. Falls back to 0 if the
# path doesn't exist yet (caller handles).
_b1_data_root_total_gb() {
  local root="${B1_DATA_ROOT:-/srv/teslausb}"
  if [[ ! -d "${root}" ]]; then
    echo 0
    return 0
  fi
  # df -B1G --output=size <path>  →  prints "1G-blocks\n<n>"
  local total
  total=$(df -B1G --output=size "${root}" 2>/dev/null | tail -n1 | tr -d ' ')
  if [[ -z "${total}" || ! "${total}" =~ ^[0-9]+$ ]]; then
    echo 0
    return 0
  fi
  echo "${total}"
}

# _b1_recommend_sizes  — emits "TESLACAM_GB MEDIA_GB" on stdout based
# on filesystem total. Strategy (per operator directive 2026-05-21):
#   * Defaults are 256 / 32 (TeslaCam / Media).
#   * Reserve ~16 GB for OS + swap + worker scratch + headroom.
#   * If avail >= 288 (256+32), recommend the defaults verbatim.
#   * If avail < 288, scale both DOWN proportionally:
#       Media  = max(8,  round(avail * 32 / 288))
#       TeslaCam = max(32, avail - Media)
#   * If total is 0 (data root absent), return the static defaults.
_b1_recommend_sizes() {
  local total=$1
  local default_teslacam=256
  local default_media=32
  if [[ "${total}" -eq 0 ]]; then
    echo "${default_teslacam} ${default_media}"
    return 0
  fi
  local reserve=16
  local avail=$(( total - reserve ))
  if [[ "${avail}" -ge $(( default_teslacam + default_media )) ]]; then
    echo "${default_teslacam} ${default_media}"
    return 0
  fi
  # Scale down. Ratio 256:32 = 8:1.
  local media=$(( avail / 9 ))
  if [[ "${media}" -lt 8 ]]; then media=8; fi
  local teslacam=$(( avail - media ))
  if [[ "${teslacam}" -lt 32 ]]; then teslacam=32; fi
  echo "${teslacam} ${media}"
}

# _b1_existing_volume_size_gb <conf_file> [key]  — extract the
# integer value for key from an existing TOML config, or empty.
# Defaults to volume_size_gb for legacy callers; setup uses the
# teslausb.toml storage keys to preserve operator choices on re-run.
_b1_existing_volume_size_gb() {
  local conf="$1" key="${2:-volume_size_gb}"
  if [[ ! "${key}" =~ ^[A-Za-z0-9_]+$ || ! -e "${conf}" ]]; then
    return 0
  fi
  local line
  while IFS= read -r line; do
    if [[ "${line}" =~ ^[[:space:]]*${key}[[:space:]]*=[[:space:]]*([0-9]+) ]]; then
      echo "${BASH_REMATCH[1]}"
      return 0
    fi
  done < "${conf}"
}

# _b1_validate_size_gb <n>  — returns 0 if n is an integer in
# teslafat'"'"'s [4, 2048] range, else 1.
_b1_validate_size_gb() {
  local n="$1"
  [[ "${n}" =~ ^[0-9]+$ ]] || return 1
  (( n >= 4 && n <= 2048 )) || return 1
  return 0
}

# _b1_resolve_size_gb <partition_index> <label> <recommended> <env_var_name>
#                     <existing_conf_path> [existing_key]
# Returns chosen size on stdout. Priority:
#   1. Env var (TESLAUSB_LUN0_SIZE_GB / TESLAUSB_LUN1_SIZE_GB) if set+valid
#   2. Existing TOML storage value (preserves operator choices on re-run)
#   3. Interactive prompt if stdin is a TTY and not dry-run/non-interactive
#   4. Recommended value
_b1_resolve_size_gb() {
  local idx="$1" label="$2" recommended="$3" env_var="$4" existing_conf="$5"
  local existing_key="${6:-volume_size_gb}"
  local env_val="${!env_var:-}"

  if [[ -n "${env_val}" ]]; then
    if _b1_validate_size_gb "${env_val}"; then
      b1_log "  partition ${idx} (${label}): using ${env_val} GB from ${env_var}" >&2
      echo "${env_val}"
      return 0
    fi
    b1_warn "  partition ${idx} (${label}): ${env_var}=${env_val} invalid (must be int 4..2048); ignoring" >&2
  fi

  local existing
  existing=$(_b1_existing_volume_size_gb "${existing_conf}" "${existing_key}")
  if [[ -n "${existing}" ]] && _b1_validate_size_gb "${existing}"; then
    b1_log "  partition ${idx} (${label}): preserving existing ${existing} GB from ${existing_conf} ${existing_key}" >&2
    echo "${existing}"
    return 0
  fi

  # Interactive prompt only if: stdin is TTY, not dry-run, not non-interactive.
  if [[ -t 0 && "${TESLAUSB_DRY_RUN:-0}" != "1" && "${TESLAUSB_NON_INTERACTIVE:-0}" != "1" ]]; then
    local input
    while true; do
      printf '  partition %s (%s) size in GB [recommended %s, range 4..2048]: ' \
        "${idx}" "${label}" "${recommended}" >&2
      if ! IFS= read -r input; then
        # EOF / Ctrl-D — fall through to recommended.
        echo >&2
        break
      fi
      input="${input// /}"
      if [[ -z "${input}" ]]; then
        b1_log "  partition ${idx} (${label}): using recommended ${recommended} GB" >&2
        echo "${recommended}"
        return 0
      fi
      if _b1_validate_size_gb "${input}"; then
        b1_log "  partition ${idx} (${label}): using ${input} GB (operator entered)" >&2
        echo "${input}"
        return 0
      fi
      printf '  invalid: must be integer in 4..2048\n' >&2
    done
  fi

  b1_log "  partition ${idx} (${label}): using recommended ${recommended} GB" >&2
  echo "${recommended}"
}

# ---- Helpers -------------------------------------------------------

# _b1_install_file <dst> <mode> <body-string>
# Idempotent file install: sha256(body) vs sha256(dst); no-op on
# match; b1_backup on first divergence; mode/owner re-applied if
# drifted. Uses repo-local stage dir (NEVER /tmp).
_b1_install_file() {
  local dst="$1" mode="$2" body="$3"
  local stage="${SCRIPT_DIR:-$(dirname "$(dirname "${BASH_SOURCE[0]}")")}/setup-lib/.b1-stage-11"
  mkdir -p "${stage}"
  local stage_file
  stage_file="${stage}/$(basename "${dst}")"
  printf '%s' "${body}" > "${stage_file}"
  chmod "${mode}" "${stage_file}"

  local stage_sha dst_sha
  stage_sha=$(sha256sum "${stage_file}" | awk '{print $1}')

  if [[ -e "${dst}" ]]; then
    dst_sha=$(sha256sum "${dst}" | awk '{print $1}')
    if [[ "${stage_sha}" == "${dst_sha}" ]]; then
      local cur_mode
      cur_mode=$(stat -c '%a' "${dst}" 2>/dev/null || echo "")
      if [[ "${cur_mode}" == "${mode#0}" || "${cur_mode}" == "${mode}" ]]; then
        b1_log "  unchanged: ${dst}"
        return 0
      fi
      b1_log "  fixing mode: ${dst} ${cur_mode} → ${mode}"
      b1_run chmod "${mode}" "${dst}"
      return 0
    fi
    b1_log "  changed: ${dst} (sha256 ${dst_sha:0:12}… → ${stage_sha:0:12}…)"
    b1_backup "${dst}"
  else
    b1_log "  new: ${dst} (sha256=${stage_sha:0:12}…, mode=${mode})"
  fi

  b1_run install -o root -g root -m "${mode}" -- "${stage_file}" "${dst}"
}

# ---- Step ----------------------------------------------------------

b1_step_11() {
  # Precondition: 6.4 installed the teslafat template unit.
  # Under dry-run we only warn (since 6.4's mutations didn't actually
  # happen yet in a chained --dry-run --only 04,11 invocation).
  if [[ ! -e /etc/systemd/system/teslafat@.service ]]; then
    if [[ "${TESLAUSB_DRY_RUN:-0}" == "1" ]]; then
      b1_warn "DRY-RUN: precondition /etc/systemd/system/teslafat@.service absent (will be installed by Phase 6.4 in a real run)"
    else
      b1_err "precondition missing: /etc/systemd/system/teslafat@.service"
      b1_err "run setup.sh --only 04 (Phase 6.4) before 11."
      return 4
    fi
  fi

  # Ensure config dir exists (6.4 also creates it but tolerate --only 11).
  if [[ ! -d "${B1_TESLAFAT_CONF_DIR}" ]]; then
    b1_log "creating ${B1_TESLAFAT_CONF_DIR}"
    b1_run mkdir -p "${B1_TESLAFAT_CONF_DIR}"
    b1_run chmod 0755 "${B1_TESLAFAT_CONF_DIR}"
  fi

  # 1. nbd kernel module options.
  b1_log "installing nbd module config"
  _b1_install_file "${B1_NBD_MODPROBE_CONF}" 0644 "${B1_NBD_MODPROBE_BODY}"
  _b1_install_file "${B1_NBD_MODULES_LOAD}"  0644 "${B1_NBD_MODULES_BODY}"

  # 2. Single DiskConfig (two exFAT partitions with operator-chosen / recommended sizes).
  b1_log "resolving partition sizes"
  local total_gb teslacam_rec media_rec teslacam_gb media_gb
  total_gb=$(_b1_data_root_total_gb)
  if [[ "${total_gb}" -gt 0 ]]; then
    b1_log "  data root ${B1_DATA_ROOT:-/srv/teslausb} total: ${total_gb} GB"
  else
    b1_log "  data root absent — using static defaults"
  fi
  read -r teslacam_rec media_rec <<<"$(_b1_recommend_sizes "${total_gb}")"
  b1_log "  recommended: TeslaCam=${teslacam_rec} GB, Media=${media_rec} GB"

  teslacam_gb=$(_b1_resolve_size_gb 0 TESLACAM "${teslacam_rec}" \
                  TESLAUSB_LUN0_SIZE_GB "${B1_TESLAUSB_CONF}" teslacam_gb)
  media_gb=$(_b1_resolve_size_gb 1 MEDIA "${media_rec}" \
              TESLAUSB_LUN1_SIZE_GB "${B1_TESLAUSB_CONF}" media_gb)

  local conf0_body
  conf0_body="${B1_TESLAFAT_CONF_0_TEMPLATE//__TESLACAM_SIZE_GB__/${teslacam_gb}}"
  conf0_body="${conf0_body//__MEDIA_SIZE_GB__/${media_gb}}"

  b1_log "installing teslafat DiskConfig"
  _b1_install_file "${B1_TESLAFAT_CONF_0}" 0644 "${conf0_body}"

  # 2b. Unified storage+cleanup config. Source of truth for partition
  # sizes going forward; teslafat-0.toml stays in sync via the
  # resize helper (AC.3). Preserves an existing file if present
  # (so the operator's cleanup knobs survive a re-run of setup.sh).
  if [[ -e "${B1_TESLAUSB_CONF}" ]]; then
    b1_log "preserving existing ${B1_TESLAUSB_CONF}"
  else
    local teslausb_body
    teslausb_body="${B1_TESLAUSB_CONF_TEMPLATE//__SAFETY_BUFFER_GB__/5}"
    teslausb_body="${teslausb_body//__TESLACAM_GB__/${teslacam_gb}}"
    teslausb_body="${teslausb_body//__MEDIA_GB__/${media_gb}}"
    b1_log "seeding ${B1_TESLAUSB_CONF}"
    _b1_install_file "${B1_TESLAUSB_CONF}" 0664 "${teslausb_body}"
  fi

  # 2c. Web-writable perms on teslausb.toml + its dir. The Flask
  # service (running as the AC.3 web user, member of the `teslausb`
  # group) writes this file via atomic-rename when the operator
  # mutates LUN sizes or cleanup knobs in /storage. Atomic rename
  # requires write+exec on the directory AND write on the existing
  # target file. We therefore:
  #
  #   * chgrp `teslausb` on /etc/teslausb so the group exists on
  #     both the dir and the file.
  #   * setgid (g+s) on the dir so any *new* file the web user
  #     drops inherits the group (e.g. teslausb.toml.tmp).
  #   * sticky bit (+t) on the dir so the web user can only
  #     unlink/rename files they own — root-owned configs like
  #     worker.toml and teslafat-0.toml stay safe.
  #   * chown the file itself to the web user so the atomic rename
  #     can replace it.
  if getent group teslausb >/dev/null && id -u "${B1_WEB_USER:-pi}" >/dev/null 2>&1; then
    b1_log "applying web-writable perms to ${B1_TESLAUSB_CONF}"
    chgrp teslausb "${B1_TESLAFAT_CONF_DIR}" "${B1_TESLAUSB_CONF}" || true
    chmod 03775 "${B1_TESLAFAT_CONF_DIR}" || true
    chown "${B1_WEB_USER:-pi}:teslausb" "${B1_TESLAUSB_CONF}" || true
    chmod 0664 "${B1_TESLAUSB_CONF}" || true
  else
    b1_log "skipping web-writable perms (group teslausb or B1_WEB_USER missing)"
  fi

  # 3. systemd units.
  b1_log "installing nbd-attach@ + usb-gadget systemd units"
  _b1_install_file "${B1_NBD_ATTACH_UNIT}" 0644 "${B1_NBD_ATTACH_UNIT_BODY}"
  _b1_install_file "${B1_USB_GADGET_UNIT}" 0644 "${B1_USB_GADGET_UNIT_BODY}"

  # 4. Wrapper scripts.
  b1_log "installing gadget control scripts"
  _b1_install_file "${B1_GADGET_UP_BIN}"   0755 "${B1_GADGET_UP_BODY}"
  _b1_install_file "${B1_GADGET_DOWN_BIN}" 0755 "${B1_GADGET_DOWN_BODY}"
  _b1_install_file "${B1_PRESENT_USB_BIN}" 0755 "${B1_PRESENT_USB_BODY}"
  _b1_install_file "${B1_HIDE_USB_BIN}"    0755 "${B1_HIDE_USB_BODY}"
  _b1_install_file "${B1_WATCH_BIN}"       0755 "${B1_WATCH_BODY}"
  # AC.3 — resize helper + narrow sudoers fragment. Sudoers
  # MUST be 0440 owned by root or visudo refuses to load it.
  _b1_install_file "${B1_RESIZE_LUN_BIN}"  0755 "${B1_RESIZE_LUN_BODY}"
  _b1_install_file "${B1_RESIZE_SUDOERS}"  0440 "${B1_RESIZE_SUDOERS_BODY}"

  # 5. Reload systemd if any unit changed.
  if [[ "${TESLAUSB_DRY_RUN:-0}" != "1" ]]; then
    b1_run systemctl daemon-reload
  else
    b1_log "DRY-RUN: systemctl daemon-reload"
  fi

  b1_log "gadget pipeline staged; activation (enable+start) deferred to Phase 6.10 re-run"
  b1_log "after 6.11: re-run 'sudo ./setup.sh --only 10' to activate teslafat + nbd-attach + usb-gadget"
}
