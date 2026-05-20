from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias

from teslausb_web.services.chime_scheduler.constants import (
    _TYPE_DATE,
    _TYPE_HOLIDAY,
    _TYPE_RECURRING,
    _TYPE_WEEKLY,
)

if TYPE_CHECKING:
    from datetime import datetime

ScheduleId: TypeAlias = str


class ChimeScheduleError(ValueError):
    """The requested schedule operation is invalid."""


class ChimeScheduleStateError(OSError):
    """The persisted schedule JSON could not be read or written."""


@dataclass(frozen=True, slots=True)
class WeeklySchedule:
    id: ScheduleId
    days: tuple[int, ...]
    time_hhmm: str
    chime: str | None
    group_id: str | None
    enabled: bool
    created_at: datetime
    updated_at: datetime
    last_run: datetime | None


@dataclass(frozen=True, slots=True)
class DateSchedule:
    id: ScheduleId
    month: int
    day: int
    time_hhmm: str
    chime: str | None
    group_id: str | None
    enabled: bool
    created_at: datetime
    updated_at: datetime
    last_run: datetime | None


@dataclass(frozen=True, slots=True)
class HolidaySchedule:
    id: ScheduleId
    holiday_name: str
    time_hhmm: str
    chime: str | None
    group_id: str | None
    enabled: bool
    created_at: datetime
    updated_at: datetime
    last_run: datetime | None


@dataclass(frozen=True, slots=True)
class RecurringSchedule:
    id: ScheduleId
    interval: str
    chime: str | None
    group_id: str | None
    enabled: bool
    created_at: datetime
    updated_at: datetime
    last_run: datetime | None


Schedule: TypeAlias = WeeklySchedule | DateSchedule | HolidaySchedule | RecurringSchedule


@dataclass(frozen=True, slots=True)
class ScheduleOperationResult:
    ok: bool
    message: str
    schedule_id: ScheduleId | None


@dataclass(frozen=True, slots=True)
class ActiveChimeResolution:
    chime_filename: str | None
    source_schedule_id: ScheduleId | None
    source_type: str | None
    reason: str


def schedule_type_of(schedule: Schedule) -> str:
    if isinstance(schedule, WeeklySchedule):
        return _TYPE_WEEKLY
    if isinstance(schedule, DateSchedule):
        return _TYPE_DATE
    if isinstance(schedule, HolidaySchedule):
        return _TYPE_HOLIDAY
    return _TYPE_RECURRING
