from __future__ import annotations

from typing import TYPE_CHECKING

from .paths import candidate_db_paths, canonical_key
from .service import IndexOutcome, IndexResult

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def _dedupe_existing_rows(
    connection: sqlite3.Connection,
    rel_path: str,
    video_path: Path,
) -> IndexResult | None:
    del rel_path
    rel_candidates = candidate_db_paths(canonical_key(video_path))
    rows = _select_existing_paths(connection, rel_candidates)
    if not rows:
        return None
    return IndexResult(IndexOutcome.ALREADY_INDEXED)


def _already_indexed_by_basename(connection: sqlite3.Connection, basename: str) -> bool:
    escaped = basename.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    row = connection.execute(
        "SELECT 1 FROM indexed_files "
        "WHERE file_path LIKE ? ESCAPE '\\' AND waypoint_count > 0 LIMIT 1",
        (f"%{escaped}",),
    ).fetchone()
    return row is not None


def _select_existing_paths(
    connection: sqlite3.Connection,
    rel_paths: tuple[str, ...],
) -> tuple[sqlite3.Row, ...]:
    placeholders = ", ".join("?" * len(rel_paths))
    rows = connection.execute(
        f"SELECT DISTINCT video_path FROM waypoints WHERE video_path IN ({placeholders})",
        rel_paths,
    ).fetchall()
    return tuple(rows)


def _update_waypoint_paths(
    connection: sqlite3.Connection,
    new_path: str | None,
    rel_paths: tuple[str, ...],
) -> int:
    placeholders = ", ".join("?" * len(rel_paths))
    return connection.execute(
        f"UPDATE waypoints SET video_path = ? WHERE video_path IN ({placeholders})",
        (new_path, *rel_paths),
    ).rowcount


def _update_event_paths(
    connection: sqlite3.Connection,
    new_path: str | None,
    rel_paths: tuple[str, ...],
) -> int:
    placeholders = ", ".join("?" * len(rel_paths))
    return connection.execute(
        f"UPDATE detected_events SET video_path = ? WHERE video_path IN ({placeholders})",
        (new_path, *rel_paths),
    ).rowcount
