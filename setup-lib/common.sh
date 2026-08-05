#!/usr/bin/env bash
#
# TeslaUSB B-1 installer — shared helpers (Task 7.1).
#
# Sourced by setup.sh and uninstall.sh. Defines paths, structured logging, and
# the SINGLE dry-run-aware mutation wrapper (`run_mutation`) that EVERY
# filesystem / systemctl mutation in the installer routes through. Nothing here
# mutates anything at source time; it only defines functions and path vars.
#
# The #1 invariant (contract §2): the installer NEVER creates, grows, lays out,
# moves, or deletes the car-facing backing image — that authority belongs solely
# to the gadgetd binary, reached only via gadgetd-provision.service. There is no
# raw image/partition tool anywhere in this file (contract §8 denylist).
#
# Sandbox: every system path is prefixed by ${TESLAUSB_PREFIX}, empty in
# production and a temp tree in the host tests, so tests never touch real
# /data or /usr/local.

# This is a library of constants/helpers consumed by the files that source it
# (setup.sh, uninstall.sh, modes.sh, units.sh, ...). shellcheck cannot see that
# cross-file usage when linting this file standalone, so SC2034 ("appears
# unused") is a false positive for every path/service constant below. Suppress
# it file-wide so the file lints clean both standalone and via `-x` from the
# entry points.
# shellcheck disable=SC2034

# --- exit codes (mirror setup.md §3) -----------------------------------------
EX_OK=0
EX_USAGE=2
EX_PRECOND=3
EX_STEP=4

# --- run identity ------------------------------------------------------------
: "${TESLAUSB_RUN_TS:=$(date -u +%Y%m%dT%H%M%SZ)}"
: "${DRY_RUN:=0}"
: "${BOOT_CHANGED:=0}"

# --- paths (contract §1; sandbox-overridable) --------------------------------
: "${TESLAUSB_PREFIX:=}"
TESLAUSB_DATA_ROOT="${TESLAUSB_PREFIX}/data/teslausb"
# Car-facing backing images (the sacred LUNs — contract §2 #1 invariant). The
# B-1 layout splits the legacy single combined image into two single-partition
# images, one per LUN:
#   teslacam.img — the dashcam LUN (TESLACAM exFAT; lun.0). NEVER interrupted.
#   media.img    — the MEDIA LUN (MEDIA exFAT; lun.1).
# disk.img is the LEGACY single combined image (MBR p1+p2); it is kept in the
# protected set so a device mid-migration (still on disk.img) is still guarded
# against an accidental write-through / rollback-over.
TESLAUSB_DISK_IMG="${TESLAUSB_DATA_ROOT}/disk.img"
TESLAUSB_TESLACAM_IMG="${TESLAUSB_DATA_ROOT}/teslacam.img"
TESLAUSB_MEDIA_IMG="${TESLAUSB_DATA_ROOT}/media.img"
# Every backing image the installer must NEVER write through or restore over.
TESLAUSB_LUN_IMAGES="${TESLAUSB_DISK_IMG} ${TESLAUSB_TESLACAM_IMG} ${TESLAUSB_MEDIA_IMG}"
TESLAUSB_ARCHIVE_DIR="${TESLAUSB_DATA_ROOT}/archive"
TESLAUSB_MEDIA_DIR="${TESLAUSB_DATA_ROOT}/media"
# webd zip-export scratch (WEBD_CACHE_DIR). On the data FS, not RAM-backed /tmp,
# so large exports never pressure the 512 MB Pi's memory.
TESLAUSB_CACHE_DIR="${TESLAUSB_DATA_ROOT}/cache"
TESLAUSB_BIN_DIR="${TESLAUSB_PREFIX}/usr/local/bin"
TESLAUSB_SPA_DIR="${TESLAUSB_PREFIX}/usr/local/share/teslausb/spa"
TESLAUSB_UNIT_DIR="${TESLAUSB_PREFIX}/etc/systemd/system"
TESLAUSB_CONFIG_DIR="${TESLAUSB_PREFIX}/etc/teslausb"
TESLAUSB_SECRETS_DIR="${TESLAUSB_CONFIG_DIR}/secrets"
TESLAUSB_STATE_DIR="${TESLAUSB_PREFIX}/var/lib/teslausb"
TESLAUSB_BOOT_DIR="${TESLAUSB_PREFIX}/boot/firmware"
TESLAUSB_BOOT_CONFIG="${TESLAUSB_BOOT_DIR}/config.txt"
TESLAUSB_MODULES_LOAD="${TESLAUSB_PREFIX}/etc/modules-load.d/teslausb-gadget.conf"
# NetworkManager Wi-Fi hardening drop-in (wifid spec §7.3): persists power-save-off
# + the uap0 unmanaged rule so the concurrent AP+STA barriers survive a fresh OS.
# Admin conf.d is authoritative over the vendor dir, so the numeric prefix is only
# for human ordering, not correctness.
TESLAUSB_NM_WIFI_CONF="${TESLAUSB_PREFIX}/etc/NetworkManager/conf.d/10-teslausb-wifi.conf"
TESLAUSB_BOOT_MARKER_BEGIN="# >>> TeslaUSB B-1 (managed) >>>"
TESLAUSB_BOOT_MARKER_END="# <<< TeslaUSB B-1 (managed) <<<"

