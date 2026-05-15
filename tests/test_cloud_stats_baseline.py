"""Tests for the cloud-sync stats baseline (counter reset).

The Cloud Sync page now exposes a *Reset counters* button that zeros the
*Synced* and *Transferred* dashboard totals **without** wiping the
``cloud_synced_files`` rows that prevent already-uploaded clips from
being re-uploaded. Implementation is a single timestamp in
``cloud_archive_meta`` that ``get_sync_stats`` uses to filter the COUNT
and SUM. These tests pin the contract:

* baseline starts unset → counters show full lifetime totals
* :func:`reset_stats_baseline` persists ``stats_baseline_at``
* rows whose ``synced_at`` is ≤ the baseline are excluded from
  ``total_synced`` and ``total_bytes``
* NULL ``synced_at`` rows are *included* (defensive: never under-count
  work the user actually saw complete)
* ``total_pending`` / ``total_failed`` (current state metrics) are NOT
  filtered — a reset must never lie about the current queue depth
* schema is at the current target version after init
* baseline survives across connections (real persistence, not session
  state)
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_WEB = Path(__file__).resolve().parent.parent / "scripts" / "web"
if str(SCRIPTS_WEB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_WEB))

from services import cloud_archive_service as cas  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cloud_db(tmp_path: Path) -> str:
    """Fresh cloud_sync.db at the current schema target."""
    db = tmp_path / "cloud_sync.db"
    conn = cas._init_cloud_tables(str(db))
    conn.close()
    return str(db)


def _insert_synced(db: str, path: str, file_size: int, synced_at: str) -> None:
    """Insert a row directly so tests don't depend on the upload pipeline."""
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO cloud_synced_files "
            "(file_path, file_size, status, synced_at) "
            "VALUES (?, ?, 'synced', ?)",
            (path, file_size, synced_at),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_pending(db: str, path: str) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO cloud_synced_files "
            "(file_path, file_size, status) "
            "VALUES (?, ?, 'pending')",
            (path, 0),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_failed(db: str, path: str) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO cloud_synced_files "
            "(file_path, file_size, status) "
            "VALUES (?, ?, 'failed')",
            (path, 0),
        )
        conn.commit()
    finally:
        conn.close()


def _module_version(db: str) -> int:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT version FROM module_versions WHERE module='cloud_archive'"
        ).fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# get_stats_baseline / reset_stats_baseline
# ---------------------------------------------------------------------------


class TestStatsBaselineHelpers:
    def test_initial_baseline_is_none(self, cloud_db):
        assert cas.get_stats_baseline(cloud_db) is None

    def test_reset_persists_timestamp(self, cloud_db):
        ok, ts = cas.reset_stats_baseline(cloud_db)
        assert ok is True
        assert ts  # ISO-8601 string
        # Returned value matches what's now stored.
        assert cas.get_stats_baseline(cloud_db) == ts

    def test_reset_returns_iso8601_utc(self, cloud_db):
        ok, ts = cas.reset_stats_baseline(cloud_db)
        assert ok is True
        # ISO-8601 with offset (+00:00) — lexicographic comparison safe.
        assert "T" in ts
        assert ts.endswith("+00:00") or ts.endswith("Z")

    def test_repeated_reset_overwrites(self, cloud_db):
        ok1, ts1 = cas.reset_stats_baseline(cloud_db)
        time.sleep(0.01)
        ok2, ts2 = cas.reset_stats_baseline(cloud_db)
        assert ok1 and ok2
        assert ts2 > ts1
        assert cas.get_stats_baseline(cloud_db) == ts2

    def test_baseline_survives_reopen(self, cloud_db):
        ok, ts = cas.reset_stats_baseline(cloud_db)
        assert ok
        # Open a fresh connection (simulates a new request).
        assert cas.get_stats_baseline(cloud_db) == ts


# ---------------------------------------------------------------------------
# get_sync_stats filtering
# ---------------------------------------------------------------------------


