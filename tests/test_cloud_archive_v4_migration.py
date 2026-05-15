"""Tests for issue #202 — cloud_sync.db v3 → v4 migration.

The v4 migration drops the orphaned ``live_event_queue`` table left
behind when Wave 4 PR-F4 deleted the standalone Live Event Sync
subsystem (issue #184 / PR #201). The defensive cross-DB sanity
check + remediation path mirrors any unmirrored LES rows into
``pipeline_queue`` BEFORE the DROP so no live-event upload work is
lost.

Test coverage:

  1. Fresh DB without ``live_event_queue`` → no-op, version bumps to 4.
  2. DB with empty ``live_event_queue`` → DROP succeeds, version bumps
     to 4, table absent.
  3. DB with mirrored rows in pipeline_queue → DROP succeeds, no
     spurious warnings.
  4. DB with **unmirrored** rows → backfill warning logged with row
     IDs, then DROP, version bumps to 4.
  5. Idempotency — re-running on a v4 DB is a no-op.
  6. Failure during DROP → ``migration_ok = False``, version stays at
     3, retries on next call.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest

# Allow importing the web modules without spinning up Flask.
SCRIPTS_WEB = Path(__file__).resolve().parent.parent / 'scripts' / 'web'
if str(SCRIPTS_WEB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_WEB))

from services import cloud_archive_service as cas  # noqa: E402
from services import pipeline_queue_service as pqs  # noqa: E402
from services.mapping_migrations import _init_db  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

LES_TABLE_DDL = """
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
CREATE INDEX idx_les_status ON live_event_queue(status);
CREATE INDEX idx_les_next_retry ON live_event_queue(next_retry_at);
"""


def _make_v3_cloud_db(db_path: str, *, with_les: bool = True) -> None:
    """Build a cloud_sync.db at schema v3 — i.e. the state immediately
    after PR-F4 / PR #201 deployed but before the v4 migration runs.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE module_versions (
                module TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at TEXT
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
            CREATE TABLE cloud_sync_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                files_synced INTEGER DEFAULT 0,
                bytes_transferred INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running',
                trigger TEXT,
                window_mode TEXT,
                error_msg TEXT
            );
            INSERT INTO module_versions (module, version, updated_at)
                VALUES ('cloud_archive', 3, '2026-05-13T09:54:21+00:00');
            """
        )
        if with_les:
            conn.executescript(LES_TABLE_DDL)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def cloud_db_v3_with_les(tmp_path):
    db = str(tmp_path / 'cloud_sync.db')
    _make_v3_cloud_db(db, with_les=True)
    return db


@pytest.fixture
def cloud_db_v3_without_les(tmp_path):
    db = str(tmp_path / 'cloud_sync.db')
    _make_v3_cloud_db(db, with_les=False)
    return db


@pytest.fixture
def geodata_db(tmp_path):
    """A fresh ``geodata.db`` at the current schema (v17)."""
    db_path = str(tmp_path / 'geodata.db')
    conn = _init_db(db_path)
    conn.close()
    return db_path


@pytest.fixture(autouse=True)
def isolate_pipeline_db(monkeypatch, geodata_db):
    """Force ``pipeline_queue_service.resolve_pipeline_db`` to return
    the test geodata.db so the cross-DB reconciliation path uses the
    isolated test DB, not whatever the host has configured. Patches
    both the public ``resolve_pipeline_db`` and the legacy private
    ``_resolve_pipeline_db`` alias so any caller (current or legacy)
    is rerouted.
    """
    monkeypatch.setattr(pqs, 'resolve_pipeline_db', lambda: geodata_db)
    monkeypatch.setattr(pqs, '_resolve_pipeline_db', lambda: geodata_db)
    return geodata_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _table_exists(db_path: str, table: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _index_exists(db_path: str, index: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            (index,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _module_version(db_path: str, module: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT version FROM module_versions WHERE module=?",
            (module,),
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _seed_les_rows(db_path: str, rows):
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO live_event_queue "
            "(event_dir, event_json_path, event_timestamp, "
            " event_reason, upload_scope, status, enqueued_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _seed_pipeline_mirror(db_path: str, legacy_id: int,
                          source_path: str,
                          stage: str = 'cloud_pending') -> None:
    """Insert a pipeline_queue row that mirrors a live_event_queue row."""
    pqs.dual_write_enqueue(
        source_path=source_path,
        stage=stage,
        legacy_table='live_event_queue',
        legacy_id=legacy_id,
        priority=pqs.PRIORITY_LIVE_EVENT,
        payload={'mirror': True},
        status='pending',
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# Direct migration helper tests
# ---------------------------------------------------------------------------

class TestDropMigrationDirect:
    """Drive ``_migrate_drop_live_event_queue_v4`` directly so each
    branch is asserted in isolation."""

    def test_no_op_when_table_absent(self, cloud_db_v3_without_les):
        conn = sqlite3.connect(cloud_db_v3_without_les)
        try:
            cas._migrate_drop_live_event_queue_v4(
                conn, cloud_db_v3_without_les,
            )
            conn.commit()
        finally:
            conn.close()
        # Table never existed; nothing to assert except that the call
        # didn't raise. cloud_synced_files must still be intact.
        assert _table_exists(cloud_db_v3_without_les, 'cloud_synced_files')
        assert not _table_exists(cloud_db_v3_without_les, 'live_event_queue')

    def test_drops_empty_table(self, cloud_db_v3_with_les):
        assert _table_exists(cloud_db_v3_with_les, 'live_event_queue')
        assert _index_exists(cloud_db_v3_with_les, 'idx_les_status')
        assert _index_exists(cloud_db_v3_with_les, 'idx_les_next_retry')

        conn = sqlite3.connect(cloud_db_v3_with_les)
        try:
            cas._migrate_drop_live_event_queue_v4(
                conn, cloud_db_v3_with_les,
            )
            conn.commit()
        finally:
            conn.close()

        assert not _table_exists(cloud_db_v3_with_les, 'live_event_queue')
        assert not _index_exists(cloud_db_v3_with_les, 'idx_les_status')
        assert not _index_exists(cloud_db_v3_with_les, 'idx_les_next_retry')
        # cloud_synced_files must still be intact.
        assert _table_exists(cloud_db_v3_with_les, 'cloud_synced_files')

    def test_drop_succeeds_when_all_rows_already_mirrored(
        self, cloud_db_v3_with_les, geodata_db, caplog,
    ):
        _seed_les_rows(cloud_db_v3_with_les, [
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
        ])
        # Seed mirrors for both rows.
        _seed_pipeline_mirror(geodata_db, 1,
                              '/Sentry/2026-05-14_10-00-00/event.json')
        _seed_pipeline_mirror(geodata_db, 2,
                              '/Sentry/2026-05-14_11-00-00/event.json')

        with caplog.at_level(logging.INFO):
            conn = sqlite3.connect(cloud_db_v3_with_les)
            try:
                cas._migrate_drop_live_event_queue_v4(
                    conn, cloud_db_v3_with_les,
                )
                conn.commit()
            finally:
                conn.close()

        assert not _table_exists(cloud_db_v3_with_les, 'live_event_queue')
        # The "all mirrored" path logs INFO, not WARNING.
        assert any('already mirrored' in m for m in caplog.messages)

    def test_backfills_unmirrored_rows_then_drops(
        self, cloud_db_v3_with_les, geodata_db, caplog,
    ):
        _seed_les_rows(cloud_db_v3_with_les, [
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
        ])
        # Only mirror row 1; row 2 is unmirrored.
        _seed_pipeline_mirror(geodata_db, 1,
                              '/Sentry/2026-05-14_10-00-00/event.json')

        with caplog.at_level(logging.WARNING):
            conn = sqlite3.connect(cloud_db_v3_with_les)
            try:
                cas._migrate_drop_live_event_queue_v4(
                    conn, cloud_db_v3_with_les,
                )
                conn.commit()
            finally:
                conn.close()

        # Table is gone.
        assert not _table_exists(cloud_db_v3_with_les, 'live_event_queue')
        # WARNING was logged listing the unmirrored ID.
        warn_msgs = [
            r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING
        ]
        assert any('unmirrored' in m for m in warn_msgs), warn_msgs
        assert any('[2]' in m for m in warn_msgs), warn_msgs

        # Row 2 was backfilled into pipeline_queue at PRIORITY_LIVE_EVENT
        # with stage='cloud_done' (legacy status='uploaded').
        gconn = sqlite3.connect(geodata_db)
        try:
            gconn.row_factory = sqlite3.Row
            rows = gconn.execute(
                "SELECT stage, status, priority, legacy_id "
                "FROM pipeline_queue "
                "WHERE legacy_table='live_event_queue' "
                "ORDER BY legacy_id"
            ).fetchall()
        finally:
            gconn.close()
        assert len(rows) == 2
        # Row 2 (uploaded → cloud_done/done at PRIORITY_LIVE_EVENT).
        assert rows[1]['legacy_id'] == 2
        assert rows[1]['stage'] == pqs.STAGE_CLOUD_DONE
        assert rows[1]['status'] == 'done'
        assert rows[1]['priority'] == pqs.PRIORITY_LIVE_EVENT

    def test_unavailable_pipeline_db_aborts_drop(
        self, cloud_db_v3_with_les, monkeypatch,
    ):
        """If the pipeline DB cannot be located, the DROP must abort
        so we don't silently lose unmirrored rows."""
        _seed_les_rows(cloud_db_v3_with_les, [
            ('/Sentry/2026-05-14_10-00-00',
             '/Sentry/2026-05-14_10-00-00/event.json',
             '2026-05-14T10:00:00', 'sentry_aware_object_detection',
             'event_minute', 'pending',
             '2026-05-14T10:00:00'),
        ])
        # Force pipeline DB to a non-existent path. Patches both names
        # to defeat the autouse fixture (which patches them too).
        monkeypatch.setattr(pqs, 'resolve_pipeline_db',
                            lambda: '/nonexistent/path/geodata.db')
        monkeypatch.setattr(pqs, '_resolve_pipeline_db',
                            lambda: '/nonexistent/path/geodata.db')

        conn = sqlite3.connect(cloud_db_v3_with_les)
        try:
            with pytest.raises(RuntimeError, match='pipeline_queue DB'):
                cas._migrate_drop_live_event_queue_v4(
                    conn, cloud_db_v3_with_les,
                )
            conn.rollback()
        finally:
            conn.close()

        # Table must still exist — DROP did not run.
        assert _table_exists(cloud_db_v3_with_les, 'live_event_queue')

    def test_backfill_maps_all_four_legacy_statuses(
        self, cloud_db_v3_with_les, geodata_db,
    ):
        """Review-pr finding #8: exercise every entry in ``status_map``
        (`pending` → `pending`, `uploading` → `in_progress`,
        `uploaded` → `done`, `failed` → `failed`). Without coverage,
        a future refactor could silently change the mapping for the
        non-canonical statuses (`uploading` and `failed` were missed
        in the initial PR)."""
        _seed_les_rows(cloud_db_v3_with_les, [
            ('/Sentry/event_a', '/Sentry/event_a/event.json',
             '2026-05-14T10:00:00', 'reason_a', 'event_minute',
             'pending', '2026-05-14T10:00:00'),
            ('/Sentry/event_b', '/Sentry/event_b/event.json',
             '2026-05-14T11:00:00', 'reason_b', 'event_minute',
             'uploading', '2026-05-14T11:00:00'),
            ('/Sentry/event_c', '/Sentry/event_c/event.json',
             '2026-05-14T12:00:00', 'reason_c', 'event_minute',
             'uploaded', '2026-05-14T12:00:00'),
            ('/Sentry/event_d', '/Sentry/event_d/event.json',
             '2026-05-14T13:00:00', 'reason_d', 'event_minute',
             'failed', '2026-05-14T13:00:00'),
        ])

        conn = sqlite3.connect(cloud_db_v3_with_les)
        try:
            cas._migrate_drop_live_event_queue_v4(
                conn, cloud_db_v3_with_les,
            )
            conn.commit()
        finally:
            conn.close()

        gconn = sqlite3.connect(geodata_db)
        try:
            gconn.row_factory = sqlite3.Row
            rows = {
                r['legacy_id']: r for r in gconn.execute(
                    "SELECT legacy_id, stage, status "
                    "FROM pipeline_queue "
                    "WHERE legacy_table='live_event_queue'"
                ).fetchall()
            }
        finally:
            gconn.close()

        assert len(rows) == 4
        assert rows[1]['stage'] == pqs.STAGE_CLOUD_PENDING
        assert rows[1]['status'] == 'pending'
        assert rows[2]['stage'] == pqs.STAGE_CLOUD_PENDING
        assert rows[2]['status'] == 'in_progress'
        assert rows[3]['stage'] == pqs.STAGE_CLOUD_DONE
        assert rows[3]['status'] == 'done'
        assert rows[4]['stage'] == pqs.STAGE_CLOUD_PENDING
        assert rows[4]['status'] == 'failed'

    def test_unknown_legacy_status_defaults_to_failed(
        self, cloud_db_v3_with_les, geodata_db,
    ):
        """Review-pr finding #4: an unrecognized legacy status MUST
        default to ``'failed'`` (conservative). The deleted
        ``_backfill_live_event_queue`` had this same behavior — a
        ``'pending'`` default could let the unified worker re-upload a
        row whose true state we don't know."""
        _seed_les_rows(cloud_db_v3_with_les, [
            ('/Sentry/weird', '/Sentry/weird/event.json',
             '2026-05-14T10:00:00', 'r', 'event_minute',
             'unrecognized_status', '2026-05-14T10:00:00'),
        ])

        conn = sqlite3.connect(cloud_db_v3_with_les)
        try:
            cas._migrate_drop_live_event_queue_v4(
                conn, cloud_db_v3_with_les,
            )
            conn.commit()
        finally:
            conn.close()

        gconn = sqlite3.connect(geodata_db)
        try:
            gconn.row_factory = sqlite3.Row
            row = gconn.execute(
                "SELECT status FROM pipeline_queue "
                "WHERE legacy_table='live_event_queue'"
            ).fetchone()
        finally:
            gconn.close()

        assert row is not None
        assert row['status'] == 'failed'

    def test_dual_write_failure_aborts_drop(
        self, cloud_db_v3_with_les, geodata_db, monkeypatch, caplog,
    ):
        """Review-pr finding #9: if ``dual_write_enqueue`` returns
        ``False`` (which can mean either UNIQUE-conflict idempotency
        OR a swallowed ``sqlite3.Error``), the post-loop verification
        must distinguish them. A genuine DB-error case leaves the row
        unmirrored, so the migration must abort and preserve the
        live_event_queue rows for retry."""
        _seed_les_rows(cloud_db_v3_with_les, [
            ('/Sentry/never_mirrored', '/Sentry/never_mirrored/event.json',
             '2026-05-14T10:00:00', 'r', 'event_minute',
             'pending', '2026-05-14T10:00:00'),
        ])

        # Patch dual_write_enqueue to return False without writing —
        # simulating the sqlite3.Error → return False branch in pqs.
        monkeypatch.setattr(
            pqs, 'dual_write_enqueue', lambda **kwargs: False,
        )

        conn = sqlite3.connect(cloud_db_v3_with_les)
        try:
            with caplog.at_level(logging.ERROR):
                with pytest.raises(RuntimeError, match='backfill incomplete'):
                    cas._migrate_drop_live_event_queue_v4(
                        conn, cloud_db_v3_with_les,
                    )
            conn.rollback()
        finally:
            conn.close()

        # DROP did NOT run — the row is preserved for the next attempt.
        assert _table_exists(cloud_db_v3_with_les, 'live_event_queue')
        # ERROR log was emitted listing the abandoned IDs.
        err_msgs = [
            r.getMessage() for r in caplog.records
            if r.levelno >= logging.ERROR
        ]
        assert any('backfill failed' in m for m in err_msgs), err_msgs

    def test_stale_mirror_at_pending_is_refreshed_to_done(
        self, cloud_db_v3_with_les, geodata_db,
    ):
        """Review-pr finding #5: if a non-canonical install mirrored a
        row at LES ``status='pending'`` time and LES later finished
        the upload (legacy ``status='uploaded'``), the mirror is stale.
        The migration must UPDATE the mirror to ``cloud_done`` so the
        worker doesn't re-upload completed work."""
        _seed_les_rows(cloud_db_v3_with_les, [
            ('/Sentry/stale', '/Sentry/stale/event.json',
             '2026-05-14T10:00:00', 'r', 'event_minute',
             'uploaded',  # legacy says: done
             '2026-05-14T10:00:00'),
        ])
        # Mirror exists but at the OLDER stage='cloud_pending' — i.e.
        # the worker would re-upload this row on its next claim.
        _seed_pipeline_mirror(geodata_db, 1,
                              '/Sentry/stale/event.json',
                              stage='cloud_pending')

        conn = sqlite3.connect(cloud_db_v3_with_les)
        try:
            cas._migrate_drop_live_event_queue_v4(
                conn, cloud_db_v3_with_les,
            )
            conn.commit()
        finally:
            conn.close()

        # Table dropped (no unmirrored rows).
        assert not _table_exists(cloud_db_v3_with_les, 'live_event_queue')

        # Mirror was UPDATED to cloud_done/done.
        gconn = sqlite3.connect(geodata_db)
        try:
            gconn.row_factory = sqlite3.Row
            row = gconn.execute(
                "SELECT stage, status FROM pipeline_queue "
                "WHERE legacy_table='live_event_queue' "
                "AND legacy_id=1"
            ).fetchone()
        finally:
            gconn.close()
        assert row is not None
        assert row['stage'] == pqs.STAGE_CLOUD_DONE
        assert row['status'] == 'done'

    def test_chunked_existence_query_handles_large_input(
        self, cloud_db_v3_with_les, geodata_db,
    ):
        """Review-pr finding #2: ``WHERE legacy_id IN (?,?,...)`` is
        chunked at ``_LIVE_EVENT_BACKFILL_CHUNK`` to stay under
        SQLite's variable-count ceiling (999 on pre-3.32 builds).
        Seed ~1.5x the chunk size to exercise multi-chunk traversal
        in :func:`_query_existing_mirrors`."""
        chunk_sz = cas._LIVE_EVENT_BACKFILL_CHUNK
        n = chunk_sz + chunk_sz // 2  # 750 by default

        rows = [
            (f'/dir/{i}', f'/dir/{i}/event.json',
             '2026-05-14T10:00:00', 'r', 'event_minute',
             'pending', '2026-05-14T10:00:00')
            for i in range(n)
        ]
        _seed_les_rows(cloud_db_v3_with_les, rows)

        conn = sqlite3.connect(cloud_db_v3_with_les)
        try:
            cas._migrate_drop_live_event_queue_v4(
                conn, cloud_db_v3_with_les,
            )
            conn.commit()
        finally:
            conn.close()

        # All rows backfilled.
        gconn = sqlite3.connect(geodata_db)
        try:
            cnt = gconn.execute(
                "SELECT COUNT(*) FROM pipeline_queue "
                "WHERE legacy_table='live_event_queue'"
            ).fetchone()[0]
        finally:
            gconn.close()
        assert cnt == n
        assert not _table_exists(cloud_db_v3_with_les, 'live_event_queue')

    def test_legacy_function_alias_still_works(
        self, cloud_db_v3_with_les, geodata_db,
    ):
        """Review-pr finding #5 (continued): the function was renamed
        from ``_reconcile_live_event_queue_into_pipeline`` to
        ``_backfill_missing_live_event_mirrors``. The old name is
        kept as a backward-compat alias so any external test or
        helper that imported the old symbol keeps working."""
        assert (
            cas._reconcile_live_event_queue_into_pipeline
            is cas._backfill_missing_live_event_mirrors
        )


