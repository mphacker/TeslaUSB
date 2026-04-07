"""
TeslaUSB Cloud rclone Configuration Service.

Wraps rclone's headless authorization flow for the web UI.
The user runs ``rclone authorize "<backend>"`` on a machine with a browser
and pastes the resulting token blob here.  We validate it, encrypt it via
the hardware-bound key, and persist it for the sync service to use.

No custom OAuth logic — rclone handles all provider-specific auth.
"""

import json
import logging
import os
import re
import subprocess
from typing import Dict, Optional, Tuple

from config import (
    GADGET_DIR,
    CLOUD_PROVIDER_CREDS_PATH,
)

logger = logging.getLogger(__name__)

# Remote name used in rclone config
RCLONE_REMOTE_NAME = "teslausb"

# Temporary rclone.conf lives on tmpfs (RAM) for security
_RCLONE_TMPFS_DIR = "/run/teslausb"
_RCLONE_CONF_PATH = os.path.join(_RCLONE_TMPFS_DIR, "rclone.conf")

# Patterns in rclone stderr that indicate the token is stale/revoked
_AUTH_ERROR_PATTERNS = (
    "invalid_grant",
    "token expired",
    "token has been expired",
    "token has been revoked",
    "couldn't fetch token",
    "failed to refresh token",
    "unauthorized",
    "401",
)


def is_auth_error(stderr: str) -> bool:
    """Check if rclone stderr indicates an authentication/token error."""
    lower = stderr.lower()
    return any(p in lower for p in _AUTH_ERROR_PATTERNS)

# ---------------------------------------------------------------------------
# Provider metadata (display labels and rclone backend types)
# ---------------------------------------------------------------------------

PROVIDERS = {
    "onedrive": {
        "label": "OneDrive",
        "rclone_type": "onedrive",
        "authorize_cmd": 'rclone authorize "onedrive"',
    },
    "google-drive": {
        "label": "Google Drive",
        "rclone_type": "drive",
        "authorize_cmd": 'rclone authorize "drive"',
    },
    "dropbox": {
        "label": "Dropbox",
        "rclone_type": "dropbox",
        "authorize_cmd": 'rclone authorize "dropbox"',
    },
}


# ---------------------------------------------------------------------------
# Token parsing
# ---------------------------------------------------------------------------

def parse_rclone_token(raw_input: str) -> Dict:
    """Parse a token blob pasted from ``rclone authorize`` output.

    Accepts either:
    - The raw JSON object: ``{"access_token":"...", ...}``
    - The full rclone output containing ``---> ... <---End paste``

    Returns the parsed token dict.
    Raises ValueError if the input cannot be parsed.
    """
    raw_input = raw_input.strip()

    # Try extracting from rclone's paste markers first
    match = re.search(r'--->\s*(.*?)\s*<---End paste', raw_input, re.DOTALL)
    if match:
        raw_input = match.group(1).strip()

    # Try parsing as JSON directly
    try:
        token = json.loads(raw_input)
    except (json.JSONDecodeError, ValueError) as e:
        raise ValueError(
            "Could not parse the token. Make sure you copied the entire "
            "output from 'rclone authorize', including the curly braces."
        ) from e

    if not isinstance(token, dict):
        raise ValueError("Token must be a JSON object.")

    # Validate minimum required fields
    if "access_token" not in token:
        raise ValueError(
            "Token is missing 'access_token'. Make sure you copied the "
            "complete output from 'rclone authorize'."
        )

    return token


# ---------------------------------------------------------------------------
# Credential storage (encrypted, hardware-bound)
# ---------------------------------------------------------------------------

