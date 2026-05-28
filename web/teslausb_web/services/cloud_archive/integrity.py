"""Detect and prune broken MP4 clips on the TeslaCam volume.

Tesla writes the ``moov`` atom (the index needed to play an MP4) at
file close. If the device reboots or the USB gadget swaps mid-write,
the resulting file lacks ``moov`` and is unplayable forever — it has
no value, will never play, and shouldn't be synced to cloud or kept
on disk consuming retention budget.

This module walks top-level ISO BMFF boxes looking for ``moov``.
Files that lack it are deleted in-place, but only when they appear
*idle* — files modified within :data:`BROKEN_VIDEO_IDLE_SECONDS` are
assumed to still be receiving writes from the vehicle and are left
alone so we never race Tesla's writer.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

BROKEN_VIDEO_IDLE_SECONDS: Final[float] = 90.0
"""A file modified within this window is assumed to still be written by Tesla."""

_BOX_HEADER_SIZE: Final[int] = 8
_LARGE_SIZE_EXT: Final[int] = 8
_MAX_BOX_WALK: Final[int] = 4096
"""Safety cap so a malformed file with bogus sizes can't make us spin forever."""

_VIDEO_SUFFIXES: Final[frozenset[str]] = frozenset({".mp4"})


@dataclass(slots=True)
class PurgeReport:
    """Outcome of a single :func:`purge_broken_videos` pass."""

    scanned: int = 0
    broken_found: int = 0
    deleted: int = 0
    skipped_in_use: int = 0
    errors: int = 0
    deleted_paths: list[Path] = field(default_factory=list)


def has_moov_atom(path: Path) -> bool:
    """Return ``True`` iff ``path`` contains a top-level ``moov`` box.

    Walks the ISO BMFF box chain reading only 8-byte headers, so this
    is O(number of top-level boxes) regardless of file size. Treats
    any I/O error or malformed header as "no moov" — broken files
    can't be played anyway.
    """
    try:
        size_total = path.stat().st_size
    except OSError:
        return False
    if size_total < _BOX_HEADER_SIZE:
        return False
    try:
        with path.open("rb") as handle:
            offset = 0
            steps = 0
            while offset + _BOX_HEADER_SIZE <= size_total and steps < _MAX_BOX_WALK:
                handle.seek(offset)
                header = handle.read(_BOX_HEADER_SIZE)
                if len(header) < _BOX_HEADER_SIZE:
                    return False
                size = int.from_bytes(header[:4], "big")
                box_type = header[4:_BOX_HEADER_SIZE]
                if box_type == b"moov":
                    return True
                header_consumed = _BOX_HEADER_SIZE
                if size == 1:
                    ext = handle.read(_LARGE_SIZE_EXT)
                    if len(ext) < _LARGE_SIZE_EXT:
                        return False
                    size = int.from_bytes(ext, "big")
                    header_consumed += _LARGE_SIZE_EXT
                elif size == 0:
                    return False
                if size < header_consumed:
                    return False
                offset += size
                steps += 1
            return False
    except OSError:
        return False


def _iter_candidate_videos(teslacam_path: Path, folders: Iterable[str]) -> Iterable[Path]:
    """Yield every ``*.mp4`` under each configured top-level folder.

    Walks both ``<folder>/clip.mp4`` (RecentClips layout) and
    ``<folder>/<event-dir>/clip.mp4`` (SentryClips / SavedClips
    layout). Symlinks and unreadable directories are skipped silently.
    """
    for folder in folders:
        root = teslacam_path / folder
        if not root.is_dir():
            continue
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_file() and entry.suffix.lower() in _VIDEO_SUFFIXES:
                    yield entry
                    continue
                if entry.is_dir():
                    try:
                        for child in entry.iterdir():
                            if (
                                child.is_file()
                                and child.suffix.lower() in _VIDEO_SUFFIXES
                            ):
                                yield child
                    except OSError:
                        continue
            except OSError:
                continue


def purge_broken_videos(
    teslacam_path: Path,
    folders: Iterable[str],
    *,
    now: float | None = None,
    idle_seconds: float = BROKEN_VIDEO_IDLE_SECONDS,
) -> PurgeReport:
    """Delete idle MP4s that are missing a ``moov`` box.

    Files whose mtime falls within ``idle_seconds`` of ``now`` are
    treated as actively being written by Tesla and are NOT deleted,
    even if they currently lack a ``moov`` box (Tesla writes the
    index last, so the file is "broken" until close — that's normal
    for an in-progress clip).
    """
    report = PurgeReport()
    threshold = (time.time() if now is None else now) - idle_seconds
    folder_list = tuple(folders)
    for video in _iter_candidate_videos(teslacam_path, folder_list):
        report.scanned += 1
        try:
            stat = video.stat()
        except OSError:
            report.errors += 1
            continue
        if has_moov_atom(video):
            continue
        report.broken_found += 1
        if stat.st_mtime > threshold:
            report.skipped_in_use += 1
            continue
        try:
            video.unlink()
        except OSError as exc:
            logger.warning(
                "integrity: failed to delete broken video %s: %s", video, exc
            )
            report.errors += 1
            continue
        report.deleted += 1
        report.deleted_paths.append(video)
        logger.info(
            "integrity: deleted broken video %s (size=%d bytes, mtime_age=%.0fs)",
            video,
            stat.st_size,
            (time.time() if now is None else now) - stat.st_mtime,
        )
    if report.broken_found or report.deleted or report.skipped_in_use:
        logger.info(
            "integrity: purge complete scanned=%d broken=%d deleted=%d in_use=%d errors=%d",
            report.scanned,
            report.broken_found,
            report.deleted,
            report.skipped_in_use,
            report.errors,
        )
    return report
