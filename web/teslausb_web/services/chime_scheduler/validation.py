from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from teslausb_web.services.chime_scheduler.constants import (
    _DOW_MAX,
    _DOW_MIN,
    _FIELD_CHIME,
    _FIELD_ENABLED,
    _FIELD_GROUP_ID,
    _SCHEDULE_NAME_MAX_LEN,
    _TIME_FMT_PATTERN,
    _TYPE_DATE,
    _TYPE_HOLIDAY,
    _TYPE_RECURRING,
    _TYPE_WEEKLY,
    _US_FIXED_HOLIDAYS,
    _US_MOVABLE_HOLIDAYS,
)
from teslausb_web.services.chime_scheduler.types import (
    ChimeScheduleError,
    DateSchedule,
    HolidaySchedule,
    RecurringSchedule,
    Schedule,
    ScheduleId,
    ScheduleOperationResult,
    WeeklySchedule,
)

_MONTH_MIN: Final[int] = 1
_MONTH_MAX: Final[int] = 12
_DAY_MIN: Final[int] = 1
_DAY_MAX: Final[int] = 31
_TIME_RE: Final[re.Pattern[str]] = re.compile(_TIME_FMT_PATTERN)
_ALLOWED_TYPES: Final[frozenset[str]] = frozenset(
    {_TYPE_WEEKLY, _TYPE_DATE, _TYPE_HOLIDAY, _TYPE_RECURRING}
)


