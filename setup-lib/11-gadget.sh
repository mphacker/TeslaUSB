#!/usr/bin/env bash
# setup-lib/11-gadget.sh — Phase 6.11 (USB gadget pipeline)
#
# Wires up the customer-facing USB gadget so Tesla sees two mass-
# storage LUNs backed by the teslafat daemons. This step is what
# makes B-1 actually USEFUL — without it teslafat is just a
# daemon talking to an empty socket and nothing reaches the car.
#
# Pipeline (in startup order):
#
#   teslafat@0.service      teslafat@1.service        (Phase 6.4)
#         │ AF_UNIX sock          │ AF_UNIX sock
#         ▼                       ▼
#   nbd-attach@0.service     nbd-attach@1.service     (this step)
#   nbd-client -unix … sock /dev/nbd0   /dev/nbd1
#         │                       │
#         ▼                       ▼
#   ┌────────────────────────────────────────────────┐
#   │ usb-gadget.service  — configfs g1               │
#   │   functions/mass_storage.usb0/                  │
#   │     lun.0/file = /dev/nbd0                      │
#   │     lun.1/file = /dev/nbd1                      │
#   │   UDC = <dwc2 UDC name from /sys/class/udc/>    │
#   └────────────────────────────────────────────────┘
#         │
#         ▼
#       Tesla
#
# === What this step installs ===
#
#   * /etc/modprobe.d/teslausb-nbd.conf — `options nbd nbds_max=2 max_part=0`
#   * /etc/modules-load.d/teslausb.conf — load `nbd` at boot
#   * /etc/teslausb/teslafat-0.toml — TeslaCam LUN config (exFAT, size chosen at install)
#   * /etc/teslausb/teslafat-1.toml — media LUN config (FAT32, size chosen at install)
#   * /etc/systemd/system/nbd-attach@.service — templated attach unit
#   * /etc/systemd/system/usb-gadget.service — configfs gadget oneshot
#   * /usr/local/bin/teslausb-gadget-up — configfs g1 builder script
#   * /usr/local/bin/teslausb-gadget-down — configfs g1 teardown script
#   * /usr/local/bin/teslausb-present-usb — UDC bind (Tesla "sees" drives)
#   * /usr/local/bin/teslausb-hide-usb — UDC unbind (Tesla "ejects" drives)
#   * /usr/local/bin/teslausb-watch — live inotify view of TeslaCam/media writes
#
# === Safety rails ===
#
#   * Refuses to install if /etc/systemd/system/teslafat@.service is
#     absent (Phase 6.4 predecessor). Exits 4.
#   * NEVER tries to bring the gadget UP itself — that is Phase 6.10's
#     job (which enables nbd-attach@{0,1}.service + usb-gadget.service).
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
B1_TESLAFAT_CONF_1="${B1_TESLAFAT_CONF_DIR}/teslafat-1.toml"

B1_NBD_ATTACH_UNIT="/etc/systemd/system/nbd-attach@.service"
B1_USB_GADGET_UNIT="/etc/systemd/system/usb-gadget.service"

B1_GADGET_UP_BIN="/usr/local/bin/teslausb-gadget-up"
B1_GADGET_DOWN_BIN="/usr/local/bin/teslausb-gadget-down"
B1_PRESENT_USB_BIN="/usr/local/bin/teslausb-present-usb"
B1_HIDE_USB_BIN="/usr/local/bin/teslausb-hide-usb"
B1_WATCH_BIN="/usr/local/bin/teslausb-watch"

B1_GADGET_TARGETS=(
  "${B1_NBD_MODPROBE_CONF}"
  "${B1_NBD_MODULES_LOAD}"
  "${B1_TESLAFAT_CONF_0}"
  "${B1_TESLAFAT_CONF_1}"
  "${B1_NBD_ATTACH_UNIT}"
  "${B1_USB_GADGET_UNIT}"
  "${B1_GADGET_UP_BIN}"
  "${B1_GADGET_DOWN_BIN}"
  "${B1_PRESENT_USB_BIN}"
  "${B1_HIDE_USB_BIN}"
  "${B1_WATCH_BIN}"
)

