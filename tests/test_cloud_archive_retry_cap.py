"""Tests for Phase 2.6 — cloud sync retry cap.

When a cloud upload fails, the worker increments ``retry_count`` and sets
``status='failed'``. The next sync iteration re-picks the row and tries
again. With NO upper bound on this loop, a permanently-broken clip
retries every cycle forever, eating bandwidth and log space and
preventing other rows from getting their fair share.

Phase 2.6 adds a configurable cap (``cloud_archive.retry_max_attempts``,
default 5, range 1-20). After the cap, the row is promoted to
``status='dead_letter'`` and excluded from auto-picking until manually
recovered (Failed Jobs page in Phase 4 or by direct DB edit).

These tests exercise:

* ``_mark_upload_failure`` — atomic increment + cap-based status promotion.
* ``_discover_events`` — dead_letter rows excluded from re-picking.
* ``_read_retry_max_attempts_setting`` — per-call YAML re-read, range
  clamping, and graceful fallback on IO errors.
* The Settings save path — drives the change through the real
  ``update_config_yaml`` so the contract "takes effect on next iteration
  without restart" is end-to-end verified.

Following the pattern from ``test_cloud_archive_non_event_filter.py``,
no in-memory module attribute is monkeypatched — the live YAML is the
single source of truth.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

import config
from helpers.config_updater import update_config_yaml
from services import cloud_archive_service as svc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_yaml(tmp_path, monkeypatch):
    """Point ``CONFIG_YAML`` at a writable per-test copy. Seeded with the
    Phase 2.6 default value so tests start from a known cap.
    """
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "cloud_archive:\n  retry_max_attempts: 5\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_YAML", str(yaml_path))
    import helpers.config_updater as cu
    monkeypatch.setattr(cu, "CONFIG_YAML", str(yaml_path))
    return str(yaml_path)


@pytest.fixture
def cloud_db(tmp_path):
    """Bare cloud_synced_files schema mirror for direct UPDATE/SELECT
    testing — avoids spinning up the full ``_initialize_db`` path which
    pulls in module-level state we don't want.
    """
    db_path = tmp_path / "cloud_sync.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE cloud_synced_files (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               file_path TEXT NOT NULL UNIQUE,
               file_size INTEGER,
               file_mtime REAL,
               remote_path TEXT,
               status TEXT DEFAULT 'pending',
               synced_at TEXT,
               retry_count INTEGER DEFAULT 0,
               last_error TEXT
           )"""
    )
    conn.commit()
    yield conn
    conn.close()


def _seed_row(conn, file_path: str, *, status: str = 'pending',
              retry_count: int = 0) -> None:
    conn.execute(
        """INSERT INTO cloud_synced_files
               (file_path, status, retry_count) VALUES (?, ?, ?)""",
        (file_path, status, retry_count),
    )
    conn.commit()


def _row(conn, file_path: str):
    return conn.execute(
        "SELECT status, retry_count, last_error FROM cloud_synced_files "
        "WHERE file_path = ?",
        (file_path,),
    ).fetchone()


# ---------------------------------------------------------------------------
# _read_retry_max_attempts_setting
# ---------------------------------------------------------------------------


class TestReadRetryMaxAttemptsSetting:
    """The picker / failure handler must honour live YAML changes."""

    def test_default_value_when_key_absent(self, tmp_path, monkeypatch):
        # YAML present but no retry_max_attempts key → import-time default.
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text("cloud_archive:\n  enabled: true\n",
                             encoding="utf-8")
        monkeypatch.setattr(config, "CONFIG_YAML", str(yaml_path))
        assert svc._read_retry_max_attempts_setting() == \
            svc.CLOUD_ARCHIVE_RETRY_MAX_ATTEMPTS

    def test_explicit_value_returned(self, isolated_yaml):
        update_config_yaml({'cloud_archive.retry_max_attempts': 10})
        assert svc._read_retry_max_attempts_setting() == 10

    def test_range_clamps_zero_to_default(self, isolated_yaml):
        # 0 would disable the cap — never allow that. Falls back to default.
        update_config_yaml({'cloud_archive.retry_max_attempts': 0})
        assert svc._read_retry_max_attempts_setting() == \
            svc.CLOUD_ARCHIVE_RETRY_MAX_ATTEMPTS

    def test_range_clamps_negative_to_default(self, isolated_yaml):
        update_config_yaml({'cloud_archive.retry_max_attempts': -1})
        assert svc._read_retry_max_attempts_setting() == \
            svc.CLOUD_ARCHIVE_RETRY_MAX_ATTEMPTS

    def test_range_clamps_above_max_to_default(self, isolated_yaml):
        # 21 is above _RETRY_MAX_ATTEMPTS_MAX (20) → fall back.
        update_config_yaml({'cloud_archive.retry_max_attempts': 1000})
        assert svc._read_retry_max_attempts_setting() == \
            svc.CLOUD_ARCHIVE_RETRY_MAX_ATTEMPTS

    def test_min_boundary_one_accepted(self, isolated_yaml):
        update_config_yaml({'cloud_archive.retry_max_attempts': 1})
        assert svc._read_retry_max_attempts_setting() == 1

    def test_max_boundary_twenty_accepted(self, isolated_yaml):
        update_config_yaml({'cloud_archive.retry_max_attempts': 20})
        assert svc._read_retry_max_attempts_setting() == 20

    def test_falls_back_when_yaml_unreadable(self, monkeypatch):
        # Point CONFIG_YAML at a path that doesn't exist; the reader
        # must NOT raise — the worker depends on it for every failure.
        monkeypatch.setattr(config, "CONFIG_YAML", "/nonexistent/path.yaml")
        result = svc._read_retry_max_attempts_setting()
        assert result == svc.CLOUD_ARCHIVE_RETRY_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# _mark_upload_failure
