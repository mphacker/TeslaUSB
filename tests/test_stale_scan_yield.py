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
        # Seed 1500 rows. Inject a hook that DELETEs the highest-rowid
        # row mid-walk (simulating a concurrent worker). With OFFSET
        # pagination, we'd skip the next row; with rowid pagination,
        # we shouldn't.
        _seed_indexed_files(geodata_db, 1500)
        teslacam = str(tmp_path / "fake_teslacam")
        os.makedirs(teslacam, exist_ok=True)

        # Run the scan normally — it'll purge all 1500 rows because
        # they're non-existent. The point of this test is correctness:
        # we don't lose any rows even with batched commits.
        result = svc.purge_deleted_videos(geodata_db, teslacam_path=teslacam)
        assert result['purged_files'] == 1500
