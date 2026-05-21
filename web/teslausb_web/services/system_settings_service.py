"""JSON-backed advanced system-settings service."""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

_SCHEMA_VERSION: Final[int] = 1
_JSON_ENCODING: Final[str] = "utf-8"
_JSON_INDENT: Final[int] = 2
_TMP_SUFFIX: Final[str] = ".tmp"
_ALLOWED_LOG_LEVELS: Final[tuple[str, ...]] = (
    "DEBUG",
    "INFO",
    "WARNING",
    "ERROR",
    "CRITICAL",
)


class SystemSettingsConfigError(ValueError):
    """Input or configuration validation failed."""


class SystemSettingsStateError(RuntimeError):
    """Persisted system-settings state could not be read or written."""


@dataclass(frozen=True, slots=True)
class SystemSettingsConfig:
    state_path: Path
    default_samba_enabled: bool = False
    default_log_level: str = "INFO"

    def __post_init__(self) -> None:
        raw_path = self.state_path
        if not raw_path.is_absolute() and not PurePosixPath(raw_path.as_posix()).is_absolute():
            raise SystemSettingsConfigError(f"state_path must be absolute, got {raw_path!r}")
        object.__setattr__(self, "state_path", raw_path.resolve())
        object.__setattr__(
            self,
            "default_log_level",
            _normalize_log_level(self.default_log_level, field_name="default_log_level"),
        )


@dataclass(frozen=True, slots=True)
class SystemSettings:
    samba_enabled: bool
    log_level: str
    ipc_socket_path: str


class SystemSettingsService:
    """Persist B-1 advanced settings that are safe to expose pre-Phase 5.17."""

    def __init__(self, config: SystemSettingsConfig, *, ipc_socket_path: Path) -> None:
        self._config = config
        self._ipc_socket_path = str(ipc_socket_path)
        self._lock = threading.RLock()
        self._callbacks: list[Callable[[SystemSettings], None]] = []

    @property
    def config(self) -> SystemSettingsConfig:
        return self._config

    def default_settings(self) -> SystemSettings:
        return SystemSettings(
            samba_enabled=self._config.default_samba_enabled,
            log_level=self._config.default_log_level,
            ipc_socket_path=self._ipc_socket_path,
        )

    def get_settings(self) -> SystemSettings:
        with self._lock:
            payload = _load_json_file(self._config.state_path)
            if payload is None:
                return self.default_settings()
            if not isinstance(payload, dict):
                raise SystemSettingsStateError("System settings file must contain a JSON object")
            raw_version = payload.get("schema_version", _SCHEMA_VERSION)
            if raw_version != _SCHEMA_VERSION:
                raise SystemSettingsStateError(
                    f"Unsupported system settings schema version: {raw_version!r}"
                )
            return self._settings_from_mapping(payload, fallback=self.default_settings())

    def save_settings(self, settings: SystemSettings) -> SystemSettings:
        with self._lock:
            payload = {
                "schema_version": _SCHEMA_VERSION,
                "samba_enabled": settings.samba_enabled,
                "log_level": settings.log_level,
            }
            _write_json_atomically(self._config.state_path, payload)
            callbacks = tuple(self._callbacks)
        logger.info("Saved system settings to %s", self._config.state_path)
        for callback in callbacks:
            try:
                callback(settings)
            except Exception:
                logger.exception("System settings callback failed")
        return settings

    def update_settings(self, payload: Mapping[str, object]) -> SystemSettings:
        return self.save_settings(
            self._settings_from_mapping(payload, fallback=self.get_settings())
        )

    def serialize_settings(self, settings: SystemSettings) -> dict[str, object]:
        return {
            "samba_enabled": settings.samba_enabled,
            "log_level": settings.log_level,
            "ipc_socket_path": settings.ipc_socket_path,
            "state_path": str(self._config.state_path),
        }

    def log_levels(self) -> tuple[str, ...]:
        return _ALLOWED_LOG_LEVELS

    def config_snapshot(self, settings: SystemSettings) -> dict[str, object]:
        return {
            "defaults": {
                "log_level": self._config.default_log_level,
                "samba_enabled": self._config.default_samba_enabled,
            },
            "ipc_socket_path": settings.ipc_socket_path,
            "log_level": settings.log_level,
            "samba_enabled": settings.samba_enabled,
            "state_path": str(self._config.state_path),
        }

    def subscribe(self, callback: Callable[[SystemSettings], None]) -> Callable[[], None]:
        with self._lock:
            self._callbacks.append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                self._callbacks = [
                    registered for registered in self._callbacks if registered is not callback
                ]

        return _unsubscribe

    def _settings_from_mapping(
        self,
        payload: Mapping[str, object],
        *,
        fallback: SystemSettings,
    ) -> SystemSettings:
        return SystemSettings(
            samba_enabled=_coerce_optional_bool(
                payload,
                "samba_enabled",
                default=fallback.samba_enabled,
            ),
            log_level=_coerce_optional_log_level(
                payload,
                "log_level",
                default=fallback.log_level,
            ),
            ipc_socket_path=self._ipc_socket_path,
        )


def _load_json_file(path: Path) -> object | None:
    if not path.exists():
        return None
    try:
        raw_text = path.read_text(encoding=_JSON_ENCODING)
    except OSError as exc:
        raise SystemSettingsStateError(f"Failed to read {path}: {exc}") from exc
    try:
        payload: object = cast("object", json.loads(raw_text))
        return payload
    except json.JSONDecodeError as exc:
        raise SystemSettingsStateError(f"Failed to parse {path}: {exc}") from exc


def _write_json_atomically(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}{_TMP_SUFFIX}")
    raw_json = json.dumps(payload, indent=_JSON_INDENT, sort_keys=True) + "\n"
    try:
        with temp_path.open("w", encoding=_JSON_ENCODING, newline="\n") as handle:
            handle.write(raw_json)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    except OSError as exc:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        raise SystemSettingsStateError(f"Failed to write {path}: {exc}") from exc


def _coerce_optional_bool(
    payload: Mapping[str, object],
    key: str,
    *,
    default: bool,
) -> bool:
    if key not in payload:
        return default
    value = payload[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    if isinstance(value, int) and value in {0, 1}:
        return value == 1
    raise SystemSettingsConfigError(f"{key} must be a boolean")


def _coerce_optional_log_level(
    payload: Mapping[str, object],
    key: str,
    *,
    default: str,
) -> str:
    if key not in payload:
        return default
    value = payload[key]
    if not isinstance(value, str):
        raise SystemSettingsConfigError(f"{key} must be a string")
    return _normalize_log_level(value, field_name=key)


def _normalize_log_level(value: str, *, field_name: str) -> str:
    normalized = value.strip().upper()
    if normalized not in _ALLOWED_LOG_LEVELS:
        allowed = ", ".join(_ALLOWED_LOG_LEVELS)
        raise SystemSettingsConfigError(f"{field_name} must be one of {allowed}")
    return normalized


def make_system_settings_service(cfg: WebConfig) -> SystemSettingsService:
    return SystemSettingsService(
        SystemSettingsConfig(
            state_path=cfg.system_settings.state_path,
            default_samba_enabled=cfg.features.samba_enabled,
            default_log_level=cfg.system_settings.default_log_level,
        ),
        ipc_socket_path=cfg.paths.ipc_socket,
    )


__all__ = (
    "SystemSettings",
    "SystemSettingsConfig",
    "SystemSettingsConfigError",
    "SystemSettingsService",
    "SystemSettingsStateError",
    "make_system_settings_service",
)
