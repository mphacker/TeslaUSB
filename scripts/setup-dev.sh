#!/usr/bin/env bash
#
# setup-dev.sh -- idempotent developer-environment installer for TeslaUSB B-1.
#
# Brings a clean dev box up to the point where:
#   ./scripts/check.sh --all    reports 16 PASS / 0 FAIL / 0 SKIP
#   pre-commit run --all-files  passes every hook
#
# What it installs (in order):
#   1. Rust toolchain via rustup (pinned by rust/rust-toolchain.toml = 1.85.0)
#   2. Cargo subcommands: cargo-deny, cargo-machete, cargo-llvm-cov
#   3. lychee (markdown link checker) via cargo install
#   4. Out-of-tree Python venv at ../.teslausb-tools-venv/
#      (NOT inside the repo per AI workspace rule)
#   5. Editable install of web/[dev] into that venv (ruff, mypy, pytest,
#      pytest-cov, vulture, bandit, pre-commit)
#   6. pre-commit install -- registers .git/hooks/pre-commit
#
# What it does NOT install:
#   - Python interpreter itself (check + abort if missing -- operator
#     decides which python to use on which platform)
#   - git, curl, build-essential / Xcode CLT / VS Build Tools (the host
#     prerequisites for running cargo at all)
#   - Pi-specific runtime deps (nginx, gunicorn, etc.) -- handled by
#     a separate provisioning script when B-1 reaches the H phases
#
# Modes:
#   --dry-run    print every action that would be taken; make no changes
#   --check      verify the env is already set up; non-zero exit if not
#   --help       this message
#   (no flag)    install + reinstall as needed (idempotent)
#
# Platform notes:
#   - Linux / macOS / WSL2: full install path works.
#   - Windows Git-Bash: works if Visual Studio Build Tools (C/C++ workload)
#     are already installed. The script will NOT install VS Build Tools.
#     The user-experience target is Pi; Windows is "works if you already
#     have a C toolchain" since this is rarely used standalone on Windows.
#
# See docs/03-CODE-QUALITY-CHARTER.md "CI Gates" for what each tool does.
# See .pre-commit-config.yaml for the hook wiring.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="$(cd "${REPO_ROOT}/.." && pwd)/.teslausb-tools-venv"
RUST_TOOLCHAIN_FILE="${REPO_ROOT}/rust/rust-toolchain.toml"

DRY_RUN=0
CHECK_ONLY=0

# ----- color + log helpers (mirror scripts/check.sh conventions) ------------
if [[ -t 1 ]] && command -v tput > /dev/null 2>&1; then
    BOLD="$(tput bold)"
    RED="$(tput setaf 1)"
    GREEN="$(tput setaf 2)"
    YELLOW="$(tput setaf 3)"
    BLUE="$(tput setaf 4)"
    RESET="$(tput sgr0)"
else
    BOLD=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; RESET=""
fi

log_step()   { printf '%s===== %s =====%s\n' "${BOLD}${BLUE}" "$1" "${RESET}"; }
log_info()   { printf '%s[INFO]%s  %s\n' "${BOLD}" "${RESET}" "$1"; }
log_pass()   { printf '%s[OK]%s    %s\n' "${BOLD}${GREEN}" "${RESET}" "$1"; }
log_warn()   { printf '%s[WARN]%s  %s\n' "${BOLD}${YELLOW}" "${RESET}" "$1"; }
log_err()    { printf '%s[ERROR]%s %s\n' "${BOLD}${RED}" "${RESET}" "$1" >&2; }
log_action() {
    # Print what we're about to do; in --dry-run, skip the action.
    # In --check, label it as a check (we're verifying, not running).
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        printf '%s[DRY-RUN]%s would run: %s\n' "${BOLD}${YELLOW}" "${RESET}" "$1"
    elif [[ "${CHECK_ONLY}" -eq 1 ]]; then
        printf '%s[CHECK]%s   verifying: %s\n' "${BOLD}${BLUE}" "${RESET}" "$1"
    else
        printf '%s[RUN]%s    %s\n' "${BOLD}" "${RESET}" "$1"
    fi
}

