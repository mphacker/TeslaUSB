from __future__ import annotations

import json
import logging
import os
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from teslausb_web.services.chime_scheduler.constants import (
    _FIELD_CHIME,
    _FIELD_CREATED_AT,
    _FIELD_DAY,
    _FIELD_DAYS,
    _FIELD_ENABLED,
    _FIELD_GROUP_ID,
    _FIELD_HOLIDAY_NAME,
    _FIELD_ID,
    _FIELD_INTERVAL,
    _FIELD_LAST_RUN,
    _FIELD_MONTH,
    _FIELD_TIME,
    _FIELD_TYPE,
    _FIELD_UPDATED_AT,
    _ISO_FMT,
    _JSON_ENCODING,
    _JSON_INDENT,
    _SCHEDULES_KEY,
    _SCHEDULES_SCHEMA_VERSION,
    _SCHEMA_VERSION_KEY,
    _TMP_SUFFIX,
    _TYPE_WEEKLY,
)
from teslausb_web.services.chime_scheduler.types import (
    ChimeScheduleStateError,
    DateSchedule,
    HolidaySchedule,
    Schedule,
    WeeklySchedule,
    schedule_type_of,
)
from teslausb_web.services.chime_scheduler.validation import (
    _build_schedule,
    _normalize_schedule_id,
    _ScheduleDraft,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)

_GROUP_FILENAMES_KEY = "chime_filenames"
_GROUP_LEGACY_FILENAMES_KEY = "chimes"


@dataclass(frozen=True, slots=True)
class _GroupTarget:
    raw_groups: object | None
    raw_group: object | None
    filenames_raw: object | None


def _datetime_to_json(value: datetime) -> str:
    return value.astimezone(UTC).strftime(_ISO_FMT)


def _datetime_from_json(raw: object, *, field_name: str) -> datetime:
    if not isinstance(raw, str):
        msg = f"{field_name} must be an ISO datetime string"
        raise ChimeScheduleStateError(msg)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        msg = f"{field_name} is not a valid ISO datetime string"
        raise ChimeScheduleStateError(msg) from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _load_json_file(path: Path) -> object | None:
    if not path.exists():
        return None
    try:
        raw_text = path.read_text(encoding=_JSON_ENCODING)
    except OSError as exc:
        msg = f"Failed to read {path}: {exc}"
        raise ChimeScheduleStateError(msg) from exc
    try:
        payload: object = json.loads(raw_text)
        return payload
    except json.JSONDecodeError as exc:
        msg = f"Failed to parse {path}: {exc}"
        raise ChimeScheduleStateError(msg) from exc


def _write_json_atomically(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}{_TMP_SUFFIX}")
    raw_json = json.dumps(payload, indent=_JSON_INDENT, sort_keys=True) + "\n"
    try:
        with temp_path.open("w", encoding=_JSON_ENCODING, newline="\n") as file_handle:
            file_handle.write(raw_json)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temp_path, path)  # noqa: PTH105 - spec requires os.replace for atomic publish
    except OSError as exc:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        msg = f"Failed to write {path}: {exc}"
        raise ChimeScheduleStateError(msg) from exc


def _schedule_to_json(schedule: Schedule) -> dict[str, object]:
    payload: dict[str, object] = {
        _FIELD_ID: schedule.id,
        _FIELD_TYPE: schedule_type_of(schedule),
        _FIELD_CHIME: schedule.chime,
        _FIELD_GROUP_ID: schedule.group_id,
        _FIELD_ENABLED: schedule.enabled,
        _FIELD_CREATED_AT: _datetime_to_json(schedule.created_at),
        _FIELD_UPDATED_AT: _datetime_to_json(schedule.updated_at),
        _FIELD_LAST_RUN: (
            _datetime_to_json(schedule.last_run) if schedule.last_run is not None else None
        ),
    }
    if isinstance(schedule, WeeklySchedule):
        payload[_FIELD_DAYS] = list(schedule.days)
        payload[_FIELD_TIME] = schedule.time_hhmm
    elif isinstance(schedule, DateSchedule):
        payload[_FIELD_MONTH] = schedule.month
        payload[_FIELD_DAY] = schedule.day
        payload[_FIELD_TIME] = schedule.time_hhmm
    elif isinstance(schedule, HolidaySchedule):
        payload[_FIELD_HOLIDAY_NAME] = schedule.holiday_name
        payload[_FIELD_TIME] = schedule.time_hhmm
    else:
        payload[_FIELD_INTERVAL] = schedule.interval
    return payload


