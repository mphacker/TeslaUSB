# pytest fixture injection.
"""Tests for the tracked license-plate service."""

from __future__ import annotations

import sqlite3
from datetime import UTC
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path
from teslausb_web.config import LicensePlateSection, PathsSection, WebConfig, WebSection
from teslausb_web.services.license_plate_service import (
    LicensePlateConfig,
    LicensePlateError,
    LicensePlateService,
    PlateConfigError,
    PlateDuplicateError,
    PlateNotFoundError,
    make_license_plate_service,
)


@pytest.fixture
def service(tmp_path: Path) -> LicensePlateService:
    return LicensePlateService(
        LicensePlateConfig(
            db_path=tmp_path / "state" / "license_plates.db",
            default_redaction_enabled=False,
            max_plate_length=8,
            max_label_length=24,
            max_notes_length=80,
        )
    )


@pytest.fixture
def populated_service(service: LicensePlateService) -> LicensePlateService:
    service.add_license_plate("abc 123", label="Front gate", notes="Day shift")
    service.add_license_plate("zz-99", label="Night", notes="Escalate if seen")
    return service


class TestLicensePlateConfig:
    def test_resolves_db_path(self, tmp_path: Path) -> None:
        config = LicensePlateConfig(db_path=tmp_path / "state" / "plates.db")
        assert config.db_path.is_absolute()

    @pytest.mark.parametrize(
        ("field", "value"),
        [("max_plate_length", 0), ("max_label_length", 0), ("max_notes_length", 0)],
    )
    def test_rejects_non_positive_lengths(self, tmp_path: Path, field: str, value: int) -> None:
        kwargs = {
            "db_path": tmp_path / "state" / "plates.db",
            "max_plate_length": 8,
            "max_label_length": 24,
            "max_notes_length": 80,
        }
        kwargs[field] = value
        with pytest.raises(PlateConfigError, match=field):
            LicensePlateConfig(**kwargs)


class TestSchemaAndRedaction:
    def test_open_db_creates_schema(self, service: LicensePlateService) -> None:
        with service.open_db() as connection:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        assert "tracked_license_plates" in tables
        assert "plate_redaction_config" in tables
        assert "license_plate_meta" in tables

    def test_default_redaction_disabled(self, service: LicensePlateService) -> None:
        config = service.get_redaction_config()
        assert config.enabled is False
        assert config.updated_at.tzinfo == UTC

    def test_default_redaction_enabled_when_configured(self, tmp_path: Path) -> None:
        seeded = LicensePlateService(
            LicensePlateConfig(
                db_path=tmp_path / "state" / "license_plates.db",
                default_redaction_enabled=True,
            )
        )
        assert seeded.get_redaction_config().enabled is True

    def test_update_redaction_config_persists(self, service: LicensePlateService) -> None:
        before = service.get_redaction_config()
        after = service.update_redaction_config(enabled=True)
        reloaded = service.get_redaction_config()
        assert before.enabled is False
        assert after.enabled is True
        assert reloaded.enabled is True
        assert after.updated_at >= before.updated_at