def save_credentials(provider: str, token: dict) -> None:
    """Encrypt and persist rclone credentials.

    Args:
        provider: Provider key (e.g. 'onedrive').
        token: Parsed token dict from rclone authorize output.
    """
    from services.crypto_utils import derive_encryption_key
    from cryptography.fernet import Fernet

    rclone_type = PROVIDERS.get(provider, {}).get("rclone_type", provider)

    # Build rclone-compatible credential dict
    creds = {
        "type": rclone_type,
        "token": json.dumps(token),
    }

    # Add provider-specific fields rclone expects
    if provider == "onedrive":
        creds["drive_type"] = "personal"
        drive_id = _discover_onedrive_id(token)
        if drive_id:
            creds["drive_id"] = drive_id

    key = derive_encryption_key()
    fernet = Fernet(key)
    encrypted = fernet.encrypt(json.dumps(creds).encode())

    os.makedirs(os.path.dirname(CLOUD_PROVIDER_CREDS_PATH) or '.', exist_ok=True)
    tmp = CLOUD_PROVIDER_CREDS_PATH + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(encrypted)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CLOUD_PROVIDER_CREDS_PATH)

    logger.info("Cloud credentials saved for provider: %s", provider)


def _discover_onedrive_id(token: dict) -> Optional[str]:
    """Query Microsoft Graph API to get the user's default drive ID.

    This is required by rclone for OneDrive to function.
    """
    access_token = token.get("access_token", "")
    if not access_token:
        return None

    try:
        from urllib.request import Request, urlopen
        req = Request("https://graph.microsoft.com/v1.0/me/drive",
                      headers={"Authorization": f"Bearer {access_token}"})
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        drive_id = data.get("id", "")
        if drive_id:
            logger.info("Discovered OneDrive drive_id: %s", drive_id[:8] + "...")
        return drive_id
    except Exception as e:
        logger.warning("Could not discover OneDrive drive_id: %s", e)
        return None


def remove_credentials() -> None:
    """Remove stored cloud credentials."""
    try:
        os.remove(CLOUD_PROVIDER_CREDS_PATH)
        logger.info("Cloud credentials removed")
    except FileNotFoundError:
        pass


def get_connection_status() -> Dict:
    """Check current cloud provider connection status.

    Returns dict with 'connected' bool, 'provider' name, token expiry info,
    and any errors.
    """
    from config import CLOUD_ARCHIVE_PROVIDER

    if not CLOUD_ARCHIVE_PROVIDER:
        return {"connected": False, "provider": None, "error": "No provider configured"}

    if not os.path.isfile(CLOUD_PROVIDER_CREDS_PATH):
        return {"connected": False, "provider": CLOUD_ARCHIVE_PROVIDER,
                "error": "No credentials stored"}

    meta = PROVIDERS.get(CLOUD_ARCHIVE_PROVIDER, {})

    # Extract token expiry from stored credentials
    token_expiry = None
    creds = _load_creds()
    if creds and "token" in creds:
        try:
            token_dict = json.loads(creds["token"])
            token_expiry = token_dict.get("expiry")
        except (json.JSONDecodeError, ValueError):
            pass

    return {
        "connected": True,
        "provider": CLOUD_ARCHIVE_PROVIDER,
        "label": meta.get("label", CLOUD_ARCHIVE_PROVIDER),
        "token_expiry": token_expiry,
    }


# ---------------------------------------------------------------------------
# Connection test via rclone
# ---------------------------------------------------------------------------

