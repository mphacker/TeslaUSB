"""Public chime-scheduler service API.

When multiple schedules match the same moment, precedence is:
``date > holiday > weekly > recurring``.
"""

from __future__ import annotations

from teslausb_web.services.chime_scheduler.constants import (
    _DAYS_OF_WEEK,
    _DOW_MAX,
    _DOW_MIN,
    _INTERVAL_TO_MINUTES,
    _MAX_SCHEDULES,
    _RECURRING_INTERVALS,
    _SCHEDULE_NAME_MAX_LEN,
    _TIME_FMT_PATTERN,
    _US_FIXED_HOLIDAYS,
    _US_MOVABLE_HOLIDAYS,
)
from teslausb_web.services.chime_scheduler.formatters import (
    format_last_run,
    format_schedule_display,
)
from teslausb_web.services.chime_scheduler.holidays import (
    _last_weekday_of_month,
    _nth_weekday_of_month,
    compute_movable_holiday,
)
from teslausb_web.services.chime_scheduler.scheduler import ChimeScheduler, make_chime_scheduler
from teslausb_web.services.chime_scheduler.types import (
    ActiveChimeResolution,
    ChimeScheduleError,
    ChimeScheduleStateError,
    DateSchedule,
    HolidaySchedule,
    RecurringSchedule,
    Schedule,
    ScheduleId,
    ScheduleOperationResult,
    WeeklySchedule,
)

__all__ = (
    "_DAYS_OF_WEEK",
    "_DOW_MAX",
    "_DOW_MIN",
    "_INTERVAL_TO_MINUTES",
    "_MAX_SCHEDULES",
    "_RECURRING_INTERVALS",
    "_SCHEDULE_NAME_MAX_LEN",
    "_TIME_FMT_PATTERN",
    "_US_FIXED_HOLIDAYS",
    "_US_MOVABLE_HOLIDAYS",
    "ActiveChimeResolution",
    "ChimeScheduleError",
    "ChimeScheduleStateError",
    "ChimeScheduler",
    "DateSchedule",
    "HolidaySchedule",
    "RecurringSchedule",
    "Schedule",
    "ScheduleId",
    "ScheduleOperationResult",
    "WeeklySchedule",
    "_last_weekday_of_month",
    "_nth_weekday_of_month",
    "compute_movable_holiday",
    "format_last_run",
    "format_schedule_display",
    "make_chime_scheduler",
)
