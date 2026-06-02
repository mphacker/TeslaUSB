"""Unit tests for the timezone day-bucketing helpers (ADR-0025).

The maps page buckets trips and events by the operator's local calendar
day, not by UTC. These tests pin the day-boundary math — DST-correct
bounds, local-day derivation, and the validate-or-fall-back-to-UTC
contract — in one place so a regression can never silently re-introduce
the UTC-bucketing bug.
"""

from __future__ import annotations

from datetime import UTC, datetime

from teslausb_web.services.mapping_tz import (
    UTC_TZ_NAME,
    day_bounds_utc,
    local_date_of,
    local_date_of_iso,
    normalize_tz,
)

_DETROIT = "America/Detroit"


def _epoch(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp())


class TestNormalizeTz:
    def test_valid_zone_passes_through(self) -> None:
        assert normalize_tz(_DETROIT) == _DETROIT

    def test_blank_and_none_become_utc(self) -> None:
        assert normalize_tz("") == UTC_TZ_NAME
        assert normalize_tz(None) == UTC_TZ_NAME

    def test_unknown_zone_falls_back_to_utc(self) -> None:
        assert normalize_tz("Mars/Olympus_Mons") == UTC_TZ_NAME


class TestLocalDateOf:
    def test_evening_drive_buckets_to_local_day(self) -> None:
        # 2024-06-02 00:10 UTC is 2024-06-01 20:10 EDT — the operator's
        # June-1 evening drive must file under June 1, not June 2.
        epoch = _epoch(2024, 6, 2, 0, 10)
        assert local_date_of(epoch, _DETROIT) == "2024-06-01"
        assert local_date_of(epoch, UTC_TZ_NAME) == "2024-06-02"

    def test_iso_variant_matches_epoch_variant(self) -> None:
        iso = "2024-06-02T00:10:00+00:00"
        assert local_date_of_iso(iso, _DETROIT) == "2024-06-01"
        assert local_date_of_iso(iso, UTC_TZ_NAME) == "2024-06-02"


class TestDayBoundsUtc:
    def test_bounds_span_a_standard_day(self) -> None:
        start, end = day_bounds_utc("2024-06-01", _DETROIT)
        # Local midnight June 1 EDT = 04:00 UTC; next local midnight = 24h later.
        assert start == _epoch(2024, 6, 1, 4, 0)
        assert end == _epoch(2024, 6, 2, 4, 0)
        assert end - start == 24 * 3600

    def test_evening_event_falls_inside_local_day_bounds(self) -> None:
        start, end = day_bounds_utc("2024-06-01", _DETROIT)
        assert start <= _epoch(2024, 6, 2, 0, 10) < end

    def test_spring_forward_day_is_23_hours(self) -> None:
        # 2024-03-10: US DST spring-forward — the local day is 23h long.
        start, end = day_bounds_utc("2024-03-10", _DETROIT)
        assert end - start == 23 * 3600

    def test_fall_back_day_is_25_hours(self) -> None:
        # 2024-11-03: US DST fall-back — the local day is 25h long.
        start, end = day_bounds_utc("2024-11-03", _DETROIT)
        assert end - start == 25 * 3600

    def test_utc_day_is_exactly_86400(self) -> None:
        start, end = day_bounds_utc("2024-06-01", UTC_TZ_NAME)
        assert start == _epoch(2024, 6, 1, 0, 0)
        assert end - start == 86400
