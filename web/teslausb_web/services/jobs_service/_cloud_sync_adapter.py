"""Adapter: cloud_archive dead-letters → ``FailedJobRow`` rows.

Direct mapping over :class:`CloudArchiveService`'s existing
``list_dead_letters`` / ``retry_dead_letter`` / ``delete_dead_letter``
trio (see ``services/cloud_archive/queue_ops.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from teslausb_web.services.jobs_service._classifier import (
    classify_clip_value,
    classify_recommendation,
)
from teslausb_web.services.jobs_service._models import (
    FailedJobRow,
    SubsystemKey,
)
from teslausb_web.services.jobs_service._redactor import redact_last_error

if TYPE_CHECKING:
    from teslausb_web.services.cloud_archive_queries import DeadLetterEntry


class CloudSyncAdapterProtocol:
    """Structural type the adapter expects from a cloud-archive service.

    Declared as a class (rather than ``typing.Protocol``) so the
    runtime ``isinstance`` checks in the JobsService factory have
    something concrete to test, while still allowing the production
    :class:`CloudArchiveService` to satisfy it by shape. Tests can
    pass a fake.
    """

    def list_dead_letters(self, limit: int = 100) -> tuple[DeadLetterEntry, ...]:
        raise NotImplementedError

    def count_dead_letters(self) -> int:
        raise NotImplementedError

    def retry_dead_letter(self, file_path: str | None = None) -> int:
        raise NotImplementedError

    def delete_dead_letter(self, file_path: str | None = None) -> int:
        raise NotImplementedError


class CloudSyncAdapter:
    """Adapts cloud-archive dead-letters to the Failed Jobs row contract."""

    def __init__(self, cloud_archive_service: CloudSyncAdapterProtocol | None) -> None:
        self._svc = cloud_archive_service

    def list_rows(self, limit: int) -> list[FailedJobRow]:
        if self._svc is None:
            return []
        entries = self._svc.list_dead_letters(limit=limit)
        return [self._entry_to_row(entry) for entry in entries]

    def count(self) -> int:
        if self._svc is None:
            return 0
        return int(self._svc.count_dead_letters())

    def retry(self, row_id: str | None) -> int:
        if self._svc is None:
            return 0
        path = None if row_id is None else str(row_id)
        return int(self._svc.retry_dead_letter(file_path=path))

    def delete(self, row_id: str | None) -> int:
        if self._svc is None:
            return 0
        path = None if row_id is None else str(row_id)
        return int(self._svc.delete_dead_letter(file_path=path))

    @staticmethod
    def _entry_to_row(entry: DeadLetterEntry) -> FailedJobRow:
        identifier = entry.file_path or ""
        redacted = redact_last_error(entry.last_error)
        prev_redacted = redact_last_error(entry.previous_last_error)
        return FailedJobRow(
            subsystem=SubsystemKey.CLOUD_SYNC,
            row_id=identifier,
            identifier=identifier,
            attempts=int(entry.retry_count or 0),
            last_error=redacted,
            previous_last_error=prev_redacted,
            value=classify_clip_value(SubsystemKey.CLOUD_SYNC, identifier),
            recommendation=classify_recommendation(redacted, attempts=int(entry.retry_count or 0)),
        )
