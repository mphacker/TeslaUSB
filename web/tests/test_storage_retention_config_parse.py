"""Focused config parsing tests for storage_retention."""

from __future__ import annotations

from pathlib import Path

import pytest
from teslausb_web.config import ConfigError, StorageRetentionSection, load_config


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_storage_retention_defaults_round_trip(tmp_path: Path) -> None:
    cfg_file = tmp_path / "storage_retention_defaults.toml"
    _write(cfg_file, "[storage_retention]\n")
    cfg = load_config(cfg_file)
    assert cfg.storage_retention == StorageRetentionSection()


def test_storage_retention_section_round_trip(tmp_path: Path) -> None:
    cfg_file = tmp_path / "storage_retention.toml"
    _write(
        cfg_file,
        """
[storage_retention]
policy_path = "/var/lib/teslausb/custom_retention_policy.json"
default_max_age_days = 60
default_target_free_pct = 20
default_max_archive_size_gb = 100
default_short_retention_warning_days = 9
""",
    )
    cfg = load_config(cfg_file)
    assert cfg.storage_retention.policy_path == Path(
        "/var/lib/teslausb/custom_retention_policy.json"
    )
    assert cfg.storage_retention.default_max_age_days == 60
    assert cfg.storage_retention.default_target_free_pct == 20
    assert cfg.storage_retention.default_max_archive_size_gb == 100
    assert cfg.storage_retention.default_short_retention_warning_days == 9


def test_storage_retention_policy_path_must_be_absolute(tmp_path: Path) -> None:
    cfg_file = tmp_path / "bad_storage_retention.toml"
    _write(cfg_file, '[storage_retention]\npolicy_path = "state/policy.json"\n')
    with pytest.raises(ConfigError, match="policy_path must be absolute"):
        load_config(cfg_file)
