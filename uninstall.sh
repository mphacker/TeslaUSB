#!/usr/bin/env bash
# TeslaUSB B-1 idempotent uninstaller — Phase 6.11.
#
# Symmetric reverse of setup.sh. Sources setup-lib/00-common.sh for
# its b1_* helpers (b1_log / b1_run / b1_backup / b1_unit_* / ...) so
# the inverse steps share the exact same dry-run + logging contract.
# Each uninstall-lib/NN-<name>.sh defines b1_undo_NN() that reverses
# the corresponding setup-lib/NN-*.sh step. Files are dispatched in
# REVERSE numeric order (10 -> 09 -> ... -> 01) so dependencies come
# down before what depends on them.
#
# CLI:
#   uninstall.sh                 reverse install (mutate)
#   uninstall.sh --dry-run       show every command, mutate nothing
#   uninstall.sh --only NN[,NN]  run only the listed step numbers
#   uninstall.sh --skip NN[,NN]  skip the listed step numbers
#   uninstall.sh --purge         also delete user data / swap / subvols /
#                                purged-by-B-1 apt packages (DANGEROUS)
#   uninstall.sh --help          usage
#
# Exit codes (mirror setup.sh):
#   0  success (or dry-run completed)
#   2  bad CLI flags
#   3  missing dependency / precondition (not root, no B-1 install)
#   4  step failed mid-way
#
# Default mode is CONSERVATIVE: it removes unit files, drop-ins, the
# sudoers fragment, the sysctl drop-in, and AP profile, but preserves:
#   * user data (/srv/teslausb, /var/lib/teslausb-b1, the teslausb user)
#   * btrfs subvolumes (operator data is sacred)
#   * the swap file (existing v1 swap untouched, B-1 swap kept)
#   * every apt package (no `apt-get purge`)
#   * /home/pi/ — v1 lived there too; NEVER touched, even under --purge
#
# --purge additionally:
#   * unmounts + btrfs-deletes B1_BTRFS_SUBVOLS
#   * swapoffs + rm /var/swap/b1.swap, removes the matching fstab line
#   * userdel -r teslausb (only if no running processes own it)
#   * apt-get purge btrfs-progs (the ONLY package on the safe purge list)
#   * wipes /var/lib/teslausb-b1
# --purge NEVER touches /home/pi/, the pi user, /var/swap/fsck.swap,
# or any package v1 also installed (nginx, python3-venv, watchdog,
# dnsmasq-base, hostapd, network-manager, nbd-client).

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETUP_LIB_DIR="${SCRIPT_DIR}/setup-lib"
UNDO_LIB_DIR="${SCRIPT_DIR}/uninstall-lib"

if [[ ! -d "${SETUP_LIB_DIR}" ]]; then
  echo "FATAL: setup-lib/ missing next to uninstall.sh (${SETUP_LIB_DIR})" >&2
  exit 3
fi
if [[ ! -d "${UNDO_LIB_DIR}" ]]; then
  echo "FATAL: uninstall-lib/ missing next to uninstall.sh (${UNDO_LIB_DIR})" >&2
  exit 3
fi

# Reuse the setup helpers verbatim — they are the source of truth for
# b1_log / b1_run / b1_backup / b1_unit_* etc.
# shellcheck source=setup-lib/00-common.sh
source "${SETUP_LIB_DIR}/00-common.sh"

