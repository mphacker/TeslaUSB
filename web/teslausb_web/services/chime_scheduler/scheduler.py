"""B-1 service: scheduled chime selection and JSON persistence.

Priority order for ``get_active_chime_for_now`` is
``date > holiday > weekly > recurring``.

The service is thread-safe inside one Python process via ``threading.Lock``.
That lock does not coordinate across multiple Gunicorn workers or other
processes, so concurrent writers still require single-worker deployment or
external coordination.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Final

from teslausb_web.services.chime_scheduler.constants import (
    _DEFAULT_GROUPS_FILE_RELPATH,
    _FIELD_CHIME,
    _FIELD_DAY,
    _FIELD_DAYS,
    _FIELD_ENABLED,
    _FIELD_GROUP_ID,
    _FIELD_HOLIDAY_NAME,
    _FIELD_INTERVAL,
    _FIELD_MONTH,
    _FIELD_TYPE,
    _INTERVAL_TO_MINUTES,
    _MAX_SCHEDULES,
    _PRIORITY_ORDER,
    _REASON_NO_MATCH,
    _REASON_NO_SCHEDULES,
    _RECURRING_INTERVALS,
    _TYPE_DATE,
    _TYPE_HOLIDAY,
    _TYPE_RECURRING,
    _TYPE_WEEKLY,
)
from teslausb_web.services.chime_scheduler.holidays import (
    get_holidays_list,
    get_holidays_with_dates,
    holidays_for_date,
)
from teslausb_web.services.chime_scheduler.storage import (
    load_schedules,
    persist_schedules,
    resolve_group_choice,
)
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
    schedule_type_of,
)
from teslausb_web.services.chime_scheduler.validation import (
    _build_schedule,
    _draft_from_schedule,
    _ensure_aware_datetime,
    _normalize_schedule_id,
    _normalize_schedule_type,
    _occurrence_key,
    _operation_result,
    _ScheduleDraft,
    _select_by_recency,
    _utc_now,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

_ALLOWED_UPDATE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        _FIELD_TYPE,
        _FIELD_DAYS,
        "time_hhmm",
        _FIELD_CHIME,
        _FIELD_GROUP_ID,
        _FIELD_ENABLED,
        _FIELD_MONTH,
        _FIELD_DAY,
        _FIELD_HOLIDAY_NAME,
        _FIELD_INTERVAL,
    }
)
_ON_BOOT_INTERVAL: Final[str] = "on_boot"


class ChimeScheduler:
    """Manage chime schedules backed by one JSON file."""

    def __init__(self, schedule_file: Path) -> None:
        self._schedule_file = schedule_file
        self._groups_file = schedule_file.with_name(_DEFAULT_GROUPS_FILE_RELPATH)
        self._lock = threading.Lock()
        self._boot_pending = False
        self._schedules = self._load_schedules_or_empty()

    def list_schedules(self) -> tuple[Schedule, ...]:
        with self._lock:
            return self._schedules

    def get_schedule(self, schedule_id: str) -> Schedule | None:
        normalized_id = _normalize_schedule_id(schedule_id)
        with self._lock:
            return self._find_schedule(normalized_id)

    def add_weekly(
        self,
        days: Sequence[int],
        time_hhmm: str,
        chime: str | None = None,
        group_id: str | None = None,
    ) -> ScheduleOperationResult:
        timestamp = _utc_now()
        draft = _ScheduleDraft(
            schedule_type=_TYPE_WEEKLY,
            schedule_id=self._new_schedule_id(),
            created_at=timestamp,
            updated_at=timestamp,
            last_run=None,
            enabled=True,
            chime=chime,
            group_id=group_id,
            days=days,
            time_hhmm=time_hhmm,
        )
        return self._add_schedule(_build_schedule(draft, intervals=_RECURRING_INTERVALS))

    def add_date(
        self,
        month: int,
        day: int,
        time_hhmm: str,
        chime: str | None = None,
        group_id: str | None = None,
    ) -> ScheduleOperationResult:
        timestamp = _utc_now()
        draft = _ScheduleDraft(
            schedule_type=_TYPE_DATE,
            schedule_id=self._new_schedule_id(),
            created_at=timestamp,
            updated_at=timestamp,
            last_run=None,
            enabled=True,
            chime=chime,
            group_id=group_id,
            month=month,
            day=day,
            time_hhmm=time_hhmm,
        )
        return self._add_schedule(_build_schedule(draft, intervals=_RECURRING_INTERVALS))

    def add_holiday(
        self,
        holiday_name: str,
        time_hhmm: str,
        chime: str | None = None,
        group_id: str | None = None,
    ) -> ScheduleOperationResult:
        timestamp = _utc_now()
        draft = _ScheduleDraft(
            schedule_type=_TYPE_HOLIDAY,
            schedule_id=self._new_schedule_id(),
            created_at=timestamp,
            updated_at=timestamp,
            last_run=None,
            enabled=True,
            chime=chime,
            group_id=group_id,
            holiday_name=holiday_name,
            time_hhmm=time_hhmm,
        )
        return self._add_schedule(_build_schedule(draft, intervals=_RECURRING_INTERVALS))

    def add_recurring(
        self,
        interval: str,
        chime: str | None = None,
        group_id: str | None = None,
    ) -> ScheduleOperationResult:
        timestamp = _utc_now()
        draft = _ScheduleDraft(
            schedule_type=_TYPE_RECURRING,
            schedule_id=self._new_schedule_id(),
            created_at=timestamp,
            updated_at=timestamp,
            last_run=None,
            enabled=True,
            chime=chime,
            group_id=group_id,
            interval=interval,
        )
        return self._add_schedule(_build_schedule(draft, intervals=_RECURRING_INTERVALS))

    def update_schedule(self, schedule_id: str, **fields: object) -> ScheduleOperationResult:
        normalized_id = _normalize_schedule_id(schedule_id)
        unknown_fields = set(fields) - _ALLOWED_UPDATE_FIELDS
        if unknown_fields:
            unknown = ", ".join(sorted(unknown_fields))
            raise ChimeScheduleError(f"Unknown update field(s): {unknown}")
        with self._lock:
            existing = self._find_schedule(normalized_id)
            if existing is None:
                return _operation_result(
                    ok=False,
                    message=f"Schedule '{normalized_id}' not found",
                    schedule_id=None,
                )
            draft = _draft_from_schedule(existing)
            replacement = replace(
                draft,
                schedule_type=_normalize_schedule_type(
                    fields.get(_FIELD_TYPE, draft.schedule_type),
                ),
                updated_at=_utc_now(),
                days=fields.get(_FIELD_DAYS, draft.days),
                time_hhmm=fields.get("time_hhmm", draft.time_hhmm),
                chime=fields.get(_FIELD_CHIME, draft.chime),
                group_id=fields.get(_FIELD_GROUP_ID, draft.group_id),
                enabled=fields.get(_FIELD_ENABLED, draft.enabled),
                month=fields.get(_FIELD_MONTH, draft.month),
                day=fields.get(_FIELD_DAY, draft.day),
                holiday_name=fields.get(_FIELD_HOLIDAY_NAME, draft.holiday_name),
                interval=fields.get(_FIELD_INTERVAL, draft.interval),
            )
            updated = _build_schedule(replacement, intervals=_RECURRING_INTERVALS)
            if _occurrence_key(existing) != _occurrence_key(updated):
                updated = replace(updated, last_run=None)
            self._replace_schedule(updated)
            return _operation_result(
                ok=True,
                message=f"Schedule '{normalized_id}' updated",
                schedule_id=normalized_id,
            )

    def delete_schedule(self, schedule_id: str) -> ScheduleOperationResult:
        normalized_id = _normalize_schedule_id(schedule_id)
        with self._lock:
            if self._find_schedule(normalized_id) is None:
                return _operation_result(
                    ok=False,
                    message=f"Schedule '{normalized_id}' not found",
                    schedule_id=None,
                )
            new_schedules = tuple(
                schedule for schedule in self._schedules if schedule.id != normalized_id
            )
            persist_schedules(self._schedule_file, new_schedules)
            self._schedules = new_schedules
            return _operation_result(
                ok=True,
                message=f"Schedule '{normalized_id}' deleted",
                schedule_id=normalized_id,
            )

    def set_enabled(self, schedule_id: str, *, enabled: bool) -> ScheduleOperationResult:
        normalized_id = _normalize_schedule_id(schedule_id)
        if not isinstance(enabled, bool):
            raise ChimeScheduleError("enabled must be a boolean")
        with self._lock:
            schedule = self._find_schedule(normalized_id)
            if schedule is None:
                return _operation_result(
                    ok=False,
                    message=f"Schedule '{normalized_id}' not found",
                    schedule_id=None,
                )
            updated = replace(schedule, enabled=enabled, updated_at=_utc_now())
            self._replace_schedule(updated)
            return _operation_result(
                ok=True,
                message=f"Schedule '{normalized_id}' updated",
                schedule_id=normalized_id,
            )

    def mark_run(self, schedule_id: str, when: datetime) -> None:
        normalized_id = _normalize_schedule_id(schedule_id)
        when_utc = _ensure_aware_datetime(when, field_name="when")
        with self._lock:
            schedule = self._find_schedule(normalized_id)
            if schedule is None:
                raise ChimeScheduleError(f"Schedule '{normalized_id}' not found")
            updated = replace(schedule, last_run=when_utc, updated_at=_utc_now())
            self._replace_schedule(updated)

    def bootstrap_now(self, now: datetime | None = None) -> None:
        if now is not None:
            _ensure_aware_datetime(now, field_name="now")
        with self._lock:
            self._boot_pending = True

    def get_active_chime_for_now(self, now: datetime | None = None) -> ActiveChimeResolution:
        current = _utc_now() if now is None else _ensure_aware_datetime(now, field_name="now")
        with self._lock:
            if not self._schedules:
                return ActiveChimeResolution(None, None, None, _REASON_NO_SCHEDULES)
            matched = self._matching_schedules(current)
            for schedule_type in _PRIORITY_ORDER:
                candidate = _select_by_recency(matched.get(schedule_type, ()))
                if candidate is None:
                    continue
                resolved_chime = self._resolve_schedule_target(candidate)
                reason = f"Matched {schedule_type} schedule '{candidate.id}'"
                if resolved_chime is None:
                    return ActiveChimeResolution(
                        None,
                        candidate.id,
                        schedule_type,
                        f"{reason}, but target could not be resolved",
                    )
                if (
                    isinstance(candidate, RecurringSchedule)
                    and candidate.interval == _ON_BOOT_INTERVAL
                ):
                    self._boot_pending = False
                return ActiveChimeResolution(
                    resolved_chime,
                    candidate.id,
                    schedule_type,
                    reason,
                )
            return ActiveChimeResolution(None, None, None, _REASON_NO_MATCH)

    def get_holidays_list(self) -> tuple[str, ...]:
        return get_holidays_list()

    def get_holidays_with_dates(self, year: int) -> tuple[tuple[str, datetime], ...]:
        return get_holidays_with_dates(year)

    def get_recurring_intervals(self) -> tuple[tuple[str, str], ...]:
        return tuple(_RECURRING_INTERVALS.items())

    def _new_schedule_id(self) -> ScheduleId:
        return _normalize_schedule_id(str(uuid.uuid4()))

    def _find_schedule(self, schedule_id: ScheduleId) -> Schedule | None:
        for schedule in self._schedules:
            if schedule.id == schedule_id:
                return schedule
        return None

    def _add_schedule(self, schedule: Schedule) -> ScheduleOperationResult:
        with self._lock:
            if len(self._schedules) >= _MAX_SCHEDULES:
                msg = f"Cannot create more than {_MAX_SCHEDULES} schedules"
                raise ChimeScheduleError(msg)
            new_schedules = (*self._schedules, schedule)
            persist_schedules(self._schedule_file, new_schedules)
            self._schedules = new_schedules
            return _operation_result(
                ok=True,
                message=f"Schedule '{schedule.id}' created",
                schedule_id=schedule.id,
            )

    def _replace_schedule(self, updated: Schedule) -> None:
        new_schedules = tuple(
            updated if schedule.id == updated.id else schedule for schedule in self._schedules
        )
        persist_schedules(self._schedule_file, new_schedules)
        self._schedules = new_schedules

    def _matching_schedules(self, current: datetime) -> dict[str, tuple[Schedule, ...]]:
        matched: dict[str, list[Schedule]] = {key: [] for key in _PRIORITY_ORDER}
        for schedule in self._schedules:
            if not schedule.enabled:
                continue
            if isinstance(schedule, RecurringSchedule):
                if self._matches_recurring(schedule, current):
                    matched[_TYPE_RECURRING].append(schedule)
                continue
            if not self._matches_time(schedule, current):
                continue
            if self._already_ran_today(schedule, current.date()):
                continue
            if self._matches_calendar(schedule, current.date()):
                matched[schedule_type_of(schedule)].append(schedule)
        return {key: tuple(value) for key, value in matched.items()}

    def _matches_time(
        self,
        schedule: WeeklySchedule | DateSchedule | HolidaySchedule,
        current: datetime,
    ) -> bool:
        return schedule.time_hhmm == current.strftime("%H:%M")

    def _already_ran_today(self, schedule: Schedule, current_date: date) -> bool:
        last_run = schedule.last_run
        return last_run is not None and last_run.astimezone(UTC).date() == current_date

    def _matches_calendar(
        self,
        schedule: WeeklySchedule | DateSchedule | HolidaySchedule,
        current_date: date,
    ) -> bool:
        if isinstance(schedule, WeeklySchedule):
            return current_date.weekday() in schedule.days
        if isinstance(schedule, DateSchedule):
            try:
                target_date = date(current_date.year, schedule.month, schedule.day)
            except ValueError:
                return False
            return target_date == current_date
        return schedule.holiday_name in holidays_for_date(current_date)

    def _matches_recurring(self, schedule: RecurringSchedule, current: datetime) -> bool:
        if schedule.interval == _ON_BOOT_INTERVAL:
            return self._boot_pending
        if schedule.last_run is None:
            return True
        interval_minutes = _INTERVAL_TO_MINUTES[schedule.interval]
        return current - schedule.last_run >= timedelta(minutes=interval_minutes)

    def _resolve_schedule_target(self, schedule: Schedule) -> str | None:
        if schedule.chime is not None:
            return schedule.chime
        if schedule.group_id is None:
            return None
        try:
            return resolve_group_choice(self._groups_file, schedule.group_id)
        except ChimeScheduleStateError as exc:
            logger.warning("Failed to load chime groups from %s: %s", self._groups_file, exc)
            return None

    def _load_schedules_or_empty(self) -> tuple[Schedule, ...]:
        try:
            return load_schedules(self._schedule_file, intervals=_RECURRING_INTERVALS)
        except ChimeScheduleStateError as exc:
            logger.warning("Failed to load chime schedules from %s: %s", self._schedule_file, exc)
            return ()


def make_chime_scheduler(cfg: WebConfig) -> ChimeScheduler:
    scheduler = ChimeScheduler(cfg.paths.state_dir / cfg.chimes.schedules_file_relpath)
    scheduler._groups_file = cfg.paths.state_dir / cfg.chimes.groups_file_relpath
    return scheduler


__all__ = ("ChimeScheduler", "make_chime_scheduler")
