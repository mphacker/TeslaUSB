from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_CAMERA_FOLDERS = ("SavedClips", "SentryClips")
_ARCHIVE_SUBTREES = ("RecentClips", "SavedClips", "SentryClips")


def _find_front_camera_videos(media_root: Path, archive_root: Path) -> Iterator[Path]:
    seen_basenames: set[str] = set()
    for path in _find_archived_videos(archive_root):
        seen_basenames.add(path.name)
        yield path
    for folder_name in _CAMERA_FOLDERS:
        yield from _iter_event_folder_videos(media_root / folder_name)
    recent_root = media_root / "RecentClips"
    if not recent_root.is_dir():
        return
    for path in sorted(recent_root.iterdir()):
        if _is_front_camera_video(path) and path.name not in seen_basenames:
            yield path


def _find_archived_videos(archive_root: Path) -> Iterator[Path]:
    if not archive_root.is_dir():
        return
    for path in sorted(archive_root.iterdir()):
        if _is_front_camera_video(path):
            yield path
    for subtree in _ARCHIVE_SUBTREES:
        root = archive_root / subtree
        if not root.is_dir():
            continue
        yield from _iter_archive_subtree(root)


def _iter_archived_with_mtime(archive_root: Path) -> Iterator[tuple[Path, float]]:
    for path in _find_archived_videos(archive_root):
        try:
            yield path, path.stat().st_mtime
        except OSError:
            continue


def _iter_archive_subtree(root: Path) -> Iterator[Path]:
    for entry in sorted(root.iterdir()):
        if _is_front_camera_video(entry):
            yield entry
            continue
        if entry.is_dir():
            yield from _iter_flat_front_videos(entry)


def _iter_event_folder_videos(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    for event_dir in sorted(root.iterdir()):
        if event_dir.is_dir():
            yield from _iter_flat_front_videos(event_dir)


def _iter_flat_front_videos(root: Path) -> Iterator[Path]:
    for path in sorted(root.iterdir()):
        if _is_front_camera_video(path):
            yield path


def _is_front_camera_video(path: Path) -> bool:
    name = path.name.lower()
    return path.is_file() and name.endswith(".mp4") and "-front" in name