# ----- usage --------------------------------------------------------------
usage() {
    cat <<'USAGE'
setup-dev.sh -- TeslaUSB B-1 dev-environment installer

USAGE:
    scripts/setup-dev.sh              # install / reinstall (idempotent)
    scripts/setup-dev.sh --dry-run    # show what would happen; make no changes
    scripts/setup-dev.sh --check      # verify env is set up; exit non-zero if not
    scripts/setup-dev.sh --help       # this message

WHAT IT INSTALLS:
    1. rustup + Rust toolchain pinned by rust/rust-toolchain.toml
    2. cargo subcommands: cargo-deny, cargo-machete, cargo-llvm-cov
    3. lychee (markdown link checker)
    4. Out-of-tree Python venv at ../.teslausb-tools-venv/
    5. Editable install of web/[dev] (ruff, mypy, pytest, pytest-cov,
       vulture, bandit, pre-commit)
    6. pre-commit install (registers .git/hooks/pre-commit)

WHAT IT DOES NOT INSTALL:
    - Python interpreter (check + abort if missing)
    - git, curl, C toolchain (prerequisites for cargo)
    - Pi-specific runtime deps (handled later)

ENVIRONMENT OVERRIDES:
    SETUP_PYTHON     -- path to python3 binary (default: auto-detect)
    SETUP_SKIP_RUST  -- set to 1 to skip Rust / cargo / lychee install
    SETUP_SKIP_PY    -- set to 1 to skip Python venv install
    SETUP_SKIP_HOOK  -- set to 1 to skip `pre-commit install`

EXIT CODES:
    0  success (everything installed / everything verified in --check)
    1  one or more install / verify steps failed
    2  required prerequisite missing (Python interpreter, git, etc.)
USAGE
}

# ----- argument parsing ---------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --check)   CHECK_ONLY=1 ;;
        --help|-h) usage; exit 0 ;;
        *) log_err "unknown argument: $1"; usage; exit 2 ;;
    esac
    shift
done

if [[ "${DRY_RUN}" -eq 1 && "${CHECK_ONLY}" -eq 1 ]]; then
    log_err "--dry-run and --check are mutually exclusive"
    exit 2
fi

# ----- prerequisite checks ------------------------------------------------
ensure_prereq() {
    local cmd="$1"
    local hint="$2"
    if ! command -v "${cmd}" > /dev/null 2>&1; then
        log_err "required prerequisite not found: ${cmd}"
        log_err "  hint: ${hint}"
        exit 2
    fi
}

ensure_prereq git "install git from your platform package manager"
ensure_prereq curl "install curl from your platform package manager"

# Python: prefer python3, fall back to python. SETUP_PYTHON overrides.
detect_python() {
    if [[ -n "${SETUP_PYTHON:-}" ]]; then
        echo "${SETUP_PYTHON}"; return
    fi
    if command -v python3 > /dev/null 2>&1; then echo "python3"; return; fi
    if command -v python  > /dev/null 2>&1; then echo "python";  return; fi
    echo ""
}
PYTHON="$(detect_python)"
if [[ -z "${PYTHON}" ]]; then
    log_err "no python interpreter found on PATH"
    log_err "  install Python >=3.11 from python.org or your package manager"
    log_err "  then re-run, optionally with SETUP_PYTHON=/path/to/python"
    exit 2
fi

PY_VERSION="$("${PYTHON}" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
log_info "python: ${PYTHON} (version ${PY_VERSION})"
PY_MAJOR="$(echo "${PY_VERSION}" | cut -d. -f1)"
PY_MINOR="$(echo "${PY_VERSION}" | cut -d. -f2)"
if [[ "${PY_MAJOR}" -lt 3 ]] || { [[ "${PY_MAJOR}" -eq 3 ]] && [[ "${PY_MINOR}" -lt 11 ]]; }; then
    log_err "python ${PY_VERSION} is too old; need >=3.11"
    exit 2
fi

