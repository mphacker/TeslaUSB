"""Tests for ``teslausb_web.services.chime_group_service``."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Final
from unittest.mock import patch

import pytest
from teslausb_web.config import ChimesSection, PathsSection, WebConfig
from teslausb_web.services.chime_group_service import (
    ChimeGroupError,
    ChimeGroupManager,
    ChimeGroupStateError,
    RandomConfig,
    make_chime_group_manager,
)

_GROUP_NAME_MAX_LEN: Final[int] = 100


@pytest.fixture
def groups_file(tmp_path: Path) -> Path:
    return tmp_path / "chime_groups.json"


@pytest.fixture
def random_config_file(tmp_path: Path) -> Path:
    return tmp_path / "chime_random_config.json"


@pytest.fixture
def manager(groups_file: Path, random_config_file: Path) -> ChimeGroupManager:
    return ChimeGroupManager(groups_file=groups_file, random_config_file=random_config_file)


def _create_group(manager: ChimeGroupManager, name: str = "Holiday") -> str:
    return manager.create_group(name).group_id or ""


def test_list_groups_is_empty_when_files_are_missing(manager: ChimeGroupManager) -> None:
    assert manager.list_groups() == ()
    assert manager.get_random_config() == RandomConfig(
        enabled=False,
        group_id=None,
        last_selected=None,
        last_selected_at=None,
    )


def test_create_group_returns_persisted_group(manager: ChimeGroupManager) -> None:
    result = manager.create_group("Holiday", chime_filenames=("jingle.wav",))
    assert result.ok is True
    assert result.group_id is not None
    group = manager.get_group(result.group_id)
    assert group is not None
    assert group.name == "Holiday"
    assert group.chime_filenames == ("jingle.wav",)
    assert group.created_at.tzinfo is not None
    assert group.updated_at == group.created_at


def test_list_groups_sorts_case_insensitively(manager: ChimeGroupManager) -> None:
    manager.create_group("zeta")
    manager.create_group("Alpha")
    assert [group.name for group in manager.list_groups()] == ["Alpha", "zeta"]


def test_rename_group_updates_name_and_timestamp(manager: ChimeGroupManager) -> None:
    group_id = _create_group(manager)
    before = manager.get_group(group_id)
    assert before is not None
    result = manager.rename_group(group_id, "Renamed")
    after = manager.get_group(group_id)
    assert result.ok is True
    assert after is not None
    assert after.name == "Renamed"
    assert after.updated_at >= before.updated_at


def test_delete_group_removes_group(manager: ChimeGroupManager) -> None:
    group_id = _create_group(manager)
    result = manager.delete_group(group_id)
    assert result.ok is True
    assert manager.get_group(group_id) is None
    assert manager.list_groups() == ()


@pytest.mark.parametrize(
    "name",
    ["", "   ", "x" * (_GROUP_NAME_MAX_LEN + 1)],
)
def test_create_group_rejects_invalid_names(manager: ChimeGroupManager, name: str) -> None:
    with pytest.raises(ChimeGroupError):
        manager.create_group(name)


def test_create_group_rejects_duplicate_names_case_insensitively(
    manager: ChimeGroupManager,
) -> None:
    manager.create_group("Holiday")
    with pytest.raises(ChimeGroupError, match="already exists"):
        manager.create_group("holiday")


def test_rename_group_rejects_duplicate_names(manager: ChimeGroupManager) -> None:
    first_group_id = _create_group(manager, "Holiday")
    second_group_id = _create_group(manager, "Seasonal")
    assert first_group_id != second_group_id
    with pytest.raises(ChimeGroupError, match="already exists"):
        manager.rename_group(second_group_id, "holiday")


def test_create_group_enforces_maximum_group_count(manager: ChimeGroupManager) -> None:
    for index in range(50):
        manager.create_group(f"Group {index}")
    with pytest.raises(ChimeGroupError, match="Cannot create more than"):
        manager.create_group("Overflow")


def test_add_chime_to_group_appends_filename(manager: ChimeGroupManager) -> None:
    group_id = _create_group(manager)
    group = manager.add_chime_to_group(group_id, "new.wav")
    assert group.chime_filenames == ("new.wav",)
    assert manager.list_group_chimes(group_id) == ("new.wav",)


def test_add_chime_to_group_accepts_filename_not_in_library(manager: ChimeGroupManager) -> None:
    group_id = _create_group(manager)
    group = manager.add_chime_to_group(group_id, "totally-made-up.wav")
    assert group.chime_filenames == ("totally-made-up.wav",)


def test_add_chime_to_group_rejects_duplicates(manager: ChimeGroupManager) -> None:
    group_id = _create_group(manager)
    manager.add_chime_to_group(group_id, "once.wav")
    with pytest.raises(ChimeGroupError, match="already exists"):
        manager.add_chime_to_group(group_id, "once.wav")


def test_remove_chime_from_group_removes_filename(manager: ChimeGroupManager) -> None:
    group_id = manager.create_group("Holiday", chime_filenames=("a.wav", "b.wav")).group_id
    assert group_id is not None
    updated = manager.remove_chime_from_group(group_id, "a.wav")
    assert updated.chime_filenames == ("b.wav",)


def test_remove_chime_from_group_raises_for_unknown_group(manager: ChimeGroupManager) -> None:
    with pytest.raises(ChimeGroupError, match="was not found"):
        manager.remove_chime_from_group("missing", "x.wav")


def test_remove_chime_from_group_raises_for_missing_chime(manager: ChimeGroupManager) -> None:
    group_id = _create_group(manager)
    with pytest.raises(ChimeGroupError, match="is not in group"):
        manager.remove_chime_from_group(group_id, "missing.wav")


def test_persistence_round_trip_reads_groups_and_random_config(
    groups_file: Path,
    random_config_file: Path,
) -> None:
    first_manager = ChimeGroupManager(
        groups_file=groups_file, random_config_file=random_config_file
    )
    group_id = first_manager.create_group("Holiday", chime_filenames=("a.wav", "b.wav")).group_id
    assert group_id is not None
    first_manager.set_random_mode(enabled=True, group_id=group_id)
    selected = first_manager.select_random_chime(group_id)
    second_manager = ChimeGroupManager(
        groups_file=groups_file, random_config_file=random_config_file
    )
    persisted_group = second_manager.get_group(group_id)
    assert persisted_group is not None
    assert persisted_group.chime_filenames == ("a.wav", "b.wav")
    assert second_manager.get_random_config().last_selected == selected


def test_create_group_rolls_back_when_atomic_replace_fails(
    manager: ChimeGroupManager,
    groups_file: Path,
) -> None:
    groups_file.write_text("{}\n", encoding="utf-8")
    original = groups_file.read_text(encoding="utf-8")
    with (
        patch("teslausb_web.services.chime_group_service.os.replace", side_effect=OSError("nope")),
        pytest.raises(ChimeGroupStateError),
    ):
        manager.create_group("Holiday")
    assert groups_file.read_text(encoding="utf-8") == original
    assert manager.list_groups() == ()


def test_corrupt_groups_json_logs_and_returns_empty(
    groups_file: Path,
    random_config_file: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    groups_file.write_text("{not-json", encoding="utf-8")
    caplog.set_level("WARNING")
    manager = ChimeGroupManager(groups_file=groups_file, random_config_file=random_config_file)
    assert manager.list_groups() == ()
    assert "Failed to load chime groups" in caplog.text


def test_corrupt_random_config_logs_and_returns_default(
    groups_file: Path,
    random_config_file: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    random_config_file.write_text("{not-json", encoding="utf-8")
    caplog.set_level("WARNING")
    manager = ChimeGroupManager(groups_file=groups_file, random_config_file=random_config_file)
    assert manager.get_random_config() == RandomConfig(
        enabled=False,
        group_id=None,
        last_selected=None,
        last_selected_at=None,
    )
    assert "Failed to load random chime config" in caplog.text


def test_missing_file_is_created_on_first_write(
    groups_file: Path,
    random_config_file: Path,
) -> None:
    manager = ChimeGroupManager(groups_file=groups_file, random_config_file=random_config_file)
    manager.create_group("Holiday")
    assert groups_file.exists()
    payload = json.loads(groups_file.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert len(payload) == 1


def test_select_random_chime_returns_none_for_empty_group(manager: ChimeGroupManager) -> None:
    group_id = _create_group(manager)
    assert manager.select_random_chime(group_id) is None


def test_select_random_chime_returns_only_choice_even_if_previously_selected(
    manager: ChimeGroupManager,
) -> None:
    group_id = manager.create_group("Holiday", chime_filenames=("only.wav",)).group_id
    assert group_id is not None
    first = manager.select_random_chime(group_id)
    second = manager.select_random_chime(group_id)
    assert first == "only.wav"
    assert second == "only.wav"


def test_select_random_chime_avoids_last_selected_when_multiple_choices(
    manager: ChimeGroupManager,
) -> None:
    group_id = manager.create_group("Holiday", chime_filenames=("a.wav", "b.wav")).group_id
    assert group_id is not None
    manager.set_random_mode(enabled=True, group_id=group_id)
    first = manager.select_random_chime(group_id)
    second = manager.select_random_chime(group_id)
    assert first in {"a.wav", "b.wav"}
    assert second in {"a.wav", "b.wav"}
    assert second != first


def test_get_active_random_chime_returns_none_when_disabled(manager: ChimeGroupManager) -> None:
    group_id = manager.create_group("Holiday", chime_filenames=("a.wav",)).group_id
    assert group_id is not None
    manager.select_random_chime(group_id)
    assert manager.get_active_random_chime() is None


def test_get_active_random_chime_returns_none_when_group_missing(
    manager: ChimeGroupManager,
) -> None:
    group_id = manager.create_group("Holiday", chime_filenames=("a.wav",)).group_id
    assert group_id is not None
    manager.set_random_mode(enabled=True, group_id=group_id)
    manager.select_random_chime(group_id)
    groups = manager._groups.copy()
    del groups[group_id]
    manager._groups = groups
    assert manager.get_active_random_chime() is None


def test_get_active_random_chime_returns_selected_chime_when_enabled(
    manager: ChimeGroupManager,
) -> None:
    group_id = manager.create_group("Holiday", chime_filenames=("a.wav",)).group_id
    assert group_id is not None
    manager.set_random_mode(enabled=True, group_id=group_id)
    selected = manager.select_random_chime(group_id)
    assert manager.get_active_random_chime() == selected


def test_set_random_mode_requires_group_for_enable(manager: ChimeGroupManager) -> None:
    with pytest.raises(ChimeGroupError, match="group_id is required"):
        manager.set_random_mode(enabled=True)


def test_set_random_mode_rejects_empty_group(manager: ChimeGroupManager) -> None:
    group_id = _create_group(manager)
    with pytest.raises(ChimeGroupError, match="empty group"):
        manager.set_random_mode(enabled=True, group_id=group_id)


def test_delete_group_rejects_group_active_for_random_mode(
    manager: ChimeGroupManager,
) -> None:
    group_id = manager.create_group("Holiday", chime_filenames=("a.wav",)).group_id
    assert group_id is not None
    manager.set_random_mode(enabled=True, group_id=group_id)
    with pytest.raises(ChimeGroupError, match="Cannot delete"):
        manager.delete_group(group_id)


def test_concurrent_add_chime_to_group_keeps_all_updates(manager: ChimeGroupManager) -> None:
    group_id = _create_group(manager)
    filenames = tuple(f"chime-{index}.wav" for index in range(10))

    def _add(filename: str) -> None:
        manager.add_chime_to_group(group_id, filename)

    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(_add, filenames))
    assert set(manager.list_group_chimes(group_id)) == set(filenames)


def test_factory_builds_manager_with_configured_state_paths(tmp_path: Path) -> None:
    cfg = WebConfig(
        paths=PathsSection(state_dir=Path("/var/lib/teslausb"), backing_root=Path("/srv/teslausb")),
        chimes=ChimesSection(
            groups_file_relpath="groups.json",
            random_config_relpath="random.json",
        ),
    )
    manager = make_chime_group_manager(cfg)
    assert manager._groups_file == Path("/var/lib/teslausb") / "groups.json"
    assert manager._random_config_file == Path("/var/lib/teslausb") / "random.json"


def test_manager_loads_legacy_chimes_key(groups_file: Path, random_config_file: Path) -> None:
    groups_file.write_text(
        json.dumps(
            {
                "legacy": {
                    "name": "Legacy",
                    "chimes": ["a.wav"],
                    "created_at": "2024-01-01T00:00:00+0000",
                    "updated_at": "2024-01-01T00:00:00+0000",
                }
            }
        ),
        encoding="utf-8",
    )
    manager = ChimeGroupManager(groups_file=groups_file, random_config_file=random_config_file)
    group = manager.get_group("legacy")
    assert group is not None
    assert group.chime_filenames == ("a.wav",)
