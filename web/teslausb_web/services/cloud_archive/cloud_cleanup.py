"""Cloud-side retention sweeper.

Reads persisted KV settings (``cloud_auto_cleanup`` and
``cloud_reserve_gb``) and, when free space on the remote is below the
configured reserve, deletes the oldest objects until the reserve is
met.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from teslausb_web.services.cloud_rclone_service import RcloneError

from .settings import (
    _read_cloud_auto_cleanup_setting,
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


@dataclass(frozen=True)
class ReserveGuardResult:
    """Outcome of a pre-flight reserve check.

    ``ok`` is True when uploading ``needed_bytes`` would leave at least
    ``cloud_reserve_gb`` of free space on the remote (possibly after a
    cleanup pass). ``free_bytes`` is ``None`` when the backend doesn't
    report free space (e.g., some S3 endpoints); in that case the guard
    fails open and the caller should proceed without enforcement.
    """

    ok: bool
    free_bytes: int | None
    reserve_bytes: int
    needed_bytes: int
    cleanup_deleted: int
    cleanup_bytes_freed: int
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


def ensure_remote_headroom(
    service: "CloudArchiveService", needed_bytes: int
) -> ReserveGuardResult:
    """Pre-flight check that uploading ``needed_bytes`` won't breach the
    configured cloud reserve.

    Probes the remote's free space; if uploading ``needed_bytes`` would
    leave less than ``cloud_reserve_gb`` free and ``cloud_auto_cleanup``
    is enabled, runs a cleanup pass and re-probes. Fails open when the
    backend doesn't report free space (no way to enforce the reserve).
    """

    with service.open_db() as connection:
        try:
            reserve_gb = _read_cloud_reserve_gb_setting(service.config, connection)
            auto = _read_cloud_auto_cleanup_setting(service.config, connection)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("reserve guard settings read failed: %s", exc)
            return ReserveGuardResult(True, None, 0, 0, 0, 0, f"settings_error: {exc}")

    reserve_bytes = int(reserve_gb * _BYTES_PER_GB)
    needed = max(0, int(needed_bytes))

    if reserve_bytes <= 0:
        return ReserveGuardResult(True, None, 0, needed, 0, 0, "reserve_disabled")

    rclone = service.rclone_service
    try:
        stats = rclone.get_stats("")
    except RcloneError as exc:
        logger.info("reserve guard: get_stats failed (%s) — allowing", exc)
        return ReserveGuardResult(True, None, reserve_bytes, needed, 0, 0, "stats_error")

    if stats.free_bytes is None:
        # Backend doesn't expose free space — can't enforce, fail open.
        return ReserveGuardResult(True, None, reserve_bytes, needed, 0, 0, "free_unknown")

    free = int(stats.free_bytes)
    if free - needed >= reserve_bytes:
        return ReserveGuardResult(True, free, reserve_bytes, needed, 0, 0, "ok")

    if not auto:
        return ReserveGuardResult(
            False, free, reserve_bytes, needed, 0, 0, "insufficient_no_cleanup"
        )

    cleanup = run_cloud_cleanup(service)

    try:
        stats2 = rclone.get_stats("")
    except RcloneError as exc:
        logger.info("reserve guard: re-probe failed (%s) — allowing", exc)
        return ReserveGuardResult(
            True,
            None,
            reserve_bytes,
            needed,
            cleanup.deleted_count,
            cleanup.bytes_freed,
            "stats_error_after_cleanup",
        )

    if stats2.free_bytes is None:
        return ReserveGuardResult(
            True,
            None,
            reserve_bytes,
            needed,
            cleanup.deleted_count,
            cleanup.bytes_freed,
            "free_unknown_after_cleanup",
        )

    free2 = int(stats2.free_bytes)
    ok = free2 - needed >= reserve_bytes
    return ReserveGuardResult(
        ok,
        free2,
        reserve_bytes,
        needed,
        cleanup.deleted_count,
        cleanup.bytes_freed,
        "ok_after_cleanup" if ok else "still_insufficient_after_cleanup",
    )


__all__ = ("CloudCleanupResult", "ReserveGuardResult", "ensure_remote_headroom", "run_cloud_cleanup")
