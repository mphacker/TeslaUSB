"""Data models, exceptions, and constants for the analytics service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

# Friendly labels for the three probed roots. v1 used part1/part2/part3;
# B-1 has no partitions so we use purpose-named labels instead.
LABEL_BACKING: Final[str] = "TeslaCam Storage"
LABEL_MEDIA: Final[str] = "Media Library"
LABEL_ARCHIVE: Final[str] = "Archive"

# Status tokens — string enum sentinels. v1 used the same vocabulary
# verbatim; preserved so the template's ``health.status`` branches
# don't need to change.
STATUS_HEALTHY: Final[str] = "healthy"
STATUS_CAUTION: Final[str] = "caution"
STATUS_WARNING: Final[str] = "warning"
STATUS_CRITICAL: Final[str] = "critical"

BYTES_PER_GIB: Final[float] = 1024.0**3
PERCENT: Final[float] = 100.0
HIGH_CONFIDENCE_VIDEO_COUNT: Final[int] = 100
CLIPS_PER_HOUR: Final[float] = 60.0

FOLDER_SAVED: Final[str] = "SavedClips"
FOLDER_SENTRY: Final[str] = "SentryClips"
FOLDER_RECENT: Final[str] = "RecentClips"
FOLDER_ARCHIVED: Final[str] = "ArchivedClips"
FOLDER_OTHER: Final[str] = "Other"

FOLDER_PRIORITY: Final[Mapping[str, str]] = {
    FOLDER_SAVED: "high",
    FOLDER_SENTRY: "high",
    FOLDER_ARCHIVED: "medium",
    FOLDER_RECENT: "low",
    FOLDER_OTHER: "medium",
}

FOLDER_DESCRIPTIONS: Final[Mapping[str, str]] = {
    FOLDER_SAVED: "Manually saved clips",
    FOLDER_SENTRY: "Sentry mode recordings",
    FOLDER_ARCHIVED: "Archived clips (cloud uploaded)",
    FOLDER_RECENT: "Recent driving footage",
    FOLDER_OTHER: "Unclassified clips",
}

STATUS_RANK: Final[Mapping[str, int]] = {
    STATUS_HEALTHY: 0,
    STATUS_CAUTION: 1,
    STATUS_WARNING: 2,
    STATUS_CRITICAL: 3,
}


class AnalyticsError(RuntimeError):
    """Base class for analytics-service errors."""


class AnalyticsConfigError(AnalyticsError):
    """The analytics service was given invalid configuration."""


class AnalyticsDataError(AnalyticsError):
    """A backing data source (disk, DB) could not be read safely."""


@dataclass(frozen=True, slots=True)
class PartitionUsage:
    """Disk-usage snapshot for one filesystem root."""

    key: str
    label: str
    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent_used: float
    error: str | None = None

    @property
    def total_gb(self) -> float:
        return self.total_bytes / BYTES_PER_GIB

    @property
    def used_gb(self) -> float:
        return self.used_bytes / BYTES_PER_GIB

    @property
    def free_gb(self) -> float:
        return self.free_bytes / BYTES_PER_GIB


@dataclass(frozen=True, slots=True)
class FolderBreakdown:
    """Per-folder video rollup used by the dashboard table."""

    name: str
    description: str
    priority: str
    count: int
    size_bytes: int
    oldest_iso: str | None
    newest_iso: str | None

    @property
    def size_gb(self) -> float:
        return self.size_bytes / BYTES_PER_GIB


@dataclass(frozen=True, slots=True)
class VideoStatistics:
    """Mapping-DB-derived clip statistics."""

    total_files: int
    total_bytes: int
    oldest_iso: str | None
    newest_iso: str | None
    folders: tuple[FolderBreakdown, ...]

    @property
    def total_size_gb(self) -> float:
        return self.total_bytes / BYTES_PER_GIB


@dataclass(frozen=True, slots=True)
class RecordingEstimate:
    """Hours of recording remaining on the primary TeslaCam volume."""

    hours_remaining: float | None
    method: str
    confidence: str


@dataclass(frozen=True, slots=True)
class StorageHealth:
    """Composite storage health verdict."""

    status: str
    percent_used: float
    alerts: tuple[str, ...] = ()
    recommendations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CompleteAnalytics:
    """Full dashboard payload computed by :class:`AnalyticsService`."""

    partitions: tuple[PartitionUsage, ...]
    video_statistics: VideoStatistics
    storage_health: StorageHealth
    recording_estimate: RecordingEstimate
    generated_at: str
