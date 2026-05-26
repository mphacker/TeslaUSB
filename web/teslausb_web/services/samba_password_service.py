"""Set and probe the Samba password for the configured Linux user.

The Samba password is **not** persisted in our JSON state. It lives only
in the Samba TDB on disk (``/var/lib/samba/passdb.tdb``), managed by
``smbpasswd``. ``pdbedit -L`` reports whether the user has an entry,
which drives the "(set)" / "(not set)" hint on the settings page —
that probe is cheap (one fork+exec per page load) and the truth lives
authoritatively in the TDB, so we never cache the result across
requests.

The web app process runs as the unprivileged ``pi`` user; both
``smbpasswd`` (writes ``/var/lib/samba``) and ``pdbedit -L`` (reads it)
require root. The ``sudo_prefix`` config tuple — shared with
``SambaService`` — is prepended to every invocation. The Pi's sudoers
already grants ``pi`` NOPASSWD: ALL via
``/etc/sudoers.d/010_pi-nopasswd``, so no new policy is needed.
"""

from __future__ import annotations

import logging
import shutil
import string
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0
_MIN_PASSWORD_LENGTH: Final[int] = 8
_MAX_PASSWORD_LENGTH: Final[int] = 127
# Printable ASCII minus whitespace control chars. smbpasswd accepts a
# broader range but limiting to printable ASCII keeps the round-trip
# through Windows / macOS auth dialogs predictable and avoids issues
# with UTF-8 normalisation drift between client and server.
_ALLOWED_PASSWORD_CHARS: Final[frozenset[str]] = frozenset(
    string.ascii_letters + string.digits + string.punctuation
)


class _RunTextCommand(Protocol):
    def __call__(
        self,
        command: list[str],
        /,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]: ...


class SambaPasswordError(RuntimeError):
    """Base error for Samba password operations."""


class SambaPasswordValidationError(SambaPasswordError):
    """Raised when a candidate password fails policy validation."""


class SambaPasswordCommandError(SambaPasswordError):
    """Raised when an underlying smbpasswd / pdbedit subprocess fails."""


class SambaPasswordNotInstalledError(SambaPasswordError):
    """Raised when the smbpasswd or pdbedit binary cannot be resolved."""


@dataclass(frozen=True, slots=True)
class SambaPasswordServiceConfig:
    """Bindings for the smbpasswd/pdbedit invocations."""

    username: str
    binary_smbpasswd: str = "smbpasswd"
    binary_pdbedit: str = "pdbedit"
    sudo_prefix: tuple[str, ...] = ()
    command_timeout_seconds: float = _COMMAND_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.username.strip():
            raise SambaPasswordError("username must be non-empty")
        for field_name, value in (
            ("binary_smbpasswd", self.binary_smbpasswd),
            ("binary_pdbedit", self.binary_pdbedit),
        ):
            if not value.strip():
                raise SambaPasswordError(f"{field_name} must be non-empty")
        for index, prefix_token in enumerate(self.sudo_prefix):
            if not isinstance(prefix_token, str) or not prefix_token.strip():
                raise SambaPasswordError(
                    f"sudo_prefix[{index}] must be a non-empty string"
                )
        if self.command_timeout_seconds <= 0:
            raise SambaPasswordError("command_timeout_seconds must be > 0")


