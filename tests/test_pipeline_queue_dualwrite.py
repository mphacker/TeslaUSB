"""Tests for issue #184 Wave 4 — Phase I.1 (pipeline_queue dual-write).

Phase I.1 is the additive half of the queue unification:

  * The new ``pipeline_queue`` table exists in ``geodata.db`` (schema v16).
  * Every legacy enqueue (archive_queue, indexing_queue,
    live_event_queue, cloud_synced_files) ALSO writes a row to
    ``pipeline_queue`` tagged with ``legacy_table`` + ``legacy_id``.
  * Reads still come from the legacy tables — no behaviour change.
  * A one-time backfill helper picks up rows that existed BEFORE
    dual-write was wired in (the upgrade backlog).

These tests verify:

  * Schema migration v15 → v16 creates the ``pipeline_queue`` table
    and the two indices.
  * Each dual-write hook produces a row in ``pipeline_queue`` with
    the expected ``stage``, ``priority``, ``legacy_table``, and
    ``payload_json`` values.
  * Re-enqueuing the same source from the same legacy producer is
    idempotent (the unique constraint catches it).
  * Backfill correctly translates each legacy queue's status enum
    to the unified stage/status pair.
  * Errors in the dual-write path NEVER propagate to the legacy
    enqueue caller.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

# Allow importing the web modules without spinning up Flask.
SCRIPTS_WEB = Path(__file__).resolve().parent.parent / 'scripts' / 'web'
if str(SCRIPTS_WEB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_WEB))

from services import pipeline_queue_service as pqs  # noqa: E402
from services.mapping_migrations import _SCHEMA_VERSION, _init_db  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def geodata_db(tmp_path):
    """A fresh ``geodata.db`` initialised at the current schema version."""
    db_path = str(tmp_path / 'geodata.db')
    conn = _init_db(db_path)
    conn.close()
    return db_path


@pytest.fixture
def cloud_sync_db(tmp_path):
    """A fresh ``cloud_sync.db`` with the LES + cloud_synced_files schemas."""
    db_path = str(tmp_path / 'cloud_sync.db')
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE live_event_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_dir TEXT NOT NULL,
            event_json_path TEXT NOT NULL,
            event_timestamp TEXT,
            event_reason TEXT,
            upload_scope TEXT DEFAULT 'event_minute',
            status TEXT DEFAULT 'pending',
            enqueued_at TEXT NOT NULL,
            uploaded_at TEXT,
            next_retry_at REAL,
            attempts INTEGER DEFAULT 0,
            last_error TEXT,
            previous_last_error TEXT,
            bytes_uploaded INTEGER DEFAULT 0,
            UNIQUE(event_dir)
        );
        CREATE TABLE cloud_synced_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL UNIQUE,
            file_size INTEGER,
            file_mtime REAL,
            remote_path TEXT,
            status TEXT DEFAULT 'pending',
            synced_at TEXT,
            retry_count INTEGER DEFAULT 0,
            last_error TEXT,
            previous_last_error TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestSchema:
    def test_schema_version_is_v16_or_later(self):
        assert _SCHEMA_VERSION >= 16

    def test_pipeline_queue_table_exists(self, geodata_db):
        conn = sqlite3.connect(geodata_db)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='pipeline_queue'"
            ).fetchone()
            assert row is not None
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(pipeline_queue)"
            ).fetchall()}
            for required in (
                'id', 'source_path', 'dest_path', 'stage', 'status',
                'priority', 'attempts', 'last_error', 'next_retry_at',
                'enqueued_at', 'completed_at', 'payload_json',
                'legacy_id', 'legacy_table',
            ):
                assert required in cols, f"missing column {required}"
        finally:
            conn.close()

    def test_pipeline_queue_indices_exist(self, geodata_db):
        conn = sqlite3.connect(geodata_db)
        try:
            indices = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='pipeline_queue'"
            ).fetchall()}
            assert 'idx_pipeline_ready' in indices
            assert 'idx_pipeline_legacy' in indices
        finally:
            conn.close()

    def test_pipeline_queue_uniqueness(self, geodata_db):
        """``(source_path, stage, legacy_table)`` is unique."""
        ok1 = pqs.dual_write_enqueue(
            source_path='/foo/bar.mp4',
            stage=pqs.STAGE_ARCHIVE_PENDING,
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            db_path=geodata_db,
        )
        ok2 = pqs.dual_write_enqueue(
            source_path='/foo/bar.mp4',
            stage=pqs.STAGE_ARCHIVE_PENDING,
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            db_path=geodata_db,
        )
        assert ok1 is True
        assert ok2 is False  # idempotent
        conn = sqlite3.connect(geodata_db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM pipeline_queue").fetchone()[0]
            assert n == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Dual-write helpers (direct calls)
# ---------------------------------------------------------------------------

class TestDualWriteEnqueue:
    def test_writes_row_with_all_fields(self, geodata_db):
        ok = pqs.dual_write_enqueue(
            source_path='/x/SentryClips/2026-05-14_10-00-00/event.mp4',
            stage=pqs.STAGE_ARCHIVE_PENDING,
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            legacy_id=42,
            priority=pqs.PRIORITY_ARCHIVE_EVENT,
            payload={'expected_size': 1234, 'expected_mtime': 1.0},
            db_path=geodata_db,
        )
        assert ok is True
        conn = sqlite3.connect(geodata_db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM pipeline_queue").fetchone()
            assert row['source_path'].endswith('/event.mp4')
            assert row['stage'] == pqs.STAGE_ARCHIVE_PENDING
            assert row['status'] == 'pending'
            assert row['priority'] == pqs.PRIORITY_ARCHIVE_EVENT
            assert row['legacy_id'] == 42
            assert row['legacy_table'] == pqs.LEGACY_TABLE_ARCHIVE
            payload = json.loads(row['payload_json'])
            assert payload['expected_size'] == 1234
            assert payload['expected_mtime'] == 1.0
        finally:
            conn.close()

    def test_missing_required_returns_false(self, geodata_db):
        assert pqs.dual_write_enqueue(
            source_path='', stage=pqs.STAGE_ARCHIVE_PENDING,
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            db_path=geodata_db,
        ) is False
        assert pqs.dual_write_enqueue(
            source_path='/foo', stage='',
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            db_path=geodata_db,
        ) is False
        assert pqs.dual_write_enqueue(
            source_path='/foo', stage=pqs.STAGE_ARCHIVE_PENDING,
            legacy_table='',
            db_path=geodata_db,
        ) is False

    def test_no_db_path_returns_false(self):
        # Without a configured DB path AND no module-level config, it
        # must NOT raise — best-effort means swallow the failure.
        with mock.patch.object(pqs, '_resolve_pipeline_db', return_value=None):
            assert pqs.dual_write_enqueue(
                source_path='/foo',
                stage=pqs.STAGE_ARCHIVE_PENDING,
                legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            ) is False

    def test_swallows_sqlite_errors(self, tmp_path):
        bad_path = str(tmp_path / 'does-not-exist' / 'x.db')
        # Writing into a non-existent directory should fail at open
        # time; the helper must NOT raise.
        result = pqs.dual_write_enqueue(
            source_path='/foo',
            stage=pqs.STAGE_ARCHIVE_PENDING,
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            db_path=bad_path,
        )
        assert result is False


class TestDualWriteEnqueueMany:
    def test_batch_inserts_multiple_rows(self, geodata_db):
        rows = [
            {
                'source_path': f'/foo/{i}.mp4',
                'stage': pqs.STAGE_INDEX_PENDING,
                'legacy_table': pqs.LEGACY_TABLE_INDEXING,
                'priority': pqs.PRIORITY_INDEXING,
                'payload': {'canonical_key': f'key-{i}'},
            }
            for i in range(5)
        ]
        n = pqs.dual_write_enqueue_many(rows, db_path=geodata_db)
        # On success n should equal the number of inserted rows.
        assert n >= 1  # SQLite executemany rowcount semantics vary
        conn = sqlite3.connect(geodata_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM pipeline_queue"
            ).fetchone()[0]
            assert count == 5
        finally:
            conn.close()

    def test_empty_list_is_noop(self, geodata_db):
        assert pqs.dual_write_enqueue_many([], db_path=geodata_db) == 0

    def test_skips_invalid_rows(self, geodata_db):
        rows = [
            {'source_path': '', 'stage': 'x', 'legacy_table': 'y'},
            {'source_path': '/a', 'stage': '', 'legacy_table': 'y'},
            {'source_path': '/b', 'stage': pqs.STAGE_INDEX_PENDING,
             'legacy_table': pqs.LEGACY_TABLE_INDEXING},
        ]
        pqs.dual_write_enqueue_many(rows, db_path=geodata_db)
        conn = sqlite3.connect(geodata_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM pipeline_queue"
            ).fetchone()[0]
            # Only the third row was valid.
            assert count == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Backfill from legacy queues
# ---------------------------------------------------------------------------

class TestBackfill:
    def test_backfill_archive_queue(self, geodata_db):
        # archive_queue lives in the same DB as pipeline_queue.
        conn = sqlite3.connect(geodata_db)
        conn.executemany(
            "INSERT INTO archive_queue "
            "(source_path, priority, status, enqueued_at, "
            " expected_size, expected_mtime) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ('/x/RecentClips/foo.mp4', 2, 'pending',
                 '2026-05-14T10:00:00', 1024, 1.0),
                ('/x/SentryClips/2026-05-14_10-00-00/event.json', 1,
                 'copied', '2026-05-14T10:00:00', 256, 2.0),
            ],
        )
        conn.commit()
        conn.close()

        counts = pqs.backfill_legacy_queues(
            pipeline_db_path=geodata_db,
            cloud_db_path=None,
        )
        assert counts[pqs.LEGACY_TABLE_ARCHIVE] == 2

        conn = sqlite3.connect(geodata_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM pipeline_queue "
                "WHERE legacy_table = ? ORDER BY source_path",
                (pqs.LEGACY_TABLE_ARCHIVE,),
            ).fetchall()
            assert len(rows) == 2
            stages = {r['stage'] for r in rows}
            assert stages == {pqs.STAGE_ARCHIVE_PENDING,
                              pqs.STAGE_ARCHIVE_DONE}
        finally:
            conn.close()

    def test_backfill_indexing_queue(self, geodata_db):
        conn = sqlite3.connect(geodata_db)
        conn.executemany(
            "INSERT INTO indexing_queue "
            "(canonical_key, file_path, priority, enqueued_at, "
            " next_attempt_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ('k1', '/a.mp4', 50, 1.0, 0.0, 'manual'),
                ('k2', '/b.mp4', 25, 2.0, 0.0, 'catchup'),
            ],
        )
        conn.commit()
        conn.close()

        counts = pqs.backfill_legacy_queues(
            pipeline_db_path=geodata_db,
            cloud_db_path=None,
        )
        assert counts[pqs.LEGACY_TABLE_INDEXING] == 2

    def test_backfill_live_event_queue_cross_db(
        self, geodata_db, cloud_sync_db,
    ):
        conn = sqlite3.connect(cloud_sync_db)
        conn.executemany(
            "INSERT INTO live_event_queue "
            "(event_dir, event_json_path, event_timestamp, "
            " event_reason, upload_scope, status, enqueued_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ('/Sentry/2026-05-14_10-00-00',
                 '/Sentry/2026-05-14_10-00-00/event.json',
                 '2026-05-14T10:00:00', 'sentry_aware_object_detection',
                 'event_minute', 'pending',
                 '2026-05-14T10:00:00'),
                ('/Sentry/2026-05-14_11-00-00',
                 '/Sentry/2026-05-14_11-00-00/event.json',
                 '2026-05-14T11:00:00', 'user_interaction_horn',
                 'event_minute', 'uploaded',
                 '2026-05-14T11:00:00'),
            ],
        )
        conn.commit()
        conn.close()

        counts = pqs.backfill_legacy_queues(
            pipeline_db_path=geodata_db,
            cloud_db_path=cloud_sync_db,
        )
        assert counts[pqs.LEGACY_TABLE_LIVE_EVENT] == 2

        conn = sqlite3.connect(geodata_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM pipeline_queue "
                "WHERE legacy_table = ? ORDER BY source_path",
                (pqs.LEGACY_TABLE_LIVE_EVENT,),
            ).fetchall()
            assert len(rows) == 2
            stages = {r['stage'] for r in rows}
            assert stages == {pqs.STAGE_LIVE_EVENT_PENDING,
                              pqs.STAGE_LIVE_EVENT_DONE}
            # Pair assertion would have caught W1 in PR #190 review:
            # cross-DB backfill computed the translated status but
            # discarded it, so 'uploaded' rows landed as
            # ``stage='live_event_done'`` with ``status='pending'``.
            stage_status_pairs = {(r['stage'], r['status']) for r in rows}
            assert stage_status_pairs == {
                (pqs.STAGE_LIVE_EVENT_PENDING, 'pending'),
                (pqs.STAGE_LIVE_EVENT_DONE, 'done'),
            }
            for r in rows:
                payload = json.loads(r['payload_json'])
                assert 'event_dir' in payload
                assert 'event_reason' in payload
                assert payload['upload_scope'] == 'event_minute'
        finally:
            conn.close()

    def test_backfill_cloud_synced_files_cross_db(
        self, geodata_db, cloud_sync_db,
    ):
        conn = sqlite3.connect(cloud_sync_db)
        conn.executemany(
            "INSERT INTO cloud_synced_files "
            "(file_path, file_size, file_mtime, remote_path, status) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ('SentryClips/2026-05-14_10-00-00', 1024, 1.0,
                 'teslausb:bucket/SentryClips/2026-05-14_10-00-00',
                 'synced'),
                ('SentryClips/2026-05-14_11-00-00', 2048, 2.0, None,
                 'pending'),
            ],
        )
        conn.commit()
        conn.close()

        counts = pqs.backfill_legacy_queues(
            pipeline_db_path=geodata_db,
            cloud_db_path=cloud_sync_db,
        )
        assert counts[pqs.LEGACY_TABLE_CLOUD_SYNCED] == 2

        # Pair assertion — the W1 review finding noted that the
        # status translation map was computed but the value was
        # discarded by ``dual_write_enqueue`` (which hardcoded
        # ``status='pending'``). Now that ``dual_write_enqueue``
        # accepts a ``status`` parameter, the 'synced' row must
        # land as ``status='done'`` and the 'pending' row stays
        # ``status='pending'``.
        conn = sqlite3.connect(geodata_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM pipeline_queue "
                "WHERE legacy_table = ? ORDER BY source_path",
                (pqs.LEGACY_TABLE_CLOUD_SYNCED,),
            ).fetchall()
            assert len(rows) == 2
            stage_status_pairs = {(r['stage'], r['status']) for r in rows}
            assert stage_status_pairs == {
                (pqs.STAGE_CLOUD_DONE, 'done'),
                (pqs.STAGE_CLOUD_PENDING, 'pending'),
            }
        finally:
            conn.close()

    def test_backfill_is_idempotent(self, geodata_db):
        conn = sqlite3.connect(geodata_db)
        conn.execute(
            "INSERT INTO archive_queue "
            "(source_path, priority, status, enqueued_at) "
            "VALUES (?, ?, ?, ?)",
            ('/x/foo.mp4', 2, 'pending', '2026-05-14T10:00:00'),
        )
        conn.commit()
        conn.close()

        a = pqs.backfill_legacy_queues(pipeline_db_path=geodata_db,
                                       cloud_db_path=None)
        b = pqs.backfill_legacy_queues(pipeline_db_path=geodata_db,
                                       cloud_db_path=None)
        # First run inserts, second run is a no-op.
        assert a[pqs.LEGACY_TABLE_ARCHIVE] == 1
        assert b[pqs.LEGACY_TABLE_ARCHIVE] == 0

    def test_backfill_one_shot_flag_skips_subsequent_calls(self, geodata_db):
        """W4 fix: the one-shot ``kv_meta`` flag must short-circuit
        every backfill call after the first. Verifies that legacy rows
        added AFTER the first backfill are NOT picked up on the second
        call (because dual-write hooks now own the upgrade-to-current
        gap; the backfill is purely the one-time upgrade migration).
        """
        # First call: empty legacy queue → completes successfully and
        # writes the kv_meta flag.
        first = pqs.backfill_legacy_queues(pipeline_db_path=geodata_db,
                                           cloud_db_path=None)
        assert first[pqs.LEGACY_TABLE_ARCHIVE] == 0

        # Verify the flag was set.
        conn = sqlite3.connect(geodata_db)
        try:
            row = conn.execute(
                "SELECT value FROM kv_meta WHERE key = ?",
                ('pipeline_backfill_completed_at',),
            ).fetchone()
            assert row is not None
            assert row[0]  # non-empty timestamp string
        finally:
            conn.close()

        # Add a legacy row AFTER the flag was set.
        conn = sqlite3.connect(geodata_db)
        conn.execute(
            "INSERT INTO archive_queue "
            "(source_path, priority, status, enqueued_at) "
            "VALUES (?, ?, ?, ?)",
            ('/x/late.mp4', 2, 'pending', '2026-05-14T12:00:00'),
        )
        conn.commit()
        conn.close()

        # Second call must SKIP — the row is left for dual-write to
        # handle on its next enqueue (or for ``force=True`` recovery).
        second = pqs.backfill_legacy_queues(pipeline_db_path=geodata_db,
                                            cloud_db_path=None)
        assert second[pqs.LEGACY_TABLE_ARCHIVE] == 0

        conn = sqlite3.connect(geodata_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM pipeline_queue "
                "WHERE source_path = ?",
                ('/x/late.mp4',),
            ).fetchone()[0]
            assert count == 0
        finally:
            conn.close()

        # ``force=True`` bypasses the guard for recovery scenarios.
        forced = pqs.backfill_legacy_queues(pipeline_db_path=geodata_db,
                                            cloud_db_path=None,
                                            force=True)
        assert forced[pqs.LEGACY_TABLE_ARCHIVE] == 1

    def test_backfill_with_no_dbs(self, tmp_path):
        # Both DB paths missing — must return empty counts dict, not raise.
        counts = pqs.backfill_legacy_queues(
            pipeline_db_path=str(tmp_path / 'absent.db'),
            cloud_db_path=str(tmp_path / 'absent2.db'),
        )
        assert counts == {
            pqs.LEGACY_TABLE_ARCHIVE: 0,
            pqs.LEGACY_TABLE_INDEXING: 0,
            pqs.LEGACY_TABLE_LIVE_EVENT: 0,
            pqs.LEGACY_TABLE_CLOUD_SYNCED: 0,
        }


# ---------------------------------------------------------------------------
# Status / introspection
# ---------------------------------------------------------------------------

class TestPipelineStatus:
    def test_empty_db_returns_zero(self, geodata_db):
        s = pqs.pipeline_status(db_path=geodata_db)
        assert s.get('total', 0) == 0

    def test_counts_grouped(self, geodata_db):
        for i in range(3):
            pqs.dual_write_enqueue(
                source_path=f'/a/{i}.mp4',
                stage=pqs.STAGE_ARCHIVE_PENDING,
                legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
                db_path=geodata_db,
            )
        for i in range(2):
            pqs.dual_write_enqueue(
                source_path=f'/b/{i}.mp4',
                stage=pqs.STAGE_INDEX_PENDING,
                legacy_table=pqs.LEGACY_TABLE_INDEXING,
                db_path=geodata_db,
            )
        s = pqs.pipeline_status(db_path=geodata_db)
        assert s['total'] == 5
        groups = s['by_legacy_stage_status']
        assert any(g['legacy_table'] == pqs.LEGACY_TABLE_ARCHIVE
                   and g['count'] == 3 for g in groups)
        assert any(g['legacy_table'] == pqs.LEGACY_TABLE_INDEXING
                   and g['count'] == 2 for g in groups)


# ---------------------------------------------------------------------------
# Producer-side dual-write integration
# ---------------------------------------------------------------------------

class TestProducerHooks:
    """Verify each legacy producer triggers a pipeline_queue dual-write."""

    def test_archive_producer_dual_writes(self, geodata_db):
        # ``enqueue_for_archive`` looks up MAPPING_DB_PATH from config
        # when db_path is None. Pass it explicitly here.
        from services import archive_queue
        ok = archive_queue.enqueue_for_archive(
            '/tmp/foo.mp4',
            priority=2,
            db_path=geodata_db,
        )
        assert ok is True
        conn = sqlite3.connect(geodata_db)
        try:
            rows = conn.execute(
                "SELECT stage, legacy_table FROM pipeline_queue"
            ).fetchall()
            assert (pqs.STAGE_ARCHIVE_PENDING,
                    pqs.LEGACY_TABLE_ARCHIVE) in rows
        finally:
            conn.close()

    def test_archive_batch_producer_dual_writes(self, geodata_db):
        from services import archive_queue
        n = archive_queue.enqueue_many_for_archive(
            ['/tmp/a.mp4', '/tmp/b.mp4'],
            priority=3,
            db_path=geodata_db,
        )
        assert n == 2
        conn = sqlite3.connect(geodata_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM pipeline_queue "
                "WHERE legacy_table = ?",
                (pqs.LEGACY_TABLE_ARCHIVE,),
            ).fetchone()[0]
            assert count == 2
        finally:
            conn.close()

    def test_indexing_producer_dual_writes(self, geodata_db):
        from services import indexing_queue_service
        ok = indexing_queue_service.enqueue_for_indexing(
            geodata_db,
            '/tmp/clip-front.mp4',
            priority=10,
        )
        assert ok is True
        conn = sqlite3.connect(geodata_db)
        try:
            rows = conn.execute(
                "SELECT stage, legacy_table, priority FROM pipeline_queue"
            ).fetchall()
            assert (pqs.STAGE_INDEX_PENDING,
                    pqs.LEGACY_TABLE_INDEXING, 10) in rows
        finally:
            conn.close()

    def test_indexing_batch_producer_dual_writes(self, geodata_db):
        from services import indexing_queue_service
        n = indexing_queue_service.enqueue_many_for_indexing(
            geodata_db,
            [('/tmp/x.mp4', 50), ('/tmp/y.mp4', 25)],
            source='catchup',
        )
        assert n == 2
        conn = sqlite3.connect(geodata_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM pipeline_queue "
                "WHERE legacy_table = ?",
                (pqs.LEGACY_TABLE_INDEXING,),
            ).fetchone()[0]
            assert count == 2
        finally:
            conn.close()

    def test_dual_write_failure_does_not_break_legacy_archive(
        self, geodata_db, monkeypatch,
    ):
        """A simulated failure inside the pipeline_queue helper must
        NOT propagate back to ``enqueue_for_archive`` — the legacy
        write succeeded and the producer must report success."""
        from services import archive_queue

        def boom(**kwargs):
            raise RuntimeError("simulated pipeline_queue failure")

        monkeypatch.setattr(pqs, 'dual_write_enqueue', boom)
        ok = archive_queue.enqueue_for_archive(
            '/tmp/legacy-must-survive.mp4',
            priority=2,
            db_path=geodata_db,
        )
        assert ok is True
        conn = sqlite3.connect(geodata_db)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM archive_queue "
                "WHERE source_path = ?",
                ('/tmp/legacy-must-survive.mp4',),
            ).fetchone()[0]
            assert n == 1
        finally:
            conn.close()