export B1_GADGET_NAME B1_GADGET_VENDOR_ID B1_GADGET_PRODUCT_ID \
  B1_GADGET_SERIAL B1_GADGET_MANUFACTURER B1_GADGET_PRODUCT \
  B1_NBD_MODPROBE_CONF B1_NBD_MODULES_LOAD B1_TESLAFAT_CONF_DIR \
  B1_TESLAFAT_CONF_0 B1_TESLAFAT_CONF_1 B1_NBD_ATTACH_UNIT \
  B1_USB_GADGET_UNIT B1_GADGET_UP_BIN B1_GADGET_DOWN_BIN \
  B1_PRESENT_USB_BIN B1_HIDE_USB_BIN B1_WATCH_BIN B1_GADGET_TARGETS

# ---- Inline file body constants -----------------------------------

B1_NBD_MODPROBE_BODY='# Managed by teslausb-b1 setup.sh (Phase 6.11). Do not edit.
# nbds_max=2 so /dev/nbd0 and /dev/nbd1 exist; max_part=0 because
# the teslafat-served block device is a single whole-disk FAT32/exFAT
# volume with no partition table — we do NOT want kernel partition
# scanning racing the synth backend.
options nbd nbds_max=2 max_part=0
'

B1_NBD_MODULES_BODY='# Managed by teslausb-b1 setup.sh (Phase 6.11). Do not edit.
nbd
'

B1_TESLAFAT_CONF_0_TEMPLATE='# Managed by teslausb-b1 setup.sh (Phase 6.11). Do not edit.
# LUN 0 — TeslaCam (dashcam + sentry + saved clips).
#
# fs_type=exfat is REQUIRED here. Tesla refuses to write to a
# FAT32 volume larger than ~32 GiB (its mass-storage stack
# treats them as malformed) and the TeslaCam LUN is sized for
# the operator-chosen drive size, which is always well above
# that threshold in practice. See docs/02-LEARNINGS.md
# "Phase 6 — Tesla requires exFAT on the TeslaCam LUN".
backing_root = "/srv/teslausb/teslacam"
volume_size_gb = __SIZE_GB__
volume_label = "TESLACAM"
fs_type = "exfat"

[retention]
# Hide RecentClips entries older than 1 hour from Tesla. Matches
# Tesla'"'"'s own RecentClips rotation window; the worker still keeps
# the underlying files until the cleanup policy reaps them.
recentclips_hide_after_seconds = 3600

[nbd]
socket_path = "/run/teslausb/teslafat-0.sock"
handshake_timeout_seconds = 30
'

B1_TESLAFAT_CONF_1_TEMPLATE='# Managed by teslausb-b1 setup.sh (Phase 6.11). Do not edit.
# LUN 1 — user-managed media (lock chimes, light shows, music, wraps).
backing_root = "/srv/teslausb/media"
volume_size_gb = __SIZE_GB__
volume_label = "MEDIA"
fs_type = "fat32"

[retention]
# Media files are user-managed; never hide based on age.
recentclips_hide_after_seconds = 0

[nbd]
socket_path = "/run/teslausb/teslafat-1.sock"
handshake_timeout_seconds = 30
'