# ----- step 1: Rust toolchain via rustup ----------------------------------
install_rustup() {
    if command -v rustup > /dev/null 2>&1; then
        log_pass "rustup already installed: $(rustup --version | head -n1)"
        return 0
    fi
    log_action "curl https://sh.rustup.rs -sSf | sh -s -- --default-toolchain none -y"
    if [[ "${DRY_RUN}" -eq 1 ]]; then return 0; fi
    if [[ "${CHECK_ONLY}" -eq 1 ]]; then
        log_err "rustup not installed (--check mode; would have been installed)"
        return 1
    fi
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- --default-toolchain none -y
    # shellcheck disable=SC1091  # cargo env script is generated at install time
    [[ -f "${HOME}/.cargo/env" ]] && source "${HOME}/.cargo/env"
}

install_toolchain() {
    if [[ ! -f "${RUST_TOOLCHAIN_FILE}" ]]; then
        log_err "rust-toolchain.toml not found at ${RUST_TOOLCHAIN_FILE}"
        return 1
    fi
    # rustup will read rust-toolchain.toml automatically when invoked from
    # the rust/ subdir; the show command triggers install of any missing
    # channel/component listed there. Idempotent.
    log_action "(cd rust && rustup show)"
    if [[ "${DRY_RUN}" -eq 1 ]]; then return 0; fi
    if [[ "${CHECK_ONLY}" -eq 1 ]]; then
        if (cd "${REPO_ROOT}/rust" && rustup show active-toolchain > /dev/null 2>&1); then
            log_pass "rust toolchain matches rust-toolchain.toml"
            return 0
        else
            log_err "rust toolchain not matching rust-toolchain.toml"
            return 1
        fi
    fi
    (cd "${REPO_ROOT}/rust" && rustup show > /dev/null)
}

install_cargo_subcommand() {
    local subcmd="$1"
    local crate="$2"
    if cargo "${subcmd}" --version > /dev/null 2>&1; then
        log_pass "cargo-${subcmd} already installed"
        return 0
    fi
    log_action "cargo install --locked ${crate}"
    if [[ "${DRY_RUN}" -eq 1 ]]; then return 0; fi
    if [[ "${CHECK_ONLY}" -eq 1 ]]; then
        log_err "cargo-${subcmd} not installed (--check mode)"
        return 1
    fi
    cargo install --locked "${crate}"
}

install_lychee() {
    if command -v lychee > /dev/null 2>&1; then
        log_pass "lychee already installed: $(lychee --version | head -n1)"
        return 0
    fi
    log_action "cargo install --locked lychee"
    if [[ "${DRY_RUN}" -eq 1 ]]; then return 0; fi
    if [[ "${CHECK_ONLY}" -eq 1 ]]; then
        log_err "lychee not installed (--check mode)"
        return 1
    fi
    cargo install --locked lychee
}

# ----- step 2: Python venv + dev tools ------------------------------------
install_venv() {
    if [[ -d "${VENV_DIR}" ]]; then
        log_pass "venv exists: ${VENV_DIR}"
        return 0
    fi
    log_action "${PYTHON} -m venv ${VENV_DIR}"
    if [[ "${DRY_RUN}" -eq 1 ]]; then return 0; fi
    if [[ "${CHECK_ONLY}" -eq 1 ]]; then
        log_err "venv missing: ${VENV_DIR} (--check mode)"
        return 1
    fi
    "${PYTHON}" -m venv "${VENV_DIR}"
}

venv_python_path() {
    if [[ -x "${VENV_DIR}/bin/python" ]];      then echo "${VENV_DIR}/bin/python"; return; fi
    if [[ -x "${VENV_DIR}/Scripts/python.exe" ]]; then echo "${VENV_DIR}/Scripts/python.exe"; return; fi
    echo ""
}