def _write_temp_conf(creds: dict) -> str:
    """Write a temporary rclone.conf to tmpfs and return its path."""
    os.makedirs(_RCLONE_TMPFS_DIR, exist_ok=True)

    lines = [f"[{RCLONE_REMOTE_NAME}]"]
    for key, value in creds.items():
        lines.append(f"{key} = {value}")

    fd = os.open(_RCLONE_CONF_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, "\n".join(lines).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return _RCLONE_CONF_PATH


def _remove_temp_conf() -> None:
    """Delete the tmpfs rclone config if it exists."""
    try:
        os.remove(_RCLONE_CONF_PATH)
    except FileNotFoundError:
        pass


def _capture_refreshed_token(original_creds: dict) -> None:
    """Read the temp rclone.conf after a command and persist any token updates."""
    if not os.path.isfile(_RCLONE_CONF_PATH):
        logger.debug("capture_token: no temp conf file")
        return

    try:
        with open(_RCLONE_CONF_PATH, 'r') as f:
            new_conf = f.read()

        # Parse the token line from the rclone conf
        new_token_str = None
        for line in new_conf.splitlines():
            stripped = line.strip()
            if stripped.startswith("token = "):
                new_token_str = stripped[len("token = "):]
                break

        if not new_token_str:
            logger.debug("capture_token: no token line in conf")
            return

        old_token_str = original_creds.get("token", "")
        if new_token_str == old_token_str:
            logger.debug("capture_token: token unchanged")
            return  # No change

        # Token was refreshed — re-encrypt and persist
        logger.info("Detected refreshed token from rclone, persisting update")
        updated_creds = dict(original_creds)
        updated_creds["token"] = new_token_str

        from services.crypto_utils import derive_encryption_key
        from cryptography.fernet import Fernet

        key = derive_encryption_key()
        fernet = Fernet(key)
        encrypted = fernet.encrypt(json.dumps(updated_creds).encode())

        tmp = CLOUD_PROVIDER_CREDS_PATH + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(encrypted)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CLOUD_PROVIDER_CREDS_PATH)
    except Exception as e:
        logger.warning("Failed to capture refreshed token: %s", e)


def _load_creds() -> dict:
    """Load and decrypt stored credentials. Returns empty dict on failure."""
    if not os.path.isfile(CLOUD_PROVIDER_CREDS_PATH):
        return {}
    try:
        from services.crypto_utils import derive_encryption_key
        from cryptography.fernet import Fernet

        key = derive_encryption_key()
        fernet = Fernet(key)

        with open(CLOUD_PROVIDER_CREDS_PATH, 'rb') as f:
            encrypted = f.read()

        decrypted = fernet.decrypt(encrypted).decode()
        creds = json.loads(decrypted)
        return creds if isinstance(creds, dict) else {}
    except Exception as e:
        logger.error("Failed to load cloud credentials: %s", e)
        return {}


def test_connection() -> Tuple[bool, str]:
    """Test the cloud connection using stored credentials.

    Returns (success: bool, message: str).
    If the token is stale, the message starts with "AUTH_ERROR:" so the
    UI can offer re-authorization.
    """
    creds = _load_creds()
    if not creds:
        return False, "No credentials configured. Please connect a provider first."

    try:
        conf_path = _write_temp_conf(creds)
        result = subprocess.run(
            ["rclone", "lsd", "--config", conf_path, f"{RCLONE_REMOTE_NAME}:"],
            capture_output=True, text=True, timeout=30,
        )
        _capture_refreshed_token(creds)
        if result.returncode == 0:
            return True, "Connection successful."
        err = result.stderr.strip() or "Connection failed."
        if is_auth_error(err):
            logger.warning("Cloud auth error detected: %s", err[:200])
            return False, ("AUTH_ERROR: Your cloud authorization has expired. "
                           "Please disconnect and reconnect with a new token.")
        return False, err
    except subprocess.TimeoutExpired:
        return False, "Connection timed out after 30 seconds."
    except Exception as e:
        logger.exception("Connection test error")
        return False, str(e)
    finally:
        _remove_temp_conf()


def get_storage_usage() -> Dict:
    """Get cloud storage quota/usage via rclone about.

    Returns dict with 'total', 'used', 'free' (in bytes), or empty on failure.
    """
    creds = _load_creds()
    if not creds:
        return {}

    try:
        conf_path = _write_temp_conf(creds)
        result = subprocess.run(
            ["rclone", "about", "--config", conf_path,
             f"{RCLONE_REMOTE_NAME}:", "--json"],
            capture_output=True, text=True, timeout=30,
        )
        _capture_refreshed_token(creds)
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
        return {}
    except Exception as e:
        logger.warning("Storage usage check failed: %s", e)
        return {}
    finally:
        _remove_temp_conf()

