from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest
from teslausb_web.app import create_app
from teslausb_web.blueprints import cleanup as cleanup_module
from teslausb_web.blueprints.cleanup import (
    _get_service,
    _request_bool,
    _serialize_orphans,
    _serialize_preview,
    _serialize_run,
)
from teslausb_web.config import PathsSection, StorageRetentionSection, WebConfig, WebSection
from teslausb_web.services.cleanup.service import (
    CleanupConfigError,
    CleanupError,
    CleanupPreview,
    CleanupReport,
    CleanupRun,
    CleanupRunStatus,
    OrphanScan,
)

if TYPE_CHECKING:
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient


def _sample_preview() -> CleanupPreview:
    return CleanupPreview(
        counts_by_category={
            "recent": 1,
            "saved": 0,
            "event": 0,
            "encrypted": 0,
            "archived": 0,
        },
        bytes_total=1024,
        sample_paths=("RecentClips/sample-front.mp4",),
        generated_at=datetime(2024, 1, 2, tzinfo=UTC),
        current_free_pct=10.0,
        projected_free_pct=15.0,
        current_free_bytes=100,
        current_used_bytes=900,
        total_capacity_bytes=1000,
        bytes_by_category={
            "recent": 1024,
            "saved": 0,
            "event": 0,
            "encrypted": 0,
            "archived": 0,
        },
        candidate_count=1,
        protected_count=2,
        orphan_scan=OrphanScan(
            db_only_paths=("RecentClips/db-only-front.mp4",),
            fs_only_paths=("RecentClips/fs-only-front.mp4",),
            total_bytes_recoverable=2048,
        ),
    )


def _sample_run(*, status: str = "completed", active: bool = False) -> CleanupRunStatus:
    run = CleanupRun(
        run_id="run-abc",
        status=status,
        action="cleanup",
        dry_run=True,
        started_at=datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
        finished_at=None if active else datetime(2024, 1, 2, 3, 5, 5, tzinfo=UTC),
        deleted_count=2,
        deleted_bytes=2048,
        errors=("warn",) if status == "failed" else (),
        policy_snapshot={"max_age_days": 30},
        counts_by_category={"recent": 2},
        sample_paths=("RecentClips/sample-front.mp4",),
        generated_at=datetime(2024, 1, 2, 3, 4, 0, tzinfo=UTC),
        current_path="RecentClips/sample-front.mp4" if active else None,
        total_candidates=3,
        processed_candidates=1 if active else 3,
        orphan_scan=OrphanScan(
            db_only_paths=("RecentClips/db-only-front.mp4",),
            fs_only_paths=("RecentClips/fs-only-front.mp4",),
            total_bytes_recoverable=2048,
        ),
    )
    return CleanupRunStatus(run=run, active=active)


class StubCleanupService:
    def __init__(self) -> None:
        self.preview_payload = _sample_preview()
        self.report_payload = CleanupReport(recent_runs=(_sample_run().run,))
        self.selected_status = _sample_run(active=False)
        self.preview_error: Exception | None = None
        self.execute_error: Exception | None = None
        self.purge_error: Exception | None = None
        self.report_error: Exception | None = None
        self.run_error: Exception | None = None
        self.started_dry_run: bool | None = None
        self.last_run_id = "run-abc"

    def preview(self) -> CleanupPreview:
        if self.preview_error is not None:
            raise self.preview_error
        return self.preview_payload

    def start_execute(self, *, dry_run: bool | None = None) -> str:
        if self.execute_error is not None:
            raise self.execute_error
        self.started_dry_run = dry_run
        return self.last_run_id

    def purge_orphans(self) -> CleanupRun:
        if self.purge_error is not None:
            raise self.purge_error
        return self.selected_status.run

    def report(self, limit: int | None = None) -> CleanupReport:
        _ = limit
        if self.report_error is not None:
            raise self.report_error
        return self.report_payload

    def get_run_status(self, run_id: str) -> CleanupRunStatus:
        _ = run_id
        if self.run_error is not None:
            raise self.run_error
        return self.selected_status


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32),
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
def stub_service(monkeypatch: pytest.MonkeyPatch) -> StubCleanupService:
    service = StubCleanupService()
    monkeypatch.setattr(cleanup_module, "_get_service", lambda: service)
    return service


