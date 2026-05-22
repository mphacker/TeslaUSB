"""Tests for ``teslausb_web.blueprints.analytics``."""

from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, cast

import pytest
from teslausb_web.app import create_app
from teslausb_web.blueprints import analytics as analytics_module
from teslausb_web.blueprints.analytics import _get_service
from teslausb_web.config import (
    PathsSection,
    StorageRetentionSection,
    WebConfig,
    WebSection,
)
from teslausb_web.services.analytics_service import (
    AnalyticsConfigError,
    AnalyticsDataError,
    AnalyticsService,
    CompleteAnalytics,
    FolderBreakdown,
    PartitionUsage,
    RecordingEstimate,
    StorageHealth,
    VideoStatistics,
)

if TYPE_CHECKING:
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient


def _sample_partition(percent: float = 50.0) -> PartitionUsage:
    return PartitionUsage(
        key="backing",
        label="TeslaCam Storage",
        path="/srv/backing",
        total_bytes=1_000_000,
        used_bytes=int(10_000 * percent),
        free_bytes=1_000_000 - int(10_000 * percent),
        percent_used=percent,
    )


def _sample_video_stats() -> VideoStatistics:
    return VideoStatistics(
        total_files=3,
        clip_count=1,
        total_bytes=600,
        oldest_iso="2024-01-01T00:00:00+00:00",
        newest_iso="2024-06-01T00:00:00+00:00",
        folders=(
            FolderBreakdown(
                name="SavedClips",
                description="Manually saved clips",
                priority="high",
                count=3,
                clip_count=1,
                size_bytes=600,
                oldest_iso="2024-01-01T00:00:00+00:00",
                newest_iso="2024-06-01T00:00:00+00:00",
            ),
        ),
    )


def _sample_health() -> StorageHealth:
    return StorageHealth(
        status="healthy",
        percent_used=50.0,
        alerts=(),
        recommendations=(),
    )


def _sample_estimate() -> RecordingEstimate:
    return RecordingEstimate(
        hours_remaining=42.0,
        method="based on 3 existing clips",
        confidence="medium",
    )


def _sample_complete() -> CompleteAnalytics:
    return CompleteAnalytics(
        partitions=(_sample_partition(),),
        video_statistics=_sample_video_stats(),
        storage_health=_sample_health(),
        recording_estimate=_sample_estimate(),
        generated_at=datetime(2024, 8, 9, 10, 11, 12, tzinfo=UTC).isoformat(),
    )


class StubAnalyticsService:
    """Drop-in for :class:`AnalyticsService` used in route tests."""

    def __init__(self) -> None:
        self.complete_payload: CompleteAnalytics | None = _sample_complete()
        self.complete_error: Exception | None = None
        self.partitions_error: Exception | None = None
        self.video_error: Exception | None = None
        self.health_error: Exception | None = None

    def get_complete_analytics(self) -> CompleteAnalytics:
        if self.complete_error is not None:
            raise self.complete_error
        assert self.complete_payload is not None
        return self.complete_payload

    def get_partition_usage(self) -> tuple[PartitionUsage, ...]:
        if self.partitions_error is not None:
            raise self.partitions_error
        return (_sample_partition(),)

    def get_video_statistics(self) -> VideoStatistics:
        if self.video_error is not None:
            raise self.video_error
        return _sample_video_stats()

    def get_storage_health(self) -> StorageHealth:
        if self.health_error is not None:
            raise self.health_error
        return _sample_health()


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
def stub_service(monkeypatch: pytest.MonkeyPatch) -> StubAnalyticsService:
    service = StubAnalyticsService()
    monkeypatch.setattr(analytics_module, "_get_service", lambda: service)
    return service


# ---------------------------------------------------------------------------
# Extension wiring
# ---------------------------------------------------------------------------


class TestExtensionLookup:
    def test_get_service_rejects_misconfigured_extension(self, app: Flask) -> None:
        with app.app_context():
            original = app.extensions.get("analytics_service")
            app.extensions["analytics_service"] = object()
            with pytest.raises(RuntimeError, match="analytics_service"):
                _get_service()
            if original is not None:
                app.extensions["analytics_service"] = original

    def test_url_map_registers_all_routes(self, app: Flask) -> None:
        endpoints = {
            r.endpoint for r in app.url_map.iter_rules() if r.endpoint.startswith("analytics.")
        }
        assert endpoints == {
            "analytics.dashboard",
            "analytics.api_data",
            "analytics.api_partition_usage",
            "analytics.api_video_stats",
            "analytics.api_health",
        }


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------


