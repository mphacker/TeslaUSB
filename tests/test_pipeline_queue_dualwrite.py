"""Tests for issue #184 Wave 4 — Phase I.1 (pipeline_queue dual-write).

Phase I.1 is the additive half of the queue unification:

  * The new ``pipeline_queue`` table exists in ``geodata.db`` (schema v16).
  * Every legacy enqueue (archive_queue, indexing_queue,
    cloud_synced_files) ALSO writes a row to ``pipeline_queue``
    tagged with ``legacy_table`` + ``legacy_id``.
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
    """A fresh ``cloud_sync.db`` with the cloud_synced_files schema.

    Note: the ``live_event_queue`` table was dropped in cloud_sync.db
    v4 (issue #202) after Wave 4 PR-F4 deleted the LES subsystem, so
    this fixture no longer creates it.
    """
    db_path = str(tmp_path / 'cloud_sync.db')
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
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

    def test_priority_live_event_constant_kept(self):
        """Issue #202: ``PRIORITY_LIVE_EVENT`` MUST remain a public
        constant. The standalone Live Event Sync subsystem was deleted
        in Wave 4 PR-F4 (#184) but the cloud worker still uses
        ``PRIORITY_LIVE_EVENT = 0`` as the priority of live-event
        rows in the unified ``pipeline_queue``. Without this constant
        live events would lose their fast-path over bulk catch-up
        rows. Moved here from ``test_cloud_archive_v4_migration.py``
        per review-pr finding #10 (the constant is owned by this
        module, so its assertion belongs here)."""
        assert pqs.PRIORITY_LIVE_EVENT == 0

    def test_resolve_pipeline_db_is_public(self):
        """Issue #202 / review-pr finding #7: ``resolve_pipeline_db``
        was promoted from a private helper to a public function so
        sibling service modules (cloud_archive_service v4 migration)
        can call it without ``# noqa: SLF001``. The legacy private
        alias ``_resolve_pipeline_db`` is preserved for backward
        compatibility."""
        assert hasattr(pqs, 'resolve_pipeline_db')
        assert callable(pqs.resolve_pipeline_db)
        # Backward-compat alias still works.
        assert pqs._resolve_pipeline_db is pqs.resolve_pipeline_db

    def test_table_exists_is_public(self, geodata_db):
        """Issue #202 / re-review N1: ``table_exists`` was promoted
        from a private helper (``_table_exists``) to a public
        function so sibling service modules (cloud_archive_service
        v4 migration) can call it without ``# noqa: SLF001``,
        mirroring the ``resolve_pipeline_db`` promotion in
        finding #7. The legacy private alias is preserved for
        backward compatibility."""
        assert hasattr(pqs, 'table_exists')
        assert callable(pqs.table_exists)
        # Backward-compat alias still works.
        assert pqs._table_exists is pqs.table_exists
        # Functional check.
        conn = sqlite3.connect(geodata_db)
        try:
            assert pqs.table_exists(conn, 'pipeline_queue') is True
            assert pqs.table_exists(conn, 'no_such_table') is False
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


# ---------------------------------------------------------------------------
# Wave 4 PR-B — state-transition dual-write helpers
# ---------------------------------------------------------------------------

class TestUpdatePipelineRow:
    """Unit tests for ``update_pipeline_row`` (source_path lookup)."""

    def test_happy_path_updates_status(self, geodata_db):
        # Enqueue first so a row exists
        pqs.dual_write_enqueue(
            source_path='/tmp/x.mp4',
            stage=pqs.STAGE_ARCHIVE_PENDING,
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            db_path=geodata_db,
        )
        ok = pqs.update_pipeline_row(
            stage=pqs.STAGE_ARCHIVE_PENDING,
            source_path='/tmp/x.mp4',
            status='in_progress',
            db_path=geodata_db,
        )
        assert ok is True
        conn = sqlite3.connect(geodata_db)
        try:
            r = conn.execute(
                "SELECT status FROM pipeline_queue WHERE source_path=?",
                ('/tmp/x.mp4',),
            ).fetchone()
            assert r[0] == 'in_progress'
        finally:
            conn.close()

    def test_missing_row_returns_false_and_no_op(self, geodata_db):
        ok = pqs.update_pipeline_row(
            stage=pqs.STAGE_ARCHIVE_PENDING,
            source_path='/never/seen.mp4',
            status='done',
            db_path=geodata_db,
        )
        assert ok is False

    def test_no_kwargs_is_silent_noop(self, geodata_db):
        pqs.dual_write_enqueue(
            source_path='/tmp/y.mp4',
            stage=pqs.STAGE_ARCHIVE_PENDING,
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            db_path=geodata_db,
        )
        ok = pqs.update_pipeline_row(
            stage=pqs.STAGE_ARCHIVE_PENDING,
            source_path='/tmp/y.mp4',
            db_path=geodata_db,
        )
        assert ok is False
        # Row is unchanged (still 'pending')
        conn = sqlite3.connect(geodata_db)
        try:
            r = conn.execute(
                "SELECT status FROM pipeline_queue WHERE source_path=?",
                ('/tmp/y.mp4',),
            ).fetchone()
            assert r[0] == 'pending'
        finally:
            conn.close()

    def test_promotes_stage_and_completed_at(self, geodata_db):
        pqs.dual_write_enqueue(
            source_path='/tmp/z.mp4',
            stage=pqs.STAGE_ARCHIVE_PENDING,
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            db_path=geodata_db,
        )
        ok = pqs.update_pipeline_row(
            stage=pqs.STAGE_ARCHIVE_PENDING,
            source_path='/tmp/z.mp4',
            new_stage='archive_done',
            status='done',
            completed_at=12345.0,
            db_path=geodata_db,
        )
        assert ok is True
        conn = sqlite3.connect(geodata_db)
        try:
            r = conn.execute(
                "SELECT stage, status, completed_at FROM pipeline_queue "
                "WHERE source_path=?",
                ('/tmp/z.mp4',),
            ).fetchone()
            assert r[0] == 'archive_done'
            assert r[1] == 'done'
            assert r[2] == 12345.0
        finally:
            conn.close()

    def test_none_columns_preserved(self, geodata_db):
        """Passing None for a kwarg leaves the column unchanged."""
        pqs.dual_write_enqueue(
            source_path='/tmp/p.mp4',
            stage=pqs.STAGE_ARCHIVE_PENDING,
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            db_path=geodata_db,
        )
        # Set initial state
        pqs.update_pipeline_row(
            stage=pqs.STAGE_ARCHIVE_PENDING,
            source_path='/tmp/p.mp4',
            status='in_progress',
            attempts=2,
            last_error='boom',
            db_path=geodata_db,
        )
        # Update only status; attempts + last_error should stick
        pqs.update_pipeline_row(
            stage=pqs.STAGE_ARCHIVE_PENDING,
            source_path='/tmp/p.mp4',
            status='pending',
            db_path=geodata_db,
        )
        conn = sqlite3.connect(geodata_db)
        try:
            r = conn.execute(
                "SELECT status, attempts, last_error "
                "FROM pipeline_queue WHERE source_path=?",
                ('/tmp/p.mp4',),
            ).fetchone()
            assert r[0] == 'pending'
            assert r[1] == 2
            assert r[2] == 'boom'
        finally:
            conn.close()

    def test_swallows_missing_db(self, tmp_path):
        # No DB at this path
        ok = pqs.update_pipeline_row(
            stage=pqs.STAGE_ARCHIVE_PENDING,
            source_path='/tmp/a.mp4',
            status='done',
            db_path=str(tmp_path / 'does-not-exist.db'),
        )
        assert ok is False


class TestUpdatePipelineRowByLegacyId:
    """Unit tests for ``update_pipeline_row_by_legacy_id``."""

    def test_happy_path_by_id(self, geodata_db):
        pqs.dual_write_enqueue(
            source_path='/tmp/q.mp4',
            stage=pqs.STAGE_ARCHIVE_PENDING,
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            legacy_id=42,
            db_path=geodata_db,
        )
        ok = pqs.update_pipeline_row_by_legacy_id(
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            legacy_id=42,
            status='in_progress',
            db_path=geodata_db,
        )
        assert ok is True
        conn = sqlite3.connect(geodata_db)
        try:
            r = conn.execute(
                "SELECT status FROM pipeline_queue WHERE legacy_id = ?",
                (42,),
            ).fetchone()
            assert r[0] == 'in_progress'
        finally:
            conn.close()

    def test_missing_legacy_id_no_op(self, geodata_db):
        ok = pqs.update_pipeline_row_by_legacy_id(
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            legacy_id=999,
            status='done',
            db_path=geodata_db,
        )
        assert ok is False

    def test_no_kwargs_no_op_by_id(self, geodata_db):
        pqs.dual_write_enqueue(
            source_path='/tmp/q2.mp4',
            stage=pqs.STAGE_ARCHIVE_PENDING,
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            legacy_id=43,
            db_path=geodata_db,
        )
        ok = pqs.update_pipeline_row_by_legacy_id(
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            legacy_id=43,
            db_path=geodata_db,
        )
        assert ok is False


# ---------------------------------------------------------------------------
# Wave 4 PR-F1 — release_pipeline_claim helper (review fix #2 of PR #198)
# ---------------------------------------------------------------------------


class TestReleasePipelineClaim:
    """Unit tests for ``release_pipeline_claim``.

    PR-F1 review fix #2 (PR #198): the helper exists so the archive
    worker can release a pipeline_queue claim back to ``pending`` AND
    null ``claimed_by`` / ``claimed_at`` in one atomic UPDATE — closing
    the gap left by ``update_pipeline_row_by_legacy_id``, which
    deliberately whitelists only the "user data" columns and cannot
    clear claim metadata.
    """

    def _claim_and_get_row(self, db, legacy_id):
        # Helper: insert a row, claim it, return the row dict.
        pqs.dual_write_enqueue(
            source_path=f'/tmp/r{legacy_id}.mp4',
            stage=pqs.STAGE_ARCHIVE_PENDING,
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            legacy_id=legacy_id,
            db_path=db,
        )
        return pqs.claim_next_for_stage(
            stage=pqs.STAGE_ARCHIVE_PENDING,
            claimed_by=f'w-{legacy_id}',
            db_path=db,
        )

    def test_release_clears_claim_metadata_and_status(self, geodata_db):
        # Set up a claimed row.
        claimed = self._claim_and_get_row(geodata_db, 1001)
        assert claimed is not None
        assert claimed['status'] == 'in_progress'
        assert claimed['claimed_by'] == 'w-1001'
        assert claimed['claimed_at'] is not None

        ok = pqs.release_pipeline_claim(
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            legacy_id=1001,
            db_path=geodata_db,
        )
        assert ok is True

        conn = sqlite3.connect(geodata_db)
        try:
            r = conn.execute(
                "SELECT status, claimed_by, claimed_at FROM pipeline_queue "
                " WHERE legacy_id = ?",
                (1001,),
            ).fetchone()
            assert r is not None
            assert r[0] == 'pending'
            assert r[1] is None, "claimed_by must be cleared"
            assert r[2] is None, "claimed_at must be cleared"
        finally:
            conn.close()

    def test_release_sets_last_error_when_provided(self, geodata_db):
        self._claim_and_get_row(geodata_db, 1002)
        ok = pqs.release_pipeline_claim(
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            legacy_id=1002,
            last_error='PR-F1 test: simulated mirror failure',
            db_path=geodata_db,
        )
        assert ok is True
        conn = sqlite3.connect(geodata_db)
        try:
            r = conn.execute(
                "SELECT last_error FROM pipeline_queue WHERE legacy_id = ?",
                (1002,),
            ).fetchone()
            assert r is not None
            assert 'PR-F1 test' in r[0]
        finally:
            conn.close()

    def test_release_without_last_error_preserves_existing_value(
        self, geodata_db,
    ):
        # Pre-populate last_error, then release without overwriting.
        self._claim_and_get_row(geodata_db, 1003)
        # Stamp an existing last_error.
        conn = sqlite3.connect(geodata_db)
        try:
            conn.execute(
                "UPDATE pipeline_queue SET last_error = ? "
                " WHERE legacy_id = ?",
                ('pre-existing forensic note', 1003),
            )
            conn.commit()
        finally:
            conn.close()

        ok = pqs.release_pipeline_claim(
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            legacy_id=1003,
            db_path=geodata_db,
        )
        assert ok is True
        conn = sqlite3.connect(geodata_db)
        try:
            r = conn.execute(
                "SELECT last_error FROM pipeline_queue WHERE legacy_id = ?",
                (1003,),
            ).fetchone()
            assert r[0] == 'pre-existing forensic note', (
                "release_pipeline_claim with last_error=None must NOT "
                "overwrite an existing last_error value"
            )
        finally:
            conn.close()

    def test_release_missing_legacy_id_returns_false(self, geodata_db):
        ok = pqs.release_pipeline_claim(
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            legacy_id=99999,
            db_path=geodata_db,
        )
        assert ok is False

    def test_release_missing_args_returns_false(self, geodata_db):
        # Missing legacy_table.
        assert pqs.release_pipeline_claim(
            legacy_table='',
            legacy_id=1,
            db_path=geodata_db,
        ) is False
        # Missing legacy_id.
        assert pqs.release_pipeline_claim(
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            legacy_id=None,  # type: ignore[arg-type]
            db_path=geodata_db,
        ) is False

    def test_release_missing_db_returns_false(self, tmp_path):
        ok = pqs.release_pipeline_claim(
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            legacy_id=1,
            db_path=str(tmp_path / 'does-not-exist.db'),
        )
        assert ok is False

    def test_release_makes_row_visible_to_next_claim(self, geodata_db):
        # End-to-end: release → next claim should succeed.
        self._claim_and_get_row(geodata_db, 1004)
        pqs.release_pipeline_claim(
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            legacy_id=1004,
            db_path=geodata_db,
        )
        next_claim = pqs.claim_next_for_stage(
            stage=pqs.STAGE_ARCHIVE_PENDING,
            claimed_by='w-after-release',
            db_path=geodata_db,
        )
        assert next_claim is not None
        assert next_claim['legacy_id'] == 1004
        assert next_claim['claimed_by'] == 'w-after-release'


# ---------------------------------------------------------------------------
# Wave 4 PR-B — integration: legacy mutation mirrors to pipeline_queue
# ---------------------------------------------------------------------------

class TestArchiveStateTransitions:
    """Integration: each archive_queue mutation mirrors into pipeline_queue."""

    def _enqueue(self, geodata_db, src):
        from services import archive_queue
        ok = archive_queue.enqueue_for_archive(
            src, priority=2, db_path=geodata_db,
        )
        assert ok
        conn = sqlite3.connect(geodata_db)
        try:
            row = conn.execute(
                "SELECT id FROM archive_queue WHERE source_path=?",
                (src,),
            ).fetchone()
            return int(row[0])
        finally:
            conn.close()

    def _pipeline_row(self, geodata_db, src):
        conn = sqlite3.connect(geodata_db)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT * FROM pipeline_queue WHERE source_path=?",
                (src,),
            ).fetchone()
        finally:
            conn.close()

    def test_claim_mirrors_in_progress(self, geodata_db):
        from services import archive_queue
        src = '/tmp/claim.mp4'
        self._enqueue(geodata_db, src)
        claimed = archive_queue.claim_next_for_worker(
            'w1', db_path=geodata_db,
        )
        assert claimed is not None
        row = self._pipeline_row(geodata_db, src)
        assert row['status'] == 'in_progress'
        assert row['stage'] == pqs.STAGE_ARCHIVE_PENDING

    def test_mark_copied_mirrors_done(self, geodata_db):
        from services import archive_queue
        src = '/tmp/copied.mp4'
        rid = self._enqueue(geodata_db, src)
        archive_queue.claim_next_for_worker('w1', db_path=geodata_db)
        ok = archive_queue.mark_copied(
            rid, '/dst/copied.mp4', db_path=geodata_db,
        )
        assert ok
        row = self._pipeline_row(geodata_db, src)
        assert row['stage'] == 'archive_done'
        assert row['status'] == 'done'
        assert row['completed_at'] is not None

    def test_mark_source_gone_mirrors_terminal(self, geodata_db):
        from services import archive_queue
        src = '/tmp/gone.mp4'
        rid = self._enqueue(geodata_db, src)
        archive_queue.claim_next_for_worker('w1', db_path=geodata_db)
        ok = archive_queue.mark_source_gone(rid, db_path=geodata_db)
        assert ok
        row = self._pipeline_row(geodata_db, src)
        assert row['stage'] == 'archive_done'
        assert row['status'] == 'source_gone'

    def test_mark_skipped_stationary_mirrors_terminal(self, geodata_db):
        from services import archive_queue
        src = '/tmp/stationary.mp4'
        rid = self._enqueue(geodata_db, src)
        archive_queue.claim_next_for_worker('w1', db_path=geodata_db)
        ok = archive_queue.mark_skipped_stationary(
            rid, db_path=geodata_db,
        )
        assert ok
        row = self._pipeline_row(geodata_db, src)
        assert row['stage'] == 'archive_done'
        assert row['status'] == 'skipped_stationary'

    def test_release_claim_mirrors_pending(self, geodata_db):
        from services import archive_queue
        src = '/tmp/release.mp4'
        rid = self._enqueue(geodata_db, src)
        archive_queue.claim_next_for_worker('w1', db_path=geodata_db)
        # pre-condition: mirror is 'in_progress'
        assert self._pipeline_row(geodata_db, src)['status'] == 'in_progress'
        ok = archive_queue.release_claim(rid, db_path=geodata_db)
        assert ok
        assert self._pipeline_row(geodata_db, src)['status'] == 'pending'

    def test_mark_failed_pending_mirrors_attempts(self, geodata_db):
        from services import archive_queue
        src = '/tmp/fail.mp4'
        rid = self._enqueue(geodata_db, src)
        archive_queue.claim_next_for_worker('w1', db_path=geodata_db)
        new_status = archive_queue.mark_failed(
            rid, 'transient', max_attempts=3, db_path=geodata_db,
        )
        assert new_status == 'pending'
        row = self._pipeline_row(geodata_db, src)
        assert row['status'] == 'pending'
        assert row['attempts'] == 1
        assert row['last_error'] == 'transient'

    def test_mark_failed_dead_letter_mirrors_terminal(self, geodata_db):
        from services import archive_queue
        src = '/tmp/dl.mp4'
        rid = self._enqueue(geodata_db, src)
        archive_queue.claim_next_for_worker('w1', db_path=geodata_db)
        # Force the row to dead_letter on first failure with cap=1
        new_status = archive_queue.mark_failed(
            rid, 'permanent', max_attempts=1, db_path=geodata_db,
        )
        assert new_status == 'dead_letter'
        row = self._pipeline_row(geodata_db, src)
        assert row['stage'] == 'archive_done'
        assert row['status'] == 'dead_letter'
        assert row['last_error'] == 'permanent'


class TestIndexingStateTransitions:
    """Integration: each indexing_queue_service mutation mirrors."""

    def test_claim_complete_mirrors(self, geodata_db):
        from services import indexing_queue_service as iqs
        from services.mapping_service import canonical_key
        # Enqueue
        ok = iqs.enqueue_for_indexing(
            geodata_db, '/tmp/clip.mp4',
            priority=50,
            source='watcher',
        )
        assert ok
        ck = canonical_key('/tmp/clip.mp4')
        # Claim
        claimed = iqs.claim_next_queue_item(geodata_db, 'w1')
        assert claimed is not None
        conn = sqlite3.connect(geodata_db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status FROM pipeline_queue WHERE source_path=?",
                (ck,),
            ).fetchone()
            assert row['status'] == 'in_progress'
        finally:
            conn.close()
        # Complete (terminal — pipeline row goes to 'index_done'/'done',
        # legacy row deleted)
        done = iqs.complete_queue_item(
            geodata_db, ck,
            claimed_by='w1',
            claimed_at=claimed['claimed_at'],
        )
        assert done
        conn = sqlite3.connect(geodata_db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT stage, status, completed_at FROM pipeline_queue "
                "WHERE source_path=?",
                (ck,),
            ).fetchone()
            assert row['stage'] == 'index_done'
            assert row['status'] == 'done'
            assert row['completed_at'] is not None
        finally:
            conn.close()

    def test_release_claim_mirrors_pending(self, geodata_db):
        from services import indexing_queue_service as iqs
        from services.mapping_service import canonical_key
        iqs.enqueue_for_indexing(
            geodata_db, '/tmp/clip2.mp4',
            priority=50, source='watcher',
        )
        ck = canonical_key('/tmp/clip2.mp4')
        claimed = iqs.claim_next_queue_item(geodata_db, 'w1')
        assert claimed is not None
        ok = iqs.release_claim(
            geodata_db, ck,
            claimed_by='w1', claimed_at=claimed['claimed_at'],
        )
        assert ok
        conn = sqlite3.connect(geodata_db)
        try:
            row = conn.execute(
                "SELECT status FROM pipeline_queue WHERE source_path=?",
                (ck,),
            ).fetchone()
            assert row[0] == 'pending'
        finally:
            conn.close()

    def test_defer_mirrors_attempts_and_retry(self, geodata_db):
        import time as _t
        from services import indexing_queue_service as iqs
        from services.mapping_service import canonical_key
        iqs.enqueue_for_indexing(
            geodata_db, '/tmp/clip3.mp4',
            priority=50, source='watcher',
        )
        ck = canonical_key('/tmp/clip3.mp4')
        claimed = iqs.claim_next_queue_item(geodata_db, 'w1')
        assert claimed is not None
        next_at = _t.time() + 60
        ok = iqs.defer_queue_item(
            geodata_db, ck, next_at,
            bump_attempts=True, last_error='parse error',
            claimed_by='w1', claimed_at=claimed['claimed_at'],
        )
        assert ok
        conn = sqlite3.connect(geodata_db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status, attempts, last_error, next_retry_at "
                "FROM pipeline_queue WHERE source_path=?",
                (ck,),
            ).fetchone()
            assert row['status'] == 'pending'
            assert row['attempts'] == 1
            assert row['last_error'] == 'parse error'
            assert row['next_retry_at'] == next_at
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Wave 4 PR-B — best-effort error swallowing
# ---------------------------------------------------------------------------

class TestStateTransitionErrorSwallowing:
    """Pipeline-side glitches must NEVER abort a legacy mutation."""

    def test_mark_copied_succeeds_when_pipeline_db_missing(
        self, geodata_db, tmp_path,
    ):
        """If geodata.db is removed mid-flight, mark_copied still
        returns True for the archive_queue row (legacy is the source
        of truth in PR-B)."""
        from services import archive_queue
        src = '/tmp/swallow.mp4'
        archive_queue.enqueue_for_archive(
            src, priority=2, db_path=geodata_db,
        )
        rid = sqlite3.connect(geodata_db).execute(
            "SELECT id FROM archive_queue WHERE source_path=?", (src,),
        ).fetchone()[0]
        archive_queue.claim_next_for_worker('w1', db_path=geodata_db)
        # Wipe the pipeline_queue table (simulate corruption / missing).
        conn = sqlite3.connect(geodata_db)
        conn.execute("DROP TABLE pipeline_queue")
        conn.commit()
        conn.close()
        ok = archive_queue.mark_copied(
            rid, '/dst/swallow.mp4', db_path=geodata_db,
        )
        assert ok is True  # legacy succeeded

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


# ---------------------------------------------------------------------------
# Wave 4 PR-B (review #191 fixes) — additional regression tests
# ---------------------------------------------------------------------------

class TestArchiveBatchEnqueueLegacyId:
    """Review #191 Info #6 fix: `enqueue_many_for_archive` must
    populate `legacy_id` on every batched `pipeline_queue` row so
    state mutations on those rows (lookup-by-legacy-id) actually find
    the mirror.
    """

    def test_batch_enqueue_populates_legacy_id_for_every_row(
        self, geodata_db,
    ):
        from services import archive_queue
        n = archive_queue.enqueue_many_for_archive(
            ['/tmp/batch-a.mp4', '/tmp/batch-b.mp4', '/tmp/batch-c.mp4'],
            priority=2, db_path=geodata_db,
        )
        assert n == 3
        conn = sqlite3.connect(geodata_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT source_path, legacy_id, legacy_table "
                "FROM pipeline_queue WHERE legacy_table = ? "
                "ORDER BY source_path",
                (pqs.LEGACY_TABLE_ARCHIVE,),
            ).fetchall()
            assert len(rows) == 3
            for row in rows:
                assert row['legacy_id'] is not None
                assert int(row['legacy_id']) > 0
            # Verify each pipeline_queue.legacy_id matches the
            # archive_queue.id with the same source_path.
            for row in rows:
                aq_id = conn.execute(
                    "SELECT id FROM archive_queue WHERE source_path = ?",
                    (row['source_path'],),
                ).fetchone()[0]
                assert int(row['legacy_id']) == int(aq_id)
        finally:
            conn.close()

    def test_batch_enqueue_then_mark_copied_mirrors_state(
        self, geodata_db,
    ):
        """Without the legacy_id fix, mark_copied's lookup-by-legacy-id
        would silently no-op on batched rows. With the fix, the mirror
        flips to `status='done'` like for single enqueues.
        """
        from services import archive_queue
        archive_queue.enqueue_many_for_archive(
            ['/tmp/batch-mark.mp4'], priority=2, db_path=geodata_db,
        )
        conn = sqlite3.connect(geodata_db)
        try:
            row_id = conn.execute(
                "SELECT id FROM archive_queue WHERE source_path = ?",
                ('/tmp/batch-mark.mp4',),
            ).fetchone()[0]
        finally:
            conn.close()
        # Claim then complete.
        archive_queue.claim_next_for_worker('w1', db_path=geodata_db)
        ok = archive_queue.mark_copied(
            row_id, '/dst/batch-mark.mp4', db_path=geodata_db,
        )
        assert ok
        conn = sqlite3.connect(geodata_db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT stage, status, completed_at FROM pipeline_queue "
                "WHERE source_path = ?",
                ('/tmp/batch-mark.mp4',),
            ).fetchone()
            assert row['stage'] == 'archive_done'
            assert row['status'] == 'done'
            assert row['completed_at'] is not None
        finally:
            conn.close()


class TestCloudMarkUploadFailureOrdering:
    """Review #191 Warning #1 fix: `_mark_upload_failure` must NOT
    write the pipeline_queue mirror itself. The caller is responsible
    for calling the mirror AFTER `_fsync_db(conn)` so the legacy
    commit always lands first.
    """

    def test_mark_upload_failure_returns_post_state(self, tmp_path):
        from services import cloud_archive_service as cas
        db = str(tmp_path / 'cloud_sync.db')
        conn = cas._init_cloud_tables(db)
        try:
            conn.execute(
                "INSERT INTO cloud_synced_files (file_path, status, "
                "retry_count) VALUES (?, 'uploading', 0)",
                ('events/2025-01-01_00-00-00/file.mp4',),
            )
            conn.commit()
            post = cas._mark_upload_failure(
                conn, 'events/2025-01-01_00-00-00/file.mp4',
                'simulated rclone error',
            )
            assert post is not None
            status, attempts = post
            assert status == 'failed'
            assert attempts == 1
        finally:
            conn.close()

    def test_mark_upload_failure_returns_none_on_unknown_path(
        self, tmp_path,
    ):
        from services import cloud_archive_service as cas
        db = str(tmp_path / 'cloud_sync2.db')
        conn = cas._init_cloud_tables(db)
        try:
            post = cas._mark_upload_failure(
                conn, 'no/such/file.mp4', 'oops',
            )
            assert post is None
        finally:
            conn.close()


class TestPipelineRowKwargGate:
    """Review #191 Info #8 fix: the `_UPDATE_COLUMNS` tuple drives
    both helpers' "no kwargs passed" gate AND the SET-clause builder.
    Verifies the tuple is the single source of truth.
    """

    def test_update_pipeline_row_no_kwargs_returns_false(self, tmp_path):
        # Even with a valid db / row, with no settable kwargs the
        # helper must silently no-op.
        db = str(tmp_path / 'pq.db')
        # The helper short-circuits on missing DB, but we want the
        # "no kwargs" path. Create the file first.
        import sqlite3
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE pipeline_queue (id INTEGER PRIMARY KEY, "
            "stage TEXT, source_path TEXT)"
        )
        conn.commit()
        conn.close()
        ok = pqs.update_pipeline_row(
            stage='archive_pending',
            source_path='/some/path.mp4',
            db_path=db,
        )
        assert ok is False

    def test_update_columns_tuple_matches_kwarg_to_column_keys(self):
        # Defense in depth — the lookup table and the order list must
        # stay in lockstep so a future kwarg addition can't be wired
        # into one but not the other.
        assert set(pqs._UPDATE_COLUMNS) == set(pqs._KWARG_TO_COLUMN.keys())

# ============================================================================
# Wave 4 PR-C reader API tests ? issue #184
# ============================================================================
# These exercise the new `claim_next_for_stage` / `peek_next_for_stage`
# / `ready_count_for_stage` helpers that PR-C adds to make the unified
# queue READABLE (PR-A added writes, PR-B added state-mirror updates;
# PR-C completes the API surface so PR-D can switch readers over).


class TestClaimNextForStage:
    def _enqueue(self, db, source, *, stage='archive_pending',
                 priority=2, payload=None, status='pending',
                 next_retry_at=None):
        # Use the dual_write_enqueue helper for the common path; for
        # next_retry_at we need to hand-insert because the public
        # producer hook doesn't accept it (only _try_upload's failure
        # path sets next_retry_at via update_pipeline_row).
        ok = __import__('services.pipeline_queue_service',
                        fromlist=['dual_write_enqueue']).dual_write_enqueue(
            source_path=source,
            stage=stage,
            legacy_table=__import__('services.pipeline_queue_service',
                                     fromlist=['LEGACY_TABLE_ARCHIVE']).LEGACY_TABLE_ARCHIVE,
            priority=priority,
            payload=payload,
            status=status,
            db_path=db,
        )
        assert ok is True
        if next_retry_at is not None:
            import sqlite3 as _sql
            c = _sql.connect(db)
            try:
                c.execute(
                    "UPDATE pipeline_queue SET next_retry_at = ? "
                    "WHERE source_path = ?",
                    (next_retry_at, source),
                )
                c.commit()
            finally:
                c.close()

    def test_returns_none_on_empty_queue(self, geodata_db):
        row = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='w1', db_path=geodata_db,
        )
        assert row is None

    def test_returns_none_when_db_missing(self, tmp_path):
        missing = str(tmp_path / 'nope.db')
        row = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='w1', db_path=missing,
        )
        assert row is None

    def test_returns_none_when_stage_blank(self, geodata_db):
        row = pqs.claim_next_for_stage(
            stage='', claimed_by='w1', db_path=geodata_db,
        )
        assert row is None

    def test_claims_pending_row_and_marks_in_progress(self, geodata_db):
        self._enqueue(geodata_db, '/x/a.mp4',
                       payload={'expected_size': 100, 'expected_mtime': 1.0})
        claimed = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='worker-A',
            db_path=geodata_db,
        )
        assert claimed is not None
        assert claimed['source_path'] == '/x/a.mp4'
        assert claimed['status'] == 'in_progress'
        assert claimed['attempts'] == 1
        assert claimed['_claimed_by'] == 'worker-A'
        assert claimed['payload'] == {'expected_size': 100, 'expected_mtime': 1.0}
        # Verify DB state matches the returned snapshot.
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        c.row_factory = _sql.Row
        try:
            row = c.execute(
                "SELECT status, attempts FROM pipeline_queue WHERE id = ?",
                (claimed['id'],),
            ).fetchone()
            assert row['status'] == 'in_progress'
            assert row['attempts'] == 1
        finally:
            c.close()

    def test_claim_increments_attempts(self, geodata_db):
        self._enqueue(geodata_db, '/x/a.mp4')
        # Bump attempts to 5 first so we can verify the increment math.
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        try:
            c.execute("UPDATE pipeline_queue SET attempts = 5 "
                       "WHERE source_path = ?", ('/x/a.mp4',))
            c.commit()
        finally:
            c.close()
        claimed = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='w1', db_path=geodata_db,
        )
        assert claimed['attempts'] == 6

    def test_skips_in_progress_rows(self, geodata_db):
        self._enqueue(geodata_db, '/x/a.mp4', status='in_progress')
        self._enqueue(geodata_db, '/x/b.mp4', status='pending')
        claimed = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='w1', db_path=geodata_db,
        )
        assert claimed is not None
        assert claimed['source_path'] == '/x/b.mp4'

    def test_skips_done_rows(self, geodata_db):
        self._enqueue(geodata_db, '/x/done.mp4', status='done')
        self._enqueue(geodata_db, '/x/pending.mp4', status='pending')
        claimed = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='w1', db_path=geodata_db,
        )
        assert claimed['source_path'] == '/x/pending.mp4'

    def test_orders_by_priority_first(self, geodata_db):
        # Insert in reverse priority order; expect highest priority
        # (lowest number) to come back first.
        self._enqueue(geodata_db, '/x/low.mp4', priority=5)
        self._enqueue(geodata_db, '/x/high.mp4', priority=1)
        self._enqueue(geodata_db, '/x/mid.mp4', priority=3)
        claimed = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='w1', db_path=geodata_db,
        )
        assert claimed['source_path'] == '/x/high.mp4'

    def test_orders_by_enqueued_at_within_priority(self, geodata_db):
        # Same priority ? older row goes first.
        self._enqueue(geodata_db, '/x/newer.mp4', priority=2)
        # Force the second row to have an older enqueued_at than the
        # first by hand-editing.
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        try:
            c.execute("UPDATE pipeline_queue SET enqueued_at = 1000 "
                       "WHERE source_path = ?", ('/x/newer.mp4',))
            c.commit()
        finally:
            c.close()
        self._enqueue(geodata_db, '/x/older.mp4', priority=2)
        c = _sql.connect(geodata_db)
        try:
            c.execute("UPDATE pipeline_queue SET enqueued_at = 100 "
                       "WHERE source_path = ?", ('/x/older.mp4',))
            c.commit()
        finally:
            c.close()
        claimed = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='w1', db_path=geodata_db,
        )
        assert claimed['source_path'] == '/x/older.mp4'

    def test_filters_by_stage(self, geodata_db):
        # Two rows in different stages ? claim from one, the other
        # remains untouched.
        self._enqueue(geodata_db, '/x/a.mp4', stage='archive_pending')
        # Build an indexing-stage row directly so we don't have to
        # juggle legacy_table.
        ok = pqs.dual_write_enqueue(
            source_path='/x/idx.mp4', stage='index_pending',
            legacy_table=pqs.LEGACY_TABLE_INDEXING, db_path=geodata_db,
        )
        assert ok is True
        claimed = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='w1', db_path=geodata_db,
        )
        assert claimed['source_path'] == '/x/a.mp4'
        # The indexing row must still be pending.
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        c.row_factory = _sql.Row
        try:
            row = c.execute(
                "SELECT status FROM pipeline_queue WHERE source_path = ?",
                ('/x/idx.mp4',),
            ).fetchone()
            assert row['status'] == 'pending'
        finally:
            c.close()

    def test_skips_rows_with_future_next_retry_at(self, geodata_db):
        self._enqueue(geodata_db, '/x/wait.mp4',
                       next_retry_at=2_000_000_000.0)  # year 2033
        self._enqueue(geodata_db, '/x/ready.mp4')
        claimed = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='w1', db_path=geodata_db,
            now=1_000_000_000.0,
        )
        assert claimed['source_path'] == '/x/ready.mp4'

    def test_picks_row_with_due_next_retry_at(self, geodata_db):
        self._enqueue(geodata_db, '/x/due.mp4', next_retry_at=500.0)
        claimed = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='w1', db_path=geodata_db,
            now=1000.0,
        )
        assert claimed is not None
        assert claimed['source_path'] == '/x/due.mp4'

    def test_picks_row_with_null_next_retry_at(self, geodata_db):
        self._enqueue(geodata_db, '/x/never-failed.mp4')  # next_retry_at IS NULL
        claimed = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='w1', db_path=geodata_db,
        )
        assert claimed is not None

    def test_sequential_claims_pick_different_rows(self, geodata_db):
        # Sequential depletion: each call returns a distinct row,
        # the third returns None when both rows are in_progress.
        # The actual race-defense (BEGIN IMMEDIATE + WHERE rowcount
        # check) is exercised by
        # ``test_two_threaded_claims_get_different_rows`` below.
        self._enqueue(geodata_db, '/x/a.mp4', priority=1)
        self._enqueue(geodata_db, '/x/b.mp4', priority=1)
        c1 = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='w1', db_path=geodata_db,
        )
        c2 = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='w2', db_path=geodata_db,
        )
        assert c1 is not None and c2 is not None
        assert c1['id'] != c2['id']
        # Third claim should return None - both rows are in_progress.
        c3 = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='w3', db_path=geodata_db,
        )
        assert c3 is None

    def test_two_threaded_claims_get_different_rows(self, geodata_db):
        # Real concurrency: two threads race through ``claim_next_for_stage``
        # against the same DB. The BEGIN IMMEDIATE write-lock + the
        # defensive ``WHERE status='pending'`` rowcount check together
        # guarantee that each row is claimed by at most one worker even
        # when both threads enter the function simultaneously. Without
        # the atomicity primitives in place, this test would
        # occasionally see the same row returned twice (or one thread
        # succeeding with a row and the other returning the same row's
        # post-update view).
        import threading
        self._enqueue(geodata_db, '/x/race-a.mp4', priority=1)
        self._enqueue(geodata_db, '/x/race-b.mp4', priority=1)
        results = [None, None]
        barrier = threading.Barrier(2)

        def _worker(idx, name):
            barrier.wait(timeout=5.0)
            results[idx] = pqs.claim_next_for_stage(
                stage='archive_pending', claimed_by=name,
                db_path=geodata_db,
            )

        t1 = threading.Thread(target=_worker, args=(0, 'thread-1'))
        t2 = threading.Thread(target=_worker, args=(1, 'thread-2'))
        t1.start(); t2.start()
        t1.join(timeout=10.0); t2.join(timeout=10.0)
        assert not t1.is_alive() and not t2.is_alive()
        assert results[0] is not None and results[1] is not None
        # Distinct ids: the BEGIN IMMEDIATE serialised the two
        # SELECT-then-UPDATE pairs so no double-pick happened.
        assert results[0]['id'] != results[1]['id']
        # Both rows are now in_progress.
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        try:
            n = c.execute(
                "SELECT COUNT(*) FROM pipeline_queue "
                "WHERE status = 'in_progress'"
            ).fetchone()[0]
            assert n == 2
        finally:
            c.close()

    def test_returns_none_when_db_corrupt(self, tmp_path):
        # Write non-SQLite bytes into a path with the right extension;
        # ``sqlite3.connect`` succeeds (it's lazy) but the first
        # statement raises ``sqlite3.DatabaseError`` (a subclass of
        # ``sqlite3.Error``). The function must swallow it and return
        # None per the documented contract — no exception propagation.
        bad = tmp_path / 'corrupt.db'
        bad.write_bytes(b'this is not a sqlite database file at all\x00\x00')
        result = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='w1', db_path=str(bad),
        )
        assert result is None
        # peek_next and ready_count must also degrade gracefully —
        # they share the same swallow contract.
        assert pqs.peek_next_for_stage(
            stage='archive_pending', db_path=str(bad),
        ) is None
        assert pqs.ready_count_for_stage(
            stage='archive_pending', db_path=str(bad),
        ) == 0

    def test_payload_non_dict_json_coerced_to_empty(self, geodata_db):
        # Producers declare payload as Dict[str, Any] but a hand-crafted
        # row (or a future producer bug) could store a JSON list /
        # number / string / null. Callers expect ``.get(...)`` on
        # ``row['payload']`` to work, so the helper must coerce
        # non-dict to {} matching the docstring.
        self._enqueue(geodata_db, '/x/list-payload.mp4')
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        try:
            c.execute(
                "UPDATE pipeline_queue SET payload_json = '[1, 2, 3]' "
                "WHERE source_path = ?", ('/x/list-payload.mp4',),
            )
            c.commit()
        finally:
            c.close()
        claimed = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='w1', db_path=geodata_db,
        )
        assert claimed is not None
        assert claimed['payload'] == {}
        # Same for a JSON null.
        self._enqueue(geodata_db, '/x/null-payload.mp4')
        c = _sql.connect(geodata_db)
        try:
            c.execute(
                "UPDATE pipeline_queue SET payload_json = 'null' "
                "WHERE source_path = ?", ('/x/null-payload.mp4',),
            )
            c.commit()
        finally:
            c.close()
        claimed2 = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='w1', db_path=geodata_db,
        )
        assert claimed2 is not None
        assert claimed2['payload'] == {}

    def test_payload_dict_synthesized_even_when_json_missing(self, geodata_db):
        self._enqueue(geodata_db, '/x/no-payload.mp4', payload=None)
        claimed = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='w1', db_path=geodata_db,
        )
        assert claimed['payload'] == {}

    def test_payload_dict_safe_against_malformed_json(self, geodata_db):
        self._enqueue(geodata_db, '/x/bad.mp4')
        # Hand-corrupt the payload_json so json.loads will raise.
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        try:
            c.execute("UPDATE pipeline_queue SET payload_json = '{not json' "
                       "WHERE source_path = ?", ('/x/bad.mp4',))
            c.commit()
        finally:
            c.close()
        claimed = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='w1', db_path=geodata_db,
        )
        # Bad JSON yields {} ? we don't crash, we don't lose the claim.
        assert claimed is not None
        assert claimed['payload'] == {}

    def test_claim_does_not_mutate_other_rows(self, geodata_db):
        self._enqueue(geodata_db, '/x/a.mp4')
        self._enqueue(geodata_db, '/x/b.mp4')
        self._enqueue(geodata_db, '/x/c.mp4')
        pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='w1', db_path=geodata_db,
        )
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        c.row_factory = _sql.Row
        try:
            n_pending = c.execute(
                "SELECT COUNT(*) AS n FROM pipeline_queue "
                "WHERE status = 'pending'"
            ).fetchone()['n']
            n_in_progress = c.execute(
                "SELECT COUNT(*) AS n FROM pipeline_queue "
                "WHERE status = 'in_progress'"
            ).fetchone()['n']
            assert n_pending == 2
            assert n_in_progress == 1
        finally:
            c.close()


class TestPeekNextForStage:
    def test_returns_none_on_empty_queue(self, geodata_db):
        assert pqs.peek_next_for_stage(
            stage='archive_pending', db_path=geodata_db,
        ) is None

    def test_returns_next_without_mutating(self, geodata_db):
        pqs.dual_write_enqueue(
            source_path='/x/a.mp4',
            stage=pqs.STAGE_ARCHIVE_PENDING,
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            priority=2,
            db_path=geodata_db,
        )
        peeked = pqs.peek_next_for_stage(
            stage='archive_pending', db_path=geodata_db,
        )
        assert peeked is not None
        assert peeked['source_path'] == '/x/a.mp4'
        # Status must still be pending after peek.
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        c.row_factory = _sql.Row
        try:
            row = c.execute(
                "SELECT status, attempts FROM pipeline_queue "
                "WHERE source_path = ?", ('/x/a.mp4',),
            ).fetchone()
            assert row['status'] == 'pending'
            assert row['attempts'] == 0
        finally:
            c.close()

    def test_peek_then_claim_returns_same_row(self, geodata_db):
        pqs.dual_write_enqueue(
            source_path='/x/a.mp4',
            stage=pqs.STAGE_ARCHIVE_PENDING,
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            db_path=geodata_db,
        )
        peeked = pqs.peek_next_for_stage(
            stage='archive_pending', db_path=geodata_db,
        )
        claimed = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='w1', db_path=geodata_db,
        )
        assert peeked['id'] == claimed['id']

    def test_peek_honors_next_retry_at(self, geodata_db):
        pqs.dual_write_enqueue(
            source_path='/x/wait.mp4',
            stage=pqs.STAGE_ARCHIVE_PENDING,
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            db_path=geodata_db,
        )
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        try:
            c.execute("UPDATE pipeline_queue SET next_retry_at = 9e9")
            c.commit()
        finally:
            c.close()
        peeked = pqs.peek_next_for_stage(
            stage='archive_pending', db_path=geodata_db, now=1.0,
        )
        assert peeked is None

    def test_blank_stage_returns_none(self, geodata_db):
        assert pqs.peek_next_for_stage(stage='', db_path=geodata_db) is None


class TestReadyCountForStage:
    def test_zero_when_empty(self, geodata_db):
        assert pqs.ready_count_for_stage(
            stage='archive_pending', db_path=geodata_db,
        ) == 0

    def test_counts_pending_only(self, geodata_db):
        for i, status in enumerate(['pending', 'pending', 'in_progress', 'done']):
            pqs.dual_write_enqueue(
                source_path=f'/x/r{i}.mp4',
                stage=pqs.STAGE_ARCHIVE_PENDING,
                legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
                status=status,
                db_path=geodata_db,
            )
        assert pqs.ready_count_for_stage(
            stage='archive_pending', db_path=geodata_db,
        ) == 2

    def test_excludes_future_retry_rows(self, geodata_db):
        pqs.dual_write_enqueue(
            source_path='/x/future.mp4',
            stage=pqs.STAGE_ARCHIVE_PENDING,
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            db_path=geodata_db,
        )
        pqs.dual_write_enqueue(
            source_path='/x/now.mp4',
            stage=pqs.STAGE_ARCHIVE_PENDING,
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            db_path=geodata_db,
        )
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        try:
            c.execute("UPDATE pipeline_queue SET next_retry_at = 9e9 "
                       "WHERE source_path = '/x/future.mp4'")
            c.commit()
        finally:
            c.close()
        assert pqs.ready_count_for_stage(
            stage='archive_pending', db_path=geodata_db, now=1.0,
        ) == 1

    def test_filters_by_stage(self, geodata_db):
        pqs.dual_write_enqueue(
            source_path='/x/arch.mp4',
            stage=pqs.STAGE_ARCHIVE_PENDING,
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            db_path=geodata_db,
        )
        pqs.dual_write_enqueue(
            source_path='/x/idx.mp4',
            stage=pqs.STAGE_INDEX_PENDING,
            legacy_table=pqs.LEGACY_TABLE_INDEXING,
            db_path=geodata_db,
        )
        assert pqs.ready_count_for_stage(
            stage='archive_pending', db_path=geodata_db,
        ) == 1
        assert pqs.ready_count_for_stage(
            stage='index_pending', db_path=geodata_db,
        ) == 1

    def test_returns_zero_when_db_missing(self, tmp_path):
        missing = str(tmp_path / 'absent.db')
        assert pqs.ready_count_for_stage(
            stage='archive_pending', db_path=missing,
        ) == 0


# ============================================================================
# Wave 4 PR-D ? schema v17 + recover_stale_claims_pipeline (issue #184 / #193)
# ============================================================================
# Covers the new `claimed_by` / `claimed_at` column persistence on
# claim, the new `recover_stale_claims_pipeline()` helper, and the
# v16 -> v17 migration path.


class TestSchemaV17:
    def test_schema_version_is_v17_or_later(self):
        assert _SCHEMA_VERSION >= 17

    def test_pipeline_queue_has_claim_columns(self, geodata_db):
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        try:
            cols = {r[1] for r in c.execute(
                "PRAGMA table_info(pipeline_queue)"
            ).fetchall()}
            assert 'claimed_by' in cols
            assert 'claimed_at' in cols
        finally:
            c.close()

    def test_idx_pipeline_stale_claims_exists(self, geodata_db):
        # Findings #8: assert not just the index name but also that
        # the partial WHERE clause is intact. A future change that
        # drops "WHERE status='in_progress' AND claimed_at IS NOT NULL"
        # (turning it into a full index) would silently pass a
        # name-only check — and a full index defeats the
        # "stay small even on a queue with thousands of done rows"
        # optimisation that justifies the column.
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        try:
            row = c.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='index' AND name='idx_pipeline_stale_claims'"
            ).fetchone()
            assert row is not None, \
                "idx_pipeline_stale_claims missing"
            sql = row[1] or ''
            # SQLite normalises CREATE INDEX SQL but preserves the
            # column references and the WHERE clause body.
            assert 'claimed_at' in sql, \
                f"index missing claimed_at column: {sql!r}"
            assert "status='in_progress'" in sql.replace(' ', '') \
                or "status = 'in_progress'" in sql, \
                f"index missing partial WHERE on status: {sql!r}"
            assert 'claimed_at IS NOT NULL' in sql.replace('  ', ' '), \
                f"index missing claimed_at IS NOT NULL: {sql!r}"
        finally:
            c.close()

    def test_v16_to_v17_alter_table_idempotent(self, tmp_path):
        # Build a v16 DB by hand (no claim columns) and simulate the
        # _init_db migration path running against it. The schema's
        # CREATE TABLE IF NOT EXISTS won't re-create the table, so
        # the only path that adds the columns is the v16->v17
        # ALTER TABLE block in _init_db.
        import sqlite3 as _sql
        db = str(tmp_path / 'v16-style.db')
        c = _sql.connect(db)
        try:
            c.executescript(
                """
                CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
                INSERT INTO schema_version (version) VALUES (16);
                CREATE TABLE pipeline_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_path TEXT NOT NULL,
                    dest_path TEXT,
                    stage TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    priority INTEGER DEFAULT 5,
                    attempts INTEGER DEFAULT 0,
                    last_error TEXT,
                    next_retry_at REAL,
                    enqueued_at REAL NOT NULL,
                    completed_at REAL,
                    payload_json TEXT,
                    legacy_id INTEGER,
                    legacy_table TEXT,
                    UNIQUE(source_path, stage, legacy_table)
                );
                INSERT INTO pipeline_queue
                    (source_path, stage, enqueued_at)
                VALUES ('/legacy/v16-row.mp4', 'archive_pending', 100.0);
                """
            )
            c.commit()
        finally:
            c.close()
        # Run the migration.
        conn = _init_db(db)
        conn.close()
        # Verify columns exist and the v16 row survived.
        c = _sql.connect(db)
        c.row_factory = _sql.Row
        try:
            cols = {r[1] for r in c.execute(
                "PRAGMA table_info(pipeline_queue)"
            ).fetchall()}
            assert 'claimed_by' in cols
            assert 'claimed_at' in cols
            row = c.execute(
                "SELECT claimed_by, claimed_at, source_path FROM pipeline_queue "
                "WHERE source_path = '/legacy/v16-row.mp4'"
            ).fetchone()
            assert row is not None
            assert row['claimed_by'] is None
            assert row['claimed_at'] is None
            v = c.execute("SELECT version FROM schema_version").fetchone()[0]
            assert v == _SCHEMA_VERSION
        finally:
            c.close()
        # Run _init_db again to verify the v16->v17 path is idempotent
        # (re-running on an already-v17 DB must not raise).
        conn2 = _init_db(db)
        conn2.close()
        # Findings #9: post-assertion that the second migration didn't
        # reset anything — schema_version should still be _SCHEMA_VERSION,
        # the v16 row's claim metadata should still be NULL, and the
        # columns should still be present.
        c = _sql.connect(db)
        c.row_factory = _sql.Row
        try:
            v = c.execute("SELECT version FROM schema_version").fetchone()[0]
            assert v == _SCHEMA_VERSION, \
                f"second _init_db reset schema_version to {v}"
            cols = {r[1] for r in c.execute(
                "PRAGMA table_info(pipeline_queue)"
            ).fetchall()}
            assert 'claimed_by' in cols
            assert 'claimed_at' in cols
            row = c.execute(
                "SELECT claimed_by, claimed_at FROM pipeline_queue "
                "WHERE source_path = '/legacy/v16-row.mp4'"
            ).fetchone()
            assert row is not None, \
                "second _init_db lost the v16 row"
            assert row['claimed_by'] is None
            assert row['claimed_at'] is None
        finally:
            c.close()


class TestClaimPersistsClaimMetadata:
    def test_claim_writes_claimed_by_and_claimed_at(self, geodata_db):
        pqs.dual_write_enqueue(
            source_path='/x/persist.mp4',
            stage=pqs.STAGE_ARCHIVE_PENDING,
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            db_path=geodata_db,
        )
        result = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='persistence-worker',
            db_path=geodata_db, now=12345.0,
        )
        assert result is not None
        assert result['claimed_by'] == 'persistence-worker'
        assert result['claimed_at'] == 12345.0
        # Synthesised back-compat key still set, equal to persisted.
        assert result['_claimed_by'] == 'persistence-worker'
        # Verify against DB.
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        c.row_factory = _sql.Row
        try:
            row = c.execute(
                "SELECT claimed_by, claimed_at, status FROM pipeline_queue "
                "WHERE source_path = ?", ('/x/persist.mp4',),
            ).fetchone()
            assert row['claimed_by'] == 'persistence-worker'
            assert row['claimed_at'] == 12345.0
            assert row['status'] == 'in_progress'
        finally:
            c.close()


class TestRecoverStaleClaimsPipeline:
    def _seed_in_progress(self, db, source, claimed_by, claimed_at):
        # Insert a row directly in the in_progress state ? simulates a
        # claim from a worker that subsequently crashed.
        import sqlite3 as _sql
        c = _sql.connect(db)
        try:
            c.execute(
                """INSERT INTO pipeline_queue
                       (source_path, stage, status, priority,
                        enqueued_at, claimed_by, claimed_at, attempts,
                        legacy_table)
                   VALUES (?, 'archive_pending', 'in_progress', 2,
                           100.0, ?, ?, 1, 'archive_queue')""",
                (source, claimed_by, claimed_at),
            )
            c.commit()
        finally:
            c.close()

    def test_returns_zero_on_empty(self, geodata_db):
        assert pqs.recover_stale_claims_pipeline(
            db_path=geodata_db,
        ) == 0

    def test_returns_zero_when_db_missing(self, tmp_path):
        missing = str(tmp_path / 'absent.db')
        assert pqs.recover_stale_claims_pipeline(
            db_path=missing,
        ) == 0

    def test_releases_old_claim(self, geodata_db):
        self._seed_in_progress(
            geodata_db, '/x/orphan.mp4', 'crashed-worker',
            claimed_at=100.0,
        )
        # 'now' = 5000, max_age = 1800 -> cutoff = 3200; row's
        # claimed_at=100 < 3200 -> stale.
        released = pqs.recover_stale_claims_pipeline(
            db_path=geodata_db, max_age_seconds=1800.0, now=5000.0,
        )
        assert released == 1
        # Verify row is now pending and claim metadata cleared.
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        c.row_factory = _sql.Row
        try:
            row = c.execute(
                "SELECT status, claimed_by, claimed_at, attempts "
                "FROM pipeline_queue WHERE source_path = ?",
                ('/x/orphan.mp4',),
            ).fetchone()
            assert row['status'] == 'pending'
            assert row['claimed_by'] is None
            assert row['claimed_at'] is None
            # Attempts preserved ? stale recovery doesn't gift retries.
            assert row['attempts'] == 1
        finally:
            c.close()

    def test_does_not_release_recent_claim(self, geodata_db):
        self._seed_in_progress(
            geodata_db, '/x/fresh.mp4', 'live-worker',
            claimed_at=4500.0,
        )
        # cutoff = 5000 - 1800 = 3200; claimed_at=4500 > 3200 -> not stale.
        released = pqs.recover_stale_claims_pipeline(
            db_path=geodata_db, max_age_seconds=1800.0, now=5000.0,
        )
        assert released == 0
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        c.row_factory = _sql.Row
        try:
            row = c.execute(
                "SELECT status, claimed_by FROM pipeline_queue "
                "WHERE source_path = ?", ('/x/fresh.mp4',),
            ).fetchone()
            assert row['status'] == 'in_progress'
            assert row['claimed_by'] == 'live-worker'
        finally:
            c.close()

    def test_does_not_touch_pending_or_done_rows(self, geodata_db):
        # Build one row in each status; only the in_progress one with
        # an old claim should be touched.
        pqs.dual_write_enqueue(
            source_path='/x/pending.mp4',
            stage=pqs.STAGE_ARCHIVE_PENDING,
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            db_path=geodata_db,
        )
        pqs.dual_write_enqueue(
            source_path='/x/done.mp4',
            stage=pqs.STAGE_ARCHIVE_DONE,
            legacy_table=pqs.LEGACY_TABLE_ARCHIVE,
            status='done',
            db_path=geodata_db,
        )
        self._seed_in_progress(
            geodata_db, '/x/stale.mp4', 'gone', claimed_at=1.0,
        )
        released = pqs.recover_stale_claims_pipeline(
            db_path=geodata_db, max_age_seconds=10.0, now=10000.0,
        )
        assert released == 1
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        c.row_factory = _sql.Row
        try:
            statuses = {
                r['source_path']: r['status']
                for r in c.execute(
                    "SELECT source_path, status FROM pipeline_queue"
                ).fetchall()
            }
            assert statuses['/x/pending.mp4'] == 'pending'
            assert statuses['/x/done.mp4'] == 'done'
            assert statuses['/x/stale.mp4'] == 'pending'  # was in_progress
        finally:
            c.close()

    def test_recovered_row_picked_by_next_claim(self, geodata_db):
        # End-to-end: seed an orphan, recover it, claim_next_for_stage
        # should return it (and bump attempts to 2).
        self._seed_in_progress(
            geodata_db, '/x/recycle.mp4', 'crashed', claimed_at=1.0,
        )
        released = pqs.recover_stale_claims_pipeline(
            db_path=geodata_db, max_age_seconds=10.0, now=10000.0,
        )
        assert released == 1
        claimed = pqs.claim_next_for_stage(
            stage='archive_pending', claimed_by='retry-worker',
            db_path=geodata_db, now=10001.0,
        )
        assert claimed is not None
        assert claimed['source_path'] == '/x/recycle.mp4'
        assert claimed['attempts'] == 2  # was 1, recovered, then bumped
        assert claimed['claimed_by'] == 'retry-worker'
        assert claimed['claimed_at'] == 10001.0

    def test_releases_multiple_stale_in_one_call(self, geodata_db):
        for i in range(5):
            self._seed_in_progress(
                geodata_db, f'/x/orphan-{i}.mp4', f'crashed-{i}',
                claimed_at=float(i),
            )
        released = pqs.recover_stale_claims_pipeline(
            db_path=geodata_db, max_age_seconds=1.0, now=10.0,
        )
        assert released == 5

    def test_preserves_next_retry_at(self, geodata_db):
        # A row with a backoff timer set must keep its next_retry_at
        # ? recovery resets the claim, not the failure-driven
        # backoff state.
        self._seed_in_progress(
            geodata_db, '/x/backoff.mp4', 'gone', claimed_at=1.0,
        )
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        try:
            c.execute(
                "UPDATE pipeline_queue SET next_retry_at = 99999.0 "
                "WHERE source_path = ?", ('/x/backoff.mp4',),
            )
            c.commit()
        finally:
            c.close()
        pqs.recover_stale_claims_pipeline(
            db_path=geodata_db, max_age_seconds=10.0, now=10000.0,
        )
        c = _sql.connect(geodata_db)
        c.row_factory = _sql.Row
        try:
            row = c.execute(
                "SELECT status, claimed_by, next_retry_at "
                "FROM pipeline_queue WHERE source_path = ?",
                ('/x/backoff.mp4',),
            ).fetchone()
            assert row['status'] == 'pending'
            assert row['claimed_by'] is None
            assert row['next_retry_at'] == 99999.0
        finally:
            c.close()

    def test_returns_zero_when_db_corrupt(self, tmp_path):
        bad = tmp_path / 'corrupt.db'
        bad.write_bytes(b'not a sqlite db at all\x00')
        assert pqs.recover_stale_claims_pipeline(
            db_path=str(bad),
        ) == 0


# ============================================================================
# Wave 4 PR-E ??? stale-recovery telemetry (#184 / closes #195)
# ============================================================================
# Counters live at module scope, reset on import. To keep tests
# isolated we manually reset them inside each test.


class TestRecoveryTelemetry:
    def _reset(self):
        with pqs._recover_stale_claims_lock:
            pqs._recover_stale_claims_total = 0
            pqs._recover_stale_claims_last_at = None
            pqs._recover_stale_claims_last_count = 0
            pqs._recover_stale_claims_call_count = 0

    def test_initial_telemetry_is_zeroed(self, geodata_db):
        self._reset()
        t = pqs.get_recovery_telemetry()
        assert t['stale_recoveries_total'] == 0
        assert t['stale_recoveries_last_at'] is None
        assert t['stale_recoveries_last_count'] == 0
        assert t['stale_recoveries_call_count'] == 0

    def test_zero_result_call_bumps_call_count_only(self, geodata_db):
        # Empty DB ??? recovery returns 0, telemetry's call_count
        # bumps but totals/last fields stay zero/None.
        self._reset()
        n = pqs.recover_stale_claims_pipeline(db_path=geodata_db)
        assert n == 0
        t = pqs.get_recovery_telemetry()
        assert t['stale_recoveries_total'] == 0
        assert t['stale_recoveries_last_at'] is None
        assert t['stale_recoveries_last_count'] == 0
        assert t['stale_recoveries_call_count'] == 1

    def test_non_zero_result_bumps_all_counters(self, geodata_db):
        self._reset()
        # Seed a stale claim then recover.
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        try:
            c.execute(
                """INSERT INTO pipeline_queue
                       (source_path, stage, status, priority,
                        enqueued_at, claimed_by, claimed_at, attempts,
                        legacy_table)
                   VALUES (?, 'archive_pending', 'in_progress', 2,
                           100.0, ?, ?, 1, 'archive_queue')""",
                ('/x/orphan-1.mp4', 'crashed', 1.0),
            )
            c.commit()
        finally:
            c.close()
        n = pqs.recover_stale_claims_pipeline(
            db_path=geodata_db, max_age_seconds=10.0, now=10000.0,
        )
        assert n == 1
        t = pqs.get_recovery_telemetry()
        assert t['stale_recoveries_total'] == 1
        assert t['stale_recoveries_last_at'] == 10000.0
        assert t['stale_recoveries_last_count'] == 1
        assert t['stale_recoveries_call_count'] == 1

    def test_total_accumulates_across_calls(self, geodata_db):
        self._reset()
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        try:
            for i in range(3):
                c.execute(
                    """INSERT INTO pipeline_queue
                           (source_path, stage, status, priority,
                            enqueued_at, claimed_by, claimed_at, attempts,
                            legacy_table)
                       VALUES (?, 'archive_pending', 'in_progress', 2,
                               100.0, ?, ?, 1, 'archive_queue')""",
                    (f'/x/orphan-{i}.mp4', f'crashed-{i}', float(i)),
                )
            c.commit()
        finally:
            c.close()
        # First call recovers 3 rows.
        n1 = pqs.recover_stale_claims_pipeline(
            db_path=geodata_db, max_age_seconds=1.0, now=100.0,
        )
        assert n1 == 3
        # Second call recovers 0 (already pending).
        n2 = pqs.recover_stale_claims_pipeline(
            db_path=geodata_db, max_age_seconds=1.0, now=200.0,
        )
        assert n2 == 0
        t = pqs.get_recovery_telemetry()
        assert t['stale_recoveries_total'] == 3
        # last_at + last_count from the FIRST (non-zero) call,
        # not overwritten by the zero-result second call.
        assert t['stale_recoveries_last_at'] == 100.0
        assert t['stale_recoveries_last_count'] == 3
        assert t['stale_recoveries_call_count'] == 2


class TestPipelineStatusMergesRecoveryTelemetry:
    def test_status_includes_telemetry_keys(self, geodata_db):
        with pqs._recover_stale_claims_lock:
            pqs._recover_stale_claims_total = 17
            pqs._recover_stale_claims_last_at = 9999.0
            pqs._recover_stale_claims_last_count = 5
            pqs._recover_stale_claims_call_count = 23
        try:
            status = pqs.pipeline_status(db_path=geodata_db)
            assert status.get('stale_recoveries_total') == 17
            assert status.get('stale_recoveries_last_at') == 9999.0
            assert status.get('stale_recoveries_last_count') == 5
            assert status.get('stale_recoveries_call_count') == 23
            # Existing keys still present.
            assert 'total' in status
            assert 'by_legacy_stage_status' in status
        finally:
            with pqs._recover_stale_claims_lock:
                pqs._recover_stale_claims_total = 0
                pqs._recover_stale_claims_last_at = None
                pqs._recover_stale_claims_last_count = 0
                pqs._recover_stale_claims_call_count = 0

    def test_status_missing_db_still_returns_telemetry(self, tmp_path):
        # Wave 4 PR-E review fix (info #3): pipeline_status MUST
        # surface telemetry even when the DB is missing — telemetry
        # is process-local and arguably MORE useful in that
        # diagnostic situation.
        with pqs._recover_stale_claims_lock:
            pqs._recover_stale_claims_total = 7
            pqs._recover_stale_claims_call_count = 12
        try:
            missing = str(tmp_path / 'does-not-exist.db')
            status = pqs.pipeline_status(db_path=missing)
            assert status.get('stale_recoveries_total') == 7
            assert status.get('stale_recoveries_call_count') == 12
            # Queue-state keys absent (no DB).
            assert 'total' not in status
            assert 'by_legacy_stage_status' not in status
        finally:
            with pqs._recover_stale_claims_lock:
                pqs._recover_stale_claims_total = 0
                pqs._recover_stale_claims_last_at = None
                pqs._recover_stale_claims_last_count = 0
                pqs._recover_stale_claims_call_count = 0

    def test_status_db_error_still_returns_telemetry(
        self, geodata_db, monkeypatch,
    ):
        # Wave 4 PR-E review fix (info #3): on sqlite3.Error the
        # function must still return the telemetry snapshot, not
        # an empty dict. Force an error by monkeypatching
        # _open_pipeline_conn to raise.
        with pqs._recover_stale_claims_lock:
            pqs._recover_stale_claims_total = 4
            pqs._recover_stale_claims_call_count = 9
        try:
            import sqlite3 as _sql

            def _raise(_p):
                raise _sql.OperationalError('forced error')

            monkeypatch.setattr(pqs, '_open_pipeline_conn', _raise)
            status = pqs.pipeline_status(db_path=geodata_db)
            assert status.get('stale_recoveries_total') == 4
            assert status.get('stale_recoveries_call_count') == 9
            assert 'total' not in status
        finally:
            with pqs._recover_stale_claims_lock:
                pqs._recover_stale_claims_total = 0
                pqs._recover_stale_claims_last_at = None
                pqs._recover_stale_claims_last_count = 0
                pqs._recover_stale_claims_call_count = 0


class TestRecoveryCallCountOnEarlyReturn:
    """Wave 4 PR-E review fix (info #4): call_count must bump on
    every recovery call, including missing-DB and sqlite-error
    early-return paths, so the docstring's
    "high call_count + zero total = called but found nothing"
    diagnostic is reliable when the DB itself is misconfigured."""

    def _reset(self):
        with pqs._recover_stale_claims_lock:
            pqs._recover_stale_claims_total = 0
            pqs._recover_stale_claims_last_at = None
            pqs._recover_stale_claims_last_count = 0
            pqs._recover_stale_claims_call_count = 0

    def test_missing_db_still_bumps_call_count(self, tmp_path):
        self._reset()
        missing = str(tmp_path / 'does-not-exist.db')
        n = pqs.recover_stale_claims_pipeline(db_path=missing)
        assert n == 0
        t = pqs.get_recovery_telemetry()
        assert t['stale_recoveries_call_count'] == 1
        assert t['stale_recoveries_total'] == 0
        assert t['stale_recoveries_last_at'] is None

    def test_sqlite_error_still_bumps_call_count(
        self, geodata_db, monkeypatch,
    ):
        self._reset()
        import sqlite3 as _sql

        def _raise(_p):
            raise _sql.OperationalError('forced error')

        monkeypatch.setattr(pqs, '_open_pipeline_conn', _raise)
        n = pqs.recover_stale_claims_pipeline(db_path=geodata_db)
        assert n == 0
        t = pqs.get_recovery_telemetry()
        assert t['stale_recoveries_call_count'] == 1
        assert t['stale_recoveries_total'] == 0


class TestPeekTopNPathsForStage:
    """Wave 4 PR-E: top-N candidate peek used by the archive worker's
    shadow-mode comparison to absorb the documented secondary-sort
    divergence between archive_queue (expected_mtime) and
    pipeline_queue (enqueued_at)."""

    def _seed(self, db, paths_with_enqueued):
        import sqlite3 as _sql
        c = _sql.connect(db)
        try:
            for path, enq in paths_with_enqueued:
                c.execute(
                    """INSERT INTO pipeline_queue
                           (source_path, stage, status, priority,
                            enqueued_at, attempts, legacy_table)
                       VALUES (?, 'archive_pending', 'pending', 2,
                               ?, 0, 'archive_queue')""",
                    (path, enq),
                )
            c.commit()
        finally:
            c.close()

    def test_returns_tuple_in_pick_order(self, geodata_db):
        self._seed(
            geodata_db,
            [('/x/c.mp4', 30.0), ('/x/a.mp4', 10.0),
             ('/x/b.mp4', 20.0)],
        )
        result = pqs.peek_top_n_paths_for_stage(
            stage='archive_pending', limit=10, db_path=geodata_db,
        )
        assert isinstance(result, tuple)
        assert result == ('/x/a.mp4', '/x/b.mp4', '/x/c.mp4')

    def test_respects_limit(self, geodata_db):
        self._seed(
            geodata_db,
            [(f'/x/{i:02d}.mp4', float(i)) for i in range(20)],
        )
        result = pqs.peek_top_n_paths_for_stage(
            stage='archive_pending', limit=3, db_path=geodata_db,
        )
        assert len(result) == 3
        assert result == ('/x/00.mp4', '/x/01.mp4', '/x/02.mp4')

    def test_clamps_limit_to_50(self, geodata_db):
        self._seed(
            geodata_db,
            [(f'/x/{i:03d}.mp4', float(i)) for i in range(60)],
        )
        result = pqs.peek_top_n_paths_for_stage(
            stage='archive_pending', limit=999, db_path=geodata_db,
        )
        assert len(result) == 50

    def test_clamps_limit_floor_to_1(self, geodata_db):
        self._seed(geodata_db, [('/x/a.mp4', 1.0)])
        result = pqs.peek_top_n_paths_for_stage(
            stage='archive_pending', limit=0, db_path=geodata_db,
        )
        # 0 clamps to 1 ÔÇö returns the single row.
        assert result == ('/x/a.mp4',)

    def test_filters_by_stage(self, geodata_db):
        self._seed(geodata_db, [('/x/a.mp4', 1.0)])
        # Different stage ÔÇö no match.
        result = pqs.peek_top_n_paths_for_stage(
            stage='cloud_upload_pending', limit=10,
            db_path=geodata_db,
        )
        assert result == ()

    def test_skips_in_progress_rows(self, geodata_db):
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        try:
            c.execute(
                """INSERT INTO pipeline_queue
                       (source_path, stage, status, priority,
                        enqueued_at, attempts, legacy_table,
                        claimed_by, claimed_at)
                   VALUES ('/x/a.mp4', 'archive_pending',
                           'in_progress', 2, 1.0, 1,
                           'archive_queue', 'w-1', 100.0)""",
            )
            c.execute(
                """INSERT INTO pipeline_queue
                       (source_path, stage, status, priority,
                        enqueued_at, attempts, legacy_table)
                   VALUES ('/x/b.mp4', 'archive_pending',
                           'pending', 2, 2.0, 0, 'archive_queue')""",
            )
            c.commit()
        finally:
            c.close()
        result = pqs.peek_top_n_paths_for_stage(
            stage='archive_pending', limit=10, db_path=geodata_db,
        )
        assert result == ('/x/b.mp4',)

    def test_respects_next_retry_at_gate(self, geodata_db):
        import sqlite3 as _sql
        c = _sql.connect(geodata_db)
        try:
            c.execute(
                """INSERT INTO pipeline_queue
                       (source_path, stage, status, priority,
                        enqueued_at, attempts, legacy_table,
                        next_retry_at)
                   VALUES ('/x/future.mp4', 'archive_pending',
                           'pending', 2, 1.0, 0, 'archive_queue',
                           99999.0)""",
            )
            c.execute(
                """INSERT INTO pipeline_queue
                       (source_path, stage, status, priority,
                        enqueued_at, attempts, legacy_table)
                   VALUES ('/x/now.mp4', 'archive_pending',
                           'pending', 2, 2.0, 0, 'archive_queue')""",
            )
            c.commit()
        finally:
            c.close()
        result = pqs.peek_top_n_paths_for_stage(
            stage='archive_pending', limit=10, now=1000.0,
            db_path=geodata_db,
        )
        assert result == ('/x/now.mp4',)

    def test_empty_stage_returns_empty(self, geodata_db):
        result = pqs.peek_top_n_paths_for_stage(
            stage='', limit=10, db_path=geodata_db,
        )
        assert result == ()

    def test_missing_db_returns_empty(self, tmp_path):
        result = pqs.peek_top_n_paths_for_stage(
            stage='archive_pending', limit=10,
            db_path=str(tmp_path / 'nope.db'),
        )
        assert result == ()

    def test_invalid_limit_returns_empty(self, geodata_db):
        result = pqs.peek_top_n_paths_for_stage(
            stage='archive_pending', limit='not-a-number',
            db_path=geodata_db,
        )
        assert result == ()
