"""JSON-backed storage-retention policy service."""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
_FUTURE_MTIME_TOLERANCE: Final[timedelta] = timedelta(minutes=5)
_MIN_RETENTION_DAYS: Final[int] = 1
_MAX_RETENTION_DAYS: Final[int] = 3650
_MIN_FREE_SPACE_TARGET_PCT: Final[int] = 5
_MAX_FREE_SPACE_TARGET_PCT: Final[int] = 50
_MIN_ARCHIVE_SIZE_GB: Final[int] = 0
_MAX_ARCHIVE_SIZE_GB: Final[int] = 10000


class RetentionConfigError(ValueError):
    """Input or configuration validation failed."""


class RetentionStateError(RuntimeError):
    """Persisted retention state could not be read or written."""


@dataclass(frozen=True, slots=True)
class StorageRetentionConfig:
    policy_path: Path
    default_max_age_days: int = 30
    default_target_free_pct: int = 10
    default_max_archive_size_gb: int = 0
    default_short_retention_warning_days: int = 7

    def __post_init__(self) -> None:
        raw_path = self.policy_path
        if not raw_path.is_absolute() and not PurePosixPath(raw_path.as_posix()).is_absolute():
            raise RetentionConfigError(f"policy_path must be absolute, got {raw_path!r}")
        resolved_path = raw_path.resolve()
        object.__setattr__(self, "policy_path", resolved_path)
        _validate_days(self.default_max_age_days, field_name="default_max_age_days")
        _validate_free_space_target(
            self.default_target_free_pct,
            field_name="default_target_free_pct",
        )
        _validate_archive_size(
            self.default_max_archive_size_gb,
            field_name="default_max_archive_size_gb",
        )
        _validate_days(
            self.default_short_retention_warning_days,
            field_name="default_short_retention_warning_days",
        )
        if resolved_path.exists():
            modified_at = datetime.fromtimestamp(resolved_path.stat().st_mtime, tz=UTC)
            if modified_at > _utc_now() + _FUTURE_MTIME_TOLERANCE:
                raise RetentionConfigError("policy_path points to a future-dated file")


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    max_age_days: int = 30
    target_free_pct: int = 10
    max_archive_size_gb: int = 0
    short_retention_warning_days: int = 7
    keep_recent_clips: bool = False
    keep_saved_clips: bool = True
    keep_event_clips: bool = True
    keep_encrypted_clips: bool = True
    keep_archived_clips: bool = False
    dry_run: bool = True
    recent_clips_days: int = 30
    saved_clips_days: int = 30
    event_clips_days: int = 30
    encrypted_clips_days: int = 30
    archived_clips_days: int = 30

    def __post_init__(self) -> None:
        _validate_days(self.max_age_days, field_name="max_age_days")
        _validate_free_space_target(self.target_free_pct, field_name="target_free_pct")
        _validate_archive_size(self.max_archive_size_gb, field_name="max_archive_size_gb")
        _validate_days(
            self.short_retention_warning_days,
            field_name="short_retention_warning_days",
        )
        _validate_days(self.recent_clips_days, field_name="recent_clips_days")
        _validate_days(self.saved_clips_days, field_name="saved_clips_days")
        _validate_days(self.event_clips_days, field_name="event_clips_days")
        _validate_days(self.encrypted_clips_days, field_name="encrypted_clips_days")
        _validate_days(self.archived_clips_days, field_name="archived_clips_days")


@dataclass(frozen=True, slots=True)
class RetentionTargetRow:
    key: str
    label: str
    keep_field: str
    days_field: str
    keep: bool
    retention_days: int
    guidance: str
    caution: str | None = None


@dataclass(frozen=True, slots=True)
class RetentionPreviewSummary:
    preview_available: bool
    deferred_reason: str


