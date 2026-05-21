# pytest fixture injection.
"""Tests for the storage-retention blueprint."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from teslausb_web.app import create_app
from teslausb_web.blueprints.storage_retention import (
    _get_service,
    _invalidate_caches,
    _request_bool,
    _request_policy_update,
    _request_text,
    _serialize_policy,
)
from teslausb_web.config import PathsSection, StorageRetentionSection, WebConfig, WebSection
from teslausb_web.services.storage_retention_service import StorageRetentionService

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient
    from teslausb_web.services.cache_invalidation import CacheInvalidator


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32, max_upload_mb=8, max_chunk_mb=1),
        paths=PathsSection(
            backing_root=tmp_path / "backing",
            state_dir=tmp_path / "state",
            cache_invalidate_script=tmp_path / "invalidate.sh",
        ),
        storage_retention=StorageRetentionSection(
            policy_path=tmp_path / "state" / "retention_policy.json"
        ),
        source_path=None,
    )
    flask_app = create_app(cfg)
    flask_app.testing = True
    return flask_app


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def service(app: Flask) -> StorageRetentionService:
    resolved = app.extensions["storage_retention_service"]
    assert isinstance(resolved, StorageRetentionService)
    return resolved


@pytest.fixture
def invalidator(app: Flask) -> CacheInvalidator:
    from teslausb_web.services.cache_invalidation import CacheInvalidator as RuntimeCacheInvalidator

    resolved = app.extensions["cache_invalidator"]
    assert isinstance(resolved, RuntimeCacheInvalidator)
    return resolved


class TestHelpers:
    def test_app_registers_blueprint_and_service(self, app: Flask) -> None:
        assert "storage_retention" in app.blueprints
        assert isinstance(app.extensions["storage_retention_service"], StorageRetentionService)

    def test_get_service_rejects_misconfigured_extension(self, app: Flask) -> None:
        with app.app_context():
            original = app.extensions["storage_retention_service"]
            app.extensions["storage_retention_service"] = object()
            with pytest.raises(RuntimeError, match="storage_retention_service"):
                _get_service()
            app.extensions["storage_retention_service"] = original

    def test_invalidate_caches_is_noop_without_extension(self, app: Flask) -> None:
        invalidator = app.extensions.pop("cache_invalidator")
        _invalidate_caches(app)
        app.extensions["cache_invalidator"] = invalidator

    def test_request_helpers_accept_json_and_form(self, app: Flask) -> None:
        with app.test_request_context(
            "/cleanup/settings",
            method="POST",
            json={"keep_saved_clips": True, "max_age_days": 45},
        ):
            assert _request_bool("keep_saved_clips") is True
            assert _request_text("max_age_days") == "45"
        with app.test_request_context(
            "/cleanup/settings",
            method="POST",
            data={"keep_saved_clips": "on", "max_age_days": "55", "partition": "part1"},
        ):
            payload = _request_policy_update()
            assert payload["keep_saved_clips"] is True
            assert payload["max_age_days"] == "55"

    def test_request_bool_rejects_invalid_type(self, app: Flask) -> None:
        with (
            app.test_request_context(
                "/cleanup/settings",
                method="POST",
                json={"keep_saved_clips": ["bad"]},
            ),
            pytest.raises(Exception, match="boolean"),
        ):
            _request_bool("keep_saved_clips")

    def test_serialize_policy_uses_service_serializer(
        self,
        app: Flask,
        service: StorageRetentionService,
    ) -> None:
        with app.app_context():
            payload = _serialize_policy(service.default_policy())
        assert payload["max_age_days"] == 30
        assert payload["keep_event_clips"] is True


class TestIndexRoutes:
    def test_index_route_renders_template(self, client: FlaskClient) -> None:
        response = client.get("/cleanup/")
        html = response.get_data(as_text=True)
        assert response.status_code == HTTPStatus.OK
        assert "Storage &amp; Retention" in html
        assert "Preview cleanup" in html
        assert "current_mode" not in html
        assert "quick_edit" not in html
        assert "mode_control" not in html
        assert "cdn.jsdelivr.net" not in html
        assert "unpkg.com" not in html
        assert "#" not in html.split("<style>", 1)[1].split("</style>", 1)[0]
        assert "<svg" in html

    def test_settings_alias_renders_template(self, client: FlaskClient) -> None:
        response = client.get("/cleanup/settings")
        assert response.status_code == HTTPStatus.OK
        assert "Storage &amp; Retention" in response.get_data(as_text=True)

    def test_index_route_does_not_schedule_cache(
        self,
        client: FlaskClient,
        invalidator: CacheInvalidator,
    ) -> None:
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.get("/cleanup/")
        assert response.status_code == HTTPStatus.OK
        schedule_mock.assert_not_called()

    def test_template_file_exists(self) -> None:
        import teslausb_web

        template = Path(teslausb_web.__file__).parent / "templates" / "cleanup_settings.html"
        assert template.is_file()
        assert template.read_text(encoding="utf-8").strip()


class TestJsonPolicyRoutes:
    def test_current_policy_json_endpoint(self, client: FlaskClient) -> None:
        response = client.get("/api/cleanup/policy")
        payload = response.get_json()
        assert response.status_code == HTTPStatus.OK
        assert payload["success"] is True
        assert payload["policy"]["max_age_days"] == 30
        assert payload["preview_available"] is False
        assert len(payload["rows"]) == 5

    def test_status_alias_returns_same_shape(self, client: FlaskClient) -> None:
        response = client.get("/api/cleanup/status")
        payload = response.get_json()
        assert response.status_code == HTTPStatus.OK
        assert payload["success"] is True
        assert payload["ranges"]["target_free_pct"] == {"min": 5, "max": 50}

    def test_api_post_saves_policy_and_invalidates_cache(
        self,
        client: FlaskClient,
        service: StorageRetentionService,
        invalidator: CacheInvalidator,
    ) -> None:
        _ = service
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/api/cleanup/policy",
                json={
                    "max_age_days": 45,
                    "target_free_pct": 15,
                    "max_archive_size_gb": 100,
                    "short_retention_warning_days": 8,
                    "keep_saved_clips": False,
                    "keep_event_clips": False,
                    "keep_recent_clips": True,
                    "keep_encrypted_clips": True,
                    "keep_archived_clips": False,
                    "dry_run": False,
                    "recent_clips_days": 44,
                    "saved_clips_days": 90,
                    "event_clips_days": 120,
                    "encrypted_clips_days": 30,
                    "archived_clips_days": 20,
                },
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        payload = response.get_json()
        assert response.status_code == HTTPStatus.OK
        assert payload["success"] is True
        assert payload["policy"]["keep_saved_clips"] is False
        assert payload["policy"]["dry_run"] is False
        schedule_mock.assert_called_once()

    def test_api_post_rejects_invalid_payload(
        self,
        client: FlaskClient,
        invalidator: CacheInvalidator,
    ) -> None:
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/api/cleanup/policy",
                json={"target_free_pct": 99},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        assert response.status_code == HTTPStatus.BAD_REQUEST
        assert response.get_json()["success"] is False
        schedule_mock.assert_not_called()


class TestFormSaveRoute:
    def test_form_post_redirects_and_invalidates_cache(
        self,
        client: FlaskClient,
        invalidator: CacheInvalidator,
        service: StorageRetentionService,
    ) -> None:
        _ = service
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/cleanup/settings?_=7",
                data={
                    "partition": "part1",
                    "max_age_days": "50",
                    "target_free_pct": "20",
                    "max_archive_size_gb": "300",
                    "short_retention_warning_days": "6",
                    "keep_recent_clips": "on",
                    "dry_run": "on",
                    "recent_clips_days": "45",
                    "saved_clips_days": "80",
                    "event_clips_days": "90",
                    "encrypted_clips_days": "35",
                    "archived_clips_days": "25",
                },
            )
        assert response.status_code == HTTPStatus.FOUND
        assert response.headers["Location"] == "/cleanup/settings?_=7"
        schedule_mock.assert_called_once()

    def test_form_post_persists_updated_policy(
        self,
        client: FlaskClient,
        service: StorageRetentionService,
    ) -> None:
        client.post(
            "/cleanup/settings",
            data={
                "max_age_days": "61",
                "target_free_pct": "18",
                "max_archive_size_gb": "80",
                "short_retention_warning_days": "9",
                "keep_saved_clips": "on",
                "keep_event_clips": "on",
                "keep_encrypted_clips": "on",
                "recent_clips_days": "61",
                "saved_clips_days": "91",
                "event_clips_days": "121",
                "encrypted_clips_days": "31",
                "archived_clips_days": "11",
            },
        )
        policy = service.get_policy()
        assert policy.max_age_days == 61
        assert policy.keep_recent_clips is False
        assert policy.keep_saved_clips is True
        assert policy.archived_clips_days == 11


class TestPreviewRoute:
    def test_preview_json_endpoint_returns_deferred_error(self, client: FlaskClient) -> None:
        response = client.get(
            "/api/cleanup/preview",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        payload = response.get_json()
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert payload["success"] is False
        assert "Phase 5.18 cleanup_service" in payload["error"]

    def test_preview_html_endpoint_redirects_back(self, client: FlaskClient) -> None:
        response = client.get("/cleanup/preview")
        assert response.status_code == HTTPStatus.FOUND
        assert response.headers["Location"] == "/cleanup/settings?_=0"

    @pytest.mark.skip(reason="preview deferred to Phase 5.18 cleanup_service")
    def test_preview_dry_run_summary_requires_cleanup_engine(self) -> None:
        raise AssertionError("unreachable")
