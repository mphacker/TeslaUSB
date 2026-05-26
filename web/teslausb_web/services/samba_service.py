"""Samba daemon orchestration and config rendering for the B-1 web app."""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

_COMMAND_TIMEOUT_SECONDS: float = 10.0
_STOP_TIMEOUT_SECONDS: float = 5.0
_KILL_WAIT_SECONDS: float = 1.0
_DEFAULT_SHARE_NAME: str = "TeslaCam"
_DEFAULT_MEDIA_SHARE_NAME: str = "Media"
_FILE_MODE_PUBLIC_READ: int = 0o644


class _RunTextCommand(Protocol):
    def __call__(
        self,
        command: list[str],
        /,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]: ...


class _ManagedProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...

    def terminate(self) -> None: ...


class _PopenBinaryCommand(Protocol):
    def __call__(
        self,
        command: list[str],
        /,
        **_kwargs: object,
    ) -> _ManagedProcess: ...


class SambaError(RuntimeError):
    """Base error for Samba service failures."""


class SambaCommandError(SambaError):
    """Raised when a Samba subprocess fails or times out."""


class SambaConfigError(ValueError):
    """Raised when the Samba service configuration is invalid."""


class SambaNotInstalledError(SambaError):
    """Raised when the configured Samba binaries cannot be resolved."""


@dataclass(frozen=True, slots=True)
class SambaShare:
    name: str
    path: Path
    comment: str
    read_only: bool = False
    browseable: bool = True
    guest_ok: bool = False


@dataclass(frozen=True, slots=True)
class SambaStatus:
    running: bool
    pid_or_none: int | None
    last_error_or_none: str | None
    since: float | None


@dataclass(frozen=True, slots=True)
class SambaConfigRenderResult:
    config_path: Path
    rendered_text: str
    shares: tuple[SambaShare, ...]


@dataclass(frozen=True, slots=True)
class SambaServiceConfig:
    config_path: Path
    shares: tuple[SambaShare, ...]
    allowed_roots: tuple[Path, ...]
    binary_smbd: str = "smbd"
    binary_smbcontrol: str = "smbcontrol"
    binary_systemctl: str = "systemctl"
    # Command prefix prepended to every subprocess invocation. Production
    # deploys this as ("sudo", "-n") because the gunicorn worker runs as
    # the unprivileged `pi` user but `systemctl start smbd` needs root.
    # Tests and dev-box CI leave this empty so command assertions stay
    # readable. The Pi's sudoers grants `pi` NOPASSWD: ALL via
    # /etc/sudoers.d/010_pi-nopasswd, so no policy work is needed.
    sudo_prefix: tuple[str, ...] = ()
    command_timeout_seconds: float = _COMMAND_TIMEOUT_SECONDS
    stop_timeout_seconds: float = _STOP_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if (
            not self.config_path.is_absolute()
            and not PurePosixPath(self.config_path.as_posix()).is_absolute()
        ):
            raise SambaConfigError(f"config_path must be absolute, got {self.config_path!r}")
        if not self.allowed_roots:
            raise SambaConfigError("allowed_roots must be non-empty")
        for index, root in enumerate(self.allowed_roots):
            if not root.is_absolute() and not PurePosixPath(root.as_posix()).is_absolute():
                raise SambaConfigError(f"allowed_roots[{index}] must be absolute, got {root!r}")
        for field_name, value in (
            ("binary_smbd", self.binary_smbd),
            ("binary_smbcontrol", self.binary_smbcontrol),
            ("binary_systemctl", self.binary_systemctl),
        ):
            if not value.strip():
                raise SambaConfigError(f"{field_name} must be non-empty")
        if self.command_timeout_seconds <= 0:
            raise SambaConfigError("command_timeout_seconds must be > 0")
        if self.stop_timeout_seconds <= 0:
            raise SambaConfigError("stop_timeout_seconds must be > 0")
        for index, prefix_token in enumerate(self.sudo_prefix):
            if not isinstance(prefix_token, str) or not prefix_token.strip():
                raise SambaConfigError(
                    f"sudo_prefix[{index}] must be a non-empty string"
                )
        seen_names: set[str] = set()
        for share in self.shares:
            if not share.name.strip():
                raise SambaConfigError("share name must be non-empty")
            if share.name in seen_names:
                raise SambaConfigError(f"duplicate share name: {share.name}")
            seen_names.add(share.name)