class TestGetSyncStatsBaselineFilter:
    def test_no_baseline_counts_everything(self, cloud_db):
        _insert_synced(cloud_db, "SentryClips/a.mp4", 1000, "2026-01-01T00:00:00+00:00")
        _insert_synced(cloud_db, "SentryClips/b.mp4", 2000, "2026-06-01T00:00:00+00:00")
        stats = cas.get_sync_stats(cloud_db)
        assert stats["total_synced"] == 2
        assert stats["total_bytes"] == 3000
        assert stats["stats_baseline_at"] is None

    def test_baseline_excludes_older_rows(self, cloud_db):
        _insert_synced(cloud_db, "SentryClips/old1.mp4", 100, "2026-01-01T00:00:00+00:00")
        _insert_synced(cloud_db, "SentryClips/old2.mp4", 200, "2026-02-01T00:00:00+00:00")
        # Reset baseline to 2026-03-01.
        baseline = "2026-03-01T00:00:00+00:00"
        conn = sqlite3.connect(cloud_db)
        conn.execute(
            "INSERT OR REPLACE INTO cloud_archive_meta (key, value) VALUES (?, ?)",
            (cas._CLOUD_STATS_BASELINE_KEY, baseline),
        )
        conn.commit()
        conn.close()
        _insert_synced(cloud_db, "SentryClips/new1.mp4", 500, "2026-04-01T00:00:00+00:00")
        _insert_synced(cloud_db, "SentryClips/new2.mp4", 1000, "2026-05-01T00:00:00+00:00")

        stats = cas.get_sync_stats(cloud_db)
        # Only the post-baseline rows count.
        assert stats["total_synced"] == 2
        assert stats["total_bytes"] == 1500
        assert stats["stats_baseline_at"] == baseline

    def test_baseline_includes_null_synced_at(self, cloud_db):
        """Legacy rows with NULL synced_at must NOT silently disappear."""
        conn = sqlite3.connect(cloud_db)
        # Synthesize a legacy row (no synced_at column value).
        conn.execute(
            "INSERT INTO cloud_synced_files "
            "(file_path, file_size, status, synced_at) "
            "VALUES (?, ?, 'synced', NULL)",
            ("SentryClips/legacy.mp4", 999),
        )
        conn.execute(
            "INSERT OR REPLACE INTO cloud_archive_meta (key, value) VALUES (?, ?)",
            (cas._CLOUD_STATS_BASELINE_KEY, "2026-12-31T23:59:59+00:00"),
        )
        conn.commit()
        conn.close()

        stats = cas.get_sync_stats(cloud_db)
        # Defensive inclusion of NULL rows so the counter never under-counts.
        assert stats["total_synced"] == 1
        assert stats["total_bytes"] == 999

    def test_pending_not_filtered_by_baseline(self, cloud_db):
        """Pending count is current state — must NOT be zeroed by a reset."""
        _insert_pending(cloud_db, "SentryClips/p1.mp4")
        _insert_pending(cloud_db, "SentryClips/p2.mp4")
        ok, _ = cas.reset_stats_baseline(cloud_db)
        assert ok
        stats = cas.get_sync_stats(cloud_db)
        assert stats["total_pending"] >= 2

    def test_failed_not_filtered_by_baseline(self, cloud_db):
        _insert_failed(cloud_db, "SentryClips/f1.mp4")
        _insert_failed(cloud_db, "SentryClips/f2.mp4")
        ok, _ = cas.reset_stats_baseline(cloud_db)
        assert ok
        stats = cas.get_sync_stats(cloud_db)
        assert stats["total_failed"] == 2

    def test_after_reset_old_synced_rows_drop_out(self, cloud_db):
        _insert_synced(cloud_db, "SentryClips/a.mp4", 100, "2026-01-01T00:00:00+00:00")
        before = cas.get_sync_stats(cloud_db)
        assert before["total_synced"] == 1
        assert before["total_bytes"] == 100

        ok, _ = cas.reset_stats_baseline(cloud_db)
        assert ok
        after = cas.get_sync_stats(cloud_db)
        # The old row's synced_at is before the just-set baseline.
        assert after["total_synced"] == 0
        assert after["total_bytes"] == 0
        assert after["stats_baseline_at"] is not None

    def test_dedup_rows_preserved_after_reset(self, cloud_db):
        """Reset MUST NOT delete cloud_synced_files rows (would re-upload)."""
        _insert_synced(cloud_db, "SentryClips/keep.mp4", 100, "2026-01-01T00:00:00+00:00")
        ok, _ = cas.reset_stats_baseline(cloud_db)
        assert ok
        conn = sqlite3.connect(cloud_db)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM cloud_synced_files WHERE status='synced'"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == 1, "Dedup rows must survive a counter reset"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestStatsBaselineSchema:
    def test_meta_table_exists(self, cloud_db):
        conn = sqlite3.connect(cloud_db)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='cloud_archive_meta'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None

    def test_synced_at_index_exists(self, cloud_db):
        """``idx_cloud_synced_synced_at`` keeps baseline-filter COUNT fast."""
        conn = sqlite3.connect(cloud_db)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name='idx_cloud_synced_synced_at'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None

    def test_schema_version_bumped(self, cloud_db):
        version = _module_version(cloud_db)
        assert version == cas._CLOUD_SCHEMA_VERSION
        assert cas._CLOUD_SCHEMA_VERSION >= 5
