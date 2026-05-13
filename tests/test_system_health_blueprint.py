"""Tests for Phase 4.2 — System Health endpoint (#101).

Verifies:

* Per-subsystem snapshot helpers (``_indexer_block``, ``_archive_block``,
  ``_cloud_block``, ``_les_block``, ``_disk_block``, ``_wifi_block``)
  produce stable severity + message under healthy, warning, error,
  disabled, and crashing conditions.
* The aggregator (``_build_health``) isolates per-subsystem crashes —
  one bad block must not 500 the page.
* ``/api/system/health`` returns a well-formed payload with the
  expected keys and a single ``overall`` rollup.
* The 30 s probe cache returns the cached value on a second call
  within the TTL and refetches after the TTL expires (verified by
  patching ``time.time``).
* ``overall.severity`` reflects the worst severity across blocks
  using the documented ``ok < unknown < warn < error`` ranking.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict
from unittest.mock import patch

import pytest

# Make sure ``scripts/web`` is on sys.path for the tests to import the
# blueprint module (the suite already does this for other blueprints,
# but we add it here defensively in case this test file is run alone).
_WEB_DIR = os.path.join(
    os.path.dirname(__file__), '..', 'scripts', 'web',
)
if _WEB_DIR not in sys.path:
    sys.path.insert(0, _WEB_DIR)


# ---------------------------------------------------------------------------
# _build_health + crash isolation
# ---------------------------------------------------------------------------

def test_build_health_returns_all_subsystems():
    from blueprints.system_health import _build_health
    payload = _build_health()
    for key in ('indexer', 'archive', 'cloud', 'live_event_sync',
                'disk', 'wifi', 'overall', 'generated_at'):
        assert key in payload, f"missing key: {key}"
    # Each block must declare a severity.
    for key in ('indexer', 'archive', 'cloud', 'live_event_sync',
                'disk', 'wifi'):
        assert payload[key].get('severity') in (
            'ok', 'warn', 'error', 'unknown'
        ), f"{key} has invalid severity: {payload[key].get('severity')}"


def test_build_health_isolates_crashing_block(monkeypatch):
    """One subsystem raising must not break the rest of the dashboard."""
    import blueprints.system_health as sh

    def boom():
        raise RuntimeError("kaboom")

    new_blocks = tuple(
        (name, boom if name == 'indexer' else fn)
        for name, fn in sh._BLOCKS
    )
    monkeypatch.setattr(sh, '_BLOCKS', new_blocks)

    payload = sh._build_health()
    assert payload['indexer']['severity'] == 'unknown'
    assert payload['indexer'].get('_error', '').startswith('kaboom')
    # Other blocks still reported.
    for key in ('archive', 'cloud', 'live_event_sync', 'disk', 'wifi'):
        assert key in payload


def test_overall_severity_ranking(monkeypatch):
    """overall == worst across blocks using ok < unknown < warn < error."""
    import blueprints.system_health as sh

    def block_ok():    return {'severity': 'ok',    'message': 'fine'}
    def block_warn():  return {'severity': 'warn',  'message': 'meh'}
    def block_err():   return {'severity': 'error', 'message': 'bad'}
    def block_unk():   return {'severity': 'unknown', 'message': 'shrug'}

    monkeypatch.setattr(sh, '_BLOCKS', (
        ('a', block_ok), ('b', block_warn), ('c', block_unk),
    ))
    out = sh._build_health()
    assert out['overall']['severity'] == 'warn'
    assert out['overall']['subsystem'] == 'b'

    monkeypatch.setattr(sh, '_BLOCKS', (
        ('a', block_ok), ('b', block_warn), ('c', block_err),
    ))
    out = sh._build_health()
    assert out['overall']['severity'] == 'error'
    assert out['overall']['subsystem'] == 'c'

    monkeypatch.setattr(sh, '_BLOCKS', (
        ('a', block_ok), ('b', block_unk),
    ))
    out = sh._build_health()
    assert out['overall']['severity'] == 'unknown'

    monkeypatch.setattr(sh, '_BLOCKS', (
        ('a', block_ok),
    ))
    out = sh._build_health()
    assert out['overall']['severity'] == 'ok'
    assert out['overall']['message'] == 'All systems normal'


# ---------------------------------------------------------------------------
# Probe cache
# ---------------------------------------------------------------------------

def test_probe_cache_returns_cached_value(monkeypatch):
    import blueprints.system_health as sh

    # Reset cache for this test.
    sh._probe_cache.clear()

    calls = {'n': 0}
    def slow_probe():
        calls['n'] += 1
        return {'value': calls['n']}

    fake_now = [1000.0]
    monkeypatch.setattr(sh.time, 'time', lambda: fake_now[0])

    a = sh._cached_probe('test', slow_probe)
    assert a == {'value': 1}
    assert calls['n'] == 1

    # Second call within TTL should hit cache.
    fake_now[0] = 1010.0
    b = sh._cached_probe('test', slow_probe)
    assert b == {'value': 1}
    assert calls['n'] == 1

    # After TTL expires, refetch.
    fake_now[0] = 1031.0
    c = sh._cached_probe('test', slow_probe)
    assert c == {'value': 2}
    assert calls['n'] == 2


def test_probe_cache_caches_failure(monkeypatch):
    """A failing probe must be cached too (don't retry every poll)."""
    import blueprints.system_health as sh
    sh._probe_cache.clear()

    calls = {'n': 0}
    def bad_probe():
        calls['n'] += 1
        raise RuntimeError("network down")

    fake_now = [1000.0]
    monkeypatch.setattr(sh.time, 'time', lambda: fake_now[0])

    a = sh._cached_probe('failing', bad_probe)
    assert a.get('_error', '').startswith('network down')

    # Same TTL window — should not re-call.
    fake_now[0] = 1015.0
    b = sh._cached_probe('failing', bad_probe)
    assert calls['n'] == 1
    assert b == a


# ---------------------------------------------------------------------------
# Disk block
# ---------------------------------------------------------------------------

def test_disk_block_critical(monkeypatch):
    import blueprints.system_health as sh
    from collections import namedtuple
    Usage = namedtuple('Usage', ['total', 'used', 'free'])
    monkeypatch.setattr(
        sh.shutil, 'disk_usage',
        lambda path: Usage(total=100 * 1024**3,
                           used=96 * 1024**3,
                           free=4 * 1024**3),
    )
    block = sh._disk_block()
    assert block['severity'] == 'error'
    assert 'Critical' in block['message']
    assert block['used_pct'] == 96.0


def test_disk_block_warn(monkeypatch):
    import blueprints.system_health as sh
    from collections import namedtuple
    Usage = namedtuple('Usage', ['total', 'used', 'free'])
    monkeypatch.setattr(
        sh.shutil, 'disk_usage',
        lambda path: Usage(total=100 * 1024**3,
                           used=88 * 1024**3,
                           free=12 * 1024**3),
    )
    block = sh._disk_block()
    assert block['severity'] == 'warn'
    assert block['used_pct'] == 88.0


def test_disk_block_ok(monkeypatch):
    import blueprints.system_health as sh
    from collections import namedtuple
    Usage = namedtuple('Usage', ['total', 'used', 'free'])
    monkeypatch.setattr(
        sh.shutil, 'disk_usage',
        lambda path: Usage(total=200 * 1024**3,
                           used=80 * 1024**3,
                           free=120 * 1024**3),
    )
    block = sh._disk_block()
    assert block['severity'] == 'ok'
    assert block['free_gb'] == 120.0
    assert 'free' in block['message']


def test_disk_block_oserror(monkeypatch):
    import blueprints.system_health as sh
    def fail(path):
        raise OSError("disk gone")
    monkeypatch.setattr(sh.shutil, 'disk_usage', fail)
    block = sh._disk_block()
    assert block['severity'] == 'unknown'
    assert 'probe failed' in block['message'].lower()


# ---------------------------------------------------------------------------
# WiFi block
# ---------------------------------------------------------------------------

def test_wifi_block_connected_strong(monkeypatch):
    import blueprints.system_health as sh
    sh._probe_cache.clear()
    monkeypatch.setattr(
        sh, '_probe_wifi_sta',
        lambda: {'connected': True, 'current_ssid': 'HomeNet', 'signal': '85'},
    )
    monkeypatch.setattr(sh, '_probe_wifi_ap', lambda: {'ap_active': False})
    block = sh._wifi_block()
    assert block['severity'] == 'ok'
    assert 'HomeNet' in block['message']
    assert block['signal'] == 85
    assert block['ap_active'] is False


def test_wifi_block_connected_weak(monkeypatch):
    import blueprints.system_health as sh
    sh._probe_cache.clear()
    monkeypatch.setattr(
        sh, '_probe_wifi_sta',
        lambda: {'connected': True, 'current_ssid': 'WeakNet', 'signal': '20'},
    )
    monkeypatch.setattr(sh, '_probe_wifi_ap', lambda: {'ap_active': False})
    block = sh._wifi_block()
    assert block['severity'] == 'warn'
    assert 'weak' in block['message'].lower()


def test_wifi_block_offline_ap_active(monkeypatch):
    import blueprints.system_health as sh
    sh._probe_cache.clear()
    monkeypatch.setattr(
        sh, '_probe_wifi_sta',
        lambda: {'connected': False, 'current_ssid': None, 'signal': None},
    )
    monkeypatch.setattr(sh, '_probe_wifi_ap', lambda: {'ap_active': True})
    block = sh._wifi_block()
    assert block['severity'] == 'warn'
    assert 'AP active' in block['message']
    assert block['ap_active'] is True


def test_wifi_block_no_wifi(monkeypatch):
    import blueprints.system_health as sh
    sh._probe_cache.clear()
    monkeypatch.setattr(
        sh, '_probe_wifi_sta',
        lambda: {'connected': False, 'current_ssid': None, 'signal': None},
    )
    monkeypatch.setattr(sh, '_probe_wifi_ap', lambda: {'ap_active': False})
    block = sh._wifi_block()
    assert block['severity'] == 'error'
    assert 'No WiFi' in block['message']


# ---------------------------------------------------------------------------
# Indexer block
# ---------------------------------------------------------------------------

def test_indexer_block_disabled(monkeypatch):
    import blueprints.system_health as sh
    monkeypatch.setattr(sh, 'MAPPING_ENABLED', False, raising=False)
    block = sh._indexer_block()
    assert block['severity'] == 'unknown'
    assert block['enabled'] is False


def test_indexer_block_running_idle(monkeypatch):
    import blueprints.system_health as sh
    from services import indexing_worker
    monkeypatch.setattr(sh, 'MAPPING_ENABLED', True, raising=False)
    monkeypatch.setattr(indexing_worker, 'get_worker_status', lambda: {
        'worker_running': True, 'queue_depth': 0,
        'dead_letter_count': 0, 'active_file': None,
    })
    block = sh._indexer_block()
    assert block['severity'] == 'ok'
    assert 'Idle' in block['message']


def test_indexer_block_dead_letter(monkeypatch):
    import blueprints.system_health as sh
    from services import indexing_worker
    monkeypatch.setattr(sh, 'MAPPING_ENABLED', True, raising=False)
    monkeypatch.setattr(indexing_worker, 'get_worker_status', lambda: {
        'worker_running': True, 'queue_depth': 5,
        'dead_letter_count': 3, 'active_file': None,
    })
    block = sh._indexer_block()
    assert block['severity'] == 'warn'
    assert '3 dead-letter' in block['message']


def test_indexer_block_not_running(monkeypatch):
    import blueprints.system_health as sh
    from services import indexing_worker
    monkeypatch.setattr(sh, 'MAPPING_ENABLED', True, raising=False)
    monkeypatch.setattr(indexing_worker, 'get_worker_status', lambda: {
        'worker_running': False, 'queue_depth': 0,
        'dead_letter_count': 0, 'active_file': None,
    })
    block = sh._indexer_block()
    assert block['severity'] == 'error'


def test_indexer_block_catchup(monkeypatch):
    import blueprints.system_health as sh
    from services import indexing_worker
    monkeypatch.setattr(sh, 'MAPPING_ENABLED', True, raising=False)
    monkeypatch.setattr(indexing_worker, 'get_worker_status', lambda: {
        'worker_running': True, 'queue_depth': 250,
        'dead_letter_count': 0, 'active_file': '/some/file.mp4',
    })
    block = sh._indexer_block()
    assert block['severity'] == 'warn'
    assert 'catch-up' in block['message']


# ---------------------------------------------------------------------------
# Archive block
# ---------------------------------------------------------------------------

def test_archive_block_disabled(monkeypatch):
    import blueprints.system_health as sh
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', False, raising=False)
    block = sh._archive_block()
    assert block['severity'] == 'unknown'


def test_archive_block_paused(monkeypatch):
    import blueprints.system_health as sh
    from services import archive_queue, archive_watchdog, archive_worker
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True, raising=False)

    monkeypatch.setattr(archive_queue, 'get_queue_status',
                        lambda: {'pending': 10, 'dead_letter': 0})
    monkeypatch.setattr(archive_watchdog, 'get_status',
                        lambda: {'severity': 'ok', 'message': ''})
    monkeypatch.setattr(archive_worker, 'get_status',
                        lambda: {'worker_running': True, 'paused': True})

    block = sh._archive_block()
    assert block['severity'] == 'warn'
    assert 'Paused' in block['message']


