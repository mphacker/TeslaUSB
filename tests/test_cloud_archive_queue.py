"""Tests for cloud_archive_service queue management — issue #67.

Focuses on queue deletion behaviour: items in the local queue must be
removable by the user regardless of cloud provider configuration, sync
worker state, or row status (queued / pending / uploading / failed).
The local SQLite queue is local data the user owns.
"""

from __future__ import annotations

import sqlite3

import pytest

from services import cloud_archive_service as svc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    """Initialise an isolated cloud_sync.db and point the service at it.

    Resets ``_startup_recovery_done`` so each test gets fresh recovery
    semantics, and patches the module-level ``CLOUD_ARCHIVE_DB_PATH`` so
    helpers that read it directly use the test database.
    """
    db_path = str(tmp_path / "cloud_sync.db")
    monkeypatch.setattr(svc, "CLOUD_ARCHIVE_DB_PATH", db_path)
    monkeypatch.setattr(svc, "_startup_recovery_done", False)
    # Materialise the schema once.
    conn = svc._init_cloud_tables(db_path)
    conn.close()
    return db_path


def _insert_row(db_path, file_path, status, file_size=1024):
    """Insert a single row into cloud_synced_files with the given status."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO cloud_synced_files "
        "(file_path, file_size, file_mtime, status, retry_count) "
        "VALUES (?, ?, ?, ?, 0)",
        (file_path, file_size, 1700000000.0, status),
    )
    conn.commit()
    conn.close()


def _row_count(db_path, status=None):
    conn = sqlite3.connect(db_path)
    if status is None:
        row = conn.execute(
            "SELECT COUNT(*) FROM cloud_synced_files"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM cloud_synced_files WHERE status = ?",
            (status,),
        ).fetchone()
    conn.close()
    return row[0]


# ---------------------------------------------------------------------------
# remove_from_queue — single-item delete
# ---------------------------------------------------------------------------

class TestRemoveFromQueue:
    """remove_from_queue must succeed for any non-synced row."""

    def test_removes_queued_row(self, db):
        _insert_row(db, "path/clip.mp4", "queued")
        ok, _ = svc.remove_from_queue("path/clip.mp4")
        assert ok is True
        assert _row_count(db) == 0

    def test_removes_pending_row(self, db):
        _insert_row(db, "path/clip.mp4", "pending")
        ok, _ = svc.remove_from_queue("path/clip.mp4")
        assert ok is True
        assert _row_count(db) == 0

    def test_removes_uploading_row(self, db):
        """Reproduction for issue #67: 'uploading' rows stuck after
        ``stop_sync`` + provider disconnect must still be deletable."""
        _insert_row(db, "path/clip.mp4", "uploading")
        ok, _ = svc.remove_from_queue("path/clip.mp4")
        assert ok is True
        assert _row_count(db) == 0

    def test_removes_failed_row(self, db):
        """Failed rows from a disconnected/unreachable provider must be
        deletable."""
        _insert_row(db, "path/clip.mp4", "failed")
        ok, _ = svc.remove_from_queue("path/clip.mp4")
        assert ok is True
        assert _row_count(db) == 0

    def test_preserves_synced_row(self, db):
        """Synced rows are historical records and must NOT be deletable
        through the queue API (they're not exposed via get_sync_queue
        either)."""
        _insert_row(db, "path/clip.mp4", "synced")
        ok, _ = svc.remove_from_queue("path/clip.mp4")
        # Returns success ("not in queue") but the synced row stays.
        assert ok is True
        assert _row_count(db, "synced") == 1

    def test_missing_path_is_not_an_error(self, db):
        """Removing a path that isn't in the queue is a no-op success —
        the UI should never see an error for a row that was already
        cleaned up by the worker."""
        ok, msg = svc.remove_from_queue("never/queued.mp4")
        assert ok is True
        assert "queue" in msg.lower()

    def test_provider_unconfigured_does_not_block_delete(self, db, monkeypatch):
        """The local queue is local data — clearing the cloud provider
        from config.yaml must not prevent deletion."""
        monkeypatch.setattr(svc, "CLOUD_ARCHIVE_PROVIDER", "")
        _insert_row(db, "path/clip.mp4", "uploading")
        ok, _ = svc.remove_from_queue("path/clip.mp4")
        assert ok is True
        assert _row_count(db) == 0

    def test_only_targets_named_path(self, db):
        """remove_from_queue must not touch other rows."""
        _insert_row(db, "a/clip.mp4", "uploading")
        _insert_row(db, "b/clip.mp4", "uploading")
        ok, _ = svc.remove_from_queue("a/clip.mp4")
        assert ok is True
        assert _row_count(db) == 1
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT file_path FROM cloud_synced_files"
        ).fetchone()
        conn.close()
        assert row[0] == "b/clip.mp4"


# ---------------------------------------------------------------------------
# clear_queue — bulk delete
# ---------------------------------------------------------------------------

class TestClearQueue:
    """clear_queue must drain every non-synced row in one call."""

    def test_clears_mixed_statuses(self, db):
        """All four queue statuses must be cleared in a single call."""
        _insert_row(db, "a.mp4", "queued")
        _insert_row(db, "b.mp4", "pending")
        _insert_row(db, "c.mp4", "uploading")
        _insert_row(db, "d.mp4", "failed")
        ok, msg = svc.clear_queue()
        assert ok is True
        assert "4" in msg
        assert _row_count(db) == 0

    def test_preserves_synced_history(self, db):
        """Synced rows are historical records — clear_queue must leave
        them alone so the dashboard's 'total synced' counter stays
        accurate."""
        _insert_row(db, "a.mp4", "queued")
        _insert_row(db, "b.mp4", "uploading")
        _insert_row(db, "c.mp4", "synced")
        ok, _ = svc.clear_queue()
        assert ok is True
        assert _row_count(db) == 1
        assert _row_count(db, "synced") == 1

    def test_provider_unconfigured_does_not_block_clear(self, db, monkeypatch):
        """Reproduction for issue #67: clearing the queue must work even
        after the cloud provider has been disconnected."""
        monkeypatch.setattr(svc, "CLOUD_ARCHIVE_PROVIDER", "")
        _insert_row(db, "a.mp4", "uploading")
        _insert_row(db, "b.mp4", "queued")
        ok, _ = svc.clear_queue()
        assert ok is True
        assert _row_count(db) == 0

    def test_empty_queue_returns_zero(self, db):
        ok, msg = svc.clear_queue()
        assert ok is True
        assert "0" in msg


# ---------------------------------------------------------------------------
# get_sync_queue interaction
# ---------------------------------------------------------------------------

class TestGetSyncQueueAfterDelete:
    """End-to-end: after delete, the row must be gone from get_sync_queue."""

    def test_uploading_row_disappears_from_queue_after_delete(self, db):
        """The original bug from issue #67: an 'uploading' row was
        visible in get_sync_queue but invisible to the delete API."""
        _insert_row(db, "stuck.mp4", "uploading")

        before = svc.get_sync_queue()
        assert before["total"] == 1
        assert before["queue"][0]["file_path"] == "stuck.mp4"
        assert before["queue"][0]["status"] == "uploading"

        ok, _ = svc.remove_from_queue("stuck.mp4")
        assert ok is True

        after = svc.get_sync_queue()
        assert after["total"] == 0
        assert after["queue"] == []
