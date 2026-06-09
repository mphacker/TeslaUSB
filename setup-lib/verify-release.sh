#!/usr/bin/env bash
#
# TeslaUSB B-1 — canonical release verifier.
#
# Phase 7.0 contract (docs/tasks/phase7-0-contract.md) §6. This is the SINGLE
# source of truth for release verification, invoked by both lanes:
#   * setup.sh sources it and calls `verify_release_dir "<extracted-dir>"`.
#   * the release pipeline (Task 7.2) runs it against release/fixtures/{good,
#     tampered}/ in its tests.
#
# Properties (contract §3/§4/§6):
#   * Coreutils-only — NO jq, NO JSON parsing in bash. Pi-side trust path uses
#     `sha256sum -c` + a safe line-parse of manifest.env.
#   * Integrity by default; authenticity (signature) is an off-by-default,
#     fail-closed seam (§4).
#   * Fail-closed: any missing or malformed input is a verification failure.
#   * NEVER `source`s or `eval`s untrusted release files.
#
# Dual-use: safe to `source` (defines functions, sets nothing global) and safe to
# execute directly (the bottom guard enables strict mode and runs main).
#
# Exit / return codes:
#   0  verified
#   2  bad usage
#   3  missing input (dir/SHA256SUMS/manifest.env absent)
#   4  verification failed (hash mismatch, bad metadata, path-traversal, sig)

# --- codes -------------------------------------------------------------------
VR_EX_OK=0
VR_EX_USAGE=2
VR_EX_MISSING=3
VR_EX_VERIFY=4

# Required manifest.env keys (exact names, contract §3.2).
VR_REQUIRED_KEYS='RELEASE_VERSION GIT_COMMIT TARGET_TRIPLE UNIT_SET_VERSION CONFIG_SCHEMA_VERSION SPA_BUNDLE_SHA256'

# Installable subdirectories the artifact may carry (contract §3.1). Every file
# under these — and only these — is hashed into SHA256SUMS by the generator and
# installed from the tree by setup.sh. The completeness check (below) rejects any
# file present under one of these dirs that is NOT listed in SHA256SUMS, so an
# unlisted/unverified payload can never ride along and get installed. Must stay
# in sync with the generator's SCAN_DIRS (release/generate-manifest.sh).
VR_INSTALLABLE_DIRS='bin spa units config'

# Expected target triple (contract §3.2). Override with EXPECT_TRIPLE=... if a
# future board needs a different one.
: "${EXPECT_TRIPLE:=aarch64-unknown-linux-gnu}"

vr__log() { printf '[verify-release] %s\n' "$*" >&2; }

# vr_require_https <url> — refuse a non-HTTPS remote source (contract §4).
# Exposed so setup.sh reuses one definition for --manifest-url / --release.
vr_require_https() {
    local url="${1:-}"
    case "$url" in
        https://*) return 0 ;;
        *) vr__log "refusing non-HTTPS source: ${url:-<empty>}"; return "$VR_EX_VERIFY" ;;
    esac
}

# vr__safe_get_env <manifest.env> <KEY> — print the value for KEY using a strict
# allow-listed line parse. NEVER sources the file. Returns non-zero if the key is
# absent. Only lines matching `^KEY=value` (KEY in [A-Z][A-Z0-9_]*) are honored.
vr__safe_get_env() {
    local file="$1" want="$2" line key val
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ''|'#'*) continue ;;
        esac
        # Must look like KEY=VALUE with a conformant key.
        if [[ "$line" =~ ^([A-Z][A-Z0-9_]*)=(.*)$ ]]; then
            key="${BASH_REMATCH[1]}"
            val="${BASH_REMATCH[2]}"
            if [ "$key" = "$want" ]; then
                printf '%s' "$val"
                return 0
            fi
        fi
    done < "$file"
    return 1
}

