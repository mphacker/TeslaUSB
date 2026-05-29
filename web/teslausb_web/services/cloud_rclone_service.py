"""B-1 service: rclone subprocess wrapper for OAuth-backed cloud remotes."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import threading

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from teslausb_web.config import WebConfig
    from teslausb_web.services.cloud_generic_remote_service import GenericRemoteService
    from teslausb_web.services.cloud_oauth_service import CloudOAuthService, OAuthCredentials

logger = logging.getLogger(__name__)

_RCLONE_CONFIG_FILENAME: Final[str] = "rclone.conf"
_RCLONE_REMOTE_NAME: Final[str] = "teslausb"
_GRAPH_DRIVE_URL: Final[str] = "https://graph.microsoft.com/v1.0/me/drive"
_SHORT_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0
_WINDOWS_PERMS_WARNING: Final[str] = (
    "rclone config/log files use the default Windows ACL; explicit 0o600-style "
    "permissions are only enforced on POSIX."
)
_AUTH_ERROR_PATTERNS: Final[tuple[str, ...]] = (
    "invalid_grant",
    "token expired",
    "token has been expired",
    "token has been revoked",
    "couldn't fetch token",
    "failed to refresh token",
    "unauthorized",
    "401",
)
_MISSING_DIRECTORY_PATTERNS: Final[tuple[str, ...]] = (
    "directory not found",
    "dir not found",
    "object not found",
    "path not found",
)
_PROVIDER_BACKENDS: Final[dict[str, str]] = {
    "dropbox": "dropbox",
    "google-drive": "drive",
    "onedrive": "onedrive",
}
_TRANSFERRED_RE: Final[re.Pattern[str]] = re.compile(
    r"Transferred:\s*(?P<transferred>.+?)"
    r"(?:\s*/\s*(?P<total>.+?))?"
    r"(?:,\s*(?P<percent>\d{1,3})%)?"
    r"(?:,\s*(?P<speed>.+?))?"
    r"(?:,\s*ETA\s*(?P<eta>.+))?$"
)
_PROGRESS_RE: Final[re.Pattern[str]] = re.compile(r"Progress:\s*(?P<summary>.+)$")
_WINDOWS_WARNING_LOCK = threading.Lock()
_WINDOWS_WARNING_STATE: dict[str, bool] = {"emitted": False}


class _ReadableResponse(Protocol):
    def read(self) -> bytes: ...

    def close(self) -> None: ...


class _SubprocessRunText(Protocol):
    def __call__(
        self,
        command: list[str],
        /,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]: ...


class _SubprocessPopenText(Protocol):
    def __call__(
        self,
        command: list[str],
        /,
        **_kwargs: object,
    ) -> subprocess.Popen[str]: ...


class RcloneError(RuntimeError):
    """Raised when an rclone command cannot be started or completes unsuccessfully."""


class RcloneNotInstalledError(RcloneError):
    """Raised when the configured rclone binary cannot be resolved."""


class RcloneAuthError(RcloneError):
    """Raised when OAuth credentials are missing, stale, or rejected by the provider."""


class RcloneTransferError(RcloneError):
    """Raised when a transfer fails or times out."""


class RcloneConfigError(ValueError):
    """Raised when the rclone service configuration or arguments are invalid."""


@dataclass(frozen=True, slots=True)
class RcloneServiceConfig:
    rclone_config_dir: Path
    rclone_log_path: Path
    allowed_local_roots: tuple[Path, ...]
    rclone_binary: str = "rclone"
    transfer_timeout_seconds: int = 3600
    bwlimit_kbps: int = 0
    retries: int = 3

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for name, value in (
            ("rclone_config_dir", self.rclone_config_dir),
            ("rclone_log_path", self.rclone_log_path),
        ):
            if not value.is_absolute() and not PurePosixPath(value.as_posix()).is_absolute():
                raise RcloneConfigError(f"{name} must be absolute, got {value!r}")
        if not self.allowed_local_roots:
            raise RcloneConfigError("allowed_local_roots must be non-empty")
        for root in self.allowed_local_roots:
            if not root.is_absolute() and not PurePosixPath(root.as_posix()).is_absolute():
                raise RcloneConfigError(f"allowed_local_roots must be absolute, got {root!r}")
        if not self.rclone_binary.strip():
            raise RcloneConfigError("rclone_binary must be non-empty")
        if self.transfer_timeout_seconds <= 0:
            raise RcloneConfigError("transfer_timeout_seconds must be > 0")
        if self.bwlimit_kbps < 0:
            raise RcloneConfigError("bwlimit_kbps must be >= 0")
        if self.retries < 0:
            raise RcloneConfigError("retries must be >= 0")


@dataclass(frozen=True, slots=True)
class RcloneRemote:
    name: str
    provider: str
    backend: str
    root: str
    config_path: Path


@dataclass(frozen=True, slots=True)
class RcloneVersion:
    binary_path: Path
    version: str


@dataclass(frozen=True, slots=True)
class RcloneEntry:
    name: str
    path: str
    is_dir: bool
    size_bytes: int | None = None
    mime_type: str | None = None
    modified_at: str | None = None


@dataclass(frozen=True, slots=True)
class RcloneListing:
    remote: RcloneRemote
    path: str
    entries: tuple[RcloneEntry, ...]


@dataclass(frozen=True, slots=True)
class RcloneStats:
    remote: RcloneRemote
    path: str
    total_bytes: int | None
    used_bytes: int | None
    free_bytes: int | None
    trashed_bytes: int | None
    object_count: int | None
    size_bytes: int | None


@dataclass(frozen=True, slots=True)
class RcloneTransferProgress:
    summary: str
    transferred: str | None = None
    total: str | None = None
    percent: float | None = None
    speed: str | None = None
    eta: str | None = None
    raw_line: str = ""


@dataclass(frozen=True, slots=True)
class RcloneTransferResult:
    remote: RcloneRemote
    operation: str
    source_path: Path
    destination: str
    returncode: int
    stdout: str
    stderr: str
    cancelled: bool
    progress: RcloneTransferProgress | None
    log_path: Path


class CloudRcloneService:
    """Render OAuth-backed rclone configs and execute rclone subprocesses."""

    def __init__(
        self,
        config: RcloneServiceConfig,
        oauth_service: CloudOAuthService,
        generic_remote_service: "GenericRemoteService | None" = None,
    ) -> None:
        config.validate()
        self._config = config
        self._oauth_service = oauth_service
        self._generic_remote_service = generic_remote_service
        self._lock = threading.RLock()
        self._active_process: subprocess.Popen[str] | None = None
        self._active_progress: RcloneTransferProgress | None = None
        self._active_cancelled = False
        self._bwlimit_kbps_override: int | None = None
        self._degraded_bwlimit_kbps: int | None = None
        self._remote_path_override: str | None = None

    def set_bwlimit_kbps_override(self, value: int | None) -> None:
        """Override the rclone bandwidth limit at runtime.

        Pass ``None`` (or a negative value) to clear the override and fall back
        to ``self._config.bwlimit_kbps``. Used by the cloud archive settings
        page to apply a saved value without a service restart.
        """
        if value is None or value < 0:
            self._bwlimit_kbps_override = None
        else:
            self._bwlimit_kbps_override = int(value)

    def set_degraded_bwlimit_kbps(self, value: int | None) -> None:
        """Apply a transient, more-restrictive cap from the WiFi watchdog.

        Driven by the cloud-archive uploader when wifi-watchdog.sh raises
        ``/run/teslausb/wifi_degraded`` (the BCM43436 SDIO chip is congesting
        but not yet wedged). This COMPOSES with — it does not clobber — the
        settings-page value set via :meth:`set_bwlimit_kbps_override`: the
        effective limit is the more restrictive of the two. Pass ``None`` (or
        a negative value) to clear it once WiFi recovers.
        """
        if value is None or value < 0:
            self._degraded_bwlimit_kbps = None
        else:
            self._degraded_bwlimit_kbps = int(value)

    def _effective_bwlimit_kbps(self) -> int:
        base = (
            self._bwlimit_kbps_override
            if self._bwlimit_kbps_override is not None
            else self._config.bwlimit_kbps
        )
        degraded = self._degraded_bwlimit_kbps
        if degraded is None:
            return base
        # bwlimit semantics: 0 == unlimited, positive == cap in KB/s. The
        # degraded cap wins whenever it is the more restrictive of the two.
        if base <= 0:
            return degraded
        return min(base, degraded)

    def set_remote_path_override(self, value: str | None) -> None:
        """Override the base remote folder prefix used for uploads."""
        if value is None:
            self._remote_path_override = None
            return
        cleaned = value.strip().replace("\\", "/").strip("/")
        self._remote_path_override = cleaned or None

    def effective_remote_path(self) -> str:
        return self._remote_path_override or ""

    def _join_remote_path(self, remote_path: str) -> str:
        base = self.effective_remote_path()
        suffix = remote_path.strip().replace("\\", "/").strip("/")
        if base and suffix:
            return f"{base}/{suffix}"
        return base or suffix

    def mkdir(self, remote_path: str) -> None:
        """Create a folder on the remote (rclone mkdir)."""
        remote, _credentials = self._prepare_remote()
        clean = _validate_remote_path(remote_path)
        if not clean:
            raise RcloneConfigError("Remote path must not be empty")
        spec = _remote_spec(remote.name, clean)
        completed = self._run_rclone(
            ["mkdir", spec],
            timeout=self._command_timeout_seconds(),
        )
        if completed.returncode != 0:
            raise self._command_error("mkdir", completed, error_type=RcloneError)

    def list_files_recursive(self, remote_path: str = "") -> tuple[RcloneEntry, ...]:
        """Recursively list files under ``remote_path``, preserving ModTime."""
        remote, _credentials = self._prepare_remote()
        clean = _validate_remote_path(self._join_remote_path(remote_path))
        command = ["lsjson", "-R", "--files-only", _remote_spec(remote.name, clean)]
        completed = self._run_rclone(command, timeout=self._command_timeout_seconds())
        if completed.returncode != 0:
            if _is_missing_directory(completed.stderr):
                return ()
            raise self._command_error("lsjson", completed, error_type=RcloneError)
        return _parse_listing(completed.stdout, base_path=clean)

    def deletefile(self, remote_path: str) -> None:
        """Delete a single object on the remote."""
        remote, _credentials = self._prepare_remote()
        clean = _validate_remote_path(remote_path)
        if not clean:
            raise RcloneConfigError("Remote path must not be empty")
        spec = _remote_spec(remote.name, clean)
        completed = self._run_rclone(
            ["deletefile", spec],
            timeout=self._command_timeout_seconds(),
        )
        if completed.returncode != 0:
            raise self._command_error("deletefile", completed, error_type=RcloneError)

    @property
    def oauth_service(self) -> CloudOAuthService:
        return self._oauth_service

    @property
    def config_file_path(self) -> Path:
        return self._config.rclone_config_dir / _RCLONE_CONFIG_FILENAME

    @property
    def log_path(self) -> Path:
        return self._config.rclone_log_path

    def get_version(self) -> RcloneVersion:
        binary_path = self._resolve_binary_path()
        completed = self._run_text_command(
            [str(binary_path), "version", "--json"],
            timeout=self._command_timeout_seconds(),
        )
        if completed.returncode != 0:
            raise self._command_error("version", completed, error_type=RcloneError)
        version = _parse_version(completed.stdout)
        if not version:
            raise RcloneError("rclone version output did not include a version string")
        return RcloneVersion(binary_path=binary_path, version=version)

    def render_config(self) -> RcloneRemote:
        remote, _credentials = self._prepare_remote()
        return remote

    def list_remotes(self) -> tuple[RcloneRemote, ...]:
        remote, _credentials = self._prepare_remote()
        completed = self._run_rclone(["listremotes"], timeout=self._command_timeout_seconds())
        if completed.returncode != 0:
            raise self._command_error("listremotes", completed, error_type=RcloneError)
        names = tuple(line.rstrip(":") for line in completed.stdout.splitlines() if line.strip())
        return (remote,) if remote.name in names else ()

    def list_directory(self, remote_path: str = "") -> RcloneListing:
        return self._list(remote_path=remote_path, files_only=False)

    def list_files(self, remote_path: str = "") -> RcloneListing:
        return self._list(remote_path=remote_path, files_only=True)

    def get_stats(self, remote_path: str = "") -> RcloneStats:
        remote, _credentials = self._prepare_remote()
        clean_remote_path = _validate_remote_path(remote_path)
        root_spec = remote.root
        target_spec = _remote_spec(remote.name, clean_remote_path)
        about = self._read_json_command(
            ["about", "--json", root_spec],
            timeout=self._command_timeout_seconds(),
            operation="about",
        )
        size = self._read_json_command(
            ["size", "--json", target_spec],
            timeout=self._command_timeout_seconds(),
            operation="size",
        )
        return RcloneStats(
            remote=remote,
            path=clean_remote_path,
            total_bytes=_json_int(about, "total"),
            used_bytes=_json_int(about, "used"),
            free_bytes=_json_int(about, "free"),
            trashed_bytes=_json_int(about, "trashed"),
            object_count=_json_int(size, "count"),
            size_bytes=_json_int(size, "bytes"),
        )

    def transfer(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        operation: str = "copy",
        progress_callback: Callable[[RcloneTransferProgress], None] | None = None,
    ) -> RcloneTransferResult:
        if operation not in {"copy", "move", "sync"}:
            raise RcloneConfigError(f"Unsupported rclone transfer operation: {operation}")
        source_path = self._resolve_local_path(Path(local_path))
        remote, _credentials = self._prepare_remote()
        destination_path = _validate_remote_path(self._join_remote_path(remote_path))
        destination_spec = _remote_spec(remote.name, destination_path)
        _rotate_log_file(self._config.rclone_log_path)
        command = self._build_transfer_command(
            operation=operation,
            source_path=source_path,
            destination_spec=destination_spec,
        )
        popen_text = cast("_SubprocessPopenText", subprocess.__dict__["Popen"])
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        with self._lock:
            if self._active_process is not None:
                raise RcloneTransferError("Another rclone transfer is already running")
            self._active_cancelled = False
            self._active_progress = None
            try:
                process = popen_text(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    preexec_fn=_lower_rclone_priority,
                )
            except FileNotFoundError as exc:
                raise RcloneNotInstalledError(
                    f"rclone binary not found: {self._config.rclone_binary}"
                ) from exc
            self._active_process = process
        threads = (
            threading.Thread(
                target=self._drain_stream,
                args=(process.stdout, stdout_buffer, None),
                daemon=True,
            ),
            threading.Thread(
                target=self._drain_stream,
                args=(process.stderr, stderr_buffer, progress_callback),
                daemon=True,
            ),
        )
        for thread in threads:
            thread.start()
        try:
            try:
                process.wait(timeout=self._config.transfer_timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                self._active_cancelled = False
                _kill_process(process)
                raise RcloneTransferError(
                    f"rclone {operation} timed out after {self._config.transfer_timeout_seconds}s"
                ) from exc
            for thread in threads:
                thread.join(timeout=1.0)
            stdout_text = stdout_buffer.getvalue()
            stderr_text = stderr_buffer.getvalue()
            cancelled = self._active_cancelled
            result = RcloneTransferResult(
                remote=remote,
                operation=operation,
                source_path=source_path,
                destination=destination_path,
                returncode=process.returncode if process.returncode is not None else -1,
                stdout=stdout_text,
                stderr=stderr_text,
                cancelled=cancelled,
                progress=self._active_progress,
                log_path=self._config.rclone_log_path,
            )
            if cancelled:
                logger.info(
                    "Cancelled rclone %s from %s to %s", operation, source_path, destination_spec
                )
                return result
            if result.returncode != 0:
                if _is_auth_error(stderr_text):
                    raise RcloneAuthError(
                        _error_message(result.returncode, stderr_text, stdout_text)
                    )
                raise RcloneTransferError(
                    _error_message(result.returncode, stderr_text, stdout_text)
                )
            if os.name != "nt" and result.log_path.exists():
                with contextlib.suppress(OSError):
                    result.log_path.chmod(0o600)
            return result
        finally:
            with self._lock:
                self._active_process = None
                self._active_progress = None
                self._active_cancelled = False

    def cancel_active_transfer(self) -> bool:
        with self._lock:
            process = self._active_process
            if process is None:
                return False
            self._active_cancelled = True
        _kill_process(process)
        return True

    def current_progress(self) -> RcloneTransferProgress | None:
        with self._lock:
            return self._active_progress

    def _list(self, *, remote_path: str, files_only: bool) -> RcloneListing:
        remote, _credentials = self._prepare_remote()
        clean_remote_path = _validate_remote_path(self._join_remote_path(remote_path))
        command = ["lsjson", "--no-modtime"]
        if files_only:
            command.append("--files-only")
        command.append(_remote_spec(remote.name, clean_remote_path))
        completed = self._run_rclone(command, timeout=self._command_timeout_seconds())
        if completed.returncode != 0:
            if _is_missing_directory(completed.stderr):
                return RcloneListing(remote=remote, path=clean_remote_path, entries=())
            raise self._command_error("lsjson", completed, error_type=RcloneError)
        entries = _parse_listing(completed.stdout, base_path=clean_remote_path)
        return RcloneListing(remote=remote, path=clean_remote_path, entries=entries)

    def _prepare_remote(self) -> tuple[RcloneRemote, OAuthCredentials | None]:
        # Generic (non-OAuth) remotes — S3, B2, Wasabi, SFTP, WebDAV,
        # SMB, FTP, Azure Blob, Swift — take precedence. The operator
        # explicitly saved one, so use it over any stale OAuth blob.
        if self._generic_remote_service is not None:
            generic_record = self._generic_remote_service.load()
            if generic_record is not None:
                backend = generic_record.get("type", "")
                config_text = _render_generic_config_text(generic_record)
                _write_text_atomically(self.config_file_path, config_text)
                remote = RcloneRemote(
                    name=_RCLONE_REMOTE_NAME,
                    provider=f"generic:{backend}",
                    backend=backend,
                    root=f"{_RCLONE_REMOTE_NAME}:",
                    config_path=self.config_file_path,
                )
                return remote, None
        credentials = self._load_fresh_credentials()
        provider = credentials.provider
        try:
            backend = _PROVIDER_BACKENDS[provider]
        except KeyError as exc:
            raise RcloneAuthError(f"Unsupported cloud provider for rclone: {provider}") from exc
        config_text = self._render_config_text(provider, backend, credentials)
        _write_text_atomically(self.config_file_path, config_text)
        remote = RcloneRemote(
            name=_RCLONE_REMOTE_NAME,
            provider=provider,
            backend=backend,
            root=f"{_RCLONE_REMOTE_NAME}:",
            config_path=self.config_file_path,
        )
        return remote, credentials

    def has_configured_remote(self) -> bool:
        """Return True iff either an OAuth or a generic rclone remote is saved.

        This is the lightweight predicate the cloud-archive worker uses to
        decide whether to attempt a sync drain. It performs no subprocess
        work and tolerates missing credential files (returns ``False``).
        """
        if self._generic_remote_service is not None:
            try:
                if self._generic_remote_service.load() is not None:
                    return True
            except Exception:  # pragma: no cover - defensive
                pass
        try:
            return self._oauth_service.load_credentials() is not None
        except Exception:  # pragma: no cover - defensive
            return False

    def _load_fresh_credentials(self) -> OAuthCredentials:
        from teslausb_web.services.cloud_oauth_service import TokenRefreshError

        try:
            credentials = self._oauth_service.load_credentials()
        except Exception as exc:
            raise RcloneAuthError(str(exc)) from exc
        if credentials is None:
            raise RcloneAuthError("No stored OAuth credentials")
        try:
            refreshed = self._oauth_service.refresh_if_needed(provider=credentials.provider)
            return refreshed.credentials if refreshed.credentials is not None else credentials
        except TokenRefreshError as exc:
            logger.warning(
                "Preemptive OAuth refresh failed for %s (%s); trusting rclone "
                "to refresh on demand using its embedded client credentials.",
                credentials.provider,
                exc,
            )
            return credentials
        except Exception as exc:
            if isinstance(exc, RcloneAuthError):
                raise
            raise RcloneAuthError(str(exc)) from exc

    def _render_config_text(
        self,
        provider: str,
        backend: str,
        credentials: OAuthCredentials,
    ) -> str:
        lines = [
            f"[{_RCLONE_REMOTE_NAME}]",
            f"type = {backend}",
            f"token = {_rclone_token_json(credentials)}",
        ]
        if provider == "onedrive":
            lines.append("drive_type = personal")
            drive_id = _discover_onedrive_drive_id(
                credentials,
                timeout=self._command_timeout_seconds(),
            )
            if not drive_id:
                drive_id = _read_cached_drive_id(self.config_file_path)
                if drive_id:
                    logger.info(
                        "Reusing cached OneDrive drive_id from existing "
                        "rclone.conf because Graph discovery failed."
                    )
            if not drive_id:
                raise RcloneAuthError(
                    "Could not reach Microsoft Graph to identify the OneDrive "
                    "drive. Check the Pi's internet connection and click "
                    "Connect again."
                )
            lines.append(f"drive_id = {drive_id}")
        return "\n".join(lines) + "\n"

    def _run_rclone(
        self,
        command: list[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        binary_path = self._resolve_binary_path()
        argv = [str(binary_path), "--config", str(self.config_file_path)]
        if self._config.retries > 0:
            argv.extend(["--retries", str(self._config.retries)])
        argv.extend(command)
        return self._run_text_command(argv, timeout=timeout)

    def _run_text_command(
        self,
        command: list[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        run_text = cast("_SubprocessRunText", subprocess.__dict__["run"])
        try:
            # argv is an explicit list, the binary path is resolved up-front, and shell is disabled.
            return run_text(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise RcloneNotInstalledError(
                f"rclone binary not found: {self._config.rclone_binary}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RcloneError(f"rclone command timed out after {timeout}s") from exc

    def _read_json_command(
        self,
        command: list[str],
        *,
        timeout: float,
        operation: str,
    ) -> Mapping[str, object]:
        completed = self._run_rclone(command, timeout=timeout)
        if completed.returncode != 0:
            raise self._command_error(operation, completed, error_type=RcloneError)
        try:
            payload: object = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RcloneError(f"rclone {operation} returned invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RcloneError(f"rclone {operation} JSON response must be an object")
        return {str(key): value for key, value in payload.items()}

    def _resolve_binary_path(self) -> Path:
        configured = self._config.rclone_binary.strip()
        candidate = Path(configured)
        if candidate.is_absolute():
            return candidate
        resolved = shutil.which(configured)
        if resolved is None:
            raise RcloneNotInstalledError(f"rclone binary not found in PATH: {configured}")
        return Path(resolved)

    def _resolve_local_path(self, path: Path) -> Path:
        resolved = path.resolve(strict=False)
        if not path.exists():
            raise RcloneConfigError(f"Local path does not exist: {path}")
        if not any(
            _is_relative_to(resolved, root.resolve(strict=False))
            for root in self._config.allowed_local_roots
        ):
            raise RcloneConfigError(f"Local path must stay within configured roots: {resolved}")
        return resolved

    def _build_transfer_command(
        self,
        *,
        operation: str,
        source_path: Path,
        destination_spec: str,
    ) -> list[str]:
        binary_path = self._resolve_binary_path()
        # rclone `copy` / `move` treat DEST as a directory, so passing
        # `dest/foo.mp4` puts the file at `dest/foo.mp4/foo.mp4`. We always
        # transfer one file at a time with the full destination path, so
        # use the exact-path variants (`copyto` / `moveto`). `sync` operates
        # on directories and stays as-is.
        subcommand = {"copy": "copyto", "move": "moveto"}.get(operation, operation)
        command = [
            str(binary_path),
            "--config",
            str(self.config_file_path),
            "--retries",
            str(self._config.retries),
            subcommand,
            "--stats",
            "1s",
            "--stats-one-line",
            "--log-file",
            str(self._config.rclone_log_path),
            "--log-level",
            "INFO",
        ]
        bwlimit = self._effective_bwlimit_kbps()
        if bwlimit > 0:
            command.extend(["--bwlimit", f"{bwlimit}k"])
        # Polite flags for the BCM43436 SDIO WiFi chip on the Pi
        # Zero 2 W. Sustained, fully parallel TX from rclone reliably
        # wedges the brcmfmac firmware (`HT Avail request error`,
        # `err=-110`) — single-stream, low-buffer, throttled API
        # calls keep the chip in a happy regime even on long upload
        # runs. See deploy/wifi-stability/wifi-watchdog.sh for the
        # recovery side of this contract.
        command.extend(
            [
                "--transfers", "1",
                "--checkers", "1",
                "--tpslimit", "4",
                "--buffer-size", "4M",
                "--use-mmap",
                "--low-level-retries", "3",
            ]
        )
        command.extend([str(source_path), destination_spec])
        return command

    def _command_error(
        self,
        operation: str,
        completed: subprocess.CompletedProcess[str],
        *,
        error_type: type[RcloneError],
    ) -> RcloneError:
        message = _error_message(completed.returncode, completed.stderr, completed.stdout)
        if _is_auth_error(completed.stderr):
            return RcloneAuthError(message)
        return error_type(f"rclone {operation} failed: {message}")

    def _command_timeout_seconds(self) -> float:
        return min(float(self._config.transfer_timeout_seconds), _SHORT_COMMAND_TIMEOUT_SECONDS)

    def _drain_stream(
        self,
        stream: io.TextIOBase | None,
        sink: io.StringIO,
        progress_callback: Callable[[RcloneTransferProgress], None] | None,
    ) -> None:
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                sink.write(line)
                progress = _parse_progress(line)
                if progress is not None:
                    with self._lock:
                        self._active_progress = progress
                    if progress_callback is not None:
                        progress_callback(progress)
            remainder = stream.read()
            if remainder:
                sink.write(remainder)
                for line in remainder.splitlines():
                    progress = _parse_progress(line)
                    if progress is not None:
                        with self._lock:
                            self._active_progress = progress
                        if progress_callback is not None:
                            progress_callback(progress)
        finally:
            with contextlib.suppress(OSError):
                stream.close()


def _validate_remote_path(remote_path: str) -> str:
    candidate = remote_path.strip().replace("\\", "/").strip("/")
    if not candidate:
        return ""
    if "\x00" in candidate:
        raise RcloneConfigError("Remote path must not contain NUL bytes")
    parts = PurePosixPath(candidate).parts
    if any(part == ".." for part in parts):
        raise RcloneConfigError(f"Remote path traversal is not allowed: {remote_path!r}")
    return candidate


def _remote_spec(remote_name: str, remote_path: str) -> str:
    return f"{remote_name}:{remote_path}" if remote_path else f"{remote_name}:"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_auth_error(stderr_text: str) -> bool:
    lowered = stderr_text.lower()
    return any(token in lowered for token in _AUTH_ERROR_PATTERNS)


def _is_missing_directory(stderr_text: str) -> bool:
    lowered = stderr_text.lower()
    return any(token in lowered for token in _MISSING_DIRECTORY_PATTERNS)


def _parse_version(stdout_text: str) -> str:
    try:
        payload: object = json.loads(stdout_text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        version = payload.get("version")
        if isinstance(version, str) and version.strip():
            return version
    first_line = next((line.strip() for line in stdout_text.splitlines() if line.strip()), "")
    return (
        first_line.split()[1]
        if first_line.startswith("rclone ") and len(first_line.split()) > 1
        else first_line
    )


def _parse_listing(stdout_text: str, *, base_path: str) -> tuple[RcloneEntry, ...]:
    try:
        payload: object = json.loads(stdout_text) if stdout_text.strip() else []
    except json.JSONDecodeError as exc:
        raise RcloneError(f"rclone lsjson returned invalid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise RcloneError("rclone lsjson JSON response must be an array")
    entries: list[RcloneEntry] = []
    for item in payload:
        if not isinstance(item, dict):
            raise RcloneError("rclone lsjson entries must be JSON objects")
        name = _json_string(item, "Name")
        if not name:
            continue
        item_path = name if not base_path else f"{base_path}/{name}"
        entries.append(
            RcloneEntry(
                name=name,
                path=item_path,
                is_dir=_json_bool(item, "IsDir"),
                size_bytes=_json_int(item, "Size"),
                mime_type=_json_optional_string(item, "MimeType"),
                modified_at=_json_optional_string(item, "ModTime"),
            )
        )
    entries.sort(key=lambda entry: entry.name.lower())
    return tuple(entries)


def _json_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise RcloneError(f"Expected string field {key!r} in rclone output")
    return value


def _json_optional_string(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RcloneError(f"Expected optional string field {key!r} in rclone output")
    return value


def _json_bool(payload: Mapping[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise RcloneError(f"Expected boolean field {key!r} in rclone output")
    return value


def _json_int(payload: Mapping[str, object], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise RcloneError(f"Expected integer field {key!r} in rclone output")
    return value


def _parse_progress(line: str) -> RcloneTransferProgress | None:
    stripped = line.strip()
    if not stripped:
        return None
    transferred_marker = stripped.find("Transferred:")
    if transferred_marker != -1:
        candidate = stripped[transferred_marker:]
        match = _TRANSFERRED_RE.search(candidate)
        if match is not None:
            percent = match.group("percent")
            return RcloneTransferProgress(
                summary=candidate,
                transferred=_clean_group(match.group("transferred")),
                total=_clean_group(match.group("total")),
                percent=float(percent) if percent is not None else None,
                speed=_clean_group(match.group("speed")),
                eta=_clean_group(match.group("eta")),
                raw_line=stripped,
            )
        return RcloneTransferProgress(summary=candidate, raw_line=stripped)
    progress_match = _PROGRESS_RE.search(stripped)
    if progress_match is not None:
        summary = progress_match.group("summary").strip()
        return RcloneTransferProgress(summary=summary, raw_line=stripped)
    return None


def _clean_group(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _rclone_token_json(credentials: OAuthCredentials) -> str:
    payload = {
        "access_token": credentials.access_token,
        "expiry": credentials.expires_at,
        "refresh_token": credentials.refresh_token,
        "token_type": credentials.token_type,
    }
    return json.dumps(payload, sort_keys=True)


def _render_generic_config_text(record: dict[str, str]) -> str:
    """Render a stored generic-remote dict as the rclone.conf body.

    Delegates to :func:`cloud_generic_remote_service.render_conf_body`
    so the format stays in lock-step with the storage layer. Imported
    locally to avoid a hard import cycle at module load.
    """
    from teslausb_web.services.cloud_generic_remote_service import render_conf_body

    return render_conf_body(record, remote_name=_RCLONE_REMOTE_NAME)


def _read_cached_drive_id(config_path: Path) -> str | None:
    """Return the previously-rendered OneDrive drive_id from rclone.conf, if any."""
    try:
        text = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # A common failure mode here is the conf having been rewritten by a
        # manual `sudo rclone ...` invocation, which leaves it owned by root
        # with 0o600 so the gunicorn (pi) process can no longer read it.
        # Log loudly so this doesn't silently masquerade as a Graph outage.
        logger.warning(
            "Could not read cached OneDrive drive_id from %s: %s. "
            "Check that the file is owned/readable by the web service user "
            "(commonly: sudo chown pi:pi %s).",
            config_path,
            exc,
            config_path,
        )
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("drive_id"):
            _, _, value = line.partition("=")
            value = value.strip()
            if value:
                return value
    return None


def _discover_onedrive_drive_id(
    credentials: OAuthCredentials,
    *,
    timeout: float,
    attempts: int = 3,
) -> str | None:
    last_error: str | None = None
    for attempt in range(1, max(1, attempts) + 1):
        request = Request(  # noqa: S310 - constant HTTPS Graph endpoint
            _GRAPH_DRIVE_URL,
            headers={"Authorization": f"Bearer {credentials.access_token}"},
        )
        try:
            response = cast(
                "_ReadableResponse",
                urlopen(  # noqa: S310  # nosec B310 - constant HTTPS Graph endpoint
                    request,
                    timeout=timeout,
                    context=ssl.create_default_context(),
                ),
            )
            with contextlib.closing(response):
                payload: object = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            logger.warning(
                "Could not discover OneDrive drive_id (attempt %d/%d): %s",
                attempt,
                attempts,
                exc,
            )
            continue
        if not isinstance(payload, dict):
            logger.warning(
                "Could not discover OneDrive drive_id: response was not a JSON object"
            )
            return None
        drive_id = payload.get("id")
        if not isinstance(drive_id, str) or not drive_id.strip():
            return None
        return drive_id.strip()
    if last_error is not None:
        logger.warning(
            "Giving up OneDrive drive_id discovery after %d attempts: %s",
            attempts,
            last_error,
        )
    return None


def _rotate_log_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rotated_path = path.with_name(f"{path.name}.1")
    with contextlib.suppress(FileNotFoundError, OSError):
        rotated_path.unlink()
    if not path.exists():
        return
    path.replace(rotated_path)


def _write_text_atomically(path: Path, content: str) -> None:
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
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            file_descriptor = None
            handle.write(content)
            handle.flush()
            _best_effort_fsync(handle.fileno())
        temp_path.replace(path)
        if os.name != "nt":
            path.chmod(0o600)
    except OSError as exc:
        raise RcloneError(f"Failed to write {path}: {exc}") from exc
    finally:
        if file_descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(file_descriptor)
        if temp_path is not None and temp_path.exists():
            with contextlib.suppress(OSError):
                temp_path.unlink()


def _best_effort_fsync(file_descriptor: int) -> None:
    with contextlib.suppress(OSError):
        os.fsync(file_descriptor)


def _lower_rclone_priority() -> None:
    """preexec_fn for the rclone child: drop CPU + I/O priority.

    Runs in the forked child between fork() and execve(). Lowering
    priority here (rather than reniceing the parent gunicorn) keeps
    the HTTP-serving threads at default priority — only the heavy
    rclone subprocess gets pushed to the back of the run-queue.

    Best-effort: any OSError is swallowed so a hardened sandbox
    that forbids setpriority/ioprio_set never blocks an upload.
    """
    # CPU: bump nice to +19 (lowest). os.nice is portable; on the Pi
    # this asks the scheduler to give us CPU only when nothing else
    # wants it.
    with contextlib.suppress(OSError):
        os.nice(19)
    # I/O: ask the kernel for the idle ioprio class via the raw
    # ioprio_set(2) syscall. Constants:
    #   which=1 (IOPRIO_WHO_PROCESS), who=0 (this process)
    #   class=3 (IDLE) packed into bits 13..15.
    # We use the syscall directly because Python stdlib has no
    # ionice equivalent. SYS_ioprio_set differs per arch:
    #   x86_64=251, arm/arm64 (and Pi)=30/289 — but Pi userspace
    #   actually runs as arm/arm64 with syscall 30 (arm32) or 30
    #   (arm64). To stay portable AND safe we use ctypes only if
    #   syscall() is available, and silently no-op otherwise.
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        # IOPRIO_CLASS_IDLE = 3 in the high bits.
        ioprio = (3 << 13)
        # Try the most common arches; first non-EINVAL wins.
        for sys_no in (251, 289, 30):
            rc = libc.syscall(sys_no, 1, 0, ioprio)
            if rc == 0:
                break
    except Exception:  # noqa: BLE001 — preexec must never raise
        pass


def _warn_windows_permissions_once() -> None:
    with _WINDOWS_WARNING_LOCK:
        if _WINDOWS_WARNING_STATE["emitted"]:
            return
        logger.warning(_WINDOWS_PERMS_WARNING)
        _WINDOWS_WARNING_STATE["emitted"] = True


def _error_message(returncode: int, stderr: str, stdout: str) -> str:
    detail = stderr.strip() or stdout.strip() or "unknown error"
    return f"exit {returncode}: {detail}"


def _kill_process(process: subprocess.Popen[str]) -> None:
    with contextlib.suppress(ProcessLookupError, OSError):
        process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        process.wait(timeout=5)


def make_rclone_service(
    cfg: WebConfig,
    oauth_service: CloudOAuthService,
    generic_remote_service: "GenericRemoteService | None" = None,
) -> CloudRcloneService:
    """Build an rclone service rooted at the configured cloud state paths."""
    if generic_remote_service is None:
        from teslausb_web.services.cloud_generic_remote_service import (
            make_generic_remote_service,
        )

        generic_remote_service = make_generic_remote_service(cfg)
    return CloudRcloneService(
        RcloneServiceConfig(
            rclone_config_dir=cfg.cloud.rclone_config_path,
            rclone_log_path=cfg.cloud.rclone_log_path,
            allowed_local_roots=(cfg.paths.backing_root, cfg.paths.state_dir),
            rclone_binary=cfg.cloud.rclone_binary,
            transfer_timeout_seconds=cfg.cloud.transfer_timeout_seconds,
            bwlimit_kbps=cfg.cloud.bwlimit_kbps,
            retries=cfg.cloud.retries,
        ),
        oauth_service,
        generic_remote_service=generic_remote_service,
    )


__all__ = (
    "CloudRcloneService",
    "RcloneAuthError",
    "RcloneConfigError",
    "RcloneEntry",
    "RcloneError",
    "RcloneListing",
    "RcloneNotInstalledError",
    "RcloneRemote",
    "RcloneServiceConfig",
    "RcloneStats",
    "RcloneTransferError",
    "RcloneTransferProgress",
    "RcloneTransferResult",
    "RcloneVersion",
    "make_rclone_service",
)
