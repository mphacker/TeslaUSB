#!/usr/bin/env bash
#
# TeslaUSB B-1 — release BUILDER (Task 7.2, Phase 7.0 contract §3/§7).
#
# Produces a release artifact for the 7 service daemons + SPA + units + config:
#   1. resolve the aarch64 service binaries (cross-compile via podman, or take a
#      prebuilt --bin-dir);
#   2. assert each binary is a real aarch64 ELF (wrong-arch guard — a host-arch
#      binary must NEVER enter SHA256SUMS as aarch64);
#   3. stage bin/ spa/ units/ config/ into teslausb-<version>-<triple>/;
#   4. run generate-manifest.sh (SHA256SUMS + manifest.env + manifest.json);
#   5. SELF-VERIFY the staged tree with the canonical setup-lib/verify-release.sh
#      (must exit 0) BEFORE packaging;
#   6. package a deterministic-ish gzip tarball.
#
# Honest-input policy (contract §7): every shipped input must be supplied
# explicitly. The builder NEVER fabricates a SPA bundle, unit, or config to get
# green; a missing required input is a fail-closed error. Stand-in inputs (e.g.
# the labelled release/fixtures stand-ins) may be passed deliberately for a
# pipeline smoke test, but the binaries themselves are arch-checked for real
# unless --skip-arch-check is given.
#
# Documented release host: Linux / WSL / container (contract §7). The cross
# build uses podman with a Debian image + gcc-aarch64-linux-gnu cross linker.
#
# Exit codes:
#   0  artifact built + self-verified
#   2  bad usage
#   3  missing required input/tool
#   4  build/arch/verify failure
set -euo pipefail

BR_EX_OK=0
BR_EX_USAGE=2
BR_EX_MISSING=3
BR_EX_FAIL=4

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
GENERATOR="${HERE}/generate-manifest.sh"
VERIFIER="${ROOT}/setup-lib/verify-release.sh"
DEFAULT_TRIPLE='aarch64-unknown-linux-gnu'
SERVICES='gadgetd scannerd indexd webd uploadd retentiond wifid'

br__log() { printf '[build-release] %s\n' "$*" >&2; }
br__die() { local code="$1"; shift; br__log "ERROR: $*"; exit "$code"; }

br__usage() {
    cat >&2 <<'EOF'
usage: build-release.sh --version VER (--bin-dir DIR | --cross-podman)
                        (--spa-dir DIR | --spa-project DIR)
                        --config-file FILE [options]

binary source (exactly one):
  --bin-dir DIR             use prebuilt aarch64 binaries from DIR/<service>
  --cross-podman            cross-compile the 7 daemons via podman + Debian

spa source (exactly one):
  --spa-dir DIR             stage a prebuilt SPA bundle from DIR (vite dist/)
  --spa-project DIR         run `npm ci && npm run build` in DIR, stage dist/

required:
  --version VER             RELEASE_VERSION (semver or tag)
  --config-file FILE        example config staged as config/config.example.toml

options:
  --commit SHA40            GIT_COMMIT (default: git rev-parse HEAD)
  --triple TRIPLE           default aarch64-unknown-linux-gnu
  --units-dir DIR           units source dir (*.service) (default deploy/systemd)
  --unit-set-version N      default 1
  --config-schema-version N default 1
  --out DIR                 output dir (default release/.build/dist)
  --build-host HOST         recorded in manifest.json
  --skip-arch-check         skip the aarch64 ELF assertion (stand-in inputs only)
  --keep-stage              keep the staged tree after packaging
  -h, --help                this help
EOF
}

# br__is_aarch64_elf <file> — true iff <file> is an ELF whose e_machine is
# EM_AARCH64 (183 / 0xB7). Pure coreutils (od), no `file` dependency: reads the
# 4-byte ELF magic and the 2-byte little-endian e_machine at offset 18.
br__is_aarch64_elf() {
    local f="$1" magic emachine
    [ -f "$f" ] || return 1
    magic="$(od -An -tx1 -N4 "$f" 2>/dev/null | tr -d ' ')"
    [ "$magic" = "7f454c46" ] || return 1
    emachine="$(od -An -tx1 -j18 -N2 "$f" 2>/dev/null | tr -d ' ')"
    # aarch64 ELF is little-endian: e_machine bytes are "b700".
    [ "$emachine" = "b700" ]
}

