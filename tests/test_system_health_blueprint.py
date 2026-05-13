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


def test_wifi_block_connected_none_ssid_no_literal(monkeypatch):
    """Regression: ssid=None must not render literal 'None' in the message.

    NetworkManager occasionally returns a connected state with a missing
    SSID field (transient race during reassociation). The card text
    must fall back to 'Unknown' rather than show the Python repr of None.
    """
    import blueprints.system_health as sh
    sh._probe_cache.clear()
    monkeypatch.setattr(
        sh, '_probe_wifi_sta',
        lambda: {'connected': True, 'current_ssid': None, 'signal': '60'},
    )
    monkeypatch.setattr(sh, '_probe_wifi_ap', lambda: {'ap_active': False})
    block = sh._wifi_block()
    assert 'None' not in block['message']
    assert 'Unknown' in block['message']


def test_wifi_block_connected_empty_ssid_no_literal(monkeypatch):
    """Empty-string SSID also falls back to 'Unknown'."""
    import blueprints.system_health as sh
    sh._probe_cache.clear()
    monkeypatch.setattr(
        sh, '_probe_wifi_sta',
        lambda: {'connected': True, 'current_ssid': '', 'signal': '60'},
    )
    monkeypatch.setattr(sh, '_probe_wifi_ap', lambda: {'ap_active': False})
    block = sh._wifi_block()
    assert 'Unknown' in block['message']


def test_probe_cache_concurrent_cold_cache_no_duplicate_spawn(monkeypatch):
    """Regression: cold-cache burst must not double-spawn ``fn()``.

    Without the per-name in-flight lock, two threads that both miss the
    cache will each release the global lock and call ``fn()`` in
    parallel — defeating the "never spawn duplicate nmcli/sudo bash"
    guarantee. With the per-name lock, the second caller waits for the
    first and returns the freshly cached value.
    """
    import threading
    import blueprints.system_health as sh
    sh._probe_cache.clear()
    sh._probe_inflight.clear()

    calls = {'n': 0}
    started = threading.Event()
    proceed = threading.Event()

    def slow_probe():
        calls['n'] += 1
        started.set()
        # Hold the probe long enough that all concurrent callers
        # would observe a cache miss if there were no in-flight lock.
        proceed.wait(timeout=2.0)
        return {'value': calls['n']}

    results: list = []

    def worker():
        results.append(sh._cached_probe('concurrent', slow_probe))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    # Wait for the first thread to enter the probe.
    assert started.wait(timeout=2.0), "no thread entered the probe"
    # Let the probe complete; remaining threads must reuse the cache.
    proceed.set()
    for t in threads:
        t.join(timeout=2.0)

    assert calls['n'] == 1, f"probe spawned {calls['n']} times, expected 1"
    assert len(results) == 5
    for r in results:
        assert r == {'value': 1}


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


def test_archive_block_files_lost_24h_warns(monkeypatch):
    """Phase 4.3 — non-zero lost_24h must surface as warn with a
    user-facing message that includes the count."""
    import blueprints.system_health as sh
    from services import archive_queue, archive_watchdog, archive_worker
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True, raising=False)

    monkeypatch.setattr(archive_queue, 'get_queue_status',
                        lambda: {'pending': 0, 'dead_letter': 0})
    monkeypatch.setattr(archive_queue, 'count_source_gone_recent',
                        lambda hours=24: 12)
    monkeypatch.setattr(archive_watchdog, 'get_status',
                        lambda: {'severity': 'ok', 'message': ''})
    monkeypatch.setattr(archive_worker, 'get_status',
                        lambda: {'worker_running': True, 'paused': False})

    block = sh._archive_block()
    assert block['severity'] == 'warn'
    assert block['lost_24h'] == 12
    assert '12' in block['message']
    assert 'lost' in block['message'].lower()


