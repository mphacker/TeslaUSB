"""Tests for :mod:`teslausb_web.services.cache_invalidation`.

Covers the Phase 4c.3 debounce + single-flight wrapper. The
acceptance criterion from ``docs/00-PLAN.md`` Phase 4c is:

    Upload 5 chimes in rapid succession → exactly ONE
    invalidation fires (verify in journalctl)

so the centerpiece test is :func:`test_five_rapid_calls_coalesce_to_one`.

All tests mock :func:`subprocess.run` — we never spawn the real
helper script here. The integration test in Phase 4c.5 covers
the real shell-out against a tmpdir-mocked configfs.
"""

from __future__ import annotations

import subprocess
import threading
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from teslausb_web.services.cache_invalidation import (
    DEFAULT_COMMAND,
    CacheInvalidator,
    InvalidationResult,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


# Debounce short enough to keep tests fast but long enough to
# reliably batch sequential schedule() calls on slow CI.
TEST_DEBOUNCE = 0.05  # 50 ms
TEST_TIMEOUT = 2.0


def _ok_result(stdout: str = "ok") -> MagicMock:
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.returncode = 0
    m.stdout = stdout
    m.stderr = ""
    return m


@pytest.fixture
def invalidator() -> Iterator[CacheInvalidator]:
    inv = CacheInvalidator(
        command=("/usr/bin/true",),
        debounce_seconds=TEST_DEBOUNCE,
        timeout_seconds=TEST_TIMEOUT,
    )
    yield inv
    inv.shutdown()


def _wait_for(predicate: Callable[[], bool], deadline_s: float = 1.0) -> bool:
    """Spin until predicate() is true or deadline elapses."""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_invalidation_result_ok() -> None:
    assert InvalidationResult(0, "", "").ok is True
    assert InvalidationResult(1, "", "").ok is False
    assert InvalidationResult(-1, "", "timeout").ok is False


def test_default_command_is_sudo_no_args() -> None:
    # Sudoers fragment in Phase 4c.2 grants NOPASSWD only for this
    # exact argv. If anyone "improves" the default, the deploy
    # breaks silently. Lock it down.
    assert DEFAULT_COMMAND == (
        "sudo",
        "-n",
        "/usr/local/bin/tesla_cache_invalidate.sh",
    )


def test_invalidate_now_runs_immediately(invalidator: CacheInvalidator) -> None:
    with patch("subprocess.run", return_value=_ok_result()) as mock_run:
        result = invalidator.invalidate_now()
    assert result.ok
    assert mock_run.call_count == 1
    mock_run.assert_called_with(
        ["/usr/bin/true"],
        capture_output=True,
        text=True,
        timeout=TEST_TIMEOUT,
        check=False,
    )


def test_five_rapid_calls_coalesce_to_one(invalidator: CacheInvalidator) -> None:
    """Acceptance: 5 rapid schedule() calls → exactly 1 subprocess.run."""
    with patch("subprocess.run", return_value=_ok_result()) as mock_run:
        for _ in range(5):
            invalidator.schedule()
        # Wait for debounced timer to fire + subprocess to complete.
        assert _wait_for(lambda: mock_run.call_count >= 1, deadline_s=2.0)
        # Give any spurious second timer a chance to fire and prove
        # it doesn't.
        time.sleep(TEST_DEBOUNCE * 4)
    assert mock_run.call_count == 1


def test_schedule_after_completion_runs_again(invalidator: CacheInvalidator) -> None:
    """A new schedule() after a previous cycle finishes runs a new cycle."""
    with patch("subprocess.run", return_value=_ok_result()) as mock_run:
        invalidator.schedule()
        assert _wait_for(lambda: mock_run.call_count >= 1, deadline_s=2.0)
        time.sleep(TEST_DEBOUNCE * 2)  # let things settle

        invalidator.schedule()
        assert _wait_for(lambda: mock_run.call_count >= 2, deadline_s=2.0)
    assert mock_run.call_count == 2


def test_schedule_during_in_flight_drains_to_one_rerun(
    invalidator: CacheInvalidator,
) -> None:
    """schedule() while a cycle is running -> exactly one re-run after debounce."""
    block = threading.Event()
    release = threading.Event()
    call_count = 0

    def fake_run(*_args: object, **_kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            block.set()
            # Block first invocation until test releases it. We
            # deliberately schedule() AFTER waiting past the
            # debounce so the second timer fires while the first
            # cycle is still in subprocess.run, which is exactly
            # the in-flight path that sets `_pending`.
            release.wait(timeout=5.0)
        return _ok_result()

    with patch("subprocess.run", side_effect=fake_run):
        invalidator.schedule()
        assert block.wait(timeout=2.0), "first cycle never started"

        # 4 more requests while first cycle is blocked in subprocess.run.
        # Wait long enough between (or after) for at least one of the
        # subsequent timers to fire WHILE in-flight — that's what sets
        # `_pending`. Without the explicit wait, all 5 schedule() calls
        # would re-arm the timer faster than the debounce, so the timer
        # would fire only AFTER release.set() returns and _in_flight is
        # already False.
        for _ in range(4):
            invalidator.schedule()
        time.sleep(TEST_DEBOUNCE * 3)  # let the 5th timer fire in-flight

        # Now unblock — the in-flight cycle completes, sees pending,
        # schedules exactly one more cycle.
        release.set()
        assert _wait_for(lambda: call_count >= 2, deadline_s=5.0)
        # Prove no third cycle sneaks in.
        time.sleep(TEST_DEBOUNCE * 6)

    assert call_count == 2


def test_invalidate_now_cancels_pending_timer(invalidator: CacheInvalidator) -> None:
    """invalidate_now() cancels the debounced timer (covers the cancel branch)."""
    with patch("subprocess.run", return_value=_ok_result()) as mock_run:
        invalidator.schedule()
        # immediately bypass the debounce
        result = invalidator.invalidate_now()
        assert result.ok
        # Wait past where the cancelled debounce would have fired —
        # no second invocation must follow.
        time.sleep(TEST_DEBOUNCE * 4)
    assert mock_run.call_count == 1


def test_shutdown_cancels_pending_timer(invalidator: CacheInvalidator) -> None:
    with patch("subprocess.run", return_value=_ok_result()) as mock_run:
        invalidator.schedule()
        invalidator.shutdown()
        # Sleep well past the debounce window; the cancelled timer
        # must NOT fire after shutdown.
        time.sleep(TEST_DEBOUNCE * 4)
    assert mock_run.call_count == 0


def test_schedule_after_shutdown_is_noop(invalidator: CacheInvalidator) -> None:
    invalidator.shutdown()
    with patch("subprocess.run", return_value=_ok_result()) as mock_run:
        invalidator.schedule()
        time.sleep(TEST_DEBOUNCE * 4)
    assert mock_run.call_count == 0


def test_timeout_returns_failure_result_no_raise(invalidator: CacheInvalidator) -> None:
    err = subprocess.TimeoutExpired(cmd=["true"], timeout=TEST_TIMEOUT, output=b"partial")
    with patch("subprocess.run", side_effect=err):
        result = invalidator.invalidate_now()
    assert not result.ok
    assert result.returncode == -1
    assert "timeout" in result.stderr


def test_missing_binary_returns_failure_result(invalidator: CacheInvalidator) -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError("nope")):
        result = invalidator.invalidate_now()
    assert not result.ok
    assert result.returncode == -1
    assert "nope" in result.stderr


def test_nonzero_exit_is_recorded_not_raised(invalidator: CacheInvalidator) -> None:
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.returncode = 4
    m.stdout = ""
    m.stderr = "LUN already empty"
    with patch("subprocess.run", return_value=m):
        result = invalidator.invalidate_now()
    assert not result.ok
    assert result.returncode == 4
    assert "LUN already empty" in result.stderr


def test_context_manager_calls_shutdown() -> None:
    inv = CacheInvalidator(
        command=("/usr/bin/true",),
        debounce_seconds=TEST_DEBOUNCE,
    )
    with patch("subprocess.run", return_value=_ok_result()) as mock_run, inv:
        inv.schedule()
    # After __exit__, shutdown was called; the timer was cancelled
    # before it could fire.
    time.sleep(TEST_DEBOUNCE * 4)
    assert mock_run.call_count == 0
