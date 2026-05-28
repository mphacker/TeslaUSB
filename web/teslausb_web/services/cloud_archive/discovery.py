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
    EVENT_FOLDER_NAMES,
    _folder_of_event_rel,
    _normalize_folder_list,
    canonical_cloud_path,
)
from teslausb_web.services.cloud_archive.settings import (
    CLOUD_PRIORITY_BULK,
    CLOUD_PRIORITY_HARSH_BRAKE,
    CLOUD_PRIORITY_LIVE_EVENT,
    FOLDER_PRIORITY_MULTIPLIER,
    HARD_BRAKE_EVENT_TYPES,
    NO_EVENT_SCORE_THRESHOLD,
    CloudArchiveConfig,
    _read_priority_order_setting,
    _read_sync_folders_setting,
    _read_sync_non_event_setting,
    _read_sync_recent_with_telemetry_setting,
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
    priority: int = CLOUD_PRIORITY_BULK


def _load_hard_brake_hits(mapping_db_path: Path | None) -> frozenset[str] | None:
    """Return basenames + timestamp prefixes of clips flagged as hard-brakes.

    Reads the Rust-managed mapping DB (``index.sqlite3``) and joins
    ``detected_events`` with ``clips`` for any
    ``event_type IN ('harsh_braking', 'emergency_braking')`` — the
    materializer emits these for acceleration_x < -4.0 m/s² (≈ 0.4 g,
    the standard hard-brake threshold) and < -7.0 m/s² respectively.

    A clip whose basename or ``YYYY-MM-DD_HH-MM-SS`` timestamp prefix
    is in the returned set should be queued at priority
    ``CLOUD_PRIORITY_HARSH_BRAKE`` so it jumps ahead of the bulk
    RecentClips backlog on the next sync cycle.

    Returns ``None`` when the DB is missing or unreadable so callers
    can distinguish "no hard-brake events anywhere" (empty set) from
    "couldn't open the index" (skip the priority-bump entirely).
    """
    if mapping_db_path is None or not mapping_db_path.is_file():
        return None
    try:
        connection = sqlite3.connect(str(mapping_db_path), timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        placeholders = ",".join("?" for _ in HARD_BRAKE_EVENT_TYPES)
        rows = connection.execute(
            "SELECT DISTINCT c.relative_path FROM detected_events e "
            "JOIN clips c ON c.id = e.clip_id "
            f"WHERE e.event_type IN ({placeholders})",
            HARD_BRAKE_EVENT_TYPES,
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("cloud discovery: could not read detected_events: %s", exc)
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


def _candidate_priority(
    relative_path: str,
    hard_brake_hits: frozenset[str] | None,
) -> int:
    """Compute the upload priority for a candidate.

    SentryClips and SavedClips are user-meaningful "live" events
    (sentry trigger, driver save, horn tap — Tesla itself promotes
    horn clips into SavedClips). Any RecentClips clip flagged as a
    hard brake by the Rust materializer earns the same priority so
    the user can review the incident as soon as the device sees wifi.
    """
    folder = _folder_of_event_rel(relative_path)
    if folder in EVENT_FOLDER_NAMES:
        return CLOUD_PRIORITY_LIVE_EVENT
    if hard_brake_hits is not None and folder == "RecentClips":
        name = Path(relative_path).name
        if name in hard_brake_hits:
            return CLOUD_PRIORITY_HARSH_BRAKE
        match = _TIMESTAMP_RE.match(name)
        if match is not None and match.group(0) in hard_brake_hits:
            return CLOUD_PRIORITY_HARSH_BRAKE
    return CLOUD_PRIORITY_BULK


def _load_geo_hits(mapping_db_path: Path | None) -> frozenset[str] | None:
    """Return basenames + timestamp prefixes of indexed clips that carry telemetry.

    Reads the Rust-managed mapping DB (``index.sqlite3``) which has the schema::

        clips(id, relative_path, bucket, waypoint_count, gps_waypoint_count, ...)
        waypoints(id, clip_id REFERENCES clips, latitude_deg, longitude_deg, ...)

    A clip with ``waypoint_count > 0`` carries SEI-derived telemetry (gear,
    steering, brake, accel, and — when the vehicle had a GPS fix — lat/lon).
    The cleanup-side keep filter and the cloud RecentClips picker treat the
    presence of any waypoint as "this clip is interesting"; clips without
    SEI/GPS data are continuous-recording filler.

    Returns ``None`` only when the DB is missing or unreadable, so callers
    can distinguish "no telemetry data at all" (empty set) from "couldn't
    open the index" (None disables the telemetry-aware code path entirely).
    """
    if mapping_db_path is None or not mapping_db_path.is_file():
        return None
    try:
        connection = sqlite3.connect(str(mapping_db_path), timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        rows = connection.execute(
            "SELECT relative_path FROM clips WHERE waypoint_count > 0"
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("cloud discovery: could not read mapping clips index: %s", exc)
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


def _discover_recent_telemetry_candidates(
    recent_folder: Path,
    geo_hits: frozenset[str],
    connection: sqlite3.Connection | None,
    hard_brake_hits: frozenset[str] | None = None,
) -> list[EventCandidate]:
    candidates: list[EventCandidate] = []
    if not recent_folder.is_dir():
        return candidates
    for child in sorted(recent_folder.iterdir()):
        if not child.is_file() or child.suffix.lower() not in {".mp4", ".ts"}:
            continue
        name = child.name
        timestamp_match = _TIMESTAMP_RE.match(name)
        timestamp_key = timestamp_match.group(0) if timestamp_match is not None else None
        if name not in geo_hits and (timestamp_key is None or timestamp_key not in geo_hits):
            continue
        relative_path = canonical_cloud_path(f"RecentClips/{name}")
        if _is_path_skipped(connection, relative_path):
            continue
        try:
            size_bytes = child.stat().st_size
        except OSError:
            continue
        candidates.append(
            EventCandidate(
                local_path=child,
                relative_path=relative_path,
                size_bytes=size_bytes,
                score=100,
                priority=_candidate_priority(relative_path, hard_brake_hits),
            )
        )
    return candidates


def _discover_events(
    config: CloudArchiveConfig,
    connection: sqlite3.Connection | None = None,
) -> tuple[EventCandidate, ...]:
    sync_folders = _normalize_folder_list(_read_sync_folders_setting(config, connection))
    priority_order = _normalize_folder_list(_read_priority_order_setting(config, connection))
    geo_hits = _load_geo_hits(config.mapping_db_path)
    hard_brake_hits = _load_hard_brake_hits(config.mapping_db_path)
    candidates: list[EventCandidate] = []
    for folder in sync_folders:
        folder_path = config.teslacam_path / folder
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
                    priority=_candidate_priority(relative_path, hard_brake_hits),
                )
            )
    if (
        "RecentClips" in sync_folders
        and _read_sync_recent_with_telemetry_setting(config, connection)
        and geo_hits is not None
    ):
        candidates.extend(
            _discover_recent_telemetry_candidates(
                config.teslacam_path / "RecentClips",
                geo_hits,
                connection,
                hard_brake_hits,
            )
        )
    if not _read_sync_non_event_setting(config, connection):
        candidates = [
            candidate for candidate in candidates if candidate.score < _SYNC_NON_EVENT_SCORE_LIMIT
        ]
    candidates.sort(
        key=lambda candidate: (
            # Highest priority first (negate so ASC sort puts it on top).
            -candidate.priority,
            _folder_priority_index(
                _folder_of_event_rel(candidate.relative_path),
                priority_order,
            )
            * FOLDER_PRIORITY_MULTIPLIER
            + candidate.score,
        )
    )
    return tuple(candidates)
