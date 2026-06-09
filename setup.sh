#!/usr/bin/env bash
#
# TeslaUSB B-1 — setup.sh (Task 7.1).
#
# The SINGLE install mechanism for the B-1 stack. Thin orchestrator: it parses
# flags, sources the numbered helper libs, and dispatches to one convergent,
# dry-run-aware mode. It NEVER creates/grows/partitions/formats/moves/deletes the
# car-facing backing image — that authority belongs solely to the gadgetd binary,
# reached only via gadgetd-provision.service behind --bootstrap-image (contract
# §2). All filesystem/systemctl mutations route through the single run_mutation
# wrapper in setup-lib/common.sh.
#
# Modes:    install [--bootstrap-image] | deploy-app | update | repair | rollback
# Exit:     0 ok/dry-run · 2 bad flags · 3 missing precondition · 4 step failed
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETUP_LIB_DIR="${SCRIPT_DIR}/setup-lib"
export SETUP_LIB_DIR

# shellcheck source=setup-lib/common.sh
. "${SETUP_LIB_DIR}/common.sh"
# shellcheck source=setup-lib/artifact.sh
. "${SETUP_LIB_DIR}/artifact.sh"
# shellcheck source=setup-lib/units.sh
. "${SETUP_LIB_DIR}/units.sh"
# shellcheck source=setup-lib/modes.sh
. "${SETUP_LIB_DIR}/modes.sh"

# Flag defaults (set before parsing so `set -u` is satisfied everywhere).
ARTIFACT_DIR="${ARTIFACT_DIR:-}"
RELEASE_TAG="${RELEASE_TAG:-}"
MANIFEST_URL="${MANIFEST_URL:-}"
BOOTSTRAP_IMAGE="${BOOTSTRAP_IMAGE:-0}"
ASSUME_YES="${ASSUME_YES:-0}"
ALLOW_UNVERIFIED="${ALLOW_UNVERIFIED:-0}"
REQUIRE_SIGNATURE="${REQUIRE_SIGNATURE:-0}"
ONLY_STEPS=""
SKIP_STEPS=""

usage() {
    cat >&2 <<'EOF'
usage: setup.sh <mode> [flags]

Modes:
  install         Full fresh-install. Destructive image bootstrap only with
                  --bootstrap-image (delegated to gadgetd).
  deploy-app      Non-destructive: binaries + SPA + units + config only.
                  Never touches disk.img/boot/partitions. (migration M4)
  update          Data-preserving converge to a release. Preserves disk.img,
                  config, secrets, archive, index.
  repair          Re-assert perms / unit enablement; no data change.
  rollback        Restore the previous release's .b1-backup sidecars.

Flags:
  --artifact-dir DIR   Trusted local release dir (default trusted source).
  --release TAG        Fetch a GitHub release (HTTPS).
  --manifest-url URL   Fetch a release by manifest URL (HTTPS).
  --bootstrap-image    Enable first-run image provisioning (install only).
  --allow-unverified   Skip integrity verification (DANGEROUS; needs --yes).
  --require-signature  Additionally require a valid SHA256SUMS.sig.
  --dry-run            Print every action and mutate nothing.
  --yes                Assume yes for dangerous confirmations.
  --only NN / --skip NN  (reserved; coarse modes do not support step filtering)
  -h, --help           This help.
EOF
}

main() {
    local mode=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            install|deploy-app|update|repair|rollback)
                [ -z "$mode" ] || die "$EX_USAGE" "multiple modes given: ${mode} and $1"
                mode="$1"; shift ;;
            --dry-run)           DRY_RUN=1; shift ;;
            --yes)               ASSUME_YES=1; shift ;;
            --bootstrap-image)   BOOTSTRAP_IMAGE=1; shift ;;
            --allow-unverified)  ALLOW_UNVERIFIED=1; shift ;;
            --require-signature) REQUIRE_SIGNATURE=1; shift ;;
            --artifact-dir)      [ "$#" -ge 2 ] || die "$EX_USAGE" "--artifact-dir requires a value"; ARTIFACT_DIR="$2"; shift 2 ;;
            --release)           [ "$#" -ge 2 ] || die "$EX_USAGE" "--release requires a value"; RELEASE_TAG="$2"; shift 2 ;;
            --manifest-url)      [ "$#" -ge 2 ] || die "$EX_USAGE" "--manifest-url requires a value"; MANIFEST_URL="$2"; shift 2 ;;
            --only)              [ "$#" -ge 2 ] || die "$EX_USAGE" "--only requires a value"; ONLY_STEPS="$2"; shift 2 ;;
            --skip)              [ "$#" -ge 2 ] || die "$EX_USAGE" "--skip requires a value"; SKIP_STEPS="$2"; shift 2 ;;
            -h|--help)           usage; exit "$EX_OK" ;;
            --) shift; break ;;
            -*) die "$EX_USAGE" "unknown flag: $1" ;;
            *)  die "$EX_USAGE" "unexpected argument: $1" ;;
        esac
    done

    if [ -z "$mode" ]; then
        usage
        exit "$EX_USAGE"
    fi
    if [ -n "$ONLY_STEPS" ] || [ -n "$SKIP_STEPS" ]; then
        log_warn "--only/--skip are reserved and ignored by the current coarse-grained modes"
    fi

    [ "${DRY_RUN}" = "1" ] && log_info "DRY-RUN: no filesystem or systemctl changes will be made"

    case "$mode" in
        install)    mode_install ;;
        deploy-app) mode_deploy_app ;;
        update)     mode_update ;;
        repair)     mode_repair ;;
        rollback)   mode_rollback ;;
    esac
}

main "$@"
