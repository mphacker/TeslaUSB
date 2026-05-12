"""Tests for scripts/web/blueprints/cloud_archive.py POST /settings.

Phase 1 item 1.3 + PR #96 review fix: the save_settings handler must NOT
write ``cloud_archive.delete_unsynced`` to config.yaml when no provider
is connected. Browsers do not submit disabled checkboxes, so the
template's disabled "Keep clips until backed up" toggle would always
look like ``False`` (i.e. ``delete_unsynced=True``) on POST. Persisting
that value would silently override the documented null/auto-default and
break the auto-protection promised when the user later connects a
provider.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def captured_updates():
    """Mutable holder so the patched _update_config_yaml records its call."""
    return {'last': None, 'count': 0}


@pytest.fixture
def app(captured_updates, monkeypatch):
    """Build a minimal Flask app with just the cloud_archive blueprint.

    Stubs ``_update_config_yaml`` so the test never touches the real
    config.yaml on disk. Stubs ``_get_cloud_config_cached`` per-test so
    different provider states can be exercised. Patches
    ``os.path.isfile`` per-test so the creds-file check resolves to the
    desired connected/not-connected state.
    """
    from flask import Flask
    from blueprints.cloud_archive import cloud_archive_bp
    import blueprints.cloud_archive as ca_bp

    def fake_update(updates):
        captured_updates['last'] = dict(updates)
        captured_updates['count'] += 1

    monkeypatch.setattr(ca_bp, '_update_config_yaml', fake_update)

    flask_app = Flask(__name__)
    flask_app.secret_key = 'test-only'
    flask_app.register_blueprint(cloud_archive_bp)
    flask_app.config['TESTING'] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def base_form():
    """Minimum form fields the route needs to not crash on coercion."""
    return {
        'sync_folders': 'SentryClips',
        'priority_order': 'event,trip',
        'max_upload_mbps': '5',
        'cloud_reserve_gb': '1',
        'cloud_min_retention_days': '30',
    }


class TestSaveSettingsCloudProtectionPersistence:
    """PR #96 review fix: persistence of ``delete_unsynced`` must depend on
    whether a provider is currently connected."""

    def test_no_provider_skips_writing_delete_unsynced(
        self, client, base_form, captured_updates,
    ):
        """When no provider is configured, the form's missing
        ``keep_clips_until_synced`` must NOT cause ``delete_unsynced=True``
        to be written. The config key must be omitted entirely so the
        documented null/auto-default is preserved.
        """
        with patch(
            'blueprints.cloud_archive._get_cloud_config_cached',
            return_value={'provider': ''},
        ), patch(
            'blueprints.cloud_archive.os.path.isfile', return_value=False,
        ):
            r = client.post('/cloud/settings',
                            data=base_form,
                            follow_redirects=False)
        assert r.status_code in (302, 303)
        last = captured_updates['last']
        assert last is not None
        assert 'cloud_archive.delete_unsynced' not in last, (
            "delete_unsynced must NOT be written when provider is not "
            "connected (would override the null/auto-default)"
        )
        assert last['cloud_archive.max_upload_mbps'] == 5

    def test_provider_set_but_creds_missing_skips_write(
        self, client, base_form, captured_updates,
    ):
        """provider_connected requires BOTH a non-empty provider name
        AND the creds file present. If creds file is missing
        (e.g. user removed it manually), the toggle is rendered
        disabled and we must skip the write.
        """
        with patch(
            'blueprints.cloud_archive._get_cloud_config_cached',
            return_value={'provider': 'dropbox'},
        ), patch(
            'blueprints.cloud_archive.os.path.isfile', return_value=False,
        ):
            r = client.post('/cloud/settings',
                            data=base_form,
                            follow_redirects=False)
        assert r.status_code in (302, 303)
        last = captured_updates['last']
        assert 'cloud_archive.delete_unsynced' not in last

    def test_provider_connected_writes_delete_unsynced_true_when_unchecked(
        self, client, base_form, captured_updates,
    ):
        """With provider connected and the keep-toggle unchecked, the
        user is explicitly opting out of cloud protection — write
        ``delete_unsynced=True``.
        """
        with patch(
            'blueprints.cloud_archive._get_cloud_config_cached',
            return_value={'provider': 'dropbox'},
        ), patch(
            'blueprints.cloud_archive.os.path.isfile', return_value=True,
        ):
            r = client.post('/cloud/settings',
                            data=base_form,
                            follow_redirects=False)
        assert r.status_code in (302, 303)
        last = captured_updates['last']
        assert last is not None
        assert last.get('cloud_archive.delete_unsynced') is True

    def test_provider_connected_writes_delete_unsynced_false_when_checked(
        self, client, base_form, captured_updates,
    ):
        """With provider connected and the keep-toggle checked, the
        user wants protection — write ``delete_unsynced=False``.
        """
        form = dict(base_form)
        form['keep_clips_until_synced'] = 'on'
        with patch(
            'blueprints.cloud_archive._get_cloud_config_cached',
            return_value={'provider': 'dropbox'},
        ), patch(
            'blueprints.cloud_archive.os.path.isfile', return_value=True,
        ):
            r = client.post('/cloud/settings',
                            data=form,
                            follow_redirects=False)
        assert r.status_code in (302, 303)
        last = captured_updates['last']
        assert last is not None
        assert last.get('cloud_archive.delete_unsynced') is False

    def test_no_provider_other_settings_still_persist(
        self, client, base_form, captured_updates,
    ):
        """Skipping ``delete_unsynced`` MUST NOT block the other fields
        on the form from being saved. Regression guard against an
        accidental short-circuit.
        """
        form = dict(base_form)
        form['max_upload_mbps'] = '15'
        form['cloud_min_retention_days'] = '45'
        with patch(
            'blueprints.cloud_archive._get_cloud_config_cached',
            return_value={'provider': ''},
        ), patch(
            'blueprints.cloud_archive.os.path.isfile', return_value=False,
        ):
            r = client.post('/cloud/settings',
                            data=form,
                            follow_redirects=False)
        assert r.status_code in (302, 303)
        last = captured_updates['last']
        assert last['cloud_archive.max_upload_mbps'] == 15
        assert last['cloud_archive.cloud_min_retention_days'] == 45


class TestSaveSettingsRetryCapClamping:
    """Phase 2.6: ``cloud_retry_max_attempts`` must be clamped to 1-20
    on POST. Out-of-range or non-numeric input must fall back to the
    documented default (5) — never crash the form handler.
    """

    def test_in_range_value_persists(
        self, client, base_form, captured_updates,
    ):
        form = dict(base_form)
        form['cloud_retry_max_attempts'] = '7'
        with patch(
            'blueprints.cloud_archive._get_cloud_config_cached',
            return_value={'provider': ''},
        ), patch(
            'blueprints.cloud_archive.os.path.isfile', return_value=False,
        ):
            r = client.post('/cloud/settings',
                            data=form,
                            follow_redirects=False)
        assert r.status_code in (302, 303)
        last = captured_updates['last']
        assert last['cloud_archive.retry_max_attempts'] == 7

    def test_above_max_clamped_to_twenty(
        self, client, base_form, captured_updates,
    ):
        form = dict(base_form)
        form['cloud_retry_max_attempts'] = '99'
        with patch(
            'blueprints.cloud_archive._get_cloud_config_cached',
            return_value={'provider': ''},
        ), patch(
            'blueprints.cloud_archive.os.path.isfile', return_value=False,
        ):
            r = client.post('/cloud/settings',
                            data=form,
                            follow_redirects=False)
        assert r.status_code in (302, 303)
        last = captured_updates['last']
        assert last['cloud_archive.retry_max_attempts'] == 20

    def test_below_min_clamped_to_one(
        self, client, base_form, captured_updates,
    ):
        form = dict(base_form)
        form['cloud_retry_max_attempts'] = '0'
        with patch(
            'blueprints.cloud_archive._get_cloud_config_cached',
            return_value={'provider': ''},
        ), patch(
            'blueprints.cloud_archive.os.path.isfile', return_value=False,
        ):
            r = client.post('/cloud/settings',
                            data=form,
                            follow_redirects=False)
        assert r.status_code in (302, 303)
        last = captured_updates['last']
        assert last['cloud_archive.retry_max_attempts'] == 1

    def test_negative_clamped_to_one(
        self, client, base_form, captured_updates,
    ):
        form = dict(base_form)
        form['cloud_retry_max_attempts'] = '-5'
        with patch(
            'blueprints.cloud_archive._get_cloud_config_cached',
            return_value={'provider': ''},
        ), patch(
            'blueprints.cloud_archive.os.path.isfile', return_value=False,
        ):
            r = client.post('/cloud/settings',
                            data=form,
                            follow_redirects=False)
        assert r.status_code in (302, 303)
        last = captured_updates['last']
        assert last['cloud_archive.retry_max_attempts'] == 1

    def test_non_numeric_falls_back_to_default(
        self, client, base_form, captured_updates,
    ):
        form = dict(base_form)
        form['cloud_retry_max_attempts'] = 'abc'
        with patch(
            'blueprints.cloud_archive._get_cloud_config_cached',
            return_value={'provider': ''},
        ), patch(
            'blueprints.cloud_archive.os.path.isfile', return_value=False,
        ):
            r = client.post('/cloud/settings',
                            data=form,
                            follow_redirects=False)
        assert r.status_code in (302, 303)
        last = captured_updates['last']
        # Default is 5 (matches config.yaml seed).
        assert last['cloud_archive.retry_max_attempts'] == 5

    def test_missing_field_falls_back_to_default(
        self, client, base_form, captured_updates,
    ):
        # base_form does NOT include cloud_retry_max_attempts at all —
        # POST handler must still write the default rather than crash
        # or skip the key.
        with patch(
            'blueprints.cloud_archive._get_cloud_config_cached',
            return_value={'provider': ''},
        ), patch(
            'blueprints.cloud_archive.os.path.isfile', return_value=False,
        ):
            r = client.post('/cloud/settings',
                            data=base_form,
                            follow_redirects=False)
        assert r.status_code in (302, 303)
        last = captured_updates['last']
        assert last['cloud_archive.retry_max_attempts'] == 5


class TestArchiveCleanupHttpContract:
    """Phase 3a.1 (#98 / closes #91) post-review fix: ``POST /api/archive_cleanup``
    must preserve the legacy 500-on-error HTTP contract that the endpoint
    advertised before the refactor.

    Before the refactor: ``trigger_archive_cleanup`` raised on watchdog
    failure; the blueprint's outer ``try/except`` produced HTTP 500.
    After the refactor: the shim swallows watchdog exceptions and
    returns ``{'error': '...'}``; the blueprint must convert that
    structured error back into an HTTP 500 so external automation /
    front-end code that keys on the status code keeps working.

    Successful runs (including the watchdog's ``status='already_running'``
    short-circuit, which is normal control flow, not an error) MUST
    return HTTP 200.
    """

    def test_successful_prune_returns_200(self, client):
        with patch(
            'services.video_archive_service.trigger_archive_cleanup',
            return_value={
                'deleted_count': 7,
                'freed_bytes': 1_400_000,
                'scanned': 100,
                'kept_unsynced_count': 0,
                'duration_seconds': 0.42,
            },
        ):
            r = client.post('/cloud/api/archive_cleanup')
        assert r.status_code == 200
        body = r.get_json()
        assert body['success'] is True
        assert body['result']['deleted_count'] == 7
        assert body['result']['freed_bytes'] == 1_400_000

    def test_already_running_returns_200(self, client):
        # The watchdog's ``_retention_running`` guard short-circuits with
        # a synthetic summary carrying ``status='already_running'``.
        # That's a normal control-flow signal — NOT an error — so the
        # endpoint MUST return 200 so the front end can render
        # "Cleanup already in progress" instead of an error toast.
        with patch(
            'services.video_archive_service.trigger_archive_cleanup',
            return_value={
                'deleted_count': 0,
                'freed_bytes': 0,
                'scanned': 0,
                'status': 'already_running',
                'duration_seconds': 0.001,
            },
        ):
            r = client.post('/cloud/api/archive_cleanup')
        assert r.status_code == 200
        body = r.get_json()
        assert body['success'] is True
        assert body['result']['status'] == 'already_running'

    def test_watchdog_error_dict_returns_500(self, client):
        # The shim catches watchdog exceptions and converts them to
        # ``{'error': '<exc>', ...}``. The blueprint MUST translate
        # that back into an HTTP 500 + ``success=False`` so the legacy
        # contract is preserved. Without this translation, callers
        # would treat watchdog crashes as silent successes.
        with patch(
            'services.video_archive_service.trigger_archive_cleanup',
            return_value={
                'deleted_count': 0,
                'freed_bytes': 0,
                'scanned': 0,
                'error': 'watchdog raised: synthetic',
            },
        ):
            r = client.post('/cloud/api/archive_cleanup')
        assert r.status_code == 500
        body = r.get_json()
        assert body['success'] is False
        assert 'synthetic' in body['message']
        # The structured error dict is preserved in the response so
        # debuggers can see the full context (cutoff, scanned, etc.).
        assert body['result']['error'] == 'watchdog raised: synthetic'

    def test_watchdog_not_started_returns_500(self, client):
        # ``force_prune_now`` returns ``{'error': 'watchdog not started', ...}``
        # without raising when the daemon isn't running. Per the legacy
        # contract this is still an error condition (the user clicked
        # "Run cleanup now" and nothing happened), so 500 is correct.
        with patch(
            'services.video_archive_service.trigger_archive_cleanup',
            return_value={
                'deleted_count': 0,
                'freed_bytes': 0,
                'scanned': 0,
                'error': 'watchdog not started',
            },
        ):
            r = client.post('/cloud/api/archive_cleanup')
        assert r.status_code == 500
        body = r.get_json()
        assert body['success'] is False
        assert body['message'] == 'watchdog not started'

    def test_unexpected_exception_returns_500(self, client):
        # Defense in depth: even if the shim itself raises (e.g.
        # ImportError on the local import), the endpoint's outer
        # try/except must still return a clean 500 rather than
        # leaking a traceback.
        with patch(
            'services.video_archive_service.trigger_archive_cleanup',
            side_effect=RuntimeError("synthetic"),
        ):
            r = client.post('/cloud/api/archive_cleanup')
        assert r.status_code == 500
        body = r.get_json()
        assert body['success'] is False
        assert 'synthetic' in body['message']
