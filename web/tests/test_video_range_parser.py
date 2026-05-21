"""Tests for the pure HTTP Range parser (Phase 5.26)."""

from __future__ import annotations

import pytest
from teslausb_web.services.video_service import RangeParseError
from teslausb_web.services.video_service._range import parse_range


class TestParseRange:
    def test_no_header_returns_none(self) -> None:
        assert parse_range(None, 1000) is None

    def test_empty_header_raises(self) -> None:
        with pytest.raises(RangeParseError):
            parse_range("", 1000)

    def test_negative_file_size_raises(self) -> None:
        with pytest.raises(RangeParseError):
            parse_range("bytes=0-99", -1)

    def test_basic_range(self) -> None:
        rng = parse_range("bytes=0-99", 1000)
        assert rng is not None
        assert rng.start == 0
        assert rng.end == 99
        assert rng.length == 100
        assert rng.full_size == 1000

    def test_open_ended_range(self) -> None:
        rng = parse_range("bytes=500-", 1000)
        assert rng is not None
        assert rng.start == 500
        assert rng.end == 999

    def test_suffix_range(self) -> None:
        rng = parse_range("bytes=-100", 1000)
        assert rng is not None
        assert rng.start == 900
        assert rng.end == 999

    def test_suffix_larger_than_file(self) -> None:
        rng = parse_range("bytes=-5000", 1000)
        assert rng is not None
        assert rng.start == 0
        assert rng.end == 999

    def test_zero_suffix_raises(self) -> None:
        with pytest.raises(RangeParseError):
            parse_range("bytes=-0", 1000)

    def test_empty_suffix_raises(self) -> None:
        with pytest.raises(RangeParseError):
            parse_range("bytes=-", 1000)

    def test_missing_equals_raises(self) -> None:
        with pytest.raises(RangeParseError):
            parse_range("bytes 0-99", 1000)

    def test_unsupported_unit_raises(self) -> None:
        with pytest.raises(RangeParseError):
            parse_range("items=0-99", 1000)

    def test_multi_range_rejected(self) -> None:
        with pytest.raises(RangeParseError):
            parse_range("bytes=0-99,200-299", 1000)

    def test_missing_dash_raises(self) -> None:
        with pytest.raises(RangeParseError):
            parse_range("bytes=100", 1000)

    def test_non_integer_bound_raises(self) -> None:
        with pytest.raises(RangeParseError):
            parse_range("bytes=a-b", 1000)

    def test_end_past_file_raises(self) -> None:
        with pytest.raises(RangeParseError):
            parse_range("bytes=0-2000", 1000)

    def test_end_before_start_raises(self) -> None:
        with pytest.raises(RangeParseError):
            parse_range("bytes=500-100", 1000)

    def test_empty_file_raises(self) -> None:
        with pytest.raises(RangeParseError):
            parse_range("bytes=0-0", 0)
