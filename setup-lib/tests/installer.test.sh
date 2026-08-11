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

# A0: discover delegates to the installed Rust utility without mutation.
new_sandbox; sbx="$SANDBOX"
legacy_root="${sbx}/legacy"; mkdir -p "$legacy_root"
mkdir -p "${TESLAUSB_PREFIX}/usr/local/bin"
cat > "${TESLAUSB_PREFIX}/usr/local/bin/teslausb-migrate" <<'EOF'
#!/bin/sh
[ "$1" = "discover" ] || [ "$1" = "plan" ] || exit 1
[ "$2" = "--root" ] && [ -d "$3" ] || exit 1
printf '{"schema":1,"installation_detected":false,"items":[],"blockers":[]}\n'
EOF
chmod 0755 "${TESLAUSB_PREFIX}/usr/local/bin/teslausb-migrate"
rc=0; run_setup discover --root "$legacy_root" >/dev/null 2>&1 || rc=$?
assert_eq "$rc" 0 "discover delegates to teslausb-migrate"
rc=0; run_setup plan --root "$legacy_root" >/dev/null 2>&1 || rc=$?
assert_eq "$rc" 0 "plan delegates to teslausb-migrate"
rc=0; run_setup convert --root "$legacy_root" >/dev/null 2>&1 || rc=$?
assert_eq "$rc" 0 "convert delegates to teslausb-migrate"
assert_file_absent "${legacy_root}/config.yaml" "discover does not write legacy files"
cleanup_sandbox "$sbx"

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
assert_grep   'daemon-reload'              "$SYSTEMCTL_LOG" "deploy-app daemon-reloads"
assert_grep   '^restart webd\.service$'    "$SYSTEMCTL_LOG" "deploy-app restarts app service webd"
assert_grep   '^enable gadgetd\.service$'  "$SYSTEMCTL_LOG" "deploy-app enables gadgetd (persist only)"
assert_nogrep '(restart|start) gadgetd\.service' "$SYSTEMCTL_LOG" "deploy-app NEVER (re)starts gadgetd"
assert_nogrep 'gadgetd-provision'          "$SYSTEMCTL_LOG" "deploy-app NEVER touches gadgetd-provision"
cleanup_sandbox "$sbx"