class TestHelpers:
    def test_get_service_rejects_misconfigured_extension(self, app: Flask) -> None:
        with app.app_context():
            original = app.extensions["cleanup_service"]
            app.extensions["cleanup_service"] = object()
            with pytest.raises(RuntimeError, match="cleanup_service"):
                _get_service()
            app.extensions["cleanup_service"] = original

    def test_request_bool_reads_json_boolean(self, app: Flask) -> None:
        with app.test_request_context("/cleanup/execute", method="POST", json={"dry_run": True}):
            assert _request_bool("dry_run") is True

    def test_request_bool_reads_json_string(self, app: Flask) -> None:
        with app.test_request_context("/cleanup/execute", method="POST", json={"dry_run": "yes"}):
            assert _request_bool("dry_run") is True

    def test_request_bool_reads_form_value(self, app: Flask) -> None:
        with app.test_request_context("/cleanup/execute", method="POST", data={"dry_run": "on"}):
            assert _request_bool("dry_run") is True

    def test_request_bool_returns_none_when_missing(self, app: Flask) -> None:
        with app.test_request_context("/cleanup/execute", method="POST"):
            assert _request_bool("dry_run") is None

    def test_request_bool_rejects_invalid_type(self, app: Flask) -> None:
        with (
            app.test_request_context("/cleanup/execute", method="POST", json={"dry_run": ["bad"]}),
            pytest.raises(CleanupConfigError, match="dry_run must be a boolean"),
        ):
            _request_bool("dry_run")

    def test_serialize_orphans_handles_missing_orphans(self) -> None:
        payload = _serialize_orphans(replace_preview(orphan_scan=None))
        assert payload["db_only_paths"] == []
        assert payload["total_bytes_recoverable"] == 0

    def test_serialize_preview_includes_gib_and_orphans(self) -> None:
        payload = _serialize_preview(_sample_preview())
        orphans = payload["orphans"]
        assert isinstance(orphans, dict)
        assert payload["candidate_count"] == 1
        assert orphans["db_only_paths"] == ["RecentClips/db-only-front.mp4"]

    def test_serialize_run_includes_orphan_scan(self) -> None:
        payload = _serialize_run(_sample_run(active=True).run)
        orphan_scan = payload["orphan_scan"]
        assert isinstance(orphan_scan, dict)
        assert payload["run_id"] == "run-abc"
        assert orphan_scan["fs_only_paths"] == ["RecentClips/fs-only-front.mp4"]


class TestPreviewRoutes:
    def test_preview_json_endpoint_returns_serialized_preview(
        self, client: FlaskClient, stub_service: StubCleanupService
    ) -> None:
        response = client.get(
            "/api/cleanup/preview", headers={"X-Requested-With": "XMLHttpRequest"}
        )
        payload = response.get_json()
        assert response.status_code == HTTPStatus.OK
        assert payload["success"] is True
        assert payload["preview"]["candidate_count"] == 1
        assert payload["preview"]["orphans"]["fs_only_paths"] == ["RecentClips/fs-only-front.mp4"]
        assert stub_service.preview_error is None

    def test_preview_html_endpoint_renders_template(
        self, client: FlaskClient, stub_service: StubCleanupService
    ) -> None:
        response = client.get("/cleanup/preview")
        html = response.get_data(as_text=True)
        assert response.status_code == HTTPStatus.OK
        assert "Cleanup Preview" in html
        assert "Purge orphans" in html
        assert "Execute cleanup" in html
        assert "<svg" in html
        assert stub_service.preview_error is None

    def test_preview_json_endpoint_returns_bad_request_on_config_error(
        self, client: FlaskClient, stub_service: StubCleanupService
    ) -> None:
        stub_service.preview_error = CleanupConfigError("bad preview")
        response = client.get(
            "/api/cleanup/preview", headers={"X-Requested-With": "XMLHttpRequest"}
        )
        assert response.status_code == HTTPStatus.BAD_REQUEST
        assert response.get_json()["success"] is False