usage() {
  cat <<'USAGE'
TeslaUSB B-1 uninstaller (Phase 6.11).

Usage:
  uninstall.sh [--dry-run] [--only NN[,NN...]] [--skip NN[,NN...]] [--purge]
  uninstall.sh --help

Options:
  --dry-run            Print every command, mutate nothing.
  --only NN[,NN...]    Run only the listed step numbers (e.g. --only 09,10).
  --skip NN[,NN...]    Skip the listed step numbers.
  --purge              Also delete user data, btrfs subvolumes, B-1 swap
                       file, and apt-purge packages that v1 did NOT install
                       (currently: only btrfs-progs). NEVER touches the
                       `pi` user or /home/pi/.
  --help               Show this message.

Undo steps (sourced from uninstall-lib/<NN>-<name>.sh in REVERSE numeric
order so dependencies are torn down before what depends on them):
  10  activate        Disable + stop every B-1 unit; daemon-reload.
  09  mask-services   Unmask the desktop/print/modem services step 09 hid.
  08  memory          Remove vm.* sysctl drop-in (swap kept unless --purge).
  07  watchdog        Remove watchdog drop-in + restore /etc/watchdog.conf
                      from its .b1-backup-<ts> sidecar.
  06  boot            Restore /boot/firmware/{cmdline.txt,config.txt} from
                      their .b1-backup-<ts> sidecars.
  05  network         Remove AP profile + dnsmasq/hostapd configs.
  04  units           Remove every B-1 systemd unit + nginx drop-in;
                      daemon-reload.
  03  btrfs           No-op by default (data is sacred); --purge unmounts
                      and `btrfs subvolume delete`s the data subvolumes.
  02  users           Remove sudoers fragment + drop pi from teslausb group
                      (the teslausb user itself stays unless --purge).
  01  packages        No-op by default; --purge runs `apt-get purge
                      btrfs-progs` (the only package on the safe purge list).

Default mode is CONSERVATIVE: removes unit files, drop-ins, sudoers
fragment, sysctl drop-in, and AP profile, but PRESERVES user data,
btrfs subvolumes, the swap file, every apt package, and /home/pi/.

--purge additionally:
  * btrfs subvolume delete each subvolume in /srv/teslausb
  * swapoff + rm /var/swap/b1.swap (NOT /var/swap/fsck.swap — v1's stays)
  * userdel -r teslausb (only if no running processes own it)
  * apt-get purge btrfs-progs (the only package on the safe purge list)
  * wipe /var/lib/teslausb-b1
--purge NEVER touches /home/pi/, the `pi` user, /var/swap/fsck.swap, or
any package v1 also installed (nginx, python3-venv, watchdog,
dnsmasq-base, hostapd, network-manager, nbd-client).

Exit codes (mirror setup.sh):
  0  success (or dry-run completed)
  2  bad CLI flags / usage error
  3  missing dependency or precondition (not root, no B-1 install marker)
  4  a step failed mid-way (script aborts under set -Eeuo pipefail)

Examples:
  sudo ./uninstall.sh                   # conservative reverse install
  ./uninstall.sh --dry-run              # preview the teardown plan
  sudo ./uninstall.sh --only 10         # just disable + stop units
  sudo ./uninstall.sh --purge           # also wipe data + swap + safe packages

Each undo step is idempotent; re-running uninstall.sh on an already
uninstalled device must be a no-op.
USAGE
}

ONLY_LIST=""
SKIP_LIST=""
export B1_PURGE=0

while (( $# > 0 )); do
  case "$1" in
    --dry-run) export TESLAUSB_DRY_RUN=1 ;;
    --purge)   export B1_PURGE=1 ;;
    --only)
      ONLY_LIST="${2:-}"
      [[ -z "${ONLY_LIST}" ]] && { echo "--only requires a value" >&2; exit 2; }
      shift ;;
    --skip)
      SKIP_LIST="${2:-}"
      [[ -z "${SKIP_LIST}" ]] && { echo "--skip requires a value" >&2; exit 2; }
      shift ;;
    --help|-h) usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2 ;;
  esac
  shift
done

# Precondition: root (or dry-run).
if [[ "${TESLAUSB_DRY_RUN:-0}" != "1" && "$(id -u)" -ne 0 ]]; then
  echo "FATAL: uninstall.sh must be run as root (or with --dry-run)." >&2
  exit 3
fi

