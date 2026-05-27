"""Configuration and typed exceptions for the cloud archive package."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

KV_KEY_SYNC_FOLDERS: Final[str] = "cloud_archive.sync_folders"
KV_KEY_PRIORITY_FOLDERS: Final[str] = "cloud_archive.priority_folders"
KV_KEY_SYNC_NON_EVENT: Final[str] = "cloud_archive.sync_non_event"
KV_KEY_MAX_RETRY_ATTEMPTS: Final[str] = "cloud_archive.max_retry_attempts"
KV_KEY_SYNC_RECENT_WITH_TELEMETRY: Final[str] = (
    "cloud_archive.sync_recent_with_telemetry"
)

PERSISTED_SETTING_KEYS: Final[tuple[str, ...]] = (
    KV_KEY_SYNC_FOLDERS,
    KV_KEY_PRIORITY_FOLDERS,
    KV_KEY_SYNC_NON_EVENT,
    KV_KEY_MAX_RETRY_ATTEMPTS,
    KV_KEY_SYNC_RECENT_WITH_TELEMETRY,
)

RETRY_MAX_ATTEMPTS_MIN: Final[int] = 1
RETRY_MAX_ATTEMPTS_MAX: Final[int] = 20
DEFAULT_WORKER_IDLE_SECONDS: Final[float] = 5.0
DEFAULT_BACKOFF_INITIAL_SECONDS: Final[float] = 5.0
DEFAULT_BACKOFF_MAX_SECONDS: Final[float] = 300.0
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS: Final[float] = 5.0
DEFAULT_PIPELINE_BATCH_SIZE: Final[int] = 32
NO_EVENT_SCORE_THRESHOLD: Final[int] = 200
FOLDER_PRIORITY_MULTIPLIER: Final[int] = 1_000


class CloudArchiveError(RuntimeError):
    """Base error raised by the cloud archive domain."""


class CloudArchiveDBError(CloudArchiveError):
    """The cloud archive SQLite database could not be opened or migrated."""


class CloudArchiveConfigError(ValueError):
    """A cloud archive configuration value is invalid."""


class CloudArchiveStateError(CloudArchiveError):
    """Cloud archive worker state is inconsistent or cannot satisfy a request."""


@dataclass(frozen=True, slots=True)
class CloudArchiveConfig:
    """Constructor-injected settings for the cloud archive service."""

    enabled: bool
    db_path: Path
    teslacam_path: Path
    mapping_db_path: Path
    worker_idle_seconds: float = DEFAULT_WORKER_IDLE_SECONDS
    backoff_initial_seconds: float = DEFAULT_BACKOFF_INITIAL_SECONDS
    backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS
    max_retry_attempts: int = 5
    wifi_check_required: bool = True
    priority_folders: tuple[str, ...] = ("SavedClips", "SentryClips")
    sync_folders: tuple[str, ...] = ("SavedClips", "SentryClips", "RecentClips")
    dead_letter_max_age_days: int = 30
    sync_non_event: bool = False
    sync_recent_with_telemetry: bool = False

    def __post_init__(self) -> None:
        if self.worker_idle_seconds <= 0:
            raise CloudArchiveConfigError("worker_idle_seconds must be > 0")
        if self.backoff_initial_seconds <= 0:
            raise CloudArchiveConfigError("backoff_initial_seconds must be > 0")
        if self.backoff_max_seconds < self.backoff_initial_seconds:
            raise CloudArchiveConfigError("backoff_max_seconds must be >= backoff_initial_seconds")
        if not RETRY_MAX_ATTEMPTS_MIN <= self.max_retry_attempts <= RETRY_MAX_ATTEMPTS_MAX:
            raise CloudArchiveConfigError("max_retry_attempts must be within 1..20")
        if self.dead_letter_max_age_days <= 0:
            raise CloudArchiveConfigError("dead_letter_max_age_days must be > 0")


def _kv_lookup_raw(connection: sqlite3.Connection | None, key: str) -> str | None:
    if connection is None:
        return None
    try:
        row = connection.execute(
            "SELECT value FROM cloud_archive_meta WHERE key = ?",
            (key,),
        ).fetchone()
    except sqlite3.Error as exc:
        logger.debug("kv lookup failed for %s: %s", key, exc)
        return None
    if row is None:
        return None
    value = row[0]
    return value if isinstance(value, str) else None


def _kv_parse_bool(raw: str | None) -> bool | None:
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, bool) else None


def _kv_parse_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, int) and not isinstance(parsed, bool) else None


def _kv_parse_str_tuple(raw: str | None) -> tuple[str, ...] | None:
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, list):
        return None
    return tuple(item for item in parsed if isinstance(item, str))


def _read_sync_non_event_setting(
    config: CloudArchiveConfig,
    connection: sqlite3.Connection | None = None,
) -> bool:
    value = _kv_parse_bool(_kv_lookup_raw(connection, KV_KEY_SYNC_NON_EVENT))
    return value if value is not None else config.sync_non_event


def _read_sync_folders_setting(
    config: CloudArchiveConfig,
    connection: sqlite3.Connection | None = None,
) -> tuple[str, ...]:
    value = _kv_parse_str_tuple(_kv_lookup_raw(connection, KV_KEY_SYNC_FOLDERS))
    return value if value is not None else config.sync_folders


def _read_priority_order_setting(
    config: CloudArchiveConfig,
    connection: sqlite3.Connection | None = None,
) -> tuple[str, ...]:
    value = _kv_parse_str_tuple(_kv_lookup_raw(connection, KV_KEY_PRIORITY_FOLDERS))
    return value if value is not None else config.priority_folders


def _read_retry_max_attempts_setting(
    config: CloudArchiveConfig,
    connection: sqlite3.Connection | None = None,
) -> int:
    value = _kv_parse_int(_kv_lookup_raw(connection, KV_KEY_MAX_RETRY_ATTEMPTS))
    if value is not None and RETRY_MAX_ATTEMPTS_MIN <= value <= RETRY_MAX_ATTEMPTS_MAX:
        return value
    return config.max_retry_attempts


def _read_sync_recent_with_telemetry_setting(
    config: CloudArchiveConfig,
    connection: sqlite3.Connection | None = None,
) -> bool:
    value = _kv_parse_bool(_kv_lookup_raw(connection, KV_KEY_SYNC_RECENT_WITH_TELEMETRY))
    return value if value is not None else config.sync_recent_with_telemetry


def _read_worker_idle_seconds_setting(config: CloudArchiveConfig) -> float:
    return config.worker_idle_seconds


def _read_backoff_initial_seconds_setting(config: CloudArchiveConfig) -> float:
    return config.backoff_initial_seconds


def _read_backoff_max_seconds_setting(config: CloudArchiveConfig) -> float:
    return config.backoff_max_seconds


def _read_wifi_check_required_setting(config: CloudArchiveConfig) -> bool:
    return config.wifi_check_required


def _write_setting(connection: sqlite3.Connection, key: str, value: object) -> None:
    serialized = json.dumps(value)
    connection.execute(
        "INSERT INTO cloud_archive_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, serialized),
    )


def make_cloud_archive_config(cfg: WebConfig) -> CloudArchiveConfig:
    """Build archive config from the app-level ``WebConfig``."""

    return CloudArchiveConfig(
        enabled=cfg.features.cloud_archive_enabled,
        db_path=cfg.cloud.db_path,
        teslacam_path=cfg.cloud.teslacam_path,
        mapping_db_path=cfg.mapping.db_path,
        worker_idle_seconds=float(cfg.cloud.worker_idle_seconds),
        backoff_initial_seconds=float(cfg.cloud.backoff_initial_seconds),
        backoff_max_seconds=float(cfg.cloud.backoff_max_seconds),
        max_retry_attempts=cfg.cloud.max_retry_attempts,
        wifi_check_required=cfg.cloud.wifi_check_required,
        priority_folders=tuple(cfg.cloud.priority_folders),
        sync_folders=tuple(cfg.cloud.sync_folders),
        dead_letter_max_age_days=cfg.cloud.dead_letter_max_age_days,
    )
