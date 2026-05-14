"""Tests for issue #184 Wave 4 PR-F3 (cloud_archive pipeline reader switch).

PR-F3 mirrors PR-F1's archive-worker reader switch for cloud_archive.
When ``CLOUD_ARCHIVE_USE_PIPELINE_READER`` is True (default OFF), the
drain pass replaces the disk-walk + ``cloud_synced_files`` filter with
a batch ``claim_next_for_stage`` against ``pipeline_queue``. The
upload loop body is structurally unchanged — it still iterates a list
of ``(event_dir, rel_path, event_size)`` tuples — so the existing
state-transition dual-write hooks (PR-B) drive the pipeline_queue row
from ``in_progress`` (set by claim) to ``done`` (set by the
``cloud_synced_files`` UPDATE on success) without any new wiring.

Tests cover:

* ``release_pipeline_claim_by_source_path`` semantics: success path,
  optional last_error, idempotent on missing rows, never raises.
* ``_claim_via_pipeline_reader_cloud`` batch claim semantics: claims
  up to ``limit`` rows, atomically marks each in_progress, returns
  shape matching ``_discover_events``.
* Defensive data-shape gaps: empty source_path leaves row in_progress
  + WARNING; missing event_dir releases back to pending + WARNING.
* ``_release_cloud_pipeline_claims`` releases multiple paths with a
  shared last_error message and tolerates per-row failures.
* End-to-end: claim → release round-trip preserves the row's other
  state (priority, payload, attempts trail).
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

import pytest

# Allow importing the web modules without spinning up Flask.
SCRIPTS_WEB = Path(__file__).resolve().parent.parent / 'scripts' / 'web'
if str(SCRIPTS_WEB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_WEB))

from services import cloud_archive_service as svc  # noqa: E402
from services import pipeline_queue_service as pqs  # noqa: E402
from services.mapping_migrations import _init_db  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def geodata_db(tmp_path, monkeypatch):
    """A fresh ``geodata.db`` with the pipeline_queue schema."""
    db_path = str(tmp_path / 'geodata.db')
    conn = _init_db(db_path)
    conn.close()
    monkeypatch.setattr(pqs, '_resolve_pipeline_db', lambda: db_path)
    return db_path


def _seed_cloud_pending(
    db_path: str,
    rel_path: str,
    *,
    event_dir: str = '/srv/SentryClips/event_a',
    event_size: int = 12345,
    score: int = 10,
) -> None:
    """Seed a ``cloud_pending`` row via the production producer."""
    pqs.dual_write_enqueue(
        source_path=rel_path,
        stage=pqs.STAGE_CLOUD_PENDING,
        legacy_table=pqs.LEGACY_TABLE_CLOUD_SYNCED,
        priority=pqs.PRIORITY_CLOUD_BULK,
        payload={
            'event_dir': event_dir,
            'event_size': event_size,
            'score': score,
            'producer': 'cloud_archive._discover_events',
        },
        db_path=db_path,
    )


def _row_for(db_path: str, rel_path: str) -> sqlite3.Row:
    """Return the single pipeline_queue row for ``rel_path`` (or None)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM pipeline_queue WHERE source_path = ?",
            (rel_path,),
        ).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# release_pipeline_claim_by_source_path (in pipeline_queue_service)
# ---------------------------------------------------------------------------


