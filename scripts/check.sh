#!/usr/bin/env bash
# scripts/check.sh — TeslaUSB B-1 local gate runner.
#
# Single source of truth for "all charter gates pass before commit."
# Mirrors `docs/03-CODE-QUALITY-CHARTER.md` §"CI Gates" verbatim;
# when the charter changes, this script changes in lockstep.
#
# ─── SCOPE: cloud / PC-runnable checks only ──────────────────────────
# Static analysis + unit tests. Anything that touches real hardware —
# USB gadget binding, NBD against a Pi loopback, FAT32 / exFAT against
# real block devices, the hardware watchdog, SDIO bus contention, Pi
# Zero 2 W performance, or a vehicle in Sentry mode — is NOT testable
# here. Those run on the Pi via the H-phases (H0, H1, …) under the
# `hardware-test` skill with dead-man switch armed.
#
# Phase 1+ integration tests that need hardware MUST be marked
# `#[ignore]` (Rust) or `@pytest.mark.skipif(…)` (Python) so this
# script keeps building + running only the cloud-safe subset.
# ──────────────────────────────────────────────────────────────────────
#
# USAGE:
#   scripts/check.sh                # run every gate, fail fast
#   scripts/check.sh --all          # run every gate, report at end
#   scripts/check.sh --rust         # only Rust gates
#   scripts/check.sh --python       # only Python gates
#   scripts/check.sh --hygiene      # only hygiene + markdown checks
#   scripts/check.sh -h | --help    # print usage
#
# EXIT CODES:
#   0   all selected gates passed (or were cleanly skipped)
#   1   one or more selected gates failed
#   2   usage error
#
# DEPENDENCIES:
#   Required (script aborts if missing): cargo, rustup, python3, pip
#   Optional (gate skipped with WARN if missing):
#       cargo-deny, cargo-machete, cargo-llvm-cov, lychee
#   Install everything with `scripts/setup-dev.sh` (Phase 0.6).

set -uo pipefail

# Resolve repo root regardless of cwd.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ─── Output helpers ──────────────────────────────────────────────────
if [[ -t 1 ]] && command -v tput >/dev/null 2>&1; then
    C_RED=$(tput setaf 1); C_GRN=$(tput setaf 2); C_YEL=$(tput setaf 3)
    C_BLU=$(tput setaf 4); C_BLD=$(tput bold);    C_RST=$(tput sgr0)
else
    C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_BLD=""; C_RST=""
fi

PASSED=(); FAILED=(); SKIPPED=()
NUM_RUN=0

log_gate_start() {
    NUM_RUN=$((NUM_RUN + 1))
    printf "\n%s===== [%d] %s =====%s\n" "$C_BLU$C_BLD" "$NUM_RUN" "$1" "$C_RST"
}

log_pass() { printf "%s[PASS]%s  %s\n" "$C_GRN" "$C_RST" "$1"; PASSED+=("$1"); }
log_fail() { printf "%s[FAIL]%s  %s\n" "$C_RED" "$C_RST" "$1"; FAILED+=("$1"); }
log_skip() { printf "%s[SKIP]%s  %s (%s)\n" "$C_YEL" "$C_RST" "$1" "$2"; SKIPPED+=("$1 ($2)"); }

# run_gate <name> <command...>
#   Runs the gate, captures pass/fail, honours $FAIL_FAST.
run_gate() {
    local name="$1"; shift
    log_gate_start "$name"
    if "$@"; then
        log_pass "$name"
    else
        log_fail "$name"
        if [[ "${FAIL_FAST:-1}" == "1" ]]; then
            print_summary
            exit 1
        fi
    fi
}

# skip_gate <name> <reason>
skip_gate() { log_gate_start "$1"; log_skip "$1" "$2"; }

# require_cmd <cmd> — abort with exit 2 if missing
require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        printf "%s[ERROR]%s required tool not on PATH: %s\n" \
            "$C_RED$C_BLD" "$C_RST" "$1" >&2
        printf "    install everything with scripts/setup-dev.sh (Phase 0.6)\n" >&2
        exit 2
    fi
}

# has_cargo_subcmd <subcommand> — true iff `cargo <subcommand>` resolves
has_cargo_subcmd() { cargo "$1" --version >/dev/null 2>&1; }

