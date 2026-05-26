"""Tests for `MappingSettingsService`.

Covers the I/O / CPU minimization contract:
* default snapshot when the file is missing
* save round-trip (atomic write + cache update)
* mtime-based cache: no re-read while the file is unchanged
* JSON parse only after the file actually changes
* validation rejects out-of-range values
* schema_version mismatch is an error
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from teslausb_web.services.mapping_settings_service import (
    MappingSettings,
    MappingSettingsError,
    MappingSettingsService,
    make_mapping_settings_service,
)


@pytest.fixture
def overrides_path(tmp_path: Path) -> Path:
    return tmp_path / "mapping_settings.json"


def test_missing_file_returns_defaults(overrides_path: Path) -> None:
    svc = MappingSettingsService(overrides_path)
    snap = svc.get_settings()
    assert snap.trip_gap_minutes == 5
    assert snap.speed_limit_mph == 0
    assert snap.speed_limit_mps == 0.0
    assert snap.speed_limit_enabled is False
    assert snap.trip_gap_seconds == 300


def test_save_round_trip_and_cache(overrides_path: Path) -> None:
    svc = MappingSettingsService(overrides_path)
    snap = svc.save_settings(trip_gap_minutes=7, speed_limit_mph=65)
    assert snap.trip_gap_minutes == 7
    assert snap.speed_limit_mph == 65
    assert snap.speed_limit_mps == pytest.approx(65 * 0.44704)
    assert snap.speed_limit_enabled is True

    payload = json.loads(overrides_path.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": 1,
        "speed_limit_mph": 65,
        "trip_gap_minutes": 7,
    }

    # Second read returns the cached snapshot — same object identity
    # to prove no re-parse happened.
    cached = svc.get_settings()
    assert cached is snap


def test_get_settings_only_stats_when_unchanged(overrides_path: Path) -> None:
    svc = MappingSettingsService(overrides_path)
    svc.save_settings(trip_gap_minutes=5, speed_limit_mph=0)
    # Warm the cache.
    svc.get_settings()
    with patch(
        "teslausb_web.services.mapping_settings_service.os.stat",
        wraps=__import__("os").stat,
    ) as stat_spy:
        for _ in range(10):
            svc.get_settings()
        # stat() called once per get_settings, never reads the file.
        assert stat_spy.call_count == 10


def test_external_mutation_invalidates_cache(overrides_path: Path) -> None:
    svc = MappingSettingsService(overrides_path)
    svc.save_settings(trip_gap_minutes=5, speed_limit_mph=0)
    snap1 = svc.get_settings()
    # Simulate an external change with a bumped mtime.
    overrides_path.write_text(
        '{"schema_version": 1, "trip_gap_minutes": 9, "speed_limit_mph": 75}\n',
        encoding="utf-8",
    )
    # The on-disk mtime changes monotonically; force a stat-cache bust.
    import os
    import time

    new_time = time.time() + 5
    os.utime(overrides_path, (new_time, new_time))
    snap2 = svc.get_settings()
    assert snap2 is not snap1
    assert snap2.trip_gap_minutes == 9
    assert snap2.speed_limit_mph == 75


def test_validation_rejects_negative_speed(overrides_path: Path) -> None:
    svc = MappingSettingsService(overrides_path)
    with pytest.raises(MappingSettingsError, match="speed_limit_mph"):
        svc.save_settings(trip_gap_minutes=5, speed_limit_mph=-1)


def test_validation_rejects_zero_trip_gap(overrides_path: Path) -> None:
    svc = MappingSettingsService(overrides_path)
    with pytest.raises(MappingSettingsError, match="trip_gap_minutes"):
        svc.save_settings(trip_gap_minutes=0, speed_limit_mph=0)


def test_validation_rejects_overlarge_trip_gap(overrides_path: Path) -> None:
    svc = MappingSettingsService(overrides_path)
    with pytest.raises(MappingSettingsError, match="trip_gap_minutes"):
        svc.save_settings(trip_gap_minutes=61, speed_limit_mph=0)


def test_unsupported_schema_version_raises(overrides_path: Path) -> None:
    overrides_path.write_text(
        '{"schema_version": 99, "trip_gap_minutes": 5, "speed_limit_mph": 0}\n',
        encoding="utf-8",
    )
    svc = MappingSettingsService(overrides_path)
    with pytest.raises(MappingSettingsError, match="schema version"):
        svc.get_settings()


def test_relative_path_rejected(tmp_path: Path) -> None:
    with pytest.raises(MappingSettingsError, match="absolute"):
        MappingSettingsService(Path("mapping_settings.json"))


def test_factory_uses_cfg_overrides_path(tmp_path: Path) -> None:
    from teslausb_web.config import (
        MappingSection,
        PathsSection,
        WebConfig,
        WebSection,
    )

    overrides = tmp_path / "state" / "mapping_settings.json"
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32),
        paths=PathsSection(
            backing_root=tmp_path / "back",
            state_dir=tmp_path / "state",
            cache_invalidate_script=tmp_path / "inv.sh",
        ),
        mapping=MappingSection(
            db_path=tmp_path / "db.sqlite3",
            media_root=tmp_path / "back",
            overrides_path=overrides,
        ),
        source_path=None,
    )
    svc = make_mapping_settings_service(cfg)
    assert svc.path == overrides
    assert svc.get_settings() == MappingSettings(
        trip_gap_minutes=5,
        speed_limit_mph=0,
        speed_limit_mps=0.0,
    )
