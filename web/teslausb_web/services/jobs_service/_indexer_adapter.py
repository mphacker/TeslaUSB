"""Adapter: B-1 mapping (indexer) → ``FailedJobRow`` rows.

B-1 deviation from v1
=====================

v1 had a dedicated ``indexing_queue`` table with a ``dead_letter``
status plus ``retry_dead_letter`` / ``delete_dead_letter`` helpers.
B-1's mapping service (``services/mapping/``) has no such table
yet — see ``services/mapping/stale_scan.py`` for the closest
analogue (it surfaces failures via logging, not a queue).

Until that store lands this adapter returns an empty list and the
retry/delete entry points are no-ops. The Failed Jobs UI keeps the
``Indexer`` filter pill enabled because the day the underlying
store arrives we want to flip a switch here, not touch the UI.

Tracking issue: https://github.com/mphacker/TeslaUSB/issues/222
"""

from __future__ import annotations

import logging
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
    from teslausb_web.services.mapping.service import MappingService

logger = logging.getLogger(__name__)


class IndexerAdapter:
    """Adapts the mapping service into the Failed Jobs row contract.

    Currently a stub: see module docstring. The mapping service is
    accepted at construction time so the adapter has a stable
    seam — when the dead-letter store lands, only this file
    changes.
    """

    def __init__(self, mapping_service: MappingService | None) -> None:
        self._mapping_service = mapping_service

    def list_rows(self, limit: int) -> list[FailedJobRow]:  # noqa: ARG002
        # TODO(https://github.com/mphacker/TeslaUSB/issues/222):
        #   Wire to the mapping dead-letter store when B-1 grows one.
        if self._mapping_service is None:
            logger.debug("IndexerAdapter.list_rows: mapping service missing")
        return []

    def count(self) -> int:
        # TODO(https://github.com/mphacker/TeslaUSB/issues/222): count
        #   real failed scans once the mapping store exposes them.
        return 0

    def retry(self, row_id: str | None) -> int:  # noqa: ARG002
        # TODO(https://github.com/mphacker/TeslaUSB/issues/222): wire.
        return 0

    def delete(self, row_id: str | None) -> int:  # noqa: ARG002
        # TODO(https://github.com/mphacker/TeslaUSB/issues/222): wire.
        return 0

    @staticmethod
    def _build_row(
        identifier: str,
        attempts: int,
        last_error: str | None,
        previous_last_error: str | None,
    ) -> FailedJobRow:
        """Helper kept ready for when real rows start flowing.

        Held in the class so the future wiring path is short and the
        contract is type-checked even while ``list_rows`` returns ``[]``.
        """
        redacted = redact_last_error(last_error)
        prev_redacted = redact_last_error(previous_last_error)
        return FailedJobRow(
            subsystem=SubsystemKey.INDEXER,
            row_id=identifier,
            identifier=identifier,
            attempts=attempts,
            last_error=redacted,
            previous_last_error=prev_redacted,
            value=classify_clip_value(SubsystemKey.INDEXER, identifier),
            recommendation=classify_recommendation(redacted, attempts=attempts),
        )