B1_NBD_ATTACH_UNIT_BODY='# Managed by teslausb-b1 setup.sh (Phase 6.11). Do not edit.
# nbd-attach@N.service — attaches /dev/nbdN to teslafat@N'"'"'s
# AF_UNIX socket. Templated: %i = instance number (0 or 1).
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
# -block-size 512 MUST match teslafat'"'"'s synthesized FAT32 BPB
# (BPB_BytsPerSec = 0x0200 / 512). If these disagree the kernel
# refuses to mount the volume with
# `FAT-fs (nbdN): logical sector size too small for device`
# and Tesla refuses to enumerate it as a usable USB drive (this
# was the smoking gun for the "Tesla sees the drive but never
# writes to RecentClips" bug in Phase 6 hardware bring-up — see
# docs/02-LEARNINGS.md "FAT logical sector size must match NBD
# logical block size" for the full diagnosis).
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
# usb-gadget.service — composes the configfs g1 gadget with two
# mass_storage LUNs backed by /dev/nbd{0,1}, then binds it to the
# dwc2 UDC so Tesla sees the drives.
[Unit]
Description=TeslaUSB B-1 USB gadget (configfs, two mass_storage LUNs)
Documentation=https://github.com/mphacker/TeslaUSB
Requires=nbd-attach@0.service nbd-attach@1.service
After=nbd-attach@0.service nbd-attach@1.service sys-kernel-config.mount
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
LUN1="/dev/nbd1"

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
  # If g1 already exists, idempotency: if bound + LUNs match, exit 0.
  # Otherwise tear it down before rebuilding.
  if [ -d "${GADGET_DIR}" ]; then
    current_udc=$(cat "${GADGET_DIR}/UDC" 2>/dev/null || true)
    current_lun0=$(cat "${GADGET_DIR}/functions/mass_storage.usb0/lun.0/file" 2>/dev/null || true)
    current_lun1=$(cat "${GADGET_DIR}/functions/mass_storage.usb0/lun.1/file" 2>/dev/null || true)
    if [ -n "${current_udc}" ] && [ "${current_lun0}" = "${LUN0}" ] && [ "${current_lun1}" = "${LUN1}" ]; then
      echo "teslausb-gadget-up: g1 already up on ${current_udc} with correct LUNs — idempotent no-op"
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

  # mass_storage function with two LUNs.
  mkdir -p "${GADGET_DIR}/functions/mass_storage.usb0"
  echo 1 > "${GADGET_DIR}/functions/mass_storage.usb0/stall"
  # lun.0 is created automatically; lun.1 must be mkdir-d.
  mkdir -p "${GADGET_DIR}/functions/mass_storage.usb0/lun.1"

  # LUN 0 — TeslaCam
  echo 1 > "${GADGET_DIR}/functions/mass_storage.usb0/lun.0/removable"
  echo 0 > "${GADGET_DIR}/functions/mass_storage.usb0/lun.0/cdrom"
  echo 0 > "${GADGET_DIR}/functions/mass_storage.usb0/lun.0/ro"
  echo "${LUN0}" > "${GADGET_DIR}/functions/mass_storage.usb0/lun.0/file"

  # LUN 1 — media
  echo 1 > "${GADGET_DIR}/functions/mass_storage.usb0/lun.1/removable"
  echo 0 > "${GADGET_DIR}/functions/mass_storage.usb0/lun.1/cdrom"
  echo 0 > "${GADGET_DIR}/functions/mass_storage.usb0/lun.1/ro"
  echo "${LUN1}" > "${GADGET_DIR}/functions/mass_storage.usb0/lun.1/file"

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
require_block "${LUN1}"
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
rmdir "${GADGET_DIR}/functions/mass_storage.usb0/lun.1" 2>/dev/null || true
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

# _b1_existing_volume_size_gb <conf_file>  — extract the
# `volume_size_gb = N` value from an existing TOML config, or empty.
_b1_existing_volume_size_gb() {
  local conf="$1"
  if [[ ! -e "${conf}" ]]; then
    return 0
  fi
  awk -F'=' '/^[[:space:]]*volume_size_gb[[:space:]]*=/ {
    gsub(/[[:space:]]/,"",$2); print $2; exit
  }' "${conf}"
}

# _b1_validate_size_gb <n>  — returns 0 if n is an integer in
# teslafat'"'"'s [4, 2048] range, else 1.
_b1_validate_size_gb() {
  local n="$1"
  [[ "${n}" =~ ^[0-9]+$ ]] || return 1
  (( n >= 4 && n <= 2048 )) || return 1
  return 0
}

