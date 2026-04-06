"""
Tesla Fleet API Client for TeslaUSB Cloud Archive.

Handles OAuth authentication, encrypted token storage, vehicle keep-awake
during sync, API budget tracking, and bonus vehicle features (climate,
sentry mode).

Security Model (6 layers):
1. Hardware-bound encryption — tokens encrypted with key derived from
   Pi serial number + machine-id + optional PIN via PBKDF2 (600k iterations).
2. Memory-only access token — access_token is NEVER written to disk.
3. Encrypted refresh token — stored in TESLA_TOKENS_PATH with Fernet.
4. Secure wipe — token files overwritten with random data before deletion.
5. Audit logging — every API call logged with timestamp, endpoint, result.
6. Budget limits — monthly spending caps prevent runaway API costs.

Designed for Pi Zero 2 W: minimal memory footprint, timeouts on all
network calls, graceful degradation on network failure.
"""

import base64
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

# Lazy imports for optional heavy dependencies
_fernet_cls = None
_requests = None


def _get_fernet():
    """Lazy-import cryptography to avoid startup cost."""
    global _fernet_cls
    if _fernet_cls is None:
        from cryptography.fernet import Fernet
        _fernet_cls = Fernet
    return _fernet_cls


def _get_requests():
    """Lazy-import requests to avoid startup cost."""
    global _requests
    if _requests is None:
        import requests as _req
        _requests = _req
    return _requests


# ---------------------------------------------------------------------------
# Configuration (imported from config.py)
# ---------------------------------------------------------------------------