def list_folders(path: str = "") -> Tuple[bool, object]:
    """List folders at the given remote path.

    Returns (success, data) where data is a list of folder dicts on success
    or an error string on failure.  Each folder dict has 'name' and 'path'.
    """
    creds = _load_creds()
    if not creds:
        return False, "No credentials configured."

    remote_path = f"{RCLONE_REMOTE_NAME}:{path}"
    try:
        conf_path = _write_temp_conf(creds)
        result = subprocess.run(
            ["rclone", "lsjson", "--config", conf_path,
             "--dirs-only", "--no-modtime", remote_path],
            capture_output=True, text=True, timeout=30,
        )
        _capture_refreshed_token(creds)
        if result.returncode != 0:
            err = result.stderr.strip()
            if "directory not found" in err.lower():
                return True, []
            if is_auth_error(err):
                return False, ("AUTH_ERROR: Your cloud authorization has expired. "
                               "Please disconnect and reconnect with a new token.")
            return False, err or "Failed to list folders."

        items = json.loads(result.stdout) if result.stdout.strip() else []
        folders = []
        for item in items:
            name = item.get("Name", "")
            if name:
                folder_path = f"{path}/{name}".lstrip("/")
                folders.append({"name": name, "path": folder_path})
        folders.sort(key=lambda f: f["name"].lower())
        return True, folders
    except subprocess.TimeoutExpired:
        return False, "Request timed out."
    except json.JSONDecodeError:
        return False, "Invalid response from cloud provider."
    except Exception as e:
        logger.exception("Folder listing error")
        return False, str(e)
    finally:
        _remove_temp_conf()


def create_folder(path: str) -> Tuple[bool, str]:
    """Create a folder at the given remote path.

    Returns (success, message).
    """
    if not path or not path.strip("/"):
        return False, "Folder path is required."

    creds = _load_creds()
    if not creds:
        return False, "No credentials configured."

    remote_path = f"{RCLONE_REMOTE_NAME}:{path}"
    try:
        conf_path = _write_temp_conf(creds)
        result = subprocess.run(
            ["rclone", "mkdir", "--config", conf_path, remote_path],
            capture_output=True, text=True, timeout=30,
        )
        _capture_refreshed_token(creds)
        if result.returncode == 0:
            logger.info("Created cloud folder: %s", path)
            return True, f"Created folder: {path}"
        return False, result.stderr.strip() or "Failed to create folder."
    except subprocess.TimeoutExpired:
        return False, "Request timed out."
    except Exception as e:
        logger.exception("Folder creation error")
        return False, str(e)
    finally:
        _remove_temp_conf()


# ---------------------------------------------------------------------------
# Single-file archive (for "Archive to Cloud" from video panel)
# ---------------------------------------------------------------------------

import threading
import time as _time

_archive_lock = threading.Lock()
_archive_status: Dict = {
    "running": False,
    "event_name": "",
    "folder": "",
    "file_count": 0,
    "files_done": 0,
    "current_file": "",
    "total_size": 0,
    "bytes_done": 0,       # Actual bytes of completed files
    "started_at": None,
    "error": None,
    "completed": False,
}
_archive_cancel = threading.Event()