def test_archive_block_files_lost_pluralization(monkeypatch):
    """Singular vs plural messages for 1 vs N clips lost."""
    import blueprints.system_health as sh
    from services import archive_queue, archive_watchdog, archive_worker
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True, raising=False)
    monkeypatch.setattr(archive_queue, 'get_queue_status',
                        lambda: {'pending': 0, 'dead_letter': 0})
    monkeypatch.setattr(archive_queue, 'count_source_gone_recent',
                        lambda hours=24: 1)
    monkeypatch.setattr(archive_watchdog, 'get_status',
                        lambda: {'severity': 'ok'})
    monkeypatch.setattr(archive_worker, 'get_status',
                        lambda: {'worker_running': True, 'paused': False})

    block = sh._archive_block()
    assert '1 clip lost' in block['message']
    assert '1 clips' not in block['message']


def test_archive_block_files_lost_takes_precedence_over_dead_letters(
        monkeypatch):
    """Lost files dominate dead-letter rows because lost footage is
    unrecoverable, while DL rows still have the source on disk."""
    import blueprints.system_health as sh
    from services import archive_queue, archive_watchdog, archive_worker
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True, raising=False)
    monkeypatch.setattr(archive_queue, 'get_queue_status',
                        lambda: {'pending': 0, 'dead_letter': 5})
    monkeypatch.setattr(archive_queue, 'count_source_gone_recent',
                        lambda hours=24: 3)
    monkeypatch.setattr(archive_watchdog, 'get_status',
                        lambda: {'severity': 'ok'})
    monkeypatch.setattr(archive_worker, 'get_status',
                        lambda: {'worker_running': True, 'paused': False})

    block = sh._archive_block()
    assert 'lost' in block['message'].lower()
    assert 'dead-letter' not in block['message'].lower()


def test_archive_block_lost_24h_zero_means_ok(monkeypatch):
    """Zero lost files must not bump severity."""
    import blueprints.system_health as sh
    from services import archive_queue, archive_watchdog, archive_worker
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True, raising=False)
    monkeypatch.setattr(archive_queue, 'get_queue_status',
                        lambda: {'pending': 0, 'dead_letter': 0})
    monkeypatch.setattr(archive_queue, 'count_source_gone_recent',
                        lambda hours=24: 0)
    monkeypatch.setattr(archive_watchdog, 'get_status',
                        lambda: {'severity': 'ok'})
    monkeypatch.setattr(archive_worker, 'get_status',
                        lambda: {'worker_running': True, 'paused': False})

    block = sh._archive_block()
    assert block['severity'] == 'ok'
    assert block['lost_24h'] == 0


def test_archive_block_count_source_gone_failure_safe(monkeypatch):
    """If count_source_gone_recent throws, _archive_block must
    degrade to lost_24h=0, not 500 the dashboard."""
    import blueprints.system_health as sh
    from services import archive_queue, archive_watchdog, archive_worker
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True, raising=False)
    monkeypatch.setattr(archive_queue, 'get_queue_status',
                        lambda: {'pending': 0, 'dead_letter': 0})
    def boom(hours=24):
        raise RuntimeError("DB locked")
    monkeypatch.setattr(archive_queue, 'count_source_gone_recent', boom)
    monkeypatch.setattr(archive_watchdog, 'get_status',
                        lambda: {'severity': 'ok'})
    monkeypatch.setattr(archive_worker, 'get_status',
                        lambda: {'worker_running': True, 'paused': False})

    block = sh._archive_block()
    assert block['lost_24h'] == 0
    assert block['severity'] == 'ok'


def test_archive_block_disabled_includes_lost_24h_field(monkeypatch):
    """The disabled block must still include lost_24h: 0 so JS
    consumers don't have to special-case missing keys."""
    import blueprints.system_health as sh
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', False, raising=False)
    block = sh._archive_block()
    assert block['lost_24h'] == 0


# ---------------------------------------------------------------------------
# Phase 4.4 (#101) — drain-rate ETA in archive block
# ---------------------------------------------------------------------------

