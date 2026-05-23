"""Public Failed Jobs facade.

Unifies the per-subsystem adapters (indexer + cloud_sync) behind
one typed API the blueprint calls. NOTHING in this package imports
Flask — the blueprint is the only Flask-aware layer (charter
§"Architectural Principles / Dependency inversion").

B-1 dropped v1's ``archive`` subsystem because cleanup is now a
fire-and-forget filesystem move with no queue; see
``docs/00-PLAN.md`` "no IMG/loopback" invariant.
"""

from __future__ import annotations

from teslausb_web.services.jobs_service._classifier import (
    classify_clip_value,
    classify_recommendation,
)
from teslausb_web.services.jobs_service._cloud_sync_adapter import (
    CloudSyncAdapter,
    CloudSyncAdapterProtocol,
)
from teslausb_web.services.jobs_service._indexer_adapter import IndexerAdapter
from teslausb_web.services.jobs_service._models import (
    DeleteOutcome,
    FailedJobRow,
    JobCounts,
    Recommendation,
    RetryOutcome,
    SubsystemKey,
    ValueTier,
)
from teslausb_web.services.jobs_service._redactor import redact_last_error


class JobsServiceError(RuntimeError):
    """Raised when the Failed Jobs facade receives an invalid request.

    Wraps unknown subsystems and other contract violations. Internal
    failures of an underlying adapter bubble up as the adapter's own
    exception type so the blueprint can log the original cause.
    """


_DEFAULT_LIMIT: int = 100
_HARD_LIMIT: int = 1000


class JobsService:
    """Public facade over the per-subsystem adapters.

    Construct via :func:`make_jobs_service` from the Flask factory so
    the indexer/cloud adapters are wired with the right backing
    services. Tests pass adapter doubles directly.
    """

    def __init__(
        self,
        *,
        indexer: IndexerAdapter,
        cloud_sync: CloudSyncAdapter,
    ) -> None:
        self._indexer = indexer
        self._cloud_sync = cloud_sync

    def count_all(self) -> JobCounts:
        """Return per-subsystem dead-letter counts + total."""
        indexer = int(self._indexer.count())
        cloud_sync = int(self._cloud_sync.count())
        return JobCounts(
            indexer=indexer,
            cloud_sync=cloud_sync,
            total=indexer + cloud_sync,
        )

    def failed(
        self,
        subsystem: SubsystemKey | None,
        *,
        limit: int = _DEFAULT_LIMIT,
        offset: int = 0,
    ) -> list[FailedJobRow]:
        """Return failed-job rows for ``subsystem``, or both when None.

        ``offset`` is honoured client-side: the underlying adapters
        only know about ``limit`` (full per-subsystem cap), so we
        fetch ``limit + offset`` and slice. ``limit`` is clamped to
        ``_HARD_LIMIT``.
        """
        clamped = max(1, min(limit, _HARD_LIMIT))
        skip = max(0, offset)
        fetch = clamped + skip
        if subsystem is None:
            rows = self._indexer.list_rows(fetch) + self._cloud_sync.list_rows(fetch)
        elif subsystem is SubsystemKey.INDEXER:
            rows = self._indexer.list_rows(fetch)
        elif subsystem is SubsystemKey.CLOUD_SYNC:
            rows = self._cloud_sync.list_rows(fetch)
        else:  # pragma: no cover — enum is exhaustive
            raise JobsServiceError(f"unknown subsystem: {subsystem!r}")
        return rows[skip : skip + clamped]

    def retry(self, subsystem: SubsystemKey, row_id: str | None) -> RetryOutcome:
        """Reset failed/dead-letter rows so the worker picks them up.

        ``row_id`` of ``None`` retries every row in the subsystem.
        """
        if subsystem is SubsystemKey.INDEXER:
            return RetryOutcome(rows_reset=int(self._indexer.retry(row_id)))
        if subsystem is SubsystemKey.CLOUD_SYNC:
            return RetryOutcome(rows_reset=int(self._cloud_sync.retry(row_id)))
        raise JobsServiceError(f"unknown subsystem: {subsystem!r}")

    def delete(self, subsystem: SubsystemKey, row_id: str | None) -> DeleteOutcome:
        """Permanently delete failed/dead-letter rows.

        Does NOT touch the underlying source file on disk; producers
        (watcher, boot scan) may re-enqueue legitimately later.
        """
        if subsystem is SubsystemKey.INDEXER:
            return DeleteOutcome(rows_deleted=int(self._indexer.delete(row_id)))
        if subsystem is SubsystemKey.CLOUD_SYNC:
            return DeleteOutcome(rows_deleted=int(self._cloud_sync.delete(row_id)))
        raise JobsServiceError(f"unknown subsystem: {subsystem!r}")

    @staticmethod
    def redact(message: str | None) -> str:
        """Public re-export of the path/credential redactor."""
        return redact_last_error(message)


def make_jobs_service(
    *,
    mapping_service: object | None,
    cloud_archive_service: CloudSyncAdapterProtocol | None,
) -> JobsService:
    """Factory used by the Flask app factory.

    ``mapping_service`` is accepted for backwards-compatibility and
    only forwarded to the indexer adapter — the Rust worker now owns
    indexing so this seam is a stub. Both arguments are optional so
    tests can stand the service up with whichever subset of backing
    services they need.
    """
    return JobsService(
        indexer=IndexerAdapter(mapping_service),
        cloud_sync=CloudSyncAdapter(cloud_archive_service),
    )


__all__ = (
    "CloudSyncAdapter",
    "CloudSyncAdapterProtocol",
    "DeleteOutcome",
    "FailedJobRow",
    "IndexerAdapter",
    "JobCounts",
    "JobsService",
    "JobsServiceError",
    "Recommendation",
    "RetryOutcome",
    "SubsystemKey",
    "ValueTier",
    "classify_clip_value",
    "classify_recommendation",
    "make_jobs_service",
    "redact_last_error",
)