def archive_event(folder: str, event_name: str, teslacam_base: str) -> Tuple[bool, str]:
    """Archive an entire event (all camera angles) to the cloud.

    Uses ``rclone copy`` which preserves directory structure, creating
    the folder hierarchy on the remote automatically.

    Args:
        folder: TeslaCam subfolder (e.g. 'SentryClips', 'RecentClips').
        event_name: Event folder or session name.
        teslacam_base: Base TeslaCam directory.

    Returns (success, message).
    """
    global _archive_status

    with _archive_lock:
        if _archive_status["running"]:
            return False, "Another archive is already in progress."

    # Don't start if a bulk sync is running (shared network/resource constraint)
    try:
        from services.cloud_archive_service import get_sync_status
        if get_sync_status().get("running"):
            return False, "A cloud sync is in progress. Please wait for it to finish."
    except Exception:
        pass

    creds = _load_creds()
    if not creds:
        return False, "No cloud provider configured."

    # Refresh the RO mount to see latest files (exFAT cache may be stale)
    try:
        from services.mapping_service import _refresh_ro_mount
        _refresh_ro_mount(teslacam_base)
    except Exception:
        pass

    # Determine local path and collect files
    event_dir = os.path.join(teslacam_base, folder, event_name)
    if os.path.isdir(event_dir):
        # Event-based structure (SentryClips, SavedClips)
        local_path = event_dir
        files = [f for f in os.listdir(event_dir)
                 if f.lower().endswith(('.mp4', '.ts'))]
        total_size = sum(os.path.getsize(os.path.join(event_dir, f))
                         for f in files)
    else:
        # Flat structure (RecentClips) — find matching session files
        folder_dir = os.path.join(teslacam_base, folder)
        files = [f for f in os.listdir(folder_dir)
                 if f.startswith(event_name) and f.lower().endswith(('.mp4', '.ts'))]
        if not files:
            return False, "No video files found for this event."
        total_size = sum(os.path.getsize(os.path.join(folder_dir, f))
                         for f in files)
        local_path = folder_dir

    if not files:
        return False, "No video files found."

    # Relative path for cloud destination: folder/event_name/
    rel_path = f"{folder}/{event_name}"

    with _archive_lock:
        _archive_cancel.clear()
        _archive_status.update({
            "running": True,
            "event_name": event_name,
            "folder": folder,
            "file_count": len(files),
            "files_done": 0,
            "current_file": "",
            "total_size": total_size,
            "bytes_done": 0,
            "started_at": _time.time(),
            "error": None,
            "completed": False,
        })

    thread = threading.Thread(
        target=_archive_worker,
        args=(local_path, rel_path, files, total_size, creds,
              os.path.isdir(event_dir)),
        daemon=True,
    )
    thread.start()
    return True, f"Archiving {len(files)} files from {event_name}..."


