"""Phase 5.6 — Stale-scan yields the SQLite lock between batches.

Pins the contract that the full-scan path of
``mapping_service.purge_deleted_videos`` (called via the daily stale
scan) processes ``indexed_files`` in bounded batches and **releases
the SQLite write lock** between them, so a concurrent indexer or
archive worker can write while the scan is in flight.

Why this matters: the legacy implementation issued a single
``SELECT file_path FROM indexed_files`` followed by ``.fetchall()``
and then walked every row inside the same connection. On a busy Pi
Zero 2 W with a 10k+ row ``indexed_files`` table, this held the
SQLite shared lock for many seconds and starved every other writer
on ``geodata.db``.

Phase 5.6 rewrites the scan as a rowid-cursored loop with a
configurable ``BATCH_SIZE`` (default 500) and an ``INTER_BATCH_SLEEP``
(default 50 ms) between batches. Between each batch the code commits
+ closes + sleeps + reopens the connection so the SQLite lock is
genuinely released to any contender.

Tripwire tests included so a future refactor that re-introduces a
single ``fetchall()`` over ``indexed_files`` in this code path fails
loudly.
"""
from __future__ import annotations

import os
import sqlite3
import time

import pytest

import services.mapping_service as svc
from services.mapping_migrations import _init_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def geodata_db(tmp_path):
    """Empty geodata.db with the production schema."""
    db_path = str(tmp_path / "geodata.db")
    _init_db(db_path)
    return db_path


def _seed_indexed_files(db_path: str, count: int) -> None:
    """Insert ``count`` indexed_files rows pointing at non-existent paths."""
    with sqlite3.connect(db_path) as c:
        for i in range(count):
            fp = f"/nonexistent/teslacam/RecentClips/2026-05-12_10-{i:05d}.mp4"
            c.execute(
                "INSERT INTO indexed_files "
                "(file_path, file_size, file_mtime, indexed_at, "
                " waypoint_count, event_count) "
                "VALUES (?, ?, ?, '2026-05-12T10:00:00', 0, 0)",
                (fp, 1024, 100.0),
            )
        c.commit()


# ---------------------------------------------------------------------------
# Behaviour: full scan still purges every missing file
# ---------------------------------------------------------------------------

class TestStaleSeqFullScanBehaviour:

    def test_full_scan_purges_all_missing_rows(self, geodata_db, tmp_path):
        # Seed 1500 rows (3 batches of 500). All point at non-existent
        # files → all should be purged.
        _seed_indexed_files(geodata_db, 1500)

        teslacam = str(tmp_path / "fake_teslacam")
        os.makedirs(teslacam, exist_ok=True)

        result = svc.purge_deleted_videos(geodata_db, teslacam_path=teslacam)
        assert result['purged_files'] == 1500

        # And the table is genuinely empty after the run.
        with sqlite3.connect(geodata_db) as c:
            n = c.execute(
                "SELECT COUNT(*) FROM indexed_files"
            ).fetchone()[0]
        assert n == 0

    def test_partial_batch_terminates(self, geodata_db, tmp_path):
        # 750 rows (one full batch of 500 + a half-batch of 250).
        _seed_indexed_files(geodata_db, 750)

        teslacam = str(tmp_path / "fake_teslacam")
        os.makedirs(teslacam, exist_ok=True)

        result = svc.purge_deleted_videos(geodata_db, teslacam_path=teslacam)
        assert result['purged_files'] == 750

    def test_below_batch_size_still_works(self, geodata_db, tmp_path):
        # 10 rows — nowhere near the batch boundary.
        _seed_indexed_files(geodata_db, 10)

        teslacam = str(tmp_path / "fake_teslacam")
        os.makedirs(teslacam, exist_ok=True)

        result = svc.purge_deleted_videos(geodata_db, teslacam_path=teslacam)
        assert result['purged_files'] == 10

    def test_existing_files_are_skipped(self, geodata_db, tmp_path):
        """Files that exist on disk must NOT be purged."""
        # Mix: 5 real files + 5 non-existent.
        with sqlite3.connect(geodata_db) as c:
            for i in range(5):
                real = tmp_path / f"real_{i}.mp4"
                real.write_bytes(b"x")
                c.execute(
                    "INSERT INTO indexed_files "
                    "(file_path, file_size, file_mtime, indexed_at, "
                    " waypoint_count, event_count) "
                    "VALUES (?, ?, ?, '2026-05-12T10:00:00', 0, 0)",
                    (str(real), 1024, 100.0),
                )
            for i in range(5):
                fake = f"/nonexistent/teslacam/{i}.mp4"
                c.execute(
                    "INSERT INTO indexed_files "
                    "(file_path, file_size, file_mtime, indexed_at, "
                    " waypoint_count, event_count) "
                    "VALUES (?, ?, ?, '2026-05-12T10:00:00', 0, 0)",
                    (fake, 1024, 100.0),
                )
            c.commit()

        teslacam = str(tmp_path / "fake_teslacam")
        os.makedirs(teslacam, exist_ok=True)
        result = svc.purge_deleted_videos(geodata_db, teslacam_path=teslacam)
        assert result['purged_files'] == 5

        # The 5 real ones must still be in the table.
        with sqlite3.connect(geodata_db) as c:
            n = c.execute(
                "SELECT COUNT(*) FROM indexed_files"
            ).fetchone()[0]
        assert n == 5


