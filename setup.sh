#!/usr/bin/env bash
# TeslaUSB B-1 idempotent installer.
#
# Thin orchestrator. Each Phase 6 increment owns one file under
# `setup-lib/`. The orchestrator sources them in numeric order and
# invokes their single public function (`b1_NN_<name>`). All shared
# helpers live in `setup-lib/00-common.sh`.
#
# Safety rails (mirrored in every step):
#   * Every step is idempotent — re-running setup.sh is a no-op.
#   * Every step honours TESLAUSB_DRY_RUN=1; nothing is mutated unless
#     the variable is empty or zero.
#   * Every file we overwrite outside our own tree gets a
#     `.b1-backup-<ISO-timestamp>` sibling FIRST.
#   * Every step prints `[step NN] start` and `[step NN] done|skipped`.
#   * Any non-zero exit aborts the run (set -Eeuo pipefail).
#
# CLI:
#   setup.sh                  install (mutate)
#   setup.sh --dry-run        show every command, mutate nothing
#   setup.sh --only NN[,NN]   run only the listed step numbers
#   setup.sh --skip NN[,NN]   skip the listed step numbers
#   setup.sh --help           usage
#
# Exit codes:
#   0  success (or dry-run completed)
#   2  bad CLI flags
#   3  missing dependency or precondition (e.g. not root, no apt)
#   4  step failed mid-way
#
# This script is part of Phase 6 (see docs/00-PLAN.md). The companion
# uninstall.sh reverses every mutation back to the captured baselines.

set -Eeuo pipefail

# --------------------------------------------------------------------
# Paths + bootstrap
# --------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="${SCRIPT_DIR}/setup-lib"

if [[ ! -d "${LIB_DIR}" ]]; then
  echo "FATAL: setup-lib/ missing next to setup.sh (${LIB_DIR})" >&2
  exit 3
fi

# 00-common MUST be sourced first; it defines b1_log / b1_run / b1_backup etc.
# shellcheck source=setup-lib/00-common.sh
source "${LIB_DIR}/00-common.sh"

# --------------------------------------------------------------------
# CLI parsing
# --------------------------------------------------------------------

usage() {
  cat <<'USAGE'
TeslaUSB B-1 installer.

Usage:
  setup.sh [--dry-run] [--only NN[,NN...]] [--skip NN[,NN...]]
  setup.sh --help

Options:
  --dry-run            Print every command, mutate nothing.
  --only NN[,NN...]    Run only the listed step numbers (e.g. --only 01,02).
  --skip NN[,NN...]    Skip the listed step numbers.
  --help               Show this message.

Steps are sourced from setup-lib/<NN>-<name>.sh in numeric order.
Each step is independently idempotent; re-running setup.sh on an
already-installed device must be a no-op.
USAGE
}

ONLY_LIST=""
SKIP_LIST=""

while (( $# > 0 )); do
  case "$1" in
    --dry-run)
      export TESLAUSB_DRY_RUN=1
      ;;
    --only)
      ONLY_LIST="${2:-}"
      [[ -z "${ONLY_LIST}" ]] && { echo "--only requires a value" >&2; exit 2; }
      shift
      ;;
    --skip)
      SKIP_LIST="${2:-}"
      [[ -z "${SKIP_LIST}" ]] && { echo "--skip requires a value" >&2; exit 2; }
      shift
      ;;
    --help|-h)
      usage; exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

# --------------------------------------------------------------------
# Precondition: root or dry-run
# --------------------------------------------------------------------

if [[ "${TESLAUSB_DRY_RUN:-0}" != "1" && "$(id -u)" -ne 0 ]]; then
  echo "FATAL: setup.sh must be run as root (or with --dry-run)." >&2
  exit 3
fi

# --------------------------------------------------------------------
# Discover + dispatch steps
# --------------------------------------------------------------------

# Each file in setup-lib/ named NN-<name>.sh defines exactly one
# function: b1_step_NN(). It MUST be idempotent and MUST honour
# TESLAUSB_DRY_RUN.
shopt -s nullglob
mapfile -t STEP_FILES < <(printf '%s\n' "${LIB_DIR}"/[0-9][0-9]-*.sh | LC_ALL=C sort)
shopt -u nullglob

if (( ${#STEP_FILES[@]} == 0 )); then
  b1_log "FATAL: no step files found in ${LIB_DIR}"
  exit 3
fi

# Parse --only / --skip into associative sets keyed by 2-digit number.
declare -A ONLY_SET SKIP_SET
if [[ -n "${ONLY_LIST}" ]]; then
  IFS=',' read -ra _only <<< "${ONLY_LIST}"
  for n in "${_only[@]}"; do ONLY_SET["$(printf '%02d' "${n}")"]=1; done
fi
if [[ -n "${SKIP_LIST}" ]]; then
  IFS=',' read -ra _skip <<< "${SKIP_LIST}"
  for n in "${_skip[@]}"; do SKIP_SET["$(printf '%02d' "${n}")"]=1; done
fi

b1_log "setup.sh starting (dry_run=${TESLAUSB_DRY_RUN:-0}, steps=${#STEP_FILES[@]})"

for step_file in "${STEP_FILES[@]}"; do
  base="$(basename "${step_file}")"
  num="${base:0:2}"   # leading 2 digits

  # 00-common is sourced separately; skip it here.
  [[ "${num}" == "00" ]] && continue

  if [[ -n "${ONLY_LIST}" && -z "${ONLY_SET[${num}]:-}" ]]; then
    b1_log "[step ${num}] skipped (--only)"
    continue
  fi
  if [[ -n "${SKIP_SET[${num}]:-}" ]]; then
    b1_log "[step ${num}] skipped (--skip)"
    continue
  fi

  # shellcheck source=/dev/null
  source "${step_file}"
  fn="b1_step_${num}"
  if ! declare -F "${fn}" >/dev/null; then
    b1_log "FATAL: ${base} did not declare ${fn}()"
    exit 4
  fi

  b1_log "[step ${num}] start (${base})"
  if "${fn}"; then
    b1_log "[step ${num}] done"
  else
    rc=$?
    b1_log "[step ${num}] FAILED rc=${rc}"
    exit 4
  fi
done

b1_log "setup.sh completed successfully"
