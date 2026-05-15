"""Issue #161 — delete failed/dead-letter rows from the Failed Jobs page.

Each subsystem (archive, indexer, cloud_sync, LES) gains a
``delete_dead_letter`` / ``delete_failed`` service-layer function and a
matching adapter under ``blueprints/jobs.py:_DELETERS``. The unified
``POST /api/jobs/delete`` endpoint dispatches by ``subsystem`` and
removes either one row (when ``id`` is provided) or every dead-letter
row in the subsystem (when ``id`` is null/absent).

These tests pin the contract:

1. Per-subsystem ``delete_dead_letter`` removes only dead-letter rows;
   pending / claimed / synced rows are preserved.
2. ``id=None`` deletes every dead-letter row in the table.
3. ``id=<value>`` deletes only the matching row.
4. Returns ``rowcount`` (``0`` if nothing matched, ``>0`` otherwise).
5. ``POST /api/jobs/delete`` route validates subsystem, returns
   ``rows_deleted`` on success, ``400`` on bad input.
6. Indexer delete preserves ``indexed_files`` / trips / waypoints
   (the queue-table delete must not cascade into the GPS history).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Shared fixtures (mirror tests/test_failed_jobs_history.py for consistency)
# ---------------------------------------------------------------------------


@pytest.fixture
def archive_db(tmp_path, monkeypatch):
    from services import archive_queue, mapping_migrations
    db = str(tmp_path / "geodata.db")
    conn = mapping_migrations._init_db(db)
    conn.close()
    monkeypatch.setattr(
        archive_queue, "_resolve_db_path", lambda p=None: db, raising=False,
    )
    return db


@pytest.fixture
def indexing_db(tmp_path):
    from services import mapping_migrations
    db = str(tmp_path / "geodata.db")
    conn = mapping_migrations._init_db(db)
    conn.close()
    return db


def _seed_archive_pending(db_path, source_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO archive_queue (source_path, status, enqueued_at)
           VALUES (?, 'pending', '2026-01-01T00:00:00Z')""",
        (source_path,),
    )
    rowid = conn.execute(
        "SELECT id FROM archive_queue WHERE source_path = ?",
        (source_path,),
    ).fetchone()[0]
    conn.commit()
    conn.close()
    return rowid


def _force_archive_dead_letter(db_path, row_id):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE archive_queue SET status='dead_letter', attempts=99 "
        "WHERE id = ?", (row_id,),
    )
    conn.commit()
    conn.close()


def _archive_count(db_path, status=None):
    conn = sqlite3.connect(db_path)
    if status is None:
        n = conn.execute("SELECT COUNT(*) FROM archive_queue").fetchone()[0]
    else:
        n = conn.execute(
            "SELECT COUNT(*) FROM archive_queue WHERE status = ?",
            (status,),
        ).fetchone()[0]
    conn.close()
    return n


# ---------------------------------------------------------------------------
# archive_queue.delete_dead_letter
# ---------------------------------------------------------------------------


class TestArchiveDelete:

    def test_delete_single_dead_letter_row(self, archive_db):
        from services import archive_queue
        rid = _seed_archive_pending(archive_db, "/src/dl1.mp4")
        _force_archive_dead_letter(archive_db, rid)
        assert _archive_count(archive_db, 'dead_letter') == 1
        n = archive_queue.delete_dead_letter(row_id=rid)
        assert n == 1
        assert _archive_count(archive_db, 'dead_letter') == 0
        assert _archive_count(archive_db) == 0

    def test_delete_all_dead_letter_rows(self, archive_db):
        from services import archive_queue
        for i in range(3):
            rid = _seed_archive_pending(archive_db, f"/src/dl_{i}.mp4")
            _force_archive_dead_letter(archive_db, rid)
        # Add a pending row that MUST survive.
        _seed_archive_pending(archive_db, "/src/keep.mp4")
        assert _archive_count(archive_db, 'dead_letter') == 3
        assert _archive_count(archive_db, 'pending') == 1
        n = archive_queue.delete_dead_letter(row_id=None)
        assert n == 3
        assert _archive_count(archive_db, 'dead_letter') == 0
        assert _archive_count(archive_db, 'pending') == 1

    def test_delete_does_not_touch_pending_or_claimed(self, archive_db):
        from services import archive_queue
        pending = _seed_archive_pending(archive_db, "/src/pending.mp4")
        claimed = _seed_archive_pending(archive_db, "/src/claimed.mp4")
        dl = _seed_archive_pending(archive_db, "/src/dl.mp4")
        conn = sqlite3.connect(archive_db)
        conn.execute(
            "UPDATE archive_queue SET status='claimed', "
            "claimed_by='w1', claimed_at=1000 WHERE id = ?", (claimed,),
        )
        conn.commit()
        conn.close()
        _force_archive_dead_letter(archive_db, dl)
        # Even with id=<dead_letter id> the WHERE filter requires
        # status='dead_letter', so passing the pending id must be a no-op.
        assert archive_queue.delete_dead_letter(row_id=pending) == 0
        assert archive_queue.delete_dead_letter(row_id=claimed) == 0
        assert _archive_count(archive_db) == 3
        # Now delete the actual dead_letter row.
        assert archive_queue.delete_dead_letter(row_id=dl) == 1
        assert _archive_count(archive_db) == 2
        assert _archive_count(archive_db, 'pending') == 1
        assert _archive_count(archive_db, 'claimed') == 1

    def test_delete_returns_zero_when_no_dead_letters(self, archive_db):
        from services import archive_queue
        _seed_archive_pending(archive_db, "/src/only_pending.mp4")
        assert archive_queue.delete_dead_letter(row_id=None) == 0
        assert _archive_count(archive_db) == 1


