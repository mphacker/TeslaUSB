#!/usr/bin/env bash
#
# Installer host tests (Task 7.1, contract §7/§8): mode wiring, the §2
# provisioning gate, the dry-run mutation guarantee, the disk.img sentinel, and
# the negative tests. Runs entirely in a fake-root sandbox. No bats dependency.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=setup-lib/tests/lib/sandbox.sh
. "${HERE}/lib/sandbox.sh"

# Required tools — skip loudly if absent (never silent-pass).
for tool in bash sha256sum stat find install; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        _skip "installer.test.sh" "missing tool: ${tool}"
        printf '\n%s passed, %s failed, %s skipped\n' "$TESTS_PASS" "$TESTS_FAIL" "$TESTS_SKIP"
        exit 0
    fi
done

# Ensure verification fixtures exist + are current.
bash "${FIXTURES_DIR}/make-fixtures.sh" >/dev/null
GOOD="${FIXTURES_DIR}/good"
TAMPERED="${FIXTURES_DIR}/tampered"

run_setup()     { bash "$SETUP_SH" "$@"; }
run_uninstall() { bash "$UNINSTALL_SH" "$@"; }

# ============================================================================
# A. Mode wiring + the §2 provisioning gate
# ============================================================================

# A1: deploy-app (verified, real) installs payload + restarts app svcs only.
new_sandbox; sbx="$SANDBOX"
rc=0; run_setup deploy-app --artifact-dir "$GOOD" --yes >/dev/null 2>&1 || rc=$?
assert_eq "$rc" 0 "deploy-app(good) succeeds"
assert_file_exists "${TESLAUSB_PREFIX}/usr/local/bin/gadgetd"        "deploy-app installs gadgetd binary"
assert_file_exists "${TESLAUSB_PREFIX}/usr/local/bin/webd"           "deploy-app installs webd binary"
assert_file_exists "${TESLAUSB_PREFIX}/etc/systemd/system/gadgetd.service" "deploy-app installs gadgetd.service"
assert_file_exists "${TESLAUSB_PREFIX}/etc/teslausb/config.toml"     "deploy-app seeds config.toml"
assert_grep   'daemon-reload'              "$SYSTEMCTL_LOG" "deploy-app daemon-reloads"
assert_grep   '^restart webd\.service$'    "$SYSTEMCTL_LOG" "deploy-app restarts app service webd"
assert_grep   '^enable gadgetd\.service$'  "$SYSTEMCTL_LOG" "deploy-app enables gadgetd (persist only)"
assert_nogrep '(restart|start) gadgetd\.service' "$SYSTEMCTL_LOG" "deploy-app NEVER (re)starts gadgetd"
assert_nogrep 'gadgetd-provision'          "$SYSTEMCTL_LOG" "deploy-app NEVER touches gadgetd-provision"
cleanup_sandbox "$sbx"

# A2: install --bootstrap-image is the ONLY path that enables provisioning.
new_sandbox; sbx="$SANDBOX"
rel="${sbx}/rel"; make_release_dir "$rel" gadgetd-provision.service gadgetd-control.service
rc=0; run_setup install --artifact-dir "$rel" --bootstrap-image --allow-unverified --yes >/dev/null 2>&1 || rc=$?
assert_eq "$rc" 0 "install --bootstrap-image succeeds"
assert_grep 'enable gadgetd-provision\.service' "$SYSTEMCTL_LOG" "bootstrap enables gadgetd-provision"
assert_grep 'start gadgetd-provision\.service'  "$SYSTEMCTL_LOG" "bootstrap runs gadgetd-provision oneshot"
assert_grep 'start gadgetd\.service'            "$SYSTEMCTL_LOG" "bootstrap starts the gadget"
cleanup_sandbox "$sbx"