def test_archive_block_eta_appears_in_message_when_pending_with_rate(
        monkeypatch):
    """When pending > 0 AND a usable ETA is available, it must appear
    in the user-facing message (e.g., '15 pending — est. 5 min')."""
    import blueprints.system_health as sh
    from services import archive_queue, archive_watchdog, archive_worker
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True, raising=False)
    monkeypatch.setattr(archive_queue, 'get_queue_status',
                        lambda: {'pending': 15, 'dead_letter': 0})
    monkeypatch.setattr(archive_queue, 'count_source_gone_recent',
                        lambda hours=24: 0)
    monkeypatch.setattr(archive_watchdog, 'get_status',
                        lambda: {'severity': 'ok'})
    monkeypatch.setattr(archive_worker, 'get_status', lambda: {
        'worker_running': True, 'paused': False,
        'eta_seconds': 300,           # 5 minutes
        'drain_rate_per_sec': 0.05,
        'drain_rate_samples': 10,
        'drain_rate_stale': False,
    })
    block = sh._archive_block()
    assert block['severity'] == 'ok'
    assert '15 pending' in block['message']
    assert 'est.' in block['message']
    assert '5 min' in block['message']
    assert block['eta_seconds'] == 300
    assert block['eta_human'] == '5 min'
    assert block['drain_rate_per_sec'] == 0.05


def test_archive_block_eta_appears_in_catchup_warn(monkeypatch):
    """Even at the >200 pending warn level, ETA must still be shown."""
    import blueprints.system_health as sh
    from services import archive_queue, archive_watchdog, archive_worker
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True, raising=False)
    monkeypatch.setattr(archive_queue, 'get_queue_status',
                        lambda: {'pending': 1233, 'dead_letter': 0})
    monkeypatch.setattr(archive_queue, 'count_source_gone_recent',
                        lambda hours=24: 0)
    monkeypatch.setattr(archive_watchdog, 'get_status',
                        lambda: {'severity': 'ok'})
    monkeypatch.setattr(archive_worker, 'get_status', lambda: {
        'worker_running': True, 'paused': False,
        'eta_seconds': 2820,        # 47 minutes — matches the issue spec
        'drain_rate_per_sec': 0.44,
        'drain_rate_samples': 50,
        'drain_rate_stale': False,
    })
    block = sh._archive_block()
    assert block['severity'] == 'warn'
    # Spec quote: "Archiving 1 233 pending — est. 47 minutes at current rate."
    assert '1233 pending' in block['message']
    assert 'est. 47 min' in block['message']


def test_archive_block_no_eta_falls_back_to_legacy_message(monkeypatch):
    """When eta_seconds is None (cold start, stale window, etc.), the
    block must still render the legacy pending message."""
    import blueprints.system_health as sh
    from services import archive_queue, archive_watchdog, archive_worker
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True, raising=False)
    monkeypatch.setattr(archive_queue, 'get_queue_status',
                        lambda: {'pending': 500, 'dead_letter': 0})
    monkeypatch.setattr(archive_queue, 'count_source_gone_recent',
                        lambda hours=24: 0)
    monkeypatch.setattr(archive_watchdog, 'get_status',
                        lambda: {'severity': 'ok'})
    monkeypatch.setattr(archive_worker, 'get_status', lambda: {
        'worker_running': True, 'paused': False,
        'eta_seconds': None,
        'drain_rate_per_sec': None,
        'drain_rate_samples': 0,
        'drain_rate_stale': False,
    })
    block = sh._archive_block()
    assert block['severity'] == 'warn'
    assert '500 pending' in block['message']
    assert 'est.' not in block['message']
    assert block['eta_human'] is None


def test_archive_block_disabled_includes_eta_fields(monkeypatch):
    """The disabled block must include the new ETA fields so JS
    consumers don't have to special-case missing keys (mirrors the
    Phase 4.3 lost_24h contract)."""
    import blueprints.system_health as sh
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', False, raising=False)
    block = sh._archive_block()
    assert block['eta_seconds'] is None
    assert block['eta_human'] is None
    assert block['drain_rate_per_sec'] is None