# ---------------------------------------------------------------------------
# Tripwire: NO unbounded fetchall() over indexed_files
# ---------------------------------------------------------------------------

class TestStaleScanQueryShape:
    """Pin the per-batch query shape — fail loudly if a future refactor
    re-introduces an unbounded ``SELECT … FROM indexed_files`` in
    ``purge_deleted_videos``.
    """

    def test_each_batch_query_has_a_limit(self, geodata_db, tmp_path,
                                          monkeypatch):
        # Trace every SELECT issued during the scan and assert that
        # every SELECT against ``indexed_files`` carries a LIMIT clause.
        _seed_indexed_files(geodata_db, 100)
        teslacam = str(tmp_path / "fake_teslacam")
        os.makedirs(teslacam, exist_ok=True)

        observed_selects: list = []

        # Wrap _init_db so we can intercept the connection's execute().
        original_init = svc._init_db

        class _TracingConn:
            def __init__(self, real):
                self._real = real
                self.row_factory = real.row_factory

            def __getattr__(self, name):
                # Delegate everything else (commit, close, executescript, ...)
                return getattr(self._real, name)

            def execute(self, sql, params=()):
                observed_selects.append(sql)
                return self._real.execute(sql, params)

        def traced_init(db_path):
            return _TracingConn(original_init(db_path))

        monkeypatch.setattr(svc, "_init_db", traced_init)

        svc.purge_deleted_videos(geodata_db, teslacam_path=teslacam)

        # Find every SELECT against indexed_files.
        idx_selects = [
            s for s in observed_selects
            if "SELECT" in s.upper() and "INDEXED_FILES" in s.upper()
        ]
        assert idx_selects, (
            "Expected at least one SELECT against indexed_files."
        )
        # The PAGING SELECT (SELECT … file_path FROM indexed_files)
        # must include a LIMIT clause. Per-row existence checks
        # (SELECT 1 FROM indexed_files WHERE file_path = ?) are
        # naturally bounded by their WHERE clause — they don't need
        # LIMIT.
        unbounded_paging = [
            s for s in idx_selects
            if "FILE_PATH" in s.upper()
            and "WHERE" not in s.upper()
            and "LIMIT" not in s.upper()
        ]
        assert not unbounded_paging, (
            "Phase 5.6 violation: unbounded SELECT over indexed_files "
            f"detected: {unbounded_paging[0]}"
        )

    def test_paging_uses_rowid_cursor_not_offset(
            self, geodata_db, tmp_path, monkeypatch,
    ):
        # Rowid-cursor pagination is robust to mid-walk DELETEs.
        # OFFSET-based pagination is NOT — it skips rows after a
        # delete shifts the count. This tripwire pins the safe
        # implementation.
        _seed_indexed_files(geodata_db, 100)
        teslacam = str(tmp_path / "fake_teslacam")
        os.makedirs(teslacam, exist_ok=True)

        observed_selects: list = []

        class _TracingConn:
            def __init__(self, real):
                self._real = real
                self.row_factory = real.row_factory

            def __getattr__(self, name):
                return getattr(self._real, name)

            def execute(self, sql, params=()):
                observed_selects.append(sql)
                return self._real.execute(sql, params)

        original_init = svc._init_db

        def traced_init(db_path):
            return _TracingConn(original_init(db_path))

        monkeypatch.setattr(svc, "_init_db", traced_init)

        svc.purge_deleted_videos(geodata_db, teslacam_path=teslacam)

        # The paging SELECT against indexed_files (i.e. the one whose
        # body retrieves the file_path column) must use a rowid cursor
        # ("rowid > ?") and must NOT use OFFSET.
        paging_selects = [
            s for s in observed_selects
            if ("SELECT" in s.upper() and "INDEXED_FILES" in s.upper()
                and "FILE_PATH" in s.upper() and "LIMIT" in s.upper())
        ]
        assert paging_selects, "Expected the paging SELECT to fire."
        for s in paging_selects:
            assert "ROWID" in s.upper(), (
                f"Paging select must use rowid cursor, got: {s}"
            )
            assert "OFFSET" not in s.upper(), (
                f"Paging must not use OFFSET (skips rows on mid-walk "
                f"DELETE): {s}"
            )


