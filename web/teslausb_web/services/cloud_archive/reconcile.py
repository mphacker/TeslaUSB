"""Remote reconciliation and mirror-tracking helpers for cloud archive."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from teslausb_web.services.cloud_archive.paths import (
    EVENT_FOLDER_NAMES,
    KNOWN_CLOUD_ROOTS,
    canonical_cloud_path,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from teslausb_web.services.cloud_rclone_service import CloudRcloneService


@dataclass(frozen=True, slots=True)
class MirrorRecord:
    legacy_id: int | None
    source_path: str
    stage: str
    status: str


@dataclass(frozen=True, slots=True)
class ReconcileSummary:
    reconciled: int
    inserted: int
    updated: int


@contextmanager
def _open_mapping_db(mapping_db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(str(mapping_db_path), timeout=10.0)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _pipeline_table_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pipeline_queue'"
    ).fetchone()
    return row is not None


def _query_existing_mirrors(
    mapping_db_path: Path,
    legacy_ids: list[int],
    source_paths: list[str] | None = None,
) -> tuple[set[int], set[str]]:
    if not legacy_ids and not source_paths:
        return set(), set()
    legacy_id_set = set(legacy_ids)
    source_path_set = set(source_paths or ())
    with _open_mapping_db(mapping_db_path) as connection:
        rows = connection.execute(
            "SELECT legacy_id, source_path FROM pipeline_queue "
            "WHERE legacy_table = 'live_event_queue'"
        ).fetchall()
    existing_ids = {
        int(row[0]) for row in rows if row[0] is not None and int(row[0]) in legacy_id_set
    }
    existing_paths = {str(row[1]) for row in rows if row[1] and str(row[1]) in source_path_set}
    return existing_ids, existing_paths


def _refresh_stale_mirrors_to_done(mapping_db_path: Path, stale_updates: list[int]) -> int:
    if not stale_updates:
        return 0
    with _open_mapping_db(mapping_db_path) as connection:
        changed = 0
        for legacy_id in stale_updates:
            changed += connection.execute(
                (
                    "UPDATE pipeline_queue SET stage = 'cloud_done', status = 'done', "
                    "completed_at = ?, claimed_by = NULL, claimed_at = NULL "
                    "WHERE legacy_table = 'live_event_queue' AND legacy_id = ? "
                    "AND stage <> 'cloud_done'"
                ),
                (datetime.now(UTC).timestamp(), legacy_id),
            ).rowcount
        connection.commit()
    return changed


def _backfill_missing_live_event_mirrors(
    connection: sqlite3.Connection,
    mapping_db_path: Path,
) -> int:
    if not mapping_db_path.exists():
        raise RuntimeError("mapping database is unavailable")
    rows = connection.execute(
        "SELECT id, event_dir, event_json_path, event_timestamp, event_reason, "
        "upload_scope, status FROM live_event_queue"
    ).fetchall()
    if not rows:
        return 0
    legacy_ids = [int(row[0]) for row in rows]
    source_paths = [str(row[2]) for row in rows if row[2]]
    existing_ids, existing_paths = _query_existing_mirrors(
        mapping_db_path, legacy_ids, source_paths
    )
    stale_updates = [
        int(row[0]) for row in rows if int(row[0]) in existing_ids and str(row[6]) == "uploaded"
    ]
    _refresh_stale_mirrors_to_done(mapping_db_path, stale_updates)
    inserted = 0
    with _open_mapping_db(mapping_db_path) as pipeline_db:
        if not _pipeline_table_exists(pipeline_db):
            raise RuntimeError("pipeline_queue table is unavailable")
        for row in rows:
            legacy_id = int(row[0])
            source_raw = row[2]
            if source_raw is None:
                continue
            source_path = str(source_raw)
            if legacy_id in existing_ids or source_path in existing_paths:
                continue
            legacy_status = str(row[6])
            stage = "cloud_done" if legacy_status == "uploaded" else "cloud_pending"
            status = "done" if legacy_status == "uploaded" else "pending"
            payload = json.dumps(
                {
                    "event_dir": row[1],
                    "event_timestamp": row[3],
                    "event_reason": row[4],
                    "upload_scope": row[5],
                },
                sort_keys=True,
            )
            inserted += pipeline_db.execute(
                (
                    "INSERT INTO pipeline_queue ("
                    "source_path, stage, status, priority, attempts, enqueued_at, "
                    "payload_json, legacy_id, legacy_table"
                    ") VALUES (?, ?, ?, 0, 0, ?, ?, ?, 'live_event_queue') "
                    "ON CONFLICT(source_path, stage, legacy_table) DO NOTHING"
                ),
                (
                    source_path,
                    stage,
                    status,
                    datetime.now(UTC).timestamp(),
                    payload,
                    legacy_id,
                ),
            ).rowcount
        pipeline_db.commit()
    after_ids, after_paths = _query_existing_mirrors(mapping_db_path, legacy_ids, source_paths)
    missing = [legacy_id for legacy_id in legacy_ids if legacy_id not in after_ids]
    if missing and not after_paths.issuperset(source_paths):
        raise RuntimeError(f"mirror backfill incomplete for ids={missing}")
    return inserted


def _list_remote_tree(rclone_service: CloudRcloneService) -> dict[str, set[str]]:
    tree: dict[str, set[str]] = {folder: set() for folder in KNOWN_CLOUD_ROOTS}
    for folder in EVENT_FOLDER_NAMES:
        listing = rclone_service.list_directory(folder)
        tree[folder] = {
            f"{entry.name.rstrip('/')}/" if entry.is_dir else entry.name.rstrip("/")
            for entry in listing.entries
        }
    archived_listing = rclone_service.list_files("ArchivedClips")
    tree["ArchivedClips"] = {entry.name.rstrip("/") for entry in archived_listing.entries}
    return tree


def _reconcile_with_remote(
    connection: sqlite3.Connection,
    rclone_service: CloudRcloneService,
) -> ReconcileSummary:
    tree = _list_remote_tree(rclone_service)
    now = datetime.now(UTC).isoformat()
    inserted = 0
    updated = 0
    for folder in EVENT_FOLDER_NAMES:
        for entry in tree.get(folder, set()):
            relative_path = canonical_cloud_path(f"{folder}/{entry.rstrip('/')}")
            rowcount = connection.execute(
                (
                    "UPDATE cloud_synced_files SET status = 'synced', synced_at = ?, "
                    "remote_path = ?, last_error = NULL WHERE file_path = ? "
                    "AND status IN ('pending', 'queued', 'failed', 'uploading')"
                ),
                (now, relative_path, relative_path),
            ).rowcount
            if rowcount > 0:
                updated += rowcount
                continue
            existing = connection.execute(
                "SELECT 1 FROM cloud_synced_files WHERE file_path = ?",
                (relative_path,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    (
                        "INSERT INTO cloud_synced_files ("
                        "file_path, status, synced_at, remote_path"
                        ") VALUES (?, 'synced', ?, ?)"
                    ),
                    (relative_path, now, relative_path),
                )
                inserted += 1
    for file_name in tree.get("ArchivedClips", set()):
        relative_path = canonical_cloud_path(f"ArchivedClips/{file_name}")
        rowcount = connection.execute(
            (
                "UPDATE cloud_synced_files SET status = 'synced', synced_at = ?, "
                "remote_path = ?, last_error = NULL WHERE file_path = ? "
                "AND status IN ('pending', 'queued', 'failed', 'uploading')"
            ),
            (now, relative_path, relative_path),
        ).rowcount
        if rowcount > 0:
            updated += rowcount
            continue
        existing = connection.execute(
            "SELECT 1 FROM cloud_synced_files WHERE file_path = ?",
            (relative_path,),
        ).fetchone()
        if existing is None:
            connection.execute(
                (
                    "INSERT INTO cloud_synced_files (file_path, status, synced_at, remote_path) "
                    "VALUES (?, 'synced', ?, ?)"
                ),
                (relative_path, now, relative_path),
            )
            inserted += 1
    connection.commit()
    return ReconcileSummary(reconciled=inserted + updated, inserted=inserted, updated=updated)


def _reconcile_with_remote_legacy(
    connection: sqlite3.Connection,
    rclone_service: CloudRcloneService,
) -> ReconcileSummary:
    return _reconcile_with_remote(connection, rclone_service)