# vr__check_sums_format <SHA256SUMS> — enforce coreutils format + path-traversal
# guard (contract §3.1) before trusting `sha256sum -c`.
vr__check_sums_format() {
    local sums="$1" line hash path n=0
    while IFS= read -r line || [ -n "$line" ]; do
        [ -z "$line" ] && continue
        if [[ ! "$line" =~ ^([0-9a-f]{64})\ \ (.+)$ ]]; then
            vr__log "malformed SHA256SUMS line: ${line}"
            return "$VR_EX_VERIFY"
        fi
        hash="${BASH_REMATCH[1]}"
        path="${BASH_REMATCH[2]}"
        : "$hash"
        # Path-traversal / absolute / escape guards.
        case "$path" in
            /*)      vr__log "absolute path in SHA256SUMS: ${path}"; return "$VR_EX_VERIFY" ;;
            *..*)    vr__log "parent-dir segment in SHA256SUMS: ${path}"; return "$VR_EX_VERIFY" ;;
            *$'\t'*) vr__log "tab in SHA256SUMS path: ${path}"; return "$VR_EX_VERIFY" ;;
        esac
        n=$((n + 1))
    done < "$sums"
    if [ "$n" -eq 0 ]; then
        vr__log "SHA256SUMS is empty"
        return "$VR_EX_VERIFY"
    fi
    return 0
}

# vr__no_listed_symlinks <dir> <SHA256SUMS> — reject symlinked members (a hash of
# a symlink target would let a swapped link bypass intent). Contract §3.1/§5.
vr__no_listed_symlinks() {
    local dir="$1" sums="$2" line path
    while IFS= read -r line || [ -n "$line" ]; do
        [ -z "$line" ] && continue
        [[ "$line" =~ ^[0-9a-f]{64}\ \ (.+)$ ]] || continue
        path="${BASH_REMATCH[1]}"
        if [ -L "${dir}/${path}" ]; then
            vr__log "listed member is a symlink: ${path}"
            return "$VR_EX_VERIFY"
        fi
    done < "$sums"
    return 0
}

# vr__no_unlisted_files <dir> <SHA256SUMS> — reject installable files present on
# disk but NOT listed in SHA256SUMS (contract §3.1/§6). sha256sum -c only checks
# the files it is GIVEN, so an extra unlisted payload (e.g. a rogue units/*.service)
# would pass integrity verification and then be installed by setup.sh, which globs
# the tree. This closes that gap by enforcing the listing is COMPLETE: every file
# under the installable dirs must appear in SHA256SUMS. Fail-closed: a find
# traversal error (e.g. an unreadable subdir), an mktemp failure, or any unlisted
# entry all return VR_EX_VERIFY. Enumeration is NUL-delimited (find -print0 /
# read -d '') so filenames containing newlines cannot fragment past the check.
# Root metadata (SHA256SUMS, manifest.*) is naturally excluded because only the
# installable subdirs are scanned.
vr__no_unlisted_files() {
    local dir="$1" sums="$2" line path d tmp rc=0
    declare -A listed=()
    while IFS= read -r line || [ -n "$line" ]; do
        [ -z "$line" ] && continue
        [[ "$line" =~ ^[0-9a-f]{64}\ \ (.+)$ ]] || continue
        listed["${BASH_REMATCH[1]}"]=1
    done < "$sums"

    local -a scan=()
    for d in $VR_INSTALLABLE_DIRS; do
        [ -d "${dir}/${d}" ] && scan+=("$d")
    done
    [ "${#scan[@]}" -eq 0 ] && return 0

    tmp="$(mktemp)" || { vr__log "mktemp failed during completeness check"; return "$VR_EX_VERIFY"; }
    # Capture find's exit status: a traversal error (unreadable subtree) must fail
    # closed, not silently hide files. The subshell's status is find's status.
    if ! ( cd "$dir" && find "${scan[@]}" \! -type d -print0 ) > "$tmp" 2>/dev/null; then
        vr__log "find failed enumerating installable dirs (fail-closed)"
        rm -f "$tmp"
        return "$VR_EX_VERIFY"
    fi
    while IFS= read -r -d '' path; do
        if [ -z "${listed[$path]:-}" ]; then
            vr__log "unlisted installable file (not in SHA256SUMS): ${path}"
            rc="$VR_EX_VERIFY"
            break
        fi
    done < "$tmp"
    rm -f "$tmp"
    return "$rc"
}

# vr__recompute_spa_digest <SHA256SUMS> — recompute SPA_BUNDLE_SHA256 from the
# verified per-file hashes (contract §3.3): the spa/ lines, C-sorted, hashed.
vr__recompute_spa_digest() {
    local sums="$1"
    grep -E '^[0-9a-f]{64}  spa/' "$sums" | LC_ALL=C sort | sha256sum | cut -d' ' -f1
}

# verify_release_dir <dir> [--require-signature] — main entry. Returns a VR_EX_*
# code; prints failures to stderr. Does not exit (safe when sourced).
verify_release_dir() {
    local dir='' require_sig=0 arg
    for arg in "$@"; do
        case "$arg" in
            --require-signature) require_sig=1 ;;
            -* ) vr__log "unknown option: $arg"; return "$VR_EX_USAGE" ;;
            * )  if [ -z "$dir" ]; then dir="$arg"; else
                     vr__log "unexpected argument: $arg"; return "$VR_EX_USAGE"; fi ;;
        esac
    done
    if [ -z "$dir" ]; then vr__log "usage: verify_release_dir <dir> [--require-signature]"; return "$VR_EX_USAGE"; fi

    local sums="${dir}/SHA256SUMS" env="${dir}/manifest.env"
    [ -d "$dir" ]  || { vr__log "no such release dir: ${dir}"; return "$VR_EX_MISSING"; }
    [ -f "$sums" ] || { vr__log "missing SHA256SUMS in ${dir}"; return "$VR_EX_MISSING"; }
    [ -f "$env" ]  || { vr__log "missing manifest.env in ${dir}"; return "$VR_EX_MISSING"; }

    # 1) Structural + path-traversal checks before trusting sha256sum -c.
    vr__check_sums_format "$sums"      || return "$VR_EX_VERIFY"
    vr__no_listed_symlinks "$dir" "$sums" || return "$VR_EX_VERIFY"

    # 2) Content integrity (coreutils, strict).
    if ! ( cd "$dir" && LC_ALL=C sha256sum -c --strict --quiet SHA256SUMS ) >/dev/null 2>&1; then
        vr__log "sha256sum -c failed (hash mismatch or missing file)"
        return "$VR_EX_VERIFY"
    fi

    # 2b) Completeness: every installable file on disk must be listed (contract
    # §3.1/§6). sha256sum -c above only proves the LISTED files are intact; this
    # rejects EXTRA unlisted payloads the globbing installer would otherwise pick up.
    vr__no_unlisted_files "$dir" "$sums" || return "$VR_EX_VERIFY"

    # 3) Required metadata present + well-formed (safe parse, no sourcing).
    local key val
    for key in $VR_REQUIRED_KEYS; do
        if ! val="$(vr__safe_get_env "$env" "$key")" || [ -z "$val" ]; then
            vr__log "manifest.env missing/empty required key: ${key}"
            return "$VR_EX_VERIFY"
        fi
    done
    local git_commit target_triple unit_ver cfg_ver spa_expect release_version
    release_version="$(vr__safe_get_env "$env" RELEASE_VERSION)"
    git_commit="$(vr__safe_get_env "$env" GIT_COMMIT)"
    target_triple="$(vr__safe_get_env "$env" TARGET_TRIPLE)"
    unit_ver="$(vr__safe_get_env "$env" UNIT_SET_VERSION)"
    cfg_ver="$(vr__safe_get_env "$env" CONFIG_SCHEMA_VERSION)"
    spa_expect="$(vr__safe_get_env "$env" SPA_BUNDLE_SHA256)"

    [[ "$git_commit" =~ ^[0-9a-f]{40}$ ]] || { vr__log "GIT_COMMIT not a 40-hex sha: ${git_commit}"; return "$VR_EX_VERIFY"; }
    [[ "$unit_ver"  =~ ^[0-9]+$ ]]        || { vr__log "UNIT_SET_VERSION not an integer: ${unit_ver}"; return "$VR_EX_VERIFY"; }
    [[ "$cfg_ver"   =~ ^[0-9]+$ ]]        || { vr__log "CONFIG_SCHEMA_VERSION not an integer: ${cfg_ver}"; return "$VR_EX_VERIFY"; }
    [[ "$spa_expect" =~ ^[0-9a-f]{64}$ ]] || { vr__log "SPA_BUNDLE_SHA256 not a 64-hex sha: ${spa_expect}"; return "$VR_EX_VERIFY"; }
    if [ "$target_triple" != "$EXPECT_TRIPLE" ]; then
        vr__log "TARGET_TRIPLE ${target_triple} != expected ${EXPECT_TRIPLE}"
        return "$VR_EX_VERIFY"
    fi

    # 4) SPA bundle digest must match the verified per-file hashes (§3.3).
    local spa_actual
    spa_actual="$(vr__recompute_spa_digest "$sums")"
    if [ "$spa_actual" != "$spa_expect" ]; then
        vr__log "SPA_BUNDLE_SHA256 mismatch (manifest ${spa_expect} vs computed ${spa_actual})"
        return "$VR_EX_VERIFY"
    fi

    # 5) Optional authenticity seam (off by default, fail-closed when on; §4).
    if [ "$require_sig" -eq 1 ]; then
        local sig="${dir}/SHA256SUMS.sig"
        [ -f "$sig" ] || { vr__log "signature required but ${sig} missing"; return "$VR_EX_VERIFY"; }
        # VR_SIG_VERIFY_CMD must be a real verifier (e.g. a key-pinned gpg/openssl
        # wrapper). It defaults to `false` so enabling --require-signature without
        # wiring a verifier fails closed rather than trusting an unchecked sig.
        if ! "${VR_SIG_VERIFY_CMD:-false}" "$sig" "$sums" "$env"; then
            vr__log "signature verification failed"
            return "$VR_EX_VERIFY"
        fi
    fi

    vr__log "verified ${dir} (version ${release_version} commit ${git_commit})"
    return "$VR_EX_OK"
}

# --- direct-execution guard --------------------------------------------------
# When run as a program (not sourced), enable strict mode and run main.
if [ "${BASH_SOURCE[0]:-}" = "${0}" ]; then
    set -euo pipefail
    verify_release_dir "$@"
    exit $?
fi