# A2: install --bootstrap-image is the ONLY path that enables provisioning.
new_sandbox; sbx="$SANDBOX"
rel="${sbx}/rel"; make_release_dir "$rel" gadgetd-provision.service gadgetd-control.service
mkdir -p "${TESLAUSB_PREFIX}/boot/firmware"
printf '%s\n' '[cm5]' 'dtoverlay=dwc2,dr_mode=host' > "${TESLAUSB_PREFIX}/boot/firmware/config.txt"
rc=0; run_setup install --artifact-dir "$rel" --bootstrap-image --allow-unverified --yes >/dev/null 2>&1 || rc=$?
assert_eq "$rc" 0 "install --bootstrap-image succeeds"
assert_grep 'enable gadgetd-provision\.service' "$SYSTEMCTL_LOG" "bootstrap enables gadgetd-provision"
assert_grep 'start gadgetd-provision\.service'  "$SYSTEMCTL_LOG" "bootstrap runs gadgetd-provision oneshot"
assert_grep 'enable gadgetd\.service'           "$SYSTEMCTL_LOG" "bootstrap ENABLES the gadget for next boot"
assert_nogrep 'start gadgetd\.service'          "$SYSTEMCTL_LOG" "bootstrap does NOT start the gadget pre-reboot (staged)"
assert_grep 'install .*exfatprogs' "$APT_LOG" "bootstrap apt-installs exfatprogs"
assert_grep 'install .*hostapd'    "$APT_LOG" "bootstrap apt-installs hostapd"
assert_grep 'install .*dnsmasq'    "$APT_LOG" "bootstrap apt-installs dnsmasq"
assert_grep 'disable --now hostapd\.service' "$SYSTEMCTL_LOG" "bootstrap disables hostapd (wifid drives it)"
assert_grep 'disable --now dnsmasq\.service' "$SYSTEMCTL_LOG" "bootstrap disables dnsmasq (wifid drives it)"
assert_grep 'dtoverlay=dwc2,dr_mode=peripheral' "${TESLAUSB_PREFIX}/boot/firmware/config.txt" "config.txt gains dwc2 peripheral overlay"
assert_grep 'dr_mode=host' "${TESLAUSB_PREFIX}/boot/firmware/config.txt" "inert [cm5] host line preserved"
assert_grep 'dwc2'         "${TESLAUSB_PREFIX}/etc/modules-load.d/teslausb-gadget.conf" "modules-load has dwc2"
assert_grep 'libcomposite' "${TESLAUSB_PREFIX}/etc/modules-load.d/teslausb-gadget.conf" "modules-load has libcomposite"
assert_file_exists "${TESLAUSB_PREFIX}/etc/NetworkManager/conf.d/10-teslausb-wifi.conf" "install writes the NM Wi-Fi hardening drop-in (wifid §7.3)"
assert_grep 'wifi\.powersave=2' "${TESLAUSB_PREFIX}/etc/NetworkManager/conf.d/10-teslausb-wifi.conf" "NM drop-in disables Wi-Fi power save (barrier #1)"
assert_grep 'unmanaged-devices=interface-name:uap0' "${TESLAUSB_PREFIX}/etc/NetworkManager/conf.d/10-teslausb-wifi.conf" "NM drop-in keeps NM off the uap0 AP vif (barrier #4)"
# --- system tuning (footprint/perf for the 512 MB Pi Zero 2 W) ---
assert_grep 'gpu_mem=16' "${TESLAUSB_PREFIX}/boot/firmware/config.txt" "config.txt gains gpu_mem=16 (headless RAM reclaim)"
assert_file_exists "${TESLAUSB_PREFIX}/etc/default/zramswap" "install writes the zram swap config"
assert_grep 'ALGO=zstd'  "${TESLAUSB_PREFIX}/etc/default/zramswap" "zram config selects zstd"
assert_grep 'PERCENT=50' "${TESLAUSB_PREFIX}/etc/default/zramswap" "zram config sizes to 50% RAM"
assert_grep 'enable zramswap\.service'          "$SYSTEMCTL_LOG" "install enables zramswap"
assert_grep 'disable dphys-swapfile\.service'   "$SYSTEMCTL_LOG" "install disables microSD dphys-swapfile"
assert_grep 'install .*zram-tools'              "$APT_LOG"       "bootstrap apt-installs zram-tools"
assert_file_exists "${TESLAUSB_PREFIX}/etc/systemd/journald.conf.d/10-teslausb.conf" "install writes the journald cap"
assert_grep 'SystemMaxUse=64M' "${TESLAUSB_PREFIX}/etc/systemd/journald.conf.d/10-teslausb.conf" "journald cap bounds SystemMaxUse"
assert_grep 'mask bluetooth\.service'    "$SYSTEMCTL_LOG" "install masks bluetooth"
assert_grep 'mask triggerhappy\.service' "$SYSTEMCTL_LOG" "install masks triggerhappy"
assert_grep 'mask ModemManager\.service' "$SYSTEMCTL_LOG" "install masks ModemManager"
assert_nogrep 'mask avahi'                      "$SYSTEMCTL_LOG" "install NEVER masks avahi (mDNS lifeline)"
assert_nogrep 'mask (wpa_supplicant|NetworkManager)' "$SYSTEMCTL_LOG" "install NEVER masks the Wi-Fi link"
assert_file_exists "${TESLAUSB_PREFIX}/etc/sudoers.d/010_pi-nopasswd" "install writes passwordless-sudo drop-in"
assert_grep 'pi ALL=\(ALL\) NOPASSWD:ALL' "${TESLAUSB_PREFIX}/etc/sudoers.d/010_pi-nopasswd" "sudoers grants pi passwordless sudo"
if ls "${TESLAUSB_PREFIX}/boot/firmware/"config.txt.b1-backup-* >/dev/null 2>&1; then
    _ok "config.txt backup sidecar created"
