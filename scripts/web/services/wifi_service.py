import json
import logging
import subprocess
import re
import os
from datetime import datetime

logger = logging.getLogger(__name__)

# File to store WiFi change history/status
WIFI_STATUS_FILE = "/tmp/teslausb_wifi_status.json"


def _save_wifi_status(status: dict):
    """Save WiFi status to a temp file for displaying on the settings page."""
    try:
        status["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(WIFI_STATUS_FILE, "w") as f:
            json.dump(status, f)
    except Exception:
        pass  # Best effort - don't fail if we can't save status


def get_wifi_status():
    """Get the last WiFi change status for display on the settings page."""
    try:
        if os.path.exists(WIFI_STATUS_FILE):
            with open(WIFI_STATUS_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def clear_wifi_status():
    """Clear the WiFi status file (called after user acknowledges the message)."""
    try:
        if os.path.exists(WIFI_STATUS_FILE):
            os.remove(WIFI_STATUS_FILE)
    except Exception:
        pass


def get_current_wifi_connection():
    """Get currently connected WiFi network information."""
    try:
        # Try to get active WiFi connection via NetworkManager
        result = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )

        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.split(":")
                if len(parts) >= 2 and parts[0] == "yes":
                    return {
                        "connected": True,
                        "current_ssid": parts[1] if len(parts) > 1 else "Unknown",
                        "signal": parts[2] if len(parts) > 2 else "0",
                    }

        # Fallback: check with iw
        result = subprocess.run(
            ["iw", "dev", "wlan0", "link"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )

        if result.returncode == 0 and "Connected to" in result.stdout:
            # Extract SSID from iw output
            ssid_match = re.search(r"SSID:\s*(.+)", result.stdout)
            ssid = ssid_match.group(1).strip() if ssid_match else "Unknown"
            return {
                "connected": True,
                "current_ssid": ssid,
                "signal": "Unknown",
            }

        return {
            "connected": False,
            "current_ssid": None,
            "signal": None,
        }
    except Exception as e:
        return {
            "connected": False,
            "current_ssid": None,
            "signal": None,
            "error": str(e),
        }


def _get_current_connection_name():
    """Get the name of the currently active WiFi connection."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE,STATE", "connection", "show", "--active"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.split(":")
                if len(parts) >= 3 and "wireless" in parts[1].lower() and "activated" in parts[2].lower():
                    return parts[0]
        return None
    except Exception:
        return None


def _activate_connection(connection_name: str, timeout: int = 30):
    """Try to activate a NetworkManager connection. Returns success boolean."""
    import time
    try:
        result = subprocess.run(
            ["sudo", "-n", "nmcli", "connection", "up", connection_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        if result.returncode == 0:
            # Give it a moment to stabilize
            time.sleep(3)
            current = get_current_wifi_connection()
            return current.get("connected", False)

        # Even if returncode != 0, wait and check if connection happened
        # NetworkManager can be finicky with reporting
        for _ in range(5):  # Check up to 5 times over 10 seconds
            time.sleep(2)
            current = get_current_wifi_connection()
            if current.get("connected", False):
                return True
        return False
    except Exception:
        return False


def _promote_new_connection(connection_name: str):
    """Set connection_name to priority 100 and decrement all other wireless connections by 10."""
    try:
        # Set new connection to highest priority
        subprocess.run(
            ["sudo", "-n", "nmcli", "connection", "modify", connection_name,
             "connection.autoconnect-priority", "100"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        # Get all wireless connections and decrement others
        result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE,AUTOCONNECT-PRIORITY", "connection", "show"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.split(":")
                if len(parts) >= 3 and "wireless" in parts[1].lower() and parts[0] != connection_name:
                    try:
                        old_pri = int(parts[2]) if parts[2] else 0
                    except ValueError:
                        old_pri = 0
                    new_pri = max(0, old_pri - 10)
                    subprocess.run(
                        ["sudo", "-n", "nmcli", "connection", "modify", parts[0],
                         "connection.autoconnect-priority", str(new_pri)],
                        capture_output=True, text=True, check=False, timeout=5,
                    )
    except Exception as e:
        logger.warning("Failed to promote connection priority: %s", e)


def _start_fallback_ap():
    """Start the fallback AP when WiFi connection fails."""
    try:
        from services.ap_service import ap_force
        ap_force("force-on")
        return True
    except Exception:
        return False


def update_wifi_credentials(ssid: str, password: str):
    """
    Update WiFi credentials using NetworkManager with failsafe mechanisms.

    This function:
    1. Stores the current working connection info
    2. Validates input
    3. Creates or modifies a NetworkManager connection
    4. Attempts to activate the new connection
    5. If connection fails, reverts to the previous connection
    6. If revert also fails, starts the fallback AP
    7. Saves status for display on the settings page

    Returns a dict with success status, message, and details about any failover actions.
    """
    # Validate inputs
    if not ssid or len(ssid) > 32:
        raise ValueError("SSID must be 1-32 characters")

    if password and (len(password) < 8 or len(password) > 63):
        raise ValueError("Password must be 8-63 characters (or empty for open network)")

    # Store the current working connection before making changes
    previous_connection = get_current_wifi_connection()
    previous_connection_name = _get_current_connection_name()
    previous_ssid = previous_connection.get("current_ssid") if previous_connection.get("connected") else None

    connection_name = f"WiFi-{ssid}"

    try:
        # Check for existing connection with same SSID (may have different name)
        existing_conn = None
        check_result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        if check_result.returncode == 0:
            for line in check_result.stdout.splitlines():
                parts = line.split(":")
                if len(parts) >= 2 and "wireless" in parts[1].lower():
                    ssid_check = subprocess.run(
                        ["nmcli", "-t", "-f", "802-11-wireless.ssid", "connection", "show", parts[0]],
                        capture_output=True, text=True, check=False, timeout=5,
                    )
                    if ssid_check.returncode == 0 and ssid in ssid_check.stdout:
                        existing_conn = parts[0]
                        break

        connection_exists = existing_conn is not None
        if existing_conn:
            connection_name = existing_conn

        initial_error = None  # Track initial errors for failsafe messages

        if connection_exists:
            # Modify existing connection
            if password:
                modify_cmd = [
                    "sudo", "-n", "nmcli", "connection", "modify", connection_name,
                    "wifi.ssid", ssid,
                    "wifi-sec.key-mgmt", "wpa-psk",
                    "wifi-sec.psk", password,
                ]
            else:
                # Open network
                modify_cmd = [
                    "sudo", "-n", "nmcli", "connection", "modify", connection_name,
                    "wifi.ssid", ssid,
                    "wifi-sec.key-mgmt", "none",
                ]

            result = subprocess.run(
                modify_cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )

            if result.returncode != 0:
                raise RuntimeError(f"Failed to modify connection: {result.stderr}")
        else:
            # Create new connection
            if password:
                add_cmd = [
                    "sudo", "-n", "nmcli", "device", "wifi", "connect", ssid,
                    "password", password,
                    "name", connection_name,
                ]
            else:
                # Open network
                add_cmd = [
                    "sudo", "-n", "nmcli", "device", "wifi", "connect", ssid,
                    "name", connection_name,
                ]

            result = subprocess.run(
                add_cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )

            # Store the initial connection attempt error for later use in failsafe
            if result.returncode != 0:
                initial_error = result.stderr

            if result.returncode != 0:
                # Connection might have been created but activation failed
                # Check if it's a critical creation failure (not just activation failure)
                if "Error: Failed to add/activate new connection" in result.stderr:
                    # Connection was created, just failed to activate immediately - continue to failsafe
                    pass
                elif "Secrets were required" in result.stderr:
                    # Wrong password - continue to failsafe handling instead of raising exception
                    pass
                elif "No network with SSID" in result.stderr:
                    # Network not found - this is a user error, raise it
                    raise ValueError(f"Network '{ssid}' not found")
                else:
                    # Other errors - continue to failsafe handling
                    pass

        # Try to activate the connection
        activate_result = subprocess.run(
            ["sudo", "-n", "nmcli", "connection", "up", connection_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

        # Wait a moment and check if we're actually connected
        import time
        time.sleep(3)
        current = get_current_wifi_connection()

        if current.get("connected") and current.get("current_ssid") == ssid:
            # Success! Set this as highest priority, decrement others
            _promote_new_connection(connection_name)
            status = {
                "success": True,
                "message": f"Successfully connected to '{ssid}'",
                "new_ssid": ssid,
                "previous_ssid": previous_ssid,
                "action": "connected",
            }
            _save_wifi_status(status)
            return status
        else:
            # New connection failed - wait a bit more for NetworkManager to potentially auto-reconnect
            # NetworkManager often auto-reconnects to previous network after a failed attempt
            for retry in range(5):  # Check up to 5 times over 10 seconds
                time.sleep(2)
                current = get_current_wifi_connection()
                current_connected_ssid = current.get("current_ssid") if current.get("connected") else None

                # Check if we've connected to the NEW network (late success)
                if current.get("connected") and current_connected_ssid == ssid:
                    _promote_new_connection(connection_name)
                    status = {
                        "success": True,
                        "message": f"Successfully connected to '{ssid}'",
                        "new_ssid": ssid,
                        "previous_ssid": previous_ssid,
                        "action": "connected",
                    }
                    _save_wifi_status(status)
                    return status

                # Check if NetworkManager auto-reconnected to the previous network
                if current.get("connected") and previous_ssid and current_connected_ssid == previous_ssid:
                    # We're back on the previous network
                    error_msg = initial_error if initial_error else (activate_result.stderr if activate_result.returncode != 0 else "Connection verification failed")
                    status = {
                        "success": False,
                        "message": f"Failed to connect to '{ssid}'. Reverted to previous network '{previous_ssid}'.",
                        "new_ssid": ssid,
                        "previous_ssid": previous_ssid,
                        "action": "reverted",
                        "error": error_msg,
                    }
                    _save_wifi_status(status)
                    return status

            # After all retries, check final state
            current = get_current_wifi_connection()
            current_connected_ssid = current.get("current_ssid") if current.get("connected") else None

            # Final check for auto-reconnection
            if current.get("connected") and previous_ssid and current_connected_ssid == previous_ssid:
                error_msg = initial_error if initial_error else (activate_result.stderr if activate_result.returncode != 0 else "Connection verification failed")
                status = {
                    "success": False,
                    "message": f"Failed to connect to '{ssid}'. Reverted to previous network '{previous_ssid}'.",
                    "new_ssid": ssid,
                    "previous_ssid": previous_ssid,
                    "action": "reverted",
                    "error": error_msg,
                }
                _save_wifi_status(status)
                return status

            # Try to manually reconnect to the previous network
            reverted = False
            ap_started = False

            if previous_connection_name and previous_ssid:
                if _activate_connection(previous_connection_name):
                    current = get_current_wifi_connection()
                    if current.get("connected"):
                        reverted = True

            if not reverted:
                # Could not revert - start the fallback AP
                ap_started = _start_fallback_ap()

            # Build the failure status
            error_msg = initial_error if initial_error else (activate_result.stderr if activate_result.returncode != 0 else "Connection verification failed")

            if reverted:
                status = {
                    "success": False,
                    "message": f"Failed to connect to '{ssid}'. Reverted to previous network '{previous_ssid}'.",
                    "new_ssid": ssid,
                    "previous_ssid": previous_ssid,
                    "action": "reverted",
                    "error": error_msg,
                }
            elif ap_started:
                status = {
                    "success": False,
                    "message": f"Failed to connect to '{ssid}' and could not restore previous connection. Fallback AP has been started for direct access.",
                    "new_ssid": ssid,
                    "previous_ssid": previous_ssid,
                    "action": "ap_started",
                    "error": error_msg,
                }
            else:
                status = {
                    "success": False,
                    "message": f"Failed to connect to '{ssid}'. Please connect to the fallback AP to reconfigure.",
                    "new_ssid": ssid,
                    "previous_ssid": previous_ssid,
                    "action": "failed",
                    "error": error_msg,
                }

            _save_wifi_status(status)
            return status

    except subprocess.TimeoutExpired:
        # Timeout - check if we're still connected (NetworkManager might have auto-reconnected)
        import time
        time.sleep(2)
        current = get_current_wifi_connection()
        reverted = False
        ap_started = False

        # Check if we're already back on the previous network
        if current.get("connected") and previous_ssid and current.get("current_ssid") == previous_ssid:
            reverted = True
        elif previous_connection_name:
            # Try to manually reconnect
            if _activate_connection(previous_connection_name, timeout=15):
                current = get_current_wifi_connection()
                if current.get("connected"):
                    reverted = True

        if not reverted:
            ap_started = _start_fallback_ap()

        if reverted:
            status = {
                "success": False,
                "message": f"Connection to '{ssid}' timed out. Reverted to previous network '{previous_ssid}'.",
                "new_ssid": ssid,
                "previous_ssid": previous_ssid,
                "action": "reverted",
                "error": "timeout",
            }
        elif ap_started:
            status = {
                "success": False,
                "message": f"Connection to '{ssid}' timed out and could not restore previous connection. Fallback AP has been started.",
                "new_ssid": ssid,
                "previous_ssid": previous_ssid,
                "action": "ap_started",
                "error": "timeout",
            }
        else:
            status = {
                "success": False,
                "message": f"Connection to '{ssid}' timed out.",
                "new_ssid": ssid,
                "previous_ssid": previous_ssid,
                "action": "failed",
                "error": "timeout",
            }

        _save_wifi_status(status)
        return status

    except ValueError as e:
        raise e
    except Exception as e:
        raise RuntimeError(f"Unexpected error updating WiFi: {str(e)}")


def get_saved_networks():
    """Get all saved WiFi networks with SSID, priority, signal, and active status.

    Returns list of dicts sorted by priority (highest first):
    [{"name": "WiFi-Trez_EXT", "ssid": "Trez_EXT", "priority": 100, "active": True, "signal": "67", "in_range": True}, ...]
    """
    try:
        # 1. Get all wireless connections with priority
        conn_result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE,AUTOCONNECT-PRIORITY", "connection", "show"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        if conn_result.returncode != 0:
            return []

        wireless_conns = []
        for line in conn_result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 3 and "wireless" in parts[1].lower():
                try:
                    priority = int(parts[2]) if parts[2] else 0
                except ValueError:
                    priority = 0
                wireless_conns.append({"name": parts[0], "priority": priority})

        if not wireless_conns:
            return []

        # 2. Get visible networks with signal strength
        scan_result = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi", "list"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        visible = {}
        if scan_result.returncode == 0:
            for line in scan_result.stdout.splitlines():
                parts = line.split(":")
                if len(parts) >= 3 and parts[1]:
                    ssid = parts[1]
                    signal = parts[2]
                    # Keep the strongest signal if duplicates
                    if ssid not in visible or int(signal or 0) > int(visible[ssid] or 0):
                        visible[ssid] = signal

        # 3. Get current active connection on wlan0
        active_result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        active_name = None
        if active_result.returncode == 0:
            for line in active_result.stdout.splitlines():
                parts = line.split(":")
                if len(parts) >= 2 and parts[1] == "wlan0":
                    active_name = parts[0]
                    break

        # 4. For each connection, get its SSID
        networks = []
        for conn in wireless_conns:
            ssid_result = subprocess.run(
                ["nmcli", "-t", "-f", "802-11-wireless.ssid", "connection", "show", conn["name"]],
                capture_output=True, text=True, check=False, timeout=5,
            )
            ssid = ""
            if ssid_result.returncode == 0:
                for line in ssid_result.stdout.splitlines():
                    if ":" in line:
                        ssid = line.split(":", 1)[1].strip()
                        break

            in_range = ssid in visible
            signal = visible.get(ssid, "0") if in_range else "0"

            networks.append({
                "name": conn["name"],
                "ssid": ssid or conn["name"],
                "priority": conn["priority"],
                "active": conn["name"] == active_name,
                "signal": signal,
                "in_range": in_range,
            })

        # 5. Sort by priority descending
        networks.sort(key=lambda n: n["priority"], reverse=True)
        return networks

    except Exception as e:
        logger.error("Error getting saved networks: %s", e)
        return []


def forget_network(connection_name: str) -> dict:
    """Remove a saved WiFi network.

    Safety guards:
    - Refuse to delete the only saved network
    - If deleting the active connection, connect to next-priority first
    - If all else fails, start AP fallback

    Returns dict with success, message, and any failover actions.
    """
    import time as _time

    # 1. Count total wireless connections
    saved = get_saved_networks()
    if len(saved) <= 1:
        return {"success": False, "message": "Cannot forget the only saved network"}

    # 2. Check if this is the active connection
    target = next((n for n in saved if n["name"] == connection_name), None)
    if target is None:
        return {"success": False, "message": f"Network '{connection_name}' not found"}

    is_active = target.get("active", False)

    # 3. If active, switch to next-priority network first
    if is_active:
        alternatives = [n for n in saved if n["name"] != connection_name]
        switched = False
        for alt in alternatives:
            if _activate_connection(alt["name"], timeout=15):
                switched = True
                break
            _time.sleep(1)

        if not switched:
            # Start AP fallback
            _start_fallback_ap()

    # 4. Delete the connection
    try:
        result = subprocess.run(
            ["sudo", "-n", "nmcli", "connection", "delete", connection_name],
            capture_output=True, text=True, check=False, timeout=10,
        )
        if result.returncode != 0:
            return {"success": False, "message": f"Failed to delete: {result.stderr.strip()}"}
    except subprocess.TimeoutExpired:
        return {"success": False, "message": "Delete command timed out"}

    msg = f"Network '{connection_name}' forgotten"
    if is_active:
        msg += " (switched to alternate network)"
    return {"success": True, "message": msg}


def reorder_networks(ordered_names: list) -> dict:
    """Set priorities based on list order (first = highest priority).

    Args:
        ordered_names: List of connection names in desired priority order.

    Returns dict with success and message.
    """
    errors = []
    for idx, name in enumerate(ordered_names):
        priority = 100 - (idx * 10)
        if priority < 0:
            priority = 0
        try:
            result = subprocess.run(
                ["sudo", "-n", "nmcli", "connection", "modify", name,
                 "connection.autoconnect-priority", str(priority)],
                capture_output=True, text=True, check=False, timeout=5,
            )
            if result.returncode != 0:
                errors.append(f"{name}: {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            errors.append(f"{name}: timed out")

    if errors:
        return {"success": False, "message": "Some networks failed: " + "; ".join(errors)}
    return {"success": True, "message": "Network priorities updated"}


def get_available_networks(rescan: bool = True):
    """
    Get list of available WiFi networks.

    Args:
        rescan: If True, trigger a new scan before listing (takes ~2-4 seconds)
                If False, return cached results quickly

    Returns:
        List of network dictionaries with 'ssid', 'signal', and 'secured' keys
    """
    try:
        if rescan:
            # Trigger a new scan - this makes nmcli actually look for networks
            # Use --rescan yes to force a fresh scan
            subprocess.run(
                ["sudo", "-n", "nmcli", "dev", "wifi", "rescan"],
                capture_output=True,
                check=False,
                timeout=10,
            )
            # Small delay to let scan complete
            import time
            time.sleep(1)

        # List available networks
        result = subprocess.run(
            ["sudo", "-n", "nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

        if result.returncode != 0:
            return []

        networks = []
        seen_ssids = set()

        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 2:
                ssid = parts[0].strip()
                signal = parts[1].strip() if len(parts) > 1 else "0"
                security = parts[2].strip() if len(parts) > 2 else ""

                # Skip duplicates and hidden networks (empty SSID)
                if ssid and ssid not in seen_ssids:
                    seen_ssids.add(ssid)
                    networks.append({
                        "ssid": ssid,
                        "signal": signal,
                        "secured": bool(security),
                    })

        # Sort by signal strength (descending)
        networks.sort(key=lambda x: int(x["signal"]) if x["signal"].isdigit() else 0, reverse=True)

        return networks
    except Exception:
        return []
