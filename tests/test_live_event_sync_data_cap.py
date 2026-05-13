"""Phase 4.6 (#101) — Tests for the LES daily data-cap surfacing in
``get_status()`` so the cloud_archive page banner can render the
"Daily data cap reached" message correctly.

The banner UI itself is template-driven (cloud_archive.html); these
tests pin only the API contract additions:

* ``data_uploaded_today_bytes`` — total bytes uploaded today (UTC),
  recomputed fresh on every ``get_status()`` call (not cached on
  ``_status``) so the banner is accurate even when the worker is
  idle (LES idles between events; the cached value would otherwise
  lag until the next upload cycle).
* ``daily_data_cap_mb`` — the configured cap in MB (0 = unlimited).
* ``data_cap_reached`` — boolean: today_bytes >= cap_bytes.
* ``data_cap_pct`` — integer 0..100 (None when cap is 0/unlimited).

All four keys MUST appear on every successful and failed code path
(matches Phase 4.4 ETA / Phase 4.5 pause_reason contract).
"""
import os
import sqlite3
from datetime import datetime, timezone

import pytest

import sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'scripts', 'web'))

from services import live_event_sync_service as les  # noqa: E402


@pytest.fixture
def fresh_les_db(tmp_path, monkeypatch):
    """Point LES at a tmp cloud_sync.db with the LES schema applied."""
    db_path = str(tmp_path / "cloud_sync.db")
    monkeypatch.setattr(les, 'CLOUD_ARCHIVE_DB_PATH', db_path, raising=False)
    # Initialise schema by opening once.
    conn = les._open_db()
    try:
        les._ensure_schema(conn)
    finally:
        conn.close()
    return db_path


def _insert_uploaded_row(db_path, bytes_uploaded, uploaded_at_iso=None):
    """Insert a fully-uploaded row with the given byte count."""
    if uploaded_at_iso is None:
        uploaded_at_iso = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO live_event_queue "
            "(event_dir, event_json_path, status, "
            " enqueued_at, uploaded_at, bytes_uploaded) "
            "VALUES (?, ?, 'uploaded', ?, ?, ?)",
            ('/dev/null', '/dev/null', uploaded_at_iso,
             uploaded_at_iso, int(bytes_uploaded)),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 4.6 contract: keys present on EVERY return path
# ---------------------------------------------------------------------------