# --- performance / footprint tuning (managed drops; applied on the post-install
#     reboot) --------------------------------------------------------------------
# Fresh-image install-time concerns for the 512 MB Pi Zero 2 W: keep swap OFF the
# microSD (zram), bound the journal's microSD writes, and shed daemons the
# appliance never uses. gpu_mem reclaim rides in the managed boot block
# (configure_boot_config). noatime is intentionally NOT managed here: Raspberry Pi
# OS Bookworm already mounts / with noatime, and editing fstab on a physically-
# unrecoverable in-car device is not worth a redundant flag.
TESLAUSB_ZRAMSWAP_CONF="${TESLAUSB_PREFIX}/etc/default/zramswap"
TESLAUSB_JOURNALD_CONF="${TESLAUSB_PREFIX}/etc/systemd/journald.conf.d/10-teslausb.conf"
# Passwordless sudo for the admin user so a re-imaged device self-configures for
# autonomous management: the imaging-set password bootstraps the FIRST
# `sudo ./setup.sh`, and this drop-in makes subsequent sudo password-free. 0440,
# visudo-validated before install.
TESLAUSB_ADMIN_USER="${TESLAUSB_ADMIN_USER:-pi}"
TESLAUSB_SUDOERS_NOPASSWD="${TESLAUSB_PREFIX}/etc/sudoers.d/010_${TESLAUSB_ADMIN_USER}-nopasswd"

# --- service / unit sets -----------------------------------------------------
# App services: ENABLED + restarted by the install/deploy/update modes because
# their live loops are wired today. (Per-service OOM kill order is set in each
# unit's OOMScoreAdjust, not by this list order — SPEC.md §7.)
# Ordering keeps each IPC producer ahead of its consumer in the restart
# sequence: scannerd before indexd (also enforced by systemd After=/Requires=),
# and schedulerd before webd. webd reaches schedulerd's control socket lazily
# (per-tick, with retry) so there is NO unit-level After= between them — this
# list order is what starts schedulerd ahead of webd's boot enforcement tick.
TESLAUSB_APP_SERVICES="scannerd indexd schedulerd webd wifid retentiond"
# retentiond is last: it is an indexd IPC consumer (eviction candidates + delete
# protocol), so indexd must already be up. It graduated from STAGED once the
# governor was calibrated and armed by default — see
# docs/specs/retention-eviction.md. It is an app service, not a gadget unit, so
# restarting it on update cannot interrupt the car's recording; archiving is
# staged-promote with durable markers (ADR-0006) and resumes on restart.
# Staged services: their unit FILES are installed (install_unit_files globs
# units/*.service), but they are NOT enabled/started because their live wiring
# is not yet landed:
#   uploadd   — `serve` is gated on Task 2.6 (WiFi TX-cap); exits non-zero until wired.
# When a service's loop lands, move its name from STAGED into APP_SERVICES.
# (scannerd graduated here once the scannerd->indexd IPC landed — ADR 0001.)
TESLAUSB_STAGED_SERVICES="uploadd"
# Gadget units: NEVER restarted by non-bootstrap modes (a restart re-enumerates
# USB and interrupts the car's recording — contract §2).
TESLAUSB_GADGET_UNITS="gadgetd gadgetd-control"
# Provisioning unit: enabled ONLY by `install --bootstrap-image` (contract §2).
TESLAUSB_PROVISION_UNIT="gadgetd-provision"
# Unnecessary services masked on a headless in-car recorder to free RAM/CPU.
# STRICTLY the lifeline-safe set: NEVER avahi-daemon (publishes the
# cybertruckusb.local mDNS name the operator reaches the car over) and NEVER
# wpa_supplicant / NetworkManager (the Wi-Fi link itself).
TESLAUSB_MASK_SERVICES="bluetooth.service triggerhappy.service ModemManager.service"
# Lifeline units the installer must NEVER mask or disable — severing any of these
# permanently cuts the only remote path to this in-car device (mDNS name +
# Wi-Fi link + SSH channel). assert_not_lifeline enforces this at RUNTIME as
# defense-in-depth (mirroring assert_safe_dest for the LUN images), so even a
# future edit that mistakenly adds one of these to TESLAUSB_MASK_SERVICES cannot
# brick the device. Matched on the bare stem: "avahi-daemon" and
# "avahi-daemon.service" both trip the guard.
TESLAUSB_LIFELINE_SERVICES="avahi-daemon wpa_supplicant NetworkManager ssh sshd"

