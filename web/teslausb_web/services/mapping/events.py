from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass(frozen=True, slots=True)
class WaypointSample:
    timestamp: str
    lat: float
    lon: float
    heading: float | None = None
    speed_mps: float | None = None
    acceleration_x: float | None = None
    acceleration_y: float | None = None
    acceleration_z: float | None = None
    gear: str | None = None
    autopilot_state: str | None = None
    steering_angle: float | None = None
    brake_applied: bool = False
    blinker_on_left: bool = False
    blinker_on_right: bool = False
    video_path: str | None = None
    frame_offset: int = 0


@dataclass(frozen=True, slots=True)
class DetectedEventRecord:
    timestamp: str
    lat: float
    lon: float
    event_type: str
    severity: str
    description: str
    video_path: str | None
    frame_offset: int
    metadata: str | None


_ENGAGED_STATES = frozenset({"SELF_DRIVING", "AUTOSTEER"})


@dataclass(frozen=True, slots=True)
class _EventSpec:
    event_type: str
    severity: str
    description: str
    metadata: dict[str, float | str | None]


def _detect_events(
    waypoints: tuple[WaypointSample, ...],
    thresholds: dict[str, float],
    video_path: str,
) -> tuple[DetectedEventRecord, ...]:
    events: list[DetectedEventRecord] = []
    previous_autopilot: str | None = None
    for waypoint in waypoints:
        events.extend(_acceleration_events(waypoint, thresholds, video_path))
        events.extend(_speed_events(waypoint, thresholds, video_path))
        engagement = _autopilot_events(waypoint, previous_autopilot, video_path)
        if engagement is not None:
            events.append(engagement)
        previous_autopilot = waypoint.autopilot_state
    return _debounce_events(tuple(events), window_seconds=5.0)


def _acceleration_events(
    waypoint: WaypointSample,
    thresholds: dict[str, float],
    video_path: str,
) -> tuple[DetectedEventRecord, ...]:
    events: list[DetectedEventRecord] = []
    longitudinal = waypoint.acceleration_x or 0.0
    lateral = waypoint.acceleration_y or 0.0
    speed = waypoint.speed_mps or 0.0
    if longitudinal <= thresholds["emergency_brake_threshold"]:
        events.append(
            _build_event(
                waypoint,
                video_path,
                _EventSpec(
                    event_type="emergency_brake",
                    severity="critical",
                    description=f"Emergency braking: {longitudinal:.1f} m/s²",
                    metadata={"accel_x": longitudinal, "speed_mps": speed},
                ),
            )
        )
    elif longitudinal <= thresholds["harsh_brake_threshold"]:
        events.append(
            _build_event(
                waypoint,
                video_path,
                _EventSpec(
                    event_type="harsh_brake",
                    severity="warning",
                    description=f"Harsh braking: {longitudinal:.1f} m/s²",
                    metadata={"accel_x": longitudinal, "speed_mps": speed},
                ),
            )
        )
    if longitudinal >= thresholds["hard_accel_threshold"]:
        events.append(
            _build_event(
                waypoint,
                video_path,
                _EventSpec(
                    event_type="hard_acceleration",
                    severity="info",
                    description=f"Hard acceleration: {longitudinal:.1f} m/s²",
                    metadata={"accel_x": longitudinal, "speed_mps": speed},
                ),
            )
        )
    if abs(lateral) >= thresholds["sharp_turn_lateral_mps2"]:
        events.append(
            _build_event(
                waypoint,
                video_path,
                _EventSpec(
                    event_type="sharp_turn",
                    severity="warning",
                    description=f"Sharp turn: lateral {lateral:.1f} m/s²",
                    metadata={"accel_y": lateral, "speed_mps": speed},
                ),
            )
        )
    return tuple(events)


def _speed_events(
    waypoint: WaypointSample,
    thresholds: dict[str, float],
    video_path: str,
) -> tuple[DetectedEventRecord, ...]:
    speed = waypoint.speed_mps or 0.0
    limit = thresholds["speed_limit_mps"]
    if limit <= 0 or speed <= limit:
        return ()
    event = _build_event(
        waypoint,
        video_path,
        _EventSpec(
            event_type="speeding",
            severity="info",
            description=f"Speed: {speed * 2.237:.0f} mph",
            metadata={"speed_mps": speed, "limit_mps": limit},
        ),
    )
    return (event,)


def _autopilot_events(
    waypoint: WaypointSample,
    previous_autopilot: str | None,
    video_path: str,
) -> DetectedEventRecord | None:
    current = waypoint.autopilot_state or "NONE"
    if previous_autopilot is None:
        return None
    if previous_autopilot in _ENGAGED_STATES and current not in _ENGAGED_STATES:
        return _build_event(
            waypoint,
            video_path,
            _EventSpec(
                event_type="fsd_disengage",
                severity="warning",
                description=f"FSD disengaged: {previous_autopilot} → {current}",
                metadata={
                    "from": previous_autopilot,
                    "to": current,
                    "speed_mps": waypoint.speed_mps,
                },
            ),
        )
    if previous_autopilot not in _ENGAGED_STATES and current in _ENGAGED_STATES:
        return _build_event(
            waypoint,
            video_path,
            _EventSpec(
                event_type="fsd_engage",
                severity="info",
                description=f"FSD engaged: {current}",
                metadata={"state": current, "speed_mps": waypoint.speed_mps},
            ),
        )
    return None


def _build_event(
    waypoint: WaypointSample,
    video_path: str,
    spec: _EventSpec,
) -> DetectedEventRecord:
    return DetectedEventRecord(
        timestamp=waypoint.timestamp,
        lat=waypoint.lat,
        lon=waypoint.lon,
        event_type=spec.event_type,
        severity=spec.severity,
        description=spec.description,
        video_path=video_path,
        frame_offset=waypoint.frame_offset,
        metadata=json.dumps(spec.metadata, sort_keys=True),
    )


def _debounce_events(
    events: tuple[DetectedEventRecord, ...],
    *,
    window_seconds: float = 5.0,
) -> tuple[DetectedEventRecord, ...]:
    if not events:
        return events
    output: list[DetectedEventRecord] = []
    last_by_type: dict[str, str] = {}
    for event in events:
        if _within_window(last_by_type.get(event.event_type), event.timestamp, window_seconds):
            continue
        output.append(event)
        last_by_type[event.event_type] = event.timestamp
    return tuple(output)


def _within_window(last_timestamp: str | None, current: str, window_seconds: float) -> bool:
    if last_timestamp is None:
        return False
    try:
        delta = abs(
            datetime.fromisoformat(current).timestamp()
            - datetime.fromisoformat(last_timestamp).timestamp()
        )
    except ValueError:
        return False
    return delta < window_seconds


def iter_event_rows(events: Iterable[DetectedEventRecord]) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            event.timestamp,
            event.lat,
            event.lon,
            event.event_type,
            event.severity,
            event.description,
            event.video_path,
            event.frame_offset,
            event.metadata,
        )
        for event in events
    )
