"""Configuration and typed exceptions for the cloud archive package."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

    from teslausb_web.config import WebConfig

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


def _read_sync_non_event_setting(config: CloudArchiveConfig) -> bool:
    return config.sync_non_event


def _read_sync_folders_setting(config: CloudArchiveConfig) -> tuple[str, ...]:
    return config.sync_folders


def _read_priority_order_setting(config: CloudArchiveConfig) -> tuple[str, ...]:
    return config.priority_folders


def _read_retry_max_attempts_setting(config: CloudArchiveConfig) -> int:
    return config.max_retry_attempts


def _read_worker_idle_seconds_setting(config: CloudArchiveConfig) -> float:
    return config.worker_idle_seconds


def _read_backoff_initial_seconds_setting(config: CloudArchiveConfig) -> float:
    return config.backoff_initial_seconds


def _read_backoff_max_seconds_setting(config: CloudArchiveConfig) -> float:
    return config.backoff_max_seconds


def _read_wifi_check_required_setting(config: CloudArchiveConfig) -> bool:
    return config.wifi_check_required


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
