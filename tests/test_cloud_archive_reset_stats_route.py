"""Tests for blueprints/cloud_archive POST /api/reset_stats.

The route invokes :func:`cloud_archive_service.reset_stats_baseline`
and must:

* return ``{"success": True, "stats_baseline_at": "..."}`` on success
* return HTTP 500 + ``{"success": False, "message": ...}`` on failure
* invalidate the 10-second ``api_status._cache`` so the user sees the
  reset reflected on the very next poll (not 10 s later)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def app(monkeypatch):
    from flask import Flask

    from blueprints.cloud_archive import cloud_archive_bp

    flask_app = Flask(__name__)
    flask_app.secret_key = "test-only"
    flask_app.register_blueprint(cloud_archive_bp)
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


class TestApiResetStats:
    def test_success_returns_baseline(self, client, monkeypatch):
        # Stub out the underlying service helper.
        from services import cloud_archive_service as cas

        captured = {}

        def fake_reset(db_path):
            captured["db_path"] = db_path
            return True, "2026-05-15T00:00:00+00:00"

        monkeypatch.setattr(cas, "reset_stats_baseline", fake_reset)

        resp = client.post("/cloud/api/reset_stats")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["stats_baseline_at"] == "2026-05-15T00:00:00+00:00"
        assert captured["db_path"]  # called with the configured DB path

    def test_failure_returns_500_with_message(self, client, monkeypatch):
        from services import cloud_archive_service as cas

        monkeypatch.setattr(
            cas,
            "reset_stats_baseline",
            lambda db: (False, "disk I/O error"),
        )

        resp = client.post("/cloud/api/reset_stats")
        assert resp.status_code == 500
        body = resp.get_json()
        assert body["success"] is False
        assert "disk I/O error" in body["message"]

    def test_unexpected_exception_returns_500(self, client, monkeypatch):
        from services import cloud_archive_service as cas

        def raises(db_path):
            raise RuntimeError("boom")

        monkeypatch.setattr(cas, "reset_stats_baseline", raises)

        resp = client.post("/cloud/api/reset_stats")
        assert resp.status_code == 500
        body = resp.get_json()
        assert body["success"] is False
        assert "boom" in body["message"]

    def test_get_method_not_allowed(self, client):
        """Reset is a mutating action — must require POST."""
        resp = client.get("/cloud/api/reset_stats")
        assert resp.status_code == 405

    def test_cache_invalidated_after_reset(self, client, monkeypatch):
        """The 10-second api_status cache must be cleared so the user
        sees the reset reflected on the next poll."""
        from services import cloud_archive_service as cas
        from blueprints import cloud_archive as bp

        # Pre-seed the cache.
        bp.api_status._cache = {"data": {"total_synced": 99}, "ts": 9999}
        assert hasattr(bp.api_status, "_cache")

        monkeypatch.setattr(
            cas,
            "reset_stats_baseline",
            lambda db: (True, "2026-05-15T00:00:00+00:00"),
        )

        resp = client.post("/cloud/api/reset_stats")
        assert resp.status_code == 200
        # Cache attribute must be gone (next poll re-fetches).
        assert not hasattr(bp.api_status, "_cache")
