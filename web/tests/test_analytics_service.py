"""Tests for ``teslausb_web.services.analytics_service``."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from teslausb_web.config import (
    AnalyticsSection,
    ConfigError,
    MappingSection,
    PathsSection,
    WebConfig,
)
from teslausb_web.services.analytics_service import (
    STATUS_CAUTION,
    STATUS_CRITICAL,
    STATUS_HEALTHY,
    STATUS_WARNING,
    AnalyticsConfigError,
    AnalyticsDataError,
    AnalyticsError,
    AnalyticsService,
    PartitionUsage,
    RecordingEstimate,
    VideoStatistics,
    complete_to_dict,
    folder_to_dict,
    health_to_dict,
    make_analytics_service,
    partition_to_dict,
    video_stats_to_dict,
)
from teslausb_web.services.analytics_service._compute import (
    Probe,
    classify_folder,
    compute_health,
    estimate_recording_hours,
    probe_usage,
    summarize_indexed_files,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


_SAMPLE_MTIME_OLD = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC).timestamp()
_SAMPLE_MTIME_NEW = datetime(2024, 6, 7, 8, 9, 10, tzinfo=UTC).timestamp()
_TEN_GIB = 10 * 1024**3


class _FakeMappingQueries:
    """Minimal MappingService stand-in exposing :meth:`open_db`."""

    def __init__(self, rows: list[tuple[str, int, float | None]]) -> None:
        self._rows = rows
        self._raise: Exception | None = None

    def set_error(self, exc: Exception) -> None:
        self._raise = exc

    @contextmanager
    def open_db(self) -> Iterator[sqlite3.Connection]:
        if self._raise is not None:
            raise self._raise
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE indexed_files (file_path TEXT, file_size INTEGER, file_mtime REAL)"
        )
        connection.executemany(
            "INSERT INTO indexed_files VALUES (?, ?, ?)",
            self._rows,
        )
        try:
            yield connection
        finally:
            connection.close()


def _fixed_clock() -> datetime:
    return datetime(2024, 8, 9, 10, 11, 12, tzinfo=UTC)


def _build_service(
    rows: list[tuple[str, int, float | None]] | None = None,
) -> tuple[AnalyticsService, _FakeMappingQueries]:
    mapping = _FakeMappingQueries(rows or [])
    probes = (Probe(key="backing", label="TeslaCam Storage", path=Path.cwd()),)
    service = AnalyticsService(
        analytics_cfg=AnalyticsSection(),
        probes=probes,
        mapping_queries=mapping,  # type: ignore[arg-type]
        clock=_fixed_clock,
    )
    return service, mapping


def _usage(percent: float, *, label: str = "TeslaCam Storage") -> PartitionUsage:
    return PartitionUsage(
        key="backing",
        label=label,
        path="/srv",
        total_bytes=1_000,
        used_bytes=int(percent * 10),
        free_bytes=1_000 - int(percent * 10),
        percent_used=percent,
    )


# ---------------------------------------------------------------------------
# Config thresholds
# ---------------------------------------------------------------------------


class TestAnalyticsConfig:
    def test_defaults_pass_validation(self) -> None:
        section = AnalyticsSection()
        assert section.caution_pct_used < section.warning_pct_used < section.critical_pct_used

    def test_out_of_order_thresholds_fail(self) -> None:
        with pytest.raises(ConfigError):
            AnalyticsSection(caution_pct_used=90.0, warning_pct_used=80.0)

    def test_non_positive_record_rate_fails(self) -> None:
        with pytest.raises(ConfigError):
            AnalyticsSection(theoretical_gb_per_hour=0.0)


# ---------------------------------------------------------------------------
# Service construction
# ---------------------------------------------------------------------------


class TestServiceConstruction:
    def test_requires_at_least_one_probe(self) -> None:
        with pytest.raises(AnalyticsConfigError):
            AnalyticsService(
                analytics_cfg=AnalyticsSection(),
                probes=(),
                mapping_queries=_FakeMappingQueries([]),  # type: ignore[arg-type]
            )

    def test_factory_dedups_partitions_by_device(self, tmp_path: Path) -> None:
        backing = tmp_path / "backing"
        backing.mkdir()
        (backing / "archive").mkdir()
        cfg = WebConfig(
            paths=PathsSection(backing_root=backing),
            mapping=MappingSection(
                db_path=tmp_path / "state" / "mapping.sqlite",
                media_root=backing,
            ),
            source_path=None,
        )
        service = make_analytics_service(cfg, _FakeMappingQueries([]))  # type: ignore[arg-type]
        partitions = service.get_partition_usage()
        assert len(partitions) == 1
        assert partitions[0].label == "TeslaCam Storage"

    def test_exception_hierarchy(self) -> None:
        assert issubclass(AnalyticsConfigError, AnalyticsError)
        assert issubclass(AnalyticsDataError, AnalyticsError)


# ---------------------------------------------------------------------------
# Partition probe
# ---------------------------------------------------------------------------


class TestPartitionProbe:
    def test_existing_path_reports_usage(self, tmp_path: Path) -> None:
        probe = Probe(key="backing", label="TeslaCam Storage", path=tmp_path)
        usage = probe_usage(probe)
        assert usage.error is None
        assert usage.total_bytes > 0
        assert 0 <= usage.percent_used <= 100
        assert usage.total_gb > 0

    def test_missing_path_records_error(self, tmp_path: Path) -> None:
        probe = Probe(
            key="backing",
            label="TeslaCam Storage",
            path=tmp_path / "no-such-dir",
        )
        usage = probe_usage(probe)
        assert usage.error is not None
        assert usage.total_bytes == 0
        assert usage.percent_used == 0.0

    def test_partition_usage_is_frozen(self) -> None:
        usage = _usage(50.0)
        with pytest.raises(AttributeError):
            usage.label = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Folder classification
# ---------------------------------------------------------------------------


class TestFolderClassification:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("TeslaCam/SavedClips/2024/x.mp4", "SavedClips"),
            ("TeslaCam/SentryClips/2024/x.mp4", "SentryClips"),
            ("TeslaCam/RecentClips/x.mp4", "RecentClips"),
            ("TeslaCam/ArchivedClips/x.mp4", "Other"),
            ("foo/bar.mp4", "Other"),
            ("", "Other"),
            (r"TeslaCam\SavedClips\x.mp4", "SavedClips"),
        ],
    )
    def test_classify(self, path: str, expected: str) -> None:
        assert classify_folder(path) == expected


# ---------------------------------------------------------------------------
# Video statistics
# ---------------------------------------------------------------------------


class TestVideoStatistics:
    def test_empty_db_returns_zeroed_stats(self) -> None:
        service, _ = _build_service([])
        stats = service.get_video_statistics()
        assert stats.total_files == 0
        assert stats.total_bytes == 0
        # The three canonical TeslaCam folders are always shown (with
        # zero counts) so the dashboard layout stays consistent.
        assert {f.name for f in stats.folders} == {
            "SavedClips",
            "SentryClips",
            "RecentClips",
        }
        assert all(f.count == 0 and f.size_bytes == 0 for f in stats.folders)
        assert stats.oldest_iso is None
        assert stats.newest_iso is None

    def test_buckets_and_extrema(self) -> None:
        rows = [
            ("TeslaCam/SavedClips/2024-01-02/front.mp4", 100, _SAMPLE_MTIME_OLD),
            ("TeslaCam/SavedClips/2024-01-02/back.mp4", 200, _SAMPLE_MTIME_NEW),
            ("TeslaCam/SentryClips/event-x/front.mp4", 300, _SAMPLE_MTIME_NEW),
            ("TeslaCam/RecentClips/front.mp4", 50, _SAMPLE_MTIME_OLD),
            ("TeslaCam/ArchivedClips/back.mp4", 75, None),
            ("foo/bar.mp4", 10, _SAMPLE_MTIME_OLD),
        ]
        service, _ = _build_service(rows)
        stats = service.get_video_statistics()
        assert stats.total_files == 6
        assert stats.total_bytes == 735
        folder_names = {f.name for f in stats.folders}
        assert folder_names == {
            "SavedClips",
            "SentryClips",
            "RecentClips",
            "Other",
        }
        # SavedClips is heaviest (100+200=300) — sorted to the front.
        assert stats.folders[0].name == "SavedClips"
        # SavedClips bucket aggregates two rows.
        saved = next(f for f in stats.folders if f.name == "SavedClips")
        assert saved.count == 2
        assert saved.size_bytes == 300
        assert saved.oldest_iso is not None
        assert saved.newest_iso is not None

    def test_summarize_helper_handles_empty_sequence(self) -> None:
        assert summarize_indexed_files(()).total_files == 0

    def test_db_error_raises_typed_exception(self) -> None:
        service, mapping = _build_service([])
        mapping.set_error(sqlite3.OperationalError("boom"))
        with pytest.raises(AnalyticsDataError):
            service.get_video_statistics()


# ---------------------------------------------------------------------------
# Storage health
# ---------------------------------------------------------------------------


class TestStorageHealth:
    def test_healthy_when_all_under_caution(self) -> None:
        health = compute_health((_usage(10.0),), AnalyticsSection())
        assert health.status == STATUS_HEALTHY
        assert health.alerts == ()

    def test_caution_band(self) -> None:
        health = compute_health((_usage(82.0),), AnalyticsSection())
        assert health.status == STATUS_CAUTION
        assert any("82.0%" in a for a in health.alerts)

    def test_warning_band(self) -> None:
        health = compute_health((_usage(92.0),), AnalyticsSection())
        assert health.status == STATUS_WARNING
        assert any("low storage" in a for a in health.alerts)

    def test_critical_band(self) -> None:
        health = compute_health((_usage(96.0),), AnalyticsSection())
        assert health.status == STATUS_CRITICAL
        assert any("critical storage" in a for a in health.alerts)
        assert health.recommendations

    def test_error_partition_escalates_to_critical(self) -> None:
        broken = replace(_usage(0.0), error="ENOENT")
        health = compute_health((broken,), AnalyticsSection())
        assert health.status == STATUS_CRITICAL
        assert any("not accessible" in a for a in health.alerts)

    def test_zero_bytes_partition_is_skipped(self) -> None:
        empty = PartitionUsage(
            key="backing",
            label="x",
            path="/",
            total_bytes=0,
            used_bytes=0,
            free_bytes=0,
            percent_used=0.0,
        )
        health = compute_health((empty,), AnalyticsSection())
        assert health.status == STATUS_HEALTHY
        assert health.percent_used == 0.0


# ---------------------------------------------------------------------------
# Recording estimate
# ---------------------------------------------------------------------------


class TestRecordingEstimate:
    def _primary(self, *, free: int = _TEN_GIB) -> PartitionUsage:
        return PartitionUsage(
            key="backing",
            label="x",
            path="/",
            total_bytes=_TEN_GIB,
            used_bytes=_TEN_GIB - free,
            free_bytes=free,
            percent_used=0.0,
        )

    def test_theoretical_when_db_empty(self) -> None:
        estimate = estimate_recording_hours(
            self._primary(),
            VideoStatistics(0, 0, 0, None, None, ()),
            theoretical_gb_per_hour=0.4,
        )
        assert estimate.confidence == "low"
        assert estimate.hours_remaining is not None
        assert estimate.hours_remaining > 0
        assert "theoretical" in estimate.method

    def test_actual_clip_average_used(self) -> None:
        stats = VideoStatistics(
            total_files=200,
            clip_count=34,
            total_bytes=1024**3,
            oldest_iso=None,
            newest_iso=None,
            folders=(),
        )
        estimate = estimate_recording_hours(self._primary(), stats, 0.4)
        assert estimate.confidence == "high"
        assert "200 existing clips" in estimate.method

    def test_medium_confidence_below_threshold(self) -> None:
        stats = VideoStatistics(
            total_files=10,
            clip_count=2,
            total_bytes=1024**3,
            oldest_iso=None,
            newest_iso=None,
            folders=(),
        )
        estimate = estimate_recording_hours(self._primary(), stats, 0.4)
        assert estimate.confidence == "medium"

    def test_zero_average_size_unavailable(self) -> None:
        stats = VideoStatistics(
            total_files=5,
            clip_count=1,
            total_bytes=0,
            oldest_iso=None,
            newest_iso=None,
            folders=(),
        )
        estimate = estimate_recording_hours(self._primary(), stats, 0.4)
        assert estimate.hours_remaining is None
        assert isinstance(estimate, RecordingEstimate)

    def test_service_returns_unavailable_when_primary_errored(self) -> None:
        service, _ = _build_service([])
        # Replace probes with a single broken probe.
        broken = Probe(
            key="backing",
            label="x",
            path=Path("/this/path/should/never/exist/zzz"),
        )
        service._probes = (broken,)  # type: ignore[assignment]
        estimate = service.get_recording_estimate()
        assert estimate.hours_remaining is None
        assert estimate.confidence == "low"

    def test_service_estimate_with_db(self) -> None:
        service, _ = _build_service(
            [("TeslaCam/SavedClips/x.mp4", 100, _SAMPLE_MTIME_OLD)],
        )
        estimate = service.get_recording_estimate()
        # Probes the cwd so primary will not be in error.
        assert estimate.method


# ---------------------------------------------------------------------------
# Complete payload + storage_health endpoint
# ---------------------------------------------------------------------------


class TestCompleteAnalytics:
    def test_includes_every_section(self) -> None:
        service, _ = _build_service(
            [("TeslaCam/SavedClips/x.mp4", 100, _SAMPLE_MTIME_OLD)],
        )
        payload = service.get_complete_analytics()
        assert payload.partitions
        assert payload.video_statistics.total_files == 1
        assert payload.storage_health.status in {
            STATUS_HEALTHY,
            STATUS_CAUTION,
            STATUS_WARNING,
            STATUS_CRITICAL,
        }
        assert payload.generated_at.startswith("2024-08-09")

    def test_survives_db_failure(self) -> None:
        service, mapping = _build_service([])
        mapping.set_error(sqlite3.OperationalError("boom"))
        payload = service.get_complete_analytics()
        assert payload.video_statistics.total_files == 0

    def test_to_dict_keys(self) -> None:
        service, _ = _build_service(
            [("TeslaCam/SavedClips/x.mp4", 100, _SAMPLE_MTIME_OLD)],
        )
        payload = service.get_complete_analytics()
        as_dict = complete_to_dict(payload)
        assert set(as_dict) == {
            "partitions",
            "video_statistics",
            "storage_health",
            "recording_estimate",
            "generated_at",
        }
        assert isinstance(as_dict["partitions"], list)

    def test_get_storage_health_route(self) -> None:
        service, _ = _build_service([])
        health = service.get_storage_health()
        assert health.status in {
            STATUS_HEALTHY,
            STATUS_CAUTION,
            STATUS_WARNING,
            STATUS_CRITICAL,
        }


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


class TestSerializers:
    def test_partition_to_dict_includes_error_field(self) -> None:
        usage = replace(_usage(0.0), error="ENOENT")
        d = partition_to_dict(usage)
        assert d["error"] == "ENOENT"
        assert d["percent_used"] == 0.0

    def test_video_stats_and_folder_serialization(self) -> None:
        service, _ = _build_service(
            [("TeslaCam/SavedClips/x.mp4", 100, _SAMPLE_MTIME_OLD)],
        )
        stats = service.get_video_statistics()
        d = video_stats_to_dict(stats)
        assert d["total_files"] == 1
        folders_dict = d["folders"]
        assert isinstance(folders_dict, list)
        # Always 3 canonical folders; only SavedClips has data here.
        assert len(folders_dict) == 3
        by_name = {f.name: f for f in stats.folders}
        assert by_name["SavedClips"].count == 1
        assert by_name["SentryClips"].count == 0
        assert by_name["RecentClips"].count == 0
        # Top folder (highest size) should be SavedClips.
        assert folder_to_dict(stats.folders[0])["name"] == "SavedClips"

    def test_health_serialization_round_trips_status(self) -> None:
        health = compute_health((_usage(50.0),), AnalyticsSection())
        d = health_to_dict(health)
        assert d["status"] == STATUS_HEALTHY