# --- logging -----------------------------------------------------------------
log_info() { printf '[setup] %s\n' "$*" >&2; }
log_warn() { printf '[setup] WARNING: %s\n' "$*" >&2; }
log_err()  { printf '[setup] ERROR: %s\n' "$*" >&2; }

# die <code> <message...>
die() {
    local code="$1"; shift
    log_err "$*"
    exit "$code"
}

# --- the single mutation chokepoint ------------------------------------------
# run_mutation "<human description>" cmd arg...
#   * In --dry-run: logs the intended action and returns WITHOUT executing
#     anything — so a dry run can never invoke a raw mutator or systemctl.
#   * Otherwise: logs, records the exact argv to ${TESLAUSB_AUDIT} (when set, for
#     the host tests), then executes.
# EVERY mutating operation in the installer MUST go through here.
run_mutation() {
    local desc="$1"; shift
    [ "$#" -ge 1 ] || die "$EX_STEP" "run_mutation: no command given for: ${desc}"
    if [ "${DRY_RUN:-0}" = "1" ]; then
        log_info "[dry-run] would: ${desc}"
        return 0
    fi
    log_info "${desc}"
    if [ -n "${TESLAUSB_AUDIT:-}" ]; then
        printf '%s\n' "$*" >> "$TESLAUSB_AUDIT"
    fi
    "$@"
}

# --- destination safety (contract §2 #1 invariant, defense-in-depth) ----------
# _resolved <path> — canonical absolute path with symlinks resolved when the path
# (or its leading components) exists; falls back to the literal path otherwise.
_resolved() { readlink -f -- "$1" 2>/dev/null || printf '%s' "$1"; }

# is_lun_image <path> — true if <path> is one of the sacred car-facing backing
# images (by literal path OR with symlinks resolved). Used by the rollback
# guards and assert_safe_dest.
is_lun_image() {
    local target="$1" rtarget img rimg
    rtarget="$(_resolved "$target")"
    for img in $TESLAUSB_LUN_IMAGES; do
        rimg="$(_resolved "$img")"
        if [ "$target" = "$img" ] || [ "$rtarget" = "$rimg" ]; then
            return 0
        fi
    done
    return 1
}

# assert_safe_dest <dst> — guard EVERY system-path mutation. Refuses when:
#   * <dst> is (or, via a symlink, resolves to) ANY car-facing backing image —
#     string equality alone is not enough, since a planted symlink could let
#     install/cp/chmod write THROUGH it onto a LUN (contract §2 #1 invariant); or
#   * <dst> is itself an existing symlink — a symlink at a managed system path is
#     anomalous (tamper/foot-gun); we refuse to write through it rather than mutate
#     whatever it points at.
assert_safe_dest() {
    local dst="$1"
    if is_lun_image "$dst"; then
        die "$EX_STEP" "refusing to mutate the car-facing disk image (target ${dst})"
    fi
    if [ -L "$dst" ]; then
        die "$EX_STEP" "refusing to write through symlink at managed path: ${dst}"
    fi
}