class TestAddListAndCount:
    def test_add_normalizes_and_stores_plate(self, service: LicensePlateService) -> None:
        plate = service.add_license_plate("ab c-123", label="Guest", notes="Keep an eye on it")
        assert plate.plate_text == "ABC123"
        assert plate.normalized_plate == "ABC123"
        assert plate.label == "Guest"
        assert plate.notes == "Keep an eye on it"
        assert plate.created_at.tzinfo == UTC

    def test_count_tracks_rows(self, service: LicensePlateService) -> None:
        assert service.count_license_plates() == 0
        service.add_license_plate("abc123")
        service.add_license_plate("zz999")
        assert service.count_license_plates() == 2

    def test_list_sorted_by_normalized_plate(self, service: LicensePlateService) -> None:
        service.add_license_plate("zz999")
        service.add_license_plate("aa111")
        service.add_license_plate("mm222")
        assert [plate.plate_text for plate in service.list_license_plates()] == [
            "AA111",
            "MM222",
            "ZZ999",
        ]

    def test_persists_across_service_instances(self, tmp_path: Path) -> None:
        config = LicensePlateConfig(db_path=tmp_path / "state" / "license_plates.db")
        first = LicensePlateService(config)
        second = LicensePlateService(config)
        first.add_license_plate("abc123", label="One")
        first.update_redaction_config(enabled=True)
        assert second.count_license_plates() == 1
        assert second.get_redaction_config().enabled is True

    def test_make_service_from_web_config(self, tmp_path: Path) -> None:
        cfg = WebConfig(
            web=WebSection(secret_key="x" * 32),
            paths=PathsSection(
                backing_root=tmp_path / "backing",
                state_dir=tmp_path / "state",
                cache_invalidate_script=tmp_path / "invalidate.sh",
            ),
            license_plates=LicensePlateSection(
                db_path=tmp_path / "state" / "plates.db",
                default_redaction_enabled=True,
                max_plate_length=9,
                max_label_length=25,
                max_notes_length=81,
            ),
            source_path=None,
        )
        service_from_cfg = make_license_plate_service(cfg)
        assert isinstance(service_from_cfg, LicensePlateService)
        assert service_from_cfg.config.default_redaction_enabled is True
        assert service_from_cfg.config.max_plate_length == 9

    def test_make_service_from_config(self, tmp_path: Path) -> None:
        config = LicensePlateConfig(db_path=tmp_path / "state" / "plates.db")
        service_from_cfg = make_license_plate_service(config)
        assert isinstance(service_from_cfg, LicensePlateService)
        assert service_from_cfg.config.db_path == config.db_path


class TestValidation:
    @pytest.mark.parametrize("raw_plate", ["", "!!!", "   ", "--"])
    def test_rejects_empty_plate_after_normalization(
        self, service: LicensePlateService, raw_plate: str
    ) -> None:
        with pytest.raises(PlateConfigError, match="License plate is required"):
            service.add_license_plate(raw_plate)

    def test_rejects_too_long_normalized_plate(self, service: LicensePlateService) -> None:
        with pytest.raises(PlateConfigError, match="8 characters or fewer"):
            service.add_license_plate("abcdefghi")

    def test_rejects_label_that_is_too_long(self, service: LicensePlateService) -> None:
        with pytest.raises(PlateConfigError, match="label"):
            service.add_license_plate("abc123", label="x" * 25)

    def test_rejects_notes_that_are_too_long(self, service: LicensePlateService) -> None:
        with pytest.raises(PlateConfigError, match="notes"):
            service.add_license_plate("abc123", notes="x" * 81)

    def test_rejects_duplicate_plate_after_normalization(
        self, service: LicensePlateService
    ) -> None:
        service.add_license_plate("abc-123")
        with pytest.raises(PlateDuplicateError, match="ABC123"):
            service.add_license_plate("ABC 123")

    def test_match_plate_rejects_blank_candidate(self, service: LicensePlateService) -> None:
        with pytest.raises(PlateConfigError, match="License plate is required"):
            service.match_plate("   ")

    def test_delete_requires_positive_id(self, service: LicensePlateService) -> None:
        with pytest.raises(PlateConfigError, match="positive integer"):
            service.delete_license_plate(0)

    def test_bulk_delete_requires_ids(self, service: LicensePlateService) -> None:
        with pytest.raises(PlateConfigError, match="At least one"):
            service.bulk_delete([])

    def test_bulk_delete_rejects_non_positive_ids(self, service: LicensePlateService) -> None:
        with pytest.raises(PlateConfigError, match="positive integer"):
            service.bulk_delete([1, 0])


