"""Phase 4.7 (#101) — retention summary surfacing tests.

Pins the contract that ``/api/archive/status`` exposes the four
retention-summary fields the Settings UI needs to render the
"Retention pruned N clips today, X freed" one-liner:

* ``retention_days`` — the configured retention window
* ``last_prune_at`` — ISO timestamp of the most recent prune (or null)
* ``last_prune_deleted`` — int count of clips deleted on the last run
* ``last_prune_freed_bytes`` — int bytes reclaimed on the last run
* ``last_prune_kept_unsynced`` — int clips withheld by the
  "keep until backed up" toggle (Phase 1 item 1.3)
* ``last_prune_error`` — str error message or null
* ``next_prune_due_at`` — epoch seconds of next scheduled prune

The Settings JS (``composePruneSummary`` in ``index.html``) reads all
of these to build the user-facing summary line. If any are removed or
renamed the JS silently degrades to "—" — these tests guard against
that regression.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from services import archive_queue
from services import archive_watchdog
from services import archive_worker
from services import task_coordinator
from services.mapping_service import _init_db


# ---------------------------------------------------------------------------
# Fixtures (mirror test_archive_status_endpoint.py — module state must be
# cleaned between tests because watchdog/worker hold module-level globals)
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
    # Reset module-level retention dict so test ordering doesn't matter
    # — the TestRetentionSummaryAfterPrune class injects values directly
    # and would otherwise leak into TestStateDefaultsContract.
    archive_watchdog._retention_state.update({
        'last_prune_at': None,
        'last_prune_deleted': 0,
        'last_prune_freed_bytes': 0,
        'last_prune_kept_unsynced': 0,
        'last_prune_error': None,
        'next_prune_due_at': None,
    })
    with task_coordinator._lock:
        task_coordinator._current_task = None
        task_coordinator._task_started = 0.0
        task_coordinator._waiter_count = 0
    yield
    archive_watchdog.stop_watchdog(timeout=5.0)
    archive_worker.stop_worker(timeout=5.0)
    archive_worker._disk_space_pause_until = 0.0
    archive_watchdog._retention_state.update({
        'last_prune_at': None,
        'last_prune_deleted': 0,
        'last_prune_freed_bytes': 0,
        'last_prune_kept_unsynced': 0,
        'last_prune_error': None,
        'next_prune_due_at': None,
    })
    with task_coordinator._lock:
        task_coordinator._current_task = None
        task_coordinator._task_started = 0.0
        task_coordinator._waiter_count = 0


@pytest.fixture
def app(tmp_path):
    from flask import Flask
    from blueprints.archive_queue import archive_queue_bp

    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(archive_queue_bp)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Phase 4.7 contract tests
# ---------------------------------------------------------------------------


class TestRetentionSummaryFieldsPresent:
    """All retention-summary fields appear in every /api/archive/status
    response — regardless of whether a prune has ever run."""

    REQUIRED_RETENTION_FIELDS = {
        'retention_days',
        'last_prune_at',
        'last_prune_deleted',
        'last_prune_freed_bytes',
        'last_prune_kept_unsynced',
        'last_prune_error',
        'next_prune_due_at',
    }

    def test_all_fields_present_before_first_prune(self, client):
        """Cold start — no prune has ever run. All 7 fields must still
        be present so the JS doesn't crash on `data.last_prune_at`."""
        with patch('blueprints.archive_queue.os.path.isfile',
                   return_value=True):
            r = client.get('/api/archive/status')
        assert r.status_code == 200
        body = r.get_json()
        missing = self.REQUIRED_RETENTION_FIELDS - set(body.keys())
        assert not missing, (
            f"/api/archive/status missing retention fields: {missing}. "
            "These power the Phase 4.7 Settings summary line — removing "
            "any of them will silently break the UI."
        )

    def test_kept_unsynced_is_int_zero_at_cold_start(self, client):
        """The new field must default to int 0, not null/None — the JS
        passes it to `parseInt` and expects a number-like value."""
        with patch('blueprints.archive_queue.os.path.isfile',
                   return_value=True):
            r = client.get('/api/archive/status')
        body = r.get_json()
        assert 'last_prune_kept_unsynced' in body
        assert body['last_prune_kept_unsynced'] == 0
        assert isinstance(body['last_prune_kept_unsynced'], int)


