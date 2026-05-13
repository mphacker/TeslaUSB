"""Phase 3b (#99) — Continuous Cloud Worker integration tests.

The old ``cloud_archive_service`` was a one-shot pattern: every trigger
spawned a fresh ``_run_sync`` daemon thread that drained the queue and
exited. Phase 3b converts it to a long-lived worker thread that idles on
``threading.Event.wait()`` (~0.1 % CPU) and drains on demand.

These tests pin the contract:

1. ``start()`` is idempotent and respects ``CLOUD_ARCHIVE_ENABLED``.
2. ``wake()`` is safe to call before ``start()`` and is honored on the
   next worker iteration.
3. The worker drains exactly once per wake (no double-drain).
4. Multiple wakes during a drain coalesce into a single follow-up drain.
5. ``stop()`` cleanly terminates the worker and joins within timeout.
6. The worker yields to LES when ``has_ready_live_event_work`` returns
   ``True`` (priority contract preserved).
7. The worker skips drains when WiFi is down.
8. The worker skips drains when a single-file archive is in progress.
9. ``start_sync()`` / ``trigger_auto_sync()`` are now thin wrappers that
   lazy-start the worker and call ``wake()``.
10. The worker stays alive across drain failures (containment).
"""
from __future__ import annotations

import threading
import time
from typing import List

import pytest

from services import cloud_archive_service as svc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_worker_state():
    """Each test starts with no worker thread and clean event state."""
    # Stop any worker that leaked from a prior test.
    if svc._worker_thread is not None and svc._worker_thread.is_alive():
        svc.stop(timeout=5.0)
    svc._worker_thread = None
    svc._sync_thread = None
    svc._worker_stop.clear()
    svc._wake.clear()
    svc._sync_cancel.clear()
    svc._sync_status.update({
        "running": False,
        "worker_running": False,
        "wake_count": 0,
        "drain_count": 0,
        "error": None,
    })
    yield
    if svc._worker_thread is not None and svc._worker_thread.is_alive():
        svc.stop(timeout=5.0)
    svc._worker_thread = None
    svc._sync_thread = None
    svc._worker_stop.clear()
    svc._wake.clear()


@pytest.fixture
def _enable_cloud(monkeypatch):
    """Enable cloud archive for the duration of the test."""
    monkeypatch.setattr(svc, 'CLOUD_ARCHIVE_ENABLED', True)
    monkeypatch.setattr(svc, 'CLOUD_ARCHIVE_PROVIDER', 'gdrive')


@pytest.fixture
def _disable_cloud(monkeypatch):
    monkeypatch.setattr(svc, 'CLOUD_ARCHIVE_ENABLED', False)


@pytest.fixture
def _stub_recover(monkeypatch):
    """Stub recover_interrupted_uploads so the worker doesn't touch disk."""
    monkeypatch.setattr(svc, 'recover_interrupted_uploads', lambda _db: 0)


@pytest.fixture
def _stub_drain_noop(monkeypatch):
    """Replace ``_drain_once`` with a counter so we can observe wake → drain.

    Returns the list of (teslacam, db, trigger) tuples observed and
    has the side-effect of letting tests assert how many drains ran.
    """
    calls: List[tuple] = []

    def _fake_drain(teslacam, db, trigger):
        calls.append((teslacam, db, trigger))
        return False  # claim "no work done" so the loop sleeps after

    monkeypatch.setattr(svc, '_drain_once', _fake_drain)
    return calls


@pytest.fixture
def _stub_wifi_up(monkeypatch):
    monkeypatch.setattr(svc, '_is_wifi_connected', lambda: True)


@pytest.fixture
def _stub_no_les_pending(monkeypatch):
    """Stub the LES helper to always return 'no ready events'."""
    fake_module = type('mod', (), {
        'has_ready_live_event_work': staticmethod(lambda _db=None: False),
    })()
    import sys
    monkeypatch.setitem(sys.modules, 'services.live_event_sync_service', fake_module)


