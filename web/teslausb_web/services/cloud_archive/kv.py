"""Key/value helpers for cloud archive state."""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

KV_KEY_STATS_BASELINE_AT = "cloud_archive.stats_baseline_at"
KV_KEY_LAST_SUCCESSFUL_SYNC = "cloud_archive.last_successful_sync"
KV_KEY_LAST_SYNC_ERROR = "cloud_archive.last_sync_error"


def kv_get(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute(
        "SELECT value FROM cloud_archive_meta WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    value = row[0]
    return value if isinstance(value, str) else None


def kv_set(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO cloud_archive_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def kv_delete(connection: sqlite3.Connection, key: str) -> None:
    try:
        connection.execute(
            "DELETE FROM cloud_archive_meta WHERE key = ?",
            (key,),
        )
    except sqlite3.Error as exc:
        logger.debug("Failed to delete cloud archive KV key %s: %s", key, exc)
