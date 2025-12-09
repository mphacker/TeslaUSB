import json
import os
import subprocess

from config import GADGET_DIR


SCRIPT_NAME = "ap_control.sh"
CONFIG_FILE = os.path.join(GADGET_DIR, "scripts", "config.sh")


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
    )

    if result.returncode != 0:
        return {"error": "status_failed", "stderr": result.stderr}

    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return {"error": "bad_json", "raw": result.stdout}


def ap_force(mode: str):
    """Set force mode: force-on, force-off, force-auto."""
    if mode not in {"force-on", "force-off", "force-auto"}:
        raise ValueError("Invalid mode")

    path = _script_path()
    result = subprocess.run(
        ["sudo", "-n", path, mode],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "ap_control failed")
    return True


def get_ap_config():
    """Read current AP SSID and password from config.sh."""
    if not os.path.isfile(CONFIG_FILE):
        return {"ssid": "TeslaUSB", "passphrase": ""}
    
    ssid = "TeslaUSB"
    passphrase = ""
    
    try:
        with open(CONFIG_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("OFFLINE_AP_SSID="):
                    # Extract value before any comment
                    value = line.split("=", 1)[1].split("#")[0].strip().strip('"')
                    ssid = value
                elif line.startswith("OFFLINE_AP_PASSPHRASE="):
                    # Extract value before any comment
                    value = line.split("=", 1)[1].split("#")[0].strip().strip('"')
                    passphrase = value
    except Exception:
        pass
    
    return {"ssid": ssid, "passphrase": passphrase}


def update_ap_config(ssid: str, passphrase: str):
    """Update AP SSID and passphrase in config.sh and reload AP to apply changes."""
    # Validate inputs
    if not ssid or len(ssid) > 32:
        raise ValueError("SSID must be 1-32 characters")
    if passphrase and (len(passphrase) < 8 or len(passphrase) > 63):
        raise ValueError("Passphrase must be 8-63 characters (or empty for open network)")
    
    # Use sudo and sed to update config file
    result = subprocess.run(
        ["sudo", "-n", "sed", "-i", 
         f"s|^OFFLINE_AP_SSID=.*|OFFLINE_AP_SSID=\"{ssid}\"|",
         CONFIG_FILE],
        capture_output=True,
        text=True,
        check=False,
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Failed to update SSID in config: {result.stderr}")
    
    result = subprocess.run(
        ["sudo", "-n", "sed", "-i", 
         f"s|^OFFLINE_AP_PASSPHRASE=.*|OFFLINE_AP_PASSPHRASE=\"{passphrase}\"|",
         CONFIG_FILE],
        capture_output=True,
        text=True,
        check=False,
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Failed to update passphrase in config: {result.stderr}")
    
    # Restart wifi-monitor to reload config.sh with new values
    result = subprocess.run(
        ["sudo", "-n", "systemctl", "restart", "wifi-monitor.service"],
        capture_output=True,
        text=True,
        check=False,
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
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to reload AP: {result.stderr}")
        # Give it time to restart with new credentials
        subprocess.run(["sleep", "2"], check=False)
    
    return True
