"""KV-backed runtime settings round-trip tests for cloud archive."""
# ruff: noqa: FBT003  # test uses literal booleans deliberately

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from teslausb_web.services.cloud_archive.settings import (
    KV_KEY_MAX_RETRY_ATTEMPTS,
    KV_KEY_PRIORITY_FOLDERS,
    KV_KEY_SYNC_FOLDERS,
    KV_KEY_SYNC_NON_EVENT,
    KV_KEY_SYNC_RECENT_WITH_TELEMETRY,
    CloudArchiveConfig,
    _read_priority_order_setting,
    _read_retry_max_attempts_setting,
    _read_sync_folders_setting,
    _read_sync_non_event_setting,
    _read_sync_recent_with_telemetry_setting,
    _write_setting,
)


def _make_config(tmp_path: Path) -> CloudArchiveConfig:
    return CloudArchiveConfig(
        enabled=True,
        db_path=tmp_path / "cloud.db",
        teslacam_path=tmp_path / "TeslaCam",
        mapping_db_path=tmp_path / "mapping.db",
        sync_folders=("SentryClips", "SavedClips"),
        priority_folders=("SentryClips", "SavedClips"),
        sync_non_event=False,
        sync_recent_with_telemetry=False,
        max_retry_attempts=5,
    )


def _make_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE cloud_archive_meta (key TEXT PRIMARY KEY, value TEXT)"
    )
    return connection


def test_kv_overrides_take_precedence_over_config_defaults(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    connection = _make_connection()
    try:
        _write_setting(connection, KV_KEY_SYNC_FOLDERS, ["RecentClips", "SavedClips"])
        _write_setting(connection, KV_KEY_PRIORITY_FOLDERS, ["SavedClips"])
        _write_setting(connection, KV_KEY_SYNC_NON_EVENT, True)
        _write_setting(connection, KV_KEY_MAX_RETRY_ATTEMPTS, 12)
        _write_setting(connection, KV_KEY_SYNC_RECENT_WITH_TELEMETRY, True)
        connection.commit()

        assert _read_sync_folders_setting(config, connection) == (
            "RecentClips",
            "SavedClips",
        )
        assert _read_priority_order_setting(config, connection) == (
            "SavedClips",
            "SentryClips",
            "RecentClips",
        )
        assert _read_sync_non_event_setting(config, connection) is True
        assert _read_retry_max_attempts_setting(config, connection) == 12
        assert _read_sync_recent_with_telemetry_setting(config, connection) is True
    finally:
        connection.close()


def test_kv_missing_falls_back_to_config_defaults(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    connection = _make_connection()
    try:
        assert _read_sync_folders_setting(config, connection) == config.sync_folders
        assert _read_priority_order_setting(config, connection) == (
            "SentryClips",
            "SavedClips",
            "RecentClips",
        )
        assert _read_sync_non_event_setting(config, connection) is False
        assert _read_retry_max_attempts_setting(config, connection) == 5
        assert _read_sync_recent_with_telemetry_setting(config, connection) is False
    finally:
        connection.close()


def test_invalid_json_in_kv_falls_back_to_config(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    connection = _make_connection()
    try:
        connection.execute(
            "INSERT INTO cloud_archive_meta (key, value) VALUES (?, ?)",
            (KV_KEY_SYNC_FOLDERS, "not-valid-json"),
        )
        connection.execute(
            "INSERT INTO cloud_archive_meta (key, value) VALUES (?, ?)",
            (KV_KEY_MAX_RETRY_ATTEMPTS, "not-a-number"),
        )
        connection.commit()
        assert _read_sync_folders_setting(config, connection) == config.sync_folders
        assert (
            _read_retry_max_attempts_setting(config, connection)
            == config.max_retry_attempts
        )
    finally:
        connection.close()


def test_out_of_range_retry_attempts_falls_back(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    connection = _make_connection()
    try:
        _write_setting(connection, KV_KEY_MAX_RETRY_ATTEMPTS, 999)
        connection.commit()
        assert (
            _read_retry_max_attempts_setting(config, connection)
            == config.max_retry_attempts
        )
    finally:
        connection.close()
