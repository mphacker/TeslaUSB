"""B-1 storage-analytics service (package facade).

Ports v1's ``services/analytics_service.py`` to B-1's data sources:

* Partition usage comes from :func:`shutil.disk_usage` on the
  configured filesystem roots — there is no IMG/loopback layer in
  B-1, so the v1 ``iter_all_partitions`` helper is gone and we
  probe ``backing_root`` (always) plus ``mapping.media_root`` and
  ``mapping.archive_root`` when they resolve to distinct mounts.
* Video statistics come from the mapping DB ``indexed_files``
  table (Phase 5.13b) rather than walking the SD card.
* Storage-health thresholds and the recording-rate fallback come
  from the new ``[analytics]`` config section — no magic literals.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

from teslausb_web.services.analytics_service._compute import (
    Probe,
    compute_health,
    device_id,
    estimate_recording_hours,
    probe_usage,
    query_indexed_files,
    summarize_indexed_files,
    utc_now,
)
from teslausb_web.services.analytics_service._models import (
    LABEL_ARCHIVE,
    LABEL_BACKING,
    LABEL_MEDIA,
    STATUS_CAUTION,
    STATUS_CRITICAL,
    STATUS_HEALTHY,
    STATUS_WARNING,
    AnalyticsConfigError,
    AnalyticsDataError,
    AnalyticsError,
    CompleteAnalytics,
    FolderBreakdown,
    PartitionUsage,
    RecordingEstimate,
    StorageHealth,
    VideoStatistics,
)
from teslausb_web.services.analytics_service._serializers import (
    complete_to_dict,
    estimate_to_dict,
    folder_to_dict,
    health_to_dict,
    partition_to_dict,
    video_stats_to_dict,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from teslausb_web.config import AnalyticsSection, WebConfig
    from teslausb_web.services.mapping.service import MappingService

logger = logging.getLogger(__name__)


class AnalyticsService:
    """Computes the storage-analytics dashboard payload.

    The service is intentionally stateless: each call re-probes the
    disks and re-queries the mapping DB. This keeps the dashboard
    correct after a Samba upload deletes thousands of clips without
    requiring an explicit invalidation hook.
    """

    def __init__(
        self,
        *,
        analytics_cfg: AnalyticsSection,
        probes: Sequence[Probe],
        mapping_service: MappingService,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not probes:
            raise AnalyticsConfigError("AnalyticsService requires at least one probe")
        self._cfg = analytics_cfg
        self._probes = tuple(probes)
        self._mapping_service = mapping_service
        self._clock = clock or utc_now

    def get_partition_usage(self) -> tuple[PartitionUsage, ...]:
        """Disk-usage snapshot for every configured root."""
        return tuple(probe_usage(probe) for probe in self._probes)

    def get_video_statistics(self) -> VideoStatistics:
        """Per-folder clip counts + sizes from the mapping DB."""
        try:
            with self._mapping_service.open_db() as connection:
                rows = query_indexed_files(connection)
        except sqlite3.Error as exc:
            logger.warning("analytics: indexed_files query failed: %s", exc)
            raise AnalyticsDataError(f"Failed to read mapping DB: {exc}") from exc
        return summarize_indexed_files(rows)

    def get_storage_health(self) -> StorageHealth:
        """Composite health verdict across every partition probe."""
        return compute_health(self.get_partition_usage(), self._cfg)

    def get_recording_estimate(self) -> RecordingEstimate:
        """Remaining recording hours based on real or theoretical rate."""
        primary = self.get_partition_usage()[0]
        if primary.error is not None or primary.free_bytes <= 0:
            return RecordingEstimate(None, "unavailable", "low")
        try:
            stats = self.get_video_statistics()
        except AnalyticsDataError:
            stats = VideoStatistics(0, 0, None, None, ())
        return estimate_recording_hours(
            primary,
            stats,
            self._cfg.theoretical_gb_per_hour,
        )

    def get_complete_analytics(self) -> CompleteAnalytics:
        """Single-call payload used by both the dashboard and the API."""
        partitions = self.get_partition_usage()
        try:
            video_stats = self.get_video_statistics()
        except AnalyticsDataError as exc:
            logger.warning("analytics: video stats unavailable: %s", exc)
            video_stats = VideoStatistics(0, 0, None, None, ())
        health = compute_health(partitions, self._cfg)
        primary = partitions[0]
        if primary.error is not None or primary.free_bytes <= 0:
            estimate = RecordingEstimate(None, "unavailable", "low")
        else:
            estimate = estimate_recording_hours(
                primary,
                video_stats,
                self._cfg.theoretical_gb_per_hour,
            )
        return CompleteAnalytics(
            partitions=partitions,
            video_statistics=video_stats,
            storage_health=health,
            recording_estimate=estimate,
            generated_at=self._clock().isoformat(timespec="seconds"),
        )


def make_analytics_service(
    cfg: WebConfig,
    mapping_service: MappingService,
) -> AnalyticsService:
    """Build the analytics service from a :class:`WebConfig`.

    ``backing_root`` is always probed first. ``media_root`` and
    ``archive_root`` are added only when they resolve to a distinct
    filesystem — otherwise the duplicate ``shutil.disk_usage``
    results would mislead the operator.
    """
    seen_devs: set[object] = set()
    probes: list[Probe] = []

    backing = cfg.paths.backing_root
    backing_dev = device_id(backing)
    probes.append(Probe(key="backing", label=LABEL_BACKING, path=backing))
    if backing_dev is not None:
        seen_devs.add(backing_dev)

    media = cfg.mapping.media_root
    media_dev = device_id(media)
    if media != backing and media_dev is not None and media_dev not in seen_devs:
        probes.append(Probe(key="media", label=LABEL_MEDIA, path=media))
        seen_devs.add(media_dev)

    archive = cfg.mapping.archive_root
    archive_dev = device_id(archive)
    if archive not in {backing, media} and archive_dev is not None and archive_dev not in seen_devs:
        probes.append(Probe(key="archive", label=LABEL_ARCHIVE, path=archive))

    return AnalyticsService(
        analytics_cfg=cfg.analytics,
        probes=probes,
        mapping_service=mapping_service,
    )


__all__ = (
    "STATUS_CAUTION",
    "STATUS_CRITICAL",
    "STATUS_HEALTHY",
    "STATUS_WARNING",
    "AnalyticsConfigError",
    "AnalyticsDataError",
    "AnalyticsError",
    "AnalyticsService",
    "CompleteAnalytics",
    "FolderBreakdown",
    "PartitionUsage",
    "RecordingEstimate",
    "StorageHealth",
    "VideoStatistics",
    "complete_to_dict",
    "estimate_to_dict",
    "folder_to_dict",
    "health_to_dict",
    "make_analytics_service",
    "partition_to_dict",
    "video_stats_to_dict",
)
