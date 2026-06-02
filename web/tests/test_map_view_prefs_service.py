"""Tests for `MapViewPreferencesService`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from teslausb_web.services.map_view_prefs_service import (
    MapViewPreferencesError,
    MapViewPreferencesService,
    SpeedUnits,
    make_map_view_prefs_service,
)


@pytest.fixture
def prefs_path(tmp_path: Path) -> Path:
    return tmp_path / "map_view_prefs.json"


def test_missing_file_returns_default_mph(prefs_path: Path) -> None:
    svc = MapViewPreferencesService(prefs_path)
    assert svc.get_preferences().speed_units is SpeedUnits.MPH


def test_save_round_trip_persists_kph(prefs_path: Path) -> None:
    svc = MapViewPreferencesService(prefs_path)
    saved = svc.save_preferences(speed_units=SpeedUnits.KPH)
    assert saved.speed_units is SpeedUnits.KPH
    assert svc.get_preferences().speed_units is SpeedUnits.KPH
    assert json.loads(prefs_path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "speed_units": "kph",
        "display_timezone": "",
    }


def test_validation_rejects_bad_speed_units(prefs_path: Path) -> None:
    svc = MapViewPreferencesService(prefs_path)
    with pytest.raises(MapViewPreferencesError, match="speed_units"):
        svc.save_preferences(speed_units="mps")


def test_default_display_timezone_is_auto(prefs_path: Path) -> None:
    svc = MapViewPreferencesService(prefs_path)
    assert svc.get_preferences().display_timezone == ""


def test_save_valid_display_timezone_round_trips(prefs_path: Path) -> None:
    svc = MapViewPreferencesService(prefs_path)
    saved = svc.save_preferences(
        speed_units=SpeedUnits.MPH, display_timezone="America/Detroit"
    )
    assert saved.display_timezone == "America/Detroit"
    assert svc.get_preferences().display_timezone == "America/Detroit"


def test_blank_display_timezone_is_auto(prefs_path: Path) -> None:
    svc = MapViewPreferencesService(prefs_path)
    saved = svc.save_preferences(speed_units=SpeedUnits.MPH, display_timezone="  ")
    assert saved.display_timezone == ""


def test_unknown_display_timezone_is_rejected(prefs_path: Path) -> None:
    svc = MapViewPreferencesService(prefs_path)
    with pytest.raises(MapViewPreferencesError, match="display_timezone"):
        svc.save_preferences(speed_units=SpeedUnits.MPH, display_timezone="Mars/Phobos")


def test_bad_on_disk_display_timezone_is_rejected(prefs_path: Path) -> None:
    prefs_path.write_text(
        '{"schema_version": 1, "speed_units": "mph", "display_timezone": "Nowhere/Land"}\n',
        encoding="utf-8",
    )
    svc = MapViewPreferencesService(prefs_path)
    with pytest.raises(MapViewPreferencesError, match="display_timezone"):
        svc.get_preferences()


def test_bad_on_disk_speed_units_is_rejected(prefs_path: Path) -> None:
    prefs_path.write_text(
        '{"schema_version": 1, "speed_units": "mps"}\n',
        encoding="utf-8",
    )
    svc = MapViewPreferencesService(prefs_path)
    with pytest.raises(MapViewPreferencesError, match="speed_units"):
        svc.get_preferences()


def test_writes_prefs_path_not_overrides_path(tmp_path: Path) -> None:
    overrides_path = tmp_path / "mapping_settings.json"
    prefs_path = tmp_path / "map_view_prefs.json"
    svc = MapViewPreferencesService(prefs_path)
    svc.save_preferences(speed_units=SpeedUnits.KPH)
    assert prefs_path.exists()
    assert not overrides_path.exists()


def test_relative_path_rejected() -> None:
    with pytest.raises(MapViewPreferencesError, match="absolute"):
        MapViewPreferencesService(Path("map_view_prefs.json"))


def test_factory_uses_cfg_view_prefs_path(tmp_path: Path) -> None:
    from teslausb_web.config import MappingSection, PathsSection, WebConfig, WebSection

    prefs = tmp_path / "state" / "map_view_prefs.json"
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
            overrides_path=tmp_path / "state" / "mapping_settings.json",
            view_prefs_path=prefs,
        ),
        source_path=None,
    )
    svc = make_map_view_prefs_service(cfg)
    assert svc.path == prefs
    assert svc.get_preferences().speed_units is SpeedUnits.MPH
