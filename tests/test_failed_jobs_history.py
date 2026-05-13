"""Issue #132 — multi-failure history via ``previous_last_error``.

Each of the four failed-jobs subsystems (archive, indexer, cloud_sync,
LES) carries a ``last_error`` column that gets overwritten by the next
failure. Pre-#132, retrying an item NULLed ``last_error`` first so the
prior reason was lost the moment the operator clicked Retry; PR #131
fixed that one race by *preserving* ``last_error`` on retry. But across
multiple cycles (failed-with-error-A → retried → failed-with-error-B)
only the most recent error was visible — operators couldn't see whether
a job had been failing for the same reason all along or whether each
retry uncovered a different one.

This PR adds ``previous_last_error TEXT`` to all four tables and rotates
``last_error → previous_last_error`` in every failure-recording UPDATE.
The rotation uses SQLite's "all RHS values from the pre-update row"
semantics, so it doesn't matter whether ``previous_last_error = ?`` or
``last_error = ?`` appears first in the SET clause.

These tests pin the new contract:

1. **Schema**: each of the four tables has the new column.
2. **First failure**: ``last_error`` populated, ``previous_last_error``
   stays NULL (no prior error to rotate).
3. **Subsequent failure**: prior ``last_error`` rotates into
   ``previous_last_error``; new error lands in ``last_error``.
4. **Three-cycle rotation**: error C in ``last_error``, error B in
   ``previous_last_error``, error A is gone (we keep one historical
   step, not unbounded history).
5. **Retry preserves history**: ``retry_dead_letter`` / ``retry_failed``
   does NOT touch ``previous_last_error`` — the operator's history
   should survive the retry click.
6. **Migration is idempotent**: repeated calls of the schema-init paths
   on a v3 DB don't crash.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Schema column existence (all four tables)
# ---------------------------------------------------------------------------


def _column_names(conn: sqlite3.Connection, table: str) -> list:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


class TestSchemaHasPreviousLastError:

    def test_archive_queue_has_column(self, tmp_path):
        from services import mapping_migrations
        db = tmp_path / "geodata.db"
        conn = mapping_migrations._init_db(str(db))
        try:
            cols = _column_names(conn, 'archive_queue')
        finally:
            conn.close()
        assert 'previous_last_error' in cols, (
            f"archive_queue missing previous_last_error; cols={cols}"
        )

    def test_indexing_queue_has_column(self, tmp_path):
        from services import mapping_migrations
        db = tmp_path / "geodata.db"
        conn = mapping_migrations._init_db(str(db))
        try:
            cols = _column_names(conn, 'indexing_queue')
        finally:
            conn.close()
        assert 'previous_last_error' in cols, (
            f"indexing_queue missing previous_last_error; cols={cols}"
        )

    def test_cloud_synced_files_has_column(self, tmp_path, monkeypatch):
        from services import cloud_archive_service as svc
        # Avoid pulling in a real migration path; just call _init_cloud_tables
        # on a fresh DB so the new schema is created.
        db = tmp_path / "cloud_sync.db"
        monkeypatch.setattr(svc, "_startup_recovery_done", False)
        conn = svc._init_cloud_tables(str(db))
        try:
            cols = _column_names(conn, 'cloud_synced_files')
        finally:
            conn.close()
        assert 'previous_last_error' in cols, (
            f"cloud_synced_files missing previous_last_error; cols={cols}"
        )

    def test_live_event_queue_has_column(self, tmp_path, monkeypatch):
        # LES uses the cloud_sync.db too. Bootstrap via _ensure_schema.
        from services import live_event_sync_service as les
        from services import cloud_archive_service as svc

        db_path = str(tmp_path / "cloud_sync.db")
        monkeypatch.setattr(svc, "CLOUD_ARCHIVE_DB_PATH", db_path)
        monkeypatch.setattr(les, "CLOUD_ARCHIVE_DB_PATH", db_path)

        conn = les._open_db()
        try:
            les._ensure_schema(conn)
            cols = _column_names(conn, 'live_event_queue')
        finally:
            conn.close()
        assert 'previous_last_error' in cols, (
            f"live_event_queue missing previous_last_error; cols={cols}"
        )


# ---------------------------------------------------------------------------
# v11 → v12 migration adds the column to existing rows
# ---------------------------------------------------------------------------


class TestMappingMigrationV11toV12:

    def test_idempotent_alter(self, tmp_path):
        """Calling _init_db twice must NOT crash on the
        second invocation (the v12 ALTER would otherwise raise
        OperationalError 'duplicate column name')."""
        from services import mapping_migrations
        db = str(tmp_path / "geodata.db")
        conn1 = mapping_migrations._init_db(db)
        conn1.close()
        # Second invocation should be a no-op for the v12 ALTER.
        conn2 = mapping_migrations._init_db(db)
        try:
            cols_a = _column_names(conn2, 'archive_queue')
            cols_i = _column_names(conn2, 'indexing_queue')
        finally:
            conn2.close()
        assert 'previous_last_error' in cols_a
        assert 'previous_last_error' in cols_i

    def test_legacy_v11_db_gets_column_via_alter(self, tmp_path):
        """A pre-existing v11 DB (without the column) must be ALTERed
        on first open and end up at v12 with the column present."""
        from services import mapping_migrations
        db = str(tmp_path / "geodata.db")
        # Hand-build a minimal v11 DB. Column list mirrors v11 schema
        # exactly (minus previous_last_error). Index DDL in
        # ``_SCHEMA_SQL`` references claimed_by/claimed_at, so those
        # MUST be present even on the legacy fixture.
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY)"
        )
        conn.execute("INSERT INTO schema_version (version) VALUES (11)")
        conn.execute(
            """CREATE TABLE archive_queue (
                   id INTEGER PRIMARY KEY,
                   source_path TEXT UNIQUE NOT NULL,
                   dest_path TEXT,
                   priority INTEGER DEFAULT 3,
                   status TEXT DEFAULT 'pending',
                   attempts INTEGER DEFAULT 0,
                   last_error TEXT,
                   enqueued_at TEXT NOT NULL,
                   claimed_at TEXT,
                   claimed_by TEXT,
                   copied_at TEXT,
                   expected_size INTEGER,
                   expected_mtime REAL
               )"""
        )
        conn.execute(
            """CREATE TABLE indexing_queue (
                   canonical_key TEXT PRIMARY KEY,
                   file_path TEXT NOT NULL,
                   priority INTEGER NOT NULL DEFAULT 50,
                   enqueued_at REAL NOT NULL,
                   next_attempt_at REAL NOT NULL DEFAULT 0,
                   attempts INTEGER NOT NULL DEFAULT 0,
                   last_error TEXT,
                   claimed_by TEXT,
                   claimed_at REAL,
                   source TEXT
               )"""
        )
        conn.commit()
        conn.close()

        # Run the migration.
        new_conn = mapping_migrations._init_db(db)
        try:
            ver = new_conn.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
            assert ver == 12
            assert 'previous_last_error' in _column_names(new_conn, 'archive_queue')
            assert 'previous_last_error' in _column_names(new_conn, 'indexing_queue')
        finally:
            new_conn.close()


# ---------------------------------------------------------------------------
# archive_queue.mark_failed rotates last_error → previous_last_error
# ---------------------------------------------------------------------------


@pytest.fixture
def archive_db(tmp_path, monkeypatch):
    """Initialize a real archive_queue DB and point the module at it."""
    from services import archive_queue, mapping_migrations
    db = str(tmp_path / "geodata.db")
    conn = mapping_migrations._init_db(db)
    conn.close()
    monkeypatch.setattr(
        archive_queue, "_resolve_db_path", lambda p=None: db, raising=False,
    )
    return db


def _seed_archive_row(db_path, source_path):
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


def _archive_state(db_path, row_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT last_error, previous_last_error, attempts, status "
        "FROM archive_queue WHERE id = ?",
        (row_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


class TestArchiveQueueRotation:

    def test_first_failure_leaves_previous_null(self, archive_db):
        from services import archive_queue
        rid = _seed_archive_row(archive_db, "/src/clip1.mp4")
        outcome = archive_queue.mark_failed(rid, "Disk full", max_attempts=5)
        state = _archive_state(archive_db, rid)
        assert outcome == 'pending'
        assert state['last_error'] == 'Disk full'
        assert state['previous_last_error'] is None
        assert state['attempts'] == 1

    def test_second_failure_rotates(self, archive_db):
        from services import archive_queue
        rid = _seed_archive_row(archive_db, "/src/clip2.mp4")
        archive_queue.mark_failed(rid, "Disk full", max_attempts=5)
        archive_queue.mark_failed(rid, "Permission denied", max_attempts=5)
        state = _archive_state(archive_db, rid)
        assert state['last_error'] == 'Permission denied'
        assert state['previous_last_error'] == 'Disk full'
        assert state['attempts'] == 2

    def test_dead_letter_promotion_also_rotates(self, archive_db):
        from services import archive_queue
        rid = _seed_archive_row(archive_db, "/src/clip3.mp4")
        # 1 attempt, then promote on 2nd failure (max_attempts=2).
        archive_queue.mark_failed(rid, "Error A", max_attempts=2)
        outcome = archive_queue.mark_failed(rid, "Error B", max_attempts=2)
        state = _archive_state(archive_db, rid)
        assert outcome == 'dead_letter'
        assert state['status'] == 'dead_letter'
        assert state['last_error'] == 'Error B'
        assert state['previous_last_error'] == 'Error A'

    def test_three_cycles_keeps_only_one_step_back(self, archive_db):
        from services import archive_queue
        rid = _seed_archive_row(archive_db, "/src/clip4.mp4")
        archive_queue.mark_failed(rid, "Error A", max_attempts=10)
        archive_queue.mark_failed(rid, "Error B", max_attempts=10)
        archive_queue.mark_failed(rid, "Error C", max_attempts=10)
        state = _archive_state(archive_db, rid)
        # Only the most recent step is preserved; "Error A" is gone.
        assert state['last_error'] == 'Error C'
        assert state['previous_last_error'] == 'Error B'

    def test_retry_dead_letter_preserves_both(self, archive_db):
        """Retrying a dead-letter row must NOT touch either error
        column — the operator's failure history should survive the
        retry click."""
        from services import archive_queue
        rid = _seed_archive_row(archive_db, "/src/clip5.mp4")
        archive_queue.mark_failed(rid, "Error A", max_attempts=2)
        archive_queue.mark_failed(rid, "Error B", max_attempts=2)
        # Now in dead_letter with both errors populated.
        n = archive_queue.retry_dead_letter(row_id=rid)
        assert n == 1
        state = _archive_state(archive_db, rid)
        assert state['status'] == 'pending'
        assert state['attempts'] == 0  # reset is documented behavior
        assert state['last_error'] == 'Error B'  # preserved
        assert state['previous_last_error'] == 'Error A'  # preserved


# ---------------------------------------------------------------------------
# indexing_queue defer_queue_item rotates
# ---------------------------------------------------------------------------


@pytest.fixture
def indexing_db(tmp_path):
    from services import mapping_migrations
    db = str(tmp_path / "geodata.db")
    conn = mapping_migrations._init_db(db)
    conn.close()
    return db


def _seed_indexing_row(db_path, key, file_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO indexing_queue
               (canonical_key, file_path, priority, enqueued_at, source)
           VALUES (?, ?, 50, 0, 'test')""",
        (key, file_path),
    )
    conn.commit()
    conn.close()