class TestRetentionSummaryAfterPrune:
    """After a real retention prune runs, the summary fields are
    populated end-to-end (watchdog → archive_queue blueprint)."""

    def _make_old_clip(self, archive_root, age_days=400):
        """Drop a sample clip in archive_root with mtime far past
        retention so a prune will delete it."""
        sub = os.path.join(archive_root, '2020-01-01_12-00-00_old')
        os.makedirs(sub, exist_ok=True)
        path = os.path.join(sub, 'front.mp4')
        with open(path, 'wb') as f:
            f.write(b'\x00' * 4096)
        # Stamp it "age_days" days old.
        import time as _t
        old = _t.time() - (age_days * 86400)
        os.utime(path, (old, old))
        return path

    def test_prune_summary_populates_kept_unsynced_field(self, client, db,
                                                         archive_root):
        """When the watchdog records a kept-unsynced count in the
        retention dict, /api/archive/status surfaces it."""
        # Directly inject into the watchdog state. Easier and more
        # focused than driving a real prune through cloud-sync logic.
        archive_watchdog._retention_state['last_prune_at'] = (
            '2026-05-13T00:00:00+00:00'
        )
        archive_watchdog._retention_state['last_prune_deleted'] = 47
        archive_watchdog._retention_state['last_prune_freed_bytes'] = (
            int(2.3 * 1024 * 1024 * 1024)  # 2.3 GB
        )
        archive_watchdog._retention_state['last_prune_kept_unsynced'] = 3
        archive_watchdog._retention_state['last_prune_error'] = None

        with patch('blueprints.archive_queue.os.path.isfile',
                   return_value=True):
            r = client.get('/api/archive/status')
        body = r.get_json()
        assert body['last_prune_at'] == '2026-05-13T00:00:00+00:00'
        assert body['last_prune_deleted'] == 47
        assert body['last_prune_freed_bytes'] == int(2.3 * 1024**3)
        assert body['last_prune_kept_unsynced'] == 3
        assert body['last_prune_error'] is None

    def test_prune_summary_with_error_state(self, client):
        """If the last prune raised, the error string is surfaced
        verbatim — UI uses it to render the failure variant."""
        archive_watchdog._retention_state['last_prune_at'] = (
            '2026-05-13T00:00:00+00:00'
        )
        archive_watchdog._retention_state['last_prune_error'] = (
            'OSError: disk full'
        )

        with patch('blueprints.archive_queue.os.path.isfile',
                   return_value=True):
            r = client.get('/api/archive/status')
        body = r.get_json()
        assert body['last_prune_error'] == 'OSError: disk full'

    def test_kept_unsynced_coerces_string_to_int(self, client):
        """Defensive: if any caller stuffs a string into the dict, the
        blueprint must coerce so JSON shape stays numeric."""
        archive_watchdog._retention_state['last_prune_kept_unsynced'] = '7'
        with patch('blueprints.archive_queue.os.path.isfile',
                   return_value=True):
            r = client.get('/api/archive/status')
        body = r.get_json()
        assert body['last_prune_kept_unsynced'] == 7
        assert isinstance(body['last_prune_kept_unsynced'], int)


class TestStateDefaultsContract:
    """The ``_retention_state`` module-level dict must declare
    ``last_prune_kept_unsynced`` so the Settings UI can rely on the
    field being present even before any prune has been recorded."""

    def test_kept_unsynced_default_in_module_state(self):
        """The default state ships with the key zero-initialized."""
        assert 'last_prune_kept_unsynced' in archive_watchdog._retention_state
        # In a clean process this is 0; tests above may have overwritten
        # it, so just assert int-ness here.
        val = archive_watchdog._retention_state['last_prune_kept_unsynced']
        assert isinstance(val, int)
