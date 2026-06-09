#!/usr/bin/env bash
#
# TeslaUSB B-1 — uninstall.sh (Task 7.1, setup.md §9).
#
# Safe by default and guarded:
#   * REFUSES while the car-facing gadget is bound (the car may be writing).
#   * Default mode stops + disables the APP services but leaves gadgetd, the LUN
#     (disk.img), and boot config intact, so the drive keeps working.
#   * --full additionally removes our unit files (restoring .b1-backup baselines).
#   * --purge-data removes archive/ + media/ ONLY.
#   * disk.img (the LUN) is NEVER deleted by this script — the #1 invariant
#     (contract §2) holds for the whole installer suite, uninstall included.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETUP_LIB_DIR="${SCRIPT_DIR}/setup-lib"
export SETUP_LIB_DIR

# shellcheck source=setup-lib/common.sh
. "${SETUP_LIB_DIR}/common.sh"
# shellcheck source=setup-lib/units.sh
. "${SETUP_LIB_DIR}/units.sh"

FULL=0
PURGE_DATA=0
ASSUME_YES="${ASSUME_YES:-0}"

usage() {
    cat >&2 <<'EOF'
usage: uninstall.sh [--full] [--purge-data] [--yes] [--dry-run]

  (default)      Stop + disable the app services. Leaves gadgetd, the LUN
                 (disk.img), and boot config intact so the drive keeps working.
  --full         Also remove our installed unit files (restoring .b1-backup).
  --purge-data   Also delete archive/ and media/ (NEVER disk.img).
  --yes          Required to actually run (guarded; car must be disconnected).
  --dry-run      Print every action and mutate nothing.

REFUSES to run while the USB gadget is bound (disconnect the car first).
EOF
}

# remove_unit_file <name> — stop/disable then remove an installed unit file,
# restoring a .b1-backup sidecar if one exists. Never touches disk.img/data.
remove_unit_file() {
    local name="$1" bak
    local unit="${TESLAUSB_UNIT_DIR}/${name}.service"
    [ -e "$unit" ] || return 0
    systemctl_do stop "${name}.service"
    systemctl_do disable "${name}.service"
    bak="$(find "$TESLAUSB_UNIT_DIR" -maxdepth 1 -name "${name}.service.b1-backup-*" -print 2>/dev/null | sort | tail -n1)"
    if [ -n "$bak" ]; then
        run_mutation "restore ${bak} -> ${unit}" cp -a "$bak" "$unit"
    else
        mut_rm "$unit"
    fi
}

uninstall_full() {
    local unit
    # Remove every unit FILE the installer could have placed (units.sh globs all
    # units/*.service, so staged units are installed on disk even though they are
    # never enabled). remove_unit_file stop/disables first, then removes/restores.
    for unit in $TESLAUSB_APP_SERVICES $TESLAUSB_STAGED_SERVICES $TESLAUSB_GADGET_UNITS "$TESLAUSB_PROVISION_UNIT"; do
        remove_unit_file "$unit"
    done
    systemctl_do daemon-reload
    log_info "full uninstall: unit files removed (LUN + boot config preserved)"
}

purge_data() {
    # archive/ and media/ ONLY. disk.img (the LUN) is intentionally never deleted.
    log_warn "purging archive/ and media/ (disk.img/LUN is preserved)"
    mut_rmdir_tree "$TESLAUSB_ARCHIVE_DIR"
    mut_rmdir_tree "$TESLAUSB_MEDIA_DIR"
}

main() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --full)       FULL=1; shift ;;
            --purge-data) PURGE_DATA=1; shift ;;
            --yes)        ASSUME_YES=1; shift ;;
            --dry-run)    DRY_RUN=1; shift ;;
            -h|--help)    usage; exit "$EX_OK" ;;
            *)            die "$EX_USAGE" "unknown argument: $1" ;;
        esac
    done

    require_privilege

    # Hard refusal: never disturb a drive the car may be using.
    if gadget_is_bound; then
        die "$EX_PRECOND" "USB gadget is bound (the car may be using the drive). Disconnect the car and retry."
    fi

    if [ "${DRY_RUN}" != "1" ] && [ "${ASSUME_YES}" != "1" ]; then
        die "$EX_USAGE" "uninstall is guarded: re-run with --yes after disconnecting the car and backing up."
    fi
    [ "${DRY_RUN}" = "1" ] && log_info "DRY-RUN: no changes will be made"

    # Safe default: app services only; gadgetd + LUN + boot stay intact.
    stop_disable_app_services
    log_info "app services stopped + disabled (gadgetd / LUN / boot left intact)"

    [ "$FULL" = "1" ]       && uninstall_full
    [ "$PURGE_DATA" = "1" ] && purge_data

    log_info "uninstall complete (disk.img preserved)"
}

main "$@"
