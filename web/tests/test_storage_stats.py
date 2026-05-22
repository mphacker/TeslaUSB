"""Tests for ``storage_stats`` service (AC.4)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest
from teslausb_web.services import storage_config as sc
from teslausb_web.services import storage_stats as ss


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    """Write a baseline teslausb.toml and return its path."""
    path = tmp_path / "teslausb.toml"
    config = sc.TeslausbConfig(
        storage=sc.StorageSection(os_reserve_gb=20, teslacam_gb=64, media_gb=32),
        cleanup=sc.CleanupSection(target_free_pct=5, sentry_max_age_days=0),
    )
    sc.save(config, path)
    return path


def test_get_storage_stats_reads_config(tmp_config: Path) -> None:
    with mock.patch.object(ss.shutil, "disk_usage") as du:
        du.return_value = mock.Mock(
            total=128 * ss.GB_BYTES, used=10 * ss.GB_BYTES, free=118 * ss.GB_BYTES,
        )
        stats = ss.get_storage_stats(config_path=tmp_config)
    assert stats.teslacam.advertised_gb == 64
    assert stats.media.advertised_gb == 32
    assert stats.os_reserve_gb == 20
    assert stats.allocated_gb == 64 + 32 + 20
    assert stats.target_free_pct == 5


def test_get_storage_stats_remaining_alloc_clamped_to_zero(tmp_config: Path) -> None:
    """If allocated > total (e.g. SD was swapped smaller), remaining is 0."""
    with mock.patch.object(ss.shutil, "disk_usage") as du:
        du.return_value = mock.Mock(total=32 * ss.GB_BYTES, used=0, free=32 * ss.GB_BYTES)
        stats = ss.get_storage_stats(config_path=tmp_config)
    assert stats.remaining_alloc_gb == 0


def test_safe_disk_usage_returns_zero_on_missing_path() -> None:
    used, free = ss._safe_disk_usage(Path("/nonexistent/" + "x" * 40))
    assert (used, free) == (0, 0)


def test_apply_storage_config_runs_helper_for_changed_lun(tmp_config: Path) -> None:
    new = sc.TeslausbConfig(
        storage=sc.StorageSection(os_reserve_gb=20, teslacam_gb=100, media_gb=32),
        cleanup=sc.CleanupSection(target_free_pct=5),
    )
    with mock.patch.object(ss.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="resize-lun: complete\n", stderr=""
        )
        msgs = ss.apply_storage_config(new, config_path=tmp_config, use_sudo=False)
    # Only teslacam changed; helper invoked exactly once.
    assert run.call_count == 1
    args = run.call_args[0][0]
    assert "--lun" in args
    assert "teslacam" in args
    assert "100" in args
    assert msgs
    assert "teslacam" in msgs[0]


def test_apply_storage_config_skips_helper_when_unchanged(tmp_config: Path) -> None:
    same = sc.load(tmp_config)
    with mock.patch.object(ss.subprocess, "run") as run:
        msgs = ss.apply_storage_config(same, config_path=tmp_config, use_sudo=False)
    assert run.call_count == 0
    assert msgs == []


def test_apply_storage_config_reports_cleanup_change(tmp_config: Path) -> None:
    old = sc.load(tmp_config)
    new = sc.TeslausbConfig(
        storage=old.storage,
        cleanup=sc.CleanupSection(target_free_pct=10),
    )
    with mock.patch.object(ss.subprocess, "run"):
        msgs = ss.apply_storage_config(new, config_path=tmp_config, use_sudo=False)
    assert any("cleanup" in m for m in msgs)


def test_apply_storage_config_helper_failure_raises(tmp_config: Path) -> None:
    new = sc.TeslausbConfig(
        storage=sc.StorageSection(os_reserve_gb=20, teslacam_gb=128, media_gb=32),
        cleanup=sc.CleanupSection(),
    )
    with mock.patch.object(ss.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=3, stdout="", stderr="refusing: too big"
        )
        with pytest.raises(ss.ApplyError, match="too big"):
            ss.apply_storage_config(new, config_path=tmp_config, use_sudo=False)


def test_apply_storage_config_helper_oserror_raises(tmp_config: Path) -> None:
    new = sc.TeslausbConfig(
        storage=sc.StorageSection(os_reserve_gb=20, teslacam_gb=128, media_gb=32),
        cleanup=sc.CleanupSection(),
    )
    with (
        mock.patch.object(ss.subprocess, "run", side_effect=OSError("boom")),
        pytest.raises(ss.ApplyError, match="boom"),
    ):
        ss.apply_storage_config(new, config_path=tmp_config, use_sudo=False)


def test_invalid_config_rejected_before_helper_invoked(tmp_config: Path) -> None:
    # os_reserve_gb=0 violates OS_RESERVE_MIN_GB.
    bad = sc.TeslausbConfig(
        storage=sc.StorageSection(os_reserve_gb=0, teslacam_gb=64, media_gb=32),
        cleanup=sc.CleanupSection(),
    )
    with mock.patch.object(ss.subprocess, "run") as run, pytest.raises(sc.StorageConfigError):
        ss.apply_storage_config(bad, config_path=tmp_config, use_sudo=False)
    assert run.call_count == 0