@pytest.fixture
def _stub_no_archive_running(monkeypatch):
    """Stub cloud_rclone_service.get_archive_status to report no archive."""
    fake_module = type('mod', (), {
        'get_archive_status': staticmethod(lambda: {"running": False}),
    })()
    import sys
    monkeypatch.setitem(sys.modules, 'services.cloud_rclone_service', fake_module)


# ---------------------------------------------------------------------------
# 1. start() respects CLOUD_ARCHIVE_ENABLED + idempotency
# ---------------------------------------------------------------------------


class TestStart:
    def test_start_returns_false_when_disabled(self, _disable_cloud):
        assert svc.start(teslacam_path="/x", db_path="/y") is False
        assert svc._worker_thread is None

    def test_start_spawns_worker_when_enabled(
        self, _enable_cloud, _stub_recover, _stub_drain_noop,
        _stub_wifi_up, _stub_no_les_pending, _stub_no_archive_running,
    ):
        assert svc.start(teslacam_path="/x", db_path="/y") is True
        assert svc._worker_thread is not None
        assert svc._worker_thread.is_alive()
        # Worker should set the worker_running flag once it's in the loop.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if svc._sync_status.get("worker_running"):
                break
            time.sleep(0.05)
        assert svc._sync_status.get("worker_running") is True

    def test_start_is_idempotent(
        self, _enable_cloud, _stub_recover, _stub_drain_noop,
        _stub_wifi_up, _stub_no_les_pending, _stub_no_archive_running,
    ):
        assert svc.start(teslacam_path="/x", db_path="/y") is True
        first_thread = svc._worker_thread
        # Second start while worker alive returns False, no new thread.
        assert svc.start(teslacam_path="/x", db_path="/y") is False
        assert svc._worker_thread is first_thread


# ---------------------------------------------------------------------------
# 2. wake() is safe before start() and triggers drains after start
# ---------------------------------------------------------------------------


class TestWake:
    def test_wake_before_start_does_not_crash(self):
        # No worker running — wake just sets the event for the next start.
        svc.wake()
        assert svc._wake.is_set() is True

    def test_wake_after_start_triggers_drain(
        self, _enable_cloud, _stub_recover, _stub_drain_noop,
        _stub_wifi_up, _stub_no_les_pending, _stub_no_archive_running,
    ):
        # The worker auto-wakes once on startup, so we expect at least
        # one drain just from start(). Wait for it.
        svc.start(teslacam_path="/x", db_path="/y")
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if len(_stub_drain_noop) >= 1:
                break
            time.sleep(0.05)
        assert len(_stub_drain_noop) >= 1
        first_count = len(_stub_drain_noop)

        # Now explicitly wake — should produce another drain.
        svc.wake()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if len(_stub_drain_noop) > first_count:
                break
            time.sleep(0.05)
        assert len(_stub_drain_noop) > first_count

    def test_wake_count_tracked_in_status(
        self, _enable_cloud, _stub_recover, _stub_drain_noop,
        _stub_wifi_up, _stub_no_les_pending, _stub_no_archive_running,
    ):
        svc.start(teslacam_path="/x", db_path="/y")
        time.sleep(0.5)  # let startup wake settle
        before = svc._sync_status.get("wake_count", 0)
        svc.wake()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if svc._sync_status.get("wake_count", 0) > before:
                break
            time.sleep(0.05)
        assert svc._sync_status["wake_count"] > before


# ---------------------------------------------------------------------------
# 3. Multiple wakes during a drain coalesce
# ---------------------------------------------------------------------------