# A3: install WITHOUT --bootstrap-image never provisions nor starts the gadget.
new_sandbox; sbx="$SANDBOX"
rel="${sbx}/rel"; make_release_dir "$rel" gadgetd-provision.service gadgetd-control.service
rc=0; run_setup install --artifact-dir "$rel" --allow-unverified --yes >/dev/null 2>&1 || rc=$?
assert_eq "$rc" 0 "install (no bootstrap) succeeds"
assert_nogrep 'gadgetd-provision'    "$SYSTEMCTL_LOG" "non-bootstrap install NEVER enables provisioning"
assert_grep   'enable gadgetd\.service' "$SYSTEMCTL_LOG" "non-bootstrap install enables gadgetd (persist)"
assert_nogrep 'start gadgetd\.service'  "$SYSTEMCTL_LOG" "non-bootstrap install NEVER starts the gadget"
cleanup_sandbox "$sbx"

# ============================================================================
# B. Dry-run invokes NO raw mutator and NO systemctl enable/restart
# ============================================================================
new_sandbox; sbx="$SANDBOX"
rc=0; run_setup deploy-app --artifact-dir "$GOOD" --dry-run --yes >/dev/null 2>&1 || rc=$?
assert_eq "$rc" 0 "deploy-app --dry-run succeeds"
assert_eq "$(wc -c < "$TESLAUSB_AUDIT" | tr -d ' ')" 0 "dry-run executed NO mutation (audit log empty)"
assert_eq "$(wc -c < "$SYSTEMCTL_LOG" | tr -d ' ')" 0 "dry-run invoked NO systemctl"
assert_file_absent "${TESLAUSB_PREFIX}/usr/local/bin/gadgetd" "dry-run created no files"
cleanup_sandbox "$sbx"

# ============================================================================
# C. Sentinel: disk.img untouched across all non-bootstrap modes (dry + real)
# ============================================================================
sentinel_modes_dry_and_real() {
    local label="$1"; shift
    local sbx img before after
    new_sandbox; sbx="$SANDBOX"
    img="$(make_fake_disk_img)"
    before="$(disk_fingerprint "$img")"
    # dry-run pass
    run_setup deploy-app --artifact-dir "$GOOD" --dry-run --yes >/dev/null 2>&1 || true
    run_setup update     --artifact-dir "$GOOD" --dry-run --yes >/dev/null 2>&1 || true
    run_setup repair     --dry-run >/dev/null 2>&1 || true
    run_setup rollback   --dry-run >/dev/null 2>&1 || true
    run_uninstall --dry-run >/dev/null 2>&1 || true
    # real pass (sandbox)
    run_setup deploy-app --artifact-dir "$GOOD" --yes >/dev/null 2>&1 || true
    run_setup update     --artifact-dir "$GOOD" --yes >/dev/null 2>&1 || true
    run_setup repair >/dev/null 2>&1 || true
    run_setup rollback >/dev/null 2>&1 || true
    run_uninstall --yes >/dev/null 2>&1 || true
    after="$(disk_fingerprint "$img")"
    assert_eq "$after" "$before" "${label}: disk.img sha/size/mtime/inode unchanged"
    cleanup_sandbox "$sbx"
}
sentinel_modes_dry_and_real "sentinel"

# C2: rollback never restores over disk.img even with a planted sidecar.
new_sandbox; sbx="$SANDBOX"
img="$(make_fake_disk_img)"
cp "$img" "${img}.b1-backup-19990101T000000Z"
printf 'TAMPER' >> "${img}.b1-backup-19990101T000000Z"   # make the backup differ
before="$(disk_fingerprint "$img")"
run_setup rollback >/dev/null 2>&1 || true
after="$(disk_fingerprint "$img")"
assert_eq "$after" "$before" "rollback ignores a planted disk.img backup"
cleanup_sandbox "$sbx"

# ============================================================================
# D. Negative tests
# ============================================================================

