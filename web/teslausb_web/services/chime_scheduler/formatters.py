from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from teslausb_web.services.chime_scheduler.constants import _DAYS_OF_WEEK, _RECURRING_INTERVALS
from teslausb_web.services.chime_scheduler.types import (
    DateSchedule,
    HolidaySchedule,
    Schedule,
    WeeklySchedule,
)

_HOURS_PER_DAY: Final[int] = 24
_HOURS_PER_HALF_DAY: Final[int] = 12
_MINUTES_PER_HOUR: Final[int] = 60
_SECONDS_PER_MINUTE: Final[int] = 60


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _format_time_hhmm(time_hhmm: str) -> str:
    hour_text, minute_text = time_hhmm.split(":", maxsplit=1)
    hour = int(hour_text)
    minute = int(minute_text)
    suffix = "AM" if hour < _HOURS_PER_HALF_DAY else "PM"
    display_hour = hour % _HOURS_PER_HALF_DAY
    if display_hour == 0:
        display_hour = _HOURS_PER_HALF_DAY
    return f"{display_hour}:{minute:02d} {suffix}"


def _target_label(schedule: Schedule) -> str:
    if schedule.chime is not None:
        return schedule.chime
    return "<unconfigured>" if schedule.group_id is None else f"group {schedule.group_id}"


def format_schedule_display(schedule: Schedule) -> str:
    target = _target_label(schedule)
    if isinstance(schedule, WeeklySchedule):
        day_names = ", ".join(_DAYS_OF_WEEK[day] for day in schedule.days)
        return f"{day_names} at {_format_time_hhmm(schedule.time_hhmm)} → {target}"
    if isinstance(schedule, DateSchedule):
        timing = f"{schedule.month}/{schedule.day} at {_format_time_hhmm(schedule.time_hhmm)}"
        return f"{timing} → {target}"
    if isinstance(schedule, HolidaySchedule):
        timing = f"{schedule.holiday_name} at {_format_time_hhmm(schedule.time_hhmm)}"
        return f"{timing} → {target}"
    interval_display = _RECURRING_INTERVALS.get(schedule.interval, schedule.interval)
    return f"{interval_display} → {target}"


def format_last_run(last_run: datetime | None) -> str:
    if last_run is None:
        return "Never"
    now = _utc_now()
    last_run_utc = (
        last_run.replace(tzinfo=UTC) if last_run.tzinfo is None else last_run.astimezone(UTC)
    )
    delta_seconds = (now - last_run_utc).total_seconds()
    if delta_seconds < _SECONDS_PER_MINUTE:
        return "Just now"
    minutes = int(delta_seconds // _SECONDS_PER_MINUTE)
    if minutes < _MINUTES_PER_HOUR:
        suffix = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {suffix} ago"
    hours = int(minutes // _MINUTES_PER_HOUR)
    if hours < _HOURS_PER_DAY:
        suffix = "hour" if hours == 1 else "hours"
        return f"{hours} {suffix} ago"
    days = int(hours // _HOURS_PER_DAY)
    suffix = "day" if days == 1 else "days"
    return f"{days} {suffix} ago"