def _archive_worker(local_path: str, rel_path: str, files: list,
                    total_size: int, creds: dict, is_event_dir: bool):
    """Background thread for event archive."""
    global _archive_status

    try:
        from config import CLOUD_ARCHIVE_REMOTE_PATH, CLOUD_ARCHIVE_MAX_UPLOAD_MBPS

        logger.info("Archive starting: %s (%d files, %d bytes, is_event_dir=%s)",
                     rel_path, len(files), total_size, is_event_dir)

        conf_path = _write_temp_conf(creds)
        remote_base = f"{RCLONE_REMOTE_NAME}:{CLOUD_ARCHIVE_REMOTE_PATH}"
        max_mbps = CLOUD_ARCHIVE_MAX_UPLOAD_MBPS

        # Memory-constrained flags for Pi Zero 2W (512MB RAM)
        _mem_flags = [
            "--buffer-size", "0",
            "--transfers", "1",
            "--checkers", "1",
            "--low-level-retries", "3",
        ]

        # Force a token refresh before uploading — rclone about writes
        # the refreshed token back to the conf file automatically
        logger.info("Archive: refreshing token before upload...")
        about_result = subprocess.run(
            ["rclone", "about", "--config", conf_path,
             f"{RCLONE_REMOTE_NAME}:", "--json"],
            capture_output=True, text=True, timeout=30,
        )
        if about_result.returncode != 0:
            logger.warning("Archive: token refresh failed: %s",
                          about_result.stderr.strip()[:200])
        else:
            logger.info("Archive: token refresh OK")
        # Persist the refreshed token to encrypted store (for next time)
        # but do NOT re-write the conf — rclone already updated it
        _capture_refreshed_token(creds)

        if is_event_dir:
            remote_dest = f"{remote_base}/{rel_path}"
            logger.info("Archive: rclone copy %s → %s", local_path, remote_dest)
            result = subprocess.run(
                [
                    "rclone", "copy",
                    "--config", conf_path,
                    "--bwlimit", f"{max_mbps}M",
                    "--stats", "0",
                    "--log-level", "ERROR",
                    *_mem_flags,
                    local_path,
                    remote_dest,
                ],
                capture_output=True, text=True, timeout=3600,
            )
            logger.info("Archive: rclone copy exit=%d", result.returncode)
            if result.stderr.strip():
                logger.warning("Archive: rclone stderr: %s", result.stderr.strip()[:500])
        else:
            remote_folder = f"{remote_base}/{os.path.dirname(rel_path)}"
            all_ok = True
            for i, f in enumerate(files):
                if _archive_cancel.is_set():
                    logger.info("Archive: cancelled at file %d/%d", i, len(files))
                    break
                src = os.path.join(local_path, f)
                dst = f"{remote_folder}/{f}"
                src_size = os.path.getsize(src) if os.path.isfile(src) else 0
                _archive_status["current_file"] = f
                logger.info("Archive: [%d/%d] %s (%d bytes)",
                           i + 1, len(files), f, src_size)
                r = subprocess.run(
                    [
                        "rclone", "copyto",
                        "--config", conf_path,
                        "--bwlimit", f"{max_mbps}M",
                        "--stats", "0",
                        "--log-level", "ERROR",
                        *_mem_flags,
                        src, dst,
                    ],
                    capture_output=True, text=True, timeout=3600,
                )
                if r.returncode == 0:
                    logger.info("Archive: [%d/%d] %s OK", i + 1, len(files), f)
                    _archive_status["files_done"] = i + 1
                    _archive_status["bytes_done"] = _archive_status.get("bytes_done", 0) + src_size
                else:
                    all_ok = False
                    logger.error("Archive: [%d/%d] %s FAILED (exit=%d): %s",
                                i + 1, len(files), f, r.returncode,
                                r.stderr.strip()[:300])

                # Persist any token refresh (don't re-write conf — rclone keeps it fresh)
                _capture_refreshed_token(creds)

            result = type('R', (), {'returncode': 0 if all_ok else 1,
                                     'stderr': '' if all_ok else 'Some files failed to copy'})()

        _capture_refreshed_token(creds)

        if _archive_cancel.is_set():
            _archive_status.update({"running": False, "error": "Cancelled"})
            logger.info("Archive: cancelled")
            return

        if result.returncode == 0:
            _archive_status.update({
                "running": False,
                "completed": True,
                "bytes_done": total_size,
            })
            logger.info("Archive COMPLETE: %s (%d files, %d bytes)",
                        rel_path, len(files), total_size)
        else:
            err = result.stderr.strip()[:300]
            _archive_status.update({
                "running": False,
                "error": err if not is_auth_error(err) else
                    "Authorization expired. Please reconnect your cloud provider.",
            })
            logger.error("Archive failed for %s: %s", rel_path, err[:200])
    except subprocess.TimeoutExpired:
        _archive_status.update({
            "running": False, "error": "Upload timed out (1 hour limit).",
        })
    except Exception as e:
        _archive_status.update({"running": False, "error": str(e)[:200]})
        logger.exception("Archive worker error")
    finally:
        # Always ensure running is cleared even if an unexpected error occurred
        if _archive_status.get("running"):
            _archive_status["running"] = False
        _remove_temp_conf()


def get_archive_status() -> Dict:
    """Return current archive status with real file-level progress."""
    status = dict(_archive_status)

    # Calculate ETA from actual completed file throughput
    if status.get("running") and status.get("started_at") and status.get("bytes_done", 0) > 0:
        elapsed = _time.time() - status["started_at"]
        bps = status["bytes_done"] / elapsed if elapsed > 0 else 0
        remaining = status.get("total_size", 0) - status.get("bytes_done", 0)
        status["eta_seconds"] = int(remaining / bps) if bps > 0 and remaining > 0 else 0
    elif status.get("running"):
        status["eta_seconds"] = None  # Not enough data yet
    else:
        status["eta_seconds"] = None

    # For API compatibility
    status["bytes_transferred"] = status.get("bytes_done", 0)

    return status


def cancel_archive() -> Tuple[bool, str]:
    """Cancel an in-progress archive."""
    if not _archive_status.get("running"):
        return False, "No archive in progress."
    _archive_cancel.set()
    return True, "Archive cancellation requested."
