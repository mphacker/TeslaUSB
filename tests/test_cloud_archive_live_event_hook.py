"""Wave 4 PR-F4 (issue #184) — live-event enqueue helper tests.

The standalone ``live_event_sync_service`` worker has been deleted.
The file_watcher's ``register_event_json_callback`` now invokes
:func:`cloud_archive_service.enqueue_live_event_from_event_json`,
which mirrors each Tesla ``event.json`` into ``pipeline_queue`` at
``PRIORITY_LIVE_EVENT`` so the unified cloud worker picks it up
ahead of bulk catch-up rows.

These tests pin the contract of the new helper:

1. Empty input is a quiet no-op (return 0, no DB writes).
2. Missing event_dir is silently dropped (no crash).
3. Successful enqueue inserts at ``PRIORITY_LIVE_EVENT`` and the
   ``producer`` field identifies the file_watcher path.
4. Re-enqueue is idempotent (UNIQUE index dedups; helper does not
   double-count).
5. ``_wake`` is set when at least one row was inserted so the worker
   doesn't sit on its idle timeout.
6. ``_wake`` is NOT set when nothing was inserted (avoids spurious
   wakes on idempotent re-fires).
7. ``_canonical_rel_path_from_local`` strips the RO mount prefix.
8. ``_canonical_rel_path_from_local`` strips the ArchivedClips prefix.
9. ``_canonical_rel_path_from_local`` falls back to basename when no
   prefix matches (so we never crash on an unexpected path shape).
10. The helper NEVER raises — even when ``_enqueue_event_to_pipeline``
    blows up for one entry, the rest of the batch still processes.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from typing import List

import pytest

from services import cloud_archive_service as svc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_pipeline_db(tmp_path, monkeypatch):
    """Build an isolated geodata.db with the pipeline_queue schema."""
    db_path = str(tmp_path / "geodata.db")
    # Force every helper that resolves the geodata path to land here.
    import config as cfg
    monkeypatch.setattr(cfg, "GEODATA_DB", db_path, raising=False)
    monkeypatch.setattr(cfg, "MAPPING_DB_PATH", db_path, raising=False)

    from services.mapping_migrations import _init_db
    conn = _init_db(db_path)
    conn.close()
    return db_path


@pytest.fixture
def reset_wake():
    """Each test starts with the wake event cleared."""
    svc._wake.clear()
    yield
    svc._wake.clear()


def _make_event_dir(parent: str, name: str, with_video: bool = True,
                    with_event_json: bool = True) -> str:
    event_dir = os.path.join(parent, name)
    os.makedirs(event_dir, exist_ok=True)
    if with_video:
        with open(os.path.join(event_dir, "front.mp4"), "wb") as f:
            f.write(b"\x00" * 1024)
    if with_event_json:
        with open(os.path.join(event_dir, "event.json"), "w") as f:
            f.write('{"reason":"sentry_aware_object_detection"}')
    return event_dir


# ---------------------------------------------------------------------------
# enqueue_live_event_from_event_json — empty / missing inputs
# ---------------------------------------------------------------------------

class TestEnqueueLiveEventEmptyInput:
    def test_empty_list_returns_zero(self, fresh_pipeline_db, reset_wake):
        assert svc.enqueue_live_event_from_event_json([]) == 0
        assert not svc._wake.is_set()

    def test_none_path_in_list_skipped(self, fresh_pipeline_db, reset_wake):
        # A literal empty string falls through the inner ``if not path``
        # check; the helper must not crash.
        assert svc.enqueue_live_event_from_event_json(["", None]) == 0  # type: ignore[list-item]

    def test_missing_event_dir_dropped(self, fresh_pipeline_db, reset_wake,
                                       tmp_path):
        # Path looks plausible but the directory doesn't exist.
        bogus = str(tmp_path / "nope" / "event.json")
        assert svc.enqueue_live_event_from_event_json([bogus]) == 0
        assert not svc._wake.is_set()


# ---------------------------------------------------------------------------
# enqueue_live_event_from_event_json — happy path & priority
# ---------------------------------------------------------------------------

class TestEnqueueLiveEventHappyPath:
    def test_successful_enqueue_uses_live_priority(
        self, fresh_pipeline_db, reset_wake, tmp_path, monkeypatch,
    ):
        # Spy on the inner per-row enqueuer so we can verify the
        # ``priority`` and ``producer`` arguments without depending on
        # the row actually landing in the DB (covered separately).
        captured = {}

        def fake_enqueue(rel_path, *, event_dir, event_size, score,
                         priority, producer):
            captured.update({
                'rel_path': rel_path,
                'event_dir': event_dir,
                'event_size': event_size,
                'priority': priority,
                'producer': producer,
            })
            return True

        monkeypatch.setattr(svc, '_enqueue_event_to_pipeline', fake_enqueue)
        # Force a known canonical path so the assertion is deterministic
        # regardless of the local RO_MNT_DIR/ARCHIVE_DIR config values.
        monkeypatch.setattr(
            svc, '_canonical_rel_path_from_local',
            lambda p: 'SentryClips/2026-05-12_11-00-00/event.json',
        )

        event_dir = _make_event_dir(str(tmp_path), 'evt1')
        n = svc.enqueue_live_event_from_event_json(
            [os.path.join(event_dir, 'event.json')]
        )

        from services import pipeline_queue_service as pqs
        assert n == 1
        assert captured['priority'] == pqs.PRIORITY_LIVE_EVENT
        assert captured['producer'] == 'file_watcher.event_json'
        assert captured['event_dir'] == event_dir
        assert captured['event_size'] > 0
        assert captured['rel_path'].endswith('event.json')

    def test_wake_set_when_at_least_one_inserted(
        self, fresh_pipeline_db, reset_wake, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(svc, '_enqueue_event_to_pipeline',
                            lambda *a, **kw: True)
        monkeypatch.setattr(
            svc, '_canonical_rel_path_from_local',
            lambda p: 'SentryClips/x/event.json',
        )
        event_dir = _make_event_dir(str(tmp_path), 'evt1')
        svc.enqueue_live_event_from_event_json(
            [os.path.join(event_dir, 'event.json')]
        )
        assert svc._wake.is_set()

    def test_wake_not_set_when_nothing_inserted(
        self, fresh_pipeline_db, reset_wake, tmp_path, monkeypatch,
    ):
        # Idempotent re-fire: every enqueue returns False.
        monkeypatch.setattr(svc, '_enqueue_event_to_pipeline',
                            lambda *a, **kw: False)
        monkeypatch.setattr(
            svc, '_canonical_rel_path_from_local',
            lambda p: 'SentryClips/x/event.json',
        )
        event_dir = _make_event_dir(str(tmp_path), 'evt1')
        n = svc.enqueue_live_event_from_event_json(
            [os.path.join(event_dir, 'event.json')]
        )
        assert n == 0
        assert not svc._wake.is_set()


# ---------------------------------------------------------------------------
# enqueue_live_event_from_event_json — failure containment
# ---------------------------------------------------------------------------

class TestEnqueueLiveEventFailureContainment:
    def test_per_row_exception_does_not_break_batch(
        self, fresh_pipeline_db, reset_wake, tmp_path, monkeypatch,
    ):
        calls = []

        def flaky_enqueue(rel_path, *, event_dir, event_size, score,
                          priority, producer):
            calls.append(rel_path)
            if 'evt1' in event_dir:
                raise RuntimeError("simulated DB hiccup")
            return True

        monkeypatch.setattr(svc, '_enqueue_event_to_pipeline', flaky_enqueue)
        monkeypatch.setattr(
            svc, '_canonical_rel_path_from_local',
            lambda p: f'SentryClips/{os.path.basename(os.path.dirname(p))}/event.json',
        )

        d1 = _make_event_dir(str(tmp_path), 'evt1')
        d2 = _make_event_dir(str(tmp_path), 'evt2')

        n = svc.enqueue_live_event_from_event_json([
            os.path.join(d1, 'event.json'),
            os.path.join(d2, 'event.json'),
        ])

        # evt1 raised, evt2 succeeded — n should be 1 and both attempted.
        assert n == 1
        assert len(calls) == 2

    def test_pipeline_priority_constant_unavailable_returns_zero(
        self, fresh_pipeline_db, reset_wake, tmp_path, monkeypatch,
    ):
        # Simulate a pipeline_queue_service that lacks
        # ``PRIORITY_LIVE_EVENT`` (e.g., a partially-loaded module
        # during a hot-reload). The helper must early-return 0 without
        # raising and without enqueuing anything.
        from services import pipeline_queue_service as pqs
        # Use ``raising=False`` so the test still succeeds even on a
        # future build that exposes the constant differently.
        monkeypatch.delattr(pqs, 'PRIORITY_LIVE_EVENT', raising=False)

        # If the early-return failed, _enqueue_event_to_pipeline would
        # be invoked; spy to confirm it wasn't.
        called = []
        monkeypatch.setattr(
            svc, '_enqueue_event_to_pipeline',
            lambda *a, **kw: called.append(1) or True,
        )

        event_dir = _make_event_dir(str(tmp_path), 'evt1')
        n = svc.enqueue_live_event_from_event_json(
            [os.path.join(event_dir, 'event.json')]
        )
        assert n == 0
        assert called == []


# ---------------------------------------------------------------------------
# _canonical_rel_path_from_local
# ---------------------------------------------------------------------------

class TestCanonicalRelPathFromLocal:
    def test_strips_ro_mount_prefix(self, monkeypatch, tmp_path):
        ro_mnt = str(tmp_path / 'mnt' / 'gadget')
        os.makedirs(os.path.join(ro_mnt, 'part1-ro', 'TeslaCam',
                                 'SentryClips', 'evt1'),
                    exist_ok=True)
        import config as cfg
        monkeypatch.setattr(cfg, 'RO_MNT_DIR', ro_mnt, raising=False)
        # ARCHIVE_DIR set to a path that does NOT contain the abs path
        # so only the RO-mount candidate matches.
        monkeypatch.setattr(cfg, 'ARCHIVE_DIR',
                            str(tmp_path / 'unrelated'), raising=False)

        local = os.path.join(
            ro_mnt, 'part1-ro', 'TeslaCam', 'SentryClips',
            'evt1', 'event.json',
        )
        rel = svc._canonical_rel_path_from_local(local)
        assert rel == 'SentryClips/evt1/event.json'

    def test_strips_archive_dir_prefix(self, monkeypatch, tmp_path):
        archive = str(tmp_path / 'archived')
        os.makedirs(archive, exist_ok=True)
        import config as cfg
        # RO_MNT_DIR pointed somewhere unrelated so only ARCHIVE_DIR
        # matches.
        monkeypatch.setattr(cfg, 'RO_MNT_DIR',
                            str(tmp_path / 'nowhere'), raising=False)
        monkeypatch.setattr(cfg, 'ARCHIVE_DIR', archive, raising=False)

        local = os.path.join(
            archive, '2026-05-12_11-00-00-front.mp4'
        )
        rel = svc._canonical_rel_path_from_local(local)
        assert rel == '2026-05-12_11-00-00-front.mp4'

    def test_unknown_prefix_falls_back_to_basename(self, monkeypatch,
                                                   tmp_path):
        import config as cfg
        monkeypatch.setattr(cfg, 'RO_MNT_DIR',
                            str(tmp_path / 'nope1'), raising=False)
        monkeypatch.setattr(cfg, 'ARCHIVE_DIR',
                            str(tmp_path / 'nope2'), raising=False)

        local = str(tmp_path / 'totally' / 'unrelated' / 'file.mp4')
        rel = svc._canonical_rel_path_from_local(local)
        assert rel == 'file.mp4'

    def test_uses_posix_separators(self, monkeypatch, tmp_path):
        """The pipeline_queue UNIQUE index is keyed on the canonical
        POSIX form. On Windows test runs the helper must convert
        ``\\`` to ``/`` so the dedup behaves identically.
        """
        ro_mnt = str(tmp_path / 'mnt')
        os.makedirs(os.path.join(ro_mnt, 'part1-ro', 'TeslaCam',
                                 'A', 'B'), exist_ok=True)
        import config as cfg
        monkeypatch.setattr(cfg, 'RO_MNT_DIR', ro_mnt, raising=False)
        monkeypatch.setattr(cfg, 'ARCHIVE_DIR',
                            str(tmp_path / 'unrelated'), raising=False)
        local = os.path.join(ro_mnt, 'part1-ro', 'TeslaCam', 'A', 'B',
                             'event.json')
        rel = svc._canonical_rel_path_from_local(local)
        assert '\\' not in rel
        assert rel == 'A/B/event.json'


# ---------------------------------------------------------------------------
# Integration — enqueue actually lands in pipeline_queue
# ---------------------------------------------------------------------------

class TestEnqueueLandsInPipelineQueue:
    def test_real_row_inserted_at_live_priority(
        self, fresh_pipeline_db, reset_wake, tmp_path, monkeypatch,
    ):
        # No mocks on _enqueue_event_to_pipeline — exercise the full
        # path including the dual-write into pipeline_queue.
        monkeypatch.setattr(
            svc, '_canonical_rel_path_from_local',
            lambda p: 'SentryClips/integration_evt/event.json',
        )

        event_dir = _make_event_dir(str(tmp_path), 'integration_evt')
        n = svc.enqueue_live_event_from_event_json(
            [os.path.join(event_dir, 'event.json')]
        )
        assert n == 1

        # The row must exist in pipeline_queue at PRIORITY_LIVE_EVENT.
        from services import pipeline_queue_service as pqs
        conn = sqlite3.connect(fresh_pipeline_db)
        try:
            cur = conn.execute(
                "SELECT stage, priority, status, source_path "
                "FROM pipeline_queue "
                "WHERE source_path = ?",
                ('SentryClips/integration_evt/event.json',),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None
        stage, priority, status, source_path = row
        assert stage == pqs.STAGE_CLOUD_PENDING
        assert priority == pqs.PRIORITY_LIVE_EVENT
        assert status == 'pending'
