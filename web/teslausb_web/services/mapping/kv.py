from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)


def _kv_get(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM kv_meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    value = row[0]
    return value if isinstance(value, str) else None


def _kv_set(connection: sqlite3.Connection, key: str, value: str) -> None:
    try:
        connection.execute(
            "INSERT INTO kv_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        connection.commit()
    except sqlite3.Error as exc:
        logger.warning("kv_meta upsert failed for %r: %s", key, exc)
