"""JSON serialization helpers for analytics dataclasses."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teslausb_web.services.analytics_service._models import (
        CompleteAnalytics,
        FolderBreakdown,
        PartitionUsage,
        RecordingEstimate,
        StorageHealth,
        VideoStatistics,
    )


def partition_to_dict(p: PartitionUsage) -> dict[str, object]:
    return {
        "key": p.key,
        "label": p.label,
        "path": p.path,
        "total_bytes": p.total_bytes,
        "used_bytes": p.used_bytes,
        "free_bytes": p.free_bytes,
        "total_gb": round(p.total_gb, 3),
        "used_gb": round(p.used_gb, 3),
        "free_gb": round(p.free_gb, 3),
        "percent_used": round(p.percent_used, 2),
        "error": p.error,
    }


def folder_to_dict(f: FolderBreakdown) -> dict[str, object]:
    return {
        "name": f.name,
        "description": f.description,
        "priority": f.priority,
        "count": f.count,
        "size_bytes": f.size_bytes,
        "size_gb": round(f.size_gb, 3),
        "oldest_iso": f.oldest_iso,
        "newest_iso": f.newest_iso,
    }


def video_stats_to_dict(v: VideoStatistics) -> dict[str, object]:
    return {
        "total_files": v.total_files,
        "total_bytes": v.total_bytes,
        "total_size_gb": round(v.total_size_gb, 3),
        "oldest_iso": v.oldest_iso,
        "newest_iso": v.newest_iso,
        "folders": [folder_to_dict(f) for f in v.folders],
    }


def health_to_dict(h: StorageHealth) -> dict[str, object]:
    return {
        "status": h.status,
        "percent_used": round(h.percent_used, 2),
        "alerts": list(h.alerts),
        "recommendations": list(h.recommendations),
    }


def estimate_to_dict(e: RecordingEstimate) -> dict[str, object]:
    return {
        "hours_remaining": (None if e.hours_remaining is None else round(e.hours_remaining, 2)),
        "method": e.method,
        "confidence": e.confidence,
    }


def complete_to_dict(c: CompleteAnalytics) -> dict[str, object]:
    """Serialize the full payload — the JSON contract used by ``/api/data``."""
    return {
        "partitions": [partition_to_dict(p) for p in c.partitions],
        "video_statistics": video_stats_to_dict(c.video_statistics),
        "storage_health": health_to_dict(c.storage_health),
        "recording_estimate": estimate_to_dict(c.recording_estimate),
        "generated_at": c.generated_at,
    }
