from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterator

    from teslausb_web.services.cleanup.service import CleanupConfig


class CleanupHistoryError(RuntimeError):
    """Cleanup history storage failed."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS cleanup_runs (
    run_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    deleted_count INTEGER NOT NULL,
    deleted_bytes INTEGER NOT NULL,
    errors_json TEXT NOT NULL,
    policy_snapshot_json TEXT NOT NULL,
    counts_by_category_json TEXT NOT NULL,
    sample_paths_json TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    current_path TEXT,
    total_candidates INTEGER NOT NULL,
    processed_candidates INTEGER NOT NULL,
    orphan_db_only_json TEXT NOT NULL,
    orphan_fs_only_json TEXT NOT NULL,
    orphan_bytes_total INTEGER NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class StoredRunRecord:
    run_id: str
    action: str
    status: str
    dry_run: bool
    started_at: str
    finished_at: str | None
    deleted_count: int
    deleted_bytes: int
    errors: tuple[str, ...]
    policy_snapshot: dict[str, object]
    counts_by_category: dict[str, int]
    sample_paths: tuple[str, ...]
    generated_at: str
    current_path: str | None
    total_candidates: int
    processed_candidates: int
    orphan_db_only_paths: tuple[str, ...]
    orphan_fs_only_paths: tuple[str, ...]
    orphan_bytes_total: int


@contextmanager
def open_db(config: CleanupConfig) -> Iterator[sqlite3.Connection]:
    db_path = config.history_db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(str(db_path), timeout=10.0)
    except sqlite3.Error as exc:
        raise CleanupHistoryError(f"Failed to open cleanup history database: {exc}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.execute(_SCHEMA)
        yield connection
    except sqlite3.Error as exc:
        connection.rollback()
        raise CleanupHistoryError(f"Cleanup history database error: {exc}") from exc
    finally:
        connection.close()


def start_run_record(  # noqa: PLR0913
    config: CleanupConfig,
    *,
    run_id: str,
    action: str,
    dry_run: bool,
    started_at: str,
    generated_at: str,
    policy_snapshot: dict[str, object],
    counts_by_category: dict[str, int],
    sample_paths: tuple[str, ...],
    total_candidates: int,
    orphan_db_only_paths: tuple[str, ...] = (),
    orphan_fs_only_paths: tuple[str, ...] = (),
    orphan_bytes_total: int = 0,
) -> None:
    with open_db(config) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO cleanup_runs (
                run_id,
                action,
                status,
                dry_run,
                started_at,
                finished_at,
                deleted_count,
                deleted_bytes,
                errors_json,
                policy_snapshot_json,
                counts_by_category_json,
                sample_paths_json,
                generated_at,
                current_path,
                total_candidates,
                processed_candidates,
                orphan_db_only_json,
                orphan_fs_only_json,
                orphan_bytes_total
            ) VALUES (?, ?, 'running', ?, ?, NULL, 0, 0, '[]', ?, ?, ?, ?, NULL, ?, 0, ?, ?, ?)
            """,
            (
                run_id,
                action,
                1 if dry_run else 0,
                started_at,
                _json_dump(policy_snapshot),
                _json_dump(counts_by_category),
                _json_dump(sample_paths),
                generated_at,
                total_candidates,
                _json_dump(orphan_db_only_paths),
                _json_dump(orphan_fs_only_paths),
                orphan_bytes_total,
            ),
        )
        connection.commit()


