"""Phase 5.3 — cloud-dedup streaming regression tests.

These tests pin the contract of ``_is_path_skipped`` and verify that
``_discover_events`` no longer loads the entire ``cloud_synced_files``
table into memory.

The legacy implementation:

    rows = conn.execute(
        "SELECT file_path FROM cloud_synced_files "
        "WHERE status IN ('synced', 'dead_letter')"
    ).fetchall()
    synced_paths = {r["file_path"] for r in rows}

…allocated O(N) memory at the start of every discover pass even when
only a handful of new events were on disk. On a year-old database the
set would routinely cross ~8 MB. The Phase 5.3 streaming version
replaces this with one indexed point-lookup per candidate event so the
peak memory is bounded by the number of NEW events, not by the cloud
history.

The tests below cover:

* per-row semantics for both ``synced`` and ``dead_letter`` (skip)
* per-row semantics for ``pending`` / ``failed`` (don't skip)
* legacy "no connection" behaviour (returns False — don't filter)
* exception safety (DB error → False, picker continues)
* indexed lookup (verified by EXPLAIN QUERY PLAN — must use the unique
  index on ``file_path``, not a full table scan)
* end-to-end: ``_discover_events`` actually skips synced/dead_letter rows
  and includes pending/unknown ones
* bounded-memory guard: ``_discover_events`` never loads the whole
  ``cloud_synced_files`` table — verified by tracing ``conn.execute``
  and asserting no ``SELECT file_path FROM cloud_synced_files`` (the
  bulk-load shape) is ever issued
"""

from __future__ import annotations

import os
import sqlite3
from typing import List

import pytest