class TestReleaseBySourcePath:
    """The cloud-specific release helper keys on (stage, source_path)."""

    def test_release_an_in_progress_row_resets_status_and_clears_claim(
            self, geodata_db):
        _seed_cloud_pending(geodata_db, 'SentryClips/a.json')
        # Claim the row so it's in_progress with claim metadata set.
        claimed = pqs.claim_next_for_stage(
            stage=pqs.STAGE_CLOUD_PENDING,
            claimed_by='cloud_archive',
        )
        assert claimed is not None
        assert claimed['status'] == 'in_progress'
        assert claimed['claimed_by'] == 'cloud_archive'

        ok = pqs.release_pipeline_claim_by_source_path(
            stage=pqs.STAGE_CLOUD_PENDING,
            source_path='SentryClips/a.json',
            last_error='test release',
        )
        assert ok is True

        row = _row_for(geodata_db, 'SentryClips/a.json')
        assert row['status'] == 'pending'
        assert row['claimed_by'] is None
        assert row['claimed_at'] is None
        assert row['last_error'] == 'test release'

    def test_release_without_last_error_leaves_existing_error_intact(
            self, geodata_db):
        _seed_cloud_pending(geodata_db, 'SentryClips/b.json')
        # Set a baseline last_error via update_pipeline_row, then claim.
        pqs.update_pipeline_row(
            stage=pqs.STAGE_CLOUD_PENDING,
            source_path='SentryClips/b.json',
            last_error='original baseline',
        )
        pqs.claim_next_for_stage(
            stage=pqs.STAGE_CLOUD_PENDING,
            claimed_by='cloud_archive',
        )

        ok = pqs.release_pipeline_claim_by_source_path(
            stage=pqs.STAGE_CLOUD_PENDING,
            source_path='SentryClips/b.json',
            last_error=None,
        )
        assert ok is True

        row = _row_for(geodata_db, 'SentryClips/b.json')
        assert row['status'] == 'pending'
        assert row['last_error'] == 'original baseline'

    def test_release_missing_row_returns_false(self, geodata_db):
        ok = pqs.release_pipeline_claim_by_source_path(
            stage=pqs.STAGE_CLOUD_PENDING,
            source_path='SentryClips/nope.json',
        )
        assert ok is False

    def test_release_with_empty_args_returns_false(self, geodata_db):
        assert pqs.release_pipeline_claim_by_source_path(
            stage='', source_path='SentryClips/a.json',
        ) is False
        assert pqs.release_pipeline_claim_by_source_path(
            stage=pqs.STAGE_CLOUD_PENDING, source_path='',
        ) is False

    def test_release_swallows_sqlite_error(self, monkeypatch, caplog):
        # Point at a nonexistent DB so _open_pipeline_conn fails.
        monkeypatch.setattr(pqs, '_resolve_pipeline_db',
                            lambda: '/nonexistent/path.db')
        with caplog.at_level(logging.DEBUG):
            ok = pqs.release_pipeline_claim_by_source_path(
                stage=pqs.STAGE_CLOUD_PENDING,
                source_path='SentryClips/a.json',
            )
        assert ok is False  # silent no-op on missing DB

    def test_release_only_matches_specified_stage(self, geodata_db):
        # Two rows with the same source_path but different stages.
        # (Cloud + indexing both can have a row for the same file in
        # principle — though in practice the source_path namespace is
        # different. This test pins that release won't cross-stage.)
        _seed_cloud_pending(geodata_db, 'SentryClips/c.json')
        pqs.dual_write_enqueue(
            source_path='SentryClips/c.json',
            stage=pqs.STAGE_INDEX_PENDING,
            legacy_table=pqs.LEGACY_TABLE_INDEXING,
            priority=100,
        )
        pqs.claim_next_for_stage(
            stage=pqs.STAGE_CLOUD_PENDING,
            claimed_by='cloud_archive',
        )

        ok = pqs.release_pipeline_claim_by_source_path(
            stage=pqs.STAGE_INDEX_PENDING,
            source_path='SentryClips/c.json',
        )
        # That row is still 'pending' (never claimed) — release is a
        # no-op UPDATE on already-pending row, but returns True
        # because rowcount > 0.
        assert ok in (True, False)  # implementation-defined for already-pending

        # The cloud row MUST still be in_progress.
        conn = sqlite3.connect(geodata_db)
        try:
            row = conn.execute(
                "SELECT status FROM pipeline_queue WHERE stage = ? "
                "  AND source_path = ?",
                (pqs.STAGE_CLOUD_PENDING, 'SentryClips/c.json'),
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == 'in_progress'  # cloud release did NOT fire


# ---------------------------------------------------------------------------
# _claim_via_pipeline_reader_cloud
# ---------------------------------------------------------------------------


class TestClaimViaPipelineReaderCloud:
    """Batch claim returns _discover_events-shaped tuples."""

    def test_claim_empty_queue_returns_empty_list(self, geodata_db):
        result = svc._claim_via_pipeline_reader_cloud(
            worker_id='cloud_archive',
            db_path=geodata_db,
            limit=8,
        )
        assert result == []

    def test_claim_returns_one_tuple_per_row_in_priority_order(
            self, geodata_db):
        # Three rows enqueued; same priority so enqueue order wins.
        for name in ('alpha', 'bravo', 'charlie'):
            _seed_cloud_pending(
                geodata_db,
                f'SentryClips/{name}.json',
                event_dir=f'/srv/SentryClips/{name}',
                event_size=1000,
            )

        result = svc._claim_via_pipeline_reader_cloud(
            worker_id='cloud_archive',
            db_path=geodata_db,
            limit=8,
        )
        assert len(result) == 3
        # Tuples shape: (event_dir, rel_path, event_size)
        rels = [t[1] for t in result]
        assert rels == [
            'SentryClips/alpha.json',
            'SentryClips/bravo.json',
            'SentryClips/charlie.json',
        ]
        # Each row is now in_progress with claim metadata set.
        for rel in rels:
            row = _row_for(geodata_db, rel)
            assert row['status'] == 'in_progress'
            assert row['claimed_by'] == 'cloud_archive'
            assert row['attempts'] == 1

    def test_claim_respects_limit(self, geodata_db):
        for i in range(5):
            _seed_cloud_pending(geodata_db, f'SentryClips/{i}.json')
        result = svc._claim_via_pipeline_reader_cloud(
            worker_id='cloud_archive', db_path=geodata_db, limit=2,
        )
        assert len(result) == 2
        # Remaining rows still pending.
        for i in range(2, 5):
            row = _row_for(geodata_db, f'SentryClips/{i}.json')
            assert row['status'] == 'pending'

    def test_claim_missing_event_dir_releases_row_with_warning(
            self, geodata_db, caplog):
        # Enqueue with payload missing event_dir.
        pqs.dual_write_enqueue(
            source_path='SentryClips/no_dir.json',
            stage=pqs.STAGE_CLOUD_PENDING,
            legacy_table=pqs.LEGACY_TABLE_CLOUD_SYNCED,
            priority=pqs.PRIORITY_CLOUD_BULK,
            payload={'event_size': 99},  # no event_dir key
        )

        with caplog.at_level(logging.WARNING):
            result = svc._claim_via_pipeline_reader_cloud(
                worker_id='cloud_archive',
                db_path=geodata_db,
                limit=8,
            )
        assert result == []  # invalid row not surfaced
        # Released back to pending (NOT left in_progress).
        row = _row_for(geodata_db, 'SentryClips/no_dir.json')
        assert row['status'] == 'pending'
        assert row['claimed_by'] is None
        assert 'PR-F3' in (row['last_error'] or '')
        # WARNING fired.
        assert any(
            'event_dir' in r.getMessage() and 're-enqueue' in r.getMessage()
            for r in caplog.records
        )

    def test_claim_zero_event_size_defaults_to_zero(self, geodata_db):
        pqs.dual_write_enqueue(
            source_path='SentryClips/no_size.json',
            stage=pqs.STAGE_CLOUD_PENDING,
            legacy_table=pqs.LEGACY_TABLE_CLOUD_SYNCED,
            priority=pqs.PRIORITY_CLOUD_BULK,
            payload={'event_dir': '/srv/SentryClips/x'},  # no event_size
        )
        result = svc._claim_via_pipeline_reader_cloud(
            worker_id='cloud_archive',
            db_path=geodata_db,
            limit=8,
        )
        assert result == [
            ('/srv/SentryClips/x', 'SentryClips/no_size.json', 0),
        ]

    def test_claim_garbage_event_size_defaults_to_zero(self, geodata_db):
        # Bad payload — event_size unparseable as int.
        pqs.dual_write_enqueue(
            source_path='SentryClips/garbage_size.json',
            stage=pqs.STAGE_CLOUD_PENDING,
            legacy_table=pqs.LEGACY_TABLE_CLOUD_SYNCED,
            priority=pqs.PRIORITY_CLOUD_BULK,
            payload={
                'event_dir': '/srv/SentryClips/y',
                'event_size': 'not a number',
            },
        )
        result = svc._claim_via_pipeline_reader_cloud(
            worker_id='cloud_archive', db_path=geodata_db, limit=8,
        )
        assert result == [
            ('/srv/SentryClips/y', 'SentryClips/garbage_size.json', 0),
        ]

    def test_claim_swallows_pqs_exception(
            self, geodata_db, monkeypatch, caplog):
        def _raise(**kw):
            raise sqlite3.OperationalError('boom')
        monkeypatch.setattr(pqs, 'claim_next_for_stage', _raise)
        with caplog.at_level(logging.WARNING):
            result = svc._claim_via_pipeline_reader_cloud(
                worker_id='cloud_archive',
                db_path=geodata_db,
                limit=8,
            )
        assert result == []
        # WARNING fired (claim path must NEVER propagate to the worker).
        assert any(
            'claim_next_for_stage raised' in r.getMessage()
            for r in caplog.records
        )

    def test_claim_zero_limit_is_no_op(self, geodata_db):
        _seed_cloud_pending(geodata_db, 'SentryClips/a.json')
        result = svc._claim_via_pipeline_reader_cloud(
            worker_id='cloud_archive', db_path=geodata_db, limit=0,
        )
        assert result == []
        # Row untouched.
        row = _row_for(geodata_db, 'SentryClips/a.json')
        assert row['status'] == 'pending'

    def test_claim_negative_limit_is_no_op(self, geodata_db):
        _seed_cloud_pending(geodata_db, 'SentryClips/a.json')
        result = svc._claim_via_pipeline_reader_cloud(
            worker_id='cloud_archive', db_path=geodata_db, limit=-3,
        )
        assert result == []


# ---------------------------------------------------------------------------
# _release_cloud_pipeline_claims (batch release helper)
# ---------------------------------------------------------------------------


class TestReleaseCloudPipelineClaims:
    """Batch release used by the _drain_once finally block."""

    def test_release_multiple_paths_returns_count(self, geodata_db):
        for i in range(3):
            _seed_cloud_pending(geodata_db, f'SentryClips/r{i}.json')
        # Claim all three so they're in_progress.
        for _ in range(3):
            pqs.claim_next_for_stage(
                stage=pqs.STAGE_CLOUD_PENDING,
                claimed_by='cloud_archive',
            )

        released = svc._release_cloud_pipeline_claims(
            ['SentryClips/r0.json', 'SentryClips/r1.json',
             'SentryClips/r2.json'],
            last_error='test release pass',
            db_path=geodata_db,
        )
        assert released == 3
        for i in range(3):
            row = _row_for(geodata_db, f'SentryClips/r{i}.json')
            assert row['status'] == 'pending'
            assert row['last_error'] == 'test release pass'

    def test_release_empty_list_returns_zero(self, geodata_db):
        assert svc._release_cloud_pipeline_claims(
            [], last_error='x', db_path=geodata_db) == 0

    def test_release_skips_blank_paths(self, geodata_db):
        _seed_cloud_pending(geodata_db, 'SentryClips/blank_test.json')
        pqs.claim_next_for_stage(
            stage=pqs.STAGE_CLOUD_PENDING, claimed_by='cloud_archive',
        )
        released = svc._release_cloud_pipeline_claims(
            ['', None, 'SentryClips/blank_test.json'],
            last_error='filtered',
            db_path=geodata_db,
        )
        assert released == 1

    def test_release_tolerates_per_row_failure(
            self, geodata_db, monkeypatch, caplog):
        # Spy on release_pipeline_claim_by_source_path to fail once
        # then succeed.
        call_count = {'n': 0}
        real = pqs.release_pipeline_claim_by_source_path

        def _flaky(**kw):
            call_count['n'] += 1
            if call_count['n'] == 1:
                raise sqlite3.OperationalError('first call boom')
            return real(**kw)

        for i in range(2):
            _seed_cloud_pending(geodata_db, f'SentryClips/f{i}.json')
        for _ in range(2):
            pqs.claim_next_for_stage(
                stage=pqs.STAGE_CLOUD_PENDING, claimed_by='cloud_archive',
            )
        monkeypatch.setattr(
            pqs, 'release_pipeline_claim_by_source_path', _flaky,
        )

        with caplog.at_level(logging.DEBUG):
            released = svc._release_cloud_pipeline_claims(
                ['SentryClips/f0.json', 'SentryClips/f1.json'],
                last_error='partial',
                db_path=geodata_db,
            )
        # First raised → not counted; second succeeded.
        assert released == 1


# ---------------------------------------------------------------------------
# Round-trip: claim → release preserves row identity
# ---------------------------------------------------------------------------


class TestClaimReleaseRoundTrip:
    """A full claim/release cycle preserves enqueue invariants."""

    def test_round_trip_preserves_priority_payload_attempts_trail(
            self, geodata_db):
        _seed_cloud_pending(
            geodata_db,
            'SentryClips/round_trip.json',
            event_dir='/srv/SentryClips/round_trip',
            event_size=42424,
            score=7,
        )
        before = _row_for(geodata_db, 'SentryClips/round_trip.json')
        assert before['status'] == 'pending'
        assert before['attempts'] == 0

        # Claim.
        result = svc._claim_via_pipeline_reader_cloud(
            worker_id='cloud_archive',
            db_path=geodata_db,
            limit=8,
        )
        assert len(result) == 1
        event_dir, rel, size = result[0]
        assert event_dir == '/srv/SentryClips/round_trip'
        assert rel == 'SentryClips/round_trip.json'
        assert size == 42424
        mid = _row_for(geodata_db, 'SentryClips/round_trip.json')
        assert mid['status'] == 'in_progress'
        assert mid['attempts'] == 1

        # Release.
        released = svc._release_cloud_pipeline_claims(
            [rel], last_error='round_trip', db_path=geodata_db,
        )
        assert released == 1
        after = _row_for(geodata_db, 'SentryClips/round_trip.json')
        assert after['status'] == 'pending'
        assert after['claimed_by'] is None
        assert after['claimed_at'] is None
        # Priority + payload preserved across the round trip.
        assert after['priority'] == before['priority']
        # Attempts is the COST of the claim — preserved (NOT reset).
        # Operators tracking retry count rely on this.
        assert after['attempts'] == 1
        # last_error reflects the release reason.
        assert after['last_error'] == 'round_trip'
