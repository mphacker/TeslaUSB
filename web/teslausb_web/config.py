"""TOML-backed configuration loader for the Flask web app.

B-1 stores configuration in `/etc/teslausb/teslausb-web.toml` (root)
and `/etc/teslausb/worker.toml` (Rust daemon). Both files are TOML
per ADR-0001 — the YAML loader used in v1 is gone.

Locations searched (first match wins):

1. The path supplied via the ``TESLAUSB_WEB_CONFIG`` environment
   variable. Tests use this to point at a tmpdir.
2. ``/etc/teslausb/teslausb-web.toml`` (production install path,
   written by ``setup.sh`` in Phase 6).
3. Built-in defaults — only used when the file is absent AND the
   caller passed ``allow_defaults=True``. In production we want a
   hard failure if the config is missing so misdeploys are loud.

Every key is mapped onto a small dataclass tree so the rest of the
app can use typed access (``config.web.port``) rather than
stringly-typed dict lookups (charter §"Anti-patterns / Stringly-typed
code"). Validation lives in ``WebConfig.validate`` and runs at load
time; misconfigurations raise ``ConfigError`` with a path-anchored
message so the operator sees exactly which key is wrong.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ENV_CONFIG_PATH: str = "TESLAUSB_WEB_CONFIG"
DEFAULT_CONFIG_PATH: Path = Path("/etc/teslausb/teslausb-web.toml")

# Match v1's defaults so the screenshot-diff acceptance gate has a
# fighting chance: a freshly installed B-1 device serves the same
# port and accepts the same upload sizes a v1 device did.
_DEFAULT_PORT: int = 8080
_DEFAULT_HOST: str = "127.0.0.1"
_DEFAULT_MAX_UPLOAD_MB: int = 512
_DEFAULT_MAX_CHUNK_MB: int = 64
_DEFAULT_BACKING_ROOT: Path = Path("/srv/teslausb")
_DEFAULT_DB_PATH: Path = Path("/var/lib/teslausb/index.sqlite3")
_DEFAULT_IPC_SOCKET: Path = Path("/run/teslausb/worker.sock")
_DEFAULT_CACHE_SCRIPT: Path = Path("/usr/local/bin/tesla_cache_invalidate.sh")

# Highest valid TCP/UDP port number per RFC 793. Named to silence
# the magic-value lint and document intent at the call site.
_TCP_PORT_MAX: int = 65_535


class ConfigError(ValueError):
    """A configuration file could not be loaded or failed validation.

    Subclasses ``ValueError`` (not a bare ``Exception``) so callers
    can catch precisely without violating charter §3 "no blind
    except". Carries the original path so the error message in
    journalctl points the operator at the right file.
    """

    def __init__(self, path: Path | None, message: str) -> None:
        if path is None:
            super().__init__(message)
        else:
            super().__init__(f"{path}: {message}")
        self.path = path


@dataclass(frozen=True, slots=True)
class WebSection:
    """Settings that govern the Flask app surface itself."""

    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    secret_key: str = ""
    max_upload_mb: int = _DEFAULT_MAX_UPLOAD_MB
    max_chunk_mb: int = _DEFAULT_MAX_CHUNK_MB

    def validate(self) -> None:
        if not (1 <= self.port <= _TCP_PORT_MAX):
            raise ConfigError(
                None,
                f"[web] port {self.port} outside 1..{_TCP_PORT_MAX}",
            )
        if self.max_upload_mb <= 0:
            raise ConfigError(None, "[web] max_upload_mb must be > 0")
        if self.max_chunk_mb <= 0:
            raise ConfigError(None, "[web] max_chunk_mb must be > 0")
        if self.max_chunk_mb > self.max_upload_mb:
            raise ConfigError(
                None,
                f"[web] max_chunk_mb ({self.max_chunk_mb}) "
                f"cannot exceed max_upload_mb ({self.max_upload_mb})",
            )
        # `secret_key == ""` is allowed at load time so test fixtures
        # don't need to bake a real key in; the app factory generates
        # one at startup if the key is empty. Production setup.sh
        # writes a 64-char hex key during initial install.


@dataclass(frozen=True, slots=True)
class PathsSection:
    """Filesystem locations the web app reads or writes."""

    backing_root: Path = _DEFAULT_BACKING_ROOT
    db_path: Path = _DEFAULT_DB_PATH
    ipc_socket: Path = _DEFAULT_IPC_SOCKET
    cache_invalidate_script: Path = _DEFAULT_CACHE_SCRIPT

    def validate(self) -> None:
        # Config paths are POSIX paths on the target device. We check
        # against PurePosixPath so the validation works the same on
        # Windows dev boxes (where Path("/srv").is_absolute() == False
        # because there's no drive letter) and on Linux production.
        for name, value in (
            ("backing_root", self.backing_root),
            ("db_path", self.db_path),
            ("ipc_socket", self.ipc_socket),
            ("cache_invalidate_script", self.cache_invalidate_script),
        ):
            if not PurePosixPath(value.as_posix()).is_absolute():
                raise ConfigError(None, f"[paths] {name} must be absolute, got {value!r}")


@dataclass(frozen=True, slots=True)
class FeaturesSection:
    """Optional feature flags read by individual blueprints.

    Default state mirrors v1: music + boombox + samba off, everything
    else on. Settings UI flips these at runtime; the values written
    here are only the initial defaults.
    """

    music_enabled: bool = False
    boombox_enabled: bool = False
    samba_enabled: bool = False
    cloud_archive_enabled: bool = True


@dataclass(frozen=True, slots=True)
class WebConfig:
    """Root config dataclass — what the rest of the app sees."""

    web: WebSection = field(default_factory=WebSection)
    paths: PathsSection = field(default_factory=PathsSection)
    features: FeaturesSection = field(default_factory=FeaturesSection)
    source_path: Path | None = None

    def validate(self) -> None:
        """Re-anchor sub-section ConfigErrors at ``source_path``."""
        try:
            self.web.validate()
            self.paths.validate()
        except ConfigError as exc:
            raise ConfigError(self.source_path, str(exc).split(": ", 1)[-1]) from exc


def _resolve_config_path(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    env_value = os.environ.get(ENV_CONFIG_PATH)
    if env_value:
        return Path(env_value)
    if DEFAULT_CONFIG_PATH.is_file():
        return DEFAULT_CONFIG_PATH
    return None


def _expect_section(raw: object, name: str, source: Path | None) -> dict[str, object]:
    if name not in raw:  # type: ignore[operator]
        return {}
    section = raw[name]  # type: ignore[index]
    if not isinstance(section, dict):
        raise ConfigError(source, f"section [{name}] must be a table, got {type(section).__name__}")
    return section


def _coerce_int(section: dict[str, object], key: str, default: int, source: Path | None) -> int:
    if key not in section:
        return default
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(source, f"{key} must be an integer, got {type(value).__name__}")
    return value


def _coerce_bool(
    section: dict[str, object],
    key: str,
    default: bool,  # noqa: FBT001 — internal coerce helper, not a public API
    source: Path | None,
) -> bool:
    if key not in section:
        return default
    value = section[key]
    if not isinstance(value, bool):
        raise ConfigError(source, f"{key} must be a boolean, got {type(value).__name__}")
    return value


def _coerce_str(section: dict[str, object], key: str, default: str, source: Path | None) -> str:
    if key not in section:
        return default
    value = section[key]
    if not isinstance(value, str):
        raise ConfigError(source, f"{key} must be a string, got {type(value).__name__}")
    return value


def _coerce_path(section: dict[str, object], key: str, default: Path, source: Path | None) -> Path:
    if key not in section:
        return default
    value = section[key]
    if not isinstance(value, str):
        raise ConfigError(source, f"{key} must be a string path, got {type(value).__name__}")
    return Path(value)


def load_config(path: Path | None = None, *, allow_defaults: bool = False) -> WebConfig:
    """Load the web-app config from TOML, or built-in defaults.

    Args:
        path: Explicit path to load. ``None`` consults
            ``TESLAUSB_WEB_CONFIG`` then ``/etc/teslausb/teslausb-web.toml``.
        allow_defaults: When ``True``, an absent config file yields
            a ``WebConfig`` populated with built-in defaults. When
            ``False`` (production), an absent file raises
            ``ConfigError`` so misdeploys fail loudly.

    Raises:
        ConfigError: when the resolved path does not exist (and
            ``allow_defaults`` is False), is not a regular file,
            cannot be parsed as TOML, contains a wrong-typed key,
            or fails validation.
    """
    resolved = _resolve_config_path(path)
    if resolved is None:
        if not allow_defaults:
            raise ConfigError(
                None,
                f"no config file found (checked ${ENV_CONFIG_PATH} and {DEFAULT_CONFIG_PATH})",
            )
        cfg = WebConfig(source_path=None)
        cfg.validate()
        return cfg

    if not resolved.is_file():
        raise ConfigError(resolved, "is not a regular file")

    try:
        with resolved.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(resolved, f"TOML parse error: {exc}") from exc

    web_raw = _expect_section(raw, "web", resolved)
    paths_raw = _expect_section(raw, "paths", resolved)
    features_raw = _expect_section(raw, "features", resolved)

    web = WebSection(
        host=_coerce_str(web_raw, "host", _DEFAULT_HOST, resolved),
        port=_coerce_int(web_raw, "port", _DEFAULT_PORT, resolved),
        secret_key=_coerce_str(web_raw, "secret_key", "", resolved),
        max_upload_mb=_coerce_int(web_raw, "max_upload_mb", _DEFAULT_MAX_UPLOAD_MB, resolved),
        max_chunk_mb=_coerce_int(web_raw, "max_chunk_mb", _DEFAULT_MAX_CHUNK_MB, resolved),
    )
    paths_section = PathsSection(
        backing_root=_coerce_path(paths_raw, "backing_root", _DEFAULT_BACKING_ROOT, resolved),
        db_path=_coerce_path(paths_raw, "db_path", _DEFAULT_DB_PATH, resolved),
        ipc_socket=_coerce_path(paths_raw, "ipc_socket", _DEFAULT_IPC_SOCKET, resolved),
        cache_invalidate_script=_coerce_path(
            paths_raw,
            "cache_invalidate_script",
            _DEFAULT_CACHE_SCRIPT,
            resolved,
        ),
    )
    features = FeaturesSection(
        music_enabled=_coerce_bool(features_raw, "music_enabled", default=False, source=resolved),
        boombox_enabled=_coerce_bool(
            features_raw,
            "boombox_enabled",
            default=False,
            source=resolved,
        ),
        samba_enabled=_coerce_bool(features_raw, "samba_enabled", default=False, source=resolved),
        cloud_archive_enabled=_coerce_bool(
            features_raw,
            "cloud_archive_enabled",
            default=True,
            source=resolved,
        ),
    )

    cfg = WebConfig(web=web, paths=paths_section, features=features, source_path=resolved)
    cfg.validate()
    return cfg
