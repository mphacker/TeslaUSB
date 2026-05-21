"""Cleanup preview, execution, orphan purge, and history routes."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Final, cast

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from teslausb_web.services.cleanup import (
    CleanupConfigError,
    CleanupError,
    CleanupPreview,
    CleanupRun,
    CleanupRunStatus,
    CleanupService,
)

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

cleanup_bp = Blueprint("cleanup", __name__)
_XHR_HEADER_VALUE: Final[str] = "XMLHttpRequest"
_BYTES_PER_GIB = 1024**3


def _get_service() -> CleanupService:
    service = current_app.extensions.get("cleanup_service")
    if not isinstance(service, CleanupService):
        raise RuntimeError("cleanup_service extension is not configured")
    return service


def _wants_json_response() -> bool:
    return request.headers.get("X-Requested-With") == _XHR_HEADER_VALUE or request.is_json


def _json_error_payload(message: str) -> Response:
    return jsonify({"success": False, "error": message})


def _serialize_orphans(preview: CleanupPreview) -> dict[str, object]:
    orphan_scan = preview.orphan_scan
    if orphan_scan is None:
        return {
            "db_only_paths": [],
            "fs_only_paths": [],
            "total_bytes_recoverable": 0,
            "total_gib_recoverable": 0.0,
        }
    return {
        "db_only_paths": list(orphan_scan.db_only_paths),
        "fs_only_paths": list(orphan_scan.fs_only_paths),
        "total_bytes_recoverable": orphan_scan.total_bytes_recoverable,
        "total_gib_recoverable": round(orphan_scan.total_bytes_recoverable / _BYTES_PER_GIB, 2),
    }


def _serialize_preview(preview: CleanupPreview) -> dict[str, object]:
    return {
        "counts_by_category": dict(preview.counts_by_category),
        "bytes_total": preview.bytes_total,
        "bytes_total_gib": round(preview.bytes_total / _BYTES_PER_GIB, 2),
        "sample_paths": list(preview.sample_paths),
        "generated_at": preview.generated_at.isoformat(),
        "current_free_pct": round(preview.current_free_pct, 2),
        "projected_free_pct": round(preview.projected_free_pct, 2),
        "current_free_bytes": preview.current_free_bytes,
        "current_used_bytes": preview.current_used_bytes,
        "total_capacity_bytes": preview.total_capacity_bytes,
        "bytes_by_category": dict(preview.bytes_by_category),
        "candidate_count": preview.candidate_count,
        "protected_count": preview.protected_count,
        "orphans": _serialize_orphans(preview),
    }


def _serialize_run(run: CleanupRun) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "status": run.status,
        "action": run.action,
        "dry_run": run.dry_run,
        "started_at": run.started_at.isoformat(),
        "finished_at": None if run.finished_at is None else run.finished_at.isoformat(),
        "deleted_count": run.deleted_count,
        "deleted_bytes": run.deleted_bytes,
        "deleted_gib": round(run.deleted_bytes / _BYTES_PER_GIB, 2),
        "errors": list(run.errors),
        "policy_snapshot": dict(run.policy_snapshot),
        "counts_by_category": dict(run.counts_by_category),
        "sample_paths": list(run.sample_paths),
        "generated_at": None if run.generated_at is None else run.generated_at.isoformat(),
        "current_path": run.current_path,
        "total_candidates": run.total_candidates,
        "processed_candidates": run.processed_candidates,
        "orphan_scan": None
        if run.orphan_scan is None
        else {
            "db_only_paths": list(run.orphan_scan.db_only_paths),
            "fs_only_paths": list(run.orphan_scan.fs_only_paths),
            "total_bytes_recoverable": run.orphan_scan.total_bytes_recoverable,
        },
    }


def _error_response(exc: CleanupConfigError | CleanupError) -> ResponseReturnValue:
    status = HTTPStatus.BAD_REQUEST if isinstance(exc, CleanupConfigError) else HTTPStatus.CONFLICT
    if _wants_json_response():
        return _json_error_payload(str(exc)), status
    flash(str(exc), "error")
    return cast("Response", redirect(url_for("cleanup.preview")))


@cleanup_bp.route("/cleanup/preview")
def preview() -> ResponseReturnValue:
    try:
        preview_payload = _get_service().preview()
    except (CleanupConfigError, CleanupError) as exc:
        return _error_response(exc)
    if _wants_json_response():
        payload = {"success": True, "preview": _serialize_preview(preview_payload)}
        return jsonify(payload), HTTPStatus.OK
    return render_template(
        "cleanup_preview.html",
        page="settings",
        storage_retention_available=True,
        preview=preview_payload,
    )


@cleanup_bp.route("/api/cleanup/preview")
def preview_api() -> ResponseReturnValue:
    return preview()


@cleanup_bp.route("/cleanup/execute", methods=["POST"])
def execute() -> ResponseReturnValue:
    try:
        run_id = _get_service().start_execute(dry_run=_request_bool("dry_run"))
    except (CleanupConfigError, CleanupError) as exc:
        return _error_response(exc)
    if _wants_json_response():
        return (
            jsonify(
                {
                    "success": True,
                    "run_id": run_id,
                    "status_url": url_for("cleanup.run_status", run_id=run_id),
                    "report_url": url_for("cleanup.report", run_id=run_id),
                }
            ),
            HTTPStatus.ACCEPTED,
        )
    flash("Cleanup run started", "success")
    return cast("Response", redirect(url_for("cleanup.report", run_id=run_id)))


@cleanup_bp.route("/api/cleanup/execute", methods=["POST"])
def execute_api() -> ResponseReturnValue:
    return execute()


@cleanup_bp.route("/cleanup/orphans/purge", methods=["POST"])
def purge_orphans() -> ResponseReturnValue:
    try:
        run = _get_service().purge_orphans()
    except (CleanupConfigError, CleanupError) as exc:
        return _error_response(exc)
    if _wants_json_response():
        return jsonify({"success": True, "run": _serialize_run(run)}), HTTPStatus.OK
    flash("Orphan purge completed", "success" if not run.errors else "warning")
    return cast("Response", redirect(url_for("cleanup.report", run_id=run.run_id)))


@cleanup_bp.route("/api/cleanup/orphans/purge", methods=["POST"])
def purge_orphans_api() -> ResponseReturnValue:
    return purge_orphans()


@cleanup_bp.route("/cleanup/report")
def report() -> ResponseReturnValue:
    service = _get_service()
    report_payload = service.report()
    selected_status: CleanupRunStatus | None = None
    run_id = request.args.get("run_id", "")
    if run_id:
        try:
            selected_status = service.get_run_status(run_id)
        except CleanupError:
            selected_status = None
    elif report_payload.recent_runs:
        selected_status = CleanupRunStatus(run=report_payload.recent_runs[0], active=False)
    if _wants_json_response():
        return (
            jsonify(
                {
                    "success": True,
                    "selected_run": (
                        None if selected_status is None else _serialize_run(selected_status.run)
                    ),
                    "active": False if selected_status is None else selected_status.active,
                    "recent_runs": [_serialize_run(run) for run in report_payload.recent_runs],
                }
            ),
            HTTPStatus.OK,
        )
    return render_template(
        "cleanup_report.html",
        page="settings",
        storage_retention_available=True,
        selected_run=None if selected_status is None else selected_status.run,
        selected_active=False if selected_status is None else selected_status.active,
        recent_runs=report_payload.recent_runs,
        poll_url=(
            None
            if selected_status is None
            else url_for("cleanup.run_status", run_id=selected_status.run.run_id)
        ),
    )


@cleanup_bp.route("/api/cleanup/report")
def report_api() -> ResponseReturnValue:
    return report()


@cleanup_bp.route("/api/cleanup/runs/<run_id>")
def run_status(run_id: str) -> ResponseReturnValue:
    try:
        run_status_payload = _get_service().get_run_status(run_id)
    except CleanupError as exc:
        return _json_error_payload(str(exc)), HTTPStatus.NOT_FOUND
    return (
        jsonify(
            {
                "success": True,
                "active": run_status_payload.active,
                "run": _serialize_run(run_status_payload.run),
            }
        ),
        HTTPStatus.OK,
    )


def _request_bool(name: str) -> bool | None:
    payload = request.get_json(silent=True)
    if isinstance(payload, dict) and name in payload:
        value = payload[name]
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "on", "yes"}
        raise CleanupConfigError(f"{name} must be a boolean")
    value = request.form.get(name)
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "on", "yes"}
