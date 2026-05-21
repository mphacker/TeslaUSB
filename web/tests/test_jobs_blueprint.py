"""Integration tests for the Failed Jobs blueprint."""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest

# See test_jobs_service.py — preload the cloud_archive package to avoid
# a partially-initialised circular import.
import teslausb_web.services.cloud_archive  # noqa: F401
from teslausb_web.app import create_app
from teslausb_web.blueprints.jobs import (
    _bad_request,
    _get_service,
    _MutationRequest,
    _parse_limit,
    _parse_offset,
    _parse_subsystem,
    _serialize_counts,
    _serialize_row,
)
from teslausb_web.config import PathsSection, StorageRetentionSection, WebConfig, WebSection
from teslausb_web.services.cloud_archive_queries import DeadLetterEntry
from teslausb_web.services.jobs_service import (
    CloudSyncAdapter,
    CloudSyncAdapterProtocol,
    IndexerAdapter,
    JobsService,
    JobsServiceError,
    Recommendation,
    SubsystemKey,
    ValueTier,
)
from teslausb_web.services.jobs_service._models import FailedJobRow, JobCounts

if TYPE_CHECKING:
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient


@dataclass
class FakeCloud(CloudSyncAdapterProtocol):
    entries: tuple[DeadLetterEntry, ...] = ()
    retries_returned: int = 1
    deletes_returned: int = 2
    last_retry: str | None = None
    last_delete: str | None = None

    def list_dead_letters(self, limit: int = 100) -> tuple[DeadLetterEntry, ...]:
        _ = limit
        return self.entries

    def count_dead_letters(self) -> int:
        return len(self.entries)

    def retry_dead_letter(self, file_path: str | None = None) -> int:
        self.last_retry = file_path
        return self.retries_returned

    def delete_dead_letter(self, file_path: str | None = None) -> int:
        self.last_delete = file_path
        return self.deletes_returned


def _dl(
    path: str = "RecentClips/clip.mp4", err: str | None = "connection refused"
) -> DeadLetterEntry:
    return DeadLetterEntry(
        id=1,
        file_path=path,
        file_size=1024,
        retry_count=3,
        last_error=err,
        previous_last_error=None,
    )


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
def fake_cloud(app: Flask) -> FakeCloud:
    fake = FakeCloud()
    fake_svc = JobsService(
        indexer=IndexerAdapter(None),
        cloud_sync=CloudSyncAdapter(fake),
    )
    app.extensions["jobs_service"] = fake_svc
    return fake


# ---------------------------------------------------------------- helpers


class TestHelpers:
    def test_get_service_rejects_misconfigured(self, app: Flask) -> None:
        with app.app_context():
            original = app.extensions["jobs_service"]
            app.extensions["jobs_service"] = object()
            with pytest.raises(RuntimeError, match="jobs_service"):
                _get_service()
            app.extensions["jobs_service"] = original

    def test_parse_subsystem_all_allowed(self) -> None:
        assert _parse_subsystem("all", allow_none=True) is None
        assert _parse_subsystem(None, allow_none=True) is None
        assert _parse_subsystem("", allow_none=True) is None

    def test_parse_subsystem_all_disallowed(self) -> None:
        with pytest.raises(JobsServiceError):
            _parse_subsystem("all", allow_none=False)
        with pytest.raises(JobsServiceError):
            _parse_subsystem(None, allow_none=False)

    def test_parse_subsystem_known(self) -> None:
        assert _parse_subsystem("indexer", allow_none=True) is SubsystemKey.INDEXER
        assert _parse_subsystem("CLOUD_SYNC", allow_none=True) is SubsystemKey.CLOUD_SYNC

    def test_parse_subsystem_unknown_raises(self) -> None:
        with pytest.raises(JobsServiceError, match="unknown"):
            _parse_subsystem("archive", allow_none=True)

    def test_parse_limit_default_and_clamp(self) -> None:
        assert _parse_limit(None) == 100
        assert _parse_limit("0") == 1
        assert _parse_limit("99999") == 1000
        assert _parse_limit("not-a-number") == 100

    def test_parse_offset(self) -> None:
        assert _parse_offset(None) == 0
        assert _parse_offset("-5") == 0
        assert _parse_offset("3") == 3
        assert _parse_offset("nan") == 0

    def test_mutation_request_dataclass_is_frozen(self) -> None:
        req = _MutationRequest(subsystem=SubsystemKey.CLOUD_SYNC, row_id="x")
        with pytest.raises(AttributeError):  # frozen dataclass guard
            req.row_id = "y"  # type: ignore[misc]

    def test_bad_request_serializes_allowed(self, app: Flask) -> None:
        from flask.wrappers import Response

        with app.app_context():
            result = _bad_request("bad")
            assert isinstance(result, tuple)
            assert len(result) == 2
            resp = result[0]
            status = result[1]
            assert status == HTTPStatus.BAD_REQUEST
            assert isinstance(resp, Response)
            payload = resp.get_json()
            assert payload["error"] == "bad"
            assert "indexer" in payload["allowed"]
            assert "cloud_sync" in payload["allowed"]

    def test_serialize_row_shape(self) -> None:
        row = FailedJobRow(
            subsystem=SubsystemKey.CLOUD_SYNC,
            row_id="x",
            identifier="x",
            attempts=1,
            last_error="err",
            previous_last_error="",
            value=ValueTier(tier="cloud", label="L", description="D"),
            recommendation=Recommendation(action="retry", reason="r"),
        )
        d = _serialize_row(row)
        assert d["subsystem"] == "cloud_sync"
        assert d["value"] == {"tier": "cloud", "label": "L", "description": "D"}
        assert d["recommendation"] == {"action": "retry", "reason": "r"}

    def test_serialize_counts_shape(self) -> None:
        c = _serialize_counts(JobCounts(indexer=2, cloud_sync=3, total=5))
        assert c == {"indexer": 2, "cloud_sync": 3, "total": 5}


