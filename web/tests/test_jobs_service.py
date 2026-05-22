"""Unit tests for the Failed Jobs service package."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

# Ensure the cloud_archive package is fully initialized before pulling
# DeadLetterEntry off cloud_archive_queries (avoids a partially-initialised
# circular import when this test module loads first).
import teslausb_web.services.cloud_archive  # noqa: F401
from teslausb_web.services.cloud_archive_queries import DeadLetterEntry
from teslausb_web.services.jobs_service import (
    CloudSyncAdapter,
    CloudSyncAdapterProtocol,
    IndexerAdapter,
    JobsService,
    JobsServiceError,
    Recommendation,
    SubsystemKey,
    ValueTier,
    classify_clip_value,
    classify_recommendation,
    make_jobs_service,
    redact_last_error,
)
from teslausb_web.services.jobs_service._indexer_adapter import IndexerAdapter as _ImportedIndexer

# ---------------------------------------------------------------- redactor


class TestRedactor:
    def test_returns_empty_for_none(self) -> None:
        assert redact_last_error(None) == ""

    def test_returns_empty_for_empty_string(self) -> None:
        assert redact_last_error("") == ""

    def test_strips_mnt_paths(self) -> None:
        msg = "open /mnt/recent/clip-2024.mp4 failed"
        out = redact_last_error(msg)
        assert "/mnt/" not in out
        assert "<path>" in out

    def test_strips_home_paths(self) -> None:
        msg = "could not read /home/pi/Backup/index.db"
        out = redact_last_error(msg)
        assert "/home/" not in out
        assert "<path>" in out

    def test_strips_rclone_remote(self) -> None:
        msg = "uploading to myremote:tesla-bucket/clips failed"
        out = redact_last_error(msg)
        assert "myremote:tesla-bucket" not in out
        assert "<remote>" in out

    def test_strips_s3_virtual_host(self) -> None:
        msg = "PUT https://my-bucket.s3.us-east-1.amazonaws.com/key.mp4"
        out = redact_last_error(msg)
        assert "amazonaws" not in out
        assert "my-bucket" not in out
        # Either the generic remote: pattern or the s3-host pattern strips
        # it; both leave the bucket name out of the output.
        assert ("<remote>" in out) or ("<s3-host>" in out)

    def test_strips_bare_s3_host(self) -> None:
        msg = "host: my-bucket.s3.us-east-1.amazonaws.com unreachable"
        out = redact_last_error(msg)
        assert "amazonaws" not in out
        assert "<s3-host>" in out

    def test_caps_long_payload_with_ellipsis(self) -> None:
        msg = "x" * 5000
        out = redact_last_error(msg)
        assert len(out) <= 610
        assert out.endswith("…")

    def test_preserves_short_safe_messages(self) -> None:
        assert redact_last_error("connection refused") == "connection refused"


# ---------------------------------------------------------------- classifier


class TestClassifyClipValue:
    def test_sentry_is_event(self) -> None:
        v = classify_clip_value(SubsystemKey.CLOUD_SYNC, "/foo/SentryClips/2024.mp4")
        assert v.tier == "event"

    def test_saved_is_event(self) -> None:
        v = classify_clip_value(SubsystemKey.CLOUD_SYNC, "/x/SavedClips/y.mp4")
        assert v.tier == "event"

    def test_recent_is_recent(self) -> None:
        v = classify_clip_value(SubsystemKey.CLOUD_SYNC, "/x/RecentClips/y.mp4")
        assert v.tier == "recent"


    def test_indexer_default_is_index(self) -> None:
        v = classify_clip_value(SubsystemKey.INDEXER, "trip-99")
        assert v.tier == "index"

    def test_cloud_default_is_cloud(self) -> None:
        v = classify_clip_value(SubsystemKey.CLOUD_SYNC, "other/file.mp4")
        assert v.tier == "cloud"

    def test_handles_empty_identifier(self) -> None:
        v = classify_clip_value(SubsystemKey.CLOUD_SYNC, "")
        assert v.tier == "cloud"
        assert isinstance(v, ValueTier)


class TestClassifyRecommendation:
    def test_no_error_returns_either(self) -> None:
        rec = classify_recommendation(None)
        assert rec.action == "either"
        assert isinstance(rec, Recommendation)

    def test_empty_error_returns_either(self) -> None:
        rec = classify_recommendation("   ")
        assert rec.action == "either"

    def test_no_such_file_is_delete(self) -> None:
        assert classify_recommendation("No such file or directory").action == "delete"

    def test_enoent_is_delete(self) -> None:
        assert classify_recommendation("ENOENT: file gone").action == "delete"

    def test_moov_atom_is_delete(self) -> None:
        assert classify_recommendation("moov atom not found").action == "delete"

    def test_io_error_is_retry(self) -> None:
        assert classify_recommendation("I/O error reading block").action == "retry"

    def test_connection_refused_is_retry(self) -> None:
        assert classify_recommendation("connection refused").action == "retry"

    def test_dial_tcp_is_retry(self) -> None:
        assert classify_recommendation("dial tcp 1.2.3.4:443").action == "retry"

    def test_403_is_retry(self) -> None:
        assert classify_recommendation("403 access denied").action == "retry"

    def test_rate_limit_is_retry(self) -> None:
        assert classify_recommendation("429 rate limit hit").action == "retry"

    def test_permission_denied_is_retry(self) -> None:
        assert classify_recommendation("permission denied").action == "retry"

    def test_lock_busy_is_retry(self) -> None:
        assert classify_recommendation("lock contention").action == "retry"

    def test_stuck_attempts_recommends_delete(self) -> None:
        rec = classify_recommendation("totally unknown gibberish", attempts=10)
        assert rec.action == "delete"

    def test_unknown_with_low_attempts_is_either(self) -> None:
        rec = classify_recommendation("totally unknown gibberish", attempts=2)
        assert rec.action == "either"


# ---------------------------------------------------------------- indexer adapter


class TestIndexerAdapter:
    def test_list_rows_empty_without_mapping_service(self) -> None:
        adapter = IndexerAdapter(None)
        assert adapter.list_rows(100) == []

    def test_count_is_zero(self) -> None:
        assert IndexerAdapter(None).count() == 0

    def test_retry_returns_zero(self) -> None:
        assert IndexerAdapter(None).retry(None) == 0
        assert IndexerAdapter(None).retry("ident") == 0

    def test_delete_returns_zero(self) -> None:
        assert IndexerAdapter(None).delete(None) == 0
        assert IndexerAdapter(None).delete("ident") == 0

    def test_build_row_helper_produces_typed_row(self) -> None:
        # Exercises the kept-ready private helper so future wiring
        # (issue #222) lands with the seam already proven.
        row = _ImportedIndexer._build_row(
            identifier="trip-7",
            attempts=3,
            last_error="No such file /mnt/recent/x.mp4",
            previous_last_error=None,
        )
        assert row.subsystem is SubsystemKey.INDEXER
        assert row.identifier == "trip-7"
        assert row.attempts == 3
        assert "<path>" in row.last_error
        assert row.recommendation.action == "delete"
        assert row.value.tier == "index"


# ---------------------------------------------------------------- cloud_sync adapter


def _entry(
    *,
    file_path: str = "RecentClips/x.mp4",
    retry_count: int = 4,
    last_error: str | None = "connection refused",
    previous_last_error: str | None = None,
) -> DeadLetterEntry:
    return DeadLetterEntry(
        id=1,
        file_path=file_path,
        file_size=1024,
        retry_count=retry_count,
        last_error=last_error,
        previous_last_error=previous_last_error,
    )


@dataclass
class FakeCloudArchive(CloudSyncAdapterProtocol):
    entries: tuple[DeadLetterEntry, ...] = ()
    last_retry: str | None = None
    last_delete: str | None = None
    retries_returned: int = 1
    deletes_returned: int = 1

    def list_dead_letters(self, limit: int = 100) -> tuple[DeadLetterEntry, ...]:
        _ = limit
        return self.entries

    def count_dead_letters(self) -> int:
        return len(self.entries)

    def retry_dead_letter(self, file_path: str | None = None) -> int:
        self.last_retry = file_path
        return self.retries_returned

    def delete_dead_letter(self, file_path: str | None = None) -> int:
        self.last_delete = file_path
        return self.deletes_returned


class TestCloudSyncAdapter:
    def test_no_service_yields_empty_and_zero(self) -> None:
        adapter = CloudSyncAdapter(None)
        assert adapter.list_rows(50) == []
        assert adapter.count() == 0
        assert adapter.retry(None) == 0
        assert adapter.delete("anything") == 0

    def test_list_rows_translates_entries(self) -> None:
        fake = FakeCloudArchive(
            entries=(
                _entry(file_path="/mnt/RecentClips/c.mp4"),
                _entry(file_path="/mnt/SentryClips/c.mp4", last_error="moov atom missing"),
            )
        )
        adapter = CloudSyncAdapter(fake)
        rows = adapter.list_rows(50)
        assert len(rows) == 2
        assert rows[0].subsystem is SubsystemKey.CLOUD_SYNC
        assert rows[0].value.tier == "recent"
        assert rows[1].value.tier == "event"
        assert rows[1].recommendation.action == "delete"

    def test_count(self) -> None:
        fake = FakeCloudArchive(entries=(_entry(),))
        assert CloudSyncAdapter(fake).count() == 1

    def test_retry_passes_row_id_through(self) -> None:
        fake = FakeCloudArchive(entries=())
        adapter = CloudSyncAdapter(fake)
        assert adapter.retry("ident.mp4") == 1
        assert fake.last_retry == "ident.mp4"
        assert adapter.retry(None) == 1
        assert fake.last_retry is None

    def test_delete_passes_row_id_through(self) -> None:
        fake = FakeCloudArchive(entries=())
        adapter = CloudSyncAdapter(fake)
        assert adapter.delete("ident.mp4") == 1
        assert fake.last_delete == "ident.mp4"

    def test_redacts_paths_in_last_error(self) -> None:
        fake = FakeCloudArchive(entries=(_entry(last_error="open /mnt/recent/clip.mp4 failed"),))
        rows = CloudSyncAdapter(fake).list_rows(10)
        assert "/mnt/" not in rows[0].last_error


# ---------------------------------------------------------------- facade


class TestJobsService:
    def _service(
        self,
        *,
        cloud_entries: tuple[DeadLetterEntry, ...] = (),
    ) -> tuple[JobsService, FakeCloudArchive]:
        fake = FakeCloudArchive(entries=cloud_entries)
        svc = JobsService(
            indexer=IndexerAdapter(None),
            cloud_sync=CloudSyncAdapter(fake),
        )
        return svc, fake

    def test_count_all_sums_subsystems(self) -> None:
        svc, _ = self._service(cloud_entries=(_entry(), _entry()))
        counts = svc.count_all()
        assert counts.indexer == 0
        assert counts.cloud_sync == 2
        assert counts.total == 2

    def test_failed_all_subsystems_returns_union(self) -> None:
        svc, _ = self._service(cloud_entries=(_entry(),))
        rows = svc.failed(None)
        assert len(rows) == 1
        assert rows[0].subsystem is SubsystemKey.CLOUD_SYNC

    def test_failed_indexer_only(self) -> None:
        svc, _ = self._service(cloud_entries=(_entry(),))
        assert svc.failed(SubsystemKey.INDEXER) == []

    def test_failed_cloud_only(self) -> None:
        svc, _ = self._service(cloud_entries=(_entry(),))
        rows = svc.failed(SubsystemKey.CLOUD_SYNC)
        assert len(rows) == 1

    def test_failed_clamps_limit_and_offset(self) -> None:
        svc, _ = self._service(
            cloud_entries=tuple(_entry(file_path=f"RecentClips/{i}.mp4") for i in range(5))
        )
        rows = svc.failed(SubsystemKey.CLOUD_SYNC, limit=2, offset=1)
        assert len(rows) == 2
        assert rows[0].identifier.endswith("1.mp4")

    def test_failed_offset_negative_clamped_to_zero(self) -> None:
        svc, _ = self._service(cloud_entries=(_entry(),))
        rows = svc.failed(SubsystemKey.CLOUD_SYNC, limit=1, offset=-100)
        assert len(rows) == 1

    def test_retry_dispatches_to_indexer(self) -> None:
        svc, _ = self._service()
        outcome = svc.retry(SubsystemKey.INDEXER, None)
        assert outcome.rows_reset == 0

    def test_retry_dispatches_to_cloud(self) -> None:
        svc, fake = self._service()
        outcome = svc.retry(SubsystemKey.CLOUD_SYNC, "abc")
        assert outcome.rows_reset == 1
        assert fake.last_retry == "abc"

    def test_delete_dispatches_to_cloud(self) -> None:
        svc, fake = self._service()
        outcome = svc.delete(SubsystemKey.CLOUD_SYNC, "abc")
        assert outcome.rows_deleted == 1
        assert fake.last_delete == "abc"

    def test_redact_public_static(self) -> None:
        assert "<path>" in JobsService.redact("open /mnt/x failed")


class TestFactory:
    def test_make_jobs_service_with_nones(self) -> None:
        svc = make_jobs_service(mapping_service=None, cloud_archive_service=None)
        assert isinstance(svc, JobsService)
        counts = svc.count_all()
        assert counts.total == 0

    def test_make_jobs_service_with_fake_cloud(self) -> None:
        fake = FakeCloudArchive(entries=(_entry(),))
        svc = make_jobs_service(mapping_service=None, cloud_archive_service=fake)
        assert svc.count_all().cloud_sync == 1


class TestJobsServiceError:
    def test_is_runtime_error(self) -> None:
        with pytest.raises(JobsServiceError):
            raise JobsServiceError("bad")
        with pytest.raises(RuntimeError):
            raise JobsServiceError("bad")