def test_archive_block_watchdog_error(monkeypatch):
    import blueprints.system_health as sh
    from services import archive_queue, archive_watchdog, archive_worker
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True, raising=False)

    monkeypatch.setattr(archive_queue, 'get_queue_status',
                        lambda: {'pending': 0, 'dead_letter': 0})
    monkeypatch.setattr(archive_watchdog, 'get_status',
                        lambda: {'severity': 'error',
                                 'message': 'Disk almost full'})
    monkeypatch.setattr(archive_worker, 'get_status',
                        lambda: {'worker_running': True, 'paused': False})

    block = sh._archive_block()
    assert block['severity'] == 'error'
    assert 'Disk almost full' in block['message']


# ---------------------------------------------------------------------------
# Cloud block
# ---------------------------------------------------------------------------

def test_cloud_block_disabled(monkeypatch):
    import blueprints.system_health as sh
    monkeypatch.setattr(sh, 'CLOUD_ARCHIVE_ENABLED', False, raising=False)
    block = sh._cloud_block()
    assert block['severity'] == 'unknown'


def test_cloud_block_dead_letters(monkeypatch):
    import blueprints.system_health as sh
    from services import cloud_archive_service as cas
    monkeypatch.setattr(sh, 'CLOUD_ARCHIVE_ENABLED', True, raising=False)

    monkeypatch.setattr(cas, 'count_dead_letters', lambda: 4)
    monkeypatch.setattr(cas, 'get_sync_status', lambda: {
        'running': False, 'files_total': 0, 'files_done': 0,
    })
    block = sh._cloud_block()
    assert block['severity'] == 'warn'
    assert 'dead-letter' in block['message']


