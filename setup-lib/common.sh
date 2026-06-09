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

# --- exit codes (mirror setup.md §3) -----------------------------------------
EX_OK=0
EX_USAGE=2
EX_PRECOND=3
EX_STEP=4

# --- run identity ------------------------------------------------------------
: "${TESLAUSB_RUN_TS:=$(date -u +%Y%m%dT%H%M%SZ)}"
: "${DRY_RUN:=0}"

# --- paths (contract §1; sandbox-overridable) --------------------------------
: "${TESLAUSB_PREFIX:=}"
TESLAUSB_DATA_ROOT="${TESLAUSB_PREFIX}/data/teslausb"
TESLAUSB_DISK_IMG="${TESLAUSB_DATA_ROOT}/disk.img"
TESLAUSB_ARCHIVE_DIR="${TESLAUSB_DATA_ROOT}/archive"
TESLAUSB_MEDIA_DIR="${TESLAUSB_DATA_ROOT}/media"
TESLAUSB_BIN_DIR="${TESLAUSB_PREFIX}/usr/local/bin"
TESLAUSB_SPA_DIR="${TESLAUSB_PREFIX}/usr/local/share/teslausb/spa"
TESLAUSB_UNIT_DIR="${TESLAUSB_PREFIX}/etc/systemd/system"
TESLAUSB_CONFIG_DIR="${TESLAUSB_PREFIX}/etc/teslausb"
TESLAUSB_CONFIG_FILE="${TESLAUSB_CONFIG_DIR}/config.toml"
TESLAUSB_SECRETS_DIR="${TESLAUSB_CONFIG_DIR}/secrets"
TESLAUSB_STATE_DIR="${TESLAUSB_PREFIX}/var/lib/teslausb"

# --- service / unit sets -----------------------------------------------------
# App services (restartable in any mode); canonical OOM order is uploadd first.
TESLAUSB_APP_SERVICES="scannerd indexd webd uploadd retentiond wifid"
# Gadget units: NEVER restarted by non-bootstrap modes (a restart re-enumerates
# USB and interrupts the car's recording — contract §2).
TESLAUSB_GADGET_UNITS="gadgetd gadgetd-control"
# Provisioning unit: enabled ONLY by `install --bootstrap-image` (contract §2).
TESLAUSB_PROVISION_UNIT="gadgetd-provision"

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

# assert_safe_dest <dst> — guard EVERY system-path mutation. Refuses when:
#   * <dst> is (or, via a symlink, resolves to) the car-facing disk image — string
#     equality alone is not enough, since a planted symlink could let install/cp/
#     chmod write THROUGH it onto disk.img (contract §2 #1 invariant); or
#   * <dst> is itself an existing symlink — a symlink at a managed system path is
#     anomalous (tamper/foot-gun); we refuse to write through it rather than mutate
#     whatever it points at.
assert_safe_dest() {
    local dst="$1" rdst rimg
    rimg="$(_resolved "$TESLAUSB_DISK_IMG")"
    rdst="$(_resolved "$dst")"
    if [ "$dst" = "$TESLAUSB_DISK_IMG" ] || [ "$rdst" = "$rimg" ]; then
        die "$EX_STEP" "refusing to mutate the car-facing disk image (target ${dst})"
    fi
    if [ -L "$dst" ]; then
        die "$EX_STEP" "refusing to write through symlink at managed path: ${dst}"
    fi
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