install_pip_deps() {
    local venv_py
    venv_py="$(venv_python_path)"
    if [[ -z "${venv_py}" ]]; then
        if [[ "${DRY_RUN}" -eq 1 ]]; then
            log_action "(venv python would be discovered after step 1) pip install -e ${REPO_ROOT}/web[dev]"
            return 0
        fi
        log_err "venv python not found under ${VENV_DIR}"
        return 1
    fi
    # In --check mode just verify each expected tool is importable.
    if [[ "${CHECK_ONLY}" -eq 1 ]]; then
        local fail=0 tool
        for tool in ruff mypy pytest pytest_cov vulture bandit pre_commit; do
            if "${venv_py}" -c "import ${tool}" 2> /dev/null; then
                log_pass "venv has ${tool}"
            else
                log_err "venv missing ${tool}"
                fail=1
            fi
        done
        return "${fail}"
    fi
    log_action "${venv_py} -m pip install --upgrade pip"
    log_action "${venv_py} -m pip install -e ${REPO_ROOT}/web[dev]"
    if [[ "${DRY_RUN}" -eq 1 ]]; then return 0; fi
    "${venv_py}" -m pip install --upgrade pip
    "${venv_py}" -m pip install -e "${REPO_ROOT}/web[dev]"
}

# ----- step 3: pre-commit hook --------------------------------------------
install_precommit_hook() {
    local venv_py hook_path
    venv_py="$(venv_python_path)"
    hook_path="${REPO_ROOT}/.git/hooks/pre-commit"
    if [[ "${CHECK_ONLY}" -eq 1 ]]; then
        if [[ -f "${hook_path}" ]] && grep -q "pre-commit" "${hook_path}" 2> /dev/null; then
            log_pass ".git/hooks/pre-commit registered"
            return 0
        fi
        log_err ".git/hooks/pre-commit NOT registered"
        return 1
    fi
    log_action "${venv_py} -m pre_commit install"
    if [[ "${DRY_RUN}" -eq 1 ]]; then return 0; fi
    if [[ -z "${venv_py}" ]]; then
        log_err "venv python not found; cannot install hook"
        return 1
    fi
    (cd "${REPO_ROOT}" && "${venv_py}" -m pre_commit install)
}

# ----- main ---------------------------------------------------------------
FAIL=0

if [[ "${SETUP_SKIP_RUST:-0}" -ne 1 ]]; then
    log_step "[1/3] Rust toolchain + cargo subcommands + lychee"
    install_rustup                                       || FAIL=1
    install_toolchain                                    || FAIL=1
    if command -v cargo > /dev/null 2>&1 || [[ "${DRY_RUN}" -eq 1 ]]; then
        install_cargo_subcommand deny     cargo-deny     || FAIL=1
        install_cargo_subcommand machete  cargo-machete  || FAIL=1
        install_cargo_subcommand "llvm-cov" cargo-llvm-cov || FAIL=1
        install_lychee                                   || FAIL=1
    else
        log_warn "cargo not on PATH after rustup install; restart shell + re-run"
        FAIL=1
    fi
else
    log_info "SETUP_SKIP_RUST=1 -- skipping Rust step"
fi

if [[ "${SETUP_SKIP_PY:-0}" -ne 1 ]]; then
    log_step "[2/3] Python venv + dev tools (${VENV_DIR})"
    install_venv     || FAIL=1
    install_pip_deps || FAIL=1
else
    log_info "SETUP_SKIP_PY=1 -- skipping Python step"
fi

if [[ "${SETUP_SKIP_HOOK:-0}" -ne 1 ]]; then
    log_step "[3/3] Pre-commit git hook"
    install_precommit_hook || FAIL=1
else
    log_info "SETUP_SKIP_HOOK=1 -- skipping git-hook step"
fi

printf '\n================== Summary ==================\n'
if [[ "${FAIL}" -eq 0 ]]; then
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        log_pass "DRY-RUN complete -- all steps would succeed"
    elif [[ "${CHECK_ONLY}" -eq 1 ]]; then
        log_pass "CHECK complete -- environment is fully set up"
    else
        log_pass "setup complete"
        printf '\nNext steps:\n'
        printf '  1. Restart your shell so PATH picks up ~/.cargo/bin\n'
        printf '  2. ./scripts/check.sh --all      (expect 16 PASS / 0 FAIL / 0 SKIP)\n'
        printf '  3. pre-commit run --all-files    (expect 11 PASS / 0 FAIL)\n'
    fi
    exit 0
else
    log_err "one or more steps failed (see [ERROR] lines above)"
    exit 1
fi