def test_cloud_block_uploading(monkeypatch):
    import blueprints.system_health as sh
    from services import cloud_archive_service as cas
    monkeypatch.setattr(sh, 'CLOUD_ARCHIVE_ENABLED', True, raising=False)

    monkeypatch.setattr(cas, 'count_dead_letters', lambda: 0)
    monkeypatch.setattr(cas, 'get_sync_status', lambda: {
        'running': True, 'files_total': 100, 'files_done': 30,
    })
    block = sh._cloud_block()
    assert block['severity'] == 'ok'
    assert '70 pending' in block['message']
    assert block['queue_depth'] == 70


# ---------------------------------------------------------------------------
# LES block
# ---------------------------------------------------------------------------

def test_les_block_disabled(monkeypatch):
    import blueprints.system_health as sh
    monkeypatch.setattr(sh, 'LIVE_EVENT_SYNC_ENABLED', False, raising=False)
    block = sh._les_block()
    assert block['severity'] == 'unknown'


def test_les_block_failed_rows(monkeypatch):
    import blueprints.system_health as sh
    from services import live_event_sync_service as les
    monkeypatch.setattr(sh, 'LIVE_EVENT_SYNC_ENABLED', True, raising=False)

    monkeypatch.setattr(les, 'count_failed', lambda: 2)
    monkeypatch.setattr(les, 'get_status', lambda: {
        'worker_running': True,
        'queue_counts': {'pending': 0, 'uploading': 0},
    })
    block = sh._les_block()
    assert block['severity'] == 'warn'
    assert 'failed' in block['message']