def test_archive_block_status_failure_includes_eta_fields(monkeypatch):
    """When the inner subsystem fetch raises, the safety-net block
    must still include the ETA fields so JS doesn't break."""
    import blueprints.system_health as sh
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True, raising=False)
    # Force an import-time raise inside the try block.
    def _explode(*a, **kw):
        raise RuntimeError("simulated")
    from services import archive_queue
    monkeypatch.setattr(archive_queue, 'get_queue_status', _explode)
    block = sh._archive_block()
    assert block['severity'] == 'unknown'
    assert block['eta_seconds'] is None
    assert block['eta_human'] is None
    assert block['drain_rate_per_sec'] is None


def test_format_eta_human_boundaries():
    """Server-side formatter must match the JS ``fmtEta`` exactly so
    System Health card and Archive chip don't show different strings."""
    import blueprints.system_health as sh
    assert sh._format_eta_human(45) == '<1 min'
    assert sh._format_eta_human(60) == '1 min'
    assert sh._format_eta_human(120) == '2 min'
    assert sh._format_eta_human(3600) == '1 h'
    assert sh._format_eta_human(5400) == '1 h 30 min'
    assert sh._format_eta_human(24 * 3600) == '24 h'


# ---------------------------------------------------------------------------
# Phase 4.5 (#101) — pause-reason in archive block
# ---------------------------------------------------------------------------

def test_format_pause_reason_load_only():
    """Spec quote: 'Archive paused: load 4.2 > 3.5 threshold'."""
    import blueprints.system_health as sh
    out = sh._format_pause_reason(
        load_pause={
            'is_paused_now': True,
            'last_loadavg': 4.2,
            'threshold': 3.5,
        },
        disk_pause={'is_paused_now': False},
    )
    assert out == 'load 4.2 > 3.5'


def test_format_pause_reason_disk_only_with_total():
    """Spec quote: 'Archive paused: SD card 96% full'."""
    import blueprints.system_health as sh
    # 96% full = 4% free → e.g. 1024 MB free of 25600 MB total.
    out = sh._format_pause_reason(
        load_pause={'is_paused_now': False},
        disk_pause={
            'is_paused_now': True,
            'last_free_mb': 1024,
            'last_total_mb': 25600,
            'critical_threshold_mb': 100,
        },
    )
    assert out == 'SD card 96% full'


def test_format_pause_reason_disk_only_without_total():
    """When ``last_total_mb`` is unknown, fall back to MB-free
    rendering with the threshold for context."""
    import blueprints.system_health as sh
    out = sh._format_pause_reason(
        load_pause={'is_paused_now': False},
        disk_pause={
            'is_paused_now': True,
            'last_free_mb': 50,
            'last_total_mb': None,
            'critical_threshold_mb': 100,
        },
    )
    assert out == 'SD card 50 MB free (threshold 100 MB)'


def test_format_pause_reason_disk_full_capped_at_99():
    """100% full claims would be misleading — the OS always reserves
    a few MB. Cap at 99% so the message stays honest."""
    import blueprints.system_health as sh
    out = sh._format_pause_reason(
        load_pause={'is_paused_now': False},
        disk_pause={
            'is_paused_now': True,
            'last_free_mb': 0,
            'last_total_mb': 25600,
            'critical_threshold_mb': 100,
        },
    )
    assert out == 'SD card 99% full'


def test_format_pause_reason_both_armed():
    """Concurrent load + disk pauses join with a semicolon."""
    import blueprints.system_health as sh
    out = sh._format_pause_reason(
        load_pause={
            'is_paused_now': True,
            'last_loadavg': 5.1,
            'threshold': 3.5,
        },
        disk_pause={
            'is_paused_now': True,
            'last_free_mb': 1024,
            'last_total_mb': 25600,
            'critical_threshold_mb': 100,
        },
    )
    assert out == 'load 5.1 > 3.5; SD card 96% full'


def test_format_pause_reason_neither_armed_returns_background():
    """When neither auto-pause has armed (e.g. ``pause_worker()``
    was called for a mode switch), don't claim false specificity —
    return ``'background'`` so the caller can render a generic
    message."""
    import blueprints.system_health as sh
    assert sh._format_pause_reason(
        load_pause={'is_paused_now': False},
        disk_pause={'is_paused_now': False},
    ) == 'background'


