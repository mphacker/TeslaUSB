"""Shared pytest fixtures."""

from __future__ import annotations

import threading

import pytest


@pytest.fixture(autouse=True)
def _cancel_lingering_debounce_timers() -> object:
    """Cancel debounce ``threading.Timer`` threads left armed by a test.

    Blueprint upload endpoints (lock chimes, boombox, music, wraps,
    light shows, license plates, cloud archive) call
    ``CacheInvalidator.schedule()``, which arms a daemon
    ``threading.Timer`` for the debounce window. If that timer were to
    fire during a *later* test that patches ``subprocess.Popen`` (e.g.
    the cloud rclone transfer tests), the timer thread would shell out
    through the patched fake and raise, surfacing as a cross-test
    ``PytestUnhandledThreadExceptionWarning``. Cancelling any still-armed
    timer at teardown keeps every test hermetic.
    """
    yield
    for thread in threading.enumerate():
        if isinstance(thread, threading.Timer):
            thread.cancel()
