"""Tests for the storage-retention service."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from teslausb_web.config import PathsSection, StorageRetentionSection, WebConfig, WebSection
from teslausb_web.services.storage_retention_service import (
    RetentionConfigError,
    RetentionPolicy,
    RetentionStateError,
    StorageRetentionConfig,
    StorageRetentionService,
    make_storage_retention_service,
)


@dataclass(frozen=True, slots=True)
class _FutureStat:
    st_mtime: float


def _build_invalid_storage_config(
    field: str, value: int, policy_path: Path
) -> StorageRetentionConfig:
    if field == "default_max_age_days":
        return StorageRetentionConfig(policy_path=policy_path, default_max_age_days=value)
    if field == "default_target_free_pct":
        return StorageRetentionConfig(policy_path=policy_path, default_target_free_pct=value)
    if field == "default_max_archive_size_gb":
        return StorageRetentionConfig(policy_path=policy_path, default_max_archive_size_gb=value)
    return StorageRetentionConfig(
        policy_path=policy_path,
        default_short_retention_warning_days=value,
    )


def _build_invalid_policy(field: str, value: int) -> RetentionPolicy:  # noqa: PLR0911
    if field == "max_age_days":
        return RetentionPolicy(max_age_days=value)
    if field == "target_free_pct":
        return RetentionPolicy(target_free_pct=value)
    if field == "max_archive_size_gb":
        return RetentionPolicy(max_archive_size_gb=value)
    if field == "short_retention_warning_days":
        return RetentionPolicy(short_retention_warning_days=value)
    if field == "recent_clips_days":
        return RetentionPolicy(recent_clips_days=value)
    if field == "saved_clips_days":
        return RetentionPolicy(saved_clips_days=value)
    if field == "event_clips_days":
        return RetentionPolicy(event_clips_days=value)
    if field == "encrypted_clips_days":
        return RetentionPolicy(encrypted_clips_days=value)
    return RetentionPolicy(archived_clips_days=value)


@pytest.fixture
def config(tmp_path: Path) -> StorageRetentionConfig:
    return StorageRetentionConfig(policy_path=tmp_path / "state" / "retention_policy.json")


@pytest.fixture
def service(config: StorageRetentionConfig) -> StorageRetentionService:
    return StorageRetentionService(config)


class TestStorageRetentionConfig:
    def test_resolves_policy_path(self, tmp_path: Path) -> None:
        cfg = StorageRetentionConfig(policy_path=tmp_path / "state" / "policy.json")
        assert cfg.policy_path.is_absolute()

    def test_rejects_relative_policy_path(self) -> None:
        with pytest.raises(RetentionConfigError, match="absolute"):
            StorageRetentionConfig(policy_path=Path("relative-policy.json"))

    def test_rejects_future_dated_file(self, tmp_path: Path) -> None:
        policy_path = tmp_path / "state" / "policy.json"
        policy_path.parent.mkdir(parents=True)
        policy_path.write_text("{}\n", encoding="utf-8")
        future = datetime.now(tz=UTC) + timedelta(days=1)
        with patch.object(Path, "stat", return_value=policy_path.stat()) as stat_mock:
            stat_mock.return_value = _FutureStat(st_mtime=future.timestamp())
            with pytest.raises(RetentionConfigError, match="future-dated"):
                StorageRetentionConfig(policy_path=policy_path)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("default_max_age_days", 0),
            ("default_target_free_pct", 4),
            ("default_max_archive_size_gb", -1),
            ("default_short_retention_warning_days", 0),
        ],
    )
    def test_rejects_invalid_default_ranges(self, tmp_path: Path, field: str, value: int) -> None:
        policy_path = tmp_path / "state" / "policy.json"
        with pytest.raises(RetentionConfigError):
            _build_invalid_storage_config(field, value, policy_path)


class TestRetentionPolicyValidation:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("max_age_days", 0),
            ("target_free_pct", 99),
            ("max_archive_size_gb", -1),
            ("short_retention_warning_days", 0),
            ("recent_clips_days", 0),
            ("saved_clips_days", 0),
            ("event_clips_days", 0),
            ("encrypted_clips_days", 0),
            ("archived_clips_days", 0),
        ],
    )
    def test_rejects_invalid_ranges(self, field: str, value: int) -> None:
        with pytest.raises(RetentionConfigError):
            _build_invalid_policy(field, value)


class TestStorageRetentionService:
    def test_default_policy_when_file_missing(self, service: StorageRetentionService) -> None:
        policy = service.get_policy()
        assert policy.max_age_days == 30
        assert policy.target_free_pct == 10
        assert policy.keep_saved_clips is True
        assert policy.keep_recent_clips is False

    def test_save_and_reload_round_trip(self, service: StorageRetentionService) -> None:
        saved = service.save_policy(
            RetentionPolicy(
                max_age_days=60,
                target_free_pct=15,
                max_archive_size_gb=200,
                short_retention_warning_days=14,
                keep_saved_clips=False,
                dry_run=False,
                saved_clips_days=120,
            )
        )
        reloaded = service.get_policy()
        assert reloaded == saved
        assert reloaded.saved_clips_days == 120

    def test_save_creates_parent_directory(
        self, service: StorageRetentionService, config: StorageRetentionConfig
    ) -> None:
        assert not config.policy_path.parent.exists()
        service.save_policy(RetentionPolicy())
        assert config.policy_path.exists()

    def test_serialize_policy_contains_expected_fields(
        self, service: StorageRetentionService
    ) -> None:
        payload = service.serialize_policy(RetentionPolicy())
        assert payload["keep_event_clips"] is True
        assert payload["event_clips_days"] == 30
        assert payload["dry_run"] is True

    def test_policy_rows_follow_policy_values(self, service: StorageRetentionService) -> None:
        rows = service.policy_rows(
            RetentionPolicy(keep_saved_clips=False, saved_clips_days=90, keep_recent_clips=True)
        )
        saved_row = next(row for row in rows if row.key == "saved")
        recent_row = next(row for row in rows if row.key == "recent")
        assert saved_row.keep is False
        assert saved_row.retention_days == 90
        assert recent_row.keep is True
        assert recent_row.label == "RecentClips"

    def test_ranges_report_expected_bounds(self, service: StorageRetentionService) -> None:
        ranges = service.ranges()
        assert ranges["retention_days"] == {"min": 1, "max": 3650}
        assert ranges["target_free_pct"] == {"min": 5, "max": 50}

    def test_update_policy_accepts_string_payload(self, service: StorageRetentionService) -> None:
        policy = service.update_policy(
            {
                "max_age_days": "45",
                "target_free_pct": "20",
                "max_archive_size_gb": "400",
                "short_retention_warning_days": "10",
                "keep_saved_clips": "false",
                "keep_event_clips": "0",
                "keep_recent_clips": "true",
                "keep_encrypted_clips": "1",
                "keep_archived_clips": "off",
                "dry_run": "false",
                "saved_clips_days": "90",
                "event_clips_days": "120",
                "recent_clips_days": "30",
                "encrypted_clips_days": "60",
                "archived_clips_days": "15",
            }
        )
        assert policy.max_age_days == 45
        assert policy.keep_saved_clips is False
        assert policy.keep_event_clips is False
        assert policy.keep_recent_clips is True
        assert policy.dry_run is False
        assert policy.archived_clips_days == 15

    def test_update_policy_preserves_existing_values_when_keys_missing(
        self, service: StorageRetentionService
    ) -> None:
        service.save_policy(RetentionPolicy(max_age_days=75, keep_saved_clips=False))
        updated = service.update_policy({"target_free_pct": "25"})
        assert updated.max_age_days == 75
        assert updated.keep_saved_clips is False
        assert updated.target_free_pct == 25

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("max_age_days", "oops", "integer"),
            ("target_free_pct", "51", "between 5 and 50"),
            ("max_archive_size_gb", "-1", "between 0 and 10000"),
            ("short_retention_warning_days", "0", "between 1 and 3650"),
            ("keep_saved_clips", "maybe", "boolean"),
            ("event_clips_days", "0", "between 1 and 3650"),
        ],
    )
    def test_update_policy_rejects_invalid_payload(
        self,
        service: StorageRetentionService,
        field: str,
        value: object,
        message: str,
    ) -> None:
        with pytest.raises(RetentionConfigError, match=message):
            service.update_policy({field: value})

    def test_get_policy_rejects_invalid_json(
        self, config: StorageRetentionConfig, service: StorageRetentionService
    ) -> None:
        config.policy_path.parent.mkdir(parents=True)
        config.policy_path.write_text("{not-json}\n", encoding="utf-8")
        with pytest.raises(RetentionStateError, match="Failed to parse"):
            service.get_policy()

    def test_get_policy_rejects_non_object_payload(
        self, config: StorageRetentionConfig, service: StorageRetentionService
    ) -> None:
        config.policy_path.parent.mkdir(parents=True)
        config.policy_path.write_text("[]\n", encoding="utf-8")
        with pytest.raises(RetentionStateError, match="JSON object"):
            service.get_policy()

    def test_get_policy_rejects_non_object_policy_block(
        self,
        config: StorageRetentionConfig,
        service: StorageRetentionService,
    ) -> None:
        config.policy_path.parent.mkdir(parents=True)
        config.policy_path.write_text('{"schema_version": 1, "policy": []}\n', encoding="utf-8")
        with pytest.raises(RetentionStateError, match="payload"):
            service.get_policy()

    def test_get_policy_rejects_unknown_schema(
        self, config: StorageRetentionConfig, service: StorageRetentionService
    ) -> None:
        config.policy_path.parent.mkdir(parents=True)
        config.policy_path.write_text('{"schema_version": 2, "policy": {}}\n', encoding="utf-8")
        with pytest.raises(
            RetentionStateError, match="Unsupported retention policy schema version"
        ):
            service.get_policy()

    def test_get_policy_accepts_root_level_policy_payload(
        self,
        config: StorageRetentionConfig,
        service: StorageRetentionService,
    ) -> None:
        config.policy_path.parent.mkdir(parents=True)
        config.policy_path.write_text(
            json.dumps({"schema_version": 1, "max_age_days": 40, "target_free_pct": 18}) + "\n",
            encoding="utf-8",
        )
        policy = service.get_policy()
        assert policy.max_age_days == 40
        assert policy.target_free_pct == 18

    def test_save_policy_cleans_temp_file_when_replace_fails(
        self,
        config: StorageRetentionConfig,
        service: StorageRetentionService,
    ) -> None:
        with (
            patch(
                "teslausb_web.services.storage_retention_service.Path.replace",
                side_effect=OSError("boom"),
            ),
            pytest.raises(RetentionStateError, match="Failed to write"),
        ):
            service.save_policy(RetentionPolicy())
        assert not config.policy_path.with_name(f"{config.policy_path.name}.tmp").exists()

    def test_preview_summary_is_deferred(self, service: StorageRetentionService) -> None:
        with pytest.raises(RetentionStateError, match=r"Phase 5\.18 cleanup_service"):
            service.preview_summary()

    def test_make_storage_retention_service_from_web_config(self, tmp_path: Path) -> None:
        cfg = WebConfig(
            web=WebSection(secret_key="x" * 32),
            paths=PathsSection(
                backing_root=tmp_path / "backing",
                state_dir=tmp_path / "state",
                cache_invalidate_script=tmp_path / "invalidate.sh",
            ),
            storage_retention=StorageRetentionSection(
                policy_path=tmp_path / "state" / "retention_policy.json",
                default_max_age_days=90,
                default_target_free_pct=22,
                default_max_archive_size_gb=333,
                default_short_retention_warning_days=12,
            ),
            source_path=None,
        )
        built = make_storage_retention_service(cfg)
        assert isinstance(built, StorageRetentionService)
        assert built.default_policy().max_age_days == 90
        assert built.default_policy().target_free_pct == 22

    def test_make_storage_retention_service_accepts_direct_config(
        self,
        config: StorageRetentionConfig,
    ) -> None:
        built = make_storage_retention_service(config)
        assert built.config.policy_path == config.policy_path

    def test_saved_policy_file_contains_schema_wrapper(
        self,
        config: StorageRetentionConfig,
        service: StorageRetentionService,
    ) -> None:
        service.save_policy(RetentionPolicy(max_age_days=44))
        payload = json.loads(config.policy_path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == 1
        assert payload["policy"]["max_age_days"] == 44
        assert payload["updated_at"].endswith("+00:00")

    def test_update_policy_accepts_integer_bools(self, service: StorageRetentionService) -> None:
        policy = service.update_policy({"keep_saved_clips": 0, "dry_run": 1})
        assert policy.keep_saved_clips is False
        assert policy.dry_run is True

    def test_update_policy_rejects_non_binary_integer_bool(
        self, service: StorageRetentionService
    ) -> None:
        with pytest.raises(RetentionConfigError, match="boolean"):
            service.update_policy({"keep_saved_clips": 2})

    def test_default_policy_uses_configured_scalar_defaults(self, tmp_path: Path) -> None:
        built = StorageRetentionService(
            StorageRetentionConfig(
                policy_path=tmp_path / "state" / "retention_policy.json",
                default_max_age_days=55,
                default_target_free_pct=11,
                default_max_archive_size_gb=77,
                default_short_retention_warning_days=9,
            )
        )
        policy = built.default_policy()
        assert policy.max_age_days == 55
        assert policy.target_free_pct == 11
        assert policy.max_archive_size_gb == 77
        assert policy.short_retention_warning_days == 9
