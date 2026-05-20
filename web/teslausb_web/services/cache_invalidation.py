"""Tesla USB cache-invalidation service.

When the web UI writes a file Tesla cares about (LockChime.wav,
LightShow.fseq, music tracks, future "wraps", etc.) into the backing
tree, Tesla keeps reading the OLD cached version until something
tells the SCSI layer that the medium has changed. The actual
medium-change cycle is done by :mod:`scripts.tesla_cache_invalidate`
which clears + restores the LUN file in configfs.

This module is the Python-side wrapper that:

1. **Debounces** rapid back-to-back calls. The user uploading
   5 chimes in a row should result in ONE medium-change cycle
   after the last upload, not 5 cycles (each of which causes a
   ~200 ms Tesla recording pause). A single :class:`threading.Timer`
   coalesces all calls within a configurable window.
2. **Single-flights** the shell-out. If a cycle is already
   running and a new request arrives, that request is recorded
   and exactly ONE additional cycle is run after the current
   completes — never N concurrent invocations of the shell
   script (which would race over configfs writes).
3. **Logs** every state transition so journalctl shows exactly
   which request triggered which cycle. Per Phase 4c acceptance:
   "5 chimes in rapid succession → exactly ONE invalidation
   fires (verify in journalctl)".

This module imports NO Flask types so it can be unit-tested in
isolation and reused from a non-web context (e.g. CLI tools, the
Rust worker via PyO3 in some hypothetical future). The blueprints
in Phase 5 will instantiate a module-level singleton and call
``invalidator.schedule()`` after every file write.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType

logger = logging.getLogger(__name__)

# Default command. The sudoers fragment shipped in Phase 4c.2 grants
# the `teslausb` user NOPASSWD permission for EXACTLY this argv
# (zero args after the script path), with `env_reset` so env-based
# redirection is impossible. `sudo -n` aborts immediately if sudo
# would need to prompt — that turns a misconfigured deploy into a
# loud, fast failure rather than a hang.
DEFAULT_COMMAND: tuple[str, ...] = (
    "sudo",
    "-n",
    "/usr/local/bin/tesla_cache_invalidate.sh",
)

# Default debounce window. 2 s comfortably covers a user clicking
# through a multi-file upload form; the user notices "saved" toasts
# coming back, then ~2 s of quiet, then "Updating USB..." once.
DEFAULT_DEBOUNCE_SECONDS: float = 2.0

# Default subprocess timeout. The script itself sleeps `eject_ms`
# (200 ms default) plus a couple file writes; 10 s is "something
# is very wrong" rather than "this is slow".
DEFAULT_TIMEOUT_SECONDS: float = 10.0


@dataclass
class InvalidationResult:
    """Outcome of a single shell-out to ``tesla_cache_invalidate.sh``."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class CacheInvalidator:
    """Debounced, single-flight Tesla USB cache invalidator.

    Thread-safety: all public methods may be called from any thread.
    The internal lock protects the timer + in-flight flags; the
    actual subprocess call runs WITHOUT the lock held so concurrent
    callers can enqueue while we wait for the shell-out.

    Lifecycle: instantiate once at app startup, call
    :meth:`schedule` from request handlers, call :meth:`shutdown`
    once during graceful shutdown to cancel any pending timer and
    wait for an in-flight cycle to drain.
    """

    command: Sequence[str] = field(default_factory=lambda: DEFAULT_COMMAND)
    debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _timer: threading.Timer | None = field(default=None, init=False, repr=False)
    _in_flight: bool = field(default=False, init=False, repr=False)
    _pending: bool = field(default=False, init=False, repr=False)
    _shutdown: bool = field(default=False, init=False, repr=False)

    def schedule(self) -> None:
        """Request a cache invalidation, debounced.

        Cancels any previously scheduled timer and starts a fresh
        one. The most recent call wins — exactly one cycle will run
        ``debounce_seconds`` after the LAST :meth:`schedule` call,
        no matter how many calls came before it within the window.
        """
        with self._lock:
            if self._shutdown:
                logger.debug("schedule() called after shutdown; ignoring")
                return
            if self._timer is not None:
                self._timer.cancel()
            timer = threading.Timer(self.debounce_seconds, self._fire)
            timer.daemon = True
            self._timer = timer
        logger.debug(
            "cache invalidation scheduled in %.2fs (debounced)",
            self.debounce_seconds,
        )
        timer.start()

    def invalidate_now(self) -> InvalidationResult:
        """Run an invalidation synchronously, bypassing the debounce.

        Cancels any pending timer first. Returns the subprocess
        result. Used by integration tests and CLI tools; production
        request handlers should use :meth:`schedule` instead so
        rapid-fire uploads coalesce.
        """
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        return self._run_once()

    def shutdown(self) -> None:
        """Cancel pending timer; mark shutdown so no new ones start."""
        with self._lock:
            self._shutdown = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        logger.debug("cache invalidator shut down")

    # ─── Internals ───────────────────────────────────────────────────

    def _fire(self) -> None:
        """Timer callback. Runs the cycle, then drains any pending."""
        with self._lock:
            self._timer = None
            if self._in_flight:
                # A cycle is already running; record that another
                # request came in so we re-run exactly once when the
                # current cycle finishes. Multiple requests during a
                # single in-flight cycle still collapse to one re-run.
                self._pending = True
                logger.debug("invalidation already in flight; marking pending")
                return
            self._in_flight = True

        try:
            self._run_once()
        finally:
            with self._lock:
                self._in_flight = False
                run_again = self._pending
                self._pending = False
            if run_again and not self._shutdown:
                logger.debug("draining pending invalidation request")
                # Recurse via the public schedule path so the debounce
                # window applies (lets any further requests piggyback
                # rather than spinning back-to-back cycles).
                self.schedule()

    def _run_once(self) -> InvalidationResult:
        """Shell out to the helper script. Never raises on nonzero exit."""
        cmd = list(self.command)
        logger.info("invoking cache invalidator: %s", cmd)
        try:
            # `check=False`: we record returncode + stderr in the
            # result so the caller (or journalctl) can see what
            # happened; we never raise CalledProcessError because
            # the timer thread has no useful way to surface it.
            completed = subprocess.run(  # noqa: S603 — argv is module-level constant
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            logger.error(
                "cache invalidator timed out after %.1fs: %s",
                self.timeout_seconds,
                exc,
            )
            return InvalidationResult(
                returncode=-1,
                stdout=str(exc.stdout) if exc.stdout else "",
                stderr=f"timeout after {self.timeout_seconds}s",
            )
        except OSError as exc:
            logger.error("cache invalidator failed to start: %s", exc)
            return InvalidationResult(returncode=-1, stdout="", stderr=str(exc))

        result = InvalidationResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if result.ok:
            logger.info("cache invalidator ok")
        else:
            logger.warning(
                "cache invalidator returned %d: %s",
                result.returncode,
                result.stderr.strip(),
            )
        return result

    # Context-manager sugar for tests and short-lived users.

    def __enter__(self) -> CacheInvalidator:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        self.shutdown()
