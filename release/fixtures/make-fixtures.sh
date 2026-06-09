#!/usr/bin/env bash
#
# Regenerate release/fixtures/{good,tampered}/ used by the verify-release.sh
# tests and (Task 7.2) the release-pipeline tests. Deterministic: stand-in
# artifacts with fixed bytes, real coreutils hashes, and a manifest.env whose
# SPA_BUNDLE_SHA256 is computed per contract §3.3. The `tampered` tree is a copy
# of `good` with one binary's bytes changed but its SHA256SUMS left stale, so the
# verifier must fail closed.
#
# Run from anywhere: ./release/fixtures/make-fixtures.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
good="${here}/good"
tampered="${here}/tampered"

build_good() {
    rm -rf "$good"
    mkdir -p "$good/bin" "$good/spa/assets" "$good/units"

    # Stand-in service binaries (real releases ship aarch64 ELF; fixtures only
    # exercise hashing/verification, so fixed text bytes are sufficient).
    local svc
    for svc in gadgetd scannerd indexd webd uploadd retentiond wifid; do
        printf 'fake %s binary for fixture\n' "$svc" > "${good}/bin/${svc}"
    done

    # Stand-in SPA bundle.
    printf '<!doctype html><title>TeslaUSB</title>\n' > "${good}/spa/index.html"
    printf 'console.log("teslausb spa fixture");\n'    > "${good}/spa/assets/app.js"
    printf 'body{font-family:Inter}\n'                 > "${good}/spa/assets/app.css"

    # Representative units.
    printf '[Unit]\nDescription=fixture gadgetd\n' > "${good}/units/gadgetd.service"
    printf '[Unit]\nDescription=fixture webd\n'    > "${good}/units/webd.service"

    # SHA256SUMS over every shipped file (sorted, stable), relative to $good.
    ( cd "$good" && find bin spa units -type f | LC_ALL=C sort \
        | xargs sha256sum > SHA256SUMS )

    # SPA bundle digest per contract §3.3: the spa/ lines, C-sorted, hashed.
    local spa_digest
    spa_digest="$(grep -E '^[0-9a-f]{64}  spa/' "${good}/SHA256SUMS" \
        | LC_ALL=C sort | sha256sum | cut -d' ' -f1)"

    cat > "${good}/manifest.env" <<EOF
RELEASE_VERSION=0.0.0-fixture
GIT_COMMIT=0000000000000000000000000000000000000000
TARGET_TRIPLE=aarch64-unknown-linux-gnu
UNIT_SET_VERSION=1
SPA_BUNDLE_SHA256=${spa_digest}
EOF
}

build_tampered() {
    rm -rf "$tampered"
    cp -a "$good" "$tampered"
    # Change a binary's bytes but DO NOT regenerate SHA256SUMS -> integrity fail.
    printf 'TAMPERED bytes — not what the manifest hashed\n' > "${tampered}/bin/webd"
}

build_good
build_tampered
echo "fixtures regenerated under ${here}"
