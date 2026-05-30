"""USB-gadget rebind service.

Lock-chime *activation* (writing a new ``LockChime.wav`` to the MEDIA
LUN root) needs more than the soft SCSI medium-change that
:mod:`teslausb_web.services.cache_invalidation` performs. Tesla caches
the lock chime and only re-reads it after a full USB *re-enumeration* —
i.e. a simulated unplug/replug of the whole gadget. This mirrors v1's
``rebind_usb_gadget`` and is the only mechanism observed to make the car
pick up a changed chime.

This module is the Python-side wrapper that shells out to
``/usr/local/bin/tesla_gadget_rebind.sh`` (which unbinds + rebinds the
configfs UDC). Unlike the cache invalidator it is:

* **Synchronous** — activation is a rare, deliberate user action and the
  caller wants a definitive result ("the chime is now active in the
  car") rather than a fire-and-forget debounce.
* **Single-flight** — a rebind briefly detaches BOTH LUNs (including
  TeslaCam); two overlapping rebinds racing over the configfs UDC would
  be unsafe, so concurrent callers serialize on one in-flight rebind.

It imports NO Flask types so it can be unit-tested in isolation.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

# Default command. The B-1 sudoers allowlist (rendered from
# ``B1_SUDOERS_ALLOWLIST`` in ``setup-lib/02-users.sh`` into
# ``/etc/sudoers.d/teslausb-b1``) grants the ``teslausb`` and ``pi`` users
# NOPASSWD permission for this script path, with ``!requiretty``.
# ``sudo -n`` aborts immediately if sudo would need to prompt — turning a
# misconfigured deploy into a loud, fast failure rather than a hang.
DEFAULT_COMMAND: tuple[str, ...] = (
    "sudo",
    "-n",
    "/usr/local/bin/tesla_gadget_rebind.sh",
)

# Default subprocess timeout. The script `sync`s, unbinds, sleeps the
# settle interval (~2 s), rebinds, then waits (bounded) for the gadget
# to come back healthy. 45 s comfortably covers the script's own 30 s
# recovery deadline plus the settle/sync overhead; exceeding it means
# "the rebind wedged" rather than "this is slow".
DEFAULT_TIMEOUT_SECONDS: float = 45.0


@dataclass
class RebindResult:
    """Outcome of a single shell-out to ``tesla_gadget_rebind.sh``."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class GadgetRebinder:
    """Synchronous, single-flight USB-gadget rebinder.

    Thread-safety: :meth:`rebind` may be called from any thread. An
    internal lock guarantees exactly one rebind runs at a time; a second
    caller arriving while a rebind is in flight waits for it to finish
    and then returns that same result (it does NOT trigger a second
    re-enumeration — the in-flight one already re-read the chime).

    Lifecycle: instantiate once at app startup, call :meth:`rebind` from
    activation handlers, call :meth:`shutdown` once during graceful
    shutdown so a rebind requested after shutdown is refused rather than
    detaching the gadget as the worker exits.
    """

    command: Sequence[str] = field(default_factory=lambda: DEFAULT_COMMAND)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _result_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _shutdown: bool = field(default=False, init=False, repr=False)

    def rebind(self) -> RebindResult:
        """Re-enumerate the USB gadget so Tesla re-reads a changed chime.

        Blocks until the rebind completes (or times out). Never raises on
        nonzero exit — the returncode/stderr are recorded in the result
        so the caller can surface a warning. Concurrent callers serialize
        on ``_lock``; the second caller returns the first's result.
        """
        with self._result_lock:
            if self._shutdown:
                logger.debug("rebind() called after shutdown; refusing")
                return RebindResult(returncode=-1, stdout="", stderr="rebinder shut down")
        with self._lock:
            # Re-check under the single-flight lock: shutdown() may have
            # been called by another thread while we waited to acquire it.
            if self._shutdown_requested():
                return RebindResult(returncode=-1, stdout="", stderr="rebinder shut down")
            return self._run_once()

    def shutdown(self) -> None:
        """Mark shutdown so no new rebinds start as the worker exits."""
        with self._result_lock:
            self._shutdown = True
        logger.debug("gadget rebinder shut down")

    # ─── Internals ───────────────────────────────────────────────────

    def _shutdown_requested(self) -> bool:
        """Return whether shutdown has been requested (read under lock)."""
        with self._result_lock:
            return self._shutdown

    def _run_once(self) -> RebindResult:
        """Shell out to the helper script. Never raises on nonzero exit."""
        cmd = list(self.command)
        logger.info("invoking gadget rebinder: %s", cmd)
        try:
            # `check=False`: we record returncode + stderr in the result
            # so the caller (or journalctl) can see what happened; we
            # never raise CalledProcessError.
            # rationale for the noqa below: argv is constructed from
            # `self.command`, which defaults to DEFAULT_COMMAND (a frozen
            # tuple pinned to the sudoers-permitted argv); the only way
            # to inject is to construct GadgetRebinder with a different
            # command, which is trusted DI not user input.
            completed = subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            logger.error(
                "gadget rebinder timed out after %.1fs: %s",
                self.timeout_seconds,
                exc,
            )
            return RebindResult(
                returncode=-1,
                stdout=str(exc.stdout) if exc.stdout else "",
                stderr=f"timeout after {self.timeout_seconds}s",
            )
        except OSError as exc:
            logger.error("gadget rebinder failed to start: %s", exc)
            return RebindResult(returncode=-1, stdout="", stderr=str(exc))

        result = RebindResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if result.ok:
            logger.info("gadget rebinder ok")
        else:
            logger.warning(
                "gadget rebinder returned %d: %s",
                result.returncode,
                result.stderr.strip(),
            )
        return result