def test_format_pause_reason_load_armed_but_no_loadavg_yet():
    """Defensive: if the worker reports paused but ``last_loadavg``
    is None (cold-start race), don't synthesize a fake number — fall
    through to ``'background'``."""
    import blueprints.system_health as sh
    out = sh._format_pause_reason(
        load_pause={
            'is_paused_now': True,
            'last_loadavg': None,
            'threshold': 3.5,
        },
        disk_pause={'is_paused_now': False},
    )
    assert out == 'background'


def test_format_pause_reason_disk_negative_free_falls_through():
    """``last_free_mb = -1`` is the sentinel for OSError on
    ``shutil.disk_usage``. Don't render '-1 MB free'."""
    import blueprints.system_health as sh
    out = sh._format_pause_reason(
        load_pause={'is_paused_now': False},
        disk_pause={
            'is_paused_now': True,
            'last_free_mb': -1,
            'last_total_mb': -1,
            'critical_threshold_mb': 100,
        },
    )
    assert out == 'background'


def test_archive_block_paused_load_message(monkeypatch):
    """End-to-end: load pause armed → message includes the loadavg
    and the threshold."""
    import blueprints.system_health as sh
    from services import archive_queue, archive_watchdog, archive_worker
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True, raising=False)
    monkeypatch.setattr(archive_queue, 'get_queue_status',
                        lambda: {'pending': 10, 'dead_letter': 0})
    monkeypatch.setattr(archive_queue, 'count_source_gone_recent',
                        lambda hours=24: 0)
    monkeypatch.setattr(archive_watchdog, 'get_status',
                        lambda: {'severity': 'ok'})
    monkeypatch.setattr(archive_worker, 'get_status', lambda: {
        'worker_running': True, 'paused': True,
        'load_pause': {
            'is_paused_now': True, 'last_loadavg': 4.2, 'threshold': 3.5,
        },
        'disk_pause': {'is_paused_now': False},
    })
    block = sh._archive_block()
    assert block['severity'] == 'warn'
    assert block['message'] == 'Paused: load 4.2 > 3.5'
    assert block['pause_reason'] == 'load 4.2 > 3.5'


def test_archive_block_load_auto_paused_without_manual_flag(monkeypatch):
    """``archive_worker.get_status()`` only sets ``paused=True`` for
    the manual ``pause_worker()`` flag — not for auto-armed
    ``_load_pause_until``. Phase 4.5 (#101) broadens the
    operator-facing paused notion in ``_archive_block`` so the
    System Health card surfaces the load auto-pause too.

    Pinned in production: smoke test on the Pi caught
    ``load_pause.is_paused_now=True`` while ``paused=False`` —
    without this broadening the card would silently drop the pause
    state entirely."""
    import blueprints.system_health as sh
    from services import archive_queue, archive_watchdog, archive_worker
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True, raising=False)
    monkeypatch.setattr(archive_queue, 'get_queue_status',
                        lambda: {'pending': 442, 'dead_letter': 0})
    monkeypatch.setattr(archive_queue, 'count_source_gone_recent',
                        lambda hours=24: 0)
    monkeypatch.setattr(archive_watchdog, 'get_status',
                        lambda: {'severity': 'ok'})
    # paused=False (no manual flag) but load_pause is armed.
    monkeypatch.setattr(archive_worker, 'get_status', lambda: {
        'worker_running': True, 'paused': False,
        'load_pause': {
            'is_paused_now': True, 'last_loadavg': 3.9, 'threshold': 3.5,
        },
        'disk_pause': {'is_paused_now': False},
    })
    block = sh._archive_block()
    # The operator-facing paused field must now reflect the
    # auto-pause, not the narrower manual flag.
    assert block['paused'] is True
    assert block['severity'] == 'warn'
    assert block['message'] == 'Paused: load 3.9 > 3.5'
    assert block['pause_reason'] == 'load 3.9 > 3.5'


