"""AC.6 — Storage settings blueprint.

Renders a single settings page (LUN sizes + cleanup knobs) and
applies submitted changes via :mod:`storage_stats` /
:mod:`storage_config`. The blueprint is intentionally thin —
all validation, persistence, and resize-helper invocation live
in the service layer. This file just brokers HTTP ↔ services.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, cast

from flask import (
    Blueprint,
    Response,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from teslausb_web.services import storage_config as sc
from teslausb_web.services import storage_stats as ss

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

logger = logging.getLogger(__name__)

storage_bp = Blueprint("storage", __name__)


def _safe_int(value: str | None, *, field: str) -> int:
    if value is None or value.strip() == "":
        raise sc.StorageConfigError(f"{field} is required")
    try:
        return int(value)
    except ValueError as exc:
        raise sc.StorageConfigError(f"{field} must be an integer") from exc


def _form_to_config(form: dict[str, str], current: sc.TeslausbConfig) -> sc.TeslausbConfig:
    """Build a new ``TeslausbConfig`` from the POSTed form.

    Fields not present fall back to the loaded ``current`` values,
    so partial UI submissions don't reset unrelated knobs.
    """
    storage = sc.StorageSection(
        os_reserve_gb=_safe_int(
            form.get("os_reserve_gb", str(current.storage.os_reserve_gb)),
            field="os_reserve_gb",
        ),
        teslacam_gb=_safe_int(
            form.get("teslacam_gb", str(current.storage.teslacam_gb)),
            field="teslacam_gb",
        ),
        media_gb=_safe_int(
            form.get("media_gb", str(current.storage.media_gb)),
            field="media_gb",
        ),
    )
    cleanup = sc.CleanupSection(
        target_free_pct=_safe_int(
            form.get("target_free_pct", str(current.cleanup.target_free_pct)),
            field="target_free_pct",
        ),
        sentry_max_age_days=_safe_int(
            form.get("sentry_max_age_days", str(current.cleanup.sentry_max_age_days)),
            field="sentry_max_age_days",
        ),
        preserve_with_gps="preserve_with_gps" in form
        if any(k in form for k in ("preserve_with_gps", "_preserve_with_gps_present"))
        else current.cleanup.preserve_with_gps,
    )
    return sc.TeslausbConfig(storage=storage, cleanup=cleanup)


def _redirect_index() -> Response:
    return cast("Response", redirect(url_for("storage.index")))


def _context() -> dict[str, object]:
    config = sc.load() if sc.DEFAULT_CONFIG_PATH.exists() else sc.default_config()
    stats = ss.get_storage_stats(config=config)
    return {
        "page": "storage",
        "auto_refresh": False,
        "operation_in_progress": False,
        "stats": stats,
        "config": config,
    }


@storage_bp.route("/storage")
@storage_bp.route("/storage/")
def index() -> ResponseReturnValue:
    return render_template("storage_settings.html", **_context())


@storage_bp.route("/storage", methods=["POST"])
def save() -> ResponseReturnValue:
    try:
        current = sc.load() if sc.DEFAULT_CONFIG_PATH.exists() else sc.default_config()
        new_config = _form_to_config(dict(request.form.items()), current)
    except sc.StorageConfigError as exc:
        flash(str(exc), "error")
        return _redirect_index(), HTTPStatus.BAD_REQUEST

    try:
        messages = ss.apply_storage_config(new_config)
    except sc.StorageConfigError as exc:
        flash(f"Configuration rejected: {exc}", "error")
        return _redirect_index(), HTTPStatus.BAD_REQUEST
    except ss.ApplyError as exc:
        logger.error("storage: apply failed: %s", exc)
        flash(f"Resize helper failed: {exc}", "error")
        return _redirect_index(), HTTPStatus.INTERNAL_SERVER_ERROR

    if messages:
        for msg in messages:
            flash(msg, "success")
    else:
        flash("No changes to apply.", "info")
    return _redirect_index()
