"""Legacy-compat ``/api/*`` blueprint.

v1 shipped a small set of ``/api/*`` endpoints used by external
scripts (Tesla phone-home, NetworkManager dispatchers, ad-hoc
monitoring tools). Their URLs must keep returning **valid JSON**
under B-1 even when the underlying subsystem has been redesigned —
breaking these URLs silently is unacceptable (charter §"User-visible
contracts").

Per-route disposition (Phase 5.28 dedupe survey):

* ``GET  /api/operation_status`` — **shim**. v1 reported whether the
  IMG-mount-cycle was in progress. B-1 has no IMG / loopback /
  mount-cycle (``docs/00-PLAN.md`` "no IMG" invariant), so the
  operation is *never* in progress. Returns a stable ``{...}`` body
  so polling clients see a steady ``in_progress=False`` instead of
  HTTP errors.
* ``GET  /api/chime_filenames`` — **shim** → ``lock_chime_service``.
  Used by external scripts to avoid filename collisions before
  upload. Body shape matches Phase 5.8's ``/lock_chimes/`` JSON:
  ``{"chime_filenames": [...]}``.
* ``POST /api/rename_chime/<old>/<new>`` — **shim** →
  :func:`lock_chime_service.rename_chime_file`. Same URL as v1 so
  external scripts continue to work; ``ValueError`` →
  ``400``, ``FileNotFoundError`` → ``404``, ``FileExistsError`` →
  ``409``, I/O error → ``500``.
* ``GET  /api/gadget_state`` — **dropped**. v1 probed configfs for
  the USB gadget. B-1's Rust ``teslafat`` daemon owns the gadget
  lifecycle but has no IPC for "report current state" yet. Returns
  ``503 {"error":"not_implemented","phase":"6"}`` so the URL stays
  alive but callers see an explicit unavailable signal.
* ``POST /api/recent_archive/trigger`` — **dropped**. v1's
  one-shot "RecentClips → SD-card archive" subsystem does not
  exist in B-1 — there is no IMG / loopback to archive into. The
  cloud-archive worker (Phase 5.14 / 5.18) handles ongoing sync.
  Returns ``501 {"error":"not_implemented","reason":"..."}``.
* ``GET  /api/recent_archive/status`` — **dropped** (mirrors
  trigger). Returns ``501``.
* ``POST /api/recover_gadget`` — **dropped (deprecated)**. Gadget
  recovery is the Rust worker's job in B-1. Returns ``410 Gone``
  with ``{"error":"deprecated", ...}``.

Charter posture:

* Module logger (no ``print``, no bare ``except``).
* No ``Any``; structured response bodies are :class:`TypedDict`
  instances built by named factories so the JSON shape lives in
  one place per route (charter §"Stringly-typed code").
* All public route functions return :data:`flask.typing.ResponseReturnValue`.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, Final, TypedDict, cast

from flask import Blueprint, current_app, jsonify

from teslausb_web.services.lock_chime_service import (
    LockChimeFileError,
    list_chime_files,
    rename_chime_file,
)

if TYPE_CHECKING:
    from pathlib import Path

    from flask.typing import ResponseReturnValue

    from teslausb_web.config import WebConfig


logger = logging.getLogger(__name__)

api_bp = Blueprint("api", __name__, url_prefix="/api")

_LIGHTSHOW_DIRNAME: Final[str] = "lightshow"
_WAV_SUFFIX: Final[str] = ".wav"


# --- Structured response bodies -------------------------------------------------


class OperationStatus(TypedDict):
    """Body of ``GET /api/operation_status``.

    Stable shape so polling JS sees the same keys whether B-1 ever
    grows a notion of "operation in progress" or not. ``operation``
    is ``None`` whenever ``in_progress`` is ``False``.
    """

    in_progress: bool
    operation: str | None
    message: str


class ChimeFilenameList(TypedDict):
    """Body of ``GET /api/chime_filenames``."""

    chime_filenames: list[str]


class RenameChimeOk(TypedDict):
    """Body of a successful ``POST /api/rename_chime/...``."""

    success: bool
    old: str
    new: str


class ApiError(TypedDict):
    """Body shared by every deprecated / unavailable route.

    ``error`` is a stable machine-readable token (``not_implemented``,
    ``deprecated``); ``reason`` is human-readable; ``phase`` /
    ``tracking_issue`` document deferred work so the operator can
    follow the trail without grepping source.
    """

    error: str
    reason: str
    phase: str | None
    tracking_issue: str | None


def _api_error(
    *,
    error: str,
    reason: str,
    phase: str | None = None,
    tracking_issue: str | None = None,
) -> ApiError:
    """Build a typed :class:`ApiError` body."""
    return {
        "error": error,
        "reason": reason,
        "phase": phase,
        "tracking_issue": tracking_issue,
    }


# --- Service-locator helpers ---------------------------------------------------


def _cfg() -> WebConfig:
    return cast("WebConfig", current_app.config["teslausb_config"])


def _chimes_dir() -> Path:
    cfg = _cfg()
    return cfg.paths.backing_root / _LIGHTSHOW_DIRNAME / cfg.chimes.chimes_folder


# --- Routes --------------------------------------------------------------------


@api_bp.route("/operation_status", methods=["GET"])
def operation_status() -> ResponseReturnValue:
    """Report whether a long-running file operation is in progress.

    **B-1 disposition: thin shim, always ``in_progress=False``.**

    v1 used this endpoint to gate the UI's auto-refresh while the
    web app was busy unmounting / re-mounting the IMG file. B-1 has
    no IMG file and no loopback subsystem, so there is no equivalent
    "operation in progress" condition. The endpoint is preserved so
    external scripts that poll it (Tesla phone-home, dispatcher
    scripts) keep working.
    """
    body: OperationStatus = {
        "in_progress": False,
        "operation": None,
        "message": "B-1 has no IMG-mount-cycle; no operation can be in progress.",
    }
    return jsonify(body)


@api_bp.route("/chime_filenames", methods=["GET"])
def chime_filenames() -> ResponseReturnValue:
    """List existing lock-chime filenames as plain JSON.

    **B-1 disposition: thin shim →
    :func:`lock_chime_service.list_chime_files`.**

    Body shape matches the Phase 5.8 lock-chimes JSON
    (``{"chime_filenames": [...]}``) so callers that already speak
    the new shape don't need a special case. On any I/O failure
    the list is empty rather than 500 — external scripts that poll
    this before upload are happier with an empty allowlist than a
    crashed response.
    """
    filenames: list[str] = []
    chimes_dir = _chimes_dir()
    try:
        infos = list_chime_files(chimes_dir)
    except LockChimeFileError:
        logger.exception("Failed to enumerate chimes at %s", chimes_dir)
    else:
        filenames = [info.name for info in infos]
    body: ChimeFilenameList = {"chime_filenames": filenames}
    return jsonify(body)


@api_bp.route("/rename_chime/<old_filename>/<new_filename>", methods=["POST"])
def rename_chime(old_filename: str, new_filename: str) -> ResponseReturnValue:
    """Rename a library chime in place (no re-encode).

    **B-1 disposition: thin shim →
    :func:`lock_chime_service.rename_chime_file`.**

    Service-level errors are mapped to HTTP codes:

    * :class:`ValueError`         → ``400`` (unsafe / malformed name)
    * :class:`FileNotFoundError`  → ``404``
    * :class:`FileExistsError`    → ``409``
    * :class:`LockChimeFileError` → ``500``
    """
    chimes_dir = _chimes_dir()
    try:
        rename_chime_file(old_filename, new_filename, chimes_dir)
    except FileNotFoundError as exc:
        return jsonify(
            _api_error(error="not_found", reason=str(exc)),
        ), HTTPStatus.NOT_FOUND
    except FileExistsError as exc:
        return jsonify(
            _api_error(error="conflict", reason=str(exc)),
        ), HTTPStatus.CONFLICT
    except ValueError as exc:
        return jsonify(
            _api_error(error="bad_request", reason=str(exc)),
        ), HTTPStatus.BAD_REQUEST
    except LockChimeFileError as exc:
        logger.exception("Rename failed: %s -> %s", old_filename, new_filename)
        return jsonify(
            _api_error(error="io_error", reason=str(exc)),
        ), HTTPStatus.INTERNAL_SERVER_ERROR
    body: RenameChimeOk = {
        "success": True,
        "old": old_filename,
        "new": new_filename,
    }
    return jsonify(body)


@api_bp.route("/gadget_state", methods=["GET"])
def gadget_state() -> ResponseReturnValue:
    """Report the current USB-gadget configfs state.

    **B-1 disposition: dropped → 503 Service Unavailable.**

    v1 probed configfs directly. In B-1 the Rust ``teslafat`` daemon
    owns the gadget lifecycle (ADR-0007), but the daemon does not
    yet expose a ``GetGadgetState`` IPC method — see
    https://github.com/mphacker/TeslaUSB/issues/226 (Phase 6). The URL is kept so
    callers see an explicit ``not_implemented`` token rather than a
    404, but the status code is 503 to signal "try again later".
    """
    body = _api_error(
        error="not_implemented",
        reason="B-1 teslafat daemon does not yet expose a gadget-state IPC method.",
        phase="6",
    )
    return jsonify(body), HTTPStatus.SERVICE_UNAVAILABLE


@api_bp.route("/recent_archive/trigger", methods=["POST"])
def recent_archive_trigger() -> ResponseReturnValue:
    """Start a one-shot "RecentClips → SD-card archive" run.

    **B-1 disposition: dropped → 501 Not Implemented.**

    v1's recent-archive subsystem copied RecentClips into the
    loopback IMG so a phone-home script could then ``rclone sync``
    that IMG. B-1 has no IMG (``docs/00-PLAN.md``) and no
    intermediate archive step — the ``cloud_archive`` worker
    (Phase 5.14 / 5.18) syncs RecentClips directly. The trigger URL
    is preserved so legacy dispatcher scripts see an explicit
    ``not_implemented`` token.
    """
    body = _api_error(
        error="not_implemented",
        reason=(
            "B-1 has no RecentClips→IMG archive step; cloud_archive worker "
            "syncs RecentClips directly. Use /cloud_archive/sync_now instead."
        ),
    )
    return jsonify(body), HTTPStatus.NOT_IMPLEMENTED


@api_bp.route("/recent_archive/status", methods=["GET"])
def recent_archive_status() -> ResponseReturnValue:
    """Report the current recent-archive run status.

    **B-1 disposition: dropped → 501 Not Implemented (mirrors
    trigger).**
    """
    body = _api_error(
        error="not_implemented",
        reason="No recent-archive subsystem in B-1; see /api/recent_archive/trigger.",
    )
    return jsonify(body), HTTPStatus.NOT_IMPLEMENTED


@api_bp.route("/recover_gadget", methods=["POST"])
def recover_gadget() -> ResponseReturnValue:
    """Manually attempt USB-gadget recovery.

    **B-1 disposition: dropped → 410 Gone.**

    v1 attempted to repair a hung configfs gadget from the web app.
    In B-1 the Rust ``teslausb-worker`` owns the gadget supervisor
    and the systemd unit restart policy handles recovery
    autonomously (ADR-0006). A user-triggered web-side "recover"
    button is not just unnecessary but actively harmful (could
    race the supervisor). The URL is kept so external scripts
    that still call it see an unambiguous ``deprecated`` signal.
    """
    body = _api_error(
        error="deprecated",
        reason="B-1 Rust worker owns gadget recovery; manual web-side trigger removed.",
    )
    return jsonify(body), HTTPStatus.GONE


__all__ = ("api_bp",)
