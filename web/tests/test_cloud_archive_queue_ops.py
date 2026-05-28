from __future__ import annotations

from pathlib import Path

from teslausb_web.services.cloud_archive.queue_ops import (
    clear_queue,
    queue_event_for_sync,
    recover_interrupted_uploads,
    remove_from_queue,
    retry_dead_letter,
)
from teslausb_web.services.cloud_archive.settings import CloudArchiveConfig
from teslausb_web.services.cloud_archive_migrations import open_db
from teslausb_web.services.cloud_archive_queries import (
    CloudArchiveQueries,
    CloudArchiveQueriesConfig,
)


def _make_config(tmp_path: Path) -> CloudArchiveConfig:
    teslacam = tmp_path / "TeslaCam"
    teslacam.mkdir(exist_ok=True)
    return CloudArchiveConfig(
        enabled=True,
        db_path=tmp_path / "cloud.db",
        teslacam_path=teslacam,
        mapping_db_path=tmp_path / "mapping.db",
    )


def _make_event(config: CloudArchiveConfig, relative_path: str) -> None:
    path = config.teslacam_path / Path(relative_path)
    path.mkdir(parents=True, exist_ok=True)
    (path / "clip.mp4").write_bytes(b"video")


def test_queue_event_for_sync_adds_row(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    with open_db(config.db_path):
        pass
    _make_event(config, "SentryClips/2026-01-01_10-00-00")

    ok, message = queue_event_for_sync(config, "SentryClips", "2026-01-01_10-00-00")

    assert ok is True
    assert "Added 1 file" in message


def test_remove_and_clear_queue_skip_synced_rows(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    with open_db(config.db_path) as connection:
        connection.execute(
            "INSERT INTO cloud_synced_files (file_path, status, retry_count) "
            "VALUES ('SentryClips/remove-me', 'queued', 0)"
        )
        connection.execute(
            "INSERT INTO cloud_synced_files (file_path, status, retry_count) "
            "VALUES ('SentryClips/done', 'synced', 0)"
        )
        connection.commit()

    ok, _message = remove_from_queue(config, "SentryClips/remove-me")
    clear_ok, clear_message = clear_queue(config)

    queries = CloudArchiveQueries(CloudArchiveQueriesConfig(db_path=config.db_path))
    assert ok is True
    assert clear_ok is True
    assert clear_message == "Cleared 0 items from queue"
    assert queries.get_sync_queue() == ()


def test_retry_dead_letter_and_recover_interrupted_uploads(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    with open_db(config.db_path) as connection:
        connection.execute(
            "INSERT INTO cloud_synced_files (file_path, status, retry_count) "
            "VALUES ('SentryClips/dl', 'dead_letter', 5)"
        )
        connection.execute(
            "INSERT INTO cloud_synced_files (file_path, status, retry_count) "
            "VALUES ('SentryClips/up', 'uploading', 0)"
        )
        connection.commit()

    retried = retry_dead_letter(config, "SentryClips/dl")
    recovered = recover_interrupted_uploads(config)

    assert retried == 1
    assert recovered == 1

def test_get_sync_queue_orders_priority_before_bulk(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    with open_db(config.db_path) as connection:
        connection.execute(
            "INSERT INTO cloud_synced_files (file_path, file_size, status, "
            "retry_count, last_error, added_at, priority) "
            "VALUES ('RecentClips/bulk-1.mp4', 100, 'pending', 0, NULL, '2026-01-01T00:00:00Z', 0)"
        )
        connection.execute(
            "INSERT INTO cloud_synced_files (file_path, file_size, status, "
            "retry_count, last_error, added_at, priority) "
            "VALUES ('SentryClips/event-1.mp4', 200, 'pending', 0, NULL, '2026-01-01T01:00:00Z', 10)"
        )
        connection.execute(
            "INSERT INTO cloud_synced_files (file_path, file_size, status, "
            "retry_count, last_error, added_at, priority) "
            "VALUES ('RecentClips/bulk-2.mp4', 100, 'pending', 0, NULL, '2026-01-01T02:00:00Z', 0)"
        )
        connection.commit()
    queries = CloudArchiveQueries(CloudArchiveQueriesConfig(db_path=config.db_path))
    items = queries.get_sync_queue()
    paths = [item.file_path for item in items]
    assert paths[0] == "SentryClips/event-1.mp4"
    assert items[0].priority == 10
    assert items[1].priority == 0
    assert items[2].priority == 0


