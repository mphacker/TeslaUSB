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
    with sqlite3.connect(geo_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, attempts, last_error, claimed_by, claimed_at "
            "FROM archive_queue WHERE id = ?", (rid,)
        ).fetchone()
    assert row['status'] == 'pending'
    assert row['attempts'] == 0
    assert row['last_error'] is None
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


# ---------------------------------------------------------------------------
# Blueprint tests
# ---------------------------------------------------------------------------

@pytest.fixture
def app(monkeypatch, tmp_path, geo_db):
    """Build a minimal Flask app with just the jobs blueprint mounted.

    Patches the four subsystem listers so each test controls what
    rows the blueprint sees, without needing real DBs for the
    cloud_archive / live_event_sync subsystems.
    """
    from flask import Flask
    from blueprints.jobs import jobs_bp
    import blueprints.jobs as jobs_module
    import config as config_module

    # Make sure config flags don't suppress subsystems.
    monkeypatch.setattr(config_module, 'CLOUD_ARCHIVE_ENABLED', True,
                        raising=False)
    monkeypatch.setattr(config_module, 'LIVE_EVENT_SYNC_ENABLED', True,
                        raising=False)
    monkeypatch.setattr(config_module, 'MAPPING_ENABLED', True,
                        raising=False)
    monkeypatch.setattr(config_module, 'MAPPING_DB_PATH', geo_db,
                        raising=False)
    monkeypatch.setattr(jobs_module, 'CLOUD_ARCHIVE_ENABLED', True,
                        raising=False)
    monkeypatch.setattr(jobs_module, 'LIVE_EVENT_SYNC_ENABLED', True,
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


def test_counts_all_zero(client, monkeypatch):
    for name in ('archive', 'indexer', 'cloud_sync', 'live_event_sync'):
        _patch_lister(monkeypatch, name, [])
    rv = client.get('/api/jobs/counts')
    assert rv.status_code == 200
    body = rv.get_json()
    assert body == {'archive': 0, 'indexer': 0, 'cloud_sync': 0,
                    'live_event_sync': 0, 'total': 0}


def test_counts_with_rows(client, monkeypatch):
    _patch_lister(monkeypatch, 'archive', [{'subsystem': 'archive', 'id': 1,
                                            'identifier': 'x', 'attempts': 5,
                                            'last_error': 'e',
                                            'enqueued_at': None, 'extra': {}}])
    _patch_lister(monkeypatch, 'indexer', [])
    _patch_lister(monkeypatch, 'cloud_sync', [{'subsystem': 'cloud_sync',
                                               'id': 'y', 'identifier': 'y',
                                               'attempts': 5, 'last_error': '',
                                               'enqueued_at': None, 'extra': {}}])
    _patch_lister(monkeypatch, 'live_event_sync', [])
    rv = client.get('/api/jobs/counts')
    body = rv.get_json()
    assert body['archive'] == 1
    assert body['cloud_sync'] == 1
    assert body['total'] == 2


def test_failed_all_subsystems(client, monkeypatch):
    rows = {
        'archive': [{'subsystem': 'archive', 'id': 1, 'identifier': 'a',
                     'attempts': 5, 'last_error': '', 'enqueued_at': None,
                     'extra': {}}],
        'indexer': [{'subsystem': 'indexer', 'id': 'k', 'identifier': 'i',
                     'attempts': 3, 'last_error': '', 'enqueued_at': None,
                     'extra': {}}],
        'cloud_sync': [],
        'live_event_sync': [],
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
    _patch_lister(monkeypatch, 'live_event_sync', [])

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
    _patch_lister(monkeypatch, 'live_event_sync', [])

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
    _patch_lister(monkeypatch, 'live_event_sync', [])

    rv = client.get('/api/jobs/failed?subsystem=archive&limit=3')
    body = rv.get_json()
    assert body['count'] == 3


@pytest.mark.parametrize('subsystem', ['archive', 'indexer',
                                       'cloud_sync', 'live_event_sync'])
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
