"""Typed value objects for the unified Failed Jobs service.

Frozen dataclasses + a ``SubsystemKey`` enum keep the service /
blueprint boundary strictly typed — no ``dict[str, Any]`` payloads
flow across the layer (charter §"Type discipline").
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SubsystemKey(StrEnum):
    """Allowed subsystem identifiers on the Failed Jobs API.

    v1 unified three subsystems (``archive``, ``indexer``,
    ``cloud_sync``). B-1 drops ``archive`` because cleanup is now a
    fire-and-forget filesystem move with no queue / dead-letter
    layer (see ``docs/00-PLAN.md`` "no IMG/loopback" invariant).
    """

    INDEXER = "indexer"
    CLOUD_SYNC = "cloud_sync"


@dataclass(frozen=True, slots=True)
class ValueTier:
    """Operator-facing "what is this clip" tier.

    Deterministic; pure function of subsystem + identifier path
    fragments. The UI maps ``tier`` to a coloured badge.
    """

    tier: str
    label: str
    description: str


@dataclass(frozen=True, slots=True)
class Recommendation:
    """Operator-facing "retry vs delete" hint.

    ``action`` is one of ``"retry"``, ``"delete"``, ``"either"``.
    ``reason`` is a short human-readable sentence (≤120 chars).
    """

    action: str
    reason: str


@dataclass(frozen=True, slots=True)
class FailedJobRow:
    """One failed-job row as surfaced to the Failed Jobs UI."""

    subsystem: SubsystemKey
    row_id: str
    identifier: str
    attempts: int
    last_error: str
    previous_last_error: str
    value: ValueTier
    recommendation: Recommendation


@dataclass(frozen=True, slots=True)
class JobCounts:
    """Per-subsystem dead-letter counts + grand total."""

    indexer: int
    cloud_sync: int
    total: int


@dataclass(frozen=True, slots=True)
class RetryOutcome:
    """Result of a retry call (number of rows reset)."""

    rows_reset: int


@dataclass(frozen=True, slots=True)
class DeleteOutcome:
    """Result of a delete call (number of rows removed)."""

    rows_deleted: int
