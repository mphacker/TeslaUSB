"""B-1 service: OAuth 2.0 PKCE and token refresh for cloud providers."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import ssl
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from collections.abc import Mapping

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

_JSON_ENCODING: Final[str] = "utf-8"
_JSON_INDENT: Final[int] = 2
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 15.0
_SESSION_TTL: Final[timedelta] = timedelta(minutes=5)
_WINDOWS_PERMS_WARNING: Final[str] = (
    "Credential file permissions use the default Windows ACL; explicit 0o600-style "
    "permissions are only enforced on POSIX."
)


class _ReadableResponse(Protocol):
    def read(self) -> bytes: ...

    def close(self) -> None: ...


class OAuthError(RuntimeError):
    """Raised when an OAuth flow cannot be started, exchanged, or revoked."""


class TokenRefreshError(RuntimeError):
    """Raised when a stored refresh token cannot mint a new access token."""


class OAuthConfigError(ValueError):
    """Raised when the OAuth service configuration is invalid."""


@dataclass(frozen=True, slots=True)
class OAuthProvider:
    key: str
    rclone_type: str
    client_id: str
    client_secret: str
    scope: str
    auth_url: str
    exchange_url: str
    redirect_uri: str
    revoke_url: str | None = None
    revoke_uses_bearer_token: bool = False
    extra_auth_params: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class OAuthConfig:
    credentials_path: Path
    oauth_state_path: Path
    rclone_config_path: Path
    refresh_window_seconds: int = 300
    request_timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for name, value in (
            ("credentials_path", self.credentials_path),
            ("oauth_state_path", self.oauth_state_path),
            ("rclone_config_path", self.rclone_config_path),
        ):
            if not value.is_absolute() and not PurePosixPath(value.as_posix()).is_absolute():
                raise OAuthConfigError(f"{name} must be absolute, got {value!r}")
        if self.refresh_window_seconds < 0:
            raise OAuthConfigError("refresh_window_seconds must be >= 0")
        if self.request_timeout_seconds <= 0:
            raise OAuthConfigError("request_timeout_seconds must be > 0")


@dataclass(frozen=True, slots=True)
class OAuthCredentials:
    provider: str
    access_token: str
    refresh_token: str
    token_type: str
    expires_at: str
    scope: str | None = None


@dataclass(frozen=True, slots=True)
class AuthorizationStart:
    session_id: str
    provider: str
    authorization_url: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class RefreshResult:
    refreshed: bool
    credentials: OAuthCredentials | None
    message: str


@dataclass(frozen=True, slots=True)
class DisconnectResult:
    disconnected: bool
    revoked: bool
    message: str


@dataclass(frozen=True, slots=True)
class _PendingOAuthState:
    session_id: str
    provider: str
    authorization_url: str
    code_verifier: str
    state_token: str
    redirect_uri: str
    expires_at: str


_GOOGLE_CLIENT_SECRET: Final[str] = "X4Z3ca8xfWDb1Voo-F9a7ZxJ"  # noqa: S105
_PROVIDERS: Final[dict[str, OAuthProvider]] = {
    "dropbox": OAuthProvider(
        key="dropbox",
        rclone_type="dropbox",
        client_id="5jcck7diasz0rqy",
        client_secret="",
        scope="",
        auth_url="https://www.dropbox.com/oauth2/authorize",
        exchange_url="https://api.dropboxapi.com/oauth2/token",
        redirect_uri="",
        revoke_url="https://api.dropboxapi.com/2/auth/token/revoke",
        revoke_uses_bearer_token=True,
        extra_auth_params=(("token_access_type", "offline"),),
    ),
    "google-drive": OAuthProvider(
        key="google-drive",
        rclone_type="drive",
        client_id="202264815644.apps.googleusercontent.com",
        client_secret=_GOOGLE_CLIENT_SECRET,
        scope="https://www.googleapis.com/auth/drive",
        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
        exchange_url="https://oauth2.googleapis.com/token",
        redirect_uri="http://localhost:53682/",
        revoke_url="https://oauth2.googleapis.com/revoke",
    ),
    "onedrive": OAuthProvider(
        key="onedrive",
        rclone_type="onedrive",
        client_id="b15665d9-eda6-4092-8539-0eec376afd59",
        client_secret="",
        scope=(
            "Files.Read Files.ReadWrite Files.Read.All Files.ReadWrite.All "
            "Sites.Read.All offline_access"
        ),
        auth_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        exchange_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        redirect_uri="http://localhost:53682/",
    ),
}

_WINDOWS_WARNING_LOCK = threading.Lock()
_WINDOWS_WARNING_STATE: dict[str, bool] = {"emitted": False}


class CloudOAuthService:
    """Handle PKCE login, token persistence, proactive refresh, and disconnect."""

    def __init__(self, oauth_config: OAuthConfig) -> None:
        oauth_config.validate()
        self._config = oauth_config
        self._lock = threading.RLock()

    @property
    def rclone_config_path(self) -> Path:
        return self._config.rclone_config_path

    def supported_providers(self) -> tuple[str, ...]:
        return tuple(_PROVIDERS)

    def start_authorization(self, provider: str) -> AuthorizationStart:
        metadata = _provider_for(provider)
        verifier = secrets.token_urlsafe(64)
        challenge = _pkce_challenge(verifier)
        state_token = secrets.token_urlsafe(32)
        expires_at = _utc_now() + _SESSION_TTL
        params: dict[str, str] = {
            "client_id": metadata.client_id,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state_token,
        }
        if metadata.redirect_uri:
            params["redirect_uri"] = metadata.redirect_uri
        if metadata.scope:
            params["scope"] = metadata.scope
        params.update(dict(metadata.extra_auth_params))
        authorization_url = f"{metadata.auth_url}?{urlencode(params)}"
        pending = _PendingOAuthState(
            session_id=secrets.token_urlsafe(16),
            provider=provider,
            authorization_url=authorization_url,
            code_verifier=verifier,
            state_token=state_token,
            redirect_uri=metadata.redirect_uri,
            expires_at=_datetime_to_json(expires_at),
        )
        with self._lock:
            _write_json_atomically(self._config.oauth_state_path, _pending_to_json(pending))
        logger.info("Started PKCE authorization for %s", provider)
        return AuthorizationStart(
            session_id=pending.session_id,
            provider=pending.provider,
            authorization_url=pending.authorization_url,
            expires_at=pending.expires_at,
        )

    def get_pending_authorization(
        self,
        session_id: str | None = None,
    ) -> AuthorizationStart | None:
        with self._lock:
            pending = self._load_pending_state()
            if pending is None:
                return None
            if session_id is not None and pending.session_id != session_id:
                return None
            return AuthorizationStart(
                session_id=pending.session_id,
                provider=pending.provider,
                authorization_url=pending.authorization_url,
                expires_at=pending.expires_at,
            )

    def cancel_authorization(self, session_id: str) -> bool:
        with self._lock:
            pending = self._load_pending_state()
            if pending is None or pending.session_id != session_id:
                return False
            _delete_file(self._config.oauth_state_path)
        logger.info("Cancelled OAuth authorization session %s", session_id)
        return True

    def exchange_code(self, session_id: str, code_or_redirect_url: str) -> OAuthCredentials:
        with self._lock:
            pending = self._require_pending_state(session_id)
        code, returned_state = _extract_code_and_state(code_or_redirect_url)
        if not code:
            raise OAuthError("Authorization code is missing")
        if returned_state is not None and returned_state != pending.state_token:
            raise OAuthError("OAuth state mismatch")
        provider = _provider_for(pending.provider)
        token_payload = self._post_form(
            provider.exchange_url,
            _authorization_code_payload(provider, pending, code),
        )
        credentials = _credentials_from_token_response(provider.key, token_payload, previous=None)
        with self._lock:
            _write_json_atomically(self._config.credentials_path, _credentials_to_json(credentials))
            _delete_file(self._config.oauth_state_path)
        logger.info("Stored OAuth credentials for %s", provider.key)
        return credentials

    def import_rclone_token(self, provider_key: str, token_text: str) -> OAuthCredentials:
        """Persist credentials produced out-of-band by ``rclone authorize``.

        The OneDrive / Google Drive / Dropbox flows in the UI ask the
        operator to run ``rclone authorize "<provider>"`` on a desktop
        and paste the resulting JSON blob into the form. That blob has
        the shape ``{"access_token":..., "token_type":..., "refresh_token":..., "expiry":...}``
        — same fields ``_rclone_token_json`` later writes back into
        rclone.conf. Without this importer the form silently fell
        through to a new PKCE flow that the JS reported as success but
        which never actually persisted credentials.

        Raises ``OAuthError`` on malformed input or unsupported provider.
        """
        metadata = _provider_for(provider_key)
        text = (token_text or "").strip()
        if not text:
            raise OAuthError("Empty rclone token")
        # `rclone authorize` wraps the JSON between paste markers
        # (`---> {...} <---End paste`) so users typically copy the
        # entire block. Strip the markers if present — v1 accepted
        # both forms and we should too.
        marker_match = re.search(r"--->\s*(\{.*?\})\s*<---End paste", text, re.DOTALL)
        if marker_match:
            text = marker_match.group(1).strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OAuthError(
                "Could not parse rclone token. Copy the entire JSON object "
                f"from `rclone authorize`, including the curly braces ({exc.msg})."
            ) from exc
        if not isinstance(payload, dict):
            raise OAuthError("rclone token must be a JSON object")
        access_token = _string_or_default(payload.get("access_token"), default="").strip()
        if not access_token:
            raise OAuthError("rclone token is missing access_token")
        refresh_token = _string_or_default(payload.get("refresh_token"), default="").strip()
        token_type = _string_or_default(payload.get("token_type"), default="Bearer").strip() or "Bearer"
        raw_expiry = _string_or_default(payload.get("expiry"), default="").strip()
        if not raw_expiry:
            raw_expiry = _string_or_default(payload.get("expires_at"), default="").strip()
        if raw_expiry:
            expires_at = _datetime_to_json(_parse_datetime(raw_expiry))
        else:
            # rclone occasionally emits tokens with no expiry (long-
            # lived refresh tokens); persist a far-future placeholder
            # so refresh_if_needed treats them as valid until they
            # actually 401 on use.
            expires_at = _datetime_to_json(_utc_now().replace(year=_utc_now().year + 10))
        scope = _optional_string(payload.get("scope"))
        credentials = OAuthCredentials(
            provider=metadata.key,
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=token_type,
            expires_at=expires_at,
            scope=scope,
        )
        with self._lock:
            _write_json_atomically(self._config.credentials_path, _credentials_to_json(credentials))
            _delete_file(self._config.oauth_state_path)
        logger.info("Imported rclone token for %s", metadata.key)
        return credentials

    def load_credentials(self) -> OAuthCredentials | None:
        with self._lock:
            payload = _load_json_object(self._config.credentials_path)
        if payload is None:
            return None
        return _credentials_from_json(payload)

    def refresh_if_needed(
        self,
        *,
        force: bool = False,
        provider: str | None = None,
    ) -> RefreshResult:
        credentials = self.load_credentials()
        if credentials is None:
            return RefreshResult(
                refreshed=False,
                credentials=None,
                message="No stored OAuth credentials",
            )
        if provider is not None and credentials.provider != provider:
            raise TokenRefreshError(
                f"Stored credentials belong to {credentials.provider}, not {provider}"
            )
        if not force and not self._needs_refresh(credentials):
            return RefreshResult(
                refreshed=False,
                credentials=credentials,
                message="OAuth credentials are still valid",
            )
        if not credentials.refresh_token:
            raise TokenRefreshError("Stored credentials do not contain a refresh_token")
        metadata = _provider_for(credentials.provider)
        try:
            token_payload = self._post_form(
                metadata.exchange_url,
                _refresh_token_payload(metadata, credentials.refresh_token),
            )
        except OAuthError as exc:
            raise TokenRefreshError(str(exc)) from exc
        refreshed = _credentials_from_token_response(
            metadata.key,
            token_payload,
            previous=credentials,
        )
        with self._lock:
            _write_json_atomically(self._config.credentials_path, _credentials_to_json(refreshed))
        logger.info("Refreshed OAuth token for %s", metadata.key)
        return RefreshResult(
            refreshed=True,
            credentials=refreshed,
            message="OAuth token refreshed",
        )

    def disconnect(self, *, provider: str | None = None) -> DisconnectResult:
        credentials = self.load_credentials()
        if credentials is not None and provider is not None and credentials.provider != provider:
            raise OAuthError(f"Stored credentials belong to {credentials.provider}, not {provider}")
        revoked = False
        revoke_error: str | None = None
        if credentials is not None:
            metadata = _provider_for(credentials.provider)
            if metadata.revoke_url is not None:
                try:
                    self._revoke(metadata, credentials)
                    revoked = True
                except OAuthError as exc:
                    revoke_error = str(exc)
                    logger.warning("Remote token revoke failed for %s: %s", metadata.key, exc)
        with self._lock:
            _delete_file(self._config.credentials_path)
            _delete_file(self._config.oauth_state_path)
            # Drop the rendered rclone.conf too. It carries the OAuth token
            # and -- for OneDrive -- a cached drive_id pinned to the account
            # being disconnected. Leaving it behind would let a later
            # re-connect (possibly to a different account) silently reuse the
            # stale drive_id. The config is regenerated on the next operation.
            _delete_file(self._config.rclone_config_path)
        if credentials is None:
            return DisconnectResult(
                disconnected=True,
                revoked=False,
                message="No stored OAuth credentials",
            )
        if revoke_error is not None:
            return DisconnectResult(
                disconnected=True,
                revoked=False,
                message=f"Removed local credentials: {revoke_error}",
            )
        if revoked:
            return DisconnectResult(
                disconnected=True,
                revoked=True,
                message="Revoked remote token and removed local credentials",
            )
        return DisconnectResult(
            disconnected=True,
            revoked=False,
            message="Removed local credentials",
        )

    def _load_pending_state(self) -> _PendingOAuthState | None:
        payload = _load_json_object(self._config.oauth_state_path)
        if payload is None:
            return None
        pending = _pending_from_json(payload)
        if _parse_datetime(pending.expires_at) <= _utc_now():
            _delete_file(self._config.oauth_state_path)
            return None
        return pending

    def _require_pending_state(self, session_id: str) -> _PendingOAuthState:
        pending = self._load_pending_state()
        if pending is None:
            raise OAuthError("OAuth authorization session not found")
        if pending.session_id != session_id:
            raise OAuthError("OAuth authorization session not found")
        return pending

    def _needs_refresh(self, credentials: OAuthCredentials) -> bool:
        refresh_at = _parse_datetime(credentials.expires_at) - timedelta(
            seconds=self._config.refresh_window_seconds
        )
        return _utc_now() >= refresh_at

    def _post_form(self, url: str, payload: Mapping[str, str]) -> dict[str, object]:
        request = _https_request(
            url,
            data=urlencode(dict(payload)).encode(_JSON_ENCODING),
            method="POST",
        )
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            return _request_json(request, timeout=self._config.request_timeout_seconds)
        except HTTPError as exc:
            raise OAuthError(_http_error_message(exc)) from exc
        except URLError as exc:
            raise OAuthError(f"Network error: {exc.reason}") from exc

    def _revoke(self, provider: OAuthProvider, credentials: OAuthCredentials) -> None:
        if provider.revoke_url is None:
            return
        if provider.revoke_uses_bearer_token:
            request = _https_request(provider.revoke_url, data=b"", method="POST")
            request.add_header("Authorization", f"Bearer {credentials.access_token}")
        else:
            token_value = credentials.refresh_token or credentials.access_token
            request = _https_request(
                provider.revoke_url,
                data=urlencode({"token": token_value}).encode(_JSON_ENCODING),
                method="POST",
            )
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            response = _open_url(request, timeout=self._config.request_timeout_seconds)
            response.close()
        except HTTPError as exc:
            raise OAuthError(_http_error_message(exc)) from exc
        except URLError as exc:
            raise OAuthError(f"Network error: {exc.reason}") from exc


def _provider_for(provider: str) -> OAuthProvider:
    try:
        return _PROVIDERS[provider]
    except KeyError as exc:
        raise OAuthError(f"Unsupported OAuth provider: {provider}") from exc


def _authorization_code_payload(
    provider: OAuthProvider,
    pending: _PendingOAuthState,
    code: str,
) -> dict[str, str]:
    payload = {
        "code": code,
        "grant_type": "authorization_code",
        "client_id": provider.client_id,
        "code_verifier": pending.code_verifier,
    }
    if provider.client_secret:
        payload["client_secret"] = provider.client_secret
    if pending.redirect_uri:
        payload["redirect_uri"] = pending.redirect_uri
    return payload


def _refresh_token_payload(provider: OAuthProvider, refresh_token: str) -> dict[str, str]:
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": provider.client_id,
    }
    if provider.client_secret:
        payload["client_secret"] = provider.client_secret
    return payload


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _extract_code_and_state(raw: str) -> tuple[str, str | None]:
    value = raw.strip()
    if value.startswith(("http://", "https://")):
        query = parse_qs(urlparse(value).query)
        codes = query.get("code", [])
        states = query.get("state", [])
        return (codes[0] if codes else "", states[0] if states else None)
    return value, None


def _request_json(request: Request, *, timeout: float) -> dict[str, object]:
    response = _open_url(request, timeout=timeout)
    try:
        return _json_object_from_bytes(response.read())
    finally:
        response.close()


def _https_request(url: str, *, data: bytes, method: str) -> Request:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise OAuthError(f"OAuth endpoints must use https: {url}")
    return Request(url, data=data, method=method)  # noqa: S310


def _open_url(request: Request, *, timeout: float) -> _ReadableResponse:
    return cast(
        "_ReadableResponse",
        urlopen(  # noqa: S310  # nosec B310 - URL scheme validated by _https_request
            request,
            timeout=timeout,
            context=ssl.create_default_context(),
        ),
    )


def _json_object_from_bytes(raw: bytes) -> dict[str, object]:
    try:
        decoded = raw.decode(_JSON_ENCODING)
        payload: object = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OAuthError(f"Invalid JSON response: {exc}") from exc
    if not isinstance(payload, dict):
        raise OAuthError("JSON response must be an object")
    return {str(key): value for key, value in payload.items()}


def _credentials_from_token_response(
    provider: str,
    payload: Mapping[str, object],
    *,
    previous: OAuthCredentials | None,
) -> OAuthCredentials:
    access_token = _require_string(payload, "access_token")
    token_type = _string_or_default(payload.get("token_type"), default="Bearer")
    refresh_token = _string_or_default(
        payload.get("refresh_token"),
        default="" if previous is None else previous.refresh_token,
    )
    expires_at = _expires_at_from_payload(payload)
    scope = _optional_string(payload.get("scope"))
    if previous is not None and scope is None:
        scope = previous.scope
    return OAuthCredentials(
        provider=provider,
        access_token=access_token,
        refresh_token=refresh_token,
        token_type=token_type,
        expires_at=expires_at,
        scope=scope,
    )


def _expires_at_from_payload(payload: Mapping[str, object]) -> str:
    for key in ("expires_at", "expiry"):
        raw_value = payload.get(key)
        if isinstance(raw_value, str) and raw_value.strip():
            return _datetime_to_json(_parse_datetime(raw_value))
    raw_expires_in = payload.get("expires_in")
    if isinstance(raw_expires_in, bool) or not isinstance(raw_expires_in, (int, float)):
        raise OAuthError("Token response did not include a valid expiry")
    return _datetime_to_json(_utc_now() + timedelta(seconds=float(raw_expires_in)))


def _credentials_to_json(credentials: OAuthCredentials) -> dict[str, object]:
    payload: dict[str, object] = {
        "provider": credentials.provider,
        "access_token": credentials.access_token,
        "refresh_token": credentials.refresh_token,
        "token_type": credentials.token_type,
        "expires_at": credentials.expires_at,
    }
    if credentials.scope is not None:
        payload["scope"] = credentials.scope
    return payload


def _credentials_from_json(payload: Mapping[str, object]) -> OAuthCredentials:
    provider = _require_string(payload, "provider")
    _provider_for(provider)
    return OAuthCredentials(
        provider=provider,
        access_token=_require_string(payload, "access_token"),
        refresh_token=_string_or_default(payload.get("refresh_token"), default=""),
        token_type=_string_or_default(payload.get("token_type"), default="Bearer"),
        expires_at=_datetime_to_json(_parse_datetime(_require_string(payload, "expires_at"))),
        scope=_optional_string(payload.get("scope")),
    )


def _pending_to_json(pending: _PendingOAuthState) -> dict[str, object]:
    return {
        "session_id": pending.session_id,
        "provider": pending.provider,
        "authorization_url": pending.authorization_url,
        "code_verifier": pending.code_verifier,
        "state_token": pending.state_token,
        "redirect_uri": pending.redirect_uri,
        "expires_at": pending.expires_at,
    }


def _pending_from_json(payload: Mapping[str, object]) -> _PendingOAuthState:
    provider = _require_string(payload, "provider")
    _provider_for(provider)
    return _PendingOAuthState(
        session_id=_require_string(payload, "session_id"),
        provider=provider,
        authorization_url=_require_string(payload, "authorization_url"),
        code_verifier=_require_string(payload, "code_verifier"),
        state_token=_require_string(payload, "state_token"),
        redirect_uri=_string_or_default(payload.get("redirect_uri"), default=""),
        expires_at=_datetime_to_json(_parse_datetime(_require_string(payload, "expires_at"))),
    )


def _load_json_object(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        raw_text = path.read_text(encoding=_JSON_ENCODING)
    except OSError as exc:
        raise OAuthError(f"Failed to read {path}: {exc}") from exc
    try:
        payload: object = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise OAuthError(f"Failed to parse {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise OAuthError(f"{path} must contain a JSON object")
    return {str(key): value for key, value in payload.items()}


def _write_json_atomically(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    file_descriptor: int | None = None
    try:
        file_descriptor, raw_temp_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        temp_path = Path(raw_temp_path)
        if os.name == "nt":
            _warn_windows_permissions_once()
        else:
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                fchmod(file_descriptor, 0o600)
        raw_json = json.dumps(dict(payload), indent=_JSON_INDENT, sort_keys=True) + "\n"
        with os.fdopen(file_descriptor, "w", encoding=_JSON_ENCODING, newline="\n") as handle:
            file_descriptor = None
            handle.write(raw_json)
            handle.flush()
            _best_effort_fsync(handle.fileno())
        temp_path.replace(path)
        if os.name != "nt":
            path.chmod(0o600)
    except OSError as exc:
        raise OAuthError(f"Failed to write {path}: {exc}") from exc
    finally:
        if file_descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(file_descriptor)
        if temp_path is not None and temp_path.exists():
            with contextlib.suppress(OSError):
                temp_path.unlink()


def _warn_windows_permissions_once() -> None:
    with _WINDOWS_WARNING_LOCK:
        if _WINDOWS_WARNING_STATE["emitted"]:
            return
        logger.warning(_WINDOWS_PERMS_WARNING)
        _WINDOWS_WARNING_STATE["emitted"] = True


def _best_effort_fsync(file_descriptor: int) -> None:
    with contextlib.suppress(OSError):
        os.fsync(file_descriptor)


def _delete_file(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError, OSError):
        path.unlink()


def _require_string(payload: Mapping[str, object], key: str) -> str:
    raw_value = payload.get(key)
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise OAuthError(f"{key} must be a non-empty string")
    return raw_value


def _string_or_default(raw_value: object, *, default: str) -> str:
    if raw_value is None:
        return default
    if not isinstance(raw_value, str):
        raise OAuthError("Expected a string value")
    return raw_value


def _optional_string(raw_value: object) -> str | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise OAuthError("Expected a string value")
    return raw_value


def _parse_datetime(raw_value: str) -> datetime:
    candidate = raw_value.strip()
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise OAuthError(f"Invalid ISO-8601 datetime: {raw_value}") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _datetime_to_json(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _http_error_message(exc: HTTPError) -> str:
    try:
        body = exc.read().decode(_JSON_ENCODING)
    except OSError:
        body = ""
    body = body.strip()
    return f"HTTP {exc.code}: {body}" if body else f"HTTP {exc.code}"


def make_oauth_service(cfg: WebConfig) -> CloudOAuthService:
    """Build an OAuth service rooted at the configured cloud state paths."""
    return CloudOAuthService(
        OAuthConfig(
            credentials_path=cfg.cloud.credentials_path,
            oauth_state_path=cfg.cloud.oauth_state_path,
            rclone_config_path=cfg.cloud.rclone_config_path,
            refresh_window_seconds=cfg.cloud.refresh_window_seconds,
        )
    )


__all__ = (
    "AuthorizationStart",
    "CloudOAuthService",
    "DisconnectResult",
    "OAuthConfig",
    "OAuthConfigError",
    "OAuthCredentials",
    "OAuthError",
    "RefreshResult",
    "TokenRefreshError",
    "make_oauth_service",
)
