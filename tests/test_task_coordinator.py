"""Tests for ``services.task_coordinator`` — the heavy-task lock that
keeps the geo-indexer, video archiver, and cloud sync from running
simultaneously on the Pi Zero 2 W.

These tests guard the fairness model added after the May 2026
phantom-trips incident, where the indexer's ~1 Hz acquire/release
cycle starved the archive's 5-minute timer for hours, causing
TeslaCam clip loss when Tesla rotated RecentClips.
"""

import threading
import time

import pytest

from services import task_coordinator as tc


@pytest.fixture(autouse=True)
def _reset_coordinator():
    """Each test starts with a clean coordinator state.

    The module holds global lock state. Tests must not leak it.
    """
    # Pre-test cleanup: in case a prior test crashed mid-acquire.
    with tc._lock:
        tc._current_task = None
        tc._task_started = 0.0
        tc._waiter_count = 0
    yield
    # Post-test cleanup.
    with tc._lock:
        tc._current_task = None
        tc._task_started = 0.0
        tc._waiter_count = 0


class TestBasicAcquireRelease:
    def test_acquire_when_free_returns_true(self):
        assert tc.acquire_task('A') is True
        assert tc.is_busy() is True
        tc.release_task('A')
        assert tc.is_busy() is False

    def test_acquire_when_busy_returns_false_immediately(self):
        assert tc.acquire_task('A') is True
        # Default wait_seconds=0 → never blocks.
        start = time.monotonic()
        assert tc.acquire_task('B') is False
        elapsed = time.monotonic() - start
        assert elapsed < 0.05, f"Should not block; took {elapsed:.3f}s"
        tc.release_task('A')

    def test_release_clears_lock(self):
        tc.acquire_task('A')
        tc.release_task('A')
        assert tc.acquire_task('B') is True
        tc.release_task('B')

    def test_release_by_wrong_owner_is_noop(self):
        tc.acquire_task('A')
        # Releasing by the wrong name must NOT clear the lock.
        tc.release_task('not-A')
        assert tc.is_busy() is True
        tc.release_task('A')


class TestWaitSeconds:
    def test_wait_returns_true_when_lock_freed_in_time(self):
        tc.acquire_task('holder')

        def release_after_delay():
            time.sleep(0.2)
            tc.release_task('holder')

        threading.Thread(target=release_after_delay, daemon=True).start()
        start = time.monotonic()
        ok = tc.acquire_task('waiter', wait_seconds=2.0)
        elapsed = time.monotonic() - start
        assert ok is True
        assert 0.15 < elapsed < 1.0, (
            f"Should wait ~0.2s, took {elapsed:.3f}s"
        )
        tc.release_task('waiter')

    def test_wait_returns_false_on_timeout(self):
        tc.acquire_task('holder')
        start = time.monotonic()
        ok = tc.acquire_task('waiter', wait_seconds=0.3)
        elapsed = time.monotonic() - start
        assert ok is False
        # Must wait at least the full timeout, not give up early.
        assert elapsed >= 0.3, (
            f"Must wait full timeout; only waited {elapsed:.3f}s"
        )
        tc.release_task('holder')

    def test_waiter_count_increments_while_waiting(self):
        tc.acquire_task('holder')
        # Sanity: nobody waiting yet.
        assert tc.waiter_count() == 0
        results = {}

        def waiter():
            results['ok'] = tc.acquire_task('w', wait_seconds=0.5)

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        # Give the waiter a moment to register.
        time.sleep(0.15)
        assert tc.waiter_count() == 1
        # Letting it time out should decrement the count again.
        t.join(timeout=2.0)
        assert results.get('ok') is False
        assert tc.waiter_count() == 0
        tc.release_task('holder')


class TestFairnessYieldToWaiters:
    def test_yield_to_waiters_refuses_when_someone_is_waiting(self):
        """Cyclic tasks (yield_to_waiters=True) must NOT take the lock
        when another task is currently inside acquire_task waiting for
        it. This is the fairness short-circuit that prevents indexer
        starvation of archive/sync."""
        results = {}

        def waiter():
            results['ok'] = tc.acquire_task('priority', wait_seconds=2.0)

        # Hold the lock so the waiter actually has to register.
        tc.acquire_task('first-holder')
        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        # Let the waiter register itself.
        time.sleep(0.15)
        assert tc.waiter_count() == 1

        # Release the holder. Now the lock is technically free, but
        # the waiter hasn't grabbed it yet (it polls every 0.1s).
        tc.release_task('first-holder')

        # An impolite cyclic task that does NOT yield would race in here
        # and steal the slot. With yield_to_waiters=True it must refuse.
        assert tc.acquire_task('cycler', yield_to_waiters=True) is False

        # The priority waiter must still be able to acquire.
        t.join(timeout=3.0)
        assert results.get('ok') is True
        tc.release_task('priority')

    def test_yield_to_waiters_acquires_normally_when_no_waiters(self):
        # No-waiter steady state — yield_to_waiters must not penalize.
        assert tc.waiter_count() == 0
        assert tc.acquire_task('cycler', yield_to_waiters=True) is True
        tc.release_task('cycler')


