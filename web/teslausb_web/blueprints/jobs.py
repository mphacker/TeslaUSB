"""Failed Jobs blueprint (Phase 5.27 — B-1 port of v1 #101).

Aggregates dead-letter / failed rows from B-1's background
subsystems into one place so the operator can see — and recover
from — every failure without clicking through five different pages.

B-1 deviation from v1
=====================

v1 unified three subsystems: ``archive``, ``indexer``, ``cloud_sync``.
B-1 ships **only** ``indexer`` and ``cloud_sync`` — the v1
``archive`` queue (move-to-ArchivedClips dead-letter) does not exist
in B-1 because the cleanup pipeline is a fire-and-forget filesystem
move with no queue layer (see ``docs/00-PLAN.md`` "no IMG/loopback"
invariant). The Failed Jobs UI never shows an ``archive`` filter
pill on B-1.

Routes (URLs match v1 exactly so existing links resolve):
    * ``GET  /jobs``                  — HTML shell, JS fills the rest.
    * ``GET  /api/jobs/counts``       — JSON ``{indexer, cloud_sync, total}``.
    * ``GET  /api/jobs/failed``       — JSON rows; query ``subsystem``,
                                        ``limit``, ``offset``.
    * ``POST /api/jobs/retry``        — body ``{subsystem, row_id}``.
    * ``POST /api/jobs/delete``       — body ``{subsystem, row_id}``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING

from flask import Blueprint, current_app, jsonify, render_template, request

from teslausb_web.services.jobs_service import (
    FailedJobRow,
    JobCounts,
    JobsService,
    JobsServiceError,
    SubsystemKey,
)

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

logger = logging.getLogger(__name__)

jobs_bp = Blueprint("jobs", __name__)

_DEFAULT_LIMIT: int = 100
_HARD_LIMIT: int = 1000


def _get_service() -> JobsService:
    svc = current_app.extensions.get("jobs_service")
    if not isinstance(svc, JobsService):
        raise RuntimeError("jobs_service extension is not configured")
    return svc


def _parse_subsystem(value: str | None, *, allow_none: bool) -> SubsystemKey | None:
    """Parse a raw subsystem string into the enum.

    ``allow_none`` controls whether an absent / blank / ``"all"``
    value yields ``None`` (the all-subsystems sentinel) or a 400.
    """
    if value is None or value == "" or value.lower() == "all":
        if allow_none:
            return None
        raise JobsServiceError("missing subsystem")
    try:
        return SubsystemKey(value.lower())
    except ValueError as exc:
        raise JobsServiceError(f"unknown subsystem: {value!r}") from exc


def _parse_limit(raw: str | None, default: int = _DEFAULT_LIMIT) -> int:
    try:
        n = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, _HARD_LIMIT))


def _parse_offset(raw: str | None) -> int:
    try:
        n = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        n = 0
    return max(0, n)


@dataclass(frozen=True, slots=True)
class _MutationRequest:
    """Typed parse of the JSON body for /retry and /delete."""

    subsystem: SubsystemKey
    row_id: str | None


def _parse_mutation_body() -> _MutationRequest:
    """Parse the JSON body for /retry and /delete.

    Raises ``JobsServiceError`` on bad input so the handler can
    return a 400 without touching the service.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise JobsServiceError("body must be a JSON object")
    raw_subsystem = payload.get("subsystem")
    if not isinstance(raw_subsystem, str):
        raise JobsServiceError("missing subsystem")
    subsystem = _parse_subsystem(raw_subsystem, allow_none=False)
    if subsystem is None:  # pragma: no cover — allow_none=False guards
        raise JobsServiceError("missing subsystem")
    raw_row_id = payload.get("row_id")
    if raw_row_id is None:
        row_id: str | None = None
    elif isinstance(raw_row_id, (str, int)):
        row_id = str(raw_row_id)
    else:
        raise JobsServiceError("row_id must be a string, int, or null")
    return _MutationRequest(subsystem=subsystem, row_id=row_id)


