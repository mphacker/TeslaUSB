"""Tests for Phase 4.1 — unified Failed Jobs page (#101).

Covers:

* Service-level helpers added in this PR:
    - ``cloud_archive_service.list_dead_letters`` / ``retry_dead_letter``
    - ``archive_queue.list_dead_letters`` / ``retry_dead_letter``
    - ``indexing_queue_service.list_dead_letters`` / ``retry_dead_letter``
* Blueprint:
    - ``GET  /api/jobs/counts``
    - ``GET  /api/jobs/failed`` (all + per-subsystem + bad subsystem)
    - ``POST /api/jobs/retry`` (each subsystem, single-id + retry-all,
      bad subsystem, missing subsystem)
    - ``GET  /jobs`` (HTML shell renders even when DBs are empty)
* Resilience: one subsystem crashing does not break the others.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from services import archive_queue, indexing_queue_service
from services.archive_queue import enqueue_for_archive
from services.mapping_service import _init_db


# ---------------------------------------------------------------------------
# Service-level helper tests
# ---------------------------------------------------------------------------

@pytest.fixture
def geo_db(tmp_path):
    db_path = str(tmp_path / "geodata.db")
    conn = _init_db(db_path)
    conn.close()
    return db_path


@pytest.fixture
def cam_clip(tmp_path):
    f = tmp_path / "RecentClips" / "clip.mp4"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x" * 100)
    return str(f)


# --- archive_queue helpers ---------------------------------------------------

def _force_archive_dead_letter(db_path: str, source_path: str) -> int:
    """Use the public record_failure cap to force one row to dead_letter."""
    inserted = enqueue_for_archive(source_path, db_path=db_path)
    assert inserted
    # Look up the auto-assigned row id (enqueue_for_archive returns bool).
    with sqlite3.connect(db_path) as conn:
        rid = conn.execute(
            "SELECT id FROM archive_queue WHERE source_path=?",
            (source_path,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE archive_queue SET status='dead_letter', attempts=99, "
            "last_error='boom' WHERE id=?",
            (rid,),
        )
        conn.commit()
    return rid


def test_archive_list_dead_letters_returns_only_dl(geo_db, tmp_path):
    a = tmp_path / "a.mp4"; a.write_bytes(b"a")
    b = tmp_path / "b.mp4"; b.write_bytes(b"b")
    enqueue_for_archive(str(a), db_path=geo_db)  # stays pending
    rid_b = _force_archive_dead_letter(geo_db, str(b))

    rows = archive_queue.list_dead_letters(db_path=geo_db, limit=10)
    assert len(rows) == 1
    assert rows[0]['id'] == rid_b
    assert rows[0]['status'] == 'dead_letter'


def test_archive_list_dead_letters_limit_and_zero(geo_db, tmp_path):
    for i in range(3):
        f = tmp_path / f"f{i}.mp4"; f.write_bytes(b"x")
        _force_archive_dead_letter(geo_db, str(f))

    assert len(archive_queue.list_dead_letters(db_path=geo_db, limit=2)) == 2
    assert archive_queue.list_dead_letters(db_path=geo_db, limit=0) == []
    assert archive_queue.list_dead_letters(db_path=geo_db, limit=-5) == []


def test_archive_retry_dead_letter_single_id(geo_db, tmp_path):
    f = tmp_path / "x.mp4"; f.write_bytes(b"x")
    rid = _force_archive_dead_letter(geo_db, str(f))

    n = archive_queue.retry_dead_letter(row_id=rid, db_path=geo_db)
    assert n == 1

    # Row should now be back to pending with attempts=0 and clean state.
    # last_error is intentionally PRESERVED so operators retain failure
    # context until the next worker attempt overwrites it.
    with sqlite3.connect(geo_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, attempts, last_error, claimed_by, claimed_at "
            "FROM archive_queue WHERE id = ?", (rid,)
        ).fetchone()
    assert row['status'] == 'pending'
    assert row['attempts'] == 0
    assert row['last_error'] == 'boom'  # preserved
    assert row['claimed_by'] is None
    assert row['claimed_at'] is None


def test_archive_retry_dead_letter_all(geo_db, tmp_path):
    ids = []
    for i in range(3):
        f = tmp_path / f"f{i}.mp4"; f.write_bytes(b"x")
        ids.append(_force_archive_dead_letter(geo_db, str(f)))

    n = archive_queue.retry_dead_letter(row_id=None, db_path=geo_db)
    assert n == 3
    assert archive_queue.list_dead_letters(db_path=geo_db, limit=10) == []


def test_archive_retry_dead_letter_skips_non_dl(geo_db, tmp_path):
    f = tmp_path / "x.mp4"; f.write_bytes(b"x")
    rid = enqueue_for_archive(str(f), db_path=geo_db)  # stays pending
    n = archive_queue.retry_dead_letter(row_id=rid, db_path=geo_db)
    assert n == 0


def test_archive_count_dead_letters(geo_db, tmp_path):
    assert archive_queue.count_dead_letters(db_path=geo_db) == 0
    for i in range(3):
        f = tmp_path / f"f{i}.mp4"; f.write_bytes(b"x")
        _force_archive_dead_letter(geo_db, str(f))
    # Add a healthy row to verify it's not counted.
    healthy = tmp_path / "ok.mp4"; healthy.write_bytes(b"x")
    enqueue_for_archive(str(healthy), db_path=geo_db)
    assert archive_queue.count_dead_letters(db_path=geo_db) == 3


# --- indexing_queue helpers --------------------------------------------------

def _force_indexer_dead_letter(db_path: str, file_path: str) -> str:
    """Push an indexing_queue row past _PARSE_ERROR_MAX_ATTEMPTS."""
    indexing_queue_service.enqueue_for_indexing(db_path, file_path,
                                                source='test')
    # Look up the canonical_key the service stored.
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT canonical_key FROM indexing_queue WHERE file_path=?",
            (file_path,),
        ).fetchone()
    key = row['canonical_key']
    cap = indexing_queue_service._PARSE_ERROR_MAX_ATTEMPTS
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE indexing_queue SET attempts=?, last_error='boom' "
            "WHERE canonical_key=?",
            (cap + 5, key),
        )
        conn.commit()
    return key


def test_indexer_list_dead_letters(geo_db, cam_clip):
    key = _force_indexer_dead_letter(geo_db, cam_clip)
    rows = indexing_queue_service.list_dead_letters(geo_db, limit=10)
    assert len(rows) == 1
    assert rows[0]['canonical_key'] == key
    assert rows[0]['attempts'] >= indexing_queue_service._PARSE_ERROR_MAX_ATTEMPTS


def test_indexer_list_dead_letters_excludes_healthy(geo_db, cam_clip, tmp_path):
    # One dead-letter, one healthy.
    _force_indexer_dead_letter(geo_db, cam_clip)
    healthy = tmp_path / "RecentClips" / "ok.mp4"
    healthy.write_bytes(b"x")
    indexing_queue_service.enqueue_for_indexing(geo_db, str(healthy),
                                                source='test')
    rows = indexing_queue_service.list_dead_letters(geo_db, limit=10)
    assert len(rows) == 1


def test_indexer_retry_dead_letter_single_key(geo_db, cam_clip):
    key = _force_indexer_dead_letter(geo_db, cam_clip)
    n = indexing_queue_service.retry_dead_letter(geo_db,
                                                 canonical_key_value=key)
    assert n == 1
    assert indexing_queue_service.list_dead_letters(geo_db, limit=10) == []


def test_indexer_retry_dead_letter_all(geo_db, cam_clip, tmp_path):
    _force_indexer_dead_letter(geo_db, cam_clip)
    other = tmp_path / "RecentClips" / "other.mp4"
    other.write_bytes(b"x")
    _force_indexer_dead_letter(geo_db, str(other))

    n = indexing_queue_service.retry_dead_letter(geo_db,
                                                 canonical_key_value=None)
    assert n == 2


def test_indexer_count_dead_letters(geo_db, cam_clip, tmp_path):
    assert indexing_queue_service.count_dead_letters(geo_db) == 0
    _force_indexer_dead_letter(geo_db, cam_clip)
    assert indexing_queue_service.count_dead_letters(geo_db) == 1
    other = tmp_path / "RecentClips" / "other.mp4"
    other.write_bytes(b"x")
    _force_indexer_dead_letter(geo_db, str(other))
    assert indexing_queue_service.count_dead_letters(geo_db) == 2


def test_indexer_retry_preserves_last_error(geo_db, cam_clip):
    """Retry resets attempts but preserves last_error for triage context."""
    key = _force_indexer_dead_letter(geo_db, cam_clip)
    indexing_queue_service.retry_dead_letter(geo_db,
                                             canonical_key_value=key)
    with sqlite3.connect(geo_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT attempts, last_error FROM indexing_queue "
            "WHERE canonical_key=?", (key,),
        ).fetchone()
    assert row['attempts'] == 0
    assert row['last_error'] == 'boom'  # preserved


# ---------------------------------------------------------------------------
# Redactor tests
# ---------------------------------------------------------------------------

def test_redactor_strips_local_paths():
    from blueprints.jobs import _redact_last_error
    s = _redact_last_error(
        "copy: OSError('moov atom missing: /mnt/gadget/part1-ro/TeslaCam/foo.mp4')"
    )
    assert '/mnt/gadget/' not in s
    assert '<path>' in s


def test_redactor_strips_home_paths():
    from blueprints.jobs import _redact_last_error
    s = _redact_last_error("Permission denied: /home/pi/TeslaUSB/state.txt")
    assert '/home/pi' not in s
    assert '<path>' in s


def test_redactor_strips_rclone_remote():
    from blueprints.jobs import _redact_last_error
    s = _redact_last_error("rclone: 2025/01/01 ERROR myremote:bucket/path/x.mp4 not found")
    assert 'myremote:bucket' not in s
    assert '<remote>' in s


def test_redactor_caps_length():
    from blueprints.jobs import _redact_last_error, _REDACT_MAX_LEN
    s = _redact_last_error('x' * (_REDACT_MAX_LEN + 100))
    assert len(s) <= _REDACT_MAX_LEN + 5  # allow for the ellipsis suffix
    assert s.endswith('…')


def test_redactor_handles_none_and_empty():
    from blueprints.jobs import _redact_last_error
    assert _redact_last_error(None) == ''
    assert _redact_last_error('') == ''


def test_listers_apply_redaction(geo_db, tmp_path, monkeypatch):
    """Production lister must redact identifiers it gets from the DB."""
    f = tmp_path / "x.mp4"; f.write_bytes(b"x")
    rid = enqueue_for_archive(str(f), db_path=geo_db)
    with sqlite3.connect(geo_db) as conn:
        conn.execute(
            "UPDATE archive_queue SET status='dead_letter', attempts=99, "
            "last_error=? WHERE id=?",
            ("error from /mnt/gadget/part1-ro/TeslaCam/x.mp4", rid),
        )
        conn.commit()

    import config as config_module
    monkeypatch.setattr(config_module, 'MAPPING_DB_PATH', geo_db,
                        raising=False)
    import blueprints.jobs as jobs_module
    monkeypatch.setattr(jobs_module, 'MAPPING_DB_PATH', geo_db, raising=False)

    rows = jobs_module._archive_rows(10)
    assert len(rows) == 1
    assert '/mnt/gadget/' not in rows[0]['last_error']
    assert '<path>' in rows[0]['last_error']


# ---------------------------------------------------------------------------
# Blueprint tests
# ---------------------------------------------------------------------------

@pytest.fixture
def app(monkeypatch, tmp_path, geo_db):
    """Build a minimal Flask app with just the jobs blueprint mounted.

    Patches the three subsystem listers so each test controls what
    rows the blueprint sees, without needing real DBs for the
    cloud_archive subsystem.
    """
    from flask import Flask
    from blueprints.jobs import jobs_bp
    import blueprints.jobs as jobs_module
    import config as config_module

    # Make sure config flags don't suppress subsystems.
    monkeypatch.setattr(config_module, 'CLOUD_ARCHIVE_ENABLED', True,
                        raising=False)
    monkeypatch.setattr(config_module, 'MAPPING_ENABLED', True,
                        raising=False)
    monkeypatch.setattr(config_module, 'MAPPING_DB_PATH', geo_db,
                        raising=False)
    monkeypatch.setattr(jobs_module, 'CLOUD_ARCHIVE_ENABLED', True,
                        raising=False)
    monkeypatch.setattr(jobs_module, 'MAPPING_ENABLED', True,
                        raising=False)
    monkeypatch.setattr(jobs_module, 'MAPPING_DB_PATH', geo_db,
                        raising=False)

    flask_app = Flask(
        __name__,
        template_folder=os.path.join(
            os.path.dirname(__file__), '..', 'scripts', 'web', 'templates',
        ),
        static_folder=os.path.join(
            os.path.dirname(__file__), '..', 'scripts', 'web', 'static',
        ),
    )
    flask_app.secret_key = 'test-only'
    flask_app.register_blueprint(jobs_bp)
    flask_app.config['TESTING'] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _patch_lister(monkeypatch, name, rows):
    import blueprints.jobs as jobs_module
    monkeypatch.setitem(jobs_module._LISTERS, name, lambda limit: rows[:limit])


def _patch_retrier(monkeypatch, name, fn):
    import blueprints.jobs as jobs_module
    monkeypatch.setitem(jobs_module._RETRIERS, name, fn)


def _patch_counter(monkeypatch, name, value):
    import blueprints.jobs as jobs_module
    monkeypatch.setitem(jobs_module._COUNTERS, name, lambda: value)


def test_counts_all_zero(client, monkeypatch):
    for name in ('archive', 'indexer', 'cloud_sync'):
        _patch_counter(monkeypatch, name, 0)
    rv = client.get('/api/jobs/counts')
    assert rv.status_code == 200
    body = rv.get_json()
    assert body == {'archive': 0, 'indexer': 0, 'cloud_sync': 0,
                    'total': 0}


def test_counts_with_rows(client, monkeypatch):
    _patch_counter(monkeypatch, 'archive', 1)
    _patch_counter(monkeypatch, 'indexer', 0)
    _patch_counter(monkeypatch, 'cloud_sync', 1)
    rv = client.get('/api/jobs/counts')
    body = rv.get_json()
    assert body['archive'] == 1
    assert body['cloud_sync'] == 1
    assert body['total'] == 2


def test_counts_one_subsystem_crashing(client, monkeypatch):
    """A crashing counter must not break the page — it returns 0."""
    import blueprints.jobs as jobs_module
    def boom():
        raise RuntimeError("boom")
    monkeypatch.setitem(jobs_module._COUNTERS, 'archive', boom)
    _patch_counter(monkeypatch, 'indexer', 5)
    _patch_counter(monkeypatch, 'cloud_sync', 0)
    rv = client.get('/api/jobs/counts')
    body = rv.get_json()
    assert body['archive'] == 0  # crashed → 0
    assert body['indexer'] == 5
    assert body['total'] == 5


def test_failed_all_subsystems(client, monkeypatch):
    rows = {
        'archive': [{'subsystem': 'archive', 'id': 1, 'identifier': 'a',
                     'attempts': 5, 'last_error': '', 'enqueued_at': None,
                     'extra': {}}],
        'indexer': [{'subsystem': 'indexer', 'id': 'k', 'identifier': 'i',
                     'attempts': 3, 'last_error': '', 'enqueued_at': None,
                     'extra': {}}],
        'cloud_sync': [],
    }
    for name, r in rows.items():
        _patch_lister(monkeypatch, name, r)

    rv = client.get('/api/jobs/failed')
    assert rv.status_code == 200
    body = rv.get_json()
    assert body['subsystem'] == 'all'
    assert body['count'] == 2
    subs = {r['subsystem'] for r in body['rows']}
    assert subs == {'archive', 'indexer'}


def test_failed_per_subsystem(client, monkeypatch):
    _patch_lister(monkeypatch, 'archive', [{'subsystem': 'archive', 'id': 1,
                                            'identifier': 'x', 'attempts': 5,
                                            'last_error': 'e',
                                            'enqueued_at': None, 'extra': {}}])
    _patch_lister(monkeypatch, 'indexer', [])
    _patch_lister(monkeypatch, 'cloud_sync', [])

    rv = client.get('/api/jobs/failed?subsystem=archive')
    assert rv.status_code == 200
    body = rv.get_json()
    assert body['subsystem'] == 'archive'
    assert body['count'] == 1
    assert body['rows'][0]['identifier'] == 'x'


def test_failed_unknown_subsystem(client):
    rv = client.get('/api/jobs/failed?subsystem=bogus')
    assert rv.status_code == 400
    assert 'allowed' in rv.get_json()


def test_failed_one_subsystem_crashing_does_not_break_others(client,
                                                             monkeypatch):
    def boom(limit):
        raise RuntimeError("boom")

    import blueprints.jobs as jobs_module
    monkeypatch.setitem(jobs_module._LISTERS, 'archive', boom)
    _patch_lister(monkeypatch, 'indexer', [{'subsystem': 'indexer',
                                            'id': 'k', 'identifier': 'i',
                                            'attempts': 5, 'last_error': '',
                                            'enqueued_at': None, 'extra': {}}])
    _patch_lister(monkeypatch, 'cloud_sync', [])

    rv = client.get('/api/jobs/failed')
    assert rv.status_code == 200
    body = rv.get_json()
    # Indexer row still surfaces; archive crash silently swallowed.
    subs = {r['subsystem'] for r in body['rows']}
    assert 'indexer' in subs
    assert 'archive' not in subs


def test_failed_limit_param(client, monkeypatch):
    rows = [{'subsystem': 'archive', 'id': i, 'identifier': str(i),
             'attempts': 5, 'last_error': '', 'enqueued_at': None,
             'extra': {}} for i in range(10)]
    _patch_lister(monkeypatch, 'archive', rows)
    _patch_lister(monkeypatch, 'indexer', [])
    _patch_lister(monkeypatch, 'cloud_sync', [])

    rv = client.get('/api/jobs/failed?subsystem=archive&limit=3')
    body = rv.get_json()
    assert body['count'] == 3


@pytest.mark.parametrize('subsystem', ['archive', 'indexer',
                                       'cloud_sync'])
def test_retry_dispatches_per_subsystem(client, monkeypatch, subsystem):
    calls = {'count': 0, 'last': None}

    def fake(row_id):
        calls['count'] += 1
        calls['last'] = row_id
        return 1

    _patch_retrier(monkeypatch, subsystem, fake)
    rv = client.post('/api/jobs/retry',
                     data=json.dumps({'subsystem': subsystem, 'id': 42}),
                     content_type='application/json')
    assert rv.status_code == 200
    assert rv.get_json() == {'subsystem': subsystem, 'rows_reset': 1}
    assert calls['count'] == 1
    assert calls['last'] == 42


def test_retry_all_in_subsystem(client, monkeypatch):
    captured = {}

    def fake(row_id):
        captured['row_id'] = row_id
        return 7

    _patch_retrier(monkeypatch, 'archive', fake)
    rv = client.post('/api/jobs/retry',
                     data=json.dumps({'subsystem': 'archive', 'id': None}),
                     content_type='application/json')
    assert rv.status_code == 200
    assert rv.get_json() == {'subsystem': 'archive', 'rows_reset': 7}
    assert captured['row_id'] is None


def test_retry_missing_subsystem(client):
    rv = client.post('/api/jobs/retry',
                     data=json.dumps({'id': 1}),
                     content_type='application/json')
    assert rv.status_code == 400


def test_retry_unknown_subsystem(client):
    rv = client.post('/api/jobs/retry',
                     data=json.dumps({'subsystem': 'bogus', 'id': 1}),
                     content_type='application/json')
    assert rv.status_code == 400


def test_retry_handler_exception_returns_500(client, monkeypatch):
    def boom(row_id):
        raise RuntimeError("boom")

    _patch_retrier(monkeypatch, 'archive', boom)
    rv = client.post('/api/jobs/retry',
                     data=json.dumps({'subsystem': 'archive', 'id': 1}),
                     content_type='application/json')
    assert rv.status_code == 500


def test_html_route_registered(app):
    # The Failed Jobs page is verified end-to-end by the deploy smoke
    # test. Here we just confirm the route is wired so a typo in the
    # blueprint registration would fail at unit-test time. Rendering
    # the actual template requires every other blueprint to be
    # registered (base.html uses url_for for nav links), which would
    # turn this into an integration test.
    rules = {r.endpoint for r in app.url_map.iter_rules()}
    assert 'jobs.failed_jobs_page' in rules
    assert 'jobs.api_counts' in rules
    assert 'jobs.api_failed' in rules
    assert 'jobs.api_retry' in rules


# ---------------------------------------------------------------------------
# Issue #180 — clip-value classifier, recommendation classifier, and
# subsystem='all' retry/delete fan-out.
# ---------------------------------------------------------------------------

class TestClipValueClassifier:
    """``_classify_clip_value`` translates the row identifier into a
    deterministic value-tier so the UI can show an at-a-glance "how
    irreplaceable is this clip" badge.
    """

    def test_sentryclips_path_is_event_tier(self):
        from blueprints.jobs import _classify_clip_value
        v = _classify_clip_value(
            'archive',
            '/mnt/gadget/part1-ro/TeslaCam/SentryClips/2025-10-29_10-39-36/'
            'front.mp4',
        )
        assert v['tier'] == 'event'
        assert v['label'] == 'Event clip'

    def test_savedclips_path_is_event_tier(self):
        from blueprints.jobs import _classify_clip_value
        v = _classify_clip_value(
            'cloud_sync',
            '/home/pi/ArchivedClips/SavedClips/2025-11-01_08-00-00/back.mp4',
        )
        # Even though it's in ArchivedClips, the SavedClips
        # sub-folder beats the archived check (it's a saved event).
        assert v['tier'] == 'event'

    def test_recentclips_path_is_recent_tier(self):
        from blueprints.jobs import _classify_clip_value
        v = _classify_clip_value(
            'archive',
            '/mnt/gadget/part1-ro/TeslaCam/RecentClips/2025-10-29_10-39-36-'
            'front.mp4',
        )
        assert v['tier'] == 'recent'

    def test_archivedclips_path_is_archived_tier(self):
        from blueprints.jobs import _classify_clip_value
        v = _classify_clip_value(
            'cloud_sync',
            '/home/pi/ArchivedClips/2025-10-29/clip.mp4',
        )
        assert v['tier'] == 'archived'

    def test_indexer_subsystem_with_unknown_path_falls_back_to_index(self):
        from blueprints.jobs import _classify_clip_value
        v = _classify_clip_value('indexer', '/some/other/path.mp4')
        assert v['tier'] == 'index'

    def test_cloud_sync_subsystem_with_unknown_path_falls_back_to_cloud(self):
        from blueprints.jobs import _classify_clip_value
        v = _classify_clip_value('cloud_sync', '/some/other/path.mp4')
        assert v['tier'] == 'cloud'

    def test_unknown_subsystem_and_path_returns_unknown_tier(self):
        from blueprints.jobs import _classify_clip_value
        v = _classify_clip_value('archive', 'just-some-string')
        assert v['tier'] == 'unknown'

    def test_empty_identifier_does_not_crash(self):
        from blueprints.jobs import _classify_clip_value
        v = _classify_clip_value('archive', '')
        assert 'tier' in v
        assert 'label' in v
        assert 'description' in v

    def test_case_insensitive_path_match(self):
        from blueprints.jobs import _classify_clip_value
        # Tesla writes mixed-case folders; the classifier lowercases
        # before matching so /TeslaCam/SentryClips/ and
        # /teslacam/sentryclips/ both classify the same.
        upper = _classify_clip_value(
            'archive', '/TeslaCam/SentryClips/x.mp4')
        lower = _classify_clip_value(
            'archive', '/teslacam/sentryclips/x.mp4')
        assert upper['tier'] == lower['tier'] == 'event'


class TestRecommendationClassifier:
    """``_classify_recommendation`` maps a redacted ``last_error``
    string to a Retry / Delete / Either action recommendation. The
    operator can always override; this just steers them to the right
    button by default.
    """

    def test_file_missing_recommends_delete(self):
        from blueprints.jobs import _classify_recommendation
        for err in (
            'No such file or directory',
            '[Errno 2] ENOENT: file not found',
            'source_gone',
            'File does not exist',
            'File missing',
        ):
            r = _classify_recommendation('archive', err)
            assert r['action'] == 'delete', f"failed on {err!r}"

    def test_parse_error_recommends_delete(self):
        from blueprints.jobs import _classify_recommendation
        for err in (
            'Invalid data found when processing input',
            'moov atom not found',
            'truncated',
            'parse error',
            'unsupported codec',
            'corrupt header',
        ):
            r = _classify_recommendation('indexer', err)
            assert r['action'] == 'delete', f"failed on {err!r}"

    def test_network_error_recommends_retry(self):
        from blueprints.jobs import _classify_recommendation
        for err in (
            'Connection refused',
            'connection reset by peer',
            'Connection timed out',
            'Network is unreachable',
            'temporary failure in name resolution',
            'no route to host',
            'TLS handshake timed out',
            'getaddrinfo: name or service not known',
            'dial tcp 1.2.3.4:443: i/o timeout',
        ):
            r = _classify_recommendation('cloud_sync', err)
            assert r['action'] == 'retry', f"failed on {err!r}"

    def test_auth_quota_recommends_retry(self):
        from blueprints.jobs import _classify_recommendation
        for err in (
            'HTTP 401 Unauthorized',
            'HTTP 403 Forbidden',
            'access denied',
            'Invalid credential',
            'Quota exceeded',
            'Out of space on device',
            'HTTP 429 too many requests',
        ):
            r = _classify_recommendation('cloud_sync', err)
            assert r['action'] == 'retry', f"failed on {err!r}"

    def test_io_error_recommends_retry(self):
        from blueprints.jobs import _classify_recommendation
        for err in (
            'I/O error',
            'Input/output error',
            'Stale file handle',
            'Device or resource busy',
        ):
            r = _classify_recommendation('archive', err)
            assert r['action'] == 'retry', f"failed on {err!r}"

    def test_permission_recommends_retry(self):
        from blueprints.jobs import _classify_recommendation
        r = _classify_recommendation(
            'archive', '[Errno 13] Permission denied')
        assert r['action'] == 'retry'
        r2 = _classify_recommendation(
            'archive', 'Read-only file system')
        assert r2['action'] == 'retry'

    def test_lock_contention_recommends_retry(self):
        from blueprints.jobs import _classify_recommendation
        r = _classify_recommendation(
            'archive', 'lock timeout while waiting for coordinator')
        assert r['action'] == 'retry'

    def test_empty_error_returns_either(self):
        from blueprints.jobs import _classify_recommendation
        for err in ('', None, '   '):
            r = _classify_recommendation('archive', err)
            assert r['action'] == 'either', f"failed on {err!r}"
            assert r['reason']

    def test_unknown_error_with_few_attempts_returns_either(self):
        from blueprints.jobs import _classify_recommendation
        r = _classify_recommendation(
            'archive', 'WeirdNeverSeenBeforeError: kaboom', attempts=2)
        assert r['action'] == 'either'

    def test_unknown_error_with_many_attempts_recommends_delete(self):
        from blueprints.jobs import _classify_recommendation
        # Heuristic: if a row has been retried 5+ times and still
        # doesn't match any known recoverable pattern, the failure
        # is probably stuck — push the operator toward delete so the
        # worker isn't hammering the same broken row forever.
        r = _classify_recommendation(
            'archive', 'WeirdNeverSeenBeforeError: kaboom', attempts=5)
        assert r['action'] == 'delete'
        assert '5' in r['reason']

    def test_response_shape_is_stable(self):
        from blueprints.jobs import _classify_recommendation
        r = _classify_recommendation(
            'archive', 'No such file or directory', attempts=3)
        assert set(r.keys()) == {'action', 'reason'}
        assert isinstance(r['action'], str)
        assert isinstance(r['reason'], str)


class TestRowEnrichment:
    """Each ``_*_rows`` adapter must populate ``value`` and
    ``recommendation`` on every row so the UI never has to handle a
    payload missing those fields.
    """

    def test_archive_rows_include_value_and_recommendation(self,
                                                            geo_db, tmp_path,
                                                            monkeypatch):
        clip = tmp_path / 'SentryClips' / 'evt' / 'front.mp4'
        clip.parent.mkdir(parents=True)
        clip.write_bytes(b'x' * 10)
        _force_archive_dead_letter(geo_db, str(clip))

        # ``archive_queue.list_dead_letters`` resolves the DB by lazy-
        # importing ``config.MAPPING_DB_PATH``, so patching the config
        # module is what redirects the read to our fixture DB.
        import config as config_module
        from blueprints import jobs as jobs_module
        monkeypatch.setattr(config_module, 'MAPPING_DB_PATH', geo_db,
                            raising=False)
        monkeypatch.setattr(jobs_module, 'MAPPING_DB_PATH', geo_db,
                            raising=False)

        from blueprints.jobs import _archive_rows
        rows = _archive_rows(limit=10)
        assert rows, 'archive adapter returned no rows'
        for r in rows:
            assert 'value' in r
            assert 'recommendation' in r
            assert r['value']['tier'] in (
                'event', 'recent', 'archived', 'index', 'cloud',
                'unknown',
            )
            assert r['recommendation']['action'] in (
                'retry', 'delete', 'either',
            )

    def test_classifier_used_for_indexer_with_event_path(self):
        # Pure unit test that does not need a DB — confirms the
        # subsystem-name + path combination flows through the
        # classifier correctly when the indexer holds a SentryClips
        # path (a common case for failed event-clip indexing).
        from blueprints.jobs import (
            _classify_clip_value, _classify_recommendation,
        )
        v = _classify_clip_value(
            'indexer',
            '/home/pi/ArchivedClips/SentryClips/2025/front.mp4',
        )
        assert v['tier'] == 'event'  # path beats subsystem fallback

        # Indexer parse errors recommend delete (the file is corrupt;
        # retrying won't help).
        r = _classify_recommendation('indexer', 'moov atom not found')
        assert r['action'] == 'delete'


class TestFanOutAcrossAllSubsystems:
    """Issue #180 — ``subsystem='all'`` on retry/delete must invoke
    every per-subsystem adapter once and return a per-subsystem
    breakdown alongside the total.
    """

    def test_retry_all_fans_out(self, client, monkeypatch):
        calls = {}

        def make(name, n):
            def fake(row_id):
                calls[name] = row_id
                return n
            return fake

        _patch_retrier(monkeypatch, 'archive',         make('archive', 2))
        _patch_retrier(monkeypatch, 'indexer',         make('indexer', 3))
        _patch_retrier(monkeypatch, 'cloud_sync',      make('cloud_sync', 1))

        rv = client.post('/api/jobs/retry',
                         data=json.dumps({'subsystem': 'all'}),
                         content_type='application/json')
        assert rv.status_code == 200
        body = rv.get_json()
        assert body['subsystem'] == 'all'
        assert body['rows_reset'] == 2 + 3 + 1
        assert body['per_subsystem'] == {
            'archive': 2, 'indexer': 3, 'cloud_sync': 1,
        }
        # Every adapter was invoked with id=None (retry-all).
        assert set(calls.keys()) == {
            'archive', 'indexer', 'cloud_sync',
        }
        for name, row_id in calls.items():
            assert row_id is None, f"{name} was invoked with id={row_id!r}"

    def test_retry_all_ignores_id_in_payload(self, client, monkeypatch):
        # Even if the client sends id=42, fan-out always uses id=None.
        captured = {}

        def fake(row_id):
            captured.setdefault('args', []).append(row_id)
            return 1

        for name in ('archive', 'indexer', 'cloud_sync'):
            _patch_retrier(monkeypatch, name, fake)

        rv = client.post('/api/jobs/retry',
                         data=json.dumps({'subsystem': 'all', 'id': 42}),
                         content_type='application/json')
        assert rv.status_code == 200
        # All three adapter invocations got None, not 42.
        assert captured['args'] == [None, None, None]

    def test_retry_all_one_subsystem_crashes(self, client, monkeypatch):
        """A crashing adapter must not block fan-out for the others."""
        import blueprints.jobs as jobs_module

        def boom(row_id):
            raise RuntimeError('boom')

        monkeypatch.setitem(jobs_module._RETRIERS, 'archive', boom)
        _patch_retrier(monkeypatch, 'indexer',         lambda _r: 5)
        _patch_retrier(monkeypatch, 'cloud_sync',      lambda _r: 0)

        rv = client.post('/api/jobs/retry',
                         data=json.dumps({'subsystem': 'all'}),
                         content_type='application/json')
        assert rv.status_code == 200
        body = rv.get_json()
        # Crashed adapter contributes 0 (swallowed by _safe);
        # other adapters still produce their counts.
        assert body['per_subsystem']['archive'] == 0
        assert body['per_subsystem']['indexer'] == 5
        assert body['rows_reset'] == 5 + 0

    def test_delete_all_fans_out(self, client, monkeypatch):
        calls = {}

        def make(name, n):
            def fake(row_id):
                calls[name] = row_id
                return n
            return fake

        import blueprints.jobs as jobs_module
        for name, n in (('archive', 7), ('indexer', 1),
                        ('cloud_sync', 0)):
            monkeypatch.setitem(jobs_module._DELETERS, name, make(name, n))

        rv = client.post('/api/jobs/delete',
                         data=json.dumps({'subsystem': 'all'}),
                         content_type='application/json')
        assert rv.status_code == 200
        body = rv.get_json()
        assert body['subsystem'] == 'all'
        assert body['rows_deleted'] == 7 + 1 + 0
        assert body['per_subsystem'] == {
            'archive': 7, 'indexer': 1, 'cloud_sync': 0,
        }
        assert all(v is None for v in calls.values())

    def test_delete_all_one_subsystem_crashes(self, client, monkeypatch):
        import blueprints.jobs as jobs_module

        def boom(row_id):
            raise RuntimeError('boom')

        monkeypatch.setitem(jobs_module._DELETERS, 'cloud_sync', boom)
        monkeypatch.setitem(jobs_module._DELETERS, 'archive',
                            lambda _r: 4)
        monkeypatch.setitem(jobs_module._DELETERS, 'indexer',
                            lambda _r: 0)

        rv = client.post('/api/jobs/delete',
                         data=json.dumps({'subsystem': 'all'}),
                         content_type='application/json')
        assert rv.status_code == 200
        body = rv.get_json()
        assert body['per_subsystem']['cloud_sync'] == 0  # crash swallowed
        assert body['rows_deleted'] == 4 + 0 + 0

    def test_unknown_subsystem_lists_all_in_allowed(self, client):
        rv = client.post('/api/jobs/retry',
                         data=json.dumps({'subsystem': 'bogus', 'id': None}),
                         content_type='application/json')
        assert rv.status_code == 400
        body = rv.get_json()
        # The "allowed" array must include 'all' so the client knows
        # the bulk fan-out option exists.
        assert 'all' in body['allowed']


class TestFailedJobsPageContext:
    """Issue #180 — the /jobs HTML route must merge
    ``get_base_context()`` so the left sidebar / mobile bottom-tab
    nav renders with all top-level pages instead of collapsing to
    just Settings.
    """

    def test_render_template_receives_base_context(self, app, monkeypatch):
        # Capture the kwargs render_template is called with so we can
        # assert that get_base_context's flags flow into the template.
        # Patching get_base_context to a known dict is simpler than
        # patching render_template (the latter would require taking
        # over the template loading machinery).
        captured = {}

        import blueprints.jobs as jobs_module

        fake_ctx = {
            'mode_token': 'present',
            'mode_label': 'Present',
            'mode_class': 'ok',
            'share_paths': [],
            'hostname': 'cybertruckusb',
            'map_available': True,
            'analytics_available': True,
            'cloud_archive_available': True,
            'chimes_available': True,
            'shows_available': False,
            'wraps_available': False,
            'music_available': False,
            'boombox_available': False,
            'license_plates_available': False,
            'videos_available': True,
        }
        monkeypatch.setattr(jobs_module, 'get_base_context',
                            lambda: dict(fake_ctx))

        # Stub render_template so the test doesn't need the entire
        # base.html chain (with its url_for calls into every other
        # blueprint). We just verify the template name and the kwargs
        # it would have been called with.
        def fake_render(template, **kwargs):
            captured['template'] = template
            captured['kwargs'] = kwargs
            return 'ok'

        monkeypatch.setattr(jobs_module, 'render_template', fake_render)

        with app.test_client() as client:
            rv = client.get('/jobs')
            assert rv.status_code == 200

        assert captured['template'] == 'failed_jobs.html'
        # Every flag that was in the base context must reach the template.
        for key, value in fake_ctx.items():
            assert captured['kwargs'].get(key) == value, \
                f"base-context flag {key!r} missing from /jobs render"
        # And the page identifier must NOT collapse the nav by
        # falsely highlighting Settings (issue #180 root cause).
        assert captured['kwargs'].get('page') == 'jobs'
        # The subsystem list still flows through.
        assert 'subsystems' in captured['kwargs']