br__cross_build_podman() {
    local repo="$1" outbin="$2"
    command -v podman >/dev/null 2>&1 || br__die "$BR_EX_MISSING" "podman not found (needed for --cross-podman)"
    br__log "cross-compiling 7 daemons via podman (Debian + gcc-aarch64-linux-gnu)..."
    mkdir -p "$outbin"
    # The container copies sources off the (read-only) bind mount, builds into a
    # named cargo volume, asserts arch, and installs the binaries to /out/bin.
    local inner
    # The single-quoting is intentional: $CARGO_HOME, $b, etc. must expand
    # INSIDE the container at runtime, not on the host here.
    # shellcheck disable=SC2016
    inner='set -euo pipefail
export DEBIAN_FRONTEND=noninteractive RUSTUP_HOME=/root/.rustup CARGO_HOME=/root/.cargo
apt-get update -qq
apt-get install -y -qq build-essential pkg-config gcc-aarch64-linux-gnu binutils-aarch64-linux-gnu file curl ca-certificates >/dev/null
if [ ! -x "$CARGO_HOME/bin/rustup" ]; then
  curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain 1.85.0 >/dev/null
fi
. "$CARGO_HOME/env"
rustup target add --toolchain 1.85.0 aarch64-unknown-linux-gnu
mkdir -p /work && cp -a /src/rust /work/rust && rm -rf /work/rust/target
cd /work/rust
rustup target add aarch64-unknown-linux-gnu
export CARGO_TARGET_DIR=/cargo-target
export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc
export CC_aarch64_unknown_linux_gnu=aarch64-linux-gnu-gcc
cargo build --release --target aarch64-unknown-linux-gnu -p gadgetd -p scannerd -p indexd -p webd -p uploadd -p retentiond -p wifid
mkdir -p /out/bin
for b in gadgetd scannerd indexd webd uploadd retentiond wifid; do
  src=/cargo-target/aarch64-unknown-linux-gnu/release/$b
  aarch64-linux-gnu-readelf -h "$src" | grep -qE "Machine:.*AArch64"
  install -m0755 "$src" /out/bin/$b
done'
    podman run --rm \
        --mount "type=bind,source=${repo},target=/src,ro" \
        --mount "type=bind,source=${outbin%/bin},target=/out" \
        --mount "type=volume,source=teslausb-cargo-target,target=/cargo-target" \
        --mount "type=volume,source=teslausb-cargo-home,target=/root/.cargo" \
        --mount "type=volume,source=teslausb-rustup,target=/root/.rustup" \
        debian:bookworm bash -lc "$inner" \
        || br__die "$BR_EX_FAIL" "podman cross build failed"
}

