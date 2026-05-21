"""Filesystem discovery and priority scoring for cloud archive."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from re import Pattern
from re import compile as re_compile
from typing import Final

from teslausb_web.services.cloud_archive.paths import (
    _folder_of_event_rel,
    _normalize_folder_list,
    canonical_cloud_path,
)
from teslausb_web.services.cloud_archive.settings import (
    FOLDER_PRIORITY_MULTIPLIER,
    NO_EVENT_SCORE_THRESHOLD,
    CloudArchiveConfig,
    _read_priority_order_setting,
    _read_sync_folders_setting,
    _read_sync_non_event_setting,
)

logger = logging.getLogger(__name__)
_TIMESTAMP_RE: Final[Pattern[str]] = re_compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}")
_SYNC_NON_EVENT_SCORE_LIMIT: Final[int] = 200


@dataclass(frozen=True, slots=True)
class EventCandidate:
    local_path: Path
    relative_path: str
    size_bytes: int
    score: int


def _load_geo_hits(mapping_db_path: Path | None) -> frozenset[str] | None:
    if mapping_db_path is None or not mapping_db_path.is_file():
        return None
    try:
        connection = sqlite3.connect(str(mapping_db_path), timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        rows = connection.execute(
            "SELECT DISTINCT video_path FROM waypoints WHERE video_path IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    hits: set[str] = set()
    for row in rows:
        raw_path = row[0]
        if not isinstance(raw_path, str) or not raw_path:
            continue
        path = Path(raw_path)
        if path.parent.name:
            hits.add(path.parent.name)
        if path.name:
            hits.add(path.name)
            match = _TIMESTAMP_RE.match(path.name)
            if match is not None:
                hits.add(match.group(0))
    return frozenset(hits)


def _score_event_priority(
    event_path: Path,
    geo_hits: frozenset[str] | None = None,
) -> int:
    score = NO_EVENT_SCORE_THRESHOLD
    name = event_path.name
    event_json_path = event_path / "event.json"
    if event_path.is_dir() and event_json_path.is_file():
        try:
            payload = json.loads(event_json_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload.get("reason"), str) and payload["reason"].strip():
            score = 0
    if score >= NO_EVENT_SCORE_THRESHOLD and geo_hits is not None and name in geo_hits:
        score = 100
    try:
        parsed = datetime.strptime(name[:19], "%Y-%m-%d_%H-%M-%S").replace(tzinfo=UTC)
        days_old = min(99, max(0, (datetime.now(UTC) - parsed).days))
        score += 99 - days_old
    except ValueError:
        score += 50
    return score


def _folder_priority_index(folder: str, priority_order: tuple[str, ...]) -> int:
    try:
        return list(priority_order).index(folder)
    except ValueError:
        return len(priority_order)


def _is_path_skipped(connection: sqlite3.Connection | None, relative_path: str) -> bool:
    if connection is None:
        return False
    try:
        row = connection.execute(
            (
                "SELECT 1 FROM cloud_synced_files WHERE file_path = ? "
                "AND status IN ('synced', 'dead_letter') LIMIT 1"
            ),
            (relative_path,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _discover_events(
    config: CloudArchiveConfig,
    connection: sqlite3.Connection | None = None,
) -> tuple[EventCandidate, ...]:
    sync_folders = _normalize_folder_list(_read_sync_folders_setting(config))
    priority_order = _normalize_folder_list(_read_priority_order_setting(config))
    geo_hits = _load_geo_hits(config.mapping_db_path)
    candidates: list[EventCandidate] = []
    for folder in sync_folders:
        folder_path = config.teslacam_path / folder
        if folder == "ArchivedClips":
            if not folder_path.is_dir():
                continue
            for child in sorted(folder_path.iterdir()):
                if not child.is_file() or child.suffix.lower() not in {".mp4", ".ts"}:
                    continue
                relative_path = canonical_cloud_path(f"ArchivedClips/{child.name}")
                if _is_path_skipped(connection, relative_path):
                    continue
                candidates.append(
                    EventCandidate(
                        local_path=child,
                        relative_path=relative_path,
                        size_bytes=child.stat().st_size,
                        score=_score_event_priority(child, geo_hits),
                    )
                )
            continue
        if not folder_path.is_dir():
            continue
        for child in sorted(folder_path.iterdir()):
            if not child.is_dir():
                continue
            total_size = sum(entry.stat().st_size for entry in child.iterdir() if entry.is_file())
            has_video = any(
                entry.is_file() and entry.suffix.lower() in {".mp4", ".ts"}
                for entry in child.iterdir()
            )
            if not has_video:
                continue
            relative_path = canonical_cloud_path(f"{folder}/{child.name}")
            if _is_path_skipped(connection, relative_path):
                continue
            candidates.append(
                EventCandidate(
                    local_path=child,
                    relative_path=relative_path,
                    size_bytes=total_size,
                    score=_score_event_priority(child, geo_hits),
                )
            )
    if not _read_sync_non_event_setting(config):
        candidates = [
            candidate for candidate in candidates if candidate.score < _SYNC_NON_EVENT_SCORE_LIMIT
        ]
    candidates.sort(
        key=lambda candidate: (
            _folder_priority_index(
                _folder_of_event_rel(candidate.relative_path),
                priority_order,
            )
            * FOLDER_PRIORITY_MULTIPLIER
            + candidate.score
        )
    )
    return tuple(candidates)
