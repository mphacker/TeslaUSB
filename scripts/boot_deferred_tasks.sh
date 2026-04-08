#!/bin/bash
set -uo pipefail

# boot_deferred_tasks.sh — Runs AFTER USB gadget is presented to Tesla.
# These tasks are deferred from boot to ensure Tesla sees the USB drive ASAP.
#
# Called by teslausb-deferred-tasks.service (After=present_usb_on_boot.service)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

BOOT_START_MS=$(date +%s%3N)
log_timing() {
    local checkpoint="$1"
    local now_ms=$(date +%s%3N)
    local elapsed=$((now_ms - BOOT_START_MS))
    echo "[DEFERRED TASKS] +${elapsed}ms: $checkpoint"
}

log_timing "Starting deferred boot tasks"

# Load configuration
source "$SCRIPT_DIR/config.sh"
log_timing "Config loaded"

CLEANUP_CONFIG="$GADGET_DIR/cleanup_config.json"
CLEANUP_SCRIPT="$GADGET_DIR/scripts/run_boot_cleanup.py"
LOG_FILE="$GADGET_DIR/boot_cleanup.log"

# ============================================================================
# Task 1: Auto-cleanup (if enabled)
# ============================================================================
needs_cleanup() {
    if [ ! -f "$CLEANUP_CONFIG" ]; then
        return 1
    fi
    if grep -q '"enabled": true' "$CLEANUP_CONFIG" 2>/dev/null; then
        return 0
    fi
    return 1
}

if needs_cleanup; then
    log_timing "Running auto-cleanup (via quick_edit if needed)..."
    # Cleanup uses the web app's cleanup service which handles mount operations
    /usr/bin/python3 "$CLEANUP_SCRIPT" 2>&1 | tee -a "$LOG_FILE" || true
    log_timing "Cleanup complete"
else
    log_timing "Cleanup not enabled, skipping"
fi

# ============================================================================
# Task 2: Random chime selection (if enabled)
# ============================================================================
RANDOM_CHIME_SCRIPT="$GADGET_DIR/scripts/select_random_chime.py"

if [ -f "$RANDOM_CHIME_SCRIPT" ]; then
    log_timing "Checking random chime mode..."
    # select_random_chime.py handles quick_edit internally if needed
    /usr/bin/python3 "$RANDOM_CHIME_SCRIPT" || true
    log_timing "Random chime check complete"
fi

log_timing "All deferred tasks complete (total: $(($(date +%s%3N) - BOOT_START_MS))ms)"