@dataclass(frozen=True, slots=True)
class _ScheduleDraft:
    schedule_type: str
    schedule_id: ScheduleId
    created_at: datetime
    updated_at: datetime
    last_run: datetime | None
    enabled: object
    chime: object
    group_id: object
    days: object | None = None
    time_hhmm: object | None = None
    month: object | None = None
    day: object | None = None
    holiday_name: object | None = None
    interval: object | None = None


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _ensure_aware_datetime(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None:
        msg = f"{field_name} must be timezone-aware"
        raise ChimeScheduleError(msg)
    return value.astimezone(UTC)


def _operation_result(
    *,
    ok: bool,
    message: str,
    schedule_id: ScheduleId | None,
) -> ScheduleOperationResult:
    return ScheduleOperationResult(ok=ok, message=message, schedule_id=schedule_id)


def _normalize_schedule_id(schedule_id: str) -> ScheduleId:
    normalized = schedule_id.strip()
    if not normalized:
        raise ChimeScheduleError("Schedule ID must not be empty")
    if len(normalized) > _SCHEDULE_NAME_MAX_LEN:
        msg = f"Schedule ID must be <= {_SCHEDULE_NAME_MAX_LEN} characters"
        raise ChimeScheduleError(msg)
    return normalized


def _normalize_schedule_type(raw: object) -> str:
    if not isinstance(raw, str) or raw not in _ALLOWED_TYPES:
        msg = "type must be one of weekly, date, holiday, recurring"
        raise ChimeScheduleError(msg)
    return raw


def _optional_str(raw: object, *, field_name: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        msg = f"{field_name} must be a string or null"
        raise ChimeScheduleError(msg)
    normalized = raw.strip()
    if not normalized:
        msg = f"{field_name} must not be empty when provided"
        raise ChimeScheduleError(msg)
    return normalized


def _validate_target(chime: object, group_id: object) -> tuple[str | None, str | None]:
    normalized_chime = _optional_str(chime, field_name=_FIELD_CHIME)
    normalized_group_id = _optional_str(group_id, field_name=_FIELD_GROUP_ID)
    if (normalized_chime is None) == (normalized_group_id is None):
        raise ChimeScheduleError("Exactly one of chime or group_id must be provided")
    return normalized_chime, normalized_group_id


def _normalize_time_hhmm(raw: object) -> str:
    if not isinstance(raw, str):
        raise ChimeScheduleError("time_hhmm must be a string")
    stripped = raw.strip()
    if not _TIME_RE.fullmatch(stripped):
        raise ChimeScheduleError("time_hhmm must match HH:MM in 24-hour time")
    hour_text, minute_text = stripped.split(":", maxsplit=1)
    return f"{int(hour_text):02d}:{int(minute_text):02d}"


def _normalize_days(raw: object) -> tuple[int, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ChimeScheduleError("days must be a sequence of integers")
    normalized: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ChimeScheduleError("days must contain only integers")
        if item < _DOW_MIN or item > _DOW_MAX:
            msg = f"days must be between {_DOW_MIN} and {_DOW_MAX}"
            raise ChimeScheduleError(msg)
        if item not in normalized:
            normalized.append(item)
    if not normalized:
        raise ChimeScheduleError("days must not be empty")
    return tuple(sorted(normalized))


def _normalize_month(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ChimeScheduleError("month must be an integer")
    if raw < _MONTH_MIN or raw > _MONTH_MAX:
        raise ChimeScheduleError(f"month must be between {_MONTH_MIN} and {_MONTH_MAX}")
    return raw


def _normalize_day(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ChimeScheduleError("day must be an integer")
    if raw < _DAY_MIN or raw > _DAY_MAX:
        raise ChimeScheduleError(f"day must be between {_DAY_MIN} and {_DAY_MAX}")
    return raw


def _normalize_holiday_name(raw: object) -> str:
    if not isinstance(raw, str):
        raise ChimeScheduleError("holiday_name must be a string")
    normalized = raw.strip()
    if normalized not in _US_FIXED_HOLIDAYS and normalized not in _US_MOVABLE_HOLIDAYS:
        raise ChimeScheduleError(f"Unknown holiday: {normalized}")
    return normalized


def _normalize_interval(raw: object, *, intervals: dict[str, str]) -> str:
    if not isinstance(raw, str):
        raise ChimeScheduleError("interval must be a string")
    normalized = raw.strip()
    if normalized not in intervals:
        choices = ", ".join(sorted(intervals))
        raise ChimeScheduleError(f"interval must be one of: {choices}")
    return normalized


def _ensure_bool(raw: object, *, field_name: str) -> bool:
    if not isinstance(raw, bool):
        msg = f"{field_name} must be a boolean"
        raise ChimeScheduleError(msg)
    return raw


def _build_schedule(draft: _ScheduleDraft, *, intervals: dict[str, str]) -> Schedule:
    enabled = _ensure_bool(draft.enabled, field_name=_FIELD_ENABLED)
    chime, group_id = _validate_target(draft.chime, draft.group_id)
    if draft.schedule_type == _TYPE_WEEKLY:
        return WeeklySchedule(
            id=draft.schedule_id,
            days=_normalize_days(draft.days),
            time_hhmm=_normalize_time_hhmm(draft.time_hhmm),
            chime=chime,
            group_id=group_id,
            enabled=enabled,
            created_at=draft.created_at,
            updated_at=draft.updated_at,
            last_run=draft.last_run,
        )
    if draft.schedule_type == _TYPE_DATE:
        return DateSchedule(
            id=draft.schedule_id,
            month=_normalize_month(draft.month),
            day=_normalize_day(draft.day),
            time_hhmm=_normalize_time_hhmm(draft.time_hhmm),
            chime=chime,
            group_id=group_id,
            enabled=enabled,
            created_at=draft.created_at,
            updated_at=draft.updated_at,
            last_run=draft.last_run,
        )
    if draft.schedule_type == _TYPE_HOLIDAY:
        return HolidaySchedule(
            id=draft.schedule_id,
            holiday_name=_normalize_holiday_name(draft.holiday_name),
            time_hhmm=_normalize_time_hhmm(draft.time_hhmm),
            chime=chime,
            group_id=group_id,
            enabled=enabled,
            created_at=draft.created_at,
            updated_at=draft.updated_at,
            last_run=draft.last_run,
        )
    if draft.schedule_type == _TYPE_RECURRING:
        return RecurringSchedule(
            id=draft.schedule_id,
            interval=_normalize_interval(draft.interval, intervals=intervals),
            chime=chime,
            group_id=group_id,
            enabled=enabled,
            created_at=draft.created_at,
            updated_at=draft.updated_at,
            last_run=draft.last_run,
        )
    raise ChimeScheduleError(f"Unsupported schedule type: {draft.schedule_type}")


def _draft_from_schedule(schedule: Schedule) -> _ScheduleDraft:
    if isinstance(schedule, WeeklySchedule):
        return _ScheduleDraft(
            schedule_type=_TYPE_WEEKLY,
            schedule_id=schedule.id,
            created_at=schedule.created_at,
            updated_at=schedule.updated_at,
            last_run=schedule.last_run,
            enabled=schedule.enabled,
            chime=schedule.chime,
            group_id=schedule.group_id,
            days=schedule.days,
            time_hhmm=schedule.time_hhmm,
        )
    if isinstance(schedule, DateSchedule):
        return _ScheduleDraft(
            schedule_type=_TYPE_DATE,
            schedule_id=schedule.id,
            created_at=schedule.created_at,
            updated_at=schedule.updated_at,
            last_run=schedule.last_run,
            enabled=schedule.enabled,
            chime=schedule.chime,
            group_id=schedule.group_id,
            month=schedule.month,
            day=schedule.day,
            time_hhmm=schedule.time_hhmm,
        )
    if isinstance(schedule, HolidaySchedule):
        return _ScheduleDraft(
            schedule_type=_TYPE_HOLIDAY,
            schedule_id=schedule.id,
            created_at=schedule.created_at,
            updated_at=schedule.updated_at,
            last_run=schedule.last_run,
            enabled=schedule.enabled,
            chime=schedule.chime,
            group_id=schedule.group_id,
            holiday_name=schedule.holiday_name,
            time_hhmm=schedule.time_hhmm,
        )
    return _ScheduleDraft(
        schedule_type=_TYPE_RECURRING,
        schedule_id=schedule.id,
        created_at=schedule.created_at,
        updated_at=schedule.updated_at,
        last_run=schedule.last_run,
        enabled=schedule.enabled,
        chime=schedule.chime,
        group_id=schedule.group_id,
        interval=schedule.interval,
    )


def _occurrence_key(schedule: Schedule) -> tuple[object, ...]:
    if isinstance(schedule, WeeklySchedule):
        return (_TYPE_WEEKLY, schedule.days, schedule.time_hhmm)
    if isinstance(schedule, DateSchedule):
        return (_TYPE_DATE, schedule.month, schedule.day, schedule.time_hhmm)
    if isinstance(schedule, HolidaySchedule):
        return (_TYPE_HOLIDAY, schedule.holiday_name, schedule.time_hhmm)
    return (_TYPE_RECURRING, schedule.interval)


def _select_by_recency(schedules: Sequence[Schedule]) -> Schedule | None:
    return (
        None
        if not schedules
        else max(
            schedules,
            key=lambda item: (item.updated_at, item.created_at, item.id),
        )
    )
