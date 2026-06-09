#!/usr/bin/env bash
#
# TeslaUSB B-1 installer — payload install + mode implementations (Task 7.1).
#
# Mode safety summary (contract §2):
#   install --bootstrap-image : the ONLY mode that enables provisioning + starts
#                               the gadget. disk.img is created (if absent) by
#                               gadgetd, never by this script.
#   install (no bootstrap)    : installs payload + enables units (no gadget
#                               start, no provisioning).
#   deploy-app / update       : non-destructive payload refresh; never enable
#                               provisioning, never restart the gadget.
#   repair                    : re-assert perms/enablement; no data, no restart.
#   rollback                  : restore .b1-backup sidecars; NEVER over disk.img.

# --- payload install ---------------------------------------------------------

# ensure_data_roots — create data/archive/media/state/config dirs. disk.img is
# deliberately NOT created here: gadgetd is its sole owner (contract §2).
ensure_data_roots() {
    mut_mkdir "$TESLAUSB_DATA_ROOT"
    mut_mkdir "$TESLAUSB_ARCHIVE_DIR"
    mut_mkdir "$TESLAUSB_MEDIA_DIR"
    mut_mkdir "$TESLAUSB_CACHE_DIR"
    mut_mkdir "$TESLAUSB_STATE_DIR"
    mut_mkdir "$TESLAUSB_CONFIG_DIR"
}

install_binaries() {
    local rel="$1" f base
    [ -d "${rel}/bin" ] || die "$EX_PRECOND" "release has no bin/ directory: ${rel}/bin"
    for f in "${rel}/bin"/*; do
        [ -e "$f" ] || continue
        base="$(basename "$f")"
        mut_install_file "$f" "${TESLAUSB_BIN_DIR}/${base}" 0755
    done
}

install_spa() {
    local rel="$1"
    [ -d "${rel}/spa" ] || die "$EX_PRECOND" "release has no spa/ directory: ${rel}/spa"
    mut_install_tree "${rel}/spa" "$TESLAUSB_SPA_DIR"
}

# ensure_secrets_dir — create the root-only secrets dir (the systemd
# LoadCredential= source, setup.md §10), 0700. Per-service secret FILES are
# delivered out of band; nothing secret is shipped in the release artifact.
# NOTE: there is deliberately no central /etc/teslausb/config.toml — every daemon
# is configured via its unit's Environment= lines today. A unified config file is
# deferred until a daemon actually consumes one (see docs/tasks/tasks.md).
ensure_secrets_dir() {
    mut_mkdir "$TESLAUSB_CONFIG_DIR"
    mut_mkdir "$TESLAUSB_SECRETS_DIR"
    mut_chmod 0700 "$TESLAUSB_SECRETS_DIR"
}

# --- modes -------------------------------------------------------------------

mode_install() {
    require_privilege
    artifact_resolve_and_verify
    local rel="$RESOLVED_RELEASE_DIR"
    ensure_data_roots
    install_binaries "$rel"
    install_spa "$rel"
    ensure_secrets_dir
    install_unit_files "$rel"
    enable_app_services
    if [ "${BOOTSTRAP_IMAGE:-0}" = "1" ]; then
        log_warn "bootstrap: enabling ${TESLAUSB_PROVISION_UNIT} — gadgetd will create disk.img IF ABSENT, then bring the gadget up"
        log_warn "staged-reboot model: boot mutations are backed up; post-boot validation gates success (setup.md §7 step 9)"
        enable_provision_unit
        start_gadget_units
    else
        log_info "no --bootstrap-image: NOT provisioning; no image will be created. Enabling gadget units for next boot only."
        enable_gadget_units
    fi
    restart_app_services
    log_info "install complete"
}

mode_deploy_app() {
    [ "${BOOTSTRAP_IMAGE:-0}" = "1" ] && \
        die "$EX_USAGE" "deploy-app is non-destructive; --bootstrap-image is not permitted (use: install --bootstrap-image)"
    require_privilege
    artifact_resolve_and_verify
    local rel="$RESOLVED_RELEASE_DIR"
    ensure_data_roots
    install_binaries "$rel"
    install_spa "$rel"
    ensure_secrets_dir
    install_unit_files "$rel"
    enable_app_services
    enable_gadget_units
    restart_app_services
    log_info "deploy-app complete (disk.img / boot / partitions untouched)"
}

mode_update() {
    [ "${BOOTSTRAP_IMAGE:-0}" = "1" ] && \
        die "$EX_USAGE" "update is data-preserving; --bootstrap-image is not permitted"
    require_privilege
    artifact_resolve_and_verify
    local rel="$RESOLVED_RELEASE_DIR"
    install_binaries "$rel"
    install_spa "$rel"
    ensure_secrets_dir
    install_unit_files "$rel"
    enable_app_services
    enable_gadget_units
    restart_app_services
    log_info "update complete (disk.img / secrets / archive / index preserved)"
}

mode_repair() {
    require_privilege
    ensure_data_roots
    local b
    if [ -d "$TESLAUSB_BIN_DIR" ]; then
        for b in gadgetd $TESLAUSB_APP_SERVICES $TESLAUSB_STAGED_SERVICES; do
            [ -e "${TESLAUSB_BIN_DIR}/${b}" ] && mut_chmod 0755 "${TESLAUSB_BIN_DIR}/${b}"
        done
    fi
    [ -d "$TESLAUSB_SECRETS_DIR" ] && mut_chmod 0700 "$TESLAUSB_SECRETS_DIR"
    systemctl_do daemon-reload
    enable_app_services
    enable_gadget_units
    log_info "repair complete (no data changed; gadget not restarted)"
}

# rollback_one <target> — restore the newest .b1-backup-* sidecar over <target>,
# unless <target> is the disk image (never restored — contract §8).
rollback_one() {
    local target="$1" newest dir base
    if [ "$target" = "$TESLAUSB_DISK_IMG" ]; then
        log_warn "rollback: refusing to restore over disk.img"
        return 0
    fi
    dir="$(dirname "$target")"
    base="$(basename "$target")"
    [ -d "$dir" ] || return 0
    newest="$(find "$dir" -maxdepth 1 -name "${base}.b1-backup-*" -print 2>/dev/null | sort | tail -n1)"
    [ -n "$newest" ] || return 0
    assert_safe_dest "$target"
    run_mutation "rollback ${newest} -> ${target}" cp -a "$newest" "$target"
}

# rollback_dir_sidecars <dir> — restore every *.b1-backup-* sidecar found
# directly under <dir>. Never touches the data root, so disk.img is structurally
# out of scope; the explicit disk.img guard in rollback_one is belt-and-braces.
rollback_dir_sidecars() {
    local dir="$1" bak orig
    [ -d "$dir" ] || return 0
    while IFS= read -r bak; do
        [ -n "$bak" ] || continue
        orig="${bak%.b1-backup-*}"
        [ "$orig" = "$TESLAUSB_DISK_IMG" ] && continue
        assert_safe_dest "$orig"
        run_mutation "rollback ${bak} -> ${orig}" cp -a "$bak" "$orig"
    done < <(find "$dir" -maxdepth 1 -name '*.b1-backup-*' -print 2>/dev/null | sort)
}

mode_rollback() {
    require_privilege
    rollback_dir_sidecars "$TESLAUSB_BIN_DIR"
    rollback_dir_sidecars "$TESLAUSB_UNIT_DIR"
    rollback_one "$TESLAUSB_SPA_DIR"
    systemctl_do daemon-reload
    enable_app_services
    restart_app_services
    log_info "rollback complete (disk.img never restored)"
}
