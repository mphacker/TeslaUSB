from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from numbers import Real
from pathlib import Path
from typing import Protocol, cast


class TelemetryMessageProtocol(Protocol):
    has_gps: bool
    timestamp_ms: int
    latitude_deg: float
    longitude_deg: float
    heading_deg: float | None
    vehicle_speed_mps: float | None
    linear_acceleration_x: float | None
    linear_acceleration_y: float | None
    linear_acceleration_z: float | None
    gear_state: str | None
    autopilot_state: str | None
    steering_wheel_angle: float | None
    brake_applied: bool
    blinker_on_left: bool
    blinker_on_right: bool
    frame_index: int
    speed_mph: float


class SeiSidecarProtocol(Protocol):
    sample_rate: int
    sei_count: int
    no_gps_count: int
    mvhd_creation_time_utc: datetime | None
    messages: tuple[TelemetryMessageProtocol, ...]


class SeiParserProtocol(Protocol):
    def read_sei_sidecar(self, video_path: Path) -> SeiSidecarProtocol | None: ...

    def extract_mvhd_creation_time(self, video_path: Path) -> datetime | None: ...

    def extract_sei_messages(
        self,
        video_path: Path,
        *,
        sample_rate: int,
    ) -> tuple[TelemetryMessageProtocol, ...]: ...


@dataclass(frozen=True, slots=True)
class FallbackTelemetryMessage:
    has_gps: bool
    timestamp_ms: int
    latitude_deg: float
    longitude_deg: float
    heading_deg: float | None
    vehicle_speed_mps: float | None
    linear_acceleration_x: float | None
    linear_acceleration_y: float | None
    linear_acceleration_z: float | None
    gear_state: str | None
    autopilot_state: str | None
    steering_wheel_angle: float | None
    brake_applied: bool
    blinker_on_left: bool
    blinker_on_right: bool
    frame_index: int

    @property
    def speed_mph(self) -> float:
        if self.vehicle_speed_mps is None:
            return 0.0
        return self.vehicle_speed_mps * 2.23694


@dataclass(frozen=True, slots=True)
class FallbackSeiSidecar:
    sample_rate: int
    sei_count: int
    no_gps_count: int
    mvhd_creation_time_utc: datetime | None
    messages: tuple[FallbackTelemetryMessage, ...]


class FallbackSeiParser:
    """JSON-sidecar fallback used until Rust-backed SEI IPC lands."""

    def read_sei_sidecar(self, video_path: Path) -> SeiSidecarProtocol | None:
        payload = _load_json_payload(video_path)
        if payload is None:
            return None
        return cast("SeiSidecarProtocol", _sidecar_from_payload(payload))

    def extract_mvhd_creation_time(self, video_path: Path) -> datetime | None:
        sidecar = self.read_sei_sidecar(video_path)
        return None if sidecar is None else sidecar.mvhd_creation_time_utc

    def extract_sei_messages(
        self,
        video_path: Path,
        *,
        sample_rate: int,
    ) -> tuple[TelemetryMessageProtocol, ...]:
        sidecar = self.read_sei_sidecar(video_path)
        if sidecar is None or sidecar.sample_rate != sample_rate:
            return ()
        return sidecar.messages


def _load_json_payload(video_path: Path) -> dict[str, object] | None:
    candidates = (Path(f"{video_path}.sei.json"), Path(f"{video_path}.json"))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        return _read_json_file(candidate)
    return None


def _read_json_file(path: Path) -> dict[str, object] | None:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else None


def _sidecar_from_payload(payload: dict[str, object]) -> FallbackSeiSidecar:
    mvhd_raw = payload.get("mvhd_creation_time_utc")
    return FallbackSeiSidecar(
        sample_rate=_as_int(payload.get("sample_rate"), 30),
        sei_count=_as_int(payload.get("sei_count"), 0),
        no_gps_count=_as_int(payload.get("no_gps_count"), 0),
        mvhd_creation_time_utc=_parse_datetime(mvhd_raw),
        messages=_messages_from_payload(payload.get("messages")),
    )


def _messages_from_payload(raw: object) -> tuple[FallbackTelemetryMessage, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(_message_from_payload(item) for item in raw if isinstance(item, dict))


def _message_from_payload(payload: dict[str, object]) -> FallbackTelemetryMessage:
    return FallbackTelemetryMessage(
        has_gps=_as_bool(payload.get("has_gps"), default=True),
        timestamp_ms=_as_int(payload.get("timestamp_ms"), 0),
        latitude_deg=_as_float(payload.get("latitude_deg"), 0.0),
        longitude_deg=_as_float(payload.get("longitude_deg"), 0.0),
        heading_deg=_as_optional_float(payload.get("heading_deg")),
        vehicle_speed_mps=_as_optional_float(payload.get("vehicle_speed_mps")),
        linear_acceleration_x=_as_optional_float(payload.get("linear_acceleration_x")),
        linear_acceleration_y=_as_optional_float(payload.get("linear_acceleration_y")),
        linear_acceleration_z=_as_optional_float(payload.get("linear_acceleration_z")),
        gear_state=_as_optional_str(payload.get("gear_state")),
        autopilot_state=_as_optional_str(payload.get("autopilot_state")),
        steering_wheel_angle=_as_optional_float(payload.get("steering_wheel_angle")),
        brake_applied=_as_bool(payload.get("brake_applied"), default=False),
        blinker_on_left=_as_bool(payload.get("blinker_on_left"), default=False),
        blinker_on_right=_as_bool(payload.get("blinker_on_right"), default=False),
        frame_index=_as_int(payload.get("frame_index"), 0),
    )


def get_sei_parser(parser: SeiParserProtocol | None = None) -> SeiParserProtocol:
    if parser is not None:
        return parser
    return cast("SeiParserProtocol", FallbackSeiParser())


def _parse_datetime(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _as_bool(raw: object, *, default: bool) -> bool:
    return raw if isinstance(raw, bool) else default


def _as_int(raw: object, default: int) -> int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else default


def _as_float(raw: object, default: float) -> float:
    if isinstance(raw, Real):
        return float(raw)
    return default


def _as_optional_float(raw: object) -> float | None:
    if isinstance(raw, Real):
        return float(raw)
    return None


def _as_optional_str(raw: object) -> str | None:
    return raw if isinstance(raw, str) and raw.strip() else None