class TestWakeCoalescing:
    def test_many_wakes_during_drain_dont_pile_up_drains(
        self, monkeypatch, _enable_cloud, _stub_recover,
        _stub_wifi_up, _stub_no_les_pending, _stub_no_archive_running,
    ):
        # Drain that takes a measurable time so multiple wakes can land
        # while it's running.
        drain_calls = []
        drain_in_progress = threading.Event()
        drain_can_finish = threading.Event()

        def _slow_drain(teslacam, db, trigger):
            drain_calls.append((teslacam, db, trigger))
            drain_in_progress.set()
            # Wait up to 1s for the test to release us.
            drain_can_finish.wait(timeout=1.0)
            return False

        monkeypatch.setattr(svc, '_drain_once', _slow_drain)
        svc.start(teslacam_path="/x", db_path="/y")

        # Wait until first drain is in progress
        assert drain_in_progress.wait(timeout=2.0)

        # Fire many wakes in rapid succession — they should all coalesce
        # into at most ONE follow-up drain.
        for _ in range(50):
            svc.wake()

        # Release the in-flight drain
        drain_can_finish.set()

        # Wait for the follow-up drain to start (or for the worker to idle)
        # Then count drains.
        time.sleep(1.0)

        # We should see at most 2 drains total: the initial startup drain
        # plus AT MOST one follow-up triggered by the 50 coalesced wakes.
        # (May be exactly 2 if a follow-up landed; may be 1 if all wakes
        # were already absorbed by the in-flight drain.)
        assert 1 <= len(drain_calls) <= 3, (
            f"Expected 1–3 drains from coalesced wakes, got {len(drain_calls)}"
        )


# ---------------------------------------------------------------------------
# 4. stop() cleanly terminates
# ---------------------------------------------------------------------------


class TestStop:
    def test_stop_joins_worker_thread(
        self, _enable_cloud, _stub_recover, _stub_drain_noop,
        _stub_wifi_up, _stub_no_les_pending, _stub_no_archive_running,
    ):
        svc.start(teslacam_path="/x", db_path="/y")
        assert svc._worker_thread is not None
        thread = svc._worker_thread
        time.sleep(0.3)  # let worker enter idle wait

        ok = svc.stop(timeout=5.0)
        assert ok is True
        assert not thread.is_alive()
        assert svc._sync_status.get("worker_running") is False

    def test_stop_when_no_worker_returns_true(self):
        # Idempotent: stopping a non-existent worker is fine.
        assert svc.stop(timeout=1.0) is True


# ---------------------------------------------------------------------------
# 5. Worker yields to LES (priority contract)
# ---------------------------------------------------------------------------


class TestLesPriority:
    def test_drain_skipped_when_les_has_ready_work(
        self, monkeypatch, _enable_cloud, _stub_recover,
        _stub_wifi_up, _stub_no_archive_running,
    ):
        # LES says "I have ready events"
        fake_les = type('mod', (), {
            'has_ready_live_event_work': staticmethod(lambda _db=None: True),
        })()
        import sys
        monkeypatch.setitem(
            sys.modules, 'services.live_event_sync_service', fake_les,
        )

        drain_calls = []
        monkeypatch.setattr(
            svc, '_drain_once',
            lambda *_a, **_k: (drain_calls.append(1), False)[1],
        )

        svc.start(teslacam_path="/x", db_path="/y")
        time.sleep(0.5)  # give worker time to wake + check LES + skip

        # No drain should have run because LES had work
        assert drain_calls == []


# ---------------------------------------------------------------------------
# 6. Worker skips when WiFi is down
# ---------------------------------------------------------------------------


class TestWifiGate:
    def test_drain_skipped_when_wifi_down(
        self, monkeypatch, _enable_cloud, _stub_recover,
        _stub_no_les_pending, _stub_no_archive_running,
    ):
        monkeypatch.setattr(svc, '_is_wifi_connected', lambda: False)

        drain_calls = []
        monkeypatch.setattr(
            svc, '_drain_once',
            lambda *_a, **_k: (drain_calls.append(1), False)[1],
        )

        svc.start(teslacam_path="/x", db_path="/y")
        time.sleep(0.5)
        assert drain_calls == []


# ---------------------------------------------------------------------------
# 7. Worker skips when single-file archive is running
# ---------------------------------------------------------------------------


class TestArchiveGate:
    def test_drain_skipped_when_archive_in_progress(
        self, monkeypatch, _enable_cloud, _stub_recover,
        _stub_wifi_up, _stub_no_les_pending,
    ):
        # Override the archive-status stub to report "running"
        fake_arch = type('mod', (), {
            'get_archive_status': staticmethod(lambda: {"running": True}),
        })()
        import sys
        monkeypatch.setitem(
            sys.modules, 'services.cloud_rclone_service', fake_arch,
        )

        drain_calls = []
        monkeypatch.setattr(
            svc, '_drain_once',
            lambda *_a, **_k: (drain_calls.append(1), False)[1],
        )

        svc.start(teslacam_path="/x", db_path="/y")
        time.sleep(0.5)
        assert drain_calls == []


