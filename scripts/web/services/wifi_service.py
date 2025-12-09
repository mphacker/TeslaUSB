import json
import subprocess
import re


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


def update_wifi_credentials(ssid: str, password: str):
    """
    Update WiFi credentials using NetworkManager.
    
    This function:
    1. Validates input
    2. Creates or modifies a NetworkManager connection
    3. Activates the new connection
    4. Returns success/failure
    
    Note: In concurrent AP mode, the AP stays up during this process,
    so users don't lose access even if the new WiFi fails to connect.
    """
    # Validate inputs
    if not ssid or len(ssid) > 32:
        raise ValueError("SSID must be 1-32 characters")
    
    if password and (len(password) < 8 or len(password) > 63):
        raise ValueError("Password must be 8-63 characters (or empty for open network)")
    
    connection_name = f"WiFi-{ssid}"
    
    try:
        # Check if connection already exists
        check_result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME", "connection", "show"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        
        connection_exists = connection_name in check_result.stdout.splitlines()
        
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
            
            if result.returncode != 0:
                # Connection might have been created but activation failed
                # This is OK - it will retry automatically
                # Check if it's just an activation failure vs creation failure
                if "Error: Failed to add/activate new connection" in result.stderr:
                    # Connection was created, just failed to activate immediately
                    pass
                elif "Secrets were required" in result.stderr:
                    raise ValueError("Invalid password")
                else:
                    raise RuntimeError(f"Failed to create connection: {result.stderr}")
        
        # Try to activate the connection
        activate_result = subprocess.run(
            ["sudo", "-n", "nmcli", "connection", "up", connection_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        
        # Success is determined by whether we connected, not just whether command succeeded
        # NetworkManager might report failure even if connection is working
        if activate_result.returncode == 0:
            return {
                "success": True,
                "message": f"Successfully connected to {ssid}",
                "connection_name": connection_name,
            }
        else:
            # Check if we're actually connected despite the error
            current = get_current_wifi_connection()
            if current.get("connected") and current.get("current_ssid") == ssid:
                return {
                    "success": True,
                    "message": f"Connected to {ssid}",
                    "connection_name": connection_name,
                }
            else:
                return {
                    "success": False,
                    "message": f"Failed to connect to {ssid}. Please check credentials and ensure the network is in range.",
                    "error": activate_result.stderr,
                    "connection_name": connection_name,
                }
    
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "message": "Connection attempt timed out",
            "error": "timeout",
        }
    except ValueError as e:
        raise e
    except Exception as e:
        raise RuntimeError(f"Unexpected error updating WiFi: {str(e)}")


def get_available_networks():
    """Get list of available WiFi networks (for future use)."""
    try:
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
                
                # Skip duplicates and hidden networks
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