def test_archive_block_disk_auto_paused_without_manual_flag(monkeypatch):
    """Mirror of the load auto-pause test for the disk-space guard.
    ``_disk_space_pause_until`` arms independently of the manual
    pause flag; both Phase 4.5 ``paused`` field and
    ``pause_reason`` must surface it."""
    import blueprints.system_health as sh
    from services import archive_queue, archive_watchdog, archive_worker
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True, raising=False)
    monkeypatch.setattr(archive_queue, 'get_queue_status',
                        lambda: {'pending': 5, 'dead_letter': 0})
    monkeypatch.setattr(archive_queue, 'count_source_gone_recent',
                        lambda hours=24: 0)
    monkeypatch.setattr(archive_watchdog, 'get_status',
                        lambda: {'severity': 'ok'})
    monkeypatch.setattr(archive_worker, 'get_status', lambda: {
        'worker_running': True, 'paused': False,
        'load_pause': {'is_paused_now': False},
        'disk_pause': {
            'is_paused_now': True,
            'last_free_mb': 1024, 'last_total_mb': 25600,
            'critical_threshold_mb': 100,
        },
    })
    block = sh._archive_block()
    assert block['paused'] is True
    assert block['severity'] == 'warn'
    assert block['message'] == 'Paused: SD card 96% full'
    assert block['pause_reason'] == 'SD card 96% full'


def test_archive_block_paused_disk_message(monkeypatch):
    """End-to-end: disk pause armed → message includes the % full."""
    import blueprints.system_health as sh
    from services import archive_queue, archive_watchdog, archive_worker
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True, raising=False)
    monkeypatch.setattr(archive_queue, 'get_queue_status',
                        lambda: {'pending': 0, 'dead_letter': 0})
    monkeypatch.setattr(archive_queue, 'count_source_gone_recent',
                        lambda hours=24: 0)
    monkeypatch.setattr(archive_watchdog, 'get_status',
                        lambda: {'severity': 'ok'})
    monkeypatch.setattr(archive_worker, 'get_status', lambda: {
        'worker_running': True, 'paused': True,
        'load_pause': {'is_paused_now': False},
        'disk_pause': {
            'is_paused_now': True,
            'last_free_mb': 1024, 'last_total_mb': 25600,
            'critical_threshold_mb': 100,
        },
    })
    block = sh._archive_block()
    assert block['severity'] == 'warn'
    assert block['message'] == 'Paused: SD card 96% full'
    assert block['pause_reason'] == 'SD card 96% full'


def test_archive_block_paused_background_message(monkeypatch):
    """When the worker reports ``paused`` but no auto-guard has
    armed (manual ``pause_worker()`` from a mode switch), render the
    generic 'Paused (background task)' fallback."""
    import blueprints.system_health as sh
    from services import archive_queue, archive_watchdog, archive_worker
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True, raising=False)
    monkeypatch.setattr(archive_queue, 'get_queue_status',
                        lambda: {'pending': 5, 'dead_letter': 0})
    monkeypatch.setattr(archive_queue, 'count_source_gone_recent',
                        lambda hours=24: 0)
    monkeypatch.setattr(archive_watchdog, 'get_status',
                        lambda: {'severity': 'ok'})
    monkeypatch.setattr(archive_worker, 'get_status', lambda: {
        'worker_running': True, 'paused': True,
        'load_pause': {'is_paused_now': False},
        'disk_pause': {'is_paused_now': False},
    })
    block = sh._archive_block()
    assert block['severity'] == 'warn'
    assert block['message'] == 'Paused (background task)'
    assert block['pause_reason'] == 'background'


