"""Privileged TeslaCam clip deletion via a root-owned helper script.

The Flask web app runs as ``pi``, but Tesla — and the Rust materializer
that rebuilds the synthetic FAT tree — create per-event clip directories
as ``teslausb:teslausb`` mode 0755. ``pi`` is a member of the ``teslausb``
group, but 0755 grants the group only ``r-x``, so ``pi`` cannot unlink the
files inside an event directory: the in-UI Delete button fails with
EACCES (see :class:`~teslausb_web.services.video_service._paths.ClipPermissionError`).

Rather than migrate the entire web service to the ``teslausb`` account
(which would also require handing it the broad ``sudo`` surface ``pi``
holds for samba / storage-health / reboot), the deployment grants ``pi``
NOPASSWD for exactly one narrow helper, ``teslausb_delete_clip.sh``, which
re-validates that its argument is a real TeslaCam clip directory before
removing it. This module is the Python-side wrapper that shells out to it.

It is used ONLY as a fallback: the video service attempts a direct
``shutil.rmtree`` first and only calls :meth:`PrivilegedClipDeleter.delete`
when that raises ``ClipPermissionError``. That keeps the privileged path
off the hot path entirely on a correctly-permissioned tree.

This module imports NO Flask types so it can be unit-tested in isolation.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)

# Hard upper bound on how long the helper may run. A single ``rm -rf`` of
# one event directory (a few dozen small MP4 segments) completes in
# milliseconds; 30 s is "something is very wrong" (e.g. sudo prompting on
# a misconfigured deploy, which ``-n`` should already turn into an instant
# failure rather than a hang).
DEFAULT_TIMEOUT_SECONDS: float = 30.0


class PrivilegedDeleteError(RuntimeError):
    """The root-owned delete helper failed or could not be invoked.

    Wraps every failure mode — nonzero exit, timeout, OS-level spawn
    failure — so the caller can translate it into the package's public
    ``DeletionError`` without caring about the underlying subprocess
    detail.
    """


@dataclass(frozen=True, slots=True)
class PrivilegedClipDeleter:
    """Deletes a TeslaCam clip directory via ``sudo`` + a root helper.

    The command run is ``[*sudo_prefix, str(script), str(path)]``. The
    ``sudo_prefix`` (``("sudo", "-n")`` in production, empty in tests)
    and ``script`` path both come from trusted config / dependency
    injection — never from request data — so the constructed argv is not
    attacker-controlled. The only request-derived value is ``path``,
    which the helper itself re-validates for containment.
    """

    script: Path
    sudo_prefix: Sequence[str] = field(default_factory=tuple)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def delete(self, path: Path) -> None:
        """Remove ``path`` via the helper; raise on any failure.

        Returns ``None`` on success (including the idempotent case where
        the target was already gone — the helper exits 0). Raises
        :class:`PrivilegedDeleteError` otherwise.
        """
        cmd = [*self.sudo_prefix, str(self.script), str(path)]
        logger.info("privileged clip delete: %s", cmd)
        try:
            # rationale for the noqa: every element of ``cmd`` derives
            # from injected config (sudo_prefix, script) except the final
            # path, which is passed as a single argv element (no shell)
            # and independently re-validated by the helper. ``check=False``
            # so we can log the helper's stderr before raising.
            completed = subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            msg = f"privileged delete timed out after {self.timeout_seconds}s: {path}"
            logger.error(msg)
            raise PrivilegedDeleteError(msg) from exc
        except OSError as exc:
            msg = f"privileged delete failed to start: {exc}"
            logger.error(msg)
            raise PrivilegedDeleteError(msg) from exc

        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            msg = f"privileged delete exited {completed.returncode}: {stderr or path}"
            logger.warning(msg)
            raise PrivilegedDeleteError(msg)
        logger.info("privileged clip delete ok: %s", path)
