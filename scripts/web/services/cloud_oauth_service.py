"""
TeslaUSB Cloud OAuth Service.

Implements tiered authentication for OAuth-based cloud providers using
rclone's own registered client IDs.  No app registration required.

Tier 1 — Device Code Flow (RFC 8628): zero-paste for OneDrive and
         Google Drive (when their client IDs support it).
Tier 2 — PKCE Authorization Code Flow: user visits auth URL on their
         phone, signs in, and pastes back the code shown by the provider.
         Works on headless devices without a browser. Used as fallback
         for OneDrive/Google and as primary flow for Dropbox.
Tier 3 — Legacy manual token paste (handled in template, not here).

Designed for Pi Zero 2 W (512 MB RAM + 1 GB swap). No Chromium required.
"""

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# rclone's public OAuth credentials (extracted from open-source rclone code)
# These are NOT secrets — they are public client identifiers shipped in rclone.
# ---------------------------------------------------------------------------

_RCLONE_CREDENTIALS = {
    "onedrive": {
        "client_id": "b15665d9-eda6-4092-8539-0eec376afd59",
        "client_secret": "",
        "scopes": "Files.Read Files.ReadWrite Files.Read.All Files.ReadWrite.All Sites.Read.All offline_access",
    },
    "google-drive": {
        "client_id": "202264815644.apps.googleusercontent.com",
        "client_secret": "X4Z3ca8xfWDb1Voo-F9a7ZxJ",
        "scopes": "https://www.googleapis.com/auth/drive",
    },
    "dropbox": {
        "app_key": "5jcck7diasz0rqy",
        "app_secret": "",
    },
}

# Provider name → rclone backend type
_RCLONE_TYPE_MAP = {
    "google-drive": "drive",
    "onedrive": "onedrive",
    "dropbox": "dropbox",
}

# Device code endpoints per provider
_DEVICE_CODE_ENDPOINTS = {
    "onedrive": {
        "device_code_url": "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
    },
    "google-drive": {
        "device_code_url": "https://oauth2.googleapis.com/device/code",
        "token_url": "https://oauth2.googleapis.com/token",
    },
}

# PKCE auth code endpoints per provider
_PKCE_ENDPOINTS = {
    "onedrive": {
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        # Microsoft supports native client redirect for public clients
        "redirect_uri": "https://login.microsoftonline.com/common/oauth2/nativeclient",
    },
    "google-drive": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
    },
    "dropbox": {
        "auth_url": "https://www.dropbox.com/oauth2/authorize",
        "token_url": "https://api.dropboxapi.com/oauth2/token",
        "redirect_uri": "",  # Dropbox uses no redirect for PKCE
    },
}

# Legacy aliases (Dropbox endpoints used by dropbox_pkce_start)
_DROPBOX_AUTH_URL = _PKCE_ENDPOINTS["dropbox"]["auth_url"]
_DROPBOX_TOKEN_URL = _PKCE_ENDPOINTS["dropbox"]["token_url"]

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

_auth_lock = threading.Lock()
_auth_session: Optional[Dict] = None  # Only one auth flow at a time
_AUTH_TIMEOUT = 300  # 5 minutes


def _new_session_id() -> str:
    return secrets.token_urlsafe(16)


# ---------------------------------------------------------------------------
# Tier 1 — Device Code Flow
# ---------------------------------------------------------------------------

class DeviceCodeUnsupported(Exception):
    """Raised when a provider rejects the device code request."""
    pass