_TARGET_ROWS: Final[tuple[tuple[str, str, str, str, str, str | None], ...]] = (
    (
        "recent",
        "RecentClips",
        "keep_recent_clips",
        "recent_clips_days",
        "Normal dashcam footage. Safe to recycle after a retention window.",
        None,
    ),
    (
        "saved",
        "SavedClips",
        "keep_saved_clips",
        "saved_clips_days",
        "Manually-saved clips. Keep protected by default.",
        "Disabled by default so manually-saved clips stay on disk.",
    ),
    (
        "event",
        "SentryClips",
        "keep_event_clips",
        "event_clips_days",
        "Security-event recordings. Keep protected by default.",
        "Disabled by default so event clips stay on disk.",
    ),
    (
        "encrypted",
        "EncryptedClips",
        "keep_encrypted_clips",
        "encrypted_clips_days",
        "Cybertruck encrypted footage. Keep protected by default.",
        "Disabled by default so encrypted clips stay on disk.",
    ),
    (
        "archived",
        "ArchivedClips",
        "keep_archived_clips",
        "archived_clips_days",
        "Locally-archived clips. Cleanup can reclaim space when needed.",
        None,
    ),
)


class StorageRetentionService:
    """Persist and validate cleanup-policy settings for later cleanup work."""

    def __init__(self, config: StorageRetentionConfig) -> None:
        self._config = config
        self._lock = threading.RLock()
        self._preview_summary_provider: Callable[[], RetentionPreviewSummary] | None = None

    @property
    def config(self) -> StorageRetentionConfig:
        return self._config

    def default_policy(self) -> RetentionPolicy:
        return RetentionPolicy(
            max_age_days=self._config.default_max_age_days,
            target_free_pct=self._config.default_target_free_pct,
            max_archive_size_gb=self._config.default_max_archive_size_gb,
            short_retention_warning_days=self._config.default_short_retention_warning_days,
            keep_recent_clips=False,
            keep_saved_clips=True,
            keep_event_clips=True,
            keep_encrypted_clips=True,
            keep_archived_clips=False,
            dry_run=True,
            recent_clips_days=self._config.default_max_age_days,
            saved_clips_days=self._config.default_max_age_days,
            event_clips_days=self._config.default_max_age_days,
            encrypted_clips_days=self._config.default_max_age_days,
            archived_clips_days=self._config.default_max_age_days,
        )

    def get_policy(self) -> RetentionPolicy:
        with self._lock:
            payload = _load_json_file(self._config.policy_path)
            if payload is None:
                return self.default_policy()
            if not isinstance(payload, dict):
                raise RetentionStateError("Retention policy file must contain a JSON object")
            raw_version = payload.get("schema_version", _SCHEMA_VERSION)
            if raw_version != _SCHEMA_VERSION:
                raise RetentionStateError(
                    f"Unsupported retention policy schema version: {raw_version!r}"
                )
            raw_policy = payload.get("policy", payload)
            if not isinstance(raw_policy, dict):
                raise RetentionStateError("Retention policy payload must be a JSON object")
            return self._policy_from_mapping(raw_policy, fallback=self.default_policy())

    def save_policy(self, policy: RetentionPolicy) -> RetentionPolicy:
        with self._lock:
            payload = {
                "schema_version": _SCHEMA_VERSION,
                "policy": self.serialize_policy(policy),
                "updated_at": _utc_now().isoformat(),
            }
            _write_json_atomically(self._config.policy_path, payload)
        logger.info("Saved storage-retention policy to %s", self._config.policy_path)
        return policy

    def update_policy(self, payload: Mapping[str, object]) -> RetentionPolicy:
        return self.save_policy(
            self._policy_from_mapping(payload, fallback=self.get_policy()),
        )

    def serialize_policy(self, policy: RetentionPolicy) -> dict[str, object]:
        return {
            "max_age_days": policy.max_age_days,
            "target_free_pct": policy.target_free_pct,
            "max_archive_size_gb": policy.max_archive_size_gb,
            "short_retention_warning_days": policy.short_retention_warning_days,
            "keep_recent_clips": policy.keep_recent_clips,
            "keep_saved_clips": policy.keep_saved_clips,
            "keep_event_clips": policy.keep_event_clips,
            "keep_encrypted_clips": policy.keep_encrypted_clips,
            "keep_archived_clips": policy.keep_archived_clips,
            "dry_run": policy.dry_run,
            "recent_clips_days": policy.recent_clips_days,
            "saved_clips_days": policy.saved_clips_days,
            "event_clips_days": policy.event_clips_days,
            "encrypted_clips_days": policy.encrypted_clips_days,
            "archived_clips_days": policy.archived_clips_days,
        }

    def policy_rows(self, policy: RetentionPolicy) -> tuple[RetentionTargetRow, ...]:
        rows: list[RetentionTargetRow] = []
        for key, label, keep_field, days_field, guidance, caution in _TARGET_ROWS:
            rows.append(
                RetentionTargetRow(
                    key=key,
                    label=label,
                    keep_field=keep_field,
                    days_field=days_field,
                    keep=_require_bool(getattr(policy, keep_field), field_name=keep_field),
                    retention_days=_require_int(getattr(policy, days_field), field_name=days_field),
                    guidance=guidance,
                    caution=caution,
                )
            )
        return tuple(rows)

    def ranges(self) -> dict[str, dict[str, int]]:
        return {
            "retention_days": {"min": _MIN_RETENTION_DAYS, "max": _MAX_RETENTION_DAYS},
            "target_free_pct": {
                "min": _MIN_FREE_SPACE_TARGET_PCT,
                "max": _MAX_FREE_SPACE_TARGET_PCT,
            },
            "max_archive_size_gb": {
                "min": _MIN_ARCHIVE_SIZE_GB,
                "max": _MAX_ARCHIVE_SIZE_GB,
            },
        }

    def bind_preview_summary_provider(
        self,
        provider: Callable[[], RetentionPreviewSummary],
    ) -> None:
        with self._lock:
            self._preview_summary_provider = provider

    def preview_summary(self) -> RetentionPreviewSummary:
        provider = self._preview_summary_provider
        if provider is None:
            return RetentionPreviewSummary(
                preview_available=False,
                deferred_reason="cleanup service unavailable",
            )
        try:
            return provider()
        except RuntimeError as exc:
            raise RetentionStateError(str(exc)) from exc

    def _policy_from_mapping(
        self,
        payload: Mapping[str, object],
        *,
        fallback: RetentionPolicy,
    ) -> RetentionPolicy:
        return RetentionPolicy(
            max_age_days=_coerce_optional_int(
                payload,
                "max_age_days",
                fallback.max_age_days,
                _validate_days,
            ),
            target_free_pct=_coerce_optional_int(
                payload,
                "target_free_pct",
                fallback.target_free_pct,
                _validate_free_space_target,
            ),
            max_archive_size_gb=_coerce_optional_int(
                payload,
                "max_archive_size_gb",
                fallback.max_archive_size_gb,
                _validate_archive_size,
            ),
            short_retention_warning_days=_coerce_optional_int(
                payload,
                "short_retention_warning_days",
                fallback.short_retention_warning_days,
                _validate_days,
            ),
            keep_recent_clips=_coerce_optional_bool(
                payload,
                "keep_recent_clips",
                default=fallback.keep_recent_clips,
            ),
            keep_saved_clips=_coerce_optional_bool(
                payload,
                "keep_saved_clips",
                default=fallback.keep_saved_clips,
            ),
            keep_event_clips=_coerce_optional_bool(
                payload,
                "keep_event_clips",
                default=fallback.keep_event_clips,
            ),
            keep_encrypted_clips=_coerce_optional_bool(
                payload,
                "keep_encrypted_clips",
                default=fallback.keep_encrypted_clips,
            ),
            keep_archived_clips=_coerce_optional_bool(
                payload,
                "keep_archived_clips",
                default=fallback.keep_archived_clips,
            ),
            dry_run=_coerce_optional_bool(payload, "dry_run", default=fallback.dry_run),
            recent_clips_days=_coerce_optional_int(
                payload,
                "recent_clips_days",
                fallback.recent_clips_days,
                _validate_days,
            ),
            saved_clips_days=_coerce_optional_int(
                payload,
                "saved_clips_days",
                fallback.saved_clips_days,
                _validate_days,
            ),
            event_clips_days=_coerce_optional_int(
                payload,
                "event_clips_days",
                fallback.event_clips_days,
                _validate_days,
            ),
            encrypted_clips_days=_coerce_optional_int(
                payload,
                "encrypted_clips_days",
                fallback.encrypted_clips_days,
                _validate_days,
            ),
            archived_clips_days=_coerce_optional_int(
                payload,
                "archived_clips_days",
                fallback.archived_clips_days,
                _validate_days,
            ),
        )


