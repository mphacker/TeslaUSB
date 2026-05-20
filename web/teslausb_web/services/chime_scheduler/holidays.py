from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final

from teslausb_web.services.chime_scheduler.constants import (
    _DOW_MAX,
    _DOW_MIN,
    _US_FIXED_HOLIDAYS,
    _US_MOVABLE_HOLIDAYS,
)

_JANUARY: Final[int] = 1
_FEBRUARY: Final[int] = 2
_MAY: Final[int] = 5
_JUNE: Final[int] = 6
_SEPTEMBER: Final[int] = 9
_OCTOBER: Final[int] = 10
_NOVEMBER: Final[int] = 11
_DECEMBER: Final[int] = 12

_MONDAY: Final[int] = 0
_THURSDAY: Final[int] = 3
_SUNDAY: Final[int] = 6
_FIRST: Final[int] = 1
_SECOND: Final[int] = 2
_THIRD: Final[int] = 3
_FOURTH: Final[int] = 4
_LAST: Final[int] = -1


@dataclass(frozen=True, slots=True)
class _HolidayRule:
    month: int
    weekday: int
    ordinal: int


_MOVABLE_RULES: Final[dict[str, _HolidayRule]] = {
    "Martin Luther King Jr. Day": _HolidayRule(_JANUARY, _MONDAY, _THIRD),
    "Presidents' Day": _HolidayRule(_FEBRUARY, _MONDAY, _THIRD),
    "Mother's Day": _HolidayRule(_MAY, _SUNDAY, _SECOND),
    "Memorial Day": _HolidayRule(_MAY, _MONDAY, _LAST),
    "Father's Day": _HolidayRule(_JUNE, _SUNDAY, _THIRD),
    "Labor Day": _HolidayRule(_SEPTEMBER, _MONDAY, _FIRST),
    "Columbus Day": _HolidayRule(_OCTOBER, _MONDAY, _SECOND),
    "Thanksgiving": _HolidayRule(_NOVEMBER, _THURSDAY, _FOURTH),
}
_EASTER_NAME: Final[str] = "Easter"


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    if weekday < _DOW_MIN or weekday > _DOW_MAX:
        msg = f"weekday must be between {_DOW_MIN} and {_DOW_MAX}"
        raise ValueError(msg)
    first_day = date(year, month, 1)
    days_ahead = (weekday - first_day.weekday()) % (_DOW_MAX + 1)
    first_occurrence = first_day + timedelta(days=days_ahead)
    return first_occurrence + timedelta(weeks=n - 1)


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    if weekday < _DOW_MIN or weekday > _DOW_MAX:
        msg = f"weekday must be between {_DOW_MIN} and {_DOW_MAX}"
        raise ValueError(msg)
    last_day = (
        date(year, _DECEMBER, 31)
        if month == _DECEMBER
        else date(year, month + 1, 1) - timedelta(days=1)
    )
    days_back = (last_day.weekday() - weekday) % (_DOW_MAX + 1)
    return last_day - timedelta(days=days_back)


def _easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    weekday_adjustment = (32 + (2 * e) + (2 * i) - h - k) % 7
    month_adjustment = (a + (11 * h) + (22 * weekday_adjustment)) // 451
    month = (h + weekday_adjustment - (7 * month_adjustment) + 114) // 31
    day = ((h + weekday_adjustment - (7 * month_adjustment) + 114) % 31) + 1
    return date(year, month, day)


def compute_movable_holiday(year: int, holiday_name: str) -> date | None:
    if holiday_name == _EASTER_NAME:
        return _easter_sunday(year)
    rule = _MOVABLE_RULES.get(holiday_name)
    if rule is None:
        return None
    if rule.ordinal == _LAST:
        return _last_weekday_of_month(year, rule.month, rule.weekday)
    return _nth_weekday_of_month(year, rule.month, rule.weekday, rule.ordinal)


def get_holidays_list() -> tuple[str, ...]:
    return tuple(sorted((*_US_FIXED_HOLIDAYS.keys(), *_US_MOVABLE_HOLIDAYS)))


def get_holidays_with_dates(year: int) -> tuple[tuple[str, datetime], ...]:
    holidays: list[tuple[str, datetime]] = []
    for holiday_name, (month, day) in _US_FIXED_HOLIDAYS.items():
        holidays.append((holiday_name, datetime(year, month, day, tzinfo=UTC)))
    for holiday_name in _US_MOVABLE_HOLIDAYS:
        holiday_date = compute_movable_holiday(year, holiday_name)
        if holiday_date is None:
            continue
        holidays.append(
            (
                holiday_name,
                datetime(
                    holiday_date.year,
                    holiday_date.month,
                    holiday_date.day,
                    tzinfo=UTC,
                ),
            )
        )
    return tuple(sorted(holidays, key=lambda item: (item[1], item[0])))


def holidays_for_date(target_date: date) -> frozenset[str]:
    matches: set[str] = set()
    for holiday_name, (month, day) in _US_FIXED_HOLIDAYS.items():
        if target_date.month == month and target_date.day == day:
            matches.add(holiday_name)
    for holiday_name in _US_MOVABLE_HOLIDAYS:
        holiday_date = compute_movable_holiday(target_date.year, holiday_name)
        if holiday_date == target_date:
            matches.add(holiday_name)
    return frozenset(matches)