# D1: deploy-app refuses --bootstrap-image.
new_sandbox; sbx="$SANDBOX"
assert_exit 2 "deploy-app refuses --bootstrap-image" -- run_setup deploy-app --artifact-dir "$GOOD" --bootstrap-image --yes
cleanup_sandbox "$sbx"

# D2: update refuses --bootstrap-image.
new_sandbox; sbx="$SANDBOX"
assert_exit 2 "update refuses --bootstrap-image" -- run_setup update --artifact-dir "$GOOD" --bootstrap-image --yes
cleanup_sandbox "$sbx"

# D3: tampered artifact fails closed without --allow-unverified.
new_sandbox; sbx="$SANDBOX"
assert_exit 4 "tampered artifact refused (no --allow-unverified)" -- run_setup deploy-app --artifact-dir "$TAMPERED" --yes
cleanup_sandbox "$sbx"

# D4: --allow-unverified without --yes is refused.
new_sandbox; sbx="$SANDBOX"
assert_exit 2 "--allow-unverified requires --yes" -- run_setup deploy-app --artifact-dir "$TAMPERED" --allow-unverified
cleanup_sandbox "$sbx"

# D5: malformed manifest.env fails closed.
new_sandbox; sbx="$SANDBOX"
bad="${sbx}/badrel"; mkdir -p "$bad"; cp -a "${GOOD}/." "$bad/"
grep -v '^GIT_COMMIT=' "${bad}/manifest.env" > "${bad}/m" && mv "${bad}/m" "${bad}/manifest.env"
assert_exit 4 "malformed manifest fails closed" -- run_setup deploy-app --artifact-dir "$bad" --yes
cleanup_sandbox "$sbx"

# D6: update preserves an existing config.toml + secrets.
new_sandbox; sbx="$SANDBOX"
mkdir -p "${TESLAUSB_PREFIX}/etc/teslausb/secrets"
printf 'LIVE_CONFIG_SENTINEL\n' > "${TESLAUSB_PREFIX}/etc/teslausb/config.toml"
printf 'SECRET_TOKEN\n'        > "${TESLAUSB_PREFIX}/etc/teslausb/secrets/token"
run_setup update --artifact-dir "$GOOD" --yes >/dev/null 2>&1 || true
assert_eq "$(cat "${TESLAUSB_PREFIX}/etc/teslausb/config.toml")" "LIVE_CONFIG_SENTINEL" "update preserves live config.toml"
assert_eq "$(cat "${TESLAUSB_PREFIX}/etc/teslausb/secrets/token")" "SECRET_TOKEN" "update preserves secrets"
cleanup_sandbox "$sbx"

# D7: uninstall REFUSES while the gadget is bound.
new_sandbox; sbx="$SANDBOX"
export FAKE_GADGET_BOUND=1
assert_exit 3 "uninstall refuses while gadget bound" -- run_uninstall --yes
export FAKE_GADGET_BOUND=0
cleanup_sandbox "$sbx"

# D8: uninstall safe-default preserves the LUN + leaves gadgetd alone.
new_sandbox; sbx="$SANDBOX"
img="$(make_fake_disk_img)"
run_setup deploy-app --artifact-dir "$GOOD" --yes >/dev/null 2>&1 || true
reset_sandbox_logs
rc=0; run_uninstall --yes >/dev/null 2>&1 || rc=$?
assert_eq "$rc" 0 "uninstall (unbound) succeeds"
assert_file_exists "$img" "uninstall preserves the LUN (disk.img)"
assert_grep   '^disable webd\.service$' "$SYSTEMCTL_LOG" "uninstall disables app service webd"
assert_nogrep 'stop gadgetd\.service'   "$SYSTEMCTL_LOG" "uninstall leaves gadgetd running (safe default)"
cleanup_sandbox "$sbx"

# ============================================================================
# E. Destination-symlink + extraction-link safety (defense-in-depth, §2/§5)
# ============================================================================