def make_storage_retention_service(
    cfg: WebConfig | StorageRetentionConfig,
) -> StorageRetentionService:
    if isinstance(cfg, StorageRetentionConfig):
        return StorageRetentionService(cfg)
    return StorageRetentionService(
        StorageRetentionConfig(
            policy_path=cfg.storage_retention.policy_path,
            default_max_age_days=cfg.storage_retention.default_max_age_days,
            default_target_free_pct=cfg.storage_retention.default_target_free_pct,
            default_max_archive_size_gb=cfg.storage_retention.default_max_archive_size_gb,
            default_short_retention_warning_days=(
                cfg.storage_retention.default_short_retention_warning_days
            ),
        )
    )


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _load_json_file(path: Path) -> object | None:
    if not path.exists():
        return None
    try:
        raw_text = path.read_text(encoding=_JSON_ENCODING)
    except OSError as exc:
        raise RetentionStateError(f"Failed to read {path}: {exc}") from exc
    try:
        payload: object = cast("object", json.loads(raw_text))
        return payload
    except json.JSONDecodeError as exc:
        raise RetentionStateError(f"Failed to parse {path}: {exc}") from exc


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
        raise RetentionStateError(f"Failed to write {path}: {exc}") from exc


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
    if isinstance(value, int) and not isinstance(value, bool) and value in {0, 1}:
        return value == 1
    raise RetentionConfigError(f"{key} must be a boolean")