def test_archive_block_pause_reason_field_present_in_all_paths(monkeypatch):
    """The ``pause_reason`` key must appear in every return path
    (disabled, error, normal) so JS consumers don't crash on missing
    keys (mirrors the Phase 4.3 lost_24h / Phase 4.4 ETA contracts)."""
    import blueprints.system_health as sh
    # Disabled path
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', False, raising=False)
    assert 'pause_reason' in sh._archive_block()
    assert sh._archive_block()['pause_reason'] is None
    # Error path
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True, raising=False)
    from services import archive_queue
    monkeypatch.setattr(archive_queue, 'get_queue_status',
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    block = sh._archive_block()
    assert 'pause_reason' in block
    assert block['pause_reason'] is None


def test_archive_block_pause_reason_null_when_not_paused(monkeypatch):
    """``pause_reason`` should be ``None`` when ``paused=False``,
    not a stale value, so the UI doesn't render 'Paused: ...' next to
    a green status."""
    import blueprints.system_health as sh
    from services import archive_queue, archive_watchdog, archive_worker
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True, raising=False)
    monkeypatch.setattr(archive_queue, 'get_queue_status',
                        lambda: {'pending': 0, 'dead_letter': 0})
    monkeypatch.setattr(archive_queue, 'count_source_gone_recent',
                        lambda hours=24: 0)
    monkeypatch.setattr(archive_watchdog, 'get_status',
                        lambda: {'severity': 'ok'})
    monkeypatch.setattr(archive_worker, 'get_status', lambda: {
        'worker_running': True, 'paused': False,
        'load_pause': {'is_paused_now': False},
        'disk_pause': {'is_paused_now': False},
    })
    block = sh._archive_block()
    assert block['paused'] is False
    assert block['pause_reason'] is None


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


# ---------------------------------------------------------------------------
# /api/system/clear_lost_clips — issue #163 dismiss banner endpoint
# ---------------------------------------------------------------------------

def test_clear_lost_clips_disabled_returns_zero(health_app, monkeypatch):
    """When ``ARCHIVE_QUEUE_ENABLED`` is False the endpoint must
    short-circuit and never even import the queue module."""
    import blueprints.system_health as sh
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', False)
    client = health_app.test_client()
    rv = client.post('/api/system/clear_lost_clips', json={})
    assert rv.status_code == 200
    body = rv.get_json()
    assert body['rows_deleted'] == 0
    assert body['enabled'] is False


def test_clear_lost_clips_calls_service(health_app, monkeypatch):
    import blueprints.system_health as sh
    import services
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True)
    captured = {}

    class _FakeArchiveQueue:
        @staticmethod
        def delete_source_gone(*, older_than_hours=None, db_path=None):
            captured['older_than_hours'] = older_than_hours
            return 7

    monkeypatch.setattr(services, 'archive_queue', _FakeArchiveQueue)
    client = health_app.test_client()
    rv = client.post('/api/system/clear_lost_clips', json={})
    assert rv.status_code == 200
    body = rv.get_json()
    assert body['rows_deleted'] == 7
    assert captured['older_than_hours'] is None


def test_clear_lost_clips_passes_older_than_hours(health_app, monkeypatch):
    import blueprints.system_health as sh
    import services
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True)
    captured = {}

    class _FakeArchiveQueue:
        @staticmethod
        def delete_source_gone(*, older_than_hours=None, db_path=None):
            captured['older_than_hours'] = older_than_hours
            return 3

    monkeypatch.setattr(services, 'archive_queue', _FakeArchiveQueue)
    client = health_app.test_client()
    rv = client.post(
        '/api/system/clear_lost_clips', json={'older_than_hours': 48})
    assert rv.status_code == 200
    assert rv.get_json()['rows_deleted'] == 3
    assert captured['older_than_hours'] == 48


def test_clear_lost_clips_rejects_non_integer_hours(health_app, monkeypatch):
    import blueprints.system_health as sh
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True)
    client = health_app.test_client()
    rv = client.post(
        '/api/system/clear_lost_clips',
        json={'older_than_hours': 'forever'})
    assert rv.status_code == 400
    assert 'error' in rv.get_json()


def test_clear_lost_clips_handles_service_crash(health_app, monkeypatch):
    import blueprints.system_health as sh
    import services
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True)

    class _Boom:
        @staticmethod
        def delete_source_gone(*, older_than_hours=None, db_path=None):
            raise RuntimeError("disk on fire")

    monkeypatch.setattr(services, 'archive_queue', _Boom)
    client = health_app.test_client()
    rv = client.post('/api/system/clear_lost_clips', json={})
    assert rv.status_code == 500
    assert 'error' in rv.get_json()


