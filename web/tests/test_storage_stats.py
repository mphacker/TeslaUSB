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
        storage=sc.StorageSection(safety_buffer_gb=20, teslacam_gb=64, media_gb=32),
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
    assert stats.safety_buffer_gb == 20
    # measured OS usage = total - free - du(teslacam) - du(media) = 128-118-0-0
    assert stats.os_usage_gb == 10
    # allocated is the advertised partitions only; reserve is separate now.
    assert stats.allocated_gb == 64 + 32
    assert stats.reserve_gb == 10 + 20
    assert stats.target_free_pct == 5


def test_get_storage_stats_max_allocatable(tmp_config: Path) -> None:
    """Per-partition max = floor(sd_total - other_advertised - os_used - buffer)."""
    with mock.patch.object(ss.shutil, "disk_usage") as du:
        du.return_value = mock.Mock(
            total=128 * ss.GB_BYTES, used=10 * ss.GB_BYTES, free=118 * ss.GB_BYTES,
        )
        stats = ss.get_storage_stats(config_path=tmp_config)
    # teslacam max = 128 - media(32) - os_used(10) - buffer(20) = 66
    assert stats.teslacam.max_allocatable_gb == 66
    # media max = 128 - teslacam(64) - os_used(10) - buffer(20) = 34
    assert stats.media.max_allocatable_gb == 34


def test_get_storage_stats_remaining_alloc_clamped_to_zero(tmp_config: Path) -> None:
    """If allocated > total (e.g. SD was swapped smaller), remaining is 0."""
    with mock.patch.object(ss.shutil, "disk_usage") as du:
        du.return_value = mock.Mock(total=32 * ss.GB_BYTES, used=0, free=32 * ss.GB_BYTES)
        stats = ss.get_storage_stats(config_path=tmp_config)
    assert stats.remaining_alloc_gb == 0


def test_safe_disk_usage_returns_zero_on_missing_path() -> None:
    used, free = ss._safe_disk_usage(Path("/nonexistent/" + "x" * 40))
    assert (used, free) == (0, 0)


def test_tree_size_bytes_sums_dir(tmp_path: Path) -> None:
    (tmp_path / "a.mp4").write_bytes(b"x" * 1024)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.mp4").write_bytes(b"y" * 2048)
    assert ss._tree_size_bytes(tmp_path) == 1024 + 2048


def test_tree_size_bytes_returns_zero_for_missing_root() -> None:
    assert ss._tree_size_bytes(Path("/nonexistent/" + "x" * 40)) == 0


def test_tree_size_bytes_ignores_symlinks(tmp_path: Path) -> None:
    target_dir = tmp_path / "real"
    target_dir.mkdir()
    (target_dir / "big.mp4").write_bytes(b"z" * 4096)
    link_root = tmp_path / "linked"
    link_root.mkdir()
    try:
        (link_root / "lnk").symlink_to(target_dir)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    # The symlink under link_root must NOT be followed, so total is 0.
    assert ss._tree_size_bytes(link_root) == 0


def test_apply_storage_config_runs_helper_for_changed_lun(tmp_config: Path) -> None:
    new = sc.TeslausbConfig(
        storage=sc.StorageSection(safety_buffer_gb=20, teslacam_gb=100, media_gb=32),
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
        storage=sc.StorageSection(safety_buffer_gb=20, teslacam_gb=128, media_gb=32),
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
        storage=sc.StorageSection(safety_buffer_gb=20, teslacam_gb=128, media_gb=32),
        cleanup=sc.CleanupSection(),
    )
    with (
        mock.patch.object(ss.subprocess, "run", side_effect=OSError("boom")),
        pytest.raises(ss.ApplyError, match="boom"),
    ):
        ss.apply_storage_config(new, config_path=tmp_config, use_sudo=False)


def test_invalid_config_rejected_before_helper_invoked(tmp_config: Path) -> None:
    # safety_buffer_gb=0 violates SAFETY_BUFFER_MIN_GB.
    bad = sc.TeslausbConfig(
        storage=sc.StorageSection(safety_buffer_gb=0, teslacam_gb=64, media_gb=32),
        cleanup=sc.CleanupSection(),
    )
    with mock.patch.object(ss.subprocess, "run") as run, pytest.raises(sc.StorageConfigError):
        ss.apply_storage_config(bad, config_path=tmp_config, use_sudo=False)
    assert run.call_count == 0


def test_apply_storage_config_refuses_shrink_below_usage(
    tmp_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TeslaCam currently stores 80 GB; requesting 64 GB must be refused
    # BEFORE the config is written (mirrors the helper shrink guard).
    monkeypatch.setattr(
        ss,
        "_tree_size_bytes",
        lambda root: 80 * ss.GB_BYTES if "teslacam" in str(root) else 0,
    )
    monkeypatch.setattr(
        ss, "_sd_capacity_bytes", lambda: (256 * ss.GB_BYTES, 100 * ss.GB_BYTES)
    )
    new = sc.TeslausbConfig(
        storage=sc.StorageSection(safety_buffer_gb=5, teslacam_gb=64, media_gb=32),
        cleanup=sc.CleanupSection(target_free_pct=5),
    )
    before = tmp_config.read_text(encoding="utf-8")
    with (
        mock.patch.object(ss.subprocess, "run") as run,
        pytest.raises(sc.StorageConfigError, match="TeslaCam usage"),
    ):
        ss.apply_storage_config(new, config_path=tmp_config, use_sudo=False)
    assert run.call_count == 0
    # Config file must be untouched (refused before save).
    assert tmp_config.read_text(encoding="utf-8") == before

