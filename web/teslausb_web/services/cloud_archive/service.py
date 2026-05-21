"""Facade service for the cloud archive package split."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

from teslausb_web.services.cloud_archive.pipeline import (
    ShadowTelemetry,
    enqueue_live_event_from_event_json,
    get_cloud_shadow_telemetry,
)
from teslausb_web.services.cloud_archive.queue_ops import (
    clear_queue,
    delete_dead_letter,
    get_sync_queue,
    list_dead_letters,
    queue_event_for_sync,
    remove_from_queue,
    retry_dead_letter,
)
from teslausb_web.services.cloud_archive.settings import (
    CloudArchiveConfig,
    make_cloud_archive_config,
)
from teslausb_web.services.cloud_archive.worker import CloudArchiveWorker, WorkerState
from teslausb_web.services.cloud_archive_migrations import (
    CloudArchiveDBConfig,
    open_db,
    recover_startup_state,
)
from teslausb_web.services.cloud_archive_queries import (
    CloudArchiveQueries,
    CloudArchiveQueriesConfig,
    make_cloud_archive_queries,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator, Sequence

    from teslausb_web.config import WebConfig
    from teslausb_web.services.cloud_archive.worker import SyncStatus
    from teslausb_web.services.cloud_archive_queries import (
        DeadLetterEntry,
        QueueItem,
        SyncHistoryEntry,
        SyncStats,
    )
    from teslausb_web.services.cloud_oauth_service import CloudOAuthService
    from teslausb_web.services.cloud_rclone_service import CloudRcloneService


class CloudArchiveService:
    """Public cloud archive facade used by the Flask app."""

    def __init__(
        self,
        *,
        config: CloudArchiveConfig,
        rclone_service: CloudRcloneService,
        oauth_service: CloudOAuthService,
        queries: CloudArchiveQueries | None = None,
    ) -> None:
        self.config = config
        self.rclone_service = rclone_service
        self.oauth_service = oauth_service
        self.queries = queries or CloudArchiveQueries(
            CloudArchiveQueriesConfig(db_path=config.db_path)
        )
        self.state = WorkerState()
        self.worker = CloudArchiveWorker(self)
        self._monotonic = time.monotonic

    @contextmanager
    def open_db(self) -> Iterator[sqlite3.Connection]:
        with open_db(
            CloudArchiveDBConfig(
                db_path=self.config.db_path,
                mapping_db_path=self.config.mapping_db_path,
            )
        ) as connection:
            yield connection

    def ensure_startup_recovery(self) -> None:
        if self.state.startup_recovery_done:
            return
        with self.open_db() as connection:
            recover_startup_state(connection)
        self.state.startup_recovery_done = True

    def start(self) -> bool:
        return self.worker.start()

    def stop(self, timeout: float = 5.0) -> bool:
        return self.worker.stop(timeout)

    def wake(self) -> None:
        self.worker.wake()

    def start_sync(self, trigger: str = "manual") -> tuple[bool, str]:
        return self.worker.start_sync(trigger)

    def stop_sync(self) -> tuple[bool, str]:
        return self.worker.stop_sync()

    def trigger_auto_sync(self) -> None:
        self.worker.trigger_auto_sync()

    def recover_interrupted_uploads(self) -> int:
        return self.worker.recover_interrupted_uploads()

    def get_sync_status(self) -> SyncStatus:
        return self.worker.get_sync_status()

    def get_sync_history(self, limit: int = 20) -> tuple[SyncHistoryEntry, ...]:
        return self.queries.get_sync_history(limit)

    def get_stats_baseline(self) -> str | None:
        return self.queries.get_stats_baseline()

    def reset_stats_baseline(self) -> tuple[bool, str]:
        return self.queries.reset_stats_baseline()

    def get_sync_stats(self) -> SyncStats:
        return self.queries.get_sync_stats(self.get_sync_status())

    def get_sync_status_for_events(self, event_names: list[str]) -> dict[str, str | None]:
        return self.queries.get_sync_status_for_events(event_names)

    def get_sync_queue(self) -> tuple[QueueItem, ...]:
        return get_sync_queue(self.queries)

    def queue_event_for_sync(
        self,
        folder: str,
        event_name: str,
        *,
        priority: bool = False,
    ) -> tuple[bool, str]:
        return queue_event_for_sync(
            self.config,
            folder,
            event_name,
            priority=priority,
        )

    def remove_from_queue(self, file_path: str) -> tuple[bool, str]:
        return remove_from_queue(self.config, file_path)

    def clear_queue(self) -> tuple[bool, str]:
        return clear_queue(self.config)

    def list_dead_letters(self, limit: int = 100) -> tuple[DeadLetterEntry, ...]:
        return list_dead_letters(self.queries, limit)

    def count_dead_letters(self) -> int:
        return self.queries.count_dead_letters()

    def retry_dead_letter(self, file_path: str | None = None) -> int:
        return retry_dead_letter(self.config, file_path)

    def delete_dead_letter(self, file_path: str | None = None) -> int:
        return delete_dead_letter(self.config, file_path)

    def enqueue_live_event_from_event_json(self, event_json_paths: Sequence[str]) -> int:
        return enqueue_live_event_from_event_json(self.config, self.state, event_json_paths)

    def get_cloud_shadow_telemetry(self) -> ShadowTelemetry:
        return get_cloud_shadow_telemetry(self.state)

    def shutdown(self, timeout: float = 5.0) -> bool:
        return self.stop(timeout)


def make_cloud_archive_service(
    cfg: WebConfig | CloudArchiveConfig,
    rclone_service: CloudRcloneService,
    oauth_service: CloudOAuthService,
) -> CloudArchiveService:
    config = cfg if isinstance(cfg, CloudArchiveConfig) else make_cloud_archive_config(cfg)
    return CloudArchiveService(
        config=config,
        rclone_service=rclone_service,
        oauth_service=oauth_service,
        queries=make_cloud_archive_queries(CloudArchiveQueriesConfig(db_path=config.db_path)),
    )