# assert_not_lifeline <unit> — guard EVERY mask/disable driven by a service LIST.
# Refuses (dies) when <unit> is one of the remote-access lifelines
# (TESLAUSB_LIFELINE_SERVICES): mDNS, the Wi-Fi link, or SSH. Defense-in-depth
# mirroring assert_safe_dest — a caller "shouldn't" pass one, but on a physically
# unrecoverable in-car device we fail closed rather than risk severing access.
assert_not_lifeline() {
    local unit="$1" stem lifeline
    stem="${unit%.service}"
    for lifeline in $TESLAUSB_LIFELINE_SERVICES; do
        if [ "$stem" = "${lifeline%.service}" ]; then
            die "$EX_STEP" "refusing to mask/disable lifeline unit: ${unit}"
        fi
    done
}

# --- mutation helpers (thin wrappers; all route through run_mutation) ---------
mut_mkdir() { run_mutation "mkdir -p ${1}" mkdir -p "$1"; }
mut_rm()    { assert_safe_dest "$1"; run_mutation "rm -f ${1}" rm -f "$1"; }
mut_rmdir_tree() { assert_safe_dest "$1"; run_mutation "rm -rf ${1}" rm -rf "$1"; }
mut_chmod() { assert_safe_dest "$2"; run_mutation "chmod ${1} ${2}" chmod "$1" "$2"; }
mut_chown() { assert_safe_dest "$2"; run_mutation "chown ${1} ${2}" chown "$1" "$2"; }

# backup_path <target> — first-touch .b1-backup sidecar before overwriting
# anything outside our own tree. No-op if absent or already backed up this run.
backup_path() {
    local target="$1" bak
    [ -e "$target" ] || return 0
    bak="${target}.b1-backup-${TESLAUSB_RUN_TS}"
    [ -e "$bak" ] && return 0
    run_mutation "backup ${target} -> ${bak}" cp -a "$target" "$bak"
}

# mut_install_file <src> <dst> [mode] — backup, ensure parent, install.
mut_install_file() {
    local src="$1" dst="$2" mode="${3:-0644}"
    [ -f "$src" ] || die "$EX_STEP" "source file missing: ${src}"
    assert_safe_dest "$dst"
    backup_path "$dst"
    mut_mkdir "$(dirname "$dst")"
    run_mutation "install ${src} -> ${dst} (mode ${mode})" \
        install -m "$mode" "$src" "$dst"
}

# mut_install_tree <srcdir> <dstdir> — replace dstdir contents with srcdir's,
# backing up the old tree first. Used for the SPA bundle.
mut_install_tree() {
    local src="$1" dst="$2"
    [ -d "$src" ] || die "$EX_STEP" "source tree missing: ${src}"
    assert_safe_dest "$dst"
    backup_path "$dst"
    mut_rmdir_tree "$dst"
    mut_mkdir "$dst"
    run_mutation "copy tree ${src}/ -> ${dst}/" cp -a "${src}/." "$dst/"
}

# --- systemctl: mutations route through run_mutation; queries run directly ----
systemctl_do() { run_mutation "systemctl $*" systemctl "$@"; }

# systemctl_query — read-only (is-active / is-enabled); must NOT be dry-run
# gated, so callers get a truthful answer even during a dry run.
systemctl_query() { systemctl "$@"; }

# unit_is_active <name> — true iff systemctl reports the unit active.
unit_is_active() {
    [ "$(systemctl_query is-active "$1" 2>/dev/null || true)" = "active" ]
}

# --- gadget binding state (uninstall safety, contract §8) --------------------
# gadget_is_bound — returns 0 (bound) when the car-facing gadget IS, or MIGHT be,
# attached. Fail-safe: any uncertainty (gadgetd missing, unparseable output) is
# treated as bound so uninstall refuses rather than risk yanking a live drive.
gadget_is_bound() {
    local out
    if ! out="$(gadgetd status 2>/dev/null)"; then
        return 0
    fi
    if printf '%s\n' "$out" | grep -Eq '^bound_udc:[[:space:]]*\(unbound\)[[:space:]]*$'; then
        return 1
    fi
    return 0
}

# --- privilege ---------------------------------------------------------------
# require_privilege — root needed for real installs; skipped for --dry-run and
# for sandbox (prefixed) trees so the host tests run unprivileged.
require_privilege() {
    [ "${DRY_RUN:-0}" = "1" ] && return 0
    [ -n "${TESLAUSB_PREFIX:-}" ] && return 0
    if [ "$(id -u)" -ne 0 ]; then
        die "$EX_PRECOND" "must run as root (or use --dry-run)"
    fi
}
