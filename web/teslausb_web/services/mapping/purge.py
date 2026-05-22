from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .dedupe import _update_event_paths, _update_waypoint_paths
from .paths import candidate_db_paths, canonical_key

if TYPE_CHECKING:
    import sqlite3

    from .service import MappingService


def purge_deleted_videos(
    service: MappingService,
    *,
    deleted_paths: tuple[str | Path, ...] | None = None,
) -> dict[str, int]:
    if deleted_paths is not None:
        return _purge_targeted_paths(service, tuple(Path(path) for path in deleted_paths))
    missing = _missing_indexed_files(service)
    if not missing:
        return _purge_result()
    return _purge_targeted_paths(service, missing)


def _purge_targeted_paths(
    service: MappingService,
    deleted_paths: tuple[Path, ...],
) -> dict[str, int]:
    result = _purge_result()
    with service.open_db() as connection:
        for deleted_path in deleted_paths:
            if _has_surviving_copy(service, deleted_path):
                continue
            result["purged_files"] += connection.execute(
                "DELETE FROM indexed_files WHERE file_path = ?",
                (str(deleted_path),),
            ).rowcount
            rel_paths = candidate_db_paths(canonical_key(deleted_path))
            result["purged_waypoints"] += _clear_video_references(
                connection,
                table_name="waypoints",
                rel_paths=rel_paths,
            )
            result["purged_events"] += _clear_video_references(
                connection,
                table_name="detected_events",
                rel_paths=rel_paths,
            )
        connection.commit()
    return result


def _purge_result() -> dict[str, int]:
    return {
        "purged_files": 0,
        "purged_waypoints": 0,
        "purged_events": 0,
        "purged_trips": 0,
    }


def _missing_indexed_files(service: MappingService) -> tuple[Path, ...]:
    with service.open_db() as connection:
        rows = connection.execute(
            "SELECT file_path FROM indexed_files ORDER BY file_path"
        ).fetchall()
    missing: list[Path] = []
    for row in rows:
        raw_path = row["file_path"]
        if isinstance(raw_path, str):
            candidate = Path(raw_path)
            if not candidate.is_file() and not _has_surviving_copy(service, candidate):
                missing.append(candidate)
    return tuple(missing)


def _has_surviving_copy(service: MappingService, deleted_path: Path) -> bool:
    key = canonical_key(deleted_path)
    basename = deleted_path.name
    if "/" in key:
        candidate = service.config.media_root / key
        return candidate.is_file() and candidate != deleted_path
    candidates = (service.config.media_root / "RecentClips" / basename,)
    return any(path.is_file() and path != deleted_path for path in candidates)


def _clear_video_references(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    rel_paths: tuple[str, ...],
) -> int:
    if table_name == "waypoints":
        return _update_waypoint_paths(connection, None, rel_paths)
    return _update_event_paths(connection, None, rel_paths)
