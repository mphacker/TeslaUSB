"""Tests for issue #184 Wave 4 PR-F2 (cloud_archive pipeline_queue integration).

PR-F2 lays the foundation for PR-F3's reader cutover by adding three
opt-in flags to ``cloud_archive_service``:

* ``CLOUD_ARCHIVE_ENQUEUE_TO_PIPELINE`` — PRODUCER hook in
  ``_discover_events``: every discovered event is also enqueued into
  ``pipeline_queue`` with ``stage='cloud_pending'`` (idempotent via
  the existing UNIQUE index).
* ``CLOUD_ARCHIVE_SHADOW_PIPELINE_QUEUE`` — OBSERVABILITY: when the
  producer is also on, ``_drain_once`` peeks the top-N
  ``cloud_pending`` rows and logs WARNING if the legacy disk-walk's
  first pick is absent from the pipeline window.
* ``CLOUD_ARCHIVE_USE_PIPELINE_READER`` — RESERVED for PR-F3, read
  here only for the shadow-skip predicate.

These tests verify:

* Each flag predicate (``_enqueue_to_pipeline_enabled`` etc.) reads
  the live config constant and returns False on import failure.
* ``_enqueue_event_to_pipeline`` produces a ``cloud_pending`` row in
  ``pipeline_queue`` with the expected priority + payload, is
  idempotent, swallows errors, and bumps the producer telemetry
  counter only on first insert.
* ``_shadow_compare_cloud_picks`` agreement / disagreement counters
  and rate-limited WARNING logging.
* ``_peek_pipeline_cloud_pending`` returns top-N from pipeline_queue
  and gracefully handles peek failures.
* ``_discover_events`` invokes the producer hook for each scored
  event when the flag is on, and is a complete no-op when the flag
  is off.
* ``get_cloud_shadow_telemetry`` snapshot keys.
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
    # Ensure pqs._resolve_pipeline_db points at our temp DB so the
    # producer hook lands rows in the same DB tests inspect.
    monkeypatch.setattr(pqs, '_resolve_pipeline_db', lambda: db_path)
    return db_path


@pytest.fixture(autouse=True)
def _reset_cloud_telemetry():
    """Reset shadow + producer counters between tests."""
    svc._reset_cloud_shadow_telemetry_for_tests()
    yield
    svc._reset_cloud_shadow_telemetry_for_tests()


@pytest.fixture
def enable_producer(monkeypatch):
    """Turn the producer flag ON for the duration of the test."""
    import config as cfg
    monkeypatch.setattr(cfg, 'CLOUD_ARCHIVE_ENQUEUE_TO_PIPELINE', True)


@pytest.fixture
def disable_producer(monkeypatch):
    """Force the producer flag OFF (default)."""
    import config as cfg
    monkeypatch.setattr(cfg, 'CLOUD_ARCHIVE_ENQUEUE_TO_PIPELINE', False)


@pytest.fixture
def enable_shadow(monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg, 'CLOUD_ARCHIVE_SHADOW_PIPELINE_QUEUE', True)


@pytest.fixture
def disable_shadow(monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg, 'CLOUD_ARCHIVE_SHADOW_PIPELINE_QUEUE', False)


@pytest.fixture
def enable_reader(monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg, 'CLOUD_ARCHIVE_USE_PIPELINE_READER', True)


@pytest.fixture
def disable_reader(monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg, 'CLOUD_ARCHIVE_USE_PIPELINE_READER', False)


# ---------------------------------------------------------------------------
# Flag predicates
# ---------------------------------------------------------------------------


class TestFlagPredicates:
    """Each flag predicate reads the live config and is import-safe."""

    def test_enqueue_to_pipeline_off_by_default(self, disable_producer):
        assert svc._enqueue_to_pipeline_enabled() is False

    def test_enqueue_to_pipeline_on(self, enable_producer):
        assert svc._enqueue_to_pipeline_enabled() is True

    def test_shadow_on_by_default(self, enable_shadow):
        assert svc._shadow_pipeline_queue_enabled() is True

    def test_shadow_off(self, disable_shadow):
        assert svc._shadow_pipeline_queue_enabled() is False

    def test_reader_off_by_default(self, disable_reader):
        assert svc._use_pipeline_reader_enabled() is False

    def test_reader_on(self, enable_reader):
        assert svc._use_pipeline_reader_enabled() is True

    def test_predicates_swallow_import_errors(self, monkeypatch):
        """If config attribute is missing the predicate returns False."""
        import config as cfg
        for attr in (
            'CLOUD_ARCHIVE_ENQUEUE_TO_PIPELINE',
            'CLOUD_ARCHIVE_SHADOW_PIPELINE_QUEUE',
            'CLOUD_ARCHIVE_USE_PIPELINE_READER',
        ):
            if hasattr(cfg, attr):
                monkeypatch.delattr(cfg, attr, raising=False)
        assert svc._enqueue_to_pipeline_enabled() is False
        assert svc._shadow_pipeline_queue_enabled() is False
        assert svc._use_pipeline_reader_enabled() is False


# ---------------------------------------------------------------------------
# Producer hook: _enqueue_event_to_pipeline
# ---------------------------------------------------------------------------


class TestEnqueueEventToPipeline:
    """Producer hook semantics."""

    def test_inserts_cloud_pending_row(self, geodata_db):
        ok = svc._enqueue_event_to_pipeline(
            'SentryClips/2024-01-01_event.json',
            event_dir='/srv/SentryClips/2024-01-01_event',
            event_size=12345,
            score=10,
        )
        assert ok is True
        conn = sqlite3.connect(geodata_db)
        try:
            row = conn.execute(
                "SELECT stage, status, priority, source_path, "
                "       payload_json, legacy_table "
                "  FROM pipeline_queue WHERE source_path = ?",
                ('SentryClips/2024-01-01_event.json',),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        stage, status, priority, source_path, payload, legacy_tbl = row
        assert stage == pqs.STAGE_CLOUD_PENDING
        assert status == 'pending'
        assert priority == pqs.PRIORITY_CLOUD_BULK
        assert source_path == 'SentryClips/2024-01-01_event.json'
        assert legacy_tbl == pqs.LEGACY_TABLE_CLOUD_SYNCED
        # Payload carries the producer metadata for PR-F3 + debugging.
        import json as _json
        decoded = _json.loads(payload)
        assert decoded['event_size'] == 12345
        assert decoded['score'] == 10
        assert decoded['producer'] == 'cloud_archive._discover_events'

    def test_idempotent_re_enqueue_returns_false(self, geodata_db):
        first = svc._enqueue_event_to_pipeline('Sentry/foo.json')
        second = svc._enqueue_event_to_pipeline('Sentry/foo.json')
        assert first is True
        assert second is False  # UNIQUE index suppresses the second insert

    def test_telemetry_counter_only_bumps_on_first_insert(self, geodata_db):
        before = svc.get_cloud_shadow_telemetry()['cloud_pipeline_enqueue_count']
        svc._enqueue_event_to_pipeline('Sentry/a.json')
        after_one = svc.get_cloud_shadow_telemetry()['cloud_pipeline_enqueue_count']
        svc._enqueue_event_to_pipeline('Sentry/a.json')  # dupe
        after_two = svc.get_cloud_shadow_telemetry()['cloud_pipeline_enqueue_count']
        svc._enqueue_event_to_pipeline('Sentry/b.json')  # new
        after_three = svc.get_cloud_shadow_telemetry()['cloud_pipeline_enqueue_count']
        assert after_one == before + 1
        assert after_two == after_one  # dupe must not bump
        assert after_three == after_one + 1

    def test_empty_path_returns_false_without_calling_pqs(
            self, monkeypatch):
        # Patch dual_write_enqueue to detect any call.
        called = {'n': 0}

        def _spy(**kw):
            called['n'] += 1
            return True
        monkeypatch.setattr(pqs, 'dual_write_enqueue', _spy)
        assert svc._enqueue_event_to_pipeline('') is False
        assert called['n'] == 0

    def test_swallows_pqs_exception(self, monkeypatch, caplog):
        def _raises(**kw):
            raise RuntimeError('boom')
        monkeypatch.setattr(pqs, 'dual_write_enqueue', _raises)
        with caplog.at_level(logging.WARNING):
            ok = svc._enqueue_event_to_pipeline('Sentry/foo.json')
        assert ok is False
        # Producer hook MUST NEVER propagate exceptions to the disk-walk.
        assert any(
            'producer hook failed' in r.getMessage()
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Batched producer hook: _enqueue_events_to_pipeline_batch
# ---------------------------------------------------------------------------


class TestEnqueueEventsToPipelineBatch:
    """Batched variant pins the I/O-savings path used by _discover_events."""

    def test_batch_inserts_all_rows_in_one_call(
            self, geodata_db, monkeypatch):
        # Spy on dual_write_enqueue_many to assert exactly one call.
        # Spy on dual_write_enqueue (single-row) to assert ZERO calls
        # — otherwise we'd silently regress to per-row writes.
        many_calls: list = []
        single_calls: list = []
        real_many = pqs.dual_write_enqueue_many

        def _many_spy(rows, db_path=None):
            rows = list(rows)
            many_calls.append(rows)
            return real_many(rows, db_path=db_path)

        def _single_spy(**kw):
            single_calls.append(kw)
            raise AssertionError(
                "dual_write_enqueue must NOT be called from "
                "_enqueue_events_to_pipeline_batch"
            )

        monkeypatch.setattr(pqs, 'dual_write_enqueue_many', _many_spy)
        monkeypatch.setattr(pqs, 'dual_write_enqueue', _single_spy)

        scored = [
            (('/srv/SentryClips/a', 'SentryClips/a', 100), 0),
            (('/srv/SentryClips/b', 'SentryClips/b', 200), 100),
            (('/srv/SentryClips/c', 'SentryClips/c', 300), 200),
        ]
        inserted = svc._enqueue_events_to_pipeline_batch(scored)
        assert inserted == 3
        assert len(many_calls) == 1, \
            "Batched path must collapse N rows into ONE call"
        assert len(single_calls) == 0
        # Telemetry reflects the actual insert count.
        tel = svc.get_cloud_shadow_telemetry()
        assert tel['cloud_pipeline_enqueue_count'] == 3

    def test_batch_idempotent_dupes_do_not_bump_counter(self, geodata_db):
        scored = [
            (('/srv/Sentry/a', 'Sentry/a', 100), 10),
        ]
        first = svc._enqueue_events_to_pipeline_batch(scored)
        second = svc._enqueue_events_to_pipeline_batch(scored)
        assert first == 1
        assert second == 0  # UNIQUE index suppressed
        tel = svc.get_cloud_shadow_telemetry()
        assert tel['cloud_pipeline_enqueue_count'] == 1

    def test_batch_skips_blank_paths_silently(self, geodata_db):
        scored = [
            (('/srv/Sentry/a', 'Sentry/a', 100), 10),
            (('/srv/Sentry/b', '', 200), 20),  # blank path filtered
            (('/srv/Sentry/c', 'Sentry/c', 300), 30),
        ]
        inserted = svc._enqueue_events_to_pipeline_batch(scored)
        assert inserted == 2

    def test_batch_empty_input_short_circuits(
            self, geodata_db, monkeypatch):
        called = {'n': 0}
        def _many_spy(rows, db_path=None):
            called['n'] += 1
            return 0
        monkeypatch.setattr(pqs, 'dual_write_enqueue_many', _many_spy)
        result = svc._enqueue_events_to_pipeline_batch([])
        assert result == 0
        assert called['n'] == 0  # MUST NOT open a connection for nothing

    def test_batch_swallows_pqs_exception(
            self, geodata_db, monkeypatch, caplog):
        def _raises(rows, db_path=None):
            raise RuntimeError('boom')
        monkeypatch.setattr(pqs, 'dual_write_enqueue_many', _raises)
        scored = [(('/srv/Sentry/a', 'Sentry/a', 100), 10)]
        with caplog.at_level(logging.WARNING):
            result = svc._enqueue_events_to_pipeline_batch(scored)
        assert result == 0
        # Batched hook MUST NEVER propagate exceptions to the disk-walk.
        assert any(
            'batched producer hook' in r.getMessage()
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Shadow comparator: _shadow_compare_cloud_picks
# ---------------------------------------------------------------------------


class TestShadowComparePicks:
    """Shadow-mode counters and rate-limited logging."""

    def test_both_empty_counts_as_agreement(self):
        before = svc.get_cloud_shadow_telemetry()
        svc._shadow_compare_cloud_picks(
            legacy_path=None, pipeline_candidates=(),
        )
        after = svc.get_cloud_shadow_telemetry()
        assert after['cloud_shadow_agreement_count'] == \
            before['cloud_shadow_agreement_count'] + 1
        assert after['cloud_shadow_disagreement_count'] == \
            before['cloud_shadow_disagreement_count']

    def test_legacy_pick_in_top_n_counts_as_agreement(self):
        svc._shadow_compare_cloud_picks(
            legacy_path='Sentry/x.json',
            pipeline_candidates=('Sentry/x.json', 'Sentry/y.json'),
        )
        tel = svc.get_cloud_shadow_telemetry()
        assert tel['cloud_shadow_agreement_count'] == 1
        assert tel['cloud_shadow_disagreement_count'] == 0

    def test_legacy_pick_absent_from_top_n_counts_as_disagreement(
            self, caplog):
        with caplog.at_level(logging.WARNING):
            svc._shadow_compare_cloud_picks(
                legacy_path='Sentry/missing.json',
                pipeline_candidates=('Sentry/y.json', 'Sentry/z.json'),
            )
        tel = svc.get_cloud_shadow_telemetry()
        assert tel['cloud_shadow_agreement_count'] == 0
        assert tel['cloud_shadow_disagreement_count'] == 1
        # First N disagreements log verbatim.
        msgs = [r.getMessage() for r in caplog.records]
        assert any('cloud shadow' in m for m in msgs)
        assert any('absent from the top-' in m for m in msgs)

    def test_disagreement_log_is_rate_limited_after_verbatim_threshold(
            self, caplog):
        svc._reset_cloud_shadow_telemetry_for_tests()
        with caplog.at_level(logging.WARNING):
            for i in range(svc._CLOUD_SHADOW_DISAGREEMENT_LOG_VERBATIM + 1):
                svc._shadow_compare_cloud_picks(
                    legacy_path=f'Sentry/{i}.json',
                    pipeline_candidates=('Sentry/other.json',),
                )
        # Exactly _CLOUD_SHADOW_DISAGREEMENT_LOG_VERBATIM verbatim WARNINGs.
        verbatim = [
            r for r in caplog.records
            if 'absent from the top-' in r.getMessage()
        ]
        assert len(verbatim) == svc._CLOUD_SHADOW_DISAGREEMENT_LOG_VERBATIM
        # The (LOG_VERBATIM + 1)-th call doesn't emit a heartbeat (heartbeat
        # fires only when count is a multiple of LOG_EVERY).
        heartbeats = [
            r for r in caplog.records
            if 'disagreement count =' in r.getMessage()
        ]
        assert len(heartbeats) == 0

    def test_heartbeat_fires_at_log_every_threshold(self, caplog):
        svc._reset_cloud_shadow_telemetry_for_tests()
        every = svc._CLOUD_SHADOW_DISAGREEMENT_LOG_EVERY
        verbatim = svc._CLOUD_SHADOW_DISAGREEMENT_LOG_VERBATIM
        # Drive the counter to the next heartbeat boundary.
        # We need enough disagreements to reach `every` cumulative.
        with caplog.at_level(logging.WARNING):
            for i in range(every):
                svc._shadow_compare_cloud_picks(
                    legacy_path=f'Sentry/{i}.json',
                    pipeline_candidates=('Sentry/other.json',),
                )
        verbatim_logs = [
            r for r in caplog.records
            if 'absent from the top-' in r.getMessage()
        ]
        heartbeats = [
            r for r in caplog.records
            if 'disagreement count =' in r.getMessage()
        ]
        assert len(verbatim_logs) == verbatim
        # When LOG_EVERY > LOG_VERBATIM, exactly one heartbeat fires
        # at the LOG_EVERY mark.
        assert len(heartbeats) == 1

    def test_agreement_log_fires_at_log_every_threshold(self, caplog):
        svc._reset_cloud_shadow_telemetry_for_tests()
        every = svc._CLOUD_SHADOW_AGREEMENT_LOG_EVERY
        with caplog.at_level(logging.INFO):
            for _ in range(every):
                svc._shadow_compare_cloud_picks(
                    legacy_path=None, pipeline_candidates=(),
                )
        agreement_logs = [
            r for r in caplog.records
            if 'agreed with cloud_archive' in r.getMessage()
        ]
        assert len(agreement_logs) == 1

    def test_telemetry_snapshot_keys(self):
        snap = svc.get_cloud_shadow_telemetry()
        assert set(snap.keys()) == {
            'cloud_shadow_agreement_count',
            'cloud_shadow_disagreement_count',
            'cloud_pipeline_enqueue_count',
        }


# ---------------------------------------------------------------------------
# Peek wrapper: _peek_pipeline_cloud_pending
# ---------------------------------------------------------------------------


class TestPeekPipelineCloudPending:
    def test_returns_top_n_paths_in_priority_order(self, geodata_db):
        # Enqueue three rows; pipeline_queue orders by priority, enqueued_at.
        for path in ('Sentry/a.json', 'Sentry/b.json', 'Sentry/c.json'):
            pqs.dual_write_enqueue(
                source_path=path,
                stage=pqs.STAGE_CLOUD_PENDING,
                legacy_table=pqs.LEGACY_TABLE_CLOUD_SYNCED,
                priority=pqs.PRIORITY_CLOUD_BULK,
            )
        result = svc._peek_pipeline_cloud_pending(limit=3)
        assert isinstance(result, tuple)
        assert set(result) == {
            'Sentry/a.json', 'Sentry/b.json', 'Sentry/c.json',
        }

    def test_returns_empty_tuple_when_no_rows(self, geodata_db):
        assert svc._peek_pipeline_cloud_pending() == ()

    def test_swallows_peek_exception(self, monkeypatch, caplog):
        def _raises(**kw):
            raise sqlite3.OperationalError('database is locked')
        monkeypatch.setattr(pqs, 'peek_top_n_paths_for_stage', _raises)
        with caplog.at_level(logging.DEBUG):
            result = svc._peek_pipeline_cloud_pending()
        assert result == ()


# ---------------------------------------------------------------------------
# _discover_events producer integration
# ---------------------------------------------------------------------------


class TestDiscoverEventsProducerIntegration:
    """The producer hook fires only when the flag is ON.

    Uses real temp directories (small files only) since the disk-walk
    is inline in ``_discover_events``; mocking would require patching
    ``os.listdir`` / ``os.path.isfile`` / ``os.path.getsize`` which
    is more brittle than just creating two small fake event dirs.
    """

    @pytest.fixture
    def teslacam_root(self, tmp_path):
        """Build a TeslaCam-shaped tree with two SentryClips events."""
        sentry = tmp_path / 'SentryClips'
        for entry in ('2024-01-01_event_a', '2024-01-02_event_b'):
            event_dir = sentry / entry
            event_dir.mkdir(parents=True)
            (event_dir / 'event.json').write_text(
                '{"timestamp": "2024-01-01T00:00:00", "reason": "sentry_aware_object_detection"}'
            )
            (event_dir / 'front.mp4').write_bytes(b'fake')
        return str(tmp_path)

    @pytest.fixture
    def cloud_conn(self, tmp_path):
        """Empty cloud_synced_files connection so nothing is filtered."""
        cloud_db = str(tmp_path / 'cloud_sync.db')
        conn = sqlite3.connect(cloud_db)
        conn.executescript(
            """
            CREATE TABLE cloud_synced_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL UNIQUE,
                file_size INTEGER, file_mtime REAL, remote_path TEXT,
                status TEXT DEFAULT 'pending',
                synced_at TEXT, retry_count INTEGER DEFAULT 0,
                last_error TEXT
            );
            """
        )
        conn.commit()
        yield conn
        conn.close()

    def test_hook_fires_for_each_event_when_flag_on(
            self, geodata_db, enable_producer, monkeypatch,
            teslacam_root, cloud_conn):
        # Restrict to SentryClips so the test tree matches.
        monkeypatch.setattr(
            svc, 'CLOUD_ARCHIVE_SYNC_FOLDERS', ['SentryClips'],
        )
        # Skip the geo-hit DB lookup and the live YAML re-read to keep
        # the test hermetic.
        monkeypatch.setattr(svc, '_load_geo_hits', lambda: None)
        monkeypatch.setattr(
            svc, '_read_sync_non_event_setting', lambda: True,
        )
        # Force a low score so all events pass the < 200 filter.
        monkeypatch.setattr(
            svc, '_score_event_priority',
            lambda dir_path, **kw: 10,
        )

        result = svc._discover_events(teslacam_root, conn=cloud_conn)

        # Both fake events should be in the result.
        assert len(result) == 2
        # And both should be in pipeline_queue under cloud_pending.
        gconn = sqlite3.connect(geodata_db)
        try:
            rows = gconn.execute(
                "SELECT source_path FROM pipeline_queue "
                " WHERE stage = ? ORDER BY source_path",
                (pqs.STAGE_CLOUD_PENDING,),
            ).fetchall()
        finally:
            gconn.close()
        paths = [r[0] for r in rows]
        assert paths == [
            'SentryClips/2024-01-01_event_a',
            'SentryClips/2024-01-02_event_b',
        ]
        # Telemetry should reflect 2 inserts.
        tel = svc.get_cloud_shadow_telemetry()
        assert tel['cloud_pipeline_enqueue_count'] == 2

    def test_hook_does_not_fire_when_flag_off(
            self, geodata_db, disable_producer, monkeypatch,
            teslacam_root, cloud_conn):
        monkeypatch.setattr(
            svc, 'CLOUD_ARCHIVE_SYNC_FOLDERS', ['SentryClips'],
        )
        monkeypatch.setattr(svc, '_load_geo_hits', lambda: None)
        monkeypatch.setattr(
            svc, '_read_sync_non_event_setting', lambda: True,
        )
        monkeypatch.setattr(
            svc, '_score_event_priority', lambda d, **kw: 10,
        )

        result = svc._discover_events(teslacam_root, conn=cloud_conn)
        # Discovery still works.
        assert len(result) == 2

        gconn = sqlite3.connect(geodata_db)
        try:
            count = gconn.execute(
                "SELECT COUNT(*) FROM pipeline_queue WHERE stage = ?",
                (pqs.STAGE_CLOUD_PENDING,),
            ).fetchone()[0]
        finally:
            gconn.close()
        assert count == 0  # producer hook stayed quiet
        tel = svc.get_cloud_shadow_telemetry()
        assert tel['cloud_pipeline_enqueue_count'] == 0


# ---------------------------------------------------------------------------
# Priority-collapse / score-mismatch shadow scenarios
# ---------------------------------------------------------------------------


class TestShadowScoreCollapseScenarios:
    """All cloud_pending rows share priority PRIORITY_CLOUD_BULK; the
    intra-band order collapses to enqueued_at. Pin the resulting
    legitimate disagreement scenarios so PR-F3 can re-rank by payload
    score without breaking the shadow contract."""

    def test_legacy_picks_high_score_event_arriving_after_backlog(
            self, geodata_db):
        """A late-arriving high-priority event lands at bottom of pipeline.

        Scenario:
        1. Producer enqueues 12 low-score (geo) events first (oldest
           enqueued_at). All sit in pipeline at PRIORITY_CLOUD_BULK.
        2. Producer enqueues 1 high-score Sentry event last.
        3. Legacy disk-walk reorders by score and picks the Sentry
           event first.
        4. Pipeline's top-8 (peek window) is dominated by the older
           backlog rows; the Sentry event ranks #13 by enqueued_at.
        5. Shadow comparator sees legacy_pick='Sentry/...' is ABSENT
           from the pipeline top-8 → disagreement counter increments.

        This is the documented "score-collapse near the window
        boundary" case from the WARNING text. PR-F3 must use
        payload['score'] for re-ranking to eliminate it.
        """
        # Enqueue 12 backlog rows first.
        for i in range(12):
            pqs.dual_write_enqueue(
                source_path=f'SentryClips/backlog_{i:03d}',
                stage=pqs.STAGE_CLOUD_PENDING,
                legacy_table=pqs.LEGACY_TABLE_CLOUD_SYNCED,
                priority=pqs.PRIORITY_CLOUD_BULK,
                payload={'score': 200},  # geo-only score
            )
        # Enqueue the late high-score Sentry event.
        pqs.dual_write_enqueue(
            source_path='SentryClips/late_sentry',
            stage=pqs.STAGE_CLOUD_PENDING,
            legacy_table=pqs.LEGACY_TABLE_CLOUD_SYNCED,
            priority=pqs.PRIORITY_CLOUD_BULK,
            payload={'score': 0},  # event score
        )

        # Pipeline peek returns top-8 ordered by enqueued_at.
        top_n = svc._peek_pipeline_cloud_pending(limit=8)
        assert 'SentryClips/late_sentry' not in top_n, \
            "test premise — late-enqueued row must be outside top-8"

        # Legacy reader picks the Sentry event (lowest score wins).
        before = svc.get_cloud_shadow_telemetry()
        svc._shadow_compare_cloud_picks(
            legacy_path='SentryClips/late_sentry',
            pipeline_candidates=top_n,
        )
        after = svc.get_cloud_shadow_telemetry()
        # This is the disagreement that PR-F3 will address.
        assert after['cloud_shadow_disagreement_count'] == \
            before['cloud_shadow_disagreement_count'] + 1
        assert after['cloud_shadow_agreement_count'] == \
            before['cloud_shadow_agreement_count']

    def test_legacy_pick_within_top_n_window_when_band_is_small(
            self, geodata_db):
        """When backlog fits in the top-N window, no disagreement fires.

        With ≤ 8 rows total in cloud_pending, the legacy pick is
        guaranteed to be in the peek window regardless of score
        ranking. Pins the absence of false-positive disagreements
        on a typical (small) drain.
        """
        # Enqueue 5 events in arbitrary order with mixed scores.
        events_and_scores = [
            ('SentryClips/c', 200),
            ('SentryClips/a', 0),
            ('SentryClips/e', 100),
            ('SentryClips/b', 50),
            ('SentryClips/d', 150),
        ]
        for path, score in events_and_scores:
            pqs.dual_write_enqueue(
                source_path=path,
                stage=pqs.STAGE_CLOUD_PENDING,
                legacy_table=pqs.LEGACY_TABLE_CLOUD_SYNCED,
                priority=pqs.PRIORITY_CLOUD_BULK,
                payload={'score': score},
            )

        # Legacy picks the lowest-score event.
        legacy_pick = 'SentryClips/a'
        top_n = svc._peek_pipeline_cloud_pending(limit=8)
        assert legacy_pick in top_n  # band fits in window

        before = svc.get_cloud_shadow_telemetry()
        svc._shadow_compare_cloud_picks(
            legacy_path=legacy_pick,
            pipeline_candidates=top_n,
        )
        after = svc.get_cloud_shadow_telemetry()
        # Agreement: small backlog never produces false positives.
        assert after['cloud_shadow_agreement_count'] == \
            before['cloud_shadow_agreement_count'] + 1
        assert after['cloud_shadow_disagreement_count'] == \
            before['cloud_shadow_disagreement_count']

    def test_warning_text_mentions_priority_collapse(
            self, geodata_db, caplog):
        """The disagreement WARNING explicitly cites the score-collapse
        cause so an operator reading the journal can immediately tell
        whether the miss is benign (transient reorder) or real
        (producer-hook gap).
        """
        svc._reset_cloud_shadow_telemetry_for_tests()
        with caplog.at_level(logging.WARNING):
            svc._shadow_compare_cloud_picks(
                legacy_path='SentryClips/missed',
                pipeline_candidates=('SentryClips/other',),
            )
        # The first verbatim WARNING must include the priority-collapse
        # explanation so operators can self-diagnose without reading
        # source.
        verbatim = [
            r.getMessage() for r in caplog.records
            if 'absent from the top-' in r.getMessage()
        ]
        assert len(verbatim) == 1
        msg = verbatim[0]
        assert 'PRIORITY_CLOUD_BULK' in msg
        assert 'enqueued_at' in msg
        assert ('producer-hook gap' in msg
                or 'producer hook gap' in msg)

