"""Tests for Phase 5.2 — score-priority connection batching.

`_score_event_priority` previously opened a fresh sqlite3 connection per
event and ran a full-scan ``LIKE '%dir%'`` query inside the per-event scoring
comprehension in ``_discover_events``. For a queue with N candidate events
that's N connection opens and N full-scan queries.

The fix pre-fetches the set of event-directory basenames that have any
waypoint geolocation hit ONCE via ``_load_geo_hits()`` (a single
``SELECT DISTINCT video_path FROM waypoints WHERE video_path IS NOT NULL``)
and passes it into each scorer call as ``geo_hits``. The per-event check
collapses to an O(1) ``in`` lookup.

The legacy per-event query path is preserved as a fallback when ``geo_hits
is None`` (mapping disabled, import failed, query raised) so direct callers
of ``_score_event_priority`` continue to work without arrange.

These tests pin:
  1. semantic equivalence — score with batched geo_hits == score without
  2. fallback — geo_hits=None preserves legacy per-event lookup
  3. single connection — _discover_events opens ONE geodata.db connection
     regardless of candidate count
  4. empty result — geo_hits=set() means no event dir matches, so geo
     tier (100) is skipped correctly
"""

from __future__ import annotations

import os
import sqlite3
from typing import List
from unittest.mock import patch

import pytest

import config
from services import cloud_archive_service as svc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_event_dir(parent: str, name: str, with_event_json: bool = False,
                    with_video: bool = True) -> str:
    """Create a minimal Tesla event directory."""
    event_dir = os.path.join(parent, name)
    os.makedirs(event_dir, exist_ok=True)
    if with_event_json:
        with open(os.path.join(event_dir, "event.json"), "w") as f:
            f.write('{"reason":"sentry_aware_object_detection"}')
    if with_video:
        with open(os.path.join(event_dir, "front.mp4"), "wb") as f:
            f.write(b"\x00" * 1024)
    return event_dir


