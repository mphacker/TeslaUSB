#!/usr/bin/env bash
# setup-lib/06-boot.sh — Phase 6.6
#
# Boot-firmware edits: kernel cmdline + `config.txt`.
#
# Per `docs/00-PLAN.md` row 6.6 + the v1 `setup_usb.sh` precedent +
# the operator brief for Phase 6.6, B-1 needs the following changes
# to the Pi's boot firmware so the SoC's USB OTG controller can be
# driven as a gadget by the cross-compiled `teslafat` daemon:
#
#   * `/boot/firmware/cmdline.txt`  — append `modules-load=dwc2`
#     (single-line file; whitespace-separated kernel args). v1 only
#     ever added this one key here; cmdline.txt is fragile so we do
#     NOT touch anything else.
#
#   * `/boot/firmware/config.txt`   — ensure these three lines exist
#     under the `[all]` section:
#         dtoverlay=dwc2,dr_mode=peripheral
#         max_usb_current=1
#         arm_boost=1
#     v1 used the looser `dtoverlay=dwc2` with no `dr_mode=` so the
#     overlay auto-detected; B-1's gadget code path is strictly
#     peripheral, so we pin the mode explicitly. If a competing
#     `dtoverlay=dwc2…` line already exists with a different value
#     we REPLACE it rather than duplicating.
#
# Boot-path detection: Bookworm Pi OS uses `/boot/firmware/` (the
# new firmware partition mount); Buster-era installs used `/boot/`
# directly. We probe both and use whichever pair exists. If neither
# exists this is not a Raspberry Pi (or the firmware partition is
# unmounted) and we abort cleanly via `b1_err`.
#
# Idempotency (charter §"every step is a no-op on re-run"):
#   * cmdline.txt key presence checked with whole-word grep before
#     any mutation.
#   * config.txt lines checked with `^line$` anchored grep before
#     any mutation; mis-valued duplicates are rewritten in-place
#     via `sed`.
#   * `b1_backup` runs BEFORE the FIRST mutation per file and is
#     itself idempotent (no .b1-backup pile-up on rerun).
#
# Dry-run (charter §"every step honours TESLAUSB_DRY_RUN"):
#   * Pure reads (grep, sed -n preview, diff) always run.
#   * Before any would-be write we emit a unified diff of
#     (current vs. proposed) so the operator can review.
#   * Every mutation flows through `b1_run`, which is a no-op when
#     TESLAUSB_DRY_RUN=1.
#
# Reboot policy: this step NEVER reboots. If either file actually
# changed we set `B1_BOOT_REBOOT_REQUIRED=1` so the eventual Phase
# 6.10 final-enable step can warn the operator that a reboot is
# needed for `dwc2` to come up as a gadget controller.

# Re-source common in case this file is invoked via --only.
# shellcheck source=00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/00-common.sh"

# --------------------------------------------------------------------
# Constants — exported so 6.10 (final enable) + 6.11 (uninstall.sh)
# can reuse them verbatim and so reviewers see the entire change
# surface from one file.
# --------------------------------------------------------------------

# Whole-word keys to ensure present in cmdline.txt (single-line file,
# space-separated). Each entry MUST appear verbatim as a standalone
# token. If a key with the same prefix but different value is already
# there (e.g. someone left `modules-load=dwc2,libcomposite`), we
# treat it as already-present and DO NOT touch the file — cmdline.txt
# is fragile and the operator's superset is acceptable.
B1_CMDLINE_KEYS=("modules-load=dwc2")
export B1_CMDLINE_KEYS

# Lines to ensure present under the `[all]` section of config.txt.
# Each entry is matched with a `^key=` anchored regex; a line whose
# key matches but whose value differs is REPLACED in-place (via
# `sed -i`) rather than duplicated.
B1_CONFIG_LINES_ALL=(
  "dtoverlay=dwc2,dr_mode=peripheral"
  "max_usb_current=1"
  "arm_boost=1"
)
export B1_CONFIG_LINES_ALL

# Resolved paths — set by _b1_resolve_boot_paths() before any
# read/write. Empty until that helper has run.
B1_BOOT_CMDLINE=""
B1_BOOT_CONFIG=""
export B1_BOOT_CMDLINE B1_BOOT_CONFIG

# Set to "1" by b1_step_06 iff either file actually changed (or
# WOULD have changed under --dry-run). Phase 6.10 consumes this to
# decide whether to prompt the operator about a reboot.
B1_BOOT_REBOOT_REQUIRED="${B1_BOOT_REBOOT_REQUIRED:-0}"
export B1_BOOT_REBOOT_REQUIRED

# --------------------------------------------------------------------
# Helpers (private to this step)
# --------------------------------------------------------------------