def test_clear_lost_clips_accepts_empty_body(health_app, monkeypatch):
    """The Dismiss button POSTs ``{}`` — and a missing/empty body
    must also work (some HTTP clients send neither Content-Type nor
    body for a no-arg POST)."""
    import blueprints.system_health as sh
    import services
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True)

    class _FakeArchiveQueue:
        @staticmethod
        def delete_source_gone(*, older_than_hours=None, db_path=None):
            return 0

    monkeypatch.setattr(services, 'archive_queue', _FakeArchiveQueue)
    client = health_app.test_client()
    rv = client.post('/api/system/clear_lost_clips')
    assert rv.status_code == 200
    assert rv.get_json()['rows_deleted'] == 0


# ===========================================================================
# /api/system/clear_lost_clips — tombstone (PR #169 follow-up)
# ===========================================================================
#
# The dismiss endpoint now returns ``dismissed_at`` so the UI can
# confirm the suppression took, and a tombstone-read failure must NOT
# 500 a DELETE that already committed.


def test_clear_lost_clips_returns_tombstone(health_app, monkeypatch):
    """A successful dismiss returns ``dismissed_at`` so the UI can
    show "Dismissed at HH:MM:SS" if it wants to (and so a smoke test
    can confirm the tombstone was written)."""
    import blueprints.system_health as sh
    import services
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True)

    class _FakeArchiveQueue:
        @staticmethod
        def delete_source_gone(*, older_than_hours=None, db_path=None):
            return 12

        @staticmethod
        def get_lost_dismissed_at():
            return '2026-05-13T17:00:00+00:00'

    monkeypatch.setattr(services, 'archive_queue', _FakeArchiveQueue)
    client = health_app.test_client()
    rv = client.post('/api/system/clear_lost_clips', json={})
    assert rv.status_code == 200
    body = rv.get_json()
    assert body['rows_deleted'] == 12
    assert body['dismissed_at'] == '2026-05-13T17:00:00+00:00'


def test_clear_lost_clips_tombstone_read_failure_still_returns_200(
    health_app, monkeypatch,
):
    """If the tombstone-read raises (disk error, missing helper),
    the DELETE has already committed — the endpoint MUST still return
    200 with ``dismissed_at: null`` so the UI doesn't show an error
    for a dismiss that actually succeeded."""
    import blueprints.system_health as sh
    import services
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True)

    class _FakeArchiveQueue:
        @staticmethod
        def delete_source_gone(*, older_than_hours=None, db_path=None):
            return 5

        @staticmethod
        def get_lost_dismissed_at():
            raise OSError('synthetic state-file error')

    monkeypatch.setattr(services, 'archive_queue', _FakeArchiveQueue)
    client = health_app.test_client()
    rv = client.post('/api/system/clear_lost_clips', json={})
    assert rv.status_code == 200
    body = rv.get_json()
    assert body['rows_deleted'] == 5
    assert body['dismissed_at'] is None


def test_clear_lost_clips_older_than_hours_skips_tombstone_lookup(
    health_app, monkeypatch,
):
    """Forensic ``older_than_hours`` purges aren't user
    acknowledgments and don't write a tombstone — so the response
    omits the lookup too (returns ``dismissed_at: null``)."""
    import blueprints.system_health as sh
    import services
    monkeypatch.setattr(sh, 'ARCHIVE_QUEUE_ENABLED', True)
    lookup_called = {'flag': False}

    class _FakeArchiveQueue:
        @staticmethod
        def delete_source_gone(*, older_than_hours=None, db_path=None):
            return 9

        @staticmethod
        def get_lost_dismissed_at():
            lookup_called['flag'] = True
            return '2026-01-01T00:00:00+00:00'

    monkeypatch.setattr(services, 'archive_queue', _FakeArchiveQueue)
    client = health_app.test_client()
    rv = client.post(
        '/api/system/clear_lost_clips', json={'older_than_hours': 48})
    assert rv.status_code == 200
    body = rv.get_json()
    assert body['rows_deleted'] == 9
    assert body['dismissed_at'] is None
    assert lookup_called['flag'] is False