class TestExecuteRoutes:
    def test_execute_json_endpoint_returns_run_urls(
        self, client: FlaskClient, stub_service: StubCleanupService
    ) -> None:
        response = client.post(
            "/api/cleanup/execute",
            json={"dry_run": False},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        payload = response.get_json()
        assert response.status_code == HTTPStatus.ACCEPTED
        assert payload["run_id"] == "run-abc"
        assert payload["status_url"].endswith("/api/cleanup/runs/run-abc")
        assert stub_service.started_dry_run is False

    def test_execute_html_endpoint_redirects_to_report(
        self, client: FlaskClient, stub_service: StubCleanupService
    ) -> None:
        response = client.post("/cleanup/execute", data={"dry_run": "on"})
        assert response.status_code == HTTPStatus.FOUND
        assert response.headers["Location"].endswith("/cleanup/report?run_id=run-abc")
        assert stub_service.started_dry_run is True

    def test_execute_json_endpoint_returns_conflict_on_service_error(
        self, client: FlaskClient, stub_service: StubCleanupService
    ) -> None:
        stub_service.execute_error = CleanupError("busy")
        response = client.post(
            "/api/cleanup/execute",
            json={"dry_run": True},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert response.status_code == HTTPStatus.CONFLICT
        assert response.get_json()["error"] == "busy"


class TestOrphanPurgeRoutes:
    def test_purge_orphans_json_endpoint_returns_run(
        self, client: FlaskClient, stub_service: StubCleanupService
    ) -> None:
        _ = stub_service
        response = client.post(
            "/api/cleanup/orphans/purge",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        payload = response.get_json()
        assert response.status_code == HTTPStatus.OK
        assert payload["run"]["run_id"] == "run-abc"

    def test_purge_orphans_html_endpoint_redirects_to_report(
        self, client: FlaskClient, stub_service: StubCleanupService
    ) -> None:
        _ = stub_service
        response = client.post("/cleanup/orphans/purge")
        assert response.status_code == HTTPStatus.FOUND
        assert response.headers["Location"].endswith("/cleanup/report?run_id=run-abc")

    def test_purge_orphans_json_endpoint_returns_conflict_on_error(
        self, client: FlaskClient, stub_service: StubCleanupService
    ) -> None:
        stub_service.purge_error = CleanupError("purge failed")
        response = client.post(
            "/api/cleanup/orphans/purge",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert response.status_code == HTTPStatus.CONFLICT
        assert response.get_json()["error"] == "purge failed"


class TestReportRoutes:
    def test_report_json_endpoint_returns_selected_run(
        self, client: FlaskClient, stub_service: StubCleanupService
    ) -> None:
        stub_service.selected_status = _sample_run(active=True)
        response = client.get(
            "/api/cleanup/report?run_id=run-abc",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        payload = response.get_json()
        assert response.status_code == HTTPStatus.OK
        assert payload["active"] is True
        assert payload["selected_run"]["run_id"] == "run-abc"

    def test_report_json_endpoint_defaults_to_recent_run(
        self, client: FlaskClient, stub_service: StubCleanupService
    ) -> None:
        _ = stub_service
        response = client.get(
            "/api/cleanup/report",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        payload = response.get_json()
        assert response.status_code == HTTPStatus.OK
        assert payload["selected_run"]["run_id"] == "run-abc"
        assert len(payload["recent_runs"]) == 1

    def test_report_html_endpoint_renders_poll_url_for_active_run(
        self, client: FlaskClient, stub_service: StubCleanupService
    ) -> None:
        stub_service.selected_status = _sample_run(active=True)
        response = client.get("/cleanup/report?run_id=run-abc")
        html = response.get_data(as_text=True)
        assert response.status_code == HTTPStatus.OK
        assert "Cleanup Report" in html
        assert "data-cleanup-poll-url" in html
        assert "Recent runs" in html

    def test_report_html_endpoint_ignores_unknown_run(
        self, client: FlaskClient, stub_service: StubCleanupService
    ) -> None:
        stub_service.run_error = CleanupError("missing")
        response = client.get("/cleanup/report?run_id=run-missing")
        html = response.get_data(as_text=True)
        assert response.status_code == HTTPStatus.OK
        assert "Cleanup Report" in html

    def test_run_status_endpoint_returns_selected_run(
        self, client: FlaskClient, stub_service: StubCleanupService
    ) -> None:
        stub_service.selected_status = _sample_run(active=True)
        response = client.get("/api/cleanup/runs/run-abc")
        payload = response.get_json()
        assert response.status_code == HTTPStatus.OK
        assert payload["active"] is True
        assert payload["run"]["current_path"] == "RecentClips/sample-front.mp4"

    def test_run_status_endpoint_returns_not_found_on_error(
        self, client: FlaskClient, stub_service: StubCleanupService
    ) -> None:
        stub_service.run_error = CleanupError("missing")
        response = client.get("/api/cleanup/runs/run-missing")
        assert response.status_code == HTTPStatus.NOT_FOUND
        assert response.get_json()["error"] == "missing"


def replace_preview(*, orphan_scan: OrphanScan | None) -> CleanupPreview:
    from dataclasses import replace

    return replace(_sample_preview(), orphan_scan=orphan_scan)