# ---------------------------------------------------------------- routes


class TestPage:
    def test_get_jobs_returns_200(self, client: FlaskClient, fake_cloud: FakeCloud) -> None:
        _ = fake_cloud
        resp = client.get("/jobs")
        assert resp.status_code == HTTPStatus.OK
        assert b"Failed Jobs" in resp.data
        assert b'data-subsystem="indexer"' in resp.data
        assert b'data-subsystem="cloud_sync"' in resp.data


class TestCounts:
    def test_counts_empty(self, client: FlaskClient, fake_cloud: FakeCloud) -> None:
        _ = fake_cloud
        resp = client.get("/api/jobs/counts")
        assert resp.status_code == HTTPStatus.OK
        assert resp.get_json() == {"indexer": 0, "cloud_sync": 0, "total": 0}

    def test_counts_with_cloud_entries(self, client: FlaskClient, fake_cloud: FakeCloud) -> None:
        fake_cloud.entries = (_dl(), _dl(path="SentryClips/y.mp4"))
        resp = client.get("/api/jobs/counts")
        assert resp.get_json() == {"indexer": 0, "cloud_sync": 2, "total": 2}


class TestFailedList:
    def test_failed_indexer_empty(self, client: FlaskClient, fake_cloud: FakeCloud) -> None:
        _ = fake_cloud
        resp = client.get("/api/jobs/failed?subsystem=indexer")
        assert resp.status_code == HTTPStatus.OK
        data = resp.get_json()
        assert data["subsystem"] == "indexer"
        assert data["rows"] == []

    def test_failed_cloud_sync_returns_rows(
        self, client: FlaskClient, fake_cloud: FakeCloud
    ) -> None:
        fake_cloud.entries = (_dl(path="/mnt/SentryClips/a.mp4"),)
        resp = client.get("/api/jobs/failed?subsystem=cloud_sync")
        assert resp.status_code == HTTPStatus.OK
        data = resp.get_json()
        assert data["count"] == 1
        assert data["rows"][0]["subsystem"] == "cloud_sync"
        assert data["rows"][0]["value"]["tier"] == "event"

    def test_failed_all_subsystems(self, client: FlaskClient, fake_cloud: FakeCloud) -> None:
        fake_cloud.entries = (_dl(),)
        resp = client.get("/api/jobs/failed?subsystem=all")
        assert resp.status_code == HTTPStatus.OK
        assert resp.get_json()["subsystem"] == "all"

    def test_failed_unknown_subsystem_400(self, client: FlaskClient, fake_cloud: FakeCloud) -> None:
        _ = fake_cloud
        resp = client.get("/api/jobs/failed?subsystem=bogus")
        assert resp.status_code == HTTPStatus.BAD_REQUEST
        assert "unknown" in resp.get_json()["error"]


