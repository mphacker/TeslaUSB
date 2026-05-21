from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from teslausb_web.services.mapping.paths import candidate_db_paths, canonical_key

if TYPE_CHECKING:
    from teslausb_web.services.cleanup.service import CleanupConfig, OrphanScan

_CATEGORY_FOLDERS: dict[str, str] = {
    "recent": "RecentClips",
    "saved": "SavedClips",
    "event": "SentryClips",
    "encrypted": "EncryptedClips",
    "archived": "ArchivedClips",
}
_CAMERA_SUFFIXES: tuple[str, ...] = (
    "-front",
    "-back",
    "-left_repeater",
    "-right_repeater",
)
_VIDEO_EXTENSIONS = frozenset({".mp4"})


@dataclass(frozen=True, slots=True)
class ClipFile:
    path: Path
    relative_path: str
    size_bytes: int
    modified_at: datetime


@dataclass(frozen=True, slots=True)
class ClipGroup:
    category: str
    folder_name: str
    recording_key: str
    display_path: str
    files: tuple[ClipFile, ...]
    total_bytes: int
    oldest_modified_at: datetime
    newest_modified_at: datetime
    has_gps: bool
    front_relative_path: str | None


@dataclass(frozen=True, slots=True)
class MappingSnapshot:
    gps_relative_paths: frozenset[str]
    indexed_paths: tuple[str, ...]
    indexed_canonical_keys: frozenset[str]


@dataclass(frozen=True, slots=True)
class OrphanDetails:
    scan: OrphanScan
    fs_only_groups: tuple[ClipGroup, ...]
    db_only_paths: tuple[Path, ...]


DbOpenFn = Callable[[], AbstractContextManager[sqlite3.Connection]]


def load_mapping_snapshot(open_db: DbOpenFn) -> MappingSnapshot:
    gps_relative_paths: set[str] = set()
    indexed_paths: list[str] = []
    with open_db() as connection:
        gps_cursor = connection.execute(
            "SELECT DISTINCT video_path FROM waypoints "
            "WHERE video_path IS NOT NULL AND video_path != ''"
        )
        for row in gps_cursor.fetchall():
            raw_path = row["video_path"]
            if isinstance(raw_path, str):
                gps_relative_paths.add(raw_path)
        index_cursor = connection.execute("SELECT file_path FROM indexed_files ORDER BY file_path")
        while True:
            rows = index_cursor.fetchmany(500)
            if not rows:
                break
            for row in rows:
                raw_path = row["file_path"]
                if isinstance(raw_path, str):
                    indexed_paths.append(raw_path)
    indexed_canonical_keys = frozenset(canonical_key(Path(path)) for path in indexed_paths)
    return MappingSnapshot(
        gps_relative_paths=frozenset(gps_relative_paths),
        indexed_paths=tuple(indexed_paths),
        indexed_canonical_keys=indexed_canonical_keys,
    )


def discover_clip_groups(config: CleanupConfig, snapshot: MappingSnapshot) -> tuple[ClipGroup, ...]:
    grouped_files: dict[tuple[str, str], list[ClipFile]] = {}
    for category, root in _category_roots(config).items():
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not _is_clip_file(path):
                continue
            relative_path = _relative_display_path(path, category=category, config=config)
            stat_result = path.stat()
            clip_file = ClipFile(
                path=path,
                relative_path=relative_path,
                size_bytes=int(stat_result.st_size),
                modified_at=datetime.fromtimestamp(stat_result.st_mtime, tz=UTC),
            )
            group_key = (category, _recording_key(relative_path))
            grouped_files.setdefault(group_key, []).append(clip_file)
    groups: list[ClipGroup] = []
    for (category, recording_key), files in grouped_files.items():
        ordered_files = tuple(sorted(files, key=lambda clip: clip.relative_path))
        display_path = _display_path_for_group(ordered_files)
        oldest_modified_at = min(file.modified_at for file in ordered_files)
        newest_modified_at = max(file.modified_at for file in ordered_files)
        front_relative_path = _front_relative_path(ordered_files)
        has_gps = _group_has_gps(front_relative_path, ordered_files, snapshot)
        groups.append(
            ClipGroup(
                category=category,
                folder_name=_CATEGORY_FOLDERS[category],
                recording_key=recording_key,
                display_path=display_path,
                files=ordered_files,
                total_bytes=sum(file.size_bytes for file in ordered_files),
                oldest_modified_at=oldest_modified_at,
                newest_modified_at=newest_modified_at,
                has_gps=has_gps,
                front_relative_path=front_relative_path,
            )
        )
    return tuple(sorted(groups, key=lambda group: (group.oldest_modified_at, group.display_path)))