class SambaService:
    """Render smb.conf and manage the smbd daemon lifecycle."""

    def __init__(
        self,
        config: SambaServiceConfig,
        *,
        which: Callable[[str], str | None] | None = None,
        run_command: _RunTextCommand | None = None,
        popen_command: _PopenBinaryCommand | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        config.validate()
        self._config = config
        self._which = shutil.which if which is None else which
        self._run_command = subprocess.run if run_command is None else run_command
        self._popen_command = subprocess.Popen if popen_command is None else popen_command
        self._monotonic = time.monotonic if monotonic is None else monotonic
        self._lock = threading.RLock()
        self._process: _ManagedProcess | None = None
        self._managed_by_systemctl = False
        self._since: float | None = None
        self._last_error: str | None = None

    @property
    def config(self) -> SambaServiceConfig:
        return self._config

    def render_config(self) -> SambaConfigRenderResult:
        shares = self._validated_shares()
        rendered_text = self._render_smb_conf(shares)
        self._install_config_text(rendered_text)
        logger.info("Rendered Samba config to %s", self._config.config_path)
        return SambaConfigRenderResult(
            config_path=self._config.config_path,
            rendered_text=rendered_text,
            shares=shares,
        )

    def _install_config_text(self, text: str) -> None:
        """Atomically write smb.conf, using sudo install(1) when needed.

        The web app process typically runs as an unprivileged user that
        cannot write into ``/etc/samba``. When ``sudo_prefix`` is
        configured we stage the file under ``/tmp`` (writable) and then
        invoke ``sudo install -m 0644 <tmp> <dest>`` to put it in
        place. ``install`` is atomic on the destination filesystem.
        """
        target = self._config.config_path
        if not self._config.sudo_prefix:
            _atomic_write_text(target, text)
            return
        fd, staged_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=tempfile.gettempdir()
        )
        staged_path = Path(staged_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            install_cmd = [
                *self._config.sudo_prefix,
                "install",
                "-m",
                f"0{_FILE_MODE_PUBLIC_READ:o}",
                "-o",
                "root",
                "-g",
                "root",
                str(staged_path),
                str(target),
            ]
            completed = self._run_text(install_cmd)
            if completed.returncode != 0:
                raise SambaError(
                    f"failed to install Samba config to {target}: "
                    f"{self._command_detail(completed)}"
                )
        except OSError as exc:
            raise SambaError(f"failed to stage Samba config: {exc}") from exc
        finally:
            staged_path.unlink(missing_ok=True)

    def start(self) -> SambaStatus:
        with self._lock:
            current = self._status_unlocked()
            if current.running:
                return current
        self.render_config()
        systemctl_binary = self._resolve_binary(self._config.binary_systemctl)
        if systemctl_binary is not None:
            completed = self._run_text(
                [
                    *self._config.sudo_prefix,
                    str(systemctl_binary),
                    "start",
                    "smbd",
                ]
            )
            if completed.returncode != 0:
                raise self._command_error("start smbd", completed)
            with self._lock:
                self._managed_by_systemctl = True
                self._process = None
                self._since = self._monotonic()
                self._last_error = None
                return SambaStatus(
                    running=True,
                    pid_or_none=None,
                    last_error_or_none=None,
                    since=self._since,
                )

        smbd_binary = self._resolve_required_binary(self._config.binary_smbd)
        process = self._start_popen(smbd_binary)
        with self._lock:
            self._managed_by_systemctl = False
            self._process = process
            self._since = self._monotonic()
            self._last_error = None
            return SambaStatus(
                running=True,
                pid_or_none=process.pid,
                last_error_or_none=None,
                since=self._since,
            )

    def stop(self, timeout: float | None = None) -> SambaStatus:
        stop_timeout = self._config.stop_timeout_seconds if timeout is None else timeout
        if stop_timeout <= 0:
            raise SambaConfigError("timeout must be > 0")
        with self._lock:
            managed_by_systemctl = self._managed_by_systemctl
            process = self._process
            if not managed_by_systemctl and process is None:
                self._since = None
                return self._status_unlocked()
        if managed_by_systemctl:
            systemctl_binary = self._resolve_required_binary(self._config.binary_systemctl)
            completed = self._run_text(
                [
                    *self._config.sudo_prefix,
                    str(systemctl_binary),
                    "stop",
                    "smbd",
                ],
                timeout=min(stop_timeout, self._config.command_timeout_seconds),
            )
            if completed.returncode != 0:
                raise self._command_error("stop smbd", completed)
            with self._lock:
                self._managed_by_systemctl = False
                self._process = None
                self._since = None
                self._last_error = None
                return SambaStatus(
                    running=False,
                    pid_or_none=None,
                    last_error_or_none=None,
                    since=None,
                )
        if process is None:
            with self._lock:
                self._since = None
                return SambaStatus(
                    running=False,
                    pid_or_none=None,
                    last_error_or_none=self._last_error,
                    since=None,
                )
        self._graceful_shutdown_process(process, stop_timeout)
        with self._lock:
            self._process = None
            self._managed_by_systemctl = False
            self._since = None
            return SambaStatus(
                running=False,
                pid_or_none=None,
                last_error_or_none=self._last_error,
                since=None,
            )

    def status(self) -> SambaStatus:
        with self._lock:
            return self._status_unlocked()

    def _status_unlocked(self) -> SambaStatus:
        if self._managed_by_systemctl:
            systemctl_binary = self._resolve_binary(self._config.binary_systemctl)
            if systemctl_binary is None:
                self._last_error = "systemctl binary not found"
                return SambaStatus(
                    running=False,
                    pid_or_none=None,
                    last_error_or_none=self._last_error,
                    since=self._since,
                )
            completed = self._run_text(
                [
                    *self._config.sudo_prefix,
                    str(systemctl_binary),
                    "is-active",
                    "--quiet",
                    "smbd",
                ],
                timeout=self._config.command_timeout_seconds,
            )
            running = completed.returncode == 0
            if completed.returncode not in {0, 3}:
                self._last_error = self._command_detail(completed)
            elif running:
                self._last_error = None
            return SambaStatus(
                running=running,
                pid_or_none=None,
                last_error_or_none=self._last_error,
                since=self._since,
            )
        if self._process is None:
            return SambaStatus(
                running=False,
                pid_or_none=None,
                last_error_or_none=self._last_error,
                since=self._since,
            )
        returncode = self._process.poll()
        if returncode is None:
            return SambaStatus(
                running=True,
                pid_or_none=self._process.pid,
                last_error_or_none=self._last_error,
                since=self._since,
            )
        self._last_error = f"smbd exited with return code {returncode}"
        self._process = None
        self._since = None
        return SambaStatus(
            running=False,
            pid_or_none=None,
            last_error_or_none=self._last_error,
            since=None,
        )

    def _validated_shares(self) -> tuple[SambaShare, ...]:
        validated: list[SambaShare] = []
        for share in self._config.shares:
            resolved_path = share.path.resolve(strict=False)
            allowed_root_match = any(
                _is_relative_to(resolved_path, root.resolve(strict=False))
                for root in self._config.allowed_roots
            )
            if not allowed_root_match:
                raise SambaConfigError(
                    "share path "
                    f"{resolved_path} is outside allowed roots {self._config.allowed_roots}"
                )
            resolved_path.mkdir(parents=True, exist_ok=True)
            validated.append(
                SambaShare(
                    name=share.name,
                    path=resolved_path,
                    comment=share.comment,
                    read_only=share.read_only,
                    browseable=share.browseable,
                    guest_ok=share.guest_ok,
                )
            )
        return tuple(validated)

    def _render_smb_conf(self, shares: tuple[SambaShare, ...]) -> str:
        lines = [
            "[global]",
            "   workgroup = WORKGROUP",
            "   server role = standalone server",
            "   map to guest = Bad User",
            "   usershare allow guests = yes",
            "   load printers = no",
            "   printing = bsd",
            "   disable spoolss = yes",
            "",
        ]
        for share in shares:
            lines.extend(
                [
                    f"[{share.name}]",
                    f"   path = {share.path.as_posix()}",
                    f"   comment = {share.comment}",
                    f"   read only = {_samba_bool(enabled=share.read_only)}",
                    f"   browseable = {_samba_bool(enabled=share.browseable)}",
                    f"   guest ok = {_samba_bool(enabled=share.guest_ok)}",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    def _start_popen(self, smbd_binary: Path) -> _ManagedProcess:
        command = [
            *self._config.sudo_prefix,
            str(smbd_binary),
            "-FS",
            "--configfile",
            str(self._config.config_path),
        ]
        try:
            process = self._popen_command(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise SambaCommandError(f"failed to start smbd: {exc}") from exc
        if process.poll() is not None:
            raise SambaCommandError("smbd exited immediately after start")
        return process

    def _graceful_shutdown_process(self, process: _ManagedProcess, timeout: float) -> None:
        deadline = self._monotonic() + timeout
        smbcontrol_binary = self._resolve_binary(self._config.binary_smbcontrol)
        if smbcontrol_binary is not None:
            completed = self._run_text(
                [*self._config.sudo_prefix, str(smbcontrol_binary), "smbd", "shutdown"],
                timeout=min(timeout, self._config.command_timeout_seconds),
            )
            if completed.returncode != 0:
                logger.warning("smbcontrol shutdown failed: %s", self._command_detail(completed))
                process.terminate()
        else:
            process.terminate()
        remaining = max(0.0, deadline - self._monotonic())
        try:
            process.wait(timeout=remaining)
            self._last_error = None
            return
        except subprocess.TimeoutExpired:
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=_KILL_WAIT_SECONDS)
            self._last_error = (
                f"timed out waiting for smbd shutdown after {timeout:.1f}s; force-killed"
            )
            logger.warning(self._last_error)

    def _resolve_required_binary(self, candidate: str) -> Path:
        resolved = self._resolve_binary(candidate)
        if resolved is None:
            raise SambaNotInstalledError(f"required binary not found: {candidate}")
        return resolved

    def _resolve_binary(self, candidate: str) -> Path | None:
        resolved = self._which(candidate)
        return None if resolved is None else Path(resolved)

    def _run_text(
        self,
        command: list[str],
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        run_timeout = self._config.command_timeout_seconds if timeout is None else timeout
        try:
            return self._run_command(
                command,
                capture_output=True,
                text=True,
                timeout=run_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SambaCommandError(
                f"command timed out after {run_timeout:.1f}s: {' '.join(command)}"
            ) from exc
        except OSError as exc:
            raise SambaCommandError(f"failed to run {' '.join(command)}: {exc}") from exc

    def _command_error(
        self,
        action: str,
        completed: subprocess.CompletedProcess[str],
    ) -> SambaCommandError:
        return SambaCommandError(f"{action} failed: {self._command_detail(completed)}")

    def _command_detail(self, completed: subprocess.CompletedProcess[str]) -> str:
        detail = (completed.stderr or completed.stdout).strip()
        if not detail:
            detail = f"exit code {completed.returncode}"
        return detail


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(_FILE_MODE_PUBLIC_READ)
        temp_path.replace(path)
        with contextlib.suppress(OSError):
            path.chmod(_FILE_MODE_PUBLIC_READ)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise SambaError(f"failed to write Samba config {path}: {exc}") from exc


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _samba_bool(*, enabled: bool) -> str:
    return "yes" if enabled else "no"


def _default_shares(cfg: WebConfig) -> tuple[SambaShare, ...]:
    if cfg.samba.shares:
        return tuple(
            SambaShare(
                name=share.name,
                path=share.path,
                comment=share.name,
                read_only=share.read_only,
                guest_ok=share.guest_ok,
            )
            for share in cfg.samba.shares
        )
    # No explicit shares configured -> expose both USB drive storage
    # locations: the Tesla LUN under `<backing_root>/TeslaCam` and the
    # Media LUN at the root of `paths.media_root` (which carries
    # Music/Boombox/LightShows/etc.). Authenticated (guest_ok=False),
    # browseable, read+write so the user can drop files onto either
    # drive from Windows Explorer or Finder.
    media_root = cfg.paths.media_root or cfg.paths.backing_root
    return (
        SambaShare(
            name=_DEFAULT_SHARE_NAME,
            path=cfg.paths.backing_root / _DEFAULT_SHARE_NAME,
            comment="TeslaUSB Dashcam & Sentry footage",
        ),
        SambaShare(
            name=_DEFAULT_MEDIA_SHARE_NAME,
            path=media_root,
            comment="TeslaUSB Music, Boombox & Light Shows",
        ),
    )


def make_samba_service(cfg: WebConfig) -> SambaService:
    media_root = cfg.paths.media_root or cfg.paths.backing_root
    allowed_roots: tuple[Path, ...] = (cfg.paths.backing_root, cfg.paths.state_dir)
    if media_root not in allowed_roots:
        allowed_roots = (*allowed_roots, media_root)
    return SambaService(
        SambaServiceConfig(
            config_path=cfg.samba.config_path,
            shares=_default_shares(cfg),
            allowed_roots=allowed_roots,
            binary_smbd=cfg.samba.binary_smbd,
            binary_smbcontrol=cfg.samba.binary_smbcontrol,
            binary_systemctl=cfg.samba.binary_systemctl,
            sudo_prefix=cfg.samba.sudo_prefix,
        )
    )


__all__ = (
    "SambaCommandError",
    "SambaConfigError",
    "SambaConfigRenderResult",
    "SambaError",
    "SambaNotInstalledError",
    "SambaService",
    "SambaServiceConfig",
    "SambaShare",
    "SambaStatus",
    "make_samba_service",
)
