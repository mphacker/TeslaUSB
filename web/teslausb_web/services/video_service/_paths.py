"""Path-resolution + traversal guards + B-1-native safe-delete.

This module owns *all* path security checks for the videos package.
The rules are deliberately strict — the public surface of the
videos blueprint takes user-supplied ``<path:filepath>`` segments
straight from the URL, so a single resolution miss would be a
traversal CVE.

B-1 contract (differs from v1):

* No IMG marker check. B-1 has no loopback IMG files
  (``docs/00-PLAN.md`` invariant). Path containment alone is the
  security boundary.
* No mount-state check. The Rust ``teslafat`` worker manages bind
  mounts; the Flask UI just probes ``Path.exists()``.
* No ``protected_files`` set. Nothing under the videos blueprint
  is in a position to delete an IMG marker because there are no
  IMG markers in B-1.

There is no Flask import in this module.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class PathSecurityError(ValueError):
    """The requested path resolves outside any allowed root.

    Always logged at WARNING with the resolved path that triggered
    it — useful when triaging probe attempts in the access log.
    """


class DeletionError(RuntimeError):
    """Filesystem deletion failed for reasons other than missing file."""


@dataclass(frozen=True, slots=True)
class ResolvedClip:
    """A path that has passed the containment check.

    ``allowed_root`` is the specific root that accepted the path —
    callers occasionally need to know which root in order to compute
    download names.
    """

    path: Path
    allowed_root: Path


def resolve_clip_path(filepath: str, allowed_roots: tuple[Path, ...]) -> ResolvedClip:
    """Resolve a user-supplied path against the allow-list.

    ``filepath`` is the raw ``<path:filepath>`` segment from the URL
    (``"SentryClips/2025-01-01_00-00-00/front.mp4"`` or
    ``"RecentClips/2024-12-25_12-00-00-front.mp4"``). Each path
    component is basenamed before joining so embedded ``..`` is
    neutralised at the syntactic level; the resolved real path is
    then checked against each allow-listed root with
    ``Path.is_relative_to`` for the semantic guarantee.

    Raises :class:`PathSecurityError` if no root contains the
    resolved target. Raises :class:`FileNotFoundError` if the target
    doesn't exist on disk.
    """
    if not filepath:
        raise PathSecurityError("empty filepath")
    parts = [p for p in filepath.split("/") if p]
    if not parts:
        raise PathSecurityError("empty filepath after split")
    safe_parts = [Path(p).name for p in parts]
    if any(not p or p in {".", ".."} for p in safe_parts):
        raise PathSecurityError(f"suspicious path segment in {filepath!r}")

    for root in allowed_roots:
        try:
            root_resolved = root.resolve(strict=False)
        except OSError as exc:
            logger.warning("video: cannot resolve allowed root %s: %s", root, exc)
            continue
        if not root_resolved.exists():
            continue
        candidate = (root_resolved / Path(*safe_parts)).resolve(strict=False)
        if not _is_relative_to(candidate, root_resolved):
            continue
        if candidate.exists():
            return ResolvedClip(path=candidate, allowed_root=root_resolved)

    logger.warning("video: path %r not found in any allowed root", filepath)
    raise FileNotFoundError(filepath)


def assert_inside(path: Path, allowed_roots: tuple[Path, ...]) -> Path:
    """Resolve ``path`` and assert it is inside one of ``allowed_roots``.

    Used by :func:`safe_delete_clip` and the zip streamer. Returns
    the resolved path on success, raises :class:`PathSecurityError`
    otherwise.
    """
    try:
        resolved = path.resolve(strict=False)
    except OSError as exc:  # pragma: no cover — extremely rare on POSIX
        raise PathSecurityError(f"cannot resolve {path}: {exc}") from exc
    for root in allowed_roots:
        try:
            root_resolved = root.resolve(strict=False)
        except OSError:
            continue
        if _is_relative_to(resolved, root_resolved):
            return resolved
    logger.warning("video: path %s is outside allowed roots", resolved)
    raise PathSecurityError(f"path outside allowed roots: {resolved}")


def safe_delete_clip(target: Path, allowed_roots: tuple[Path, ...]) -> None:
    """Delete a file or directory, refusing anything outside the allow-list.

    B-1-native helper — see module docstring. There are deliberately
    no IMG, mount-state, or protected-file probes; path containment
    is the entire contract.
    """
    resolved = assert_inside(target, allowed_roots)
    if resolved.is_dir():
        try:
            shutil.rmtree(resolved)
        except OSError as exc:
            raise DeletionError(f"rmtree failed: {resolved}: {exc}") from exc
        return
    try:
        resolved.unlink()
    except FileNotFoundError:
        # Treat as success — the post-condition (file absent) holds.
        return
    except OSError as exc:
        raise DeletionError(f"unlink failed: {resolved}: {exc}") from exc


def _is_relative_to(candidate: Path, root: Path) -> bool:
    """Backport of ``Path.is_relative_to`` semantics for safety.

    ``Path.is_relative_to`` exists on 3.9+, but we don't want a
    ``ValueError`` re-raised on a non-match — we want a boolean
    that the caller can branch on without try/except noise.
    """
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True