print_summary() {
    printf "\n%s================== Summary ==================%s\n" "$C_BLD" "$C_RST"
    printf "  %sPassed:%s  %d\n" "$C_GRN" "$C_RST" "${#PASSED[@]}"
    printf "  %sFailed:%s  %d\n" "$C_RED" "$C_RST" "${#FAILED[@]}"
    printf "  %sSkipped:%s %d\n" "$C_YEL" "$C_RST" "${#SKIPPED[@]}"
    if [[ ${#SKIPPED[@]} -gt 0 ]]; then
        printf "\n  Skipped gates:\n"
        for s in "${SKIPPED[@]}"; do printf "    - %s\n" "$s"; done
    fi
    if [[ ${#FAILED[@]} -gt 0 ]]; then
        printf "\n  %sFailed gates:%s\n" "$C_RED$C_BLD" "$C_RST"
        for f in "${FAILED[@]}"; do printf "    - %s\n" "$f"; done
    fi
}

# ─── Gate definitions ────────────────────────────────────────────────

run_rust_gates() {
    require_cmd cargo
    require_cmd rustup
    cd "$REPO_ROOT/rust" || exit 1

    # Ensures pinned toolchain (from rust-toolchain.toml) is installed.
    rustup show >/dev/null

    run_gate "rust: cargo fmt --check" \
        cargo fmt --all -- --check
    run_gate "rust: cargo clippy -D warnings" \
        cargo clippy --workspace --all-targets --all-features -- -D warnings
    run_gate "rust: cargo test" \
        cargo test --workspace --all-targets --all-features

    # cargo-llvm-cov: covers Phase 1+ paths that don't exist yet; no-match
    # globs make llvm-cov report 100% trivially during Phase 0.
    if has_cargo_subcmd "llvm-cov"; then
        run_gate "rust: cargo llvm-cov (coverage gate)" \
            cargo llvm-cov --workspace --fail-under-lines 80 \
                --include-files 'rust/crates/teslafat/src/fs/**' \
                --include-files 'rust/crates/teslafat/src/nbd/**'
    else
        skip_gate "rust: cargo llvm-cov (coverage gate)" "cargo-llvm-cov not installed"
    fi

    if has_cargo_subcmd "deny"; then
        run_gate "rust: cargo deny check" cargo deny check
    else
        skip_gate "rust: cargo deny check" "cargo-deny not installed"
    fi

    if has_cargo_subcmd "machete"; then
        run_gate "rust: cargo machete (unused deps)" cargo machete
    else
        skip_gate "rust: cargo machete (unused deps)" "cargo-machete not installed"
    fi

    RUSTDOCFLAGS="-D warnings" run_gate "rust: cargo doc (no broken doc links)" \
        cargo doc --no-deps --document-private-items --workspace

    cd "$REPO_ROOT" || exit 1
}

run_python_gates() {
    require_cmd python3 || require_cmd python
    cd "$REPO_ROOT/web" || exit 1

    # Find a python that has the dev tools. Order:
    #   1. $PYTHON env var (operator override)
    #   2. ../../.teslausb-tools-venv (out-of-tree venv per AI workspace rule)
    #   3. bare `python3` / `python` on PATH (must have teslausb-web installed)
    local py="${PYTHON:-}"
    if [[ -z "$py" ]]; then
        if   [[ -x "$REPO_ROOT/../.teslausb-tools-venv/bin/python" ]]; then
            py="$REPO_ROOT/../.teslausb-tools-venv/bin/python"
        elif [[ -x "$REPO_ROOT/../.teslausb-tools-venv/Scripts/python.exe" ]]; then
            py="$REPO_ROOT/../.teslausb-tools-venv/Scripts/python.exe"
        elif command -v python3 >/dev/null 2>&1; then
            py="python3"
        else
            py="python"
        fi
    fi
    printf "  Using Python: %s\n" "$py"

    run_gate "python: ruff check" "$py" -m ruff check .
    run_gate "python: ruff format --check" "$py" -m ruff format --check .
    run_gate "python: mypy (strict, files from pyproject.toml)" "$py" -m mypy
    run_gate "python: pytest --cov-fail-under=80" \
        "$py" -m pytest --strict-markers --strict-config \
                       --cov=teslausb_web --cov-fail-under=80
    run_gate "python: vulture teslausb_web --min-confidence 80" \
        "$py" -m vulture teslausb_web --min-confidence 80
    run_gate "python: bandit -r teslausb_web -ll" \
        "$py" -m bandit -r teslausb_web -ll

    cd "$REPO_ROOT" || exit 1
}

run_hygiene_gates() {
    cd "$REPO_ROOT" || exit 1
    require_cmd git

    # Charter L564: no files > 1 MiB without LFS approval.
    # Scope to TRACKED files only — gitignored build artifacts on
    # disk (e.g. rust/target/, web/.ruff_cache/) are irrelevant; the
    # rule is about what got committed.
    check_large_files() {
        local oversized
        oversized=$(git ls-files -z \
            | while IFS= read -r -d '' f; do
                  if [[ -f "$f" ]]; then
                      sz=$(wc -c <"$f")
                      [[ "$sz" -gt 1048576 ]] && printf "%s\t%s bytes\n" "$f" "$sz"
                  fi
              done)
        if [[ -n "$oversized" ]]; then
            printf "Tracked files exceed 1 MiB without LFS approval:\n%s\n" "$oversized"
            return 1
        fi
    }
    run_gate "hygiene: no tracked files > 1 MiB" check_large_files

    # Charter L565-566: no .bak / __pycache__ / target / node_modules /
    # IDE files COMMITTED to the repo. Same git-ls-files scope — local
    # untracked __pycache__ from a fresh pytest run is fine; what we
    # care about is preventing accidental commits.
    check_forbidden_artifacts() {
        local forbidden
        forbidden=$(git ls-files \
            | grep -E '(^|/)(\.bak$|\.bak/|__pycache__/|node_modules/|target/|\.idea/|\.vscode/)' \
            || true)
        if [[ -n "$forbidden" ]]; then
            printf "Forbidden artifacts committed to repo:\n%s\n" "$forbidden"
            return 1
        fi
    }
    run_gate "hygiene: no forbidden artifacts in git" check_forbidden_artifacts

    # Phase 4c.2: shell scripts in the repo must be shellcheck-clean
    # at `-S warning` (charter §"CI Gates" + 00-PLAN.md Phase 4c review
    # gate). Scoped to tracked .sh files so out-of-tree scratch scripts
    # don't break the gate. Optional gate — skips cleanly if shellcheck
    # isn't installed (CI installs it via setup-dev.sh / podman image).
    if command -v shellcheck >/dev/null 2>&1; then
        check_shellcheck() {
            local sh_files
            mapfile -t sh_files < <(git ls-files '*.sh')
            [[ ${#sh_files[@]} -eq 0 ]] && return 0
            shellcheck -S warning "${sh_files[@]}"
        }
        run_gate "hygiene: shellcheck -S warning (tracked *.sh)" check_shellcheck
    else
        skip_gate "hygiene: shellcheck -S warning (tracked *.sh)" "shellcheck not installed"
    fi

    # Charter L568: markdown links resolve (lychee). Scoped to tracked
    # markdown so out-of-tree generated docs don't break the gate.
    if command -v lychee >/dev/null 2>&1; then
        check_markdown_links() {
            local md_files
            mapfile -t md_files < <(git ls-files '*.md')
            [[ ${#md_files[@]} -eq 0 ]] && return 0
            lychee --no-progress --cache --max-cache-age 1d "${md_files[@]}"
        }
        run_gate "hygiene: markdown links (lychee)" check_markdown_links
    else
        skip_gate "hygiene: markdown links (lychee)" "lychee not installed"
    fi
}

usage() {
    sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ─── Arg parsing ─────────────────────────────────────────────────────
RUN_RUST=1; RUN_PYTHON=1; RUN_HYGIENE=1
FAIL_FAST=1

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    --all)        FAIL_FAST=0 ;;
    --rust)       RUN_PYTHON=0; RUN_HYGIENE=0 ;;
    --python)    RUN_RUST=0;    RUN_HYGIENE=0 ;;
    --hygiene)   RUN_RUST=0;    RUN_PYTHON=0 ;;
    "")          ;;  # default: all, fail fast
    *) printf "unknown arg: %s\n" "$1" >&2; usage >&2; exit 2 ;;
esac

# ─── Main ────────────────────────────────────────────────────────────
printf "%sTeslaUSB B-1 local gate runner%s (charter §\"CI Gates\")\n" "$C_BLD" "$C_RST"
printf "Repo: %s\n" "$REPO_ROOT"
printf "Mode: fail_fast=%d rust=%d python=%d hygiene=%d\n" \
    "$FAIL_FAST" "$RUN_RUST" "$RUN_PYTHON" "$RUN_HYGIENE"

[[ "$RUN_RUST"    == "1" ]] && run_rust_gates
[[ "$RUN_PYTHON"  == "1" ]] && run_python_gates
[[ "$RUN_HYGIENE" == "1" ]] && run_hygiene_gates

print_summary

if [[ ${#FAILED[@]} -gt 0 ]]; then
    exit 1
fi
exit 0