def _schedule_from_json(raw_schedule: object, *, intervals: dict[str, str]) -> Schedule:
    if not isinstance(raw_schedule, dict):
        raise ChimeScheduleStateError("Each schedule must be a JSON object")
    raw_id = raw_schedule.get(_FIELD_ID)
    raw_type = raw_schedule.get(_FIELD_TYPE, _TYPE_WEEKLY)
    if not isinstance(raw_id, str):
        raise ChimeScheduleStateError("Schedule ID must be a string")
    if not isinstance(raw_type, str):
        raise ChimeScheduleStateError("Schedule type is invalid")
    draft = _ScheduleDraft(
        schedule_type=raw_type,
        schedule_id=_normalize_schedule_id(raw_id),
        created_at=_datetime_from_json(
            raw_schedule.get(_FIELD_CREATED_AT),
            field_name=_FIELD_CREATED_AT,
        ),
        updated_at=_datetime_from_json(
            raw_schedule.get(_FIELD_UPDATED_AT),
            field_name=_FIELD_UPDATED_AT,
        ),
        last_run=(
            None
            if raw_schedule.get(_FIELD_LAST_RUN) is None
            else _datetime_from_json(
                raw_schedule.get(_FIELD_LAST_RUN),
                field_name=_FIELD_LAST_RUN,
            )
        ),
        enabled=raw_schedule.get(_FIELD_ENABLED, True),
        chime=raw_schedule.get(_FIELD_CHIME),
        group_id=raw_schedule.get(_FIELD_GROUP_ID),
        days=raw_schedule.get(_FIELD_DAYS),
        time_hhmm=raw_schedule.get(_FIELD_TIME),
        month=raw_schedule.get(_FIELD_MONTH),
        day=raw_schedule.get(_FIELD_DAY),
        holiday_name=raw_schedule.get(_FIELD_HOLIDAY_NAME),
        interval=raw_schedule.get(_FIELD_INTERVAL),
    )
    try:
        return _build_schedule(draft, intervals=intervals)
    except ValueError as exc:
        raise ChimeScheduleStateError(str(exc)) from exc


def load_schedules(path: Path, *, intervals: dict[str, str]) -> tuple[Schedule, ...]:
    payload = _load_json_file(path)
    if payload is None:
        return ()
    if isinstance(payload, list):
        schedules_raw: object = payload
    elif isinstance(payload, dict):
        raw_version = payload.get(_SCHEMA_VERSION_KEY)
        if not isinstance(raw_version, int):
            raise ChimeScheduleStateError("Schedule schema version must be an integer")
        if raw_version != _SCHEDULES_SCHEMA_VERSION:
            logger.warning("Unsupported chime schedule schema version %s in %s", raw_version, path)
            return ()
        schedules_raw = payload.get(_SCHEDULES_KEY, [])
    else:
        raise ChimeScheduleStateError("Schedules file must contain a JSON object or list")
    if not isinstance(schedules_raw, list):
        raise ChimeScheduleStateError("Schedules payload must be a list")
    return tuple(
        _schedule_from_json(raw_schedule, intervals=intervals) for raw_schedule in schedules_raw
    )


def persist_schedules(path: Path, schedules: Sequence[Schedule]) -> None:
    payload = {
        _SCHEMA_VERSION_KEY: _SCHEDULES_SCHEMA_VERSION,
        _SCHEDULES_KEY: [_schedule_to_json(schedule) for schedule in schedules],
    }
    _write_json_atomically(path, payload)


def resolve_group_choice(groups_file: Path, group_id: str) -> str | None:
    raw_groups = _load_json_file(groups_file)
    target = _group_target(raw_groups, group_id)
    if raw_groups is not None and not isinstance(raw_groups, dict):
        logger.warning("Chime groups file %s must contain a JSON object", groups_file)
    filenames = _group_filenames(target)
    if not filenames:
        return None
    return random.SystemRandom().choice(filenames)


def _group_target(raw_groups: object, group_id: str) -> _GroupTarget:
    raw_group = raw_groups.get(group_id) if isinstance(raw_groups, dict) else None
    filenames_raw = None
    if isinstance(raw_group, dict):
        filenames_raw = raw_group.get(
            _GROUP_FILENAMES_KEY,
            raw_group.get(_GROUP_LEGACY_FILENAMES_KEY, []),
        )
    return _GroupTarget(raw_groups=raw_groups, raw_group=raw_group, filenames_raw=filenames_raw)


def _group_filenames(target: _GroupTarget) -> tuple[str, ...]:
    if not isinstance(target.raw_groups, dict):
        return ()
    if not isinstance(target.raw_group, dict):
        return ()
    if not isinstance(target.filenames_raw, list):
        return ()
    return tuple(
        item.strip() for item in target.filenames_raw if isinstance(item, str) and item.strip()
    )
