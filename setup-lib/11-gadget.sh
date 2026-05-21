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
#   * /etc/teslausb/teslafat-0.toml — TeslaCam LUN config (256 GiB FAT32)
#   * /etc/teslausb/teslafat-1.toml — media LUN config (16 GiB FAT32)
#   * /etc/systemd/system/nbd-attach@.service — templated attach unit
#   * /etc/systemd/system/usb-gadget.service — configfs gadget oneshot
#   * /usr/local/bin/teslausb-gadget-up — configfs g1 builder script
#   * /usr/local/bin/teslausb-gadget-down — configfs g1 teardown script
#   * /usr/local/bin/teslausb-present-usb — UDC bind (Tesla "sees" drives)
#   * /usr/local/bin/teslausb-hide-usb — UDC unbind (Tesla "ejects" drives)
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
)

export B1_GADGET_NAME B1_GADGET_VENDOR_ID B1_GADGET_PRODUCT_ID \
  B1_GADGET_SERIAL B1_GADGET_MANUFACTURER B1_GADGET_PRODUCT \
  B1_NBD_MODPROBE_CONF B1_NBD_MODULES_LOAD B1_TESLAFAT_CONF_DIR \
  B1_TESLAFAT_CONF_0 B1_TESLAFAT_CONF_1 B1_NBD_ATTACH_UNIT \
  B1_USB_GADGET_UNIT B1_GADGET_UP_BIN B1_GADGET_DOWN_BIN \
  B1_PRESENT_USB_BIN B1_HIDE_USB_BIN B1_GADGET_TARGETS

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

B1_TESLAFAT_CONF_0_BODY='# Managed by teslausb-b1 setup.sh (Phase 6.11). Do not edit.
# LUN 0 — TeslaCam (dashcam + sentry + saved clips).
backing_root = "/srv/teslausb/teslacam"
volume_size_gb = 256
volume_label = "TESLACAM"
fs_type = "fat32"

[retention]
# Hide RecentClips entries older than 1 hour from Tesla. Matches
# Tesla'"'"'s own RecentClips rotation window; the worker still keeps
# the underlying files until the cleanup policy reaps them.
recentclips_hide_after_seconds = 3600

[nbd]
socket_path = "/run/teslausb/teslafat-0.sock"
handshake_timeout_seconds = 30
'

B1_TESLAFAT_CONF_1_BODY='# Managed by teslausb-b1 setup.sh (Phase 6.11). Do not edit.
# LUN 1 — user-managed media (lock chimes, light shows, music, wraps).
backing_root = "/srv/teslausb/media"
volume_size_gb = 16
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

[Service]
Type=oneshot
RemainAfterExit=yes
# nbd-client uses a unix socket: -unix <path> instead of -h/-p.
# -persist keeps the kernel side alive across brief daemon restarts.
# -block-size 4096 matches teslafat'"'"'s 4 KiB sector emulation.
ExecStart=/usr/sbin/nbd-client -unix /run/teslausb/teslafat-%i.sock /dev/nbd%i -persist -block-size 4096 -nofork
ExecStop=/usr/sbin/nbd-client -d /dev/nbd%i

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

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/teslausb-gadget-up
ExecStop=/usr/local/bin/teslausb-gadget-down

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

  # 2. teslafat per-LUN configs.
  b1_log "installing teslafat per-LUN configs"
  _b1_install_file "${B1_TESLAFAT_CONF_0}" 0644 "${B1_TESLAFAT_CONF_0_BODY}"
  _b1_install_file "${B1_TESLAFAT_CONF_1}" 0644 "${B1_TESLAFAT_CONF_1_BODY}"

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

  # 5. Reload systemd if any unit changed.
  if [[ "${TESLAUSB_DRY_RUN:-0}" != "1" ]]; then
    b1_run systemctl daemon-reload
  else
    b1_log "DRY-RUN: systemctl daemon-reload"
  fi

  b1_log "gadget pipeline staged; activation (enable+start) deferred to Phase 6.10 re-run"
  b1_log "after 6.11: re-run 'sudo ./setup.sh --only 10' to activate teslafat + nbd-attach + usb-gadget"
}