# _b1_resolve_boot_paths
#   Probes the two known boot layouts and sets B1_BOOT_CMDLINE +
#   B1_BOOT_CONFIG. Returns non-zero (and b1_err's) if neither pair
#   exists — this is not a Raspberry Pi or the firmware partition is
#   not mounted; either way 6.6 is a no-go.
_b1_resolve_boot_paths() {
  local candidates=(
    "/boot/firmware/cmdline.txt:/boot/firmware/config.txt"  # Bookworm
    "/boot/cmdline.txt:/boot/config.txt"                    # Buster
  )
  local pair cmd cfg
  for pair in "${candidates[@]}"; do
    cmd="${pair%%:*}"
    cfg="${pair##*:}"
    if [[ -f "${cmd}" && -f "${cfg}" ]]; then
      B1_BOOT_CMDLINE="${cmd}"
      B1_BOOT_CONFIG="${cfg}"
      b1_log "boot files: ${B1_BOOT_CMDLINE} + ${B1_BOOT_CONFIG}"
      return 0
    fi
  done
  b1_err "no boot-firmware files found at /boot/firmware/ or /boot/"
  b1_err "  (not a Raspberry Pi, or firmware partition is not mounted)"
  return 1
}

# _b1_cmdline_has_key <file> <key>
#   True iff <key> appears as a standalone whitespace-separated token
#   on the first non-blank line of <file>. cmdline.txt is documented
#   as a single-line file; we only inspect the first non-blank line
#   to avoid being fooled by trailing comments / editor artefacts.
_b1_cmdline_has_key() {
  local file="$1"
  local key="$2"
  # Read the first non-blank line and check for the token surrounded
  # by start-of-line/whitespace and whitespace/end-of-line.
  awk -v k="${key}" '
    NF == 0 { next }
    {
      n = split($0, toks, /[[:space:]]+/)
      for (i = 1; i <= n; i++) {
        if (toks[i] == k) { found = 1; exit }
      }
      exit
    }
    END { exit found ? 0 : 1 }
  ' "${file}"
}