else
    _fail "config.txt backup sidecar created"
fi
cleanup_sandbox "$sbx"

# A1c: the lifeline guard (defense-in-depth) refuses to mask/disable any unit that
# provides the device's only remote access — even if a future edit adds one to the
# mask list. Pure-function check, run in a separate bash process so sourcing
# common.sh cannot perturb this test shell's sandbox state.
_lifeline_rc() { bash "${HERE}/lib/lifeline-probe.sh" "$1" >/dev/null 2>&1; }
_lifeline_rc NetworkManager.service; assert_eq "$?" 4 "assert_not_lifeline dies (EX_STEP) on NetworkManager.service"
_lifeline_rc avahi-daemon;           assert_eq "$?" 4 "assert_not_lifeline dies on the bare avahi-daemon stem"
_lifeline_rc wpa_supplicant.service; assert_eq "$?" 4 "assert_not_lifeline dies on wpa_supplicant.service"
_lifeline_rc bluetooth.service;      assert_eq "$?" 0 "assert_not_lifeline allows a genuinely unnecessary unit"

# A2b: already-configured bootstrap is idempotent and starts gadget immediately.
new_sandbox; sbx="$SANDBOX"
rel="${sbx}/rel"; make_release_dir "$rel" gadgetd-provision.service gadgetd-control.service
mkdir -p "${TESLAUSB_PREFIX}/boot/firmware"
cat > "${TESLAUSB_PREFIX}/boot/firmware/config.txt" <<'EOF'
# >>> TeslaUSB B-1 (managed) >>>
[all]
dtoverlay=dwc2,dr_mode=peripheral
# <<< TeslaUSB B-1 (managed) <<<
EOF
mkdir -p "${TESLAUSB_PREFIX}/etc/modules-load.d"
cat > "${TESLAUSB_PREFIX}/etc/modules-load.d/teslausb-gadget.conf" <<'EOF'
# TeslaUSB B-1: USB gadget modules (managed)
dwc2
libcomposite
EOF
mkdir -p "${TESLAUSB_PREFIX}/etc/NetworkManager/conf.d"
cat > "${TESLAUSB_PREFIX}/etc/NetworkManager/conf.d/10-teslausb-wifi.conf" <<'EOF'
# TeslaUSB B-1: Wi-Fi hardening for concurrent AP+STA (managed; wifid spec §7.3)
# Single-radio Pi Zero 2 W: never sleep the Wi-Fi chip or manage the AP overlay vif.
[connection]
wifi.powersave=2

[keyfile]
unmanaged-devices=interface-name:uap0
EOF
rc=0; run_setup install --artifact-dir "$rel" --bootstrap-image --allow-unverified --yes >/dev/null 2>&1 || rc=$?
assert_eq "$rc" 0 "install --bootstrap-image (preconfigured) succeeds"
assert_nogrep 'dr_mode=otg' "${TESLAUSB_PREFIX}/boot/firmware/config.txt" "preconfigured bootstrap does not write dr_mode=otg"
if [ "$(find "${TESLAUSB_PREFIX}/boot/firmware" -maxdepth 1 -name 'config.txt.b1-backup-*' | wc -l | tr -d ' ')" = "0" ]; then
    _ok "preconfigured bootstrap creates no new config backup"
else
    _fail "preconfigured bootstrap creates no new config backup"
fi
if [ "$(find "${TESLAUSB_PREFIX}/etc/NetworkManager/conf.d" -maxdepth 1 -name '10-teslausb-wifi.conf.b1-backup-*' | wc -l | tr -d ' ')" = "0" ]; then
    _ok "preconfigured install rewrites no NM Wi-Fi drop-in (idempotent, no backup)"