# Precondition: refuse to run on a host where B-1 was never installed.
# teslausb-web.service is the canonical Phase 6.4 artefact; its absence
# means setup.sh has not been run here. (Skipped under --dry-run so the
# operator can preview the uninstall plan on a clean dev box.)
B1_INSTALL_MARKER="/etc/systemd/system/teslausb-web.service"
if [[ "${TESLAUSB_DRY_RUN:-0}" != "1" && ! -e "${B1_INSTALL_MARKER}" ]]; then
  echo "FATAL: B-1 install marker missing: ${B1_INSTALL_MARKER}" >&2
  echo "  (setup.sh has not been run on this host — nothing to uninstall.)" >&2
  exit 3
fi

# Scary banner before --purge actually runs.
if (( B1_PURGE == 1 )) && [[ "${TESLAUSB_DRY_RUN:-0}" != "1" ]]; then
  cat >&2 <<'PURGE_BANNER'
================================================================================
  --purge is set. This will additionally:
    * btrfs subvolume delete each subvolume in /srv/teslausb (teslacam, media)
    * swapoff + rm /var/swap/b1.swap (NOT /var/swap/fsck.swap — v1's swap stays)
    * userdel -r teslausb (only if no running processes own it)
    * apt-get purge btrfs-progs (the only package on the safe purge list)
    * wipe /var/lib/teslausb-b1
  /home/pi/, the pi user, and any package v1 also installed are NEVER touched.
  Sleeping 5 seconds; Ctrl-C now to abort.
================================================================================
PURGE_BANNER
  sleep 5
fi

# Discover undo files; dispatch in REVERSE numeric order.
shopt -s nullglob
mapfile -t STEP_FILES < <(printf '%s\n' "${UNDO_LIB_DIR}"/[0-9][0-9]-*.sh | LC_ALL=C sort -r)
shopt -u nullglob
if (( ${#STEP_FILES[@]} == 0 )); then
  b1_log "FATAL: no undo step files found in ${UNDO_LIB_DIR}"
  exit 3
fi

declare -A ONLY_SET SKIP_SET
if [[ -n "${ONLY_LIST}" ]]; then
  IFS=',' read -ra _only <<< "${ONLY_LIST}"
  # 10#${n} forces base-10 so "08"/"09" aren't parsed as octal.
  for n in "${_only[@]}"; do ONLY_SET["$(printf '%02d' "$((10#${n}))")"]=1; done
fi
if [[ -n "${SKIP_LIST}" ]]; then
  IFS=',' read -ra _skip <<< "${SKIP_LIST}"
  for n in "${_skip[@]}"; do SKIP_SET["$(printf '%02d' "$((10#${n}))")"]=1; done
fi

b1_log "uninstall.sh starting (dry_run=${TESLAUSB_DRY_RUN:-0}, purge=${B1_PURGE}, steps=${#STEP_FILES[@]})"

for step_file in "${STEP_FILES[@]}"; do
  base="$(basename "${step_file}")"
  num="${base:0:2}"
  [[ "${num}" == "00" ]] && continue

  if [[ -n "${ONLY_LIST}" && -z "${ONLY_SET[${num}]:-}" ]]; then
    b1_log "[undo ${num}] skipped (--only)"; continue
  fi
  if [[ -n "${SKIP_SET[${num}]:-}" ]]; then
    b1_log "[undo ${num}] skipped (--skip)"; continue
  fi

  # shellcheck source=/dev/null
  source "${step_file}"
  fn="b1_undo_${num}"
  if ! declare -F "${fn}" >/dev/null; then
    b1_log "FATAL: ${base} did not declare ${fn}()"
    exit 4
  fi

  b1_log "[undo ${num}] start (${base})"
  if "${fn}"; then
    b1_log "[undo ${num}] done"
  else
    rc=$?
    b1_log "[undo ${num}] FAILED rc=${rc}"
    exit 4
  fi
done

b1_log "uninstall.sh completed successfully"