def _coerce_optional_int(
    payload: Mapping[str, object],
    key: str,
    default: int,
    validator: Callable[[int, str], None],
) -> int:
    if key not in payload:
        return default
    value = payload[key]
    if isinstance(value, bool):
        raise RetentionConfigError(f"{key} must be an integer")
    if isinstance(value, int):
        candidate = value
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise RetentionConfigError(f"{key} must be an integer")
        try:
            candidate = int(stripped)
        except ValueError as exc:
            raise RetentionConfigError(f"{key} must be an integer") from exc
    else:
        raise RetentionConfigError(f"{key} must be an integer")
    validator(candidate, key)
    return candidate


def _validate_days(value: int, field_name: str) -> None:
    if not (_MIN_RETENTION_DAYS <= value <= _MAX_RETENTION_DAYS):
        raise RetentionConfigError(
            f"{field_name} must be between {_MIN_RETENTION_DAYS} and {_MAX_RETENTION_DAYS}"
        )


def _validate_free_space_target(value: int, field_name: str) -> None:
    if not (_MIN_FREE_SPACE_TARGET_PCT <= value <= _MAX_FREE_SPACE_TARGET_PCT):
        raise RetentionConfigError(
            f"{field_name} must be between "
            f"{_MIN_FREE_SPACE_TARGET_PCT} and {_MAX_FREE_SPACE_TARGET_PCT}"
        )


def _validate_archive_size(value: int, field_name: str) -> None:
    if not (_MIN_ARCHIVE_SIZE_GB <= value <= _MAX_ARCHIVE_SIZE_GB):
        raise RetentionConfigError(
            f"{field_name} must be between {_MIN_ARCHIVE_SIZE_GB} and {_MAX_ARCHIVE_SIZE_GB}"
        )


def _require_bool(value: object, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise RetentionStateError(f"{field_name} must be a boolean")
    return value


def _require_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RetentionStateError(f"{field_name} must be an integer")
    return value