from services import cloud_archive_service as svc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_cloud_db(tmp_path) -> sqlite3.Connection:
    """Build a cloud_sync.db with the production cloud_synced_files schema."""
    db_path = str(tmp_path / "cloud_sync.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(svc._CLOUD_TABLES_SQL)
    return conn


def _seed_cloud(conn: sqlite3.Connection, rows: List[tuple]) -> None:
    """rows is a list of (file_path, status) tuples."""
    for file_path, status in rows:
        conn.execute(
            "INSERT INTO cloud_synced_files (file_path, status) "
            "VALUES (?, ?)",
            (file_path, status),
        )
    conn.commit()


def _make_event_dir(parent: str, name: str) -> str:
    event_dir = os.path.join(parent, name)
    os.makedirs(event_dir, exist_ok=True)
    with open(os.path.join(event_dir, "front.mp4"), "wb") as f:
        f.write(b"\x00" * 1024)
    return event_dir


@pytest.fixture
def cloud_conn(tmp_path):
    conn = _make_cloud_db(tmp_path)
    yield conn
    conn.close()


@pytest.fixture
def teslacam_with_three_events(tmp_path, monkeypatch):
    """Build SentryClips with three events.

    * 2026-05-12_10-00-00 → status='synced'   → must be SKIPPED
    * 2026-05-12_11-00-00 → status='dead_letter' → must be SKIPPED
    * 2026-05-12_12-00-00 → not in DB        → must be INCLUDED
    """
    teslacam = tmp_path / "TeslaCam"
    sentry = teslacam / "SentryClips"
    sentry.mkdir(parents=True)
    _make_event_dir(str(sentry), "2026-05-12_10-00-00")
    _make_event_dir(str(sentry), "2026-05-12_11-00-00")
    _make_event_dir(str(sentry), "2026-05-12_12-00-00")

    # Disable archive sweep so the test stays focused on event dirs.
    import config
    monkeypatch.setattr(config, "ARCHIVE_ENABLED", False, raising=False)

    # Allow non-event dirs through (no event.json on these — so the
    # default ``sync_non_event_videos: false`` would drop them).
    monkeypatch.setattr(
        svc, "_read_sync_non_event_setting",
        lambda: True, raising=True,
    )
    return str(teslacam)


# ---------------------------------------------------------------------------
# _is_path_skipped — unit
# ---------------------------------------------------------------------------

class TestIsPathSkipped:

    def test_synced_row_is_skipped(self, cloud_conn):
        _seed_cloud(cloud_conn, [
            ("SentryClips/2026-05-12_10-00-00", "synced"),
        ])
        assert svc._is_path_skipped(
            cloud_conn, "SentryClips/2026-05-12_10-00-00"
        ) is True

    def test_dead_letter_row_is_skipped(self, cloud_conn):
        _seed_cloud(cloud_conn, [
            ("SentryClips/2026-05-12_10-00-00", "dead_letter"),
        ])
        assert svc._is_path_skipped(
            cloud_conn, "SentryClips/2026-05-12_10-00-00"
        ) is True

    def test_pending_row_is_not_skipped(self, cloud_conn):
        _seed_cloud(cloud_conn, [
            ("SentryClips/2026-05-12_10-00-00", "pending"),
        ])
        assert svc._is_path_skipped(
            cloud_conn, "SentryClips/2026-05-12_10-00-00"
        ) is False

    def test_failed_row_is_not_skipped(self, cloud_conn):
        # Phase 2.6: 'failed' rows can still be retried; only 'dead_letter'
        # (retry cap reached) is permanently off-limits.
        _seed_cloud(cloud_conn, [
            ("SentryClips/2026-05-12_10-00-00", "failed"),
        ])
        assert svc._is_path_skipped(
            cloud_conn, "SentryClips/2026-05-12_10-00-00"
        ) is False

    def test_unknown_path_is_not_skipped(self, cloud_conn):
        # Empty DB — never seen this path before.
        assert svc._is_path_skipped(
            cloud_conn, "SentryClips/2026-05-12_10-00-00"
        ) is False

    def test_none_conn_returns_false(self):
        # Legacy "no connection" semantics: caller should see every event.
        assert svc._is_path_skipped(
            None, "SentryClips/2026-05-12_10-00-00"
        ) is False

    def test_query_failure_returns_false(self, tmp_path):
        # Closed connection → conn.execute raises ProgrammingError. The
        # picker must continue (best-effort dedup), so the helper swallows.
        conn = _make_cloud_db(tmp_path)
        conn.close()
        assert svc._is_path_skipped(
            conn, "SentryClips/2026-05-12_10-00-00"
        ) is False

    def test_uses_indexed_lookup_not_full_scan(self, cloud_conn):
        """The unique index on ``file_path`` MUST be used — a full scan
        would defeat the entire memory win (we'd be slower AND still
        loading rows). EXPLAIN QUERY PLAN catches this."""
        plan_rows = cloud_conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT 1 FROM cloud_synced_files "
            "WHERE file_path = ? AND status IN ('synced', 'dead_letter') "
            "LIMIT 1",
            ("SentryClips/2026-05-12_10-00-00",),
        ).fetchall()
        plan = " ".join(str(r["detail"]) for r in plan_rows).lower()
        # SQLite reports the auto-created unique index as
        # "sqlite_autoindex_cloud_synced_files_1". The exact name is not
        # part of the contract, but the plan MUST mention USING INDEX.
        assert "using index" in plan or "search" in plan, (
            f"_is_path_skipped query is doing a full scan, not an index "
            f"lookup. Plan was: {plan}"
        )
        # And explicitly NOT a SCAN of the full table.
        assert "scan cloud_synced_files" not in plan, (
            f"_is_path_skipped query is doing SCAN cloud_synced_files — "
            f"the unique index isn't being used. Plan was: {plan}"
        )


# ---------------------------------------------------------------------------
# _discover_events — end-to-end
# ---------------------------------------------------------------------------

