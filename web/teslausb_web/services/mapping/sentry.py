from __future__ import annotations

import json
import math
from numbers import Real
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


_MIN_EVENT_PARTS = 2
_LAT_MIN = -90.0
_LAT_MAX = 90.0
_LON_MIN = -180.0
_LON_MAX = 180.0
_EVENT_FOLDER_INDEX = 1
_EVENT_DIR_PARTS = 2


def _read_event_json(rel_path: str, media_root: Path) -> dict[str, object] | None:
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) < _MIN_EVENT_PARTS:
        return None
    event_json = media_root / parts[0] / parts[1] / "event.json"
    if not event_json.is_file():
        return None
    with event_json.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        return None
    coords = _validated_coordinates(data)
    if coords is None:
        return None
    data["_lat"] = coords[0]
    data["_lon"] = coords[1]
    return data


def _validated_coordinates(payload: dict[str, object]) -> tuple[float, float] | None:
    lat_raw = payload.get("est_lat")
    lon_raw = payload.get("est_lon")
    if not isinstance(lat_raw, Real) or not isinstance(lon_raw, Real):
        return None
    lat = float(lat_raw)
    lon = float(lon_raw)
    if not math.isfinite(lat) or not math.isfinite(lon):
        return None
    if not (_LAT_MIN <= lat <= _LAT_MAX) or not (_LON_MIN <= lon <= _LON_MAX):
        return None
    if lat == 0.0 and lon == 0.0:
        return None
    return lat, lon


def _infer_sentry_event(
    connection: sqlite3.Connection,
    rel_path: str,
    file_timestamp: str | None,
    *,
    media_root: Path | None = None,
) -> bool:
    if file_timestamp is None:
        return False
    event_type = "sentry" if "SentryClips" in rel_path else "saved"
    folder_name, event_folder = _folder_names(rel_path)
    existing = _existing_event(connection, event_type, event_folder)
    if existing is not None and _metadata_source(existing[1]) == "event_json":
        return False
    if existing is not None:
        connection.execute("DELETE FROM detected_events WHERE id = ?", (existing[0],))
    coordinates, reason, location_source = _preferred_coordinates(
        connection,
        rel_path,
        file_timestamp,
        media_root=media_root,
    )
    if coordinates is None or location_source is None:
        return False
    connection.execute(
        """
        INSERT INTO detected_events (
            trip_id,
            timestamp,
            lat,
            lon,
            event_type,
            severity,
            description,
            video_path,
            frame_offset,
            metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            None,
            file_timestamp,
            coordinates[0],
            coordinates[1],
            event_type,
            "info",
            _event_description(event_type, reason, location_source),
            rel_path,
            0,
            json.dumps(
                {
                    "location_source": location_source,
                    "source_folder": folder_name,
                    "reason": reason,
                },
                sort_keys=True,
            ),
        ),
    )
    return True


def _folder_names(rel_path: str) -> tuple[str, str]:
    parts = rel_path.replace("\\", "/").split("/")
    folder_name = parts[0]
    event_folder = parts[_EVENT_FOLDER_INDEX] if len(parts) > _EVENT_DIR_PARTS else folder_name
    return folder_name, event_folder


def _existing_event(
    connection: sqlite3.Connection,
    event_type: str,
    event_folder: str,
) -> tuple[int, str | None] | None:
    row = connection.execute(
        "SELECT id, metadata FROM detected_events "
        "WHERE event_type = ? AND video_path LIKE ? LIMIT 1",
        (event_type, f"%{event_folder}%"),
    ).fetchone()
    if row is None:
        return None
    metadata = row["metadata"]
    return int(row["id"]), metadata if isinstance(metadata, str) else None


def _metadata_source(metadata: str | None) -> str | None:
    if metadata is None:
        return None
    try:
        parsed = json.loads(metadata)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    value = parsed.get("location_source")
    return value if isinstance(value, str) else None


def _preferred_coordinates(
    connection: sqlite3.Connection,
    rel_path: str,
    file_timestamp: str,
    *,
    media_root: Path | None,
) -> tuple[tuple[float, float] | None, str | None, str | None]:
    if media_root is not None:
        event_json = _read_event_json(rel_path, media_root)
        if event_json is not None:
            lat_value = event_json.get("_lat")
            lon_value = event_json.get("_lon")
            if isinstance(lat_value, Real) and isinstance(lon_value, Real):
                return (
                    (float(lat_value), float(lon_value)),
                    _optional_str(event_json.get("reason")) or "unknown",
                    "event_json",
                )
    nearest = _nearest_waypoint(connection, file_timestamp)
    if nearest is None:
        return None, None, None
    return nearest, None, "nearest_waypoint"


def _nearest_waypoint(
    connection: sqlite3.Connection,
    file_timestamp: str,
) -> tuple[float, float] | None:
    row = connection.execute(
        "SELECT lat, lon FROM waypoints WHERE timestamp <= ? AND lat != 0 AND lon != 0 "
        "ORDER BY timestamp DESC LIMIT 1",
        (file_timestamp,),
    ).fetchone()
    if row is None:
        row = connection.execute(
            "SELECT lat, lon FROM waypoints WHERE lat != 0 AND lon != 0 "
            "ORDER BY timestamp ASC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    lat_value = row["lat"]
    lon_value = row["lon"]
    if not isinstance(lat_value, Real) or not isinstance(lon_value, Real):
        return None
    return float(lat_value), float(lon_value)


def _event_description(event_type: str, reason: str | None, location_source: str) -> str:
    label = "Sentry Mode" if event_type == "sentry" else "Saved Clip"
    if reason is None:
        return f"{label} event (location from {location_source})"
    return f"{label} event ({reason}, location from {location_source})"


def _optional_str(raw: object) -> str | None:
    return raw if isinstance(raw, str) and raw.strip() else None