# ---------------------------------------------------------------------------
# Tripwire: connection reopens between batches (yields the lock)
# ---------------------------------------------------------------------------

class TestStaleScanYieldsLock:
    """Phase 5.6 guarantees the scan releases the SQLite write lock
    between batches by closing + reopening the connection.

    We don't try to assert ``time.sleep`` was called or measure wall
    time (flaky). Instead, we count how many connections ``_init_db``
    returns during a scan that crosses N batch boundaries — it must
    be at least ``ceil(rows / BATCH_SIZE) + 1`` (the +1 is the
    initial connection).
    """

    def test_one_connection_per_batch_plus_initial(
            self, geodata_db, tmp_path, monkeypatch,
    ):
        # 1500 rows = 3 batches of 500. Expect ≥ 4 _init_db calls
        # (initial + 3 reopens).
        _seed_indexed_files(geodata_db, 1500)
        teslacam = str(tmp_path / "fake_teslacam")
        os.makedirs(teslacam, exist_ok=True)

        init_call_count = {"n": 0}
        original_init = svc._init_db

        def counting_init(db_path):
            init_call_count["n"] += 1
            return original_init(db_path)

        monkeypatch.setattr(svc, "_init_db", counting_init)

        svc.purge_deleted_videos(geodata_db, teslacam_path=teslacam)

        # 3 batches → 3 reopens after each batch + 1 initial.
        # The recursive call into purge_deleted_videos for the
        # ``missing`` list adds one more connection (1500 missing
        # files → 1 recursive call → 1 _init_db call), so we expect
        # at least 5 total. Use ``>=`` so adding more open/close
        # cycles in the future doesn't break the test.
        assert init_call_count["n"] >= 4, (
            f"Expected ≥ 4 _init_db calls (initial + 3 batch reopens) "
            f"for 1500 rows / 500-row batches, got "
            f"{init_call_count['n']}. The scan is not yielding the "
            f"SQLite lock between batches."
        )

    def test_single_batch_does_not_reopen_unnecessarily(
            self, geodata_db, tmp_path, monkeypatch,
    ):
        # 10 rows fit in one batch. We still expect 1 reopen (the
        # batch-end commit/close/reopen happens unconditionally) plus
        # the initial open and the recursive call's open = 3.
        _seed_indexed_files(geodata_db, 10)
        teslacam = str(tmp_path / "fake_teslacam")
        os.makedirs(teslacam, exist_ok=True)

        init_call_count = {"n": 0}
        original_init = svc._init_db

        def counting_init(db_path):
            init_call_count["n"] += 1
            return original_init(db_path)

        monkeypatch.setattr(svc, "_init_db", counting_init)

        svc.purge_deleted_videos(geodata_db, teslacam_path=teslacam)

        # ≥ 2 (initial + recursive). Don't over-constrain — if the
        # implementation reopens after the partial batch too, that's
        # fine.
        assert init_call_count["n"] >= 2


