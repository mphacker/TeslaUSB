"""Best-effort pruning of deleted clips from the worker mapping index.

The Rust worker owns normal indexing and periodic orphan GC. This
module exists only for user-initiated web deletes so the map stops
showing a deleted clip/event immediately after the files are removed.
It mirrors the cloud-archive services' established direct-SQLite access
pattern and deliberately has no Flask dependency.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

_DB_CONNECT_TIMEOUT_SECONDS: Final[float] = 10.0
_FOREIGN_KEYS_ON_PRAGMA: Final[str] = "PRAGMA foreign_keys = ON"


@dataclass(frozen=True, slots=True)
class PruneResult:
    """Counts for worker-index rows removed after a web delete."""

    clips_deleted: int = 0
    clip_events_deleted: int = 0
    detected_events_deleted: int = 0
    waypoints_deleted: int = 0


@contextmanager
def _open_mapping_db(mapping_db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(str(mapping_db_path), timeout=_DB_CONNECT_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(_FOREIGN_KEYS_ON_PRAGMA)
        yield connection
        connection.commit()
    finally:
        connection.close()


def prune_deleted_clips(
    mapping_db_path: Path,
    backing_root: Path,
    deleted_relative_paths: Iterable[Path],
) -> PruneResult:
    """Remove clip rows matching deleted paths from the worker index.

    ``deleted_relative_paths`` must use the worker convention: paths are
    relative to ``backing_root`` (for example,
    ``TeslaCam/SentryClips/<event>/<clip>-front.mp4``). Missing database
    files are treated as a no-op because the worker's periodic orphan GC
    remains the authoritative backstop.
    """
    relative_paths = _normalise_relative_paths(deleted_relative_paths, backing_root)
    if not mapping_db_path.is_file() or not relative_paths:
        return PruneResult()

    with _open_mapping_db(mapping_db_path) as connection:
        clip_ids = _clip_ids_for_relative_paths(connection, relative_paths)
        if not clip_ids:
            return PruneResult()
        detected_events_deleted = _delete_detected_events_for_clip_ids(connection, clip_ids)
        waypoints_deleted = _count_waypoints_for_clip_ids(connection, clip_ids)
        clips_deleted = _delete_clips_for_relative_paths(connection, relative_paths)
        return PruneResult(
            clips_deleted=clips_deleted,
            detected_events_deleted=detected_events_deleted,
            waypoints_deleted=waypoints_deleted,
        )


def prune_deleted_event_folder(
    mapping_db_path: Path,
    backing_root: Path,
    deleted_event_relative_dir: Path,
    deleted_relative_paths: Iterable[Path],
) -> PruneResult:
    """Remove an event directory's clip rows and raw ``clip_events`` row."""
    relative_paths = _normalise_relative_paths(deleted_relative_paths, backing_root)
    event_dir = _db_path_text(_relative_to_backing_root(deleted_event_relative_dir, backing_root))
    if not mapping_db_path.is_file():
        return PruneResult()

    with _open_mapping_db(mapping_db_path) as connection:
        clip_ids = _clip_ids_for_relative_paths(connection, relative_paths)
        detected_events_deleted = _delete_detected_events_for_clip_ids(connection, clip_ids)
        waypoints_deleted = _count_waypoints_for_clip_ids(connection, clip_ids)
        clip_events_deleted = _delete_clip_events_for_event_dir(connection, event_dir)
        clips_deleted = _delete_clips_for_relative_paths(connection, relative_paths)
        return PruneResult(
            clips_deleted=clips_deleted,
            clip_events_deleted=clip_events_deleted,
            detected_events_deleted=detected_events_deleted,
            waypoints_deleted=waypoints_deleted,
        )


def _normalise_relative_paths(paths: Iterable[Path], backing_root: Path) -> tuple[str, ...]:
    unique = {_db_path_text(_relative_to_backing_root(path, backing_root)) for path in paths}
    return tuple(sorted(path for path in unique if path))


def _relative_to_backing_root(path: Path, backing_root: Path) -> Path:
    if not path.is_absolute():
        return path
    return path.resolve(strict=False).relative_to(backing_root.resolve(strict=False))


def _db_path_text(path: Path) -> str:
    return path.as_posix()


def _clip_ids_for_relative_paths(
    connection: sqlite3.Connection,
    relative_paths: tuple[str, ...],
) -> tuple[int, ...]:
    clip_ids: list[int] = []
    for relative_path in relative_paths:
        row = connection.execute(
            "SELECT id FROM clips WHERE relative_path = ?",
            (relative_path,),
        ).fetchone()
        if row is not None:
            clip_ids.append(int(row["id"]))
    return tuple(clip_ids)


def _delete_detected_events_for_clip_ids(
    connection: sqlite3.Connection,
    clip_ids: tuple[int, ...],
) -> int:
    if not clip_ids or not _table_exists(connection, "detected_events"):
        return 0
    deleted = 0
    for clip_id in clip_ids:
        deleted += int(
            connection.execute(
                "DELETE FROM detected_events WHERE clip_id = ?",
                (clip_id,),
            ).rowcount
        )
    return deleted


def _count_waypoints_for_clip_ids(
    connection: sqlite3.Connection,
    clip_ids: tuple[int, ...],
) -> int:
    total = 0
    for clip_id in clip_ids:
        row = connection.execute(
            "SELECT COUNT(*) AS c FROM waypoints WHERE clip_id = ?",
            (clip_id,),
        ).fetchone()
        total += 0 if row is None else int(row["c"])
    return total


def _delete_clip_events_for_event_dir(connection: sqlite3.Connection, event_dir: str) -> int:
    if not event_dir or not _table_exists(connection, "clip_events"):
        return 0
    return int(
        connection.execute(
            "DELETE FROM clip_events WHERE event_dir_relative_path = ?",
            (event_dir,),
        ).rowcount
    )


def _delete_clips_for_relative_paths(
    connection: sqlite3.Connection,
    relative_paths: tuple[str, ...],
) -> int:
    deleted = 0
    for relative_path in relative_paths:
        deleted += int(
            connection.execute(
                "DELETE FROM clips WHERE relative_path = ?",
                (relative_path,),
            ).rowcount
        )
    return deleted


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None