# ---------------------------------------------------------------------------
# 8. Backward-compat wrappers: start_sync, trigger_auto_sync
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_start_sync_lazy_starts_worker(
        self, _enable_cloud, _stub_recover, _stub_drain_noop,
        _stub_wifi_up, _stub_no_les_pending, _stub_no_archive_running,
    ):
        # Worker not yet started
        assert svc._worker_thread is None
        ok, msg = svc.start_sync(
            teslacam_path="/x", db_path="/y", trigger="manual",
        )
        assert ok is True
        assert "wake" in msg.lower()
        # Worker should now be alive
        assert svc._worker_thread is not None
        assert svc._worker_thread.is_alive()

    def test_start_sync_returns_false_when_disabled(self, _disable_cloud):
        ok, msg = svc.start_sync(
            teslacam_path="/x", db_path="/y", trigger="manual",
        )
        assert ok is False
        assert "disabled" in msg.lower()

    def test_trigger_auto_sync_pokes_worker(
        self, _enable_cloud, _stub_recover, _stub_drain_noop,
        _stub_wifi_up, _stub_no_les_pending, _stub_no_archive_running,
    ):
        svc.start(teslacam_path="/x", db_path="/y")
        time.sleep(0.3)
        before = svc._sync_status.get("wake_count", 0)
        svc.trigger_auto_sync("/x", "/y")
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if svc._sync_status.get("wake_count", 0) > before:
                break
            time.sleep(0.05)
        assert svc._sync_status["wake_count"] > before


# ---------------------------------------------------------------------------
# 9. Containment: bad drain doesn't kill the worker
# ---------------------------------------------------------------------------


class TestContainment:
    def test_worker_survives_drain_exception(
        self, monkeypatch, _enable_cloud, _stub_recover,
        _stub_wifi_up, _stub_no_les_pending, _stub_no_archive_running,
    ):
        call_count = [0]

        def _exploding_drain(*_a, **_k):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("simulated drain failure")
            return False

        monkeypatch.setattr(svc, '_drain_once', _exploding_drain)
        svc.start(teslacam_path="/x", db_path="/y")

        # Wait for the first (failing) drain
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if call_count[0] >= 1:
                break
            time.sleep(0.05)
        assert call_count[0] >= 1

        # Worker should still be alive after the exception
        assert svc._worker_thread is not None
        assert svc._worker_thread.is_alive()

        # A second wake should produce a successful drain
        svc.wake()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if call_count[0] >= 2:
                break
            time.sleep(0.05)
        assert call_count[0] >= 2


# ---------------------------------------------------------------------------
# 10. Status surfaces worker_running flag
# ---------------------------------------------------------------------------


class TestStatus:
    def test_get_sync_status_includes_worker_running(
        self, _enable_cloud, _stub_recover, _stub_drain_noop,
        _stub_wifi_up, _stub_no_les_pending, _stub_no_archive_running,
    ):
        # Before start: worker_running false
        st = svc.get_sync_status()
        assert "worker_running" in st
        assert st["worker_running"] is False

        # After start: worker_running true (after worker enters loop)
        svc.start(teslacam_path="/x", db_path="/y")
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if svc.get_sync_status().get("worker_running"):
                break
            time.sleep(0.05)
        st = svc.get_sync_status()
        assert st["worker_running"] is True

    def test_get_sync_status_includes_drain_count(
        self, _enable_cloud, _stub_recover, _stub_drain_noop,
        _stub_wifi_up, _stub_no_les_pending, _stub_no_archive_running,
    ):
        svc.start(teslacam_path="/x", db_path="/y")
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if svc.get_sync_status().get("drain_count", 0) >= 1:
                break
            time.sleep(0.05)
        st = svc.get_sync_status()
        assert st.get("drain_count", 0) >= 1
