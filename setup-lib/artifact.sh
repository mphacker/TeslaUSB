#!/usr/bin/env bash
#
# TeslaUSB B-1 installer — artifact acquisition, extraction safety, and trust
# (Task 7.1, contract §3/§4/§5/§6).
#
# This lib CONSUMES the canonical verifier setup-lib/verify-release.sh (it never
# reimplements hashing). verify-release.sh is sourced from the in-repo, trusted
# location — NEVER from a downloaded artifact — which closes the "what verifies
# the verifier" gap (contract §4/§6).
#
# SETUP_LIB_DIR must be set by the caller (setup.sh) to this directory.

# Source the canonical verifier once (defines verify_release_dir + vr_*).
# shellcheck source=setup-lib/verify-release.sh
. "${SETUP_LIB_DIR}/verify-release.sh"

# artifact_require_https <url> — refuse a non-HTTPS remote source (contract §4).
# Delegates to the verifier's single definition.
artifact_require_https() {
    vr_require_https "$1" || die "$EX_USAGE" "remote artifact source must be HTTPS: ${1:-<empty>}"
}

# extract_tarball_safe <tarball> <destdir> — contract §5 extraction safety.
# Refuses absolute paths, parent-dir segments, symlinks, and setuid/setgid bits.
# Extracts into a fresh dir the installer owns; never over live paths.
extract_tarball_safe() {
    local tarball="$1" dest="$2" entry mode
    [ -f "$tarball" ] || die "$EX_PRECOND" "release tarball missing: ${tarball}"
    # Pre-scan the member NAMES before extracting anything (contract §5).
    while IFS= read -r entry; do
        case "$entry" in
            /*)     die "$EX_STEP" "refusing absolute path in tarball: ${entry}" ;;
            *..*)   die "$EX_STEP" "refusing parent-dir segment in tarball: ${entry}" ;;
        esac
    done < <(tar -tzf "$tarball")
    # Pre-scan member TYPES before extraction: reject symlink ('l') and hardlink
    # ('h') members up front, so a hostile archive cannot create a link and then
    # write THROUGH it to escape ${dest} mid-extraction (the post-extract find
    # below is belt-and-braces). GNU tar -tv encodes the type in column-1 of the
    # mode string.
    while IFS= read -r mode _; do
        case "$mode" in
            l*) die "$EX_STEP" "refusing symlink member in tarball" ;;
            h*) die "$EX_STEP" "refusing hardlink member in tarball" ;;
        esac
    done < <(tar -tvzf "$tarball" 2>/dev/null)
    mkdir -p "$dest"
    tar -xzf "$tarball" -C "$dest" --no-same-owner
    # Post-extract structural guards.
    if [ -n "$(find "$dest" -type l -print -quit)" ]; then
        die "$EX_STEP" "refusing extracted symlink under ${dest}"
    fi
    if [ -n "$(find "$dest" -perm /6000 -print -quit)" ]; then
        die "$EX_STEP" "refusing setuid/setgid file under ${dest}"
    fi
}

# artifact_fetch_remote <destdir> — download the configured remote release
# (MANIFEST_URL or RELEASE_TAG) into destdir and return the extracted release
# dir path on stdout. HTTPS enforced. Not exercised by host tests (network).
artifact_fetch_remote() {
    local dest="$1" url="${MANIFEST_URL:-}" tarball
    if [ -z "$url" ] && [ -n "${RELEASE_TAG:-}" ]; then
        url="https://github.com/mphacker/TeslaUSB/releases/download/${RELEASE_TAG}/teslausb-${RELEASE_TAG}.tar.gz"
    fi
    [ -n "$url" ] || die "$EX_USAGE" "no remote artifact URL configured"
    artifact_require_https "$url"
    tarball="${dest}/release.tar.gz"
    mkdir -p "$dest"
    log_info "fetching release tarball: ${url}"
    curl --fail --location --proto '=https' --tlsv1.2 -o "$tarball" "$url" \
        || die "$EX_STEP" "download failed: ${url}"
    extract_tarball_safe "$tarball" "${dest}/unpacked"
    # The tarball extracts to a single teslausb-<ver>-<triple>/ dir (contract §3).
    local inner
    inner="$(find "${dest}/unpacked" -mindepth 1 -maxdepth 1 -type d -print -quit)"
    [ -n "$inner" ] || die "$EX_STEP" "release tarball has no top-level dir"
    printf '%s' "$inner"
}

# artifact_source_dir — resolve flags to a local release dir ready for verify.
#   --artifact-dir DIR : trusted local path, used as-is (default trusted path).
#   --release / --manifest-url : fetched + extracted into an owned temp dir.
# Echoes the release dir path on stdout.
artifact_source_dir() {
    if [ -n "${ARTIFACT_DIR:-}" ]; then
        [ -d "$ARTIFACT_DIR" ] || die "$EX_PRECOND" "--artifact-dir is not a directory: ${ARTIFACT_DIR}"
        printf '%s' "$ARTIFACT_DIR"
        return 0
    fi
    if [ -n "${MANIFEST_URL:-}" ] || [ -n "${RELEASE_TAG:-}" ]; then
        local tmp
        tmp="$(mktemp -d "${TMPDIR:-/tmp}/teslausb-rel.XXXXXX")"
        artifact_fetch_remote "$tmp"
        return 0
    fi
    die "$EX_USAGE" "no artifact source given: use --artifact-dir, --release, or --manifest-url"
}

# artifact_verify_dir <dir> — gate installation on integrity. Honors
# --allow-unverified (loud warning + mandatory --yes) and --require-signature.
artifact_verify_dir() {
    local dir="$1" rc=0
    if [ "${ALLOW_UNVERIFIED:-0}" = "1" ]; then
        log_warn "##########################################################"
        log_warn "# ARTIFACT VERIFICATION DISABLED (--allow-unverified).    #"
        log_warn "# Installing UNVERIFIED, possibly tampered binaries onto  #"
        log_warn "# the car-facing device. This is dangerous.               #"
        log_warn "##########################################################"
        if [ "${ASSUME_YES:-0}" != "1" ]; then
            die "$EX_USAGE" "--allow-unverified requires explicit --yes"
        fi
        log_warn "proceeding UNVERIFIED for ${dir} (--yes given)"
        return 0
    fi
    local args=( "$dir" )
    [ "${REQUIRE_SIGNATURE:-0}" = "1" ] && args+=( --require-signature )
    verify_release_dir "${args[@]}" || rc=$?
    if [ "$rc" -ne 0 ]; then
        die "$EX_STEP" "artifact verification failed (verify-release.sh exit ${rc}); refusing to install. Override with --allow-unverified --yes (dangerous)."
    fi
}

# artifact_resolve_and_verify — resolve the source dir and verify it. Sets the
# global RESOLVED_RELEASE_DIR. NOTE: artifact_source_dir is captured in a
# subshell, so its die() only exits that subshell; `|| exit $?` re-propagates the
# code to the parent. artifact_verify_dir runs in the parent so its die() exits
# the whole installer as intended.
artifact_resolve_and_verify() {
    RESOLVED_RELEASE_DIR="$(artifact_source_dir)" || exit $?
    [ -n "$RESOLVED_RELEASE_DIR" ] || die "$EX_STEP" "could not resolve a release directory"
    artifact_verify_dir "$RESOLVED_RELEASE_DIR"
}
