import json
import logging
import os
import subprocess
import yaml

from config import GADGET_DIR

logger = logging.getLogger(__name__)


SCRIPT_NAME = "ap_control.sh"
CONFIG_YAML = os.path.join(GADGET_DIR, "config.yaml")


def _script_path():
    return os.path.join(GADGET_DIR, "scripts", SCRIPT_NAME)


def ap_status():
    path = _script_path()
    if not os.path.isfile(path):
        return {"error": "missing_script"}

    result = subprocess.run(
        ["sudo", "-n", path, "status"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,  # 5 second timeout to prevent hangs
    )

    if result.returncode != 0:
        return {"error": "status_failed", "stderr": result.stderr}

    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return {"error": "bad_json", "raw": result.stdout}


def ap_force(mode: str):
    """Set force mode: force-on, force-off, force-auto.

    This setting persists across reboots:
    - force-on: AP always on (even with good WiFi)
    - force-off: AP blocked (never starts, even with bad WiFi)
    - force-auto: AP starts/stops based on WiFi health (default)
    """
    if mode not in {"force-on", "force-off", "force-auto"}:
        raise ValueError("Invalid mode")

    path = _script_path()
    result = subprocess.run(
        ["sudo", "-n", path, mode],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,  # 10 second timeout for mode changes
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "ap_control failed")
    return True


def get_ap_config():
    """Read current AP SSID and password from config.yaml."""
    if not os.path.isfile(CONFIG_YAML):
        return {"ssid": "TeslaUSB", "passphrase": ""}

    try:
        with open(CONFIG_YAML, "r") as f:
            config = yaml.safe_load(f)

        ssid = config.get("offline_ap", {}).get("ssid", "TeslaUSB")
        passphrase = config.get("offline_ap", {}).get("passphrase", "")

        return {"ssid": ssid, "passphrase": passphrase}
    except Exception:
        return {"ssid": "TeslaUSB", "passphrase": ""}


def update_ap_config(ssid: str, passphrase: str):
    """Update AP SSID and passphrase in config.yaml and reload AP to apply changes."""
    # Validate inputs
    if not ssid or len(ssid) > 32:
        raise ValueError("SSID must be 1-32 characters")
    if passphrase and (len(passphrase) < 8 or len(passphrase) > 63):
        raise ValueError("Passphrase must be 8-63 characters (or empty for open network)")

    # Read current config
    try:
        with open(CONFIG_YAML, "r") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        raise RuntimeError(f"Failed to read config.yaml: {e}")

    # Update offline_ap section
    if "offline_ap" not in config:
        config["offline_ap"] = {}

    config["offline_ap"]["ssid"] = ssid
    config["offline_ap"]["passphrase"] = passphrase

    # Write config to temporary file first (atomic write)
    temp_file = CONFIG_YAML + ".tmp"
    try:
        with open(temp_file, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

        # Use sudo to move temp file to final location
        result = subprocess.run(
            ["sudo", "-n", "mv", temp_file, CONFIG_YAML],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            raise RuntimeError(f"Failed to write config.yaml: {result.stderr}")
    except Exception as e:
        # Clean up temp file if it exists
        if os.path.exists(temp_file):
            os.remove(temp_file)
        raise RuntimeError(f"Failed to update config.yaml: {e}")
    finally:
        # Ensure temp file is removed
        if os.path.exists(temp_file):
            os.remove(temp_file)

    # Restart wifi-monitor to reload config.yaml with new values
    result = subprocess.run(
        ["sudo", "-n", "systemctl", "restart", "wifi-monitor.service"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,  # 15 second timeout for service restart
    )

    if result.returncode != 0:
        raise RuntimeError(f"Failed to restart wifi-monitor: {result.stderr}")

    # Wait for wifi-monitor to stabilize
    subprocess.run(["sleep", "3"], check=False)

    # Check if AP is active NOW (after restart)
    status = ap_status()

    # If AP is active, reload it to apply new credentials
    if status.get("ap_active"):
        path = _script_path()
        result = subprocess.run(
            ["sudo", "-n", path, "reload"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,  # 10 second timeout for AP reload
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to reload AP: {result.stderr}")
        # Give it time to restart with new credentials
        subprocess.run(["sleep", "2"], check=False)

    return True
