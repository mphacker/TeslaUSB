"""Tests for the advanced system-settings service."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from teslausb_web.config import (
    FeaturesSection,
    PathsSection,
    SystemSettingsSection,
    WebConfig,
    WebSection,
)
from teslausb_web.services.system_settings_service import (
    SystemSettings,
    SystemSettingsConfig,
    SystemSettingsConfigError,
    SystemSettingsService,
    SystemSettingsStateError,
    make_system_settings_service,
)


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "system_settings.json"


@pytest.fixture
def service(state_path: Path, tmp_path: Path) -> SystemSettingsService:
    return SystemSettingsService(
        SystemSettingsConfig(
            state_path=state_path,
            default_samba_enabled=False,
            default_log_level="warning",
        ),
        ipc_socket_path=tmp_path / "worker.sock",
    )


class TestSystemSettingsConfig:
    def test_rejects_relative_state_path(self) -> None:
        with pytest.raises(SystemSettingsConfigError, match="state_path"):
            SystemSettingsConfig(state_path=Path("relative.json"))

    def test_rejects_unknown_default_log_level(self, state_path: Path) -> None:
        with pytest.raises(SystemSettingsConfigError, match="default_log_level"):
            SystemSettingsConfig(state_path=state_path, default_log_level="verbose")

    def test_normalizes_default_log_level(self, state_path: Path) -> None:
        config = SystemSettingsConfig(state_path=state_path, default_log_level="debug")
        assert config.default_log_level == "DEBUG"


class TestDefaultsAndFactory:
    def test_default_settings_use_config_fallbacks(
        self,
        service: SystemSettingsService,
        tmp_path: Path,
    ) -> None:
        settings = service.default_settings()
        assert settings == SystemSettings(
            samba_enabled=False,
            log_level="WARNING",
            ipc_socket_path=str(tmp_path / "worker.sock"),
        )

    def test_get_settings_returns_defaults_when_state_missing(
        self,
        service: SystemSettingsService,
    ) -> None:
        settings = service.get_settings()
        assert settings.samba_enabled is False
        assert settings.log_level == "WARNING"

    def test_make_service_reads_web_config(self, tmp_path: Path) -> None:
        cfg = WebConfig(
            web=WebSection(secret_key="x" * 32),
            paths=PathsSection(
                backing_root=tmp_path / "backing",
                state_dir=tmp_path / "state",
                ipc_socket=tmp_path / "ipc" / "worker.sock",
                cache_invalidate_script=tmp_path / "invalidate.sh",
            ),
            features=FeaturesSection(samba_enabled=True),
            system_settings=SystemSettingsSection(
                state_path=tmp_path / "state" / "advanced.json",
                default_log_level="ERROR",
            ),
            source_path=None,
        )
        built = make_system_settings_service(cfg)
        assert built.get_settings().samba_enabled is True
        assert built.get_settings().log_level == "ERROR"
        assert built.get_settings().ipc_socket_path == str(tmp_path / "ipc" / "worker.sock")


class TestPersistence:
    def test_save_settings_writes_atomic_json(
        self,
        service: SystemSettingsService,
        state_path: Path,
    ) -> None:
        saved = service.save_settings(
            SystemSettings(samba_enabled=True, log_level="ERROR", ipc_socket_path="/run/test.sock")
        )
        assert saved.samba_enabled is True
        raw = state_path.read_text(encoding="utf-8")
        assert raw.endswith("\n")
        payload = json.loads(raw)
        assert payload == {
            "log_level": "ERROR",
            "samba_enabled": True,
            "schema_version": 1,
        }

    def test_update_settings_persists_both_fields(
        self,
        service: SystemSettingsService,
    ) -> None:
        updated = service.update_settings({"samba_enabled": True, "log_level": "debug"})
        assert updated.samba_enabled is True
        assert updated.log_level == "DEBUG"

    def test_get_settings_reads_persisted_state(
        self,
        service: SystemSettingsService,
    ) -> None:
        service.update_settings({"samba_enabled": True, "log_level": "ERROR"})
        loaded = service.get_settings()
        assert loaded.samba_enabled is True
        assert loaded.log_level == "ERROR"

    def test_partial_update_preserves_existing_values(
        self,
        service: SystemSettingsService,
    ) -> None:
        service.update_settings({"samba_enabled": True, "log_level": "ERROR"})
        updated = service.update_settings({"log_level": "INFO"})
        assert updated.samba_enabled is True
        assert updated.log_level == "INFO"

    def test_update_creates_parent_directory(
        self,
        service: SystemSettingsService,
        state_path: Path,
    ) -> None:
        service.update_settings({"samba_enabled": True})
        assert state_path.parent.is_dir()
        assert state_path.is_file()


class TestValidationFailures:
    def test_invalid_json_raises_state_error(
        self,
        service: SystemSettingsService,
        state_path: Path,
    ) -> None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("not json", encoding="utf-8")
        with pytest.raises(SystemSettingsStateError, match="Failed to parse"):
            service.get_settings()

    def test_non_object_state_raises_state_error(
        self,
        service: SystemSettingsService,
        state_path: Path,
    ) -> None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("[]\n", encoding="utf-8")
        with pytest.raises(SystemSettingsStateError, match="JSON object"):
            service.get_settings()

    def test_unknown_schema_version_raises_state_error(
        self,
        service: SystemSettingsService,
        state_path: Path,
    ) -> None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"schema_version": 9, "samba_enabled": True, "log_level": "INFO"}),
            encoding="utf-8",
        )
        with pytest.raises(SystemSettingsStateError, match="schema version"):
            service.get_settings()

    def test_invalid_persisted_bool_raises_config_error(
        self,
        service: SystemSettingsService,
        state_path: Path,
    ) -> None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"schema_version": 1, "samba_enabled": "later", "log_level": "INFO"}),
            encoding="utf-8",
        )
        with pytest.raises(SystemSettingsConfigError, match="samba_enabled"):
            service.get_settings()

    def test_invalid_persisted_log_level_raises_config_error(
        self,
        service: SystemSettingsService,
        state_path: Path,
    ) -> None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"schema_version": 1, "samba_enabled": True, "log_level": "TRACE"}),
            encoding="utf-8",
        )
        with pytest.raises(SystemSettingsConfigError, match="log_level"):
            service.get_settings()

    def test_invalid_update_bool_raises_config_error(self, service: SystemSettingsService) -> None:
        with pytest.raises(SystemSettingsConfigError, match="samba_enabled"):
            service.update_settings({"samba_enabled": object()})

    def test_invalid_update_log_level_raises_config_error(
        self,
        service: SystemSettingsService,
    ) -> None:
        with pytest.raises(SystemSettingsConfigError, match="log_level"):
            service.update_settings({"log_level": "trace"})


class TestHelpers:
    def test_bool_coercion_accepts_strings(self, service: SystemSettingsService) -> None:
        assert service.update_settings({"samba_enabled": "yes"}).samba_enabled is True
        assert service.update_settings({"samba_enabled": "off"}).samba_enabled is False

    def test_bool_coercion_accepts_ints(self, service: SystemSettingsService) -> None:
        assert service.update_settings({"samba_enabled": 1}).samba_enabled is True
        assert service.update_settings({"samba_enabled": 0}).samba_enabled is False

    def test_serialize_settings_payload(self, service: SystemSettingsService) -> None:
        payload = service.serialize_settings(service.default_settings())
        assert payload["samba_enabled"] is False
        assert payload["log_level"] == "WARNING"
        state_path = payload["state_path"]
        assert isinstance(state_path, str)
        assert state_path.endswith("system_settings.json")

    def test_config_snapshot_is_sanitized(self, service: SystemSettingsService) -> None:
        snapshot = service.config_snapshot(service.default_settings())
        assert "secret_key" not in json.dumps(snapshot, sort_keys=True)
        assert snapshot["defaults"] == {"log_level": "WARNING", "samba_enabled": False}

    def test_log_levels_returns_supported_choices(self, service: SystemSettingsService) -> None:
        assert service.log_levels() == ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

    def test_subscribe_receives_saved_settings(self, service: SystemSettingsService) -> None:
        observed: list[SystemSettings] = []
        unsubscribe = service.subscribe(observed.append)
        service.update_settings({"samba_enabled": True, "log_level": "ERROR"})
        unsubscribe()
        assert observed[-1].samba_enabled is True
        assert observed[-1].log_level == "ERROR"

    def test_unsubscribe_removes_callback(self, service: SystemSettingsService) -> None:
        observed: list[SystemSettings] = []
        unsubscribe = service.subscribe(observed.append)
        unsubscribe()
        service.update_settings({"samba_enabled": True})
        assert observed == []