from config import (
    GADGET_DIR,
    TESLA_API_CLIENT_ID,
    TESLA_API_CLIENT_SECRET,
    TESLA_API_KEEP_AWAKE_METHOD,
    TESLA_API_LOW_BATTERY_THRESHOLD,
    TESLA_API_MAX_AWAKE_MINUTES,
    TESLA_API_MONTHLY_BUDGET,
    TESLA_API_WAKE_INTERVAL,
    TESLA_API_DB_PATH,
    TESLA_TOKENS_PATH,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TESLA_AUTH_URL = "https://auth.tesla.com/oauth2/v3/authorize"
TESLA_TOKEN_URL = "https://auth.tesla.com/oauth2/v3/token"
TESLA_FLEET_BASE = "https://fleet-api.prd.na.vn.cloud.tesla.com"
TESLA_SCOPES = "openid offline_access vehicle_device_data vehicle_location vehicle_cmds"

API_TIMEOUT = 10  # seconds
API_MAX_RETRIES = 3

# Cost per API call type (USD)
API_COSTS: Dict[str, float] = {
    'wake': 0.02,
    'command': 0.001,
    'data': 0.002,
}

# Budget thresholds (USD)
BUDGET_DISABLE_KEEPAWAKE = 8.00
BUDGET_DISABLE_NONESSENTIAL = 9.50

# ---------------------------------------------------------------------------
# Database Schema
# ---------------------------------------------------------------------------

_SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS module_versions (
    module TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS tesla_api_budget (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL,
    call_type TEXT NOT NULL,
    call_count INTEGER DEFAULT 0,
    estimated_cost REAL DEFAULT 0.0,
    UNIQUE(month, call_type)
);

CREATE TABLE IF NOT EXISTS tesla_api_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    method TEXT,
    success INTEGER,
    source TEXT,
    error_msg TEXT
);

CREATE INDEX IF NOT EXISTS idx_budget_month ON tesla_api_budget(month);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON tesla_api_audit(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_endpoint ON tesla_api_audit(endpoint);
"""

# ---------------------------------------------------------------------------
# In-memory state (never written to disk)
# ---------------------------------------------------------------------------

_ACCESS_TOKEN_CACHE: Optional[str] = None
_REFRESH_TOKEN_CACHE: Optional[str] = None
_TOKEN_EXPIRES_AT: Optional[float] = None
_KEY_CACHE: Optional[bytes] = None
_PIN_REQUIRED: Optional[bool] = None

# Keep-awake state
_keep_awake_thread: Optional[threading.Thread] = None
_keep_awake_cancel: Optional[threading.Event] = None


# ---------------------------------------------------------------------------
# Database Initialization
# ---------------------------------------------------------------------------

def _check_db_integrity(db_path: str) -> bool:
    """Return True if database is healthy, False if corrupt or unreadable."""
    if not os.path.exists(db_path):
        return True
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        return result is not None and result[0] == "ok"
    except Exception as exc:
        logger.warning("Integrity check failed for %s: %s", db_path, exc)
        return False


def _handle_corrupt_db(db_path: str) -> None:
    """Rename a corrupt database aside so it can be rebuilt fresh."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    corrupt_path = f"{db_path}.corrupt.{ts}"
    try:
        os.rename(db_path, corrupt_path)
        logger.warning("Corrupt Tesla API DB renamed to %s — rebuilding", corrupt_path)
    except OSError as exc:
        logger.error("Failed to rename corrupt DB: %s — deleting", exc)
        try:
            os.remove(db_path)
        except OSError:
            pass
    for suffix in ("-wal", "-shm"):
        try:
            wal = db_path + suffix
            if os.path.exists(wal):
                os.remove(wal)
        except OSError:
            pass


def _init_tesla_tables(db_path: str) -> sqlite3.Connection:
    """
    Initialize Tesla API database tables with migration-safe versioning.

    Runs an integrity check first; if the database is corrupt it is renamed
    aside and rebuilt from scratch.  Budget/audit history loss is acceptable
    versus a dead service.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        sqlite3.Connection: Open database connection with WAL mode.
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    if not _check_db_integrity(db_path):
        _handle_corrupt_db(db_path)

    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # Check current module version
    try:
        row = conn.execute(
            "SELECT version FROM module_versions WHERE module = 'tesla_api'"
        ).fetchone()
        current = row['version'] if row else 0
    except sqlite3.OperationalError:
        current = 0

    if current < _SCHEMA_VERSION:
        conn.executescript(_SCHEMA_SQL)
        conn.execute(
            "INSERT OR REPLACE INTO module_versions (module, version, updated_at) "
            "VALUES ('tesla_api', ?, ?)",
            (_SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        logger.info(
            "Tesla API tables initialized (v%d) at %s", _SCHEMA_VERSION, db_path
        )

    return conn


# ---------------------------------------------------------------------------
# Hardware-Bound Encryption (Layer 1)
# ---------------------------------------------------------------------------

def _get_pi_serial() -> str:
    """Read Pi SoC serial number (hardware-bound, not on SD card)."""
    try:
        with open('/proc/cpuinfo', 'r') as f:
            for line in f:
                if line.startswith('Serial'):
                    return line.split(':')[1].strip()
    except Exception:
        pass
    return 'unknown-serial'


def _get_machine_id() -> str:
    """Read machine-id (unique per OS install)."""
    try:
        with open('/etc/machine-id', 'r') as f:
            return f.read().strip()
    except Exception:
        return 'unknown-machine'


def derive_encryption_key(pin: str = '') -> bytes:
    """
    Derive a Fernet encryption key from hardware identity + optional PIN.

    Key material combines: optional PIN, Pi serial number, and machine-id.
    Uses PBKDF2-HMAC-SHA256 with 600,000 iterations and a persistent
    random salt stored alongside the token file.

    Args:
        pin: Optional user PIN for additional security.

    Returns:
        bytes: URL-safe base64-encoded 32-byte key suitable for Fernet.
    """
    global _KEY_CACHE

    serial = _get_pi_serial()
    machine_id = _get_machine_id()
    key_material = f"{pin}:{serial}:{machine_id}".encode()

    salt_path = os.path.join(os.path.dirname(TESLA_TOKENS_PATH), 'tesla_salt.bin')
    if os.path.exists(salt_path):
        with open(salt_path, 'rb') as f:
            salt = f.read()
    else:
        salt = os.urandom(16)
        with open(salt_path, 'wb') as f:
            f.write(salt)
            f.flush()
            os.fsync(f.fileno())

    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=600_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(key_material))
    _KEY_CACHE = key
    return key


# ---------------------------------------------------------------------------
# Secure Wipe (Layer 4)
# ---------------------------------------------------------------------------

def secure_wipe(filepath: str) -> None:
    """
    Overwrite file with random data before deleting.

    Provides defense-in-depth against forensic recovery of token data
    from the SD card. Writes random bytes, fsyncs, then removes.

    Args:
        filepath: Path to the file to securely delete.
    """
    if not os.path.exists(filepath):
        return

    try:
        size = os.path.getsize(filepath)
        with open(filepath, 'wb') as f:
            f.write(os.urandom(size))
            f.flush()
            os.fsync(f.fileno())
        os.remove(filepath)
        logger.info("Securely wiped: %s", filepath)
    except Exception as e:
        logger.error("Failed to securely wipe %s: %s", filepath, e)
        try:
            os.remove(filepath)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Token Management (Layers 2 & 3)
# ---------------------------------------------------------------------------

def save_tokens(
    access_token: str,
    refresh_token: str,
    expires_at: float,
    pin: str = '',
) -> bool:
    """
    Save tokens: access token in memory only, refresh token encrypted on disk.

    The access token is NEVER written to disk — it exists only in process
    memory and is lost on reboot (by design). The refresh token is encrypted
    with a hardware-bound Fernet key and written atomically with fsync.

    Args:
        access_token: OAuth access token (kept in memory only).
        refresh_token: OAuth refresh token (encrypted to disk).
        expires_at: Unix timestamp when access token expires.
        pin: Optional PIN for key derivation.

    Returns:
        bool: True if tokens were saved successfully.
    """
    global _ACCESS_TOKEN_CACHE, _REFRESH_TOKEN_CACHE, _TOKEN_EXPIRES_AT, _PIN_REQUIRED

    try:
        key = derive_encryption_key(pin)
        Fernet = _get_fernet()
        f = Fernet(key)

        # Encrypt refresh token + metadata (access token NOT included)
        token_data = json.dumps({
            'refresh_token': refresh_token,
            'expires_at': expires_at,
            'saved_at': time.time(),
            'pin_protected': bool(pin),
        }).encode()
        encrypted = f.encrypt(token_data)

        # Atomic write with fsync
        token_dir = os.path.dirname(TESLA_TOKENS_PATH)
        os.makedirs(token_dir, exist_ok=True)

        tmp_path = TESLA_TOKENS_PATH + '.tmp'
        with open(tmp_path, 'wb') as tf:
            tf.write(encrypted)
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(tmp_path, TESLA_TOKENS_PATH)

        # Cache in memory
        _ACCESS_TOKEN_CACHE = access_token
        _REFRESH_TOKEN_CACHE = refresh_token
        _TOKEN_EXPIRES_AT = expires_at
        _PIN_REQUIRED = bool(pin)

        logger.info("Tesla tokens saved (PIN-protected: %s)", bool(pin))
        return True

    except Exception as e:
        logger.error("Failed to save Tesla tokens: %s", e, exc_info=True)
        return False


def load_tokens(pin: str = '') -> Optional[Dict[str, Any]]:
    """
    Load tokens: return cached access token or decrypt refresh token from disk.

    If the access token is still cached in memory and not expired, it is
    returned directly. Otherwise only the refresh token is available
    (the access token must be refreshed via the OAuth flow).

    Args:
        pin: PIN used during token encryption (must match).

    Returns:
        dict with {access_token, refresh_token, expires_at}, or None
        if no tokens exist or decryption fails.
    """
    global _ACCESS_TOKEN_CACHE, _REFRESH_TOKEN_CACHE, _TOKEN_EXPIRES_AT, _PIN_REQUIRED

    # Return memory cache if access token is still valid
    if _ACCESS_TOKEN_CACHE and _TOKEN_EXPIRES_AT:
        if time.time() < _TOKEN_EXPIRES_AT:
            return {
                'access_token': _ACCESS_TOKEN_CACHE,
                'refresh_token': _REFRESH_TOKEN_CACHE,
                'expires_at': _TOKEN_EXPIRES_AT,
            }

    if not os.path.exists(TESLA_TOKENS_PATH):
        return None

    try:
        key = derive_encryption_key(pin)
        Fernet = _get_fernet()
        f = Fernet(key)

        with open(TESLA_TOKENS_PATH, 'rb') as tf:
            encrypted = tf.read()

        decrypted = json.loads(f.decrypt(encrypted))
        _REFRESH_TOKEN_CACHE = decrypted['refresh_token']
        _TOKEN_EXPIRES_AT = decrypted.get('expires_at', 0)
        _PIN_REQUIRED = decrypted.get('pin_protected', False)

        # Access token is NOT on disk — may be None after reboot
        return {
            'access_token': _ACCESS_TOKEN_CACHE,
            'refresh_token': _REFRESH_TOKEN_CACHE,
            'expires_at': _TOKEN_EXPIRES_AT,
        }

    except Exception as e:
        logger.error("Failed to load Tesla tokens: %s", e)
        return None


def clear_tokens() -> None:
    """
    Securely destroy all Tesla tokens.

    Wipes the encrypted token file and salt with random data, then clears
    all in-memory caches. After this call the user must re-authenticate.
    """
    global _ACCESS_TOKEN_CACHE, _REFRESH_TOKEN_CACHE, _TOKEN_EXPIRES_AT
    global _KEY_CACHE, _PIN_REQUIRED

    secure_wipe(TESLA_TOKENS_PATH)
    salt_path = os.path.join(os.path.dirname(TESLA_TOKENS_PATH), 'tesla_salt.bin')
    secure_wipe(salt_path)

    _ACCESS_TOKEN_CACHE = None
    _REFRESH_TOKEN_CACHE = None
    _TOKEN_EXPIRES_AT = None
    _KEY_CACHE = None
    _PIN_REQUIRED = None

    logger.info("Tesla tokens cleared and wiped")


def is_connected() -> bool:
    """
    Check if valid Tesla API tokens exist.

    Returns True if either the access token is cached in memory or an
    encrypted token file exists on disk (may need PIN to unlock).
    """
    if _ACCESS_TOKEN_CACHE and _TOKEN_EXPIRES_AT:
        return True
    return os.path.exists(TESLA_TOKENS_PATH)


def needs_pin_unlock() -> bool:
    """
    Check if tokens are PIN-protected but not yet unlocked this boot.

    Returns True when the encrypted token file exists and is PIN-protected
    but the access/refresh tokens haven't been decrypted into memory yet.
    """
    if _ACCESS_TOKEN_CACHE is not None:
        return False
    if not os.path.exists(TESLA_TOKENS_PATH):
        return False
    if _PIN_REQUIRED is True:
        return True
    # Haven't tried loading yet — assume PIN may be needed
    if _PIN_REQUIRED is None and _REFRESH_TOKEN_CACHE is None:
        return True
    return False


# ---------------------------------------------------------------------------
# OAuth Flow
# ---------------------------------------------------------------------------

def get_auth_url(redirect_uri: str) -> str:
    """
    Build Tesla OAuth authorization URL.

    Args:
        redirect_uri: Callback URL after user authorizes (must match
            Tesla developer app configuration).

    Returns:
        str: Full authorization URL to redirect the user to.
    """
    params = {
        'client_id': TESLA_API_CLIENT_ID,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': TESLA_SCOPES,
        'state': base64.urlsafe_b64encode(os.urandom(16)).decode(),
    }
    return f"{TESLA_AUTH_URL}?{urlencode(params)}"


def exchange_code(
    code: str,
    redirect_uri: str,
    pin: str = '',
) -> Optional[Dict[str, Any]]:
    """
    Exchange OAuth authorization code for access and refresh tokens.

    Makes a POST to Tesla's token endpoint, then encrypts and saves the
    tokens. The access token is cached in memory; the refresh token is
    encrypted to disk.

    Args:
        code: Authorization code from Tesla OAuth callback.
        redirect_uri: Must match the redirect_uri used in get_auth_url.
        pin: Optional PIN for encrypting the refresh token.

    Returns:
        dict with {access_token, refresh_token, expires_at} on success,
        None on failure.
    """
    requests = _get_requests()

    # Read credentials fresh from config (may have been saved at runtime)
    _cid = TESLA_API_CLIENT_ID
    _csec = TESLA_API_CLIENT_SECRET
    try:
        import yaml as _yaml
        with open(os.path.join(os.path.dirname(__file__), '..', 'config.yaml'), 'r') as _f:
            _tcfg = (_yaml.safe_load(_f) or {}).get('tesla_api', {})
        _cid = _tcfg.get('client_id', '') or _cid
        _csec = _tcfg.get('client_secret', '') or _csec
    except Exception:
        pass

    payload = {
        'grant_type': 'authorization_code',
        'client_id': _cid,
        'client_secret': _csec,
        'code': code,
        'redirect_uri': redirect_uri,
    }

    try:
        resp = requests.post(TESLA_TOKEN_URL, json=payload, timeout=API_TIMEOUT)
        if resp.status_code != 200:
            logger.error("Tesla token exchange failed (%d): %s",
                        resp.status_code, resp.text[:500])
        resp.raise_for_status()
        data = resp.json()

        access_token = data['access_token']
        refresh_token = data['refresh_token']
        expires_at = time.time() + data.get('expires_in', 3600)

        if save_tokens(access_token, refresh_token, expires_at, pin):
            logger.info("Tesla OAuth tokens exchanged and saved")
            return {
                'access_token': access_token,
                'refresh_token': refresh_token,
                'expires_at': expires_at,
            }
        return None

    except Exception as e:
        logger.error("OAuth code exchange failed: %s", e, exc_info=True)
        return None


def refresh_access_token(pin: str = '') -> bool:
    """
    Refresh the access token using the stored refresh token.

    Decrypts the refresh token from disk (or uses the in-memory cache),
    posts to Tesla's token endpoint, and updates both the in-memory
    cache and the encrypted on-disk store.

    Args:
        pin: PIN if tokens are PIN-protected.

    Returns:
        bool: True if the access token was successfully refreshed.
    """
    global _ACCESS_TOKEN_CACHE, _TOKEN_EXPIRES_AT

    tokens = load_tokens(pin)
    if not tokens or not tokens.get('refresh_token'):
        logger.error("No refresh token available")
        return False

    requests = _get_requests()

    payload = {
        'grant_type': 'refresh_token',
        'client_id': TESLA_API_CLIENT_ID,
        'client_secret': TESLA_API_CLIENT_SECRET,
        'refresh_token': tokens['refresh_token'],
    }

    try:
        resp = requests.post(TESLA_TOKEN_URL, json=payload, timeout=API_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        _ACCESS_TOKEN_CACHE = data['access_token']
        _TOKEN_EXPIRES_AT = time.time() + data.get('expires_in', 3600)

        # Tesla may rotate the refresh token
        new_refresh = data.get('refresh_token', tokens['refresh_token'])
        save_tokens(_ACCESS_TOKEN_CACHE, new_refresh, _TOKEN_EXPIRES_AT, pin)

        logger.info("Tesla access token refreshed")
        return True

    except Exception as e:
        logger.error("Token refresh failed: %s", e, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Internal API Call Helper
# ---------------------------------------------------------------------------

def _get_access_token() -> Optional[str]:
    """Get a valid access token, refreshing if expired."""
    if _ACCESS_TOKEN_CACHE and _TOKEN_EXPIRES_AT:
        if time.time() < _TOKEN_EXPIRES_AT - 60:  # 60s safety buffer
            return _ACCESS_TOKEN_CACHE

    if refresh_access_token():
        return _ACCESS_TOKEN_CACHE
    return None


def _api_request(
    method: str,
    endpoint: str,
    call_type: str,
    db_path: str = '',
    json_body: Optional[dict] = None,
    source: str = 'web',
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    Make an authenticated request to the Tesla Fleet API.

    Handles token refresh, budget checks, retries with exponential
    backoff, timeouts, and audit logging. All Fleet API calls should
    go through this method.

    Args:
        method: HTTP method ('GET' or 'POST').
        endpoint: API endpoint path (e.g., '/api/1/vehicles').
        call_type: Budget category ('wake', 'command', or 'data').
        db_path: Database path for budget/audit logging.
        json_body: Optional JSON body for POST requests.
        source: Caller identifier for audit trail.

    Returns:
        Tuple of (success, response_data). response_data is the parsed
        JSON response body (with the 'response' wrapper unwrapped if
        present), or None on failure.
    """
    if not db_path:
        db_path = TESLA_API_DB_PATH

    requests = _get_requests()

    # Budget gate
    if not can_spend(db_path, call_type):
        logger.warning("Budget exceeded for call type '%s'", call_type)
        _log_api_call(
            db_path, endpoint, call_type, 0, False, source,
            "Monthly budget exceeded",
        )
        return False, None

    access_token = _get_access_token()
    if not access_token:
        logger.error("No valid access token available")
        return False, None

    url = f"{TESLA_FLEET_BASE}{endpoint}"
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
    }

    cost = API_COSTS.get(call_type, 0.001)
    last_error: Optional[str] = None

    for attempt in range(1, API_MAX_RETRIES + 1):
        try:
            if method.upper() == 'GET':
                resp = requests.get(url, headers=headers, timeout=API_TIMEOUT)
            else:
                resp = requests.post(
                    url, headers=headers, json=json_body or {},
                    timeout=API_TIMEOUT,
                )

            # Token expired mid-request — refresh once and retry
            if resp.status_code == 401:
                if attempt == 1 and refresh_access_token():
                    headers['Authorization'] = f'Bearer {_ACCESS_TOKEN_CACHE}'
                    continue
                _log_api_call(
                    db_path, endpoint, call_type, cost, False, source,
                    "Authentication failed",
                )
                return False, None

            resp.raise_for_status()
            data = resp.json()

            _log_api_call(db_path, endpoint, call_type, cost, True, source)
            # Unwrap Tesla's {"response": {...}} envelope when present
            return True, data.get('response', data)

        except requests.exceptions.Timeout:
            last_error = f"Timeout (attempt {attempt}/{API_MAX_RETRIES})"
            logger.warning("Tesla API timeout: %s attempt %d", endpoint, attempt)
        except requests.exceptions.ConnectionError:
            last_error = f"Connection error (attempt {attempt}/{API_MAX_RETRIES})"
            logger.warning(
                "Tesla API connection error: %s attempt %d", endpoint, attempt
            )
        except requests.exceptions.HTTPError as e:
            last_error = f"HTTP {e.response.status_code}: {e}"
            logger.error("Tesla API HTTP error: %s", e)
            break  # Don't retry 4xx client errors
        except Exception as e:
            last_error = str(e)
            logger.error("Tesla API unexpected error: %s", e, exc_info=True)
            break

        # Exponential backoff between retries (max 8s)
        if attempt < API_MAX_RETRIES:
            time.sleep(min(2 ** attempt, 8))

    _log_api_call(db_path, endpoint, call_type, cost, False, source, last_error)
    return False, None


# ---------------------------------------------------------------------------
# Fleet API Methods
# ---------------------------------------------------------------------------

def wake_up(vin: str, db_path: str = '') -> Optional[Dict[str, Any]]:
    """
    Wake up a Tesla vehicle.

    Args:
        vin: Vehicle Identification Number.
        db_path: Database path for logging.

    Returns:
        dict with vehicle wake state, or None on failure.
    """
    success, data = _api_request(
        'POST', f'/api/1/vehicles/{vin}/wake_up', 'wake', db_path,
    )
    return data if success else None


def get_vehicle_data(vin: str, db_path: str = '') -> Optional[Dict[str, Any]]:
    """
    Get comprehensive vehicle data (location, charge, climate, etc.).

    Args:
        vin: Vehicle Identification Number.
        db_path: Database path for logging.

    Returns:
        dict with vehicle data, or None on failure.
    """
    success, data = _api_request(
        'GET', f'/api/1/vehicles/{vin}/vehicle_data', 'data', db_path,
    )
    return data if success else None


def set_sentry_mode(
    vin: str,
    on: bool,
    db_path: str = '',
) -> Optional[Dict[str, Any]]:
    """
    Enable or disable Sentry Mode.

    Args:
        vin: Vehicle Identification Number.
        on: True to enable, False to disable.
        db_path: Database path for logging.

    Returns:
        dict with command result, or None on failure.
    """
    success, data = _api_request(
        'POST',
        f'/api/1/vehicles/{vin}/command/set_sentry_mode',
        'command',
        db_path,
        json_body={'on': on},
    )
    return data if success else None


def start_climate(vin: str, db_path: str = '') -> Optional[Dict[str, Any]]:
    """
    Start HVAC auto-conditioning.

    Args:
        vin: Vehicle Identification Number.
        db_path: Database path for logging.

    Returns:
        dict with command result, or None on failure.
    """
    success, data = _api_request(
        'POST',
        f'/api/1/vehicles/{vin}/command/auto_conditioning_start',
        'command',
        db_path,
    )
    return data if success else None


def stop_climate(vin: str, db_path: str = '') -> Optional[Dict[str, Any]]:
    """
    Stop HVAC auto-conditioning.

    Args:
        vin: Vehicle Identification Number.
        db_path: Database path for logging.

    Returns:
        dict with command result, or None on failure.
    """
    success, data = _api_request(
        'POST',
        f'/api/1/vehicles/{vin}/command/auto_conditioning_stop',
        'command',
        db_path,
    )
    return data if success else None


def get_vehicles(db_path: str = '') -> Optional[List[Dict[str, Any]]]:
    """
    List all vehicles associated with the Tesla account.

    Useful for VIN selection during initial setup.

    Args:
        db_path: Database path for logging.

    Returns:
        list of vehicle dicts with id, vin, display_name, etc.,
        or None on failure.
    """
    success, data = _api_request(
        'GET', '/api/1/vehicles', 'data', db_path,
    )
    if not success:
        return None
    # data is already unwrapped from 'response' by _api_request;
    # handle both list and dict-with-list forms
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get('results', data.get('vehicles', []))
    return None


# ---------------------------------------------------------------------------
# Budget Tracking
# ---------------------------------------------------------------------------

def _log_api_call(
    db_path: str,
    endpoint: str,
    call_type: str,
    cost: float,
    success: bool,
    source: str = 'web',
    error_msg: Optional[str] = None,
) -> None:
    """
    Log API call to audit table and update monthly budget.

    Args:
        db_path: Database path.
        endpoint: API endpoint called.
        call_type: Budget category ('wake', 'command', 'data').
        cost: Estimated cost in USD.
        success: Whether the call succeeded.
        source: Caller identifier.
        error_msg: Error message if the call failed.
    """
    if not db_path:
        db_path = TESLA_API_DB_PATH

    try:
        conn = _init_tesla_tables(db_path)
        now = datetime.now(timezone.utc).isoformat()
        month = datetime.now(timezone.utc).strftime('%Y-%m')

        conn.execute(
            "INSERT INTO tesla_api_audit "
            "(timestamp, endpoint, method, success, source, error_msg) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now, endpoint, call_type, 1 if success else 0, source, error_msg),
        )

        # Only count cost for successful calls
        if success and cost > 0:
            conn.execute(
                "INSERT INTO tesla_api_budget "
                "(month, call_type, call_count, estimated_cost) "
                "VALUES (?, ?, 1, ?) "
                "ON CONFLICT(month, call_type) DO UPDATE SET "
                "call_count = call_count + 1, "
                "estimated_cost = estimated_cost + ?",
                (month, call_type, cost, cost),
            )

        conn.commit()
        conn.close()

    except Exception as e:
        logger.error("Failed to log API call: %s", e)


def get_monthly_spend(db_path: str = '') -> Dict[str, Any]:
    """
    Get current month's API spending summary.

    Args:
        db_path: Database path.

    Returns:
        dict with keys:
            - total: Total estimated spend this month (USD).
            - by_type: Dict of {call_type: {count, cost}}.
            - budget_limit: Configured monthly budget.
            - remaining: Budget remaining.
    """
    if not db_path:
        db_path = TESLA_API_DB_PATH

    result: Dict[str, Any] = {
        'total': 0.0,
        'by_type': {},
        'budget_limit': TESLA_API_MONTHLY_BUDGET,
        'remaining': TESLA_API_MONTHLY_BUDGET,
    }

    try:
        conn = _init_tesla_tables(db_path)
        month = datetime.now(timezone.utc).strftime('%Y-%m')

        rows = conn.execute(
            "SELECT call_type, call_count, estimated_cost "
            "FROM tesla_api_budget WHERE month = ?",
            (month,),
        ).fetchall()
        conn.close()

        total = 0.0
        by_type: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            ct = row['call_type']
            row_cost = row['estimated_cost']
            by_type[ct] = {
                'count': row['call_count'],
                'cost': round(row_cost, 4),
            }
            total += row_cost

        result['total'] = round(total, 4)
        result['by_type'] = by_type
        result['remaining'] = round(TESLA_API_MONTHLY_BUDGET - total, 4)

    except Exception as e:
        logger.error("Failed to get monthly spend: %s", e)

    return result


def can_spend(db_path: str, call_type: str) -> bool:
    """
    Check if the monthly budget allows this call type.

    Thresholds:
        - $8.00: Disable auto keep-awake (wake calls).
        - $9.50: Disable all non-essential calls (commands, data).
        - Monthly budget limit: Hard stop on ALL calls.

    Args:
        db_path: Database path.
        call_type: API call category to check.

    Returns:
        bool: True if the call is within budget.
    """
    spend = get_monthly_spend(db_path)
    total = spend['total']

    if total >= TESLA_API_MONTHLY_BUDGET:
        logger.warning(
            "Monthly budget exhausted: $%.2f / $%.2f",
            total, TESLA_API_MONTHLY_BUDGET,
        )
        return False

    if total >= BUDGET_DISABLE_NONESSENTIAL and call_type != 'wake':
        logger.warning(
            "Non-essential calls disabled at $%.2f (threshold $%.2f)",
            total, BUDGET_DISABLE_NONESSENTIAL,
        )
        return False

    if total >= BUDGET_DISABLE_KEEPAWAKE and call_type == 'wake':
        logger.warning(
            "Auto keep-awake disabled at $%.2f (threshold $%.2f)",
            total, BUDGET_DISABLE_KEEPAWAKE,
        )
        return False

    return True


# ---------------------------------------------------------------------------
# Keep-Awake Manager
# ---------------------------------------------------------------------------

def start_keep_awake(
    vin: str,
    db_path: str = '',
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """
    Start the vehicle keep-awake polling loop in a background thread.

    Sends wake_up commands at the configured interval to prevent the
    vehicle from sleeping during cloud sync. Respects budget limits
    and the configured max awake duration.

    Args:
        vin: Vehicle Identification Number.
        db_path: Database path for logging.
        cancel_event: Optional external cancellation event.
    """
    global _keep_awake_thread, _keep_awake_cancel

    if _keep_awake_thread and _keep_awake_thread.is_alive():
        logger.warning("Keep-awake already running")
        return

    if not db_path:
        db_path = TESLA_API_DB_PATH

    _keep_awake_cancel = cancel_event or threading.Event()

    _keep_awake_thread = threading.Thread(
        target=_keep_awake_loop,
        args=(vin, db_path, _keep_awake_cancel),
        name='tesla-keep-awake',
        daemon=True,
    )
    _keep_awake_thread.start()
    logger.info("Keep-awake started for VIN %s...%s", vin[:3], vin[-4:])


def stop_keep_awake() -> None:
    """Stop the keep-awake polling loop."""
    global _keep_awake_thread, _keep_awake_cancel

    if _keep_awake_cancel:
        _keep_awake_cancel.set()
    if _keep_awake_thread and _keep_awake_thread.is_alive():
        _keep_awake_thread.join(timeout=10)

    _keep_awake_thread = None
    _keep_awake_cancel = None
    logger.info("Keep-awake stopped")


def _keep_awake_loop(
    vin: str,
    db_path: str,
    cancel_event: threading.Event,
) -> None:
    """
    Thread target: periodically wake the vehicle.

    Runs until cancel_event is set, max awake duration is reached, or
    budget is exhausted. Uses the configured wake interval.

    Args:
        vin: Vehicle Identification Number.
        db_path: Database path for logging.
        cancel_event: Event to signal thread shutdown.
    """
    start_time = time.monotonic()
    max_seconds = TESLA_API_MAX_AWAKE_MINUTES * 60
    interval = TESLA_API_WAKE_INTERVAL

    logger.info(
        "Keep-awake loop: interval=%ds, max=%dm, method=%s",
        interval, TESLA_API_MAX_AWAKE_MINUTES, TESLA_API_KEEP_AWAKE_METHOD,
    )

    while not cancel_event.is_set():
        elapsed = time.monotonic() - start_time
        if elapsed >= max_seconds:
            logger.info(
                "Keep-awake max duration reached (%dm)",
                TESLA_API_MAX_AWAKE_MINUTES,
            )
            break

        if not can_spend(db_path, 'wake'):
            logger.warning("Keep-awake stopped: budget exceeded")
            break

        result = wake_up(vin, db_path)
        if result:
            state = result.get('state', 'unknown')
            logger.debug("Keep-awake ping: vehicle state = %s", state)
        else:
            logger.warning("Keep-awake ping failed")

        cancel_event.wait(timeout=interval)

    logger.info("Keep-awake loop exited")


# ---------------------------------------------------------------------------
# Vehicle Status & Sync Window Detection
# ---------------------------------------------------------------------------

def get_vehicle_status(
    vin: str,
    db_path: str = '',
) -> Optional[Dict[str, Any]]:
    """
    Get parsed vehicle status for the dashboard widget.

    Extracts key fields from the raw vehicle data response into a
    flat dict suitable for UI display.

    Args:
        vin: Vehicle Identification Number.
        db_path: Database path for logging.

    Returns:
        dict with keys: state, battery_level, charging_state, sentry_mode,
        climate_on, inside_temp, outside_temp, odometer, location; or
        None on failure.
    """
    data = get_vehicle_data(vin, db_path)
    if not data:
        return None

    try:
        charge = data.get('charge_state', {})
        climate = data.get('climate_state', {})
        vehicle = data.get('vehicle_state', {})
        drive = data.get('drive_state', {})

        return {
            'state': data.get('state', 'unknown'),
            'battery_level': charge.get('battery_level'),
            'charging_state': charge.get('charging_state', 'Unknown'),
            'sentry_mode': vehicle.get('sentry_mode', False),
            'climate_on': climate.get('is_climate_on', False),
            'inside_temp': climate.get('inside_temp'),
            'outside_temp': climate.get('outside_temp'),
            'odometer': vehicle.get('odometer'),
            'location': {
                'lat': drive.get('latitude'),
                'lon': drive.get('longitude'),
                'heading': drive.get('heading'),
            },
        }

    except Exception as e:
        logger.error("Failed to parse vehicle status: %s", e)
        return None


def detect_sync_window(
    vin: str,
    db_path: str = '',
) -> str:
    """
    Determine optimal sync window based on vehicle state.

    Checks charging state, sentry mode, and battery level to decide
    how aggressively to sync:

        - 'full': Vehicle is charging or sentry mode is active — safe
          for extended sync with keep-awake.
        - 'sprint': Vehicle is awake but on battery — quick sync only,
          no keep-awake to avoid draining the battery.
        - 'none': Vehicle is asleep, battery too low, or unavailable.

    Args:
        vin: Vehicle Identification Number.
        db_path: Database path for logging.

    Returns:
        str: 'full', 'sprint', or 'none'.
    """
    status = get_vehicle_status(vin, db_path)
    if not status:
        return 'none'

    battery = status.get('battery_level')
    charging = status.get('charging_state', '')
    sentry = status.get('sentry_mode', False)
    state = status.get('state', '')

    # Low battery — don't sync
    if battery is not None and battery < TESLA_API_LOW_BATTERY_THRESHOLD:
        logger.info("Battery too low (%d%%) for sync", battery)
        return 'none'

    # Charging or sentry — full sync window
    if charging in ('Charging', 'Complete') or sentry:
        logger.info(
            "Full sync window: charging=%s, sentry=%s", charging, sentry
        )
        return 'full'

    # Awake on battery — quick sprint only
    if state == 'online':
        logger.info("Sprint sync window: vehicle online on battery")
        return 'sprint'

    return 'none'
