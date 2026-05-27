"""Cloud-side retention sweeper.

Reads persisted KV settings (cloud_auto_cleanup, cloud_reserve_gb,
cloud_min_retention_days) and, when free space on the remote is below
the configured reserve, deletes the oldest objects above the
min-retention age until the reserve is met.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from teslausb_web.services.cloud_rclone_service import RcloneError

from .settings import (
    _read_cloud_auto_cleanup_setting,
    _read_cloud_min_retention_days_setting,
    _read_cloud_reserve_gb_setting,
)

if TYPE_CHECKING:
    from .service import CloudArchiveService

logger = logging.getLogger(__name__)

_MAX_DELETIONS_PER_RUN = 200
_BYTES_PER_GB = 1024 * 1024 * 1024


@dataclass(frozen=True)
class CloudCleanupResult:
    triggered: bool
    deleted_count: int
    bytes_freed: int
    reason: str


def _parse_modtime(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def run_cloud_cleanup(service: "CloudArchiveService") -> CloudCleanupResult:
    """Free space on the remote when below ``cloud_reserve_gb``.

    No-op if cloud_auto_cleanup is disabled, the remote has no free-space
    info, or the reserve is already satisfied.
    """

    with service.open_db() as connection:
        try:
            auto = _read_cloud_auto_cleanup_setting(service.config, connection)
            reserve_gb = _read_cloud_reserve_gb_setting(service.config, connection)
            min_age_days = _read_cloud_min_retention_days_setting(
                service.config, connection
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("cloud cleanup settings read failed: %s", exc)
            return CloudCleanupResult(False, 0, 0, f"settings_error: {exc}")

    if not auto:
        return CloudCleanupResult(False, 0, 0, "auto_cleanup_disabled")

    rclone = service.rclone_service
    try:
        stats = rclone.get_stats("")
    except RcloneError as exc:
        logger.info("cloud cleanup skipped — get_stats failed: %s", exc)
        return CloudCleanupResult(False, 0, 0, f"stats_error: {exc}")

    reserve_bytes = int(reserve_gb * _BYTES_PER_GB)
    free_bytes = int(stats.free_bytes or 0)
    if reserve_bytes <= 0:
        return CloudCleanupResult(False, 0, 0, "reserve_disabled")
    if free_bytes >= reserve_bytes:
        return CloudCleanupResult(False, 0, 0, "reserve_satisfied")

    cutoff = datetime.now(timezone.utc) - timedelta(days=int(min_age_days))

    try:
        entries = rclone.list_files_recursive("")
    except RcloneError as exc:
        logger.info("cloud cleanup skipped — list_files_recursive failed: %s", exc)
        return CloudCleanupResult(False, 0, 0, f"list_error: {exc}")

    candidates: list[tuple[datetime, int, str]] = []
    for entry in entries:
        modtime = _parse_modtime(entry.modified_at)
        if modtime is None:
            continue
        if modtime > cutoff:
            continue
        candidates.append((modtime, int(entry.size_bytes or 0), entry.path))

    candidates.sort(key=lambda item: item[0])

    deleted = 0
    bytes_freed = 0
    target = reserve_bytes - free_bytes
    for _modtime, size, path in candidates:
        if deleted >= _MAX_DELETIONS_PER_RUN:
            break
        if bytes_freed >= target:
            break
        try:
            rclone.deletefile(path)
        except RcloneError as exc:
            logger.warning("cloud cleanup deletefile failed for %s: %s", path, exc)
            continue
        deleted += 1
        bytes_freed += size
        logger.info("cloud cleanup deleted %s (%d bytes)", path, size)

    return CloudCleanupResult(
        triggered=True,
        deleted_count=deleted,
        bytes_freed=bytes_freed,
        reason="ok",
    )


__all__ = ("CloudCleanupResult", "run_cloud_cleanup")