# _b1_render_cmdline <file> <key...>
#   Print to stdout the proposed contents of cmdline.txt: the first
#   non-blank line with any missing keys appended (space-separated),
#   trailing newline preserved. Pure (no mutation).
_b1_render_cmdline() {
  local file="$1"; shift
  local missing=()
  local k
  for k in "$@"; do
    if ! _b1_cmdline_has_key "${file}" "${k}"; then
      missing+=("${k}")
    fi
  done
  if (( ${#missing[@]} == 0 )); then
    cat -- "${file}"
    return 0
  fi
  # Append missing keys to the first non-blank line, preserve any
  # subsequent lines verbatim (Pi OS doesn't put them there but we
  # don't want to silently drop operator additions).
  awk -v extra="${missing[*]}" '
    BEGIN { done = 0 }
    {
      if (!done && NF > 0) {
        sub(/[[:space:]]+$/, "", $0)
        printf "%s %s\n", $0, extra
        done = 1
        next
      }
      print
    }
  ' "${file}"
}

# _b1_config_key_of <line>
#   Echo the `key=` prefix of a `key=value` config.txt line. Used to
#   spot lines whose key matches but whose value differs (replace
#   target, not duplicate target).
_b1_config_key_of() {
  printf '%s' "${1%%=*}="
}

# _b1_render_config <file> <line...>
#   Print to stdout the proposed contents of config.txt: ensure each
#   requested line is present under `[all]`, replacing any existing
#   line that shares the same `key=` prefix. Pure (no mutation).
#
#   Strategy:
#     1. For each desired line, if it already matches verbatim → noop.
#     2. Else if a line with the same `key=` prefix exists ANYWHERE
#        under `[all]` (i.e. before the next `[section]` header) →
#        rewrite that line.
#     3. Else append the line to the end of the `[all]` block (just
#        before the next `[section]` header, or EOF).
#     4. If no `[all]` section exists, create one at EOF with all
#        requested lines (mirrors v1's fallback).
_b1_render_config() {
  local file="$1"; shift
  local desired=("$@")
  awk -v desired_str="$(printf '%s\n' "${desired[@]}")" '
    BEGIN {
      n = split(desired_str, desired, /\n/)
      # Track which desired lines we still need to place.
      for (i = 1; i <= n; i++) {
        if (desired[i] == "") continue
        key = desired[i]; sub(/=.*$/, "=", key)
        want_line[key] = desired[i]
        want_order[++want_count] = key
      }
    }
    # Detect entering / leaving the [all] section.
    /^[[:space:]]*\[[^]]+\][[:space:]]*$/ {
      # Section header. If we were inside [all] and have unplaced
      # keys, emit them just before the new header.
      if (in_all) {
        for (i = 1; i <= want_count; i++) {
          k = want_order[i]
          if (!(k in placed)) {
            print want_line[k]
            placed[k] = 1
          }
        }
        in_all = 0
      }
      seen_all_header = seen_all_header || ($0 ~ /^[[:space:]]*\[all\][[:space:]]*$/)
      if ($0 ~ /^[[:space:]]*\[all\][[:space:]]*$/) in_all = 1
      print
      next
    }
    {
      if (in_all) {
        # Check for a key match.
        line_key = $0
        if (line_key ~ /^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=/) {
          sub(/=.*$/, "=", line_key)
          sub(/^[[:space:]]+/, "", line_key)
          if (line_key in want_line && !(line_key in placed)) {
            # Replace (collapses duplicates: only first occurrence
            # is rewritten, later ones are dropped).
            print want_line[line_key]
            placed[line_key] = 1
            next
          }
          if (line_key in want_line && (line_key in placed)) {
            # Drop duplicate.
            next
          }
        }
      }
      print
    }
    END {
      if (in_all) {
        # File ended while still inside [all] — flush remaining.
        for (i = 1; i <= want_count; i++) {
          k = want_order[i]
          if (!(k in placed)) {
            print want_line[k]
            placed[k] = 1
          }
        }
      } else if (!seen_all_header) {
        # No [all] section at all — append one.
        print ""
        print "[all]"
        for (i = 1; i <= want_count; i++) {
          k = want_order[i]
          if (!(k in placed)) print want_line[k]
        }
      } else {
        # [all] existed earlier and was already closed by another
        # section header before we placed everything. Append a
        # second [all] block at EOF; the Pi firmware concatenates
        # multiple [all] blocks, so this is safe.
        any_missing = 0
        for (i = 1; i <= want_count; i++) {
          if (!(want_order[i] in placed)) { any_missing = 1; break }
        }
        if (any_missing) {
          print ""
          print "[all]"
          for (i = 1; i <= want_count; i++) {
            k = want_order[i]
            if (!(k in placed)) print want_line[k]
          }
        }
      }
    }
  ' "${file}"
}

# _b1_diff_or_log <file> <rendered>
#   Print a unified diff of <file> vs the string <rendered> to the
#   log (via b1_log per-line). Always runs (dry-run or not) — gives
#   the operator a final preview right before the would-be write.
_b1_diff_or_log() {
  local file="$1"
  local rendered="$2"
  # `diff` returns 1 when files differ; we never want that to abort
  # the script (set -e), hence the `|| true`.
  diff -u "${file}" <(printf '%s' "${rendered}") 2>&1 | while IFS= read -r line; do
    b1_log "  diff: ${line}"
  done || true
}

# _b1_write_if_changed <file> <rendered> <state-var>
#   Compare <file> contents to <rendered>; if identical, no-op. Else
#   b1_backup the file, log a diff, and b1_run a write. Sets the
#   named state var to "1" if a write happened (or would have under
#   dry-run).
_b1_write_if_changed() {
  local file="$1"
  local rendered="$2"
  local state_var="$3"

  local current
  current="$(cat -- "${file}")"
  if [[ "${current}" == "${rendered}" ]]; then
    b1_log "unchanged: ${file}"
    return 0
  fi

  b1_log "would update: ${file}"
  _b1_diff_or_log "${file}" "${rendered}"

  # Backup BEFORE the would-be write so the operator can always roll
  # back. b1_backup is itself idempotent (one sibling per file ever).
  b1_backup "${file}"

  if [[ "${TESLAUSB_DRY_RUN:-0}" == "1" ]]; then
    b1_log "DRY-RUN: write ${file} (${#rendered} bytes)"
  else
    # Stage to a repo-local temp file (NEVER /tmp per runtime policy)
    # then `install -m 0644` atomically into place, preserving the
    # well-known mode the bootloader expects.
    local stage
    stage="$(dirname "${BASH_SOURCE[0]}")/.b1-stage-06-$$-${RANDOM}"
    # shellcheck disable=SC2064
    trap "rm -f -- '${stage}'" RETURN
    printf '%s' "${rendered}" > "${stage}"
    b1_run install -m 0644 -- "${stage}" "${file}"
  fi

  printf -v "${state_var}" '%s' 1
}

# --------------------------------------------------------------------
# Step entry point
# --------------------------------------------------------------------

b1_step_06() {
  local cmdline_changed=""
  local config_changed=""

  _b1_resolve_boot_paths || return 1

  # ------------------------------------------------------------------
  # 1) cmdline.txt — append missing keys (whole-word match).
  # ------------------------------------------------------------------
  local rendered_cmdline
  rendered_cmdline="$(_b1_render_cmdline "${B1_BOOT_CMDLINE}" "${B1_CMDLINE_KEYS[@]}")"
  _b1_write_if_changed "${B1_BOOT_CMDLINE}" "${rendered_cmdline}" cmdline_changed

  # ------------------------------------------------------------------
  # 2) config.txt — ensure each line under [all], replacing keys
  #    that exist with a different value.
  # ------------------------------------------------------------------
  local rendered_config
  rendered_config="$(_b1_render_config "${B1_BOOT_CONFIG}" "${B1_CONFIG_LINES_ALL[@]}")"
  _b1_write_if_changed "${B1_BOOT_CONFIG}" "${rendered_config}" config_changed

  # ------------------------------------------------------------------
  # 3) Reboot-required flag — consumed by 6.10.
  # ------------------------------------------------------------------
  if [[ -n "${cmdline_changed}${config_changed}" ]]; then
    B1_BOOT_REBOOT_REQUIRED=1
    export B1_BOOT_REBOOT_REQUIRED
    b1_log "boot files changed — reboot required (deferred to Phase 6.10)"
  else
    b1_log "boot files already in desired state — no reboot required"
  fi

  return 0
}
