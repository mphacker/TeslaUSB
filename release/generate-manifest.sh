#!/usr/bin/env bash
#
# TeslaUSB B-1 — release manifest GENERATOR (Task 7.2, Phase 7.0 contract §3).
#
# Given a STAGED release tree (bin/ spa/ units/ config/), emit the three
# metadata files the contract freezes:
#   * SHA256SUMS    — coreutils "<64-hex>␠␠<relpath>" per shipped file (§3.1).
#   * manifest.env  — flat KEY=value with the exact §3.2 keys, in order.
#   * manifest.json — rich host-only metadata, schema-valid (§3, release/
#                     manifest.schema.json). NOT trusted on the Pi.
#
# The output is designed to be accepted by setup-lib/verify-release.sh (the
# single canonical verifier) and to fail closed under any mutation. This script
# OWNS generation only; the builder (build-release.sh) calls the verifier.
#
# Coreutils-native: only sha256sum/find/sort/grep/sed for the trusted outputs.
# python3 is used ONLY to emit valid manifest.json (host-side tooling, never on
# the Pi trust path) and is optional — without it, manifest.json is skipped with
# a warning unless --require-json is given.
#
# Exit codes:
#   0  manifest generated
#   2  bad usage
#   3  missing/empty required staged input
#   4  invalid metadata (would not pass the verifier)
set -euo pipefail

GM_EX_OK=0
GM_EX_USAGE=2
GM_EX_MISSING=3
GM_EX_INVALID=4

DEFAULT_TRIPLE='aarch64-unknown-linux-gnu'
# Subdirs hashed into SHA256SUMS, in a stable order (§3.1). Root-level
# metadata (SHA256SUMS, manifest.*) is excluded by only scanning these.
SCAN_DIRS='bin spa units config'
# The 7 service binaries that a complete release ships (schema enum, §3).
EXPECTED_BINS='gadgetd scannerd indexd webd uploadd retentiond wifid'

gm__log() { printf '[generate-manifest] %s\n' "$*" >&2; }
gm__die() { local code="$1"; shift; gm__log "$*"; exit "$code"; }

gm__usage() {
    cat >&2 <<'EOF'
usage: generate-manifest.sh --dir DIR --version VER --commit SHA40 [options]

required:
  --dir DIR                 staged release tree (contains bin/ spa/ units/ config/)
  --version VER             RELEASE_VERSION (semver or tag; single line)
  --commit SHA40            GIT_COMMIT (full 40-hex sha)

options:
  --triple TRIPLE           TARGET_TRIPLE (default: aarch64-unknown-linux-gnu)
  --unit-set-version N      UNIT_SET_VERSION integer (default: 1)
  --config-schema-version N CONFIG_SCHEMA_VERSION integer (default: 1)
  --build-host HOST         optional manifest.json build_host
  --built-at ISO            optional manifest.json built_at (ISO-8601)
  --require-json            fail (exit 3) if python3 is unavailable for manifest.json
  --allow-missing-inputs    permit absent units/ or config/ (binaries+spa still required)
  -h, --help                this help
EOF
}

main() {
    local dir='' version='' commit='' triple="$DEFAULT_TRIPLE"
    local unit_ver='1' cfg_ver='1' build_host='' built_at=''
    local require_json=0 allow_missing=0

    while [ "$#" -gt 0 ]; do
        case "$1" in
            --dir) dir="${2:?--dir needs a value}"; shift 2 ;;
            --version) version="${2:?--version needs a value}"; shift 2 ;;
            --commit) commit="${2:?--commit needs a value}"; shift 2 ;;
            --triple) triple="${2:?--triple needs a value}"; shift 2 ;;
            --unit-set-version) unit_ver="${2:?}"; shift 2 ;;
            --config-schema-version) cfg_ver="${2:?}"; shift 2 ;;
            --build-host) build_host="${2:?}"; shift 2 ;;
            --built-at) built_at="${2:?}"; shift 2 ;;
            --require-json) require_json=1; shift ;;
            --allow-missing-inputs) allow_missing=1; shift ;;
            -h|--help) gm__usage; exit "$GM_EX_OK" ;;
            *) gm__usage; gm__die "$GM_EX_USAGE" "unknown argument: $1" ;;
        esac
    done

    [ -n "$dir" ]     || { gm__usage; gm__die "$GM_EX_USAGE" "--dir is required"; }
    [ -n "$version" ] || { gm__usage; gm__die "$GM_EX_USAGE" "--version is required"; }
    [ -n "$commit" ]  || { gm__usage; gm__die "$GM_EX_USAGE" "--commit is required"; }
    [ -d "$dir" ]     || gm__die "$GM_EX_MISSING" "no such staged dir: $dir"

    # --- metadata validation (fail before writing anything the verifier rejects)
    case "$version" in
        *[![:print:]]*|'') gm__die "$GM_EX_INVALID" "RELEASE_VERSION must be a single printable line" ;;
    esac
    case "$triple" in
        *[![:print:]]*|'') gm__die "$GM_EX_INVALID" "TARGET_TRIPLE must be a single printable line" ;;
    esac
    [[ "$commit"   =~ ^[0-9a-f]{40}$ ]] || gm__die "$GM_EX_INVALID" "GIT_COMMIT must be 40 lowercase hex: $commit"
    [[ "$unit_ver" =~ ^[0-9]+$ ]]       || gm__die "$GM_EX_INVALID" "UNIT_SET_VERSION must be an integer: $unit_ver"
    [[ "$cfg_ver"  =~ ^[0-9]+$ ]]       || gm__die "$GM_EX_INVALID" "CONFIG_SCHEMA_VERSION must be an integer: $cfg_ver"
    if [ "$triple" != "$DEFAULT_TRIPLE" ]; then
        gm__log "WARNING: TARGET_TRIPLE '$triple' != frozen '$DEFAULT_TRIPLE'; the verifier will reject it unless EXPECT_TRIPLE is overridden"
    fi

    # --- required staged inputs ------------------------------------------------
    local d present=()
    for d in $SCAN_DIRS; do
        if [ -d "$dir/$d" ] && [ -n "$(find "$dir/$d" -type f -print -quit 2>/dev/null)" ]; then
            present+=("$d")
        else
            case "$d" in
                bin|spa) gm__die "$GM_EX_MISSING" "required staged dir missing or empty: $d/ (in $dir)" ;;
                *)
                    if [ "$allow_missing" -eq 1 ]; then
                        gm__log "WARNING: optional staged dir missing or empty: $d/ (allowed via --allow-missing-inputs)"
                    else
                        gm__die "$GM_EX_MISSING" "staged dir missing or empty: $d/ (in $dir; pass --allow-missing-inputs to permit)"
                    fi
                    ;;
            esac
        fi
    done

    # --- SHA256SUMS (coreutils, NUL-safe, C-sorted, two-space format) ----------
    local sums="$dir/SHA256SUMS"
    (
        cd "$dir"
        # shellcheck disable=SC2086  # word-split present[] into find args intentionally
        find "${present[@]}" -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum
    ) > "$sums"
    [ -s "$sums" ] || gm__die "$GM_EX_MISSING" "no files hashed into SHA256SUMS"

    # --- SPA bundle digest (§3.3): C-sorted spa/ lines of SHA256SUMS, hashed ----
    local spa_digest
    spa_digest="$(grep -E '^[0-9a-f]{64}  spa/' "$sums" | LC_ALL=C sort | sha256sum | cut -d' ' -f1)"
    [[ "$spa_digest" =~ ^[0-9a-f]{64}$ ]] || gm__die "$GM_EX_INVALID" "could not compute SPA_BUNDLE_SHA256"

    # --- manifest.env (exact §3.2 keys, in order) ------------------------------
    cat > "$dir/manifest.env" <<EOF