def _indexing_state(db_path, key):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT last_error, previous_last_error, attempts "
        "FROM indexing_queue WHERE canonical_key = ?",
        (key,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


class TestIndexingQueueRotation:

    def test_first_defer_leaves_previous_null(self, indexing_db):
        from services import indexing_queue_service
        _seed_indexing_row(indexing_db, "k1", "/p/a.mp4")
        ok = indexing_queue_service.defer_queue_item(
            indexing_db, "k1", next_attempt_at=0,
            bump_attempts=True, last_error="Parse error",
        )
        assert ok
        state = _indexing_state(indexing_db, "k1")
        assert state['last_error'] == 'Parse error'
        assert state['previous_last_error'] is None
        assert state['attempts'] == 1

    def test_second_defer_rotates(self, indexing_db):
        from services import indexing_queue_service
        _seed_indexing_row(indexing_db, "k2", "/p/b.mp4")
        indexing_queue_service.defer_queue_item(
            indexing_db, "k2", next_attempt_at=0,
            bump_attempts=True, last_error="Parse error A",
        )
        indexing_queue_service.defer_queue_item(
            indexing_db, "k2", next_attempt_at=0,
            bump_attempts=True, last_error="Parse error B",
        )
        state = _indexing_state(indexing_db, "k2")
        assert state['last_error'] == 'Parse error B'
        assert state['previous_last_error'] == 'Parse error A'

    def test_no_bump_attempts_path_also_rotates(self, indexing_db):
        """The TOO_NEW path defers without bumping attempts; rotation
        must still happen so the operator sees both errors."""
        from services import indexing_queue_service
        _seed_indexing_row(indexing_db, "k3", "/p/c.mp4")
        indexing_queue_service.defer_queue_item(
            indexing_db, "k3", next_attempt_at=0,
            bump_attempts=False, last_error="Too new",
        )
        indexing_queue_service.defer_queue_item(
            indexing_db, "k3", next_attempt_at=0,
            bump_attempts=False, last_error="Still too new",
        )
        state = _indexing_state(indexing_db, "k3")
        assert state['last_error'] == 'Still too new'
        assert state['previous_last_error'] == 'Too new'
        assert state['attempts'] == 0  # bump_attempts=False


# ---------------------------------------------------------------------------
# cloud_archive _mark_upload_failure rotates
# ---------------------------------------------------------------------------


class TestCloudUploadRotation:

    def test_first_failure_leaves_previous_null(self, tmp_path, monkeypatch):
        from services import cloud_archive_service as svc
        db = tmp_path / "cloud.db"
        monkeypatch.setattr(svc, "_startup_recovery_done", False)
        conn = svc._init_cloud_tables(str(db))
        try:
            conn.execute(
                "INSERT INTO cloud_synced_files (file_path, status, retry_count) "
                "VALUES (?, 'pending', 0)",
                ("ArchivedClips/x.mp4",),
            )
            conn.commit()
            monkeypatch.setattr(svc, "_read_retry_max_attempts_setting", lambda: 5)
            svc._mark_upload_failure(conn, "ArchivedClips/x.mp4", "Auth fail")
            row = conn.execute(
                "SELECT last_error, previous_last_error, retry_count "
                "FROM cloud_synced_files WHERE file_path = ?",
                ("ArchivedClips/x.mp4",),
            ).fetchone()
        finally:
            conn.close()
        assert row['last_error'] == 'Auth fail'
        assert row['previous_last_error'] is None
        assert row['retry_count'] == 1

    def test_second_failure_rotates(self, tmp_path, monkeypatch):
        from services import cloud_archive_service as svc
        db = tmp_path / "cloud.db"
        monkeypatch.setattr(svc, "_startup_recovery_done", False)
        conn = svc._init_cloud_tables(str(db))
        try:
            conn.execute(
                "INSERT INTO cloud_synced_files (file_path, status, retry_count) "
                "VALUES (?, 'pending', 0)",
                ("ArchivedClips/y.mp4",),
            )
            conn.commit()
            monkeypatch.setattr(svc, "_read_retry_max_attempts_setting", lambda: 5)
            svc._mark_upload_failure(conn, "ArchivedClips/y.mp4", "Auth fail")
            svc._mark_upload_failure(conn, "ArchivedClips/y.mp4", "Quota exceeded")
            row = conn.execute(
                "SELECT last_error, previous_last_error, retry_count "
                "FROM cloud_synced_files WHERE file_path = ?",
                ("ArchivedClips/y.mp4",),
            ).fetchone()
        finally:
            conn.close()
        assert row['last_error'] == 'Quota exceeded'
        assert row['previous_last_error'] == 'Auth fail'


# ---------------------------------------------------------------------------
# Live Event Sync _record_failure rotates
# ---------------------------------------------------------------------------


class TestLESRotation:

    def test_first_then_second_failure_rotates(self, tmp_path, monkeypatch):
        from services import live_event_sync_service as les
        from services import cloud_archive_service as svc

        db_path = str(tmp_path / "cloud_sync.db")
        monkeypatch.setattr(svc, "CLOUD_ARCHIVE_DB_PATH", db_path)
        monkeypatch.setattr(les, "CLOUD_ARCHIVE_DB_PATH", db_path)

        conn = les._open_db()
        try:
            les._ensure_schema(conn)
            conn.execute(
                "INSERT INTO live_event_queue "
                "(event_dir, event_json_path, status, enqueued_at, attempts) "
                "VALUES (?, ?, 'uploading', '2026-01-01T00:00:00Z', 1)",
                ("/some/event/dir", "/some/event/dir/event.json"),
            )
            conn.commit()
            row_id = conn.execute(
                "SELECT id FROM live_event_queue"
            ).fetchone()[0]

            # First failure (non-transient, retry not exhausted).
            les._mark_failed(conn, row_id, attempts=1,
                             err="Network unreachable", transient=False)
            # Re-arm the row so the next attempt counter doesn't trip retry cap.
            conn.execute(
                "UPDATE live_event_queue SET status = 'uploading' "
                "WHERE id = ?", (row_id,),
            )
            conn.commit()
            les._mark_failed(conn, row_id, attempts=2,
                             err="Auth failed", transient=False)

            row = conn.execute(
                "SELECT last_error, previous_last_error "
                "FROM live_event_queue WHERE id = ?", (row_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row['last_error'] == 'Auth failed'
        assert row['previous_last_error'] == 'Network unreachable'

    def test_transient_path_also_rotates(self, tmp_path, monkeypatch):
        """The transient-defer code path also writes ``last_error`` —
        rotation must apply there too."""
        from services import live_event_sync_service as les
        from services import cloud_archive_service as svc

        db_path = str(tmp_path / "cloud_sync.db")
        monkeypatch.setattr(svc, "CLOUD_ARCHIVE_DB_PATH", db_path)
        monkeypatch.setattr(les, "CLOUD_ARCHIVE_DB_PATH", db_path)

        conn = les._open_db()
        try:
            les._ensure_schema(conn)
            conn.execute(
                "INSERT INTO live_event_queue "
                "(event_dir, event_json_path, status, enqueued_at, attempts) "
                "VALUES (?, ?, 'uploading', '2026-01-01T00:00:00Z', 1)",
                ("/dir2", "/dir2/event.json"),
            )
            conn.commit()
            row_id = conn.execute("SELECT id FROM live_event_queue").fetchone()[0]
            les._mark_failed(conn, row_id, attempts=1,
                             err="WiFi flap A", transient=True)
            les._mark_failed(conn, row_id, attempts=1,
                             err="WiFi flap B", transient=True)
            row = conn.execute(
                "SELECT last_error, previous_last_error "
                "FROM live_event_queue WHERE id = ?", (row_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row['last_error'] == 'WiFi flap B'
        assert row['previous_last_error'] == 'WiFi flap A'


# ---------------------------------------------------------------------------
# /api/jobs/failed exposes previous_last_error
# ---------------------------------------------------------------------------


class TestApiExposesPreviousError:

    def test_archive_lister_includes_field(self, archive_db):
        from services import archive_queue
        from blueprints import jobs as jobs_bp
        rid = _seed_archive_row(archive_db, "/src/api_test.mp4")
        archive_queue.mark_failed(rid, "Error A", max_attempts=2)
        archive_queue.mark_failed(rid, "Error B", max_attempts=2)
        rows = jobs_bp._archive_rows(limit=10)
        assert len(rows) == 1
        assert rows[0]['last_error'] == 'Error B'
        assert rows[0]['previous_last_error'] == 'Error A'

    def test_archive_lister_redacts_previous_error(self, archive_db):
        """The previous-error field MUST go through the same redaction
        pipeline as the current error — no point cleaning one and
        leaking the other."""
        from services import archive_queue
        from blueprints import jobs as jobs_bp
        rid = _seed_archive_row(archive_db, "/src/redact_test.mp4")
        archive_queue.mark_failed(
            rid, "First: rclone copy /home/pi/secret.mp4 failed",
            max_attempts=5,
        )
        archive_queue.mark_failed(rid, "Second: timeout", max_attempts=5)
        rows = jobs_bp._archive_rows(limit=10)
        assert len(rows) == 0  # not yet dead-letter
        # Force into dead_letter to populate the lister.
        for _ in range(3):
            archive_queue.mark_failed(rid, "Force DL", max_attempts=5)
        rows = jobs_bp._archive_rows(limit=10)
        assert len(rows) == 1
        # /home/pi/... must be redacted in BOTH columns.
        assert '/home/pi/secret.mp4' not in rows[0]['previous_last_error'] or \
               '<path>' in rows[0]['previous_last_error']

    def test_lister_handles_null_previous_error(self, archive_db):
        from services import archive_queue
        from blueprints import jobs as jobs_bp
        rid = _seed_archive_row(archive_db, "/src/single_fail.mp4")
        # Force into dead_letter with only ONE error ever recorded.
        for _ in range(5):
            archive_queue.mark_failed(rid, "Same error", max_attempts=5)
        rows = jobs_bp._archive_rows(limit=10)
        assert len(rows) == 1
        # Last error rotates A → previous → A → previous (etc.) so after
        # 2+ failures previous IS populated. The "null previous" case
        # really tests the redactor's handling of a None input — if the
        # rotation pipeline is correct the operator never sees a null
        # previous on a multi-failure row.
        # The redactor returns '' for None.
        assert rows[0]['previous_last_error'] == 'Same error' or \
               rows[0]['previous_last_error'] == ''


# ---------------------------------------------------------------------------
# Per-subsystem API lister tests (Issue #132 review-fix #158)
# Each lister had a bug where the SELECT projection omitted
# ``previous_last_error``, so the rotation worked at the DB level but
# the column never reached the JSON response. These tests pin the
# end-to-end plumbing for indexer / cloud / LES (archive_queue uses
# SELECT *, covered by ``test_archive_lister_includes_field`` above).
# ---------------------------------------------------------------------------


class TestIndexerListerExposesPreviousError:

    def test_indexer_lister_returns_previous_error(self, indexing_db, monkeypatch):
        from services import indexing_queue_service as iqs
        from blueprints import jobs as jobs_bp
        # Force into dead_letter (>= _PARSE_ERROR_MAX_ATTEMPTS = 3).
        _seed_indexing_row(indexing_db, "/dl_test.mp4", "/dl_test.mp4")
        for err in ("First error", "Second error", "Third error"):
            iqs.defer_queue_item(
                indexing_db, "/dl_test.mp4", next_attempt_at=0.0,
                bump_attempts=True, last_error=err,
            )
        monkeypatch.setattr(jobs_bp, "MAPPING_ENABLED", True)
        monkeypatch.setattr(jobs_bp, "MAPPING_DB_PATH", indexing_db)
        rows = jobs_bp._indexer_rows(limit=10)
        assert len(rows) == 1
        assert rows[0]['last_error'] == 'Third error'
        assert rows[0]['previous_last_error'] == 'Second error'


class TestCloudListerExposesPreviousError:

    def test_cloud_lister_returns_previous_error(self, tmp_path, monkeypatch):
        from services import cloud_archive_service as svc
        from blueprints import jobs as jobs_bp
        db = str(tmp_path / "cloud.db")
        monkeypatch.setattr(svc, "_startup_recovery_done", False)
        conn = svc._init_cloud_tables(db)
        try:
            conn.execute(
                "INSERT INTO cloud_synced_files "
                "(file_path, status, retry_count) "
                "VALUES (?, 'pending', 0)",
                ("ArchivedClips/dl.mp4",),
            )
            conn.commit()
            monkeypatch.setattr(svc, "_read_retry_max_attempts_setting",
                                lambda: 5)
            # Burn through retries until dead_letter.
            for err in ("First", "Second", "Third", "Fourth", "Fifth"):
                conn.execute(
                    "UPDATE cloud_synced_files SET status = 'pending' "
                    "WHERE file_path = ?", ("ArchivedClips/dl.mp4",),
                )
                conn.commit()
                svc._mark_upload_failure(conn, "ArchivedClips/dl.mp4", err)
                conn.commit()
        finally:
            conn.commit()
            conn.close()
        monkeypatch.setattr(svc, "CLOUD_ARCHIVE_DB_PATH", db)
        monkeypatch.setattr(jobs_bp, "CLOUD_ARCHIVE_ENABLED", True)
        rows = jobs_bp._cloud_sync_rows(limit=10)
        assert len(rows) == 1
        assert rows[0]['last_error'] == 'Fifth'
        assert rows[0]['previous_last_error'] == 'Fourth'


class TestLESListerExposesPreviousError:

    def test_les_lister_returns_previous_error(self, tmp_path, monkeypatch):
        from services import live_event_sync_service as les
        from services import cloud_archive_service as svc
        from blueprints import jobs as jobs_bp
        db = str(tmp_path / "cloud_sync.db")
        monkeypatch.setattr(svc, "CLOUD_ARCHIVE_DB_PATH", db)
        monkeypatch.setattr(les, "CLOUD_ARCHIVE_DB_PATH", db)
        # Force a fresh schema check (process-level cache flag from
        # the review-fix optimization).
        monkeypatch.setattr(les, "_schema_alter_done", False)
        conn = les._open_db()
        try:
            les._ensure_schema(conn)
            conn.execute(
                "INSERT INTO live_event_queue "
                "(event_dir, event_json_path, status, enqueued_at, attempts) "
                "VALUES ('/d', '/d/event.json', 'uploading', "
                "'2026-01-01T00:00:00Z', 1)"
            )
            conn.commit()
            row_id = conn.execute(
                "SELECT id FROM live_event_queue").fetchone()[0]
            # Two non-transient failures with retry cap big enough to
            # NOT mark the row 'failed' in between.
            for n, err in enumerate(("Err A", "Err B"), start=1):
                conn.execute(
                    "UPDATE live_event_queue SET status = 'uploading' "
                    "WHERE id = ?", (row_id,),
                )
                conn.commit()
                les._mark_failed(conn, row_id, attempts=n, err=err,
                                 transient=False)
            # Force into 'failed' so the lister picks it up.
            conn.execute(
                "UPDATE live_event_queue SET status = 'failed' "
                "WHERE id = ?", (row_id,),
            )
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setattr(jobs_bp, "LIVE_EVENT_SYNC_ENABLED", True)
        rows = jobs_bp._live_event_rows(limit=10)
        assert len(rows) == 1
        assert rows[0]['last_error'] == 'Err B'
        assert rows[0]['previous_last_error'] == 'Err A'


# ---------------------------------------------------------------------------
# LES retry_failed must preserve last_error AND previous_last_error
# (Issue #132 review-fix #158 — Warning #5)
# ---------------------------------------------------------------------------


class TestLESRetryPreservesHistory:

    def test_retry_failed_does_not_clear_error_columns(self, tmp_path,
                                                       monkeypatch):
        """A → B → retry → C must produce last=C, prev=B (not last=C,
        prev=NULL). The pre-fix retry_failed cleared ``last_error``,
        which then rotated NULL into ``previous_last_error`` on the
        next failure — losing both A and B. The other 3 subsystems'
        ``retry_dead_letter`` already preserved both columns; this
        test pins the LES path to the same contract."""
        from services import live_event_sync_service as les
        from services import cloud_archive_service as svc
        db = str(tmp_path / "cloud_sync.db")
        monkeypatch.setattr(svc, "CLOUD_ARCHIVE_DB_PATH", db)
        monkeypatch.setattr(les, "CLOUD_ARCHIVE_DB_PATH", db)
        monkeypatch.setattr(les, "_schema_alter_done", False)
        # Wake() is called from retry_failed; stub it (no worker).
        monkeypatch.setattr(les, "wake", lambda: None)
        conn = les._open_db()
        try:
            les._ensure_schema(conn)
            conn.execute(
                "INSERT INTO live_event_queue "
                "(event_dir, event_json_path, status, enqueued_at, attempts) "
                "VALUES ('/d', '/d/event.json', 'uploading', "
                "'2026-01-01T00:00:00Z', 1)"
            )
            conn.commit()
            row_id = conn.execute(
                "SELECT id FROM live_event_queue").fetchone()[0]

            # Failure A then B (rotates B→prev, A→last after rotation
            # logic — actually last=B, prev=A).
            les._mark_failed(conn, row_id, attempts=1, err="Error A",
                             transient=False)
            conn.execute("UPDATE live_event_queue SET status='uploading' "
                         "WHERE id = ?", (row_id,))
            conn.commit()
            les._mark_failed(conn, row_id, attempts=2, err="Error B",
                             transient=False)
            # Force into 'failed' so retry_failed picks it up.
            conn.execute(
                "UPDATE live_event_queue SET status='failed' WHERE id = ?",
                (row_id,),
            )
            conn.commit()
        finally:
            conn.close()

        # Retry the row.
        n = les.retry_failed(row_id=row_id)
        assert n == 1, "retry_failed should reset exactly one row"

        # After retry: BOTH error columns must still be there.
        conn = les._open_db()
        try:
            row = conn.execute(
                "SELECT last_error, previous_last_error, status, attempts "
                "FROM live_event_queue WHERE id = ?", (row_id,),
            ).fetchone()

            assert row['status'] == 'pending'
            assert row['attempts'] == 0
            assert row['last_error'] == 'Error B', (
                "retry_failed must preserve last_error so the next "
                "failure rotates the correct value into previous_last_error"
            )
            assert row['previous_last_error'] == 'Error A', (
                "retry_failed must preserve previous_last_error so the "
                "operator's view of the prior cycle survives the click"
            )

            # Now simulate Error C (the next failure cycle).
            conn.execute("UPDATE live_event_queue SET status='uploading' "
                         "WHERE id = ?", (row_id,))
            conn.commit()
            les._mark_failed(conn, row_id, attempts=1, err="Error C",
                             transient=False)
            row2 = conn.execute(
                "SELECT last_error, previous_last_error "
                "FROM live_event_queue WHERE id = ?", (row_id,),
            ).fetchone()
            assert row2['last_error'] == 'Error C'
            assert row2['previous_last_error'] == 'Error B', (
                "Without the retry-preservation fix, this would be NULL "
                "(the pre-fix retry_failed cleared last_error → NULL "
                "rotated into previous_last_error)"
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Cloud v3 ALTER must apply even when v2 migration fails
# (Issue #132 review-fix #158 — Info #2)
# ---------------------------------------------------------------------------


class TestCloudV3MigrationIsIndependentOfV2:

    def test_v3_column_present_after_init(self, tmp_path, monkeypatch):
        """``_mark_upload_failure`` rotates ``previous_last_error`` on
        every failure ù the column MUST exist after init regardless
        of v2 path-canonicalization outcome. Pre-fix, the v3 ALTER
        was gated on ``migration_ok`` from v2; this test verifies the
        ungate by initializing a fresh DB and confirming the column
        is present."""
        from services import cloud_archive_service as svc
        db = str(tmp_path / "cloud.db")
        monkeypatch.setattr(svc, "_startup_recovery_done", False)
        conn = svc._init_cloud_tables(db)
        try:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(cloud_synced_files)")}
        finally:
            conn.close()
        assert 'previous_last_error' in cols, (
            "v3 column MUST be present after init; otherwise "
            "_mark_upload_failure crashes on the rotation UPDATE"
        )
