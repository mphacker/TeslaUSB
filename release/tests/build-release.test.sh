#!/usr/bin/env bash
#
# Tests for release/build-release.sh (Task 7.2). Hermetic: uses the labelled
# release/fixtures stand-ins (no podman, no real cross-compile) to prove the
# pipeline mechanics — stage -> generate -> SELF-VERIFY (verify-release.sh) ->
# package -> the packaged tarball re-verifies and fails closed on mutation.
# Also proves the wrong-arch guard rejects a non-aarch64 binary.
#
# The REAL aarch64 build (arch check ON) is exercised live in the task report,
# not here, because it needs podman + the compiled binaries.
# Exits 0 iff every case passes.
set -u

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${here}/../.." && pwd)"
BR="${root}/release/build-release.sh"
VR="${root}/setup-lib/verify-release.sh"
FIXGOOD="${root}/release/fixtures/good"
VERSION='9.9.9-test'
COMMIT='0123456789abcdef0123456789abcdef01234567'

bash "${root}/release/fixtures/make-fixtures.sh" >/dev/null

pass=0 fail=0
ok()  { printf 'ok   %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf 'FAIL %s -- %s\n' "$1" "${2:-}"; fail=$((fail + 1)); }
assert_code() {
    local expected="$1" label="$2"; shift 3
    local got=0
    "$@" >/dev/null 2>&1 || got=$?
    if [ "$got" -eq "$expected" ]; then ok "$label (exit $got)"
    else bad "$label" "want $expected, got $got"; fi
}

# ---------------------------------------------------------------------------
# 1) Happy path (stand-ins + --skip-arch-check): build self-verifies + packages.
OUT="$(mktemp -d)"
assert_code 0 "build succeeds (stand-in inputs, skip-arch)" -- \
    bash "$BR" --version "$VERSION" --commit "$COMMIT" \
        --bin-dir "${FIXGOOD}/bin" --spa-dir "${FIXGOOD}/spa" \
        --units-dir "${FIXGOOD}/units" \
        --out "$OUT" --skip-arch-check

TARBALL="${OUT}/teslausb-${VERSION}-aarch64-unknown-linux-gnu.tar.gz"
if [ -f "$TARBALL" ] && [ -f "${TARBALL}.sha256" ]; then ok "tarball + .sha256 produced"
else bad "tarball produced" "no $TARBALL"; fi

# 2) The PACKAGED artifact extracts and re-verifies (exit 0).
EX="$(mktemp -d)"; tar -xzf "$TARBALL" -C "$EX"
EXDIR="${EX}/teslausb-${VERSION}-aarch64-unknown-linux-gnu"
assert_code 0 "packaged artifact re-verifies" -- bash "$VR" "$EXDIR"

# 3) Mutating the packaged artifact fails closed (exit 4).
printf 'tampered\n' > "${EXDIR}/bin/gadgetd"
assert_code 4 "mutated packaged artifact fails closed" -- bash "$VR" "$EXDIR"
rm -rf "$EX" "$OUT"

# ---------------------------------------------------------------------------
# 4) Wrong-arch guard: stand-in text "binaries" are NOT aarch64 -> reject (exit 4).
OUT2="$(mktemp -d)"
assert_code 4 "non-aarch64 binary rejected (arch check on)" -- \
    bash "$BR" --version "$VERSION" --commit "$COMMIT" \
        --bin-dir "${FIXGOOD}/bin" --spa-dir "${FIXGOOD}/spa" \
        --units-dir "${FIXGOOD}/units" --out "$OUT2"
rm -rf "$OUT2"

# ---------------------------------------------------------------------------
# 5) Usage / missing-input fail-closed.
OUT3="$(mktemp -d)"
assert_code 2 "no binary source is usage error" -- \
    bash "$BR" --version "$VERSION" --spa-dir "${FIXGOOD}/spa" --out "$OUT3"
assert_code 2 "no spa source is usage error" -- \
    bash "$BR" --version "$VERSION" --bin-dir "${FIXGOOD}/bin" --out "$OUT3"
assert_code 2 "both binary sources is usage error" -- \
    bash "$BR" --version "$VERSION" --bin-dir "${FIXGOOD}/bin" --cross-podman \
        --spa-dir "${FIXGOOD}/spa" --out "$OUT3"
assert_code 3 "missing a binary fails closed" -- \
    bash "$BR" --version "$VERSION" --commit "$COMMIT" --bin-dir "${OUT3}" --spa-dir "${FIXGOOD}/spa" \
        --units-dir "${FIXGOOD}/units" --skip-arch-check --out "$OUT3"
rm -rf "$OUT3"

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