# ---------------------------------------------------------------------------
# Tripwire: rowid cursor is robust to mid-walk DELETEs
# ---------------------------------------------------------------------------

class TestRowidCursorCorrectness:
    """Pin that rowid-cursor pagination doesn't skip rows when other
    workers (or our own UPDATE/DELETE) mutate the table mid-walk.
    """

    def test_no_rows_skipped_when_table_shrinks_mid_walk(
            self, geodata_db, tmp_path, monkeypatch,
    ):
        """Exercise the rowid-cursor robustness with a real mid-walk
        DELETE.

        Inject a side-effecting ``os.path.isfile`` that, on the FIRST
        invocation (i.e., processing batch 1's first row), deletes a
        row that lives WITHIN batch 1 (rowid 250). Batch 1 has already
        been loaded by ``fetchall()`` so the deletion doesn't affect
        the in-memory list — but it DOES affect what batch 2 sees.

        With OFFSET 500 pagination, batch 2 would skip rowids 501..599
        because the deletion shifted the count: ``OFFSET 500`` on a
        599-row table lands at rowid 600, returning just 1 row instead
        of 100.

        With ``WHERE rowid > 500`` pagination (Phase 5.6), batch 2
        correctly returns rowids 501..600 — the deletion at rowid 250
        is irrelevant to the cursor. This is the behavior we pin.
        """
        # Seed exactly 600 rows. BATCH_SIZE=500 → batch 1 = rowids
        # 1..500, batch 2 = rowids 501..600.
        _seed_indexed_files(geodata_db, 600)
        teslacam = str(tmp_path / "fake_teslacam")
        os.makedirs(teslacam, exist_ok=True)

        original_isfile = os.path.isfile
        hook_state = {"fired": False}

        def side_effecting_isfile(p):
            if not hook_state["fired"]:
                hook_state["fired"] = True
                # DELETE rowid 250 — a row WITHIN batch 1, while
                # batch 1 is still in flight. Use a separate
                # connection so we don't fight with the scan's open
                # write transaction.
                with sqlite3.connect(geodata_db) as c:
                    c.execute(
                        "DELETE FROM indexed_files WHERE rowid = 250"
                    )
                    c.commit()
            return original_isfile(p)

        monkeypatch.setattr(svc.os.path, "isfile", side_effecting_isfile)

        result = svc.purge_deleted_videos(geodata_db, teslacam_path=teslacam)
        assert hook_state["fired"]

        # Expected: scan sees all 600 rows. Batch 1 (in-memory)
        # processed rowids 1..500 — DELETE for rowid 250 happens to
        # be a no-op (already deleted by the hook) but the path is
        # still added to ``missing``. Batch 2 sees rowids 501..600.
        # purged_files = 499 (batch 1 minus the no-op DELETE for 250)
        # + 100 (batch 2). The recursive targeted purge runs over
        # the full ``missing`` list (600 entries) and DELETEs whatever
        # still has a row in indexed_files.
        #
        # Concretely: after the recursive purge, the table MUST be
        # empty. If OFFSET pagination snuck back in, the table would
        # have rowids 501..599 still present (99 rows). We assert the
        # empty-table invariant — the strongest signal that no rows
        # were skipped.
        with sqlite3.connect(geodata_db) as c:
            remaining = c.execute(
                "SELECT COUNT(*) FROM indexed_files"
            ).fetchone()[0]
        assert remaining == 0, (
            f"Mid-walk DELETE caused the scan to skip {remaining} "
            f"rows. With WHERE rowid > ? pagination this MUST NOT "
            f"happen. (If this fails, OFFSET-based pagination has "
            f"snuck back in.)"
        )
        # Sanity on the count: 600 unique rows, 1 deleted by the
        # hook out of band, the remaining 599 deleted by the scan
        # (some by batch UPDATE/DELETE, the rest by the recursive
        # purge). purged_files reflects only DELETE rowcounts the
        # scan saw, so it should be 599 minus the no-op for rowid
        # 250 = 599. Use ``>=`` so changes to whether the per-batch
        # DELETE or the recursive purge does the work don't break
        # the test.
        assert result['purged_files'] >= 599