# ---------------------------------------------------------------------------
# indexing_queue_service.delete_dead_letter
# ---------------------------------------------------------------------------


def _seed_indexing_row(db_path, key, file_path, attempts=0):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO indexing_queue
               (canonical_key, file_path, priority, enqueued_at, source,
                attempts)
           VALUES (?, ?, 50, 0, 'test', ?)""",
        (key, file_path, attempts),
    )
    conn.commit()
    conn.close()


def _indexing_count(db_path, dead_letter_only=False):
    from services import indexing_queue_service as iqs
    conn = sqlite3.connect(db_path)
    if dead_letter_only:
        n = conn.execute(
            "SELECT COUNT(*) FROM indexing_queue WHERE attempts >= ?",
            (iqs._PARSE_ERROR_MAX_ATTEMPTS,),
        ).fetchone()[0]
    else:
        n = conn.execute("SELECT COUNT(*) FROM indexing_queue").fetchone()[0]
    conn.close()
    return n


class TestIndexerDelete:

    def test_delete_single_dead_letter_row(self, indexing_db):
        from services import indexing_queue_service as iqs
        _seed_indexing_row(indexing_db, "k_dl", "/p/dl.mp4",
                           attempts=iqs._PARSE_ERROR_MAX_ATTEMPTS)
        assert _indexing_count(indexing_db, dead_letter_only=True) == 1
        n = iqs.delete_dead_letter(indexing_db, canonical_key_value="k_dl")
        assert n == 1
        assert _indexing_count(indexing_db) == 0

    def test_delete_all_dead_letter_rows(self, indexing_db):
        from services import indexing_queue_service as iqs
        # 3 dead-letter rows, 1 fresh row.
        for i in range(3):
            _seed_indexing_row(indexing_db, f"k_dl_{i}", f"/p/dl_{i}.mp4",
                               attempts=iqs._PARSE_ERROR_MAX_ATTEMPTS + i)
        _seed_indexing_row(indexing_db, "k_fresh", "/p/fresh.mp4", attempts=0)
        assert _indexing_count(indexing_db, dead_letter_only=True) == 3
        n = iqs.delete_dead_letter(indexing_db, canonical_key_value=None)
        assert n == 3
        assert _indexing_count(indexing_db) == 1  # fresh row survives

    def test_delete_does_not_touch_fresh_rows(self, indexing_db):
        from services import indexing_queue_service as iqs
        _seed_indexing_row(indexing_db, "k_fresh", "/p/fresh.mp4", attempts=0)
        # Targeted delete by key matches only dead-letter rows.
        assert iqs.delete_dead_letter(
            indexing_db, canonical_key_value="k_fresh") == 0
        assert _indexing_count(indexing_db) == 1

    def test_delete_preserves_indexed_files_and_trips(
            self, indexing_db, monkeypatch):
        """Dead-letter delete must NOT cascade into ``indexed_files``,
        ``trips``, ``waypoints``, or ``detected_events``. The user's
        GPS history is sacred — the May-7 McDonalds-trip regression
        was caused by exactly this kind of accidental cascade."""
        from services import indexing_queue_service as iqs
        # Seed a dead-letter queue row.
        _seed_indexing_row(indexing_db, "/p/dl.mp4", "/p/dl.mp4",
                           attempts=iqs._PARSE_ERROR_MAX_ATTEMPTS)
        # Seed a row in indexed_files for the same file.
        conn = sqlite3.connect(indexing_db)
        conn.execute(
            "INSERT INTO indexed_files (file_path, file_size, file_mtime, "
            "indexed_at, waypoint_count, event_count) "
            "VALUES (?, 0, 0, '2026-01-01', 1, 0)",
            ("/p/dl.mp4",),
        )
        conn.execute(
            "INSERT INTO trips (start_time, end_time) "
            "VALUES ('2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z')",
        )
        trip_id = conn.execute("SELECT id FROM trips").fetchone()[0]
        conn.execute(
            "INSERT INTO waypoints (trip_id, timestamp, lat, lon, video_path) "
            "VALUES (?, '2026-01-01T00:30:00Z', 0.0, 0.0, ?)",
            (trip_id, "/p/dl.mp4"),
        )
        conn.commit()
        conn.close()

        n = iqs.delete_dead_letter(indexing_db,
                                   canonical_key_value="/p/dl.mp4")
        assert n == 1

        conn = sqlite3.connect(indexing_db)
        try:
            indexed_count = conn.execute(
                "SELECT COUNT(*) FROM indexed_files "
                "WHERE file_path = '/p/dl.mp4'"
            ).fetchone()[0]
            trip_count = conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0]
            waypoint_count = conn.execute(
                "SELECT COUNT(*) FROM waypoints WHERE trip_id = ?",
                (trip_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        # Queue row is gone but the GPS history survives untouched.
        assert indexed_count == 1
        assert trip_count == 1
        assert waypoint_count == 1


# ---------------------------------------------------------------------------
# cloud_archive_service.delete_dead_letter
# ---------------------------------------------------------------------------


class TestCloudDelete:

    def _setup_cloud_db(self, tmp_path, monkeypatch):
        from services import cloud_archive_service as svc
        db = str(tmp_path / "cloud.db")
        monkeypatch.setattr(svc, "_startup_recovery_done", False)
        monkeypatch.setattr(svc, "CLOUD_ARCHIVE_DB_PATH", db)
        # Force canonical_cloud_path to a deterministic transform so
        # tests aren't sensitive to the configured ARCHIVE_BASE.
        monkeypatch.setattr(svc, "canonical_cloud_path", lambda p: p)
        conn = svc._init_cloud_tables(db)
        conn.close()
        return svc, db

    def _seed(self, db, file_path, status):
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO cloud_synced_files (file_path, status, retry_count) "
            "VALUES (?, ?, 0)",
            (file_path, status),
        )
        conn.commit()
        conn.close()

    def _count(self, db, status=None):
        conn = sqlite3.connect(db)
        if status is None:
            n = conn.execute("SELECT COUNT(*) FROM cloud_synced_files").fetchone()[0]
        else:
            n = conn.execute(
                "SELECT COUNT(*) FROM cloud_synced_files WHERE status = ?",
                (status,),
            ).fetchone()[0]
        conn.close()
        return n

    def test_delete_single_dead_letter_row(self, tmp_path, monkeypatch):
        svc, db = self._setup_cloud_db(tmp_path, monkeypatch)
        self._seed(db, "ArchivedClips/dl.mp4", "dead_letter")
        n = svc.delete_dead_letter(file_path="ArchivedClips/dl.mp4")
        assert n == 1
        assert self._count(db) == 0

    def test_delete_all_dead_letter_rows(self, tmp_path, monkeypatch):
        svc, db = self._setup_cloud_db(tmp_path, monkeypatch)
        for i in range(3):
            self._seed(db, f"ArchivedClips/dl_{i}.mp4", "dead_letter")
        self._seed(db, "ArchivedClips/synced.mp4", "synced")
        self._seed(db, "ArchivedClips/pending.mp4", "pending")
        n = svc.delete_dead_letter(file_path=None)
        assert n == 3
        assert self._count(db, 'dead_letter') == 0
        assert self._count(db, 'synced') == 1
        assert self._count(db, 'pending') == 1

    def test_delete_does_not_touch_synced_or_pending(self, tmp_path, monkeypatch):
        svc, db = self._setup_cloud_db(tmp_path, monkeypatch)
        self._seed(db, "ArchivedClips/synced.mp4", "synced")
        # Targeted delete by file_path matches only dead-letter rows.
        assert svc.delete_dead_letter(file_path="ArchivedClips/synced.mp4") == 0
        assert self._count(db) == 1



# ---------------------------------------------------------------------------
# Blueprint routes — POST /api/jobs/delete
# ---------------------------------------------------------------------------


class TestDeleteRoute:

    def _make_app(self, monkeypatch):
        from flask import Flask
        from blueprints import jobs as jobs_bp
        # Capture every call to the deleter adapters.
        calls = []

        def fake_delete(name):
            def _impl(row_id):
                calls.append((name, row_id))
                return 7  # arbitrary non-zero
            return _impl

        monkeypatch.setattr(jobs_bp, "_DELETERS", {
            'archive': fake_delete('archive'),
            'indexer': fake_delete('indexer'),
            'cloud_sync': fake_delete('cloud_sync'),
        })

        app = Flask(__name__)
        app.register_blueprint(jobs_bp.jobs_bp)
        return app, calls

    def test_delete_route_dispatches_to_correct_subsystem(self, monkeypatch):
        app, calls = self._make_app(monkeypatch)
        client = app.test_client()
        resp = client.post(
            '/api/jobs/delete',
            data=json.dumps({'subsystem': 'archive', 'id': 42}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body == {'subsystem': 'archive', 'rows_deleted': 7}
        assert calls == [('archive', 42)]

    def test_delete_route_passes_null_id_for_clear_all(self, monkeypatch):
        app, calls = self._make_app(monkeypatch)
        client = app.test_client()
        resp = client.post(
            '/api/jobs/delete',
            data=json.dumps({'subsystem': 'cloud_sync', 'id': None}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        assert resp.get_json()['rows_deleted'] == 7
        assert calls == [('cloud_sync', None)]

    def test_delete_route_omitted_id_means_clear_all(self, monkeypatch):
        app, calls = self._make_app(monkeypatch)
        client = app.test_client()
        resp = client.post(
            '/api/jobs/delete',
            data=json.dumps({'subsystem': 'indexer'}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        assert calls == [('indexer', None)]

    def test_delete_route_rejects_unknown_subsystem(self, monkeypatch):
        app, _ = self._make_app(monkeypatch)
        client = app.test_client()
        resp = client.post(
            '/api/jobs/delete',
            data=json.dumps({'subsystem': 'bogus', 'id': 1}),
            content_type='application/json',
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body['error']
        assert 'archive' in body['allowed']

    def test_delete_route_rejects_missing_subsystem(self, monkeypatch):
        app, _ = self._make_app(monkeypatch)
        client = app.test_client()
        resp = client.post(
            '/api/jobs/delete',
            data=json.dumps({'id': 1}),
            content_type='application/json',
        )
        assert resp.status_code == 400

    def test_delete_route_returns_500_on_adapter_crash(self, monkeypatch):
        from flask import Flask
        from blueprints import jobs as jobs_bp

        def boom(_row_id):
            raise RuntimeError("intentional test crash")

        monkeypatch.setattr(jobs_bp, "_DELETERS", {
            'archive': boom, 'indexer': boom,
            'cloud_sync': boom,
        })
        app = Flask(__name__)
        app.register_blueprint(jobs_bp.jobs_bp)
        client = app.test_client()
        resp = client.post(
            '/api/jobs/delete',
            data=json.dumps({'subsystem': 'archive', 'id': 1}),
            content_type='application/json',
        )
        assert resp.status_code == 500
        body = resp.get_json()
        assert body['subsystem'] == 'archive'

    def test_archive_adapter_coerces_id_to_int(self, monkeypatch):
        from blueprints import jobs as jobs_bp
        captured = {}

        def fake_aq_delete(row_id=None, db_path=None):
            captured['row_id'] = row_id
            return 1

        from services import archive_queue
        monkeypatch.setattr(archive_queue, "delete_dead_letter", fake_aq_delete)
        # String id from JSON gets coerced to int.
        assert jobs_bp._delete_archive("123") == 1
        assert captured['row_id'] == 123
        assert isinstance(captured['row_id'], int)

    def test_archive_adapter_returns_zero_on_unparseable_id(self, monkeypatch):
        from blueprints import jobs as jobs_bp
        called = {'n': 0}

        def fake_aq_delete(row_id=None, db_path=None):
            called['n'] += 1
            return 99

        from services import archive_queue
        monkeypatch.setattr(archive_queue, "delete_dead_letter", fake_aq_delete)
        # "not-an-int" can't be coerced — adapter returns 0 without calling.
        assert jobs_bp._delete_archive("not-an-int") == 0
        assert called['n'] == 0

    def test_indexer_adapter_passes_canonical_key_string(self, monkeypatch):
        from blueprints import jobs as jobs_bp
        captured = {}

        def fake_iqs_delete(db_path, canonical_key_value=None):
            captured['db_path'] = db_path
            captured['key'] = canonical_key_value
            return 1

        from services import indexing_queue_service
        monkeypatch.setattr(indexing_queue_service, "delete_dead_letter",
                            fake_iqs_delete)
        monkeypatch.setattr(jobs_bp, "MAPPING_ENABLED", True)
        monkeypatch.setattr(jobs_bp, "MAPPING_DB_PATH", "/tmp/geodata.db")
        assert jobs_bp._delete_indexer("/some/canonical/key") == 1
        assert captured['db_path'] == "/tmp/geodata.db"
        assert captured['key'] == "/some/canonical/key"

    def test_disabled_subsystem_returns_zero(self, monkeypatch):
        from blueprints import jobs as jobs_bp
        monkeypatch.setattr(jobs_bp, "MAPPING_ENABLED", False)
        monkeypatch.setattr(jobs_bp, "CLOUD_ARCHIVE_ENABLED", False)
        # Both adapters guard at the top and return 0 without
        # importing or calling the service module.
        assert jobs_bp._delete_indexer("any") == 0
        assert jobs_bp._delete_cloud_sync("any") == 0