class TestArchiveWinsAgainstCyclingIndexer:
    def test_archive_acquires_within_wait_window(self):
        """Production scenario: indexer holds + releases the lock at
        ~1 Hz forever (any work to do). Archive's 5-minute timer fires
        and tries to acquire with wait_seconds=60. Archive MUST win
        before the timeout because the indexer yields to waiters."""
        stop = threading.Event()

        def cycling_indexer():
            while not stop.is_set():
                if tc.acquire_task('indexer', yield_to_waiters=True):
                    # Simulate ~1s of indexing work, then release.
                    time.sleep(0.05)
                    tc.release_task('indexer')
                # Inter-file gap.
                time.sleep(0.02)

        t = threading.Thread(target=cycling_indexer, daemon=True)
        t.start()

        # Let the indexer get into its cycle.
        time.sleep(0.2)

        start = time.monotonic()
        ok = tc.acquire_task('archive', wait_seconds=2.0)
        elapsed = time.monotonic() - start
        assert ok is True, (
            f"Archive failed to acquire within 2s "
            f"(elapsed={elapsed:.2f}s); fairness regression"
        )
        # Should win quickly — at most one indexer cycle (~0.1s) plus
        # a poll interval. Allow generous margin for CI jitter.
        assert elapsed < 1.0, (
            f"Archive took too long: {elapsed:.2f}s — fairness "
            f"short-circuit may not be engaged"
        )
        tc.release_task('archive')
        stop.set()
        t.join(timeout=2.0)


class TestStaleLockClearing:
    def test_stale_lock_is_cleared_on_next_acquire(self, monkeypatch):
        """If a holder dies without releasing, the next acquirer must
        not be blocked forever. Stale = older than _MAX_TASK_AGE_SECONDS.
        """
        # Install a tiny stale threshold so the test runs fast.
        monkeypatch.setattr(tc, '_MAX_TASK_AGE_SECONDS', 0.1)
        tc.acquire_task('zombie')
        time.sleep(0.15)
        # Next acquirer should clear and take the lock.
        assert tc.acquire_task('rescuer') is True
        tc.release_task('rescuer')


class TestHeavyTaskContextManager:
    def test_context_manager_releases_on_exit(self):
        with tc.heavy_task('A') as acquired:
            assert acquired is True
            assert tc.is_busy() is True
        assert tc.is_busy() is False

    def test_context_manager_releases_on_exception(self):
        with pytest.raises(RuntimeError):
            with tc.heavy_task('A') as acquired:
                assert acquired is True
                raise RuntimeError("boom")
        assert tc.is_busy() is False

    def test_context_manager_yields_false_when_busy(self):
        tc.acquire_task('first')
        with tc.heavy_task('second') as acquired:
            assert acquired is False
        # First holder still has the lock.
        assert tc.is_busy() is True
        tc.release_task('first')


class TestCurrentTaskInfo:
    def test_info_when_idle(self):
        info = tc.current_task_info()
        assert info['busy'] is False
        assert info['task'] is None
        assert info['waiters'] == 0

    def test_info_when_busy(self):
        tc.acquire_task('worker')
        info = tc.current_task_info()
        assert info['busy'] is True
        assert info['task'] == 'worker'
        assert info['elapsed'] >= 0
        assert info['waiters'] == 0
        tc.release_task('worker')


class TestMultipleWaiters:
    """Verify ``_waiter_count`` accounting holds up with several waiters
    racing for the same lock — important because the indexer's fairness
    short-circuit depends on an accurate count."""

    def test_multiple_waiters_count_correctly(self):
        tc.acquire_task('holder')
        results = {}
        threads = []

        def waiter(name):
            results[name] = tc.acquire_task(name, wait_seconds=0.5)

        for i in range(3):
            t = threading.Thread(target=waiter, args=(f'w{i}',), daemon=True)
            threads.append(t)
            t.start()

        # Give all three waiters time to register.
        time.sleep(0.2)
        assert tc.waiter_count() == 3

        # All three should time out (lock never released).
        for t in threads:
            t.join(timeout=2.0)

        # All timed out → all decremented their waiter slot.
        assert all(v is False for v in results.values())
        assert tc.waiter_count() == 0
        tc.release_task('holder')

    def test_mixed_success_and_timeout_decrements_correctly(self):
        tc.acquire_task('holder')
        results = {}

        def waiter_long():
            results['long'] = tc.acquire_task('long', wait_seconds=2.0)

        def waiter_short():
            results['short'] = tc.acquire_task('short', wait_seconds=0.3)

        t_long = threading.Thread(target=waiter_long, daemon=True)
        t_short = threading.Thread(target=waiter_short, daemon=True)
        t_long.start()
        t_short.start()
        time.sleep(0.15)
        assert tc.waiter_count() == 2

        # Short waiter times out first.
        t_short.join(timeout=1.0)
        assert results.get('short') is False
        assert tc.waiter_count() == 1

        # Release lock so the long waiter can grab it.
        tc.release_task('holder')
        t_long.join(timeout=3.0)
        assert results.get('long') is True
        assert tc.waiter_count() == 0
        tc.release_task('long')

    def test_yield_to_waiters_combined_with_wait_seconds_does_block(self):
        """Documented behaviour: a caller that itself wants to wait
        cannot also yield-to-waiters (it would yield to itself on
        every poll). Verify the documented "no effect" semantics."""
        tc.acquire_task('holder')
        results = {}

        def waiter():
            # Even with yield_to_waiters=True, this caller must block
            # for the full wait window, not return immediately.
            start = time.monotonic()
            results['ok'] = tc.acquire_task(
                'priority', wait_seconds=0.4, yield_to_waiters=True,
            )
            results['elapsed'] = time.monotonic() - start

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        t.join(timeout=2.0)
        assert results.get('ok') is False
        # Must have waited the full timeout, not bailed early because
        # of the (irrelevant) waiter-count check.
        assert results.get('elapsed', 0) >= 0.35, (
            f"Should have waited ~0.4s, only waited "
            f"{results.get('elapsed'):.3f}s — yield_to_waiters wrongly "
            "applied to a blocking caller"
        )
        tc.release_task('holder')