# E1: a destination symlink resolving to disk.img is refused, and disk.img is
# left byte-for-byte untouched (string-equality guard alone would miss this).
new_sandbox; sbx="$SANDBOX"
img="$(make_fake_disk_img)"
mkdir -p "${TESLAUSB_PREFIX}/usr/local/bin"
ln -s "$img" "${TESLAUSB_PREFIX}/usr/local/bin/gadgetd"
before="$(disk_fingerprint "$img")"
assert_exit 4 "deploy-app refuses to write through a disk.img symlink" -- \
    run_setup deploy-app --artifact-dir "$GOOD" --yes
after="$(disk_fingerprint "$img")"
assert_eq "$after" "$before" "disk.img untouched after refused symlink write"
cleanup_sandbox "$sbx"

# E2: any pre-existing symlink at a managed system path is refused (not only the
# disk.img case) — we never write through a planted link.
new_sandbox; sbx="$SANDBOX"
mkdir -p "${TESLAUSB_PREFIX}/usr/local/bin" "${sbx}/decoy"
printf 'x\n' > "${sbx}/decoy/target"
ln -s "${sbx}/decoy/target" "${TESLAUSB_PREFIX}/usr/local/bin/gadgetd"
assert_exit 4 "deploy-app refuses a planted symlink at a managed path" -- \
    run_setup deploy-app --artifact-dir "$GOOD" --yes
assert_eq "$(cat "${sbx}/decoy/target")" "x" "decoy symlink target left unmodified"
cleanup_sandbox "$sbx"

# E3: extract_tarball_safe rejects a tarball containing a symlink member BEFORE
# any extraction (so a link cannot be used to escape the destination). Gated on
# tar + ln; the remote extraction path is otherwise network-only.
if command -v tar >/dev/null 2>&1 && command -v ln >/dev/null 2>&1; then
    new_sandbox; sbx="$SANDBOX"
    # Set once in the parent; the (..) subshells below inherit it.
    SETUP_LIB_DIR="${REPO_ROOT}/setup-lib"
    mdir="${sbx}/payload"; mkdir -p "$mdir"
    ln -s /etc "${mdir}/escape"
    printf 'x\n' > "${mdir}/file"
    tar -czf "${sbx}/evil.tgz" -C "$mdir" .
    rc=0
    ( # shellcheck source=setup-lib/common.sh
      . "${SETUP_LIB_DIR}/common.sh"
      # shellcheck source=setup-lib/artifact.sh
      . "${SETUP_LIB_DIR}/artifact.sh"
      extract_tarball_safe "${sbx}/evil.tgz" "${sbx}/out" ) >/dev/null 2>&1 || rc=$?
    assert_eq "$rc" 4 "extract_tarball_safe refuses a symlink member (exit 4)"
    assert_file_absent "${sbx}/out/escape" "no link member was extracted"

    # Positive control: a clean tarball extracts successfully.
    cdir="${sbx}/clean"; mkdir -p "${cdir}/bin"
    printf 'x\n' > "${cdir}/bin/app"
    tar -czf "${sbx}/clean.tgz" -C "$cdir" .
    rc=0
    ( # shellcheck source=setup-lib/common.sh
      . "${SETUP_LIB_DIR}/common.sh"
      # shellcheck source=setup-lib/artifact.sh
      . "${SETUP_LIB_DIR}/artifact.sh"
      extract_tarball_safe "${sbx}/clean.tgz" "${sbx}/cout" ) >/dev/null 2>&1 || rc=$?
    assert_eq "$rc" 0 "extract_tarball_safe accepts a clean tarball (exit 0)"
    cleanup_sandbox "$sbx"
else
    _skip "extract_tarball_safe link-member tests" "missing tar or ln"
fi

printf '\n%s passed, %s failed, %s skipped\n' "$TESTS_PASS" "$TESTS_FAIL" "$TESTS_SKIP"
[ "$TESTS_FAIL" -eq 0 ]
