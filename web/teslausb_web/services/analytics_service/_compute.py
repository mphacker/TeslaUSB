"""Pure computation helpers for the analytics service.

Disk probing, folder classification, and storage-health verdicts live
here so the ``AnalyticsService`` facade in ``__init__`` stays slim.
"""

from __future__ import annotations

import logging
import shutil
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from teslausb_web.services.analytics_service._models import (
    CLIPS_PER_HOUR,
    FOLDER_DESCRIPTIONS,
    FOLDER_OTHER,
    FOLDER_PRIORITY,
    FOLDER_RECENT,
    FOLDER_SAVED,
    FOLDER_SENTRY,
    HIGH_CONFIDENCE_VIDEO_COUNT,
    PERCENT,
    STATUS_CAUTION,
    STATUS_CRITICAL,
    STATUS_HEALTHY,
    STATUS_RANK,
    STATUS_WARNING,
    FolderBreakdown,
    PartitionUsage,
    RecordingEstimate,
    StorageHealth,
    VideoStatistics,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from teslausb_web.config import AnalyticsSection

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Probe:
    """One filesystem root to report on, with its display label.

    When ``capacity_bytes`` is set, the reported ``total_bytes`` is the
    fixed LUN capacity (the size Tesla sees), and ``used_bytes`` is
    computed by walking the backing subtree. When ``None`` (legacy /
    dev-machine fallback) ``shutil.disk_usage`` is used and the totals
    reflect the underlying filesystem instead of any LUN cap.
    """

    key: str
    label: str
    path: Path
    capacity_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class IndexedFileRow:
    file_path: str
    file_size: int
    file_mtime: float | None


@dataclass(slots=True)
class _FolderAccumulator:
    """Mutable scratch bucket — folded into a frozen FolderBreakdown."""

    count: int = 0
    size_bytes: int = 0
    oldest: float | None = None
    newest: float | None = None
    clip_stems: set[str] = field(default_factory=set)


def device_id(path: Path) -> object | None:
    """``st_dev`` for ``path`` if it exists; ``None`` otherwise."""
    try:
        return path.stat().st_dev
    except OSError:
        return None


def _subtree_used_bytes(root: Path) -> int:
    """Sum every regular file's size under ``root``. Best-effort: any
    unreadable entry is skipped silently — partial figures are still
    more useful than no figure at all on a busy device.
    """
    total = 0
    try:
        for entry in _iter_files(root):
            with suppress(OSError):
                total += entry.stat(follow_symlinks=False).st_size
    except OSError as exc:
        logger.warning("analytics: subtree walk failed at %s: %s", root, exc)
    return total


def _iter_files(root: Path):  # noqa: ANN202 — internal iterator
    import os

    for dirpath, _dirnames, filenames in os.walk(str(root), followlinks=False):
        for name in filenames:
            yield Path(dirpath, name)


def probe_usage(probe: Probe) -> PartitionUsage:
    if probe.capacity_bytes is not None:
        # LUN-mode: total is the LUN cap; used is what's actually on the
        # backing subtree. Free is whatever's left in the cap.
        total = probe.capacity_bytes
        try:
            used = _subtree_used_bytes(probe.path)
        except OSError as exc:
            logger.warning("analytics: subtree walk(%s) failed: %s", probe.path, exc)
            return PartitionUsage(
                key=probe.key,
                label=probe.label,
                path=str(probe.path),
                total_bytes=total,
                used_bytes=0,
                free_bytes=total,
                percent_used=0.0,
                error=str(exc),
            )
        # Cap used at total so a runaway tree doesn't render as 200% full.
        used = min(used, total)
        free = max(0, total - used)
        percent = (used / total * PERCENT) if total > 0 else 0.0
        return PartitionUsage(
            key=probe.key,
            label=probe.label,
            path=str(probe.path),
            total_bytes=total,
            used_bytes=used,
            free_bytes=free,
            percent_used=percent,
        )
    try:
        usage = shutil.disk_usage(probe.path)
    except OSError as exc:
        logger.warning("analytics: disk_usage(%s) failed: %s", probe.path, exc)
        return PartitionUsage(
            key=probe.key,
            label=probe.label,
            path=str(probe.path),
            total_bytes=0,
            used_bytes=0,
            free_bytes=0,
            percent_used=0.0,
            error=str(exc),
        )
    percent = (usage.used / usage.total * PERCENT) if usage.total > 0 else 0.0
    return PartitionUsage(
        key=probe.key,
        label=probe.label,
        path=str(probe.path),
        total_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
        percent_used=percent,
    )


def query_indexed_files(connection: sqlite3.Connection) -> tuple[IndexedFileRow, ...]:
    cursor = connection.execute("SELECT file_path, file_size, file_mtime FROM indexed_files")
    rows: list[IndexedFileRow] = []
    for raw in cursor.fetchall():
        file_path = str(raw["file_path"]) if raw["file_path"] is not None else ""
        file_size = int(raw["file_size"]) if raw["file_size"] is not None else 0
        mtime: float | None = None
        with suppress(TypeError, ValueError):
            if raw["file_mtime"] is not None:
                mtime = float(raw["file_mtime"])
        rows.append(IndexedFileRow(file_path=file_path, file_size=file_size, file_mtime=mtime))
    return tuple(rows)


def classify_folder(file_path: str) -> str:
    """Return the TeslaCam folder name a clip lives under."""
    normalized = file_path.replace("\\", "/")
    parts = [segment for segment in normalized.split("/") if segment]
    for folder in (FOLDER_SAVED, FOLDER_SENTRY, FOLDER_RECENT):
        if folder in parts:
            return folder
    return FOLDER_OTHER


def iso_from_mtime(mtime: float | None) -> str | None:
    if mtime is None:
        return None
    return datetime.fromtimestamp(mtime, tz=UTC).isoformat(timespec="seconds")


def _accumulate_row(
    row: IndexedFileRow,
    buckets: dict[str, _FolderAccumulator],
    extrema: list[float | None],
) -> None:
    """Update folder bucket + global mtime extrema for one indexed row."""
    folder = classify_folder(row.file_path)
    bucket = buckets.setdefault(folder, _FolderAccumulator())
    bucket.count += 1
    bucket.size_bytes += row.file_size
    bucket.clip_stems.add(_clip_stem_from_path(row.file_path))
    mtime = row.file_mtime
    if mtime is None:
        return
    if extrema[0] is None or mtime < extrema[0]:
        extrema[0] = mtime
    if extrema[1] is None or mtime > extrema[1]:
        extrema[1] = mtime
    if bucket.oldest is None or mtime < bucket.oldest:
        bucket.oldest = mtime
    if bucket.newest is None or mtime > bucket.newest:
        bucket.newest = mtime


def summarize_indexed_files(rows: Sequence[IndexedFileRow]) -> VideoStatistics:
    buckets: dict[str, _FolderAccumulator] = {}
    total_files = 0
    total_bytes = 0
    extrema: list[float | None] = [None, None]
    for row in rows:
        total_files += 1
        total_bytes += row.file_size
        _accumulate_row(row, buckets, extrema)

    # Always surface the three canonical TeslaCam folders so the
    # dashboard table shows the full layout (with 0/0/0 rows) even on
    # a freshly-wiped volume — operators expect to see all buckets,
    # not just the ones that happen to have data right now.
    for canonical in (FOLDER_SAVED, FOLDER_SENTRY, FOLDER_RECENT):
        buckets.setdefault(canonical, _FolderAccumulator())

    folders = tuple(
        sorted(
            (
                FolderBreakdown(
                    name=name,
                    description=FOLDER_DESCRIPTIONS.get(name, FOLDER_DESCRIPTIONS[FOLDER_OTHER]),
                    priority=FOLDER_PRIORITY.get(name, "medium"),
                    count=bucket.count,
                    clip_count=len(bucket.clip_stems),
                    size_bytes=bucket.size_bytes,
                    oldest_iso=iso_from_mtime(bucket.oldest),
                    newest_iso=iso_from_mtime(bucket.newest),
                )
                for name, bucket in buckets.items()
            ),
            key=lambda f: f.size_bytes,
            reverse=True,
        )
    )
    clip_total = sum(len(bucket.clip_stems) for bucket in buckets.values())
    return VideoStatistics(
        total_files=total_files,
        clip_count=clip_total,
        total_bytes=total_bytes,
        oldest_iso=iso_from_mtime(extrema[0]),
        newest_iso=iso_from_mtime(extrema[1]),
        folders=folders,
    )


# Tesla writes one ``.mp4`` per camera angle, all sharing a timestamp
# prefix. Stripping the trailing ``-<angle>`` yields the clip stem we
# count as a single "clip".
_CAMERA_SUFFIXES: tuple[str, ...] = (
    "-front",
    "-back",
    "-left_pillar",
    "-right_pillar",
    "-left_repeater",
    "-right_repeater",
)


def _clip_stem_from_path(file_path: str) -> str:
    """Return the timestamp prefix that groups multi-angle clip files."""
    normalized = file_path.replace("\\", "/")
    leaf = normalized.rsplit("/", 1)[-1]
    if leaf.lower().endswith(".mp4"):
        leaf = leaf[:-4]
    for suffix in _CAMERA_SUFFIXES:
        if leaf.endswith(suffix):
            return leaf[: -len(suffix)]
    return leaf


def walk_teslacam_videos(clips_root: Path) -> tuple[IndexedFileRow, ...]:
    """Walk the TeslaCam volume and emit one row per ``.mp4`` clip file.

    Returns an empty tuple when ``clips_root`` does not exist (clean
    install, or the mount is not yet ready). Files we can't ``stat``
    are skipped — partial data is more useful to the operator than a
    crashed dashboard.
    """
    if not clips_root.is_dir():
        return ()
    rows: list[IndexedFileRow] = []
    stack: list[Path] = [clips_root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry)
                    continue
                if not entry.name.lower().endswith(".mp4"):
                    continue
                stat_result = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            try:
                rel = entry.relative_to(clips_root).as_posix()
            except ValueError:
                rel = entry.name
            rows.append(
                IndexedFileRow(
                    file_path=rel,
                    file_size=int(stat_result.st_size),
                    file_mtime=float(stat_result.st_mtime),
                )
            )
    return tuple(rows)


def _escalate(current: str, candidate: str) -> str:
    if STATUS_RANK[candidate] > STATUS_RANK[current]:
        return candidate
    return current


def _classify_percent(percent: float, cfg: AnalyticsSection) -> str:
    if percent >= cfg.critical_pct_used:
        return STATUS_CRITICAL
    if percent >= cfg.warning_pct_used:
        return STATUS_WARNING
    if percent >= cfg.caution_pct_used:
        return STATUS_CAUTION
    return STATUS_HEALTHY


def _record_severity(
    partition: PartitionUsage,
    severity: str,
    alerts: list[str],
    recommendations: list[str],
) -> None:
    """Append per-partition alert + recommendation strings for ``severity``."""
    if severity == STATUS_CRITICAL:
        alerts.append(f"{partition.label}: critical storage ({partition.percent_used:.1f}% used)")
        recommendations.append(f"Delete videos from {partition.label} immediately")
    elif severity == STATUS_WARNING:
        alerts.append(f"{partition.label}: low storage ({partition.percent_used:.1f}% used)")
        recommendations.append(f"Consider cleaning up old videos from {partition.label}")
    elif severity == STATUS_CAUTION:
        alerts.append(f"{partition.label}: storage usage at {partition.percent_used:.1f}%")


def compute_health(
    partitions: Sequence[PartitionUsage],
    cfg: AnalyticsSection,
) -> StorageHealth:
    alerts: list[str] = []
    recommendations: list[str] = []
    worst = STATUS_HEALTHY
    aggregate_percent = 0.0
    sized_count = 0
    for partition in partitions:
        if partition.error is not None:
            worst = _escalate(worst, STATUS_CRITICAL)
            alerts.append(f"{partition.label}: not accessible ({partition.error})")
            continue
        if partition.total_bytes == 0:
            continue
        aggregate_percent += partition.percent_used
        sized_count += 1
        severity = _classify_percent(partition.percent_used, cfg)
        worst = _escalate(worst, severity)
        _record_severity(partition, severity, alerts, recommendations)
    overall_percent = aggregate_percent / sized_count if sized_count > 0 else 0.0
    return StorageHealth(
        status=worst,
        percent_used=overall_percent,
        alerts=tuple(alerts),
        recommendations=tuple(recommendations),
    )


def estimate_recording_hours(
    primary: PartitionUsage,
    stats: VideoStatistics,
    theoretical_gb_per_hour: float,
) -> RecordingEstimate:
    free_gb = primary.free_gb
    if stats.total_files == 0:
        hours = free_gb / theoretical_gb_per_hour
        return RecordingEstimate(
            hours_remaining=hours,
            method=f"theoretical ({theoretical_gb_per_hour:.2f} GB/hour)",
            confidence="low",
        )
    avg_gb = stats.total_size_gb / stats.total_files
    if avg_gb <= 0:
        return RecordingEstimate(None, "unavailable", "low")
    clips_remaining = free_gb / avg_gb
    hours = clips_remaining / CLIPS_PER_HOUR
    confidence = "high" if stats.total_files > HIGH_CONFIDENCE_VIDEO_COUNT else "medium"
    return RecordingEstimate(
        hours_remaining=hours,
        method=f"based on {stats.total_files} existing clips",
        confidence=confidence,
    )


def utc_now() -> datetime:
    """Wall-clock now, always tz-aware (charter §3 — no naive datetimes)."""
    return datetime.now(tz=UTC)
