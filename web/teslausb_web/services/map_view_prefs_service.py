"""Web-only map-view preferences service.

These preferences affect only presentation in the Flask-rendered map UI.
They are deliberately stored outside the worker-shared mapping overrides
file so display-only changes never trigger worker materialization.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

    from teslausb_web.config import WebConfig

_SCHEMA_VERSION: Final[int] = 1
_JSON_ENCODING: Final[str] = "utf-8"
_JSON_INDENT: Final[int] = 2
_TMP_SUFFIX: Final[str] = ".tmp"


class SpeedUnits(StrEnum):
    """Allowed display units for map speeds."""

    MPH = "mph"
    KPH = "kph"


_DEFAULT_SPEED_UNITS: Final[SpeedUnits] = SpeedUnits.MPH


class MapViewPreferencesError(ValueError):
    """Validation or persistence failed for map-view preferences."""


@dataclass(frozen=True, slots=True)
class MapViewPreferences:
    """Cold-path map presentation preferences."""

    speed_units: SpeedUnits = _DEFAULT_SPEED_UNITS


_DEFAULT_PREFERENCES: Final[MapViewPreferences] = MapViewPreferences()


class MapViewPreferencesService:
    """Read and write web-only map view preferences."""

    def __init__(self, prefs_path: Path) -> None:
        if not prefs_path.is_absolute() and not PurePosixPath(prefs_path.as_posix()).is_absolute():
            raise MapViewPreferencesError(
                f"prefs_path must be absolute, got {prefs_path!r}",
            )
        self._path = prefs_path
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        """Return the web-only preferences file path."""
        return self._path

    def get_preferences(self) -> MapViewPreferences:
        """Return preferences from disk, or defaults when the file is missing."""
        with self._lock:
            try:
                raw_text = self._path.read_text(encoding=_JSON_ENCODING)
            except FileNotFoundError:
                return _DEFAULT_PREFERENCES
            except OSError as exc:
                raise MapViewPreferencesError(f"Failed to read {self._path}: {exc}") from exc
        try:
            payload = cast("object", json.loads(raw_text))
        except json.JSONDecodeError as exc:
            raise MapViewPreferencesError(f"Failed to parse {self._path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise MapViewPreferencesError(f"{self._path} must contain a JSON object")
        raw_version = payload.get("schema_version", _SCHEMA_VERSION)
        if raw_version != _SCHEMA_VERSION:
            raise MapViewPreferencesError(
                f"Unsupported map-view-preferences schema version: {raw_version!r}",
            )
        return _preferences_from_mapping(payload)

    def save_preferences(self, *, speed_units: SpeedUnits | str) -> MapViewPreferences:
        """Validate and persist preferences atomically."""
        preferences = MapViewPreferences(speed_units=_coerce_speed_units(speed_units))
        payload: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "speed_units": preferences.speed_units.value,
        }
        with self._lock:
            _write_json_atomically(self._path, payload)
        return preferences

    def serialize_for_template(self, preferences: MapViewPreferences) -> dict[str, object]:
        """Return template keys for map-view preferences."""
        return {"speed_units": preferences.speed_units.value}


def _preferences_from_mapping(payload: Mapping[str, object]) -> MapViewPreferences:
    raw_speed_units = payload.get("speed_units", _DEFAULT_SPEED_UNITS)
    return MapViewPreferences(speed_units=_coerce_speed_units(raw_speed_units))


def _coerce_speed_units(value: object) -> SpeedUnits:
    if isinstance(value, SpeedUnits):
        return value
    if not isinstance(value, str):
        raise MapViewPreferencesError(f"speed_units must be 'mph' or 'kph', got {value!r}")
    try:
        return SpeedUnits(value.strip().lower())
    except ValueError as exc:
        raise MapViewPreferencesError(
            f"speed_units must be 'mph' or 'kph', got {value!r}",
        ) from exc


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
        raise MapViewPreferencesError(f"Failed to write {path}: {exc}") from exc


def make_map_view_prefs_service(cfg: WebConfig) -> MapViewPreferencesService:
    """Build a map-view preferences service from application config."""
    return MapViewPreferencesService(cfg.mapping.view_prefs_path)


__all__ = (
    "MapViewPreferences",
    "MapViewPreferencesError",
    "MapViewPreferencesService",
    "SpeedUnits",
    "make_map_view_prefs_service",
)
