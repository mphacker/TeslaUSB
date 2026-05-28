"""Facade service for the cloud archive package split."""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

from teslausb_web.services.cloud_archive.paths import (
    VALID_SYNC_FOLDERS,
    _normalize_folder_list,
)
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
    BWLIMIT_KBPS_MAX,
    BWLIMIT_KBPS_MIN,
    CLOUD_RESERVE_GB_MAX,
    CLOUD_RESERVE_GB_MIN,
    KV_KEY_AUTO_SYNC_ENABLED,
    KV_KEY_BWLIMIT_KBPS,
    KV_KEY_CLOUD_AUTO_CLEANUP,
    KV_KEY_CLOUD_RESERVE_GB,
    KV_KEY_KEEP_CLIPS_UNTIL_SYNCED,
    KV_KEY_MAX_RETRY_ATTEMPTS,
    KV_KEY_PRIORITY_FOLDERS,
    KV_KEY_REMOTE_PATH,
    KV_KEY_SYNC_FOLDERS,
    KV_KEY_SYNC_NON_EVENT,
    KV_KEY_SYNC_RECENT_WITH_TELEMETRY,
    RETRY_MAX_ATTEMPTS_MAX,
    RETRY_MAX_ATTEMPTS_MIN,
    CloudArchiveConfig,
    CloudArchiveConfigError,
    _read_auto_sync_enabled_setting,
    _read_bwlimit_kbps_setting,
    _read_remote_path_setting,
    _write_setting,
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

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
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
        # Live overrides that update_settings can change without a restart.
        self._auto_sync_enabled_override: bool | None = None

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
            self._apply_persisted_bwlimit(connection)
            self._apply_persisted_auto_sync_enabled(connection)
            self._apply_persisted_remote_path(connection)
        self.state.startup_recovery_done = True

    def _apply_persisted_bwlimit(self, connection: sqlite3.Connection) -> None:
        setter = getattr(self.rclone_service, "set_bwlimit_kbps_override", None)
        if not callable(setter):
            return
        try:
            value = _read_bwlimit_kbps_setting(self.config, connection)
        except (sqlite3.Error, ValueError, TypeError):  # pragma: no cover - defensive
            return
        setter(int(value))

    def _apply_persisted_auto_sync_enabled(self, connection: sqlite3.Connection) -> None:
        try:
            value = _read_auto_sync_enabled_setting(self.config, connection)
        except (sqlite3.Error, ValueError, TypeError):  # pragma: no cover - defensive
            return
        self._auto_sync_enabled_override = bool(value)

    def _apply_persisted_remote_path(self, connection: sqlite3.Connection) -> None:
        setter = getattr(self.rclone_service, "set_remote_path_override", None)
        if not callable(setter):
            return
        try:
            value = _read_remote_path_setting(self.config, connection)
        except (sqlite3.Error, ValueError, TypeError):  # pragma: no cover - defensive
            return
        setter(value or None)

    def is_auto_sync_enabled(self) -> bool:
        """Live-evaluated auto-sync flag, honoring KV override set via the UI."""
        if self._auto_sync_enabled_override is not None:
            return self._auto_sync_enabled_override
        return self.config.enabled

    def start(self) -> bool:
        self._restore_persisted_settings()
        return self.worker.start()

    def _restore_persisted_settings(self) -> None:
        """Re-apply persisted KV settings (remote_path, bwlimit, auto-sync) to the running process.

        Called once per service startup so a restart of teslausb-web doesn't
        silently revert the user's last settings to defaults. Distinct from
        ``ensure_startup_recovery`` which is gated on ``startup_recovery_done``
        and also runs the worker's interrupted-upload recovery.
        """
        try:
            with self.open_db() as connection:
                self._apply_persisted_bwlimit(connection)
                self._apply_persisted_auto_sync_enabled(connection)
                self._apply_persisted_remote_path(connection)
        except sqlite3.Error:  # pragma: no cover - defensive, never block worker start
            logger.exception("Failed to restore persisted cloud archive settings")

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

    def update_settings(
        self,
        *,
        sync_folders: tuple[str, ...] | None = None,
        priority_folders: tuple[str, ...] | None = None,
        sync_non_event: bool | None = None,
        max_retry_attempts: int | None = None,
        sync_recent_with_telemetry: bool | None = None,
        bwlimit_kbps: int | None = None,
        cloud_reserve_gb: float | None = None,
        cloud_auto_cleanup: bool | None = None,
        keep_clips_until_synced: bool | None = None,
        enabled: bool | None = None,
        remote_path: str | None = None,
    ) -> None:
        """Persist runtime overrides for user-tunable cloud archive settings."""

        updates: list[tuple[str, object]] = []
        if sync_folders is not None:
            normalized = _normalize_folder_list(sync_folders)
            updates.append((KV_KEY_SYNC_FOLDERS, list(normalized)))
        if priority_folders is not None:
            normalized = _normalize_folder_list(priority_folders)
            for folder in normalized:
                if folder not in VALID_SYNC_FOLDERS:
                    raise CloudArchiveConfigError(
                        f"unknown folder in priority list: {folder!r}"
                    )
            updates.append((KV_KEY_PRIORITY_FOLDERS, list(normalized)))
        if sync_non_event is not None:
            updates.append((KV_KEY_SYNC_NON_EVENT, bool(sync_non_event)))
        if max_retry_attempts is not None:
            if not (
                RETRY_MAX_ATTEMPTS_MIN
                <= int(max_retry_attempts)
                <= RETRY_MAX_ATTEMPTS_MAX
            ):
                raise CloudArchiveConfigError(
                    f"max_retry_attempts must be within "
                    f"{RETRY_MAX_ATTEMPTS_MIN}..{RETRY_MAX_ATTEMPTS_MAX}"
                )
            updates.append((KV_KEY_MAX_RETRY_ATTEMPTS, int(max_retry_attempts)))
        if sync_recent_with_telemetry is not None:
            updates.append(
                (KV_KEY_SYNC_RECENT_WITH_TELEMETRY, bool(sync_recent_with_telemetry))
            )
        if bwlimit_kbps is not None:
            if not BWLIMIT_KBPS_MIN <= int(bwlimit_kbps) <= BWLIMIT_KBPS_MAX:
                raise CloudArchiveConfigError(
                    f"bwlimit_kbps must be within "
                    f"{BWLIMIT_KBPS_MIN}..{BWLIMIT_KBPS_MAX}"
                )
            updates.append((KV_KEY_BWLIMIT_KBPS, int(bwlimit_kbps)))
        if cloud_reserve_gb is not None:
            if not (
                CLOUD_RESERVE_GB_MIN
                <= float(cloud_reserve_gb)
                <= CLOUD_RESERVE_GB_MAX
            ):
                raise CloudArchiveConfigError(
                    f"cloud_reserve_gb must be within "
                    f"{CLOUD_RESERVE_GB_MIN}..{CLOUD_RESERVE_GB_MAX}"
                )
            updates.append((KV_KEY_CLOUD_RESERVE_GB, float(cloud_reserve_gb)))
        if cloud_auto_cleanup is not None:
            updates.append((KV_KEY_CLOUD_AUTO_CLEANUP, bool(cloud_auto_cleanup)))
        if keep_clips_until_synced is not None:
            updates.append(
                (KV_KEY_KEEP_CLIPS_UNTIL_SYNCED, bool(keep_clips_until_synced))
            )
        if enabled is not None:
            updates.append((KV_KEY_AUTO_SYNC_ENABLED, bool(enabled)))
        normalized_remote_path: str | None = None
        if remote_path is not None:
            normalized_remote_path = (
                str(remote_path).strip().replace("\\", "/").strip("/")
            )
            updates.append((KV_KEY_REMOTE_PATH, normalized_remote_path))
        if not updates:
            return
        with self.open_db() as connection:
            for key, value in updates:
                _write_setting(connection, key, value)
            connection.commit()
        if bwlimit_kbps is not None:
            setter = getattr(self.rclone_service, "set_bwlimit_kbps_override", None)
            if callable(setter):
                setter(int(bwlimit_kbps))
        if enabled is not None:
            self._auto_sync_enabled_override = bool(enabled)
        if normalized_remote_path is not None:
            setter = getattr(self.rclone_service, "set_remote_path_override", None)
            if callable(setter):
                setter(normalized_remote_path or None)

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
