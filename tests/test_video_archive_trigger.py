"""Tests for the Phase 2b/3a ``video_archive_service`` shim layer.

Phase 2b (issue #76) replaced the legacy ``_archive_timer_loop`` /
``_run_archive`` / ``_archive_pending`` duplicate-guard internals with
the queue-driven ``archive_worker``. Phase 3a (issue #98 / closes #91)
deletes the legacy retention cascade — ``smart_cleanup_archive``,
``_proactive_retention``, ``_enforce_retention``, ``_purge_corrupt_archives``,
``_prune_non_driving_archives``, ``_update_geodata_paths``, and the
helper ladder beneath them — and rewires
``trigger_archive_cleanup`` to a one-line wrapper around
``archive_watchdog.force_prune_now``.

These tests pin the surviving public API:

* ``trigger_archive_now`` — wakes the worker, swallows worker exceptions
* ``start_archive_timer`` / ``stop_archive_timer`` — delegate to the
  worker's lifecycle, swallow exceptions
* ``get_archive_status`` — bridges to ``archive_worker.get_status`` and
  reports a backwards-compatible ``running`` flag for the dispatcher
* ``trigger_archive_cleanup`` — Phase 3a wrapper for
  ``archive_watchdog.force_prune_now``; passes through the watchdog's
  ``status='already_running'`` short-circuit
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
        vas.ARCHIVE_ENABLED = True
        with patch(
            'services.archive_worker.ensure_worker_started',
        ) as mock_start:
            vas.start_archive_timer()
            mock_start.assert_called_once()

    def test_start_archive_timer_disabled_does_not_start_worker(self):
        # When ARCHIVE_ENABLED is False, start should be a no-op.
        # Importantly, it must NOT call ensure_worker_started — the
        # worker shouldn't even be initialised when archiving is off.
        vas.ARCHIVE_ENABLED = False
        with patch(
            'services.archive_worker.ensure_worker_started',
        ) as mock_start:
            vas.start_archive_timer()
            mock_start.assert_not_called()

    def test_start_archive_timer_swallows_worker_failure(self):
        # Same resilience contract as trigger_archive_now: a worker
        # startup failure must not crash gadget_web's main thread.
        vas.ARCHIVE_ENABLED = True
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


class TestGetArchiveStatus:
    """``get_archive_status()`` bridges to ``archive_worker.get_status()``
    and reports a backwards-compatible ``running`` flag for the
    NM dispatcher (``helpers/refresh_cloud_token.py``).

    The dispatcher polls ``/api/recent_archive/status`` after
    ``/api/recent_archive/trigger`` and waits for ``running == False``
    before kicking cloud sync. In the queue-driven architecture the
    worker thread is always alive once started, so ``running`` must
    track "is there work in flight right now" — the union of an
    active file being copied AND any pending queue depth.
    """

    def test_running_true_when_active_file(self):
        # Worker is mid-file — dispatcher must wait.
        with patch(
            'services.archive_worker.get_status',
            return_value={
                'worker_running': True,
                'active_file': '/foo/bar.mp4',
                'queue_depth': 0,
                'last_outcome': 'copied',
            },
        ):
            status = vas.get_archive_status()
            assert status['running'] is True
            assert status['current_file'] == '/foo/bar.mp4'

    def test_running_true_when_queue_has_work(self):
        # Worker is between files but more work is queued — still busy.
        with patch(
            'services.archive_worker.get_status',
            return_value={
                'worker_running': True,
                'active_file': None,
                'queue_depth': 12,
                'last_outcome': 'copied',
            },
        ):
            status = vas.get_archive_status()
            assert status['running'] is True
            assert status['queue_depth'] == 12

    def test_running_false_when_idle(self):
        # Worker is alive but the queue has drained and no active
        # file. Dispatcher's wait loop exits and cloud sync proceeds.
        with patch(
            'services.archive_worker.get_status',
            return_value={
                'worker_running': True,
                'active_file': None,
                'queue_depth': 0,
                'last_outcome': 'copied',
            },
        ):
            status = vas.get_archive_status()
            assert status['running'] is False

    def test_running_false_when_worker_not_started(self):
        # Worker thread never started (e.g. ARCHIVE_ENABLED was False
        # at boot). Don't make the dispatcher wait forever.
        with patch(
            'services.archive_worker.get_status',
            return_value={
                'worker_running': False,
                'active_file': None,
                'queue_depth': 5,
                'last_outcome': None,
            },
        ):
            status = vas.get_archive_status()
            assert status['running'] is False

    def test_get_status_failure_returns_safe_default(self):
        # If the worker module raises (e.g. import error in tests),
        # return a safe default rather than 500-ing the API. The
        # dispatcher will see running=False and move on.
        with patch(
            'services.archive_worker.get_status',
            side_effect=RuntimeError("synthetic"),
        ):
            status = vas.get_archive_status()
            assert status['running'] is False
            assert 'error' in status

    def test_dispatcher_compatible_keys_always_present(self):
        # Pin the legacy field surface so the dispatcher's
        # ``status.get("running")`` and any UI poll never KeyError.
        with patch(
            'services.archive_worker.get_status',
            return_value={
                'worker_running': True,
                'active_file': None,
                'queue_depth': 0,
                'last_outcome': 'copied',
            },
        ):
            status = vas.get_archive_status()
            for key in (
                'running', 'current_file', 'queue_depth',
                'worker_running', 'last_outcome', 'error',
            ):
                assert key in status, f"missing legacy key {key!r}"


class TestTriggerArchiveCleanup:
    """Phase 3a (#98 / closes #91): ``trigger_archive_cleanup`` is now
    a one-line wrapper for ``archive_watchdog.force_prune_now``.

    The legacy implementation called ``smart_cleanup_archive``, which
    in turn duplicated retention logic that already lives in
    ``archive_watchdog._run_retention_prune``. Three overlapping
    retention systems racing each other was the main bug source the
    Phase 3a refactor closes. These tests pin the new contract:

    * The shim MUST call into ``archive_watchdog.force_prune_now`` —
      not into the deleted ``smart_cleanup_archive`` (which would
      AttributeError now).
    * The watchdog's return shape — including the
      ``status='already_running'`` short-circuit — passes through
      unchanged so the ``POST /api/archive_cleanup`` endpoint
      response stays informative.
    * Watchdog exceptions are swallowed and converted to a
      structured error dict so the request thread never 500s.
    """

    def test_delegates_to_force_prune_now(self):
        with patch(
            'services.archive_watchdog.force_prune_now',
        ) as mock_prune:
            mock_prune.return_value = {
                'deleted_count': 3,
                'freed_bytes': 9_000_000,
                'scanned': 42,
                'kept_unsynced_count': 1,
                'cutoff_iso': '2026-04-12T00:00:00+00:00',
                'duration_seconds': 0.234,
            }
            result = vas.trigger_archive_cleanup()
            mock_prune.assert_called_once_with()
            assert result['deleted_count'] == 3
            assert result['freed_bytes'] == 9_000_000
            assert result['scanned'] == 42
            assert result['kept_unsynced_count'] == 1
            assert 'cutoff_iso' in result

    def test_already_running_status_passthrough(self):
        # When the watchdog short-circuits via the _retention_running
        # guard (issue #91 fix), the wrapper must propagate the
        # status verbatim — the cloud_archive blueprint surfaces it
        # to the front end so the user sees "Cleanup already in
        # progress" instead of a confusing zero-result response.
        with patch(
            'services.archive_watchdog.force_prune_now',
            return_value={
                'deleted_count': 0,
                'freed_bytes': 0,
                'scanned': 0,
                'status': 'already_running',
                'duration_seconds': 0.001,
            },
        ):
            result = vas.trigger_archive_cleanup()
            assert result.get('status') == 'already_running'
            assert result['deleted_count'] == 0

    def test_watchdog_exception_returns_error_dict(self):
        # The watchdog raising must NOT 500 the endpoint — return a
        # safe-default summary with an ``error`` key so the UI can
        # render a clear message.
        with patch(
            'services.archive_watchdog.force_prune_now',
            side_effect=RuntimeError("synthetic"),
        ):
            result = vas.trigger_archive_cleanup()
            assert result['deleted_count'] == 0
            assert result['freed_bytes'] == 0
            assert result['scanned'] == 0
            assert 'error' in result
            assert 'synthetic' in result['error']

    def test_watchdog_not_started_returns_error_dict(self):
        # When the watchdog reports it isn't started, force_prune_now
        # returns ``{'error': 'watchdog not started', ...}`` rather
        # than raising. Pass the structure through unchanged.
        with patch(
            'services.archive_watchdog.force_prune_now',
            return_value={
                'deleted_count': 0,
                'freed_bytes': 0,
                'scanned': 0,
                'error': 'watchdog not started',
            },
        ):
            result = vas.trigger_archive_cleanup()
            assert result['error'] == 'watchdog not started'

    def test_no_legacy_smart_cleanup_attribute(self):
        # Defensive regression: the old ``smart_cleanup_archive``,
        # ``_proactive_retention``, ``_enforce_retention``,
        # ``_prune_non_driving_archives``, and
        # ``_purge_corrupt_archives`` symbols must NOT exist on the
        # module. If a future refactor accidentally re-imports them
        # from a backup, this test catches it before it ships.
        for symbol in (
            'smart_cleanup_archive',
            '_proactive_retention',
            '_enforce_retention',
            '_purge_corrupt_archives',
            '_prune_non_driving_archives',
            '_update_geodata_paths',
            '_delete_files_older_than',
            '_trim_archive_to_size',
            '_trim_archive_for_free_space',
            '_get_archived_files_sorted',
            '_buffered_copy',
            '_is_complete_mp4',
            '_check_memory',
            '_check_disk_space',
            '_get_archive_size',
            '_update_archive_size',
            '_get_teslacam_ro_path',
            '_get_driving_time_ranges',
            '_timestamp_from_filename',
            '_is_during_driving',
        ):
            assert not hasattr(vas, symbol), (
                f"video_archive_service.{symbol} was deleted in Phase 3a "
                "(#98); reintroducing it would re-enable the racing "
                "retention systems the refactor removed. Move any new "
                "logic to archive_worker or archive_watchdog instead."
            )
