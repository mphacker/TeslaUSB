"""Tests for indexing_queue_service.purge_orphaned_dead_letters.

Issue #110 — When the archive watchdog's retention prune deletes a
truncated archive copy, it cleans ``indexed_files`` via
``purge_deleted_videos`` but leaves orphaned dead-letter rows in
``indexing_queue``. Those rows linger forever, inflating
``dead_letter_count`` and showing stale paths in
``list_dead_letters``. The new helper sweeps them up.
"""
from __future__ import annotations

import sqlite3

import pytest

from services import indexing_queue_service
from services.indexing_queue_service import (
    _PARSE_ERROR_MAX_ATTEMPTS,
    enqueue_for_indexing,
    get_queue_status,
    purge_orphaned_dead_letters,
)
from services.mapping_service import _init_db


@pytest.fixture
def db(tmp_path):
    db_path = str(tmp_path / "geodata.db")
    conn = _init_db(db_path)
    conn.close()
    return db_path


def _make_clip(tmp_path, name: str) -> str:
    f = tmp_path / f"{name}-front.mp4"
    f.write_bytes(b"x")
    return str(f)


def _force_dead_letter(db_path: str, file_path: str) -> None:
    """Bump a queue row to dead-letter (attempts >= _PARSE_ERROR_MAX_ATTEMPTS)
    by direct DB update, simulating the worker burning through retries."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE indexing_queue SET attempts = ?, last_error = ?, "
            "claimed_by = NULL, claimed_at = NULL "
            "WHERE file_path = ?",
            (_PARSE_ERROR_MAX_ATTEMPTS, "No mdat box found", file_path),
        )
        conn.commit()
    finally:
        conn.close()


class TestPurgeOrphanedDeadLetters:
    """Issue #110 — periodic cleanup of orphaned dead-letter rows."""

    def test_orphaned_dead_letter_row_is_purged(self, db, tmp_path):
        """Dead-letter row + missing source file -> purged."""
        clip = _make_clip(tmp_path, "orphan")
        assert enqueue_for_indexing(db, clip) is True
        _force_dead_letter(db, clip)
        # File deleted (simulates retention prune).
        import os
        os.remove(clip)
        assert os.path.isfile(clip) is False

        purged = purge_orphaned_dead_letters(db)
        assert purged == 1

        status = get_queue_status(db)
        assert status['dead_letter_count'] == 0
        assert status['queue_depth'] == 0

    def test_dead_letter_with_existing_file_is_preserved(self, db, tmp_path):
        """Dead-letter row + file STILL on disk -> preserved.

        Operator might fix the parser and re-process, or the file
        might become valid after a future write. Don't delete just
        because the row hit the attempt cap.
        """
        clip = _make_clip(tmp_path, "still_here")
        assert enqueue_for_indexing(db, clip) is True
        _force_dead_letter(db, clip)
        assert __import__('os').path.isfile(clip)  # still there

        purged = purge_orphaned_dead_letters(db)
        assert purged == 0

        status = get_queue_status(db)
        assert status['dead_letter_count'] == 1

    def test_non_dead_letter_with_missing_file_is_preserved(
        self, db, tmp_path,
    ):
        """attempts < _PARSE_ERROR_MAX_ATTEMPTS rows are NEVER touched.

        The worker's normal FILE_MISSING outcome will handle them on
        the next claim — that's its job, not the orphan sweeper's.
        Removing them here would risk racing the worker mid-claim.
        """
        clip = _make_clip(tmp_path, "live")
        assert enqueue_for_indexing(db, clip) is True
        # attempts stays at 0 — NOT dead-letter.
        import os
        os.remove(clip)

        purged = purge_orphaned_dead_letters(db)
        assert purged == 0

        status = get_queue_status(db)
        assert status['queue_depth'] == 1
        assert status['dead_letter_count'] == 0

    def test_claimed_dead_letter_is_preserved(self, db, tmp_path):
        """claimed_by IS NOT NULL rows are NEVER touched, even at the
        dead-letter threshold. The worker is mid-processing — let it
        finish."""
        clip = _make_clip(tmp_path, "claimed")
        assert enqueue_for_indexing(db, clip) is True
        _force_dead_letter(db, clip)
        # Manually mark claimed AFTER the dead-letter bump.
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "UPDATE indexing_queue SET claimed_by = 'w1', "
                "claimed_at = ? WHERE file_path = ?",
                (1234567890.0, clip),
            )
            conn.commit()
        finally:
            conn.close()
        import os
        os.remove(clip)

        purged = purge_orphaned_dead_letters(db)
        assert purged == 0  # claimed - skip even though file is gone

    def test_mixed_batch_purges_only_orphans(self, db, tmp_path):
        """Mix of orphans + alive files + non-dead-letter rows. Only
        the dead-letter+missing rows get purged."""
        # 5 orphans (dead-letter, file gone)
        orphans = [_make_clip(tmp_path, f"orphan_{i}") for i in range(5)]
        for o in orphans:
            assert enqueue_for_indexing(db, o) is True
            _force_dead_letter(db, o)
        # 3 alive (dead-letter, file present)
        alive = [_make_clip(tmp_path, f"alive_{i}") for i in range(3)]
        for a in alive:
            assert enqueue_for_indexing(db, a) is True
            _force_dead_letter(db, a)
        # 2 fresh (attempts=0)
        fresh = [_make_clip(tmp_path, f"fresh_{i}") for i in range(2)]
        for f in fresh:
            assert enqueue_for_indexing(db, f) is True

        # Delete just the orphans.
        import os
        for o in orphans:
            os.remove(o)

        purged = purge_orphaned_dead_letters(db)
        assert purged == 5

        status = get_queue_status(db)
        # 3 dead-letter alive + 2 fresh = 5 total
        assert status['dead_letter_count'] == 3
        assert status['queue_depth'] == 2

    def test_empty_queue_is_a_noop(self, db):
        purged = purge_orphaned_dead_letters(db)
        assert purged == 0

    def test_no_dead_letters_is_a_noop(self, db, tmp_path):
        clip = _make_clip(tmp_path, "fresh_only")
        assert enqueue_for_indexing(db, clip) is True
        purged = purge_orphaned_dead_letters(db)
        assert purged == 0

    def test_handles_missing_file_path_gracefully(self, db, tmp_path):
        """The current schema enforces NOT NULL on file_path, but the
        purge code defensively skips rows with falsy file_path
        (`if r['file_path'] and not os.path.isfile(...)`). This test
        sanity-checks that defensive code: an empty-string file_path
        (which the NOT NULL constraint allows) isn't treated as a
        missing-on-disk file — would otherwise raise OSError or
        produce a confusing DELETE."""
        import time as _time
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO indexing_queue "
                "(canonical_key, file_path, source, attempts, "
                "next_attempt_at, enqueued_at, last_error, "
                "claimed_by, claimed_at) "
                "VALUES (?, '', ?, ?, 0, ?, ?, NULL, NULL)",
                ("weird-key.mp4", "test", _PARSE_ERROR_MAX_ATTEMPTS,
                 _time.time(), "synthetic"),
            )
            conn.commit()
        finally:
            conn.close()

        # Defensive `if r['file_path']` check skips this row → 0.
        purged = purge_orphaned_dead_letters(db)
        assert purged == 0

    def test_idempotent(self, db, tmp_path):
        """Running purge twice in a row doesn't error and the second
        call is a no-op."""
        import os
        clip = _make_clip(tmp_path, "twice")
        assert enqueue_for_indexing(db, clip) is True
        _force_dead_letter(db, clip)
        os.remove(clip)

        first = purge_orphaned_dead_letters(db)
        second = purge_orphaned_dead_letters(db)
        assert first == 1
        assert second == 0

    def test_db_error_returns_zero(self, db, tmp_path, monkeypatch):
        """DB error during the SELECT phase returns 0, doesn't raise."""
        clip = _make_clip(tmp_path, "err")
        assert enqueue_for_indexing(db, clip) is True
        _force_dead_letter(db, clip)
        import os
        os.remove(clip)

        original_open = indexing_queue_service._open_queue_conn

        class _RaisingOnSelect:
            def __init__(self, real):
                self._c = real

            def __getattr__(self, name):
                return getattr(self._c, name)

            def execute(self, sql, *args):
                if 'SELECT' in sql.upper():
                    raise sqlite3.OperationalError("simulated select fail")
                return self._c.execute(sql, *args)

        monkeypatch.setattr(
            indexing_queue_service, '_open_queue_conn',
            lambda p: _RaisingOnSelect(original_open(p)),
        )
        purged = purge_orphaned_dead_letters(db)
        assert purged == 0