def _make_geodata_db(tmp_path, video_paths: List[str]) -> str:
    """Build a geodata.db with the production schema and the given video
    paths inserted into the ``waypoints`` table.

    Uses ``mapping_migrations._init_db`` so the schema matches the v6
    layout that the legacy ``_score_event_priority`` per-event path
    expects (it goes through ``mapping_queries.get_db_connection`` which
    runs the full migration on every connect).
    """
    db_path = str(tmp_path / "geodata.db")
    # Provision the full v6 schema by calling _init_db once.
    from services.mapping_migrations import _init_db
    conn = _init_db(db_path)
    try:
        # waypoints requires a parent trip row (FK ON DELETE CASCADE).
        conn.execute(
            "INSERT INTO trips (start_time, end_time) "
            "VALUES ('2026-05-12T11:00:00', '2026-05-12T11:05:00')"
        )
        trip_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for vp in video_paths:
            conn.execute(
                "INSERT INTO waypoints "
                "(trip_id, timestamp, lat, lon, video_path) "
                "VALUES (?, ?, ?, ?, ?)",
                (trip_id, "2026-05-12T11:01:00", 37.0, -122.0, vp),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.fixture
def teslacam_with_geo(tmp_path, monkeypatch):
    """Build a TeslaCam tree with mixed event/non-event dirs and a
    geodata.db where SOME of the non-event dirs have waypoints.

    Layout:
      SentryClips/
        2026-05-12_10-00-00/  (event.json → score 0)
        2026-05-12_11-00-00/  (no event.json, HAS geodata waypoint → score 100)
        2026-05-12_12-00-00/  (no event.json, NO geodata → score 200+)
    """
    teslacam = tmp_path / "TeslaCam"
    sentry = teslacam / "SentryClips"
    sentry.mkdir(parents=True)

    _make_event_dir(str(sentry), "2026-05-12_10-00-00", with_event_json=True)
    _make_event_dir(str(sentry), "2026-05-12_11-00-00", with_event_json=False)
    _make_event_dir(str(sentry), "2026-05-12_12-00-00", with_event_json=False)

    # geodata.db: only the 11-00-00 dir has waypoints. The flat ArchivedClips
    # path is included to verify the dirname-derivation does not blow up
    # when a waypoint references a flat file.
    db_path = _make_geodata_db(tmp_path, [
        f"{sentry}/2026-05-12_11-00-00/front-2026-05-12_11-01-00.mp4",
        "/home/pi/ArchivedClips/somefile.mp4",  # flat — parent is ArchivedClips, irrelevant
    ])

    monkeypatch.setattr(config, "MAPPING_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "MAPPING_DB_PATH", db_path, raising=False)
    # Make sure the non-event filter doesn't drop our score-200 dir.
    monkeypatch.setattr(svc, "_read_sync_non_event_setting",
                        lambda: True, raising=True)

    return str(teslacam), db_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestScorePriorityBatching:
    """Phase 5.2 — collapse N+1 to 1 SQL connection per discover pass."""

    def test_geo_hits_lookup_matches_legacy_per_event_score(
            self, teslacam_with_geo):
        """Score with batched geo_hits == score with legacy per-event query."""
        teslacam, _ = teslacam_with_geo
        sentry = os.path.join(teslacam, "SentryClips")
        event_dir_with_geo = os.path.join(sentry, "2026-05-12_11-00-00")
        event_dir_no_geo = os.path.join(sentry, "2026-05-12_12-00-00")

        # Legacy path — geo_hits=None falls through to per-event query.
        legacy_with_geo = svc._score_event_priority(event_dir_with_geo)
        legacy_no_geo = svc._score_event_priority(event_dir_no_geo)

        # Batched path — pre-fetch once, pass set in.
        geo_hits = svc._load_geo_hits()
        assert geo_hits is not None, "Expected geo_hits to load successfully"

        batched_with_geo = svc._score_event_priority(
            event_dir_with_geo, geo_hits=geo_hits)
        batched_no_geo = svc._score_event_priority(
            event_dir_no_geo, geo_hits=geo_hits)

        assert legacy_with_geo == batched_with_geo, (
            f"Score mismatch for geo dir: legacy={legacy_with_geo} "
            f"batched={batched_with_geo}"
        )
        assert legacy_no_geo == batched_no_geo, (
            f"Score mismatch for non-geo dir: legacy={legacy_no_geo} "
            f"batched={batched_no_geo}"
        )
        # Tier sanity: geo dir is in the 100-199 tier, no-geo dir is 200+.
        assert 100 <= batched_with_geo < 200, batched_with_geo
        assert batched_no_geo >= 200, batched_no_geo

    def test_geo_hits_none_falls_back_to_per_event_query(
            self, teslacam_with_geo):
        """``geo_hits=None`` MUST trigger the legacy per-event SQLite path so
        existing direct callers (tests, future code) keep working.
        """
        teslacam, _ = teslacam_with_geo
        event_dir_with_geo = os.path.join(
            teslacam, "SentryClips", "2026-05-12_11-00-00")

        score = svc._score_event_priority(event_dir_with_geo, geo_hits=None)
        assert 100 <= score < 200, (
            f"Expected geo tier (100-199) via legacy path, got {score}"
        )

    def test_discover_events_opens_single_geodata_connection(
            self, teslacam_with_geo, monkeypatch):
        """For N candidate events, ``_discover_events`` opens ONE geodata.db
        connection (in ``_load_geo_hits``) — not N (one per event).

        This is the regression guard — if a future refactor moves the
        per-event query back inside the comprehension, this test fails.
        """
        teslacam, db_path = teslacam_with_geo

        # Disable the Wave 4 PR-F4 producer hook for this test — it
        # opens its own batched connection to enqueue rows into
        # pipeline_queue (which lives in geodata.db). That extra
        # connection is intentional and exercised by
        # tests/test_pipeline_queue_dualwrite.py; here we want to pin
        # the ``_load_geo_hits`` batching contract in isolation.
        monkeypatch.setattr(svc, '_enqueue_to_pipeline_enabled',
                            lambda: False)

        # Spy on sqlite3.connect to count opens.
        original_connect = sqlite3.connect
        call_count = {"n": 0, "paths": []}

        def counting_connect(target, *args, **kwargs):
            # Only count opens to the geodata.db under test (other tests in
            # the same process may use sqlite3 for their own fixtures).
            if target == db_path:
                call_count["n"] += 1
                call_count["paths"].append(target)
            return original_connect(target, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", counting_connect,
                            raising=True)

        result = svc._discover_events(teslacam, conn=None)

        # Sanity: discovered all 3 event dirs.
        assert len(result) == 3, (
            f"Expected 3 events, got {[r[1] for r in result]}"
        )

        # ONE connection — the _load_geo_hits batched fetch — regardless of
        # candidate count. Legacy code would have opened 2 connections (for
        # the two no-event-json dirs that fall through to the geo check).
        assert call_count["n"] == 1, (
            f"Expected 1 geodata connection (batched), got {call_count['n']} "
            f"to paths: {call_count['paths']}"
        )

    def test_load_geo_hits_returns_dirname_set(self, teslacam_with_geo):
        """``_load_geo_hits`` returns the parent-dir basename of every
        non-NULL ``waypoints.video_path``.
        """
        _, _ = teslacam_with_geo
        hits = svc._load_geo_hits()
        assert hits is not None
        # The 11-00-00 dir's waypoint contributes its parent basename:
        assert "2026-05-12_11-00-00" in hits
        # The flat ArchivedClips path contributes 'ArchivedClips' (irrelevant
        # for matching but proves we don't blow up on flat paths).
        assert "ArchivedClips" in hits

    def test_load_geo_hits_returns_none_when_mapping_disabled(
            self, tmp_path, monkeypatch):
        """``MAPPING_ENABLED=False`` ⇒ ``_load_geo_hits`` returns None so
        callers fall through to legacy per-event query.
        """
        monkeypatch.setattr(config, "MAPPING_ENABLED", False, raising=False)
        assert svc._load_geo_hits() is None

    def test_load_geo_hits_handles_missing_db_gracefully(
            self, tmp_path, monkeypatch):
        """A missing geodata.db returns None (not a crash, not an empty set).
        Returning ``None`` is the documented signal for "fall back to
        per-event lookup" — empty set would silently say "no geo hits"
        and demote events that DO have geolocation in production.
        """
        monkeypatch.setattr(config, "MAPPING_ENABLED", True, raising=False)
        monkeypatch.setattr(config, "MAPPING_DB_PATH",
                            str(tmp_path / "does_not_exist.db"),
                            raising=False)
        result = svc._load_geo_hits()
        assert result is None

    def test_load_geo_hits_handles_missing_table_gracefully(
            self, tmp_path, monkeypatch):
        """An existing-but-empty SQLite file (no waypoints table) returns
        None — same fallback semantics as the missing-DB case.
        """
        empty_db = str(tmp_path / "empty.db")
        # Create empty DB (no schema).
        sqlite3.connect(empty_db).close()
        monkeypatch.setattr(config, "MAPPING_ENABLED", True, raising=False)
        monkeypatch.setattr(config, "MAPPING_DB_PATH", empty_db, raising=False)
        result = svc._load_geo_hits()
        assert result is None

    def test_geo_hits_matches_flat_archived_clips_shape(
            self, tmp_path, monkeypatch):
        """REGRESSION GUARD (PR #143 review).

        In production the indexer rewrites every waypoint's
        ``video_path`` to the flat ``ArchivedClips/<basename>`` form —
        NEVER the original nested ``SentryClips/<event-dir>/<file>``
        form. The first iteration of this PR derived only the parent
        dir basename, which always produced ``"ArchivedClips"`` /
        ``"RecentClips"`` — never the event timestamp.

        The result was that ``_score_event_priority`` for an event
        like ``SentryClips/2026-05-12_10-00-00`` would NOT find its
        waypoint anchor in ``geo_hits`` and would silently demote the
        event from the geo tier (100) to the no-geo tier (200+).
        Under ``sync_non_event_videos: false`` (the default) that
        causes the clip to be DROPPED from the queue.

        This test pins the production data shape end-to-end:
        - waypoint stored as ``ArchivedClips/<timestamp>-front.mp4``
        - event dir ``SentryClips/<timestamp>`` (timestamp dir_name)
        - geo_hits MUST contain the timestamp string so the per-event
          ``in`` check matches.
        """
        # Production-shaped geodata.db: waypoint stored as flat
        # ``ArchivedClips/2026-05-12_10-00-00-front.mp4``.
        db_path = _make_geodata_db(tmp_path, [
            "ArchivedClips/2026-05-12_10-00-00-front.mp4",
            "ArchivedClips/2026-05-12_11-00-00-back.mp4",
        ])
        monkeypatch.setattr(config, "MAPPING_ENABLED", True, raising=False)
        monkeypatch.setattr(config, "MAPPING_DB_PATH", db_path, raising=False)

        hits = svc._load_geo_hits()
        assert hits is not None

        # Anchor 3: the leading 19-char timestamp prefix MUST be in
        # the set so a SentryClips event-dir basename can match.
        assert "2026-05-12_10-00-00" in hits, (
            "Missing timestamp anchor — SentryClips event dirs would "
            "fail to match their flat-stored waypoints. Reproduces "
            "PR #143 critical finding."
        )
        assert "2026-05-12_11-00-00" in hits

        # Anchor 2: the full filename MUST also be in the set so a
        # flat-ArchivedClips event call (where dir_name is the file
        # basename) can match.
        assert "2026-05-12_10-00-00-front.mp4" in hits
        assert "2026-05-12_11-00-00-back.mp4" in hits

        # Anchor 1: parent-dir basename — harmless noise but documents
        # the contract.
        assert "ArchivedClips" in hits

    def test_score_via_geo_hits_matches_flat_path_production_shape(
            self, tmp_path, monkeypatch):
        """End-to-end: a SentryClips event-dir score with batched
        geo_hits MUST equal the legacy per-event score when waypoints
        are stored in the flat production form.

        This is the test that would have caught the PR #143 critical
        finding before deploy.
        """
        # Production-shaped geodata.db.
        db_path = _make_geodata_db(tmp_path, [
            "ArchivedClips/2026-05-12_10-00-00-front.mp4",
        ])
        monkeypatch.setattr(config, "MAPPING_ENABLED", True, raising=False)
        monkeypatch.setattr(config, "MAPPING_DB_PATH", db_path, raising=False)

        # SentryClips event dir with the matching timestamp.
        teslacam = tmp_path / "TeslaCam"
        sentry = teslacam / "SentryClips"
        sentry.mkdir(parents=True)
        event_dir = _make_event_dir(
            str(sentry), "2026-05-12_10-00-00", with_event_json=False)

        legacy_score = svc._score_event_priority(event_dir)
        hits = svc._load_geo_hits()
        batched_score = svc._score_event_priority(event_dir, geo_hits=hits)

        assert legacy_score == batched_score, (
            f"Score mismatch on production-shape data: "
            f"legacy={legacy_score} batched={batched_score}. "
            f"PR #143 critical finding regression."
        )
        # Both should be in the geo tier (100-199), not the no-geo
        # tier (200+).
        assert 100 <= batched_score < 200, (
            f"Expected geo tier, got {batched_score}. The flat-path "
            f"anchors (basename + 19-char prefix) were not added to "
            f"geo_hits."
        )

    def test_score_via_geo_hits_skips_db_connection_entirely(
            self, teslacam_with_geo, monkeypatch):
        """When ``geo_hits`` is provided, ``_score_event_priority`` MUST NOT
        open a SQLite connection — proves the fast path is wired.
        """
        teslacam, db_path = teslacam_with_geo
        event_dir = os.path.join(
            teslacam, "SentryClips", "2026-05-12_11-00-00")

        # Tripwire: if sqlite3.connect targets the geodata DB while
        # geo_hits is provided, raise. Other sqlite3 connections (rare in
        # this code path, but test isolation requires the filter) are
        # allowed.
        original_connect = sqlite3.connect

        def must_not_call_for_geo(target, *args, **kwargs):
            if target == db_path:
                raise AssertionError(
                    "Scorer opened a SQLite connection to geodata.db "
                    "despite geo_hits being provided — fast path is broken."
                )
            return original_connect(target, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", must_not_call_for_geo,
                            raising=True)

        # Should succeed without touching SQLite.
        score = svc._score_event_priority(
            event_dir, geo_hits={"2026-05-12_11-00-00"})
        assert 100 <= score < 200, score