# _b1_resolve_size_gb <lun_index> <label> <recommended> <env_var_name>
#                     <existing_conf_path>
# Returns chosen size on stdout. Priority:
#   1. Env var (TESLAUSB_LUN0_SIZE_GB / TESLAUSB_LUN1_SIZE_GB) if set+valid
#   2. Existing TOML config value (preserves operator choices on re-run)
#   3. Interactive prompt if stdin is a TTY and not dry-run/non-interactive
#   4. Recommended value
_b1_resolve_size_gb() {
  local idx="$1" label="$2" recommended="$3" env_var="$4" existing_conf="$5"
  local env_val="${!env_var:-}"

  if [[ -n "${env_val}" ]]; then
    if _b1_validate_size_gb "${env_val}"; then
      b1_log "  LUN ${idx} (${label}): using ${env_val} GB from ${env_var}" >&2
      echo "${env_val}"
      return 0
    fi
    b1_warn "  LUN ${idx} (${label}): ${env_var}=${env_val} invalid (must be int 4..2048); ignoring" >&2
  fi

  local existing
  existing=$(_b1_existing_volume_size_gb "${existing_conf}")
  if [[ -n "${existing}" ]] && _b1_validate_size_gb "${existing}"; then
    b1_log "  LUN ${idx} (${label}): preserving existing ${existing} GB from ${existing_conf}" >&2
    echo "${existing}"
    return 0
  fi

  # Interactive prompt only if: stdin is TTY, not dry-run, not non-interactive.
  if [[ -t 0 && "${TESLAUSB_DRY_RUN:-0}" != "1" && "${TESLAUSB_NON_INTERACTIVE:-0}" != "1" ]]; then
    local input
    while true; do
      printf '  LUN %s (%s) size in GB [recommended %s, range 4..2048]: ' \
        "${idx}" "${label}" "${recommended}" >&2
      if ! IFS= read -r input; then
        # EOF / Ctrl-D — fall through to recommended.
        echo >&2
        break
      fi
      input="${input// /}"
      if [[ -z "${input}" ]]; then
        b1_log "  LUN ${idx} (${label}): using recommended ${recommended} GB" >&2
        echo "${recommended}"
        return 0
      fi
      if _b1_validate_size_gb "${input}"; then
        b1_log "  LUN ${idx} (${label}): using ${input} GB (operator entered)" >&2
        echo "${input}"
        return 0
      fi
      printf '  invalid: must be integer in 4..2048\n' >&2
    done
  fi

  b1_log "  LUN ${idx} (${label}): using recommended ${recommended} GB" >&2
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
  local stage_file="${stage}/$(basename "${dst}")"
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

  # 2. teslafat per-LUN configs (with operator-chosen / recommended sizes).
  b1_log "resolving LUN sizes"
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
                  TESLAUSB_LUN0_SIZE_GB "${B1_TESLAFAT_CONF_0}")
  media_gb=$(_b1_resolve_size_gb 1 MEDIA "${media_rec}" \
              TESLAUSB_LUN1_SIZE_GB "${B1_TESLAFAT_CONF_1}")

  local conf0_body conf1_body
  conf0_body="${B1_TESLAFAT_CONF_0_TEMPLATE//__SIZE_GB__/${teslacam_gb}}"
  conf1_body="${B1_TESLAFAT_CONF_1_TEMPLATE//__SIZE_GB__/${media_gb}}"

  b1_log "installing teslafat per-LUN configs"
  _b1_install_file "${B1_TESLAFAT_CONF_0}" 0644 "${conf0_body}"
  _b1_install_file "${B1_TESLAFAT_CONF_1}" 0644 "${conf1_body}"

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

  # 5. Reload systemd if any unit changed.
  if [[ "${TESLAUSB_DRY_RUN:-0}" != "1" ]]; then
    b1_run systemctl daemon-reload
  else
    b1_log "DRY-RUN: systemctl daemon-reload"
  fi

  b1_log "gadget pipeline staged; activation (enable+start) deferred to Phase 6.10 re-run"
  b1_log "after 6.11: re-run 'sudo ./setup.sh --only 10' to activate teslafat + nbd-attach + usb-gadget"
}
