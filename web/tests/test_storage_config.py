"""Tests for `teslausb_web.services.storage_config`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from teslausb_web.services import storage_config
from teslausb_web.services.storage_config import (
    CleanupSection,
    StorageConfigError,
    StorageSection,
    TeslausbConfig,
    default_config,
    load,
    save,
    validate_against_capacity,
    with_cleanup,
    with_storage,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_load_returns_defaults_when_file_absent(tmp_path: Path) -> None:
    config = load(tmp_path / "missing.toml")
    assert config == default_config()


def test_load_parses_full_file(tmp_path: Path) -> None:
    target = tmp_path / "teslausb.toml"
    _write(
        target,
        """
        [storage]
        safety_buffer_gb = 12
        teslacam_gb = 128
        media_gb = 64

        [cleanup]
        target_free_pct = 12
        sentry_max_age_days = 90
        preserve_with_gps = false
        """,
    )
    config = load(target)
    assert config.storage == StorageSection(
        safety_buffer_gb=12,
        teslacam_gb=128,
        media_gb=64,
    )
    assert config.cleanup == CleanupSection(
        target_free_pct=12,
        sentry_max_age_days=90,
        preserve_with_gps=False,
    )


def test_load_falls_back_to_defaults_for_missing_keys(tmp_path: Path) -> None:
    target = tmp_path / "teslausb.toml"
    _write(target, "[storage]\nteslacam_gb = 100\n")
    config = load(target)
    assert config.storage.teslacam_gb == 100
    assert config.storage.media_gb == StorageSection().media_gb
    assert config.cleanup == CleanupSection()


def test_load_rejects_malformed_toml(tmp_path: Path) -> None:
    target = tmp_path / "teslausb.toml"
    _write(target, "this is not = valid =toml [[[")
    with pytest.raises(StorageConfigError, match="failed to read"):
        load(target)


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ("[storage]\nsafety_buffer_gb = 4\n", "safety_buffer_gb must be >= 5"),
        ("[storage]\nteslacam_gb = 1\n", "teslacam_gb must be in"),
        ("[storage]\nmedia_gb = 9999\n", "media_gb must be in"),
        ("[cleanup]\ntarget_free_pct = -1\n", "target_free_pct"),
        ("[cleanup]\ntarget_free_pct = 90\n", "target_free_pct"),
        ("[cleanup]\nsentry_max_age_days = 9999\n", "sentry_max_age_days"),
        ("[storage]\nteslacam_gb = true\n", "teslacam_gb must be an integer"),
        ("[cleanup]\npreserve_with_gps = 1\n", "preserve_with_gps must be a boolean"),
    ],
)
def test_load_validates_bounds(tmp_path: Path, body: str, needle: str) -> None:
    target = tmp_path / "teslausb.toml"
    _write(target, body)
    with pytest.raises(StorageConfigError, match=needle):
        load(target)


def test_save_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "teslausb.toml"
    config = TeslausbConfig(
        storage=StorageSection(safety_buffer_gb=24, teslacam_gb=200, media_gb=40),
        cleanup=CleanupSection(
            target_free_pct=8,
            sentry_max_age_days=30,
            preserve_with_gps=False,
        ),
    )
    save(config, target)
    assert load(target) == config


def test_save_creates_parent_dir(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "teslausb.toml"
    save(default_config(), target)
    assert target.exists()


def test_save_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "teslausb.toml"
    save(default_config(), target)
    original = target.read_text(encoding="utf-8")

    def boom(self: Path, _dst: Path) -> Path:
        del self
        raise OSError("simulated rename failure")

    monkeypatch.setattr("pathlib.Path.replace", boom)
    bad = TeslausbConfig(
        storage=StorageSection(teslacam_gb=512),
        cleanup=CleanupSection(),
    )
    with pytest.raises(OSError, match="simulated rename"):
        save(bad, target)
    # Original survives, no half-written file remains under the real name.
    assert target.read_text(encoding="utf-8") == original


def test_save_validates_before_writing(tmp_path: Path) -> None:
    target = tmp_path / "teslausb.toml"
    bad = TeslausbConfig(
        storage=StorageSection(safety_buffer_gb=2),
        cleanup=CleanupSection(),
    )
    with pytest.raises(StorageConfigError, match="safety_buffer_gb"):
        save(bad, target)
    assert not target.exists()


def test_capacity_check_passes_when_sd_total_unknown() -> None:
    validate_against_capacity(default_config(), sd_total_gb=0)


def test_capacity_check_passes_when_under_limit() -> None:
    config = TeslausbConfig(
        storage=StorageSection(safety_buffer_gb=20, teslacam_gb=64, media_gb=32),
        cleanup=CleanupSection(),
    )
    validate_against_capacity(config, sd_total_gb=128)  # 128 - 20 = 108 usable


def test_capacity_check_rejects_overcommit() -> None:
    config = TeslausbConfig(
        storage=StorageSection(safety_buffer_gb=20, teslacam_gb=256, media_gb=64),
        cleanup=CleanupSection(),
    )
    with pytest.raises(StorageConfigError, match="exceeds usable capacity"):
        validate_against_capacity(config, sd_total_gb=256)  # 256-20=236 < 320


def test_capacity_check_counts_measured_os_usage() -> None:
    # Buffer alone fits, but once the measured OS footprint is added the
    # request overcommits — proves os_usage_gb is enforced, not just buffer.
    config = TeslausbConfig(
        storage=StorageSection(safety_buffer_gb=5, teslacam_gb=64, media_gb=32),
        cleanup=CleanupSection(),
    )
    validate_against_capacity(config, sd_total_gb=128, os_usage_gb=10)  # 96 <= 113
    with pytest.raises(StorageConfigError, match="exceeds usable capacity"):
        validate_against_capacity(config, sd_total_gb=128, os_usage_gb=40)  # 96 > 83


def test_with_storage_replaces_only_named_fields() -> None:
    base = default_config()
    updated = with_storage(base, teslacam_gb=500)
    assert updated.storage.teslacam_gb == 500
    assert updated.storage.media_gb == base.storage.media_gb
    assert updated.storage.safety_buffer_gb == base.storage.safety_buffer_gb
    assert updated.cleanup == base.cleanup


def test_with_cleanup_replaces_only_named_fields() -> None:
    base = default_config()
    updated = with_cleanup(base, sentry_max_age_days=60, preserve_with_gps=False)
    assert updated.cleanup.sentry_max_age_days == 60
    assert updated.cleanup.preserve_with_gps is False
    assert updated.cleanup.target_free_pct == base.cleanup.target_free_pct
    assert updated.storage == base.storage


def test_rendered_toml_has_sections_and_managed_header(tmp_path: Path) -> None:
    target = tmp_path / "teslausb.toml"
    save(default_config(), target)
    body = target.read_text(encoding="utf-8")
    assert body.startswith("# Managed by teslausb-b1")
    assert "[storage]" in body
    assert "[cleanup]" in body
    assert "preserve_with_gps = true" in body


def test_default_constants_documented() -> None:
    # Pin defaults — the operator chose a 5 GB safety buffer (floor and
    # default) and these bounds; a silent change would surprise users.
    assert storage_config.SAFETY_BUFFER_DEFAULT_GB == 5
    assert storage_config.SAFETY_BUFFER_MIN_GB == 5
    assert storage_config.LUN_MIN_GB == 4
    assert storage_config.LUN_MAX_GB == 2048