def scan_orphans(
    config: CleanupConfig,
    snapshot: MappingSnapshot,
    groups: tuple[ClipGroup, ...],
) -> OrphanDetails:
    from teslausb_web.services.cleanup.service import OrphanScan  # noqa: PLC0415

    cutoff = _utc_now() - timedelta(seconds=config.orphan_min_age_seconds)
    indexable_groups = tuple(
        group
        for group in groups
        if group.front_relative_path is not None and group.newest_modified_at <= cutoff
    )
    fs_canonical_keys = frozenset(
        canonical_key(group.front_relative_path)
        for group in indexable_groups
        if group.front_relative_path is not None
    )
    fs_only_groups = tuple(
        group
        for group in indexable_groups
        if group.front_relative_path is not None
        and canonical_key(group.front_relative_path) not in snapshot.indexed_canonical_keys
    )
    db_only_paths: list[Path] = []
    for raw_path in snapshot.indexed_paths:
        candidate = Path(raw_path)
        if candidate.is_file():
            continue
        if canonical_key(candidate) in fs_canonical_keys:
            continue
        db_only_paths.append(candidate)
    orphan_scan = OrphanScan(
        db_only_paths=tuple(str(path) for path in db_only_paths),
        fs_only_paths=tuple(group.display_path for group in fs_only_groups),
        total_bytes_recoverable=sum(group.total_bytes for group in fs_only_groups),
    )
    return OrphanDetails(
        scan=orphan_scan,
        fs_only_groups=fs_only_groups,
        db_only_paths=tuple(db_only_paths),
    )


def _category_roots(config: CleanupConfig) -> dict[str, Path]:
    return {
        "recent": config.media_root / "RecentClips",
        "saved": config.media_root / "SavedClips",
        "event": config.media_root / "SentryClips",
        "encrypted": config.media_root / "EncryptedClips",
        "archived": config.archive_root,
    }


def _is_clip_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in _VIDEO_EXTENSIONS


def _relative_display_path(path: Path, *, category: str, config: CleanupConfig) -> str:
    if category == "archived":
        relative = path.relative_to(config.archive_root).as_posix()
        prefix = config.archived_clips_dirname
        return relative if not relative else f"{prefix}/{relative}"
    return path.relative_to(config.media_root).as_posix()


def _recording_key(relative_path: str) -> str:
    posix_path = PurePosixPath(relative_path)
    stem = posix_path.stem
    for suffix in _CAMERA_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    parent = posix_path.parent.as_posix()
    if parent == ".":
        return stem
    return f"{parent}/{stem}"


def _display_path_for_group(files: tuple[ClipFile, ...]) -> str:
    front = _front_relative_path(files)
    return files[0].relative_path if front is None else front


def _front_relative_path(files: tuple[ClipFile, ...]) -> str | None:
    for file in files:
        if file.relative_path.endswith("-front.mp4"):
            return file.relative_path
    return None


def _group_has_gps(
    front_relative_path: str | None,
    files: tuple[ClipFile, ...],
    snapshot: MappingSnapshot,
) -> bool:
    if front_relative_path is not None and _path_has_gps(front_relative_path, snapshot):
        return True
    return any(_path_has_gps(file.relative_path, snapshot) for file in files)


def _path_has_gps(relative_path: str, snapshot: MappingSnapshot) -> bool:
    return any(
        candidate in snapshot.gps_relative_paths
        for candidate in candidate_db_paths(canonical_key(relative_path))
    )


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)