def finish_run_record(config: CleanupConfig, record: StoredRunRecord) -> None:
    with open_db(config) as connection:
        connection.execute(
            """
            UPDATE cleanup_runs
               SET action = ?,
                   status = ?,
                   dry_run = ?,
                   started_at = ?,
                   finished_at = ?,
                   deleted_count = ?,
                   deleted_bytes = ?,
                   errors_json = ?,
                   policy_snapshot_json = ?,
                   counts_by_category_json = ?,
                   sample_paths_json = ?,
                   generated_at = ?,
                   current_path = ?,
                   total_candidates = ?,
                   processed_candidates = ?,
                   orphan_db_only_json = ?,
                   orphan_fs_only_json = ?,
                   orphan_bytes_total = ?
             WHERE run_id = ?
            """,
            (
                record.action,
                record.status,
                1 if record.dry_run else 0,
                record.started_at,
                record.finished_at,
                record.deleted_count,
                record.deleted_bytes,
                _json_dump(record.errors),
                _json_dump(record.policy_snapshot),
                _json_dump(record.counts_by_category),
                _json_dump(record.sample_paths),
                record.generated_at,
                record.current_path,
                record.total_candidates,
                record.processed_candidates,
                _json_dump(record.orphan_db_only_paths),
                _json_dump(record.orphan_fs_only_paths),
                record.orphan_bytes_total,
                record.run_id,
            ),
        )
        connection.commit()


def load_run_record(config: CleanupConfig, run_id: str) -> StoredRunRecord | None:
    with open_db(config) as connection:
        row = connection.execute(
            "SELECT * FROM cleanup_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    return None if row is None else _row_to_record(row)


def load_recent_run_records(config: CleanupConfig, limit: int) -> tuple[StoredRunRecord, ...]:
    with open_db(config) as connection:
        rows = connection.execute(
            "SELECT * FROM cleanup_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return tuple(_row_to_record(cast("sqlite3.Row", row)) for row in rows)


def _row_to_record(row: sqlite3.Row) -> StoredRunRecord:
    return StoredRunRecord(
        run_id=_required_str(row, "run_id"),
        action=_required_str(row, "action"),
        status=_required_str(row, "status"),
        dry_run=bool(_required_int(row, "dry_run")),
        started_at=_required_str(row, "started_at"),
        finished_at=_optional_str(row, "finished_at"),
        deleted_count=_required_int(row, "deleted_count"),
        deleted_bytes=_required_int(row, "deleted_bytes"),
        errors=_tuple_of_strings(_load_json(row, "errors_json")),
        policy_snapshot=_dict_json(_load_json(row, "policy_snapshot_json")),
        counts_by_category=_dict_of_ints(_load_json(row, "counts_by_category_json")),
        sample_paths=_tuple_of_strings(_load_json(row, "sample_paths_json")),
        generated_at=_required_str(row, "generated_at"),
        current_path=_optional_str(row, "current_path"),
        total_candidates=_required_int(row, "total_candidates"),
        processed_candidates=_required_int(row, "processed_candidates"),
        orphan_db_only_paths=_tuple_of_strings(_load_json(row, "orphan_db_only_json")),
        orphan_fs_only_paths=_tuple_of_strings(_load_json(row, "orphan_fs_only_json")),
        orphan_bytes_total=_required_int(row, "orphan_bytes_total"),
    )


def _load_json(row: sqlite3.Row, key: str) -> object:
    raw = _required_str(row, key)
    return cast("object", json.loads(raw))


def _json_dump(payload: object) -> str:
    return json.dumps(payload, sort_keys=True)


def _required_str(row: sqlite3.Row, key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _optional_str(row: sqlite3.Row, key: str) -> str | None:
    value = row[key]
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"{key} must be a string or null")


def _required_int(row: sqlite3.Row, key: str) -> int:
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{key} must be an integer")
    return value


def _tuple_of_strings(payload: object) -> tuple[str, ...]:
    if not isinstance(payload, list):
        raise TypeError("expected list of strings")
    values: list[str] = []
    for item in payload:
        if not isinstance(item, str):
            raise TypeError("expected list of strings")
        values.append(item)
    return tuple(values)


def _dict_json(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise TypeError("expected object")
    return {str(key): value for key, value in payload.items()}


def _dict_of_ints(payload: object) -> dict[str, int]:
    if not isinstance(payload, dict):
        raise TypeError("expected object")
    result: dict[str, int] = {}
    for key, value in payload.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("expected object of integers")
        result[str(key)] = value
    return result