class TestDashboard:
    def test_dashboard_renders_with_payload(
        self, client: FlaskClient, stub_service: StubAnalyticsService
    ) -> None:
        _ = stub_service
        response = client.get("/analytics/")
        assert response.status_code == HTTPStatus.OK
        body = response.get_data(as_text=True)
        assert "Storage Analytics" in body or "analytics" in body.lower()
        # Sample partition label should appear somewhere in the rendered card.
        assert "TeslaCam Storage" in body

    def test_dashboard_renders_friendly_placeholder_on_failure(
        self, client: FlaskClient, stub_service: StubAnalyticsService
    ) -> None:
        stub_service.complete_error = AnalyticsDataError("DB locked")
        response = client.get("/analytics/")
        assert response.status_code == HTTPStatus.OK


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


class TestApiData:
    def test_returns_complete_json(
        self, client: FlaskClient, stub_service: StubAnalyticsService
    ) -> None:
        _ = stub_service
        response = client.get("/analytics/api/data")
        assert response.status_code == HTTPStatus.OK
        payload = response.get_json()
        assert set(payload) == {
            "partitions",
            "video_statistics",
            "storage_health",
            "recording_estimate",
            "generated_at",
        }
        assert payload["partitions"][0]["label"] == "TeslaCam Storage"

    def test_returns_503_on_failure(
        self, client: FlaskClient, stub_service: StubAnalyticsService
    ) -> None:
        stub_service.complete_error = AnalyticsDataError("DB locked")
        response = client.get("/analytics/api/data")
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        payload = response.get_json()
        assert payload["success"] is False


class TestApiPartitionUsage:
    def test_returns_partitions(
        self, client: FlaskClient, stub_service: StubAnalyticsService
    ) -> None:
        _ = stub_service
        response = client.get("/analytics/api/partition-usage")
        assert response.status_code == HTTPStatus.OK
        payload = response.get_json()
        assert isinstance(payload["partitions"], list)
        assert payload["partitions"][0]["key"] == "backing"

    def test_returns_503_when_service_errors(
        self, client: FlaskClient, stub_service: StubAnalyticsService
    ) -> None:
        stub_service.partitions_error = AnalyticsDataError("disk gone")
        response = client.get("/analytics/api/partition-usage")
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE


class TestApiVideoStats:
    def test_returns_video_stats(
        self, client: FlaskClient, stub_service: StubAnalyticsService
    ) -> None:
        _ = stub_service
        response = client.get("/analytics/api/video-stats")
        assert response.status_code == HTTPStatus.OK
        payload = response.get_json()
        assert payload["total_files"] == 3
        assert len(payload["folders"]) == 1

    def test_data_error_returns_503(
        self, client: FlaskClient, stub_service: StubAnalyticsService
    ) -> None:
        stub_service.video_error = AnalyticsDataError("DB locked")
        response = client.get("/analytics/api/video-stats")
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE

    def test_other_analytics_error_returns_500(
        self, client: FlaskClient, stub_service: StubAnalyticsService
    ) -> None:
        stub_service.video_error = AnalyticsConfigError("bad cfg")
        response = client.get("/analytics/api/video-stats")
        assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR


class TestApiHealth:
    def test_returns_health(self, client: FlaskClient, stub_service: StubAnalyticsService) -> None:
        _ = stub_service
        response = client.get("/analytics/api/health")
        assert response.status_code == HTTPStatus.OK
        payload = response.get_json()
        assert payload["status"] == "healthy"

    def test_returns_503_when_service_errors(
        self, client: FlaskClient, stub_service: StubAnalyticsService
    ) -> None:
        stub_service.health_error = AnalyticsDataError("disk gone")
        response = client.get("/analytics/api/health")
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE


# ---------------------------------------------------------------------------
# Real-service smoke (no stub) — exercises factory + extension registration.
# ---------------------------------------------------------------------------


class TestRealServiceRegistration:
    def test_analytics_service_registered_on_create_app(self, app: Flask) -> None:
        service = app.extensions.get("analytics_service")
        assert isinstance(service, AnalyticsService)
        # And the resolver returns the same instance.
        with app.app_context():
            assert _get_service() is service

    def test_dashboard_does_not_500_without_mapping_db(self, client: FlaskClient) -> None:
        # No stub here; the underlying mapping DB does not exist on disk.
        response = client.get("/analytics/")
        assert response.status_code == HTTPStatus.OK
        assert b"<html" in cast("bytes", response.data).lower()
