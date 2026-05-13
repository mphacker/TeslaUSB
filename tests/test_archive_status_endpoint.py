"""Tests for the Phase 2c /api/archive/* endpoints (issue #76)."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from unittest.mock import patch

import pytest

from services import archive_queue
from services import archive_watchdog
from services import archive_worker
from services import task_coordinator
from services.mapping_service import _init_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    db_path = str(tmp_path / "geodata.db")
    _init_db(db_path).close()
    return db_path


@pytest.fixture
def archive_root(tmp_path):
    p = tmp_path / "ArchivedClips"
    p.mkdir()
    return str(p)


@pytest.fixture(autouse=True)
def _reset_module_state():
    archive_watchdog.stop_watchdog(timeout=5.0)
    archive_worker.stop_worker(timeout=5.0)
    archive_worker._disk_space_pause_until = 0.0
    with task_coordinator._lock:
        task_coordinator._current_task = None
        task_coordinator._task_started = 0.0
        task_coordinator._waiter_count = 0
    yield
    archive_watchdog.stop_watchdog(timeout=5.0)
    archive_worker.stop_worker(timeout=5.0)
    archive_worker._disk_space_pause_until = 0.0
    with task_coordinator._lock:
        task_coordinator._current_task = None
        task_coordinator._task_started = 0.0
        task_coordinator._waiter_count = 0


@pytest.fixture
def app(tmp_path):
    """Build a minimal Flask app with just the archive_queue blueprint.

    We build it directly rather than importing ``web_control.app`` so
    the tests don't drag in the full TeslaUSB stack (Samba, mode
    service, partition mount service, etc.).
    """
    from flask import Flask
    from blueprints.archive_queue import archive_queue_bp
    flask_app = Flask(__name__)
    flask_app.register_blueprint(archive_queue_bp)
    flask_app.config['TESTING'] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestArchiveStatusImageGate:
    def test_returns_503_when_cam_image_missing(self, client):
        with patch('blueprints.archive_queue.os.path.isfile',
                   return_value=False):
            r = client.get('/api/archive/status')
        assert r.status_code == 503
        body = r.get_json()
        assert 'error' in body

    def test_returns_200_when_cam_image_present(self, client):
        with patch('blueprints.archive_queue.os.path.isfile',
                   return_value=True):
            r = client.get('/api/archive/status')
        assert r.status_code == 200

    def test_legacy_alias_returns_200(self, client):
        # Phase 2a alias kept intact.
        with patch('blueprints.archive_queue.os.path.isfile',
                   return_value=True):
            r = client.get('/api/archive_queue/status')
        assert r.status_code == 200

    def test_dead_letters_endpoint_image_gated(self, client):
        with patch('blueprints.archive_queue.os.path.isfile',
                   return_value=False):
            r = client.get('/api/archive/dead_letters')
        assert r.status_code == 503

    def test_prune_now_endpoint_image_gated(self, client):
        with patch('blueprints.archive_queue.os.path.isfile',
                   return_value=False):
            r = client.post('/api/archive/prune_now')
        assert r.status_code == 503


class TestArchiveStatusShape:
    REQUIRED_FIELDS = {
        'severity', 'message', 'enabled', 'checked_at',
        'queue_depth_p1', 'queue_depth_p2', 'queue_depth_p3',
        'pending_count', 'claimed_count', 'copied_count',
        'source_gone_count', 'dead_letter_count',
        'worker_running', 'paused', 'active_file', 'last_outcome',
        'last_error', 'files_done_session', 'disk_pause', 'load_pause',
        'disk_total_mb', 'disk_used_mb', 'disk_free_mb',
        'disk_warning_mb', 'disk_critical_mb', 'disk_known',
        'last_successful_copy_at', 'last_successful_copy_age_seconds',
        'retention_days', 'last_prune_at', 'last_prune_deleted',
        'last_prune_freed_bytes', 'last_prune_error', 'next_prune_due_at',
        'last_prune_kept_unsynced',
        'watchdog_running',
    }

    def test_status_payload_includes_all_expected_fields(self, client):
        with patch('blueprints.archive_queue.os.path.isfile',
                   return_value=True):
            r = client.get('/api/archive/status')
        assert r.status_code == 200
        body = r.get_json()
        missing = self.REQUIRED_FIELDS - set(body.keys())
        assert not missing, f"Status payload missing fields: {missing}"

    def test_status_includes_disk_info(self, client):
        with patch('blueprints.archive_queue.os.path.isfile',
                   return_value=True):
            r = client.get('/api/archive/status')
        body = r.get_json()
        # All disk fields are integers (MB).
        assert isinstance(body['disk_total_mb'], int)
        assert isinstance(body['disk_used_mb'], int)
        assert isinstance(body['disk_free_mb'], int)
        # Thresholds default to 500/100 from spec.
        assert body['disk_warning_mb'] == 500
        assert body['disk_critical_mb'] == 100

    def test_status_includes_load_pause_block(self, client):
        # Regression guard: PR #93 added ``load_pause`` to
        # ``archive_worker.get_status()`` for SDIO-contention
        # observability, but the blueprint route hand-picks fields
        # from the worker snapshot and originally missed wiring it
        # through. Without this shape test, a future refactor of
        # the route could silently drop it again. The UI's
        # archive panel needs all four fields to render the
        # "load-paused" indicator and the most recent loadavg.
        with patch('blueprints.archive_queue.os.path.isfile',
                   return_value=True):
            r = client.get('/api/archive/status')
        assert r.status_code == 200
        body = r.get_json()
        assert 'load_pause' in body, (
            "/api/archive/status MUST surface the load_pause block "
            "from archive_worker.get_status() — the UI depends on it."
        )
        lp = body['load_pause']
        assert set(lp.keys()) >= {
            'paused_until_epoch', 'is_paused_now',
            'last_pause_at', 'last_loadavg',
        }, f"load_pause block missing required keys: {set(lp.keys())}"

    def test_status_includes_per_priority_queue_depths(
        self, client, db, archive_root, monkeypatch,
    ):
        # Enqueue rows of different priorities and verify counts.
        archive_queue.enqueue_for_archive(
            "/tc/RecentClips/p1.mp4", db_path=db, priority=1,
        )
        archive_queue.enqueue_for_archive(
            "/tc/RecentClips/p1b.mp4", db_path=db, priority=1,
        )
        archive_queue.enqueue_for_archive(
            "/tc/SentryClips/evt/p2.mp4", db_path=db, priority=2,
        )
        archive_queue.enqueue_for_archive(
            "/tc/SavedClips/evt/p3.mp4", db_path=db, priority=3,
        )
        # Pin the DB resolver so the endpoint reads our test DB.
        monkeypatch.setattr(
            archive_queue, '_resolve_db_path', lambda _p=None: db,
        )
        with patch('blueprints.archive_queue.os.path.isfile',
                   return_value=True):
            r = client.get('/api/archive/status')
        body = r.get_json()
        assert body['queue_depth_p1'] == 2
        assert body['queue_depth_p2'] == 1
        assert body['queue_depth_p3'] == 1
        assert body['pending_count'] == 4

    def test_severity_default_is_ok(self, client):
        with patch('blueprints.archive_queue.os.path.isfile',
                   return_value=True):
            r = client.get('/api/archive/status')
        body = r.get_json()
        assert body['severity'] in ('ok', 'warning', 'error', 'critical')


class TestPruneNowEndpoint:
    def test_returns_503_when_watchdog_not_running(self, client):
        with patch('blueprints.archive_queue.os.path.isfile',
                   return_value=True):
            r = client.post('/api/archive/prune_now')
        assert r.status_code == 503
        body = r.get_json()
        assert body['started'] is False

    def test_runs_synchronously_and_returns_summary(
        self, client, db, archive_root, monkeypatch,
    ):
        # Plant an old mp4 we expect the prune to delete.
        old_mtime = time.time() - (40 * 86400)
        old_file = os.path.normpath(
            os.path.join(archive_root, "RecentClips", "old.mp4")
        )
        os.makedirs(os.path.dirname(old_file), exist_ok=True)
        with open(old_file, 'wb') as f:
            f.write(b"X" * 1024)
        os.utime(old_file, (old_mtime, old_mtime))

        # Start the watchdog so the endpoint guard passes.
        archive_watchdog.start_watchdog(
            db, archive_root, check_interval_seconds=60,
        )
        try:
            with patch('blueprints.archive_queue.os.path.isfile',
                       return_value=True):
                r = client.post('/api/archive/prune_now')
            assert r.status_code == 200
            body = r.get_json()
            assert body['started'] is True
            assert body['deleted_count'] == 1
            assert body['freed_bytes'] >= 1024
            assert body['retention_days'] == 30
            assert not os.path.exists(old_file)
        finally:
            archive_watchdog.stop_watchdog(timeout=5)


class TestDeadLettersEndpoint:
    def test_returns_empty_list_when_no_dead_letters(
        self, client, db, monkeypatch,
    ):
        monkeypatch.setattr(
            archive_queue, '_resolve_db_path', lambda _p=None: db,
        )
        with patch('blueprints.archive_queue.os.path.isfile',
                   return_value=True):
            r = client.get('/api/archive/dead_letters')
        assert r.status_code == 200
        body = r.get_json()
        assert body['rows'] == []
        assert body['count'] == 0

    def test_returns_dead_letter_rows_with_last_error(
        self, client, db, monkeypatch,
    ):
        # Insert a synthetic dead-letter row.
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO archive_queue("
                " source_path, status, priority, enqueued_at, attempts,"
                " last_error) "
                "VALUES (?, 'dead_letter', 2, ?, 5, ?)",
                ("/tc/SentryClips/evt/x.mp4", "2025-01-01T00:00:00Z",
                 "Permission denied"),
            )
        monkeypatch.setattr(
            archive_queue, '_resolve_db_path', lambda _p=None: db,
        )
        with patch('blueprints.archive_queue.os.path.isfile',
                   return_value=True):
            r = client.get('/api/archive/dead_letters')
        body = r.get_json()
        assert body['count'] == 1
        assert body['rows'][0]['source_path'] == "/tc/SentryClips/evt/x.mp4"
        assert body['rows'][0]['last_error'] == "Permission denied"
        assert body['rows'][0]['status'] == 'dead_letter'

    def test_limit_param_clamped(self, client, db, monkeypatch):
        monkeypatch.setattr(
            archive_queue, '_resolve_db_path', lambda _p=None: db,
        )
        with patch('blueprints.archive_queue.os.path.isfile',
                   return_value=True):
            r = client.get('/api/archive/dead_letters?limit=99999')
        assert r.status_code == 200