class SambaPasswordService:
    """Set the Samba password for the configured Linux user via smbpasswd."""

    def __init__(
        self,
        config: SambaPasswordServiceConfig,
        *,
        which: Callable[[str], str | None] | None = None,
        run_command: _RunTextCommand | None = None,
    ) -> None:
        config.validate()
        self._config = config
        self._which = shutil.which if which is None else which
        self._run_command = subprocess.run if run_command is None else run_command

    @property
    def config(self) -> SambaPasswordServiceConfig:
        return self._config

    def set_password(self, plaintext: str) -> None:
        """Set the Samba password for the configured user.

        Raises ``SambaPasswordValidationError`` for bad input,
        ``SambaPasswordCommandError`` for a failed subprocess, or
        ``SambaPasswordNotInstalledError`` if smbpasswd isn't on PATH.
        Never logs ``plaintext``.
        """
        self._validate_password(plaintext)
        smbpasswd_binary = self._resolve_required_binary(self._config.binary_smbpasswd)
        command = [
            *self._config.sudo_prefix,
            str(smbpasswd_binary),
            "-s",
            "-a",
            self._config.username,
        ]
        # smbpasswd -s expects "newpass\nnewpass\n" on stdin (no prompts).
        # -a creates the entry first time, then idempotently updates on
        # subsequent calls (smbpasswd treats -a as "add or update").
        stdin_payload = f"{plaintext}\n{plaintext}\n"
        try:
            completed = self._run_command(
                command,
                input=stdin_payload,
                capture_output=True,
                text=True,
                timeout=self._config.command_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SambaPasswordCommandError(
                f"smbpasswd timed out after {self._config.command_timeout_seconds:.1f}s"
            ) from exc
        except OSError as exc:
            raise SambaPasswordCommandError(f"failed to invoke smbpasswd: {exc}") from exc
        if completed.returncode != 0:
            detail = _command_detail(completed)
            raise SambaPasswordCommandError(f"smbpasswd failed: {detail}")
        logger.info(
            "Updated Samba password for user %s via smbpasswd",
            self._config.username,
        )

    def user_exists(self) -> bool:
        """Return True if the configured user has a Samba TDB entry."""
        pdbedit_binary = self._resolve_binary(self._config.binary_pdbedit)
        if pdbedit_binary is None:
            return False
        command = [
            *self._config.sudo_prefix,
            str(pdbedit_binary),
            "-L",
        ]
        try:
            completed = self._run_command(
                command,
                capture_output=True,
                text=True,
                timeout=self._config.command_timeout_seconds,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("pdbedit probe failed: %s", exc)
            return False
        if completed.returncode != 0:
            logger.debug("pdbedit -L exit %d: %s", completed.returncode, _command_detail(completed))
            return False
        # Output format: "username:uid:fullname" on each line.
        username = self._config.username
        for line in (completed.stdout or "").splitlines():
            entry = line.split(":", 1)[0].strip()
            if entry == username:
                return True
        return False

    def _validate_password(self, plaintext: str) -> None:
        if not isinstance(plaintext, str):
            raise SambaPasswordValidationError("password must be a string")
        if len(plaintext) < _MIN_PASSWORD_LENGTH:
            raise SambaPasswordValidationError(
                f"password must be at least {_MIN_PASSWORD_LENGTH} characters"
            )
        if len(plaintext) > _MAX_PASSWORD_LENGTH:
            raise SambaPasswordValidationError(
                f"password must be at most {_MAX_PASSWORD_LENGTH} characters"
            )
        for ch in plaintext:
            if ch not in _ALLOWED_PASSWORD_CHARS:
                raise SambaPasswordValidationError(
                    "password must contain only printable ASCII characters "
                    "(letters, digits, punctuation)"
                )

    def _resolve_required_binary(self, candidate: str) -> Path:
        resolved = self._resolve_binary(candidate)
        if resolved is None:
            raise SambaPasswordNotInstalledError(f"required binary not found: {candidate}")
        return resolved

    def _resolve_binary(self, candidate: str) -> Path | None:
        resolved = self._which(candidate)
        return None if resolved is None else Path(resolved)


def _command_detail(completed: subprocess.CompletedProcess[str]) -> str:
    detail = (completed.stderr or completed.stdout or "").strip()
    if not detail:
        detail = f"exit code {completed.returncode}"
    return detail


def make_samba_password_service(cfg: WebConfig) -> SambaPasswordService:
    return SambaPasswordService(
        SambaPasswordServiceConfig(
            username=cfg.samba.password_username,
            binary_smbpasswd=cfg.samba.binary_smbpasswd,
            binary_pdbedit=cfg.samba.binary_pdbedit,
            sudo_prefix=cfg.samba.sudo_prefix,
        )
    )


__all__ = (
    "SambaPasswordCommandError",
    "SambaPasswordError",
    "SambaPasswordNotInstalledError",
    "SambaPasswordService",
    "SambaPasswordServiceConfig",
    "SambaPasswordValidationError",
    "make_samba_password_service",
)