RELEASE_VERSION=${version}
GIT_COMMIT=${commit}
TARGET_TRIPLE=${triple}
UNIT_SET_VERSION=${unit_ver}
CONFIG_SCHEMA_VERSION=${cfg_ver}
SPA_BUNDLE_SHA256=${spa_digest}
EOF

    # --- manifest.json (host-only; python3 for guaranteed-valid JSON) ----------
    if command -v python3 >/dev/null 2>&1; then
        gm__emit_json "$dir" "$sums" "$version" "$commit" "$triple" \
            "$unit_ver" "$cfg_ver" "$spa_digest" "$build_host" "$built_at"
    elif [ "$require_json" -eq 1 ]; then
        gm__die "$GM_EX_MISSING" "python3 unavailable but --require-json was given"
    else
        gm__log "WARNING: python3 unavailable; skipped manifest.json (host-only metadata)"
    fi

    gm__log "generated SHA256SUMS + manifest.env$( [ -f "$dir/manifest.json" ] && printf ' + manifest.json' ) in $dir"
    exit "$GM_EX_OK"
}

# gm__emit_json <dir> <sums> <version> <commit> <triple> <unit_ver> <cfg_ver>
#               <spa_digest> <build_host> <built_at>
# Build manifest.json from the verified bin/ hashes. python only assembles JSON.
gm__emit_json() {
    GM_DIR="$1" GM_SUMS="$2" GM_VERSION="$3" GM_COMMIT="$4" GM_TRIPLE="$5" \
    GM_UNIT_VER="$6" GM_CFG_VER="$7" GM_SPA="$8" GM_BUILD_HOST="$9" GM_BUILT_AT="${10}" \
    GM_EXPECTED_BINS="$EXPECTED_BINS" \
    python3 - <<'PY'
import json, os, re, sys

sums = os.environ["GM_SUMS"]
expected = set(os.environ["GM_EXPECTED_BINS"].split())
line_re = re.compile(r"^([0-9a-f]{64})  (.+)$")

binaries = []
with open(sums, "r", encoding="utf-8") as fh:
    for raw in fh:
        line = raw.rstrip("\n")
        if not line:
            continue
        m = line_re.match(line)
        if not m:
            continue
        digest, path = m.group(1), m.group(2)
        if path.startswith("bin/") and path.count("/") == 1:
            name = path[len("bin/"):]
            binaries.append({"name": name, "path": path, "sha256": digest})

binaries.sort(key=lambda b: b["name"])
unknown = [b["name"] for b in binaries if b["name"] not in expected]
if unknown:
    sys.stderr.write("[generate-manifest] WARNING: bin/ has non-service entries: %s\n" % ", ".join(unknown))
if not binaries:
    sys.stderr.write("[generate-manifest] ERROR: no bin/ entries for manifest.json\n")
    sys.exit(4)

manifest = {
    "release_version": os.environ["GM_VERSION"],
    "git_commit": os.environ["GM_COMMIT"],
    "target_triple": os.environ["GM_TRIPLE"],
    "unit_set_version": int(os.environ["GM_UNIT_VER"]),
    "config_schema_version": int(os.environ["GM_CFG_VER"]),
    "spa_bundle_sha256": os.environ["GM_SPA"],
    "binaries": binaries,
}
build_host = os.environ.get("GM_BUILD_HOST", "")
built_at = os.environ.get("GM_BUILT_AT", "")
if built_at:
    manifest["built_at"] = built_at
if build_host:
    manifest["build_host"] = build_host

out = os.path.join(os.environ["GM_DIR"], "manifest.json")
with open(out, "w", encoding="utf-8") as fh:
    json.dump(manifest, fh, indent=2, sort_keys=True)
    fh.write("\n")
PY
}

main "$@"