def device_code_start(provider: str) -> Dict:
    """Initiate a device code flow for the given provider.

    Returns dict with keys: session_id, user_code, verification_uri,
    device_code, interval, expires_in.

    Raises DeviceCodeUnsupported if the provider rejects the request.
    """
    global _auth_session

    if provider not in _DEVICE_CODE_ENDPOINTS:
        raise DeviceCodeUnsupported(f"No device code endpoint for {provider}")

    creds = _RCLONE_CREDENTIALS.get(provider, {})
    endpoints = _DEVICE_CODE_ENDPOINTS[provider]

    client_id = creds.get("client_id", "")
    scopes = creds.get("scopes", "")

    body = urlencode({"client_id": client_id, "scope": scopes}).encode()
    req = Request(endpoints["device_code_url"], data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except HTTPError as e:
        err_body = e.read().decode() if e.fp else ""
        logger.warning("Device code request rejected for %s: %s %s",
                       provider, e.code, err_body[:300])
        raise DeviceCodeUnsupported(
            f"Provider rejected device code request: {err_body[:200]}"
        ) from e
    except URLError as e:
        raise DeviceCodeUnsupported(f"Network error: {e}") from e

    session_id = _new_session_id()
    with _auth_lock:
        _cancel_existing_session()
        _auth_session = {
            "session_id": session_id,
            "method": "device_code",
            "provider": provider,
            "device_code": data.get("device_code", ""),
            "user_code": data.get("user_code", ""),
            "verification_uri": data.get("verification_uri")
                                or data.get("verification_url", ""),
            "interval": data.get("interval", 5),
            "expires_at": time.time() + data.get("expires_in", 900),
            "token": None,
            "error": None,
            "state": "pending",
        }

    return {
        "session_id": session_id,
        "method": "device_code",
        "user_code": _auth_session["user_code"],
        "verification_uri": _auth_session["verification_uri"],
        "interval": _auth_session["interval"],
        "expires_in": data.get("expires_in", 900),
    }


def device_code_poll(session_id: str) -> Dict:
    """Poll the token endpoint for a pending device code flow.

    Returns dict with 'state' key: 'pending', 'completed', 'expired', 'error'.
    On completion, includes 'token' dict.
    """
    global _auth_session

    with _auth_lock:
        sess = _auth_session
        if not sess or sess["session_id"] != session_id:
            return {"state": "error", "error": "Session not found"}
        if sess["method"] != "device_code":
            return {"state": "error", "error": "Not a device code session"}
        if sess["state"] == "completed":
            return {"state": "completed", "token": sess["token"]}
        if time.time() > sess["expires_at"]:
            sess["state"] = "expired"
            return {"state": "expired", "error": "Device code expired"}

    provider = sess["provider"]
    creds = _RCLONE_CREDENTIALS.get(provider, {})
    endpoints = _DEVICE_CODE_ENDPOINTS[provider]

    body_params = {
        "client_id": creds.get("client_id", ""),
        "device_code": sess["device_code"],
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }
    # Microsoft requires client_secret even if empty; Google doesn't mind
    if creds.get("client_secret"):
        body_params["client_secret"] = creds["client_secret"]

    body = urlencode(body_params).encode()
    req = Request(endpoints["token_url"], data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except HTTPError as e:
        err_body = json.loads(e.read().decode()) if e.fp else {}
        error_code = err_body.get("error", "")

        if error_code == "authorization_pending":
            return {"state": "pending"}
        if error_code == "slow_down":
            with _auth_lock:
                sess["interval"] = sess.get("interval", 5) + 5
            return {"state": "pending", "slow_down": True}
        if error_code in ("expired_token", "access_denied"):
            with _auth_lock:
                sess["state"] = "expired"
            return {"state": "expired", "error": error_code}

        logger.error("Device code poll error for %s: %s", provider, err_body)
        with _auth_lock:
            sess["state"] = "error"
            sess["error"] = str(err_body)[:200]
        return {"state": "error", "error": sess["error"]}
    except URLError as e:
        return {"state": "error", "error": f"Network error: {e}"}

    # Success — we have tokens
    token_dict = _build_token_dict(data)
    with _auth_lock:
        sess["state"] = "completed"
        sess["token"] = token_dict

    return {"state": "completed", "token": token_dict}


# ---------------------------------------------------------------------------
# Tier 2 — Embedded Browser via CDP (Chrome DevTools Protocol)
# ---------------------------------------------------------------------------

def browser_auth_start(provider: str) -> Dict:
    """Start an embedded browser auth flow for the given provider.

    Launches rclone authorize + headless Chromium on the Pi.
    Returns dict with session_id and method.
    """
    global _auth_session

    rclone_type = _RCLONE_TYPE_MAP.get(provider)
    if not rclone_type:
        raise ValueError(f"Unknown provider: {provider}")

    session_id = _new_session_id()
    with _auth_lock:
        _cancel_existing_session()
        _auth_session = {
            "session_id": session_id,
            "method": "browser",
            "provider": provider,
            "state": "starting",
            "auth_url": None,
            "rclone_proc": None,
            "chrome_proc": None,
            "token": None,
            "error": None,
            "expires_at": time.time() + _AUTH_TIMEOUT,
            "_stdout_buf": "",
        }

    # Start rclone authorize in a background thread
    thread = threading.Thread(
        target=_browser_auth_worker,
        args=(session_id, provider, rclone_type),
        daemon=True,
    )
    thread.start()

    return {"session_id": session_id, "method": "browser"}


def browser_auth_status(session_id: str) -> Dict:
    """Check the status of a browser auth flow."""
    with _auth_lock:
        sess = _auth_session
        if not sess or sess["session_id"] != session_id:
            return {"state": "error", "error": "Session not found"}
        result = {
            "state": sess["state"],
            "auth_url": sess.get("auth_url"),
        }
        if sess["state"] == "completed":
            result["token"] = sess["token"]
        if sess.get("error"):
            result["error"] = sess["error"]
        return result


def browser_get_screenshot(session_id: str) -> Optional[bytes]:
    """Capture a screenshot from the headless browser via CDP.

    Returns PNG bytes or None if not available.
    """
    with _auth_lock:
        sess = _auth_session
        if not sess or sess["session_id"] != session_id:
            return None
        if sess["state"] not in ("browser_active",):
            return None

    try:
        # CDP screenshot command
        cdp_url = "http://127.0.0.1:9222/json"
        with urlopen(cdp_url, timeout=3) as resp:
            pages = json.loads(resp.read().decode())
        if not pages:
            return None

        ws_url = pages[0].get("webSocketDebuggerUrl")
        if not ws_url:
            return None

        # Use CDP HTTP endpoint for screenshot (simpler than WebSocket)
        # Page.captureScreenshot via the /json/protocol is complex;
        # use the simpler approach of subprocess screenshot
        result = subprocess.run(
            [
                "chromium-browser", "--headless=new",
                "--screenshot=/dev/stdout",
                "--window-size=480,640",
                "--no-sandbox",
                "--disable-gpu",
                "--virtual-time-budget=1",
                pages[0].get("url", "about:blank"),
            ],
            capture_output=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except Exception as e:
        logger.debug("Screenshot capture failed: %s", e)
    return None


def browser_send_input(session_id: str, input_type: str, data: dict) -> bool:
    """Send keyboard/mouse input to the headless browser via CDP.

    input_type: 'key' | 'text' | 'click'
    data: depends on type — {'key': 'Enter'}, {'text': 'hello'}, {'x': 100, 'y': 200}
    """
    with _auth_lock:
        sess = _auth_session
        if not sess or sess["session_id"] != session_id:
            return False
        if sess["state"] != "browser_active":
            return False

    try:
        import websocket  # noqa: delayed import — only needed for CDP
        cdp_url = "http://127.0.0.1:9222/json"
        with urlopen(cdp_url, timeout=3) as resp:
            pages = json.loads(resp.read().decode())
        if not pages:
            return False

        ws_url = pages[0].get("webSocketDebuggerUrl")
        if not ws_url:
            return False

        ws = websocket.create_connection(ws_url, timeout=5)
        try:
            msg_id = 1
            if input_type == "text":
                for char in data.get("text", ""):
                    ws.send(json.dumps({
                        "id": msg_id,
                        "method": "Input.dispatchKeyEvent",
                        "params": {
                            "type": "keyDown",
                            "text": char,
                        }
                    }))
                    msg_id += 1
            elif input_type == "key":
                ws.send(json.dumps({
                    "id": msg_id,
                    "method": "Input.dispatchKeyEvent",
                    "params": {
                        "type": "keyDown",
                        "key": data.get("key", ""),
                        "code": data.get("code", ""),
                        "windowsVirtualKeyCode": data.get("keyCode", 0),
                    }
                }))
            elif input_type == "click":
                x, y = data.get("x", 0), data.get("y", 0)
                for etype in ("mousePressed", "mouseReleased"):
                    ws.send(json.dumps({
                        "id": msg_id,
                        "method": "Input.dispatchMouseEvent",
                        "params": {
                            "type": etype,
                            "x": x, "y": y,
                            "button": "left",
                            "clickCount": 1,
                        }
                    }))
                    msg_id += 1
            return True
        finally:
            ws.close()
    except Exception as e:
        logger.debug("CDP input failed: %s", e)
        return False


def _browser_auth_worker(session_id: str, provider: str, rclone_type: str):
    """Background thread: run rclone authorize + headless Chromium."""
    rclone_proc = None
    chrome_proc = None

    try:
        # Step 1: Start rclone authorize
        rclone_proc = subprocess.Popen(
            ["rclone", "authorize", rclone_type, "--auth-no-open-browser"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        with _auth_lock:
            sess = _auth_session
            if not sess or sess["session_id"] != session_id:
                rclone_proc.kill()
                return
            sess["rclone_proc"] = rclone_proc

        # Step 2: Read rclone output to find auth URL
        auth_url = None
        buf = ""
        deadline = time.time() + 30  # 30s to get auth URL

        while time.time() < deadline:
            line = rclone_proc.stdout.readline()
            if not line:
                if rclone_proc.poll() is not None:
                    break
                continue
            buf += line
            logger.debug("rclone: %s", line.rstrip())

            # Look for auth URL
            match = re.search(r'(https?://\S+)', line)
            if match and ("accounts.google" in match.group(1)
                          or "login.microsoftonline" in match.group(1)
                          or "dropbox.com" in match.group(1)
                          or "auth" in match.group(1).lower()):
                auth_url = match.group(1)
                break

        if not auth_url:
            with _auth_lock:
                if _auth_session and _auth_session["session_id"] == session_id:
                    _auth_session["state"] = "error"
                    _auth_session["error"] = "Could not find auth URL in rclone output"
            return

        with _auth_lock:
            if _auth_session and _auth_session["session_id"] == session_id:
                _auth_session["auth_url"] = auth_url
                _auth_session["state"] = "browser_active"

        # Step 3: Launch headless Chromium
        chrome_proc = subprocess.Popen(
            [
                "chromium-browser",
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--single-process",
                "--disable-extensions",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--disable-software-rasterizer",
                "--window-size=480,640",
                "--remote-debugging-port=9222",
                auth_url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        with _auth_lock:
            if _auth_session and _auth_session["session_id"] == session_id:
                _auth_session["chrome_proc"] = chrome_proc

        # Step 4: Wait for rclone to output the token
        token_buf = buf
        token_deadline = time.time() + _AUTH_TIMEOUT

        while time.time() < token_deadline:
            line = rclone_proc.stdout.readline()
            if not line:
                if rclone_proc.poll() is not None:
                    break
                time.sleep(0.5)
                continue
            token_buf += line
            logger.debug("rclone: %s", line.rstrip())

            with _auth_lock:
                if _auth_session and _auth_session["session_id"] == session_id:
                    _auth_session["_stdout_buf"] = token_buf

            # Check for token markers
            token = _extract_rclone_token(token_buf)
            if token:
                creds = _RCLONE_CREDENTIALS.get(provider, {})
                rclone_creds = format_rclone_credentials(provider, token, creds)
                with _auth_lock:
                    if _auth_session and _auth_session["session_id"] == session_id:
                        _auth_session["state"] = "completed"
                        _auth_session["token"] = rclone_creds
                return

        # Timeout
        with _auth_lock:
            if _auth_session and _auth_session["session_id"] == session_id:
                _auth_session["state"] = "expired"
                _auth_session["error"] = "Auth timed out"

    except Exception as e:
        logger.exception("Browser auth worker failed")
        with _auth_lock:
            if _auth_session and _auth_session["session_id"] == session_id:
                _auth_session["state"] = "error"
                _auth_session["error"] = str(e)[:200]
    finally:
        if chrome_proc:
            try:
                chrome_proc.terminate()
                chrome_proc.wait(timeout=5)
            except Exception:
                try:
                    chrome_proc.kill()
                except Exception:
                    pass
        if rclone_proc:
            try:
                rclone_proc.terminate()
                rclone_proc.wait(timeout=5)
            except Exception:
                try:
                    rclone_proc.kill()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Tier 2 alt — PKCE Authorization Code Flow (all providers)
# ---------------------------------------------------------------------------

def pkce_auth_start(provider: str) -> Dict:
    """Start a PKCE authorization code flow for any supported provider.

    Works without a browser on the Pi. The user visits the auth URL on
    their phone/computer, signs in, and pastes back the authorization code.

    Returns dict with session_id, method, auth_url, provider.
    """
    global _auth_session

    endpoints = _PKCE_ENDPOINTS.get(provider)
    if not endpoints:
        raise ValueError(f"No PKCE endpoints for {provider}")

    creds = _RCLONE_CREDENTIALS.get(provider, {})
    client_id = creds.get("client_id") or creds.get("app_key", "")
    scopes = creds.get("scopes", "")

    verifier, challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(16)

    params = {
        "client_id": client_id,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }

    # Provider-specific parameters
    if endpoints["redirect_uri"]:
        params["redirect_uri"] = endpoints["redirect_uri"]
    if scopes:
        params["scope"] = scopes
    if provider == "dropbox":
        params["token_access_type"] = "offline"

    auth_url = f"{endpoints['auth_url']}?{urlencode(params)}"

    session_id = _new_session_id()
    with _auth_lock:
        _cancel_existing_session()
        _auth_session = {
            "session_id": session_id,
            "method": "pkce",
            "provider": provider,
            "state_param": state,
            "code_verifier": verifier,
            "redirect_uri": endpoints.get("redirect_uri", ""),
            "auth_url": auth_url,
            "token": None,
            "error": None,
            "state": "pending",
            "expires_at": time.time() + _AUTH_TIMEOUT,
        }

    return {
        "session_id": session_id,
        "method": "pkce",
        "auth_url": auth_url,
        "provider": provider,
    }


def pkce_exchange(session_id: str, code: str) -> Dict:
    """Exchange an authorization code for tokens (any PKCE provider).

    Returns dict with 'state' and optionally 'token'.
    """
    global _auth_session

    with _auth_lock:
        sess = _auth_session
        if not sess or sess["session_id"] != session_id:
            return {"state": "error", "error": "Session not found"}
        if sess["method"] != "pkce":
            return {"state": "error", "error": "Not a PKCE session"}
        verifier = sess["code_verifier"]
        provider = sess["provider"]
        redirect_uri = sess.get("redirect_uri", "")

    endpoints = _PKCE_ENDPOINTS.get(provider)
    if not endpoints:
        return {"state": "error", "error": f"Unknown provider: {provider}"}

    creds = _RCLONE_CREDENTIALS.get(provider, {})
    client_id = creds.get("client_id") or creds.get("app_key", "")

    body_params = {
        "code": code.strip(),
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code_verifier": verifier,
    }
    if redirect_uri:
        body_params["redirect_uri"] = redirect_uri
    # Some providers need client_secret even if empty
    client_secret = creds.get("client_secret") or creds.get("app_secret", "")
    if client_secret:
        body_params["client_secret"] = client_secret

    body = urlencode(body_params).encode()
    req = Request(endpoints["token_url"], data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except HTTPError as e:
        err_body = e.read().decode() if e.fp else str(e)
        logger.error("PKCE exchange failed for %s: %s", provider, err_body[:300])
        with _auth_lock:
            if sess:
                sess["state"] = "error"
                sess["error"] = err_body[:200]
        return {"state": "error", "error": err_body[:200]}
    except URLError as e:
        return {"state": "error", "error": f"Network error: {e}"}

    token_dict = _build_token_dict(data)
    rclone_creds = format_rclone_credentials(provider, token_dict, creds)

    with _auth_lock:
        if _auth_session and _auth_session["session_id"] == session_id:
            _auth_session["state"] = "completed"
            _auth_session["token"] = rclone_creds

    return {"state": "completed", "token": rclone_creds}


# Backward-compatible aliases
def dropbox_pkce_start() -> Dict:
    """Start a Dropbox PKCE flow (legacy wrapper)."""
    return pkce_auth_start("dropbox")


def dropbox_pkce_exchange(session_id: str, code: str) -> Dict:
    """Exchange a Dropbox code (legacy wrapper)."""
    return pkce_exchange(session_id, code)


def _generate_pkce_pair() -> Tuple[str, str]:
    """Generate a PKCE code_verifier and code_challenge pair."""
    verifier = secrets.token_urlsafe(32)  # 43 chars
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ---------------------------------------------------------------------------
# Token formatting
# ---------------------------------------------------------------------------

def format_rclone_credentials(provider: str, token_dict: dict,
                              creds: dict) -> dict:
    """Convert OAuth tokens to rclone.conf-compatible credential dict.

    Returns a dict suitable for _write_rclone_conf() and encrypted storage.
    """
    rclone_type = _RCLONE_TYPE_MAP.get(provider, provider)

    # Build rclone token JSON string
    token_json = json.dumps({
        "access_token": token_dict.get("access_token", ""),
        "token_type": token_dict.get("token_type", "Bearer"),
        "refresh_token": token_dict.get("refresh_token", ""),
        "expiry": token_dict.get("expiry", ""),
    })

    result = {"type": rclone_type, "token": token_json}

    # Add provider-specific fields
    if provider == "google-drive":
        result["client_id"] = creds.get("client_id", "")
        result["client_secret"] = creds.get("client_secret", "")
        result["scope"] = "drive"
    elif provider == "onedrive":
        result["client_id"] = creds.get("client_id", "")
        result["drive_type"] = "personal"
    elif provider == "dropbox":
        result["client_id"] = creds.get("app_key", "")
        result["client_secret"] = creds.get("app_secret", "")

    return result


def _build_token_dict(data: dict) -> dict:
    """Build a standardized token dict from an OAuth token response."""
    expires_in = data.get("expires_in", 3600)
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)
              ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    return {
        "access_token": data.get("access_token", ""),
        "token_type": data.get("token_type", "Bearer"),
        "refresh_token": data.get("refresh_token", ""),
        "expiry": expiry,
        "expires_in": expires_in,
    }


def _extract_rclone_token(text: str) -> Optional[dict]:
    """Extract token JSON from rclone authorize stdout.

    Looks for content between ---> and <---End paste markers.
    """
    match = re.search(r'--->\s*(.*?)\s*<---End paste', text, re.DOTALL)
    if not match:
        return None
    raw = match.group(1).strip()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Failed to parse rclone token: %s", raw[:100])
        return None


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def cancel_auth(session_id: str) -> bool:
    """Cancel an in-progress auth flow."""
    with _auth_lock:
        sess = _auth_session
        if not sess or sess["session_id"] != session_id:
            return False
        _cancel_existing_session()
        return True


def _cancel_existing_session():
    """Cancel and clean up the current auth session (must hold _auth_lock)."""
    global _auth_session
    if _auth_session is None:
        return

    # Kill any subprocesses
    for key in ("rclone_proc", "chrome_proc"):
        proc = _auth_session.get(key)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    _auth_session = None


def get_session_info() -> Optional[Dict]:
    """Get current auth session info (for status checks)."""
    with _auth_lock:
        if _auth_session is None:
            return None
        return {
            "session_id": _auth_session["session_id"],
            "method": _auth_session["method"],
            "provider": _auth_session["provider"],
            "state": _auth_session["state"],
        }


# ---------------------------------------------------------------------------
# High-level: auto-tier start
# ---------------------------------------------------------------------------

def start_oauth(provider: str) -> Dict:
    """Start OAuth for a provider using the best available method.

    Tries device code first (zero-paste), falls back to browser auth
    or PKCE depending on provider.

    Returns dict describing the started flow.
    """
    # Tier 1: Try device code for OneDrive and Google Drive
    if provider in _DEVICE_CODE_ENDPOINTS:
        try:
            result = device_code_start(provider)
            logger.info("Device code flow started for %s", provider)
            return result
        except DeviceCodeUnsupported as e:
            logger.info("Device code not supported for %s, trying fallback: %s",
                        provider, e)

    # Tier 2: PKCE authorization code flow (works on headless devices)
    if provider in _PKCE_ENDPOINTS:
        result = pkce_auth_start(provider)
        logger.info("PKCE auth flow started for %s", provider)
        return result

    raise ValueError(f"Unsupported provider: {provider}")


def poll_oauth(session_id: str) -> Dict:
    """Poll the current auth flow for completion."""
    with _auth_lock:
        sess = _auth_session
        if not sess or sess["session_id"] != session_id:
            return {"state": "error", "error": "Session not found"}
        method = sess["method"]

    if method == "device_code":
        return device_code_poll(session_id)
    elif method == "browser":
        return browser_auth_status(session_id)
    elif method == "pkce":
        with _auth_lock:
            return {
                "state": sess["state"],
                "auth_url": sess.get("auth_url"),
                "error": sess.get("error"),
            }
    return {"state": "error", "error": f"Unknown method: {method}"}