def test_les_block_worker_idle(monkeypatch):
    import blueprints.system_health as sh
    from services import live_event_sync_service as les
    monkeypatch.setattr(sh, 'LIVE_EVENT_SYNC_ENABLED', True, raising=False)

    monkeypatch.setattr(les, 'count_failed', lambda: 0)
    monkeypatch.setattr(les, 'get_status', lambda: {
        'worker_running': False,
        'queue_counts': {'pending': 0, 'uploading': 0},
    })
    block = sh._les_block()
    assert block['severity'] == 'warn'
    assert 'idle' in block['message'].lower()


# ---------------------------------------------------------------------------
# Blueprint route
# ---------------------------------------------------------------------------

@pytest.fixture
def health_app():
    from flask import Flask
    from blueprints.system_health import system_health_bp

    app = Flask(__name__)
    app.register_blueprint(system_health_bp)
    app.config['TESTING'] = True
    return app


def test_api_returns_json_payload(health_app, monkeypatch):
    import blueprints.system_health as sh
    monkeypatch.setattr(sh, '_BLOCKS', (
        ('indexer', lambda: {'severity': 'ok', 'message': 'idle'}),
    ))
    client = health_app.test_client()
    rv = client.get('/api/system/health')
    assert rv.status_code == 200
    body = rv.get_json()
    assert 'overall' in body
    assert 'generated_at' in body
    assert body['overall']['severity'] == 'ok'
    assert body['indexer']['severity'] == 'ok'
