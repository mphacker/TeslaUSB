#!/usr/bin/env python3
"""TeslaUSB Safe Mode — boot counter and safe-mode detection.

Tracks rapid reboots to detect boot loops. If the device reboots 3+ times
within 10 minutes, subsequent boots enter "safe mode": only SSH and the
fallback AP start; all TeslaUSB services are skipped.

State file: /var/lib/teslausb/boot_count
Touch file: /run/teslausb-safe-mode  (signals safe-mode to other services)

Called by teslausb-safe-mode.service at early boot (Before= all TeslaUSB units).
"""

import json
import os
import sys
import time

STATE_DIR = "/var/lib/teslausb"
STATE_FILE = os.path.join(STATE_DIR, "boot_count")
SAFE_MODE_FLAG = "/run/teslausb-safe-mode"

MAX_BOOTS = 3
WINDOW_SECONDS = 600  # 10 minutes
CLEAR_DELAY = WINDOW_SECONDS  # how long to wait before clearing the counter


def _read_state():
    """Read the boot counter state file. Returns (count, first_boot_ts)."""
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        return int(data.get("count", 0)), float(data.get("first_ts", 0))
    except (FileNotFoundError, json.JSONDecodeError, ValueError, KeyError):
        return 0, 0.0


def _write_state(count, first_ts):
    """Atomically write the boot counter state."""
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    data = json.dumps({"count": count, "first_ts": first_ts})
    with open(tmp, "w") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)


def _clear_state():
    """Remove the boot counter (system is stable)."""
    try:
        os.remove(STATE_FILE)
    except FileNotFoundError:
        pass
    # Also remove the safe-mode flag if present
    try:
        os.remove(SAFE_MODE_FLAG)
    except FileNotFoundError:
        pass


def check_and_increment():
    """Increment boot counter and check for safe mode.

    Returns True if safe mode should be activated.
    """
    now = time.time()
    count, first_ts = _read_state()

    # If the window has expired, reset the counter
    if first_ts > 0 and (now - first_ts) > WINDOW_SECONDS:
        count = 0
        first_ts = 0

    # First boot in this window
    if count == 0:
        first_ts = now

    count += 1
    _write_state(count, first_ts)

    if count >= MAX_BOOTS:
        return True
    return False


def enter_safe_mode():
    """Create the safe-mode flag file so other services know to skip."""
    with open(SAFE_MODE_FLAG, "w") as f:
        f.write(f"Safe mode activated at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Reason: {MAX_BOOTS}+ reboots within {WINDOW_SECONDS}s\n")
        f.write("SSH and AP are available. TeslaUSB services skipped.\n")
        f.flush()
        os.fsync(f.fileno())


def wait_and_clear():
    """Wait for the stability window, then clear the counter.

    This runs in the background (forked by systemd via Type=oneshot).
    After CLEAR_DELAY seconds of uptime, the counter resets — proving
    the system is stable.
    """
    time.sleep(CLEAR_DELAY)
    _clear_state()


def main():
    if len(sys.argv) < 2:
        print("Usage: safe_mode.py [check|clear]", file=sys.stderr)
        return 1

    action = sys.argv[1]

    if action == "check":
        if check_and_increment():
            enter_safe_mode()
            print(f"SAFE MODE: {MAX_BOOTS}+ reboots in {WINDOW_SECONDS}s — "
                  "skipping TeslaUSB services. SSH and AP remain available.",
                  file=sys.stderr)
            return 2  # Exit code 2 = safe mode activated
        return 0  # Normal boot

    elif action == "clear":
        wait_and_clear()
        return 0

    else:
        print(f"Unknown action: {action}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
