"""Phase 5.5 — get_sync_status_for_events single-query batching.

Pins the contract of the batched UI status lookup and verifies that
``get_sync_status_for_events`` issues exactly ONE SQL query against
``cloud_synced_files`` regardless of how many event names are
requested (the legacy code issued one ``SELECT … LIKE '%name%' …
LIMIT 1`` per event — typical UI request: 30 events → 30 queries).

Tests cover:

* empty input returns empty dict (legacy parity)
* single name returns single-status dict
* multiple names — each gets its own most-recent-match status
* substring (LIKE) semantics preserved (exact-name in path)
* "most recent wins" — when a name matches multiple rows, the row
  with the most recent ``synced_at`` is the one returned
* unresolved names land as ``None`` (legacy parity)
* exact query count: ONE statement against ``cloud_synced_files`` per
  call regardless of N
* a row that matches multiple names independently populates each name
  (not "first name wins for the row")
* DB error → empty-skeleton dict (best-effort, UI degrades gracefully)

Bonus: the per-row scan stops once every name has been resolved.
"""

from __future__ import annotations

import sqlite3
from typing import List
from unittest.mock import patch

import pytest

from services import cloud_archive_service as svc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_cloud_db(tmp_path) -> sqlite3.Connection:
    db_path = str(tmp_path / "cloud_sync.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(svc._CLOUD_TABLES_SQL)
    return conn


def _seed(conn, rows):
    """rows: (file_path, status, synced_at)"""
    for fp, st, sa in rows:
        conn.execute(
            "INSERT INTO cloud_synced_files (file_path, status, synced_at) "
            "VALUES (?, ?, ?)",
            (fp, st, sa),
        )
    conn.commit()


@pytest.fixture
def cloud_db(tmp_path, monkeypatch):
    """Build a real cloud_sync.db on disk and point the service at it
    so ``_init_cloud_tables`` returns this DB on connect.
    """
    db_path = str(tmp_path / "cloud_sync.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(svc._CLOUD_TABLES_SQL)
    conn.commit()
    conn.close()
    monkeypatch.setattr(svc, "CLOUD_ARCHIVE_DB_PATH", db_path, raising=True)
    return db_path


def _conn_for(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Behaviour tests
# ---------------------------------------------------------------------------

class TestGetSyncStatusBatched:

    def test_empty_input_returns_empty(self, cloud_db):
        assert svc.get_sync_status_for_events([]) == {}

    def test_single_name_status(self, cloud_db):
        c = _conn_for(cloud_db)
        try:
            _seed(c, [
                ("SentryClips/2026-05-12_10-00-00", "synced", "2026-05-12T11:00:00"),
            ])
        finally:
            c.close()

        out = svc.get_sync_status_for_events(["2026-05-12_10-00-00"])
        assert out == {"2026-05-12_10-00-00": "synced"}

    def test_multiple_names_each_get_own_status(self, cloud_db):
        c = _conn_for(cloud_db)
        try:
            _seed(c, [
                ("SentryClips/2026-05-12_10-00-00", "synced", "2026-05-12T11:00:00"),
                ("SentryClips/2026-05-12_11-00-00", "pending", "2026-05-12T11:30:00"),
                ("SavedClips/2026-05-12_12-00-00", "failed", "2026-05-12T12:30:00"),
            ])
        finally:
            c.close()

        out = svc.get_sync_status_for_events([
            "2026-05-12_10-00-00",
            "2026-05-12_11-00-00",
            "2026-05-12_12-00-00",
        ])
        assert out == {
            "2026-05-12_10-00-00": "synced",
            "2026-05-12_11-00-00": "pending",
            "2026-05-12_12-00-00": "failed",
        }

    def test_unknown_name_is_none(self, cloud_db):
        # Empty DB — every requested name lands as None.
        out = svc.get_sync_status_for_events([
            "2026-05-12_10-00-00",
            "2026-05-12_11-00-00",
        ])
        assert out == {
            "2026-05-12_10-00-00": None,
            "2026-05-12_11-00-00": None,
        }

    def test_most_recent_match_wins(self, cloud_db):
        # The same event has two cloud rows (e.g. two clips inside the
        # event dir). The MOST RECENT synced_at must be the returned
        # status — same semantics as the legacy LIMIT 1 ORDER BY DESC.
        c = _conn_for(cloud_db)
        try:
            _seed(c, [
                ("SentryClips/2026-05-12_10-00-00/front.mp4", "failed",
                 "2026-05-12T11:00:00"),
                ("SentryClips/2026-05-12_10-00-00/back.mp4", "synced",
                 "2026-05-12T12:00:00"),  # newer
            ])
        finally:
            c.close()

        out = svc.get_sync_status_for_events(["2026-05-12_10-00-00"])
        assert out == {"2026-05-12_10-00-00": "synced"}

    def test_substring_match_semantics_preserved(self, cloud_db):
        # The legacy LIKE '%name%' means a name is matched anywhere in
        # file_path. Verify that's still true (e.g. matches inside a
        # nested SentryClips path).
        c = _conn_for(cloud_db)
        try:
            _seed(c, [
                ("SentryClips/2026-05-12_10-00-00/front-2026-05-12_10-00-15.mp4",
                 "synced", "2026-05-12T11:00:00"),
            ])
        finally:
            c.close()

        out = svc.get_sync_status_for_events(["2026-05-12_10-00-00"])
        assert out == {"2026-05-12_10-00-00": "synced"}

    def test_db_error_returns_skeleton(self, cloud_db, monkeypatch):
        # Simulate a SQL error during the batched query — function must
        # not 500; UI gets the empty-skeleton dict instead.
        class _FailingConn:
            def __init__(self, real):
                self._real = real
                self.row_factory = real.row_factory

            def execute(self, sql, *args, **kwargs):
                if "cloud_synced_files" in sql and "WHERE file_path LIKE" in sql:
                    raise sqlite3.OperationalError("simulated DB busy")
                return self._real.execute(sql, *args, **kwargs)

            def close(self):
                self._real.close()

        original_init = svc._init_cloud_tables

        def fake_init(path):
            return _FailingConn(original_init(path))

        monkeypatch.setattr(svc, "_init_cloud_tables", fake_init, raising=True)

        out = svc.get_sync_status_for_events([
            "2026-05-12_10-00-00",
            "2026-05-12_11-00-00",
        ])
        # Skeleton with all values None.
        assert out == {
            "2026-05-12_10-00-00": None,
            "2026-05-12_11-00-00": None,
        }


# ---------------------------------------------------------------------------
# Single-query invariant — Phase 5.5 contract
# ---------------------------------------------------------------------------

class TestSingleQueryInvariant:

    def test_one_query_against_cloud_synced_files_for_30_names(
        self, cloud_db, monkeypatch,
    ):
        """The win in Phase 5.5: regardless of how many event names the
        UI passes, ``get_sync_status_for_events`` issues exactly ONE
        ``SELECT ... FROM cloud_synced_files`` query. The legacy code
        issued one per name (30 names → 30 round-trips)."""
        # Seed 5 of the 30 names so we exercise the matching code too.
        names = [f"2026-05-12_10-{i:02d}-00" for i in range(30)]
        c = _conn_for(cloud_db)
        try:
            _seed(c, [
                (f"SentryClips/{names[i]}/front.mp4", "synced",
                 f"2026-05-12T11:{i:02d}:00")
                for i in range(5)
            ])
        finally:
            c.close()

        # Wrap the connection returned by _init_cloud_tables so we can
        # trace .execute() without monkeypatching the immutable
        # sqlite3.Connection class.
        executed_sql: List[str] = []

        class _TracingConn:
            def __init__(self, real):
                self._real = real
                self.row_factory = real.row_factory

            def execute(self, sql, *args, **kwargs):
                executed_sql.append(sql)
                return self._real.execute(sql, *args, **kwargs)

            def close(self):
                self._real.close()

        original_init = svc._init_cloud_tables
        monkeypatch.setattr(
            svc, "_init_cloud_tables",
            lambda p: _TracingConn(original_init(p)),
            raising=True,
        )

        out = svc.get_sync_status_for_events(names)

        # Sanity: matched names came back with their statuses.
        for i in range(5):
            assert out[names[i]] == "synced", f"Name {names[i]} should be synced"
        for i in range(5, 30):
            assert out[names[i]] is None, f"Name {names[i]} should be None"

        # Count queries hitting cloud_synced_files. The Phase 5.5
        # contract is ONE — schema/setup queries are allowed (DDL,
        # PRAGMA, sqlite_master) but data queries are not.
        cloud_data_queries = [
            sql for sql in executed_sql
            if "FROM cloud_synced_files" in sql
        ]
        assert len(cloud_data_queries) == 1, (
            f"Expected exactly 1 query against cloud_synced_files for 30 "
            f"names (Phase 5.5 batched), got {len(cloud_data_queries)}. "
            f"Legacy per-name loop must not be reintroduced.\n"
            f"Queries: {cloud_data_queries}"
        )

    def test_query_count_constant_in_input_size(self, cloud_db, monkeypatch):
        """Tripwire: the number of cloud_synced_files queries must be
        the same whether we ask about 1 name or 100 names."""
        original_init = svc._init_cloud_tables

        def count_for(names):
            calls: List[str] = []

            class _TracingConn:
                def __init__(self, real):
                    self._real = real
                    self.row_factory = real.row_factory

                def execute(self, sql, *args, **kwargs):
                    calls.append(sql)
                    return self._real.execute(sql, *args, **kwargs)

                def close(self):
                    self._real.close()

            with monkeypatch.context() as mp:
                mp.setattr(
                    svc, "_init_cloud_tables",
                    lambda p: _TracingConn(original_init(p)),
                    raising=True,
                )
                svc.get_sync_status_for_events(names)
            return sum(1 for sql in calls if "FROM cloud_synced_files" in sql)

        n1 = count_for(["2026-05-12_10-00-00"])
        n100 = count_for([f"2026-05-12_10-{i:02d}-{j:02d}"
                          for i in range(10) for j in range(10)])
        assert n1 == n100, (
            f"Query count grew with input size: 1-name={n1}, 100-name={n100}. "
            f"Phase 5.5 requires a single batched query."
        )


# ---------------------------------------------------------------------------
# Cross-row matching — multiple names hitting the same row
# ---------------------------------------------------------------------------

class TestCrossRowMatching:

    def test_row_matching_two_names_populates_both(self, cloud_db):
        # An ``ArchivedClips`` filename like
        # ``2026-05-12_10-00-00-2026-05-12_10-00-15-front.mp4`` could in
        # theory match two timestamp queries. The function must
        # populate BOTH names independently — not stop at the first.
        c = _conn_for(cloud_db)
        try:
            _seed(c, [
                ("ArchivedClips/2026-05-12_10-00-00-2026-05-12_10-00-15-front.mp4",
                 "synced", "2026-05-12T11:00:00"),
            ])
        finally:
            c.close()

        out = svc.get_sync_status_for_events([
            "2026-05-12_10-00-00",
            "2026-05-12_10-00-15",
        ])
        assert out == {
            "2026-05-12_10-00-00": "synced",
            "2026-05-12_10-00-15": "synced",
        }
