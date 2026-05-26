"""Event derivation over the worker DB's waypoints.

Driver events (speed, accel, sharp turn, AP transitions) and sentry
events are derived at query time — no second table, no second DB. See
ADR-0017 ``§Decision`` for the rationale.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from teslausb_web.services.mapping_trip_derivation import epoch_to_iso

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from teslausb_web.services.mapping_trip_derivation import (
        AbsoluteWaypoint,
        TripGroup,
        WorkerClip,
    )

SEVERITY_INFO: Final[str] = "info"
SEVERITY_WARNING: Final[str] = "warning"
SEVERITY_CRITICAL: Final[str] = "critical"

EVENT_SPEED_LIMIT_EXCEEDED: Final[str] = "speed_limit_exceeded"
EVENT_HARD_ACCELERATION: Final[str] = "hard_acceleration"
EVENT_HARSH_BRAKING: Final[str] = "harsh_braking"
EVENT_EMERGENCY_BRAKING: Final[str] = "emergency_braking"
EVENT_SHARP_TURN: Final[str] = "sharp_turn"
EVENT_AUTOPILOT_ENGAGED: Final[str] = "autopilot_engaged"
EVENT_AUTOPILOT_DISENGAGED: Final[str] = "autopilot_disengaged"
EVENT_SENTRY: Final[str] = "sentry"

_SPEED_LIMIT_MPS: Final[float] = 35.76
_HARD_ACCEL_MPS2: Final[float] = 3.5
_HARSH_BRAKE_MPS2: Final[float] = -4.0
_EMERGENCY_BRAKE_MPS2: Final[float] = -7.0
_SHARP_TURN_LATERAL_MPS2: Final[float] = 4.0

_AP_ENGAGED_STATES: Final[frozenset[str]] = frozenset({"SELF_DRIVING", "AUTOSTEER", "TACC"})

_EVENT_CODES: Final[dict[str, int]] = {
    EVENT_SPEED_LIMIT_EXCEEDED: 0,
    EVENT_HARD_ACCELERATION: 1,
    EVENT_HARSH_BRAKING: 2,
    EVENT_EMERGENCY_BRAKING: 3,
    EVENT_SHARP_TURN: 4,
    EVENT_AUTOPILOT_ENGAGED: 5,
    EVENT_AUTOPILOT_DISENGAGED: 6,
}
_SENTRY_CODE: Final[int] = 15
_ID_STRIDE: Final[int] = 16

_EVENT_SEVERITIES: Final[dict[str, str]] = {
    EVENT_SPEED_LIMIT_EXCEEDED: SEVERITY_WARNING,
    EVENT_HARD_ACCELERATION: SEVERITY_WARNING,
    EVENT_HARSH_BRAKING: SEVERITY_WARNING,
    EVENT_EMERGENCY_BRAKING: SEVERITY_CRITICAL,
    EVENT_SHARP_TURN: SEVERITY_WARNING,
    EVENT_AUTOPILOT_ENGAGED: SEVERITY_INFO,
    EVENT_AUTOPILOT_DISENGAGED: SEVERITY_INFO,
    EVENT_SENTRY: SEVERITY_INFO,
}


@dataclass(frozen=True, slots=True)
class DerivedEvent:
    """A single derived event suitable for materialising into an EventRow."""

    id: int
    trip_id: int | None
    timestamp: str
    lat: float | None
    lon: float | None
    event_type: str
    severity: str
    description: str
    video_path: str | None
    frame_offset: int | None


def event_severity(event_type: str) -> str:
    """Return the severity bucket for a derived-event type."""
    return _EVENT_SEVERITIES.get(event_type, SEVERITY_INFO)


def is_autopilot_engaged(autopilot_state: str | None) -> bool:
    """True when the autopilot state means the car is actively driving."""
    return autopilot_state is not None and autopilot_state in _AP_ENGAGED_STATES


def derive_trip_events(
    trip: TripGroup,
    waypoints: Sequence[AbsoluteWaypoint],
    *,
    speed_limit_mps: float = _SPEED_LIMIT_MPS,
) -> tuple[DerivedEvent, ...]:
    """Emit every waypoint-derived event for the given trip.

    ``speed_limit_mps`` controls the speed-limit-exceeded threshold;
    pass ``0`` (or any non-positive value) to disable the event entirely.
    Defaults to the legacy 80 mph (35.76 m/s) to keep existing call
    sites working unchanged.
    """
    events: list[DerivedEvent] = []
    previous_engaged: bool | None = None
    for entry in waypoints:
        events.extend(_speed_event(entry, trip.id, speed_limit_mps))
        events.extend(_accel_event(entry, trip.id))
        events.extend(_sharp_turn_event(entry, trip.id))
        current_engaged = is_autopilot_engaged(entry.waypoint.autopilot_state)
        if previous_engaged is not None and current_engaged != previous_engaged:
            events.append(_autopilot_transition_event(entry, trip.id, engaged=current_engaged))
        previous_engaged = current_engaged
    return tuple(events)


def derive_sentry_events(clips: Iterable[WorkerClip]) -> tuple[DerivedEvent, ...]:
    """Emit one event per sentry clip with zero GPS waypoints."""
    return tuple(_sentry_event(clip) for clip in clips)


def _speed_event(
    entry: AbsoluteWaypoint,
    trip_id: int,
    speed_limit_mps: float,
) -> tuple[DerivedEvent, ...]:
    if speed_limit_mps <= 0.0 or entry.waypoint.speed_mps <= speed_limit_mps:
        return ()
    return (
        _waypoint_event(
            entry,
            trip_id,
            EVENT_SPEED_LIMIT_EXCEEDED,
            f"Speed {entry.waypoint.speed_mps:.1f} m/s exceeded limit {speed_limit_mps:.1f} m/s",
        ),
    )


def _accel_event(entry: AbsoluteWaypoint, trip_id: int) -> tuple[DerivedEvent, ...]:
    accel = entry.waypoint.acceleration_x
    if accel is None:
        return ()
    if accel < _EMERGENCY_BRAKE_MPS2:
        return (
            _waypoint_event(
                entry,
                trip_id,
                EVENT_EMERGENCY_BRAKING,
                f"Emergency braking detected ({accel:.2f} m/s^2)",
            ),
        )
    if accel < _HARSH_BRAKE_MPS2:
        return (
            _waypoint_event(
                entry,
                trip_id,
                EVENT_HARSH_BRAKING,
                f"Harsh braking detected ({accel:.2f} m/s^2)",
            ),
        )
    if accel > _HARD_ACCEL_MPS2:
        return (
            _waypoint_event(
                entry,
                trip_id,
                EVENT_HARD_ACCELERATION,
                f"Hard acceleration detected ({accel:.2f} m/s^2)",
            ),
        )
    return ()


def _sharp_turn_event(entry: AbsoluteWaypoint, trip_id: int) -> tuple[DerivedEvent, ...]:
    accel_y = entry.waypoint.acceleration_y
    if accel_y is None or abs(accel_y) <= _SHARP_TURN_LATERAL_MPS2:
        return ()
    return (
        _waypoint_event(
            entry,
            trip_id,
            EVENT_SHARP_TURN,
            f"Sharp turn detected (lateral {accel_y:.2f} m/s^2)",
        ),
    )


def _autopilot_transition_event(
    entry: AbsoluteWaypoint,
    trip_id: int,
    *,
    engaged: bool,
) -> DerivedEvent:
    event_type = EVENT_AUTOPILOT_ENGAGED if engaged else EVENT_AUTOPILOT_DISENGAGED
    description = (
        f"Autopilot engaged ({entry.waypoint.autopilot_state})"
        if engaged
        else "Autopilot disengaged"
    )
    return _waypoint_event(entry, trip_id, event_type, description)


def _waypoint_event(
    entry: AbsoluteWaypoint,
    trip_id: int,
    event_type: str,
    description: str,
) -> DerivedEvent:
    return DerivedEvent(
        id=_waypoint_event_id(entry.waypoint.id, event_type),
        trip_id=trip_id,
        timestamp=entry.iso_timestamp,
        lat=entry.waypoint.latitude_deg,
        lon=entry.waypoint.longitude_deg,
        event_type=event_type,
        severity=event_severity(event_type),
        description=description,
        video_path=entry.clip.relative_path,
        frame_offset=entry.waypoint.frame_index,
    )


def _sentry_event(clip: WorkerClip) -> DerivedEvent:
    return DerivedEvent(
        id=_sentry_event_id(clip.id),
        trip_id=None,
        timestamp=epoch_to_iso(clip.clip_started_utc),
        lat=None,
        lon=None,
        event_type=EVENT_SENTRY,
        severity=event_severity(EVENT_SENTRY),
        description="Sentry mode recording",
        video_path=clip.relative_path,
        frame_offset=None,
    )


def _waypoint_event_id(waypoint_id: int, event_type: str) -> int:
    code = _EVENT_CODES[event_type]
    return waypoint_id * _ID_STRIDE + code


def _sentry_event_id(clip_id: int) -> int:
    return clip_id * _ID_STRIDE + _SENTRY_CODE


__all__ = (
    "EVENT_AUTOPILOT_DISENGAGED",
    "EVENT_AUTOPILOT_ENGAGED",
    "EVENT_EMERGENCY_BRAKING",
    "EVENT_HARD_ACCELERATION",
    "EVENT_HARSH_BRAKING",
    "EVENT_SENTRY",
    "EVENT_SHARP_TURN",
    "EVENT_SPEED_LIMIT_EXCEEDED",
    "SEVERITY_CRITICAL",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "DerivedEvent",
    "derive_sentry_events",
    "derive_trip_events",
    "event_severity",
    "is_autopilot_engaged",
)