# ---------------------------------------------------------------------------
# End-to-end migration via _init_cloud_tables
# ---------------------------------------------------------------------------

class TestInitCloudDbV4:
    """Drive the full ``_init_cloud_tables`` migration pipeline so the
    version-bump gating, ``migration_ok`` flag, and idempotency are
    all asserted together."""

    def test_v3_to_v4_bumps_version_and_drops_table(
        self, cloud_db_v3_with_les,
    ):
        conn = cas._init_cloud_tables(cloud_db_v3_with_les)
        conn.close()

        assert _module_version(cloud_db_v3_with_les, 'cloud_archive') == 4
        assert not _table_exists(cloud_db_v3_with_les, 'live_event_queue')

    def test_idempotent_on_v4(self, cloud_db_v3_with_les):
        # First call lifts to v4.
        conn = cas._init_cloud_tables(cloud_db_v3_with_les)
        conn.close()
        assert _module_version(cloud_db_v3_with_les, 'cloud_archive') == 4

        # Second call must be a no-op (the ``if current < ...`` guard
        # short-circuits the migration block entirely).
        conn = cas._init_cloud_tables(cloud_db_v3_with_les)
        conn.close()
        assert _module_version(cloud_db_v3_with_les, 'cloud_archive') == 4
        assert not _table_exists(cloud_db_v3_with_les, 'live_event_queue')

    def test_failure_holds_version_at_3_for_retry(
        self, cloud_db_v3_with_les, monkeypatch,
    ):
        """If the v4 migration helper raises, the version bump must NOT
        run so the migration retries on the next service start."""
        def boom(conn, db_path):
            raise RuntimeError('simulated migration failure')
        monkeypatch.setattr(
            cas, '_migrate_drop_live_event_queue_v4', boom,
        )

        conn = cas._init_cloud_tables(cloud_db_v3_with_les)
        conn.close()

        # Version must remain at 3; table must still exist (rollback
        # restored the pre-DROP state).
        assert _module_version(cloud_db_v3_with_les, 'cloud_archive') == 3
        assert _table_exists(cloud_db_v3_with_les, 'live_event_queue')


# ---------------------------------------------------------------------------
# Schema constant
# ---------------------------------------------------------------------------

class TestSchemaVersionConstant:
    def test_cloud_schema_version_is_v4_or_later(self):
        assert cas._CLOUD_SCHEMA_VERSION >= 4

    def test_legacy_table_live_event_is_removed(self):
        assert not hasattr(pqs, 'LEGACY_TABLE_LIVE_EVENT')

    def test_stage_live_event_constants_are_removed(self):
        assert not hasattr(pqs, 'STAGE_LIVE_EVENT_PENDING')
        assert not hasattr(pqs, 'STAGE_LIVE_EVENT_DONE')

    def test_backfill_live_event_helper_is_removed(self):
        assert not hasattr(pqs, '_backfill_live_event_queue')