class TestRetry:
    def test_retry_single_row(self, client: FlaskClient, fake_cloud: FakeCloud) -> None:
        resp = client.post(
            "/api/jobs/retry",
            json={"subsystem": "cloud_sync", "row_id": "RecentClips/x.mp4"},
        )
        assert resp.status_code == HTTPStatus.OK
        data = resp.get_json()
        assert data["subsystem"] == "cloud_sync"
        assert data["retried"] == fake_cloud.retries_returned
        assert fake_cloud.last_retry == "RecentClips/x.mp4"

    def test_retry_all_in_subsystem(self, client: FlaskClient, fake_cloud: FakeCloud) -> None:
        resp = client.post("/api/jobs/retry", json={"subsystem": "cloud_sync", "row_id": None})
        assert resp.status_code == HTTPStatus.OK
        assert fake_cloud.last_retry is None

    def test_retry_indexer_is_noop(self, client: FlaskClient, fake_cloud: FakeCloud) -> None:
        _ = fake_cloud
        resp = client.post("/api/jobs/retry", json={"subsystem": "indexer"})
        assert resp.status_code == HTTPStatus.OK
        assert resp.get_json()["retried"] == 0

    def test_retry_missing_body_400(self, client: FlaskClient, fake_cloud: FakeCloud) -> None:
        _ = fake_cloud
        resp = client.post("/api/jobs/retry", data="not-json", content_type="text/plain")
        assert resp.status_code == HTTPStatus.BAD_REQUEST

    def test_retry_missing_subsystem_400(self, client: FlaskClient, fake_cloud: FakeCloud) -> None:
        _ = fake_cloud
        resp = client.post("/api/jobs/retry", json={"row_id": "x"})
        assert resp.status_code == HTTPStatus.BAD_REQUEST

    def test_retry_unknown_subsystem_400(self, client: FlaskClient, fake_cloud: FakeCloud) -> None:
        _ = fake_cloud
        resp = client.post("/api/jobs/retry", json={"subsystem": "archive"})
        assert resp.status_code == HTTPStatus.BAD_REQUEST

    def test_retry_row_id_invalid_type_400(
        self, client: FlaskClient, fake_cloud: FakeCloud
    ) -> None:
        _ = fake_cloud
        resp = client.post("/api/jobs/retry", json={"subsystem": "cloud_sync", "row_id": ["bad"]})
        assert resp.status_code == HTTPStatus.BAD_REQUEST

    def test_retry_row_id_int_is_coerced(self, client: FlaskClient, fake_cloud: FakeCloud) -> None:
        resp = client.post("/api/jobs/retry", json={"subsystem": "cloud_sync", "row_id": 42})
        assert resp.status_code == HTTPStatus.OK
        assert fake_cloud.last_retry == "42"


class TestDelete:
    def test_delete_single(self, client: FlaskClient, fake_cloud: FakeCloud) -> None:
        resp = client.post("/api/jobs/delete", json={"subsystem": "cloud_sync", "row_id": "x"})
        assert resp.status_code == HTTPStatus.OK
        assert resp.get_json()["deleted"] == fake_cloud.deletes_returned
        assert fake_cloud.last_delete == "x"

    def test_delete_unknown_subsystem_400(self, client: FlaskClient, fake_cloud: FakeCloud) -> None:
        _ = fake_cloud
        resp = client.post("/api/jobs/delete", json={"subsystem": "archive"})
        assert resp.status_code == HTTPStatus.BAD_REQUEST


class TestServiceErrorPath:
    def test_service_error_in_retry_returns_400(
        self,
        client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
        fake_cloud: FakeCloud,
    ) -> None:
        _ = fake_cloud

        def boom(self: JobsService, subsystem: SubsystemKey, row_id: str | None) -> object:
            _ = self, subsystem, row_id
            raise JobsServiceError("contract violation")

        monkeypatch.setattr(JobsService, "retry", boom)
        resp = client.post("/api/jobs/retry", json={"subsystem": "cloud_sync", "row_id": "x"})
        assert resp.status_code == HTTPStatus.BAD_REQUEST
        assert "contract violation" in resp.get_json()["error"]

    def test_service_error_in_delete_returns_400(
        self,
        client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
        fake_cloud: FakeCloud,
    ) -> None:
        _ = fake_cloud

        def boom(self: JobsService, subsystem: SubsystemKey, row_id: str | None) -> object:
            _ = self, subsystem, row_id
            raise JobsServiceError("nope")

        monkeypatch.setattr(JobsService, "delete", boom)
        resp = client.post("/api/jobs/delete", json={"subsystem": "cloud_sync", "row_id": "x"})
        assert resp.status_code == HTTPStatus.BAD_REQUEST