main() {
    local version='' commit='' triple="$DEFAULT_TRIPLE"
    local bin_dir='' cross_podman=0 spa_dir='' spa_project=''
    local units_dir="${ROOT}/deploy/systemd" config_file=''
    local unit_ver='1' cfg_ver='1' out_dir="${ROOT}/release/.build/dist"
    local build_host='' skip_arch=0 keep_stage=0

    while [ "$#" -gt 0 ]; do
        case "$1" in
            --version) version="${2:?}"; shift 2 ;;
            --commit) commit="${2:?}"; shift 2 ;;
            --triple) triple="${2:?}"; shift 2 ;;
            --bin-dir) bin_dir="${2:?}"; shift 2 ;;
            --cross-podman) cross_podman=1; shift ;;
            --spa-dir) spa_dir="${2:?}"; shift 2 ;;
            --spa-project) spa_project="${2:?}"; shift 2 ;;
            --units-dir) units_dir="${2:?}"; shift 2 ;;
            --config-file) config_file="${2:?}"; shift 2 ;;
            --unit-set-version) unit_ver="${2:?}"; shift 2 ;;
            --config-schema-version) cfg_ver="${2:?}"; shift 2 ;;
            --out) out_dir="${2:?}"; shift 2 ;;
            --build-host) build_host="${2:?}"; shift 2 ;;
            --skip-arch-check) skip_arch=1; shift ;;
            --keep-stage) keep_stage=1; shift ;;
            -h|--help) br__usage; exit "$BR_EX_OK" ;;
            *) br__usage; br__die "$BR_EX_USAGE" "unknown argument: $1" ;;
        esac
    done

    [ -n "$version" ] || { br__usage; br__die "$BR_EX_USAGE" "--version is required"; }
    [ -n "$config_file" ] || { br__usage; br__die "$BR_EX_USAGE" "--config-file is required"; }
    [ -f "$config_file" ] || br__die "$BR_EX_MISSING" "config file not found: $config_file"
    if [ -n "$bin_dir" ] && [ "$cross_podman" -eq 1 ]; then
        br__die "$BR_EX_USAGE" "--bin-dir and --cross-podman are mutually exclusive"
    fi
    if [ -z "$bin_dir" ] && [ "$cross_podman" -eq 0 ]; then
        br__die "$BR_EX_USAGE" "one of --bin-dir or --cross-podman is required"
    fi
    if [ -n "$spa_dir" ] && [ -n "$spa_project" ]; then
        br__die "$BR_EX_USAGE" "--spa-dir and --spa-project are mutually exclusive"
    fi
    if [ -z "$spa_dir" ] && [ -z "$spa_project" ]; then
        br__die "$BR_EX_USAGE" "one of --spa-dir or --spa-project is required"
    fi
    [ -f "$GENERATOR" ] || br__die "$BR_EX_MISSING" "generator not found: $GENERATOR"
    [ -f "$VERIFIER" ]  || br__die "$BR_EX_MISSING" "verifier not found: $VERIFIER"

    # Reject newline/control chars early: version + triple feed the stage dir and
    # tarball names before the generator runs (defense-in-depth; the generator
    # re-validates at the trust boundary).
    case "$version" in *[![:print:]]*|'') br__die "$BR_EX_USAGE" "--version must be a single printable line" ;; esac
    case "$triple"  in *[![:print:]]*|'') br__die "$BR_EX_USAGE" "--triple must be a single printable line" ;; esac

    if [ -z "$commit" ]; then
        command -v git >/dev/null 2>&1 || br__die "$BR_EX_MISSING" "git not found and --commit not given"
        commit="$(cd "$ROOT" && git rev-parse HEAD)"
    fi

    local name="teslausb-${version}-${triple}"
    mkdir -p "$out_dir"
    out_dir="$(cd "$out_dir" && pwd)"
    local stage="${out_dir}/${name}"
    br__log "staging into ${stage}"
    rm -rf "$stage"
    mkdir -p "$stage/bin" "$stage/spa" "$stage/units" "$stage/config"

    # --- 1) binaries ----------------------------------------------------------
    if [ "$cross_podman" -eq 1 ]; then
        bin_dir="${out_dir}/${name}.binsrc/bin"
        br__cross_build_podman "$ROOT" "$bin_dir"
    fi
    local svc
    for svc in $SERVICES; do
        [ -f "${bin_dir}/${svc}" ] || br__die "$BR_EX_MISSING" "missing binary: ${bin_dir}/${svc}"
        # --- 2) wrong-arch guard ---
        if [ "$skip_arch" -eq 0 ]; then
            br__is_aarch64_elf "${bin_dir}/${svc}" \
                || br__die "$BR_EX_FAIL" "not an aarch64 ELF: ${bin_dir}/${svc} (use --skip-arch-check only for stand-ins)"
        fi
        install -m0755 "${bin_dir}/${svc}" "${stage}/bin/${svc}"
    done
    br__log "staged 7 binaries$( [ "$skip_arch" -eq 0 ] && printf ' (all verified aarch64 ELF)' )"

    # --- 3a) SPA --------------------------------------------------------------
    if [ -n "$spa_project" ]; then
        [ -d "$spa_project" ] || br__die "$BR_EX_MISSING" "spa project not found: $spa_project"
        command -v npm >/dev/null 2>&1 || br__die "$BR_EX_MISSING" "npm not found (needed for --spa-project)"
        br__log "building SPA in ${spa_project} (npm ci && npm run build)..."
        ( cd "$spa_project" && npm ci && npm run build ) || br__die "$BR_EX_FAIL" "npm build failed"
        spa_dir="${spa_project}/dist"
    fi
    [ -d "$spa_dir" ] || br__die "$BR_EX_MISSING" "spa dir not found: $spa_dir"
    [ -n "$(find "$spa_dir" -type f -print -quit)" ] || br__die "$BR_EX_MISSING" "spa dir is empty: $spa_dir"
    cp -a "${spa_dir}/." "${stage}/spa/"

    # --- 3b) units ------------------------------------------------------------
    [ -d "$units_dir" ] || br__die "$BR_EX_MISSING" "units dir not found: $units_dir"
    local nunits=0 u
    while IFS= read -r u; do
        install -m0644 "$u" "${stage}/units/$(basename "$u")"
        nunits=$((nunits + 1))
    done < <(find "$units_dir" -maxdepth 1 -type f -name '*.service' | LC_ALL=C sort)
    [ "$nunits" -gt 0 ] || br__die "$BR_EX_MISSING" "no *.service files in $units_dir"
    br__log "staged ${nunits} unit file(s) from ${units_dir}"

    # --- 3c) config -----------------------------------------------------------
    install -m0644 "$config_file" "${stage}/config/config.example.toml"

    # --- 4) generate manifest -------------------------------------------------
    bash "$GENERATOR" --dir "$stage" --version "$version" --commit "$commit" \
        --triple "$triple" --unit-set-version "$unit_ver" \
        --config-schema-version "$cfg_ver" \
        ${build_host:+--build-host "$build_host"} \
        --built-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        || br__die "$BR_EX_FAIL" "manifest generation failed"

    # --- 5) self-verify with the canonical verifier ---------------------------
    br__log "self-verifying staged tree with verify-release.sh..."
    if ! bash "$VERIFIER" "$stage"; then
        br__die "$BR_EX_FAIL" "self-verification FAILED (verify-release.sh non-zero)"
    fi
    br__log "self-verify PASSED (exit 0)"

    # --- 6) package -----------------------------------------------------------
    local tarball="${out_dir}/${name}.tar.gz"
    ( cd "$out_dir" && tar --owner=0 --group=0 --numeric-owner --sort=name \
        -czf "$tarball" "$name" ) || br__die "$BR_EX_FAIL" "tar packaging failed"
    br__log "packaged ${tarball}"
    ( cd "$out_dir" && sha256sum "$(basename "$tarball")" > "${tarball}.sha256" )

    if [ "$keep_stage" -eq 0 ]; then
        rm -rf "$stage" "${out_dir}/${name}.binsrc"
    fi

    printf '%s\n' "$tarball"
    exit "$BR_EX_OK"
}

main "$@"