class TestDataCapStatusContract:

    def test_unlimited_cap_returns_none_pct(self, fresh_les_db, monkeypatch):
        # Cap=0 means unlimited — banner stays hidden, pct is None.
        monkeypatch.setattr(les, 'LIVE_EVENT_DAILY_DATA_CAP_MB', 0,
                            raising=False)
        snap = les.get_status()
        assert snap['data_cap_reached'] is False
        assert snap['data_cap_pct'] is None
        assert snap['daily_data_cap_mb'] == 0
        assert snap['data_uploaded_today_bytes'] == 0

    def test_cap_set_no_uploads_yet_zero_pct(self, fresh_les_db, monkeypatch):
        monkeypatch.setattr(les, 'LIVE_EVENT_DAILY_DATA_CAP_MB', 100,
                            raising=False)
        snap = les.get_status()
        assert snap['data_cap_reached'] is False
        assert snap['data_cap_pct'] == 0
        assert snap['daily_data_cap_mb'] == 100
        assert snap['data_uploaded_today_bytes'] == 0

    def test_cap_set_partial_usage(self, fresh_les_db, monkeypatch):
        # 50 MB uploaded today against a 100 MB cap → 50 % full.
        _insert_uploaded_row(fresh_les_db, 50 * 1024 * 1024)
        monkeypatch.setattr(les, 'LIVE_EVENT_DAILY_DATA_CAP_MB', 100,
                            raising=False)
        snap = les.get_status()
        assert snap['data_cap_reached'] is False
        assert snap['data_cap_pct'] == 50
        assert snap['data_uploaded_today_bytes'] == 50 * 1024 * 1024

    def test_cap_reached_exactly_at_threshold(self, fresh_les_db, monkeypatch):
        # 100 MB uploaded against 100 MB cap → reached (>=).
        _insert_uploaded_row(fresh_les_db, 100 * 1024 * 1024)
        monkeypatch.setattr(les, 'LIVE_EVENT_DAILY_DATA_CAP_MB', 100,
                            raising=False)
        snap = les.get_status()
        assert snap['data_cap_reached'] is True
        assert snap['data_cap_pct'] == 100

    def test_cap_overshoot_capped_at_100_pct(self, fresh_les_db, monkeypatch):
        # 250 MB uploaded against 100 MB cap → cap_pct must clamp at 100.
        # (e.g., a single event upload that exceeded the cap because the
        # check is post-upload.)
        _insert_uploaded_row(fresh_les_db, 250 * 1024 * 1024)
        monkeypatch.setattr(les, 'LIVE_EVENT_DAILY_DATA_CAP_MB', 100,
                            raising=False)
        snap = les.get_status()
        assert snap['data_cap_reached'] is True
        assert snap['data_cap_pct'] == 100, (
            "cap_pct must clamp at 100 — never claim 250% so the banner "
            "stays trustworthy."
        )

    def test_db_error_falls_back_with_keys_present(
        self, fresh_les_db, monkeypatch,
    ):
        # If the DB read raises, the keys MUST still be present so JS
        # consumers don't crash on missing keys.
        monkeypatch.setattr(les, 'LIVE_EVENT_DAILY_DATA_CAP_MB', 200,
                            raising=False)

        def boom(*_a, **_kw):
            raise RuntimeError("DB error")

        monkeypatch.setattr(les, '_open_db', boom)
        snap = les.get_status()
        # Queue counts surface the error as documented.
        assert 'error' in snap['queue_counts']
        # Phase 4.6 keys must be present even on error path.
        assert snap['data_uploaded_today_bytes'] == 0
        assert snap['daily_data_cap_mb'] == 200
        assert snap['data_cap_reached'] is False
        assert snap['data_cap_pct'] is None

    def test_yesterdays_uploads_dont_count(self, fresh_les_db, monkeypatch):
        # ``_today_uploaded_bytes`` filters by ``uploaded_at >= today_iso``.
        # An upload from yesterday must not be billed against today's cap.
        from datetime import timedelta
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)
                     ).isoformat()
        _insert_uploaded_row(
            fresh_les_db, 200 * 1024 * 1024,
            uploaded_at_iso=yesterday,
        )
        monkeypatch.setattr(les, 'LIVE_EVENT_DAILY_DATA_CAP_MB', 100,
                            raising=False)
        snap = les.get_status()
        # Yesterday's upload doesn't count → today_bytes == 0.
        assert snap['data_uploaded_today_bytes'] == 0
        assert snap['data_cap_reached'] is False
        assert snap['data_cap_pct'] == 0


# ---------------------------------------------------------------------------
# Phase 4.6: queue counts contract still intact (regression guard)
# ---------------------------------------------------------------------------

class TestExistingContractStillIntact:
    """The Phase 4.6 changes added cap fields but must not regress the
    pre-existing queue_counts / has_ready_work contract."""

    def test_queue_counts_still_present(self, fresh_les_db, monkeypatch):
        monkeypatch.setattr(les, 'LIVE_EVENT_DAILY_DATA_CAP_MB', 0,
                            raising=False)
        snap = les.get_status()
        assert 'queue_counts' in snap
        assert set(snap['queue_counts'].keys()) >= {
            'pending', 'uploading', 'uploaded', 'failed',
        }

    def test_has_ready_work_still_present(self, fresh_les_db, monkeypatch):
        monkeypatch.setattr(les, 'LIVE_EVENT_DAILY_DATA_CAP_MB', 0,
                            raising=False)
        snap = les.get_status()
        assert 'has_ready_work' in snap
        assert isinstance(snap['has_ready_work'], bool)