class TestUpdateDeleteAndBulkDelete:
    def test_update_changes_existing_plate(self, populated_service: LicensePlateService) -> None:
        original = populated_service.list_license_plates()[0]
        updated = populated_service.update_license_plate(
            original.id,
            plate_text="abc999",
            label="Updated",
            notes="Reviewed",
        )
        assert updated.id == original.id
        assert updated.plate_text == "ABC999"
        assert updated.label == "Updated"
        assert updated.notes == "Reviewed"
        assert updated.updated_at >= original.updated_at

    def test_update_rejects_duplicate_target(self, populated_service: LicensePlateService) -> None:
        first, second = populated_service.list_license_plates()
        with pytest.raises(PlateDuplicateError, match=second.plate_text):
            populated_service.update_license_plate(first.id, plate_text=second.plate_text)

    def test_update_missing_plate_raises(self, service: LicensePlateService) -> None:
        with pytest.raises(PlateNotFoundError, match="999"):
            service.update_license_plate(999, plate_text="abc123")

    def test_delete_existing_plate(self, populated_service: LicensePlateService) -> None:
        plate = populated_service.list_license_plates()[0]
        assert populated_service.delete_license_plate(plate.id) is True
        assert populated_service.count_license_plates() == 1

    def test_delete_missing_plate_raises(self, service: LicensePlateService) -> None:
        with pytest.raises(PlateNotFoundError, match="999"):
            service.delete_license_plate(999)

    def test_bulk_delete_deletes_unique_ids_once(
        self, populated_service: LicensePlateService
    ) -> None:
        plates = populated_service.list_license_plates()
        result = populated_service.bulk_delete([plates[0].id, plates[0].id, plates[1].id])
        assert result.deleted_count == 2
        assert result.missing_ids == ()
        assert populated_service.count_license_plates() == 0

    def test_bulk_delete_reports_missing_ids(self, populated_service: LicensePlateService) -> None:
        plate = populated_service.list_license_plates()[0]
        result = populated_service.bulk_delete([plate.id, 999])
        assert result.deleted_count == 1
        assert result.missing_ids == (999,)
        assert "missing IDs: 999" in result.message

    def test_bulk_delete_all_missing_returns_zero(
        self, populated_service: LicensePlateService
    ) -> None:
        result = populated_service.bulk_delete([999, 1000])
        assert result.deleted_count == 0
        assert result.missing_ids == (999, 1000)
        assert result.success is False


class TestMatchPlate:
    def test_match_plate_finds_normalized_plate(
        self, populated_service: LicensePlateService
    ) -> None:
        match = populated_service.match_plate("abc-123")
        assert match.is_match is True
        assert match.normalized_candidate == "ABC123"
        assert match.matched_plate is not None
        assert match.matched_plate.plate_text == "ABC123"

    def test_match_plate_returns_no_match(self, populated_service: LicensePlateService) -> None:
        match = populated_service.match_plate("new-plate")
        assert match.is_match is False
        assert match.matched_plate is None
        assert match.normalized_candidate == "NEWPLATE"


class TestDatabaseFailureHandling:
    def test_open_db_wraps_connect_errors(self, tmp_path: Path) -> None:
        service = LicensePlateService(LicensePlateConfig(db_path=tmp_path / "state" / "plates.db"))
        with (
            patch(
                "teslausb_web.services.license_plate_service.sqlite3.connect",
                side_effect=sqlite3.Error("boom"),
            ),
            pytest.raises(LicensePlateError, match="Failed to open"),
            service.open_db(),
        ):
            pass

    def test_query_errors_roll_back_and_raise(self, service: LicensePlateService) -> None:
        with (
            patch.object(
                service,
                "_fetch_rows",
                side_effect=sqlite3.Error("boom"),
            ),
            pytest.raises(LicensePlateError, match="database error"),
        ):
            service.list_license_plates()

    def test_schema_initialization_recreates_redaction_row(
        self, service: LicensePlateService
    ) -> None:
        with service.open_db() as connection:
            connection.execute("DELETE FROM plate_redaction_config WHERE config_id = 1")
            connection.commit()
        assert service.get_redaction_config().enabled is False

    def test_database_meta_tracks_schema_version(self, service: LicensePlateService) -> None:
        with service.open_db() as connection:
            row = connection.execute(
                "SELECT value FROM license_plate_meta WHERE key = 'schema_version'"
            ).fetchone()
        assert row is not None
        assert str(row[0]) == "1"
