"""Tests for the Phase 3a.2 (#98) Storage & Retention blueprint.

Covers the contract for:

* ``GET  /api/cleanup/status``    — combined snapshot of config + watchdog
* ``POST /api/cleanup/policy``    — persist unified retention settings
* ``POST /api/cleanup/run_now``   — trigger immediate prune (mirrors the
  legacy ``/cloud/api/archive_cleanup`` HTTP contract — see Phase 3a.1
  review fix)

Also pins the input-clamping behavior of ``_coerce_int`` and
``_coerce_bool`` so accidental relaxation can't widen the attack
surface that lets a malicious payload bloat config.yaml.
"""

from __future__ import annotations

import json

import pytest
from unittest.mock import patch

from blueprints.storage_retention import (
    storage_retention_bp,
    _coerce_int,
    _coerce_bool,
    _resolve_cleanup_block,
    ALLOWED_FOLDER_NAMES,
    RETENTION_DAYS_MIN,
    RETENTION_DAYS_MAX,
    FREE_SPACE_PCT_MIN,
    FREE_SPACE_PCT_MAX,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_updates():
    """Captures the dict passed to update_config_yaml so tests can
    assert on what would have been written without actually touching
    config.yaml on disk."""
    return {'last': None, 'calls': 0}


@pytest.fixture
def app(captured_updates, monkeypatch):
    from flask import Flask

    flask_app = Flask(__name__)
    flask_app.secret_key = 'test-secret'
    flask_app.register_blueprint(storage_retention_bp)

    def _capture(updates):
        captured_updates['last'] = dict(updates)
        captured_updates['calls'] += 1

    monkeypatch.setattr(
        'helpers.config_updater.update_config_yaml', _capture, raising=False,
    )
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Coercers — tested in isolation because they're the security boundary
# between user input and config.yaml writes.
# ---------------------------------------------------------------------------


class TestCoerceInt:
    def test_in_range_returned_as_int(self):
        assert _coerce_int(42, default=0, lo=1, hi=100) == 42

    def test_string_int_parsed(self):
        assert _coerce_int("17", default=0, lo=1, hi=100) == 17

    def test_below_lo_clamped(self):
        assert _coerce_int(-5, default=0, lo=1, hi=100) == 1

    def test_above_hi_clamped(self):
        assert _coerce_int(9_999, default=0, lo=1, hi=100) == 100

    def test_garbage_returns_default(self):
        assert _coerce_int("not-a-number", default=42, lo=1, hi=100) == 42

    def test_none_returns_default(self):
        assert _coerce_int(None, default=42, lo=1, hi=100) == 42

    def test_bool_rejected_as_default(self):
        # isinstance(True, int) is True in Python — guard against a
        # checkbox value silently becoming 0/1 in a numeric field.
        assert _coerce_int(True, default=99, lo=1, hi=100) == 99
        assert _coerce_int(False, default=99, lo=1, hi=100) == 99


class TestCoerceBool:
    @pytest.mark.parametrize("v", [True, "true", "True", "1", "yes", "on", "checked", 1, 2.5])
    def test_truthy(self, v):
        assert _coerce_bool(v) is True

    @pytest.mark.parametrize("v", [False, "false", "0", "no", "off", "", None, 0])
    def test_falsy(self, v):
        assert _coerce_bool(v) is False


# ---------------------------------------------------------------------------
# /api/cleanup/status
# ---------------------------------------------------------------------------


class TestApiCleanupStatus:
    """The status endpoint is the page-load contract for the card."""

    def test_returns_200_with_full_shape(self, client, monkeypatch):
        monkeypatch.setattr(
            'blueprints.storage_retention._load_config_dict',
            lambda: {'cleanup': {
                'default_retention_days': 45,
                'free_space_target_pct': 12,
                'max_archive_size_gb': 100,
                'short_retention_warning_days': 7,
                'policies': {
                    'ArchivedClips': {'enabled': True, 'retention_days': 30},
                },
            }},
        )
        monkeypatch.setattr(
            'blueprints.storage_retention._watchdog_status',
            lambda: {
                'watchdog_running': True,
                'retention': {
                    'last_prune_at': '2026-05-12T19:00:00Z',
                    'last_prune_deleted': 12,
                    'last_prune_freed_bytes': 1_500_000,
                    'last_prune_kept_unsynced': 0,
                    'last_prune_error': None,
                    'next_prune_due_at': 1747084800,
                    'retention_days': 30,
                    'delete_unsynced': True,
                    'cloud_configured': False,
                },
            },
        )
        monkeypatch.setattr(
            'blueprints.storage_retention._disk_free_summary',
            lambda: {
                'path': '/home/pi/ArchivedClips', 'total_bytes': 100_000_000_000,
                'free_bytes': 30_000_000_000, 'used_bytes': 70_000_000_000,
                'free_pct': 30,
            },
        )
        r = client.get('/api/cleanup/status')
        assert r.status_code == 200
        body = r.get_json()
        assert body['success'] is True
        assert body['config']['default_retention_days'] == 45
        assert body['config']['policies']['ArchivedClips']['enabled'] is True
        assert body['resolved_retention_days'] == 30
        assert body['last_run']['deleted_count'] == 12
        assert body['last_run']['freed_bytes'] == 1_500_000
        assert body['watchdog_running'] is True
        assert body['disk']['free_pct'] == 30

    def test_handles_missing_cleanup_section_gracefully(self, client, monkeypatch):
        monkeypatch.setattr(
            'blueprints.storage_retention._load_config_dict', lambda: {},
        )
        monkeypatch.setattr(
            'blueprints.storage_retention._watchdog_status', lambda: {},
        )
        monkeypatch.setattr(
            'blueprints.storage_retention._disk_free_summary', lambda: {},
        )
        # Pin the resolver so the test doesn't depend on whatever
        # CLEANUP_DEFAULT_RETENTION_DAYS / CLOUD_ARCHIVE_RETENTION_DAYS
        # config.py happens to load at test time.
        monkeypatch.setattr(
            'services.archive_watchdog._resolve_retention_days', lambda: 30,
        )
        r = client.get('/api/cleanup/status')
        assert r.status_code == 200
        body = r.get_json()
        assert body['success'] is True
        # Defaults must be present so the form renders sensibly even on
        # a brand-new install with no cleanup section yet. The persisted
        # default_retention_days is 0 ("inherit from legacy fallback")
        # and the API surfaces the resolved value separately so the UI
        # can show a sensible number without overwriting the customization.
        assert body['config']['default_retention_days'] == 0
        assert body['config']['free_space_target_pct'] == 10
        assert body['config']['max_archive_size_gb'] == 0
        assert body['config']['policies'] == {}
        assert body['resolved_retention_days'] == 30

    def test_drops_unknown_folder_names_from_config(self, client, monkeypatch):
        # Defense in depth: even if config.yaml gets a typo'd folder
        # name (e.g. via direct edit), the status response only
        # surfaces the canonical allow-list so the UI form stays sane.
        monkeypatch.setattr(
            'blueprints.storage_retention._load_config_dict',
            lambda: {'cleanup': {'policies': {
                'BogusClips': {'enabled': True, 'retention_days': 1},
                'SentryClips': {'enabled': True, 'retention_days': 90},
            }}},
        )
        monkeypatch.setattr(
            'blueprints.storage_retention._watchdog_status', lambda: {},
        )
        monkeypatch.setattr(
            'blueprints.storage_retention._disk_free_summary', lambda: {},
        )
        monkeypatch.setattr(
            'services.archive_watchdog._resolve_retention_days', lambda: 30,
        )
        r = client.get('/api/cleanup/status')
        body = r.get_json()
        assert 'SentryClips' in body['config']['policies']
        assert 'BogusClips' not in body['config']['policies']


# ---------------------------------------------------------------------------
# /api/cleanup/policy
# ---------------------------------------------------------------------------


class TestApiCleanupPolicy:
    def test_json_payload_persisted_with_clamping(self, client, captured_updates, monkeypatch):
        monkeypatch.setattr(
            'blueprints.storage_retention._load_config_dict',
            lambda: {'cleanup': {}},
        )
        r = client.post(
            '/api/cleanup/policy',
            data=json.dumps({
                'default_retention_days': 45,
                'free_space_target_pct': 99,    # above max → clamped to 50
                'max_archive_size_gb': -10,     # below min → clamped to 0
                'short_retention_warning_days': 14,
                'policies': {
                    'ArchivedClips': {'enabled': True, 'retention_days': 30},
                    'BogusClips': {'enabled': True, 'retention_days': 1},   # dropped
                },
            }),
            content_type='application/json',
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body['success'] is True
        last = captured_updates['last']
        assert last is not None
        assert last['cleanup.default_retention_days'] == 45
        # free_space_target_pct clamped at FREE_SPACE_PCT_MAX
        assert last['cleanup.free_space_target_pct'] == FREE_SPACE_PCT_MAX
        # max_archive_size_gb clamped at MAX_ARCHIVE_GB_MIN
        assert last['cleanup.max_archive_size_gb'] == 0
        assert last['cleanup.short_retention_warning_days'] == 14
        assert 'ArchivedClips' in last['cleanup.policies']
        assert 'BogusClips' not in last['cleanup.policies']

    def test_form_payload_supported(self, client, captured_updates, monkeypatch):
        monkeypatch.setattr(
            'blueprints.storage_retention._load_config_dict',
            lambda: {'cleanup': {}},
        )
        r = client.post('/api/cleanup/policy', data={
            'default_retention_days': '60',
            'free_space_target_pct': '15',
            'max_archive_size_gb': '100',
            'short_retention_warning_days': '7',
            'policy_SentryClips_enabled': 'on',
            'policy_SentryClips_days': '90',
        })
        assert r.status_code == 200
        last = captured_updates['last']
        assert last['cleanup.default_retention_days'] == 60
        assert last['cleanup.policies']['SentryClips']['enabled'] is True
        assert last['cleanup.policies']['SentryClips']['retention_days'] == 90

    def test_garbage_payload_uses_defaults(self, client, captured_updates, monkeypatch):
        # Empty/garbage POST must still produce a sane config rather
        # than crash. ``default_retention_days`` falls back to 0 (=
        # "inherit from legacy fallback chain" — the safer choice when
        # we don't know what the user meant) instead of imposing an
        # arbitrary 30. Other scalars fall back to their UI defaults.
        monkeypatch.setattr(
            'blueprints.storage_retention._load_config_dict',
            lambda: {'cleanup': {}},
        )
        r = client.post(
            '/api/cleanup/policy',
            data=json.dumps({
                'default_retention_days': 'oops',
                'free_space_target_pct': None,
                'max_archive_size_gb': 'x',
                'short_retention_warning_days': [],
            }),
            content_type='application/json',
        )
        assert r.status_code == 200
        last = captured_updates['last']
        assert last['cleanup.default_retention_days'] == 0
        assert last['cleanup.free_space_target_pct'] == 10
        assert last['cleanup.max_archive_size_gb'] == 0
        assert last['cleanup.short_retention_warning_days'] == 7

    def test_writer_failure_returns_500(self, client, monkeypatch):
        monkeypatch.setattr(
            'blueprints.storage_retention._load_config_dict',
            lambda: {'cleanup': {}},
        )
        # Force update_config_yaml to raise — endpoint must return 500
        # with a structured success=false response, not leak a traceback.
        def boom(_updates):
            raise OSError("disk full")
        monkeypatch.setattr(
            'helpers.config_updater.update_config_yaml', boom, raising=False,
        )
        r = client.post(
            '/api/cleanup/policy',
            data=json.dumps({'default_retention_days': 30}),
            content_type='application/json',
        )
        assert r.status_code == 500
        body = r.get_json()
        assert body['success'] is False
        assert 'disk full' in body['message']

    def test_policy_rows_capped(self, client, captured_updates, monkeypatch):
        # Even if a malicious client tries to seed thousands of fake
        # folder names, the endpoint only persists the allow-listed
        # ones — and at most ALLOWED_FOLDER_NAMES of them.
        # Critically: the cap must apply AFTER the allow-list filter,
        # not before, so legitimate entries that come after garbage
        # ones survive (Phase 3a.2 PR #124 review fix).
        monkeypatch.setattr(
            'blueprints.storage_retention._load_config_dict',
            lambda: {'cleanup': {}},
        )
        many = {f'Fake{i}': {'enabled': True, 'retention_days': 1} for i in range(1000)}
        many.update({
            'SentryClips': {'enabled': True, 'retention_days': 90},
            'ArchivedClips': {'enabled': True, 'retention_days': 30},
        })
        r = client.post(
            '/api/cleanup/policy',
            data=json.dumps({'default_retention_days': 30, 'policies': many}),
            content_type='application/json',
        )
        assert r.status_code == 200
        last = captured_updates['last']
        for k in last['cleanup.policies']:
            assert k in ALLOWED_FOLDER_NAMES
        # Both legitimate entries must have made it through the
        # filter, even though they were inserted AFTER 1000 garbage
        # rows in dict insertion order.
        assert 'SentryClips' in last['cleanup.policies']
        assert 'ArchivedClips' in last['cleanup.policies']
        assert last['cleanup.policies']['SentryClips']['retention_days'] == 90
        assert last['cleanup.policies']['ArchivedClips']['retention_days'] == 30

    def test_default_retention_zero_is_persisted_verbatim(self, client, captured_updates, monkeypatch):
        # Phase 3a.2 PR #124 review fix: ``0`` is a meaningful value
        # ("inherit from legacy fallback chain"), NOT a parse failure.
        # It must be persisted verbatim, not silently clamped up to 1.
        monkeypatch.setattr(
            'blueprints.storage_retention._load_config_dict',
            lambda: {'cleanup': {}},
        )
        r = client.post(
            '/api/cleanup/policy',
            data=json.dumps({'default_retention_days': 0}),
            content_type='application/json',
        )
        assert r.status_code == 200
        last = captured_updates['last']
        assert last['cleanup.default_retention_days'] == 0


# ---------------------------------------------------------------------------
# /api/cleanup/run_now — mirrors Phase 3a.1's HTTP contract
# ---------------------------------------------------------------------------


class TestApiCleanupRunNow:
    def test_success_returns_200_with_summary(self, client):
        with patch(
            'services.video_archive_service.trigger_archive_cleanup',
            return_value={
                'deleted_count': 7, 'freed_bytes': 1_500_000,
                'scanned': 100, 'duration_seconds': 0.5,
            },
        ):
            r = client.post('/api/cleanup/run_now')
        assert r.status_code == 200
        body = r.get_json()
        assert body['success'] is True
        assert body['result']['deleted_count'] == 7

    def test_already_running_returns_200(self, client):
        # Same control-flow contract as /cloud/api/archive_cleanup —
        # already_running is normal, not an error.
        with patch(
            'services.video_archive_service.trigger_archive_cleanup',
            return_value={
                'deleted_count': 0, 'freed_bytes': 0,
                'status': 'already_running', 'duration_seconds': 0.0,
            },
        ):
            r = client.post('/api/cleanup/run_now')
        assert r.status_code == 200
        body = r.get_json()
        assert body['success'] is True
        assert body['result']['status'] == 'already_running'

    def test_watchdog_error_dict_returns_500(self, client):
        with patch(
            'services.video_archive_service.trigger_archive_cleanup',
            return_value={
                'deleted_count': 0, 'freed_bytes': 0,
                'error': 'watchdog raised: synthetic',
            },
        ):
            r = client.post('/api/cleanup/run_now')
        assert r.status_code == 500
        body = r.get_json()
        assert body['success'] is False
        assert 'synthetic' in body['message']
        assert body['result']['error'] == 'watchdog raised: synthetic'

    def test_unexpected_exception_returns_500(self, client):
        with patch(
            'services.video_archive_service.trigger_archive_cleanup',
            side_effect=RuntimeError("boom"),
        ):
            r = client.post('/api/cleanup/run_now')
        assert r.status_code == 500
        body = r.get_json()
        assert body['success'] is False
        assert 'boom' in body['message']
