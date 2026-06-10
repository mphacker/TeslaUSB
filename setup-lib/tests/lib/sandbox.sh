#!/usr/bin/env bash
#
# Shared host-test harness for the TeslaUSB installer (Task 7.1).
#
# Provides a FAKE-ROOT sandbox (a temp tree + fake `systemctl`/`gadgetd` on PATH)
# so tests never touch real /data, /usr/local, or the real systemd. Plus a tiny
# assertion framework. Sourced by the *.test.sh files.
#
# Tool gating: callers check `command -v` and skip-with-loud-note when a required
# tool is missing — never silent-pass.

# This harness defines constants/state (SETUP_SH, FIXTURES_DIR, SANDBOX, ...)
# consumed by the *.test.sh files that source it; SC2034 is a false positive
# when linting this file standalone. Suppress file-wide.
# shellcheck disable=SC2034

# Resolve repo root from this file's location.
TEST_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${TEST_LIB_DIR}/../../.." && pwd)"
SETUP_SH="${REPO_ROOT}/setup.sh"
UNINSTALL_SH="${REPO_ROOT}/uninstall.sh"
FIXTURES_DIR="${REPO_ROOT}/release/fixtures"

# --- assertions --------------------------------------------------------------
TESTS_PASS=0
TESTS_FAIL=0
TESTS_SKIP=0

_ok()   { printf 'ok   %s\n' "$1"; TESTS_PASS=$((TESTS_PASS + 1)); }
_fail() { printf 'FAIL %s\n' "$1"; TESTS_FAIL=$((TESTS_FAIL + 1)); }
_skip() { printf 'SKIP %s (%s)\n' "$1" "${2:-}"; TESTS_SKIP=$((TESTS_SKIP + 1)); }

# assert_exit <expected> <label> -- <cmd...>
assert_exit() {
    local expected="$1" label="$2"; shift 3
    local got=0
    "$@" >/dev/null 2>&1 || got=$?
    if [ "$got" -eq "$expected" ]; then _ok "${label} (exit ${got})"
    else _fail "${label} (want ${expected}, got ${got})"; fi
}

assert_file_exists()  { if [ -e "$1" ]; then _ok "$2"; else _fail "$2 (missing: $1)"; fi; }
assert_file_absent()  { if [ ! -e "$1" ]; then _ok "$2"; else _fail "$2 (present: $1)"; fi; }
assert_eq()           { if [ "$1" = "$2" ]; then _ok "$3"; else _fail "$3 (got '$1' != '$2')"; fi; }
assert_ne()           { if [ "$1" != "$2" ]; then _ok "$3"; else _fail "$3 (both '$1')"; fi; }

# assert_grep <pattern> <file> <label>
assert_grep()   { if grep -Eq "$1" "$2" 2>/dev/null; then _ok "$3"; else _fail "$3 (no /$1/ in $2)"; fi; }
assert_nogrep() { if grep -Eq "$1" "$2" 2>/dev/null; then _fail "$3 (found /$1/ in $2)"; else _ok "$3"; fi; }

# --- sandbox -----------------------------------------------------------------
# new_sandbox — create a fresh sandbox and set globals IN THE CURRENT SHELL
# (must NOT be called via $(...) or the exports would be lost to a subshell).
# Sets ${SANDBOX} to the base dir and exports the env the installer reads
# (TESLAUSB_PREFIX/AUDIT, SYSTEMCTL_LOG, PATH, FAKE_GADGET_BOUND).
new_sandbox() {
    local base; base="$(mktemp -d "${TMPDIR:-/tmp}/teslausb-sbx.XXXXXX")"
    mkdir -p "${base}/root" "${base}/bin"

    cat > "${base}/bin/systemctl" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "\${SYSTEMCTL_LOG}"
case "\${1:-}" in
  is-active)  echo inactive; exit 0 ;;
  is-enabled) echo disabled; exit 0 ;;
esac
exit 0
EOF
    cat > "${base}/bin/gadgetd" <<EOF
#!/usr/bin/env bash
case "\${1:-}" in
  status)
    echo "present:   true"
    if [ "\${FAKE_GADGET_BOUND:-0}" = "1" ]; then
      echo "bound_udc: 3f980000.usb"
    else
      echo "bound_udc: (unbound)"
    fi
    echo "udc_state: configured"
    echo "lun_file:  /data/teslausb/disk.img"
    ;;
esac
exit 0
EOF
    chmod +x "${base}/bin/systemctl" "${base}/bin/gadgetd"

    export TESLAUSB_PREFIX="${base}/root"
    export SYSTEMCTL_LOG="${base}/systemctl.log"
    export TESLAUSB_AUDIT="${base}/audit.log"
    : > "$SYSTEMCTL_LOG"
    : > "$TESLAUSB_AUDIT"
    export FAKE_GADGET_BOUND=0
    export PATH="${base}/bin:${PATH}"
    # Force a fresh run timestamp per sandbox so backups are deterministic.
    unset TESLAUSB_RUN_TS DRY_RUN
    SANDBOX="$base"
}

# reset_sandbox_logs — clear the audit + systemctl logs between phases.
reset_sandbox_logs() { : > "$SYSTEMCTL_LOG"; : > "$TESLAUSB_AUDIT"; }

# make_fake_disk_img — create a fake LUN under the sandbox; echoes its path.
make_fake_disk_img() {
    local img="${TESLAUSB_PREFIX}/data/teslausb/disk.img"
    mkdir -p "$(dirname "$img")"
    head -c 1048576 /dev/zero > "$img"
    printf '%s' "$img"
}

# make_fake_lun_img <name> — create a fake single-partition LUN image (e.g.
# teslacam.img or media.img) under the sandbox; echoes its path.
make_fake_lun_img() {
    local img="${TESLAUSB_PREFIX}/data/teslausb/$1"
    mkdir -p "$(dirname "$img")"
    head -c 1048576 /dev/zero > "$img"
    printf '%s' "$img"
}

# disk_fingerprint <img> — sha256 + size + mtime + inode, one line.
disk_fingerprint() {
    local img="$1"
    printf '%s|%s' \
        "$(sha256sum "$img" | cut -d' ' -f1)" \
        "$(stat -c '%s|%Y|%i' "$img")"
}

# make_release_dir <dir> [extra-unit...] — assemble a minimal, UNVERIFIED release
# tree (bin/spa/units) for gating tests that bypass verification. Copies
# the named extra unit files from deploy/systemd into units/.
make_release_dir() {
    local dir="$1"; shift
    mkdir -p "${dir}/bin" "${dir}/spa/assets" "${dir}/units"
    printf '#!/bin/true\n' > "${dir}/bin/gadgetd"
    printf '#!/bin/true\n' > "${dir}/bin/webd"
    printf 'x\n' > "${dir}/spa/index.html"
    printf 'x\n' > "${dir}/spa/assets/app.js"
    cp "${REPO_ROOT}/deploy/systemd/gadgetd.service" "${dir}/units/gadgetd.service"
    local u
    for u in "$@"; do
        cp "${REPO_ROOT}/deploy/systemd/${u}" "${dir}/units/${u}"
    done
}

cleanup_sandbox() { [ -n "${1:-}" ] && rm -rf "$1"; }
