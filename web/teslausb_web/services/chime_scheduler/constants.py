from __future__ import annotations

from typing import Final

_DAYS_OF_WEEK: Final[tuple[str, ...]] = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_RECURRING_INTERVALS: Final[dict[str, str]] = {
    "on_boot": "On every boot/startup",
    "15min": "Every 15 minutes",
    "30min": "Every 30 minutes",
    "1hour": "Every hour",
    "2hour": "Every 2 hours",
    "4hour": "Every 4 hours",
    "6hour": "Every 6 hours",
    "12hour": "Every 12 hours",
}
_INTERVAL_TO_MINUTES: Final[dict[str, int]] = {
    "15min": 15,
    "30min": 30,
    "1hour": 60,
    "2hour": 120,
    "4hour": 240,
    "6hour": 360,
    "12hour": 720,
}
_US_FIXED_HOLIDAYS: Final[dict[str, tuple[int, int]]] = {
    "New Year's Day": (1, 1),
    "Valentine's Day": (2, 14),
    "St. Patrick's Day": (3, 17),
    "Independence Day": (7, 4),
    "Halloween": (10, 31),
    "Veterans Day": (11, 11),
    "Christmas Eve": (12, 24),
    "Christmas Day": (12, 25),
    "New Year's Eve": (12, 31),
}
_US_MOVABLE_HOLIDAYS: Final[frozenset[str]] = frozenset(
    {
        "Martin Luther King Jr. Day",
        "Presidents' Day",
        "Easter",
        "Mother's Day",
        "Memorial Day",
        "Father's Day",
        "Labor Day",
        "Columbus Day",
        "Thanksgiving",
    }
)
_TIME_FMT_PATTERN: Final[str] = r"^([01]?\d|2[0-3]):[0-5]\d$"
_MAX_SCHEDULES: Final[int] = 100
_SCHEDULE_NAME_MAX_LEN: Final[int] = 100
_DOW_MIN: Final[int] = 0
_DOW_MAX: Final[int] = 6

_JSON_ENCODING: Final[str] = "utf-8"
_JSON_INDENT: Final[int] = 2
_TMP_SUFFIX: Final[str] = ".tmp"
_ISO_FMT: Final[str] = "%Y-%m-%dT%H:%M:%S%z"
_SCHEMA_VERSION_KEY: Final[str] = "version"
_SCHEDULES_KEY: Final[str] = "schedules"
_SCHEDULES_SCHEMA_VERSION: Final[int] = 1
_DEFAULT_GROUPS_FILE_RELPATH: Final[str] = "chime_groups.json"

_TYPE_WEEKLY: Final[str] = "weekly"
_TYPE_DATE: Final[str] = "date"
_TYPE_HOLIDAY: Final[str] = "holiday"
_TYPE_RECURRING: Final[str] = "recurring"

_FIELD_ID: Final[str] = "id"
_FIELD_TYPE: Final[str] = "type"
_FIELD_DAYS: Final[str] = "days"
_FIELD_TIME: Final[str] = "time"
_FIELD_CHIME: Final[str] = "chime"
_FIELD_GROUP_ID: Final[str] = "group_id"
_FIELD_ENABLED: Final[str] = "enabled"
_FIELD_CREATED_AT: Final[str] = "created_at"
_FIELD_UPDATED_AT: Final[str] = "updated_at"
_FIELD_LAST_RUN: Final[str] = "last_run"
_FIELD_MONTH: Final[str] = "month"
_FIELD_DAY: Final[str] = "day"
_FIELD_HOLIDAY_NAME: Final[str] = "holiday_name"
_FIELD_INTERVAL: Final[str] = "interval"

_PRIORITY_ORDER: Final[tuple[str, ...]] = (
    _TYPE_DATE,
    _TYPE_HOLIDAY,
    _TYPE_WEEKLY,
    _TYPE_RECURRING,
)

_TARGET_KIND_CHIME: Final[str] = "chime"
_TARGET_KIND_GROUP: Final[str] = "group"

_REASON_NO_MATCH: Final[str] = "No schedule matched the current moment"
_REASON_NO_SCHEDULES: Final[str] = "No schedules are configured"
_REASON_ON_BOOT_PENDING: Final[str] = "On-boot schedule is pending for this bootstrap"
