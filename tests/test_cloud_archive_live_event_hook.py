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
11. Cross-producer dedup parity (the regression PR-F4 review caught):
    ``_canonical_rel_path_from_local(event_dir)`` must equal
    ``canonical_cloud_path(f"SentryClips/<basename>")`` so the bulk
    discovery row collides with the live-hook row on the
    ``idx_pipeline_source_unique`` UNIQUE index — never two uploads.
12. The basename fallback in ``_canonical_rel_path_from_local`` emits
    a WARNING so a misconfigured RO_MNT_DIR/ARCHIVE_DIR is visible in
    journalctl (silent collapse would otherwise look like successful
    enqueues that secretly all alias the same row).
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


# ---------------------------------------------------------------------------
# Cross-producer dedup parity (the regression PR-F4 review caught)
# ---------------------------------------------------------------------------

class TestCrossProducerDedupParity:
    """Pin the canonical-key contract that prevents double-uploads.

    Two producers can enqueue the same Tesla event:

    1. The file_watcher's ``register_event_json_callback`` →
       :func:`enqueue_live_event_from_event_json` (LIVE priority).
    2. The bulk discovery pass in :func:`_discover_events`
       (BULK priority).

    The ``pipeline_queue.idx_pipeline_source_unique`` UNIQUE index is
    keyed on ``(stage, source_path)``. If the two producers compute
    different canonical forms for the same event, both rows survive
    and the worker uploads the event twice — once at LIVE, once at
    BULK. The first PR-F4 implementation hit this exact bug because
    the live hook canonicalised the event.json path while the bulk
    producer canonicalised the event directory. These tests pin the
    parity so a regression cannot silently double-upload.
    """

    def test_live_hook_canonical_form_equals_bulk_form(
        self, fresh_pipeline_db, reset_wake, tmp_path, monkeypatch,
    ):
        # Set RO_MNT_DIR to a tmp path that mirrors the production
        # layout so _canonical_rel_path_from_local follows its real
        # prefix-strip code path (no mocks).
        ro_mnt = str(tmp_path / 'mnt' / 'gadget')
        teslacam = os.path.join(ro_mnt, 'part1-ro', 'TeslaCam')
        sentry = os.path.join(teslacam, 'SentryClips')
        os.makedirs(sentry, exist_ok=True)
        import config as cfg
        monkeypatch.setattr(cfg, 'RO_MNT_DIR', ro_mnt, raising=False)
        monkeypatch.setattr(cfg, 'ARCHIVE_DIR',
                            str(tmp_path / 'unrelated'), raising=False)

        event_basename = '2026-05-12_11-00-00'
        event_dir = _make_event_dir(sentry, event_basename)

        # The live-hook canonical form (what enqueue_live_event_*
        # writes to source_path) — derived from the event DIRECTORY,
        # not the event.json file inside it.
        live_form = svc._canonical_rel_path_from_local(event_dir)

        # The bulk-discovery canonical form (what _discover_events
        # writes to source_path).
        bulk_form = svc.canonical_cloud_path(
            f'SentryClips/{event_basename}'
        )

        assert live_form == bulk_form, (
            f"Cross-producer canonical-key mismatch! "
            f"live={live_form!r} bulk={bulk_form!r} — "
            f"would cause double-upload via the UNIQUE-index miss."
        )
        assert live_form == f'SentryClips/{event_basename}'

    def test_live_then_bulk_collide_on_unique_index(
        self, fresh_pipeline_db, reset_wake, tmp_path, monkeypatch,
    ):
        """End-to-end: enqueue via live hook, then enqueue the SAME
        event via the bulk producer's canonical form. The UNIQUE
        index on ``(stage, source_path)`` must collapse them — only
        ONE row exists in pipeline_queue.
        """
        ro_mnt = str(tmp_path / 'mnt' / 'gadget')
        teslacam = os.path.join(ro_mnt, 'part1-ro', 'TeslaCam')
        sentry = os.path.join(teslacam, 'SentryClips')
        os.makedirs(sentry, exist_ok=True)
        import config as cfg
        monkeypatch.setattr(cfg, 'RO_MNT_DIR', ro_mnt, raising=False)
        monkeypatch.setattr(cfg, 'ARCHIVE_DIR',
                            str(tmp_path / 'unrelated'), raising=False)

        event_basename = '2026-05-12_11-00-00'
        event_dir = _make_event_dir(sentry, event_basename)

        # Producer 1: live hook fires first.
        n_live = svc.enqueue_live_event_from_event_json(
            [os.path.join(event_dir, 'event.json')]
        )
        assert n_live == 1

        # Producer 2: bulk discovery for the same event. We invoke
        # the same internal helper _discover_events uses so the test
        # exercises the real cross-producer collision path.
        bulk_rel = svc.canonical_cloud_path(
            f'SentryClips/{event_basename}'
        )
        try:
            event_size = sum(
                os.path.getsize(os.path.join(event_dir, n))
                for n in os.listdir(event_dir)
                if os.path.isfile(os.path.join(event_dir, n))
            )
        except OSError:
            event_size = 0
        from services import pipeline_queue_service as pqs
        ok = svc._enqueue_event_to_pipeline(
            bulk_rel,
            event_dir=event_dir,
            event_size=event_size,
            score=None,
            priority=pqs.PRIORITY_CLOUD_BULK,
            producer='_discover_events',
        )
        # The UNIQUE index returns False from _enqueue_event_to_pipeline
        # because the row already exists — that's the dedup signal.
        assert ok is False, (
            "Bulk producer's row was inserted as a SEPARATE row — "
            "the UNIQUE index did NOT dedup, so the worker would "
            "upload this event twice."
        )

        # Confirm exactly ONE row exists in pipeline_queue for this
        # event (across both producers).
        conn = sqlite3.connect(fresh_pipeline_db)
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM pipeline_queue "
                "WHERE source_path = ? AND stage = ?",
                (bulk_rel, pqs.STAGE_CLOUD_PENDING),
            )
            count = cur.fetchone()[0]
        finally:
            conn.close()
        assert count == 1, (
            f"Expected exactly 1 row, got {count} — UNIQUE-index "
            f"dedup failed across producers."
        )

    def test_basename_fallback_logs_warning(
        self, monkeypatch, tmp_path, caplog,
    ):
        """Info #2: the basename fallback in
        ``_canonical_rel_path_from_local`` must emit a WARNING so a
        misconfigured deploy is visible in journalctl.
        """
        import logging
        import config as cfg
        monkeypatch.setattr(cfg, 'RO_MNT_DIR',
                            str(tmp_path / 'nope1'), raising=False)
        monkeypatch.setattr(cfg, 'ARCHIVE_DIR',
                            str(tmp_path / 'nope2'), raising=False)

        local = str(tmp_path / 'totally' / 'unrelated' / 'file.mp4')
        with caplog.at_level(logging.WARNING,
                             logger=svc.logger.name):
            rel = svc._canonical_rel_path_from_local(local)
        assert rel == 'file.mp4'
        # Find the warning we emitted (other unrelated warnings may
        # also be present).
        matched = [r for r in caplog.records
                   if r.levelno == logging.WARNING
                   and 'is not under any known TeslaCam root' in r.message]
        assert matched, (
            "Expected a WARNING about the unknown TeslaCam root in "
            f"caplog. Got records: {[r.message for r in caplog.records]}"
        )
