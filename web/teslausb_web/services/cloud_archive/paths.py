"""Path and folder helpers for cloud archive."""

from __future__ import annotations

import logging
import posixpath
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)
_ROOT_AND_CHILD_SEGMENTS: Final[int] = 2

KNOWN_CLOUD_ROOTS: Final[tuple[str, ...]] = (
    "RecentClips",
    "SentryClips",
    "SavedClips",
    "TeslaTrackMode",
)
EVENT_FOLDER_NAMES: Final[tuple[str, ...]] = ("SentryClips", "SavedClips")
VALID_SYNC_FOLDERS: Final[tuple[str, ...]] = (
    "SavedClips",
    "SentryClips",
    "RecentClips",
)


def canonical_cloud_path(file_path: str) -> str:
    """Normalise a cloud path to a canonical relative POSIX form."""

    if not file_path:
        return file_path
    candidate = file_path.replace("\\", "/")
    for segment in candidate.split("/"):
        if segment == "..":
            raise ValueError(f"Path traversal is not permitted: {file_path!r}")
    stripped: str | None = None
    for root in KNOWN_CLOUD_ROOTS:
        marker = f"/{root}/"
        index = candidate.find(marker)
        if index >= 0:
            stripped = candidate[index + 1 :]
            break
        if candidate == root or candidate.startswith(f"{root}/"):
            stripped = candidate
            break
    if stripped is not None:
        candidate = stripped
    candidate = posixpath.normpath(candidate)
    if candidate == ".":
        return ""
    while candidate.startswith("/"):
        candidate = candidate[1:]
    return candidate.rstrip("/")


def _canonical_rel_path_from_local(local_path: str | Path, teslacam_root: Path) -> str:
    """Map a local file or directory path into its canonical cloud-relative form."""

    resolved = Path(local_path).resolve(strict=False)
    root = teslacam_root.resolve(strict=False)
    if resolved.is_relative_to(root):
        return canonical_cloud_path(resolved.relative_to(root).as_posix())
    logger.warning(
        "%s is outside TeslaCam root %s; falling back to basename",
        resolved,
        root,
    )
    return canonical_cloud_path(resolved.name)


def _normalize_folder_list(values: object) -> tuple[str, ...]:
    """Coerce config input into a deduplicated folder tuple."""

    if not isinstance(values, (list, tuple)):
        return ()
    ordered: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            continue
        folder = raw.strip()
        if folder in VALID_SYNC_FOLDERS and folder not in ordered:
            ordered.append(folder)
    return tuple(ordered)


def _folder_of_event_rel(rel_path: str) -> str:
    if not rel_path:
        return ""
    parts = rel_path.split("/", 1)
    return parts[0] if len(parts) == _ROOT_AND_CHILD_SEGMENTS else ""
