#!/usr/bin/env bash
#
# Tests for setup-lib/verify-release.sh (Phase 7.0 contract §6/§8). Plain bash —
# no bats dependency, so it runs anywhere coreutils + bash exist (WSL, the Pi,
# CI). Asserts the documented exit codes and the fail-closed behavior both lanes
# rely on. Exits 0 iff every case passes.
set -u

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${here}/../.." && pwd)"
VR="${root}/setup-lib/verify-release.sh"
FIX="${root}/release/fixtures"

# Ensure fixtures exist / are current.
bash "${FIX}/make-fixtures.sh" >/dev/null

pass=0 fail=0
# assert_code <expected> <label> -- <command...>
assert_code() {
    local expected="$1" label="$2"; shift 3
    local got=0
    "$@" >/dev/null 2>&1 || got=$?
    if [ "$got" -eq "$expected" ]; then
        printf 'ok   %-44s (exit %s)\n' "$label" "$got"; pass=$((pass + 1))
    else
        printf 'FAIL %-44s (want %s, got %s)\n' "$label" "$expected" "$got"; fail=$((fail + 1))
    fi
}

# Build a mutated copy of good/ and print its path.
mk() { local d; d="$(mktemp -d)"; cp -a "${FIX}/good/." "$d/"; printf '%s' "$d"; }

# 1) Happy path.
assert_code 0 "good verifies" -- bash "$VR" "${FIX}/good"
# 2) Tampered binary, stale SHA256SUMS.
assert_code 4 "tampered fails closed" -- bash "$VR" "${FIX}/tampered"
# 3) Usage.
assert_code 2 "no args is usage error" -- bash "$VR"
assert_code 2 "unknown option is usage error" -- bash "$VR" "${FIX}/good" --bogus
# 4) Missing inputs.
assert_code 3 "missing dir" -- bash "$VR" "${FIX}/does-not-exist"
d="$(mk)"; rm -f "$d/SHA256SUMS";  assert_code 3 "missing SHA256SUMS" -- bash "$VR" "$d"; rm -rf "$d"
d="$(mk)"; rm -f "$d/manifest.env"; assert_code 3 "missing manifest.env" -- bash "$VR" "$d"; rm -rf "$d"
# 5) Metadata failures (fail closed, exit 4).
d="$(mk)"; grep -v '^SPA_BUNDLE_SHA256=' "$d/manifest.env" > "$d/m" && mv "$d/m" "$d/manifest.env"
assert_code 4 "missing required key" -- bash "$VR" "$d"; rm -rf "$d"
d="$(mk)"; sed -i 's/^GIT_COMMIT=.*/GIT_COMMIT=nothex/' "$d/manifest.env"
assert_code 4 "bad GIT_COMMIT format" -- bash "$VR" "$d"; rm -rf "$d"
d="$(mk)"; sed -i 's/^TARGET_TRIPLE=.*/TARGET_TRIPLE=x86_64-pc-windows-msvc/' "$d/manifest.env"
assert_code 4 "wrong target triple" -- bash "$VR" "$d"; rm -rf "$d"
d="$(mk)"; sed -i 's/^SPA_BUNDLE_SHA256=.*/SPA_BUNDLE_SHA256=0000000000000000000000000000000000000000000000000000000000000000/' "$d/manifest.env"
assert_code 4 "spa digest mismatch" -- bash "$VR" "$d"; rm -rf "$d"
# 6) SHA256SUMS structural failures.
d="$(mk)"; printf '%s\n' "ffff  ../escape" >> "$d/SHA256SUMS"
assert_code 4 "path-traversal entry rejected" -- bash "$VR" "$d"; rm -rf "$d"
d="$(mk)"; printf '%s\n' "deadbeef  /etc/passwd" >> "$d/SHA256SUMS"
assert_code 4 "absolute-path entry rejected" -- bash "$VR" "$d"; rm -rf "$d"
d="$(mk)"; : > "$d/SHA256SUMS"
assert_code 4 "empty SHA256SUMS rejected" -- bash "$VR" "$d"; rm -rf "$d"
# 6b) Completeness: extra UNLISTED installable files are rejected (contract §3.1/§6).
d="$(mk)"; printf 'rogue\n' > "$d/units/rogue.service"
assert_code 4 "unlisted units file rejected" -- bash "$VR" "$d"; rm -rf "$d"
d="$(mk)"; printf 'x\n' > "$d/bin/rogued"
assert_code 4 "unlisted bin file rejected" -- bash "$VR" "$d"; rm -rf "$d"
d="$(mk)"; printf 'x\n' > "$d/spa/assets/rogue.js"
assert_code 4 "unlisted nested spa file rejected" -- bash "$VR" "$d"; rm -rf "$d"
# Newline-in-filename must fail closed (NUL-safe enumeration must not fragment past it).
d="$(mk)"; touch "$d/units/ev"$'\n'"il.service"
assert_code 4 "unlisted newline-name file rejected" -- bash "$VR" "$d"; rm -rf "$d"
# Control: a stray file OUTSIDE the installable dirs is harmless (installer never
# globs it), so it must NOT fail verification — only installable extras are rejected.
d="$(mk)"; printf 'note\n' > "$d/EXTRA.txt"
assert_code 0 "unlisted root file ignored" -- bash "$VR" "$d"; rm -rf "$d"
# 7) Signature seam: required but absent -> fail closed.
assert_code 4 "require-signature, none present" -- bash "$VR" "${FIX}/good" --require-signature

# 8) vr_require_https helper (sourced).
# shellcheck source=/dev/null
. "$VR"
assert_code 0 "https url accepted" -- vr_require_https "https://example.com/r"
assert_code 4 "http url refused" -- vr_require_https "http://example.com/r"
assert_code 4 "empty url refused" -- vr_require_https ""

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