else
    _fail "preconfigured install rewrites no NM Wi-Fi drop-in (idempotent, no backup)"
fi
assert_grep 'start gadgetd\.service' "$SYSTEMCTL_LOG" "already-configured bootstrap starts the gadget (BOOT_CHANGED=0)"
cleanup_sandbox "$sbx"

# A2c: package install is idempotent when required tools already exist.
new_sandbox; sbx="$SANDBOX"
rel="${sbx}/rel"; make_release_dir "$rel" gadgetd-provision.service gadgetd-control.service
mkdir -p "${TESLAUSB_PREFIX}/boot/firmware"
printf '%s\n' '[cm5]' 'dtoverlay=dwc2,dr_mode=host' > "${TESLAUSB_PREFIX}/boot/firmware/config.txt"
while read -r pkg probe _ || [ -n "$pkg" ]; do
    case "$pkg" in ''|'#'*) continue ;; esac
    cat > "${SANDBOX}/bin/${probe}" <<'EOF'
#!/bin/sh
exit 0
EOF
    chmod +x "${SANDBOX}/bin/${probe}"
done < "${REPO_ROOT}/setup-lib/required-packages.list"
rc=0; run_setup install --artifact-dir "$rel" --bootstrap-image --allow-unverified --yes >/dev/null 2>&1 || rc=$?
assert_eq "$rc" 0 "install --bootstrap-image succeeds when tools preexist"
assert_nogrep 'install' "$APT_LOG" "no apt install when all tools present"
assert_grep 'disable --now hostapd\.service' "$SYSTEMCTL_LOG" "hostapd still disabled even when present"
cleanup_sandbox "$sbx"

# A2d: a policy-rc.d guard left by an interrupted prior run is self-healed
# (adopted by its content marker and removed) on the next install. Probe tools
# are left ABSENT so packages install and the guard path runs.
new_sandbox; sbx="$SANDBOX"
rel="${sbx}/rel"; make_release_dir "$rel" gadgetd-provision.service gadgetd-control.service
mkdir -p "${TESLAUSB_PREFIX}/boot/firmware"
printf '%s\n' '[cm5]' 'dtoverlay=dwc2,dr_mode=host' > "${TESLAUSB_PREFIX}/boot/firmware/config.txt"
mkdir -p "${TESLAUSB_PREFIX}/usr/sbin"
printf '%s\n' '#!/bin/sh' \
    '# teslausb: transient apt no-start guard (auto-removed after install)' \
    'exit 101' > "${TESLAUSB_PREFIX}/usr/sbin/policy-rc.d"
chmod 0755 "${TESLAUSB_PREFIX}/usr/sbin/policy-rc.d"
run_setup install --artifact-dir "$rel" --bootstrap-image --allow-unverified --yes >/dev/null 2>&1 || true
assert_file_absent "${TESLAUSB_PREFIX}/usr/sbin/policy-rc.d" "leftover teslausb policy-rc.d guard is self-healed on re-run"
cleanup_sandbox "$sbx"

# A2e: a FOREIGN policy-rc.d (not our marker body) is NEVER removed or rewritten.
new_sandbox; sbx="$SANDBOX"
rel="${sbx}/rel"; make_release_dir "$rel" gadgetd-provision.service gadgetd-control.service
mkdir -p "${TESLAUSB_PREFIX}/boot/firmware"
printf '%s\n' '[cm5]' 'dtoverlay=dwc2,dr_mode=host' > "${TESLAUSB_PREFIX}/boot/firmware/config.txt"
mkdir -p "${TESLAUSB_PREFIX}/usr/sbin"
printf '%s\n' '#!/bin/sh' '# admin policy: allow all' 'exit 0' \
    > "${TESLAUSB_PREFIX}/usr/sbin/policy-rc.d"
