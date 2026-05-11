"""Tests for the Phase 2b ``video_archive_service`` shims (issue #76).

The legacy ``_archive_timer_loop`` periodic thread + ``_archive_pending``
duplicate-guard pattern is gone. ``video_archive_service`` is now a thin
compatibility layer over ``archive_worker``:

* ``start_archive_timer()`` → ``archive_worker.ensure_worker_started()``
* ``stop_archive_timer()``  → ``archive_worker.stop_worker()``
* ``trigger_archive_now()`` → ``archive_worker.wake()``

The old tests asserted on the deleted internals (``_run_archive``,
``_archive_pending``, ``_archive_timer_loop``, the ionice-on-the-process
bug). Those are no longer reachable code paths and have been deleted.
The Phase 2b worker has its own pause/resume/wake/dead-letter test
coverage in ``test_archive_worker.py``.
"""

from unittest.mock import patch

import pytest

from services import video_archive_service as vas


@pytest.fixture(autouse=True)
def _reset_state():
    """Snapshot/restore module-level state so test order doesn't matter.

    The shim layer no longer carries ``_archive_pending`` or any other
    duplicate-guard mutex (the worker is a singleton thread; that IS
    the guard). All we need to preserve is ``ARCHIVE_ENABLED`` so a
    test that toggles it doesn't bleed into the next.
    """
    saved_enabled = vas.ARCHIVE_ENABLED
    yield
    vas.ARCHIVE_ENABLED = saved_enabled


class TestTriggerArchiveNow:
    """``trigger_archive_now()`` is now a thin wrapper that delegates
    to ``archive_worker.wake()`` after a short config check."""

    def test_disabled_returns_false(self):
        # When ARCHIVE_ENABLED is False the wrapper short-circuits and
        # never touches the worker — returning False so callers know
        # nothing happened.
        vas.ARCHIVE_ENABLED = False
        with patch('services.archive_worker.wake') as mock_wake, \
             patch(
                 'services.archive_worker.ensure_worker_started',
             ) as mock_start:
            assert vas.trigger_archive_now() is False
            mock_wake.assert_not_called()
            mock_start.assert_not_called()

    def test_enabled_calls_worker_wake(self):
        vas.ARCHIVE_ENABLED = True
        with patch('services.archive_worker.wake') as mock_wake, \
             patch(
                 'services.archive_worker.ensure_worker_started',
             ) as mock_start:
            assert vas.trigger_archive_now() is True
            mock_start.assert_called_once()
            mock_wake.assert_called_once()

    def test_wake_failure_is_swallowed(self):
        # The wrapper is called from the NM dispatcher
        # (``helpers/refresh_cloud_token.py``). A worker-side
        # exception MUST NOT propagate up — the caller treats False
        # as "no archive started" and moves on to the cloud sync
        # trigger. Silent fail keeps WiFi-connect resilient.
        vas.ARCHIVE_ENABLED = True
        with patch(
            'services.archive_worker.ensure_worker_started',
        ), patch(
            'services.archive_worker.wake',
            side_effect=RuntimeError("synthetic"),
        ):
            # Should not raise. False return is acceptable.
            result = vas.trigger_archive_now()
            assert result is False


class TestStartStopShims:
    """``start_archive_timer`` and ``stop_archive_timer`` are now pure
    delegations to the worker; the legacy internal thread is gone."""

    def test_start_archive_timer_starts_worker(self):
        with patch(
            'services.archive_worker.ensure_worker_started',
        ) as mock_start:
            vas.start_archive_timer()
            mock_start.assert_called_once()

    def test_start_archive_timer_swallows_worker_failure(self):
        # Same resilience contract as trigger_archive_now: a worker
        # startup failure must not crash gadget_web's main thread.
        with patch(
            'services.archive_worker.ensure_worker_started',
            side_effect=RuntimeError("synthetic"),
        ):
            # Should not raise.
            vas.start_archive_timer()

    def test_stop_archive_timer_stops_worker(self):
        with patch('services.archive_worker.stop_worker') as mock_stop:
            vas.stop_archive_timer()
            mock_stop.assert_called_once()

    def test_stop_archive_timer_swallows_worker_failure(self):
        # Shutdown path needs to be resilient — a worker that's already
        # gone should not block the rest of the shutdown handler.
        with patch(
            'services.archive_worker.stop_worker',
            side_effect=RuntimeError("synthetic"),
        ):
            # Should not raise.
            vas.stop_archive_timer()
