# ruff: noqa: ANN001  # pytest fixture injection.
"""Tests for the tracked license-plates blueprint."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient
    from teslausb_web.services.cache_invalidation import CacheInvalidator

from teslausb_web.app import create_app
from teslausb_web.blueprints.license_plates import (
    _get_service,
    _invalidate_caches,
    _request_bool,
    _request_int_list,
    _serialize_bulk_result,
    _serialize_match,
    _serialize_plate,
    _serialize_redaction,
)
from teslausb_web.config import LicensePlateSection, PathsSection, WebConfig, WebSection
from teslausb_web.services.license_plate_service import (
    LicensePlateService,
    PlateBulkOperationResult,
)


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32, max_upload_mb=8, max_chunk_mb=1),
        paths=PathsSection(
            backing_root=tmp_path / "backing",
            state_dir=tmp_path / "state",
            cache_invalidate_script=tmp_path / "invalidate.sh",
        ),
        license_plates=LicensePlateSection(
            db_path=tmp_path / "state" / "license_plates.db",
            default_redaction_enabled=False,
            max_plate_length=8,
            max_label_length=24,
            max_notes_length=80,
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
def service(app) -> LicensePlateService:
    resolved = app.extensions["license_plate_service"]
    assert isinstance(resolved, LicensePlateService)
    return resolved


@pytest.fixture
def invalidator(app: Flask) -> CacheInvalidator:
    return app.extensions["cache_invalidator"]


@pytest.fixture
def seeded_service(service: LicensePlateService) -> LicensePlateService:
    service.add_license_plate("abc123", label="Front gate", notes="Watch list")
    service.add_license_plate("zz999", label="Night", notes="Review manually")
    return service


class TestHelpers:
    def test_app_registers_blueprint_and_service(self, app) -> None:
        assert "license_plates" in app.blueprints
        assert isinstance(app.extensions["license_plate_service"], LicensePlateService)

    def test_get_service_rejects_misconfigured_extension(self, app) -> None:
        with app.app_context():
            original = app.extensions["license_plate_service"]
            app.extensions["license_plate_service"] = object()
            with pytest.raises(RuntimeError, match="license_plate_service"):
                _get_service()
            app.extensions["license_plate_service"] = original

    def test_invalidate_caches_is_noop_without_extension(self, app) -> None:
        invalidator = app.extensions.pop("cache_invalidator")
        _invalidate_caches(app)
        app.extensions["cache_invalidator"] = invalidator

    def test_request_helpers_accept_json_and_form(self, app) -> None:
        with app.test_request_context(
            "/license_plates/bulk_delete", method="POST", json={"plate_ids": [1, "2"]}
        ):
            assert _request_int_list("plate_ids") == [1, 2]
        with app.test_request_context(
            "/license_plates/bulk_delete",
            method="POST",
            data={"plate_ids": ["3", "4"]},
        ):
            assert _request_int_list("plate_ids") == [3, 4]
        with app.test_request_context(
            "/license_plates/redaction", method="POST", json={"enabled": True}
        ):
            assert _request_bool("enabled") is True
        with app.test_request_context("/license_plates/redaction", method="POST", data={}):
            assert _request_bool("enabled") is False

    def test_request_int_list_rejects_invalid_items(self, app: Flask) -> None:
        with (
            app.test_request_context(
                "/license_plates/bulk_delete", method="POST", json={"plate_ids": ["x"]}
            ),
            pytest.raises(Exception, match="integers"),
        ):
            _request_int_list("plate_ids")

    def test_serialize_helpers_shape_payloads(self, service: LicensePlateService) -> None:
        plate = service.add_license_plate("abc123", label="Front gate", notes="Watch list")
        redaction = service.update_redaction_config(enabled=True)
        match = service.match_plate("abc123")
        bulk = PlateBulkOperationResult(
            requested_count=2,
            deleted_count=1,
            missing_ids=(9,),
            message="Deleted 1 tracked plate(s); missing IDs: 9",
        )
        assert _serialize_plate(plate)["plate_text"] == "ABC123"
        assert _serialize_redaction(redaction)["enabled"] is True
        assert _serialize_match(match)["is_match"] is True
        assert _serialize_bulk_result(bulk)["missing_ids"] == [9]


class TestIndexRoute:
    def test_index_route_renders_template(
        self, client: FlaskClient, seeded_service: LicensePlateService
    ) -> None:
        _ = seeded_service
        response = client.get("/license_plates/")
        html = response.get_data(as_text=True)
        assert response.status_code == HTTPStatus.OK
        assert "License Plates" in html
        assert "license_plates.js" in html
        assert "Front gate" in html
        assert "current_mode" not in html
        assert "quick_edit" not in html
        assert "mode_control" not in html
        assert "cdn.jsdelivr.net" not in html
        assert "unpkg.com" not in html
        assert "#" not in html.split("<style>", 1)[1].split("</style>", 1)[0]
        assert "<svg" in html

    def test_index_route_does_not_schedule_cache(
        self,
        client: FlaskClient,
        seeded_service: LicensePlateService,
        invalidator: CacheInvalidator,
    ) -> None:
        _ = seeded_service
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.get("/license_plates/")
        assert response.status_code == HTTPStatus.OK
        schedule_mock.assert_not_called()

    def test_template_file_exists(self) -> None:
        import teslausb_web

        template = Path(teslausb_web.__file__).parent / "templates" / "license_plates.html"
        assert template.is_file()
        assert template.read_text(encoding="utf-8").strip()


class TestAddRoute:
    def test_add_route_redirects_on_success(self, client, invalidator) -> None:
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/license_plates/add?_=7",
                data={"plate_text": "abc123", "label": "Front gate", "notes": "Watch list"},
            )
        assert response.status_code == HTTPStatus.FOUND
        assert response.headers["Location"] == "/license_plates/?_=7"
        schedule_mock.assert_called_once()

    def test_add_route_returns_json_on_success(self, client, invalidator) -> None:
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/license_plates/add",
                json={"plate_text": "abc123", "label": "Front gate", "notes": "Watch list"},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        payload = response.get_json()
        assert response.status_code == HTTPStatus.CREATED
        assert payload["success"] is True
        assert payload["plate"]["plate_text"] == "ABC123"
        schedule_mock.assert_called_once()

    def test_add_route_rejects_duplicate(
        self, client, service: LicensePlateService, invalidator
    ) -> None:
        service.add_license_plate("abc123")
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/license_plates/add",
                json={"plate_text": "ABC 123"},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        assert response.status_code == HTTPStatus.CONFLICT
        assert response.get_json()["success"] is False
        schedule_mock.assert_not_called()

    def test_add_route_rejects_invalid_payload(self, client, invalidator) -> None:
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/license_plates/add",
                json={"plate_text": "!!!"},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        assert response.status_code == HTTPStatus.BAD_REQUEST
        schedule_mock.assert_not_called()


class TestUpdateAndDeleteRoutes:
    def test_update_route_updates_plate(
        self, client, seeded_service: LicensePlateService, invalidator
    ) -> None:
        plate = seeded_service.list_license_plates()[0]
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                f"/license_plates/update/{plate.id}",
                json={"plate_text": "abc999", "label": "Updated", "notes": "Reviewed"},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        payload = response.get_json()
        assert response.status_code == HTTPStatus.OK
        assert payload["plate"]["plate_text"] == "ABC999"
        assert payload["plate"]["label"] == "Updated"
        schedule_mock.assert_called_once()

    def test_update_route_returns_not_found(self, client, invalidator) -> None:
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/license_plates/update/999",
                json={"plate_text": "abc123"},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        assert response.status_code == HTTPStatus.NOT_FOUND
        schedule_mock.assert_not_called()

    def test_delete_route_accepts_partition_param(
        self, client, seeded_service: LicensePlateService, invalidator
    ) -> None:
        plate = seeded_service.list_license_plates()[0]
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                f"/license_plates/delete/legacy-partition/{plate.id}",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        assert response.status_code == HTTPStatus.OK
        assert seeded_service.count_license_plates() == 1
        schedule_mock.assert_called_once()

    def test_delete_route_returns_not_found(self, client, invalidator) -> None:
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/license_plates/delete/part2/999",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        assert response.status_code == HTTPStatus.NOT_FOUND
        schedule_mock.assert_not_called()


class TestBulkDeleteRoute:
    def test_bulk_delete_route_deletes_selected_ids(
        self, client, seeded_service: LicensePlateService, invalidator
    ) -> None:
        plates = seeded_service.list_license_plates()
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/license_plates/bulk_delete",
                json={"plate_ids": [plates[0].id, plates[1].id]},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        payload = response.get_json()
        assert response.status_code == HTTPStatus.OK
        assert payload["bulk_result"]["deleted_count"] == 2
        assert seeded_service.count_license_plates() == 0
        schedule_mock.assert_called_once()

    def test_bulk_delete_route_partial_success_still_invalidates_once(
        self, client, seeded_service: LicensePlateService, invalidator
    ) -> None:
        plate = seeded_service.list_license_plates()[0]
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/license_plates/bulk_delete",
                json={"plate_ids": [plate.id, 999]},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        payload = response.get_json()
        assert response.status_code == HTTPStatus.OK
        assert payload["bulk_result"]["deleted_count"] == 1
        assert payload["bulk_result"]["missing_ids"] == [999]
        schedule_mock.assert_called_once()

    def test_bulk_delete_route_rejects_empty_selection(self, client, invalidator) -> None:
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/license_plates/bulk_delete",
                json={"plate_ids": []},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        assert response.status_code == HTTPStatus.BAD_REQUEST
        schedule_mock.assert_not_called()

    def test_bulk_delete_route_returns_not_found_when_all_missing(
        self, client, invalidator
    ) -> None:
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/license_plates/bulk_delete",
                json={"plate_ids": [999]},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        assert response.status_code == HTTPStatus.NOT_FOUND
        schedule_mock.assert_not_called()


class TestRedactionRoute:
    def test_redaction_route_updates_setting_from_form(self, client, invalidator) -> None:
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post("/license_plates/redaction?_=9", data={"enabled": "on"})
        assert response.status_code == HTTPStatus.FOUND
        assert response.headers["Location"] == "/license_plates/?_=9"
        schedule_mock.assert_called_once()

    def test_redaction_route_updates_setting_from_json(self, client, invalidator) -> None:
        with patch.object(invalidator, "schedule") as schedule_mock:
            response = client.post(
                "/license_plates/redaction",
                json={"enabled": False},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        payload = response.get_json()
        assert response.status_code == HTTPStatus.OK
        assert payload["redaction_config"]["enabled"] is False
        schedule_mock.assert_called_once()


class TestMatchRoute:
    def test_match_route_returns_match_payload(
        self, client: FlaskClient, seeded_service: LicensePlateService
    ) -> None:
        _ = seeded_service
        response = client.post(
            "/license_plates/match",
            json={"candidate": "abc-123"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        payload = response.get_json()
        assert response.status_code == HTTPStatus.OK
        assert payload["match"]["is_match"] is True
        assert payload["match"]["matched_plate"]["plate_text"] == "ABC123"

    def test_match_route_returns_no_match_payload(
        self, client: FlaskClient, seeded_service: LicensePlateService
    ) -> None:
        _ = seeded_service
        response = client.post(
            "/license_plates/match",
            json={"candidate": "new-plate"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        payload = response.get_json()
        assert response.status_code == HTTPStatus.OK
        assert payload["match"]["is_match"] is False
        assert payload["match"]["normalized_candidate"] == "NEWPLATE"

    def test_match_route_rejects_blank_candidate(self, client) -> None:
        response = client.post(
            "/license_plates/match",
            json={"candidate": "   "},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert response.status_code == HTTPStatus.BAD_REQUEST
        assert response.get_json()["success"] is False


class TestPhotoRoutes:
    """Photo plate upload/download/delete routes (Tesla custom-background PNGs)."""

    def test_upload_plate_with_no_file_redirects_with_error(self, client: FlaskClient) -> None:
        response = client.post("/license_plates/upload", data={"plate_region": "na"})
        assert response.status_code == HTTPStatus.FOUND

    def test_upload_multiple_with_no_files_returns_json_error(self, client: FlaskClient) -> None:
        response = client.post(
            "/license_plates/upload_multiple",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert response.status_code == HTTPStatus.BAD_REQUEST
        payload = response.get_json()
        assert payload is not None
        assert payload["success"] is False
        assert "no files selected" in payload.get("message", "").lower()

    def test_download_missing_plate_returns_404(self, client: FlaskClient) -> None:
        response = client.get("/license_plates/download/LightShow/missing.png")
        assert response.status_code == HTTPStatus.NOT_FOUND

    def test_delete_missing_plate_redirects(self, client: FlaskClient) -> None:
        response = client.post("/license_plates/delete_image/LightShow/missing.png")
        assert response.status_code == HTTPStatus.FOUND


class TestPhotoContextVars:
    """Index route must expose photo spec context so the template can render."""

    def test_index_includes_photo_spec_context(self, client: FlaskClient) -> None:
        html = client.get("/license_plates/").get_data(as_text=True)
        # max_file_size (524288) and plate dimensions are embedded as data-* attrs
        assert "524288" in html
        assert "420" in html  # plate_width_na
        assert "492" in html  # plate_width_eu

    def test_template_body_over_25kb(self, client: FlaskClient) -> None:
        data = client.get("/license_plates/").get_data(as_text=True)
        assert len(data) > 25_000, f"Template too short: {len(data)} bytes"
