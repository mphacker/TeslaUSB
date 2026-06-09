#!/usr/bin/env bash
#
# TeslaUSB B-1 installer — systemd unit install + the §2 provisioning gate.
#
# THE load-bearing safety logic of Task 7.1. The danger (contract §2) is not a
# raw mutator — it is INDIRECTLY triggering gadgetd provisioning on a device
# with no image by enabling/starting the wrong unit. So:
#   * installing unit FILES is always safe (daemon-reload only).
#   * enabling/starting gadgetd-provision.service happens ONLY in the bootstrap
#     path (enable_provision_unit), never from the generic mode helpers.
#   * gadget units are enabled (persistence) but NEVER restarted by non-bootstrap
#     modes — a restart re-enumerates USB and interrupts the car's recording.

# install_unit_files <release_dir> — copy every units/*.service into place (with
# backup) and daemon-reload. Installing the FILE never enables anything.
install_unit_files() {
    local rel="$1" src unit base found=0
    src="${rel}/units"
    [ -d "$src" ] || die "$EX_PRECOND" "release has no units/ directory: ${src}"
    for unit in "$src"/*.service; do
        [ -e "$unit" ] || continue
        found=1
        base="$(basename "$unit")"
        mut_install_file "$unit" "${TESLAUSB_UNIT_DIR}/${base}" 0644
    done
    [ "$found" -eq 1 ] || die "$EX_PRECOND" "no *.service files in ${src}"
    systemctl_do daemon-reload
}

# enable_app_services — enable (persist) each installed app service. `enable`
# does not start/restart, so this is safe to call in any mode.
enable_app_services() {
    local svc
    for svc in $TESLAUSB_APP_SERVICES; do
        [ -e "${TESLAUSB_UNIT_DIR}/${svc}.service" ] || continue
        systemctl_do enable "${svc}.service"
    done
}

# restart_app_services — restart ONLY the app services (never the gadget units).
restart_app_services() {
    local svc
    for svc in $TESLAUSB_APP_SERVICES; do
        [ -e "${TESLAUSB_UNIT_DIR}/${svc}.service" ] || continue
        systemctl_do restart "${svc}.service"
    done
}

# stop_disable_app_services — used by uninstall safe-default. Covers staged
# services too: their unit FILES are installed (units.sh globs all *.service), so
# if one was ever enabled (now or by a future build) it must still be torn down.
# The [ -e ] guard makes this a harmless no-op for units not present.
stop_disable_app_services() {
    local svc
    for svc in $TESLAUSB_APP_SERVICES $TESLAUSB_STAGED_SERVICES; do
        [ -e "${TESLAUSB_UNIT_DIR}/${svc}.service" ] || continue
        systemctl_do stop "${svc}.service"
        systemctl_do disable "${svc}.service"
    done
}

# enable_gadget_units — persistence only. NEVER start/restart here, so a healthy
# already-running gadget is never disturbed by deploy-app/update/repair/rollback.
enable_gadget_units() {
    local unit
    for unit in $TESLAUSB_GADGET_UNITS; do
        [ -e "${TESLAUSB_UNIT_DIR}/${unit}.service" ] || continue
        systemctl_do enable "${unit}.service"
    done
}

# start_gadget_units — bootstrap/activate ONLY. Enables and starts the gadget
# units in order (gadgetd before gadgetd-control).
start_gadget_units() {
    local unit
    for unit in $TESLAUSB_GADGET_UNITS; do
        [ -e "${TESLAUSB_UNIT_DIR}/${unit}.service" ] || continue
        systemctl_do enable "${unit}.service"
        systemctl_do start "${unit}.service"
    done
}

# enable_provision_unit — THE ONLY path that enables provisioning (contract §2).
# Called exclusively by `install --bootstrap-image`. Enables and runs the oneshot
# so gadgetd creates the image if (and only if) it is absent.
enable_provision_unit() {
    local unit="${TESLAUSB_UNIT_DIR}/${TESLAUSB_PROVISION_UNIT}.service"
    [ -e "$unit" ] || die "$EX_PRECOND" \
        "--bootstrap-image requested but ${TESLAUSB_PROVISION_UNIT}.service is not in the release"
    systemctl_do enable "${TESLAUSB_PROVISION_UNIT}.service"
    systemctl_do start "${TESLAUSB_PROVISION_UNIT}.service"
}