chmod 0755 "${TESLAUSB_PREFIX}/usr/sbin/policy-rc.d"
run_setup install --artifact-dir "$rel" --bootstrap-image --allow-unverified --yes >/dev/null 2>&1 || true
assert_file_exists "${TESLAUSB_PREFIX}/usr/sbin/policy-rc.d" "foreign policy-rc.d is preserved (never removed)"
assert_grep 'admin policy: allow all' "${TESLAUSB_PREFIX}/usr/sbin/policy-rc.d" "foreign policy-rc.d content is untouched"
cleanup_sandbox "$sbx"

# A2f: a stale matching guard is self-healed even when ALL probe tools already
# exist (pkgs=0, so the package-install branch is skipped) — covers a crash that
# happened AFTER apt succeeded on a prior run.
new_sandbox; sbx="$SANDBOX"
rel="${sbx}/rel"; make_release_dir "$rel" gadgetd-provision.service gadgetd-control.service
mkdir -p "${TESLAUSB_PREFIX}/boot/firmware"
printf '%s\n' '[cm5]' 'dtoverlay=dwc2,dr_mode=host' > "${TESLAUSB_PREFIX}/boot/firmware/config.txt"
while read -r pkg probe _ || [ -n "$pkg" ]; do
    case "$pkg" in ''|'#'*) continue ;; esac
    cat > "${SANDBOX}/bin/${probe}" <<'EOF'
#!/bin/sh
exit 0
EOF
    chmod +x "${SANDBOX}/bin/${probe}"
done < "${REPO_ROOT}/setup-lib/required-packages.list"
mkdir -p "${TESLAUSB_PREFIX}/usr/sbin"
printf '%s\n' '#!/bin/sh' \
    '# teslausb: transient apt no-start guard (auto-removed after install)' \
    'exit 101' > "${TESLAUSB_PREFIX}/usr/sbin/policy-rc.d"
chmod 0755 "${TESLAUSB_PREFIX}/usr/sbin/policy-rc.d"
run_setup install --artifact-dir "$rel" --bootstrap-image --allow-unverified --yes >/dev/null 2>&1 || true
assert_nogrep 'install' "$APT_LOG" "no apt install when all tools present (self-heal case)"
assert_file_absent "${TESLAUSB_PREFIX}/usr/sbin/policy-rc.d" "stale guard self-healed even with pkgs=0 (crash-after-apt)"
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

# A3f: schedulerd (the chime-scheduler state owner) is a first-class app service
# — webd reaches its control socket — so install must ENABLE + RESTART it. This
# guards the fresh-OS deploy gap where schedulerd shipped in the release but was
# omitted from TESLAUSB_APP_SERVICES, leaving the chime scheduler dormant.
new_sandbox; sbx="$SANDBOX"
rel="${sbx}/rel"; make_release_dir "$rel" schedulerd.service
rc=0; run_setup install --artifact-dir "$rel" --allow-unverified --yes >/dev/null 2>&1 || rc=$?
assert_eq "$rc" 0 "install (with schedulerd unit) succeeds"
assert_grep '^enable schedulerd\.service$'  "$SYSTEMCTL_LOG" "install ENABLES schedulerd (chime scheduler state owner)"
assert_grep '^restart schedulerd\.service$' "$SYSTEMCTL_LOG" "install RESTARTS schedulerd app service"
cleanup_sandbox "$sbx"

