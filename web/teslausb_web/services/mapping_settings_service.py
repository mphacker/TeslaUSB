"""JSON-backed worker mapping-overrides service with mtime-cached snapshot.

The materializer-affecting settings (``trip_gap_minutes`` and
``speed_limit_mph``) live in a tiny JSON file alongside the worker DB.
Hot-path callers (``MappingQueries``, ``derive_trip_events``) read the
cached snapshot hundreds of times per query — so the load path is built
around three properties:

* **One ``stat()`` syscall per access** (no file read, no JSON parse
  when the file hasn't changed). ``os.stat`` is sub-microsecond on
  the Pi Zero 2 W.
* **In-memory snapshot is a single ~24-byte frozen dataclass**;
  the speed-limit threshold is pre-converted to m/s at load time so
  derivation never re-multiplies.
* **Atomic write on save** (temp + ``rename``) and the snapshot cache
  is updated in-place, so we never read what we just wrote.

The Rust worker reads the same file (see
``rust/crates/teslausb-worker/src/mapping_overrides.rs``) and uses
the same ``stat → cache`` discipline so the two implementations stay
in sync without an IPC round-trip.

Speed-limit semantics: ``speed_limit_mph = 0`` disables speed-limit
events entirely. Anything > 0 is the threshold.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

_SCHEMA_VERSION: Final[int] = 1
_JSON_ENCODING: Final[str] = "utf-8"
_JSON_INDENT: Final[int] = 2
_TMP_SUFFIX: Final[str] = ".tmp"

# Operator-visible bounds. The HTML form min/max mirror these.
_TRIP_GAP_MIN: Final[int] = 1
_TRIP_GAP_MAX: Final[int] = 60
_SPEED_LIMIT_MIN_MPH: Final[int] = 0
_SPEED_LIMIT_MAX_MPH: Final[int] = 200

# Defaults match config.py / materializer.rs so a missing file behaves
# identically to a baseline install.
_DEFAULT_TRIP_GAP_MINUTES: Final[int] = 5
_DEFAULT_SPEED_LIMIT_MPH: Final[int] = 0

_MPH_TO_MPS: Final[float] = 0.44704


class MappingSettingsError(ValueError):
    """Validation or persistence failed for mapping settings."""


@dataclass(frozen=True, slots=True)
class MappingSettings:
    """Immutable view used by the hot path.

    ``speed_limit_mps`` is derived once at load time; derivation
    compares directly against this float without per-call conversion.
    """

    trip_gap_minutes: int
    speed_limit_mph: int
    speed_limit_mps: float

    @property
    def trip_gap_seconds(self) -> int:
        """Return the trip gap threshold in seconds."""
        return self.trip_gap_minutes * 60

    @property
    def speed_limit_enabled(self) -> bool:
        """Return whether speed-limit event derivation is enabled."""
        return self.speed_limit_mph > 0


_DEFAULT_SNAPSHOT: Final[MappingSettings] = MappingSettings(
    trip_gap_minutes=_DEFAULT_TRIP_GAP_MINUTES,
    speed_limit_mph=_DEFAULT_SPEED_LIMIT_MPH,
    speed_limit_mps=_DEFAULT_SPEED_LIMIT_MPH * _MPH_TO_MPS,
)


class MappingSettingsService:
    """Hot-path-friendly mapping settings reader/writer.

    The service caches the parsed snapshot plus the file's ``mtime_ns``.
    ``get_settings()`` calls ``os.stat`` once and returns the cache
    when the timestamp is unchanged — no file read, no JSON parse.
    """

    def __init__(self, overrides_path: Path) -> None:
        if (
            not overrides_path.is_absolute()
            and not PurePosixPath(
                overrides_path.as_posix(),
            ).is_absolute()
        ):
            raise MappingSettingsError(
                f"overrides_path must be absolute, got {overrides_path!r}",
            )
        self._path = overrides_path
        self._lock = threading.RLock()
        # `(mtime_ns, size)` of the cached snapshot. `None` means
        # "no successful load yet" — forces the first call to read.
        # A sentinel of `(0, 0)` represents "file is known to be
        # missing; default snapshot applies."
        self._cached_stat: tuple[int, int] | None = None
        self._cached_snapshot: MappingSettings = _DEFAULT_SNAPSHOT

    @property
    def path(self) -> Path:
        """Return the worker-shared overrides file path."""
        return self._path

    def get_settings(self) -> MappingSettings:
        """Return the live snapshot — at most one ``stat()`` syscall.

        Read once, JSON-parse only when the file's mtime/size changes.
        Safe to call from every request and every materializer tick.
        """
        with self._lock:
            current = self._stat_or_missing()
            if current == self._cached_stat:
                return self._cached_snapshot
            self._cached_snapshot = self._load_locked(current)
            self._cached_stat = current
            return self._cached_snapshot

    def save_settings(
        self,
        *,
        trip_gap_minutes: int,
        speed_limit_mph: int,
    ) -> MappingSettings:
        """Validate, persist atomically, and update the cache in-place."""
        snapshot = _build_snapshot(
            trip_gap_minutes=trip_gap_minutes,
            speed_limit_mph=speed_limit_mph,
        )
        payload: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "speed_limit_mph": snapshot.speed_limit_mph,
            "trip_gap_minutes": snapshot.trip_gap_minutes,
        }
        with self._lock:
            _write_json_atomically(self._path, payload)
            # Update the cache from the value we just wrote — never
            # re-read disk.
            self._cached_snapshot = snapshot
            try:
                stat_result = self._path.stat()
                self._cached_stat = (stat_result.st_mtime_ns, stat_result.st_size)
            except OSError as exc:
                # The write succeeded but we cannot stat the file —
                # invalidate the cache so the next read falls through.
                logger.warning("Saved mapping settings but stat failed: %s", exc)
                self._cached_stat = None
        logger.info(
            "Saved mapping settings to %s (trip_gap=%d min, speed_limit=%d mph)",
            self._path,
            snapshot.trip_gap_minutes,
            snapshot.speed_limit_mph,
        )
        return snapshot

    def serialize_for_template(self, snapshot: MappingSettings) -> dict[str, object]:
        """Mirror the legacy ``cfg_mapping`` dict shape used by the dashboard."""
        return {
            "trip_gap_minutes": snapshot.trip_gap_minutes,
            "speed_limit_mph": snapshot.speed_limit_mph,
        }

    def _stat_or_missing(self) -> tuple[int, int]:
        try:
            result = self._path.stat()
        except FileNotFoundError:
            return (0, 0)
        except OSError as exc:
            # A stat failure other than "missing" is unusual; surface
            # via the load path so the caller gets a clear error.
            raise MappingSettingsError(f"Failed to stat {self._path}: {exc}") from exc
        return (result.st_mtime_ns, result.st_size)

    def _load_locked(self, stat_signature: tuple[int, int]) -> MappingSettings:
        if stat_signature == (0, 0):
            logger.debug(
                "mapping settings file %s missing; using defaults",
                self._path,
            )
            return _DEFAULT_SNAPSHOT
        try:
            raw_text = self._path.read_text(encoding=_JSON_ENCODING)
        except OSError as exc:
            raise MappingSettingsError(f"Failed to read {self._path}: {exc}") from exc
        try:
            payload = cast("object", json.loads(raw_text))
        except json.JSONDecodeError as exc:
            raise MappingSettingsError(
                f"Failed to parse {self._path}: {exc}",
            ) from exc
        if not isinstance(payload, dict):
            raise MappingSettingsError(
                f"{self._path} must contain a JSON object",
            )
        raw_version = payload.get("schema_version", _SCHEMA_VERSION)
        if raw_version != _SCHEMA_VERSION:
            raise MappingSettingsError(
                f"Unsupported mapping-settings schema version: {raw_version!r}",
            )
        return _snapshot_from_mapping(payload)


def _snapshot_from_mapping(payload: Mapping[str, object]) -> MappingSettings:
    trip_gap = _coerce_int(payload, "trip_gap_minutes", _DEFAULT_TRIP_GAP_MINUTES)
    speed_mph = _coerce_int(payload, "speed_limit_mph", _DEFAULT_SPEED_LIMIT_MPH)
    return _build_snapshot(
        trip_gap_minutes=trip_gap,
        speed_limit_mph=speed_mph,
    )


def _build_snapshot(
    *,
    trip_gap_minutes: int,
    speed_limit_mph: int,
) -> MappingSettings:
    if not _TRIP_GAP_MIN <= trip_gap_minutes <= _TRIP_GAP_MAX:
        raise MappingSettingsError(
            f"trip_gap_minutes must be between {_TRIP_GAP_MIN} and {_TRIP_GAP_MAX}, "
            f"got {trip_gap_minutes}",
        )
    if not _SPEED_LIMIT_MIN_MPH <= speed_limit_mph <= _SPEED_LIMIT_MAX_MPH:
        raise MappingSettingsError(
            f"speed_limit_mph must be between {_SPEED_LIMIT_MIN_MPH} and "
            f"{_SPEED_LIMIT_MAX_MPH}, got {speed_limit_mph}",
        )
    return MappingSettings(
        trip_gap_minutes=trip_gap_minutes,
        speed_limit_mph=speed_limit_mph,
        speed_limit_mps=speed_limit_mph * _MPH_TO_MPS,
    )


def _coerce_int(payload: Mapping[str, object], key: str, default: int) -> int:
    if key not in payload:
        return default
    value = payload[key]
    if isinstance(value, bool):
        # `bool` is a subclass of `int` — explicitly reject it so a
        # stray ``true`` in the JSON doesn't silently mean ``1``.
        raise MappingSettingsError(f"{key} must be an integer, got bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lstrip("-").isdigit():
            return int(stripped)
    raise MappingSettingsError(f"{key} must be an integer, got {value!r}")


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
        raise MappingSettingsError(f"Failed to write {path}: {exc}") from exc


def make_mapping_settings_service(cfg: WebConfig) -> MappingSettingsService:
    """Build a mapping settings service from application config."""
    return MappingSettingsService(cfg.mapping.overrides_path)


__all__ = (
    "MappingSettings",
    "MappingSettingsError",
    "MappingSettingsService",
    "make_mapping_settings_service",
)