def _serialize_row(row: FailedJobRow) -> dict[str, object]:
    return {
        "subsystem": row.subsystem.value,
        "row_id": row.row_id,
        "identifier": row.identifier,
        "attempts": row.attempts,
        "last_error": row.last_error,
        "previous_last_error": row.previous_last_error,
        "value": {
            "tier": row.value.tier,
            "label": row.value.label,
            "description": row.value.description,
        },
        "recommendation": {
            "action": row.recommendation.action,
            "reason": row.recommendation.reason,
        },
    }


def _serialize_counts(counts: JobCounts) -> dict[str, int]:
    return {
        "indexer": counts.indexer,
        "cloud_sync": counts.cloud_sync,
        "total": counts.total,
    }


@jobs_bp.route("/jobs", methods=["GET"], endpoint="failed_jobs_page")
def failed_jobs_page() -> ResponseReturnValue:
    """Render the Failed Jobs page shell.

    The page polls the JSON endpoints client-side so the HTML
    handler does no DB I/O and renders instantly even when one
    subsystem is slow or unavailable.
    """
    return render_template(
        "failed_jobs.html",
        page="jobs",
        subsystems=[s.value for s in SubsystemKey],
    )


@jobs_bp.route("/api/jobs/counts", methods=["GET"], endpoint="api_counts")
def api_counts() -> ResponseReturnValue:
    """Return per-subsystem dead-letter counts."""
    svc = _get_service()
    counts = svc.count_all()
    return jsonify(_serialize_counts(counts))


@jobs_bp.route("/api/jobs/failed", methods=["GET"], endpoint="api_failed")
def api_failed() -> ResponseReturnValue:
    """Return failed-job rows for one subsystem (or all when omitted)."""
    svc = _get_service()
    raw_subsystem = request.args.get("subsystem")
    try:
        subsystem = _parse_subsystem(raw_subsystem, allow_none=True)
    except JobsServiceError as exc:
        return _bad_request(str(exc))
    limit = _parse_limit(request.args.get("limit"))
    offset = _parse_offset(request.args.get("offset"))
    rows = svc.failed(subsystem, limit=limit, offset=offset)
    return jsonify(
        {
            "subsystem": subsystem.value if subsystem else "all",
            "count": len(rows),
            "rows": [_serialize_row(r) for r in rows],
        }
    )


@jobs_bp.route("/api/jobs/retry", methods=["POST"], endpoint="api_retry")
def api_retry() -> ResponseReturnValue:
    """Reset failed/dead-letter rows so the worker picks them up."""
    svc = _get_service()
    try:
        parsed = _parse_mutation_body()
    except JobsServiceError as exc:
        return _bad_request(str(exc))
    try:
        outcome = svc.retry(parsed.subsystem, parsed.row_id)
    except JobsServiceError as exc:
        logger.warning("/api/jobs/retry: %s", exc)
        return _bad_request(str(exc))
    return jsonify({"subsystem": parsed.subsystem.value, "retried": outcome.rows_reset})


@jobs_bp.route("/api/jobs/delete", methods=["POST"], endpoint="api_delete")
def api_delete() -> ResponseReturnValue:
    """Permanently delete failed/dead-letter rows.

    Does NOT delete the underlying source file from disk — producers
    may legitimately re-enqueue the same source later.
    """
    svc = _get_service()
    try:
        parsed = _parse_mutation_body()
    except JobsServiceError as exc:
        return _bad_request(str(exc))
    try:
        outcome = svc.delete(parsed.subsystem, parsed.row_id)
    except JobsServiceError as exc:
        logger.warning("/api/jobs/delete: %s", exc)
        return _bad_request(str(exc))
    return jsonify({"subsystem": parsed.subsystem.value, "deleted": outcome.rows_deleted})


def _bad_request(message: str) -> ResponseReturnValue:
    return (
        jsonify(
            {
                "error": message,
                "allowed": [s.value for s in SubsystemKey],
            }
        ),
        HTTPStatus.BAD_REQUEST,
    )
