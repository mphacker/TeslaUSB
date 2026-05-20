"""Tests for ``teslausb_web.services.chime_scheduler_service``."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from teslausb_web.config import ChimesSection, PathsSection, WebConfig
from teslausb_web.services.chime_scheduler_service import (
    ChimeScheduleError,
    ChimeScheduler,
    DateSchedule,
    HolidaySchedule,
    RecurringSchedule,
    WeeklySchedule,
    compute_movable_holiday,
    format_last_run,
    format_schedule_display,
    make_chime_scheduler,
)


@pytest.fixture
def schedule_file(tmp_path: Path) -> Path:
    return tmp_path / "chime_schedules.json"


@pytest.fixture
def scheduler(schedule_file: Path) -> ChimeScheduler:
    return ChimeScheduler(schedule_file)


def _dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _group_file_for(schedule_file: Path) -> Path:
    return schedule_file.with_name("chime_groups.json")


def _write_group_file(schedule_file: Path, group_id: str, filenames: tuple[str, ...]) -> None:
    payload = {
        group_id: {
            "name": "Group",
            "chime_filenames": list(filenames),
            "created_at": "2024-01-01T00:00:00+0000",
            "updated_at": "2024-01-01T00:00:00+0000",
        }
    }
    _group_file_for(schedule_file).write_text(json.dumps(payload), encoding="utf-8")


def test_list_schedules_is_empty_when_file_missing(scheduler: ChimeScheduler) -> None:
    assert scheduler.list_schedules() == ()


def test_add_weekly_returns_persisted_schedule(scheduler: ChimeScheduler) -> None:
    result = scheduler.add_weekly((0, 2), "9:05", chime="weekday.wav")
    assert result.ok is True
    assert result.schedule_id is not None
    schedule = scheduler.get_schedule(result.schedule_id)
    assert isinstance(schedule, WeeklySchedule)
    assert schedule.days == (0, 2)
    assert schedule.time_hhmm == "09:05"
    assert schedule.chime == "weekday.wav"
    assert schedule.updated_at == schedule.created_at


def test_add_date_returns_persisted_schedule(scheduler: ChimeScheduler) -> None:
    result = scheduler.add_date(12, 25, "09:00", chime="christmas.wav")
    assert result.ok is True
    schedule = scheduler.get_schedule(result.schedule_id or "")
    assert isinstance(schedule, DateSchedule)
    assert schedule.month == 12
    assert schedule.day == 25


def test_add_holiday_returns_persisted_schedule(scheduler: ChimeScheduler) -> None:
    result = scheduler.add_holiday("Christmas Day", "09:00", chime="holiday.wav")
    assert result.ok is True
    schedule = scheduler.get_schedule(result.schedule_id or "")
    assert isinstance(schedule, HolidaySchedule)
    assert schedule.holiday_name == "Christmas Day"


def test_add_recurring_returns_persisted_schedule(scheduler: ChimeScheduler) -> None:
    result = scheduler.add_recurring("1hour", chime="rotate.wav")
    assert result.ok is True
    schedule = scheduler.get_schedule(result.schedule_id or "")
    assert isinstance(schedule, RecurringSchedule)
    assert schedule.interval == "1hour"


def test_update_schedule_changes_fields_and_clears_last_run(scheduler: ChimeScheduler) -> None:
    result = scheduler.add_weekly((0,), "09:00", chime="old.wav")
    schedule_id = result.schedule_id or ""
    scheduler.mark_run(schedule_id, _dt(2024, 1, 1, 9, 0))
    update = scheduler.update_schedule(schedule_id, time_hhmm="10:30", chime="new.wav")
    assert update.ok is True
    updated = scheduler.get_schedule(schedule_id)
    assert isinstance(updated, WeeklySchedule)
    assert updated.time_hhmm == "10:30"
    assert updated.chime == "new.wav"
    assert updated.last_run is None


def test_delete_schedule_removes_saved_schedule(scheduler: ChimeScheduler) -> None:
    schedule_id = scheduler.add_date(1, 2, "09:00", chime="date.wav").schedule_id or ""
    result = scheduler.delete_schedule(schedule_id)
    assert result.ok is True
    assert scheduler.get_schedule(schedule_id) is None


def test_set_enabled_updates_flag(scheduler: ChimeScheduler) -> None:
    schedule_id = scheduler.add_holiday("Christmas Day", "09:00", chime="x.wav").schedule_id or ""
    result = scheduler.set_enabled(schedule_id, enabled=False)
    assert result.ok is True
    schedule = scheduler.get_schedule(schedule_id)
    assert isinstance(schedule, HolidaySchedule)
    assert schedule.enabled is False


def test_mark_run_updates_last_run_and_persists(
    schedule_file: Path,
    scheduler: ChimeScheduler,
) -> None:
    schedule_id = scheduler.add_recurring("1hour", chime="hourly.wav").schedule_id or ""
    when = _dt(2024, 2, 3, 4, 5)
    scheduler.mark_run(schedule_id, when)
    reloaded = ChimeScheduler(schedule_file)
    schedule = reloaded.get_schedule(schedule_id)
    assert isinstance(schedule, RecurringSchedule)
    assert schedule.last_run == when


def test_get_active_chime_for_now_returns_none_when_empty(scheduler: ChimeScheduler) -> None:
    resolution = scheduler.get_active_chime_for_now(_dt(2024, 1, 1, 9, 0))
    assert resolution.chime_filename is None
    assert resolution.reason == "No schedules are configured"


def test_weekly_schedule_matches_exact_minute_only(scheduler: ChimeScheduler) -> None:
    scheduler.add_weekly((0,), "09:00", chime="monday.wav")
    assert scheduler.get_active_chime_for_now(_dt(2024, 1, 1, 9, 0)).chime_filename == "monday.wav"
    assert scheduler.get_active_chime_for_now(_dt(2024, 1, 1, 9, 1)).chime_filename is None


def test_date_priority_beats_weekly_for_same_moment(scheduler: ChimeScheduler) -> None:
    scheduler.add_weekly((6,), "09:00", chime="weekly.wav")
    scheduler.add_date(12, 25, "09:00", chime="date.wav")
    resolution = scheduler.get_active_chime_for_now(_dt(2022, 12, 25, 9, 0))
    assert resolution.chime_filename == "date.wav"
    assert resolution.source_type == "date"


def test_holiday_schedule_matches_fixed_holiday(scheduler: ChimeScheduler) -> None:
    scheduler.add_holiday("Christmas Day", "09:00", chime="christmas.wav")
    resolution = scheduler.get_active_chime_for_now(_dt(2024, 12, 25, 9, 0))
    assert resolution.chime_filename == "christmas.wav"
    assert resolution.source_type == "holiday"


@pytest.mark.parametrize(
    ("year", "expected_day"),
    [(2024, 27), (2025, 26)],
)
def test_movable_holiday_memorial_day_matches(
    year: int,
    expected_day: int,
    scheduler: ChimeScheduler,
) -> None:
    scheduler.add_holiday("Memorial Day", "09:00", chime="memorial.wav")
    resolution = scheduler.get_active_chime_for_now(_dt(year, 5, expected_day, 9, 0))
    assert resolution.chime_filename == "memorial.wav"


def test_recurring_hourly_fires_immediately_when_never_run(scheduler: ChimeScheduler) -> None:
    scheduler.add_recurring("1hour", chime="hourly.wav")
    assert scheduler.get_active_chime_for_now(_dt(2024, 1, 1, 0, 0)).chime_filename == "hourly.wav"


def test_recurring_hourly_waits_for_interval_after_last_run(scheduler: ChimeScheduler) -> None:
    schedule_id = scheduler.add_recurring("1hour", chime="hourly.wav").schedule_id or ""
    scheduler.mark_run(schedule_id, _dt(2024, 1, 1, 0, 0))
    assert scheduler.get_active_chime_for_now(_dt(2024, 1, 1, 0, 59)).chime_filename is None
    assert scheduler.get_active_chime_for_now(_dt(2024, 1, 1, 1, 0)).chime_filename == "hourly.wav"


def test_recurring_on_boot_only_fires_after_bootstrap(scheduler: ChimeScheduler) -> None:
    scheduler.add_recurring("on_boot", chime="boot.wav")
    assert scheduler.get_active_chime_for_now(_dt(2024, 1, 1, 0, 0)).chime_filename is None
    scheduler.bootstrap_now(_dt(2024, 1, 1, 0, 0))
    first = scheduler.get_active_chime_for_now(_dt(2024, 1, 1, 0, 0))
    second = scheduler.get_active_chime_for_now(_dt(2024, 1, 1, 0, 1))
    assert first.chime_filename == "boot.wav"
    assert second.chime_filename is None


def test_disabled_schedule_never_fires(scheduler: ChimeScheduler) -> None:
    schedule_id = scheduler.add_weekly((0,), "09:00", chime="off.wav").schedule_id or ""
    scheduler.set_enabled(schedule_id, enabled=False)
    assert scheduler.get_active_chime_for_now(_dt(2024, 1, 1, 9, 0)).chime_filename is None


def test_group_schedule_resolves_random_member(
    schedule_file: Path,
    scheduler: ChimeScheduler,
) -> None:
    _write_group_file(schedule_file, "holiday", ("a.wav", "b.wav"))
    scheduler.add_weekly((0,), "09:00", group_id="holiday")
    resolution = scheduler.get_active_chime_for_now(_dt(2024, 1, 1, 9, 0))
    assert resolution.chime_filename in {"a.wav", "b.wav"}
    assert resolution.source_schedule_id is not None


def test_invalid_date_schedule_is_skipped_at_runtime(scheduler: ChimeScheduler) -> None:
    scheduler.add_date(2, 30, "09:00", chime="bad-date.wav")
    assert scheduler.get_active_chime_for_now(_dt(2024, 2, 29, 9, 0)).chime_filename is None


def test_get_holidays_list_is_sorted_and_complete(scheduler: ChimeScheduler) -> None:
    holidays = scheduler.get_holidays_list()
    assert holidays == tuple(sorted(holidays))
    assert "Christmas Day" in holidays
    assert "Memorial Day" in holidays


def test_get_holidays_with_dates_returns_fixed_and_movable_dates(scheduler: ChimeScheduler) -> None:
    holidays = dict(scheduler.get_holidays_with_dates(2024))
    assert holidays["Christmas Day"] == _dt(2024, 12, 25, 0, 0)
    assert holidays["Memorial Day"] == _dt(2024, 5, 27, 0, 0)


def test_get_recurring_intervals_returns_pairs(scheduler: ChimeScheduler) -> None:
    intervals = dict(scheduler.get_recurring_intervals())
    assert intervals["on_boot"].startswith("On every boot")
    assert intervals["1hour"] == "Every hour"


def test_format_schedule_display_includes_weekly_fields(scheduler: ChimeScheduler) -> None:
    schedule_id = scheduler.add_weekly((0, 2), "09:00", chime="weekly.wav").schedule_id or ""
    display = format_schedule_display(scheduler.get_schedule(schedule_id) or pytest.fail("missing"))
    assert "Monday" in display
    assert "Wednesday" in display
    assert "weekly.wav" in display


def test_format_schedule_display_includes_date_fields(scheduler: ChimeScheduler) -> None:
    schedule_id = scheduler.add_date(12, 25, "09:00", chime="date.wav").schedule_id or ""
    display = format_schedule_display(scheduler.get_schedule(schedule_id) or pytest.fail("missing"))
    assert "12/25" in display
    assert "date.wav" in display


def test_format_schedule_display_includes_holiday_fields(scheduler: ChimeScheduler) -> None:
    schedule_id = (
        scheduler.add_holiday("Christmas Day", "09:00", chime="holiday.wav").schedule_id or ""
    )
    display = format_schedule_display(scheduler.get_schedule(schedule_id) or pytest.fail("missing"))
    assert "Christmas Day" in display
    assert "holiday.wav" in display


def test_format_schedule_display_includes_recurring_fields(scheduler: ChimeScheduler) -> None:
    schedule_id = scheduler.add_recurring("1hour", chime="repeat.wav").schedule_id or ""
    display = format_schedule_display(scheduler.get_schedule(schedule_id) or pytest.fail("missing"))
    assert "Every hour" in display
    assert "repeat.wav" in display


def test_format_last_run_none_is_never() -> None:
    assert format_last_run(None) == "Never"


def test_format_last_run_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "teslausb_web.services.chime_scheduler.formatters._utc_now",
        lambda: _dt(2024, 1, 1, 12, 0),
    )
    assert format_last_run(_dt(2024, 1, 1, 11, 50)) == "10 minutes ago"


def test_format_last_run_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "teslausb_web.services.chime_scheduler.formatters._utc_now",
        lambda: _dt(2024, 1, 1, 12, 0),
    )
    assert format_last_run(_dt(2024, 1, 1, 9, 0)) == "3 hours ago"


def test_format_last_run_days(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "teslausb_web.services.chime_scheduler.formatters._utc_now",
        lambda: _dt(2024, 1, 10, 12, 0),
    )
    assert format_last_run(_dt(2024, 1, 7, 12, 0)) == "3 days ago"


def test_atomic_persistence_round_trip(schedule_file: Path, scheduler: ChimeScheduler) -> None:
    scheduler.add_weekly((0,), "09:00", chime="persist.wav")
    payload = json.loads(schedule_file.read_text(encoding="utf-8"))
    reloaded = ChimeScheduler(schedule_file)
    assert payload["version"] == 1
    assert len(payload["schedules"]) == 1
    assert len(reloaded.list_schedules()) == 1


def test_corrupt_json_logs_and_returns_empty(
    schedule_file: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    schedule_file.write_text("{not-json", encoding="utf-8")
    caplog.set_level("WARNING")
    scheduler = ChimeScheduler(schedule_file)
    assert scheduler.list_schedules() == ()
    assert "Failed to load chime schedules" in caplog.text


def test_future_schema_version_logs_and_returns_empty(
    schedule_file: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    schedule_file.write_text(json.dumps({"version": 2, "schedules": []}), encoding="utf-8")
    caplog.set_level("WARNING")
    scheduler = ChimeScheduler(schedule_file)
    assert scheduler.list_schedules() == ()
    assert "Unsupported chime schedule schema version" in caplog.text


def test_concurrent_add_weekly_keeps_all_schedules(scheduler: ChimeScheduler) -> None:
    def _add(index: int) -> None:
        scheduler.add_weekly((index % 7,), f"{index:02d}:00", chime=f"{index}.wav")

    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(_add, range(10)))
    assert len(scheduler.list_schedules()) == 10


def test_factory_uses_configured_state_paths(tmp_path: Path) -> None:
    cfg = WebConfig(
        paths=PathsSection(state_dir=tmp_path, backing_root=Path("/srv/teslausb")),
        chimes=ChimesSection(
            groups_file_relpath="groups.json",
            random_config_relpath="random.json",
            schedules_file_relpath="schedules.json",
        ),
    )
    scheduler = make_chime_scheduler(cfg)
    result = scheduler.add_weekly((0,), "09:00", chime="factory.wav")
    assert result.ok is True
    assert (tmp_path / "schedules.json").exists()


@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("add_weekly", ((0,), "25:00")),
        ("add_date", (1, 1, "25:00")),
        ("add_holiday", ("Christmas Day", "25:00")),
    ],
)
def test_bad_time_format_is_rejected(
    scheduler: ChimeScheduler,
    method_name: str,
    args: tuple[object, ...],
) -> None:
    method = getattr(scheduler, method_name)
    with pytest.raises(ChimeScheduleError, match="HH:MM"):
        method(*args, chime="bad.wav")


def test_bad_day_range_is_rejected(scheduler: ChimeScheduler) -> None:
    with pytest.raises(ChimeScheduleError, match="between 0 and 6"):
        scheduler.add_weekly((7,), "09:00", chime="bad.wav")


@pytest.mark.parametrize(("month", "day"), [(0, 1), (13, 1), (1, 0), (1, 32)])
def test_bad_month_day_ranges_are_rejected(
    scheduler: ChimeScheduler,
    month: int,
    day: int,
) -> None:
    with pytest.raises(ChimeScheduleError):
        scheduler.add_date(month, day, "09:00", chime="bad.wav")


def test_unknown_holiday_is_rejected(scheduler: ChimeScheduler) -> None:
    with pytest.raises(ChimeScheduleError, match="Unknown holiday"):
        scheduler.add_holiday("Not Real", "09:00", chime="bad.wav")


def test_unknown_interval_is_rejected(scheduler: ChimeScheduler) -> None:
    with pytest.raises(ChimeScheduleError, match="interval must be one of"):
        scheduler.add_recurring("99hour", chime="bad.wav")


@pytest.mark.parametrize(
    ("chime", "group_id"),
    [("x.wav", "group"), (None, None)],
)
def test_target_xor_validation_is_enforced(
    scheduler: ChimeScheduler,
    chime: str | None,
    group_id: str | None,
) -> None:
    with pytest.raises(ChimeScheduleError, match="Exactly one of chime or group_id"):
        scheduler.add_weekly((0,), "09:00", chime=chime, group_id=group_id)


def test_mark_run_requires_aware_datetime(scheduler: ChimeScheduler) -> None:
    schedule_id = scheduler.add_weekly((0,), "09:00", chime="aware.wav").schedule_id or ""
    with pytest.raises(ChimeScheduleError, match="timezone-aware"):
        scheduler.mark_run(schedule_id, _dt(2024, 1, 1, 9, 0).replace(tzinfo=None))


def test_get_active_requires_aware_datetime(scheduler: ChimeScheduler) -> None:
    with pytest.raises(ChimeScheduleError, match="timezone-aware"):
        scheduler.get_active_chime_for_now(_dt(2024, 1, 1, 9, 0).replace(tzinfo=None))


def test_compute_movable_holiday_returns_expected_date() -> None:
    assert compute_movable_holiday(2024, "Memorial Day") == _dt(2024, 5, 27, 0, 0).date()
    assert compute_movable_holiday(2024, "Not Real") is None