# ---------------------------------------------------------------------------


class TestMarkUploadFailure:
    """Atomic increment + cap-based status promotion."""

    def test_below_cap_status_failed(self, cloud_db, isolated_yaml):
        # Cap is 5; row at retry_count=0; first failure → retry_count=1,
        # status='failed' (still has 4 attempts left).
        _seed_row(cloud_db, "SentryClips/x", status='uploading',
                  retry_count=0)
        svc._mark_upload_failure(cloud_db, "SentryClips/x", "transient")
        row = _row(cloud_db, "SentryClips/x")
        assert row["status"] == 'failed'
        assert row["retry_count"] == 1
        assert row["last_error"] == "transient"

    def test_at_cap_promotes_to_dead_letter(self, cloud_db, isolated_yaml):
        # Cap is 5; row at retry_count=4; next failure brings count to 5
        # which IS the cap → promote to dead_letter.
        _seed_row(cloud_db, "SentryClips/y", status='failed',
                  retry_count=4)
        svc._mark_upload_failure(cloud_db, "SentryClips/y", "perm err")
        row = _row(cloud_db, "SentryClips/y")
        assert row["status"] == 'dead_letter'
        assert row["retry_count"] == 5
        assert row["last_error"] == "perm err"

    def test_above_cap_remains_dead_letter(self, cloud_db, isolated_yaml):
        # Defensive: a row that's already over the cap (e.g., cap was
        # lowered after the row racked up failures) MUST stay
        # dead_letter and not be silently demoted back to 'failed'.
        _seed_row(cloud_db, "SentryClips/z", status='failed',
                  retry_count=10)
        svc._mark_upload_failure(cloud_db, "SentryClips/z", "still failing")
        row = _row(cloud_db, "SentryClips/z")
        assert row["status"] == 'dead_letter'
        assert row["retry_count"] == 11

    def test_cap_change_takes_effect_immediately(
            self, cloud_db, isolated_yaml):
        """Lowering the cap mid-run must promote rows on the very next
        failure — proves the per-call YAML re-read works end-to-end.
        """
        _seed_row(cloud_db, "SentryClips/q", status='failed',
                  retry_count=2)
        # First failure with default cap (5): 2 → 3, still 'failed'.
        svc._mark_upload_failure(cloud_db, "SentryClips/q", "err1")
        assert _row(cloud_db, "SentryClips/q")["status"] == 'failed'

        # Lower the cap to 3. Next failure (3 → 4) is at-cap → dead_letter.
        update_config_yaml({'cloud_archive.retry_max_attempts': 3})
        svc._mark_upload_failure(cloud_db, "SentryClips/q", "err2")
        post = _row(cloud_db, "SentryClips/q")
        assert post["status"] == 'dead_letter', (
            "Lowering retry_max_attempts mid-run did not take effect — "
            "regression to import-time caching of the cap."
        )
        assert post["retry_count"] == 4

    def test_missing_row_no_op(self, cloud_db, isolated_yaml):
        # No-op when the row doesn't exist — the worker never gets here
        # for an unknown row but be defensive.
        svc._mark_upload_failure(cloud_db, "SentryClips/never", "err")
        # No exception, no row to query.
        assert _row(cloud_db, "SentryClips/never") is None

    def test_dead_letter_promotion_logged(
            self, cloud_db, isolated_yaml, caplog):
        """A dead_letter promotion must be logged at WARNING — operators
        need to see in journalctl which files have been permanently
        abandoned.
        """
        import logging
        _seed_row(cloud_db, "SentryClips/w", status='failed',
                  retry_count=4)
        with caplog.at_level(logging.WARNING, logger=svc.logger.name):
            svc._mark_upload_failure(cloud_db, "SentryClips/w", "err")
        assert any(
            "dead_letter" in rec.message and "SentryClips/w" in rec.message
            for rec in caplog.records
        ), f"Expected dead_letter WARNING; got {[r.message for r in caplog.records]}"

    def test_below_cap_promotion_not_logged(
            self, cloud_db, isolated_yaml, caplog):
        """The 'failed' path is the common case — DON'T log a warning
        every time, only on actual cap-hit promotions.
        """
        import logging
        _seed_row(cloud_db, "SentryClips/v", status='uploading',
                  retry_count=0)
        with caplog.at_level(logging.WARNING, logger=svc.logger.name):
            svc._mark_upload_failure(cloud_db, "SentryClips/v", "transient")
        # Should be no WARNING from cloud_archive about this row.
        assert not any(
            "SentryClips/v" in rec.message and rec.levelno >= logging.WARNING
            for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# _discover_events — dead_letter exclusion
# ---------------------------------------------------------------------------


def _make_event_dir(parent: str, name: str, with_event_json: bool = True,
                    with_video: bool = True) -> str:
    event_dir = os.path.join(parent, name)
    os.makedirs(event_dir, exist_ok=True)
    if with_event_json:
        with open(os.path.join(event_dir, "event.json"), "w") as f:
            f.write('{"reason":"sentry_aware_object_detection"}')
    if with_video:
        with open(os.path.join(event_dir, "front.mp4"), "wb") as f:
            f.write(b"\x00" * 1024)
    return event_dir


class TestDeadLetterExclusion:
    """``_discover_events`` must skip both 'synced' AND 'dead_letter' rows."""

    def test_dead_letter_row_not_re_picked(self, tmp_path, cloud_db,
                                           isolated_yaml, monkeypatch):
        # Two event dirs on disk; one already in dead_letter state in DB.
        teslacam = tmp_path / "TeslaCam"
        sentry = teslacam / "SentryClips"
        sentry.mkdir(parents=True)
        good = "2026-05-12_10-00-00"
        bad = "2026-05-12_11-00-00"
        _make_event_dir(str(sentry), good)
        _make_event_dir(str(sentry), bad)
        monkeypatch.setattr(config, "MAPPING_ENABLED", False, raising=False)

        _seed_row(cloud_db, f"SentryClips/{bad}", status='dead_letter',
                  retry_count=5)

        result = svc._discover_events(str(teslacam), conn=cloud_db)

        rel_paths = [r[1] for r in result]
        assert f"SentryClips/{good}" in rel_paths
        assert f"SentryClips/{bad}" not in rel_paths, (
            "dead_letter row was re-picked — auto-sync would re-burn "
            "bandwidth on a row that has already exhausted its retries."
        )

    def test_synced_and_dead_letter_both_excluded(
            self, tmp_path, cloud_db, isolated_yaml, monkeypatch):
        teslacam = tmp_path / "TeslaCam"
        sentry = teslacam / "SentryClips"
        sentry.mkdir(parents=True)
        for name in ("a", "b", "c"):
            _make_event_dir(str(sentry), f"2026-05-12_10-{name}-00")
        monkeypatch.setattr(config, "MAPPING_ENABLED", False, raising=False)

        _seed_row(cloud_db, "SentryClips/2026-05-12_10-a-00",
                  status='synced', retry_count=0)
        _seed_row(cloud_db, "SentryClips/2026-05-12_10-b-00",
                  status='dead_letter', retry_count=5)

        result = svc._discover_events(str(teslacam), conn=cloud_db)
        rel_paths = [r[1] for r in result]
        assert rel_paths == ["SentryClips/2026-05-12_10-c-00"]

    def test_failed_status_still_picked_for_retry(
            self, tmp_path, cloud_db, isolated_yaml, monkeypatch):
        # 'failed' is the auto-retry state — DO NOT exclude it.
        teslacam = tmp_path / "TeslaCam"
        sentry = teslacam / "SentryClips"
        sentry.mkdir(parents=True)
        _make_event_dir(str(sentry), "2026-05-12_10-00-00")
        monkeypatch.setattr(config, "MAPPING_ENABLED", False, raising=False)

        _seed_row(cloud_db, "SentryClips/2026-05-12_10-00-00",
                  status='failed', retry_count=2)

        result = svc._discover_events(str(teslacam), conn=cloud_db)
        rel_paths = [r[1] for r in result]
        assert "SentryClips/2026-05-12_10-00-00" in rel_paths, (
            "'failed' rows must still be picked for retry — only "
            "'dead_letter' is permanently excluded from auto-picking."
        )


# ---------------------------------------------------------------------------
# get_sync_stats — dashboard counter must not "self-resolve" on cap promotion
# ---------------------------------------------------------------------------


class TestSyncStatsDeadLetterAccounting:
    """The dashboard's ``Failed`` tile reads ``total_failed`` from
    ``get_sync_stats``. Phase 2.6 promotes rows from ``failed`` to
    ``dead_letter`` when the retry cap is hit; without including
    dead_letter rows in the failed count, the dashboard counter would
    silently DECREASE on promotion — making problems look like they
    self-resolved.
    """

    def _stats(self, db_path: str):
        """``get_sync_stats`` is the dashboard data source — call the
        real function so we exercise the same code path the blueprint
        does, not an in-test reimplementation.
        """
        return svc.get_sync_stats(db_path)

    def test_total_failed_includes_dead_letter(self, tmp_path):
        db_path = str(tmp_path / "cloud_sync.db")
        # Use the real init path so the schema matches production.
        conn = svc._init_cloud_tables(db_path)
        try:
            conn.execute(
                "INSERT INTO cloud_synced_files (file_path, status, "
                "retry_count) VALUES (?, 'failed', 3)", ("a",))
            conn.execute(
                "INSERT INTO cloud_synced_files (file_path, status, "
                "retry_count) VALUES (?, 'dead_letter', 5)", ("b",))
            conn.execute(
                "INSERT INTO cloud_synced_files (file_path, status, "
                "retry_count) VALUES (?, 'dead_letter', 5)", ("c",))
            conn.commit()
        finally:
            conn.close()

        stats = self._stats(db_path)
        # 1 failed + 2 dead_letter == 3 broken uploads on the dashboard.
        assert stats["total_failed"] == 3, (
            "total_failed must include dead_letter rows so the "
            "dashboard counter does not silently shrink when cap "
            "promotion fires."
        )
        assert stats["total_dead_letter"] == 2, (
            "total_dead_letter must be exposed as a subset for any "
            "future Failed Jobs page."
        )

    def test_total_failed_does_not_include_synced_or_pending(
            self, tmp_path):
        db_path = str(tmp_path / "cloud_sync.db")
        conn = svc._init_cloud_tables(db_path)
        try:
            conn.execute("INSERT INTO cloud_synced_files (file_path, "
                         "status) VALUES ('a', 'synced')")
            conn.execute("INSERT INTO cloud_synced_files (file_path, "
                         "status) VALUES ('b', 'pending')")
            conn.execute("INSERT INTO cloud_synced_files (file_path, "
                         "status) VALUES ('c', 'uploading')")
            conn.commit()
        finally:
            conn.close()
        stats = self._stats(db_path)
        assert stats["total_failed"] == 0
        assert stats["total_dead_letter"] == 0
        assert stats["total_synced"] == 1

    def test_promotion_does_not_decrease_total_failed(self, tmp_path,
                                                     isolated_yaml):
        """End-to-end pin: a row that fails then promotes must NOT
        decrease the dashboard counter. Was the bug — fixing this is
        the whole reason the dashboard sums failed + dead_letter.
        """
        db_path = str(tmp_path / "cloud_sync.db")
        conn = svc._init_cloud_tables(db_path)
        try:
            conn.execute("INSERT INTO cloud_synced_files (file_path, "
                         "status, retry_count) VALUES (?, 'failed', 4)",
                         ("a",))
            conn.commit()
            before = self._stats(db_path)["total_failed"]
            assert before == 1
            # Cap is 5 (isolated_yaml seed) — next failure promotes to dead_letter.
            svc._mark_upload_failure(conn, "a", "still failing")
            conn.commit()
        finally:
            conn.close()
        after = self._stats(db_path)["total_failed"]
        assert after == before, (
            "total_failed went from %d to %d after dead_letter promotion "
            "— the dashboard would now hide the broken row." % (before, after)
        )