# ============================================================================
# B. Dry-run invokes NO raw mutator and NO systemctl enable/restart
# ============================================================================
new_sandbox; sbx="$SANDBOX"
rel="${sbx}/rel"; make_release_dir "$rel" gadgetd-provision.service gadgetd-control.service
mkdir -p "${TESLAUSB_PREFIX}/boot/firmware"
printf '%s\n' '[cm5]' 'dtoverlay=dwc2,dr_mode=host' > "${TESLAUSB_PREFIX}/boot/firmware/config.txt"
rc=0; run_setup install --artifact-dir "$rel" --bootstrap-image --dry-run --allow-unverified --yes >/dev/null 2>&1 || rc=$?
assert_eq "$rc" 0 "install --bootstrap-image --dry-run succeeds"
assert_eq "$(wc -c < "$APT_LOG" | tr -d ' ')" 0 "dry-run invoked NO apt-get"
assert_eq "$(wc -c < "$TESLAUSB_AUDIT" | tr -d ' ')" 0 "dry-run executed NO mutation (audit log empty)"
assert_eq "$(wc -c < "$SYSTEMCTL_LOG" | tr -d ' ')" 0 "dry-run invoked NO systemctl"
assert_grep 'dr_mode=host' "${TESLAUSB_PREFIX}/boot/firmware/config.txt" "dry-run keeps existing boot config"
assert_nogrep 'dtoverlay=dwc2,dr_mode=peripheral' "${TESLAUSB_PREFIX}/boot/firmware/config.txt" "dry-run does not append dwc2 overlay"
assert_file_absent "${TESLAUSB_PREFIX}/etc/NetworkManager/conf.d/10-teslausb-wifi.conf" "dry-run does not write the NM Wi-Fi drop-in"
assert_file_absent "${TESLAUSB_PREFIX}/etc/modules-load.d/teslausb-gadget.conf" "dry-run does not write modules-load file"
assert_file_absent "${TESLAUSB_PREFIX}/etc/default/zramswap" "dry-run does not write zram config"
assert_file_absent "${TESLAUSB_PREFIX}/etc/sudoers.d/010_pi-nopasswd" "dry-run does not write sudoers drop-in"
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

# D6: update preserves existing secrets (and never manages a central config.toml).
new_sandbox; sbx="$SANDBOX"
mkdir -p "${TESLAUSB_PREFIX}/etc/teslausb/secrets"
printf 'SECRET_TOKEN\n'        > "${TESLAUSB_PREFIX}/etc/teslausb/secrets/token"
run_setup update --artifact-dir "$GOOD" --yes >/dev/null 2>&1 || true
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

# ============================================================================
# F. Two-image layout: BOTH single-partition LUN images are sacred (§2 #1)
# ============================================================================

# F1: a destination symlink resolving to teslacam.img or media.img is refused,
# and the image is left byte-for-byte untouched (the B-1 layout splits the old
# combined disk.img into two single-partition images, one per LUN — both must be
# protected by the same write-through guard).
for lun_img in teslacam.img media.img; do
    new_sandbox; sbx="$SANDBOX"
    img="$(make_fake_lun_img "$lun_img")"
    mkdir -p "${TESLAUSB_PREFIX}/usr/local/bin"
    ln -s "$img" "${TESLAUSB_PREFIX}/usr/local/bin/gadgetd"
    before="$(disk_fingerprint "$img")"
    assert_exit 4 "deploy-app refuses to write through a ${lun_img} symlink" -- \
        run_setup deploy-app --artifact-dir "$GOOD" --yes
    after="$(disk_fingerprint "$img")"
    assert_eq "$after" "$before" "${lun_img} untouched after refused symlink write"
    cleanup_sandbox "$sbx"
done

# F2: rollback never restores over a single-partition LUN image even with a
# planted sidecar (the disk.img guard must extend to teslacam.img/media.img).
for lun_img in teslacam.img media.img; do
    new_sandbox; sbx="$SANDBOX"
    img="$(make_fake_lun_img "$lun_img")"
    cp "$img" "${img}.b1-backup-19990101T000000Z"
    printf 'TAMPER' >> "${img}.b1-backup-19990101T000000Z"
    before="$(disk_fingerprint "$img")"
    run_setup rollback >/dev/null 2>&1 || true
    after="$(disk_fingerprint "$img")"
    assert_eq "$after" "$before" "rollback ignores a planted ${lun_img} backup"
    cleanup_sandbox "$sbx"
done

# ============================================================================
# E3. extraction-link safety (subshell-sourced; kept last so its subshell var
# usage doesn't shadow the straight-line tests above)
# ============================================================================

# extract_tarball_safe rejects a tarball containing a symlink member BEFORE
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