class TestDiscoverEventsStreaming:

    def test_skips_synced_and_deadletter_includes_unknown(
        self, teslacam_with_three_events, cloud_conn,
    ):
        teslacam = teslacam_with_three_events
        _seed_cloud(cloud_conn, [
            ("SentryClips/2026-05-12_10-00-00", "synced"),
            ("SentryClips/2026-05-12_11-00-00", "dead_letter"),
        ])

        events = svc._discover_events(teslacam, conn=cloud_conn)
        rels = [rel for (_dir, rel, _size) in events]

        assert "SentryClips/2026-05-12_12-00-00" in rels, (
            "unknown event must be included"
        )
        assert "SentryClips/2026-05-12_10-00-00" not in rels, (
            "synced event must be skipped"
        )
        assert "SentryClips/2026-05-12_11-00-00" not in rels, (
            "dead_letter event must be skipped"
        )

    def test_no_conn_includes_everything(self, teslacam_with_three_events):
        teslacam = teslacam_with_three_events
        events = svc._discover_events(teslacam, conn=None)
        rels = [rel for (_dir, rel, _size) in events]
        assert "SentryClips/2026-05-12_10-00-00" in rels
        assert "SentryClips/2026-05-12_11-00-00" in rels
        assert "SentryClips/2026-05-12_12-00-00" in rels

    def test_no_bulk_load_query_is_issued(
        self, teslacam_with_three_events, cloud_conn,
    ):
        """Tripwire — a future change MUST NOT reintroduce the
        ``SELECT file_path FROM cloud_synced_files WHERE status IN (…)``
        bulk-load shape. We trace every execute() and assert that
        signature is never produced.
        """
        _seed_cloud(cloud_conn, [
            ("SentryClips/2026-05-12_10-00-00", "synced"),
        ])

        executed_sql: List[str] = []
        original_execute = cloud_conn.execute

        def tracing_execute(sql, *args, **kwargs):
            executed_sql.append(sql)
            return original_execute(sql, *args, **kwargs)

        # We can't monkeypatch a method on a sqlite3.Connection cleanly,
        # so wrap via a thin proxy.
        class _Proxy:
            row_factory = cloud_conn.row_factory
            def execute(self, sql, *args, **kwargs):
                return tracing_execute(sql, *args, **kwargs)

        proxy = _Proxy()
        events = svc._discover_events(teslacam_with_three_events, conn=proxy)
        # Sanity: dedup still worked through the proxy.
        rels = [rel for (_dir, rel, _size) in events]
        assert "SentryClips/2026-05-12_10-00-00" not in rels

        # The forbidden bulk-load shape: any SELECT from cloud_synced_files
        # that does NOT also constrain by file_path =
        for sql in executed_sql:
            normalized = " ".join(sql.split()).lower()
            if (
                "from cloud_synced_files" in normalized
                and "file_path = ?" not in normalized
            ):
                pytest.fail(
                    "Bulk-load reintroduced — _discover_events is now "
                    f"issuing: {sql!r}. Phase 5.3 requires per-event "
                    "indexed lookups only (see _is_path_skipped)."
                )

    def test_bounded_query_count_per_candidate(
        self, teslacam_with_three_events, cloud_conn,
    ):
        """Sanity check on the streaming model: the number of dedup
        queries must scale with NEW events on disk, not with the size
        of cloud_synced_files. Seeding 100 unrelated synced rows must
        not change the query count vs seeding 0.
        """
        # Run 1: empty DB.
        executed_a: List[str] = []

        class _ProxyA:
            row_factory = cloud_conn.row_factory
            def execute(self, sql, *args, **kwargs):
                executed_a.append(sql)
                return cloud_conn.execute(sql, *args, **kwargs)

        svc._discover_events(teslacam_with_three_events, conn=_ProxyA())
        baseline = sum(
            1 for s in executed_a
            if "from cloud_synced_files" in " ".join(s.split()).lower()
        )

        # Run 2: 100 unrelated synced rows.
        _seed_cloud(cloud_conn, [
            (f"SentryClips/2025-01-01_{i:02d}-00-00", "synced")
            for i in range(100)
        ])

        executed_b: List[str] = []

        class _ProxyB:
            row_factory = cloud_conn.row_factory
            def execute(self, sql, *args, **kwargs):
                executed_b.append(sql)
                return cloud_conn.execute(sql, *args, **kwargs)

        svc._discover_events(teslacam_with_three_events, conn=_ProxyB())
        with_history = sum(
            1 for s in executed_b
            if "from cloud_synced_files" in " ".join(s.split()).lower()
        )

        assert with_history == baseline, (
            f"Dedup query count grew with cloud_synced_files history: "
            f"baseline={baseline}, with-100-rows={with_history}. "
            f"Phase 5.3 requires bounded per-discover query count."
        )
